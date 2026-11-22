# coding=utf-8
"""Tests for the ``simple-server`` entry point and the GSMTAP-SIM tracer.

No card reader and no listening socket are involved: the PC/SC pre-flight is
driven through an injected readers function, the state through an injected
factory, the HTTP server through an injected factory and the serving step
through an injected callable.  The GSMTAP packet form is byte-pinned against
the libosmocore header layout (16 bytes: version 2, ``hdr_len`` 4, type 0x04);
the comparison against the legacy builder lives in ``tests/test_simple_drift.py``
(the one module allowed to import both implementations - this file must stay
pysim-free, like the package it tests).
"""

import contextlib
import inspect
import io
import logging
import os
import pathlib
import signal
import struct
import sys
import tempfile
import unittest
from unittest import mock

from simple_server import __main__ as m  # noqa: E402
from simple_server import gsmtap  # noqa: E402
from simple_server import server as server_module  # noqa: E402
from simple_server import transport  # noqa: E402

ATR = bytes.fromhex('3b9f95801fc78031e073fe211b67a3a0001505017204')


# ---- helpers -----------------------------------------------------------------

class FakeExc(Exception):
    """A pyscard-shaped exception (the hresult is the reliable signal)."""

    def __init__(self, hresult=None, message=''):
        super().__init__(message)
        self.hresult = hresult


class FakeState(object):
    """A ``ServerState`` stand-in: no transport, no threads, no card."""

    version = '9.9.9-test'

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.opened = 0
        self.closed = 0
        self.skip_card_init = None
        self.iccid = None
        self.error = None

    def open(self, skip_card_init=False):
        self.opened += 1
        self.skip_card_init = skip_card_init
        if self.error is not None:
            raise self.error
        return {'connected': True}

    def close(self):
        self.closed += 1


class FakeHttp(object):
    """A ``ThreadingHTTPServer`` stand-in recording the serve lifecycle."""

    def __init__(self, address=('127.0.0.1', 8080), interrupt=False):
        self.server_address = address
        self.interrupt = interrupt
        self.served = 0
        self.shutdowns = 0
        self.closed = 0

    def serve_forever(self):
        self.served += 1
        if self.interrupt:
            raise KeyboardInterrupt

    def shutdown(self):
        self.shutdowns += 1

    def server_close(self):
        self.closed += 1


class Recorder(object):
    """A GSMTAP sender stand-in recording the APDU and ATR payloads."""

    def __init__(self):
        self.apdus = []
        self.atrs = []

    def send_apdu(self, data, slot_nr=0):
        self.apdus.append(bytes(data))

    def send_atr(self, atr, slot_nr=0):
        self.atrs.append(bytes(atr))


class FakeLink(object):
    """A link with the ``send_tpdu``/``connect``/``reset_card`` shape."""

    def __init__(self, answers=None, atr=ATR, reader_name='Fake reader'):
        self.answers = list(answers or [])
        self.atr = atr
        self.reader_name = reader_name
        self.sent = []
        self.connects = 0
        self.resets = 0
        self.connected = False

    def send_tpdu(self, tpdu):
        self.sent.append(bytes(tpdu))
        if self.answers:
            return self.answers.pop(0)
        return b'\x90\x00', b'\x90\x00'

    def send_apdu(self, apdu):
        """The logical exchange (what the card/file code calls)."""
        return self.send_tpdu(apdu)

    def connect(self):
        self.connects += 1
        self.connected = True
        return self

    def reset_card(self):
        self.resets += 1
        # The real PcscLink power-cycles through ``self.connect()``, so the
        # wrapped connect is what emits the post-reset ATR.
        self.connect()
        return 1

    def get_atr(self):
        if not self.connected:
            raise transport.ReaderError('PC/SC link is not connected')
        return self.atr


# ---- the CLI -----------------------------------------------------------------

