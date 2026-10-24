# coding=utf-8
"""EF.PBR session cache (TS 31.102 4.4.2.1): the equip-time read of the
phonebook reference records, the /api/pbr route and the cache lifecycle."""

import json
import pathlib
import threading
import unittest
import urllib.request
from unittest import mock

from pysim_simple_server.__main__ import _build_http_server
from pysim_simple_server.server import (PBR_READ_PATHS, PBR_RECORD_LIMIT,
                                        PysimHandler, _CARD_FREE_GET,
                                        _pbr_files_for_node, _pbr_inject_children,
                                        _pbr_known_fid, _pbr_norm_path,
                                        _pbr_parse, _read_pbr,
                                        _refresh_pbr_cache)

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = (ROOT / 'pysim_simple_server' / 'server.py').read_text(encoding='utf-8')

# The PBR_READ_PATHS the frontend's uicc_files.json lists for EF.PBR (4F30).
FRONTEND_PATHS = (ROOT / 'frontend' / 'uicc_files.json').read_text(encoding='utf-8')
# The shared decode vectors: the PWA decoder (ef_decode.test.js) asserts the
# same file, so the two PBR parsers cannot drift apart.
PBR_VECTORS = json.loads((ROOT / 'frontend' / 'tests' / 'pbr_vectors.json').read_text(encoding='utf-8'))

FIXTURE = 'A80AC0034F3A02C5034F0904AA0ACB034F3D07C2034F4A06'


class FakeFile:
    def __init__(self, fid):
        self.fid = fid


class FakeScc:
    """Serves READ RECORD for the selected fake PBR file."""

    cla_byte = '00'
    max_cmd_len = 255

    def __init__(self, lchan):
        self.lchan = lchan
        self.sent = []

    def send_apdu_checksw(self, pdu):
        self.sent.append(pdu)
        return (self.lchan.files[self.lchan._cur][int(pdu[4:6], 16) - 1], '9000')


class FakeLchan:
    """One selected PBR file: READ RECORD serves its records."""

    def __init__(self, files):
        self.files = files          # path -> [record hex]
        self._cur = None
        self.selected_file = None
        self.scc = FakeScc(self)

    def selected_file_num_of_rec(self):
        return len(self.files.get(self._cur) or [])

    def selected_file_record_len(self):
        return 32

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
        return _read_pbr(app)


class ReadPbrTests(unittest.TestCase):
    def test_reads_all_paths(self):
        app, _ = _app({'MF/7F10/5F3A/4F30': ['A80AC0034F3A02C5034F0904'],
                       'ADF.USIM/5F3A/4F30': ['A80AC0034F3A02'],
                       'ADF.USIM-NON-IMSI/5F3A/4F30': ['A80AC0034F3A02']})
        pbrs = _read(app)
        self.assertEqual([p['fid'] for p in pbrs], ['4F30', '4F30', '4F30'])
        self.assertEqual([p['path'] for p in pbrs], list(PBR_READ_PATHS))
        self.assertEqual(pbrs[0]['records'], ['A80AC0034F3A02C5034F0904'])
        self.assertEqual(pbrs[0]['num_records'], 1)
        self.assertEqual(pbrs[0]['record_len'], 32)

    def test_a_missing_copy_is_skipped(self):
        app, _ = _app({'ADF.USIM/5F3A/4F30': ['A80AC0034F3A02']})
        pbrs = _read(app)
        self.assertEqual([p['path'] for p in pbrs], ['ADF.USIM/5F3A/4F30'])

    def test_no_pbr_returns_none(self):
        app, _ = _app({})
        self.assertIsNone(_read(app))

    def test_a_transparent_file_is_skipped(self):
        app, _ = _app({'MF/7F10/5F3A/4F30': ['A80AC0034F3A02']})
        self.assertIsNone(_read(app, file_type='transparent'))

    def test_a_failing_read_skips_the_file(self):
        app, lchan = _app({'MF/7F10/5F3A/4F30': ['A80AC0034F3A02'],
                           'ADF.USIM/5F3A/4F30': ['A80AC0034F3A02']})
        orig = lchan.scc.send_apdu_checksw

        def flaky(pdu):
            if lchan._cur == 'MF/7F10/5F3A/4F30':
                raise RuntimeError('6A82')
            return orig(pdu)
        lchan.scc.send_apdu_checksw = flaky
        pbrs = _read(app)
        self.assertEqual([p['path'] for p in pbrs], ['ADF.USIM/5F3A/4F30'])

    def test_the_record_count_is_capped(self):
        app, _ = _app({'MF/7F10/5F3A/4F30': ['A80AC0034F3A02'] * 100})
        pbrs = _read(app)
        self.assertEqual(pbrs[0]['num_records'], PBR_RECORD_LIMIT)
        self.assertEqual(PBR_RECORD_LIMIT, 16)

    def test_records_are_uppercased_hex(self):
        app, _ = _app({'MF/7F10/5F3A/4F30': ['a80ac0034f3a02']})
        pbrs = _read(app)
        self.assertEqual(pbrs[0]['records'], ['A80AC0034F3A02'])

    def test_no_app_returns_none(self):
        self.assertIsNone(_read_pbr(None))
        self.assertIsNone(_read_pbr(mock.Mock(rs=None)))

    def test_the_paths_match_the_frontend_file_list(self):
        # the read paths must be the paths uicc_files.json ships for EF.PBR
        for path in PBR_READ_PATHS:
            self.assertIn('"%s"' % path, FRONTEND_PATHS, path)


