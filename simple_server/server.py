# coding=utf-8
"""The pysim-free server core: one server's state, card lifecycle, HTTP API.

This module owns what the legacy ``pysim_simple_server/server.py`` spread over
module globals: the card link, the detected card, the CAT session, the BIP
session, the file model, the session caches and the background threads.  One
:class:`ServerState` describes one server; nothing here is a module global.

Scope (Phase 3c-3a): the state and the card lifecycle (:meth:`ServerState.open`,
:meth:`~ServerState.equip`, :meth:`~ServerState.reset`,
:meth:`~ServerState.disconnect`, the presence monitor and the background STATUS
poll), the HTTP handler + :class:`http.server.ThreadingHTTPServer` factory over
it, the static PWA serving and the first endpoints - ``GET /api/version``,
``GET /api/status``, ``GET /api/readers``, ``POST /api/reader``,
``POST /api/equip`` and ``POST /api/reset``.  Every other API path is a 404
until the file/preset/SCP80/RAM/test/eSIM/SCP81/netsim endpoints land in
3c-3b/3c-3c.

Deliberate API decisions (see ``docs/pysim-phaseout.md`` "Phase 3c API
surface"): there is no pySim command line, no ``/api/command``/``/api/help``/
``/api/commands``/``/api/cardinfo``; ``/api/status`` has our own shape (no
pySim concepts); ``/api/apdu`` (3c-3b) keeps the automatic proactive handler on
an ``91XX`` answer.  The card lock discipline is the legacy one: every
``/api/`` GET/POST is serialized by the one ``threading.RLock`` except the
card-free GET allowlist (:data:`CARD_FREE_GET`) and the static PWA files, so a
long card operation cannot starve the UI's state poll.

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
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from pysim_simple_server import presets as presets_store

from simple_server import bip as bip_module
from simple_server import cards
from simple_server import cat_session
from simple_server import commands
from simple_server import files as files_module
from simple_server.transport import PcscLink, ReaderError

log = logging.getLogger(__name__)

__all__ = [
    'ServerState', 'make_handler', 'build_http_server', 'reader_names',
    'CardBusyError', 'CARD_FREE_GET', 'CARD_FREE_POST',
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
#: is deliberately small in 3c-3a; the later endpoints add themselves as they
#: are ported (the legacy list is the reference).
CARD_FREE_GET = frozenset([
    '/api/status', '/api/version', '/api/readers',
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
    preset store (default ``pysim_simple_server.presets.PresetStore``);
    ``presence_install`` registers the presence observer (tests replace it to
    avoid real pyscard polling).  ``monitor=False`` disables the presence
    observer and ``open(start_threads=False)`` keeps the background poll off -
    the two test seams.

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
    * ``selection`` is owned by this state but only the file endpoints (3c-3b)
      populate it; the post-equip reads restore MF and never publish a
      selection.
    """

    def __init__(self, transport=None, transport_factory=None, reader=None,
                 presets=None, web_dir=None,
                 terminal_profile=DEFAULT_TERMINAL_PROFILE,
                 poll_interval=DEFAULT_POLL_INTERVAL, menu_timeout=60,
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
        self.presets = (presets if presets is not None
                        else presets_store.PresetStore())

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

    def _file_ops(self):
        """A :class:`~simple_server.commands.FileOps` for the equipped card."""
        card = self.card
        uicc = card is None or card.kind in ('uicc', 'euicc')
        return commands.FileOps(
            self.transport,
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
                elif path.startswith('/api/'):
                    self._send_json({'error': 'Not found'}, 404)
                else:
                    self._serve_static()
            except Exception as exc:
                log.exception('GET %s failed', self.path)
                self._send_json({'error': str(exc)}, 500)

        def _dispatch_post(self):
            path = self.path.split('?', 1)[0]
            if log_requests:
                log.info('POST %s', self.path)
            try:
                body = self._read_body()
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                self._send_json({'error': 'invalid JSON body: %s' % exc}, 400)
                return
            except Exception as exc:
                log.exception('POST %s failed reading the body', self.path)
                self._send_json({'error': str(exc)}, 500)
                return
            try:
                if not isinstance(body, dict):
                    self._send_json({'error': 'the request body must be a '
                                              'JSON object'}, 400)
                    return
                if path == '/api/reader':
                    self._post_reader(body)
                elif path == '/api/equip':
                    self._post_lifecycle(self.state_ref.equip)
                elif path == '/api/reset':
                    self._post_lifecycle(self.state_ref.reset)
                else:
                    self._send_json({'error': 'Not found'}, 404)
            except ValueError as exc:
                # An endpoint's own validation error: its 400, not a JSON one.
                self._send_json({'error': str(exc)}, 400)
            except Exception as exc:
                log.exception('POST %s failed', self.path)
                self._send_json({'error': str(exc)}, 500)

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