class ParserTests(unittest.TestCase):
    def parse(self, *args):
        return m.build_parser().parse_args(list(args))

    def test_defaults(self):
        opts = self.parse()
        self.assertIsNone(opts.reader)
        self.assertEqual(opts.http_host, '127.0.0.1')
        self.assertEqual(opts.http_port, 8080)
        self.assertTrue(opts.web_dir.endswith('frontend'), opts.web_dir)
        self.assertTrue(opts.mcc_mnc_list.endswith('mcc-mnc-list.json'),
                        opts.mcc_mnc_list)
        self.assertIsNone(opts.card_presets)
        self.assertIsNone(opts.test_scripts)
        self.assertIsNone(opts.test_suites)
        self.assertEqual(opts.sms_oa, '12345')
        self.assertEqual(opts.sms_sm_sc, '12345678912')
        self.assertEqual(opts.terminal_profile, m.DEFAULT_TERMINAL_PROFILE)
        self.assertEqual(opts.poll_interval, 30)
        self.assertEqual(opts.menu_timeout, 60)
        self.assertFalse(opts.no_card_init)
        self.assertFalse(opts.no_auto_equip)
        self.assertFalse(opts.apdu_trace)
        self.assertIsNone(opts.gsmtap)
        self.assertFalse(opts.log_requests)
        self.assertFalse(opts.timing)

    def test_each_option(self):
        opts = self.parse(
            '-r', 'Reader X',
            '--http-host', '0.0.0.0',
            '--http-port', '9090',
            '--web-dir', '/w',
            '--mcc-mnc-list', '/m.json',
            '--card-presets', '/p.json',
            '--test-scripts', '/s.json',
            '--test-suites', '/u.json',
            '--sms-oa', '11111',
            '--sms-sm-sc', '22222',
            '--terminal-profile', 'FFFF',
            '--poll-interval', '5',
            '--menu-timeout', '0',
            '--no-card-init',
            '--no-auto-equip',
            '--apdu-trace',
            '--gsmtap', '10.0.0.5:5000',
            '--log-requests',
            '--timing')
        self.assertEqual(opts.reader, 'Reader X')
        self.assertEqual(opts.http_host, '0.0.0.0')
        self.assertEqual(opts.http_port, 9090)
        self.assertEqual(opts.web_dir, '/w')
        self.assertEqual(opts.mcc_mnc_list, '/m.json')
        self.assertEqual(opts.card_presets, '/p.json')
        self.assertEqual(opts.test_scripts, '/s.json')
        self.assertEqual(opts.test_suites, '/u.json')
        self.assertEqual(opts.sms_oa, '11111')
        self.assertEqual(opts.sms_sm_sc, '22222')
        self.assertEqual(opts.terminal_profile, 'FFFF')
        self.assertEqual(opts.poll_interval, 5)
        self.assertEqual(opts.menu_timeout, 0)
        self.assertTrue(opts.no_card_init)
        self.assertTrue(opts.no_auto_equip)
        self.assertTrue(opts.apdu_trace)
        self.assertEqual(opts.gsmtap, '10.0.0.5:5000')
        self.assertTrue(opts.log_requests)
        self.assertTrue(opts.timing)

    def test_the_reader_long_option(self):
        self.assertEqual(self.parse('--reader', 'R').reader, 'R')

    def test_a_bare_gsmtap_uses_the_default_target(self):
        self.assertEqual(self.parse('--gsmtap').gsmtap, gsmtap.DEFAULT_TARGET)

    def test_help_renders(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit) as raised:
                m.build_parser().parse_args(['--help'])
        self.assertEqual(raised.exception.code, 0)
        text = out.getvalue()
        self.assertIn('usage: simple-server', text)
        for option in ('--reader', '--http-port', '--web-dir', '--gsmtap',
                       '--no-card-init', '--no-auto-equip', '--timing'):
            self.assertIn(option, text)

    def test_the_terminal_profile_default_matches_the_server(self):
        # Two literals (the parser must render --help without importing the
        # pyscard-touching server module): a drift guard keeps them equal.
        self.assertEqual(m.DEFAULT_TERMINAL_PROFILE,
                         server_module.DEFAULT_TERMINAL_PROFILE)

    def test_the_http_port_range_is_validated_by_the_parser(self):
        # socket.bind raises OverflowError (not OSError) for these, so the
        # parser refuses them with the friendly CLI error before any state.
        for value in ('65536', '99999', '-1'):
            with self.subTest(port=value):
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    with self.assertRaises(SystemExit) as raised:
                        self.parse('--http-port', value)
                self.assertEqual(raised.exception.code, 2)
                self.assertIn('port must be 0-65535', err.getvalue())
        self.assertEqual(self.parse('--http-port', '0').http_port, 0)
        self.assertEqual(self.parse('--http-port', '65535').http_port, 65535)


# ---- the PC/SC pre-flight ----------------------------------------------------

class PcscErrorKindTests(unittest.TestCase):
    def test_access_denied_by_hresult(self):
        self.assertEqual(m._pcsc_error_kind(FakeExc(0x80100027)), 'access')

    def test_access_denied_by_message(self):
        self.assertEqual(m._pcsc_error_kind(FakeExc(None, 'Access denied.')),
                         'access')

    def test_service_errors(self):
        self.assertEqual(m._pcsc_error_kind(FakeExc(0x8010001D)), 'service')
        self.assertEqual(m._pcsc_error_kind(FakeExc(0x8010001E)), 'service')

    def test_reader_errors(self):
        self.assertEqual(m._pcsc_error_kind(FakeExc(0x8010002E)), 'reader')
        self.assertEqual(m._pcsc_error_kind(FakeExc(0x80100009)), 'reader')

    def test_other_failures_are_not_classified(self):
        self.assertIsNone(m._pcsc_error_kind(FakeExc(0x8010000B)))  # sharing
        self.assertIsNone(m._pcsc_error_kind(ValueError('boom')))


class ProbeTests(unittest.TestCase):
    def test_the_first_reader_is_returned(self):
        self.assertEqual(
            m._probe_pcsc_reader(readers_fn=lambda: ['Reader X']), 'Reader X')

    def test_a_named_reader_is_selected(self):
        self.assertEqual(
            m._probe_pcsc_reader(readers_fn=lambda: ['A', 'B'], reader='B'),
            'B')

    def test_the_reader_name_is_stripped_once(self):
        # The pre-flight and the state normalise the same way; comparing the
        # raw value here would report a present reader as missing.
        self.assertEqual(
            m._probe_pcsc_reader(readers_fn=lambda: ['A'], reader='  A  '),
            'A')
        self.assertEqual(
            m._probe_pcsc_reader(readers_fn=lambda: ['A'], reader='   '),
            'A')

    def test_a_missing_named_reader_is_a_reader_error(self):
        with self.assertRaises(m.PcscUnavailable) as raised:
            m._probe_pcsc_reader(readers_fn=lambda: ['A'], reader='B')
        self.assertEqual(raised.exception.kind, 'reader')
        self.assertIn('B', str(raised.exception.cause))
        self.assertIn('A', str(raised.exception.cause))

    def test_access_denied_is_raised_immediately(self):
        calls = []

        def denied():
            calls.append(1)
            raise FakeExc(0x80100027)

        with self.assertRaises(m.PcscUnavailable) as raised:
            m._probe_pcsc_reader(retries=3, sleep_fn=lambda s: None,
                                 readers_fn=denied)
        self.assertEqual(raised.exception.kind, 'access')
        self.assertEqual(len(calls), 1)      # permissions do not fix themselves

    def test_an_empty_list_is_retried_then_reported(self):
        calls = []

        def empty():
            calls.append(1)
            return []

        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(m.PcscUnavailable) as raised:
                m._probe_pcsc_reader(retries=2, sleep_fn=lambda s: None,
                                     readers_fn=empty)
        self.assertEqual(raised.exception.kind, 'reader')
        self.assertEqual(len(calls), 2)

    def test_a_transient_failure_is_retried(self):
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) == 1:
                raise FakeExc(0x8010000B)    # sharing violation
            return ['Reader Y']

        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(
                m._probe_pcsc_reader(retries=2, sleep_fn=lambda s: None,
                                     readers_fn=flaky), 'Reader Y')
        self.assertEqual(len(calls), 2)


