"""Server-side event-data builders (the semantic test-script `event` action).

The byte vectors live in the SHARED fixture `frontend/tests/event_vectors.json`:
the Node suite asserts the same file against the PWA's own `EVENT_FORMS`
builders, this suite asserts it against `events.py`, so neither implementation
can drift alone (a change on one side fails the other until the fixture and the
code agree).
"""

import json
import pathlib
import time
import unittest

from pysim_simple_server import events

VECTORS = json.loads((pathlib.Path(__file__).resolve().parents[1]
                      / 'frontend' / 'tests' / 'event_vectors.json')
                     .read_text(encoding='utf-8'))


def st(year=2026, mon=9, day=30, hour=21, minute=55, sec=0):
    return time.struct_time((year, mon, day, hour, minute, sec, 0, 0, -1))


class EventVectorTests(unittest.TestCase):
    """Byte parity with the PWA builders from the shared fixture."""

    def test_the_shared_event_vectors(self):
        self.assertTrue(VECTORS['events'], 'the fixture must not be empty')
        for v in VECTORS['events']:
            code = int(v['event'], 16)
            self.assertEqual(events.build(code, v['fields']).upper(), v['hex'],
                             'event 0x%s %s' % (v['event'], v['fields']))

    def test_the_shared_scts_vectors(self):
        self.assertTrue(VECTORS['scts'], 'the fixture must not be empty')
        for v in VECTORS['scts']:
            stamp = time.struct_time((v['year'], v['month'], v['day'], v['hour'],
                                      v['min'], v['sec'], 0, 0, -1))
            self.assertEqual(events.scts_bytes(stamp, v['tz_east_min']).hex().upper(),
                             v['hex'])

    def test_the_shared_event_name_aliases(self):
        # the script's `event` value may be one of these name keywords; the
        # PWA mirrors the map (frontend/tests/event_forms.test.js)
        want = {name: int(hexv, 16) for name, hexv in VECTORS['aliases'].items()}
        self.assertEqual(events.EVENT_NAMES, want)
        for name, code in want.items():
            self.assertEqual(events.resolve_event(name), code, name)
            self.assertEqual(events.resolve_event(hex(code)[2:].upper().zfill(2)), code)
        self.assertIsNone(events.resolve_event('nonsense'))

    def test_the_host_clock_date_time_object(self):
        # the data-connection builder embeds the 8.39 object 26 07 ...
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
