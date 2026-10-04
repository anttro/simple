"""Server-side event-data builders (the semantic test-script `event` action).

The byte vectors are generated from the PWA's own `EVENT_FORMS` builders (the
Node generator run over `frontend/index.html`) and pinned here, so the two
implementations cannot drift apart: every vector below is asserted by the
frontend suite too (or was produced by the very code it asserts).
"""

import time
import unittest

from pysim_simple_server import events


def st(year=2026, mon=9, day=30, hour=21, minute=55, sec=0):
    return time.struct_time((year, mon, day, hour, minute, sec, 0, 0, -1))


class EventVectorTests(unittest.TestCase):
    def test_location_status(self):
        cases = [
            ({'status': '0', 'mcc': '250', 'mnc': '01', 'lac': '00FF', 'cell': '0001'},
             '9B0100930752F01000FF0001'),
            ({'status': '1', 'mcc': '250'}, '9B0101'),
            ({'status': '0'}, '9B0100'),
            ({'status': '0', 'mcc': '310', 'mnc': '410', 'lac': 'ABCD', 'cell': '1234'},
             '9B01009307130014ABCD1234'),
        ]
        for fields, want in cases:
            self.assertEqual(events.build(0x03, fields).upper(), want)

    def test_access_tech(self):
        for fields, want in [({'tech': '0'}, 'BF0100'), ({'tech': '8'}, 'BF0108'),
                             ({'tech': '12'}, 'BF010C')]:
            self.assertEqual(events.build(0x0B, fields).upper(), want)

    def test_network_rejection(self):
        cases = [
            # location updating -> LAI, defaults filled
            ({'reg_type': '0'}, 'F40100BF0100F50102930552F0100000'),
            # GPRS -> RAI
            ({'reg_type': '3', 'mcc': '262', 'mnc': '02', 'lac': '00FF',
              'rac': '05', 'access_tech': '3', 'cause': '15'},
             'F40103BF0103F5010FF30662F22000FF05'),
            # EPS -> TAI (6-hex TAC) + the extended EMM cause
            ({'reg_type': '9', 'mcc': '310', 'mnc': '260', 'tac': 'A1B2C3',
              'access_tech': '8', 'cause': '7', 'ext_cause': '22'},
             'F40109BF0108F501077D06130062A1B2C3570116'),
            # 5GS -> TAI (4-hex TAC)
            ({'reg_type': '17', 'tac': '0001', 'access_tech': '10', 'cause': '2'},
             'F40111BF010AF501027D0552F0100001'),
            # the extended information object (CAG ID)
            ({'reg_type': '9', 'access_tech': '8', 'cause': '2',
              'ext_info_type': '1', 'ext_info': '00000001'},
             'F40109BF0108F501027D0552F0100001F2050100000001'),
        ]
        for fields, want in cases:
            self.assertEqual(events.build(0x12, fields).upper(), want)

    def test_data_connection(self):
        cases = [
            ({'status': '0', 'type': '0', 'ti': '00', 'loc_status': '0'},
             '9D0100AA01001C01009B0100'),
            ({'status': '1', 'type': '2', 'cause': '26', 'ti': '85', 'mcc': '250',
              'mnc': '01', 'lac': '00FF', 'cell': '0001', 'tech': '8',
              'loc_status': '1', 'apn': 'internet', 'pdp_type': '3'},
             '9D0101AA0102AE011A1C0185130752F01000FF0001BF01089B0101'
             'C708696E7465726E65740B0103'),
            ({'status': '2', 'type': '1', 'ti': '', 'mcc': '250', 'loc_status': '2'},
             '9D0102AA01011C0100130752F010000000019B0102'),
            ({'status': '0', 'type': '2', 'ti': 'AA', 'mcc': '262', 'mnc': '01',
              'lac': '0001', 'cell': '0002', 'tech': '7', 'loc_status': '1',
              'apn': 'web.operator.example', 'pdp_type': '4'},
             '9D0100AA01021C01AA130762F21000010002BF01079B0101'
             'C7147765622E6F70657261746F722E6578616D706C650B0104'),
        ]
        for fields, want in cases:
            self.assertEqual(events.build(0x1D, fields).upper(), want)

    def test_the_host_clock_date_time_object(self):
        # the PWA encoder's vector: UTC+3, 2026-09-30 21:55:00
        self.assertEqual(events.scts_bytes(st(), 180).hex().upper(), '62900312550021')
        # UTC-2:30: 10 quarters with the sign bit
        self.assertEqual(events.scts_bytes(st(2026, 1, 5, 8, 7, 9), -150).hex().upper(),
                         '62105080709009')
        # the data-connection builder embeds it as the 8.39 object 26 07 ...
        out = events.build(0x1D, {'status': '0', 'type': '0', 'ti': '00',
                                  'loc_status': '0', 'datetime': 'now'},
                           now=st()).upper()
        self.assertRegex(out, r'^9D0100AA01001C01002607[0-9A-F]{14}9B0100$')

    def test_plmn_encoding(self):
        self.assertEqual(events.plmn_hex('250', '01'), '52F010')
        self.assertEqual(events.plmn_hex('310', '410'), '130014')
        self.assertEqual(events.plmn_hex('262', '02'), '62F220')
        self.assertEqual(events.plmn_hex('310', '260'), '130062')


class EventValidationTests(unittest.TestCase):
    def test_resolve_event_accepts_names_and_hex(self):
        self.assertEqual(events.resolve_event('location status'), 0x03)
        self.assertEqual(events.resolve_event('Location_Status'), 0x03)
        self.assertEqual(events.resolve_event('0B'), 0x0B)
        self.assertEqual(events.resolve_event('0x12'), 0x12)
        self.assertEqual(events.resolve_event(0x1D), 0x1D)
        self.assertIsNone(events.resolve_event('nope'))
        self.assertIsNone(events.resolve_event(300))
        self.assertIsNone(events.resolve_event(True))
        self.assertIsNone(events.resolve_event(''))

    def test_field_validation(self):
        with self.assertRaises(events.EventError):
            events.build(0x03, {'status': '9'})
        with self.assertRaises(events.EventError):
            events.build(0x03, {'status': '0', 'mcc': '25'})
        with self.assertRaises(events.EventError):
            events.build(0x0B, {'tech': '300'})
        with self.assertRaises(events.EventError):
            events.build(0x12, {'reg_type': '0x12'})
        with self.assertRaises(events.EventError):
            events.build(0x12, {'reg_type': '9', 'tac': 'ABC'})
        with self.assertRaises(events.EventError):
            events.build(0x12, {'reg_type': '9', 'ext_info_type': '1', 'ext_info': 'A'})
        with self.assertRaises(events.EventError):
            events.build(0x1D, {'status': '0', 'apn': 'x' * 101})
        with self.assertRaises(events.EventError):
            events.build(0x1D, {'status': '0', 'ti': 'ZZ'})
        # an event without a semantic builder is refused with a hint
        with self.assertRaises(events.EventError) as ctx:
            events.normalise(0x05, {})
        self.assertIn('envelope', str(ctx.exception))

    def test_normalise_returns_the_validated_fields(self):
        fields = {'status': '1', 'mcc': '250'}
        self.assertEqual(events.normalise(0x03, fields), fields)
        self.assertEqual(events.normalise(0x0B, None), {})


if __name__ == '__main__':
    unittest.main()