class ReportTests(unittest.TestCase):
    def _report(self, kind, exc=None):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            m._report_pcsc_unavailable(kind, exc)
        return err.getvalue()

    def test_access_guidance(self):
        text = self._report('access', FakeExc(0x80100027, 'Access denied.'))
        low = text.lower()
        self.assertIn('access denied', low)
        self.assertIn('pcscd', low)
        # polkit, not the socket group, is the gate (the socket may be 0666)
        self.assertIn('polkit', low)
        self.assertIn('org.debian.pcsc-lite.access_pcsc', low)
        self.assertIn('org.debian.pcsc-lite.access_card', low)
        self.assertIn('99-pcscd-allow.rules', low)
        self.assertIn('0666', low)
        self.assertIn('pcsc_scan', low)
        self.assertIn('0x80100027', text)

    def test_service_guidance(self):
        text = self._report('service')
        self.assertIn('pcscd is not running', text)
        self.assertIn('systemctl start pcscd', text)

    def test_reader_guidance(self):
        text = self._report('reader')
        self.assertIn('no PC/SC reader', text)
        self.assertIn('pcsc_scan', text)


# ---- the startup -------------------------------------------------------------

def ok_probe(reader=None, readers_fn=None):
    return 'Reader X'


def failing_probe(kind, exc=None):
    def probe(reader=None, readers_fn=None):
        raise m.PcscUnavailable(kind, exc)
    return probe


