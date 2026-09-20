# coding=utf-8
"""Tests for the Network-state monitor (pure helpers)."""

import unittest

from pysim_simple_server import netsim, netstate


def file_entry(name, fid, data, kind='transparent', source='init'):
    e = {'name': name, 'fid': fid, 'present': True, 'kind': kind,
         'source': source, 'updated': 1.0}
    if kind == 'record':
        e['records'] = [{'num': i + 1, 'data': d} for i, d in enumerate(data)]
    else:
        e['data'] = data
    return e


def state_with(**files):
    st = netstate.new_state()
    for key, entry in files.items():
        st['files'][key] = entry
    return st


OP_LIST = [
    {'mcc': '262', 'mnc': '01', 'countryName': 'Germany', 'brand': 'Telekom',
     'operator': 'Telekom Deutschland GmbH'},
    {'mcc': '262', 'mnc': '02', 'countryName': 'Germany', 'brand': 'Vodafone',
     'operator': 'Vodafone GmbH'},
    {'mcc': '246', 'mnc': '81', 'countryName': 'Finland', 'brand': 'Elisa',
     'operator': 'Elisa Oyj'},
]


class DecodeTests(unittest.TestCase):
    def test_parse_imsi_matches_the_pysim_vector(self):
        # pySim/EF vector: 228-06 IMSI
        self.assertEqual(netstate.parse_imsi('082982608200002080'),
                         '228062800000208')
        self.assertEqual(netstate.parse_imsi(''), None)

    def test_plmn_from_hex(self):
        self.assertEqual(netstate.plmn_from_hex('22F860'),
                         {'mcc': '228', 'mnc': '06', 'plmn': '22806'})
        self.assertEqual(netstate.plmn_from_hex('62F210'),
                         {'mcc': '262', 'mnc': '01', 'plmn': '26201'})
        self.assertIsNone(netstate.plmn_from_hex('62F2F0'))
        self.assertIsNone(netstate.plmn_from_hex('FFFFFF'))
        self.assertIsNone(netstate.plmn_from_hex('FE'))

    def test_plmn_list_skips_gaps_without_terminating(self):
        # TS 31.102 4.2.16: FFFFFF may appear in any position, not as an end
        entries = netstate._plmn_hex_list('22F860' + 'FFFFFF' + '62F210')
        self.assertEqual(entries, ['22F860', '62F210'])


