"""PC/SC access-denied handling at startup (v3.38.1).

A user without permission to pcscd used to get a silent serial fallback that
failed on /dev/ttyUSB0 while the server still printed "listening".  The
startup now classifies the pyscard failure, reports the cause with guidance
and exits; ``start.sh`` runs the same access pre-flight.
"""

import inspect
import io
import unittest
from pathlib import Path
from unittest import mock

from pysim_simple_server import __main__ as m


class FakeExc(Exception):
    def __init__(self, hresult=None, message=''):
        super().__init__(message)
        self.hresult = hresult


class ErrorKindTests(unittest.TestCase):
    def test_access_denied_by_hresult(self):
        # pyscard raises CardConnectionException(hresult=...) with an empty
        # message on the SCardEstablishContext failure
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

    def test_access_denied_is_raised_immediately(self):
        calls = []

        def denied():
            calls.append(1)
            raise FakeExc(0x80100027)

        with self.assertRaises(m.PcscUnavailable) as cm:
            m._probe_pcsc_reader(retries=3, sleep_fn=lambda s: None,
                                 readers_fn=denied)
        self.assertEqual(cm.exception.kind, 'access')
        self.assertEqual(len(calls), 1)      # permissions do not fix themselves

    def test_an_empty_list_is_retried_then_reported(self):
        calls = []

        def empty():
            calls.append(1)
            return []

        with self.assertRaises(m.PcscUnavailable) as cm:
            m._probe_pcsc_reader(retries=2, sleep_fn=lambda s: None,
                                 readers_fn=empty)
        self.assertEqual(cm.exception.kind, 'reader')
        self.assertEqual(len(calls), 2)

    def test_a_transient_failure_is_retried(self):
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) == 1:
                raise FakeExc(0x8010000B)    # sharing violation
            return ['Reader Y']

        self.assertEqual(
            m._probe_pcsc_reader(retries=2, sleep_fn=lambda s: None,
                                 readers_fn=flaky), 'Reader Y')
        self.assertEqual(len(calls), 2)


class ReportTests(unittest.TestCase):
    def _report(self, kind, exc=None):
        err = io.StringIO()
        with mock.patch('sys.stderr', err):
            m._report_pcsc_unavailable(kind, exc)
        return err.getvalue()

    def test_access_guidance(self):
        text = self._report('access', FakeExc(0x80100027, 'Access denied.'))
        low = text.lower()
        self.assertIn('access denied', low)
        self.assertIn('pcscd', low)
        self.assertIn('usermod -aG pcscd'.lower(), low)
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


class SerialArgTests(unittest.TestCase):
    def test_explicit_serial_flags(self):
        for argv in (['-d', '/dev/ttyS0'], ['--device', 'x'],
                     ['--device=x'], ['-d/dev/ttyS0']):
            self.assertTrue(m._serial_arg_given(argv), argv)

    def test_the_default_is_not_an_explicit_choice(self):
        for argv in ([], ['--http-port', '8080'], ['-p', '0']):
            self.assertFalse(m._serial_arg_given(argv), argv)


class SourcePins(unittest.TestCase):
    def test_the_module_propagates_the_exit_code(self):
        src = Path(m.__file__).read_text()
        self.assertIn('sys.exit(main())', src)

    def test_the_start_script_probes_access_and_guides(self):
        src = (Path(m.__file__).parent.parent / 'start.sh').read_text()
        self.assertIn('from smartcard.System import readers', src)
        self.assertIn('usermod -aG pcscd', src)
        self.assertIn('pcsc_scan', src)
        # the old "is pcscd running" heuristic is gone
        self.assertNotIn('pgrep -x pcscd', src)


if __name__ == '__main__':
    unittest.main()