class MainResult(object):
    """The result of one :meth:`StartupTests.run_main` invocation."""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class StartupTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(m, 'configure_logging')
        self.logging = patcher.start()
        self.addCleanup(patcher.stop)
        # --timing writes to stderr; keep the tests independent of its state.
        patcher = mock.patch.dict(m._TIMING, {'on': False, 'start': 0.0})
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_main(self, argv, state=None, probe_fn=ok_probe, http=None,
                 serve_fn=None):
        state = state if state is not None else FakeState()
        http = http if http is not None else FakeHttp()
        created = []

        def factory(**kwargs):
            created.append(kwargs)
            return state

        served = []
        if serve_fn is None:
            def serve_fn(target_state, target_http):
                served.append((target_state, target_http))
                return 0
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = m.main(
                argv, probe_fn=probe_fn,
                readers_fn=lambda: ['Reader X'], state_factory=factory,
                http_factory=lambda *a, **k: http, serve_fn=serve_fn)
        return MainResult(code=code, out=out.getvalue(), err=err.getvalue(),
                          created=created, state=state, http=http,
                          served=served)

    def test_the_startup_sequence(self):
        result = self.run_main([])
        self.assertEqual(result.code, 0)
        self.assertEqual(result.state.opened, 1)
        self.assertEqual(result.served, [(result.state, result.http)])
        self.assertIn('INIT: PC/SC reader: Reader X', result.err)
        self.assertIn('INIT: card ready', result.err)
        self.assertIn('listening on http://127.0.0.1:8080', result.out)
        self.assertIn('simple-server v9.9.9-test', result.out)
        # without -r the reader the pre-flight found is the one the state uses
        self.assertEqual(result.created[0]['reader'], 'Reader X')
        self.logging.assert_called_once()

    def test_a_cardless_start_is_one_line_and_no_traceback(self):
        state = FakeState()
        state.error = transport.ReaderError('No card in the reader')
        result = self.run_main([], state=state)
        self.assertEqual(result.code, 0)
        self.assertEqual(state.opened, 1)
        self.assertIn('INIT: no card in the reader - server ready; insert a '
                      'card or press Equip', result.err)
        self.assertNotIn('Traceback', result.err)
        self.assertNotIn('card initialization failed', result.err)
        # the server still starts and serves
        self.assertEqual(result.served, [(state, result.http)])

    def test_a_real_init_failure_keeps_serving(self):
        state = FakeState()
        state.error = RuntimeError('card classified as nothing')
        with self.assertLogs('simple_server.__main__', level='INFO') as logs:
            result = self.run_main([], state=state)
        self.assertEqual(result.code, 0)
        self.assertIn('INIT: card initialization failed', result.err)
        self.assertNotIn('Traceback', result.err)     # the traceback is logged
        self.assertTrue(any('card initialization traceback' in line
                            for line in logs.output), logs.output)
        self.assertEqual(result.served, [(state, result.http)])

    def test_a_card_removed_during_init_is_a_removal_not_a_failure(self):
        # v3.32.5: a PC/SC error during the startup init means the card went
        # away - one line, no traceback, the server stays ready.
        state = FakeState()
        state.error = transport.ReaderError('card removed (0x80100069)')
        result = self.run_main([], state=state)
        self.assertEqual(result.code, 0)
        self.assertIn('INIT: card removed during initialization - server ready',
                      result.err)
        self.assertNotIn('card initialization failed', result.err)
        self.assertNotIn('Traceback', result.err)
        self.assertEqual(result.served, [(state, result.http)])

    def test_no_card_init_skips_only_the_phase(self):
        # The flag skips the initialization phase, not the process's card
        # plumbing: open() is still called (with skip_card_init=True), so the
        # link, the ATR, the poll thread and the presence monitor exist.
        result = self.run_main(['--no-card-init'])
        self.assertEqual(result.code, 0)
        self.assertEqual(result.state.opened, 1)
        self.assertIs(result.state.skip_card_init, True)
        self.assertIn('INIT: card initialization skipped (--no-card-init)',
                      result.err)
        self.assertEqual(result.served, [(result.state, result.http)])

    def test_the_state_receives_the_cli_options(self):
        result = self.run_main([
            '-r', 'Reader X', '--http-port', '9091', '--web-dir', '/w',
            '--card-presets', '/p.json', '--test-scripts', '/s.json',
            '--test-suites', '/u.json', '--sms-oa', '111',
            '--sms-sm-sc', '222', '--terminal-profile', 'FFFF',
            '--poll-interval', '7', '--menu-timeout', '9',
            '--mcc-mnc-list', '/m.json', '--no-auto-equip'])
        kwargs = result.created[0]
        self.assertEqual(kwargs['reader'], 'Reader X')
        self.assertEqual(kwargs['presets_path'], '/p.json')
        self.assertEqual(kwargs['test_scripts_path'], '/s.json')
        self.assertEqual(kwargs['test_suites_path'], '/u.json')
        self.assertEqual(kwargs['web_dir'], '/w')
        self.assertEqual(kwargs['mcc_mnc_path'], '/m.json')
        self.assertEqual(kwargs['terminal_profile'], 'FFFF')
        self.assertEqual(kwargs['poll_interval'], 7)
        self.assertEqual(kwargs['menu_timeout'], 9)
        self.assertEqual(kwargs['sms_oa'], '111')
        self.assertEqual(kwargs['sms_sc'], '222')
        self.assertFalse(kwargs['auto_equip'])
        self.assertTrue(callable(kwargs['transport_factory']))

    def test_auto_equip_defaults_on_but_off_with_no_card_init(self):
        self.assertTrue(self.run_main([]).created[0]['auto_equip'])
        self.assertFalse(
            self.run_main(['--no-card-init']).created[0]['auto_equip'])

    def test_main_resets_the_timing_state(self):
        # A repeated main() in one process must not inherit --timing.
        m._TIMING.update({'on': True, 'start': 1.0})
        result = self.run_main([])
        self.assertEqual(result.code, 0)
        self.assertFalse(m._TIMING['on'])
        self.assertEqual(m._TIMING['start'], 0.0)
        self.assertNotIn('TIMING', result.err)

    def test_the_http_factory_receives_the_options(self):
        seen = {}

        def http_factory(host, port, state, **kwargs):
            seen.update({'host': host, 'port': port, 'state': state,
                         'kwargs': kwargs})
            return FakeHttp(address=(host, port))

        state = FakeState()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            m.main(['--http-host', '0.0.0.0', '--http-port', '9092',
                    '--log-requests'], probe_fn=ok_probe,
                   readers_fn=lambda: ['Reader X'],
                   state_factory=lambda **k: state,
                   http_factory=http_factory,
                   serve_fn=lambda s, h: 0)
        self.assertEqual(seen['host'], '0.0.0.0')
        self.assertEqual(seen['port'], 9092)
        self.assertIs(seen['state'], state)
        self.assertEqual(seen['kwargs']['web_dir'], m._default_web_dir())
        self.assertTrue(seen['kwargs']['log_requests'])

    def test_a_pcsc_access_denial_exits_two_before_the_state(self):
        result = self.run_main(
            [], probe_fn=failing_probe('access', FakeExc(0x80100027,
                                                         'Access denied.')))
        self.assertEqual(result.code, 2)
        self.assertIn('PC/SC access denied', result.err)
        self.assertIn('polkit', result.err)
        self.assertEqual(result.created, [])
        self.assertEqual(result.served, [])

    def test_no_reader_exits_two(self):
        def probe(reader=None, readers_fn=None):
            raise m.PcscUnavailable('reader')
        result = self.run_main([], probe_fn=probe)
        self.assertEqual(result.code, 2)
        self.assertIn('no PC/SC reader found', result.err)
        self.assertEqual(result.created, [])

    def test_a_named_reader_goes_to_the_probe(self):
        seen = {}

        def probe(reader=None, readers_fn=None):
            seen['reader'] = reader
            return reader or 'first'
        result = self.run_main(['-r', 'Reader Z'], probe_fn=probe)
        self.assertEqual(seen['reader'], 'Reader Z')
        self.assertEqual(result.created[0]['reader'], 'Reader Z')

    def test_an_invalid_terminal_profile_exits_two(self):
        result = self.run_main(['--terminal-profile', 'XYZ'])
        self.assertEqual(result.code, 2)
        self.assertIn('invalid --terminal-profile', result.err)
        self.assertEqual(result.created, [])

    def test_a_bad_gsmtap_target_only_disables_the_stream(self):
        result = self.run_main(['--gsmtap', 'host:not-a-port'])
        self.assertEqual(result.code, 0)
        self.assertIn('GSMTAP: disabled', result.err)
        self.assertEqual(result.state.opened, 1)

    def test_a_good_gsmtap_target_announces_the_stream(self):
        tracers = []
        real_build = m._build_tracers

        def build(opts, stderr=None):
            tracer = real_build(opts, stderr=stderr)
            tracers.append(tracer)
            return tracer

        with mock.patch.object(m, '_build_tracers', side_effect=build):
            result = self.run_main(['--gsmtap', '127.0.0.1:4730'])
        try:
            self.assertIn('GSMTAP: streaming APDUs to 127.0.0.1:4730',
                          result.err)
        finally:
            for tracer in tracers:
                tracer.sender.close()

    def test_the_banner_reports_the_bound_port(self):
        http = FakeHttp(address=('127.0.0.1', 43210))
        result = self.run_main(['--http-port', '0'], http=http)
        self.assertIn('listening on http://127.0.0.1:43210', result.out)

    def test_a_bind_failure_reports_and_closes_the_state(self):
        def broken(*args, **kwargs):
            raise OSError('Address already in use')

        out, err = io.StringIO(), io.StringIO()
        state = FakeState()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = m.main([], probe_fn=ok_probe,
                          readers_fn=lambda: ['Reader X'],
                          state_factory=lambda **k: state,
                          http_factory=broken,
                          serve_fn=lambda s, h: 0)
        self.assertEqual(code, 2)
        self.assertIn('cannot listen on 127.0.0.1:8080', err.getvalue())
        self.assertIn('Address already in use', err.getvalue())
        self.assertEqual(state.closed, 1)

    def test_an_overflowing_port_is_the_same_friendly_failure(self):
        # An embedding caller can bypass the parser's range check; socket.bind
        # raises OverflowError then, which the catch must treat like OSError.
        def broken(*args, **kwargs):
            raise OverflowError('getsockaddrarg: port must be 0-65535.')

        out, err = io.StringIO(), io.StringIO()
        state = FakeState()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = m.main([], probe_fn=ok_probe,
                          readers_fn=lambda: ['Reader X'],
                          state_factory=lambda **k: state,
                          http_factory=broken,
                          serve_fn=lambda s, h: 0)
        self.assertEqual(code, 2)
        self.assertIn('cannot listen on', err.getvalue())
        self.assertIn('port must be 0-65535', err.getvalue())
        self.assertNotIn('Traceback', err.getvalue())
        self.assertEqual(state.closed, 1)


