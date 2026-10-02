"""Card preset store tests (server-side presets).

The store replaces the localStorage presets; the ICCID normalisation must stay
byte-compatible with the PWA's `cardsNormIccid` (digits / spaced / raw
nibble-swapped EF hex), every keyset carries its own monotonic counter (one per
keyset number, TS 102 225 Annex A.1), and the file must survive a corrupt store
without silently destroying it.
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


def keyset(**kw):
    ks = {'kic': '15', 'kid': '15', 'kicKey': 'AA', 'kidKey': 'BB'}
    ks.update(kw)
    return ks


def preset(**kw):
    p = {'name': 'C', 'keysets': [keyset()]}
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


class KeysetKvnTests(unittest.TestCase):
    def test_the_key_version_is_the_high_nibble_of_kic(self):
        self.assertEqual(presets.keyset_kvn(keyset(kic='15')), 1)
        self.assertEqual(presets.keyset_kvn(keyset(kic='29')), 2)
        self.assertEqual(presets.keyset_kvn(keyset(kic='3A')), 3)
        self.assertIsNone(presets.keyset_kvn(keyset(kic='')))
        self.assertIsNone(presets.keyset_kvn(keyset(kic='5')))
        self.assertIsNone(presets.keyset_kvn(None))


class StoreCrudTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = make_store(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_add_applies_the_form_defaults_and_assigns_an_id(self):
        p = self.store.add(preset(iccid=DIGITS))
        self.assertTrue(p['id'])
        self.assertEqual([t['role'] for t in p['tars']],
                         ['isd', 'uiccRfm', 'usimRfm'])
        self.assertEqual([t['tar'] for t in p['tars']],
                         ['000000', 'B00000', 'B00001'])
        self.assertEqual([t['msl'] for t in p['tars']], ['16', '16', '16'])
        self.assertNotIn('spi1', p)     # the card-wide SPI1 is gone: per-TAR MSL
        self.assertNotIn('spi2', p)
        self.assertEqual(p['keysets'][0]['cntr'], '0000000001')
        self.assertEqual(presets.keyset_kvn(p['keysets'][0]), 1)
        self.assertEqual([x['id'] for x in self.store.list()], [p['id']])

    def test_a_preset_carries_extra_tars_with_optional_descriptions(self):
        p = self.store.add(preset(tars=[
            {'role': 'isd', 'tar': 'AF4D01', 'msl': '1A'},
            {'role': 'uiccRfm', 'tar': 'B00000', 'msl': '16'},
            {'role': 'usimRfm', 'tar': 'B00001', 'msl': '16'},
            {'tar': 'AF4D02', 'msl': '0A', 'desc': 'My applet'},
            {'tar': '', 'msl': '', 'desc': ''},        # a blank editor row
        ]))
        self.assertEqual([(t.get('role', ''), t['tar'], t['msl']) for t in p['tars']],
                         [('isd', 'AF4D01', '1A'), ('uiccRfm', 'B00000', '16'),
                          ('usimRfm', 'B00001', '16'), ('', 'AF4D02', '0A')])
        self.assertEqual(p['tars'][3]['desc'], 'My applet')
        self.assertNotIn('role', p['tars'][3])   # free rows carry no role
        self.assertEqual(len(p['tars']), 4)     # the blank row was dropped

    def test_every_tar_needs_its_msl_and_a_unique_tar(self):
        with self.assertRaises(presets.PresetError) as ctx:
            self.store.add(preset(tars=[
                {'role': 'isd', 'tar': '000000', 'msl': '16'},
                {'role': 'uiccRfm', 'tar': 'B00000', 'msl': '16'},
                {'role': 'usimRfm', 'tar': 'B00001', 'msl': '16'},
                {'tar': 'AF4D02', 'msl': ''},
            ]))
        self.assertIn('MSL', str(ctx.exception))
        with self.assertRaises(presets.PresetError) as ctx:
            self.store.add(preset(tars=[
                {'role': 'isd', 'tar': '000000', 'msl': '16'},
                {'role': 'uiccRfm', 'tar': 'B00000', 'msl': '16'},
                {'role': 'usimRfm', 'tar': '000000', 'msl': '16'},
            ]))
        self.assertIn('duplicate TAR', str(ctx.exception))
        with self.assertRaises(presets.PresetError) as ctx:
            self.store.add(preset(tars=[
                {'role': 'isd', 'tar': '0000', 'msl': '16'}]))
        self.assertIn('three hex bytes', str(ctx.exception))

    def test_tar_lookup_helpers(self):
        p = self.store.add(preset(tars=[
            {'role': 'isd', 'tar': 'AF4D01', 'msl': '1A'},
            {'role': 'uiccRfm', 'tar': 'B00000', 'msl': '16'},
            {'role': 'usimRfm', 'tar': 'B00001', 'msl': '16'},
            {'tar': 'AF4D02', 'msl': '0A', 'desc': 'My applet'},
        ]))
        self.assertEqual(presets.role_tar(p, 'isd'), 'AF4D01')
        self.assertEqual(presets.role_tar(p, 'usimRfm'), 'B00001')
        self.assertEqual(presets.role_tar(p, 'nope'), '')
        self.assertEqual(presets.tar_msl(p, 'af4d01'), '1A')
        self.assertEqual(presets.tar_msl(p, 'AF4D02'), '0A')
        self.assertEqual(presets.tar_msl(p, '123456'), '')
        self.assertIsNone(presets.tar_entry(p, ''))
        self.assertEqual(presets.tar_entry(p, 'AF4D02')['desc'], 'My applet')

    def test_update_replaces_the_tar_table(self):
        p = self.store.add(preset())
        upd = self.store.update(p['id'], {'tars': [
            {'role': 'isd', 'tar': 'AF4D01', 'msl': '1A'},
            {'role': 'uiccRfm', 'tar': 'B00000', 'msl': '16'},
            {'role': 'usimRfm', 'tar': 'B00001', 'msl': '16'}]})
        self.assertEqual(presets.role_tar(upd, 'isd'), 'AF4D01')
        self.assertEqual(presets.tar_msl(upd, 'AF4D01'), '1A')
        # a malformed tars value is refused, not silently ignored or reset
        with self.assertRaises(presets.PresetError):
            self.store.update(p['id'], {'tars': 'nonsense'})
        self.assertEqual(presets.role_tar(self.store.get(p['id']), 'isd'), 'AF4D01')

    def test_a_preset_carries_several_keysets(self):
        p = self.store.add(preset(keysets=[keyset(kic='15', kid='15'),
                                           keyset(kic='29', kid='29', cntr='0000000005')]))
        self.assertEqual([presets.keyset_kvn(ks) for ks in p['keysets']], [1, 2])
        self.assertEqual(self.store.find_keyset(p['id'], 2)['cntr'], '0000000005')
        self.assertIsNone(self.store.find_keyset(p['id'], 3))

    def test_validation_requires_at_least_one_keyset(self):
        with self.assertRaises(presets.PresetError) as ctx:
            self.store.add(preset(keysets=[]))
        self.assertIn('keyset', str(ctx.exception))

    def test_a_keyset_needs_both_keys(self):
        with self.assertRaises(presets.PresetError):
            self.store.add(preset(keysets=[keyset(kicKey='')]))
        with self.assertRaises(presets.PresetError):
            self.store.add(preset(keysets=[keyset(kidKey='')]))

    def test_kic_and_kid_must_carry_the_same_key_version(self):
        # TS 102 225 A.2: a mismatch is rejected by the card - refuse it here
        with self.assertRaises(presets.PresetError) as ctx:
            self.store.add(preset(keysets=[keyset(kic='15', kid='25')]))
        self.assertIn('same keyset number', str(ctx.exception))

    def test_key_version_00_and_duplicates_are_refused(self):
        with self.assertRaises(presets.PresetError) as ctx:
            self.store.add(preset(keysets=[keyset(kic='05', kid='05')]))
        self.assertIn('01-0F', str(ctx.exception))
        with self.assertRaises(presets.PresetError) as ctx:
            self.store.add(preset(keysets=[keyset(kic='15', kid='15'),
                                           keyset(kic='15', kid='15')]))
        self.assertIn('duplicate keyset number', str(ctx.exception))
        # the byte must be a full hex byte
        with self.assertRaises(presets.PresetError):
            self.store.add(preset(keysets=[keyset(kic='1', kid='1')]))

    def test_validation_requires_the_psk_rules(self):
        with self.assertRaises(presets.PresetError):
            self.store.add(preset(pskIdentity='id'))
        with self.assertRaises(presets.PresetError):
            self.store.add(preset(pskIdentity='id', pskKey='00'))
        with self.assertRaises(presets.PresetError):
            self.store.add(preset(name=''))

    def test_duplicate_iccid_is_refused_across_stored_forms(self):
        self.store.add(preset(iccid=DIGITS))
        with self.assertRaises(presets.PresetError) as ctx:
            self.store.add(preset(name='Other', iccid=RAW_HEX))
        self.assertIn('already exists', str(ctx.exception))
        # an empty ICCID is optional and never a duplicate
        self.store.add(preset(name='No ICCID'))
        self.store.add(preset(name='No ICCID 2'))
        self.assertEqual(len(self.store.list()), 3)

    def test_a_caller_supplied_id_never_collides(self):
        first = self.store.add(preset())
        second = self.store.add(dict(preset(name='B'), id=first['id']))
        self.assertNotEqual(second['id'], first['id'])
        self.assertEqual(len({p['id'] for p in self.store.list()}), 2)

    def test_update_is_partial_and_keeps_the_id(self):
        p = self.store.add(preset(iccid=DIGITS))
        up = self.store.update(p['id'], {'name': 'Renamed'})
        self.assertEqual(up['id'], p['id'])
        self.assertEqual(up['name'], 'Renamed')
        self.assertEqual(up['keysets'][0]['kic'], '15')
        # the keysets are replaceable as a whole (the Cards tab edit path)
        up = self.store.update(p['id'], {'keysets': [keyset(kic='29', kid='29',
                                                           cntr='0000000009')]})
        self.assertEqual(presets.keyset_kvn(up['keysets'][0]), 2)
        self.assertEqual(up['keysets'][0]['cntr'], '0000000009')
        self.assertIsNone(self.store.update('nope', {'name': 'x'}))

    def test_the_counter_must_be_hex_and_at_most_ten_digits(self):
        for bad in ('ZZ', '00000000000000000000'):
            with self.assertRaises(presets.PresetError) as ctx:
                self.store.add(preset(keysets=[keyset(cntr=bad)]))
            self.assertIn('counter', str(ctx.exception))
        # whitespace is cleaned and a short value is stored fixed-width
        p = self.store.add(preset(keysets=[keyset(cntr='12 34')]))
        self.assertEqual(p['keysets'][0]['cntr'], '0000001234')
        p = self.store.add(preset(keysets=[keyset(cntr='abc')]))
        self.assertEqual(p['keysets'][0]['cntr'], '0000000ABC')

    def test_update_validates_the_counter_too(self):
        p = self.store.add(preset())
        with self.assertRaises(presets.PresetError):
            self.store.update(p['id'], {'keysets': [keyset(cntr='ZZ')]})
        self.assertEqual(self.store.get(p['id'])['keysets'][0]['cntr'], '0000000001')

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
        p['keysets'][0]['cntr'] = 'mutated'
        self.assertEqual(self.store.get(p['id'])['name'], 'C')
        self.assertEqual(self.store.get(p['id'])['keysets'][0]['cntr'], '0000000001')
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

    def counter(self, kvn=1):
        return self.store.find_keyset(self.p['id'], kvn)['cntr']

    def test_set_counter_advances_and_ignores_stale_values(self):
        self.store.set_counter(self.p['id'], '0000000005', 'send-ota', kvn=1)
        self.assertEqual(self.counter(), '0000000005')
        # a stale caller can never regress the counter
        self.store.set_counter(self.p['id'], '0000000003', 'send-ota', kvn=1)
        self.assertEqual(self.counter(), '0000000005')
        # equal values are a no-op
        self.store.set_counter(self.p['id'], '0000000005', 'send-ota', kvn=1)
        self.assertEqual(self.counter(), '0000000005')

    def test_each_key_version_has_its_own_counter(self):
        # TS 102 225 A.1: a dedicated counter per keyset
        p = self.store.update(self.p['id'], {'keysets': [
            keyset(kic='15', kid='15', cntr='0000000002'),
            keyset(kic='29', kid='29', cntr='0000000007')]})
        self.store.set_counter(p['id'], '0000000003', 'send-ota', kvn=1)
        self.assertEqual(self.counter(1), '0000000003')
        self.assertEqual(self.counter(2), '0000000007')     # untouched
        self.store.set_counter(p['id'], '0000000008', 'send-ota', kvn=2)
        self.assertEqual(self.counter(1), '0000000003')
        self.assertEqual(self.counter(2), '0000000008')

    def test_an_unknown_key_version_changes_nothing(self):
        self.store.set_counter(self.p['id'], '0000000005', 'send-ota', kvn=3)
        self.assertEqual(self.counter(1), '0000000001')

    def test_kvn_none_uses_the_only_keyset(self):
        self.store.set_counter(self.p['id'], '0000000005', 'send-ota')
        self.assertEqual(self.counter(1), '0000000005')
        # ... and is refused (logged) when several keysets exist
        p = self.store.update(self.p['id'], {'keysets': [
            keyset(kic='15', kid='15'), keyset(kic='29', kid='29')]})
        self.store.set_counter(p['id'], '0000000009', 'send-ota')
        self.assertEqual(self.counter(1), '0000000001')

    def test_set_counter_allows_a_wrapped_value(self):
        self.store.update(self.p['id'], {'keysets': [keyset(cntr='FFFFFFFFFF')]})
        self.store.set_counter(self.p['id'], '0000000000', 'send-ota', kvn=1)
        self.assertEqual(self.counter(), '0000000000')

    def test_set_counter_on_unknown_id_is_none(self):
        self.assertIsNone(self.store.set_counter('nope', '0000000002', kvn=1))

    def test_set_counter_ignores_a_malformed_value(self):
        for bad in ('ZZ', '00000000000000000000', ''):
            self.store.set_counter(self.p['id'], bad, 'send-ota', kvn=1)
            self.assertEqual(self.counter(), '0000000001')

    def test_counter_changes_are_audited_with_the_key_version(self):
        self.store.set_counter(self.p['id'], '0000000005', 'send-ota', kvn=1)
        lines = self.store.audit_path.read_text(encoding='utf-8').strip().splitlines()
        self.assertEqual(len(lines), 1)
        entry = json.loads(lines[0])
        self.assertEqual(entry['old'], '0000000001')
        self.assertEqual(entry['new'], '0000000005')
        self.assertEqual(entry['kvn'], 1)
        self.assertEqual(entry['source'], 'send-ota')


class StorePersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_file_round_trips(self):
        store = make_store(self.tmp.name)
        p = store.add(preset(iccid=DIGITS, pskIdentity='id', pskKey='00' * 16,
                             keysets=[keyset(kic='15', kid='15'),
                                      keyset(kic='29', kid='29', cntr='0000000004')]))
        raw = json.loads(pathlib.Path(store.path).read_text(encoding='utf-8'))
        self.assertEqual(raw['version'], presets.SCHEMA_VERSION)
        self.assertEqual(raw['presets'][0]['keysets'][1]['cntr'], '0000000004')
        again = make_store(self.tmp.name)
        self.assertEqual([x['id'] for x in again.list()], [p['id']])
        self.assertEqual(again.find_keyset(p['id'], 2)['cntr'], '0000000004')

    def test_a_v380_flat_preset_loads_as_one_keyset(self):
        path = pathlib.Path(self.tmp.name) / 'card_presets.json'
        path.write_text(json.dumps({'version': 1, 'presets': [
            {'name': 'Old', 'iccid': DIGITS, 'kic': '29', 'kid': '29',
             'kicKey': '11', 'kidKey': '22', 'cntr': '0000000007',
             'spi1': '16', 'spi2': '01'}]}), encoding='utf-8')
        store = make_store(self.tmp.name)
        p = store.list()[0]
        self.assertEqual(len(p['keysets']), 1)
        self.assertEqual(presets.keyset_kvn(p['keysets'][0]), 2)
        self.assertEqual(p['keysets'][0]['cntr'], '0000000007')
        self.assertEqual(p['keysets'][0]['kicKey'], '11')
        self.assertNotIn('cntr', p)
        # the card-wide spi1 becomes the MSL of all three role entries
        self.assertEqual([t['msl'] for t in p['tars']], ['16', '16', '16'])

    def test_a_v391_flat_tars_preset_converts_to_the_tar_table(self):
        path = pathlib.Path(self.tmp.name) / 'card_presets.json'
        path.write_text(json.dumps({'version': 2, 'presets': [
            {'name': 'Old', 'tar': 'AF4D01', 'uiccTar': 'B00000',
             'usimTar': 'B00001', 'spi1': '1A', 'spi2': '21',
             'keysets': [keyset()]}]}), encoding='utf-8')
        store = make_store(self.tmp.name)
        p = store.list()[0]
        self.assertEqual([t['tar'] for t in p['tars']],
                         ['AF4D01', 'B00000', 'B00001'])
        self.assertEqual([t['msl'] for t in p['tars']], ['1A', '1A', '1A'])
        self.assertNotIn('spi2', p)

    def test_no_temporary_files_are_left_behind(self):
        store = make_store(self.tmp.name)
        p = store.add(preset())
        store.add(preset(name='Second'))
        store.set_counter(p['id'], '0000000005', 'send-ota', kvn=1)
        leftovers = [f.name for f in pathlib.Path(self.tmp.name).iterdir()
                     if f.name not in ('card_presets.json', 'card_presets.json.audit.jsonl')]
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
        # an export from the browser (v3.8.0 or the localStorage era): flat
        # key fields, no ids, no defaults filled in
        old = [{'name': 'A', 'iccid': DIGITS, 'kic': '15', 'kid': '15',
                'kicKey': 'AA', 'kidKey': 'BB', 'cntr': '0000000007'},
               {'name': 'B', 'iccid': '', 'kic': '25', 'kid': '25',
                'kicKey': 'CC', 'kidKey': 'DD'}]
        res = self.store.import_presets(old)
        self.assertEqual(res['added'], 2)
        self.assertEqual(res['skipped'], 0)
        a = self.store.find_by_iccid(DIGITS)
        self.assertEqual(a['keysets'][0]['cntr'], '0000000007')
        self.assertEqual(presets.keyset_kvn(a['keysets'][0]), 1)
        self.assertTrue(a['id'])

    def test_import_takes_the_keyset_shape_too(self):
        res = self.store.import_presets([preset(iccid=DIGITS)])
        self.assertEqual(res['added'], 1)
        self.assertEqual(self.store.find_by_iccid(DIGITS)['keysets'][0]['kic'], '15')

    def test_import_skips_duplicates_and_reports_invalid_entries(self):
        self.store.add(preset(iccid=DIGITS))
        res = self.store.import_presets([
            preset(iccid=RAW_HEX),                    # same card in raw form
            {'name': 'Broken'},                       # no keyset at all
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
