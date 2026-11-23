#!/usr/bin/env python3
"""PWA <-> ``simple_server`` HTTP contract tests (the 4.0.0 route matrix).

``pysimFetch(path, body)`` sends POST whenever a body is passed and GET
otherwise; the routes are read from the pysim-free server's
``_dispatch_get`` / ``_dispatch_post`` (``simple_server/server.py``).  The
matrix is the guard for the "endpoint registered under the wrong method" class
of bug (the legacy v3.6.41 issue: the PWA POSTed to test endpoints that were
registered in ``_do_GET``, so every Start answered 404) and for the 4.0.0
cut-over: the pySim passthrough routes are gone and the reader/lifecycle
routes replace them.

The smoke test boots the real :class:`ThreadingHTTPServer` over a cardless
:class:`~simple_server.server.ServerState` and checks the test-script endpoint
methods; the lock test holds ``state.card_lock`` and asserts the cached GETs
still answer (the legacy v3.6.47 starvation fix).

The suite needs no card and no pysim.
"""

import json
import pathlib
import re
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from simple_server.server import (CARD_FREE_GET, ServerState,
                                  build_http_server)

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _pwa_calls():
    """[(path, has_body)] for every literal pysimFetch()/pysimFetchOk() call in
    the PWA."""
    html = (ROOT / 'frontend' / 'index.html').read_text(encoding='utf-8')
    return [(m.group(1).split('?')[0], m.group(2) == ',')
            for m in re.finditer(r"pysimFetch(?:Ok)?\(\s*'([^']+)'\s*(,|\))", html)]


def _server_routes():
    """((GET exact, GET prefixes), (POST exact, POST prefixes)) of /api paths.

    The dispatch stores the path in a local (``path = self.path.split(...)``)
    and compares that; the legacy form compared ``self.path`` directly, so both
    spellings are accepted.  The extraction stops at the POST helpers so no
    other comparison in the handler body can be mistaken for a route.
    """
    src = (ROOT / 'simple_server' / 'server.py').read_text(encoding='utf-8')
    get_seg = src[src.index('def _dispatch_get('):src.index('def _dispatch_post(')]
    post_start = src.index('def _dispatch_post(')
    marker = src.find('        # ---- the POST endpoints', post_start)
    post_seg = src[post_start:marker if marker > 0 else None]

    def routes(seg):
        exact = set(re.findall(r"(?:self\.path|path) == '(/api/[^']+)'", seg))
        for group in re.findall(r"(?:self\.path|path) in \(([^)]*)\)", seg):
            exact |= set(re.findall(r"'(/api/[^']+)'", group))
        prefixes = set(re.findall(
            r"(?:self\.path|path)\.startswith\('(/api/[^']+)'\)", seg))
        return exact, prefixes - {'/api/'}

    return routes(get_seg), routes(post_seg)


class FetchRouteMatrixTests(unittest.TestCase):
    def test_every_fetch_matches_a_route_with_the_right_method(self):
        (get, get_pref), (post, post_pref) = _server_routes()
        self.assertTrue(get and post, 'route extraction found nothing')

        def in_routes(path, exact, prefixes):
            return path in exact or any(path.startswith(p) for p in prefixes)

        calls = _pwa_calls()
        self.assertTrue(calls, 'no pysimFetch() call found in the PWA')
        for path, has_body in calls:
            with self.subTest(path=path, post=has_body):
                if has_body:
                    self.assertTrue(in_routes(path, post, post_pref),
                                    '%s is POSTed (body) but has no POST route' % path)
                else:
                    self.assertTrue(
                        in_routes(path, get, get_pref) or in_routes(path, post, post_pref),
                        '%s is fetched without a body but has no GET/POST route' % path)

    def test_the_pysim_passthrough_routes_are_gone(self):
        """The 4.0.0 cut-over removed the pySim command line: neither the PWA
        nor the pysim-free server may expose the passthrough/help/verify
        routes (sp-verify compared the JS packet against pySim)."""
        (get, _get_pref), (post, _post_pref) = _server_routes()
        calls = [path for path, _body in _pwa_calls()]
        for path in ('/api/command', '/api/commands', '/api/cardinfo',
                     '/api/help', '/api/sp-verify'):
            with self.subTest(path=path):
                self.assertNotIn(path, get | post)
                self.assertNotIn(path, calls)

    def test_the_replacement_routes_exist_with_the_right_method(self):
        (get, _get_pref), (post, _post_pref) = _server_routes()
        self.assertIn('/api/readers', get)
        for path in ('/api/reader', '/api/equip', '/api/reset', '/api/status-poll'):
            with self.subTest(path=path):
                self.assertIn(path, post)


class _HTTPHarness(object):
    """A real ThreadingHTTPServer over a cardless ServerState."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.tmp.name)
        self.state = ServerState(
            monitor=False,
            readers_fn=lambda: ['Reader A'],
            presets_path=root / 'card_presets.json',
            test_scripts_path=root / 'test_scripts.json',
            test_suites_path=root / 'test_suites.json',
        )
        self.server = build_http_server('127.0.0.1', 0, self.state)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={'poll_interval': 0.05},
            name='fetch-routes-http', daemon=True)
        self.thread.start()

    def request(self, method, path, body=None):
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body).encode('utf-8')
            headers['Content-Type'] = 'application/json'
        req = urllib.request.Request(
            'http://127.0.0.1:%d%s' % (self.port, path),
            data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as res:
                return res.status, json.loads(res.read() or b'{}')
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read() or b'{}')
            finally:
                exc.close()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()


class TestScriptHttpTests(unittest.TestCase):
    """The test-script endpoints must answer on POST (the PWA's method)."""

    def setUp(self):
        self.harness = _HTTPHarness()

    def tearDown(self):
        self.harness.close()

    def test_start_stop_clear_answer_on_post(self):
        # an empty script is invalid, but it proves the route exists (400)
        status, resp = self.harness.request('POST', '/api/test/run', {})
        self.assertEqual(status, 400, resp)
        self.assertIn('error', resp)
        status, resp = self.harness.request('POST', '/api/test/stop', {})
        self.assertEqual(status, 200, resp)
        status, resp = self.harness.request('POST', '/api/test/clear', {})
        self.assertEqual(status, 200, resp)

    def test_the_test_endpoints_are_not_get_routes(self):
        status, resp = self.harness.request('GET', '/api/test/run')
        self.assertEqual(status, 404, resp)


class CardFreeGetTests(unittest.TestCase):
    """Cached GETs must answer while the card lock is held.

    During a long RAM install the UI polls several cached endpoints every few
    seconds; while they queued behind the install they ate the browser's
    per-host connection budget and starved /api/status - the modal's progress
    bar froze around 70% and jumped to 100% at the end (live 2026-09-29).
    """

    def setUp(self):
        self.harness = _HTTPHarness()

    def tearDown(self):
        self.harness.close()

    def test_cached_endpoints_answer_while_the_card_lock_is_held(self):
        with self.harness.state.card_lock:
            for path in sorted(CARD_FREE_GET):
                with self.subTest(path=path):
                    status, resp = self.harness.request('GET', path)
                    self.assertEqual(status, 200, resp)

    def test_card_touching_gets_stay_locked(self):
        # the ES10 endpoints select/STORE DATA on the card
        for path in ('/api/esim/chip', '/api/esim/profiles',
                     '/api/esim/notifications'):
            with self.subTest(path=path):
                self.assertNotIn(path, CARD_FREE_GET)


if __name__ == '__main__':
    unittest.main()