class StateContractTests(unittest.TestCase):
    def test_the_cli_kwargs_build_a_real_cardless_state(self):
        # The fakes above hide a keyword typo; build the real ServerState
        # (cardless: nothing connects, no thread starts) from the same kwargs
        # and close it.  The paths go to a temp dir so no user store is touched.
        with tempfile.TemporaryDirectory() as tmp:
            opts = m.build_parser().parse_args([
                '--card-presets', os.path.join(tmp, 'p.json'),
                '--test-scripts', os.path.join(tmp, 's.json'),
                '--test-suites', os.path.join(tmp, 'u.json')])
            kwargs = m._state_kwargs(opts, 'Fake reader', None)
            state = server_module.ServerState(**kwargs)
            try:
                self.assertFalse(state.connected)
                self.assertEqual(state.reader_name, 'Fake reader')
                self.assertEqual(state.terminal_profile,
                                 m.DEFAULT_TERMINAL_PROFILE)
                self.assertIsNone(state.card)
            finally:
                state.close()

    def test_the_default_web_dir_is_the_repo_frontend(self):
        repo = pathlib.Path(__file__).resolve().parents[1]
        self.assertEqual(pathlib.Path(m._default_web_dir()),
                         repo / 'frontend')


class NoCardInitStateTests(unittest.TestCase):
    """``--no-card-init`` against the real ``ServerState``.

    The flag skips the initialization *phase*: the link is built, the ATR
    read and both background threads run, so an Equip later works and a card
    removal is still noticed (the legacy contract).
    """

    def setUp(self):
        patcher = mock.patch.object(m, 'configure_logging')
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.installed = []
        self.link = FakeLink()
        self.state = server_module.ServerState(
            transport=self.link,
            transport_factory=lambda name: FakeLink(),
            reader='Fake reader',
            presence_install=self.installed.append,
            auto_equip=False,
            presets_path=os.path.join(self.tmp.name, 'p.json'),
            test_scripts_path=os.path.join(self.tmp.name, 's.json'),
            test_suites_path=os.path.join(self.tmp.name, 'u.json'))
        self.addCleanup(self.state.close)

    def test_the_phase_is_skipped_but_the_card_plumbing_runs(self):
        self.state.open(skip_card_init=True)
        self.assertTrue(self.state.connected)
        self.assertEqual(self.state.card.kind, 'unknown')
        self.assertEqual(self.state.atr, ATR.hex())
        self.assertIsNone(self.state.iccid)
        # not a full initialization, so no "the equip finished" marker
        self.assertEqual(self.state.card_ready_session, 0)
        # both background threads/observers are running
        self.assertIsNotNone(self.state._poll_thread)
        self.assertTrue(self.state._poll_thread.is_alive())
        self.assertEqual(self.installed, [self.state._presence_observer])
        self.assertIsNotNone(self.state._presence_observer)

    def test_the_poll_still_sends_a_status(self):
        self.state.open(skip_card_init=True)
        self.link.sent[:] = []
        self.assertIsNotNone(self.state.poll_once())
        self.assertTrue(any(b'\xf2' in apdu for apdu in self.link.sent),
                        [apdu.hex() for apdu in self.link.sent])

    def test_the_monitor_still_sees_a_removal_and_an_equip_works(self):
        self.state.open(skip_card_init=True)
        card = mock.Mock(reader='Fake reader')
        self.state.handle_presence(removed=[card])
        self.assertFalse(self.state.connected)
        self.assertIsNone(self.state.transport)     # released, a rebuild follows
        self.state.equip()
        self.assertTrue(self.state.connected)
        self.assertNotEqual(self.state.card.kind, 'unknown')
        self.assertEqual(self.state.card_ready_session, self.state.card_session)

    def test_a_main_run_with_the_real_state_serves(self):
        # The end-to-end path: main() -> the real ServerState.open() (with the
        # injected link) -> the HTTP factory -> the serve step.
        out, err = io.StringIO(), io.StringIO()
        served = []
        http = FakeHttp()

        def serve(state, target_http):
            served.append((state, target_http))
            return 0

        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = m.main([], probe_fn=ok_probe,
                          readers_fn=lambda: ['Fake reader'],
                          state_factory=lambda **k: self.state,
                          http_factory=lambda *a, **k: http,
                          serve_fn=serve)
        self.assertEqual(code, 0)
        self.assertEqual(served, [(self.state, http)])
        self.assertTrue(self.state.connected)       # the real init ran
        self.assertIn('INIT: card ready', err.getvalue())
        self.assertIn('listening on http://127.0.0.1:8080', out.getvalue())


# ---- the serving step --------------------------------------------------------

class ServeTests(unittest.TestCase):
    def serve(self, interrupt=False):
        state, http = FakeState(), FakeHttp(interrupt=interrupt)
        with mock.patch.object(m.signal, 'signal'):     # never touch the process
            code = m._serve(state, http)
        return code, state, http

    def test_serve_forever_then_close(self):
        code, state, http = self.serve()
        self.assertEqual(code, 0)
        self.assertEqual(http.served, 1)
        self.assertEqual(http.shutdowns, 0)
        self.assertEqual(http.closed, 1)
        self.assertEqual(state.closed, 1)

    def test_keyboard_interrupt_is_a_clean_shutdown(self):
        out = io.StringIO()
        state, http = FakeState(), FakeHttp(interrupt=True)
        with mock.patch.object(m.signal, 'signal'), \
                contextlib.redirect_stdout(out):
            code = m._serve(state, http)
        self.assertEqual(code, 0)
        self.assertIn('Shutting down...', out.getvalue())
        self.assertEqual(state.closed, 1)

    def test_a_close_failure_does_not_escape(self):
        state, http = FakeState(), FakeHttp()

        def broken():
            raise OSError('boom')
        state.close = broken
        with mock.patch.object(m.signal, 'signal'), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(m._serve(state, http), 0)

    def test_the_signal_handler_shuts_the_server_down(self):
        http = FakeHttp()
        installed = {}

        def capture(signum, handler):
            installed[signum] = handler
        with mock.patch.object(m.signal, 'signal', side_effect=capture):
            m._install_signal_handlers(http)
        self.assertTrue(installed)
        # Calling the handler spawns the shutdown thread (never on the thread
        # that is inside serve_forever).
        handler = list(installed.values())[0]
        with mock.patch.object(m.threading, 'Thread') as thread:
            handler(2, None)
        thread.assert_called_once()
        self.assertTrue(thread.call_args.kwargs.get('daemon'))

    def test_both_sigint_and_sigterm_are_installed(self):
        installed = {}

        def capture(signum, handler):
            installed[signum] = handler
        with mock.patch.object(m.signal, 'signal', side_effect=capture):
            m._install_signal_handlers(FakeHttp())
        self.assertIn(signal.SIGINT, installed)
        self.assertIn(signal.SIGTERM, installed)


