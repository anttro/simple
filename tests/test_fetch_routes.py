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
from unittest import mock

from pysim_simple_server.__main__ import _build_http_server
from pysim_simple_server.server import PysimHandler, _CARD_FREE_GET, _CARD_LOCK

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


class CardFreeGetTests(unittest.TestCase):
    """Cached GETs must answer while the card lock is held.

    During a long RAM install the UI polls several cached endpoints every few
    seconds; while they queued behind the install they ate the browser's
    per-host connection budget and starved /api/status - the modal's progress
    bar froze around 70% and jumped to 100% at the end (live 2026-09-29).
    """

    def setUp(self):
        self.server = _build_http_server('127.0.0.1', 0, PysimHandler)
        self.server.log_requests = False
        self.server.app = None
        self.server.sl = None
        self.server.scc = None
        self.server.stk_pending = None
        self.server.menu_active = False
        self.server.sim_menu = None
        self.server.event_list = None
        self.server.terminal_profile = None
        self.server.cli_terminal_profile = None
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def _get(self, path, timeout=2):
        return urllib.request.urlopen(
            'http://127.0.0.1:%d%s' % (self.port, path), timeout=timeout)

    def test_cached_endpoints_answer_while_the_card_lock_is_held(self):
        with _CARD_LOCK:
            for path in ('/api/status', '/api/stk-status', '/api/poll-status',
                         '/api/proactive-log', '/api/menu', '/api/events'):
                with self.subTest(path=path):
                    with self._get(path) as res:
                        self.assertEqual(res.status, 200)

    def test_card_touching_gets_stay_locked(self):
        # cardinfo runs a card command; the ES10 endpoints select/STORE DATA
        for path in ('/api/cardinfo', '/api/esim/chip', '/api/esim/profiles'):
            self.assertNotIn(path, _CARD_FREE_GET)


def _mini_cap_hex():
    """A minimal parseable CAP archive (Header + Applet + one Import)."""
    import io as _io
    import struct as _struct
    import zipfile as _zipfile

    def comp(tag, payload):
        return bytes([tag]) + _struct.pack('>H', len(payload)) + payload

    aid = bytes.fromhex('0102030405')
    header = comp(0x01, _struct.pack('>I', 0xDECAFFED) + bytes([1, 2, 0, 1, 2]) +
                  bytes([len(aid)]) + aid)
    applet = comp(0x03, bytes([1, len(aid)]) + aid + b'\x00\x01')
    imports = comp(0x04, bytes([1]) + bytes([0, 1, 7]) + bytes.fromhex('A0000000620101'))
    buf = _io.BytesIO()
    with _zipfile.ZipFile(buf, 'w') as z:
        z.writestr('Header.cap', header)
        z.writestr('Applet.cap', applet)
        z.writestr('Import.cap', imports)
    return buf.getvalue().hex().upper()


