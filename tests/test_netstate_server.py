# coding=utf-8
"""Tests for the server-side Network-state monitor plumbing.

Covers the read helper (candidate order, transparent vs record files, absent
files) and the install/ensure helpers shared by the equip paths and the HTTP
endpoints.  The card I/O is faked; ``_select_path`` is stubbed so no pySim
file tree is needed.
"""

import unittest
from types import SimpleNamespace

from pysim_simple_server import server


class FakeLchan:
    def __init__(self, files):
        self.files = files          # path -> ('transparent', hex) | ('record', [hex])
        self.calls = []
        self.selected = None
        self.selected_file = None
        self.selected_file_fcp = True
        self.selected_file_type = lambda: 'ef'
        self.selected_file_structure = lambda: self.files[self.selected][0]
        self.selected_file_num_of_rec = lambda: len(self.files[self.selected][1])
        self.read_binary = lambda: (self.files[self.selected][1], '9000')
        self.read_record = lambda i: (self.files[self.selected][1][i - 1], '9000')

    def select(self, path):
        self.calls.append(('select', path))
        if path not in self.files:
            raise RuntimeError('file not found: %s' % path)
        self.selected = path


class NetStateServerTests(unittest.TestCase):
    def setUp(self):
        self._orig_select = server._select_path

        def fake_select(lchan, path, app):
            lchan.select(path)
            return None, None

        server._select_path = fake_select

    def tearDown(self):
        server._select_path = self._orig_select

    def test_read_walks_candidates_and_marks_missing(self):
        lchan = FakeLchan({
            'DF.GSM/6F07': ('transparent', '0829AA'),        # 2nd IMSI candidate
            'ADF.USIM/6FE4': ('linear_fixed', ['AABB', 'CCDD']),  # EPSNSC records
        })
        app = SimpleNamespace(rs=SimpleNamespace(lchan=[lchan]))
        out = server._netstate_read(app, ['imsi', 'epsnsc', 'spdi'])
        self.assertTrue(out['imsi']['present'])
        self.assertEqual(out['imsi']['path'], 'DF.GSM/6F07')
        self.assertEqual(out['imsi']['kind'], 'transparent')
        self.assertEqual(out['imsi']['data'], '0829AA')
        self.assertEqual(out['epsnsc']['kind'], 'record')
        self.assertEqual(out['epsnsc']['records'],
                         [{'num': 1, 'data': 'AABB'},
                          {'num': 2, 'data': 'CCDD'}])
        self.assertFalse(out['spdi']['present'])
        # candidates are walked in FILE_DEFS order (spdi sits before epsnsc)
        self.assertEqual(lchan.calls,
                         [('select', 'ADF.USIM/6F07'),
                          ('select', 'DF.GSM/6F07'),
                          ('select', 'ADF.USIM/6FCD'),
                          ('select', 'ADF.USIM/6FE4')])

    def test_install_builds_the_state_and_network(self):
        files = {'imsi': {'name': 'EF.IMSI', 'fid': '6F07', 'present': True,
                          'kind': 'transparent', 'data': '082982608200002080'}}
        srv = SimpleNamespace(mcc_mnc_path=None)
        server._netstate_install(srv, files)
        self.assertEqual(srv.net_state['files']['imsi']['source'], 'init')
        self.assertEqual(srv.net_state['files']['imsi']['data'],
                         '082982608200002080')
        self.assertIsNotNone(srv.net_state['read_at'])
        self.assertIsNotNone(srv.net_state['network'])

    def test_ensure_creates_the_state_only_with_a_readable_iccid(self):
        lchan = FakeLchan({})
        app = SimpleNamespace(rs=SimpleNamespace(lchan=[lchan]))
        srv = SimpleNamespace(iccid='8970119000004600098', app=app,
                              mcc_mnc_path=None)
        state = server._netstate_ensure(srv)
        self.assertIsNotNone(state)
        self.assertEqual(srv.net_state, state)
        srv2 = SimpleNamespace(iccid=None, app=app, mcc_mnc_path=None)
        self.assertIsNone(server._netstate_ensure(srv2))
        self.assertFalse(hasattr(srv2, 'net_state'))


if __name__ == '__main__':
    unittest.main()