# ---- the console rule --------------------------------------------------------

def _pipe(encoding='cp1251'):
    raw = io.BytesIO()
    return io.TextIOWrapper(raw, encoding=encoding), raw


class ConsoleTests(unittest.TestCase):
    def setUp(self):
        self._stdout = sys.stdout
        self.addCleanup(setattr, sys, 'stdout', self._stdout)

    def test_the_rule_falls_back_to_ascii_on_a_cp1251_pipe(self):
        out, _raw = _pipe()
        sys.stdout = out
        rule = m._console_rule()
        self.assertEqual(rule, '-' * 70)
        self.assertTrue(rule.isascii())

    def test_the_rule_stays_pretty_on_a_utf8_stream(self):
        out, _raw = _pipe(encoding='utf-8')
        sys.stdout = out
        self.assertEqual(m._console_rule(), '\u2500' * 70)

    def test_the_guard_tolerates_streams_without_reconfigure(self):
        sys.stdout = io.StringIO()
        m._guard_console_streams()                     # must not raise
        print('still works')
        self.assertEqual(sys.stdout.getvalue(), 'still works\n')

    def test_the_entry_point_sources_are_ascii(self):
        # The console text layer's rule (v3.30.3).  The legacy package pins it
        # with an AST walk over the print/stdout/stderr literals
        # (tests/test_console_encoding.py); here the *whole* two entry-point
        # files are ASCII, which is stricter and costs nothing - the rule is
        # local to these two modules (they own the console output).
        for name in ('__main__.py', 'gsmtap.py'):
            path = pathlib.Path(m.__file__).parent / name
            path.read_bytes().decode('ascii')          # raises on a literal

    def test_main_guards_the_streams_and_configures_logging(self):
        src = inspect.getsource(m.main)
        lines = [line.strip() for line in src.splitlines()]
        # The guard is the first statement of main(), the logging setup the
        # next one (v3.30.3: an unencodable character must never kill the
        # process before the diagnostics are configured).
        guard = lines.index('_guard_console_streams()')
        self.assertEqual(lines[guard + 1], 'configure_logging()')


# ---- logging -----------------------------------------------------------------

class LoggingTests(unittest.TestCase):
    def setUp(self):
        root = logging.getLogger()
        self._handlers = root.handlers[:]
        self._level = root.level
        self._named = {
            name: logging.getLogger(name).level
            for name in ('simple_server',) + m.THIRD_PARTY_LOGGERS}
        self.stream = io.StringIO()

    def tearDown(self):
        root = logging.getLogger()
        for handler in root.handlers[:]:
            if handler not in self._handlers:
                handler.close()
        root.handlers[:] = self._handlers
        root.setLevel(self._level)
        for name, level in self._named.items():
            logging.getLogger(name).setLevel(level)

    def test_simple_server_info_reaches_the_stream(self):
        m.configure_logging(stream=self.stream)
        logging.getLogger('simple_server.server').info(
            'RAM C-APDU (LOAD): 80E2')
        self.assertIn('RAM C-APDU (LOAD): 80E2', self.stream.getvalue())
        self.assertIn('INFO', self.stream.getvalue())

    def test_third_party_loggers_stay_at_warning(self):
        m.configure_logging(stream=self.stream)
        logging.getLogger('smartcard.transport').info('noise')
        self.assertNotIn('noise', self.stream.getvalue())
        for name in m.THIRD_PARTY_LOGGERS:
            self.assertEqual(logging.getLogger(name).level, logging.WARNING)

    def test_the_format_carries_the_time_level_and_logger(self):
        m.configure_logging(stream=self.stream)
        logging.getLogger('simple_server.test').warning('hello')
        text = self.stream.getvalue()
        self.assertIn('WARNING simple_server.test: hello', text)
        self.assertRegex(text, r'^\[\d\d:\d\d:\d\d\] ')

    def test_a_repeated_setup_does_not_stack_handlers(self):
        m.configure_logging(stream=self.stream)
        m.configure_logging(stream=self.stream)
        logging.getLogger('simple_server.test').info('once')
        self.assertEqual(self.stream.getvalue().count('once'), 1)


# ---- the GSMTAP packet -------------------------------------------------------

