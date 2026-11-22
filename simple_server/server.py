# coding=utf-8
"""The pysim-free server core: one server's state, card lifecycle, HTTP API.

This module owns what the legacy ``pysim_simple_server/server.py`` spread over
module globals: the card link, the detected card, the CAT session, the BIP
session, the file model, the session caches and the background threads.  One
:class:`ServerState` describes one server; nothing here is a module global.

Scope (Phase 3c-3a/3b/3c-1): the state and the card lifecycle
(:meth:`ServerState.open`, :meth:`~ServerState.equip`,
:meth:`~ServerState.reset`, :meth:`~ServerState.disconnect`, the presence
monitor and the background STATUS poll), the HTTP handler and the
:class:`http.server.ThreadingHTTPServer` factory over it, the static PWA
serving and the endpoints - ``GET /api/version``, ``GET /api/status``,
``GET /api/readers``, ``GET /api/arr``, ``GET /api/pbr``,
``POST /api/reader``, ``POST /api/equip``, ``POST /api/reset`` (3c-3a), the
file endpoints ``POST /api/tree|select|read|write`` (3c-3b) and the
preset/plain-SCP80 set ``GET/POST /api/presets`` +
``/api/presets/update|delete|import|import-outfile``, ``POST /api/verify-adm``,
``POST /api/send-ota`` and ``POST /api/counter-probe`` (3c-3c-1).  Every other
API path is a 404 until the remaining 3c-3c endpoints (RAM install/batch, the
TAR probe, the test runner, eSIM/SCP81/netsim) and 3c-3d/3d land.

Deliberate API decisions (see ``docs/pysim-phaseout.md`` "Phase 3c API
surface"): there is no pySim command line, no ``/api/command``/``/api/help``/
``/api/commands``/``/api/cardinfo``, and **no ``/api/sp-verify``** (the PWA's
"Verify vs pySim" builds its reference with pySim itself, which this server
does not have - the SCP80 packet builder is pinned to the same vectors by the
unit tests instead); ``/api/status`` has our own shape (no
pySim concepts); ``/api/apdu`` (3c-3b) keeps the automatic proactive handler on
an ``91XX`` answer.  ``/api/read`` has **no** ``mode: 'decoded'``: the legacy
mode ran pySim's server-side EF decoders, the PWA decodes the same raw bytes
client-side, so the mode is refused with a 400 instead of being silently
ignored.  The file endpoints replace the legacy pySim-shell calls with the
file model + :class:`~simple_server.commands.FileOps`; a model-unknown 4-hex
FID is probed without an ``allow_probe`` flag (the legacy flag/detach dance
protected pySim's model, which is never mutated here).  The card lock
discipline is the legacy one: every ``/api/`` GET/POST is serialized by the one
``threading.RLock`` except the card-free GET allowlist
(:data:`CARD_FREE_GET`) and the static PWA files, so a long card operation
cannot starve the UI's state poll.

Provenance / legacy parity: :class:`ServerState` ports the legacy
``_apply_equipped_card`` (the post-equip refresh + TERMINAL PROFILE), the
startup init's ICCID/EF.ARR/EF.PBR reads and drain loop, ``_handle_card_disconnect``
and the poll / presence behaviour.  Simplifications vs the legacy are named in
the class and method docstrings; the biggest is that the legacy's pySim-shell
equip (``onecmd_plus_hooks('equip')`` with its swallowed-exception and
command-set registration checks) is a direct :func:`simple_server.cards.init_card`
call here, and the presence watchdog (restarting pyscard's monitor after a
pcscd restart) is not ported yet.

Spec references: TS 102 221 §11.2 (the CAT command APDUs), §14.6.2 (the
terminal polls the UICC during idle); TS 102 223 §6.4.6/§6.4.14 (the card's
POLL INTERVAL / POLLING OFF wishes, adopted by the poll); TS 102 221 §13.2
(EF.ICCID, the E.118 digit string); ISO/IEC 7816-4 Table 25 / TS 102 221
§11.1.1.4 (the FCP ``8B`` EF.ARR reference); TS 31.102 §4.4.2.1 (EF.PBR).
"""

import json
import logging
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from pysim_simple_server import outfile
from pysim_simple_server import presets as presets_store

from simple_server import bip as bip_module
from simple_server import cards
from simple_server import cat_session
from simple_server import commands
from simple_server import files as files_module
from simple_server import ota
from simple_server import pbr as pbr_module
from simple_server import scp80
from simple_server import sms
from simple_server import util
from simple_server.transport import PcscLink, ReaderError

log = logging.getLogger(__name__)

__all__ = [
    'ServerState', 'make_handler', 'build_http_server', 'reader_names',
    'CardBusyError', 'NoCardSessionError', 'CARD_FREE_GET', 'CARD_FREE_POST',
    'DEFAULT_TERMINAL_PROFILE', 'DEFAULT_POLL_INTERVAL', 'POLL_CHAIN_MAX',
    'VERSION', 'ARR_READ_PATHS', 'PBR_READ_PATHS', 'check_reader',
    'decode_iccid',
]

#: The server version served by ``GET /api/version``.  The entry point (Phase
#: 3d) owns the release version; until the cutover this marks the new server.
VERSION = '4.0.0-dev'

#: The TERMINAL PROFILE of a real BIP-capable handset (the legacy default): the
#: live card only starts HTTP OTA when BIP commands and events are advertised.
DEFAULT_TERMINAL_PROFILE = (
    'FFFFFFFF7F9F00DFFF03021FE2000000C3FB000704117800710100000038428003')

#: The idle STATUS poll interval in seconds (TS 102 221 §14.6.2); the card's
#: POLL INTERVAL replaces it (TS 102 223 §6.4.6).  ``0`` disables polling.
DEFAULT_POLL_INTERVAL = 30

#: The answers one poll cycle may send before it yields (the legacy
#: ``_POLL_CHAIN_MAX``): a card re-queueing a command must not hold the card
#: lock (v3.25.1 - the live EP BAP's 1100 PROVIDE LOCAL INFORMATION storm).
POLL_CHAIN_MAX = 10

#: ``/api/`` GETs served without the card lock: pure cached state and the
#: reader list (the UI polls them while a long card operation runs).  The list
#: grows as the endpoints are ported (the legacy list is the reference);
#: ``/api/arr`` and ``/api/pbr`` are the session caches the file manager reads
#: while a snapshot scan is running, and ``/api/presets`` is the server-side
#: preset store (no card involved).
CARD_FREE_GET = frozenset([
    '/api/status', '/api/version', '/api/readers', '/api/arr', '/api/pbr',
    '/api/presets',
])

#: POSTs that must answer while the card lock is held (the legacy
#: ``_CARD_FREE_POST``).  No 3c-3a endpoint qualifies: starting a card operation
#: is exactly what the lock serializes.
CARD_FREE_POST = frozenset()

#: The EF.ARR copies an FCP ``8B`` security attribute can reference (the legacy
#: ``ARR_READ_PATHS``, label for label): the MF copy, the DF.TELECOM/DF.GSM
#: copies (TS 51 011 7F10/7F20) and the ADF.USIM copy.  The label is the legacy
#: **FID path** - ``arr.path`` is compared against the file manager's FID-based
#: path by the PWA, so a symbolic DF name here would never resolve.  Read once
#: per card session.
ARR_READ_PATHS = (
    ('MF/2F06', ('3F00', '2F06')),
    ('MF/7F10/6F06', ('3F00', '7F10', '6F06')),
    ('MF/7F20/6F06', ('3F00', '7F20', '6F06')),
    ('ADF.USIM/6F06', (cards.AID_USIM, '6F06')),
)
ARR_RECORD_LIMIT = 64

#: The EF.PBR copies (TS 31.102 §4.4.2.1) read once per card session - the
#: legacy ``PBR_READ_PATHS`` (FID labels, see :data:`ARR_READ_PATHS`): the
#: DF.TELECOM copy (7F10), the ADF.USIM copy and the ADF.USIM-NON-IMSI copy
#: (``A000000087100B``, pySim's name for it).
PBR_READ_PATHS = (
    ('MF/7F10/5F3A/4F30', ('3F00', '7F10', '5F3A', '4F30')),
    ('ADF.USIM/5F3A/4F30', (cards.AID_USIM, '5F3A', '4F30')),
    ('ADF.USIM-NON-IMSI/5F3A/4F30',
     (cards.AID_USIM_NON_IMSI, '5F3A', '4F30')),
)
PBR_RECORD_LIMIT = 16

#: The human names of :class:`~simple_server.cards.CardType` kinds.
CARD_NAMES = {'uicc': 'UICC', 'sim': 'SIM', 'euicc': 'eUICC',
              'unknown': 'Unknown'}


class CardBusyError(RuntimeError):
    """The card is owned by another operation (a test run or a BIP session).

    Distinct from :class:`~simple_server.transport.ReaderError` (also a
    ``RuntimeError``) on purpose: the endpoint answers this one ``409`` and a
    real switch failure ``503`` with the status body - a ``ReaderError`` used
    to be caught as "busy", so every failed switch lost its status (and the
    PWA never saw the bumped ``card_session``).
    """


class NoCardSessionError(RuntimeError):
    """A card endpoint was called with no equipped card (the endpoint's 503)."""


def _is_transport_error(exc):
    """True when ``exc`` means the PC/SC link itself is unusable.

    The explicit signal is :class:`~simple_server.transport.ReaderError`; a
    pyscard exception carries an ``hresult`` and is transport-level too (a
    dead handle, a removed card, pcscd gone).  A decoder/protocol failure
    (:class:`~simple_server.cat_session.CatSessionError`, an ``IndexError`` in
    a TLV walk) is not: the card still talks, only the answer was not
    understood - tearing the session down for that would lose a healthy card.
    """
    if isinstance(exc, ReaderError):
        return True
    return getattr(exc, 'hresult', None) is not None

# Static file serving (the PWA lives in ``<repo>/frontend``, served by this
# server so the UI and the API share an origin and no CORS/PNA is involved).
_STATIC_MIME = {
    '.html': 'text/html; charset=utf-8',
    '.css': 'text/css',
    '.js': 'application/javascript',
    '.json': 'application/json',
    '.png': 'image/png',
    '.svg': 'image/svg+xml',
    '.ico': 'image/x-icon',
    '.webmanifest': 'application/manifest+json',
    '.map': 'application/json',
    '.wasm': 'application/wasm',
    '.woff': 'font/woff',
    '.woff2': 'font/woff2',
}


def reader_names():
    """The PC/SC reader names (``[]`` when pyscard or pcscd is unavailable)."""
    try:
        import smartcard.System
    except ImportError:                         # no pyscard (a test harness)
        return []
    try:
        return [reader.name for reader in smartcard.System.readers()]
    except Exception as exc:                    # no pcscd service / no context
        log.debug('PC/SC reader list failed: %s', exc)
        return []


def check_reader(name, available=None):
    """Validate a reader name against the current list; returns it.

    Raises :class:`ValueError` naming the available readers, which the
    ``POST /api/reader`` endpoint turns into a 400.
    """
    name = str(name if name is not None else '').strip()
    if not name:
        raise ValueError('a reader name is required')
    available = reader_names() if available is None else list(available)
    if name not in available:
        raise ValueError('PC/SC reader %r not found (available: %s)'
                         % (name, ', '.join(available) or 'none'))
    return name


def decode_iccid(data):
    """Decode EF.ICCID content: nibble-swapped E.118 digits with an F pad.

    TS 102 221 V18.4.0 §13.2 Table 13.3 (EF_ICCID at MF level: identifier
    '2FE2', transparent, mandatory, file size 10 bytes) codes the content
    "BCD, left justified and padded with 'F'", with byte 1 b1 = "LSB of
    Digit 1" and the following nibbles the higher bits of digit 1, then digit
    2 in b8..b5 - i.e. the low nibble carries the first digit.  So every byte
    is read low-nibble first and a trailing ``F`` nibble is the odd-digit pad.
    Returns the digit string, or ``None`` when the bytes are not plausible
    (an odd number of hex digits, a non-digit after the swap, only padding).
    ``data`` is bytes or a hex string.
    """
    if isinstance(data, (bytes, bytearray)):
        text = bytes(data).hex()
    else:
        text = str(data or '')
    text = ''.join(character for character in text
                   if character in '0123456789abcdefABCDEF').upper()
    if len(text) < 2 or len(text) % 2:
        return None
    digits = ''.join(text[i + 1] + text[i] for i in range(0, len(text), 2))
    digits = digits.rstrip('F')
    if not digits or not digits.isdigit():
        return None
    return digits


#: A 2-byte hex FID (the only selector shape a custom-file probe accepts).
_FID_RE = re.compile(r'^[0-9A-F]{4}$')

#: The status words the file endpoints name for the PWA's status line (the
#: legacy ``/api/read|write`` wording).  The status word itself stays in the
#: response, in the lower case form :class:`simple_server.commands.FileOps`
#: reports (the PWA compares ``6982``/``9804`` for its inline ADM action).
_SW_ERRORS = {
    '6982': 'Security status not satisfied',
    '6983': 'PIN blocked',
    '6985': 'Conditions of use not satisfied',
    '6a88': 'Referenced data not found',
    '6a82': 'File not found',
}


