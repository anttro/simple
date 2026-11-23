# coding=utf-8
"""SCP81 remote administration and the generic BIP session (Phase 3c-3e-2b).

The terminal side of RAM over HTTP (GPC v2.2 Amendment B): the SCP81
listener control (``tls`` / ``dump`` / ``redirect`` / ``passthru``), the
command-script engine that answers the Security Domain's administration POSTs
with one C-APDU at a time, the PSK table of the TLS listener and the
``.cap`` -> INSTALL/LOAD/INSTALL generator.  One :class:`Scp81Control` per
:class:`~simple_server.server.ServerState` replaces the legacy server's
``_SCP81_*``/``_BIP*`` module globals; the BIP session itself (the terminal,
the channels, the redirect/sink/passthru modes and the CAT events) stays
:class:`simple_server.bip.BipSession` (Phase 3c-2), which this module drives
through ``start``/``start_listener``/``stop``/``status``.

Provenance: a behavioural port of the legacy 3.x
``server.py`` helpers ``_scp81_bip_control``,
``_bip_control``/``_bip_session_stop``/``_bip_status_body``/
``_bip_listener_status``, ``_scp81_reset_script``/``_scp81_queue_script``/
``_scp81_restart_run``/``_scp81_script_mid_run``/``_scp81_script_state``/
``_scp81_script_responder``, ``_scp81_command_body``/``_scp81_parse_response``/
``_scp81_continuation``/``_scp81_response_headers``, ``_parse_psk_map``,
``_scp81_update_psk_map`` and ``_scp81_gen_install``.  The TLS listener and
the GP HTTP dialog are the reusable, pysim-free
:mod:`simple_server.shared.scp81` (moved from the legacy tree:
:class:`~simple_server.shared.scp81.PskTlsServer`,
``parse_http_request``/``build_http_response``), the sink/dump
listener is :class:`simple_server.shared.httpota.TcpDumpServer` and the RAM
APDU builders are :mod:`simple_server.capram` - the same code the RAM install
uses, so the generated script cannot drift from the install chain.

The administration script **runs on the card**: the Security Domain fetches
the C-APDU from the response body and reports the R-APDUs in its next POST
(``X-Admin-Script-Status`` + the Response Scripting template,
TS 102 226 §5.2.2).  This module therefore does no card I/O at all - it
parses what the card reports and decides the next command; the card exchange
is the BIP terminal's (``SEND DATA``/``RECEIVE DATA``, Phase 3c-2) under the
state's card lock.

Spec references: GPC v2.2 Amendment B §4.3 (the TLS-PSK dialect: the
cipher suites of Table 4-2, TLS 1.0-1.2, no resumption), §4.4 (the HTTP
administration dialog: ``X-Admin-*``, 200 + a command string or 204 to
close), §4.7 (the Trigger/Store SMS), TS 102 226 §5.2.1 (the Command
Scripting template: ``AA`` definite / ``AE 80`` indefinite around the
C-APDU), §5.2.2/Table 5.12 (the Response Scripting template: the executed
count ``80`` and the R-APDU ``23``), §8.2.1.7.2 (GET DATA FF21) and GP Card
Specification v2.3.1 §11.4.2 (GET STATUS, ``63 10`` = more occurrences) and
§11.5/§11.6 (INSTALL/LOAD, the ``.cap`` -> APDU sequence).
"""

import re
import ssl
import threading

from simple_server.shared import httpota
from simple_server.shared import scp81 as listener_module

from simple_server import bip as bip_module
from simple_server import capram
from simple_server import tlv

__all__ = [
    'Scp81Control', 'ScriptEngine', 'LISTENER_MODES', 'OWNER_SCP81',
    'DEFAULT_PORT', 'TLS_VERSION_CHOICES', 'CONN_HEADER_CHOICES', 'MAX_PAGES',
    'parse_psk_map', 'script_apdus', 'command_body', 'parse_response',
    'continuation',
]

#: The owner the SCP81 listener reports in the session state (the legacy
#: ``'scp81'``); the generic BIP pill reports ``'bip'``
#: (:data:`simple_server.bip.OWNER`).
OWNER_SCP81 = 'scp81'

#: The listener modes the SCP81 control accepts: ``tls`` (the local PSK TLS
#: administration server), ``dump`` (capture-only TCP listener), ``redirect``
#: (no listener, the channels go to the configured target) and ``passthru``
#: (no listener and no target, the channels dial the card's own destination).
LISTENER_MODES = ('tls', 'dump', 'redirect', 'passthru')

#: The default port of the SCP81 TLS/dump listener (the legacy default).
DEFAULT_PORT = 8443

#: The most listing continuation pages auto-inserted for one script (the
#: legacy ``SCP81_MAX_PAGES``): a card that keeps answering ``63 10`` must not
#: grow the queue forever.
MAX_PAGES = 24

#: The TLS versions a caller may pin (``auto`` accepts TLS 1.0-1.2 and lets
#: OpenSSL pick the highest the card offers - the only setting normal use
#: needs).  The names are :data:`simple_server.shared.scp81.TLS_VERSIONS`'.
TLS_VERSION_CHOICES = ('auto',) + tuple(listener_module.TLS_VERSIONS)

#: The ``Connection`` header values the TLS listener accepts: ``none`` omits
#: the header (implicit HTTP/1.1 keep-alive), ``keep-alive`` states it.  The
#: connection is never closed mid-dialog - only the 204 ends the session.
CONN_HEADER_CHOICES = ('none', 'keep-alive', 'close')