class PacketTests(unittest.TestCase):
    #: libosmocore's gsmtap.h layout (Wireshark's packet-gsmtap.h prints the
    #: same struct): version 2, hdr_len 4 words (16 bytes), type 0x04 (SIM),
    #: then the zeroed timeslot/arfcn/signal/snr/frame, the sub-type, antenna,
    #: slot and res fields.
    HEADER_ZERO = '02040400000000000000000000000000'

    def test_the_header_bytes(self):
        packet = gsmtap.build_packet(gsmtap.GSMTAP_SIM_APDU,
                                     bytes.fromhex('00a40004023f00'))
        self.assertEqual(len(packet), 16 + 7)
        self.assertEqual(packet[:16].hex(), self.HEADER_ZERO)
        (version, hdr_len, pkt_type, timeslot, arfcn, signal, snr, frame,
         sub_type, antenna, slot, res) = struct.unpack('!BBBBHBBIBBBB',
                                                       packet[:16])
        self.assertEqual((version, hdr_len, pkt_type), (2, 4, 0x04))
        self.assertEqual((timeslot, arfcn, signal, snr, frame), (0, 0, 0, 0, 0))
        self.assertEqual(sub_type, gsmtap.GSMTAP_SIM_APDU)
        self.assertEqual((antenna, slot, res), (0, 0, 0))
        self.assertEqual(packet[16:], bytes.fromhex('00a40004023f00'))

    def test_the_atr_sub_type_and_payload(self):
        packet = gsmtap.build_packet(gsmtap.GSMTAP_SIM_ATR, ATR)
        self.assertEqual(packet[12], gsmtap.GSMTAP_SIM_ATR)
        self.assertEqual(packet[12], 0x01)
        self.assertEqual(packet[16:], ATR)

    def test_parse_target(self):
        self.assertEqual(gsmtap.parse_target(None), ('127.0.0.1', 4729))
        self.assertEqual(gsmtap.parse_target(''), ('127.0.0.1', 4729))
        self.assertEqual(gsmtap.parse_target('10.0.0.5'), ('10.0.0.5', 4729))
        self.assertEqual(gsmtap.parse_target('10.0.0.5:5000'),
                         ('10.0.0.5', 5000))
        self.assertEqual(gsmtap.parse_target(':5000'), ('127.0.0.1', 5000))
        with self.assertRaises(ValueError):
            gsmtap.parse_target('host:not-a-port')

    def test_parse_target_rejects_an_out_of_range_port(self):
        # sendto() raises OverflowError (not OSError) for a port above 65535,
        # so the port is validated where the target is parsed.
        for target in ('host:99999', 'host:-1', 'host:65536', 'host:'):
            with self.subTest(target=target):
                with self.assertRaises(ValueError):
                    gsmtap.parse_target(target)
        self.assertEqual(gsmtap.parse_target('host:0'), ('host', 0))
        self.assertEqual(gsmtap.parse_target('host:65535'), ('host', 65535))


class SenderTests(unittest.TestCase):
    def test_apdu_and_atr_reach_a_udp_listener(self):
        import socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(('127.0.0.1', 0))
        sock.settimeout(2.0)
        host, port = sock.getsockname()
        sender = gsmtap.GsmtapSender(host, port)
        try:
            sender.send_apdu(bytes.fromhex('00a40004023f00'))
            apdu_packet, _addr = sock.recvfrom(2048)
            sender.send_atr(ATR)
            atr_packet, _addr = sock.recvfrom(2048)
        finally:
            sender.close()
            sock.close()
        self.assertEqual(apdu_packet[12], gsmtap.GSMTAP_SIM_APDU)
        self.assertEqual(apdu_packet[16:], bytes.fromhex('00a40004023f00'))
        self.assertEqual(atr_packet[12], gsmtap.GSMTAP_SIM_ATR)
        self.assertEqual(atr_packet[16:], ATR)

    def test_send_never_raises_without_a_listener(self):
        sender = gsmtap.GsmtapSender('127.0.0.1', 1)   # nothing listening
        sender.send_apdu(b'\x00\xa4')                  # must not raise
        sender.send_atr(ATR)
        sender.close()

    def test_send_never_raises_on_an_out_of_range_port(self):
        # sendto raises OverflowError (not OSError) for a port above 65535;
        # parse_target refuses it, and a caller that bypassed the parser (a
        # direct GsmtapSender) must still never raise into card I/O.
        sender = gsmtap.GsmtapSender('127.0.0.1', 1)
        try:
            sender._addr = ('127.0.0.1', 99999)
            sender.send_apdu(b'\x00\xa4')
            sender.send_atr(ATR)
        finally:
            sender.close()


# ---- the transmit-level tracer -----------------------------------------------

class TransmitTracerTests(unittest.TestCase):
    def test_every_transmit_becomes_one_packet(self):
        recorder = Recorder()
        link = FakeLink()
        link.send_tpdu = lambda tpdu: (b'\x90\x00', b'\x90\x00')
        link = gsmtap.attach(link, gsmtap.GsmtapTracer(recorder))
        data, sw = link.send_tpdu(bytes.fromhex('00b0000004'))
        self.assertEqual((data, sw), (b'\x90\x00', b'\x90\x00'))
        self.assertEqual(recorder.apdus,
                         [bytes.fromhex('00b000000490009000')])

    def test_the_true_t0_chain_is_two_packets(self):
        # send_apdu on a T=0 link transmits the case-4 command without its Le
        # byte, gets "61xx bytes available", then fetches the data with GET
        # RESPONSE - the tracer reports exactly those two transmits.
        recorder = Recorder()

        class Connection(object):
            def __init__(self):
                self.sent = []

            def connect(self):
                pass

            def disconnect(self):
                pass

            def getATR(self):
                return list(ATR)

            def setProtocol(self, protocol):
                pass

            def transmit(self, apdu):
                self.sent.append(bytes(apdu))
                if len(self.sent) == 1:
                    return [], 0x61, 0x04
                return [0x01, 0x02, 0x03, 0x04], 0x90, 0x00

        connection = Connection()
        link = transport.PcscLink(connection=connection)
        gsmtap.attach(link, gsmtap.GsmtapTracer(recorder))
        link.connect()
        data, sw = link.send_apdu(bytes.fromhex('00a40004023f0000'))
        self.assertEqual(data, bytes.fromhex('01020304'))
        self.assertEqual(sw, b'\x90\x00')
        self.assertEqual(recorder.apdus, [
            bytes.fromhex('00a40004023f00') + bytes.fromhex('6104'),
            bytes.fromhex('00c00000') + bytes.fromhex('04'
                                                       '01020304'
                                                       '9000'),
        ])
        self.assertEqual(recorder.atrs, [ATR])

    def test_the_atr_is_sent_on_connect_and_reset(self):
        recorder = Recorder()
        link = gsmtap.attach(FakeLink(), gsmtap.GsmtapTracer(recorder))
        link.connect()
        self.assertEqual(recorder.atrs, [ATR])
        link.reset_card()
        self.assertEqual(recorder.atrs, [ATR, ATR])

    def test_the_reset_marker_precedes_the_post_reset_atr(self):
        stderr = io.StringIO()
        link = gsmtap.attach(FakeLink(),
                             gsmtap.StderrTracer(stream=stderr))
        link.reset_card()
        self.assertEqual(stderr.getvalue().splitlines(), [
            'APDU-TRACE: RESET',
            'APDU-TRACE: ATR %s' % ATR.hex(),
        ])

    def test_attach_is_idempotent(self):
        recorder = Recorder()
        link = gsmtap.attach(FakeLink(), gsmtap.GsmtapTracer(recorder))
        tracer = link._tpdu_send_tracer
        self.assertIs(gsmtap.attach(link, gsmtap.GsmtapTracer(Recorder())),
                      link)
        self.assertIs(link._tpdu_send_tracer, tracer)
        link.send_tpdu(b'\x00\xa4')
        self.assertEqual(len(recorder.apdus), 1)

    def test_a_raising_tracer_never_breaks_the_transmit(self):
        class Broken(gsmtap.TpduTracer):
            def trace_tpdu(self, tpdu, data, sw, elapsed_ms=0.0):
                raise RuntimeError('tracer exploded')

            def trace_atr(self, atr):
                raise RuntimeError('tracer exploded')

        link = gsmtap.attach(FakeLink(), Broken())
        self.assertEqual(link.send_tpdu(b'\x00\xa4'), (b'\x90\x00', b'\x90\x00'))
        link.connect()                                 # must not raise

    def test_the_fanout_reaches_every_sink(self):
        recorder = Recorder()
        stderr = io.StringIO()
        tracer = gsmtap.Fanout([gsmtap.GsmtapTracer(recorder),
                                gsmtap.StderrTracer(stream=stderr)])
        link = gsmtap.attach(FakeLink(), tracer)
        link.connect()
        link.send_tpdu(bytes.fromhex('00a40004023f00'))
        self.assertEqual(recorder.atrs, [ATR])
        self.assertEqual(recorder.apdus,
                         [bytes.fromhex('00a40004023f0090009000')])
        trace = stderr.getvalue()
        self.assertIn('APDU-TRACE: ATR %s' % ATR.hex(), trace)
        self.assertIn('APDU-TRACE(', trace)
        self.assertIn('00a40004023f00 -> SW: 9000', trace)

    def test_one_failing_sink_does_not_stop_the_other(self):
        recorder = Recorder()

        class Broken(gsmtap.TpduTracer):
            def trace_tpdu(self, tpdu, data, sw, elapsed_ms=0.0):
                raise RuntimeError('boom')

        tracer = gsmtap.Fanout([Broken(), gsmtap.GsmtapTracer(recorder)])
        gsmtap.attach(FakeLink(), tracer).send_tpdu(b'\x00\xa4')
        self.assertEqual(len(recorder.apdus), 1)

    def test_attach_without_a_tracer_is_a_no_op(self):
        link = FakeLink()
        self.assertIs(gsmtap.attach(link, None), link)
        self.assertFalse(hasattr(link, '_tpdu_send_tracer'))