def _sw_error(sw):
    """A human name for a status word (lower-case hex in, legacy names out)."""
    return _SW_ERRORS.get(str(sw or '').strip().lower(), 'Error')


#: The request-body fields whose values are SCP80 key material (masked in the
#: optional request log).  The project policy says card keys in local logs are
#: not a risk, but masking them is cheap and keeps a pasted log shareable;
#: identities and preset names stay visible for diagnostics.
_REDACT_FIELDS = ('adm', 'pin1', 'puk1', 'pin2', 'puk2', 'kicKey', 'kidKey',
                  'pskKey', 'psk_hex')


def redact_body(body):
    """A copy of a request body with its key material masked (the request log).

    The preset endpoints carry the keysets both at the top level
    (``POST /api/presets``, the import) and under ``fields`` (the update), so
    both are walked.  The outfile import's ``text`` is deliberately logged
    verbatim: it is the vendor file, which carries the card's KIC/KID keys by
    nature and is pasted as-is for diagnostics.
    """
    if not isinstance(body, dict):
        return body
    out = dict(body)
    for field in _REDACT_FIELDS:
        if out.get(field):
            out[field] = '<redacted>'
    if isinstance(out.get('psk_map'), dict):
        out['psk_map'] = {key: '<redacted>' for key in out['psk_map']}
    elif isinstance(out.get('psk_map'), list):
        out['psk_map'] = [
            dict(entry, psk_hex='<redacted>')
            if isinstance(entry, dict) and entry.get('psk_hex') else entry
            for entry in out['psk_map']]
    if isinstance(out.get('fields'), dict):
        out['fields'] = redact_body(out['fields'])
    if isinstance(out.get('keysets'), list):
        out['keysets'] = [redact_body(entry) if isinstance(entry, dict)
                          else entry for entry in out['keysets']]
    if isinstance(out.get('presets'), list):
        out['presets'] = [redact_body(entry) if isinstance(entry, dict)
                          else entry for entry in out['presets']]
    return out


def _norm_segments(segments):
    """Normalise a selector chain: upper case, ``3F00`` -> ``MF``, no empties.

    A segment may itself carry a path (``'MF/7F10'``); the chain is flattened.
    """
    out = []
    for segment in segments or ():
        text = str(segment if segment is not None else '').strip().upper()
        for part in text.split('/'):
            part = part.strip()
            if not part:
                continue
            out.append('MF' if part == '3F00' else part)
    return out


def _body_segments(body):
    """The target selector chain of a file-endpoint request body.

    ``path`` names the whole chain; otherwise the selector is ``fid`` (the
    PWA sends both, the FID is authoritative) or ``name``, and the parent
    chain is ``parent_path`` (a list, or a ``'/'`` string).

    A bare ``parent_sel`` (the legacy single-segment form) is resolved as a
    root selector: it works for an ADF name/AID and for a DF FID that is
    unique in the model, and a FID that appears in several DFs is
    :class:`~simple_server.files.AmbiguousFile` (a 404) rather than a guess -
    the probe is then rooted at MF.  ``parent_path`` is the unambiguous form
    and what the PWA sends.
    """
    path = body.get('path')
    if path:
        return _norm_segments([path])
    fid = body.get('fid')
    name = fid if fid else body.get('name')
    parent_path = body.get('parent_path')
    if parent_path:
        if isinstance(parent_path, (list, tuple)):
            parents = list(parent_path)
        else:
            parents = [parent_path]
    else:
        parent_sel = body.get('parent_sel')
        parents = [parent_sel] if parent_sel else []
    if name is not None and str(name).strip():
        parents.append(name)
    return _norm_segments(parents)


