"""Card preset store tests (server-side presets).

The store replaces the localStorage presets; the ICCID normalisation must stay
byte-compatible with the PWA's `cardsNormIccid` (digits / spaced / raw
nibble-swapped EF hex), the counter must only move forward, and the file must
survive a corrupt store without silently destroying it.
"""

import json
import pathlib
import tempfile
import unittest

from pysim_simple_server import presets

DIGITS = '8970119000004600098'
RAW_HEX = '980711090000640090F8'      # the PWA's fixture for DIGITS


def make_store(tmp):
    return presets.PresetStore(pathlib.Path(tmp) / 'card_presets.json')


def preset(**kw):
    p = {'name': 'C', 'kic': '15', 'kid': '15', 'kicKey': 'AA', 'kidKey': 'BB'}
    p.update(kw)
    return p


class IccidNormalisationTests(unittest.TestCase):
    def test_digits_spaces_and_raw_hex_normalise_alike(self):
        self.assertEqual(presets.normalize_iccid(DIGITS), DIGITS)
        self.assertEqual(presets.normalize_iccid(' 89 70 1190-0000 4600 098 '), DIGITS)
        self.assertEqual(presets.normalize_iccid(RAW_HEX), DIGITS)
        self.assertEqual(presets.normalize_iccid(RAW_HEX.lower()), DIGITS)

    def test_empty_values_mean_no_iccid(self):
        for value in ('', None, '   ', '---'):
            self.assertEqual(presets.normalize_iccid(value), '')

    def test_leading_zeros_are_stripped(self):
        self.assertEqual(presets.normalize_iccid('008970119000004600098'),
                         '8970119000004600098')

    def test_odd_length_raw_hex_gets_the_js_fallback_nibble(self):
        # swapNibbles pads the missing nibble with 'f', then trailing Fs go
        self.assertEqual(presets.normalize_iccid('980711090000640090F8'), DIGITS)
        self.assertEqual(presets._swap_nibbles('123'), '21F3')


class CounterTests(unittest.TestCase):
    def test_counter_hex_is_the_fixed_width_protocol_form(self):
        self.assertEqual(presets.counter_hex('1'), '0000000001')
        self.assertEqual(presets.counter_hex('abc'), '0000000ABC')
        self.assertEqual(presets.counter_hex('FFFFFFFFFF'), 'FFFFFFFFFF')

    def test_counter_ahead_is_forward_only_with_wrap(self):
        self.assertTrue(presets.counter_ahead('0000000001', '0000000002'))
        self.assertFalse(presets.counter_ahead('0000000002', '0000000001'))
        self.assertFalse(presets.counter_ahead('0000000002', '0000000002'))
        # the 40-bit field wrapped past the top
        self.assertTrue(presets.counter_ahead('FFFFFFFFFF', '0000000000'))
        self.assertFalse(presets.counter_ahead('nonsense', '0000000002'))


class StoreCrudTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = make_store(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_add_applies_the_form_defaults_and_assigns_an_id(self):
        p = self.store.add(preset(iccid=DIGITS))
        self.assertTrue(p['id'])
        self.assertEqual(p['spi1'], '16')
        self.assertEqual(p['spi2'], '01')
        self.assertEqual(p['cntr'], '0000000001')
        self.assertEqual(p['tar'], '000000')
        self.assertEqual(p['uiccTar'], 'B00000')
        self.assertEqual(p['usimTar'], 'B00001')
        self.assertEqual([x['id'] for x in self.store.list()], [p['id']])

    def test_validation_requires_the_scp80_fields(self):
        with self.assertRaises(presets.PresetError):
            self.store.add(preset(name=''))
        with self.assertRaises(presets.PresetError):
            self.store.add(preset(kicKey=''))
        with self.assertRaises(presets.PresetError):
            self.store.add(preset(pskIdentity='id'))
        with self.assertRaises(presets.PresetError):
            self.store.add(preset(pskIdentity='id', pskKey='00'))

    def test_duplicate_iccid_is_refused_across_stored_forms(self):
        self.store.add(preset(iccid=DIGITS))
        with self.assertRaises(presets.PresetError) as ctx:
            self.store.add(preset(name='Other', iccid=RAW_HEX))
        self.assertIn('already exists', str(ctx.exception))
        # an empty ICCID is optional and never a duplicate
        self.store.add(preset(name='No ICCID'))
        self.store.add(preset(name='No ICCID 2'))
        self.assertEqual(len(self.store.list()), 3)

    def test_update_is_partial_and_keeps_the_id(self):
        p = self.store.add(preset(iccid=DIGITS))
        up = self.store.update(p['id'], {'name': 'Renamed', 'cntr': '0000000009'})
        self.assertEqual(up['id'], p['id'])
        self.assertEqual(up['name'], 'Renamed')
        self.assertEqual(up['cntr'], '0000000009')
        self.assertEqual(up['kic'], '15')
        self.assertIsNone(self.store.update('nope', {'name': 'x'}))

    def test_remove(self):
        p = self.store.add(preset())
        self.assertTrue(self.store.remove(p['id']))
        self.assertFalse(self.store.remove(p['id']))
        self.assertEqual(self.store.list(), [])

    def test_find_by_iccid(self):
        p = self.store.add(preset(iccid=DIGITS))
        self.assertEqual(self.store.find_by_iccid(RAW_HEX)['id'], p['id'])
        self.assertEqual(self.store.find_by_iccid(' 89 70 1190-0000 4600 098 ')['id'], p['id'])
        self.assertIsNone(self.store.find_by_iccid('1234567890123456789'))
        self.assertIsNone(self.store.find_by_iccid(''))

    def test_callers_get_copies(self):
        p = self.store.add(preset())
        p['name'] = 'mutated'
        self.assertEqual(self.store.get(p['id'])['name'], 'C')
        items = self.store.list()
        items[0]['name'] = 'mutated'
        self.assertEqual(self.store.list()[0]['name'], 'C')


class StoreCounterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = make_store(self.tmp.name)
        self.p = self.store.add(preset(iccid=DIGITS))

    def tearDown(self):
        self.tmp.cleanup()

    def test_set_counter_advances_and_ignores_stale_values(self):
        self.store.set_counter(self.p['id'], '0000000005', 'send-ota')
        self.assertEqual(self.store.get(self.p['id'])['cntr'], '0000000005')
        # a stale caller can never regress the counter
        self.store.set_counter(self.p['id'], '0000000003', 'send-ota')
        self.assertEqual(self.store.get(self.p['id'])['cntr'], '0000000005')
        # equal values are a no-op
        self.store.set_counter(self.p['id'], '0000000005', 'send-ota')
        self.assertEqual(self.store.get(self.p['id'])['cntr'], '0000000005')

    def test_set_counter_allows_a_wrapped_value(self):
        self.store.update(self.p['id'], {'cntr': 'FFFFFFFFFF'})
        self.store.set_counter(self.p['id'], '0000000000', 'send-ota')
        self.assertEqual(self.store.get(self.p['id'])['cntr'], '0000000000')

    def test_set_counter_on_unknown_id_is_none(self):
        self.assertIsNone(self.store.set_counter('nope', '0000000002'))

    def test_counter_changes_are_audited(self):
        self.store.set_counter(self.p['id'], '0000000005', 'send-ota')
        lines = self.store.audit_path.read_text(encoding='utf-8').strip().splitlines()
        self.assertEqual(len(lines), 1)
        entry = json.loads(lines[0])
        self.assertEqual(entry['old'], '0000000001')
        self.assertEqual(entry['new'], '0000000005')
        self.assertEqual(entry['source'], 'send-ota')


class StorePersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_file_round_trips(self):
        store = make_store(self.tmp.name)
        p = store.add(preset(iccid=DIGITS, pskIdentity='id', pskKey='00' * 16))
        store.set_counter(p['id'], '0000000004', 'send-ota')
        raw = json.loads(pathlib.Path(store.path).read_text(encoding='utf-8'))
        self.assertEqual(raw['version'], presets.SCHEMA_VERSION)
        again = make_store(self.tmp.name)
        self.assertEqual([x['id'] for x in again.list()], [p['id']])
        self.assertEqual(again.get(p['id'])['cntr'], '0000000004')

    def test_no_temporary_files_are_left_behind(self):
        store = make_store(self.tmp.name)
        store.add(preset())
        store.add(preset(name='Second'))
        leftovers = [f.name for f in pathlib.Path(self.tmp.name).iterdir()
                     if '.json.' in f.name]
        self.assertEqual(leftovers, [])

    def test_a_corrupt_store_is_kept_and_the_store_starts_empty(self):
        path = pathlib.Path(self.tmp.name) / 'card_presets.json'
        path.write_text('{not json', encoding='utf-8')
        store = make_store(self.tmp.name)
        self.assertEqual(store.list(), [])
        kept = [f.name for f in pathlib.Path(self.tmp.name).iterdir()
                if '.bad-' in f.name]
        self.assertEqual(len(kept), 1)
        self.assertEqual(pathlib.Path(self.tmp.name, kept[0]).read_text(encoding='utf-8'),
                         '{not json')

    def test_a_missing_file_is_an_empty_store(self):
        self.assertEqual(make_store(self.tmp.name).list(), [])


class StoreImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = make_store(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_import_accepts_the_old_localstorage_shape(self):
        # an export from the browser: no ids, no defaults filled in
        old = [{'name': 'A', 'iccid': DIGITS, 'kic': '15', 'kid': '15',
                'kicKey': 'AA', 'kidKey': 'BB', 'cntr': '0000000007'},
               {'name': 'B', 'iccid': '', 'kic': '25', 'kid': '25',
                'kicKey': 'CC', 'kidKey': 'DD'}]
        res = self.store.import_presets(old)
        self.assertEqual(res['added'], 2)
        self.assertEqual(res['skipped'], 0)
        a = self.store.find_by_iccid(DIGITS)
        self.assertEqual(a['cntr'], '0000000007')
        self.assertTrue(a['id'])

    def test_import_skips_duplicates_and_reports_invalid_entries(self):
        self.store.add(preset(iccid=DIGITS))
        res = self.store.import_presets([
            preset(iccid=RAW_HEX),                    # same card in raw form
            {'name': 'Broken'},                       # no KIc/KID/keys
            'not a dict',
        ])
        self.assertEqual(res['added'], 0)
        self.assertEqual(res['skipped'], 3)
        self.assertTrue(any('already exists' in e for e in res['errors']))
        self.assertEqual(len(self.store.list()), 1)

    def test_import_replace_wipes_the_store(self):
        self.store.add(preset(iccid=DIGITS))
        res = self.store.import_presets([preset(name='Fresh', iccid='')], mode='replace')
        self.assertEqual(res['added'], 1)
        self.assertEqual([p['name'] for p in self.store.list()], ['Fresh'])

    def test_an_existing_id_is_never_overwritten(self):
        p = self.store.add(preset(iccid=DIGITS))
        res = self.store.import_presets([dict(preset(name='Copy'), id=p['id'], iccid='')])
        self.assertEqual(res['added'], 1)
        ids = [x['id'] for x in self.store.list()]
        self.assertEqual(len(set(ids)), 2)


class StoreInfoTests(unittest.TestCase):
    def test_info_reports_the_path_and_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = make_store(tmp)
            store.add(preset())
            info = store.info()
            self.assertEqual(info['count'], 1)
            self.assertEqual(info['path'], str(store.path))
            self.assertEqual(info['version'], presets.SCHEMA_VERSION)

    def test_default_path_is_the_home_dot_directory(self):
        self.assertEqual(presets.default_path().name, 'card_presets.json')
        self.assertEqual(presets.default_path().parent.name, '.pysim-simple-server')


if __name__ == '__main__':
    unittest.main()
