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
from pysim_simple_server import presets
from pysim_simple_server.server import PysimHandler, _CARD_FREE_GET, _CARD_LOCK

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _pwa_calls():
    """[(path, has_body)] for every literal pysimFetch()/pysimFetchOk() call in
    the PWA."""
    html = (ROOT / 'frontend' / 'index.html').read_text(encoding='utf-8')
    return [(m.group(1).split('?')[0], m.group(2) == ',')
            for m in re.finditer(r"pysimFetch(?:Ok)?\(\s*'([^']+)'\s*(,|\))", html)]


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


class TarProbeListTests(unittest.TestCase):
    """The PWA's TAR checklist must match the server's TAR_PROBE_TARS - the
    panel is pre-populated client-side, the server's list is the API fallback,
    and both must describe the same allocations (TS 101 220 Annex D)."""

    def test_the_pwa_and_the_server_share_the_standard_tar_list(self):
        from pysim_simple_server import server as srv
        html = (ROOT / 'frontend' / 'index.html').read_text(encoding='utf-8')
        block = html[html.index('const TAR_PROBE_TARS = ['):]
        block = block[:block.index('\n];')]
        pwa = re.findall(r"tar:\s*'([0-9A-F]{6})',\s*label:\s*'([^']*)'", block)
        self.assertEqual(pwa, list(srv.TAR_PROBE_TARS))
        self.assertTrue(pwa, 'the PWA list must not be empty')