class ServerState(object):
    """One server's mutable state: the link, the card and the session caches.

    The constructor builds no transport and touches no card - a cardless start
    is a normal state.  :meth:`open` performs the startup initialization (build
    the link, :func:`simple_server.cards.init_card`, then the post-equip
    sequence); :meth:`equip` re-initializes the current card and
    :meth:`disconnect` tears the session down.

    **Threads.**  Two daemon threads are owned here: the background STATUS poll
    (:meth:`start_poll`, started by :meth:`open`) and the presence observer,
    which is pyscard's own ``CardMonitor`` observer rather than a thread we
    spin (see :meth:`start_monitor`).  :meth:`close` stops both.

    **The injected hooks.**  ``transport_factory(reader_name)`` builds a
    transport (default :class:`simple_server.transport.PcscLink`);
    ``transport`` injects one directly (tests); ``presets`` injects the card
    preset store (default ``pysim_simple_server.presets.PresetStore``), and
    ``presets_path`` builds that store at the given file (the entry point's
    ``--card-presets`` - the store is pysim-free, so it is a direct
    dependency); ``presence_install`` registers the presence observer (tests
    replace it to avoid real pyscard polling).  ``monitor=False`` disables the
    presence observer and ``open(start_threads=False)`` keeps the background
    poll off - the two test seams.  ``sms_oa``/``sms_sc`` are the SMS-PP
    download addresses (the legacy CLI options).

    **Simplifications vs the legacy server** (documented, not hidden):

    * The legacy equip drove pySim's shell (``onecmd_plus_hooks('equip')``)
      with its swallowed-exception detection, the command-set registration
      probe and the fast-init reset-free path.  Here equip is
      :func:`simple_server.cards.init_card` - a connect/reset + detect + MF
      select - so every equip is a full re-initialization (no reset-free fast
      init yet).  The card's own session marker is still ``card_ready_session``
      (set at the end of the post-equip sequence, the v3.28.0 audit's meaning).
    * The presence watchdog (restarting pyscard's monitor thread after a pcscd
      restart) and the auto-equip re-arm backoff are not ported: a lost monitor
      needs a server restart until 3d adds the watchdog.  An insertion marks the
      link stale so the first auto-equip attempt rebuilds it (v3.32.4) and the
      worker retries a bounded number of times (the legacy
      ``_AUTO_EQUIP_ATTEMPTS``).  A transport failure in the background poll
      keeps ``card_present`` (the card is usually still inserted) and re-arms
      the auto-equip, so the session recovers without a manual Equip.
    * The legacy gated the EF.ARR/EF.PBR reads on a readable ICCID (they fed the
      network-state monitor).  This port reads the two caches unconditionally:
      they are best effort, they belong to the file views (not the monitor) and
      a card whose ICCID read fails may still serve them.  The legacy order
      (ICCID, then ARR/PBR, then the TERMINAL PROFILE) is kept.
    * The poll is a daemon loop with one-second slices rather than a chain of
      ``threading.Timer`` callbacks: an interval change takes effect within a
      second and a disconnect/disable needs no timer bookkeeping.  Like the
      legacy, a card-free or disconnected session keeps the loop alive but
      sends nothing, and ``poll_enabled`` survives an equip (the operator's
      preference, not the card's).
    * The network-state monitor is a field here; its read/compute port belongs
      with the network endpoints (3c-3c), so ``netstate`` stays ``None``.
    * ``selection`` is owned by this state and published by the file endpoints
      (3c-3b) - ``{path, name, fid, kind, size, record_len, num_records}``; the
      post-equip reads restore MF and never publish a selection.  It describes
      the last file a file endpoint addressed, not a live mirror of the card's
      selected file.
    """

    def __init__(self, transport=None, transport_factory=None, reader=None,
                 presets=None, presets_path=None, web_dir=None,
                 terminal_profile=DEFAULT_TERMINAL_PROFILE,
                 poll_interval=DEFAULT_POLL_INTERVAL, menu_timeout=60,
                 sms_oa='12345', sms_sc='12345678912',
                 auto_equip=True, monitor=True, version=VERSION,
                 presence_install=None, presence_remove=None,
                 readers_fn=None):
        self.version = version
        self.web_dir = web_dir
        self.terminal_profile = (terminal_profile or '')
        self.transport_factory = transport_factory
        self.transport = transport
        self.reader_name = (reader if reader is None
                            else str(reader).strip() or None)
        self.readers_fn = readers_fn
        self.tracer = None
        self.proactive_handler = None
        self.menu_timeout = menu_timeout
        self.auto_equip_enabled = bool(auto_equip)
        self.monitor_enabled = bool(monitor)
        self.presence_install = presence_install
        self.presence_remove = presence_remove

        self.card_lock = threading.RLock()
        self.files = files_module.FileModel.from_asset()
        if presets is not None:
            self.presets = presets
        elif presets_path is not None:
            # The entry point's ``--card-presets``: the store path is a
            # deployment concern, so it is a constructor argument here.
            self.presets = presets_store.PresetStore(path=presets_path)
        else:
            self.presets = presets_store.PresetStore()
        # The SMS-PP download addresses (the legacy ``server.sms_oa``/
        # ``sms_sc`` CLI options).
        self.sms_oa = str(sms_oa or '12345')
        self.sms_sc = str(sms_sc or '12345678912')

        self.card = None
        self.cat = None
        self.bip = None
        self.atr = None
        self.iccid = None
        self.selection = None
        self.card_session = 0
        self.card_ready_session = 0
        self.card_present = False
        self.equipping = False
        self.connected = False
        self.test_running = False
        self.arr = None
        self.pbr = None
        self.netstate = None
        #: The ADM latch the 3d test runner will read: the last card session an
        #: ADM verify succeeded in (and, for a failed gate, the one it failed
        #: in - set by the suite runner, not by ``/api/verify-adm``).
        self.adm_session = None
        self.adm_failed_session = None

        # The poll state: the operator's preference (poll_enabled), the
        # effective interval and the card's POLLING OFF wish.  poll_interval 0
        # disables polling like the legacy --poll-interval 0.
        self.poll_enabled = int(poll_interval) > 0
        self.poll_interval = max(0, min(255, int(poll_interval)))
        self.poll_suspended = False
        self._poll_thread = None
        self._poll_stop = threading.Event()

        self._transport_stale = False
        self._equip_busy = False
        self._presence_observer = None
        self._readers_cache = None
        self._readers_at = 0.0
        #: Set by :meth:`close`: the auto-equip worker observes it between
        #: attempts so a shutdown never races a fresh equip.
        self._closed = threading.Event()
        self._equip_thread = None

        # A transport injected by the caller owns its reader name.
        if transport is not None and self.reader_name is None:
            self.reader_name = getattr(transport, 'reader_name', None)

    # ---- card lifecycle ----------------------------------------------------

    def open(self, reader=None, transport=None, start_threads=True):
        """Build the link and run the startup initialization.

        ``transport`` injects a link (the tests' fake); otherwise
        :attr:`transport` or ``transport_factory(reader)`` builds one.  The
        sequence is :meth:`_init_session`'s (the legacy startup init): detect
        the card and select MF, then the CAT-free reads - EF.ICCID, EF.ARR,
        EF.PBR - and only then the TERMINAL PROFILE + drain, so no CAT session
        is active during the reads.

        **A cardless start is a normal state.**  The background poll and the
        presence monitor start even when the card initialization fails; the
        failure is then re-raised so the caller (the 3d entry point) can report
        it - the presence monitor is what equips the first card later, so it
        must be running either way.  ``start_threads=False`` skips both
        threads (the tests' seam).
        """
        error = None
        with self.card_lock:
            self._teardown_session(bump=False)
            try:
                if transport is not None:
                    self.transport = transport
                elif self.transport is None:
                    self.transport = self._build_transport(
                        reader if reader is not None else self.reader_name)
                if reader is not None:
                    self.reader_name = str(reader).strip() or self.reader_name
                self.reader_name = (
                    self.reader_name
                    or getattr(self.transport, 'reader_name', None))
                self._transport_stale = False
                self._init_session()
            except Exception as exc:
                self._teardown_session(bump=True)
                error = exc
                if not self.reader_name:
                    self.reader_name = getattr(
                        self.transport, 'reader_name', None)
        if start_threads:
            self.start_poll()
            self.start_monitor()
        if error is not None:
            raise error
        return self.status()

    def equip(self):
        """Re-initialize the current card (the ``POST /api/equip`` action).

        Rebuilds a stale link first (a card swap / a failed attempt leaves the
        PC/SC handle unusable - the v3.32.4 rule), then runs the same post-equip
        sequence as :meth:`open`.  Raises the underlying
        :class:`~simple_server.transport.ReaderError` /
        :class:`~simple_server.cards.CardError` on failure after clearing the
        session, so the caller can report the cause.

        **A failed attempt marks the link stale** (the legacy v3.32.2 rule): a
        half-connected PC/SC handle usually cannot be revived, so attempt 2/3
        must build a fresh link instead of reusing it.
        """
        with self.card_lock:
            self.equipping = True
            try:
                self._teardown_session(bump=False)
                if self._transport_stale and self.transport is not None:
                    old = self.transport
                    self.transport = None
                    try:
                        old.disconnect()
                    except Exception as exc:
                        log.debug('releasing the old link failed: %s', exc)
                self._transport_stale = False
                if self.transport is None:
                    self.transport = self._build_transport(self.reader_name)
                self._init_session()
            except Exception:
                # Never leave a half-initialized session: a failed attempt is a
                # session change (the UI resets its views).  The link is
                # suspect too - the next attempt rebuilds it.
                self._transport_stale = True
                self._teardown_session(bump=True)
                raise
            finally:
                self.equipping = False
        return self.status()

    def reset(self):
        """Physically reset the card and re-initialize it (``POST /api/reset``).

        A reset is a power cycle plus a full re-initialization, and
        :func:`simple_server.cards.init_card` (behind :meth:`equip`) already
        resets/connects the card.  The endpoint is therefore exactly
        :meth:`equip`: an extra ``reset_card`` here would power-cycle the card
        twice per request (the legacy reset re-init did one).
        """
        return self.equip()

    def disconnect(self, stale=False, keep_present=False):
        """Tear the card session down; ``stale`` marks the link unusable.

        Clears the card, the CAT/BIP sessions and the caches, bumps
        ``card_session`` and leaves ``card_present`` false.  ``stale`` releases
        the PC/SC link so the next equip builds a fresh one (a removed card's
        dead handle can never be revived).  ``keep_present`` is for a transport
        failure in a running session: the card is usually still inserted and
        pyscard only notifies on *changes*, so clearing the flag would strand
        the server at "no card" (auto-equip refuses without it) until a manual
        Equip - the legacy kept ``card_present`` on an error too.
        """
        with self.card_lock:
            self._teardown_session(bump=True)
            if not keep_present:
                self.card_present = False
            if stale and self.transport is not None:
                try:
                    self.transport.disconnect()
                except Exception as exc:
                    log.debug('PC/SC disconnect failed: %s', exc)
                self.transport = None
                self._transport_stale = False

    def close(self):
        """Stop the background threads and drop the card session.

        ``_closed`` is set first so the auto-equip worker never starts another
        attempt, then the worker is joined (bounded: its retry wait observes
        the same event).  **Caveat:** a worker stuck inside a blocking
        ``SCardTransmit`` cannot be cancelled - the legacy watchdog has the
        same limit (pcsc-lite serializes a disconnect behind the pending
        transmit), so the join is bounded and shutdown proceeds without it.
        """
        self._closed.set()
        self.stop_poll()
        self.stop_monitor()
        thread = self._equip_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        with self.card_lock:
            self._teardown_session(bump=True)
            self.card_present = False
            transport, self.transport = self.transport, None
            if transport is not None:
                try:
                    transport.disconnect()
                except Exception:
                    pass

    def switch_reader(self, name):
        """Switch the active PC/SC reader (``POST /api/reader``).

        Rebuilds the transport for the chosen reader, resets and re-initializes
        its card and bumps ``card_session`` (each reader has its own card, so
        the previous selection/CAT session/ICCID/caches are meaningless).  The
        endpoint refuses the switch while a test run or a BIP session owns the
        card (:class:`CardBusyError` -> ``409``); a cardless reader is not an
        error for the switch itself - the error is reported (``503`` with the
        status body) and the reader stays selected for the next equip.
        """
        with self.card_lock:
            self.equipping = True
            try:
                name = check_reader(name, self._readers())
                if self.test_running:
                    raise CardBusyError(
                        'a test run owns the card; stop it first')
                if self.bip is not None and self.bip.owner:
                    raise CardBusyError(
                        'a BIP session owns the card; stop it first')
                old = self.transport
                self.transport = None
                self.reader_name = name
                self._teardown_session(bump=False)
                if old is not None:
                    try:
                        old.disconnect()
                    except Exception as exc:
                        log.debug('releasing the old link failed: %s', exc)
                transport = self._build_transport(name)
                self.transport = transport
                try:
                    self._init_session()
                except Exception:
                    self._teardown_session(bump=True)
                    raise
            finally:
                self.equipping = False
        return self.status()

    # ---- the post-equip sequence ------------------------------------------

    def _init_session(self):
        """Connect/reset, detect the card and run the post-equip sequence.

        The order is the legacy startup init's: the card is detected and MF
        selected by :func:`~simple_server.cards.init_card`, then the CAT-free
        reads (EF.ICCID, EF.ARR, EF.PBR) run *before* the TERMINAL PROFILE
        starts a CAT session, and the profile's answers are drained.  The next
        ``card_session`` value is the caller's session marker; the ready marker
        is set only after the whole sequence succeeded.
        """
        transport = self.transport
        if transport is None:
            raise RuntimeError('no transport to initialize')
        card = cards.init_card(transport)
        self.card = card
        self.atr = (bytes(card.atr).hex() if card.atr else None)
        self.cat = cat_session.CatSession(
            transport, cat_cla=card.cla,
            test_running=lambda: self.test_running,
            bip_handler=self._bip_handler,
            bip_flush=self._bip_flush,
            menu_timeout=self.menu_timeout,
            poll_interval=self.poll_interval,
            card_lock=self.card_lock)
        self.bip = bip_module.BipSession(
            self.cat, card_lock=self.card_lock,
            card_connected=lambda: self.connected)
        self.connected = True
        self.card_present = True
        self.card_session += 1
        # The caches belong to the previous card session.
        self.iccid = None
        self.arr = None
        self.pbr = None
        self.netstate = None
        self.selection = None
        # ICCID/ARR/PBR in the CAT-free window (the legacy order), then MF.
        try:
            self.iccid = self._read_iccid()
        except Exception as exc:                # best effort, never fatal
            log.debug('ICCID read failed: %s', exc)
        self.arr = self._read_ef_cache(ARR_READ_PATHS, ARR_RECORD_LIMIT)
        self.pbr = self._read_ef_cache(PBR_READ_PATHS, PBR_RECORD_LIMIT)
        self._restore_mf()
        self._send_terminal_profile()
        self._drain_proactive()
        self.poll_suspended = bool(getattr(self.cat, 'poll_suspended', False))
        self._adopt_poll_interval()
        self.card_ready_session = self.card_session

    def _send_terminal_profile(self):
        """Send TERMINAL PROFILE and let the CAT session answer its commands."""
        profile = (self.terminal_profile or '').strip()
        if not profile or self.cat is None:
            return
        try:
            self.cat.send_terminal_profile(profile)
        except Exception as exc:
            # A card that refuses the profile is not a dead session: the file
            # operations still work.  The CAT conversation stays unavailable.
            log.warning('TERMINAL PROFILE failed: %s', exc)

    def _drain_proactive(self):
        """Drain the proactive commands the profile raised (bounded, 3 rounds).

        Port of the legacy startup drain loop: STATUS; a ``91XX`` is answered
        with the chain (which runs its own trailing STATUS); the loop then
        polls again, so a card that yields the chain (the round bound, a test
        run) is picked up by the next round instead of being left pending.
        ``CatSession.handle_chain`` returns ``None`` unless an ``on_fetch``
        exits the chain, so its return value says nothing about the card's
        state - the outer STATUS is the check (the legacy's "break when the
        chain's last SW was idle" cannot be reproduced from that return value).
        """
        if self.cat is None:
            return
        for _round in range(3):
            try:
                _data, sw = self.cat.send_status()
            except Exception as exc:
                log.debug('startup STATUS failed: %s', exc)
                return
            if sw[:1] != b'\x91':
                return
            try:
                self.cat.handle_chain(sw, max_rounds=POLL_CHAIN_MAX)
            except Exception as exc:
                log.warning('startup proactive chain failed: %s', exc)
                return

    def _teardown_session(self, bump):
        """Drop the card session's state (a re-init, a disconnect, a failure)."""
        cat, self.cat = self.cat, None
        if cat is not None:
            try:
                cat.cancel_timers()
            except Exception:
                pass
            try:
                cat.cancel_menu_timeout()
            except Exception:
                pass
        bip, self.bip = self.bip, None
        if bip is not None:
            try:
                bip.stop()
            except Exception as exc:
                log.debug('BIP session stop failed: %s', exc)
        self.card = None
        self.connected = False
        self.atr = None
        self.iccid = None
        self.selection = None
        self.arr = None
        self.pbr = None
        self.netstate = None
        self.poll_suspended = False
        if bump:
            self.card_session += 1

    # ---- the injected CAT/BIP hooks ---------------------------------------

    def _bip_handler(self, cmd_num, cmd_type, cmd_qual, raw, dev_src, dev_dst,
                     cd_tag=None):
        """The CAT chain's BIP handler (``None`` = the generic TR)."""
        if self.bip is None:
            return None
        return self.bip.handle_command(cmd_num, cmd_type, cmd_qual, raw,
                                       dev_src, dev_dst, cd_tag)

    def _bip_flush(self):
        """The CAT chain's channel-status event flush."""
        if self.bip is not None:
            self.bip.flush_channel_events()

    # ---- the card-free reads ----------------------------------------------

    def _file_ops(self, transport=None):
        """A :class:`~simple_server.commands.FileOps` for the equipped card.

        ``transport`` overrides the link (the file endpoints pass their
        instrumented proxy); the CLA and the SELECT form follow the card class
        (``00``+FCP for a UICC/eUICC, ``a0``+the TS 51 011 form for a 2G SIM).
        """
        card = self.card
        uicc = card is None or card.kind in ('uicc', 'euicc')
        return commands.FileOps(
            transport if transport is not None else self.transport,
            cla='00' if uicc else 'a0',
            status_cla=(card.cla if card is not None else '80'),
            sel_fcp=uicc)

    def _read_iccid(self):
        """Best-effort EF.ICCID (MF/2FE2); the E.118 digits or ``None``.

        EF.ICCID is a mandatory 10-octet transparent EF (TS 102 221 §13.2), so
        the read passes the fixed length: it works even when the SELECT's FCP
        does not carry the size.  Never raises.
        """
        ops = self._file_ops()
        try:
            chain = self.files.path_chain('EF.ICCID')
        except Exception:
            chain = ['3F00', '2FE2']
        try:
            fcp = ops.select_path(chain)
            data, _sw = ops.read_file(fcp, length=10)
        except Exception:
            return None
        return decode_iccid(data)

    def _read_ef_cache(self, paths, record_limit):
        """Read the record-structured EF copies of ``paths`` (best effort).

        Returns the legacy cache shape - a list of
        ``{'fid','path','record_len','num_records','records'}`` with the
        records as upper-case hex - or ``None`` when no copy was readable.
        ``path`` is the legacy **FID label** (``MF/7F10/6F06``), never a
        symbolic DF name: the PWA resolves an FCP ``8B`` reference and the PBR
        children by comparing this path against its FID-based file paths.  A
        failed selection/read skips that copy; a non-record file is skipped.
        """
        out = []
        ops = self._file_ops()
        for label, chain in paths:
            try:
                fcp = ops.select_path(chain)
                if fcp.structure not in ('linear_fixed', 'cyclic'):
                    continue
                count = min(fcp.num_of_rec or 1, record_limit)
                records = []
                for index in range(1, count + 1):
                    data, _sw = ops.read_file(fcp, rec=index)
                    records.append(bytes(data or b'').hex().upper())
                out.append({'fid': chain[-1].upper(), 'path': label,
                            'record_len': fcp.record_len,
                            'num_records': len(records),
                            'records': records})
            except Exception as exc:
                log.debug('EF cache read %s failed: %s', label, exc)
                continue
        return out or None

    def _restore_mf(self):
        """Return to MF after the post-equip reads (best effort)."""
        try:
            self._file_ops().select_mf()
        except Exception as exc:
            log.debug('MF restore failed: %s', exc)

    # ---- the file endpoints (3c-3b) ---------------------------------------
    #
    # The PWA's file manager, its "probe all files" walk, the snapshot scan and
    # the Profiler's tree walk address a file the same two ways: a full ``path``
    # ('MF/2FE2', 'ADF.USIM/6F07') or a selector (``fid``/``name``) plus its
    # parent chain (``parent_sel``/``parent_path``).  Resolution goes through
    # the file model segment by segment, so it stays parent-scoped: a same-FID
    # file under another DF is never picked.
    #
    # A model-unknown 4-hex FID under a model-known DF is a **custom file** -
    # and the EF.PBR phonebook files are exactly that (they are not in the
    # standard list, so the tree injects them).  It is selected by FID with no
    # ``allow_probe`` flag: the legacy probe/detach dance existed to protect
    # pySim's model from the probe, and this model is never mutated by a
    # select.

    def file_tree(self, body):
        """``POST /api/tree``: the requested node's children from the model.

        The node itself is SELECTed first - that is what answers ``exists``
        (and probes a custom file) - then its children come from
        :meth:`~simple_server.files.FileModel.children` and the EF.PBR
        injection (:func:`simple_server.pbr.inject_children`).  ADF roots are
        appended under MF: an ADF sits beside MF in the model (it is selected
        by its DF name, TS 102 221 §11.1.1.1) but the tree shows it as an MF
        child, carrying its ``aid`` so the Profiler's walk can root it by AID.

        The model lists what the standard file list describes, not what the
        card holds: a file or ADF the card does not have appears in the tree
        and answers ``exists: false`` when it is selected.
        """
        ops, _timed = self._file_endpoint_ops()
        info, probe, fcp, path = self._select_node(body, ops)
        children = self._model_children(info)
        pbr_module.inject_children(self.pbr,
                                   pbr_module.norm_path(path.split('/')),
                                   children)
        return {'exists': True,
                'name': (info.name if info is not None
                         else path.rsplit('/', 1)[-1]),
                'fid': (info.fid or None) if info is not None else probe[1],
                'file_type': self._node_file_type(info, fcp),
                'children': children}

    def file_select(self, body):
        """``POST /api/select``: SELECT the node and report its FCP metadata.

        The response is the legacy shape the PWA's file manager, the snapshot
        scan and the Profiler consume (``file_size``/``record_len``/
        ``num_of_rec``/``fci_hex``/``apdu_times``); ``fci_hex`` is the raw
        SELECT response (the FCP/FCI template) or ``None`` when the card
        returned none (a 2G SIM's SELECT carries no FCP).  The file becomes
        the published ``/api/status`` ``selection``.
        """
        ops, timed = self._file_endpoint_ops()
        info, probe, fcp, path = self._select_node(body, ops)
        self._publish_selection(info, probe, fcp, path)
        return {'name': (info.name if info is not None
                         else path.rsplit('/', 1)[-1]),
                'fid': (info.fid or None) if info is not None else probe[1],
                'file_type': self._node_file_type(info, fcp),
                'file_size': fcp.file_size,
                'record_len': fcp.record_len,
                'num_of_rec': fcp.num_of_rec,
                'fci_hex': fcp.raw or None,
                'apdu_times': timed.times,
                'exists': True}

    def file_read(self, body):
        """``POST /api/read``: SELECT and read the file.

        A transparent EF answers ``data`` (upper-case hex, the whole file); a
        linear-fixed/cyclic EF answers ``records`` as ``[{num, data}]``
        (1-based).  ``mode`` is ``'raw'`` only - the legacy ``'decoded'`` mode
        ran pySim's server-side EF decoders, which this server does not have;
        the PWA decodes client-side from these same bytes, so the mode is
        refused with a 400 instead of being silently ignored.

        A card-level read failure (a refused READ) is a ``200`` with
        ``success: false`` and the card's ``sw`` - the PWA prints the status
        word and offers its ADM action for ``6982``/``9804`` (the legacy lower
        case form).  Only a failed SELECT is a ``404`` (the file is not there).

        A record file whose FCP announces a count **must** be read to that
        count: a refusal mid-file is a ``success: false`` with the refusing
        ``sw`` and the partial ``records`` (see :meth:`_read_records`).  A
        reported count is the card's promise; the PWA's ``if (rd && rd.success)``
        would otherwise store a truncated list in a snapshot or profile.
        """
        mode = str(body.get('mode') or 'raw').strip().lower()
        if mode != 'raw':
            raise ValueError(
                "mode %r is not supported: /api/read returns the raw bytes "
                "(mode 'raw'); the decoded view is client-side.  The legacy "
                "'decoded' mode was pySim's server-side EF decoder and is "
                "gone with pySim." % (body.get('mode'),))
        ops, timed = self._file_endpoint_ops()
        info, probe, fcp, path = self._select_node(body, ops)
        file_type = self._node_file_type(info, fcp)
        self._publish_selection(info, probe, fcp, path)
        try:
            if file_type in ('linear_fixed', 'cyclic'):
                records, sw, refusal = self._read_records(ops, fcp)
                if refusal is None:
                    result = {'success': True, 'sw': sw,
                              'file_type': file_type, 'records': records}
                else:
                    refused_sw, message = refusal
                    result = {'success': False, 'sw': refused_sw,
                              'file_type': file_type,
                              'error': (_sw_error(refused_sw) if refused_sw
                                        else message),
                              'records': records}
            else:
                data, sw = ops.read_file(fcp)
                result = {'success': True, 'sw': sw, 'file_type': file_type,
                          'data': bytes(data or b'').hex().upper()}
        except (commands.FileError, ValueError) as exc:
            sw = getattr(exc, 'sw', None)
            return {'success': False, 'sw': sw, 'file_type': file_type,
                    'error': _sw_error(sw) if sw else str(exc)}
        result['apdu_times'] = timed.times
        return result

    def file_write(self, body):
        """``POST /api/write``: SELECT and UPDATE BINARY/UPDATE RECORD.

        ``data`` (hex) and ``record_nr`` are validated **before** the
        selection, so a malformed request spends no APDU.  A record file takes
        ``record_nr`` (default record 1, the legacy behaviour); a transparent
        file takes no ``record_nr`` (``UPDATE RECORD`` on a transparent EF is
        a caller error, not the card's) - that mismatch is known only after
        the SELECT and is refused **before any write**.  An EF.ARR write
        refreshes the session cache: the ``8B`` references the file manager
        resolves may have changed.

        A card-level write failure is a ``200`` with ``success: false`` and the
        card's status word, like :meth:`file_read`.
        """
        payload = commands.data_bytes(body.get('data'))
        record = body.get('record_nr')
        record = (commands.record_number(record)
                  if record not in (None, '') else None)
        ops, _timed = self._file_endpoint_ops()
        info, probe, fcp, path = self._select_node(body, ops)
        file_type = self._node_file_type(info, fcp)
        if record is not None and file_type not in ('linear_fixed', 'cyclic'):
            raise ValueError('record_nr %d was given but the file is %s, not '
                             'a record file' % (record, file_type))
        self._publish_selection(info, probe, fcp, path)
        try:
            if record is not None or file_type in ('linear_fixed', 'cyclic'):
                sw = ops.update_record(record or 1, payload)
            else:
                sw = ops.update_binary(0, payload)
        except (commands.FileError, ValueError) as exc:
            sw = getattr(exc, 'sw', None)
            return {'success': False, 'sw': sw,
                    'error': _sw_error(sw) if sw else str(exc)}
        self._refresh_arr_after_write(info, probe)
        return {'success': True, 'sw': sw}

    def arr_cache(self):
        """The cached EF.ARR copies (``GET /api/arr``) - card-free.

        The legacy response shape: ``{ok, session, arrs}``; ``arrs`` is ``[]``
        when the card has no readable copy (the PWA then resolves no ``8B``
        reference).
        """
        arrs = self.arr
        return {'ok': bool(arrs), 'session': int(self.card_session),
                'arrs': arrs or []}

    def pbr_cache(self):
        """The cached EF.PBR records (``GET /api/pbr``) - card-free.

        ``{ok, session, pbrs}``; the PWA owns the decoder (``efDecPbr``), the
        server serves the raw records.
        """
        pbrs = self.pbr
        return {'ok': bool(pbrs), 'session': int(self.card_session),
                'pbrs': pbrs or []}

    def _file_endpoint_ops(self):
        """``(FileOps, TimedTransport)`` for one file endpoint.

        The timing proxy is per request (the snapshot scan reads
        ``apdu_times``), so one request can never see another's timings.  A
        cardless server raises :class:`NoCardSessionError` (the endpoint
        answers 503) instead of failing inside the exchange.
        """
        if not self.connected or self.transport is None:
            raise NoCardSessionError('no card session: equip a card first')
        timed = commands.TimedTransport(self.transport)
        return self._file_ops(timed), timed

    def _select_node(self, body, ops):
        """Resolve the request and SELECT it; ``(info, probe, fcp, path)``.

        ``info`` is the model file, ``probe`` the ``(parent_info, fid)`` pair
        of a model-unknown FID (exactly one of the two is set), ``fcp`` the
        last SELECT's parsed FCP and ``path`` the canonical FID path the
        status ``selection`` and the PBR comparison use.
        """
        info, probe, chain, path = self._resolve_target(_body_segments(body))
        fcp = ops.select_path(chain)
        return info, probe, fcp, path

    def _resolve_target(self, segments):
        """Resolve a selector chain to ``(info, probe, chain, path)``.

        Three shapes: a model file (``info`` set), a model-unknown FID under a
        model-known DF (``probe`` set - a custom file) and an all-FID chain the
        model does not describe, which is walked by FID from MF (or from the
        ADF it starts with) - the PWA's custom files may sit under a parent DF
        the standard list does not know.  Nothing is added to the model either
        way.
        """
        segments = _norm_segments(segments)
        if not segments:
            raise files_module.UnknownFile('empty file selector')
        try:
            info, probe = self._resolve_segments(segments)
        except files_module.UnknownFile:
            chain = self._raw_chain(segments)
            if chain is None:
                raise
            return None, (None, segments[-1]), chain, '/'.join(segments)
        if info is not None:
            return info, None, self.files.path_chain(info), info.path
        parent, fid = probe
        return (None, probe, self.files.path_chain(parent) + [fid],
                '%s/%s' % (parent.path, fid))

    def _raw_chain(self, segments):
        """A SELECT chain for an all-FID selector the model does not describe.

        The chain is rooted at MF (``3F00``) or at the DF/ADF the first segment
        names in the model; every following segment must be a 4-hex FID.  A
        SELECT by FID needs no model entry - that is the whole rule - so this
        only decides the root.  Returns ``None`` when the shape does not fit.
        """
        if len(segments) < 2:
            return None
        if segments[0] == 'MF':
            root, rest = self.files.resolve('MF'), segments[1:]
        else:
            root, _error = self._model_path([segments[0]])
            rest = segments[1:]
            if root is None or not root.is_df:
                return None
        if not rest or not all(_FID_RE.match(segment) for segment in rest):
            return None
        return self.files.path_chain(root) + list(rest)

    def _resolve_segments(self, segments):
        """Resolve a selector chain to a model file or a custom-file probe.

        Returns ``(info, None)`` for a model file and ``(None, (parent, fid))``
        for a model-unknown 4-hex FID whose parent DF the model knows.  Raises
        :class:`~simple_server.files.UnknownFile`/
        :class:`~simple_server.files.AmbiguousFile` otherwise (an ambiguous
        bare selector is never guessed).
        """
        segments = _norm_segments(segments)
        if not segments:
            raise files_module.UnknownFile('empty file selector')
        unknown = files_module.UnknownFile(
            'no file matches the selector %s' % '/'.join(segments))
        info, error = self._model_path(segments)
        if info is not None:
            return info, None
        if len(segments) == 1 and not isinstance(error,
                                                files_module.UnknownFile):
            raise error or unknown
        fid = segments[-1]
        if not _FID_RE.match(fid):
            raise error or unknown
        if len(segments) == 1:
            parent = self.files.resolve('MF')
        else:
            parent, parent_error = self._model_path(segments[:-1])
            if parent is None:
                raise parent_error or unknown
        if not parent.is_df:
            raise files_module.FileModelError(
                'the parent %s of FID %s is an EF, not a DF'
                % (parent.name, fid))
        return None, (parent, fid)

    def _model_path(self, segments):
        """Resolve a segment chain in the model; ``(None, error)`` when unknown.

        The retry without a leading ``MF`` covers the PWA's whole-chain form
        for an ADF (``MF/ADF.USIM/6F07``) and its AID form: an ADF is a root
        of its own.  It fires only when the second segment really is an ADF -
        an unconditional retry turned a wrong ``MF/EF.IMPI`` into ADF.ISIM's
        file (the selector says "an EF.IMPI directly under MF", which does not
        exist).  The returned error is the first attempt's (the authoritative
        selector), not the retry's.
        """
        attempts = [segments]
        if len(segments) > 1 and segments[0] == 'MF' \
                and self._is_adf_selector(segments[1]):
            attempts.append(segments[1:])
        error = None
        for index, attempt in enumerate(attempts):
            try:
                return self.files.resolve_path('/'.join(attempt)), None
            except files_module.FileModelError as exc:
                if index == 0:
                    error = exc
        return None, error

    def _is_adf_selector(self, segment):
        """True when ``segment`` names an ADF (the only root the retry drops).

        The ``MF/<ADF>/...`` retry exists because an ADF is a model root; a
        second segment that is an EF (or that the model does not know) must
        keep the path authoritative.
        """
        try:
            info = self.files.resolve(segment)
        except files_module.FileModelError:
            return False
        return info.kind == 'adf'

    def _model_children(self, info):
        """The tree children of a model node, in the PWA's node shape."""
        if info is None:                # a probed file: an EF has no children
            return []
        children = []
        for child in self.files.children(info):
            entry = {'name': child.name, 'fid': child.fid or None,
                     'isDir': child.is_df}
            if child.kind == 'adf' and child.aid:
                entry['aid'] = child.aid
            if child.source == 'custom':
                entry['custom'] = True
            children.append(entry)
        if info.kind == 'mf':
            # An ADF is a root beside MF in the model but an MF child in the
            # tree (the PWA roots an ADF node at the ADF and the Profiler's
            # walk roots an ADF child by its AID).
            for adf in self.files.iter_all():
                if adf.kind != 'adf':
                    continue
                entry = {'name': adf.name, 'fid': None, 'isDir': True}
                if adf.aid:
                    entry['aid'] = adf.aid
                if adf.source == 'custom':
                    entry['custom'] = True
                children.append(entry)
        return children

    @staticmethod
    def _node_file_type(info, fcp):
        """The API's ``file_type``: 'df' for a DF/ADF/MF, else the EF structure.

        A named EF whose SELECT returned no (or an unparseable) FCP reports
        'transparent', like the legacy ``_get_file_type``.  A descriptor whose
        structure bits are RFU (TS 102 221 Table 11.5) keeps the legacy
        ``'no_info_given'`` (``fcp.STRUCTURES``' ``'no_info'`` is the parser's
        internal name) - the PWA's stored profiles and snapshots pin that
        string with ``===``.  A probed custom file follows its FCP too; a
        probed DF carries the ``38`` descriptor, so it reads
        ``'no_info_given'``, not 'transparent'.
        """
        if info is not None and info.is_df:
            return 'df'
        structure = getattr(fcp, 'structure', None)
        if structure is None:
            return 'transparent'
        if structure == 'no_info':
            return 'no_info_given'
        return structure

    def _publish_selection(self, info, probe, fcp, path):
        """Publish the ``/api/status`` ``selection`` of the addressed file.

        The shape the Phase 3c status block expects:
        ``{path, name, fid, kind, size, record_len, num_records}``.  It
        describes the last file a file endpoint addressed - not a live mirror
        of the card's selected file (an EF.ARR write's cache refresh leaves an
        ARR copy selected on the card while the selection still names the
        written file).
        """
        fid = (info.fid if info is not None else probe[1]) or None
        self.selection = {
            'path': path,
            'name': (info.name if info is not None else fid),
            'fid': fid,
            'kind': (info.kind if info is not None else 'ef'),
            'size': fcp.file_size,
            'record_len': fcp.record_len,
            'num_records': fcp.num_of_rec,
        }

    @staticmethod
    def _read_records(ops, fcp):
        """Read every record of the selected record-structured file.

        Bounded by the FCP's record count.  A card that does not report the
        count is read until it refuses a record (the legacy pySim
        ``read_records`` tolerance - ``num_of_rec`` is ``None``, not ``0``),
        and a refusal of record 1 is an error.

        Returns ``(records, sw, refusal)``.  ``refusal`` is ``None`` when the
        whole announced file was read; otherwise it is ``(sw, message)`` for
        the record that stopped the loop.  A **reported** count is the card's
        promise, so stopping short of it is not a success: pySim's
        ``read_records`` raised ``SwMatchError`` there, and the legacy
        ``_cmd_exception`` turned that into ``{'success': false, ...}``.
        """
        limit = 255 if fcp.num_of_rec is None else fcp.num_of_rec
        records = []
        sw = commands.FCP_SW
        refusal = None
        while len(records) < limit:
            index = len(records) + 1
            try:
                data, sw = ops.read_file(fcp, rec=index)
            except (commands.FileError, ValueError) as exc:
                if not records:
                    raise
                if fcp.num_of_rec is not None:
                    sw = getattr(exc, 'sw', None)
                    refusal = (sw, str(exc))
                break
            records.append({'num': index,
                            'data': bytes(data or b'').hex().upper()})
        return records, sw, refusal

    def _refresh_arr_after_write(self, info, probe):
        """Re-read the EF.ARR cache after a write to one of its copies.

        Port of the legacy ``_arr_refresh_after_write``: the ``8B`` security
        attribute references the file manager resolves point into those
        records, so a write makes the cache stale.  Best effort - a failed
        re-read keeps the session.
        """
        fid = str(info.fid if info is not None else probe[1]).upper()
        if fid not in ('2F06', '6F06'):
            return
        try:
            self.arr = self._read_ef_cache(ARR_READ_PATHS, ARR_RECORD_LIMIT)
        except Exception as exc:
            log.debug('EF.ARR refresh after write failed: %s', exc)

    # ---- the preset store (3c-3c-1) ---------------------------------------

    def presets_info(self):
        """``GET /api/presets``: the store info plus every preset (card-free)."""
        store = self.presets
        info = (store.info() if hasattr(store, 'info')
                else {'path': getattr(store, 'path', None)})
        info = dict(info)
        info['presets'] = store.list()
        return info

    def preset_add(self, body):
        """``POST /api/presets``: create a preset; returns the stored shape."""
        try:
            return self.presets.add(body)
        except OSError as exc:
            raise self._store_failure(exc) from exc

    def preset_update(self, body):
        """``POST /api/presets/update``: a partial/full edit; None if unknown."""
        fields = body.get('fields')
        if not isinstance(fields, dict):
            fields = {k: v for k, v in body.items()
                      if k not in ('id', 'fields')}
        try:
            return self.presets.update(body.get('id'), fields)
        except OSError as exc:
            raise self._store_failure(exc) from exc

    def preset_delete(self, body):
        """``POST /api/presets/delete``: the legacy ``{ok, removed}`` body."""
        try:
            removed = self.presets.remove(body.get('id'))
        except OSError as exc:
            raise self._store_failure(exc) from exc
        return {'ok': True, 'removed': bool(removed)}

    def preset_import(self, body):
        """``POST /api/presets/import``: ``merge``/``replace`` (legacy shape)."""
        items = body.get('presets')
        if not isinstance(items, list) or len(items) > 1000:
            raise ValueError('presets must be a list of at most 1000 entries')
        mode = body.get('mode') if body.get('mode') in ('merge', 'replace') \
            else 'merge'
        try:
            result = self.presets.import_presets(items, mode)
        except OSError as exc:
            raise self._store_failure(exc) from exc
        result['ok'] = True
        return result

    def preset_import_outfile(self, body):
        """``POST /api/presets/import-outfile``: the vendor-outfile import.

        Without ``mode`` this is a dry run: the parsed presets and the report
        come back for the preview (the UI picks the common name, the 16-byte
        algorithm, the TAR table and its per-row keyset first); with ``mode``
        the same parse runs and the presets are imported.  A structurally
        invalid file is never partially imported (the preview shows the error,
        the import refuses).
        """
        text = body.get('text')
        if not isinstance(text, str) or not text.strip():
            raise ValueError('text is required (the outfile contents)')
        tars = body.get('tars')
        if tars is not None and not isinstance(tars, list):
            raise ValueError('tars must be a list of TAR entries')
        tar_keyset = body.get('tar_keyset')
        if tar_keyset in (None, ''):
            tar_keyset = None
        else:
            try:
                tar_keyset = int(tar_keyset)
            except (TypeError, ValueError):
                tar_keyset = -1
            if not 1 <= tar_keyset <= presets_store.KVN_MAX:
                raise ValueError('tar_keyset must be 1-%02X'
                                 % presets_store.KVN_MAX)
        # A misspelled algorithm must not silently pick one: the 16-byte key's
        # algorithm nibble decides whether the card accepts the packet at all.
        algorithm16 = str(body.get('algorithm16') or '3des2').strip().lower()
        if algorithm16 not in outfile.ALGORITHM16_CHOICES:
            raise ValueError('algorithm16 must be one of: %s'
                             % ', '.join(outfile.ALGORITHM16_CHOICES))
        mode = body.get('mode')
        if mode in (None, ''):
            mode = None
        elif mode not in ('merge', 'replace'):
            raise ValueError("mode must be 'merge', 'replace' or omitted "
                             '(a preview)')
        parsed = outfile.parse_outfile(
            text,
            name=str(body.get('name') or ''),
            algorithm16=algorithm16,
            tars=tars,
            tar_keyset=tar_keyset,
            start_counter=body.get('start_counter'))
        if mode is None:
            return {'ok': True, 'preview': True,
                    'presets': parsed['presets'], 'report': parsed['report']}
        if parsed['report'].get('error'):
            raise ValueError(parsed['report']['error'])
        try:
            result = self.presets.import_presets(parsed['presets'], mode)
        except OSError as exc:
            raise self._store_failure(exc) from exc
        response = {'ok': True, 'preview': False,
                    'report': parsed['report']}
        response.update(result)
        return response

    @staticmethod
    def _store_failure(exc):
        """A store I/O failure the handler answers as a JSON 500."""
        return RuntimeError('preset store write failed: %s' % exc)

    # ---- ADM verification (3c-3c-1) ---------------------------------------

    def verify_adm(self, body):
        """``POST /api/verify-adm``: VERIFY the card's ADM PIN (TS 102 221).

        The key comes from the request's ``adm`` or the named preset's ``adm``
        (4-16 hex digits in byte pairs, :data:`presets_store.ADM_PATTERN`),
        right-padded to the 8 CHV bytes with ``'f'`` like pySim's
        ``verify_adm``.  ``chv`` is the CHV number as a hex string (one byte,
        e.g. ``'0A'``) or as an integer byte value (``10`` is 0x0A, not a
        hex-encoded ``10``); it defaults to ``0x0A``.  The structured result
        warns about the remaining attempts - every failed VERIFY consumes one
        and a blocked ADM cannot be recovered here.
        """
        if not self.connected or self.transport is None:
            raise NoCardSessionError('no card session: equip a card first')
        adm = re.sub(r'\s', '', str(body.get('adm') or '')).upper()
        if not adm and body.get('preset_id'):
            preset = self.presets.get(str(body.get('preset_id')))
            if preset is None:
                raise ValueError('unknown preset id')
            adm = re.sub(r'\s', '', str(preset.get('adm') or '')).upper()
            if not adm:
                raise ValueError('the preset has no ADM key')
        if not re.fullmatch(presets_store.ADM_PATTERN, adm):
            raise ValueError('adm must be 4-16 hex digits')
        chv = body.get('chv')
        if isinstance(chv, int):
            # an integer is the byte value itself (10 = 0x0A), never a
            # hex-encoded string - the two readings disagree for 10..99
            chv_value = chv
        else:
            try:
                chv_value = (0x0A if chv in (None, '')
                             else int(str(chv), 16))
            except (TypeError, ValueError):
                raise ValueError('chv must be one hex byte') from None
        if not 0 <= chv_value <= 0xFF:
            raise ValueError('chv must be one hex byte')
        # The card's own CLA (00 UICC / A0 2G SIM), exactly like pySim-shell's
        # verify_adm - the startup placeholder could answer 6E00 on a UICC.
        cla = self._file_ops().cla
        fc = util.rpad(adm.lower(), 16)
        apdu = bytes.fromhex(cla + '2000' + '%02X' % chv_value + '08' + fc)
        _data, sw = self.transport.send_apdu(apdu)
        sw = bytes(sw or b'').hex().upper()
        log.info('VERIFY ADM -> SW: %s', sw)
        if sw == '9000':
            # the latch the 3d runner's ADM gate reads
            self.adm_session = self.card_session
            self.adm_failed_session = None
            return {'ok': True, 'sw': sw}
        if re.fullmatch(r'63C[0-9A-F]', sw):
            return {'ok': False, 'sw': sw, 'attempts_left': int(sw[3], 16)}
        if sw in ('6983', '9804'):
            return {'ok': False, 'sw': sw, 'blocked': True}
        return {'ok': False, 'sw': sw,
                'error': ('Security status not satisfied' if sw == '6982'
                          else 'Error')}

    # ---- the plain SCP80 secured-packet send (3c-3c-1) --------------------

    def send_ota(self, body):
        """``POST /api/send-ota``: deliver one secured packet (SCP80/TS 102 225).

        Two request paths: ``sp`` is a pre-built packet (sent as it is - its
        own counter is the caller's bookkeeping value) and ``apdu`` is a raw
        C-APDU the server wraps (the packet may span several SMs, TS 31.115
        §4.3).  The keyset: an explicit KIc/KID wins, otherwise the preset's
        default keyset for the packet's TAR (``tar``, else the ISD role entry
        for an ``apdu`` send) supplies both bytes and keys.  The counter: an
        explicit ``cntr`` wins, otherwise the preset store's value for the
        keyset; the effective value is echoed and the accepted one persisted
        (v3.20.0).  The response is the PWA's shape - ``success``, ``sw``,
        ``bytes``, ``segments``, ``por``, ``cntr``, ``final_cntr``,
        ``cntr_low``, ``warning`` - and a warning ENVELOPE still gets its PoR
        decoded so a low counter shows as ``cntr_low`` instead of a bare
        failure.

        ``ram_format`` rewrites an explicit expanded Command Scripting
        template (TS 102 226 §5.2.1); its per-operation ``auto`` detection
        belongs to the RAM operation sender (3c-3c-2), which will probe like
        the legacy ``_ram_detect_format``.  The plain send cannot probe without
        a RAM step, so ``auto`` reports the compact form it actually sends
        (the decision is recorded in ``docs/pysim-phaseout.md``).
        """
        if not self.connected or self.transport is None or self.cat is None:
            raise NoCardSessionError('no card session: equip a card first')
        body = dict(body or {})
        store = self.presets
        sp = str(body.get('sp') or '').strip()
        apdu = str(body.get('apdu') or '').strip()
        preset_id = str(body.get('preset_id') or '').strip()
        # The packet's keyset number (b8..b5 of KIc/KID, TS 102 225 A.2) must
        # be consistent - a mismatch is invalid input (the card rejects it
        # with "Unidentified security error") - and, when a preset is named,
        # defined in it.  Both are refused before anything is built.
        kvn, err = ota.kvn_of(body.get('kic', ''), body.get('kid', ''))
        if err:
            raise ValueError(err)
        err = ota.preset_keyset_check(store, preset_id, body.get('kic', ''),
                                      body.get('kid', ''))
        if err:
            raise ValueError(err)
        preset_ref = store.get(preset_id) if preset_id else None
        # SPI1: the caller's value wins (a hand send may deliberately ask for
        # more security than the TAR's minimum), with a warning below the
        # TAR's MSL; without one it is derived from the MSL of the packet's
        # TAR (TS 102 226 §8.2.1.3.2.4).  An APDU send without a TAR uses the
        # preset's ISD role entry.
        tar_ref = str(body.get('tar') or '').strip().upper()
        if not tar_ref and apdu and preset_ref:
            tar_ref = presets_store.role_tar(preset_ref, 'isd')
        spi1, spi_warn = ota.spi1_for_tar(preset_ref, tar_ref,
                                          body.get('spi1'))
        spi2 = str(body.get('spi2') or '01').strip().upper()
        spi2_val = int(spi2, 16)
        # Only a packet whose SPI1 asks the card to check the counter has a
        # counter to track (TS 102 225 §5.1.1 b5b4): with b5b4 = 00 the field
        # is ignored and never updated, so nothing is advanced or persisted.
        counter_tracked = scp80.counter_tracked(spi1)
        include_cpi = body.get('includeCpi', True)
        # The response form follows the command's TAR (TS 102 226 §4): only an
        # RM TAR answers with the RM structures; a hand send may name any TAR
        # (a pre-built `sp` may name none - unknown, structural fallback).
        rm = ota.tar_is_rm(preset_ref,
                           body.get('tar') or ('000000' if apdu else ''))
        ram_format = None
        if apdu:
            tar = str(body.get('tar') or '').strip().upper() or '000000'
            kvn, kic, kid, keyset_err = ota.op_keyset_resolve(store, body, tar)
            if keyset_err:
                raise ValueError(keyset_err)
            if not (kic and kid):
                raise ValueError(
                    'the packet needs KIc/KID - name a preset with a keyset '
                    'for TAR %s or pass them explicitly' % tar)
            kic_key = str(body.get('kicKey') or '').strip()
            kid_key = str(body.get('kidKey') or '').strip()
            # The counter: an explicit `cntr` wins; otherwise the preset
            # store's value for the packet's keyset (v3.20.0).
            cntr = ota.counter_seed(store, body, kvn)
            if body.get('ram_format') is not None:
                ram_format = ota.normalise_ram_format(body.get('ram_format'))
                apdu = ota.format_apdu(apdu, ram_format)
                if ram_format == 'auto':
                    # the RAM operation sender's per-operation probe (3c-3c-2)
                    ram_format = 'compact'
            try:
                packet, _spi = scp80.build_secured_packet(
                    spi1, spi2, kic, kid, tar, cntr, apdu, kic_key, kid_key)
            except scp80.Scp80Error as exc:
                raise ValueError(str(exc)) from exc
            sp_hex = bytes(packet).hex()
        else:
            # Regular SCP80: the pre-built packet carries its own counter;
            # `cntr` is the caller's bookkeeping value for final_cntr.
            sp_hex = sp
            cntr = str(body.get('cntr') or '').strip().upper()
        if cntr and not counter_tracked:
            log.info('OTA SEND: counter not tracked (SPI1 %s: no counter '
                     'check)', spi1)
        log.info('OTA SEND: SPI %s %s KIc %s KID %s TAR %s CNTR %s LEN %dB',
                 spi1, spi2, body.get('kic', ''), body.get('kid', ''),
                 body.get('tar', ''), cntr, len(sp_hex) // 2)
        if apdu:
            log.info('OTA C-APDU: %s', apdu)
        log.info('OTA SECURED-PACKET: %s', sp_hex)
        capture = cat_session.PorSubmitCapture()
        result = self._send_secured_packet(sp_hex, include_cpi=include_cpi,
                                           submit_handler=capture)
        cmd_len = len(body.get('apdu') or '') // 2 or None
        if not result.get('success'):
            resp = dict(result)
            if ram_format is not None:
                resp['ram_format'] = ram_format
            # A warning ENVELOPE may still carry the PoR (the card's counter
            # verdict): decode it so the caller sees cntr_low.
            failed_por_hex = (result.get('response_data')
                              or capture.submit_response_hex())
            failed_por = ota.decode_por(
                spi1, spi2, body.get('kic', ''), body.get('kid', ''),
                cntr, body.get('kicKey', ''), body.get('kidKey', ''),
                failed_por_hex, cmd_len=cmd_len, rm=rm)
            if failed_por:
                resp['por'] = failed_por
                low = ota.cntr_low_fields(failed_por.get('response_status'))
                if low:
                    resp.update(low)
                    log.info('OTA CNTR-LOW: the packet counter is not above '
                             "the card's")
                # only a packet the card accepted consumes the counter (a
                # warning ENVELOPE can still carry a por_ok PoR); a rejected
                # one must leave the store untouched
                if (ota.counter_valid(cntr) and counter_tracked
                        and failed_por.get('response_status')
                        in ('por_ok', 'actual_response_sms_submit')):
                    resp['final_cntr'] = ota.next_counter(cntr)
            log.info('OTA SEND FAILED: %s', result.get('error'))
        else:
            resp = {'success': True, 'sw': result.get('sw'),
                    'response_data': result.get('response_data'),
                    'bytes': result.get('bytes'),
                    'segments': result.get('segments')}
            por_src = 'envelope'
            por_hex = resp.get('response_data') or ''
            submit_hex = capture.submit_response_hex()
            if submit_hex:
                por_hex = submit_hex
                por_src = 'sms-submit'
            elif capture.submit_tpdu_hex:
                # no assembled UD: fall back to a raw RPI packet
                tpdu = bytes.fromhex(capture.submit_tpdu_hex)
                index = tpdu.find(b'\x02\x71\x00')
                if index >= 0:
                    por_hex = tpdu[index:].hex()
                    por_src = 'sms-submit'
            por = ota.decode_por(
                spi1, spi2, body.get('kic', ''), body.get('kid', ''),
                cntr, body.get('kicKey', ''), body.get('kidKey', ''),
                por_hex, cmd_len=cmd_len, rm=rm)
            if (bool(spi2_val & 0x01) and result.get('sw') == '9000'
                    and not por_hex and not capture.submit_tpdu_hex):
                # the PoR was requested and the card did not answer - true for
                # pre-built packets (the keyless form) too
                resp['por_missing'] = True
                log.info('OTA SEND: the card answered 9000 with no PoR '
                         '(SPI2 %s requests one)', spi2)
            if por:
                resp['por'] = por
                low = ota.cntr_low_fields(por.get('response_status'))
                if low:
                    # a 9000/91xx ENVELOPE can still carry a cntr_low PoR
                    resp.update(low)
                    log.info('OTA CNTR-LOW: the packet counter is not above '
                             "the card's")
                log.info('OTA PoR[%s]: status=%s TAR=%s CNTR=%s PCNTR=%s '
                         'RPL=%s RHL=%s', por_src, por.get('response_status'),
                         por.get('tar'), por.get('cntr'), por.get('pcntr'),
                         por.get('rpl'), por.get('rhl'))
            elif por_hex:
                log.info('OTA PoR[%s]: undecodable raw=%s', por_src, por_hex)
            else:
                log.info('OTA PoR[%s]: none', por_src)
            accepted = (por is None or por.get('response_status')
                        in ('por_ok', 'actual_response_sms_submit'))
            if ram_format is not None:
                resp['ram_format'] = ram_format
            # The counter to use next, reported for the RAM and the plain
            # SCP80 path alike; the server persists it.  A pre-built packet
            # may come without a counter and a packet whose SPI1 does not ask
            # the card to check it has none to track - nothing is reported
            # then (and int('') never happens).  A rejected verdict reports
            # nothing either: the store must not move on a packet the card
            # did not consume.
            if ota.counter_valid(cntr) and counter_tracked and accepted:
                resp['final_cntr'] = ota.next_counter(cntr)
        if cntr:
            # echo the effective counter (a request without one got the
            # preset's value - v3.20.0)
            resp['cntr'] = cntr
        if counter_tracked:
            ota.counter_persist(store, preset_id, resp.get('final_cntr'),
                                'send-ota', kvn)
        if spi_warn:
            resp['warning'] = spi_warn
        return resp

    def _send_secured_packet(self, sp_hex, include_cpi=True, submit_handler=None,
                             max_segments=scp80.SCP80_MAX_SEGMENTS):
        """Deliver a secured packet as SMS-PP download ENVELOPEs.

        TS 31.115 §4.3: the whole command packet is split into SMS user-data
        parts (the first SM carries the concatenation IE plus the CPI IE, the
        following ones only the concatenation IE); the card reassembles them.
        One ENVELOPE per segment, the late-PoR STATUS poll only after the last
        (a concatenated download cannot have produced its PoR before the card
        got every part).  Returns the legacy result dict - ``success``,
        ``bytes``, ``segments``, ``sw``, ``response_data`` or ``error``.
        """
        try:
            packet = bytes.fromhex(str(sp_hex or '').strip())
        except ValueError as exc:
            return {'success': False, 'bytes': 0, 'segments': 0,
                    'error': 'Invalid secured packet: %s' % exc}
        if not packet:
            return {'success': False, 'bytes': 0, 'segments': 0,
                    'error': 'Empty secured packet'}
        parts = scp80.split_secured_packet(packet, include_cpi=include_cpi)
        total = len(parts)
        if total > max_segments:
            return {'success': False, 'bytes': len(packet), 'segments': total,
                    'error': 'Secured packet too large: %d segments (max %d - '
                             'the card concatenation buffer)'
                             % (total, max_segments)}
        data, sw = b'', b''
        log.info('OTA SEND: %d SMS segment(s), %d bytes', total, len(packet))
        for index, part in enumerate(parts):
            try:
                tpdu = sms.build_sms_tpdu(part, total, index + 1,
                                          oa_number=self.sms_oa,
                                          include_cpi=include_cpi)
            except sms.SmsError as exc:
                return {'success': False, 'bytes': len(packet),
                        'segments': total, 'error': str(exc)}
            data, sw = self.cat.send_envelope(
                tpdu, sm_sc=self.sms_sc, submit_handler=submit_handler,
                poll_status=(index == total - 1))
            sw_hex = bytes(sw or b'').hex().upper()
            log.info('OTA SEND: ENVELOPE %d/%d -> %s', index + 1, total,
                     sw_hex)
            if sw_hex != '9000' and not sw_hex.startswith('91'):
                # A warning (62xx/63xx, e.g. a low counter) may still hold the
                # PoR: pass it on so the caller can name the card's verdict
                # instead of a bare ENVELOPE failure.
                late_por = (submit_handler.submit_response_hex()
                            if submit_handler is not None else '')
                return {'success': False, 'sw': sw_hex, 'bytes': len(packet),
                        'segments': total,
                        'response_data': (bytes(data).hex().upper() if data
                                          else late_por or None),
                        'error': 'ENVELOPE failed at segment %d' % (index + 1)}
        return {'success': True, 'bytes': len(packet), 'segments': total,
                'sw': bytes(sw or b'').hex().upper(),
                'response_data': (bytes(data).hex().upper() if data else None)}

    # ---- the counter-sync probe (3c-3c-1) ---------------------------------

    def counter_probe(self, body):
        """``POST /api/counter-probe``: sync a preset's counter with the card.

        Sends a read-only SELECT MF with increasing counter values until the
        card accepts one (PoR ``por_ok``), then persists ``accepted + 1``
        (monotonic).  Every rejected attempt is a no-op on the card; the
        ceiling and the attempt budget bound the search away from the 40-bit
        maximum.  The parameters, the ladder and the verdict rules live in
        :func:`simple_server.ota.counter_probe`.
        """
        if not self.connected or self.cat is None:
            raise NoCardSessionError('no card session: equip a card first')
        body = dict(body or {})
        preset_id = str(body.get('preset_id') or '').strip()
        if not preset_id:
            raise ValueError('preset_id is required')
        if not hasattr(self.presets, 'get'):
            raise RuntimeError('preset store not initialized')
        preset = self.presets.get(preset_id)
        if not preset:
            raise ValueError('unknown preset')
        log.info('counter probe: preset %s, start %s, ceiling %s',
                 preset.get('name'), body.get('cntr', ''),
                 body.get('ceiling', ota.COUNTER_PROBE_CEILING))

        def send_fn(params, cntr):
            """One probe attempt: build, deliver and decode the PoR."""
            packet, _spi = scp80.build_secured_packet(
                params['spi1'], params['spi2'], params['kic'], params['kid'],
                params['tar'], cntr, params['apdu'], params['kic_key'],
                params['kid_key'])
            sp_hex = bytes(packet).hex()
            capture = cat_session.PorSubmitCapture()
            send = self._send_secured_packet(sp_hex, include_cpi=True,
                                             submit_handler=capture)
            por_hex = send.get('response_data') or capture.submit_response_hex()
            por = (ota.decode_por(
                params['spi1'], params['spi2'], params['kic'], params['kid'],
                cntr, params['kic_key'], params['kid_key'], por_hex,
                cmd_len=len(params['apdu']) // 2,
                rm=ota.tar_is_rm(preset, params['tar'])) if por_hex else None)
            return send, por

        return ota.counter_probe(self.presets, preset, body, send_fn)

    # ---- the background STATUS poll ---------------------------------------

    def start_poll(self):
        """Start the background STATUS poll thread (idempotent)."""
        if self._poll_thread is not None and self._poll_thread.is_alive():
            return self._poll_thread
        self._poll_stop.clear()
        self._poll_thread = threading.Thread(
            target=self._poll_loop, name='simple-status-poll', daemon=True)
        self._poll_thread.start()
        return self._poll_thread

    def stop_poll(self):
        """Stop the background STATUS poll thread."""
        self._poll_stop.set()
        thread, self._poll_thread = self._poll_thread, None
        if thread is not None and thread.is_alive() \
                and thread is not threading.current_thread():
            thread.join(timeout=2.0)

    def _effective_interval(self):
        """The poll interval to sleep (a disabled poll keeps the default tick)."""
        if self.poll_interval > 0:
            return self.poll_interval
        return DEFAULT_POLL_INTERVAL

    def _poll_loop(self):
        """Tick in one-second slices; a changed interval re-bases the countdown.

        One-second slices (rather than a ``threading.Timer`` chain) let the
        card's POLL INTERVAL and the operator's toggle take effect within a
        second without cancelling/rebuilding a timer from another thread.
        """
        interval = self._effective_interval()
        remaining = interval
        while True:
            if self._poll_stop.wait(1.0):
                return
            current = self._effective_interval()
            if current != interval:
                interval = current
                remaining = interval
                continue
            remaining -= 1.0
            if remaining > 0:
                continue
            remaining = interval
            try:
                self.poll_once()
            except Exception as exc:            # never kill the thread
                log.warning('background STATUS poll failed: %s', exc)

    def poll_once(self):
        """One poll cycle: STATUS and, on ``91XX``, the bounded chain.

        Respects ``poll_enabled``, the card's POLLING OFF wish and a test run
        that owns the card (the legacy ``_do_status_poll`` rules); adopts the
        card's POLL INTERVAL and POLLING OFF wishes.

        **A failure must not lie about the card being gone.**  A transport
        error (:func:`_is_transport_error`: a ``ReaderError`` or a PC/SC
        exception) tears the link down but keeps ``card_present`` (the card is
        usually still inserted and pyscard only notifies on changes) and
        re-arms the auto-equip, so the session heals without a manual Equip.
        A decoder/protocol error (``CatSessionError``, a TLV-walk bug) is
        logged and the session is kept: the card still talks, only the answer
        was not understood.
        """
        if not self.poll_enabled or self.poll_suspended or self.test_running:
            return None
        with self.card_lock:
            cat = self.cat
            if cat is None or not self.connected:
                return None
            try:
                _data, sw = cat.send_status()
                log.debug('AUTO-STATUS -> %s', sw.hex())
                if sw[:1] == b'\x91':
                    cat.handle_chain(sw, max_rounds=POLL_CHAIN_MAX)
            except Exception as exc:
                if _is_transport_error(exc):
                    log.warning('AUTO-STATUS transport error: %s', exc)
                    self.disconnect(stale=True, keep_present=True)
                    self.auto_equip()
                else:
                    log.warning('AUTO-STATUS error (session kept): %s', exc)
                return None
            finally:
                # The session may have been torn down by the error path: never
                # write the dead session's state back over the new one.
                if self.cat is cat:
                    self.poll_suspended = bool(
                        getattr(cat, 'poll_suspended', False))
                    self._adopt_poll_interval()
            return sw

    def _adopt_poll_interval(self):
        """Adopt the CAT session's recorded POLL INTERVAL (§6.4.6)."""
        cat = self.cat
        if cat is None:
            return
        try:
            interval = int(cat.poll_interval)
        except (TypeError, ValueError):
            return
        if interval > 0:
            self.poll_interval = max(0, min(255, interval))

    # ---- the presence monitor ---------------------------------------------

    def start_monitor(self):
        """Register the presence observer (pyscard's own monitor thread).

        The observer filters by :attr:`reader_name`, so a reader switch needs no
        re-registration.  ``presence_install`` injects the registration (the
        tests' seam); without it the pyscard ``CardMonitor`` singleton is used.
        Returns the observer (``None`` when disabled or reader-less).
        """
        if not self.monitor_enabled or not self.reader_name:
            return None
        if self._presence_observer is not None:
            return self._presence_observer
        observer = _PresenceObserver(self)
        if self.presence_install is not None:
            self.presence_install(observer)
        else:
            try:
                from smartcard.CardMonitoring import CardMonitor
                CardMonitor().addObserver(observer)
            except Exception as exc:
                log.warning('presence monitor unavailable: %s', exc)
                return None
        self._presence_observer = observer
        return observer

    def stop_monitor(self):
        """Remove the presence observer (no-op when it was never installed).

        The removal is symmetric with :meth:`start_monitor`: ``presence_remove``
        when injected (the tests' seam), otherwise pyscard's ``CardMonitor`` -
        and only when *we* registered through it.  Constructing
        ``CardMonitor()`` just to remove a seam-installed observer would start
        pyscard's real polling thread and leak it.
        """
        observer, self._presence_observer = self._presence_observer, None
        if observer is None:
            return
        if self.presence_remove is not None:
            try:
                self.presence_remove(observer)
            except Exception as exc:
                log.debug('presence observer removal failed: %s', exc)
            return
        if self.presence_install is not None:
            # Installed through the seam, not pyscard: touching the singleton
            # here would create its monitor thread just to delete an observer
            # it never had.
            return
        try:
            from smartcard.CardMonitoring import CardMonitor
            # pyscard's removal API is ``deleteObserver`` (CardMonitor
            # delegates to the singleton; there is no ``removeObserver``).
            CardMonitor().deleteObserver(observer)
        except Exception as exc:
            log.debug('presence observer removal failed: %s', exc)

    def handle_presence(self, added=None, removed=None):
        """Process pyscard's added/removed card events for our reader.

        A removal clears the session (and marks the link unusable); an insertion
        marks the link stale and spawns one bounded auto-equip (never in the
        pyscard thread itself - the legacy rule).
        """
        for card in removed or ():
            if self._card_reader(card) != self.reader_name:
                continue
            log.info('card removed from %s', self.reader_name)
            with self.card_lock:
                self._transport_stale = True
            self.disconnect(stale=True)
        inserted = False
        for card in added or ():
            if self._card_reader(card) != self.reader_name:
                continue
            log.info('card inserted into %s', self.reader_name)
            with self.card_lock:
                self.card_present = True
                # A link created at a cardless startup has never connected and
                # a swap leaves a dead handle, so the next equip must rebuild
                # (v3.32.4).  An already-equipped session needs no rebuild -
                # pyscard reports the cards present at observer registration as
                # inserted.
                if not self.connected:
                    self._transport_stale = True
            inserted = True
        if inserted:
            self.auto_equip()
        return inserted

    @staticmethod
    def _card_reader(card):
        """The reader name a pyscard card event names."""
        return str(getattr(card, 'reader', ''))

    def auto_equip(self, attempts=3, retry_delay=1.0):
        """Spawn one bounded auto-equip worker; returns its thread (or None).

        Port of the legacy ``_auto_equip_worker``/``_auto_equip_attempts``
        without its pySim-shell checks: a card that is present while the
        session is down is equipped, a failed attempt is retried ``attempts``
        times (the v3.32.2 retry) and a successful one clears the failure.
        Returns the worker thread so a test can join it.
        """
        if not self.auto_equip_enabled or self._equip_busy:
            return None
        if self._closed.is_set():
            return None
        with self.card_lock:
            if self.connected or not self.card_present:
                return None
            if self._equip_busy:
                return None
            self._equip_busy = True
        thread = threading.Thread(
            target=self._auto_equip_worker, args=(attempts, retry_delay),
            name='simple-auto-equip', daemon=True)
        self._equip_thread = thread
        thread.start()
        return thread

    def _auto_equip_worker(self, attempts, retry_delay):
        """The auto-equip worker: bounded attempts, one spawn at a time."""
        try:
            for attempt in range(1, max(1, int(attempts)) + 1):
                if self._closed.is_set() or self.connected \
                        or not self.card_present:
                    return
                log.info('auto-equip: card present, initializing (attempt %d/%d)',
                         attempt, attempts)
                try:
                    self.equip()
                    log.info('auto-equip: done')
                    return
                except Exception as exc:
                    log.warning('auto-equip failed: %s', exc)
                    if attempt < attempts:
                        # Wake early on close: the retry wait is the only long
                        # sleep in the worker.
                        if self._closed.wait(max(0.0, float(retry_delay))):
                            return
        finally:
            self._equip_busy = False

    # ---- the status shape --------------------------------------------------

    def _readers(self):
        """The reader list, cached briefly (the UI polls /api/status)."""
        now = time.monotonic()
        if self._readers_cache is not None and now - self._readers_at < 2.0:
            return list(self._readers_cache)
        names = self.readers_fn() if self.readers_fn else reader_names()
        self._readers_cache = list(names)
        self._readers_at = now
        return list(names)

    def card_info(self):
        """The card block of ``/api/status``: our kind + a human name."""
        card = self.card
        if card is None or not self.connected:
            return None
        return {'kind': card.kind,
                'name': CARD_NAMES.get(card.kind, card.kind),
                'cla': card.cla,
                'aids': list(card.aids or [])}

    def proactive_mode(self):
        """The CAT state machine the Status tab renders.

        ``test`` (a run owns the card) > ``paused`` (a FETCHed command awaits
        its TERMINAL RESPONSE) > ``chain`` (a FETCH/TR chain runs) > ``bip``
        (a BIP session is active) > ``polling`` (idle with the poll on) >
        ``idle``.
        """
        if self.test_running:
            return 'test'
        cat = self.cat
        if cat is not None:
            if cat.stk_pending:
                return 'paused'
            if cat.busy:
                return 'chain'
        if self.bip is not None and self.bip.owner:
            return 'bip'
        if self.connected and self.poll_enabled and not self.poll_suspended:
            return 'polling'
        return 'idle'

    def status(self):
        """The ``/api/status`` body: our own shape, no pySim internals.

        Served without the card lock (it only reads cached state), so it must
        not enter the card exchange: a long operation's live progress stays
        visible.  ``selection`` is the file manager's current EF once a file
        endpoint (3c-3b) published one -
        ``{path, name, fid, kind, size, record_len, num_records}``; the
        post-equip reads restore MF and never publish a selection.
        ``test_run`` is the run state (the runner lands in 3c-3c) and
        ``scp80`` the counter/keyset state (3c-3b), both placeholders now.
        ``adm_verified`` is the ADM latch of the current card session (the
        legacy's ``rs.adm_verified``): a successful VERIFY until the next
        equip/disconnect.
        """
        cat = self.cat
        suspended = (bool(getattr(cat, 'poll_suspended', False))
                     if cat is not None else bool(self.poll_suspended))
        bip = self.bip
        bip_status = None
        if bip is not None:
            # The BIP session mutates owner/mode/target/listener under its own
            # lock and publishes them through status(); snapshot under the same
            # lock, else the listener block can tear between the reads (the
            # legacy read it under the card lock).
            state_lock = getattr(bip, '_state_lock', None)
            if state_lock is not None:
                with state_lock:
                    bip_status = bip.status()
            else:
                bip_status = bip.status()
        return {
            'version': self.version,
            'app_ready': True,
            'connected': bool(self.connected and self.card is not None),
            'card_present': bool(self.card_present),
            'equipping': bool(self.equipping),
            'card_session': int(self.card_session),
            'card_ready_session': int(self.card_ready_session),
            'reader': self.reader_name,
            'readers': self._readers(),
            'atr': self.atr,
            'card': self.card_info(),
            'iccid': self.iccid if self.connected else None,
            'selection': self.selection if self.connected else None,
            # The ADM latch: a VERIFY that succeeded in the current card
            # session (the legacy's `rs.adm_verified`); an equip/disconnect
            # bumps `card_session` and clears it.
            'adm_verified': bool(self.connected
                                 and self.adm_session == self.card_session),
            'proactive_mode': self.proactive_mode(),
            'poll': {'enabled': bool(self.poll_enabled),
                     'suspended': suspended,
                     'interval': int(self.poll_interval)},
            'test_run': {'running': bool(self.test_running)},
            'bip': bip_status,
            'scp80': None,
            'terminal_profile': self.terminal_profile,
        }

    # ---- helpers -----------------------------------------------------------

    def _build_transport(self, reader=None):
        """Build the PC/SC link for ``reader`` (the factory when injected)."""
        if self.transport_factory is not None:
            return self.transport_factory(reader)
        return PcscLink(reader=reader, tracer=self.tracer,
                        proactive_handler=self.proactive_handler)


class _PresenceObserver(object):
    """A pyscard ``CardObserver`` shape forwarding to one :class:`ServerState`.

    pyscard's ``CardMonitor`` only polls ``SCardGetStatusChange`` (no
    connection, no APDUs), so it can never interleave with our card exchange.
    The observer deliberately does no work in the monitor thread beyond the
    state calls; a removal tears the session down, an insertion triggers the
    auto-equip worker.
    """

    def __init__(self, state):
        self.state = state

    def update(self, observable, handlers):
        try:
            added, removed = handlers
            self.state.handle_presence(added, removed)
        except Exception as exc:
            log.warning('presence observer error: %s', exc)


def make_handler(state, log_requests=False):
    """Build the ``BaseHTTPRequestHandler`` class serving ``state``.

    The class is a factory result (one class per server) so a test can mount
    the state on a real :class:`ThreadingHTTPServer` without module globals.
    """

    class SimpleHandler(BaseHTTPRequestHandler):
        """The PWA + JSON API handler for one :class:`ServerState`."""

        state_ref = state

        # ---- plumbing --------------------------------------------------

        def log_message(self, format, *args):
            log.debug('%s - %s', self.address_string(), format % args)

        def do_OPTIONS(self):
            """The CORS/PNA preflight a custom-URL PWA needs (legacy parity)."""
            self.send_response(204)
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Access-Control-Allow-Methods',
                             'GET, POST, OPTIONS')
            self.send_header('Access-Control-Allow-Headers', 'Content-Type')
            self.send_header('Access-Control-Allow-Private-Network', 'true')
            self.end_headers()

        def _send_json(self, data, status=200):
            body = json.dumps(data, ensure_ascii=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            # The PWA can point at a custom server URL; the same-origin
            # deployment never needs this.
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError,
                    ConnectionAbortedError):
                log.info('client gone: %s %s', self.command, self.path)

        def _read_body(self):
            length = int(self.headers.get('Content-Length') or 0)
            if not length:
                return {}
            raw = self.rfile.read(length)
            if not raw:
                return {}
            return json.loads(raw.decode('utf-8'))

        # ---- static files ---------------------------------------------

        def _serve_static(self):
            """Serve the PWA files from ``web_dir`` with no-cache headers.

            The legacy rules: ``/`` is ``index.html``, a path with ``..`` or a
            missing file is a 404 (there is **no** SPA rewrite - the legacy
            server has none, so an unknown path must not silently return the
            app shell; the API answers its own JSON 404).  The resolved path
            must stay inside ``web_dir`` (a Windows drive-relative segment is
            not a ``..`` but would still escape the root).  Every file
            revalidates through ``Last-Modified``/304 because a stale
            ``style.css`` silently drops styles.
            """
            web_dir = getattr(self.server, 'web_dir', None)
            if not web_dir:
                self.send_error(404)
                return
            rel = self.path.split('?', 1)[0].lstrip('/').replace('\\', '/')
            if rel in ('', '/'):
                rel = 'index.html'
            if '..' in rel.split('/') or rel.startswith('/'):
                self.send_error(404)
                return
            # Containment check: a Windows drive-relative segment ("C:...") is
            # not a ".." but os.path.join would discard web_dir for it, and a
            # symlink could point outside the root.
            root = os.path.realpath(web_dir)
            fs_path = os.path.realpath(os.path.join(root, *rel.split('/')))
            try:
                inside = os.path.commonpath([root, fs_path]) == root
            except ValueError:              # different drives (Windows)
                inside = False
            if not inside:
                self.send_error(404)
                return
            if not os.path.isfile(fs_path):
                self.send_error(404)
                return
            with open(fs_path, 'rb') as handle:
                data = handle.read()
            content_type = _STATIC_MIME.get(
                os.path.splitext(rel)[1].lower(), 'application/octet-stream')
            try:
                mtime = int(os.path.getmtime(fs_path))
            except OSError:
                mtime = 0
            last_modified = (
                time.strftime('%a, %d %b %Y %H:%M:%S GMT', time.gmtime(mtime))
                if mtime else '')
            if last_modified and self.headers.get('If-Modified-Since') \
                    == last_modified:
                self.send_response(304)
                self.send_header('Cache-Control', 'no-cache')
                self.send_header('Last-Modified', last_modified)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-cache')
            if last_modified:
                self.send_header('Last-Modified', last_modified)
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError,
                    ConnectionAbortedError):
                log.info('client gone: GET %s', rel)

        # ---- dispatch --------------------------------------------------

        def do_GET(self):
            path = self.path.split('?', 1)[0]
            if path in CARD_FREE_GET or not path.startswith('/api/'):
                self._dispatch_get()
                return
            with self.state_ref.card_lock:
                self._dispatch_get()

        def do_POST(self):
            path = self.path.split('?', 1)[0]
            if path in CARD_FREE_POST:
                self._dispatch_post()
                return
            with self.state_ref.card_lock:
                self._dispatch_post()

        def _dispatch_get(self):
            path = self.path.split('?', 1)[0]
            if log_requests:
                log.info('GET %s', self.path)
            try:
                if path == '/api/version':
                    self._send_json({'version': self.state_ref.version})
                elif path == '/api/status':
                    self._send_json(self.state_ref.status())
                elif path == '/api/readers':
                    self._send_json({'readers': self.state_ref._readers(),
                                     'active': self.state_ref.reader_name})
                elif path == '/api/arr':
                    self._send_json(self.state_ref.arr_cache())
                elif path == '/api/pbr':
                    self._send_json(self.state_ref.pbr_cache())
                elif path == '/api/presets':
                    self._send_json(self.state_ref.presets_info())
                elif path.startswith('/api/'):
                    self._send_json({'error': 'Not found'}, 404)
                else:
                    self._serve_static()
            except Exception as exc:
                log.exception('GET %s failed', self.path)
                self._send_json({'error': str(exc)}, 500)

        def _dispatch_post(self):
            path = self.path.split('?', 1)[0]
            try:
                body = self._read_body()
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                self._send_json({'error': 'invalid JSON body: %s' % exc}, 400)
                return
            except Exception as exc:
                log.exception('POST %s failed reading the body', self.path)
                self._send_json({'error': str(exc)}, 500)
                return
            if log_requests:
                log.info('POST %s %s', self.path,
                         json.dumps(redact_body(body), ensure_ascii=False))
            try:
                if not isinstance(body, dict):
                    self._send_json({'error': 'the request body must be a '
                                              'JSON object'}, 400)
                    return
                if path == '/api/reader':
                    self._post_reader(body)
                elif path == '/api/tree':
                    self._post_file(self.state_ref.file_tree, body)
                elif path == '/api/select':
                    self._post_file(self.state_ref.file_select, body)
                elif path == '/api/read':
                    self._post_file(self.state_ref.file_read, body)
                elif path == '/api/write':
                    self._post_file(self.state_ref.file_write, body)
                elif path == '/api/equip':
                    self._post_lifecycle(self.state_ref.equip)
                elif path == '/api/reset':
                    self._post_lifecycle(self.state_ref.reset)
                elif path == '/api/verify-adm':
                    self._post_adm(body)
                elif path == '/api/send-ota':
                    self._post_ota(body)
                elif path == '/api/counter-probe':
                    self._post_counter_probe(body)
                elif path == '/api/presets':
                    self._post_presets(body)
                elif path == '/api/presets/update':
                    self._post_presets_update(body)
                elif path == '/api/presets/delete':
                    self._post_presets_delete(body)
                elif path == '/api/presets/import':
                    self._post_presets_import(body)
                elif path == '/api/presets/import-outfile':
                    self._post_presets_import_outfile(body)
                else:
                    self._send_json({'error': 'Not found'}, 404)
            except presets_store.PresetError as exc:
                # a refused store mutation: the store's message is the answer
                self._send_json({'error': str(exc)}, 400)
            except ValueError as exc:
                # An endpoint's own validation error: its 400, not a JSON one.
                self._send_json({'error': str(exc)}, 400)
            except Exception as exc:
                log.exception('POST %s failed', self.path)
                # The PWA reads `success`/`ok` on its card operations; a 500
                # must still be a negative answer, not just an `error` key.
                self._send_json({'error': str(exc),
                                 'success': False, 'ok': False}, 500)

        # ---- the POST endpoints ---------------------------------------

        def _post_reader(self, body):
            # ``switch_reader`` validates the name itself (check_reader raises
            # ValueError -> the dispatch's 400) and raises CardBusyError only
            # for the two owners; every other failure is a real switch failure
            # and answers 503 with the status body (the card_session may have
            # been bumped, and the PWA resets its views from it).
            try:
                status = self.state_ref.switch_reader(body.get('name'))
            except CardBusyError as exc:
                self._send_json({'error': str(exc)}, 409)
                return
            except ValueError:
                # The endpoint's validation (check_reader's message): let the
                # dispatch answer its own 400, never "invalid JSON body".
                raise
            except Exception as exc:
                self._send_json({'error': 'reader switch failed: %s' % exc,
                                 **self.state_ref.status()}, 503)
                return
            self._send_json(status)

        def _post_lifecycle(self, action):
            try:
                status = action()
            except Exception as exc:
                self._send_json({'error': str(exc),
                                 **self.state_ref.status()}, 503)
                return
            self._send_json(status)

        def _post_file(self, action, body):
            """One file endpoint, with the 3c-3a error classification.

            * a resolution/selection failure (:class:`~simple_server.files.
              FileModelError`, a :class:`~simple_server.commands.FileError`) is
              the PWA's "not found": ``404`` with ``exists``/``success`` false;
            * a request parameter (:class:`ValueError`) is a ``400``;
            * no card session is a ``503`` with the status body;
            * a **transport** error tears the link down and re-arms the
              auto-equip (``503``): the card is usually still inserted, so the
              session is rebuilt instead of staying dead;
            * anything else is a ``500`` that **keeps the session** - a
              decoder/protocol error must not lose a healthy card.
            """
            try:
                self._send_json(action(body))
            except (files_module.FileModelError, commands.FileError) as exc:
                self._send_json({'success': False, 'exists': False,
                                 'error': str(exc)}, 404)
            except ValueError as exc:
                self._send_json({'error': str(exc)}, 400)
            except NoCardSessionError as exc:
                self._send_json({'success': False, 'exists': False,
                                 'error': str(exc),
                                 **self.state_ref.status()}, 503)
            except Exception as exc:
                if not _is_transport_error(exc):
                    raise
                log.warning('%s: transport error: %s', self.path, exc)
                state = self.state_ref
                state.disconnect(stale=True, keep_present=True)
                state.auto_equip()
                self._send_json({'success': False, 'exists': False,
                                 'error': str(exc), **state.status()}, 503)

        # ---- the preset / SCP80 endpoints (3c-3c-1) -------------------

        def _card_failure(self, exc, extra):
            """The card endpoints' 3c-3a classification (shared tail).

            A transport error tears the link down and re-arms the auto-equip
            (the card is usually still inserted); every other exception is
            re-raised - a decoder/protocol error must not lose a healthy card.
            """
            if not _is_transport_error(exc):
                # An active exception is being handled by the caller, whose
                # dispatch turns it into its 500 without touching the link.
                raise
            log.warning('%s: transport error: %s', self.path, exc)
            state = self.state_ref
            state.disconnect(stale=True, keep_present=True)
            state.auto_equip()
            self._send_json(dict(extra, error=str(exc), **state.status()), 503)

        def _post_card(self, action, extra):
            """A card endpoint's classification (the shared `_card_failure` tail).

            * no card session is a ``503`` with the status body;
            * a request parameter (:class:`ValueError`) is a ``400``;
            * a **transport** error tears the link down and re-arms the
              auto-equip (``503``); anything else is re-raised, so the dispatch
              answers its 500 without touching a healthy card.
            """
            try:
                resp = action()
            except NoCardSessionError as exc:
                self._send_json(dict(extra, error=str(exc),
                                     **self.state_ref.status()), 503)
            except ValueError as exc:
                self._send_json(dict(extra, error=str(exc)), 400)
            except Exception as exc:
                self._card_failure(exc, extra)
            else:
                self._send_json(resp)

        def _post_adm(self, body):
            self._post_card(lambda: self.state_ref.verify_adm(body),
                            {'ok': False})

        def _post_ota(self, body):
            self._post_card(lambda: self.state_ref.send_ota(body),
                            {'success': False})

        def _post_counter_probe(self, body):
            self._post_card(lambda: self.state_ref.counter_probe(body),
                            {'success': False})

        # A PresetError (a refused store mutation) is the dispatch's 400 - the
        # inner handlers only add their own status for the non-error answers.

        def _post_presets(self, body):
            preset = self.state_ref.preset_add(body)
            self._send_json({'ok': True, 'preset': preset})

        def _post_presets_update(self, body):
            preset = self.state_ref.preset_update(body)
            if preset is None:
                self._send_json({'error': 'unknown preset id'}, 404)
                return
            self._send_json({'ok': True, 'preset': preset})

        def _post_presets_delete(self, body):
            self._send_json(self.state_ref.preset_delete(body))

        def _post_presets_import(self, body):
            self._send_json(self.state_ref.preset_import(body))

        def _post_presets_import_outfile(self, body):
            self._send_json(self.state_ref.preset_import_outfile(body))

    return SimpleHandler


def build_http_server(host, port, state, web_dir=None, log_requests=False):
    """A :class:`ThreadingHTTPServer` serving ``state`` (the PWA + the API).

    Threaded on purpose (the legacy v3.6.39 fix): card access is serialized by
    ``state.card_lock``, so the cached endpoints and the static files must stay
    answerable while a long card operation runs.  ``web_dir`` overrides
    ``state.web_dir``.
    """
    server = ThreadingHTTPServer((host, port),
                                 make_handler(state, log_requests=log_requests))
    server.state = state
    server.web_dir = web_dir if web_dir is not None else state.web_dir
    return server