class CacheTests(unittest.TestCase):
    def test_refresh_stores_and_clears(self):
        s = mock.Mock()
        with mock.patch('pysim_simple_server.server._read_pbr',
                        return_value=[{'fid': '4F30', 'num_records': 1}]), \
             mock.patch('pysim_simple_server.server._tlog'):
            _refresh_pbr_cache(s)
        self.assertEqual(s.pbr, [{'fid': '4F30', 'num_records': 1}])
        with mock.patch('pysim_simple_server.server._read_pbr', return_value=None):
            _refresh_pbr_cache(s)
        self.assertIsNone(s.pbr)

    def test_equip_and_disconnect_wiring(self):
        # the equip hook reads the cache; a disconnect clears it
        seg = SRC[SRC.index('def _apply_equipped_card'):SRC.index('def _card_reset_reinit')]
        self.assertIn('_refresh_pbr_cache(server)', seg)
        self.assertIn('server.pbr = None', seg)
        seg = SRC[SRC.index('def _handle_card_disconnect'):SRC.index('def _ensure_transport')]
        self.assertIn('_server_ref.pbr = None', seg)

    def test_the_startup_init_reads_the_pbr(self):
        # a card present at server start never runs _apply_equipped_card, so
        # the startup init reads the PBR in its own CAT-free window
        src = (ROOT / 'pysim_simple_server' / '__main__.py').read_text(encoding='utf-8')
        self.assertIn('pbr_files = _read_pbr(app)', src)
        self.assertIn('server.pbr = pbr_files', src)
        imp = src[src.index('from .server import'):]
        self.assertIn('_read_pbr', imp[:imp.index('\n')])


