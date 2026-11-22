# coding=utf-8
"""The ``simple-server`` entry point: the CLI, the startup and the shutdown.

The pysim-free server's only operating-system-facing module.  It parses the
command line, runs the PC/SC access pre-flight (v3.38.1: an access denial, a
stopped pcscd or a missing reader is reported with guidance and exits ``2``
instead of starting a server without a card link), builds the
:class:`~simple_server.server.ServerState` with the deployment paths,
initializes the card (a cardless start is a normal state - one line, no
traceback) and serves the PWA + API until SIGINT/SIGTERM.

Two operating rules the rest of the package relies on:

* **The console text layer is ASCII-only** (v3.30.3).  ``_guard_console_streams``
  runs first and sets ``errors="replace"`` on both streams, so an unencodable
  character can never kill the process (under ``start.bat`` they are pipes into
  PowerShell, which encodes with the ANSI code page); ``_console_rule`` picks
  the banner rule its stream can encode, and every printed literal here is
  ASCII.
* **The diagnostics are INFO-level log lines** for ``simple_server`` and
  WARNING for third parties (:func:`configure_logging`).  The RAM/OTA/runner
  progress lines (`RAM C-APDU`, `OTA SEND`, `TEST-RUN`, ...) are the bench's
  primary diagnostic and must reach ``simple_lastrun.log``, which mirrors
  stderr; the modules themselves stay logging-configuration free so the
  package can also be embedded.

Usage: ``python -m simple_server`` or the ``simple-server`` console script
(:mod:`simple_server` must be importable, i.e. installed or run from the repo).
"""

import argparse
import logging
import os
import signal
import sys
import threading
import time
import traceback

from simple_server import gsmtap

log = logging.getLogger('simple_server.__main__')

__all__ = [
    'DEFAULT_TERMINAL_PROFILE', 'PROBE_RETRIES', 'PROBE_RETRY_DELAY',
    'PcscUnavailable', 'build_parser', 'configure_logging', 'main',
]

#: The TERMINAL PROFILE of a real BIP-capable handset (the legacy default,
#: byte-identical to ``simple_server.server.DEFAULT_TERMINAL_PROFILE`` - a
#: drift test pins the pair): the live card only starts HTTP OTA when the BIP
#: commands and events are advertised.
DEFAULT_TERMINAL_PROFILE = (
    'FFFFFFFF7F9F00DFFF03021FE2000000C3FB000704117800710100000038428003')

#: The PC/SC pre-flight: how often the reader list is polled (USB enumeration
#: needs a moment) and how long between attempts.
PROBE_RETRIES = 3
PROBE_RETRY_DELAY = 2

#: The log format: a short wall clock, the level, the logger and the message.
LOG_FORMAT = '[%(asctime)s] %(levelname)s %(name)s: %(message)s'
LOG_DATEFMT = '%H:%M:%S'

#: Third-party loggers pinned to WARNING so the INFO diagnostics above stay
#: readable (pyscard's negotiation chatter, urllib3's retries, ...).
THIRD_PARTY_LOGGERS = ('smartcard', 'urllib3', 'requests', 'asyncio')

# PC/SC hresults (pyscard's exceptions carry them; the messages are often
# empty, so the hresult is the reliable signal - v3.38.1).
_PCSC_E_UNKNOWN_READER = 0x80100009
_PCSC_E_NO_SERVICE = 0x8010001D
_PCSC_E_SERVICE_STOPPED = 0x8010001E
_PCSC_E_NO_ACCESS = 0x80100027
_PCSC_E_NO_READERS_AVAILABLE = 0x8010002E

#: ``--timing``: the startup phases (and the shutdown) log their duration.
_TIMING = {'on': False, 'start': 0.0}


class PcscUnavailable(RuntimeError):
    """The PC/SC stack cannot be used (``kind``: access/service/reader)."""

    def __init__(self, kind, cause=None):
        super().__init__(kind)
        self.kind = kind
        self.cause = cause

    def __str__(self):
        return '%s%s' % (self.kind, ': %s' % self.cause if self.cause else '')


