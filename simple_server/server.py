# coding=utf-8
"""The pysim-free server core: one server's state, card lifecycle, HTTP API.

This module owns what the legacy 3.x ``server.py`` spread over
module globals: the card link, the detected card, the CAT session, the BIP
session, the file model, the session caches and the background threads.  One
:class:`ServerState` describes one server; nothing here is a module global.

Scope (Phase 3c-3a/3b/3c-1/3c-2/3d-1/3d-2/3e-1/3e-2): the state and the card
lifecycle (:meth:`ServerState.open`, :meth:`~ServerState.equip`,
:meth:`~ServerState.reset`, :meth:`~ServerState.disconnect`, the presence
monitor and the background STATUS poll), the HTTP handler and the
:class:`http.server.ThreadingHTTPServer` factory over it, the static PWA
serving and the endpoints - ``GET /api/version``, ``GET /api/status``,
``GET /api/readers``, ``GET /api/arr``, ``GET /api/pbr``,
``POST /api/reader``, ``POST /api/equip``, ``POST /api/reset`` (3c-3a), the
file endpoints ``POST /api/tree|select|read|write`` (3c-3b) and the
preset/plain-SCP80 set ``GET/POST /api/presets`` +
``/api/presets/update|delete|import|import-outfile``, ``POST /api/verify-adm``,
``POST /api/send-ota`` and ``POST /api/counter-probe`` (3c-3c-1), the CAP
analysis and the single-card RAM installs ``POST /api/cap-info``,
``POST /api/ram-install`` and ``POST /api/ram-install-app`` (3c-3c-2a), the
``POST /api/cap-compat``, ``POST /api/tar-probe`` and the batch install
``POST /api/ram-batch/start|stop|clear`` + ``GET /api/ram-batch/status``
(3c-3c-2b) and the test-script/suite stores and the runs
``GET/POST /api/test/scripts|suites`` + ``/api/test/scripts|suites/update|
delete|import`` + ``/api/test/scripts/move|copy``, ``POST /api/test/run|stop|
clear`` and ``GET /api/test/status`` (3c-3d-1 and 3c-3d-2, with the run engine
in :mod:`simple_server.testrun` and the cross-store suite rules in
:mod:`simple_server.suites`), and the CAT/Simulator set (3c-3e-1):
``GET /api/menu|stk-status|events|poll-status|proactive-log|terminal-profile|
pli-qualifiers|pli-dict``, ``POST /api/menu-select|menu-respond|event-send|
terminal-profile|rescue|status-poll|poll-toggle|pli-dict`` and the raw APDU
``POST /api/apdu``.  Phase 3c-3e-2a adds the eSIM/network set:
``GET /api/esim/chip|profiles|notifications``, ``POST /api/esim/profile``,
``POST /api/net-sim``, ``GET /api/net-state``,
``POST /api/net-state-refresh`` and ``GET /api/mcc-mnc`` (the card-level ES10
flow in :mod:`simple_server.esim`, the net-sim adapter in
:mod:`simple_server.netsim`, the monitor and operator list in
:mod:`simple_server.netmon`).  Phase 3c-3e-2b adds the SCP81/BIP control
surface: ``POST /api/scp81/bip``, ``POST /api/bip/control``,
``GET /api/scp81/status|bip/status``, ``GET /api/scp81/log|bip/log``
(``?after=``), ``POST /api/scp81/log-clear|bip/log-clear``,
``GET /api/scp81/script``, ``POST /api/scp81/queue|psk-map|gen-install`` (the
control and the script engine in :mod:`simple_server.scp81` on top of the
per-server :class:`~simple_server.bip.BipSession` and the reusable
:class:`~simple_server.shared.scp81.PskTlsServer`, moved here from the
legacy tree).  Every other API path is a
404 until 3d (the entry point and the cutover) lands.

Deliberate API decisions (see ``docs/pysim-phaseout.md`` "Phase 3c API
surface"): there is no pySim command line, no ``/api/command``/``/api/help``/
``/api/commands``/``/api/cardinfo``, and **no ``/api/sp-verify``** (the PWA's
"Verify vs pySim" builds its reference with pySim itself, which this server
does not have - the SCP80 packet builder is pinned to the same vectors by the
unit tests instead); ``/api/status`` has our own shape (no
pySim concepts); ``/api/apdu`` (3c-3e-1) keeps the automatic proactive handler on
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
(EF.ICCID, the E.118 digit string); SGP.22 v2.6 §5.7.8/§5.7.9/§5.7.15/§5.7.16/
§5.7.17/§5.7.20 (the eSIM endpoints); TS 31.102 §4.4.3.1/§4.4.3.2 and
TS 51 011 §10.3.3/§10.3.32 (the network simulator's EF.Kc paths); TS 23.122
(EF.FPLMN);
TS 102 223 §7.5/§8.27 (the Location status event); ISO/IEC 7816-4 Table 25 /
TS 102 221 §11.1.1.4 (the FCP ``8B`` EF.ARR reference); TS 31.102 §4.4.2.1
(EF.PBR).
"""

import copy
import json
import logging
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

from simple_server.shared import events
from simple_server.shared import netstate
from simple_server.shared import outfile
from simple_server.shared import presets as presets_store
from simple_server.shared import test_scripts as test_scripts_store
from simple_server.shared import test_suites as test_suites_store
from simple_server.shared import testscript

from simple_server import bip as bip_module
from simple_server import capram
from simple_server import cards
from simple_server import cat as cat_module
from simple_server import cat_session
from simple_server import commands
from simple_server import esim as esim_module
from simple_server import files as files_module
from simple_server import netmon
from simple_server import netsim as netsim_module
from simple_server import ota
from simple_server import pbr as pbr_module
from simple_server import proactive
from simple_server import scp80
from simple_server import scp81 as scp81_module
from simple_server import sms
from simple_server import suites as suites_module
from simple_server import testrun
from simple_server import util
from simple_server.transport import PcscLink, ReaderError

log = logging.getLogger(__name__)

__all__ = [
    'ServerState', 'make_handler', 'build_http_server', 'reader_names',
    'CardBusyError', 'BatchBusyError', 'NoCardSessionError', 'CARD_FREE_GET',
    'CARD_FREE_POST', 'DEFAULT_TERMINAL_PROFILE', 'DEFAULT_POLL_INTERVAL',
    'POLL_CHAIN_MAX', 'RAM_BATCH_POLL', 'RAM_BATCH_ATTEMPTS_MAX',
    'CLOSE_LOCK_TIMEOUT', 'VERSION', 'ARR_READ_PATHS', 'PBR_READ_PATHS',
    'check_reader', 'decode_iccid', 'validate_terminal_profile',
    'resolve_menu_item', 'event_code',
]

#: The server version served by ``GET /api/version``.  It moves together with
#: ``pyproject.toml`` and the PWA header (``tests/test_version.py`` pins the
#: three).
VERSION = '4.0.0'

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

#: The batch install worker's card-state poll interval (seconds) and the number
#: of attempts its report keeps (the total keeps counting, so a long run stays
#: readable).
RAM_BATCH_POLL = 0.5
RAM_BATCH_ATTEMPTS_MAX = 500

#: How long :meth:`ServerState.close` waits for the card lock before it skips
#: the teardown: a batch install holds the lock for its whole chain, and a
#: shutdown must not block on a blocking card exchange.
CLOSE_LOCK_TIMEOUT = 2.0

#: ``/api/`` GETs served without the card lock: pure cached state and the
#: reader list (the UI polls them while a long card operation runs).  The list
#: grows as the endpoints are ported (the legacy list is the reference);
#: ``/api/arr`` and ``/api/pbr`` are the session caches the file manager reads
#: while a snapshot scan is running, ``/api/presets`` is the server-side
#: preset store (no card involved) and ``/api/ram-batch/status`` is the batch
#: panel's poll (the worker holds the card lock for a whole install).
CARD_FREE_GET = frozenset([
    '/api/status', '/api/version', '/api/readers', '/api/arr', '/api/pbr',
    '/api/presets', '/api/ram-batch/status',
    # the test runner's state and the script/suite stores: the PWA polls the
    # status every 2 s while a run owns the card (the run holds the lock
    # inside a step), and the stores/indexes are card-free
    '/api/test/status', '/api/test/scripts', '/api/test/suites',
    # the CAT/Simulator state (3c-3e-1): the menu, the paused-command state,
    # the subscribed events, the poll state and configuration, the proactive
    # log and the runtime TERMINAL PROFILE - all pure cached state (the
    # legacy list), so a 2 s poll cannot starve a long card operation
    '/api/menu', '/api/stk-status', '/api/events', '/api/poll-status',
    '/api/proactive-log', '/api/terminal-profile', '/api/pli-qualifiers',
    '/api/pli-dict',
    # the network-state monitor and the operator list (3c-3e-2a): both are
    # cached/asset reads (the monitor's card I/O is the endpoints' own POST),
    # so the Phone tab can render them during a long operation
    '/api/net-state', '/api/mcc-mnc',
    # the SCP81/BIP control state (3c-3e-2b): the listener/session status, the
    # terminal event log and the script's run state are cached state on the
    # server-owned session - the SCP81/BIP views poll them (and a listener may
    # run cardless), so they must answer while a long card operation runs
    '/api/scp81/status', '/api/bip/status', '/api/scp81/script',
    '/api/scp81/log', '/api/bip/log',
])

#: POSTs that answer while another card operation holds the lock (the legacy
#: ``_CARD_FREE_POST``).  ``/api/cap-info`` only analyzes the uploaded archive
#: (no card, no preset); a long RAM install must not block the PWA's CAP
#: analysis with its progress poll.  ``/api/ram-batch/stop`` only sets a flag
#: and must work while the batch worker holds the card lock inside an install.
#: ``/api/test/stop``/``/api/test/clear`` are the same for a run (the runner
#: holds the card lock inside a step, so Stop must not queue behind it).  The
#: test-script/-suite store mutations touch no card: the editor must stay
#: usable while a run owns the card (the legacy queued them behind the lock,
#: an unnecessary wait).  The two log clears only touch the terminal's log,
#: which is the terminal side (a listener may run cardless) - the run block
#: still applies to them, since TEST_BLOCKED_PATHS is checked first.
CARD_FREE_POST = frozenset([
    '/api/cap-info', '/api/ram-batch/stop', '/api/test/stop', '/api/test/clear',
    '/api/test/scripts', '/api/test/scripts/update',
    '/api/test/scripts/delete', '/api/test/scripts/import',
    '/api/test/scripts/move', '/api/test/scripts/copy',
    '/api/test/suites', '/api/test/suites/update', '/api/test/suites/delete',
    '/api/test/suites/import',
    '/api/scp81/log-clear', '/api/bip/log-clear',
])

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


class BatchBusyError(RuntimeError):
    """A batch install cannot start: another batch or a test run owns the card.

    The endpoint answers ``409`` (the legacy's ``RuntimeError`` -> 409); a
    *request* problem stays a :class:`ValueError` (400).
    """


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


def validate_terminal_profile(value):
    """Validate a TERMINAL PROFILE hex string: ``(hex_or_None, error_or_None)``.

    Port of the legacy ``_validate_tp_hex``: whitespace is stripped, the
    digits are upper-cased, and the profile must be non-empty, even-length hex
    and at most 255 bytes (one APDU's ``Lc``, TS 102 221 §11.2.1.2 - a longer
    profile has no short-APDU form).
    """
    text = re.sub(r'\s', '', str(value or '')).upper()
    if not text:
        return None, 'profile is empty'
    if not re.fullmatch(r'[0-9A-F]+', text):
        return None, 'profile is not valid hex'
    if len(text) % 2:
        return None, 'profile must have an even number of hex digits'
    if len(text) // 2 > 255:
        return None, 'profile exceeds 255 bytes'
    return text, None


#: The menu-text match modes :func:`resolve_menu_item` accepts (the two
#: :func:`simple_server.shared.testscript.match_text` implements).
MENU_MATCH_MODES = ('exact', 'contains')


def _menu_mode(value):
    """A validated menu-text match mode (``exact``/``contains``)."""
    if value is None or value == '':
        return 'exact'
    mode = str(value).strip().lower()
    if mode not in MENU_MATCH_MODES:
        raise ValueError('mode must be "exact" or "contains": %r' % (value,))
    return mode


def _menu_case_sensitive(value):
    """A validated ``case_sensitive`` flag (a JSON boolean; 0/1 tolerated).

    A string is accepted only in its literal boolean spellings (``true``/
    ``false``/``1``/``0``) - ``"false"`` must not silently read as true.
    """
    if value in (None, ''):
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.strip().lower() in (
            'true', 'false', '1', '0'):
        return value.strip().lower() in ('true', '1')
    raise ValueError('case_sensitive must be a boolean: %r' % (value,))


def _boolean_flag(value, name):
    """A validated request flag with a default of **true**.

    ``None``/``''`` means "not given" (the caller's default, ``True``); a JSON
    boolean and the integer ``0``/``1`` are taken as they are; a string is
    accepted only in its literal boolean spellings (``true``/``false``/``1``/
    ``0``).  ``bool("false")`` is truthy, so a hand-written API call used to
    turn an explicit "false" into TRUE - here it is read as FALSE, and
    anything else is a 400.
    """
    if value in (None, ''):
        return True
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.strip().lower() in (
            'true', 'false', '1', '0'):
        return value.strip().lower() in ('true', '1')
    raise ValueError('%s must be a boolean: %r' % (name, value))


def resolve_menu_item(menu, text, mode='exact', case_sensitive=False):
    """The item id of a cached SET UP MENU whose text matches (the run's rule).

    The endpoint's variant of the test runner's ``_menu_item_by_text``; both
    use :func:`simple_server.shared.testscript.match_text`, so the matching
    rule (``exact``/``contains``, case-insensitive unless ``case_sensitive``)
    cannot drift.  The item ids vary with the applet's install parameters, so
    a caller may name the text the card actually shows.

    It stays beside the endpoint rather than beside ``match_text`` because the
    run's helper raises the engine's ``ScriptError`` (the run report's
    wording) while this one raises the dispatch's ``ValueError`` (a 400); only
    the matching rule is shared.  ``mode`` is validated (``exact``/
    ``contains`` - an unknown mode used to degrade to ``contains``) and
    ``case_sensitive`` is read as a boolean.

    Raises :class:`ValueError` on a bad mode/flag, on a miss (naming the
    menu's items, and the case-insensitive candidates for a strict-case miss)
    and on ambiguity.
    """
    items = (menu or {}).get('items') or []
    spec = {'mode': _menu_mode(mode), 'value': text,
            'case_sensitive': _menu_case_sensitive(case_sensitive)}
    matches = [item for item in items
               if testscript.match_text(spec, item.get('text') or '')]
    if not matches:
        listing = ', '.join('%s=%r' % (item.get('id'), item.get('text'))
                            for item in items)
        hint = ''
        if spec['case_sensitive'] and items:
            loose = [item for item in items
                     if testscript.match_text(dict(spec, case_sensitive=False),
                                              item.get('text') or '')]
            if loose:
                hint = (' - a case-insensitive match exists (%s): set '
                        '"case_sensitive": false or fix the case'
                        % ', '.join('%s=%r' % (item.get('id'),
                                               item.get('text'))
                                    for item in loose))
        raise ValueError(
            'menu-select: no menu item matches %r (%s)%s'
            % (text, 'menu: ' + listing if listing
               else 'no menu cached - fetch the menu first (poll STATUS until '
                    'the card announces SET UP MENU)', hint))
    if len(matches) > 1:
        raise ValueError(
            'menu-select: %d items match %r (%s) - use the item id'
            % (len(matches), text,
               ', '.join('%s=%r' % (item.get('id'), item.get('text'))
                         for item in matches)))
    return matches[0]['id']


