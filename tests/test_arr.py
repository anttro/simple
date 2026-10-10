# coding=utf-8
"""EF.ARR session cache (v3.24.0): the equip-time read, the FCP '8B' backing
store, the /api/arr route and the write refresh."""

import json
import pathlib
import threading
import unittest
import urllib.request
from unittest import mock

from pysim_simple_server.__main__ import _build_http_server
from pysim_simple_server.server import (PysimHandler, _CARD_FREE_GET, _arr_fid,
                                        _arr_refresh_after_write, _read_arr,
                                        _refresh_arr_cache)

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = (ROOT / 'pysim_simple_server' / 'server.py').read_text(encoding='utf-8')


class FakeFile:
    def __init__(self, fid):
        self.fid = fid


class FakeLchan:
    """One selected ARR file: read_record serves its records."""

    def __init__(self, files):
        self.files = files          # path -> [record hex]
        self._cur = None
        self.selected_file = None

    def selected_file_num_of_rec(self):
        return len(self.files.get(self._cur) or [])

    def selected_file_record_len(self):
        return 6

    def read_record(self, n):
        return (self.files[self._cur][n - 1], '9000')


def _select(lchan, path, app):
    if path not in lchan.files:
        raise RuntimeError('file not found')
    lchan._cur = path
    lchan.selected_file = FakeFile(path.rsplit('/', 1)[-1])
    return lchan.selected_file, None


def _app(files):
    lchan = FakeLchan(files)
    app = mock.Mock()
    app.rs.lchan = [lchan]
    return app, lchan


def _read(app, file_type='linear_fixed'):
    with mock.patch('pysim_simple_server.server._select_path', _select), \
         mock.patch('pysim_simple_server.server._get_file_type', return_value=file_type):
        return _read_arr(app)


class ReadArrTests(unittest.TestCase):
    def test_reads_both_copies(self):
        app, _ = _app({'MF/2F06': ['800101A40683010A950108', '9000'],
                       'ADF.USIM/6F06': ['8001019000']})
        arrs = _read(app)
        self.assertEqual([a['fid'] for a in arrs], ['2F06', '6F06'])
        self.assertEqual(arrs[0]['path'], 'MF/2F06')
        self.assertEqual(arrs[0]['records'], ['800101A40683010A950108', '9000'])
        self.assertEqual(arrs[0]['num_records'], 2)
        self.assertEqual(arrs[0]['record_len'], 6)
        self.assertEqual(arrs[1]['path'], 'ADF.USIM/6F06')

    def test_reads_the_df_tree_copies(self):
        app, _ = _app({'MF/2F06': ['9000'], 'MF/7F10/6F06': ['9000'],
                       'MF/7F20/6F06': ['9000'], 'ADF.USIM/6F06': ['9000']})
        arrs = _read(app)
        self.assertEqual([a['path'] for a in arrs],
                         ['MF/2F06', 'MF/7F10/6F06', 'MF/7F20/6F06', 'ADF.USIM/6F06'])

    def test_a_missing_copy_is_skipped(self):
        app, _ = _app({'ADF.USIM/6F06': ['9000']})
        arrs = _read(app)
        self.assertEqual([a['fid'] for a in arrs], ['6F06'])

    def test_no_arr_returns_none(self):
        app, _ = _app({})
        self.assertIsNone(_read(app))

    def test_a_transparent_file_is_skipped(self):
        app, _ = _app({'MF/2F06': ['9000']})
        self.assertIsNone(_read(app, file_type='transparent'))

    def test_a_failing_read_skips_the_file(self):
        app, lchan = _app({'MF/2F06': ['9000'], 'ADF.USIM/6F06': ['9000']})
        orig = lchan.read_record

        def flaky(n):
            if lchan._cur == 'MF/2F06':
                raise RuntimeError('6A82')
            return orig(n)
        lchan.read_record = flaky
        arrs = _read(app)
        self.assertEqual([a['fid'] for a in arrs], ['6F06'])

    def test_the_record_count_is_capped(self):
        app, _ = _app({'MF/2F06': ['9000'] * 200})
        arrs = _read(app)
        self.assertEqual(arrs[0]['num_records'], 64)

    def test_no_app_returns_none(self):
        self.assertIsNone(_read_arr(None))
        self.assertIsNone(_read_arr(mock.Mock(rs=None)))


