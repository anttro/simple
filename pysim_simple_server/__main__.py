import argparse
import logging
import os
import sys
import time
import traceback
from http.server import HTTPServer
from pySim.card_handler import CardHandler
from pySim.commands import SimCardCommands
from pySim.exceptions import NoCardError
from pySim.log import PySimLogger
from pySim.cards import UiccCardBase

from .shell import load_pysim_app
from . import fastinit
from .server import PysimHandler, StderrApduTracer, _LoggingApduTracer, VERSION, _send_terminal_profile, _DefaultProactiveHandler, _handle_proactive_chain, _send_status, _init_proactive_session, _timing_on, _tlog, _set_menu_timeout, start_card_monitor, set_auto_equip, _read_iccid, _netstate_read, _netstate_install, _LineFilter


_server_start = 0

def _log_stdout(msg):
    elapsed = time.time() - _server_start
    os.write(1, ('[%8.3f] %s\n' % (elapsed, msg)).encode())


def _default_web_dir():
    # <repo>/frontend, whether run from source or an editable install.
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'frontend')


def _default_mcc_mnc_list():
    # Workspace default: the CC-BY-SA operator list lives outside the repo
    # (<workspace>/samples/mcc-mnc-list.json); clones can point elsewhere with
    # --mcc-mnc-list and the endpoint reports available=false when missing.
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.normpath(os.path.join(repo, '..', 'samples', 'mcc-mnc-list.json'))