def _flag(value, name, default):
    """A validated request flag with an explicit default.

    A JSON boolean and the integer ``0``/``1`` are taken as they are; a string
    is accepted only in its literal boolean spellings (``true``/``false``/
    ``1``/``0``).  ``bool("false")`` is truthy, so a hand-written API call
    used to turn an explicit "false" into TRUE (the v3.22.0 rule); anything
    else is a :class:`ValueError` (the endpoint's 400).  ``None``/``''`` mean
    "not given" and yield ``default``.
    """
    if value in (None, ''):
        return bool(default)
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.strip().lower() in (
            'true', 'false', '1', '0'):
        return value.strip().lower() in ('true', '1')
    raise ValueError('%s must be a boolean: %r' % (name, value))


def _number(value, name, default=None, minimum=0, maximum=0xFFFF):
    """A validated integer request field (the endpoint's 400 on a bad value)."""
    if value in (None, ''):
        if default is None:
            raise ValueError('%s is required' % name)
        return default
    if isinstance(value, bool):
        raise ValueError('%s must be a number: %r' % (name, value))
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError('%s is not a number: %r' % (name, value)) from None
    if not minimum <= number <= maximum:
        raise ValueError('%s must be %d-%d: %r'
                         % (name, minimum, maximum, value))
    return number


def _seconds(value, name, default=0.0, maximum=3600.0):
    """A validated fractional request field in seconds (the endpoint's 400)."""
    if value in (None, ''):
        return default
    if isinstance(value, bool):
        raise ValueError('%s must be a number: %r' % (name, value))
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        raise ValueError('%s is not a number: %r' % (name, value)) from None
    if not 0 <= seconds <= maximum:
        raise ValueError('%s must be 0-%g: %r' % (name, maximum, value))
    return seconds


def parse_psk_map(raw):
    """Parse a PSK lookup table from an API request -> ``(table, error)``.

    The legacy ``_parse_psk_map``: a list of ``{identity, psk_hex}`` objects
    (the PWA form, built from the card presets) or an ``{identity: psk_hex}``
    map.  Entries without a usable identity or key are skipped; a malformed
    entry is an error naming it (never a silent drop of the whole table).
    """
    if raw is None:
        return None, None
    if isinstance(raw, dict):
        raw = [{'identity': key, 'psk_hex': value}
               for key, value in raw.items()]
    if not isinstance(raw, list):
        return None, 'psk_map must be a list of {identity, psk_hex}'
    table = {}
    for item in raw:
        if not isinstance(item, dict):
            return None, 'psk_map entries must be {identity, psk_hex} objects'
        identity = str(item.get('identity') or '').strip()
        key_hex = re.sub(r'\s', '', str(item.get('psk_hex') or ''))
        if not key_hex or not identity:
            continue
        try:
            key = bytes.fromhex(key_hex)
        except ValueError:
            return None, 'psk_map[%s]: psk_hex is not valid hex' % identity
        if key:
            table[identity] = key
    return table, None


def script_apdus(body):
    """The APDU list of a script/queue request -> normalised hex strings.

    The legacy fields ``apdus``/``apdu`` are accepted (the PWA posts
    ``apdus``) and ``script`` is the documented alias (the task-list form); an
    empty ``apdus``/``script`` falls through to the next form, so the legacy
    single ``apdu`` keeps working beside an empty list.
    Each entry must be a hex string; whitespace is stripped and the case
    upper-cased (:meth:`ScriptEngine.reset` normalises the stored list the
    same way).  A malformed entry is a :class:`ValueError` (the 400) - an
    APDU the card would refuse must not reach the listener.
    """
    body = body or {}
    # An EMPTY `apdus` falls back too (the legacy `apdus or [apdu]`): a caller
    # that posts the full request shape with an empty list keeps the single
    # `apdu` working.
    raw = body.get('apdus')
    if not raw:
        raw = body.get('script')
    if not raw and body.get('apdu'):
        raw = [body.get('apdu')]
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError('apdus must be a list of APDU hex strings')
    out = []
    for entry in raw:
        if not entry:
            continue
        if not isinstance(entry, str):
            raise ValueError('every APDU must be a hex string: %r' % (entry,))
        apdu = re.sub(r'\s', '', entry).upper()
        if not apdu:
            continue
        if not re.match(r'^[0-9A-F]+$', apdu) or len(apdu) % 2 \
                or len(apdu) < 8:
            raise ValueError('not a hex APDU: %r' % (entry,))
        out.append(apdu)
    return out


def command_body(apdu_hex, definite=False, cr_tag=False):
    """The Command Scripting template holding one C-APDU (the legacy builder).

    The indefinite-length variant (``AE 80 22 <len> <apdu> 00 00``, the form
    TS 102 226 §5.2.1.0 recommends for RAM over HTTPS) or the definite one
    (``AA <len> 22 <len> <apdu>``).  The C-APDU TLV tag is ``22`` per
    TS 101 220 (CR flag 0); some cards expect the CR-set ``A2``, so it is
    configurable.  Both lengths are BER-encoded: a raw octet above ``7F`` is
    read as a long-form marker by the card, which garbles the script (the
    245-octet LOAD commands of a RAM install, live 2026-09-16).
    """
    apdu = bytes.fromhex(re.sub(r'\s', '', apdu_hex))
    cmd_tlv = (bytes([0xA2 if cr_tag else 0x22])
               + tlv.encode_ber_len(len(apdu)) + apdu)
    if definite:
        return bytes([0xAA]) + tlv.encode_ber_len(len(cmd_tlv)) + cmd_tlv
    return bytes([0xAE, 0x80]) + cmd_tlv + b'\x00\x00'