class CapCompatHttpTests(unittest.TestCase):
    """POST /api/cap-compat exists (the CAP compatibility test, v3.6.48)."""

    def setUp(self):
        self.server = _build_http_server('127.0.0.1', 0, PysimHandler)
        self.server.log_requests = False
        self.server.app = None
        self.server.sl = None
        self.server.scc = None
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

    def test_cap_compat_route_exists(self):
        # without a card session it answers 503 (route present), not 404
        status, resp = self._post('/api/cap-compat', {'cap_hex': '00'})
        self.assertEqual(status, 503, resp)
        self.assertIn('error', resp)

    def test_cap_compat_rejects_a_bad_probe(self):
        # a parseable CAP + a bad override -> 400 from the probe validation,
        # before any card I/O (a dummy scc object is enough)
        self.server.scc = object()
        status, resp = self._post('/api/cap-compat',
                                  {'cap_hex': _mini_cap_hex(), 'probe_imports': {'all': 'x'}})
        self.assertEqual(status, 400, resp)
        self.assertIn('import probe', resp['error'])

    def test_cap_compat_reports_unmatched_probe_lines_with_versions(self):
        # v3.6.52: probe overrides that match no Import entry come back as a
        # per-AID list ({aid, version}) for the modal's detail block.
        from pysim_simple_server import server as srv
        self.server.scc = object()
        patches = [
            mock.patch.object(srv, '_ram_detect_format', lambda *a, **k: 'compact'),
            mock.patch.object(srv, '_ram_send_gp_apdu', lambda *a, **k: True),
        ]
        for p in patches:
            p.start()
        try:
            status, resp = self._post('/api/cap-compat', {
                'cap_hex': _mini_cap_hex(),
                'probe_imports': {'A0000000620101': '0.0', 'dead beef': '1.2'},
            })
        finally:
            for p in reversed(patches):
                p.stop()
        self.assertEqual(status, 200, resp)
        self.assertTrue(resp['success'], resp)
        self.assertEqual(resp['probe_unmatched'], [{'aid': 'DEADBEEF', 'version': '1.2'}])
        self.assertEqual(resp['probe_imports'],
                         [{'aid': 'A0000000620101', 'from': '1.0', 'to': '0.0'}])


class RamInstallNvFootprintHttpTests(unittest.TestCase):
    """The RAM install response carries the measured NV footprint (v3.6.51):
    the server reads GET DATA FF21 before and after the chain (best effort)
    and reports the free-NV values and their delta."""

    def setUp(self):
        from pysim_simple_server import server as srv
        self.srv = srv
        self.server = _build_http_server('127.0.0.1', 0, PysimHandler)
        self.server.log_requests = False
        self.server.app = None
        self.server.sl = None
        self.server.scc = object()
        self.server.sms_oa = '12345'
        self.server.sms_sc = '12345678912'
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

    def _install(self, fail_at=None, nv_before=50000, nv_after=20000):
        def fake_send(server, scc, sp, state, name, apdu, silent=False):
            if fail_at == name:
                state['steps'].append({'name': name, 'por_status': 'por_error', 'por_sw': '6A88'})
                return False
            state['steps'].append({'name': name, 'por_status': 'por_ok', 'por_sw': '9000'})
            return True

        def fake_read(server, scc, sp, state, ram_format, label):
            return nv_before if label == 'NV before' else nv_after

        patches = [
            mock.patch.object(self.srv, '_cap_parse',
                              lambda cap_hex: ('F0414C46416101', 'F0414C4641610101', b'\x01\x02')),
            mock.patch.object(self.srv, '_cap_apdu_sequence',
                              lambda *a, **k: ['80E60200', '80E80000', '80E60000']),
            mock.patch.object(self.srv, '_ram_detect_format', lambda *a, **k: 'compact'),
            mock.patch.object(self.srv, '_ram_send_gp_apdu', fake_send),
            mock.patch.object(self.srv, '_ram_read_ff21', fake_read),
        ]
        for p in patches:
            p.start()
        try:
            return self._post('/api/ram-install', {'cap_hex': 'AA'})
        finally:
            for p in reversed(patches):
                p.stop()

    def test_success_reports_the_nv_delta(self):
        status, resp = self._install()
        self.assertEqual(status, 200, resp)
        self.assertTrue(resp['success'], resp)
        self.assertEqual(resp['nv_before'], 50000)
        self.assertEqual(resp['nv_after'], 20000)
        self.assertEqual(resp['nv_delta'], 30000)

    def test_failed_install_still_reports_the_nv_delta(self):
        status, resp = self._install(fail_at='LOAD (1/1)')
        self.assertEqual(status, 200, resp)
        self.assertFalse(resp['success'], resp)
        self.assertEqual(resp['failed_step'], 2)
        self.assertEqual(resp['nv_before'], 50000)
        self.assertEqual(resp['nv_after'], 20000)
        self.assertEqual(resp['nv_delta'], 30000)

    def test_a_card_without_ff21_has_no_nv_fields(self):
        status, resp = self._install(nv_before=None, nv_after=None)
        self.assertEqual(status, 200, resp)
        self.assertTrue(resp['success'], resp)
        for key in ('nv_before', 'nv_after', 'nv_delta'):
            self.assertNotIn(key, resp)


