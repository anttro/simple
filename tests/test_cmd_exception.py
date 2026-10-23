"""cmd2/pySim exception output must fail /api/write and /api/read.

`onecmd_plus_hooks()` catches a command's exception and prints
``EXCEPTION of type ...`` (plus a traceback in debug mode) instead of
re-raising, so the handlers that only looked for ``got (XXXX)`` reported a
successful write while nothing was written.  The handlers now treat both
markers as failure; the SW-based success/failure semantics stay as before.
"""

import types
import unittest
from unittest import mock

from pysim_simple_server import server as srv
from pysim_simple_server.server import PysimHandler, _cmd_exception


class FakeApp:
    def __init__(self, output):
        self.output = output
        self.stdout = None
        self.rs = types.SimpleNamespace(lchan=[types.SimpleNamespace(selected_file=None)])

    def onecmd_plus_hooks(self, cmd):
        # pySim writes into the app's stdout, which the handler redirects
        self.stdout.write(self.output)


def _fake_handler(path, body, app):
    h = object.__new__(PysimHandler)
    h.path = path
    h.command = 'POST'
    h.headers = {}
    h.server = types.SimpleNamespace(app=app, log_requests=False, pbr=None)
    h._read_body = lambda: body
    h._log_req = lambda *a, **k: None
    h._log_resp = lambda *a, **k: None
    sent = []
    h._send_json = lambda data, status=200: sent.append((data, status))
    return h, sent


class CmdExceptionTests(unittest.TestCase):
    def test_the_helper_detects_both_markers_and_returns_the_first_line(self):
        self.assertIsNone(_cmd_exception(''))
        self.assertIsNone(_cmd_exception('SW: 9000\n'))
        exc = ("EXCEPTION of type 'ValueError' occurred with message: 'boom'\n"
               "Traceback (most recent call last):\n  File x\nValueError: boom\n")
        self.assertEqual(_cmd_exception(exc),
                         "EXCEPTION of type 'ValueError' occurred with message: 'boom'")
        tb = 'Traceback (most recent call last):\n  File x\nRuntimeError: boom\n'
        self.assertEqual(_cmd_exception(tb), 'Traceback (most recent call last):')


class WriteExceptionTests(unittest.TestCase):
    def _write(self, output):
        app = FakeApp(output)
        h, sent = _fake_handler('/api/write', {
            'name': 'EF.ADN', 'data': 'AA' * 30, 'record_nr': 1, 'allow_probe': True,
        }, app)
        body = {'name': 'EF.ADN', 'data': 'AA' * 30, 'record_nr': 1, 'allow_probe': True}
        h._read_body = lambda: body
        with mock.patch.object(srv, '_select_with_parent', lambda *a, **k: (None, None)), \
                mock.patch.object(srv, '_get_file_type', lambda *a: 'linear_fixed'), \
                mock.patch.object(srv, '_arr_refresh_after_write', lambda *a, **k: None):
            PysimHandler._do_POST(h)
        self.assertEqual(len(sent), 1, sent)
        return sent[0][0]

    def test_a_cmd2_exception_is_not_a_successful_write(self):
        resp = self._write("EXCEPTION of type 'Exception' occurred with message: 'no card'\n")
        self.assertFalse(resp['success'])
        self.assertIn('EXCEPTION of type', resp['error'])
        self.assertNotIn('sw', resp)

    def test_a_traceback_is_not_a_successful_write(self):
        resp = self._write('Traceback (most recent call last):\n  File x\nValueError: boom\n')
        self.assertFalse(resp['success'])
        self.assertEqual(resp['error'], 'Traceback (most recent call last):')

    def test_the_sw_semantics_are_unchanged(self):
        self.assertEqual(self._write('SW: 9000\n'), {'success': True, 'sw': '9000'})
        self.assertEqual(self._write('SW: 6982 - Security status not satisfied\n'
                                     'got 6982'),
                         {'success': False, 'sw': '6982',
                          'error': 'Security status not satisfied'})


class ReadExceptionTests(unittest.TestCase):
    def _read(self, output, mode='raw'):
        app = FakeApp(output)
        body = {'name': 'EF.ADN', 'mode': mode, 'allow_probe': True, 'record_nr': 1}
        h, sent = _fake_handler('/api/read', body, app)
        with mock.patch.object(srv, '_select_with_parent', lambda *a, **k: (None, None)), \
                mock.patch.object(srv, '_get_file_type', lambda *a: 'transparent'), \
                mock.patch.object(srv, '_collect_apdu_times', lambda: None), \
                mock.patch.object(srv, '_end_apdu_time_collection', lambda: []):
            PysimHandler._do_POST(h)
        self.assertEqual(len(sent), 1, sent)
        return sent[0][0]

    def test_a_cmd2_exception_is_not_a_successful_read(self):
        resp = self._read("EXCEPTION of type 'Exception' occurred with message: 'no card'\n")
        self.assertFalse(resp['success'])
        self.assertIn('EXCEPTION of type', resp['error'])
        resp = self._read('Traceback (most recent call last):\n  File x\n', mode='decoded')
        self.assertFalse(resp['success'])
        self.assertEqual(resp['error'], 'Traceback (most recent call last):')

    def test_a_normal_read_keeps_the_sw_parse(self):
        resp = self._read('AABB\nSW: 9000\n')
        self.assertTrue(resp['success'])
        self.assertEqual(resp['sw'], '9000')
        self.assertEqual(resp['data'], 'AABB')


if __name__ == '__main__':
    unittest.main()