def parse_response(body):
    """Parse a Response Scripting template -> ``(executed_count, [(rapdu, sw)])``.

    The legacy ``_scp81_parse_response``: TS 102 226 §5.2.2, the definite
    ``AB`` or indefinite ``AF 80`` template; the executed-count TLV ``80`` and
    the R-APDU TLV ``23``/``A3`` (the last two octets of an R-APDU are its
    status word).  A TLV whose length runs past the body stops the walk - a
    truncated answer must not be misread as more R-APDUs.  BER lengths are
    walked (an R-APDU above 127 octets is ``23 81 FC ...``; a raw octet
    used as a length silently truncated every listing page to 127 octets,
    live 2026-09-16).
    """
    body = bytes(body or b'')
    if not body:
        return 0, []
    if body[0] == 0xAF and len(body) >= 2 and body[1] == 0x80:
        content = body[2:-2] if body.endswith(b'\x00\x00') else body[2:]
    elif body[0] == 0xAB:
        length, offset = tlv.parse_ber_len(body, 1)
        content = body[offset:offset + length]
    else:
        content = body
    count, results = 0, []
    off = 0
    while off < len(content):
        tag = content[off]
        length, value_off = tlv.parse_ber_len(content, off + 1)
        if value_off + length > len(content):
            break
        value = content[value_off:value_off + length]
        off = value_off + length
        if tag == 0x80:
            count = int.from_bytes(value, 'big') if value else 0
        elif tag in (0x23, 0xA3) and len(value) >= 2:
            results.append((value[:-2], value[-2:].hex().upper()))
    return count, results


def continuation(apdu):
    """The continuation APDU of a truncated GET STATUS page, or ``None``.

    GP Card Specification v2.3.1 §11.4.2: GET STATUS P2.b1 selects the
    *first* (0) or the *next* (1) batch of the matches for the same search
    criteria - the pagination state lives in the card, so the continuation is
    the same command with P2.b1 set.  Using a changed search criterion (the
    last returned AID) was rejected with ``6A80``: the criterion is a match
    filter, not a position.
    """
    apdu = str(apdu or '').upper()
    if not apdu.startswith('80F2') or len(apdu) < 8:
        return None
    p2 = int(apdu[6:8], 16) | 0x01
    return '%s%02X%s' % (apdu[:6], p2, apdu[8:])