class PbrParserTests(unittest.TestCase):
    """The pure server-side PBR parser (mirrors the PWA decoder)."""

    def test_the_shared_vectors(self):
        # every case in pbr_vectors.json: the parser must agree with the PWA
        # decoder's links (fid/kind/link/sfi - a raw-preserved unknown tag is
        # not a phonebook file) and with the FID list.
        self.assertTrue(PBR_VECTORS['cases'])
        for c in PBR_VECTORS['cases']:
            parsed = _pbr_parse(c['records'])
            expected = []
            for l in c['links']:
                if not l.get('kind'):
                    continue        # a raw-preserved unknown tag is not a file
                if any(e['fid'] == l['fid'] for e in expected):
                    continue        # the parser deduplicates by FID (first wins)
                expected.append({'fid': l['fid'], 'kind': l['kind'], 'link': l['link'],
                                 'sfi': l['sfi']})
            self.assertEqual(parsed, expected, c['name'])
            self.assertEqual([f['fid'] for f in parsed], c['files'], c['name'])

    def test_the_pysim_fixture(self):
        self.assertEqual(_pbr_parse([FIXTURE]), [
            {'fid': '4F3A', 'kind': 'EF.ADN', 'link': 1, 'sfi': '02'},
            {'fid': '4F09', 'kind': 'EF.PBC', 'link': 1, 'sfi': '04'},
            {'fid': '4F3D', 'kind': 'EF.CCP1', 'link': 3, 'sfi': '07'},
            {'fid': '4F4A', 'kind': 'EF.EXT1', 'link': 3, 'sfi': '06'},
        ])

    def test_the_no_sfi_form_and_the_type_2_container(self):
        parsed = _pbr_parse(['A808C0024F3AC1024F09A90AC6034F0B06C7034F0C07'])
        self.assertEqual([f['sfi'] for f in parsed], [None, None, '06', '07'])
        self.assertEqual([f['link'] for f in parsed], [1, 1, 2, 2])
        self.assertEqual(parsed[1]['kind'], 'EF.IAP')

    def test_ff_padding_is_ignored(self):
        self.assertEqual(_pbr_parse([FIXTURE + 'FF' * 8]), _pbr_parse([FIXTURE]))

    def test_a_truncated_tlv_keeps_the_earlier_links(self):
        self.assertEqual([f['fid'] for f in _pbr_parse(['A80AC0034F3A02C5034F0904C5'])],
                         ['4F3A', '4F09'])
        # a container whose value runs past the record parses to nothing
        self.assertEqual(_pbr_parse(['A80AC0034F3A02C5034F09']), [])

    def test_an_unknown_primitive_tag_is_skipped(self):
        self.assertEqual([f['fid'] for f in _pbr_parse(['A80AC0034F3A02CE034F0904'])],
                         ['4F3A'])

    def test_a_bad_primitive_length_is_skipped(self):
        # 'CE 05' with five bytes: not a FID (2) and not FID + SFI (3)
        self.assertEqual([f['fid'] for f in _pbr_parse(['A80CC0034F3A02CE05AABBCCDDEE'])],
                         ['4F3A'])

    def test_dedupe_first_occurrence_wins(self):
        # a phonebook over 254 entries uses further records; a FID repeated in
        # a later record must not appear twice
        parsed = _pbr_parse([FIXTURE, 'A80AC0034F3A02'])
        self.assertEqual([f['fid'] for f in parsed], ['4F3A', '4F09', '4F3D', '4F4A'])
        self.assertEqual(len(parsed), 4)

    def test_tolerances(self):
        self.assertEqual(_pbr_parse(None), [])
        self.assertEqual(_pbr_parse([]), [])
        self.assertEqual(_pbr_parse(['']), [])
        self.assertEqual(_pbr_parse(['FFFFFFFF']), [])
        self.assertEqual(_pbr_parse(['zz']), [])
        self.assertEqual(_pbr_parse(['a808c0024f3ac1024f09'])[0]['fid'], '4F3A')
        self.assertEqual(_pbr_parse(['A808C0024F3AC1024F09', '4F'])[0]['fid'], '4F3A')


PBR_ENTRY = {'fid': '4F30', 'path': 'MF/7F10/5F3A/4F30',
             'record_len': 32, 'num_records': 1, 'records': [FIXTURE]}


def _server(pbrs):
    s = mock.Mock()
    s.pbr = pbrs
    return s


