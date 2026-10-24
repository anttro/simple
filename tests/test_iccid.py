#!/usr/bin/env python3
"""Tests for the EF.ICCID helpers (decode + best-effort read after equip).

EF.ICCID (MF/2FE2) is a mandatory transparent EF carrying the nibble-swapped
E.118 digit string with an optional trailing 'F' pad (TS 102 221 13.2 /
TS 151 011 10.2). The equip flow reads it so the PWA can auto-select the
matching card preset; the read is optional and must never break an equip.
"""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

PROJECTS = Path(__file__).resolve().parents[2]
PY_SIM = PROJECTS / 'pysim'
if str(PY_SIM) not in sys.path:
    sys.path.insert(0, str(PY_SIM))

from pysim_simple_server import server
from pysim_simple_server.server import _decode_iccid, _read_iccid


class FakeFile:
    def __init__(self, fid=None, name=None, parent=None):
        self.fid = fid
        self.name = name
        self.parent = parent
        self.sfid = None
        self.children = {}
        self.applications = {}


class FakeScc:
    cla_byte = '00'
    max_cmd_len = 255

    def __init__(self, lchan):
        self.lchan = lchan
        self.sent = []

    def send_apdu_checksw(self, pdu):
        self.sent.append(pdu)
        if self.lchan.error:
            raise self.lchan.error
        return self.lchan.data, '9000'


class FakeLchan:
    def __init__(self, mf, data=None, error=None):
        self.selected_file = mf
        self.data = data
        self.error = error
        self.selects = []
        self.probes = []
        self.scc = FakeScc(self)

    def selected_file_size(self):
        return len(self.data or '') // 2

    def select_file(self, f, app=None):
        self.selected_file = f
        self.selects.append(f)

    def probe_file(self, fid, app=None):
        self.probes.append(fid)
        f = FakeFile(fid=fid, name='EF.' + fid.upper(), parent=self.selected_file)
        self.selected_file.children[fid] = f
        self.selected_file = f

    def read_binary(self, length=None, offset=0):
        if self.error:
            raise self.error
        return self.data, '9000'


def make_app(with_iccid=True, data='980711090000640090F8', error=None):
    mf = FakeFile('3f00', 'MF')
    iccid = None
    if with_iccid:
        iccid = FakeFile('2fe2', 'EF.ICCID', mf)
        mf.children['2fe2'] = iccid
    lchan = FakeLchan(mf, data=data, error=error)
    rs = SimpleNamespace(mf=mf, lchan=[lchan])
    app = SimpleNamespace(rs=rs)
    return app, lchan, mf, iccid


class DecodeIccidTest(unittest.TestCase):
    def test_live_card_style_content_with_f_pad(self):
        # 98 07 11 09 00 00 64 00 90 F8 -> 8970119000004600098
        self.assertEqual(_decode_iccid('980711090000640090F8'), '8970119000004600098')

    def test_even_length_digit_string_is_kept_whole(self):
        self.assertEqual(_decode_iccid('21436587092143658709'), '12345678901234567890')

    def test_odd_length_hex_is_rejected(self):
        self.assertIsNone(_decode_iccid('98071109000064009F8'))

    def test_non_hex_and_empty_are_rejected(self):
        self.assertIsNone(_decode_iccid(''))
        self.assertIsNone(_decode_iccid(None))
        self.assertIsNone(_decode_iccid('zzzz'))
        self.assertIsNone(_decode_iccid('abcd'))     # swaps to non-digits
        self.assertIsNone(_decode_iccid('FFFF'))     # only the pad


class ReadIccidTest(unittest.TestCase):
    def test_reads_and_decodes_the_model_file(self):
        app, lchan, mf, iccid = make_app()
        self.assertEqual(_read_iccid(app), '8970119000004600098')
        self.assertIs(lchan.selected_file, iccid)
        self.assertEqual([f.name for f in lchan.selects], ['MF', 'EF.ICCID'])
        self.assertEqual(lchan.probes, [])
        # one READ BINARY, no second SELECT (v3.38.1)
        self.assertEqual(lchan.scc.sent, ['00b000000a'])

    def test_missing_model_entry_is_probed_and_detached(self):
        app, lchan, mf, _ = make_app(with_iccid=False)
        self.assertEqual(_read_iccid(app), '8970119000004600098')
        self.assertEqual(lchan.probes, ['2fe2'])
        self.assertIs(lchan.selected_file, mf)      # previous selection restored
        self.assertNotIn('2fe2', mf.children)       # probe entry detached

    def test_card_errors_are_not_fatal(self):
        app, _, _, _ = make_app(error=Exception('SW 6A82'))
        self.assertIsNone(_read_iccid(app))

    def test_no_app_or_state(self):
        self.assertIsNone(_read_iccid(None))
        self.assertIsNone(_read_iccid(SimpleNamespace(rs=None)))


class EquipFlowIccidTest(unittest.TestCase):
    def _apply(self, read_result):
        scc = SimpleNamespace(cat_cla=None)
        card = SimpleNamespace(_scc=scc)
        app = SimpleNamespace(card=card)
        srv = SimpleNamespace(app=app, terminal_profile='FF', card=None, scc=None)
        with mock.patch.object(server, '_cancel_menu_timeout'), \
             mock.patch.object(server, '_timer_cancel'), \
             mock.patch.object(server, '_reset_proactive_log'), \
             mock.patch.object(server, '_poll_enable'), \
             mock.patch.object(server, '_send_terminal_profile', return_value=(None, None)), \
             mock.patch.object(server, '_tlog'), \
             mock.patch.object(server, '_read_iccid', return_value=read_result):
            server._apply_equipped_card(srv)
        return srv

    def test_equip_records_the_iccid(self):
        srv = self._apply('8970119000004600098')
        self.assertEqual(srv.iccid, '8970119000004600098')

    def test_unreadable_iccid_leaves_it_none(self):
        srv = self._apply(None)
        self.assertIsNone(srv.iccid)

    def test_disconnect_clears_the_iccid(self):
        old_ref = server._server_ref
        old_connected = server._CARD_CONNECTED
        ref = SimpleNamespace(iccid='8970119000004600098', card='x', scc='y')
        try:
            server._server_ref = ref
            with mock.patch.object(server, '_poll_disable'), \
                 mock.patch.object(server, '_cancel_menu_timeout'), \
                 mock.patch.object(server, '_timer_cancel'), \
                 mock.patch.object(server, '_reset_proactive_log'):
                server._handle_card_disconnect()
            self.assertIsNone(ref.iccid)
            self.assertFalse(server._CARD_CONNECTED)
        finally:
            server._server_ref = old_ref
            server._CARD_CONNECTED = old_connected


if __name__ == '__main__':
    unittest.main()