class ScriptEngine(object):
    """The configured SCP81 command script and its run state.

    The port of the legacy ``_SCP81_SCRIPT_*`` globals: ``base`` is the
    configured APDU list (never mutated by a run), ``next`` the index of the
    next APDU to send, ``done`` the ``base`` indices the card reported,
    ``pending`` the APDU sent in the previous POST (waiting for the card's
    ``X-Admin-Script-Status`` report; a session that dies before the report
    resends it), ``results`` one entry per reported APDU and ``page_queue``
    the auto-inserted listing continuation pages (sent before ``base[next]``).

    :meth:`responder` is the ``PskTlsServer`` responder: the Remote
    Administration Server side of the dialog (GPC v2.2 Am. B §4.4) - it sends
    the next scripted C-APDU or closes the session with 204.  An APDU counts
    as executed only when the card reports it in the next POST; a POST with
    ``X-Admin-Resume`` continues with the unexecuted tail, a POST without it
    is a fresh dialog where the script runs from the start (so a completed
    script runs again on a new trigger).

    All state is guarded by an ``RLock``: the listener answers on its own
    connection thread while the HTTP endpoints read the state and queue a new
    script (the legacy relied on the blanket card lock, which the listener
    thread never took - a pre-existing race this port closes).
    """

    def __init__(self, log=None):
        self._log_callback = log
        self.lock = threading.RLock()
        self.base = []
        self.next = 0
        self.done = set()
        self.pending = None
        self.results = []
        self.page_queue = []
        self.sent_no = 0
        self.pages = 0
        self.kind = 'none'
        # The script framing knobs (the legacy `_SCP81_SCRIPT_TEMPLATE`/
        # `_CR_TAG`/`_NEXT_URI`/`_TARGETED_APP`): set by the listener start.
        self.template = 'indefinite'
        self.cr_tag = False
        # None = the built-in per-command Next-URI; '' omits the header (the
        # card executes the script, sends no response string and closes).
        self.next_uri = None
        self.targeted_app = None

    # ---- logging ----------------------------------------------------------

    def log(self, kind, **fields):
        """One terminal-log entry (the legacy ``_BIP.log`` target)."""
        callback = self._log_callback
        if callback is None:
            return
        try:
            callback(kind, **fields)
        except Exception:                       # the log must never raise
            pass

    # ---- the configured script -------------------------------------------

    def restart(self):
        """Start a new run over the configured script (a fresh dialog).

        Clears the execution marks, the pending APDU, the continuation pages,
        the results and the counters - the APDU list itself stays.
        """
        with self.lock:
            self.next = 0
            self.done = set()
            self.pending = None
            self.results = []
            self.page_queue = []
            self.sent_no = 0
            self.pages = 0

    def mid_run(self):
        """True while a run has started and not finished (queue needs force)."""
        with self.lock:
            return (0 < self.next < len(self.base)
                    or self.pending is not None
                    or bool(self.page_queue))

    def reset(self, script, kind='custom'):
        """Install a new APDU script and clear all execution progress."""
        apdus = [re.sub(r'\s', '', str(entry)).upper()
                 for entry in (script or []) if entry]
        with self.lock:
            self.base = apdus
            self.kind = kind
            self.restart()
            queued = len(self.base)
        self.log('script-queued', script_kind=kind, apdus=queued)
        return queued

    def queue(self, apdus, kind='custom', force=False):
        """Replace the script; refused mid-run unless ``force`` (the legacy)."""
        with self.lock:
            if not force and self.mid_run():
                return {'queued': False, 'reason': 'script in progress',
                        'next': self.next, 'of': len(self.base)}
            queued = self.reset(apdus, kind)
        return {'queued': True, 'apdus': queued}

    def state(self):
        """The ``GET /api/scp81/script`` body (the legacy ``_scp81_script_state``).

        Progress counts only the configured APDUs (``total``/``done``); the
        C-APDU awaiting the card's report is ``pending`` (``{index, pos,
        page, apdu}`` or null) and the auto-inserted listing continuation
        pages are ``pages``/``pages_queued``, so a run whose script APDUs are
        all executed is not mistaken for "still pending" while a page is in
        flight.  ``complete`` needs both: every APDU reported *and* nothing in
        flight.
        """
        with self.lock:
            pending = None
            if self.pending is not None:
                entry = self.pending
                pending = {'index': entry['index'], 'pos': entry.get('pos'),
                           'page': bool(entry.get('page')),
                           'apdu': entry['apdu']}
            return {'script': list(self.base),
                    'next': self.next,
                    'total': len(self.base),
                    'done': sorted(self.done),
                    'pending': pending,
                    'pages': self.pages,
                    'pages_queued': len(self.page_queue),
                    'complete': (len(self.done) >= len(self.base)
                                 and self.pending is None
                                 and not self.page_queue),
                    'kind': self.kind,
                    'template': self.template,
                    'cr_tag': self.cr_tag,
                    'results': [dict(entry) for entry in self.results]}

    # ---- the administration dialog ---------------------------------------

    @staticmethod
    def response_headers():
        """The administration dialog's base response headers."""
        return {'X-Admin-Protocol': listener_module.GP_PROTOCOL}

    def responder(self, method, target, headers, body):
        """Send the next scripted C-APDU, or end the dialog (204).

        ``headers`` maps the lower-case header names (``PskTlsServer``'s
        ``parse_http_request`` form).
        """
        headers = headers or {}
        status = headers.get('x-admin-script-status')
        resume = headers.get('x-admin-resume')
        with self.lock:
            pending = self.pending
            if pending is not None:
                self.pending = None
                index = pending['index']
                if status is not None:
                    self._report(pending, index, status, body)
                elif resume:
                    # Resumed dialog: the pending APDU was never reported, so
                    # it is still unexecuted - put it back in line and resend.
                    if pending.get('page'):
                        self.page_queue.insert(0, pending['apdu'])
                    else:
                        self.next = pending['pos']
                        self.done.discard(pending['pos'])
                    self.log('script-resend', index=index, apdu=pending['apdu'])
                else:
                    # Fresh dialog (new trigger): the script runs from the top.
                    self.restart()
            elif status is not None:
                # A report without a pending APDU (the server may have
                # restarted mid-session): log the R-APDUs, advance nothing.
                self._report(None, None, status, body)
            elif not resume:
                # The first POST of a fresh dialog: reset a previous run.
                self.restart()
            apdu = None
            if self.page_queue:
                apdu = self.page_queue.pop(0)
                self.pending = {'index': self.sent_no + 1, 'pos': None,
                                'page': True, 'apdu': apdu}
            elif self.next < len(self.base):
                position = self.next
                apdu = self.base[position]
                self.next = position + 1
                self.pending = {'index': self.sent_no + 1, 'pos': position,
                                'page': False, 'apdu': apdu}
            if apdu is None:
                self.log('script-done', sent=self.sent_no,
                         results=len(self.results))
                return 204, self.response_headers(), b''
            index = self.pending['index']
            self.sent_no = index
            out_headers = self.response_headers()
            if self.targeted_app:
                out_headers['X-Admin-Targeted-Application'] = self.targeted_app
            # A relative URI plus a QUERY: a query-less Next-URI makes the
            # card abort the TLS session (the reference server's
            # `/adminserver?PHPSESSID=…&apdu_id=N`).  A `%d` template is
            # replaced with the command number.
            template = (self.next_uri if self.next_uri is not None
                        else '/api/scp81?req=%d')
            next_uri = template % index if '%d' in template else template
            if next_uri:
                out_headers['X-Admin-Next-URI'] = next_uri
            upper = apdu.upper()
            self.log('script-send', index=index, apdu=apdu)
            if upper.startswith('AA') or upper.startswith('AE80'):
                # Already a Command Scripting template (the expanded RAM
                # format): send it verbatim instead of wrapping it again.
                body_out = bytes.fromhex(upper)
            else:
                body_out = command_body(
                    apdu, definite=(self.template == 'definite'),
                    cr_tag=self.cr_tag)
            out_headers['Content-Type'] = listener_module.GP_CT_COMMAND
            return 200, out_headers, body_out

    def _report(self, pending, index, status, body):
        """Handle the card's report of the pending APDU (TS 102 226 §5.2.2).

        A report without a pending APDU (the server lost the session state, or
        a stray POST) is logged only - it advances nothing and adds no result
        (the legacy behaviour: there is no script position to attribute it to).
        """
        if status != 'ok':
            self.log('script-status', index=index, status=status)
            if pending is not None and pending.get('pos') is not None:
                self.done.add(pending['pos'])
            return
        _count, rapdus = parse_response(body)
        for rapdu, sw in rapdus:
            self.log('script-rapdu', index=index, sw=sw, bytes=len(rapdu),
                     hex=rapdu.hex().upper()[:2000])
            if pending is None:
                continue
            self.results.append(
                {'index': index, 'pos': pending.get('pos'),
                 'page': bool(pending.get('page')),
                 'apdu': pending['apdu'],
                 'rapdu': rapdu.hex().upper(), 'sw': sw})
        if pending is None:
            return
        if rapdus:
            if pending['apdu'].startswith('80CAFF21'):
                decoded = capram.decode_memory(rapdus[-1][0])
                if decoded:
                    self.log('script-memory', **decoded)
            # `63 10` = "more occurrences available" (GP Table 11-38); the
            # live card uses the proprietary `CA FE` for the same case.
            if (rapdus[-1][1].upper() in ('CAFE', '6310')
                    and self.pages < MAX_PAGES):
                cont = continuation(pending['apdu'])
                if cont:
                    self.pages += 1
                    self.page_queue.append(cont)
                    self.log('script-page', index=index, page=self.pages,
                             apdu=cont)
        if pending.get('pos') is not None:
            self.done.add(pending['pos'])


