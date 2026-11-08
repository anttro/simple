"""The request/access log must never enter a pySim output capture.

Handlers capture pySim output by temporarily rebinding the global ``sys.stderr``
to a StringIO (``_capture_app_output``).  Concurrent card-free requests (the 2 s
/api/status poll) log via ``log_message`` / ``_log_req`` / ``_log_resp``; if
those wrote to ``sys.stderr`` their lines would land in the active capture
buffer and corrupt the file content (the snapshot "access log line in contents"
bug).  The log now writes to fd 2 directly, and the capture is a context
manager that restores both streams.
"""

import io
import pathlib
import sys
import types
import unittest
from unittest import mock

from pysim_simple_server import server as srv
from pysim_simple_server.server import PysimHandler, _capture_app_output, _log_stderr


class LogStderrTests(unittest.TestCase):
    def test_log_stderr_writes_to_fd2_not_the_rebindable_stream(self):
        writes = []
        err = io.StringIO()
        with mock.patch.object(srv.os, 'write', side_effect=lambda fd, data: writes.append((fd, data))), \
                mock.patch('sys.stderr', new=err):
            _log_stderr('hello\n')
        self.assertEqual(writes, [(2, b'hello\n')])
        self.assertEqual(err.getvalue(), '')

    def test_log_message_goes_to_fd2(self):
        h = object.__new__(PysimHandler)
        h.client_address = ('127.0.0.1', 1234)
        h.log_date_time_string = lambda: '05/Nov/2026 14:09:10'
        writes = []
        err = io.StringIO()
        with mock.patch.object(srv.os, 'write', side_effect=lambda fd, data: writes.append((fd, data))), \
                mock.patch('sys.stderr', new=err):
            h.log_message('"%s" %s %s', 'GET /api/status HTTP/1.1', '200', '-')
        self.assertEqual(
            writes,
            [(2, b'127.0.0.1 - - [05/Nov/2026 14:09:10] '
                  b'"GET /api/status HTTP/1.1" 200 -\n')])
        self.assertEqual(err.getvalue(), '',
                         'the access log must not land in a redirected sys.stderr')


class CaptureOutputTests(unittest.TestCase):
    def test_captures_both_streams_and_restores(self):
        orig_stdout = io.StringIO()
        app = types.SimpleNamespace(stdout=orig_stdout)
        real_stderr = sys.stderr
        with _capture_app_output(app) as out:
            app.stdout.write('AABB\n')
            sys.stderr.write('SW: 9000\n')
        self.assertIn('AABB\n', out.getvalue())
        self.assertIn('SW: 9000\n', out.getvalue())
        self.assertIs(app.stdout, orig_stdout)
        self.assertIs(sys.stderr, real_stderr)

    def test_restores_on_exception(self):
        orig_stdout = io.StringIO()
        app = types.SimpleNamespace(stdout=orig_stdout)
        real_stderr = sys.stderr
        with self.assertRaises(RuntimeError):
            with _capture_app_output(app) as out:
                app.stdout.write('partial\n')
                raise RuntimeError('boom')
        self.assertIs(app.stdout, orig_stdout)
        self.assertIs(sys.stderr, real_stderr)


class SourcePinTests(unittest.TestCase):
    def test_log_message_uses_fd2(self):
        src = pathlib.Path(srv.__file__).read_text()
        seg = src[src.index('def log_message('):]
        self.assertIn('_log_stderr(', seg)
        self.assertNotIn('sys.stderr.write', seg)

    def test_the_handlers_use_the_capture_context_manager(self):
        # The six handlers that capture pySim output share one context manager;
        # a re-introduced inline ``sys.stderr = out`` block would be a
        # thread-safety regression.
        src = pathlib.Path(srv.__file__).read_text()
        self.assertGreaterEqual(src.count('with _capture_app_output(app) as out:'), 6)


if __name__ == '__main__':
    unittest.main()
