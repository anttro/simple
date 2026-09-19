#!/usr/bin/env python3
"""Tests for the network-condition simulation (netsim.py).

Builders follow the trace study in `projects/UICC_NAA.md` (section 13):
EPS NAS security context, LOCI/PSLOCI/EPSLOCI real and dummy forms, Kc,
SMS-status counter, CBMI/CBMIR, Location status events and AUTHENTICATE.
The runner test drives a fake card so no hardware is needed.
"""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

PROJECTS = Path(__file__).resolve().parents[2]
PY_SIM = PROJECTS / 'pysim'
if str(PY_SIM) not in sys.path:
    sys.path.insert(0, str(PY_SIM))

from pysim_simple_server import netsim


class BuilderTests(unittest.TestCase):
    def test_plmn_bcd_round_trip_with_known_vectors(self):
        # pySim PLMNsel test vector: 228/06 -> 22 F8 60
        self.assertEqual(netsim.plmn_bcd('228', '06'), '22F860')
        # two-digit MNC pads the third nibble with F, three-digit does not
        self.assertEqual(netsim.plmn_bcd('262', '01'), '62F210')
        self.assertEqual(netsim.plmn_bcd('262', '001'), '621200')
        with self.assertRaises(ValueError):
            netsim.plmn_bcd('26', '01')
        with self.assertRaises(ValueError):
            netsim.plmn_bcd('262', '1')

    def test_loci_real_and_dummy(self):
        real = netsim.build_loci('3E905D6B', '52F002', '6CD7', 0x00)
        self.assertEqual(len(real) // 2, 11)
        self.assertEqual(real, '3E905D6B52F0026CD7FF00')
        dummy = netsim.build_loci_dummy('52F002')
        self.assertEqual(dummy, 'FFFFFFFF52F002FFFEFF01')
        # PLMN kept, LAC FFFE, status "not updated"

    def test_psloci_real_and_dummy(self):
        real = netsim.build_psloci('F9236619', 'FFFFFF', '52F002', '6CD7', 'CA', 0x00)
        self.assertEqual(len(real) // 2, 14)
        self.assertEqual(real[-2:], '00')
        dummy = netsim.build_psloci_dummy('52F002')
        self.assertEqual(dummy, 'FFFFFFFFFFFFFF52F002FFFEFF01')

    def test_epsloci_real_and_dummy(self):
        real = netsim.build_epsloci('AB' * 12, '52F099', '8001', 0x00)
        self.assertEqual(len(real) // 2, 18)
        self.assertEqual(real[24:30], '52F099')   # TAI PLMN after the 12-byte GUTI
        self.assertEqual(real[-2:], '00')
        dummy = netsim.build_epsloci_dummy('52F099')
        self.assertEqual(len(dummy) // 2, 18)
        self.assertTrue(dummy.startswith('0BF652F099'))
        self.assertEqual(dummy[10:24], 'FF' * 7)  # identity wiped
        self.assertTrue(dummy.endswith('52F099FFFF01'))

    def test_epsnsc_record_layout_and_padding(self):
        rec = netsim.build_epsnsc(0x03, 'AB' * 32, 0x0E, 0x08, 0x02)
        data = bytes.fromhex(rec)
        self.assertEqual(len(data), 54)
        self.assertEqual(data[:2], b'\xA0\x34')
        self.assertEqual(data[2:5], b'\x80\x01\x03')
        self.assertEqual(data[5:7], b'\x81\x20')
        self.assertEqual(data[7:39], b'\xAB' * 32)
        self.assertEqual(data[39:45], b'\x82\x04\x00\x00\x00\x0E')
        self.assertEqual(data[45:51], b'\x83\x04\x00\x00\x00\x08')
        self.assertEqual(data[51:54], b'\x84\x01\x02')
        # card-specific record size 80 is padded with FF, no truncation
        rec80 = netsim.build_epsnsc(0x04, 'CD' * 32, 1, 2, 1, size=80)
        self.assertEqual(len(bytes.fromhex(rec80)), 80)
        self.assertEqual(bytes.fromhex(rec80)[54:], b'\xFF' * 26)

    def test_epsnsc_invalidate_and_keep_key(self):
        wipe = netsim.build_epsnsc_invalidate(54)
        data = bytes.fromhex(wipe)
        self.assertEqual(data[2:5], b'\x80\x01\x07')
        self.assertEqual(data[7:39], b'\xFF' * 32)
        self.assertEqual(data[39:51],
                         b'\x82\x04\xFF\xFF\xFF\xFF\x83\x04\xFF\xFF\xFF\xFF')
        keep = netsim.build_epsnsc_invalidate(54, 'AB' * 32)
        self.assertEqual(bytes.fromhex(keep)[7:39], b'\xAB' * 32)
        self.assertEqual(netsim.parse_epsnsc_kasme(keep), 'AB' * 32)
        self.assertIsNone(netsim.parse_epsnsc_kasme(wipe))

    def test_kc_forms(self):
        self.assertEqual(netsim.build_kc('0123456789ABCDEF', 0x01, 9),
                         '0123456789ABCDEF01')
        self.assertEqual(len(bytes.fromhex(netsim.build_kc('01' * 8, 1, 33))), 33)
        self.assertEqual(netsim.build_kc_invalidate(1), '07')
        self.assertEqual(netsim.build_kc_invalidate(9), 'FFFFFFFFFFFFFFFF07')
        self.assertEqual(netsim.build_kc_invalidate(33), '07' + 'FF' * 32)

    def test_smsstatus_counter(self):
        self.assertEqual(netsim.build_smsstatus(0x27FF), '27FF')
        self.assertEqual(netsim.parse_smsstatus('27FF'), 0x27FF)
        self.assertEqual(netsim.bump_smsstatus('27FF'), '2800')
        self.assertIsNone(netsim.parse_smsstatus('FFFF'))
        self.assertEqual(netsim.bump_smsstatus('FFFF'), '01FF')

    def test_cbmi_and_cbmir(self):
        self.assertEqual(netsim.build_cbmi([0x111F, 0x1112], 20),
                         '111F1112' + 'FF' * 16)
        self.assertEqual(netsim.build_cbmir([[0x111F, 0x111F]], 8),
                         '111F111FFFFFFFFF')
        # FF-cleared list
        self.assertEqual(netsim.build_cbmi([], 4), 'FFFFFFFF')

    def test_location_status_event(self):
        no_service = netsim.build_location_status_event(netsim.LOC_STATUS_NO_SERVICE)
        self.assertEqual(no_service, '9B0102')
        limited = netsim.build_location_status_event(netsim.LOC_STATUS_LIMITED)
        self.assertEqual(limited, '9B0101')
        # normal service may carry the Location information object (8.19)
        normal = netsim.build_location_status_event(
            netsim.LOC_STATUS_NORMAL, '52F099', '6CD7', '1234')
        self.assertEqual(normal, '9B01001307' + '52F099' + '6CD7' + '1234')

    def test_authenticate_apdu_and_response(self):
        apdu = netsim.build_auth_apdu('11' * 16, '22' * 16)
        self.assertTrue(apdu.startswith('0088008122'))
        self.assertEqual(apdu[10:12], '10')
        self.assertEqual(apdu[12:44], '11' * 16)
        self.assertEqual(apdu[44:46], '10')
        self.assertEqual(apdu[46:78], '22' * 16)
        self.assertEqual(len(apdu) // 2, 39)   # 5 header + 34 data
        parsed = netsim.parse_auth_response('DB10' + 'AA' * 16 + '10' + 'BB' * 16)
        self.assertEqual(parsed['type'], 'success')
        self.assertEqual(parsed['objects'][0], 'AA' * 16)
        sync = netsim.parse_auth_response('DC10' + 'CC' * 16)
        self.assertEqual(sync['type'], 'synchronisation_failure')
        self.assertEqual(sync['objects'], ['CC' * 16])
        self.assertIsNone(netsim.parse_auth_response('9000'))


class FakeFileInfo:
    def __init__(self, size=None, record_len=None, num=1, data=''):
        self.size = size
        self.record_len = record_len
        self.num = num
        self.data = data


class FakeLchan:
    def __init__(self, files):
        self.files = files
        self.selected = None
        self.writes = []

    def select_by_path(self, path, app):
        fid = path.split('/')[-1].upper()
        if fid not in self.files:
            raise RuntimeError('file not found: %s' % path)
        self.selected = fid
        return SimpleNamespace(fid=fid.lower()), None

    def selected_file_size(self):
        return self.files[self.selected].size

    def selected_file_record_len(self):
        return self.files[self.selected].record_len

    def selected_file_num_of_rec(self):
        return self.files[self.selected].num

    def update_binary(self, data, offset=0):
        self.writes.append(('binary', self.selected, data))
        return data, '9000'

    def update_record(self, rec, data):
        self.writes.append(('record', self.selected, data))
        return data, '9000'

    def read_binary(self, length=None, offset=0):
        return self.files[self.selected].data, '9000'

    def read_record(self, rec):
        return self.files[self.selected].data, '9000'


class FakeScc:
    def __init__(self):
        self.apdus = []
        self._tp = SimpleNamespace(send_apdu=self._send)

    def _send(self, apdu):
        self.apdus.append(apdu)
        if apdu.upper().startswith('00C0'):    # the 61xx follow-up GET RESPONSE
            return 'DB10' + 'AA' * 16, '9000'
        return '', '6102'


class FakeSrv:
    def __init__(self, scc=None):
        self._server_ref = SimpleNamespace(scc=scc or FakeScc())
        self.events = []

    def _select_path(self, lchan, path, app):
        return lchan.select_by_path(path, app)

    def _send_event_download(self, scc, event_type, event_data=None):
        self.events.append((event_type, bytes(event_data or b'').hex().upper()))
        return b'', '9000'


FILES = {
    '6FE4': FakeFileInfo(record_len=54, num=1, data=netsim.build_epsnsc(0x03, 'AB' * 32, 0, 0, 2)),
    '6F7E': FakeFileInfo(size=11),
    '6F73': FakeFileInfo(size=14),
    '6FE3': FakeFileInfo(size=18),
    '4F20': FakeFileInfo(size=9),
    '4F52': FakeFileInfo(size=9),
    '6F43': FakeFileInfo(size=2, data='27FF'),
    '6F45': FakeFileInfo(size=20),
    '6F50': FakeFileInfo(size=40),
}


def make_runner(params=None, event_list=(3,), sleep=None):
    lchan = FakeLchan(dict(FILES))
    app = SimpleNamespace(rs=SimpleNamespace(lchan=[lchan]))
    srv = FakeSrv()
    runner = netsim.NetSimRunner(srv, app, params=params, event_list=event_list,
                                 sleep=sleep or (lambda s: None))
    return runner, lchan, srv


class RunnerTests(unittest.TestCase):
    def test_cold_boot_invalidates_and_dummies_locations(self):
        runner, lchan, srv = make_runner()
        out = runner.run('cold_boot')
        self.assertTrue(out['success'])
        writes = [(w[0], w[1]) for w in lchan.writes]
        self.assertEqual(writes, [('record', '6FE4'), ('binary', '6F7E'),
                                  ('binary', '6F73'), ('binary', '6FE3')])
        epsnsc = lchan.writes[0][2]
        self.assertTrue(epsnsc.startswith('A0348001078120' + 'FF' * 32))
        # dummy LOCI: TMSI FF, PLMN 001-01 (00 F1 10), LAC FFFE, status 01
        self.assertEqual(lchan.writes[1][2], 'FFFFFFFF00F110FFFEFF01')

    def test_service_lost_keeps_the_old_kasme_and_writes_dummies(self):
        runner, lchan, srv = make_runner()
        out = runner.run('service_lost')
        self.assertTrue(out['success'])
        self.assertEqual(srv.events[0][0], 3)
        self.assertEqual(srv.events[0][1], '9B0102')
        epsnsc = [w for w in lchan.writes if w[1] == '6FE4'][0][2]
        self.assertTrue(epsnsc.startswith('A0348001078120' + 'AB' * 32))
        keys = [w[1] for w in lchan.writes if w[0] == 'binary']
        self.assertIn('6F7E', keys)
        self.assertIn('6F73', keys)
        self.assertIn('6FE3', keys)
        self.assertIn('4F20', keys)   # Kc invalidate (07 form)
        kc = [w for w in lchan.writes if w[1] == '4F20'][0][2]
        self.assertEqual(kc, 'FFFFFFFFFFFFFFFF07')

    def test_event_step_skipped_when_not_subscribed(self):
        runner, lchan, srv = make_runner(event_list=[])
        out = runner.run('service_lost')
        self.assertTrue(out['success'])
        self.assertEqual(srv.events, [])
        self.assertTrue(any(s.get('note') for s in out['steps']))

    def test_attach_eps_stores_context_and_real_locations(self):
        runner, lchan, srv = make_runner({'ksi': '04', 'kasme': 'CD' * 32,
                                          'ul': 1, 'dl': 2, 'algo': '02'})
        out = runner.run('attach_eps')
        self.assertTrue(out['success'])
        epsnsc = [w for w in lchan.writes if w[1] == '6FE4'][0][2]
        self.assertTrue(epsnsc.startswith('A0348001048120' + 'CD' * 32))
        loci = [w for w in lchan.writes if w[1] == '6F7E'][0][2]
        self.assertTrue(loci.endswith('00'))
        self.assertEqual(len(loci) // 2, 11)

    def test_sms_received_bumps_the_counter(self):
        runner, lchan, srv = make_runner()
        out = runner.run('sms_received')
        self.assertTrue(out['success'])
        sms = [w for w in lchan.writes if w[1] == '6F43'][0][2]
        self.assertEqual(sms, '2800')

    def test_churn_alternates_real_and_invalid_with_delays(self):
        delays = []
        runner, lchan, srv = make_runner({'churn_count': 2, 'churn_delay_ms': 10},
                                         sleep=lambda s: delays.append(s))
        out = runner.run('churn')
        self.assertTrue(out['success'])
        recs = [w[2] for w in lchan.writes if w[1] == '6FE4']
        self.assertEqual(len(recs), 4)
        self.assertTrue(recs[0].startswith('A0348001018120'))
        self.assertTrue(recs[1].startswith('A0348001078120'))
        self.assertEqual(len(delays), 4)

    def test_authenticate_sends_apdu_and_parses_response(self):
        runner, lchan, srv = make_runner()
        out = runner.run('authenticate')
        self.assertTrue(out['success'])
        apdus = srv._server_ref.scc.apdus
        self.assertTrue(apdus[0].startswith('0088008122'))
        self.assertTrue(apdus[1].startswith('00C00000'))
        step = [s for s in out['steps'] if s['action'] == 'authenticate'][0]
        self.assertEqual(step['sw'], '9000')
        self.assertEqual(step['parsed']['type'], 'success')

    def test_unknown_scenario_raises(self):
        runner, _lchan, _srv = make_runner()
        with self.assertRaises(ValueError):
            runner.run('nope')


if __name__ == '__main__':
    unittest.main()