class Scp81Control(object):
    """The SCP81/BIP control surface of one server (the legacy globals).

    ``bip`` is the server's persistent :class:`~simple_server.bip.BipSession`:
    the session state (owner/mode/target/listener/link_events) survives a card
    change because the listener is the *terminal* side, while the per-card
    channels are attached/detached by the server
    (:meth:`~simple_server.bip.BipSession.attach_card`/``detach_card``).  The
    one-session rule is the session's own (a start stops whatever ran before
    and reports it as ``replaced``), so the SCP81 listener and the BIP pill
    replace each other through this one object.

    ``log`` defaults to the BIP terminal's log, so every SCP81 entry lands in
    the same ``/api/scp81/log`` stream the BIP entries use (the legacy
    ``_BIP.log``).
    """

    def __init__(self, bip, log=None):
        self.bip = bip
        self.script = ScriptEngine(
            log=log if log is not None else bip.terminal.log)
        # The last resolved PSK table / single key, reused when a TLS start
        # carries no keys (the legacy `_SCP81_PSKS`/`_SCP81_PSK_LEGACY`): the
        # PWA pushes preset edits separately, so a plain Stop/Start must not
        # require re-pasting the table.
        self.psk_table = None
        self.psk_legacy = None
        # The listener's chunked framing, echoed by the start response (the
        # legacy `_SCP81_CHUNKED`; the header itself is `build_http_response`'s).
        self.chunked = False

    def _log(self, kind, **fields):
        """One SCP81 entry in the shared terminal log (the legacy ``_BIP.log``)."""
        self.script.log(kind, **fields)

    # ---- status ----------------------------------------------------------

    def _tls_listener_status(self):
        """The TLS branch of the listener block, or ``None`` (the legacy)."""
        listener = self.bip.listener
        if not isinstance(listener, listener_module.PskTlsServer):
            return None
        return {'mode': 'tls', 'host': listener.host, 'port': listener.port,
                'psk_identities': listener.psk_identities,
                'psk_wildcard': listener.wildcard_psk is not None,
                'identity_seen': listener.identity_seen,
                'identity_matched': listener.identity_matched,
                'version_seen': listener.version_seen,
                'cipher_seen': listener.cipher_seen,
                'chunked': listener.chunked,
                'chunk_size': listener.chunk_size,
                'compact_headers': listener.compact_headers,
                'tls_version': listener.tls_version,
                'cipher': listener.cipher}

    def status_body(self):
        """The ``GET /api/scp81/status`` / ``/api/bip/status`` body.

        ``{owner, bip, listener}`` is the legacy shape: ``owner`` is
        ``scp81``/``bip`` (whichever control started the session) or ``null``
        when idle, ``bip`` is the terminal state (channels, seq, mode, target)
        and ``listener`` the mode's block - the TLS listener reports its PSK
        identities and the last handshake, a sink/dump listener its bound port
        and connection count, a redirect session its target, a passthru
        session its mode.

        Three additive fields serve one-poll rendering: ``mode`` (the
        listener's mode: ``tls``/``dump``/``sink``/``redirect``/``passthru``,
        ``null`` when idle), ``target`` (the ``host:port`` the channels go to,
        ``null`` for passthru/idle) and ``script`` (the
        :meth:`ScriptEngine.state` copy - the listener status and the script
        progress can then never disagree between two polls).  The start
        responses carry ``mode``/``target`` too but keep the legacy
        ``script`` **APDU list** echo, so ``script`` is a dict on the status
        endpoints only.

        The snapshot is taken under the session's state lock (the one the
        start/stop paths mutate under), so the two listener reads cannot
        straddle a replacement.
        """
        state_lock = getattr(self.bip, '_state_lock', None)
        if state_lock is None:
            return self._status_body_locked()
        with state_lock:
            return self._status_body_locked()

    def _status_body_locked(self):
        """The status fields (the caller holds the session's state lock)."""
        body = self.bip.status()
        terminal_status = body.get('bip') or {}
        listener = self._tls_listener_status()
        if listener is None:
            listener = body.get('listener')
        # `mode`/`target` are the control's view of the session (what the
        # listener block says, not the terminal's internal redirect mode), and
        # `script` lets one poll drive the whole SCP81 panel (the PWA's script
        # view and the listener status then cannot disagree).
        return {'owner': body.get('owner'), 'bip': terminal_status,
                'listener': listener,
                'mode': (listener or {}).get('mode'),
                'target': ((listener or {}).get('target')
                           or terminal_status.get('target')),
                'script': self.script.state()}

    def script_state(self):
        """``GET /api/scp81/script`` (card-free)."""
        return self.script.state()

    # ---- the SCP81 listener ----------------------------------------------

    def start(self, body):
        """``POST /api/scp81/bip``: start/stop the SCP81 listener.

        Returns the control response (``{ok, replaced, owner, bip, listener,
        mode, target}``, plus the script APDU list and the framing fields for
        a TLS start); a refusal is an ``{'ok': False, 'error': ...}`` dict,
        which the endpoint answers ``400``.  ``action: 'stop'`` stops whatever
        session runs (the SCP81 listener or the BIP pill) and reports it as
        ``replaced``.

        A validation error never disturbs a running session: every request
        field is checked before the previous session is stopped.  The stop
        itself runs **before** the listener binds (the legacy order: its
        ``_bip_session_stop`` came before the bind), so a Start on a port the
        previous listener holds can rebind it - the PWA's Start is the normal
        way to apply the Options.  The accepted trade-off is that a bind
        failure at a *foreign* port leaves no session behind (the previous one
        is already stopped), unlike a validation refusal.
        """
        body = body or {}
        action = body.get('action', 'start')
        if action == 'stop':
            return self.stop()
        if action != 'start':
            raise ValueError('unsupported action: %r' % (action,))
        mode = body.get('mode', 'dump')
        if mode not in LISTENER_MODES:
            raise ValueError('unsupported mode: %s' % (mode,))
        link_events = _flag(body.get('link_events'), 'link_events', True)
        if mode in ('redirect', 'passthru'):
            # No local listener: the session is the BIP pill's own
            # redirect/passthru - delegate so the single state machine stays
            # in `BipSession` (the legacy did the same by hand).  It has no
            # bind to fail, so the stop-before-bind order is its own.
            resp = self.bip.start(dict(body, action='start', mode=mode,
                                       link_events=link_events),
                                  owner=OWNER_SCP81)
            # The documented control shape, whatever the delegated handler
            # returned (`BipSession.start` answers owner/bip/listener/ok/
            # replaced only).
            status = self.status_body()
            resp['mode'] = status.get('mode')
            resp['target'] = status.get('target')
            return resp
        host = (str(body.get('host') or '').strip() or '127.0.0.1')
        port = _number(body.get('port'), 'port', default=DEFAULT_PORT)
        if mode == 'tls':
            return self._start_tls(body, host, port, link_events)
        # Everything is validated: stop the previous session before the bind
        # (see the docstring).  `replaced` is forwarded so the start reports
        # what this explicit stop replaced.
        replaced = self.bip.stop()
        try:
            listener = httpota.TcpDumpServer(
                host, port,
                on_rx=lambda peer, data: self._log(
                    'dump-rx', peer=peer, bytes=len(data),
                    hex=data.hex().upper()[:2000]),
                on_log=lambda kind, **fields: self._log(kind, **fields))
        except OSError as exc:
            return {'ok': False, 'error': str(exc)}
        return self._start_listener(listener, 'dump', link_events,
                                    replaced=replaced)

    def _start_listener(self, listener, mode, link_events, replaced=None):
        """Wrap a built listener in the BIP session and report it.

        ``replaced`` is the result of the stop the caller ran **before** the
        bind (the legacy order); when it is ``None`` the previous session was
        idle, so ``start_listener``'s own stop reports the same ``None``.

        The response is the legacy ``_bip_status_body`` plus the start's own
        fields; the status-only ``script`` state is deliberately left out (a
        TLS start echoes the legacy ``script`` **APDU list** instead, so one
        field name keeps one type per endpoint - the state lives in
        ``GET /api/scp81/status``).
        """
        stopped = self.bip.start_listener(listener, owner=OWNER_SCP81,
                                          mode=mode, link_events=link_events)
        if replaced is None:
            replaced = stopped
        status = self.status_body()
        resp = {'ok': True, 'replaced': replaced}
        for key in ('owner', 'bip', 'listener', 'mode', 'target'):
            resp[key] = status.get(key)
        return resp

    @staticmethod
    def _header_value(value, name):
        """A response-header value: no control characters (a CRLF would split
        the HTTP response the TLS listener builds).  Empty means "unset"."""
        text = str(value or '')
        if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in text):
            raise ValueError('%s contains a control character' % name)
        return text

    def _start_tls(self, body, host, port, link_events):
        """Build the PSK TLS listener (the legacy ``tls`` branch).

        Every knob is validated, and the script request is validated without
        being applied, **before** the previous session is stopped and the
        listener binds; the script reset/rewrite then runs only after a
        successful bind, so a refused Start neither loses the running
        session's progress nor leaves it half-swapped.  A construction error
        (a taken port, ``PskTlsServer``'s Python-3.13 RuntimeError, an
        ``ssl.SSLError`` from ``set_ciphers``) is the control's
        ``{'ok': False, 'error': ...}``, which the endpoint answers 400.
        """
        # Validate every knob that would otherwise fail inside the listener's
        # constructor *after* its socket is bound (a leak) - and refuse a
        # misspelled pin instead of silently widening the protocol window.
        tls_version = str(body.get('tls_version') or 'auto').strip()
        if tls_version not in TLS_VERSION_CHOICES:
            raise ValueError('tls_version must be one of %s: %r'
                             % ('/'.join(TLS_VERSION_CHOICES), tls_version))
        script_template = str(body.get('script_template') or 'indefinite')
        if script_template not in ('indefinite', 'definite'):
            raise ValueError('script_template must be indefinite or definite')
        conn_header = str(body.get('conn_header') or 'none').strip()
        if conn_header not in CONN_HEADER_CHOICES:
            raise ValueError('conn_header must be one of %s: %r'
                             % ('/'.join(CONN_HEADER_CHOICES), conn_header))
        cipher = (str(body.get('cipher') or '').strip() or None)
        if cipher is not None:
            try:
                ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).set_ciphers(cipher)
            except ssl.SSLError as exc:
                raise ValueError('cipher is not usable: %s' % exc) from None
        chunk_size = _number(body.get('chunk_size'), 'chunk_size', default=0,
                             maximum=0xFFFF)
        answer_delay = _seconds(body.get('answer_delay'), 'answer_delay')
        chunked = _flag(body.get('chunked'), 'chunked', True)
        compact_headers = _flag(body.get('compact_headers'),
                               'compact_headers', False)
        cr_tag = _flag(body.get('cr_tag'), 'cr_tag', False)
        # The two values reach the administration response headers verbatim.
        next_uri = (self._header_value(body.get('next_uri'), 'next_uri')
                    if 'next_uri' in body else None)
        targeted_app = (self._header_value(body.get('targeted_app'),
                                           'targeted_app') or None)

        table, psk, identity = self._resolve_psk(body)

        # The script: an explicit list starts a fresh run, 'none' clears it,
        # omitting the key keeps the configured script and its progress (the
        # legacy contract) - validated here, applied only after the bind.
        script = body.get('script')
        new_script = None
        if isinstance(script, str):
            if script not in ('none', ''):
                raise ValueError('unknown script preset: %s; send an explicit '
                                 'APDU list or none' % script)
            new_script = ([], 'none')
        elif isinstance(script, list):
            new_script = (script_apdus({'apdus': script}),
                          str(body.get('script_kind') or 'custom'))
        elif script is not None:
            raise ValueError('script must be an APDU list or "none"')

        # All validation passed: stop the previous session before the bind
        # (`start`'s docstring; the legacy order), so a restart on the port it
        # holds can rebind it.
        replaced = self.bip.stop()
        try:
            listener = listener_module.PskTlsServer(
                host, port, psk, identity=identity, psk_map=(table or None),
                responder=self.script.responder,
                chunked=chunked, chunk_size=chunk_size,
                compact_headers=compact_headers, tls_version=tls_version,
                cipher=cipher, keylog=(str(body.get('keylog') or '') or None),
                conn_header=conn_header, answer_delay=answer_delay,
                on_log=lambda kind, **fields: self._log(kind, **fields))
        except (OSError, RuntimeError, ssl.SSLError) as exc:
            # the port is taken (the listener retries briefly, then gives up),
            # or TLS-PSK is unavailable below Python 3.13 / the cipher set is
            # rejected - a control refusal, never a 500
            return {'ok': False, 'error': str(exc)}

        # The listener bound: now the script state is safe to change.
        if new_script is not None:
            self.script.reset(new_script[0], new_script[1])
        with self.script.lock:
            self.script.template = script_template
            self.script.cr_tag = cr_tag
            if next_uri is not None:
                self.script.next_uri = next_uri
            self.script.targeted_app = targeted_app
        self.chunked = bool(listener.chunked)
        # Remember what the listener ended up accepting (the legacy kept the
        # resolved table, never the request body).
        self.psk_table = dict(listener.psk_map) if listener.psk_map else None
        self.psk_legacy = (None if listener.psk_map
                           else (listener.psk, listener.identity))
        resp = self._start_listener(listener, 'tls', link_events,
                                    replaced=replaced)
        resp.update({'script': list(self.script.base),
                     'script_kind': self.script.kind,
                     'script_template': self.script.template,
                     'cr_tag': self.script.cr_tag,
                     'link_events': bool(self.bip.link_events),
                     'targeted_app': self.script.targeted_app,
                     'chunked': self.chunked})
        return resp

    def _resolve_psk(self, body):
        """The PSK table or legacy single key of a TLS start.

        The legacy order: an explicit ``psk_map`` (the PWA's card-preset
        table) wins; then the legacy single ``psk_hex`` (+ ``psk_identity``,
        empty = accept any identity); then the last start's table/key.
        Returns ``(table, psk, identity)``.
        """
        raw_map = body.get('psk_map')
        table, error = parse_psk_map(raw_map)
        if error:
            raise ValueError(error)
        if raw_map is not None and not table:
            raise ValueError('psk_map has no usable entries '
                             '(identity + key required)')
        if table:
            return table, None, None
        psk_hex = body.get('psk_hex') or ''
        if psk_hex:
            try:
                psk = bytes.fromhex(re.sub(r'\s', '', str(psk_hex)))
            except ValueError:
                raise ValueError('psk_hex is not valid hex') from None
            if not psk:
                raise ValueError('psk_hex is empty')
            identity = body.get('psk_identity')
            if identity is not None:
                identity = str(identity).strip() or None
            return None, psk, identity
        if self.psk_table:
            return dict(self.psk_table), None, None
        if self.psk_legacy:
            psk, identity = self.psk_legacy
            return None, psk, identity
        raise ValueError('no PSK configured: send psk_map (card presets) or '
                         'psk_hex')

    def stop(self):
        """Stop the active session (SCP81 listener or BIP pill)."""
        replaced = self.bip.stop()
        resp = {'ok': True, 'replaced': replaced}
        resp.update(self.status_body())
        return resp

    # ---- the BIP pill ----------------------------------------------------

    def bip_control(self, body):
        """``POST /api/bip/control``: the generic BIP session (sink/redirect/
        passthru) - one session at a time, the SCP81 listener included."""
        body = body or {}
        if body.get('link_events') is not None:
            body = dict(body, link_events=_flag(body.get('link_events'),
                                                'link_events', True))
        return self.bip.start(body, owner=bip_module.OWNER)

    # ---- the script and the PSK table ------------------------------------

    def queue(self, body):
        """``POST /api/scp81/queue``: replace/restart the command script."""
        body = body or {}
        apdus = script_apdus(body)
        if not apdus:
            return {'ok': False, 'error': 'no apdus given'}
        if self.bip.listener is None:
            return {'ok': False, 'error': 'SCP81 listener is not running'}
        force = _flag(body.get('force'), 'force', False)
        queued = self.script.queue(apdus, kind=str(body.get('kind') or 'custom'),
                                   force=force)
        resp = dict(queued, ok=bool(queued.get('queued')))
        if queued.get('queued'):
            resp['note'] = ('queued as the SCP81 command script; it runs on '
                            'the card\'s next POST (push/trigger)')
        return resp

    def update_psk_map(self, body):
        """``POST /api/scp81/psk-map``: replace a running listener's table."""
        body = body or {}
        table, error = parse_psk_map(body.get('psk_map'))
        if error:
            raise ValueError(error)
        if not table:
            return {'ok': False,
                    'error': 'no usable PSK entries (identity + key required)'}
        listener = self.bip.listener
        if not isinstance(listener, listener_module.PskTlsServer):
            return {'ok': False, 'error': 'PSK TLS listener is not running'}
        listener.set_psk_map(table)
        self.psk_table = dict(table)
        self.psk_legacy = None
        self._log('tls-psk-map', identities=sorted(table))
        return {'ok': True, 'identities': sorted(table),
                'listener': self._tls_listener_status()}

    # ---- the .cap -> APDU list -------------------------------------------

    def gen_install(self, body):
        """``POST /api/scp81/gen-install``: the RAM APDU list of a ``.cap``.

        The legacy ``_scp81_gen_install``: the archive is parsed and expanded
        to INSTALL [for load] -> LOAD blocks -> INSTALL [for install] with the
        same builders the RAM install uses (:mod:`simple_server.capram`, GP
        Card Specification v2.3.1 §11.5.2.3.1/§11.6.2/§11.5.2.3.2); nothing
        is queued, the listener/script state is untouched and the file is
        never stored.  The archive travels as ``cap_hex`` (the documented
        field) or the ``cap`` shorthand.  A refusal answers
        ``{'ok': False, 'error': ...}`` (the legacy contract - the endpoint
        answers 200, the PWA reads ``ok``).
        """
        body = body or {}
        cap_hex = re.sub(r'\s', '', str(body.get('cap_hex')
                                        or body.get('cap') or ''))
        if not cap_hex:
            return {'ok': False, 'error': 'No cap_hex provided'}
        try:
            loadfile_aid, module_aid, loadfile_data = capram.cap_parse(cap_hex)
            apdus = capram.cap_apdu_sequence(
                loadfile_aid, module_aid, loadfile_data,
                sd_aid=re.sub(r'\s', '', str(body.get('sd_aid') or '')),
                privileges=(re.sub(r'\s', '',
                                   str(body.get('privileges') or '')) or '00'),
                install_params=re.sub(r'\s', '',
                                      str(body.get('install_params') or '')),
                stk_params=re.sub(r'\s', '', str(body.get('stk_params') or '')),
                make_selectable=_flag(body.get('make_selectable'),
                                      'make_selectable', True),
                nv_quota=body.get('nv_quota'),
                volatile_quota=body.get('volatile_quota'))
        except ValueError as exc:
            # a CAP parse refusal or the install-parameter composition rule
            return {'ok': False, 'error': str(exc)}
        except Exception as exc:                # a malformed component
            return {'ok': False, 'error': 'cap parse failed: %s' % exc}
        self._log('gen-install', apdus=len(apdus),
                  load_file_aid=loadfile_aid, module_aid=module_aid)
        return {'ok': True, 'apdus': apdus, 'load_file_aid': loadfile_aid,
                'module_aid': module_aid}