class PresetStoreHttpTests(unittest.TestCase):
    """The card preset endpoints (v3.8.0): the PWA's Cards tab talks to the
    server store instead of localStorage, and the counter an operation
    accepted is persisted server-side."""

    def setUp(self):
        import tempfile
        from pysim_simple_server import presets
        from pysim_simple_server import server as srv
        self.srv = srv
        self.tmp = tempfile.TemporaryDirectory()
        self.store = presets.PresetStore(pathlib.Path(self.tmp.name) / 'card_presets.json')
        self.server = _build_http_server('127.0.0.1', 0, PysimHandler)
        self.server.log_requests = False
        self.server.app = None
        self.server.sl = None
        self.server.scc = object()
        self.server.sms_oa = '12345'
        self.server.sms_sc = '12345678912'
        self.server.card_presets = self.store
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def _post(self, path, body):
        req = urllib.request.Request(
            'http://127.0.0.1:%d%s' % (self.port, path),
            data=json.dumps(body).encode(),
            headers={'Content-Type': 'application/json'}, method='POST')
        try:
            with urllib.request.urlopen(req, timeout=5) as res:
                return res.status, json.loads(res.read() or b'{}')
        except urllib.error.HTTPError as e:
            body = json.loads(e.read() or b'{}')
            e.close()
            return e.code, body

    def _get(self, path, timeout=5):
        try:
            with urllib.request.urlopen(
                    'http://127.0.0.1:%d%s' % (self.port, path), timeout=timeout) as res:
                return res.status, json.loads(res.read() or b'{}')
        except urllib.error.HTTPError as e:
            body = json.loads(e.read() or b'{}')
            e.close()
            return e.code, body

    def _keyset(self, **kw):
        ks = {'kic': '15', 'kid': '15', 'kicKey': 'AA', 'kidKey': 'BB'}
        ks.update(kw)
        return ks

    def _preset(self, **kw):
        p = {'name': 'C', 'keysets': [self._keyset()]}
        p.update(kw)
        return p

    def test_the_get_route_is_card_free_and_reports_the_store(self):
        self.assertIn('/api/presets', _CARD_FREE_GET)
        with _CARD_LOCK:
            status, resp = self._get('/api/presets')
        self.assertEqual(status, 200, resp)
        self.assertEqual(resp['presets'], [])
        self.assertEqual(resp['path'], str(self.store.path))

    def test_create_update_delete_round_trip(self):
        status, resp = self._post('/api/presets', self._preset(iccid='8970119000004600098'))
        self.assertEqual(status, 200, resp)
        pid = resp['preset']['id']
        self.assertTrue(pid)
        self.assertEqual(resp['preset']['keysets'][0]['cntr'], '0000000001')

        # a duplicate ICCID (raw-hex form) is refused
        status, resp = self._post('/api/presets',
                                  self._preset(name='Other', iccid='980711090000640090F8'))
        self.assertEqual(status, 400, resp)
        self.assertIn('already exists', resp['error'])

        status, resp = self._post('/api/presets/update',
                                  {'id': pid, 'fields': {'name': 'Renamed'}})
        self.assertEqual(status, 200, resp)
        self.assertEqual(resp['preset']['name'], 'Renamed')
        self.assertEqual(resp['preset']['id'], pid)

        status, resp = self._post('/api/presets/update', {'id': 'nope', 'fields': {}})
        self.assertEqual(status, 404, resp)

        status, resp = self._post('/api/presets/delete', {'id': pid})
        self.assertEqual(status, 200, resp)
        self.assertTrue(resp['removed'])
        self.assertEqual(self.store.list(), [])

    def test_import_accepts_the_old_localstorage_export(self):
        status, resp = self._post('/api/presets/import', {'presets': [
            {'name': 'A', 'iccid': '8970119000004600098', 'kic': '15', 'kid': '15',
             'kicKey': 'AA', 'kidKey': 'BB', 'cntr': '0000000007'},
            {'name': 'Broken'},
        ]})
        self.assertEqual(status, 200, resp)
        self.assertEqual(resp['added'], 1)
        self.assertEqual(resp['skipped'], 1)
        imported = self.store.find_by_iccid('8970119000004600098')
        self.assertEqual(imported['keysets'][0]['cntr'], '0000000007')
        self.assertEqual(presets.keyset_kvn(imported['keysets'][0]), 1)

        status, resp = self._post('/api/presets/import', {'presets': 'nope'})
        self.assertEqual(status, 400, resp)

    def test_the_counter_an_operation_accepted_is_persisted(self):
        p = self.store.add(self._preset(iccid='8970119000004600098'))
        patches = [
            mock.patch.object(self.srv, '_send_secured_packet', lambda *a, **k: {
                'success': True, 'sw': '9000', 'bytes': 34, 'segments': 1,
                'response_data': '027100000000'}),
            mock.patch.object(self.srv, '_decode_por', lambda *a, **k: {
                'response_status': 'por_ok', 'tar': '000000', 'cntr': '0000000001',
                'pcntr': 0, 'rpl': 11, 'rhl': 10, 'raw': '',
                'decoded': {'number_of_commands': 1, 'last_status_word': '9000',
                            'last_response_data': ''}}),
        ]
        self.server.app = object()
        for patch in patches:
            patch.start()
        try:
            status, resp = self._post('/api/send-ota', {
                'sp': '00', 'spi1': '16', 'spi2': '01', 'kic': '15', 'kid': '15',
                'tar': '000000', 'cntr': '0000000001', 'preset_id': p['id']})
        finally:
            for patch in reversed(patches):
                patch.stop()
        self.assertEqual(status, 200, resp)
        self.assertEqual(resp['final_cntr'], '0000000002')
        self.assertEqual(self.store.find_keyset(p['id'], 1)['cntr'], '0000000002')

    def test_a_counter_less_send_ota_body_still_answers(self):
        # a pre-built packet without a counter: no final_cntr, no persist,
        # and above all no int('') crash (v3.8.0 review fix)
        p = self.store.add(self._preset(iccid='8970119000004600098'))
        patches = [
            mock.patch.object(self.srv, '_send_secured_packet', lambda *a, **k: {
                'success': True, 'sw': '9000', 'bytes': 34, 'segments': 1,
                'response_data': ''}),
            mock.patch.object(self.srv, '_decode_por', lambda *a, **k: None),
        ]
        self.server.app = object()
        for patch in patches:
            patch.start()
        try:
            status, resp = self._post('/api/send-ota', {
                'sp': '00', 'spi1': '16', 'spi2': '01', 'kic': '15', 'kid': '15',
                'tar': '000000', 'preset_id': p['id']})
        finally:
            for patch in reversed(patches):
                patch.stop()
        self.assertEqual(status, 200, resp)
        self.assertNotIn('final_cntr', resp)
        self.assertEqual(self.store.find_keyset(p['id'], 1)['cntr'], '0000000001')

    def test_send_ota_refuses_a_kic_kid_version_mismatch(self):
        # TS 102 225 A.2: the versions must be identical - invalid input
        self.server.app = object()
        status, resp = self._post('/api/send-ota', {
            'sp': '00', 'spi1': '16', 'spi2': '01', 'kic': '15', 'kid': '25',
            'tar': '000000', 'cntr': '0000000001'})
        self.assertEqual(status, 400, resp)
        self.assertIn('same keyset number', resp['error'])

    def test_send_ota_refuses_an_undefined_key_version(self):
        p = self.store.add(self._preset(iccid='8970119000004600098'))
        self.server.app = object()
        status, resp = self._post('/api/send-ota', {
            'sp': '00', 'spi1': '16', 'spi2': '01', 'kic': '35', 'kid': '35',
            'tar': '000000', 'cntr': '0000000001', 'preset_id': p['id']})
        self.assertEqual(status, 400, resp)
        self.assertIn('keyset 3 is not defined', resp['error'])

    def test_the_counter_lands_in_the_keyset_of_the_packet(self):
        p = self.store.add(self._preset(keysets=[
            self._keyset(kic='15', kid='15'),
            self._keyset(kic='29', kid='29', kicKey='CC', kidKey='DD', cntr='0000000005')]))
        patches = [
            mock.patch.object(self.srv, '_send_secured_packet', lambda *a, **k: {
                'success': True, 'sw': '9000', 'bytes': 34, 'segments': 1,
                'response_data': '027100000000'}),
            mock.patch.object(self.srv, '_decode_por', lambda *a, **k: {
                'response_status': 'por_ok', 'tar': '000000', 'cntr': '0000000005',
                'pcntr': 0, 'rpl': 11, 'rhl': 10, 'raw': '',
                'decoded': {'number_of_commands': 1, 'last_status_word': '9000',
                            'last_response_data': ''}}),
        ]
        self.server.app = object()
        for patch in patches:
            patch.start()
        try:
            status, resp = self._post('/api/send-ota', {
                'sp': '00', 'spi1': '16', 'spi2': '01', 'kic': '29', 'kid': '29',
                'tar': '000000', 'cntr': '0000000005', 'kicKey': 'CC', 'kidKey': 'DD',
                'preset_id': p['id']})
        finally:
            for patch in reversed(patches):
                patch.stop()
        self.assertEqual(status, 200, resp)
        self.assertEqual(resp['final_cntr'], '0000000006')
        self.assertEqual(self.store.find_keyset(p['id'], 2)['cntr'], '0000000006')
        self.assertEqual(self.store.find_keyset(p['id'], 1)['cntr'], '0000000001')

    def test_a_ram_install_refuses_an_undefined_key_version(self):
        p = self.store.add(self._preset(iccid='8970119000004600098'))
        status, resp = self._post('/api/ram-install', {
            'cap_hex': _mini_cap_hex(), 'kic': '35', 'kid': '35', 'preset_id': p['id']})
        self.assertEqual(status, 400, resp)
        self.assertIn('keyset 3 is not defined', resp['error'])

    def test_a_keyless_send_leaves_every_counter_untouched(self):
        # SPI1 00: the counter is "present, ignored, never updated"
        # (TS 102 225 5.1.1 b5b4=00) - the server must not report, advance or
        # persist any counter for such a packet
        p = self.store.add(self._preset(keysets=[
            self._keyset(kic='15', kid='15', cntr='0000000005'),
            self._keyset(kic='29', kid='29', kicKey='CC', kidKey='DD', cntr='0000000007')]))
        before = [ks['cntr'] for ks in self.store.get(p['id'])['keysets']]
        patches = [
            mock.patch.object(self.srv, '_send_secured_packet', lambda *a, **k: {
                'success': True, 'sw': '9000', 'bytes': 18, 'segments': 1,
                'response_data': ''}),
            mock.patch.object(self.srv, '_decode_por', lambda *a, **k: None),
        ]
        self.server.app = object()
        for patch in patches:
            patch.start()
        try:
            status, resp = self._post('/api/send-ota', {
                'sp': '00', 'spi1': '00', 'spi2': '01', 'kic': '00', 'kid': '00',
                'tar': 'B00000', 'cntr': '00000000AA', 'preset_id': p['id']})
        finally:
            for patch in reversed(patches):
                patch.stop()
        self.assertEqual(status, 200, resp)
        self.assertNotIn('final_cntr', resp)
        self.assertEqual([ks['cntr'] for ks in self.store.get(p['id'])['keysets']], before)
        self.assertFalse(self.store.audit_path.exists(),
                         'a keyless send must not write a counter at all')

    def test_a_no_security_packet_is_built_with_zero_kic_kid(self):
        # TS 102 225 A.2: KIc/KID '00' are valid with SPI1 00 - the server
        # builds the packet with no keys at all (the card accepted this exact
        # form; a fake KIc/KID 15/15 is refused with 6200)
        sent = []
        patches = [
            mock.patch.object(self.srv, '_send_secured_packet',
                              lambda scc, sp_hex, **kw: (sent.append(sp_hex), {
                                  'success': True, 'sw': '9000', 'bytes': 18,
                                  'segments': 1, 'response_data': ''})[1]),
            mock.patch.object(self.srv, '_decode_por', lambda *a, **k: None),
        ]
        self.server.app = object()
        for patch in patches:
            patch.start()
        try:
            status, resp = self._post('/api/send-ota', {
                'apdu': '00A40000023F00', 'spi1': '00', 'spi2': '01',
                'kic': '00', 'kid': '00', 'tar': 'B00000', 'cntr': '0000000001',
                'kicKey': '', 'kidKey': ''})
        finally:
            for patch in reversed(patches):
                patch.stop()
        self.assertEqual(status, 200, resp)
        self.assertEqual(sent[0].lower(), '0d00010000b0000000000000010000a40000023f00')

    def test_the_tar_probe_classifies_each_tar(self):
        p = self.store.add(self._preset(iccid='8970119000004600098', keysets=[
            self._keyset(kic='25', kid='25', kicKey='AA', kidKey='BB', cntr='0000000005')]))
        seen = []

        def fake_step(server, scc, sp, state, name, apdu, silent=False):
            tar = sp['tar']
            seen.append(tar)
            if tar == '000000':
                state['steps'].append({'name': name, 'sw': '9000', 'por_status': 'por_ok',
                                       'por_sw': '6D00', 'por_data': ''})
            elif tar == 'B00000':
                state['steps'].append({'name': name, 'sw': '9000', 'por_status': 'por_ok',
                                       'por_sw': '6B00', 'por_data': '3F00'})
            elif tar == 'B00001':
                state['steps'].append({'name': name, 'sw': '9000', 'por_status': 'no_por'})
            else:
                state['steps'].append({'name': name, 'sw': '6200', 'por_status': 'envelope_error'})
            return True

        self.server.app = object()      # the probe only checks truthiness
        with mock.patch.object(self.srv, '_ram_send_gp_apdu', fake_step):
            status, resp = self._post('/api/tar-probe', {
                'preset_id': p['id'], 'kic': '25', 'kid': '25',
                'kicKey': 'AA', 'kidKey': 'BB', 'cntr': '0000000005',
                'tars': ['000000', 'B00000', 'B00001', 'B00200']})
        self.assertEqual(status, 200, resp)
        self.assertEqual(seen, ['000000', 'B00000', 'B00001', 'B00200'])
        verdicts = {r['tar']: r['verdict'] for r in resp['results']}
        self.assertEqual(verdicts, {'000000': 'registered', 'B00000': 'registered',
                                    'B00001': 'no_por', 'B00200': 'refused'})
        self.assertEqual(resp['registered'], 2)
        self.assertEqual(resp['total'], 4)
        self.assertEqual(resp['kvn'], 2)
        self.assertEqual(resp['results'][0]['label'], 'Issuer Security Domain (compact)')
        self.assertEqual(resp['results'][0]['por_sw'], '6D00')

    def test_the_tar_probe_needs_a_counter_check_and_a_defined_keyset(self):
        self.server.app = object()
        status, resp = self._post('/api/tar-probe', {'spi1': '00', 'kic': '15', 'kid': '15'})
        self.assertEqual(status, 400, resp)
        self.assertIn('counter check', resp['error'])
        p = self.store.add(self._preset(iccid='8970119000004600098'))
        status, resp = self._post('/api/tar-probe',
                                  {'preset_id': p['id'], 'kic': '35', 'kid': '35'})
        self.assertEqual(status, 400, resp)
        self.assertIn('not defined', resp['error'])

    def test_a_store_write_failure_answers_a_json_500(self):
        with mock.patch.object(self.store, 'add', side_effect=OSError('read-only file system')):
            status, resp = self._post('/api/presets', self._preset())
        self.assertEqual(status, 500, resp)
        self.assertIn('preset store write failed', resp['error'])

    def test_the_store_endpoints_answer_503_without_a_store(self):
        self.server.card_presets = None
        status, resp = self._get('/api/presets')
        self.assertEqual(status, 503, resp)
        status, resp = self._post('/api/presets', self._preset())
        self.assertEqual(status, 503, resp)


if __name__ == '__main__':
    unittest.main()