def event_code(value):
    """The event byte of an ``event-send`` request's ``event_type``.

    The accepted forms: a JSON number (the documented API form), a string of
    decimal digits (read as **decimal** for tolerance - ``'12'`` is event
    ``0x0C``), the test scripts' hex form (``0x1D``) and the server's event
    name keywords (``network_rejection``, ...), the last two through
    :func:`simple_server.shared.events.resolve_event`.  The digits form is a
    tolerance, not legacy parity: the legacy only accepted a number - a
    string raised a ``TypeError`` while it built the packet, so it never sent
    one.  Raises :class:`ValueError` for anything else.
    """
    if isinstance(value, bool) or value is None:
        raise ValueError('event_type is required')
    if isinstance(value, int):
        code = value
    elif isinstance(value, str) and value.strip().isdigit():
        code = int(value.strip())
    else:
        code = events.resolve_event(value)
        if code is None:
            raise ValueError('event_type %r is not an event byte or name'
                             % (value,))
    if not 0 <= code <= 0xFF:
        raise ValueError('event_type must be one octet: %r' % (value,))
    return code


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

    **Threads.**  Four daemon threads can be owned here: the background
    STATUS poll (:meth:`start_poll`, started by :meth:`open`), the batch
    install worker (:meth:`ram_batch_start`), the test-run worker with its
    stuck-card watchdog (``test_runner``:
    :class:`simple_server.testrun.TestRunner`) and the presence observer,
    which is pyscard's own ``CardMonitor`` observer rather than a thread we
    spin (see :meth:`start_monitor`).  :meth:`close` stops all of them.

    **The injected hooks.**  ``transport_factory(reader_name)`` builds a
    transport (default :class:`simple_server.transport.PcscLink`);
    ``transport`` injects one directly (tests); ``presets`` injects the card
    preset store (default ``simple_server.shared.presets.PresetStore``), and
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
    * The network-state monitor is read at equip (after a readable ICCID) and
      patched by the simulator's own writes; the read/compute pair lives in
      :mod:`simple_server.netmon` and the state on :attr:`netstate`.
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
                 readers_fn=None, test_scripts=None, test_scripts_path=None,
                 test_suites=None, test_suites_path=None, mcc_mnc_path=None):
        self.version = version
        self.web_dir = web_dir
        self.terminal_profile = (terminal_profile or '')
        #: The startup profile ``GET /api/terminal-profile`` reports as
        #: ``cli_default``: a runtime POST changes :attr:`terminal_profile`
        #: only (in memory, the legacy contract).
        self.default_terminal_profile = self.terminal_profile
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
        #: The bundled MCC/MNC operator list (``--mcc-mnc-list``): the network
        #: simulation's operator picker and the monitor's country/operator line.
        self.mcc_mnc_path = (str(mcc_mnc_path) if mcc_mnc_path
                             else netmon.default_mcc_mnc_path())

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
        # The test-script store (the runner's scripts).  The constructor takes
        # the path (the entry point's ``--test-scripts``) or the store itself;
        # the validator is the runner's own engine, so a stored script can
        # never fail at run start for a validation reason.
        if test_scripts is not None:
            self.test_scripts = test_scripts
        else:
            self.test_scripts = test_scripts_store.TestScriptStore(
                path=test_scripts_path, validator=testrun.validate_script)
        # The test-suite store (the ordered member lists).  The suite holds
        # references, so it needs the script store - the cross-store rules live
        # in :mod:`simple_server.suites`.
        if test_suites is not None:
            self.test_suites = test_suites
        else:
            self.test_suites = test_suites_store.TestSuiteStore(
                path=test_suites_path)
        # The single-script run and the suite runs: the per-state run state,
        # the workers and the stuck-card watchdog (the legacy
        # ``_TEST_RUN``/``_TEST_THREAD``).
        self.test_runner = testrun.TestRunner(self)
        # The BIP/SCP81 session control: **one persistent session per server**
        # (the legacy module globals).  It owns the terminal (its channel log,
        # the sink/TLS listener, owner/mode/target) and therefore outlives a
        # card change: a listener started before a card, or kept across a
        # swap, simply serves the next card's OPEN CHANNEL (the listener is
        # the terminal side, not per-card state).  ``self.bip`` below is the
        # alias for the *current card session* (``None`` when cardless), so
        # the CAT hooks and the per-card status keep their meaning.
        self.bip_session = bip_module.BipSession(
            card_lock=self.card_lock,
            card_connected=lambda: self.connected)
        # The SCP81 control surface (3c-3e-2b): the listener start/stop, the
        # script engine and the PSK table, on the same session.
        self.scp81 = scp81_module.Scp81Control(self.bip_session)
        # The SMS-PP download addresses (the legacy ``server.sms_oa``/
        # ``sms_sc`` CLI options).
        self.sms_oa = str(sms_oa or '12345')
        self.sms_sc = str(sms_sc or '12345678912')

        self.card = None
        self.cat = None
        self.bip = None
        self.atr = None
        self.iccid = None
        #: The eUICC's EID (ES10c GetEuiccData, read best effort at equip on a
        #: card the ISD-R probe flagged); ``/api/status`` reports it.
        self.eid = None
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
        #: The operator's PROVIDE LOCAL INFORMATION overrides (qualifier code ->
        #: hex), the legacy module-level ``_PLI_DATA``.  It belongs to the
        #: operator, not to the card session: every :class:`CatSession` gets
        #: this dict, so an equip (a new session object) keeps the answers and
        #: a cardless ``GET /api/pli-dict`` still serves them.
        self.pli_data = {}
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
        #: Live progress of the RAM operation in flight.  The install chains
        #: hold the card lock for the whole request, so the PWA cannot learn the
        #: step from the response; ``/api/status`` (card-free, polled every 2 s)
        #: carries this payload instead (the legacy ``_RAM_PROGRESS``, v3.6.34).
        self.ram_progress = {'active': False, 'kind': '', 'step': 0, 'total': 0,
                             'name': '', 'started': 0.0}
        #: The batch install run (the legacy ``_RAM_BATCH``), per state: the
        #: plan built once at Start, the per-card attempts and counts, the
        #: stop flag.  ``ram_batch_lock`` guards it (the worker, the status
        #: poll and the stop endpoint run on different threads); the card
        #: exchange inside a card uses :attr:`card_lock`.
        self.ram_batch = None
        self.ram_batch_lock = threading.RLock()
        self._ram_batch_thread = None
        #: Set by :meth:`close`: the auto-equip worker observes it between
        #: attempts so a shutdown never races a fresh equip.
        self._closed = threading.Event()
        self._equip_thread = None

        # A transport injected by the caller owns its reader name.
        if transport is not None and self.reader_name is None:
            self.reader_name = getattr(transport, 'reader_name', None)

    # ---- card lifecycle ----------------------------------------------------

    def open(self, reader=None, transport=None, start_threads=True,
             skip_card_init=False):
        """Build the link and run the startup initialization.

        ``transport`` injects a link (the tests' fake); otherwise
        :attr:`transport` or ``transport_factory(reader)`` builds one.  The
        sequence is :meth:`_init_session`'s (the legacy startup init): detect
        the card and select MF, then the CAT-free reads - EF.ICCID, EF.ARR,
        EF.PBR - and only then the TERMINAL PROFILE + drain, so no CAT session
        is active during the reads.

        ``skip_card_init`` is the ``--no-card-init`` mode: the link is built
        and the ATR read, but the detection, the MF select, the CAT-free reads
        and the TERMINAL PROFILE/drain are skipped (a placeholder card,
        :func:`~simple_server.cards.init_card` with ``skip_init``).  The poll
        and the presence monitor still start - the phase is skipped, not the
        process's card plumbing.

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
                self._init_session(skip_card_init=skip_card_init)
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

        ``_closed`` is set first so the auto-equip worker and the batch
        worker's wait never start another attempt, then both are joined
        (bounded: their waits observe the same event).  **Caveat:** a worker
        stuck inside a blocking ``SCardTransmit`` cannot be cancelled - the
        legacy watchdog has the same limit (pcsc-lite serializes a disconnect
        behind the pending transmit), so the join is bounded and shutdown
        proceeds without it.  **The card teardown is bounded too**: a batch
        install holds :attr:`card_lock` for its whole chain, so the lock is
        acquired with :data:`CLOSE_LOCK_TIMEOUT` and the teardown is skipped
        when the install is still running (the process is shutting down; the
        worker is a daemon and its ``_closed`` check ends the loop after the
        chain).
        """
        self._closed.set()
        self.stop_poll()
        self.stop_monitor()
        self._stop_ram_batch_worker()
        self.test_runner.close()
        # The SCP81/BIP listener is server-owned (it survives card changes),
        # so the shutdown stops it explicitly.  Lock-free on purpose: the card
        # teardown below takes the lock with a timeout, and the listener stop
        # must not block the process exit behind a blocking card exchange -
        # its channel cleanup is the terminal's disable() (a local socket
        # close), and the teardown's own detach repeats it idempotently.
        try:
            self.bip_session.stop(lock=False)
        except Exception as exc:
            log.debug('BIP/SCP81 session stop failed: %s', exc)
        thread = self._equip_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        if not self.card_lock.acquire(timeout=CLOSE_LOCK_TIMEOUT):
            log.debug('close: the card lock is held by a running operation - '
                      'skipping the teardown')
            return
        try:
            self._teardown_session(bump=True)
            self.card_present = False
            transport, self.transport = self.transport, None
            if transport is not None:
                try:
                    transport.disconnect()
                except Exception:
                    pass
        finally:
            self.card_lock.release()

    def switch_reader(self, name):
        """Switch the active PC/SC reader (``POST /api/reader``).

        Rebuilds the transport for the chosen reader, resets and re-initializes
        its card and bumps ``card_session`` (each reader has its own card, so
        the previous selection/CAT session/ICCID/caches are meaningless).  The
        endpoint refuses the switch while a test run or a BIP/SCP81 session
        owns the terminal (:class:`CardBusyError` -> ``409``) - the guard keys
        on the persistent ``bip_session.owner``, not the per-card
        :attr:`bip` alias, because a cardless listener would otherwise be
        missed while the switch resets the card and kills its channels either
        way.  A cardless reader is not an error for the switch itself - the
        error is reported (``503`` with the status body) and the reader stays
        selected for the next equip.
        """
        with self.card_lock:
            self.equipping = True
            try:
                name = check_reader(name, self._readers())
                if self.test_running:
                    raise CardBusyError(
                        'a test run owns the card; stop it first')
                if self.bip_session.owner:
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

    def _init_session(self, skip_card_init=False):
        """Connect/reset, detect the card and run the post-equip sequence.

        The order is the legacy startup init's: the card is detected and MF
        selected by :func:`~simple_server.cards.init_card`, then the CAT-free
        reads (EF.ICCID, EF.ARR, EF.PBR) run *before* the TERMINAL PROFILE
        starts a CAT session, and the profile's answers are drained.  The next
        ``card_session`` value is the caller's session marker; the ready marker
        is set only after the whole sequence succeeded.

        ``skip_card_init`` (``--no-card-init``) stops after the link/ATR step:
        :func:`~simple_server.cards.init_card` runs with ``skip_init`` (a
        placeholder ``unknown`` card) and the detection, MF select, the reads,
        the TERMINAL PROFILE and the drain are skipped.  The session is still
        ``connected`` and carries a CAT session (the poll/monitor have a live
        link to work with), but it is **not** marked ready - the marker means
        "a full initialization finished".
        """
        transport = self.transport
        if transport is None:
            raise RuntimeError('no transport to initialize')
        card = cards.init_card(transport, skip_init=skip_card_init)
        self.card = card
        self.atr = (bytes(card.atr).hex() if card.atr else None)
        self.cat = cat_session.CatSession(
            transport, cat_cla=card.cla,
            test_running=lambda: self.test_running,
            bip_handler=self._bip_handler,
            bip_flush=self._bip_flush,
            menu_timeout=self.menu_timeout,
            poll_interval=self.poll_interval,
            card_lock=self.card_lock,
            pli_data=self.pli_data)
        # The persistent BIP/SCP81 session serves this card from now on: the
        # channels of the previous card were closed by the teardown, the
        # listener (if any) kept running.
        self.bip_session.attach_card(self.cat)
        self.bip = self.bip_session
        self.connected = True
        self.card_present = True
        self.card_session += 1
        # The caches belong to the previous card session.
        self.iccid = None
        self.eid = None
        self.arr = None
        self.pbr = None
        self.netstate = None
        self.selection = None
        if skip_card_init:
            self.poll_suspended = False
            return
        # ICCID/ARR/PBR in the CAT-free window (the legacy order), then MF.
        try:
            self.iccid = self._read_iccid()
        except Exception as exc:                # best effort, never fatal
            log.debug('ICCID read failed: %s', exc)
        # The EID on an eUICC (pySim's equip read it too): one ES10 exchange in
        # the CAT-free window, best effort - the chip view re-reads it.
        if esim_module.card_is_euicc(self.card):
            try:
                with esim_module.isdr_selected(self._file_ops()):
                    self.eid = esim_module.get_eid(self.transport)
            except Exception as exc:
                log.debug('EID read failed: %s', exc)
        self.arr = self._read_ef_cache(ARR_READ_PATHS, ARR_RECORD_LIMIT)
        self.pbr = self._read_ef_cache(PBR_READ_PATHS, PBR_RECORD_LIMIT)
        # The network-state monitor: only for a card whose ICCID was readable
        # (the legacy rule - the monitor describes one identifiable profile).
        if self.iccid:
            self._netstate_init()
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
        self.bip = None
        # The BIP/SCP81 session is server-owned and survives a card change;
        # only the old card's channels are dropped (the listener keeps
        # serving the terminal side).  The detach is idempotent.
        try:
            self.bip_session.detach_card()
        except Exception as exc:
            log.debug('BIP session detach failed: %s', exc)
        self.card = None
        self.connected = False
        self.atr = None
        self.iccid = None
        self.eid = None
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
        failure.  A transport exception after the packet was handed to the
        transport persists the counter as well (the outcome is unclear - see
        the ``except`` below) and then propagates: the request still fails.

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
        try:
            result = self._send_secured_packet(sp_hex,
                                               include_cpi=include_cpi,
                                               submit_handler=capture)
        except Exception:
            # The transport failed mid-exchange: the packet may already have
            # reached the card, so the outcome is unclear and the counter
            # advances (the three-way rule the test runner follows too) - an
            # unnecessarily skipped value is harmless, a card left expecting a
            # higher one answers cntr_low forever.  The request itself still
            # fails with the original exception (no response shape to keep).
            if counter_tracked and ota.counter_valid(cntr):
                ota.counter_persist(store, preset_id, ota.next_counter(cntr),
                                    'send-ota', kvn)
            raise
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
                             max_segments=scp80.SCP80_MAX_SEGMENTS,
                             handle_proactive=True):
        """Deliver a secured packet as SMS-PP download ENVELOPEs.

        TS 31.115 §4.3: the whole command packet is split into SMS user-data
        parts (the first SM carries the concatenation IE plus the CPI IE, the
        following ones only the concatenation IE); the card reassembles them.
        One ENVELOPE per segment, the late-PoR STATUS poll only after the last
        (a concatenated download cannot have produced its PoR before the card
        got every part).  Returns the legacy result dict - ``success``,
        ``bytes``, ``segments``, ``sw``, ``response_data`` or ``error``.

        ``handle_proactive=False`` keeps a ``91XX`` answer pending: the test
        runner's scp80 step must not consume the command the script is about
        to assert (its expectation fetches and answers it), and the late-PoR
        STATUS poll is skipped for the same reason.
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
                handle_proactive=handle_proactive,
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

    # ---- the CAP analysis and the single-card RAM installs (3c-3c-2a) -----

    def cap_info(self, body):
        """``POST /api/cap-info``: analyze a ``.cap`` (card-free, read-only).

        The structural parse plus the capmem estimate, in
        :func:`simple_server.capram.cap_info_body`; nothing here touches the
        card, the presets or the SCP80 counters.
        """
        return capram.cap_info_body(body)

    def ram_install(self, body):
        """``POST /api/ram-install``: load and install a ``.cap`` over SCP80.

        The card-independent plan (the ``.cap`` parse and the INSTALL/LOAD
        sequence) is built first, so a bad file or an invalid parameter
        combination is a 400 before anything is sent; the plan then runs as one
        chain on the equipped card (:meth:`_ram_plan_run`).  The caller holds
        the card lock (the handler does).
        """
        if not self.connected or self.transport is None or self.cat is None:
            raise NoCardSessionError('no card session: equip a card first')
        body = dict(body or {})
        plan = capram.install_plan(body)
        return self._ram_plan_run(body, plan)

    def ram_install_app(self, body):
        """``POST /api/ram-install-app``: INSTALL [for install/make selectable].

        For a package that is already loaded: ``mode: 'install'`` reinstalls
        the applet instance (the plan deletes the instance first, tolerating
        "not found"), ``mode: 'make_selectable'`` only re-runs INSTALL [for
        make selectable].  Nothing is loaded - no ``.cap``, no LOAD.
        """
        if not self.connected or self.transport is None or self.cat is None:
            raise NoCardSessionError('no card session: equip a card first')
        body = dict(body or {})
        mode = str(body.get('mode') or 'install').strip()
        if mode == 'make_selectable':
            instance_aid = str(body.get('instance_aid') or '')
            instance_aid = re.sub(r'\s', '', instance_aid).upper()
            if not instance_aid:
                raise ValueError('instance_aid required')
            privileges = (str(body.get('privileges') or '').replace(' ', '')
                          or '00')
            plan = {'kind': 'make-selectable',
                    'seq': [{'name': 'INSTALL [for make selectable]',
                             'apdu': capram.cap_make_selectable_apdu(
                                 instance_aid, privileges)}],
                    'spi2': str(body.get('spi2') or '01'),
                    'include_cpi': body.get('includeCpi', True),
                    'ram_format': ota.normalise_ram_format(
                        body.get('ram_format'))}
        elif mode == 'install':
            # The install deletes the applet instance first (tolerating "not
            # found") so re-installing with new parameters works in one step;
            # the plan builder validates the AIDs and the parameter
            # composition (a 400 here, never mid-chain).
            plan = capram.reinstall_plan(body, kind='install-app')
        else:
            raise ValueError("mode must be 'install' or 'make_selectable'")
        return self._ram_plan_run(body, plan)

    def _ram_plan_run(self, body, plan):
        """Run one RAM plan on the equipped card (the caller holds the lock).

        A plan is a list of named steps (an INSTALL/LOAD chain, GP DELETEs, a
        single INSTALL [for install]); a step flagged ``delete`` tolerates the
        "not found" status words (:data:`simple_server.capram.DELETE_NOT_FOUND`
        - the object is already gone, so the delete's goal is met and the chain
        proceeds), while any other failure stops it.  The per-card part: the
        preset's ISD TAR supplies the keyset (v3.26.0) and the counter is
        seeded from the store (v3.20.0, its accepted value persisted per step).
        A request error raises :class:`ValueError` (the endpoint's 400); a chain
        failure comes back as ``{'success': False, ...}``.
        """
        if not self.connected or self.transport is None or self.cat is None:
            raise NoCardSessionError('no card session: equip a card first')
        body = body or {}
        preset_id = str(body.get('preset_id') or '').strip()
        preset = self.presets.get(preset_id) if preset_id else None
        tar = str(body.get('tar') or presets_store.role_tar(preset, 'isd')
                  or '000000').strip().upper()
        spi1, _warning = ota.spi1_for_tar(preset, tar, body.get('spi1'),
                                          prefer_msl=True)
        kvn, kic, kid, keyset_error = ota.op_keyset_resolve(self.presets, body,
                                                           tar)
        if keyset_error:
            raise ValueError(keyset_error)
        kic_key = str(body.get('kicKey') or '').strip()
        kid_key = str(body.get('kidKey') or '').strip()
        # The counter: an explicit `cntr` wins, else the preset's value for
        # this keyset (the server owns the counters, v3.20.0).
        counter = (ota.counter_seed(self.presets, body, kvn)
                   or str(body.get('cntr') or '00000000'))
        steps = []
        state = {'steps': steps, 'encode_error': None, 'failure': {},
                 'cntr': counter, 'preset_id': preset_id, 'kvn': kvn,
                 'preset': preset, 'rm': ota.tar_is_rm(preset, tar)}
        secured = {'spi1': spi1, 'spi2': plan['spi2'], 'kic': kic, 'kid': kid,
                   'tar': tar, 'kic_key': kic_key, 'kid_key': kid_key,
                   'include_cpi': plan['include_cpi']}
        sequence = plan['seq']
        kind = plan.get('kind') or 'ram'
        label = 'RAM-%s' % kind.upper().replace('-', ' ')
        nv_before = None
        nv_after = None
        self._ram_progress_begin(kind, len(sequence) + 1)
        try:
            # The RAM command format: detected per operation (a read-only probe
            # step) unless the caller pinned compact/expanded.
            ram_format = plan['ram_format']
            if ram_format == 'auto':
                self._ram_progress_step(0, 'FORMAT CHECK')
                ram_format = self._ram_detect_format(secured, state)
            if plan.get('load_size'):
                log.info('%s: LOAD block size %d bytes', label,
                         plan['load_size'])
            log.info('%s: %d step(s), %s', label, len(sequence), ram_format)
            # The NV footprint before the chain (best effort - a card without
            # FF21 skips it).
            if plan.get('nv'):
                nv_before = self._ram_read_ff21(secured, state, ram_format,
                                                'NV before')
            for index, step in enumerate(sequence):
                name = step['name']
                self._ram_progress_step(index + 1, name)
                ok = self._ram_send_gp_apdu(
                    secured, state, name,
                    ota.format_apdu(step['apdu'], ram_format))
                if not ok and step.get('delete'):
                    # a delete tolerates "not found": the object is already
                    # gone, which is exactly what the delete wanted
                    record = steps[-1] if steps else {}
                    if str(record.get('por_sw') or '').upper() \
                            in capram.DELETE_NOT_FOUND:
                        record['delete_not_found'] = True
                        ok = True
                if not ok:
                    # what the failed attempt consumed (a rejected load may keep
                    # or discard its partial file - the delta tells which)
                    if plan.get('nv'):
                        nv_after = self._ram_read_ff21(
                            secured, state, ram_format, 'NV after')
                    response = {
                        'success': False, 'steps': steps,
                        'failed_step': len(steps),
                        'error': (state['encode_error']
                                  or state['failure'].get('error')
                                  or ('%s failed' % name)),
                        'final_cntr': state['cntr'],
                        'ram_format': ram_format}
                    response.update(plan.get('response') or {})
                    response.update(capram.nv_fields(nv_before, nv_after))
                    failed = steps[-1] if steps else {}
                    response.update(
                        ota.cntr_low_fields(failed.get('por_status')))
                    if response.get('cntr_low'):
                        log.info('%s: low counter - the packet counter is not '
                                 "above the card's", label)
                    return response
            if plan.get('nv'):
                nv_after = self._ram_read_ff21(secured, state, ram_format,
                                               'NV after')
        finally:
            self._ram_progress_end()
        response = {'success': True, 'steps': steps,
                    'final_cntr': state['cntr'], 'ram_format': ram_format}
        response.update(plan.get('response') or {})
        response.update(capram.nv_fields(nv_before, nv_after))
        log.info('%s: complete (%d step(s), cntr=%s)', label, len(sequence),
                 state['cntr'])
        return response

    def _ram_send_gp_apdu(self, secured, state, step_name, apdu_hex,
                          silent=False):
        """Send one GP APDU as an SCP80 secured packet; ``True`` on success.

        The step record is appended to ``state['steps']`` (and always available
        as ``state['last_step']``).  ``secured`` carries the SCP80 parameters
        (spi1, spi2, kic, kid, tar, kic_key, kid_key, include_cpi); ``state``
        the mutable run state (steps, encode_error, failure, cntr, ...) and the
        counter advances only for a packet the card accepted and only when the
        packet asks it to check the counter (TS 102 225 §5.1.1 b5b4).

        ``silent=True`` (the NV footprint reads) leaves no step record and no
        failure note behind: the card's answer is only used by the caller, and
        a rejected read must never fail the operation.
        """
        spi1, spi2 = secured['spi1'], secured['spi2']
        try:
            packet, _spi = scp80.build_secured_packet(
                spi1, spi2, secured['kic'], secured['kid'], secured['tar'],
                state['cntr'], apdu_hex, secured['kic_key'],
                secured['kid_key'])
            packet_hex = bytes(packet).hex()
            log.info('RAM C-APDU (%s): %s', step_name, apdu_hex)
            log.info('RAM SECURED-PACKET (%s): %s', step_name, packet_hex)
        except ValueError as exc:
            if not silent:
                state['encode_error'] = str(exc)
                state['steps'].append({'name': step_name,
                                       'por_status': 'encode_error',
                                       'sw': str(exc)})
            log.warning('RAM: %s encode failed: %s', step_name, exc)
            return False
        # Capture the PoR from either transport: inline in the ENVELOPE answer
        # or as a proactive SEND SHORT MESSAGE.  Cards differ - some always
        # submit, whatever the SPI2 request bit says - so the capture is
        # unconditional (v3.6.24).
        capture = cat_session.PorSubmitCapture()
        result = self._send_secured_packet(packet_hex,
                                           include_cpi=secured['include_cpi'],
                                           submit_handler=capture)
        if not result.get('success'):
            if not silent:
                state['steps'].append({
                    'name': step_name, 'por_status': 'envelope_error',
                    'sw': result.get('sw') or result.get('error'),
                    'bytes': result.get('bytes'),
                    'segments': result.get('segments')})
            log.warning('RAM: %s send failed: %s', step_name,
                        result.get('error'))
            return False
        por_hex = result.get('response_data') or ''
        last_sw = result.get('sw')
        por_source = 'envelope'
        submit_hex = capture.submit_response_hex()
        if submit_hex:
            por_hex = submit_hex
            por_source = 'sms-submit'
        elif capture.submit_tpdu_hex:
            # no assembled UD: fall back to a raw RPI packet
            tpdu = bytes.fromhex(capture.submit_tpdu_hex)
            index = tpdu.find(b'\x02\x71\x00')
            if index >= 0:
                por_hex = tpdu[index:].hex()
                por_source = 'sms-submit'
        por = ota.decode_por(spi1, spi2, secured['kic'], secured['kid'],
                             state['cntr'], secured['kic_key'],
                             secured['kid_key'], por_hex,
                             cmd_len=len(apdu_hex) // 2, rm=state.get('rm'))
        step, error = capram.step_result(step_name, last_sw, por, por_hex,
                                         result.get('bytes'),
                                         result.get('segments'))
        state['last_step'] = step
        if not silent:
            state['steps'].append(step)
        log.info('RAM: %s PoR[%s] status=%s remote_sw=%s%s (%s B, %s SM)',
                 step_name, por_source, step.get('por_status', '?'),
                 step.get('por_sw', '-'),
                 (' error=%s' % error) if error else '',
                 result.get('bytes'), result.get('segments'))
        if (step.get('por_status') in ('por_ok', 'no_por')
                and scp80.counter_tracked(spi1)):
            state['cntr'] = ota.next_counter(state['cntr'], True)
            # ... and persist it right away: the server owns the counter now,
            # so a crash mid-install cannot leave the preset behind the card.
            ota.counter_persist(self.presets, state.get('preset_id'),
                                state['cntr'],
                                'ram-%s' % step_name.split()[0].lower(),
                                state.get('kvn'))
        if error:
            if not silent:
                state['failure']['error'] = '%s: %s' % (step_name, error)
            return False
        return True

    def _ram_detect_format(self, secured, state):
        """Detect the card's RAM command format for one operation.

        Send the read-only GET STATUS [ISD] probe compact, then in the two
        expanded codings; the first format whose remote SW succeeds wins
        (compact preferred).  Cards implement different subsets: the reference
        TCA loader sends the listing queries wrapped and unchained, while the
        live card needs the compact chain (a length-mismatched template draws a
        Bad format TLV, which the step record names).  The probe packets are
        recorded as steps and consume counters only when accepted; the result
        is used for this operation only - the next operation re-checks
        (v3.6.24).
        """
        probes = (
            ('compact', capram.RAM_PROBE_APDU),
            ('expanded', ota.format_apdu(capram.RAM_PROBE_APDU_LE,
                                         'expanded')),
            ('expanded-ae', ota.format_apdu(capram.RAM_PROBE_APDU_LE,
                                            'expanded-ae')),
        )
        for ram_format, apdu in probes:
            scratch = {'steps': [], 'encode_error': None, 'failure': {},
                       'cntr': state['cntr'],
                       'preset_id': state.get('preset_id'),
                       'kvn': state.get('kvn'),
                       'preset': state.get('preset'),
                       'rm': state.get('rm')}
            ok = self._ram_send_gp_apdu(secured, scratch,
                                        'FORMAT CHECK (%s)' % ram_format, apdu)
            state['steps'].extend(scratch['steps'])
            state['cntr'] = scratch['cntr']
            if ok:
                return ram_format
        return 'compact'

    def _ram_read_ff21(self, secured, state, ram_format, label):
        """Best-effort GET DATA FF21 read: the free NV bytes, or ``None``.

        Sent through the normal step sender with ``silent=True``, so it
        consumes and advances the counter correctly but leaves no step record
        or failure note behind.  A card without FF21 (or a rejected read) must
        never fail the operation (TS 102 226 §8.2.1.7.2).
        """
        apdu = ota.format_apdu('80CAFF2100', ram_format)
        if not self._ram_send_gp_apdu(secured, state, label, apdu,
                                      silent=True):
            return None
        return capram.free_nv((state.get('last_step') or {}).get('por_data'))

    def _ram_progress_begin(self, kind, total):
        self.ram_progress.update({'active': True, 'kind': str(kind), 'step': 0,
                                  'total': int(total or 0), 'name': '',
                                  'started': time.time()})

    def _ram_progress_step(self, step, name):
        self.ram_progress.update({'step': int(step), 'name': str(name or '')})

    def _ram_progress_end(self):
        self.ram_progress['active'] = False

    def ram_progress_payload(self):
        """The ``/api/status.ram_progress`` body (pure read of the progress)."""
        progress = self.ram_progress
        started = progress.get('started') or 0
        return {'active': bool(progress.get('active')),
                'kind': progress.get('kind', ''),
                'step': int(progress.get('step') or 0),
                'total': int(progress.get('total') or 0),
                'name': progress.get('name', ''),
                'elapsed': (round(time.time() - started, 1) if started else 0)}

    # ---- the CAP compatibility test (3c-3c-2b) ----------------------------

    def cap_compat(self, body):
        """``POST /api/cap-compat``: can this card satisfy the CAP's imports?

        The plan (the parsed ``.cap``, the diagnostic import probe, the block
        completing the Import component) is :func:`simple_server.capram.
        cap_compat_plan`'s; the truncated chain - ``INSTALL [for load]`` plus
        the LOAD blocks up to the boundary - then runs on the equipped card.
        Stopping there answers the LOAD/import gate (JC VM Specification
        v2.2.2 §4.5.2: the major versions must be equal and the CAP's minor not
        above the resident package's; the observed live rejections land in the
        boundary block) **without committing anything**: no ``INSTALL [for
        install]``, and the last-block flag is cleared when the Import ends in
        the load file's last block
        (:func:`~simple_server.capram.cap_compat_plan`).  The link gate stays
        the real install's.

        The response is the legacy shape the PWA reads: ``success`` and
        ``imports_ok`` (the verdict), ``steps``, ``boundary_block``/
        ``total_blocks``, ``load_file_aid``/``module_aid``, ``ram_format``,
        ``probe_imports``/``probe_unmatched``, ``load_block_size*``,
        ``final_cntr`` and - on a rejection - ``failed_step``, ``error``,
        ``cntr_low``.  The generic "<step> failed" becomes the legacy's "the
        card rejected the import list": a refused ENVELOPE carries no
        step-level detail.
        """
        if not self.connected or self.transport is None or self.cat is None:
            raise NoCardSessionError('no card session: equip a card first')
        body = dict(body or {})
        plan = capram.cap_compat_plan(body)
        response = self._ram_plan_run(body, plan)
        response['imports_ok'] = bool(response.get('success'))
        if not response.get('success'):
            error = str(response.get('error') or '')
            if not error or error.endswith(' failed'):
                response['error'] = 'the card rejected the import list'
        log.info('CAP-COMPAT: %s - imports %s (LOADs to %d/%d)', str(
            response.get('load_file_aid') or ''),
            'OK' if response.get('imports_ok') else 'REJECTED',
            int(response.get('boundary_block') or 0),
            int(response.get('total_blocks') or 0))
        return response

    # ---- the TAR probe (3c-3c-2b) -----------------------------------------

    def tar_probe(self, body):
        """``POST /api/tar-probe``: one secured packet per preset TAR.

        The probe checks the **preset's TAR list only** (v3.26.0): a ``tars``
        list selects a subset, a TAR outside the preset is refused - there is
        no free-TAR input any more.  Each row uses its own TAR's MSL for SPI1
        (the request's ``spi1`` is only the fallback the preset does not need)
        and its own **default keyset**, and sends the harmless SELECT MF
        (:data:`~simple_server.capram.TAR_PROBE_DEFAULT_APDU`) so the card's
        answer to the *packet* is the verdict (`registered`/`no_answer`/
        `no_por`/`refused`, :func:`simple_server.ota.tar_probe_verdict`).

        Every TAR's SPI1 and keyset is resolved **before the first packet**:
        a security MSL without a keyset or a TAR without a counter check
        refuses the whole probe instead of leaving it half-run; the probe
        command and ``spi2`` are pre-flighted the same way (a malformed APDU
        would otherwise turn into one ``encode_error`` per row, i.e. a
        `refused` verdict for every TAR).  Each accepted packet consumes a
        counter, persisted per keyset (the server owns the counters, v3.20.0);
        an explicit ``cntr`` applies to the first row only.
        """
        if not self.connected or self.transport is None or self.cat is None:
            raise NoCardSessionError('no card session: equip a card first')
        body = dict(body or {})
        preset_id = str(body.get('preset_id') or '').strip()
        if not preset_id:
            raise ValueError('preset_id is required - the probe checks the '
                             'preset TAR list')
        preset = self.presets.get(preset_id)
        if not preset:
            raise ValueError('unknown preset')
        spi1_base = str(body.get('spi1') or '').strip().upper()
        spi2 = str(body.get('spi2') or '01').strip().upper()
        if not re.fullmatch(r'[0-9A-F]{2}', spi2):
            raise ValueError('spi2 must be one hex byte')
        apdu = re.sub(r'\s', '', str(
            body.get('apdu') or capram.TAR_PROBE_DEFAULT_APDU)).upper()
        # Pre-flight the probe command.  A malformed one only fails inside
        # each row's encoder, where the `encode_error` record reads as a
        # `refused` verdict for every TAR - an operator typo would look like
        # "no application is registered".
        if (not apdu or len(apdu) % 2
                or not re.fullmatch(r'[0-9A-F]+', apdu)):
            raise ValueError('apdu must be an even number of hex digits')
        if len(apdu) > 2 * 255:
            raise ValueError('apdu is larger than the 255-octet data field')
        role_label = {'isd': 'ISD', 'uiccRfm': 'UICC RFM',
                      'usimRfm': 'ADF RFM'}
        by_tar = {}
        for entry in (preset.get('tars') or []):
            tar = str(entry.get('tar') or '').strip().upper()
            if tar:
                by_tar[tar] = entry
        wanted = body.get('tars')
        if isinstance(wanted, list) and wanted:
            entries = []
            for item in wanted:
                tar = str(item).replace(' ', '').upper()
                entry = by_tar.get(tar)
                if entry is None:
                    raise ValueError(
                        'TAR %s is not in the preset - the probe checks the '
                        'preset TAR list only' % tar)
                entries.append((tar, entry))
        else:
            entries = [(tar, entry) for tar, entry in by_tar.items()]
        # Resolve every TAR's SPI1 and keyset before the first packet is sent.
        resolved = []
        for tar, entry in entries:
            tar_spi1, _warning = ota.spi1_for_tar(preset, tar, spi1_base,
                                                 prefer_msl=True)
            if not scp80.counter_tracked(tar_spi1):
                raise ValueError(
                    'the TAR probe needs a counter check - SPI1 %s for TAR %s '
                    'has none (b5b4 = 00)' % (tar_spi1, tar))
            row_kvn = entry.get('keyset')
            row_keyset = None
            if row_kvn:
                row_keyset = ota.preset_keyset_by_kvn(preset, row_kvn)
                if row_keyset is None:
                    raise ValueError(
                        'keyset %02X (TAR %s) is not defined in the preset'
                        % (row_kvn, tar))
            else:
                msl = str(entry.get('msl') or '')
                if msl and msl != '00':
                    raise ValueError(
                        'TAR %s needs a default keyset (its MSL %s requires '
                        'security) - set it in the Cards tab' % (tar, msl))
            label = (str(entry.get('desc') or '').strip()
                     or role_label.get(entry.get('role') or '', ''))
            resolved.append((tar, label, tar_spi1, row_kvn, row_keyset))
        explicit_cntr = str(body.get('cntr') or '').strip().upper()
        state = {'steps': [], 'encode_error': None, 'failure': {},
                 'cntr': '00000000', 'preset_id': preset_id, 'kvn': None}
        secured = {'spi1': spi1_base, 'spi2': spi2, 'kic': '00', 'kid': '00',
                   'tar': '000000', 'kic_key': '', 'kid_key': '',
                   'include_cpi': body.get('includeCpi', True)}
        log.info('TAR-PROBE: %d TAR(s), APDU %s, SPI %s/%s', len(entries),
                 apdu, spi1_base or '-', spi2)
        self._ram_progress_begin('tar-probe', len(entries))
        try:
            for index, (tar, _label, tar_spi1, row_kvn,
                        row_keyset) in enumerate(resolved):
                self._ram_progress_step(index, 'TAR ' + tar)
                secured['tar'] = tar
                secured['spi1'] = tar_spi1
                secured['kic'] = str((row_keyset or {}).get('kic') or '00')
                secured['kid'] = str((row_keyset or {}).get('kid') or '00')
                secured['kic_key'] = str((row_keyset or {}).get('kicKey') or '')
                secured['kid_key'] = str((row_keyset or {}).get('kidKey') or '')
                state['kvn'] = row_kvn
                # The response form follows the packet's TAR (TS 102 226 §4,
                # the legacy's per-packet rule): only a remote-management TAR
                # answers with the compact/scripting structures, so a non-RM
                # TAR's secured data stays raw and no status word is invented
                # from bytes that merely happen to parse (v3.21.0).
                state['rm'] = ota.tar_is_rm(preset, tar)
                # Each keyset has its own counter (TS 102 225 A.1): seed the
                # row's from the store (an explicit `cntr` is the first row's).
                if index == 0 and explicit_cntr:
                    state['cntr'] = explicit_cntr
                else:
                    state['cntr'] = (ota.counter_seed(
                        self.presets, {'preset_id': preset_id}, row_kvn)
                        or '00000000')
                self._ram_send_gp_apdu(secured, state, 'TAR ' + tar, apdu)
        finally:
            self._ram_progress_end()
        results = []
        for (tar, label, tar_spi1, row_kvn, _keyset), step in zip(
                resolved, state['steps']):
            results.append({
                'tar': tar, 'label': label, 'spi1': tar_spi1, 'kvn': row_kvn,
                'verdict': ota.tar_probe_verdict(step),
                'sw': step.get('sw'),
                'por_status': step.get('por_status'),
                'por_sw': step.get('por_sw'),
                'por_data': step.get('por_data'),
                'bytes': step.get('bytes'), 'segments': step.get('segments'),
            })
        registered = sum(1 for row in results
                         if row['verdict'] == 'registered')
        # The top-level `kvn`/`spi1` are the legacy's: the first row's keyset
        # and the request's `spi1` fallback - **not** a row's effective SPI1
        # (which is the row's `spi1` in `results`).
        return {'success': True, 'results': results, 'steps': state['steps'],
                'registered': registered, 'total': len(results),
                'final_cntr': state['cntr'],
                'kvn': resolved[0][3] if resolved else None,
                'spi1': spi1_base, 'spi2': spi2, 'apdu': apdu}

    # ---- the batch install (3c-3c-2b) -------------------------------------

    def ram_batch_active(self):
        """True while a batch runs (the start guard; the test-runner guard
        will ask too)."""
        with self.ram_batch_lock:
            return bool(self.ram_batch and self.ram_batch.get('active'))

    def _ram_batch_new_state(self, plan):
        """A fresh run's state: the plan is card independent, everything else
        is resolved at each card."""
        return {'active': True, 'phase': 'waiting_card', 'stop': False,
                'started': time.time(), 'stopped': None, 'plan': plan,
                'session': None, 'seen': {}, 'attempts': [],
                'attempts_total': 0,
                'counts': {'installed': 0, 'already_absent': 0,
                           'failed': 0, 'skipped': 0},
                'current': None, 'notice': ''}

    def _ram_batch_record(self, rec):
        """Record one attempt in the current run.

        The report keeps the last :data:`RAM_BATCH_ATTEMPTS_MAX` entries (the
        total keeps counting), the counts update and the notice names the
        outcome for the panel's headline.
        """
        state = self.ram_batch
        if state is None:
            return
        with self.ram_batch_lock:
            rec.setdefault('ts', time.time())
            state['attempts'].append(rec)
            if len(state['attempts']) > RAM_BATCH_ATTEMPTS_MAX:
                del state['attempts'][:-RAM_BATCH_ATTEMPTS_MAX]
            state['attempts_total'] += 1
            result = rec.get('result')
            if result in state['counts']:
                state['counts'][result] += 1
            who = rec.get('iccid') or 'the card'
            # "Card <ICCID>" - a card without a readable ICCID is "The card"
            # (the old placeholder produced "Card the card skipped: ...").
            card = ('Card %s' % who) if rec.get('iccid') else 'The card'
            if result == 'installed':
                # the success verb follows the operation (the counts bucket is
                # 'installed' for every kind - "the operation succeeded")
                kind = ((state.get('plan') or {}).get('kind')) or 'install-cap'
                verb = {'delete': 'deleted',
                        'reinstall-app': 'reinstalled'}.get(kind, 'installed')
                state['notice'] = '%s %s.' % (card, verb)
            elif result == 'already_absent':
                state['notice'] = '%s was already absent.' % card
            elif result == 'failed':
                step = rec.get('failed_step') or ''
                sw = rec.get('por_sw') or rec.get('sw') or ''
                state['notice'] = '%s failed%s%s.' % (
                    card, (' at ' + step) if step else '',
                    (' (' + sw + ')') if sw else '')
            else:
                state['notice'] = '%s was skipped: %s.' % (
                    card, rec.get('reason') or '')
        detail = rec.get('reason') or rec.get('failed_step') or ''
        label = {'already_absent': 'already absent'}.get(result, result)
        log.info('RAM-BATCH: %s%s%s', label,
                 (' ' + who) if rec.get('iccid') else '',
                 (' - ' + detail) if detail else '')

    @staticmethod
    def _ram_batch_failure_fields(result):
        """The failing step's name and status words for the batch report.

        The panel shows e.g. ``LOAD (3/7) - 6A80``: ``por_sw`` is the card's
        own answer to the remote command, ``sw`` the transport status word.
        """
        steps = result.get('steps') or []
        index = result.get('failed_step')
        rec = (steps[index - 1]
               if isinstance(index, int) and 0 < index <= len(steps)
               else (steps[-1] if steps else {}))
        return {'failed_step': rec.get('name') or '',
                'por_sw': rec.get('por_sw') or '',
                'por_status': rec.get('por_status') or '',
                'por_error': rec.get('por_error') or '',
                'sw': rec.get('sw') or ''}

    @staticmethod
    def _ram_batch_steps_fields(result):
        """The attempt's per-step verdict tail for the panel.

        The last three steps (name, ok, remote SW) plus the chain length; a
        delete step that answered "not found" counts as ok, while a step the
        transport refused (``envelope_error`` - it has no ``por_error``) is
        never ok.  A full LOAD chain stays short in the report.
        """
        steps = result.get('steps') or []
        out = []
        for step in steps[-3:]:
            out.append({'name': step.get('name') or '',
                        'ok': ((not step.get('por_error')
                                and step.get('por_status') != 'envelope_error')
                               or bool(step.get('delete_not_found'))),
                        'sw': step.get('por_sw') or '',
                        'not_found': bool(step.get('delete_not_found'))})
        return {'steps': out, 'steps_total': len(steps)}

    def _ram_batch_handle_card(self, state, session=None):
        """One equipped card: match the preset by ICCID, then run the plan.

        The preset is matched by ICCID (:meth:`PresetStore.find_by_iccid`); its
        ISD role TAR supplies the keyset and the counter seed (v3.26.0/v3.20.0).
        A card already attempted in this run - any outcome - is reported, never
        re-installed: restarting the batch is the deliberate retry.

        ``session`` is the card session the worker decided on; the ICCID and
        the preset are read *before* the card lock, so the chain re-checks both
        under the lock and skips the card if either moved (a swap in between
        must never run another card's plan - the v3.28.0 audit class).
        """
        session = self.card_session if session is None else session
        iccid = presets_store.normalize_iccid(self.iccid or '')
        if not iccid:
            self._ram_batch_record({'result': 'skipped',
                                    'reason': 'no ICCID readable'})
            return
        with self.ram_batch_lock:
            seen_outcome = state['seen'].get(iccid)
        if seen_outcome is not None:
            self._ram_batch_record({
                'iccid': iccid, 'result': 'skipped',
                'reason': 'already processed in this batch (%s)'
                          % seen_outcome})
            return
        store = self.presets
        preset = store.find_by_iccid(iccid) if store is not None else None
        if preset is None:
            with self.ram_batch_lock:
                state['seen'][iccid] = 'no preset'
            self._ram_batch_record({'iccid': iccid, 'result': 'skipped',
                                    'reason': 'no preset matches this ICCID'})
            return
        name = preset.get('name') or ''
        body = {'preset_id': preset.get('id') or '',
                'tar': presets_store.role_tar(preset, 'isd')}
        with self.ram_batch_lock:
            state['seen'][iccid] = 'pending'
            state['current'] = {'iccid': iccid, 'preset': name,
                                'started': time.time()}
            state['phase'] = 'installing'
            # A new card's install supersedes the previous attempt's headline:
            # the notice reports the *last finished* card, so it must not
            # linger while the next card is being installed (v3.28.1).
            state['notice'] = ''
        started = time.time()
        result = None
        error = None
        request_error = False
        try:
            with self.card_lock:
                if (self.card_session != session
                        or presets_store.normalize_iccid(
                            self.iccid or '') != iccid):
                    # The card was swapped while the preset was being matched:
                    # the plan belongs to the previous card - skip it (the new
                    # session is the next poll's card).
                    raise ValueError('the card changed before the install '
                                     'started')
                result = self._ram_plan_run(body, state['plan'])
        except ValueError as exc:
            # A request/eligibility error (the preset's ISD keyset, the plan):
            # the install never started - the card is skipped.
            error = str(exc)
            request_error = True
        except Exception as exc:            # a lost card, a transport error
            error = str(exc)
        finally:
            with self.ram_batch_lock:
                state['current'] = None
                if not state['stop']:
                    state['phase'] = 'waiting_card'
        duration = int((time.time() - started) * 1000)
        if result is not None and result.get('success'):
            outcome = 'installed'
            rec = {'iccid': iccid, 'preset': name, 'result': outcome,
                   'duration_ms': duration,
                   'final_cntr': result.get('final_cntr'),
                   'load_file_aid': result.get('load_file_aid'),
                   'module_aid': result.get('module_aid'),
                   'nv_delta': result.get('nv_delta')}
            rec.update(self._ram_batch_steps_fields(result))
            # A delete run whose DELETE answered "not found" is a success (the
            # object is gone, the goal is met) but not a deletion: report it as
            # "already absent" and count it separately (v3.36.0).  Only the
            # delete kind - a reinstall's tolerated pre-delete is not the
            # outcome.
            if ((state.get('plan') or {}).get('kind') == 'delete'
                    and any(step.get('delete_not_found')
                            for step in (result.get('steps') or []))):
                outcome = 'already_absent'
                rec['result'] = outcome
                rec['not_found'] = True
        elif result is not None:
            outcome = 'failed'
            rec = {'iccid': iccid, 'preset': name, 'result': outcome,
                   'duration_ms': duration,
                   'final_cntr': result.get('final_cntr'),
                   'cntr_low': bool(result.get('cntr_low')),
                   'error': result.get('error') or ''}
            rec.update(self._ram_batch_failure_fields(result))
            rec.update(self._ram_batch_steps_fields(result))
        elif request_error:
            outcome = 'skipped'
            rec = {'iccid': iccid, 'preset': name, 'result': outcome,
                   'duration_ms': duration,
                   'reason': error or 'the install could not start'}
        else:
            outcome = 'failed'
            rec = {'iccid': iccid, 'preset': name, 'result': outcome,
                   'duration_ms': duration,
                   'error': error or 'the install failed'}
        with self.ram_batch_lock:
            state['seen'][iccid] = outcome
        self._ram_batch_record(rec)

    def _ram_batch_worker(self, state):
        """The batch loop: wait for a new card session, install, record, repeat.

        A card already equipped at Start installs immediately.  The equip must
        have finished (``card_ready_session`` matches) - acting mid-equip
        would consume the session with a bogus "no ICCID" skip (the v3.28.0
        audit).  The stop flag ends the loop; a running install always
        finishes first (`stop` is checked between cards, never inside a
        chain).
        """
        try:
            while True:
                with self.ram_batch_lock:
                    if state['stop']:
                        break
                session = self.card_session
                with self.ram_batch_lock:
                    if state['stop']:
                        break
                    connected = bool(self.connected
                                     and self.card_ready_session == session)
                    fresh = connected and session != state['session']
                    if fresh:
                        state['session'] = session
                    elif state['phase'] != 'installing':
                        state['phase'] = 'waiting_card'
                if not fresh:
                    if self._closed.wait(RAM_BATCH_POLL):
                        break
                    continue
                try:
                    self._ram_batch_handle_card(state, session)
                except Exception as exc:
                    # An unattended bulk run must survive a surprise: the card
                    # is recorded and the loop keeps waiting for the next one.
                    log.warning('RAM-BATCH: card handling error: %s', exc)
                    self._ram_batch_record(
                        {'result': 'skipped',
                         'reason': 'unexpected error: %s' % exc})
        finally:
            with self.ram_batch_lock:
                state['active'] = False
                state['current'] = None
                state['phase'] = 'stopped' if state['stop'] else 'finished'
                state['stopped'] = time.time()
                phase, total = state['phase'], state['attempts_total']
            log.info('RAM-BATCH: %s after %d attempts', phase, total)

    def ram_batch_start(self, body):
        """``POST /api/ram-batch/start``: validate the plan, spawn the worker.

        ``kind`` selects the operation: ``install-cap`` (the ``.cap`` chain,
        optionally with the delete-package pre-step), ``delete`` (one GP
        DELETE) or ``reinstall-app`` (DELETE the instance + INSTALL [for
        install]).  The plan is card independent, so it is built **once** here -
        a bad file or parameter is a 400 before any card is touched.

        Raises :class:`~simple_server.server.BatchBusyError` (409) when another
        batch or a test run is active and :class:`ValueError` (400) for a bad
        request.  No card is needed: the worker waits for one.
        """
        with self.ram_batch_lock:
            if self.ram_batch_active():
                raise BatchBusyError('a batch is already running - stop it '
                                     'first')
            if self.test_running:
                raise BatchBusyError('a test run is active - stop it first')
        body = dict(body or {})
        kind = str(body.get('kind') or 'install-cap').strip().lower()
        if kind == 'install-cap':
            plan = capram.install_plan(body)
        elif kind == 'delete':
            plan = capram.delete_plan(body)
        elif kind == 'reinstall-app':
            plan = capram.reinstall_plan(body)
        else:
            raise ValueError("kind must be 'install-cap', 'delete' or "
                             "'reinstall-app'")
        state = self._ram_batch_new_state(plan)
        with self.ram_batch_lock:
            self.ram_batch = state
        thread = threading.Thread(target=self._ram_batch_worker,
                                  args=(state,), name='ram-batch', daemon=True)
        with self.ram_batch_lock:
            self._ram_batch_thread = thread
        thread.start()
        log.info('RAM-BATCH: started (%s, %d step(s))', plan['kind'],
                 len(plan['seq']))
        return state

    def ram_batch_stop(self):
        """Request a batch stop; ``False`` when no batch is running.

        A running install finishes first - an interrupted LOAD chain can leave
        a half-loaded package - then the worker ends the run.  The endpoint is
        card-free (:data:`CARD_FREE_POST`), so the button works while the
        worker holds the card lock.
        """
        with self.ram_batch_lock:
            if self.ram_batch is None or not self.ram_batch.get('active'):
                return False
            self.ram_batch['stop'] = True
            self.ram_batch['phase'] = 'stopping'
        log.info('RAM-BATCH: stop requested')
        return True

    def ram_batch_clear(self):
        """Drop a finished run's report; ``False`` while a batch is active."""
        with self.ram_batch_lock:
            if self.ram_batch and self.ram_batch.get('active'):
                return False
            self.ram_batch = None
        return True

    def ram_batch_status(self):
        """The batch panel's status payload (the summary plus the attempts)."""
        with self.ram_batch_lock:
            state = self.ram_batch
            if state is None:
                return {'active': False, 'phase': 'idle', 'kind': None,
                        'attempts': [], 'attempts_total': 0, 'current': None,
                        'notice': '',
                        'counts': {'installed': 0, 'already_absent': 0,
                                   'failed': 0, 'skipped': 0},
                        'started': None, 'stopped': None, 'progress': None}
            current = state.get('current')
            return {'active': bool(state.get('active')),
                    'phase': state.get('phase'),
                    'kind': ((state.get('plan') or {}).get('kind')) or None,
                    'attempts': list(state['attempts']),
                    'attempts_total': state['attempts_total'],
                    'current': dict(current) if current else None,
                    'notice': state.get('notice') or '',
                    'counts': dict(state['counts']),
                    'started': state.get('started'),
                    'stopped': state.get('stopped'),
                    'progress': (self.ram_progress_payload()
                                 if current else None)}

    def ram_batch_summary(self):
        """The small ``/api/status.ram_batch`` summary (None when no run).

        The panel's headline rides the card-free 2 s status poll, so it must
        not carry the attempts list - the detailed report is fetched only when
        the summary says it changed (v3.28.0).
        """
        with self.ram_batch_lock:
            state = self.ram_batch
            if state is None:
                return None
            current = state.get('current')
            return {'active': bool(state.get('active')),
                    'phase': state.get('phase'),
                    'kind': ((state.get('plan') or {}).get('kind')) or None,
                    'current': dict(current) if current else None,
                    'notice': state.get('notice') or '',
                    'attempts_total': state['attempts_total'],
                    'counts': dict(state['counts']),
                    'started': state.get('started'),
                    'stopped': state.get('stopped')}

    def _stop_ram_batch_worker(self):
        """End the batch worker at shutdown (a bounded join, see :meth:`close`)."""
        with self.ram_batch_lock:
            if self.ram_batch is not None:
                self.ram_batch['stop'] = True
            thread = self._ram_batch_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)

    # ---- the test scripts, the suites and the runs (3c-3d-1/2) ------------

    def abort_card_transport(self):
        """Try to cancel a blocking ``send_apdu``; the link is marked stale.

        The stuck-card watchdog calls this (through
        :meth:`simple_server.testrun.TestRunner.watchdog_check`): pyscard's
        ``SCardTransmit`` has no timeout, so the only way to break a silent
        card's exchange is to disconnect the PC/SC link.  The disconnect runs
        on its own daemon thread and the call returns immediately - pcsc-lite
        serializes the disconnect behind the pending transmit (both take the
        per-context lock), so a synchronous call would block the watchdog
        instead of cancelling anything.  **Never takes the card lock**: the
        stuck runner holds it.  Where the platform does cancel, the blocked
        call raises and the normal step-failure path finalises the run; where
        it does not, the run stays flagged stuck until the driver returns.
        Returns ``True`` when a PC/SC connection existed (the abort was
        attempted).
        """
        connection = getattr(self.transport, '_con', None)
        if connection is None:
            log.warning('TEST-WATCHDOG: no PC/SC connection to abort')
            self._transport_stale = True
            return False

        def _run():
            try:
                connection.disconnect()
            except Exception as exc:
                log.warning('TEST-WATCHDOG: PC/SC disconnect failed: %s', exc)
            finally:
                self._transport_stale = True

        threading.Thread(target=_run, name='test-abort', daemon=True).start()
        return True

    def test_scripts_info(self):
        """``GET /api/test/scripts``: the store info plus every script."""
        store = self.test_scripts
        info = dict(store.info())
        info['scripts'] = store.list()
        return info

    def test_script_add(self, body):
        """``POST /api/test/scripts``: create one script in its suite.

        The body is ``{name, steps, suite_id, require_adm?}`` or
        ``{script: {...}}``.  A script belongs to exactly one suite (the suite
        is the root object): the ``suite_id`` must name an existing suite
        (400 otherwise) and the suite's member list gains the new script.  The
        store validates with the runner's engine
        (:class:`~simple_server.shared.test_scripts.TestScriptError` -> the
        dispatch's 400).
        """
        script = (body.get('script') if isinstance(body.get('script'), dict)
                  else body)
        suite_id = str((script or {}).get('suite_id') or '').strip().lower()
        suite = self.test_suites.get(suite_id) if suite_id else None
        if suite is None:
            raise ValueError('a test script belongs to a suite: pass the '
                             'suite_id of an existing suite')
        try:
            stored = self.test_scripts.add(script)
        except OSError as exc:
            raise self._test_store_failure(exc) from exc
        # The suite owns the member list (order, role, on_fail): attach.
        try:
            entries = list(suite['scripts']) + [
                {'script_id': stored['id'], 'role': 'member',
                 'on_fail': 'stop'}]
            self.test_suites.update(suite['id'], {'scripts': entries})
        except (test_suites_store.TestSuiteError, OSError) as exc:
            raise self._test_store_failure(exc) from exc
        return {'ok': True, 'script': stored}

    def test_script_update(self, body):
        """``POST /api/test/scripts/update``: replace name/steps/require_adm.

        Returns ``None`` for an unknown id (the endpoint's 404).  A changed
        ``suite_id`` is the **move** operation: both stores are updated in
        the one request (:func:`simple_server.suites.script_move`), so a
        duplicate listing or a stale owner is repaired; an unchanged
        ``suite_id`` keeps its value.  Returns ``(entry, moved)``.
        """
        fields = (body.get('script') if isinstance(body.get('script'), dict)
                  else body)
        sid = body.get('id')
        current = self.test_scripts.get(sid)
        new_suite = fields.get('suite_id') if isinstance(fields, dict) else None
        if current is not None and new_suite not in (None, '',
                                                     current.get('suite_id')):
            entry, error, status = suites_module.script_move(
                self.test_scripts, self.test_suites, current, new_suite)
            if entry is None:
                raise suites_module.MoveError(error, status)
            return entry, True
        fields = {key: value for key, value in (fields or {}).items()
                  if key not in ('id', 'suite_id')}
        try:
            return self.test_scripts.update(sid, fields), False
        except OSError as exc:
            raise self._test_store_failure(exc) from exc

    def test_script_delete(self, body):
        """``POST /api/test/scripts/delete``: the legacy ``{ok, removed}``.

        The owning suite's member list is the only reference, so the script is
        detached from it first - a delete never leaves a dangling id.
        """
        current = self.test_scripts.get(body.get('id'))
        if current is not None:
            suites_module.script_detach(self.test_suites, current)
        try:
            removed = self.test_scripts.remove(body.get('id'))
        except OSError as exc:
            raise self._test_store_failure(exc) from exc
        return {'ok': True, 'removed': bool(removed)}

    def test_script_import(self, body):
        """``POST /api/test/scripts/import``: the merge/replace import.

        The legacy shape (an old localStorage export included) is accepted;
        invalid entries are reported in ``errors`` instead of failing the
        whole import.  A ``replace`` is refused while any suite lists scripts
        (the wipes would leave every member list dangling), a target
        ``suite_id`` attaches the imported scripts to that suite's member
        list, and a misspelled ``mode`` is a 400 - never a silent merge (the
        v3.27.0 rule).
        """
        items = body.get('scripts')
        if not isinstance(items, list) or len(items) > 1000:
            raise ValueError('scripts must be a list of at most 1000 entries')
        raw_mode = body.get('mode')
        if raw_mode is not None and raw_mode not in ('merge', 'replace'):
            raise ValueError("mode must be 'merge' or 'replace'")
        mode = raw_mode or 'merge'
        if mode == 'replace':
            holders = [suite for suite in self.test_suites.list()
                       if suite['scripts']]
            if holders:
                raise ValueError(
                    'replace would leave %d suite(s) with dangling script '
                    'references - delete their scripts first, or import with '
                    'merge' % len(holders))
        target = (self.test_suites.get(body.get('suite_id'))
                  if body.get('suite_id') else None)
        if body.get('suite_id') and target is None:
            raise ValueError('unknown target suite')
        try:
            result = self.test_scripts.import_scripts(
                items, mode, suite_id=target['id'] if target else None)
        except OSError as exc:
            raise self._test_store_failure(exc) from exc
        if target is not None and result.get('added_ids'):
            try:
                entries = list(target['scripts']) + [
                    {'script_id': sid, 'role': 'member', 'on_fail': 'stop'}
                    for sid in result['added_ids']]
                self.test_suites.update(target['id'], {'scripts': entries})
            except (test_suites_store.TestSuiteError, OSError) as exc:
                raise self._test_store_failure(exc) from exc
        result.pop('added_ids', None)
        result['ok'] = True
        return result

    def test_script_move(self, body):
        """``POST /api/test/scripts/move``: hand a script to another suite.

        The cross-store operation (:func:`simple_server.suites.script_move`):
        detach from every listing suite, attach to the target, keep the
        ``on_fail``.  Returns the moved entry.
        """
        current = self.test_scripts.get(body.get('id'))
        if current is None:
            raise suites_module.MoveError('unknown test script id', 404)
        entry, error, status = suites_module.script_move(
            self.test_scripts, self.test_suites, current, body.get('suite_id'))
        if entry is None:
            raise suites_module.MoveError(error, status)
        return entry

    def test_script_copy(self, body):
        """``POST /api/test/scripts/copy``: duplicate a script into a suite.

        The copy carries the source's ``require_adm`` and steps under a new
        name (``<name> (copy)`` unless given) and joins the target's member
        list as a ``member``.  Returns the created entry.
        """
        current = self.test_scripts.get(body.get('id'))
        if current is None:
            raise suites_module.MoveError('unknown test script id', 404)
        target = self.test_suites.get(body.get('suite_id'))
        if target is None:
            raise ValueError('unknown target suite')
        name = (str(body.get('name') or '').strip()
                or (current['name'] + ' (copy)'))
        try:
            entry = self.test_scripts.add(
                {'name': name, 'suite_id': target['id'],
                 'require_adm': current.get('require_adm', False),
                 'steps': copy.deepcopy(current['steps'])})
        except OSError as exc:
            raise self._test_store_failure(exc) from exc
        try:
            entries = list(target['scripts']) + [
                {'script_id': entry['id'], 'role': 'member',
                 'on_fail': 'stop'}]
            self.test_suites.update(target['id'], {'scripts': entries})
        except (test_suites_store.TestSuiteError, OSError) as exc:
            raise self._test_store_failure(exc) from exc
        return entry

    @staticmethod
    def _test_store_failure(exc):
        """A test-script/test-suite store I/O failure (a JSON 500).

        One message serves both stores (the legacy named the script store;
        the suites use the same helper).
        """
        return RuntimeError('test store write failed: %s' % exc)

    def migrate_scripts_to_suites(self):
        """Run the suite ownership migration/reconciliation (the startup pass).

        The entry point (Phase 3d) calls this once before serving: it attaches
        the pre-suite scripts (:mod:`simple_server.suites`).  Idempotent.
        """
        return suites_module.migrate_scripts_to_suites(self.test_scripts,
                                                       self.test_suites)

    def reconcile_scripts_to_suites(self):
        """Repair the script/suite ownership invariant; returns the count."""
        return suites_module.reconcile_scripts_to_suites(self.test_scripts,
                                                         self.test_suites)

    # ---- the test suites -------------------------------------------------

    def test_suites_info(self):
        """``GET /api/test/suites``: the store info plus every suite."""
        info = dict(self.test_suites.info())
        info['suites'] = self.test_suites.list()
        return info

    def test_suite_add(self, body):
        """``POST /api/test/suites``: create one suite.

        The body is ``{name, scripts?, require_adm?, start_reset?}`` or
        ``{suite: {...}}``.  Every member reference must name a stored script
        before the suite is written (a suite holds references) and must not
        already belong to another existing suite (the one-owner invariant:
        ownership changes through the move/copy endpoints - a reconciled
        dangling ``suite_id`` is not a conflict).
        """
        suite = (body.get('suite') if isinstance(body.get('suite'), dict)
                 else body)
        error = suites_module.suite_refs_error(self.test_scripts, suite)
        if error:
            raise ValueError(error)
        error = suites_module.suite_owners_error(
            self.test_scripts, self.test_suites, suite)
        if error:
            raise ValueError(error)
        try:
            stored = self.test_suites.add(suite)
        except OSError as exc:
            raise self._test_store_failure(exc) from exc
        return {'ok': True, 'suite': stored}

    def test_suite_update(self, body):
        """``POST /api/test/suites/update``: replace name/require_adm/... .

        Returns ``None`` for an unknown id (the endpoint's 404).  The member
        set changes through the script endpoints (add/move/delete); the one
        repair exception is **pruning a dangling reference** - a member whose
        script is already gone (no script endpoint can reach it any more, so
        the editor's row Delete prunes it).  Adding a script, or dropping a
        live one, stays refused.
        """
        fields = (body.get('suite') if isinstance(body.get('suite'), dict)
                  else body)
        current = self.test_suites.get(body.get('id'))
        if current is None:
            return None
        if isinstance(fields, dict) and 'scripts' in fields:
            old_ids = [entry['script_id'] for entry in current['scripts']]
            new = fields['scripts'] if isinstance(fields['scripts'], list) else []
            new_ids = [str((entry or {}).get('script_id') or '').strip().lower()
                       for entry in new if isinstance(entry, dict)]
            if set(new_ids) != set(old_ids):
                removed = set(old_ids) - set(new_ids)
                added = set(new_ids) - set(old_ids)
                if added or any(self.test_scripts.get(sid) is not None
                                for sid in removed):
                    raise ValueError(
                        'the member set changes through the script endpoints '
                        '(add/move/delete), not by editing the suite')
        error = suites_module.suite_refs_error(self.test_scripts, fields)
        if error:
            raise ValueError(error)
        try:
            return self.test_suites.update(body.get('id'), fields)
        except OSError as exc:
            raise self._test_store_failure(exc) from exc

    def test_suite_delete(self, body):
        """``POST /api/test/suites/delete``: the legacy ``{ok, removed}``.

        A suite that still holds scripts is refused (400): move or delete them
        first, so no script is left owned by a nonexistent suite.
        """
        current = self.test_suites.get(body.get('id'))
        if current is not None and current['scripts']:
            raise ValueError('the suite still holds %d script(s): move or '
                             'delete them first' % len(current['scripts']))
        try:
            removed = self.test_suites.remove(body.get('id'))
        except OSError as exc:
            raise self._test_store_failure(exc) from exc
        return {'ok': True, 'removed': bool(removed)}

    def test_suite_import(self, body):
        """``POST /api/test/suites/import``: the bundle (scripts + suites).

        Scripts are imported first (always ``merge``: a replace would wipe
        every other suite's scripts), the suite references are rewritten
        through the store's id map, a suite whose references stay unresolvable
        is skipped with a report line, and the ownership reconciliation runs
        afterwards - so ``mode: "replace"`` adopts the wiped suites' scripts
        into "Imported scripts".  A misspelled ``mode`` is a 400 (never a
        silent merge), a reference the bundle does not bring must not already
        belong to another suite (the one-owner invariant - the bundle's own
        scripts are the reconciliation's job), and a ``replace`` with nothing
        importable is refused instead of wiping the store.
        """
        suites_in = body.get('suites')
        if not isinstance(suites_in, list) or len(suites_in) > 500:
            raise ValueError('suites must be a list of at most 500 entries')
        scripts_in = body.get('scripts')
        if scripts_in is not None and (not isinstance(scripts_in, list)
                                       or len(scripts_in) > 1000):
            raise ValueError('scripts must be a list of at most 1000 entries')
        raw_mode = body.get('mode')
        if raw_mode is not None and raw_mode not in ('merge', 'replace'):
            raise ValueError("mode must be 'merge' or 'replace'")
        mode = raw_mode or 'merge'
        # The one-owner rule for the references the bundle does **not** bring:
        # a suite may not list a live script another suite owns.  The bundle's
        # own scripts are new in this store (their suite_id still names the
        # bundle's suites) - the member list is authoritative for them, so
        # they are the reconciliation's job, not a refusal.
        bundle_ids = {str(entry.get('id') or '').strip().lower()
                      for entry in (scripts_in or [])
                      if isinstance(entry, dict)}
        for suite in suites_in:
            if not isinstance(suite, dict):
                continue
            error = suites_module.suite_owners_error(
                self.test_scripts, self.test_suites, suite,
                ignore=bundle_ids)
            if error:
                raise ValueError(error)
        script_result = {'added': 0, 'skipped': 0, 'errors': [], 'id_map': {}}
        if scripts_in:
            try:
                script_result = self.test_scripts.import_scripts(scripts_in,
                                                                'merge')
            except OSError as exc:
                raise self._test_store_failure(exc) from exc
        id_map = script_result.get('id_map') or {}
        mapped, ref_errors = [], []
        for index, suite in enumerate(suites_in):
            if not isinstance(suite, dict):
                continue
            entry = copy.deepcopy(suite)
            for script_entry in entry.get('scripts') or []:
                if isinstance(script_entry, dict):
                    sid = str(script_entry.get('script_id') or '').strip().lower()
                    script_entry['script_id'] = id_map.get(sid, sid)
            missing = [script_entry['script_id']
                       for script_entry in (entry.get('scripts') or [])
                       if isinstance(script_entry, dict)
                       and not self.test_scripts.get(script_entry['script_id'])]
            if missing:
                ref_errors.append('%s: unknown script(s) %s'
                                  % (entry.get('name') or ('#' + str(index + 1)),
                                     ', '.join(missing[:3])))
                continue
            mapped.append(entry)
        if mode == 'replace' and not mapped:
            # A replace with nothing importable would silently wipe the store
            # (the suites were all skipped or every reference was missing).
            raise ValueError(
                'nothing to import: a replace would wipe the suite store'
                + (': ' + '; '.join(ref_errors[:3]) if ref_errors else ''))
        try:
            result = self.test_suites.import_suites(mapped, mode)
        except OSError as exc:
            raise self._test_store_failure(exc) from exc
        # The ownership invariant: the imported scripts carry the *old* suite
        # id, and a `replace` wiped the suites that owned the rest - the
        # reconciliation re-points the listed scripts and adopts the newly
        # unreferenced ones into "Imported scripts".
        suites_module.reconcile_scripts_to_suites(self.test_scripts,
                                                  self.test_suites)
        result['scripts'] = {'added': script_result.get('added', 0),
                             'skipped': script_result.get('skipped', 0)}
        result['skipped'] = result.get('skipped', 0) + len(ref_errors)
        result['errors'] = (result.get('errors', []) + ref_errors)[:10]
        result['ok'] = True
        return result

    def test_run(self, body):
        """``POST /api/test/run``: validate and start a run.

        The body carries either a **suite** (``suite_id`` - every member in
        order) or one script inline (``script``, the PWA's edited copy) or by
        ``script_id``, the optional ``preset_id`` (or an inline ``preset`` for
        API callers) and ``iterations`` for a load test.  Every precondition is
        checked before the card is touched: the busy guard (a run or a batch),
        the iteration count, the script validation, the preset resolution, the
        SCP80 keyset precondition and the ADM gate.  The ADM flag rides
        **inside** the script object (``require_adm``); a suite's ADM need is
        its own flag or any member's.

        Raises :class:`~simple_server.testrun.TestRunBusy` (409),
        :class:`~simple_server.testrun.TestRunNotFound` (404),
        :class:`~simple_server.testrun.TestAdmError` (400 + the ``adm``
        state) or a :class:`ValueError` (400); a worker that cannot start is
        a :class:`RuntimeError` (the handler's 500).
        """
        body = dict(body or {})
        runner = self.test_runner
        if runner.running or self.test_running:
            raise testrun.TestRunBusy('a test script is already running')
        if self.ram_batch_active():
            raise testrun.TestRunBusy('a batch install is running - stop it '
                                      'first')
        iterations = testrun.load_iterations(body)
        suite_id = str(body.get('suite_id') or '').strip().lower()
        if suite_id:
            return self._test_run_suite(suite_id, body, iterations)
        script_raw, script_id, error = self._run_script_from_body(body)
        if error:
            raise testrun.TestRunNotFound(error)
        try:
            script = testscript.normalise_script(script_raw,
                                                 testrun.command_type)
        except testscript.ScriptError as exc:
            raise testrun.TestRunError(str(exc))
        preset, preset_error = self._run_preset_from_body(body)
        if preset_error:
            raise testrun.TestRunError(preset_error)
        needs_scp80 = any(step['type'] == 'action'
                          and step['kind'] == 'scp80'
                          for step in script['steps'])
        if needs_scp80:
            error = testrun.preset_error(script, preset)
            if error:
                raise testrun.TestRunError(error)
        ok, adm_info, adm_error = runner.ensure_adm(
            preset, bool((script_raw or {}).get('require_adm')))
        if not ok:
            raise testrun.TestAdmError(adm_error, adm_info)
        try:
            runner.start(script, preset, script_id or None, adm_info,
                         iterations)
        except Exception as exc:
            # A worker that cannot start is a server failure (the legacy's
            # 500), not a refused request.
            raise RuntimeError(
                'could not start the test run: %s' % exc) from exc
        return runner.snapshot()

    def _test_run_suite(self, suite_id, body, iterations=1):
        """Start a **suite** run: the members, the preset, the ADM gate, then
        hand over to the suite runner (one card session, no resume).

        The suite's ordered member list is resolved and every script
        re-validated here (a hand edit is reported before anything is sent),
        the SCP80 precondition covers **all** members' steps, the optional
        ``start_reset`` runs the physical reset + full re-initialization
        *after* those checks (a run that cannot start must not reset the card)
        and *before* the ADM gate (the session changes, so the ADM is
        re-verified for the new one), and the post-reset ICCID must still
        match the run's preset.
        """
        suite = self.test_suites.get(suite_id)
        if suite is None:
            raise testrun.TestRunNotFound('unknown test suite id')
        if not suite['scripts']:
            raise testrun.TestRunError('the suite has no scripts')
        members = []
        missing = []
        for entry in suite['scripts']:
            stored = self.test_scripts.get(entry['script_id'])
            if stored is None:
                missing.append(entry['script_id'])
                continue
            try:
                # the store validated at save; a hand edit (or a tightened
                # rule) is reported here, before anything is sent
                script = testscript.normalise_script(stored,
                                                     testrun.command_type)
            except testscript.ScriptError as exc:
                raise testrun.TestRunError(
                    'script %r does not validate: %s'
                    % (stored.get('name') or stored['id'], exc))
            script['require_adm'] = bool(stored.get('require_adm'))
            members.append((entry, script))
        if missing:
            raise testrun.TestRunError(
                'the suite references unknown test script(s): %s'
                % ', '.join(missing[:3]))
        preset, preset_error = self._run_preset_from_body(body)
        if preset_error:
            raise testrun.TestRunError(preset_error)
        steps = [step for _entry, script in members for step in script['steps']]
        if any(step['type'] == 'action' and step['kind'] == 'scp80'
               for step in steps):
            error = testrun.preset_error({'steps': steps}, preset)
            if error:
                raise testrun.TestRunError(error)
        reset_info = None
        if suite.get('start_reset'):
            # A suite may start with a card reset: the physical reset + the
            # full re-initialization, so its scripts run against a freshly
            # initialized card.  This runs after the preset checks - a run
            # that cannot start must not reset the card - and before the ADM
            # gate: the card session changes, so the ADM latch is re-verified
            # against the post-reset card, and the ICCID must still match the
            # run's preset (a reset can switch the active profile on an
            # eUICC).
            if not self.connected or self.cat is None:
                raise testrun.TestRunError('start_reset: no card is equipped')
            started = time.time()
            try:
                self.reset()
            except Exception as exc:
                raise RuntimeError(
                    'start_reset: the card re-initialization failed: %s'
                    % exc) from exc
            elapsed_ms = int((time.time() - started) * 1000)
            reset_info = {'ok': True, 'iccid': self.iccid, 'ms': elapsed_ms}
            log.info('SUITE-RESET: re-initialized in %dms, ICCID %s',
                     elapsed_ms, self.iccid or '(unreadable)')
            want = presets_store.normalize_iccid((preset or {}).get('iccid'))
            got = presets_store.normalize_iccid(self.iccid)
            if want and got and want != got:
                raise testrun.TestRunError(
                    'start_reset: the card reports ICCID %s after the reset, '
                    'but the run preset is for %s - re-select the preset and '
                    'retry' % (self.iccid, (preset or {}).get('iccid')))
        needed_adm = bool(suite.get('require_adm')) or any(
            bool(script.get('require_adm')) for _entry, script in members)
        ok, adm_info, adm_error = self.test_runner.ensure_adm(preset,
                                                              needed_adm)
        if not ok:
            raise testrun.TestAdmError(adm_error, adm_info)
        preset_id = str(body.get('preset_id') or '') or None
        try:
            self.test_runner.start_suite(suite, members, preset, preset_id,
                                         adm_info, reset_info, iterations)
        except Exception as exc:
            raise RuntimeError(
                'could not start the suite run: %s' % exc) from exc
        return self.test_runner.snapshot()

    def _run_script_from_body(self, body):
        """The run's script: the inline ``script`` or the stored ``script_id``.

        Returns ``(script_raw, script_id, error)``; ``error`` is ``None`` on
        success.  A stored script is served as ``{name, steps, require_adm}``
        - an entry that no longer validates is served exactly like any other
        and fails the run's validation (the store never drops it).
        """
        script_raw = body.get('script')
        script_id = str(body.get('script_id') or '').strip()
        if isinstance(script_raw, dict):
            return script_raw, script_id or None, None
        if not script_id:
            return None, None, None
        store = getattr(self, 'test_scripts', None)
        stored = store.get(script_id) if store else None
        if stored is None:
            return None, None, 'test script %s not found' % script_id
        return ({'name': stored['name'], 'steps': stored['steps'],
                 'require_adm': bool(stored.get('require_adm'))},
                stored['id'], None)

    def _run_preset_from_body(self, body):
        """The run's preset: the inline ``preset`` body or the stored id.

        Returns ``(preset, error)``; ``error`` is ``None`` on success.  The
        PWA sends ``preset_id``, so a run always uses the store's
        authoritative keysets, TARs and counters; the inline form stays for
        external API callers.
        """
        preset = body.get('preset') or {}
        if preset:
            return preset, None
        preset_id = str(body.get('preset_id') or '').strip()
        if not preset_id:
            return {}, None
        preset = self.presets.get(preset_id)
        if preset is None:
            return None, 'card preset %s not found' % preset_id
        return preset, None

    def test_status(self):
        """``GET /api/test/status``: the current (or last) run, card-free."""
        return self.test_runner.snapshot()

    def test_stop(self):
        """``POST /api/test/stop``: request a stop, answer the run state."""
        return self.test_runner.stop()

    def test_clear(self):
        """``POST /api/test/clear``: drop a finished report (``False``: 409)."""
        return self.test_runner.clear()

    def test_request_blocked(self, path):
        """True when ``path`` must answer 409 because a run owns the card."""
        return testrun.request_blocked(path, self.test_running)

    # ---- the CAT/Simulator endpoints (3c-3e-1) ----------------------------
    #
    # The Phone/Simulator tab's whole surface: the STK menu browser (the
    # cached SET UP MENU, the ENVELOPE(Menu Selection), the paused command's
    # TERMINAL RESPONSE and the watchdog), the event downloads, the runtime
    # TERMINAL PROFILE (Send / Rescue), the manual STATUS poll and the poll
    # switch, the proactive log, the PLI dictionary and the raw APDU.
    #
    # The cached state lives on :class:`~simple_server.cat_session.CatSession`
    # (``sim_menu``, ``event_list``, ``stk_pending``, ``menu_active``, the
    # log, the PLI dictionary); the operator-owned ones (the PLI overrides)
    # sit on this state so they survive an equip.  The card-free GETs read the
    # cached state without the card lock (the legacy allowlist - the PWA polls
    # them every 2 s while a long operation holds the card).

    def _require_cat(self):
        """The equipped card's CAT session, or :class:`NoCardSessionError`."""
        cat = self.cat
        if cat is None or not self.connected:
            raise NoCardSessionError('no card session: equip a card first')
        return cat

    def _require_card(self):
        """The equipped card's transport, or :class:`NoCardSessionError`."""
        if not self.connected or self.card is None or self.transport is None:
            raise NoCardSessionError('no card session: equip a card first')
        return self.transport

    def _bip_owns_card(self):
        """True while a BIP session runs (its terminal answers its commands)."""
        bip = self.bip
        return bool(bip is not None and getattr(bip, 'owner', None))

    def menu_info(self):
        """``GET /api/menu``: the cached SET UP MENU plus ``active``.

        The legacy shape: the menu dict as parsed (``command_number``,
        ``title``, ``items`` - the NAI suffixes included) or ``{'items': []}``
        when the card sent none.  TS 102 223 §6.6.7 marks the **Alpha
        identifier mandatory**, but a card that omits it must still render
        (guarded fallback): the menu then carries no ``title`` key and the PWA
        supplies its translated label - the server never fabricates an English
        ``'Menu'``.
        """
        cat = self.cat
        menu = dict((cat.sim_menu if cat is not None else None) or {})
        if not menu:
            menu = {'items': []}
        menu['active'] = bool(cat is not None and cat.menu_active)
        return menu

    def menu_select(self, body):
        """``POST /api/menu-select``: ENVELOPE(Menu Selection) + the chain.

        The item is named by ``item_id`` or by ``item_name``/``text``
        (``mode`` ``exact``/``contains``, case-insensitive unless
        ``case_sensitive`` is set; both are validated - an unknown ``mode`` is
        a 400, it is never silently read as ``contains``) resolved against the
        cached menu before anything touches the card.  A paused command is
        answered first
        (:meth:`CatSession.finish_pending_menu` - a new selection must never
        shadow a FETCHed command), then ``D3 07 02 02 01 81 90 01 <id>`` goes
        out as the ENVELOPE and the answers are consumed: a user-interactive
        command pauses the chain into ``stk_pending`` (the response carries its
        prompt/items and the watchdog is armed), an idle card is polled once
        (a command may be queued after the envelope's ``9000``).

        Returns the legacy response dict: ``{'type', 'sw', ...}`` where
        ``type``/``items``/``text`` describe the paused command (``done``
        when none).
        """
        cat = self._require_cat()
        item_id = body.get('item_id')
        if item_id in (None, ''):
            text = body.get('item_name')
            if text in (None, ''):
                text = body.get('text')
            if text in (None, ''):
                raise ValueError('item_id or item_name is required')
            item_id = resolve_menu_item(cat.sim_menu, str(text),
                                        body.get('mode'),
                                        body.get('case_sensitive'))
        else:
            try:
                item_id = int(item_id)
            except (TypeError, ValueError):
                raise ValueError('item_id must be an integer: %r'
                                 % (body.get('item_id'),))
            if not 0 <= item_id <= 0xFF:
                raise ValueError('item_id must be one octet: %r' % (item_id,))
        cat.finish_pending_menu()
        cat.menu_active = True
        tlv = bytes([0xD3, 0x07, 0x02, 0x02, 0x01, 0x81, 0x90, 0x01,
                     item_id])
        apdu = cat_module.envelope_apdu(tlv, int(cat.cat_cla, 16))
        _data, sw = cat.transport.send_apdu(apdu)
        sw = bytes(sw or b'').hex().upper()
        log.info('MENU-SELECT: ENVELOPE %s item=%d -> %s', apdu.hex().upper(),
                 item_id, sw)
        response = {'type': 'done', 'sw': sw}
        on_fetch = cat.menu_fetch_handler(response)
        if sw.startswith('91'):
            cat.handle_chain(sw, on_fetch)
        else:
            cat.menu_active = False
            cat.stk_pending = None
            if sw == '9000':
                _data, st_sw = cat.send_status()
                log.debug('STATUS poll (menu-select 9000) -> %s',
                          bytes(st_sw or b'').hex().upper())
                if bytes(st_sw or b'').hex().upper().startswith('91'):
                    cat.handle_chain(st_sw, on_fetch)
        if cat.stk_pending:
            cat.arm_menu_timeout()
        else:
            cat.cancel_menu_timeout()
        return response

    def menu_respond(self, body):
        """``POST /api/menu-respond``: the paused command's TERMINAL RESPONSE.

        ``result`` is ``ok``/``cancel``/``back``/``timeout``/``help``
        (TS 102 223 §8.12.0), ``item_id`` the SELECT ITEM/SET UP MENU choice
        (one octet, like ``/api/menu-select`` - a larger value is a 400, not a
        silent truncation, so the report never names a different item) and
        ``text`` the GET INKEY/GET INPUT answer (validated against the
        command's Response Length/charset, then coded per §6.8.5).  The chain
        continues on ``91XX``; the watchdog follows the new pending state.
        """
        cat = self._require_cat()
        item_id = body.get('item_id')
        if item_id not in (None, ''):
            if isinstance(item_id, bool):
                raise ValueError('item_id must be an integer: %r' % (item_id,))
            try:
                item_id = int(item_id)
            except (TypeError, ValueError):
                raise ValueError('item_id must be an integer: %r'
                                 % (body.get('item_id'),))
            if not 0 <= item_id <= 0xFF:
                raise ValueError('item_id must be one octet: %r' % (item_id,))
        else:
            item_id = None
        response = cat.menu_send_response(body.get('result') or 'ok', item_id,
                                          body.get('text'))
        # the API's status words are upper-case (cat_session's own response is
        # the transport's hex)
        response['sw'] = str(response.get('sw') or '').upper()
        return response

    def stk_status(self):
        """``GET /api/stk-status``: active dialogue + the pending command.

        The legacy shape: ``active``/``pending``/``pending_type``, plus
        ``pending_data`` - the paused command's JSON-safe dict verbatim (the
        PWA renders the prompt/items); the watchdog's ``shown_at`` monotonic
        stamp is stripped.
        """
        cat = self.cat
        pending = cat.stk_pending if cat is not None else None
        resp = {'active': bool(cat is not None and cat.menu_active),
                'pending': pending is not None,
                'pending_type': pending['type'] if pending else None}
        if pending:
            resp['pending_data'] = {key: value for key, value in pending.items()
                                    if key != 'shown_at'}
        return resp

    def events_info(self):
        """``GET /api/events``: the subscribed event bytes, or ``[]``.

        The SET UP EVENT LIST the card sent (TS 102 223 §6.4.16); the values
        are ints (the legacy shape the PWA renders as hex).
        """
        cat = self.cat
        return list(cat.event_list or []) if cat is not None else []

    def event_send(self, body):
        """``POST /api/event-send``: ENVELOPE(Event Download) and its answers.

        ``event_type`` is required (an int byte, a decimal/hex string or an
        event name - see :func:`event_code`).  The event objects come from
        ``fields`` - the semantic builders of :mod:`simple_server.shared.events`
        for the four events it models (0x03/0x0B/0x12/0x1D), validated by the
        builder - or from the raw ``event_data`` hex (every other event form);
        when both are present **``fields`` wins** (``event_data`` is ignored).
        ``event_src`` overrides the device-identities source (``82`` terminal
        default, ``83`` network - TS 102 223 §8.7).

        The ``91XX`` answer is drained (the generic chain answers whatever
        the event raised).  The Poll Interval Negotiation event's answer
        (§7.5.22/§8.97) is decoded into ``negotiation`` and a *modified*
        result becomes the poll interval (§6.4.6/§6.4.14: a new POLL INTERVAL
        clears the card's POLLING OFF).

        Returns ``{'sw', ['data'], ['net_state'], ['negotiation']}``.  A
        **Location status** event patches the network-state monitor (it sets
        the simulated service state and re-reads EF.IMSI - a multi-IMSI applet
        may switch it after the event); the monitor is attached as
        ``net_state`` when the session has one.  Every other event leaves it
        alone - no extra SELECT/READ, no ``source`` flip (the legacy gate).
        """
        cat = self._require_cat()
        code = event_code(body.get('event_type'))
        fields = body.get('fields')
        if fields is not None:
            # the semantic builders validate the fields (an EventError is a
            # ValueError, so the dispatch answers 400)
            event_data_hex = events.build(code, fields)
        else:
            event_data_hex = str(body.get('event_data') or '').strip()
            if event_data_hex:
                try:
                    bytes.fromhex(event_data_hex)
                except ValueError:
                    raise ValueError('event_data is not valid hex: %r'
                                     % (event_data_hex,))
        data, sw = cat.send_event_download(
            code, bytes.fromhex(event_data_hex) if event_data_hex else None,
            src=body.get('event_src'))
        sw_hex = bytes(sw or b'').hex().upper()
        data_hex = bytes(data or b'').hex().upper()
        log.info('ENVELOPE(Event Download) type=0x%02X data=%s -> %s',
                 code, event_data_hex or '(none)', sw_hex)
        # Only a Location status event changes the simulated service state; a
        # multi-IMSI applet may switch EF.IMSI after it (the legacy patch).
        # Every other event must issue no read at all: the monitor is not the
        # event's business, and an unrelated download must not flip a file's
        # ``source`` to ``read``.
        if code == netsim_module.EVENT_LOCATION_STATUS:
            self._netstate_after_event(
                code, bytes.fromhex(event_data_hex) if event_data_hex else b'')
        resp = {'sw': sw_hex}
        if data_hex:
            resp['data'] = data_hex
        if self.netstate is not None:
            resp['net_state'] = self.netstate
        if code == 0x1C:
            negotiation = proactive.decode_poll_negotiation(data_hex)
            if negotiation:
                resp['negotiation'] = negotiation
                self._apply_poll_negotiation(negotiation)
        return resp

    def _apply_poll_negotiation(self, negotiation):
        """Act on a poll-interval negotiation answer (TS 102 223 §7.5.22).

        A *modified* result carries the duration the terminal shall use from
        now on (§8.97); the card's POLLING OFF wish is then over (a new POLL
        INTERVAL is exactly what §6.4.14 says resumes polling).
        """
        if not negotiation or negotiation.get('result') != 2:
            return
        seconds = negotiation.get('seconds')
        if not seconds:
            return
        interval = max(1, min(255, int(seconds)))
        cat = self.cat
        with self.card_lock:
            self.poll_interval = interval
            self.poll_suspended = False
            if cat is not None:
                cat.poll_interval = interval
                cat.poll_suspended = False
        log.info('POLL NEGOTIATION: modified -> background poll %d s',
                 interval)

    def terminal_profile_info(self):
        """``GET /api/terminal-profile``: the profile in effect + the startup one."""
        profile = self.terminal_profile or ''
        return {'profile': profile or None,
                'bytes': len(profile) // 2,
                'cli_default': self.default_terminal_profile or None}

    def set_terminal_profile(self, body):
        """``POST /api/terminal-profile``: set/send the profile at runtime.

        A body with ``profile`` validates the hex and replaces the in-memory
        value; an empty body (the PWA's Send) re-sends the current one.  The
        re-send answers a paused command first
        (:meth:`CatSession.resend_terminal_profile` - re-sending must never
        shadow a FETCHed command), keeps the proactive log with a boundary
        marker and clears the per-session menu/event state.
        """
        cat = self._require_cat()
        if body.get('profile') is not None:
            profile, error = validate_terminal_profile(body.get('profile'))
            if error:
                raise ValueError(error)
            self.terminal_profile = profile
        if not self.terminal_profile:
            raise ValueError('no terminal profile configured')
        result = cat.resend_terminal_profile(self.terminal_profile)
        self.poll_suspended = bool(getattr(cat, 'poll_suspended', False))
        self._adopt_poll_interval()
        return dict({'ok': True, 'profile': self.terminal_profile}, **result)

    def rescue(self):
        """``POST /api/rescue``: re-send the current profile (a CAT recovery)."""
        return self.set_terminal_profile({})

    def status_poll(self):
        """``POST /api/status-poll``: STATUS and, on ``91XX``, the chain.

        The manual poll is bounded like the background one
        (:data:`POLL_CHAIN_MAX`) - a card that re-queues a command must not
        hold the card lock - and runs whatever the operator's poll switch
        says: the button is an explicit action, and the card's POLLING OFF
        wish suspends only the background poll (TS 102 223 §6.4.14).
        """
        cat = self._require_cat()
        _data, sw = cat.send_status()
        sw_hex = bytes(sw or b'').hex().upper()
        log.info('STATUS poll (manual) -> %s', sw_hex)
        resp = {'sw': sw_hex}
        if sw_hex.startswith('91'):
            cat.handle_chain(sw, max_rounds=POLL_CHAIN_MAX)
            resp['proactive'] = True
        self.poll_suspended = bool(getattr(cat, 'poll_suspended', False))
        self._adopt_poll_interval()
        return resp

    def _poll_state(self):
        """The effective poll state (the legacy ``/api/poll-status`` body)."""
        cat = self.cat
        suspended = (bool(getattr(cat, 'poll_suspended', False))
                     if cat is not None else bool(self.poll_suspended))
        return {'enabled': bool(self.poll_enabled),
                'interval': int(self.poll_interval),
                'card_disabled': suspended}

    def poll_status(self):
        """``GET /api/poll-status``: the operator's switch and the card's wish.

        ``interval`` is the adopted POLL INTERVAL (TS 102 223 §6.4.6), not
        the startup default; ``card_disabled`` is the card's POLLING OFF
        (§6.4.14) - the PWA shows the effective state (OFF while suspended).
        """
        return self._poll_state()

    def poll_toggle(self, body):
        """``POST /api/poll-toggle``: turn the background STATUS poll on/off.

        Enabling is refused (stays off) when no interval is configured -
        ``--poll-interval 0`` is the operator's "no polling" setup and the
        toggle must not override it (the legacy ``_poll_enable`` rule).  The
        card's POLLING OFF is reported as a warning while polling is on: the
        timer stays suspended until the card sends a POLL INTERVAL.
        """
        enabled = bool(body.get('enabled'))
        if enabled and self.poll_interval <= 0:
            enabled = False
        self.poll_enabled = enabled
        resp = self._poll_state()
        if resp['enabled'] and resp['card_disabled']:
            resp['warning'] = ('card disabled proactive polling with POLLING '
                               'OFF (TS 102 223 6.4.14); it resumes after a '
                               'POLL INTERVAL')
        return resp

    def proactive_log(self):
        """``GET /api/proactive-log``: the last 50 fetched commands, newest first.

        The entries are :meth:`CatSession.entries`' (the legacy shape the PWA
        renders: ``id``, ``type_hex``/``type_name``, ``elapsed``, ``bytes``,
        ``raw``, the decoded ``cmd_decoded`` and the sent TERMINAL RESPONSE's
        fields); a TERMINAL PROFILE re-send's boundary marker is in there too.
        """
        cat = self.cat
        return cat.entries(50) if cat is not None else []

    def pli_qualifiers(self):
        """``GET /api/pli-qualifiers``: the PLI qualifier codes with their names."""
        return [{'code': '%02X' % code, 'name': proactive.PLI_QUALIFIER_NAMES[code]}
                for code in proactive.PLI_QUALIFIER_NAMES]

    def _pli_effective(self):
        """The effective PLI dictionary: the override, else the built-in default.

        The legacy GET shape: one entry per known qualifier, code-keyed hex; a
        cleared override falls back to :data:`~simple_server.cat_session.
        PLI_DEFAULTS`, and the date/time qualifier stays empty (the host clock
        answers it at TERMINAL RESPONSE time, TS 102 223 §6.4.15).
        """
        overrides = self.pli_data
        return {('%02X' % code): (overrides.get(code)
                                  or cat_session.PLI_DEFAULTS.get(code, ''))
                for code in proactive.PLI_QUALIFIER_NAMES}

    def pli_dict(self):
        """``GET /api/pli-dict``: the effective PLI data dictionary."""
        return self._pli_effective()

    def set_pli_dict(self, body):
        """``POST /api/pli-dict``: update the PLI overrides.

        A non-empty valid-hex value becomes an override; an empty value clears
        it (the default applies again).  A field the server cannot use is
        named in the response instead of being silently dropped (v3.25.6):
        ``invalid`` (a non-string value, a malformed key, a non-hex or
        odd-length value) and ``unknown`` (a well-formed code outside the
        qualifier list).  The guard tests the known qualifiers, never "an
        override exists", so a new override always sticks and a clear cannot
        poison a later set (v3.25.5).
        """
        invalid, unknown = [], []
        for key, value in body.items():
            if not isinstance(value, str):
                invalid.append(key)
                continue
            try:
                code = int(key, 16)
            except (TypeError, ValueError):
                invalid.append(key)
                continue
            if code not in proactive.PLI_QUALIFIER_NAMES:
                unknown.append(key)
                continue
            if value:
                try:
                    bytes.fromhex(value)
                except ValueError:
                    invalid.append(key)
                    continue
                self.pli_data[code] = value
            else:
                self.pli_data.pop(code, None)
        resp = self._pli_effective()
        if invalid:
            resp['invalid'] = invalid
        if unknown:
            resp['unknown'] = unknown
        return resp

    def raw_apdu(self, body):
        """``POST /api/apdu``: send one raw APDU; ``{response, sw}``.

        The APDU is sent exactly as given through the transport (the T=0
        ``61xx``/``6Cxx`` chaining is the transport's).  A ``91XX`` answer is
        **not** passed through to the caller: the card announced a proactive
        command, so the terminal FETCHes and answers it with the full chain
        (the PLI dictionary/host clock, BIP, the timers, the display/menu
        generic answers) and reports ``9000`` - the legacy
        ``send_apdu_checksw`` rule, so a card that queues a command as a side
        effect is never left with an unanswered FETCH.

        The auto-handler is off only when something else owns the card: a
        test run (the endpoint answers 409 before it gets here) or a running
        BIP session (its terminal answers its own commands - the chain would
        shortcut it), where the ``91XX`` status is returned as-is.
        """
        apdu_hex = re.sub(r'\s', '', str(body.get('apdu') or '')).upper()
        if not apdu_hex:
            raise ValueError('apdu is required')
        if not re.fullmatch(r'[0-9A-F]+', apdu_hex) or len(apdu_hex) % 2:
            raise ValueError('apdu is not valid hex: %r' % (body.get('apdu'),))
        apdu = bytes.fromhex(apdu_hex)
        if len(apdu) < 4:
            raise ValueError('an APDU needs at least 4 bytes: %r'
                             % (apdu_hex,))
        cat = self._require_cat()
        data, sw = cat.transport.send_apdu(apdu)
        sw_hex = bytes(sw or b'').hex().upper()
        log.info('APDU: %s -> SW: %s', apdu_hex, sw_hex)
        if sw_hex.startswith('91') and not self._bip_owns_card():
            cat.handle_chain(bytes(sw))
            sw_hex = '9000'
        return {'response': bytes(data or b'').hex().upper(), 'sw': sw_hex}

    # ---- the eSIM / LPA endpoints (3c-3e-2a) ------------------------------
    #
    # The ES10 layer lives in :mod:`simple_server.esim` (the TLV objects, the
    # transport functions and the card-level flow); this state owns the ISD-R
    # selection, the launch of the REFRESH chain on the CAT session, the
    # equip-like re-initialization after a switch and the resulting profile
    # state check.  The endpoints are card-locked like the legacy's (each one
    # selects the ISD-R and transceives), so they are not in
    # ``CARD_FREE_GET``.
    #
    # Ported from the legacy handlers: `GET /api/esim/chip|profiles|
    # notifications` and `POST /api/esim/profile` (`_esim_refresh_chain`,
    # `_esim_reinit`, `_esim_verify_switch`, `_finish_pending_menu`).

    def esim_chip(self):
        """``GET /api/esim/chip``: EID, EUICCInfo1/2, addresses and the RAT."""
        transport = self._require_card()
        with esim_module.isdr_selected(self._file_ops()):
            return esim_module.chip_info(transport)

    def esim_profiles(self):
        """``GET /api/esim/profiles``: GetProfilesInfo with the metadata."""
        transport = self._require_card()
        with esim_module.isdr_selected(self._file_ops()):
            return esim_module.profiles_info(transport)

    def esim_notifications(self):
        """``GET /api/esim/notifications``: ListNotification (read-only)."""
        transport = self._require_card()
        with esim_module.isdr_selected(self._file_ops()):
            return esim_module.notifications_info(transport)

    def _esim_refresh_chain(self, sw):
        """Answer the proactive command(s) that accompany a profile switch.

        With the refresh flag set the card sends REFRESH (SGP.22 v2.6 §3.2.1
        step 7 for Enable, §3.2.2 step 6 for Disable); it is answered and the
        chain stops - the card reset that follows performs the switch (§3.2.1
        step 8 / §3.2.2 step 7).  **No STATUS poll** is sent: the card is
        mid-switch and refuses further commands (``6985``) until the reset.
        Returns True when a REFRESH was answered.
        """
        cat = self._require_cat()
        seen = {'refresh': False}

        def on_fetch(raw, cmd_num, cmd_type, dev_src, dev_dst):
            if cmd_type == 0x01:                # REFRESH
                seen['refresh'] = True
                return 'exit'
            return None

        cat.handle_chain(sw, on_fetch=on_fetch, status_poll=False,
                         max_rounds=POLL_CHAIN_MAX)
        return seen['refresh']

    def esim_profile_switch(self, body):
        """``POST /api/esim/profile``: Enable/DisableProfile + re-init + check.

        The STORE DATA's ``91XX`` is the spec's "result OK before REFRESH"
        (SGP.22 v2.6 §3.2.1 step 6 for Enable, §3.2.2 step 5 for Disable); the
        chain answers the REFRESH (§3.2.1 step 7 / §3.2.2 step 6), and the card
        is then reset and fully re-initialized (the switch completes on the
        reset - §3.2.1 step 8 / §3.2.2 step 7).  The new profile list is
        re-read and the requested state reported as ``verified``/
        ``state_after``; a failed post-switch verification degrades to
        ``None``/``None`` - the switch itself already happened and must never
        be reported as a request error.  The request is validated before the
        card is touched; a switch failure is a result (``ok: false``), never an
        exception.

        ``refresh`` defaults to **true**, the legacy's choice; a card may
        refuse ``refreshFlag`` TRUE (the Phase 3a bench probe got an undefined
        ``enableResult`` 7 and accepted FALSE; the 4.0.0 live run accepted
        TRUE), so a client that resets the card itself may prefer
        ``refresh: false``.  This endpoint always re-initializes the card after
        the switch, so the flag only decides whether the card also sends its
        REFRESH.
        """
        action = str(body.get('action') or '').strip().lower()
        iccid = body.get('iccid')
        isdp_aid = body.get('isdp_aid')
        refresh = _boolean_flag(body.get('refresh', True), 'refresh')
        # Validate the request before the card is touched: an incomplete
        # one must not finish a pending menu or start a STORE DATA.
        request = esim_module.switch_request(
            action, iccid, isdp_aid, refresh)
        cat = self._require_cat()
        transport = self._require_card()
        cat.finish_pending_menu()
        cursor = getattr(cat, '_entry_id', 0)
        with esim_module.isdr_selected(self._file_ops()):
            response = esim_module.switch_profile(
                transport, action, iccid=iccid, isdp_aid=isdp_aid,
                refresh=refresh, run_chain=self._esim_refresh_chain,
                request=request)
        # A REFRESH delivered outside the chain still means the card wants the
        # terminal to re-initialize (the legacy safety net over the log).
        response['refresh_seen'] = bool(response.get('refresh_seen')) or any(
            entry.get('type_hex') == '01' and entry.get('id', 0) > cursor
            for entry in cat.log)
        response['reinitialized'] = False
        response['verified'] = None
        response['state_after'] = None
        if response['ok'] or response['refresh_seen']:
            try:
                self.reset()
            except Exception as exc:
                log.warning('ESIM: re-initialization after the profile switch '
                            'failed: %s', exc)
            else:
                response['reinitialized'] = True
                try:
                    with esim_module.isdr_selected(self._file_ops()):
                        response.update(esim_module.verify_switch(
                            self.transport, action, iccid=iccid,
                            isdp_aid=isdp_aid))
                except Exception as exc:
                    # The switch already happened (and was destructive): a
                    # failed verification - the ISD-R refused the selection
                    # after the reset, a disabled active profile - must not
                    # turn it into a 400.  The result stays "not verified".
                    log.warning('ESIM: post-switch verification failed: %s', exc)
                    response['verified'] = None
                    response['state_after'] = None
        response['iccid'] = self.iccid
        response['card_session'] = self.card_session
        return response

    # ---- the network simulation and the state monitor (3c-3e-2a) ----------

    def _netstate_read(self, keys=None):
        """Read the monitored network-state EFs (best effort per file)."""
        return netmon.read_files(self._file_ops(), self.files, keys)

    def _netstate_compute(self):
        """Derive the monitor header (service/location/roaming) in place."""
        if self.netstate is None:
            return None
        return netmon.compute(self.netstate,
                              netmon.load_mcc_mnc(self.mcc_mnc_path))

    def _netstate_install(self, files, source='init'):
        """Install a freshly read monitor state (``None``/``{}`` = nothing)."""
        state = netstate.new_state()
        netstate.merge_read(state, files or {}, source=source)
        netstate.set_read_time(state)
        self.netstate = state
        self._netstate_compute()

    def _netstate_init(self):
        """Create the monitor state from a fresh read (best effort)."""
        try:
            self._netstate_install(self._netstate_read())
        except Exception as exc:
            self.netstate = None
            log.warning('network state read failed: %s', exc)

    def _netstate_ensure(self):
        """The monitor state of this session, created on demand.

        The equip path creates it after a readable ICCID; this covers a session
        that got past that (a card whose ICCID read failed at equip) when a
        network operation needs the state.
        """
        if self.netstate is None and self.iccid:
            self._netstate_init()
        return self.netstate

    def _netstate_after_net_sim(self, scenario, result):
        """Update the cached monitor after a scenario run.

        The bytes we wrote are known without re-reading (``apply_steps``); the
        card may also switch EF.IMSI itself, so that file is re-read.
        """
        state = self._netstate_ensure()
        if state is None:
            return
        netstate.apply_steps(state, (result or {}).get('steps') or [])
        service = netsim_module.SCENARIO_SERVICE.get(scenario)
        if service and (result or {}).get('success'):
            netstate.set_service(state, service, 'net-sim:' + scenario)
        netstate.merge_read(state, self._netstate_read(['imsi']),
                            source='read')
        self._netstate_compute()

    def _netstate_after_event(self, event_type, event_data):
        """Patch the monitor after a **Location status** event download.

        The endpoint gates the call on the event type (the legacy
        ``is_location`` rule) - this method is the Location-status patch only.
        That event changes the *simulated* service state (the card's own
        Location Information object is not the monitor's source); a multi-IMSI
        applet may also switch EF.IMSI after such an event, so that file is
        re-read.
        """
        state = self._netstate_ensure()
        if state is None:
            return
        try:
            code = int(event_type)
        except (TypeError, ValueError):
            code = None
        if code == netsim_module.EVENT_LOCATION_STATUS and event_data:
            status = None
            if (len(event_data) >= 3 and event_data[0] == 0x9B
                    and event_data[1] == 0x01):
                status = event_data[2]
            service = {0x00: netstate.SERVICE_NORMAL,
                       0x01: netstate.SERVICE_LIMITED,
                       0x02: netstate.SERVICE_NONE}.get(status)
            if service:
                netstate.set_service(state, service,
                                     'event:location-status')
        netstate.merge_read(state, self._netstate_read(['imsi']),
                            source='read')
        self._netstate_compute()

    def net_sim(self, body):
        """``POST /api/net-sim``: run one scenario under the card lock.

        The scenario builders/step log are :mod:`simple_server.shared.netsim`
        (reused); :mod:`simple_server.netsim` supplies the card adapter.  The
        response is the legacy ``{scenario, success, error, steps, net_state}``
        - ``net_state`` is the monitor patched from the writes.
        """
        self._require_card()
        self._require_cat()
        scenario = body.get('scenario')
        if not scenario:
            raise ValueError('scenario is required')
        result = netsim_module.run_scenario(self, scenario, body)
        self._netstate_after_net_sim(scenario, result)
        result['net_state'] = self.netstate
        return result

    def net_state_info(self):
        """``GET /api/net-state``: the cached monitor (no card I/O)."""
        self._netstate_compute()
        if self.netstate is None:
            return {'available': False, 'state': None}
        return {'available': True, 'state': self.netstate}

    def net_state_refresh(self, body):
        """``POST /api/net-state-refresh``: re-read the monitored files.

        ``files`` optionally filters the definition keys (the PWA refreshes
        EF.IMSI only after an event, everything on demand).  The state is
        created when the session has none (an equip path that predates it).
        """
        self._require_card()
        keys = body.get('files')
        if keys is not None and (not isinstance(keys, list)
                                 or not all(isinstance(key, str)
                                            for key in keys)):
            raise ValueError('files must be a list of file keys')
        state = self.netstate
        if state is None:
            state = netstate.new_state()
            self.netstate = state
        files = self._netstate_read(keys)
        netstate.merge_read(state, files, source='refresh')
        netstate.set_read_time(state)
        self._netstate_compute()
        return {'available': True, 'state': state}

    def mcc_mnc_info(self, query=''):
        """``GET /api/mcc-mnc``: the bundled operator list search / pick.

        ``query`` is the raw query string (``q=``, ``random=1``,
        ``exclude=<mcc><mnc>``).  MVNO entries stay hidden from both the
        search and the random pick (they do not operate their own network);
        ``available: false`` when the list is not deployed.
        """
        params = parse_qs(query or '', keep_blank_values=True)

        def value(name):
            entries = params.get(name)
            return entries[-1] if entries else ''

        data = netmon.load_mcc_mnc(self.mcc_mnc_path)
        if data is None:
            return {'available': False}
        if value('random') not in ('0', 'false', ''):
            return {'available': True, 'count': len(data),
                    'result': netmon.random_entry(data, value('exclude'))}
        return {'available': True, 'count': len(data),
                'results': netmon.search(data, value('q'))}

    # ---- the SCP81/BIP control endpoints (3c-3e-2b) -----------------------
    #
    # The SCP81 tab's listener/scripts and the Simulator's BIP pill.  The
    # session (owner/mode/target/listener, the terminal and its log) is the
    # persistent :attr:`bip_session`, so the status/log/script GETs answer
    # card-free and a listener survives a card change; the control object
    # (:mod:`simple_server.scp81`) owns the script engine and the PSK table.
    #
    # `POST /api/scp81/bip|bip/control|psk-map|queue` are card-locked like the
    # legacy (they are not in :data:`CARD_FREE_POST`): a start/stop closes or
    # enables the terminal's channels under the session's own lock, and a
    # listener start must not interleave a card exchange.  `gen-install` is
    # card-free work behind the same lock for simplicity (it is fast).

    def bip_status(self):
        """``GET /api/scp81/status`` / ``GET /api/bip/status`` (card-free).

        ``{owner, bip, listener}`` - one shared shape for both views (the
        legacy ``_bip_status_body``), with the TLS listener's own block
        (identities, last handshake, framing) in ``listener``.
        """
        return self.scp81.status_body()

    def bip_log(self, after=0):
        """``GET /api/scp81/log`` / ``GET /api/bip/log`` (card-free).

        The terminal's event log; ``?after=<seq>`` returns only newer
        entries, capped to the last 200 for the UI (the terminal itself keeps
        its own bounded history).
        """
        terminal = self.bip_session.terminal
        return {'seq': terminal.seq,
                'entries': terminal.entries_after(after)[-200:]}

    def bip_log_clear(self):
        """``POST /api/scp81/log-clear`` / ``POST /api/bip/log-clear``."""
        terminal = self.bip_session.terminal
        terminal.clear_log()
        return {'ok': True, 'seq': terminal.seq}

    def bip_control(self, body):
        """``POST /api/bip/control``: the Simulator's BIP pill.

        ``sink``/``redirect``/``passthru`` (the legacy ``_bip_control``);
        starting replaces any session - the SCP81 listener included - and the
        response reports what was stopped as ``replaced``.  A refusal is
        ``{'ok': False, 'error': ...}`` (the endpoint answers 400).
        """
        return self.scp81.bip_control(body)

    def scp81_bip(self, body):
        """``POST /api/scp81/bip``: start/stop the SCP81 listener.

        Modes ``tls`` (PSK TLS administration server), ``dump`` (capture
        listener), ``redirect``/``passthru`` (no listener - the BIP session's
        own modes).  Starting stops the running session (the BIP pill
        included) and reports it as ``replaced``.
        """
        return self.scp81.start(body)

    def scp81_script(self):
        """``GET /api/scp81/script``: the configured script and its run state."""
        return self.scp81.script_state()

    def scp81_queue(self, body):
        """``POST /api/scp81/queue``: replace/restart the command script.

        ``{apdus|script, kind, force}``; refused mid-run without ``force``
        (the caller's ``ok: False`` is the endpoint's 400).
        """
        return self.scp81.queue(body)

    def scp81_psk_map(self, body):
        """``POST /api/scp81/psk-map``: push card-preset PSK edits.

        Replaces the table of the running TLS listener without a restart.
        """
        return self.scp81.update_psk_map(body)

    def scp81_gen_install(self, body):
        """``POST /api/scp81/gen-install``: ``.cap`` -> the RAM APDU list.

        A generator for the PWA's "Install from .cap" script template:
        nothing is queued and the file is never stored (the legacy contract
        keeps ``ok: false`` in the body rather than a 4xx).
        """
        return self.scp81.gen_install(body)

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
        ``test_run`` is the run state's summary (the full report is
        ``GET /api/test/status``) and ``scp80`` the counter/keyset state
        (3c-3b), the latter a placeholder now.
        ``adm_verified`` is the ADM latch of the current card session (the
        legacy's ``rs.adm_verified``): a successful VERIFY until the next
        equip/disconnect.  ``euicc``/``eid`` describe the eUICC (the EID is
        read best effort at equip); ``ram_progress`` is the RAM operation in
        flight (:attr:`ram_progress`): the install chain holds the card lock,
        so this card-free poll is how its step/elapsed stay visible.
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
            'euicc': (self.connected and self.card is not None
                      and esim_module.card_is_euicc(self.card)),
            'eid': self.eid if self.connected else None,
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
            # The RAM operation in flight (step name/count and elapsed): the
            # install chains hold the card lock, so the modal's live status
            # rides this 2 s poll (the legacy ``ram_progress``, v3.6.34).
            'ram_progress': self.ram_progress_payload(),
            # The batch install's headline (the legacy ``ram_batch`` summary):
            # the PWA renders the phase/counts/notice on every 2 s poll and
            # fetches the detailed report only when it changed.
            'ram_batch': self.ram_batch_summary(),
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


def _after_param(path):
    """The ``?after=<seq>`` cursor of a BIP/SCP81 log request (0 if absent).

    The legacy parsed the query by hand and fell back to ``0`` for anything
    unparsable; a log cursor is not a request parameter worth a 400 (the
    caller polls it, and a stale cursor only replays the log).
    """
    query = path.split('?', 1)[1] if '?' in path else ''
    values = parse_qs(query, keep_blank_values=True).get('after') or []
    if not values:
        return 0
    try:
        return max(0, int(values[-1]))
    except (TypeError, ValueError):
        return 0


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

        def _blocked_by_run(self):
            """Answer 409 when a test run owns the card (``TEST_BLOCKED_PATHS``)."""
            if not self.state_ref.test_request_blocked(self.path):
                return False
            self._send_json({'error': 'test script running - card commands are '
                                      'blocked until it finishes'}, 409)
            return True

        def do_GET(self):
            path = self.path.split('?', 1)[0]
            if self._blocked_by_run():
                return
            if path in CARD_FREE_GET or not path.startswith('/api/'):
                self._dispatch_get()
                return
            with self.state_ref.card_lock:
                self._dispatch_get()

        def do_POST(self):
            path = self.path.split('?', 1)[0]
            if self._blocked_by_run():
                return
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
                elif path == '/api/ram-batch/status':
                    self._send_json(self.state_ref.ram_batch_status())
                elif path == '/api/test/status':
                    self._send_json(self.state_ref.test_status())
                elif path == '/api/test/scripts':
                    self._send_json(self.state_ref.test_scripts_info())
                elif path == '/api/test/suites':
                    self._send_json(self.state_ref.test_suites_info())
                elif path == '/api/menu':
                    self._send_json(self.state_ref.menu_info())
                elif path == '/api/stk-status':
                    self._send_json(self.state_ref.stk_status())
                elif path == '/api/events':
                    self._send_json(self.state_ref.events_info())
                elif path == '/api/poll-status':
                    self._send_json(self.state_ref.poll_status())
                elif path == '/api/proactive-log':
                    self._send_json(self.state_ref.proactive_log())
                elif path == '/api/terminal-profile':
                    self._send_json(self.state_ref.terminal_profile_info())
                elif path == '/api/pli-qualifiers':
                    self._send_json(self.state_ref.pli_qualifiers())
                elif path == '/api/pli-dict':
                    self._send_json(self.state_ref.pli_dict())
                elif path == '/api/net-state':
                    self._send_json(self.state_ref.net_state_info())
                elif path == '/api/mcc-mnc':
                    self._send_json(self.state_ref.mcc_mnc_info(
                        self.path.split('?', 1)[1] if '?' in self.path else ''))
                elif path == '/api/esim/chip':
                    self._get_esim(self.state_ref.esim_chip)
                elif path == '/api/esim/profiles':
                    self._get_esim(self.state_ref.esim_profiles)
                elif path == '/api/esim/notifications':
                    self._get_esim(self.state_ref.esim_notifications)
                elif path in ('/api/scp81/status', '/api/bip/status'):
                    self._send_json(self.state_ref.bip_status())
                elif path in ('/api/scp81/log', '/api/bip/log'):
                    self._send_json(self.state_ref.bip_log(
                        _after_param(self.path)))
                elif path == '/api/scp81/script':
                    self._send_json(self.state_ref.scp81_script())
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
                elif path == '/api/cap-info':
                    self._post_cap_info(body)
                elif path == '/api/ram-install':
                    self._post_ram_install(body)
                elif path == '/api/ram-install-app':
                    self._post_ram_install_app(body)
                elif path == '/api/cap-compat':
                    self._post_cap_compat(body)
                elif path == '/api/tar-probe':
                    self._post_tar_probe(body)
                elif path == '/api/ram-batch/start':
                    self._post_ram_batch_start(body)
                elif path == '/api/ram-batch/stop':
                    self._post_ram_batch_stop(body)
                elif path == '/api/ram-batch/clear':
                    self._post_ram_batch_clear(body)
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
                elif path == '/api/test/scripts':
                    self._send_json(self.state_ref.test_script_add(body))
                elif path == '/api/test/scripts/update':
                    stored, moved = self.state_ref.test_script_update(body)
                    if stored is None:
                        self._send_json({'error': 'unknown test script id'}, 404)
                    elif moved:
                        self._send_json({'ok': True, 'script': stored,
                                         'moved': True})
                    else:
                        self._send_json({'ok': True, 'script': stored})
                elif path == '/api/test/scripts/delete':
                    self._send_json(self.state_ref.test_script_delete(body))
                elif path == '/api/test/scripts/import':
                    self._send_json(self.state_ref.test_script_import(body))
                elif path == '/api/test/scripts/move':
                    self._send_json({'ok': True,
                                     'script': self.state_ref.test_script_move(body)})
                elif path == '/api/test/scripts/copy':
                    self._send_json({'ok': True,
                                     'script': self.state_ref.test_script_copy(body)})
                elif path == '/api/test/suites':
                    self._send_json(self.state_ref.test_suite_add(body))
                elif path == '/api/test/suites/update':
                    stored = self.state_ref.test_suite_update(body)
                    if stored is None:
                        self._send_json({'error': 'unknown test suite id'}, 404)
                    else:
                        self._send_json({'ok': True, 'suite': stored})
                elif path == '/api/test/suites/delete':
                    self._send_json(self.state_ref.test_suite_delete(body))
                elif path == '/api/test/suites/import':
                    self._send_json(self.state_ref.test_suite_import(body))
                elif path == '/api/test/run':
                    self._post_test_run(body)
                elif path == '/api/test/stop':
                    self._send_json(self.state_ref.test_stop())
                elif path == '/api/test/clear':
                    if not self.state_ref.test_clear():
                        self._send_json({'error': 'test script is running'},
                                        409)
                    else:
                        self._send_json(self.state_ref.test_status())
                elif path == '/api/menu-select':
                    self._post_card(lambda: self.state_ref.menu_select(body),
                                    {})
                elif path == '/api/menu-respond':
                    self._post_card(lambda: self.state_ref.menu_respond(body),
                                    {})
                elif path == '/api/event-send':
                    self._post_card(lambda: self.state_ref.event_send(body),
                                    {'sw': None})
                elif path == '/api/terminal-profile':
                    self._post_card(
                        lambda: self.state_ref.set_terminal_profile(body),
                        {'ok': False})
                elif path == '/api/rescue':
                    self._post_card(self.state_ref.rescue, {'ok': False})
                elif path == '/api/status-poll':
                    self._post_card(self.state_ref.status_poll, {'sw': None})
                elif path == '/api/poll-toggle':
                    self._post_card(lambda: self.state_ref.poll_toggle(body),
                                    {})
                elif path == '/api/pli-dict':
                    self._send_json(self.state_ref.set_pli_dict(body))
                elif path == '/api/apdu':
                    self._post_card(lambda: self.state_ref.raw_apdu(body),
                                    {})
                elif path == '/api/esim/profile':
                    self._post_card(
                        lambda: self.state_ref.esim_profile_switch(body),
                        {'ok': False})
                elif path == '/api/net-sim':
                    self._post_card(lambda: self.state_ref.net_sim(body),
                                    {'success': False})
                elif path == '/api/net-state-refresh':
                    self._post_card(
                        lambda: self.state_ref.net_state_refresh(body),
                        {'available': False})
                elif path == '/api/scp81/bip':
                    self._post_scp81(lambda: self.state_ref.scp81_bip(body))
                elif path == '/api/bip/control':
                    self._post_scp81(lambda: self.state_ref.bip_control(body))
                elif path == '/api/scp81/psk-map':
                    self._post_scp81(
                        lambda: self.state_ref.scp81_psk_map(body))
                elif path == '/api/scp81/queue':
                    self._post_scp81(lambda: self.state_ref.scp81_queue(body))
                elif path == '/api/scp81/gen-install':
                    self._post_scp81(
                        lambda: self.state_ref.scp81_gen_install(body),
                        legacy_ok_false=True)
                elif path in ('/api/scp81/log-clear', '/api/bip/log-clear'):
                    self._send_json(self.state_ref.bip_log_clear())
                else:
                    self._send_json({'error': 'Not found'}, 404)
            except presets_store.PresetError as exc:
                # a refused store mutation: the store's message is the answer
                self._send_json({'error': str(exc)}, 400)
            except test_scripts_store.TestScriptError as exc:
                # a refused test-script mutation (validation): the store's own
                # message is the endpoint's 400
                self._send_json({'error': str(exc)}, 400)
            except test_suites_store.TestSuiteError as exc:
                # the same for a suite mutation (its own shape validation)
                self._send_json({'error': str(exc)}, 400)
            except suites_module.MoveError as exc:
                # a refused cross-store move/copy carries the legacy status
                self._send_json({'error': str(exc)}, exc.status)
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

        def _get_esim(self, action):
            """A card-touching eSIM GET: 400 for a non-eUICC, 503 without a card.

            The eSIM endpoints select the ISD-R and transceive, so they are
            card-locked (not in ``CARD_FREE_GET``).  A card without the ISD-R
            is a request-level refusal (:class:`simple_server.esim.
            NotAnEuiccError`, a ``ValueError``) and answers ``400`` like the
            legacy's ``not_an_euicc``; a transport error follows the shared
            classification.
            """
            try:
                self._send_json(action())
            except NoCardSessionError as exc:
                self._send_json({'error': str(exc),
                                 **self.state_ref.status()}, 503)
            except ValueError as exc:
                self._send_json({'error': str(exc)}, 400)
            except Exception as exc:
                self._card_failure(exc, {})

        def _post_adm(self, body):
            self._post_card(lambda: self.state_ref.verify_adm(body),
                            {'ok': False})

        def _post_ota(self, body):
            self._post_card(lambda: self.state_ref.send_ota(body),
                            {'success': False})

        def _post_counter_probe(self, body):
            self._post_card(lambda: self.state_ref.counter_probe(body),
                            {'success': False})

        # ---- the CAP analysis and the single-card RAM installs (3c-3c-2a) --

        def _post_cap_info(self, body):
            """The card-free CAP analysis: a bad file is the caller's 400.

            ``ok: false`` is kept in the body (the PWA reads ``ok`` and
            ``error``), so its CAP panel shows the reason either way.
            """
            try:
                self._send_json(self.state_ref.cap_info(body))
            except ValueError as exc:
                self._send_json({'ok': False, 'error': str(exc)}, 400)

        def _post_ram_install(self, body):
            self._post_card(lambda: self.state_ref.ram_install(body),
                            {'success': False})

        def _post_ram_install_app(self, body):
            self._post_card(lambda: self.state_ref.ram_install_app(body),
                            {'success': False})

        # ---- the CAP compatibility test, the TAR probe and the batch (3c-3c-2b)

        def _post_cap_compat(self, body):
            """The import-list verdict: a rejection is a 200 with success false."""
            self._post_card(lambda: self.state_ref.cap_compat(body),
                            {'success': False, 'imports_ok': False})

        def _post_tar_probe(self, body):
            self._post_card(lambda: self.state_ref.tar_probe(body),
                            {'success': False})

        def _post_ram_batch_start(self, body):
            """Start the batch: 409 while busy, 400 for a bad request/plan.

            The plan is built before the worker starts, so a malformed ``.cap``
            (and every other plan refusal) is the caller's 400 - never a
            silently half-started run.
            """
            try:
                self.state_ref.ram_batch_start(body)
            except BatchBusyError as exc:
                self._send_json({'error': str(exc)}, 409)
                return
            except Exception as exc:        # ValueError and a plan-level error
                self._send_json({'error': str(exc)}, 400)
                return
            resp = self.state_ref.ram_batch_status()
            resp['ok'] = True
            self._send_json(resp)

        def _post_ram_batch_stop(self, body):
            if not self.state_ref.ram_batch_stop():
                self._send_json({'error': 'no batch is running'}, 409)
                return
            resp = self.state_ref.ram_batch_status()
            resp['ok'] = True
            self._send_json(resp)

        def _post_ram_batch_clear(self, body):
            if not self.state_ref.ram_batch_clear():
                self._send_json({'error': 'a batch is running - stop it first'},
                                409)
                return
            self._send_json({'ok': True})

        # ---- the SCP81/BIP control (3c-3e-2b) -------------------------

        def _post_scp81(self, action, legacy_ok_false=False):
            """A SCP81/BIP control endpoint: a refusal is a 400 with its body.

            The control responses carry their own ``ok``; a refusal
            (``ok: false`` - a bad mode, a listener bind failure, a script
            mid-run, no PSK configured) answers ``400`` so a caller can tell
            it from a success.  ``/api/scp81/gen-install`` keeps the legacy's
            ``200 {ok: false}`` (the PWA's CAP/script panel reads ``ok``
            either way); its refusals are file/parameter problems, not a
            request shape the caller can fix.
            """
            try:
                resp = action()
            except ValueError as exc:
                self._send_json({'ok': False, 'error': str(exc)}, 400)
                return
            success = bool(resp.get('ok', True))
            self._send_json(resp, 200 if (success or legacy_ok_false) else 400)

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

        # ---- the test scripts/suites and the runs (3c-3d-1/2) ------

        def _post_test_run(self, body):
            """Start a run: 409 while busy, 404 for an unknown script id.

            The validation/ADM refusals are a ``ValueError`` (the dispatch's
            400); the ADM gate's structured state rides the body as ``adm``
            so the PWA can show the attempts left.
            """
            try:
                self._send_json(self.state_ref.test_run(body))
            except testrun.TestRunBusy as exc:
                self._send_json({'error': str(exc)}, 409)
            except testrun.TestAdmError as exc:
                self._send_json({'error': str(exc), 'adm': exc.adm}, 400)
            except testrun.TestRunNotFound as exc:
                self._send_json({'error': str(exc)}, 404)

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
