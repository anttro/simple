"""The HTTP server must stay answerable while a card operation holds the lock.

The RAM modal's live progress comes from /api/status.ram_progress, and the
static PWA files are card-free: both are served outside _CARD_LOCK by design
(server.do_GET), which only works when the server dispatches requests
concurrently.  A single-threaded HTTPServer starved every other request until
the install POST finished, so the progress bar never grew (live 2026-09-29).
"""

import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from pysim_simple_server.__main__ import _build_http_server


class _StubHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/slow':
            time.sleep(1.0)
        body = b'ok'
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class TestThreadedHttpServer(unittest.TestCase):
    def setUp(self):
        self.server = _build_http_server('127.0.0.1', 0, _StubHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_built_server_is_threaded(self):
        self.assertIsInstance(self.server, ThreadingHTTPServer)
        # a stuck request must not block shutdown
        self.assertTrue(self.server.daemon_threads)

    def test_cached_request_is_served_while_a_slow_one_runs(self):
        started = time.monotonic()
        slow = threading.Thread(
            target=lambda: urllib.request.urlopen(
                'http://127.0.0.1:%d/slow' % self.port, timeout=5).read())
        slow.start()
        time.sleep(0.15)                       # let /slow enter its handler
        urllib.request.urlopen('http://127.0.0.1:%d/fast' % self.port, timeout=5).read()
        fast_elapsed = time.monotonic() - started
        slow.join(timeout=5)
        # a single-threaded server would answer only after /slow finished
        self.assertLess(fast_elapsed, 0.6,
                        'fast request waited for the slow one: server not threaded')


if __name__ == '__main__':
    unittest.main()