def main():
    global _server_start
    _server_start = time.time()
    mod = load_pysim_app()
    parser = mod.option_parser
    parser.description = 'pysim-simple-server — HTTP API for pysim'
    parser.add_argument('--http-host', default='127.0.0.1', help='Bind address (default: 127.0.0.1)')
    parser.add_argument('--http-port', type=int, default=8080, help='Bind port (default: 8080)')
    parser.add_argument('--web-dir', default=_default_web_dir(), metavar='PATH',
                        help='Directory with the SIMple PWA static files to serve (default: <repo>/frontend)')
    parser.add_argument('--mcc-mnc-list', default=_default_mcc_mnc_list(), metavar='PATH',
                        help='Worldwide MCC/MNC operator list (JSON) for the network-simulation operator picker (default: <workspace>/samples/mcc-mnc-list.json)')
    parser.add_argument('--log-requests', action='store_true', default=False, help='Log request/response payloads to stderr')
    parser.add_argument('--sms-oa', default='12345', metavar='DIGITS',
                        help='TP-Originating-Address (SMSC number) for the SMS-DELIVER TPDU (default: 12345)')
    parser.add_argument('--sms-sm-sc', default='12345678912', metavar='DIGITS',
                        help='SM-SC address for SMS-SUBMIT routing in PoR-in-submit mode (default: 12345678912)')
    parser.add_argument('--terminal-profile',
                        default='FFFFFFFF7F9F00DFFF03021FE2000000C3FB000704117800710100000038428003',
                        metavar='HEX',
                        help='TERMINAL PROFILE payload (default: the 33-byte profile of a real BIP-capable handset - the live card only starts HTTP OTA when BIP events/commands are advertised)')
    parser.add_argument('--poll-interval', type=int, default=30, metavar='SECS',
                        help='Idle interval before automatic STATUS polling (1-255 seconds, default: 30). Disable with --poll-interval 0')
    parser.add_argument('--no-card-init', action='store_true', default=False,
                        help='Skip pysim card initialization (preserve CAT session — no file manager)')
    parser.add_argument('--timing', action='store_true', default=False,
                        help='Log phase durations, card resets and APDU counters with elapsed timestamps')
    parser.add_argument('--fast-init', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--full-pysim-init', action='store_true', default=False,
                        help="Use pysim's stock init_card/equip (multiple physical card resets) instead of the default reset-free fast init")
    parser.add_argument('--menu-timeout', type=int, default=60, metavar='SECS',
                        help='Auto-send a timeout TERMINAL RESPONSE if a paused STK command is not answered (default: 60, 0 disables)')
    parser.add_argument('--no-auto-equip', action='store_true', default=False,
                        help='Do not automatically initialize a card right after it is inserted (default: auto-equip on)')

    opts = parser.parse_args()
    opts.skip_card_init = opts.no_card_init
    opts.fast_init = not opts.full_pysim_init
    if opts.timing:
        _timing_on()
    if opts.menu_timeout is not None:
        _set_menu_timeout(opts.menu_timeout)
    set_auto_equip(not opts.no_auto_equip and not opts.skip_card_init)
    sl = None
    scc = None
    card = None
    rs = None
    sim_menu = None
    event_list = None
    # Auto-detect PC/SC reader if none was explicitly specified.
    # Handles late pcscd startup and USB enumeration delays.
    if opts.pcsc_dev is None and opts.pcsc_regex is None:
        try:
            from smartcard.System import readers
            for attempt in range(3):
                r = readers()
                if r:
                    sys.stderr.write('INIT: PC/SC reader detected: %s\n' % r[0])
                    opts.pcsc_dev = 0
                    break
                if attempt < 2:
                    sys.stderr.write('INIT: no PC/SC readers found, retrying in 2s...\n')
                    time.sleep(2)
        except Exception:
            pass  # smartcard module not available or pcscd unreachable

    try:
        kwargs = {}
        if opts.apdu_trace:
            kwargs['apdu_tracer'] = _LoggingApduTracer()
        t_phase = time.time()
        sl = mod.init_reader(opts, **kwargs)
        _tlog('init_reader: %.0fms' % ((time.time() - t_phase) * 1000))
        scc = SimCardCommands(sl)
        scc.cat_cla = '80'  # UICC CLA default; overridden for SIM after init_card
        scc._tp.proactive_handler = _DefaultProactiveHandler()
        t_phase = time.time()
        if opts.fast_init:
            try:
                rs, card = fastinit.init_card_fast(sl, opts.skip_card_init, wait=True)
            except NoCardError:
                # Normal cardless start: there was no card in the reader (the
                # 3s wait timed out). The presence monitor auto-equips once a
                # card appears; nothing to recover, no traceback.
                sys.stderr.write('INIT: no card in the reader — server ready; insert a card or press Equip\n')
            except Exception:
                print("Warning: fast card initialization failed, falling back to pysim init:", file=sys.stderr)
                traceback.print_exc()
                try:
                    rs, card = mod.init_card(sl, opts.skip_card_init)
                except NoCardError:
                    # The fallback retried the cardless wait; still a normal
                    # cardless start, not an initialization failure.
                    sys.stderr.write('INIT: no card in the reader — server ready; insert a card or press Equip\n')
        else:
            try:
                sl.wait_for_card(3)
                rs, card = mod.init_card(sl, opts.skip_card_init)
            except NoCardError:
                sys.stderr.write('INIT: no card in the reader — server ready; insert a card or press Equip\n')
        _tlog('card_init: %.0fms' % ((time.time() - t_phase) * 1000))
        if card is not None:
            scc.cat_cla = '80' if isinstance(card, UiccCardBase) else 'a0'
    except Exception:
        print("Warning: reader/card initialization failed:", file=sys.stderr)
        traceback.print_exc()
    ch = CardHandler(sl) if sl else None
    t_phase = time.time()
    try:
        if card is not None:
            app = mod.PysimApp(verbose=opts.verbose, card=card, rs=rs, sl=sl, ch=ch)
        else:
            # Cardless start: pySim logs 'Waiting for card...' (its own retry
            # path) and pySim-shell prints 'pySim-shell not equipped!'; we
            # report both cases with our own single line above. A PysimApp
            # without a card would also retry the cardless wait, so install
            # the filter before constructing it and drop the two internals.
            saved_stdout = sys.stdout
            sys.stdout = _LineFilter(saved_stdout, ('Waiting for card...', 'pySim-shell not equipped!'))
            try:
                app = mod.PysimApp(verbose=opts.verbose, card=None, rs=None, sl=sl, ch=ch)
            finally:
                sys.stdout = saved_stdout
    except Exception:
        print("Warning: PysimApp creation failed:", file=sys.stderr)
        traceback.print_exc()
        app = None
    _tlog('pysim_app: %.0fms' % ((time.time() - t_phase) * 1000))
    if app is not None and opts.fast_init:
        fastinit.install(app)
    iccid = None
    netstate_files = None
    if scc and card is not None and hasattr(scc, '_tp'):
        scc._tp.apdu_tracer = _LoggingApduTracer()
        try:
            _init_proactive_session()
            # Read EF.ICCID before the TERMINAL PROFILE starts the CAT session
            # (the PWA auto-selects the matching card preset from it).
            iccid = _read_iccid(app)
            if iccid:
                sys.stderr.write('INIT: ICCID %s\n' % iccid)
                # Network state monitor: read the network-related EFs in the
                # same CAT-free window (skipped without a readable ICCID).
                try:
                    netstate_files = _netstate_read(app)
                except Exception:
                    netstate_files = None
            t_phase = time.time()
            sys.stderr.write('INIT: sending TERMINAL PROFILE %s (CLA=%s)\n' % (opts.terminal_profile, scc.cat_cla))
            sm, el = _send_terminal_profile(scc, opts.terminal_profile)
            sys.stderr.write('INIT: TP done, menu=%s events=%s\n' % ('yes' if sm else 'no', 'yes' if el else 'no'))
            sim_menu = sm or sim_menu
            event_list = el or event_list
            for _ in range(3):
                st_data, st_sw = _send_status(scc)
                sys.stderr.write('INIT: drain STATUS -> %s\n' % st_sw)
                if not st_sw.startswith('91'):
                    break
                _handle_proactive_chain(scc, st_sw)
            _tlog('terminal_profile_drain: %.0fms' % ((time.time() - t_phase) * 1000))
        except Exception:
            traceback.print_exc(file=sys.stderr)
    if app is not None and opts.apdu_trace:
        # PysimApp.__init__ routes PySimLogger through app.poutput() (app.stdout)
        # and drops the root level to INFO. Re-route pysim's own APDU trace logging
        # directly to fd 1 so it survives the app.stdout/StringIO redirection in the
        # HTTP handlers and the INFO level suppression.
        PySimLogger.setup(print_callback=_log_stdout)
        PySimLogger.set_level(logging.DEBUG)
        # PysimApp.__init__ and every `equip` wipe the transport apdu_tracer
        # (_onchange_apdu_trace sets it to None). Re-attach our tracer and make
        # sure it stays attached across equip/re-equip.
        tracer = _LoggingApduTracer()
        def _reattach_tracer():
            if app.card:
                app.card._scc._tp.apdu_tracer = tracer
        _reattach_tracer()
        orig_onchange = app._onchange_apdu_trace
        def _onchange_apdu_trace(param_name, old, new):
            orig_onchange(param_name, old, new)
            _reattach_tracer()
        app._onchange_apdu_trace = _onchange_apdu_trace
    server = HTTPServer((opts.http_host, opts.http_port), PysimHandler)
    server.sl = sl
    server.scc = scc
    server.card = card
    server.rs = rs
    server.app = app
    server.sms_oa = opts.sms_oa
    server.sms_sc = opts.sms_sm_sc
    server.log_requests = opts.log_requests
    server.terminal_profile = opts.terminal_profile
    server.cli_terminal_profile = opts.terminal_profile
    server.web_dir = opts.web_dir
    server.mcc_mnc_path = opts.mcc_mnc_list
    server.sim_menu = sim_menu
    server.event_list = event_list
    server.menu_active = False
    server.stk_pending = None
    server.card_present = card is not None
    server.card_session = 1 if card is not None else 0
    server.iccid = iccid
    # Network state monitor: install the state read during the startup init
    # (right after the ICCID, before the TERMINAL PROFILE).  No readable
    # ICCID means the card is considered unusable - give up.
    try:
        _netstate_install(server, netstate_files if iccid else None)
    except Exception as e:
        server.net_state = None
        sys.stderr.write('INIT: network state failed: %s\n' % e)
    server.equipping = False
    # Set server reference for polling timer and mark the card session state
    import pysim_simple_server.server
    pysim_simple_server.server._server_ref = server
    pysim_simple_server.server._CARD_CONNECTED = card is not None
    if opts.poll_interval is not None:
        pysim_simple_server.server._set_poll_interval(opts.poll_interval)
    # Auto-enable polling if card initialized successfully (unless interval is 0)
    if server.scc and server.card and opts.poll_interval != 0:
        pysim_simple_server.server._poll_enable()
    # Start presence monitoring only after the startup init: pyscard reports an
    # already-present card as "added" on the first pass, and we must not
    # auto-equip over a session we just initialized. If startup init failed,
    # that event triggers auto-equip instead — the desired retry.
    if sl is not None and getattr(sl, '_reader', None) is not None:
        start_card_monitor(str(sl._reader))
    print("─" * 70)
    print("  pysim-simple-server v%s listening on http://%s:%s" % (VERSION, opts.http_host, opts.http_port))
    print("  Open http://%s:%s in your browser for the SIMple UI (served by this server)."
          % (opts.http_host, opts.http_port))
    print("─" * 70)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.shutdown()


if __name__ == '__main__':
    main()