class PbrTreeTests(unittest.TestCase):
    """The /api/tree injection: the DF holding a cached EF.PBR gains the
    referenced phonebook files as children."""

    def test_norm_path(self):
        self.assertEqual(_pbr_norm_path(['3f00', '7f10', '5f3a', '4f30']),
                         ['MF', '7F10', '5F3A', '4F30'])
        self.assertEqual(_pbr_norm_path(['mf', '']), ['MF'])
        self.assertEqual(_pbr_norm_path(None), [])
        self.assertEqual(_pbr_norm_path(['ADF.USIM', '5F3A']), ['ADF.USIM', '5F3A'])

    def test_the_df_with_a_pbr_gets_the_children(self):
        s = _server([PBR_ENTRY])
        children = [{'name': 'EF.PBR', 'fid': '4f30', 'isDir': False}]
        self.assertEqual(_pbr_inject_children(s, ['MF', '7F10', '5F3A'], children), 4)
        self.assertEqual([c['fid'] for c in children],
                         ['4f30', '4F3A', '4F09', '4F3D', '4F4A'])
        self.assertEqual(children[1], {'name': 'EF.ADN', 'fid': '4F3A',
                                       'isDir': False, 'pbr': True, 'sfi': '02'})

    def test_a_full_df_path_is_never_answered_by_another_dfs_copy(self):
        # Only the ADF copy is cached: the MF/DF.TELECOM phonebook DF (a
        # different DF that happens to share the FID) must NOT be answered by
        # it - the FID-only fallback is for a bare DF request only.
        s = _server([dict(PBR_ENTRY, path='ADF.USIM/5F3A/4F30')])
        self.assertIsNone(_pbr_files_for_node(s, ['MF', '7F10', '5F3A']))
        self.assertIsNone(_pbr_files_for_node(s, ['MF', '5F3A']))
        children = []
        self.assertEqual(_pbr_inject_children(s, ['MF', '7F10', '5F3A'], children), 0)
        # ...while the bare DF request still resolves the single cached copy
        self.assertIsNotNone(_pbr_files_for_node(s, ['5F3A']))

    def test_an_adf_addressed_by_aid_resolves_to_its_name(self):
        # The Profiler's tree walk addresses an ADF by AID (the file manager by
        # name): both must fold onto the cache's name-based path.
        aid = 'A0000000871002FF33FF0181FFFF8900'
        self.assertEqual(_pbr_norm_path(['MF', aid, '5F3A'], {aid: 'ADF.USIM'}),
                         ['ADF.USIM', '5F3A'])
        self.assertEqual(_pbr_norm_path(['MF', 'ADF.USIM', '5F3A'], {aid: 'ADF.USIM'}),
                         ['ADF.USIM', '5F3A'])
        # with both copies cached the AID-shaped request still picks its own
        s = _server([dict(PBR_ENTRY, path='MF/7F10/5F3A/4F30'),
                     dict(PBR_ENTRY, path='ADF.USIM/5F3A/4F30')])
        children = []
        self.assertEqual(_pbr_inject_children(
            s, _pbr_norm_path(['MF', aid, '5F3A'], {aid: 'ADF.USIM'}), children), 4)

    def test_the_pwa_style_adf_path_matches_with_two_cached_pbrs(self):
        # The PWA sends the whole chain (MF/ADF.USIM/5F3A) while the cache path
        # is rooted at the ADF; with both PBR copies cached the ADF's DF must
        # still resolve to its own entry (the normaliser drops the leading MF).
        s = _server([dict(PBR_ENTRY, path='MF/7F10/5F3A/4F30'),
                     dict(PBR_ENTRY, path='ADF.USIM/5F3A/4F30')])
        children = []
        self.assertEqual(_pbr_inject_children(s, _pbr_norm_path(['MF', 'ADF.USIM', '5F3A']),
                                             children), 4)
        self.assertEqual(_pbr_norm_path(['MF', 'ADF.USIM', '5F3A']), ['ADF.USIM', '5F3A'])
        # ...and the MF/DF.TELECOM copy keeps matching exactly
        children2 = []
        self.assertEqual(_pbr_inject_children(s, _pbr_norm_path(['MF', '7F10', '5F3A']),
                                             children2), 4)
        # a bare FID stays ambiguous between the two copies: nothing is injected
        children3 = []
        self.assertEqual(_pbr_inject_children(s, ['5F3A'], children3), 0)
        self.assertEqual(children3, [])

    def test_the_adf_path_matches(self):
        s = _server([dict(PBR_ENTRY, path='ADF.USIM/5F3A/4F30')])
        children = []
        self.assertEqual(_pbr_inject_children(s, ['ADF.USIM', '5F3A'], children), 4)
        self.assertEqual(children[0]['fid'], '4F3A')

    def test_an_unrelated_df_is_untouched(self):
        s = _server([PBR_ENTRY])
        children = [{'name': 'EF.ADN', 'fid': '4f3a', 'isDir': False}]
        self.assertEqual(_pbr_inject_children(s, ['MF', '7F10', '5F3B'], children), 0)
        self.assertEqual(len(children), 1)
        self.assertEqual(_pbr_inject_children(_server(None), ['MF', '7F10', '5F3A'],
                                              children), 0)

    def test_a_known_fid_is_not_added_twice(self):
        # pySim's model already lists EF.ADN under DF.PHONEBOOK
        s = _server([PBR_ENTRY])
        children = [{'name': 'EF.ADN', 'fid': '4F3A', 'isDir': False}]
        self.assertEqual(_pbr_inject_children(s, ['MF', '7F10', '5F3A'], children), 3)
        self.assertEqual([c['fid'] for c in children], ['4F3A', '4F09', '4F3D', '4F4A'])

    def test_an_ambiguous_fid_only_path_is_not_guessed(self):
        # the three PBR copies all sit in a DF with FID 5F3A: a bare '5F3A'
        # cannot say which one, so nothing is injected
        s = _server([PBR_ENTRY, dict(PBR_ENTRY, path='ADF.USIM/5F3A/4F30'),
                     dict(PBR_ENTRY, path='ADF.USIM-NON-IMSI/5F3A/4F30')])
        children = []
        self.assertEqual(_pbr_inject_children(s, ['5F3A'], children), 0)
        self.assertEqual(children, [])

    def test_a_single_cached_entry_answers_a_bare_fid(self):
        children = []
        self.assertEqual(_pbr_inject_children(_server([PBR_ENTRY]), ['5F3A'], children), 4)

    def test_an_empty_path_leaves_children_untouched(self):
        children = []
        self.assertEqual(_pbr_inject_children(_server([PBR_ENTRY]), [], children), 0)
        self.assertEqual(children, [])

    def test_known_fid(self):
        s = _server([PBR_ENTRY])
        self.assertTrue(_pbr_known_fid(s, '4f3a'))
        self.assertTrue(_pbr_known_fid(s, '4F4A'))
        self.assertFalse(_pbr_known_fid(s, '4F30'))    # EF.PBR itself: model-known
        self.assertFalse(_pbr_known_fid(s, 'EF.ADN'))
        self.assertFalse(_pbr_known_fid(s, None))
        self.assertFalse(_pbr_known_fid(_server(None), '4F3A'))

    def test_the_handlers_inject_and_auto_allow_the_probe(self):
        # the /api/tree handler appends the PBR children...
        tree = SRC[SRC.index("elif self.path == '/api/tree':"):
                   SRC.index("elif self.path == '/api/menu-select':")]
        self.assertIn('_pbr_inject_children(self.server, node_path, children)', tree)
        # ...and every tree/select/read/write handler auto-allows the probe for
        # the PBR FIDs (after the caller's own allow_probe flag)
        for marker in ("elif self.path == '/api/select':", "elif self.path == '/api/read':",
                       "elif self.path == '/api/write':", "elif self.path == '/api/tree':"):
            i = SRC.index(marker)
            seg = SRC[i:i + 3000]
            self.assertIn('_pbr_known_fid(self.server', seg, marker)
            self.assertLess(seg.index("allow_probe = bool(body.get('allow_probe'))"),
                            seg.index('_pbr_known_fid(self.server'), marker)


class PbrRouteTests(unittest.TestCase):
    """GET /api/pbr serves the session cache without touching the card."""

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

    def test_pbr_is_a_card_free_get(self):
        self.assertIn('/api/pbr', _CARD_FREE_GET)

    def test_empty_cache_answers_not_found(self):
        status, resp = self._get('/api/pbr')
        self.assertEqual(status, 200, resp)
        self.assertFalse(resp['ok'])
        self.assertEqual(resp['pbrs'], [])

    def test_the_cache_is_served_with_the_session(self):
        self.server.pbr = [{'fid': '4F30', 'path': 'MF/7F10/5F3A/4F30',
                            'records': ['A80AC0034F3A02']}]
        self.server.card_session = 9
        status, resp = self._get('/api/pbr')
        self.assertEqual(status, 200, resp)
        self.assertTrue(resp['ok'])
        self.assertEqual(resp['session'], 9)
        self.assertEqual(resp['pbrs'][0]['fid'], '4F30')


if __name__ == '__main__':
    unittest.main()