# ---- console -----------------------------------------------------------------

def _guard_console_streams():
    """Never let a non-encodable character kill the server (the first call).

    On Windows the launcher pipes stdout/stderr into PowerShell (see
    ``start.bat``), so Python encodes with the ANSI code page instead of the
    wide console API - cp1251 cannot encode the banner's box-drawing rule
    (U+2500) and one such character used to raise ``UnicodeEncodeError`` and
    kill the process.  ``errors="replace"`` keeps every message, whatever the
    code page (a character the code page lacks prints as ``?``).
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors='replace')
        except Exception:
            pass


def _console_rule(n=70):
    """The banner rule in the widest character the output stream can encode.

    Under ``start.bat`` stdout is a pipe (not a console), so Python encodes
    with the ANSI code page - cp1251 (Russian Windows) cannot encode U+2500 and
    falls back to a plain ASCII rule (cp437 has the box-drawing characters and
    keeps the pretty one).
    """
    enc = getattr(sys.stdout, 'encoding', None) or 'ascii'
    try:
        '\u2500'.encode(enc)
        return '\u2500' * n
    except (UnicodeEncodeError, LookupError):
        return '-' * n


def _timing_on():
    """Enable the ``--timing`` phase log."""
    _TIMING['on'] = True
    _TIMING['start'] = time.time()


def _tlog(message):
    """One ``--timing`` line (``TIMING [+1.234s] ...``), ASCII only."""
    if not _TIMING['on']:
        return
    elapsed = time.time() - (_TIMING['start'] or time.time())
    sys.stderr.write('TIMING [+%7.3fs] %s\n' % (elapsed, message))


# ---- defaults ----------------------------------------------------------------

def _repo_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _default_web_dir():
    """``<repo>/frontend``, whether run from source or an editable install."""
    return os.path.join(_repo_root(), 'frontend')


def _default_mcc_mnc_list():
    """The bundled MCC/MNC operator list (``--mcc-mnc-list`` overrides)."""
    from simple_server import netmon
    return netmon.default_mcc_mnc_path()


# ---- logging -----------------------------------------------------------------

def configure_logging(stream=None, level=logging.INFO):
    """Install the server's logging: INFO for ``simple_server``, WARNING else.

    The package's modules are configuration-free on purpose (it can be
    embedded); the entry point owns the setup.  ``basicConfig(force=True)``
    reinstalls the handler, so a repeated call (a test, an embedding restart)
    cannot stack handlers, and the third-party loggers are pinned to WARNING
    so they stay out of the diagnostic stream.
    """
    logging.basicConfig(
        level=level,
        stream=stream if stream is not None else sys.stderr,
        format=LOG_FORMAT,
        datefmt=LOG_DATEFMT,
        force=True)
    logging.getLogger('simple_server').setLevel(level)
    for name in THIRD_PARTY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


# ---- the PC/SC pre-flight ----------------------------------------------------

def _pcsc_readers():
    """The PC/SC reader names; the pyscard failure propagates for classification."""
    from smartcard.System import readers
    return [str(reader.name) for reader in readers()]


def _pcsc_error_kind(exc):
    """Classify a pyscard failure: ``access``/``service``/``reader`` or ``None``.

    ``SCARD_E_NO_ACCESS`` is the "Access denied" a user without permission to
    pcscd gets; the message is a fallback because some pyscard paths raise a
    ``CardConnectionException`` with an empty message and only the hresult.
    """
    hr = getattr(exc, 'hresult', None)
    msg = str(exc or '').lower()
    if hr == _PCSC_E_NO_ACCESS or 'access denied' in msg \
            or 'access is denied' in msg:
        return 'access'
    if hr in (_PCSC_E_NO_SERVICE, _PCSC_E_SERVICE_STOPPED) \
            or 'resource manager is not running' in msg \
            or 'service not available' in msg:
        return 'service'
    if hr in (_PCSC_E_UNKNOWN_READER, _PCSC_E_NO_READERS_AVAILABLE) \
            or 'no smart card readers' in msg:
        return 'reader'
    return None


def _report_pcsc_unavailable(kind, exc=None):
    """Print an actionable reason instead of starting a readerless server."""
    if kind == 'access':
        user = os.environ.get('USER') or os.environ.get('LOGNAME') or '$USER'
        sys.stderr.write('INIT: PC/SC access denied - the current user may '
                         'not use pcscd.\n')
        sys.stderr.write('  pcscd gates access through PolicyKit: the default '
                         'policy allows\n')
        sys.stderr.write('  only *active local* sessions, so an SSH/headless '
                         'session is denied\n')
        sys.stderr.write('  even when /run/pcscd/pcscd.comm is world-writable '
                         '(0666).\n')
        sys.stderr.write('  Allow the user in '
                         '/etc/polkit-1/rules.d/99-pcscd-allow.rules:\n')
        sys.stderr.write('    polkit.addRule(function(a, s) {\n')
        sys.stderr.write('      if ((a.id == "org.debian.pcsc-lite.access_pcsc" ||\n')
        sys.stderr.write('           a.id == "org.debian.pcsc-lite.access_card") &&\n')
        sys.stderr.write('          s.user == "%s") return polkit.Result.YES;\n' % user)
        sys.stderr.write('    });\n')
        sys.stderr.write('  then restart polkit (sudo systemctl restart polkit) '
                         'or log out/in;\n')
        sys.stderr.write('  `pcsc_scan` shows the same "SCardEstablishContext: '
                         'Access denied"\n')
        sys.stderr.write('  error until the rule is active.\n')
    elif kind == 'service':
        sys.stderr.write('INIT: pcscd is not running or not reachable.\n')
        sys.stderr.write('  Start it (sudo systemctl start pcscd) and restart '
                         'the server.\n')
    else:
        sys.stderr.write('INIT: no PC/SC reader found.\n')
        sys.stderr.write('  Connect a reader (pcsc_scan lists them) and restart '
                         'the server.\n')
    if exc is not None:
        hr = getattr(exc, 'hresult', None)
        detail = str(exc) or type(exc).__name__
        if hr not in (None, -1):
            detail += ' (hresult=0x%08X)' % hr
        sys.stderr.write('  (%s)\n' % detail)


def _probe_pcsc_reader(retries=PROBE_RETRIES, sleep_fn=time.sleep,
                       readers_fn=None, reader=None):
    """Return the PC/SC reader to use or raise :class:`PcscUnavailable`.

    ``reader`` names an explicit ``-r/--reader``; without one the first reader
    is used.  ``SCARD_E_NO_ACCESS``/service failures are raised immediately
    (retrying cannot fix permissions or a stopped service); an empty list is
    retried (USB enumeration) and reported as ``reader`` after the last
    attempt.  An explicit reader that is absent is a ``reader`` failure at once
    (waiting will not make its name appear).
    """
    if readers_fn is None:
        readers_fn = _pcsc_readers
    if reader is not None:
        # One normalisation for both the pre-flight and the state (the state
        # strips too); a whitespace-only name means "the first reader".
        reader = str(reader).strip() or None
    last = None
    attempts = max(1, int(retries))
    for attempt in range(attempts):
        try:
            found = [str(name) for name in readers_fn()]
        except Exception as exc:
            last = exc
            kind = _pcsc_error_kind(exc)
            if kind in ('access', 'service'):
                raise PcscUnavailable(kind, exc) from exc
        else:
            if found:
                if reader is None:
                    return found[0]
                if str(reader) in found:
                    return str(reader)
                raise PcscUnavailable('reader', LookupError(
                    'PC/SC reader %r not found (available: %s)'
                    % (reader, ', '.join(found))))
            last = None
        if attempt + 1 < attempts:
            sys.stderr.write('INIT: no PC/SC reader yet, retrying in %ds...\n'
                             % PROBE_RETRY_DELAY)
            sleep_fn(PROBE_RETRY_DELAY)
    raise PcscUnavailable('reader', last)


# ---- the CLI -----------------------------------------------------------------

def _port_number(value):
    """An ``argparse`` type for a TCP port: ``0``-``65535`` (0 = ephemeral).

    Without this an out-of-range value reached ``socket.bind``, which raises
    ``OverflowError`` (not ``OSError``), so the friendly bind-failure message
    was skipped and the process died with a traceback.
    """
    try:
        port = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError('invalid port: %r' % (value,))
    if not 0 <= port <= 65535:
        raise argparse.ArgumentTypeError(
            'port must be 0-65535 (0 = an ephemeral port), got %d' % port)
    return port


def build_parser():
    """The command-line parser (``--help`` needs no card and no pyscard)."""
    parser = argparse.ArgumentParser(
        prog='simple-server',
        description='simple-server - the pysim-free HTTP API for the SIMple PWA')
    parser.add_argument(
        '-r', '--reader', default=None, metavar='NAME',
        help='PC/SC reader name (default: the first reader; the list is '
             'checked at startup)')
    parser.add_argument(
        '--http-host', default='127.0.0.1', metavar='ADDR',
        help='Bind address (default: 127.0.0.1; use 0.0.0.0 for a container '
             'or a phone on the LAN)')
    parser.add_argument(
        '--http-port', type=_port_number, default=8080, metavar='PORT',
        help='Bind port 0-65535 (default: 8080; 0 picks an ephemeral port)')
    parser.add_argument(
        '--web-dir', default=_default_web_dir(), metavar='PATH',
        help='Directory with the SIMple PWA static files to serve '
             '(default: <repo>/frontend)')
    parser.add_argument(
        '--mcc-mnc-list', default=_default_mcc_mnc_list(), metavar='PATH',
        help='Worldwide MCC/MNC operator list (JSON) for the network '
             'simulation operator picker (default: the bundled list)')
    parser.add_argument(
        '--card-presets', default=None, metavar='PATH',
        help='Card preset store (default: '
             '~/.pysim-simple-server/card_presets.json)')
    parser.add_argument(
        '--test-scripts', default=None, metavar='PATH',
        help='Test script store (default: '
             '~/.pysim-simple-server/test_scripts.json)')
    parser.add_argument(
        '--test-suites', default=None, metavar='PATH',
        help='Test suite store (default: '
             '~/.pysim-simple-server/test_suites.json)')
    parser.add_argument(
        '--sms-oa', default='12345', metavar='DIGITS',
        help='TP-Originating-Address (SMSC number) for the SMS-DELIVER TPDU '
             '(default: 12345)')
    parser.add_argument(
        '--sms-sm-sc', default='12345678912', metavar='DIGITS',
        help='SM-SC address for SMS-SUBMIT routing in PoR-in-submit mode '
             '(default: 12345678912)')
    parser.add_argument(
        '--terminal-profile', default=DEFAULT_TERMINAL_PROFILE, metavar='HEX',
        help='TERMINAL PROFILE payload (default: the 33-byte profile of a '
             'real BIP-capable handset - the live card only starts HTTP OTA '
             'when BIP events/commands are advertised)')
    parser.add_argument(
        '--poll-interval', type=int, default=30, metavar='SECS',
        help='Idle interval before automatic STATUS polling (1-255 seconds, '
             'default: 30; 0 disables it)')
    parser.add_argument(
        '--menu-timeout', type=int, default=60, metavar='SECS',
        help='Auto-send a timeout TERMINAL RESPONSE when a paused STK command '
             'is not answered (default: 60; 0 disables)')
    parser.add_argument(
        '--no-card-init', action='store_true', default=False,
        help='Skip the startup card initialization phase (no detection, no MF '
             'select, no TERMINAL PROFILE/drain; the link and the ATR are '
             'still read, the poll and the presence monitor run, and Equip '
             'initializes a card later)')
    parser.add_argument(
        '--no-auto-equip', action='store_true', default=False,
        help='Do not automatically initialize a card after it is inserted '
             '(default: auto-equip on)')
    parser.add_argument(
        '--apdu-trace', action='store_true', default=False,
        help='Trace the card exchange on stderr (what SCardTransmit carries - '
             'the logical APDU in APDU mode, the TPDU on a TPDU-mode reader - '
             'with the response data, the status word and the ATR)')
    parser.add_argument(
        '--gsmtap', nargs='?', const=gsmtap.DEFAULT_TARGET, default=None,
        metavar='HOST[:PORT]',
        help='Stream the card exchange and the ATR as GSMTAP-SIM UDP packets '
             'for Wireshark / SIMtrace Analyser (default target: %s)'
             % gsmtap.DEFAULT_TARGET)
    parser.add_argument(
        '--log-requests', action='store_true', default=False,
        help='Log request/response payloads to stderr')
    parser.add_argument(
        '--timing', action='store_true', default=False,
        help='Log the startup phase durations and the shutdown with elapsed '
             'timestamps')
    return parser


# ---- tracers -----------------------------------------------------------------

def _build_tracers(opts, stderr=None):
    """The ``--apdu-trace`` / ``--gsmtap`` sinks; ``None`` when neither is set.

    Both are transmit-level (:mod:`simple_server.gsmtap`) and combine through a
    :class:`~simple_server.gsmtap.Fanout`.  A bad ``--gsmtap`` target disables
    only the streaming - the server still starts (the legacy behaviour).
    """
    stderr = sys.stderr if stderr is None else stderr
    tracers = []
    if opts.apdu_trace:
        tracers.append(gsmtap.StderrTracer())
    if opts.gsmtap is not None:
        try:
            host, port = gsmtap.parse_target(opts.gsmtap)
            sender = gsmtap.GsmtapSender(host, port)
        except (ValueError, OSError) as exc:
            stderr.write('GSMTAP: disabled (%s)\n' % exc)
        else:
            tracers.append(gsmtap.GsmtapTracer(sender))
            stderr.write('GSMTAP: streaming APDUs to %s\n' % sender.target)
    if not tracers:
        return None
    if len(tracers) == 1:
        return tracers[0]
    return gsmtap.Fanout(tracers)


# ---- the state ---------------------------------------------------------------

def _build_state(**kwargs):
    """The default state factory (the entry point's seam for the tests)."""
    from simple_server.server import ServerState
    return ServerState(**kwargs)


def _transport_factory(tracer):
    """Build a link and attach the transmit-level tracer.

    Passed to :class:`~simple_server.server.ServerState`, so every rebuilt link
    (a card swap, a reader switch, the PC/SC recovery) carries the tracer -
    the legacy's ``_keep_tracer_attached`` problem cannot recur.
    """
    from simple_server.transport import PcscLink

    def factory(name):
        link = PcscLink(reader=name)
        gsmtap.attach(link, tracer)
        return link

    return factory


def _state_kwargs(opts, reader, tracer):
    """The :class:`ServerState` keyword arguments for the parsed CLI.

    ``readers_fn`` is deliberately not forwarded: the state's default
    (:func:`simple_server.server.reader_names`) swallows a failing PC/SC list
    so ``/api/status`` keeps answering, while the pre-flight's reader function
    must let the failure through for classification.
    """
    return {
        'reader': reader,
        'transport_factory': _transport_factory(tracer),
        'presets_path': opts.card_presets,
        'web_dir': opts.web_dir,
        'terminal_profile': opts.terminal_profile,
        'poll_interval': opts.poll_interval,
        'menu_timeout': opts.menu_timeout,
        'sms_oa': opts.sms_oa,
        'sms_sc': opts.sms_sm_sc,
        'auto_equip': not opts.no_auto_equip and not opts.no_card_init,
        'test_scripts_path': opts.test_scripts,
        'test_suites_path': opts.test_suites,
        'mcc_mnc_path': opts.mcc_mnc_list,
    }


# ---- startup helpers ---------------------------------------------------------

def _is_no_card_error(exc):
    """True for the "no card in the reader" condition (a normal cardless start)."""
    return 'no card' in str(exc or '').lower()


def _is_transport_error(exc):
    """True when the startup failure is the PC/SC link, not a card decoding bug.

    The server's own classifier (:func:`simple_server.server._is_transport_error`)
    is imported lazily: ``--help`` must not need pyscard.
    """
    from simple_server.server import _is_transport_error as classify
    return classify(exc)


def _report_card_error(exc, stderr=None):
    """Report a startup init failure that is not "no card" (keep serving)."""
    stderr = sys.stderr if stderr is None else stderr
    stderr.write('INIT: card initialization failed (%s) - server ready; '
                 'insert a card or press Equip\n' % exc)
    log.info('card initialization traceback:\n%s',
             ''.join(traceback.format_exception(
                 type(exc), exc, exc.__traceback__)))


def _report_card_removed(stderr=None):
    """The v3.32.5 line: the card went away during the startup init.

    A removal is not an initialization failure - the server stays ready and
    the presence monitor equips the next card.  No traceback: nothing went
    wrong in our code.
    """
    stderr = sys.stderr if stderr is None else stderr
    stderr.write('INIT: card removed during initialization - server ready; '
                 'insert a card or press Equip\n')


def _startup(state, opts, stderr=None):
    """Run the card initialization; a cardless start is a normal state.

    ``--no-card-init`` skips the initialization *phase* only
    (``state.open(skip_card_init=True)``): the link is still built, the ATR
    read and the background STATUS poll plus the presence monitor started, so
    an Equip later works and a removal is still noticed.
    """
    stderr = sys.stderr if stderr is None else stderr
    started = time.time()
    try:
        state.open(skip_card_init=opts.no_card_init)
    except Exception as exc:
        if _is_no_card_error(exc):
            stderr.write('INIT: no card in the reader - server ready; insert '
                         'a card or press Equip\n')
        elif _is_transport_error(exc):
            _report_card_removed(stderr)
        else:
            _report_card_error(exc, stderr)
    else:
        if opts.no_card_init:
            stderr.write('INIT: card initialization skipped (--no-card-init) - '
                         'server ready; press Equip to initialize a card\n')
        else:
            iccid = getattr(state, 'iccid', None)
            stderr.write('INIT: card ready%s\n'
                         % (' - ICCID %s' % iccid if iccid else ''))
    _tlog('card initialization: %.0fms' % ((time.time() - started) * 1000))


def _install_signal_handlers(http):
    """SIGINT/SIGTERM shut the HTTP server down (``serve_forever`` returns)."""
    def handler(_signum, _frame):
        # shutdown() blocks until serve_forever() returns, so it must not run
        # on the thread that is inside serve_forever().
        threading.Thread(target=http.shutdown, name='simple-shutdown',
                         daemon=True).start()

    for name in ('SIGINT', 'SIGTERM'):
        number = getattr(signal, name, None)
        if number is None:                      # a platform without the constant
            continue
        try:
            signal.signal(number, handler)
        except (OSError, ValueError, RuntimeError):   # not the main thread
            log.debug('cannot install the %s handler', name)


def _serve(state, http):
    """Serve until a signal, then close the state; returns the exit code."""
    started = time.time()
    _install_signal_handlers(http)
    try:
        http.serve_forever()
    except KeyboardInterrupt:
        # A platform without a replaceable SIGINT handler (or a call chain
        # that bypassed it) still shuts down cleanly.
        print('\nShutting down...')
    finally:
        try:
            http.server_close()
        except Exception as exc:
            log.debug('HTTP server close failed: %s', exc)
        try:
            state.close()
        except Exception as exc:
            log.warning('state close failed: %s', exc)
        _tlog('served %.1fs' % (time.time() - started))
    return 0


def main(argv=None, probe_fn=None, readers_fn=None, state_factory=None,
         http_factory=None, serve_fn=None):
    """The entry point; returns the process exit code (2 = PC/SC unavailable).

    The keyword seams (``probe_fn``, ``readers_fn``, ``state_factory``,
    ``http_factory``, ``serve_fn``) let the tests drive the startup without a
    real reader or a listening socket.
    """
    _guard_console_streams()
    configure_logging()
    # A repeated main() in one process (a test, an embedding restart) must not
    # inherit the previous run's --timing state.
    _TIMING['on'] = False
    _TIMING['start'] = 0.0
    parser = build_parser()
    opts = parser.parse_args(argv)
    if opts.timing:
        _timing_on()
    profile, profile_error = _validate_profile(opts.terminal_profile)
    if profile_error:
        sys.stderr.write('INIT: invalid --terminal-profile: %s\n' % profile_error)
        return 2
    opts.terminal_profile = profile
    tracer = _build_tracers(opts)

    probe = probe_fn if probe_fn is not None else _probe_pcsc_reader
    readers = readers_fn if readers_fn is not None else _pcsc_readers
    started = time.time()
    try:
        reader = probe(reader=opts.reader, readers_fn=readers)
    except PcscUnavailable as exc:
        _report_pcsc_unavailable(exc.kind, exc.cause)
        return 2
    _tlog('PC/SC pre-flight: %.0fms' % ((time.time() - started) * 1000))
    sys.stderr.write('INIT: PC/SC reader: %s\n' % reader)

    factory = state_factory if state_factory is not None else _build_state
    started = time.time()
    state = factory(**_state_kwargs(opts, reader, tracer))
    _tlog('state construction: %.0fms' % ((time.time() - started) * 1000))
    try:
        _startup(state, opts)
    except Exception:
        # A reporting failure must not leave the process without a close.
        traceback.print_exc(file=sys.stderr)
        state.close()
        return 2

    started = time.time()
    build = http_factory if http_factory is not None else _default_http_factory()
    try:
        http = build(opts.http_host, opts.http_port, state,
                     web_dir=opts.web_dir, log_requests=opts.log_requests)
    except (OSError, OverflowError) as exc:
        # OverflowError: an out-of-range port that bypassed the parser's range
        # check (an embedding caller); socket.bind raises it, not OSError.
        sys.stderr.write('INIT: cannot listen on %s:%s (%s)\n'
                         % (opts.http_host, opts.http_port, exc))
        state.close()
        return 2
    _tlog('HTTP bind: %.0fms' % ((time.time() - started) * 1000))
    _print_banner(reader, state, http, opts)
    serve = serve_fn if serve_fn is not None else _serve
    try:
        return serve(state, http)
    finally:
        log.info('simple-server stopped')


def _default_http_factory():
    """``build_http_server``, imported lazily so ``--help`` needs no pyscard."""
    from simple_server.server import build_http_server
    return build_http_server


def _validate_profile(value):
    """Validate ``--terminal-profile`` with the endpoint's own rule."""
    from simple_server.server import validate_terminal_profile
    return validate_terminal_profile(value)


def _server_version(state):
    """The banner version: the state's own, else the package's."""
    version = getattr(state, 'version', None)
    if version:
        return version
    from simple_server.server import VERSION
    return VERSION


def _print_banner(reader, state, http, opts):
    """The listening banner (ASCII-safe through :func:`_console_rule`)."""
    address = getattr(http, 'server_address', None)
    if address and len(address) > 1:
        host, port = address[0], address[1]
    else:
        host, port = opts.http_host, opts.http_port
    rule = _console_rule()
    print(rule)
    print('  simple-server v%s listening on http://%s:%s'
          % (_server_version(state), host, port))
    print('  Reader: %s' % (reader or 'none'))
    print('  Open http://%s:%s in your browser for the SIMple UI '
          '(served by this server).' % (host, port))
    print(rule)
    # start.sh pipes stdout through tee: flush so the banner precedes the
    # (unbuffered, stderr) request log instead of trailing it.
    try:
        sys.stdout.flush()
    except (OSError, ValueError):
        pass


if __name__ == '__main__':
    sys.exit(main())