class TransportFactoryTests(unittest.TestCase):
    """The state's transport factory attaches the tracer to every link."""

    class Reader(object):
        name = 'Fake reader'

        def createConnection(self):
            raise AssertionError('no connection is opened here')

    class ConnectedReader(object):
        """A reader whose connection connects and reports the fixture ATR."""

        name = 'Fake reader'

        def __init__(self):
            self.connections = []

        def createConnection(self):
            connection = mock.Mock()
            connection.getATR.return_value = list(ATR)
            self.connections.append(connection)
            return connection

    def test_the_factory_attaches_the_tracer(self):
        with mock.patch.object(transport.smartcard.System, 'readers',
                               return_value=[self.Reader()]):
            link = m._transport_factory(
                gsmtap.GsmtapTracer(Recorder()))('Fake reader')
            plain = m._transport_factory(None)('Fake reader')
        self.assertIsInstance(link, transport.PcscLink)
        self.assertIsNotNone(getattr(link, '_tpdu_send_tracer', None))
        # without --apdu-trace/--gsmtap the link is untouched
        self.assertFalse(hasattr(plain, '_tpdu_send_tracer'))

    def test_state_open_builds_the_link_through_the_factory_with_the_tracer(self):
        # main --apdu-trace/--gsmtap -> _transport_factory -> ServerState.open:
        # the link the state ends up with is the traced one, and the ATR
        # reaches the sink through the connect hook.  The exclusive-connect
        # decorator is passed through (it would call the real PC/SC stack).
        recorder = Recorder()
        tracer = gsmtap.GsmtapTracer(recorder)
        reader = self.ConnectedReader()
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(transport.smartcard.System, 'readers',
                                  return_value=[reader]), \
                mock.patch.object(transport, 'ExclusiveConnectCardConnection',
                                  lambda connection: connection):
            state = server_module.ServerState(
                reader='Fake reader',
                transport_factory=m._transport_factory(tracer),
                presets_path=os.path.join(tmp, 'p.json'),
                test_scripts_path=os.path.join(tmp, 's.json'),
                test_suites_path=os.path.join(tmp, 'u.json'))
            try:
                state.open(skip_card_init=True, start_threads=False)
                self.assertIsNotNone(
                    getattr(state.transport, '_tpdu_send_tracer', None))
                self.assertEqual(state.atr, ATR.hex())
                self.assertEqual(recorder.atrs, [ATR])
            finally:
                state.close()


class TracerSelectionTests(unittest.TestCase):
    def parse(self, *args):
        return m.build_parser().parse_args(list(args))

    def test_no_flag_builds_no_tracer(self):
        err = io.StringIO()
        self.assertIsNone(m._build_tracers(self.parse(), stderr=err))
        self.assertEqual(err.getvalue(), '')

    def test_apdu_trace_builds_the_stderr_tracer(self):
        tracer = m._build_tracers(self.parse('--apdu-trace'))
        self.assertIsInstance(tracer, gsmtap.StderrTracer)

    def test_both_flags_build_a_fanout(self):
        err = io.StringIO()
        tracer = m._build_tracers(
            self.parse('--apdu-trace', '--gsmtap', '127.0.0.1:4731'),
            stderr=err)
        try:
            self.assertIsInstance(tracer, gsmtap.Fanout)
            self.assertEqual(len(tracer.tracers), 2)
            self.assertIn('GSMTAP: streaming APDUs to 127.0.0.1:4731',
                          err.getvalue())
        finally:
            for sink in tracer.tracers:
                if hasattr(sink, 'sender'):
                    sink.sender.close()


if __name__ == '__main__':
    unittest.main()