class CacheTests(unittest.TestCase):
    def test_arr_fid(self):
        self.assertTrue(_arr_fid('2F06'))
        self.assertTrue(_arr_fid('6f06'))
        self.assertFalse(_arr_fid('6F07'))
        self.assertFalse(_arr_fid(None))

    def test_refresh_stores_and_clears(self):
        s = mock.Mock()
        with mock.patch('pysim_simple_server.server._read_arr',
                        return_value=[{'fid': '2F06', 'num_records': 2}]), \
             mock.patch('pysim_simple_server.server._tlog'):
            _refresh_arr_cache(s)
        self.assertEqual(s.arr, [{'fid': '2F06', 'num_records': 2}])
        with mock.patch('pysim_simple_server.server._read_arr', return_value=None):
            _refresh_arr_cache(s)
        self.assertIsNone(s.arr)

    def test_write_refresh_only_for_arr_fids(self):
        s = mock.Mock()
        with mock.patch('pysim_simple_server.server._read_arr',
                        return_value=[{'fid': '2F06', 'num_records': 2}]) as m:
            self.assertFalse(_arr_refresh_after_write(s, None, '6F07'))
            m.assert_not_called()
            self.assertTrue(_arr_refresh_after_write(s, None, '2f06'))
            m.assert_called_once()
        self.assertEqual(s.arr, [{'fid': '2F06', 'num_records': 2}])

    def test_equip_and_disconnect_wiring(self):
        # the equip hook reads the cache; a disconnect clears it; an EF.ARR
        # write refreshes it
        seg = SRC[SRC.index('def _apply_equipped_card'):SRC.index('def _card_reset_reinit')]
        self.assertIn('_refresh_arr_cache(server)', seg)
        seg = SRC[SRC.index('def _handle_card_disconnect'):SRC.index('def _ensure_transport')]
        self.assertIn('_server_ref.arr = None', seg)
        seg = SRC[SRC.index("elif self.path == '/api/write':"):SRC.index("elif self.path == '/api/tree':")]
        self.assertIn('_arr_refresh_after_write', seg)

    def test_the_startup_init_reads_the_arr(self):
        # a card present at server start never runs _apply_equipped_card, so
        # the startup init reads the ARR in its own CAT-free window (v3.24.0
        # review - the cache was empty on a startup-equipped card)
        src = (ROOT / 'pysim_simple_server' / '__main__.py').read_text(encoding='utf-8')
        self.assertIn('arr_files = _read_arr(app)', src)
        self.assertIn('server.arr = arr_files', src)
        imp = src[src.index('from .server import'):]
        self.assertIn('_read_arr', imp[:imp.index('\n')])


class ArrRouteTests(unittest.TestCase):
    """GET /api/arr serves the session cache without touching the card."""

    def setUp(self):
        self.server = _build_http_server('127.0.0.1', 0, PysimHandler)
        self.server.log_requests = False
        self.server.app = None
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def _get(self, path):
        with urllib.request.urlopen('http://127.0.0.1:%d%s' % (self.port, path),
                                    timeout=5) as res:
            return res.status, json.loads(res.read() or b'{}')

    def test_arr_is_a_card_free_get(self):
        self.assertIn('/api/arr', _CARD_FREE_GET)

    def test_empty_cache_answers_not_found(self):
        status, resp = self._get('/api/arr')
        self.assertEqual(status, 200, resp)
        self.assertFalse(resp['ok'])
        self.assertEqual(resp['arrs'], [])

    def test_the_cache_is_served_with_the_session(self):
        self.server.arr = [{'fid': '2F06', 'path': 'MF/2F06', 'records': ['9000']}]
        self.server.card_session = 7
        status, resp = self._get('/api/arr')
        self.assertEqual(status, 200, resp)
        self.assertTrue(resp['ok'])
        self.assertEqual(resp['session'], 7)
        self.assertEqual(resp['arrs'][0]['fid'], '2F06')


if __name__ == '__main__':
    unittest.main()