class NetworkTests(unittest.TestCase):
    def test_location_prefers_epsloci_and_falls_back_to_loci(self):
        st = state_with(
            loci=file_entry('EF.LOCI', '6F7E', 'FFFFFFFF62F2106CD7FF01'),
            psloci=file_entry('EF.PSLOCI', '6F73',
                              'FFFFFFFFFFFFFF62F2106CD7CA01'),
            epsloci=file_entry('EF.EPSLOCI', '6FE3',
                               '0BF6' + 'FF' * 13 + 'FFFE' + '01'))
        loc = netstate._current_location(st['files'])
        # wiped EPSLOCI -> RAI from PSLOCI
        self.assertEqual(loc['plmn'], '26201')
        self.assertEqual(loc['area'], 'RAI')
        self.assertEqual(loc['lac'], '6CD7')
        st['files']['psloci'] = file_entry('EF.PSLOCI', '6F73', 'FF' * 14)
        loc = netstate._current_location(st['files'])
        self.assertEqual(loc['area'], 'LAI')

    def test_network_home_equivalent_and_guest(self):
        # HPLMN 262-01 (HPLMNwAcT first record), EHPLMN 228-06
        base = dict(
            hplmnwact=file_entry('EF.HPLMNwAcT', '6F62', '62F210' + '0000'),
            ehplmn=file_entry('EF.EHPLMN', '6FD9', '22F860'))
        st = state_with(epsloci=file_entry('EF.EPSLOCI', '6FE3',
                                           'AB' * 12 + '62F210' + '8001' + '00'),
                        **base)
        net = netstate.compute_network(st, OP_LIST)
        self.assertEqual(net['location']['plmn'], '26201')
        self.assertEqual(net['location']['country'], 'Germany')
        self.assertEqual(net['location']['operator'], 'Telekom')
        self.assertEqual(net['location']['roaming'], 'home')
        self.assertFalse(net['location']['rejected'])
        # equivalent home
        st['files']['epsloci'] = file_entry('EF.EPSLOCI', '6FE3',
                                            'AB' * 12 + '22F860' + '8001' + '00')
        net = netstate.compute_network(st, OP_LIST)
        self.assertEqual(net['location']['roaming'], 'equivalent')
        # guest
        st['files']['epsloci'] = file_entry('EF.EPSLOCI', '6FE3',
                                            'AB' * 12 + '00F110' + '8001' + '00')
        net = netstate.compute_network(st, OP_LIST)
        self.assertEqual(net['location']['roaming'], 'guest')
        self.assertIsNone(net['location']['country'])   # 001 not in the list

    def test_home_falls_back_to_the_imsi(self):
        st = state_with(
            imsi=file_entry('EF.IMSI', '6F07', '082982608200002080'),
            epsloci=file_entry('EF.EPSLOCI', '6FE3',
                               'AB' * 12 + '22F860' + '8001' + '00'))
        home, eq = netstate._home_sets(st['files'])
        # the IMSI does not encode the MNC length, so both interpretations
        # are kept as home candidates
        self.assertIn('22F860', home)
        self.assertEqual(eq, set())
        net = netstate.compute_network(st, OP_LIST)
        self.assertEqual(net['location']['roaming'], 'home')

    def test_rejection_fingerprint(self):
        st = state_with(loci=file_entry('EF.LOCI', '6F7E',
                                        'FFFFFFFF62F210FFFEFF02'))
        net = netstate.compute_network(st, OP_LIST)
        self.assertTrue(net['location']['rejected'])
        # non-empty FPLMN also marks a rejection
        st = state_with(
            loci=file_entry('EF.LOCI', '6F7E', 'FFFFFFFF62F210FFFEFF01'),
            fplmn=file_entry('EF.FPLMN', '6F7B', '22F860' + 'FF' * 9))
        net = netstate.compute_network(st, OP_LIST)
        self.assertTrue(net['location']['rejected'])
        # ... but an empty FPLMN with update status 01 is just "no service"
        st = state_with(
            loci=file_entry('EF.LOCI', '6F7E', 'FFFFFFFF62F210FFFEFF01'),
            fplmn=file_entry('EF.FPLMN', '6F7B', 'FF' * 12))
        net = netstate.compute_network(st, OP_LIST)
        self.assertFalse(net['location']['rejected'])

    def test_service_passthrough_and_steps(self):
        st = state_with()
        netstate.set_service(st, netstate.SERVICE_LIMITED, 'net-sim:limited_service')
        self.assertEqual(st['service']['state'], 'limited')
        netstate.apply_steps(st, [
            {'action': 'update_binary', 'key': 'loci', 'path': 'ADF.USIM/6F7E',
             'data': 'aabb'},
            {'action': 'update_record', 'key': 'epsnsc', 'path': 'ADF.USIM/6FE4',
             'data': 'ccdd', 'record': 1},
            {'action': 'skip', 'file': 'kc'},
        ])
        self.assertEqual(st['files']['loci']['data'], 'AABB')
        self.assertEqual(st['files']['loci']['source'], 'write')
        self.assertEqual(st['files']['epsnsc']['kind'], 'record')
        self.assertEqual(st['files']['epsnsc']['records'],
                         [{'num': 1, 'data': 'CCDD'}])
        net = netstate.compute_network(st, OP_LIST)
        self.assertEqual(net['service']['state'], 'limited')

    def test_scenario_service_mapping_covers_the_location_scenarios(self):
        for scenario in ('cold_boot', 'attach_eps', 'attach_2g',
                         'service_lost', 'limited_service', 'roaming_denied',
                         'sms_received'):
            self.assertIn(scenario, netsim.SCENARIO_SERVICE)
        for scenario in ('churn', 'cb_reconfig', 'authenticate'):
            self.assertNotIn(scenario, netsim.SCENARIO_SERVICE)


if __name__ == '__main__':
    unittest.main()