class CntrLowGuardHttpTests(unittest.TestCase):
    """The low-counter guard on the HTTP paths (v3.6.58): /api/send-ota
    reports the card's verdict as data, and a RAM chain stops at it."""

    def setUp(self):
        from pysim_simple_server import server as srv
        self.srv = srv
        self.server = _build_http_server('127.0.0.1', 0, PysimHandler)
        self.server.log_requests = False
        self.server.app = None
        self.server.sl = None
        self.server.scc = object()
        self.server.sms_oa = '12345'
        self.server.sms_sc = '12345678912'
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

    def test_send_ota_reports_the_low_counter_verdict(self):
        patches = [
            mock.patch.object(self.srv, '_send_secured_packet', lambda *a, **k: {
                'success': True, 'sw': '9000', 'bytes': 34, 'segments': 1,
                'response_data': '027100000b0a00000000000000bb0002'}),
            mock.patch.object(self.srv, '_decode_por', lambda *a, **k: {
                'response_status': 'cntr_low', 'tar': '000000', 'cntr': '00000000BB',
                'pcntr': 0, 'rpl': 11, 'rhl': 10, 'raw': ''}),
        ]
        self.server.app = object()      # the send path only checks truthiness
        for p in patches:
            p.start()
        try:
            status, resp = self._post('/api/send-ota', {
                'sp': '00', 'spi1': '16', 'spi2': '01', 'kic': '15', 'kid': '15',
                'tar': '000000', 'cntr': '00000000BB'})
        finally:
            for p in reversed(patches):
                p.stop()
        self.assertEqual(status, 200, resp)
        self.assertTrue(resp['cntr_low'], resp)
        self.assertEqual(resp['card_cntr'], '00000000BB')
        self.assertEqual(resp['suggested_cntr'], '00000000BC')

    def test_the_ram_chain_stops_at_a_low_counter(self):
        calls = []

        def fake_send(server, scc, sp, state, name, apdu, silent=False):
            calls.append(name)
            if len(calls) == 2:
                # the card's verdict: stop - no further step may be sent
                state['steps'].append({'name': name, 'por_status': 'cntr_low',
                                       'por_cntr': '00000000BB'})
                return False
            state['steps'].append({'name': name, 'por_status': 'por_ok', 'por_sw': '9000'})
            return True

        patches = [
            mock.patch.object(self.srv, '_cap_parse',
                              lambda cap_hex: ('F0414C46416101', 'F0414C4641610101', b'\x01\x02')),
            mock.patch.object(self.srv, '_cap_apdu_sequence',
                              lambda *a, **k: ['80E60200', '80E80000', '80E80000', '80E60000']),
            mock.patch.object(self.srv, '_ram_detect_format', lambda *a, **k: 'compact'),
            mock.patch.object(self.srv, '_ram_send_gp_apdu', fake_send),
            mock.patch.object(self.srv, '_ram_read_ff21', lambda *a, **k: None),
        ]
        for p in patches:
            p.start()
        try:
            status, resp = self._post('/api/ram-install', {'cap_hex': 'AA'})
        finally:
            for p in reversed(patches):
                p.stop()
        self.assertEqual(status, 200, resp)
        self.assertFalse(resp['success'], resp)
        self.assertEqual(len(calls), 2, calls)          # nothing after the verdict
        self.assertTrue(resp['cntr_low'], resp)
        self.assertEqual(resp['card_cntr'], '00000000BB')
        self.assertEqual(resp['suggested_cntr'], '00000000BC')


if __name__ == '__main__':
    unittest.main()
