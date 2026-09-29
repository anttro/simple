"""PWA <-> server HTTP contract tests.

`pysimFetch(path, body)` sends POST whenever a body is passed and GET
otherwise; the server's routes live in `PysimHandler._do_GET` / `_do_POST`.
The test-script endpoints were registered in `_do_GET` while the PWA POSTs
to them, so every Start answered 404 "Not found" (live 2026-09-29; present
since the phase-1 commit and never exercised over HTTP).  The matrix below
is the guard for that contract; the smoke test boots the real handler and
checks the three endpoints.
"""

import json
import pathlib
import re
import threading
import unittest
import urllib.error
import urllib.request

from pysim_simple_server.__main__ import _build_http_server
from pysim_simple_server.server import PysimHandler

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _pwa_calls():
    """[(path, has_body)] for every literal pysimFetch() call in the PWA."""
    html = (ROOT / 'frontend' / 'index.html').read_text(encoding='utf-8')
    return [(m.group(1).split('?')[0], m.group(2) == ',')
            for m in re.finditer(r"pysimFetch\(\s*'([^']+)'\s*(,|\))", html)]


def _server_routes():
    """((GET exact, GET prefixes), (POST exact, POST prefixes)) of /api paths."""
    src = (ROOT / 'pysim_simple_server' / 'server.py').read_text(encoding='utf-8')
    get_seg = src[src.index('def _do_GET('):src.index('def do_POST(')]
    post_seg = src[src.index('def _do_POST('):src.index('def log_message(')]

    def routes(seg):
        exact = set(re.findall(r"self\.path == '(/api/[^']+)'", seg))
        for group in re.findall(r"self\.path in \(([^)]*)\)", seg):
            exact |= set(re.findall(r"'(/api/[^']+)'", group))
        prefixes = set(re.findall(r"self\.path\.startswith\('(/api/[^']+)'\)", seg))
        return exact, prefixes - {'/api/'}

    return routes(get_seg), routes(post_seg)


class FetchRouteMatrixTests(unittest.TestCase):
    def test_every_fetch_matches_a_route_with_the_right_method(self):
        (get, get_pref), (post, post_pref) = _server_routes()
        self.assertTrue(get and post, 'route extraction found nothing')

        def in_routes(path, exact, prefixes):
            return path in exact or any(path.startswith(p) for p in prefixes)

        for path, has_body in _pwa_calls():
            with self.subTest(path=path, post=has_body):
                if has_body:
                    self.assertTrue(in_routes(path, post, post_pref),
                                    '%s is POSTed (body) but has no POST route' % path)
                else:
                    self.assertTrue(
                        in_routes(path, get, get_pref) or in_routes(path, post, post_pref),
                        '%s is fetched without a body but has no GET/POST route' % path)


class TestScriptHttpTests(unittest.TestCase):
    """The test-script endpoints must answer on POST (the PWA's method)."""

    def setUp(self):
        self.server = _build_http_server('127.0.0.1', 0, PysimHandler)
        self.server.log_requests = False
        self.server.app = None
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def _post(self, path, body):
        req = urllib.request.Request(
            'http://127.0.0.1:%d%s' % (self.port, path),
            data=json.dumps(body).encode(),
            headers={'Content-Type': 'application/json'}, method='POST')
        try:
            with urllib.request.urlopen(req, timeout=5) as res:
                return res.status, json.loads(res.read() or b'{}')
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b'{}')

    def test_start_stop_clear_answer_on_post(self):
        # an empty script is invalid, but it proves the route exists (400)
        status, resp = self._post('/api/test/run', {})
        self.assertEqual(status, 400, resp)
        self.assertIn('error', resp)
        status, resp = self._post('/api/test/stop', {})
        self.assertEqual(status, 200, resp)
        status, resp = self._post('/api/test/clear', {})
        self.assertEqual(status, 200, resp)

    def test_the_test_endpoints_are_not_get_routes(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen('http://127.0.0.1:%d/api/test/run' % self.port, timeout=5)
        self.assertEqual(cm.exception.code, 404)
        cm.exception.close()


if __name__ == '__main__':
    unittest.main()
