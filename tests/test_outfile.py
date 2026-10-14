"""Outfile import tests (v3.27.0).

The vendors' outfiles carry real key material, so the fixtures here are
**synthetic** files shaped like the two observed layouts: the ALFA style
(a ``var_out:`` column line, three keysets, no codes) and the BAP style (a
long banner/configuration header, the PIN/PUK/ADM columns and the EKI/KIK
columns that must be ignored).
"""

import pathlib
import tempfile
import unittest

from pysim_simple_server import outfile, presets

DIGITS = '8970119000004600098'
DIGITS_F = DIGITS + 'F'          # the vendors' odd-nibble F padding

# 16-byte synthetic keys (3DES2 by default)
K16A = '00112233445566778899AABBCCDDEEFF'
K16B = 'FFEEDDCCBBAA99887766554433221100'
K16C = '0123456789ABCDEFFEDCBA9876543210'


def alfa_text():
    return '\n'.join([
        'var_out:IMSI ICCID KIC1 KID1 KIK1 KIC2 KID2 KIK2',
        '250197700000000 ' + DIGITS_F + ' ' + K16A + ' ' + K16B + ' ' + K16A +
        ' ' + K16C + ' ' + K16B + ' ' + K16A,
        '250197700000001 8970119000004600106F ' + K16B + ' ' + K16A + ' ' + K16B +
        ' ' + K16A + ' ' + K16C + ' ' + K16B,
    ]) + '\n'


def bap_text():
    return '\n'.join([
        '********************************',
        '*     HEADER DESCRIPTION       *',
        '********************************',
        'Customer:                Synthetic',
        'Batch:                   0002',
        'TransportKey:            004',
        'ICCID:897011900000460005',
        '',
        '*******************************',
        '*   OUTPUT VARIABLES          *',
        '*******************************',
        'IMSI1 IMSI2 ICCID EKI PUK1 PUK2 PIN1 PIN2 ADM KIC1 KID1 KIK1',
        '250199800390005 FFFFFFFFFFFFFFF ' + DIGITS_F + ' ' + K16A +
        ' 12345678 87654321 1234 4321 3632333239463337 ' +
        K16A + ' ' + K16B + ' ' + K16C,
    ]) + '\n'


class ColumnDetectionTests(unittest.TestCase):
    def test_find_columns_skips_the_header(self):
        cols, i = outfile.find_columns(alfa_text())
        self.assertEqual(i, 0)
        self.assertEqual(cols[:2], ['IMSI', 'ICCID'])
        cols, i = outfile.find_columns(bap_text())
        self.assertEqual(cols[0], 'IMSI1')
        self.assertEqual(cols.index('ICCID'), 2)
        # the config line ICCID:897... is a single token and never matches:
        # the column line sits after it (line 6 in the fixture)
        config_line = bap_text().splitlines().index('ICCID:897011900000460005')
        self.assertGreater(i, config_line)

    def test_a_file_without_the_column_line_reports_it(self):
        res = outfile.parse_outfile('just a banner\nno columns here\n')
        self.assertEqual(res['presets'], [])
        self.assertIn('column-name line', res['report']['error'])

    def test_a_config_line_naming_a_value_is_not_the_column_line(self):
        # 'ICCID <value>' names a value, not the column header: the value
        # token is not a column name, so the real column line is found
        text = ('ICCID 897011900000460005\n'
                'ICCID KIC1 KID1\n' + DIGITS_F + ' ' + K16A + ' ' + K16B + '\n')
        cols, i = outfile.find_columns(text)
        self.assertEqual(i, 1)
        self.assertEqual(cols, ['ICCID', 'KIC1', 'KID1'])
        res = outfile.parse_outfile(text)
        self.assertEqual(res['report']['cards'], 1)
        self.assertEqual(res['presets'][0]['iccid'], DIGITS)


class AlfaShapeTests(unittest.TestCase):
    def test_the_rows_become_presets_with_three_keysets(self):
        res = outfile.parse_outfile(alfa_text())
        self.assertEqual(res['report']['cards'], 2)
        self.assertEqual(res['report']['rows'], 2)
        self.assertEqual(res['report']['keysets'], ['KIC1/KID1', 'KIC2/KID2'])
        p = res['presets'][0]
        self.assertEqual(p['iccid'], DIGITS)
        self.assertEqual(p['name'], DIGITS)          # no name given: the ICCID
        self.assertEqual([ks['kic'] for ks in p['keysets']], ['15', '25'])
        self.assertEqual([ks['kid'] for ks in p['keysets']], ['15', '25'])
        self.assertEqual(p['keysets'][0]['kicKey'], K16A)
        self.assertEqual(p['keysets'][1]['kicKey'], K16C)
        # the TARs are the three role defaults, all on the card's first keyset
        self.assertEqual([t['role'] for t in p['tars']],
                         ['isd', 'uiccRfm', 'usimRfm'])
        self.assertEqual([t['tar'] for t in p['tars']],
                         ['000000', 'B00000', 'B00001'])
        self.assertTrue(all(t['keyset'] == 1 for t in p['tars']))
        self.assertNotIn('pskKey', p)
        self.assertNotIn('adm', p)

    def test_the_parsed_presets_validate_through_the_store(self):
        res = outfile.parse_outfile(alfa_text())
        with tempfile.TemporaryDirectory() as tmp:
            store = presets.PresetStore(pathlib.Path(tmp) / 'card_presets.json')
            for entry in res['presets']:
                p = store.add(entry)
                self.assertEqual(p['iccid'], entry['iccid'])


class BapShapeTests(unittest.TestCase):
    def test_the_codes_and_the_adm_are_read(self):
        res = outfile.parse_outfile(bap_text())
        self.assertEqual(res['report']['cards'], 1)
        p = res['presets'][0]
        self.assertEqual(p['iccid'], DIGITS)
        self.assertEqual(p['pin1'], '1234')
        self.assertEqual(p['pin2'], '4321')
        self.assertEqual(p['puk1'], '12345678')
        self.assertEqual(p['puk2'], '87654321')
        self.assertEqual(p['adm'], '62329F37')       # the ASCII-hex decode
        # EKI, KIK and the IMSI columns are ignored - one keyset only
        self.assertEqual(len(p['keysets']), 1)
        self.assertEqual(p['keysets'][0]['kicKey'], K16A)
        self.assertNotIn(K16C, [p['keysets'][0]['kicKey'], p['keysets'][0]['kidKey']])


class KeyInferenceTests(unittest.TestCase):
    def _one(self, kic_key, algorithm16='3des2', kic='KIC1', kid='KID1'):
        text = ('ICCID ' + kic + ' ' + kid + '\n' +
                DIGITS_F + ' ' + kic_key + ' ' + kic_key + '\n')
        return outfile.parse_outfile(text, algorithm16=algorithm16)

    def test_key_size_picks_the_algorithm_nibble(self):
        # 8 bytes -> DES (1), 24 -> 3DES3 (9), 16 -> 3DES2 (5) or AES (2)
        p = self._one('0011223344556677')['presets'][0]
        self.assertEqual(p['keysets'][0]['kic'], '11')
        p = self._one('00112233445566778899AABBCCDDEEFF0011223344556677')['presets'][0]
        self.assertEqual(p['keysets'][0]['kic'], '19')
        p = self._one(K16A)['presets'][0]
        self.assertEqual(p['keysets'][0]['kic'], '15')
        p = self._one(K16A, algorithm16='aes')['presets'][0]
        self.assertEqual(p['keysets'][0]['kic'], '12')

    def test_an_unsupported_key_size_skips_the_keyset(self):
        res = self._one('00112233445566778899')      # 10 bytes
        self.assertEqual(res['presets'], [])
        self.assertTrue(any('unsupported key size' in s for s in res['report']['skipped']))

    def test_the_keyset_number_is_the_column_number(self):
        text = ('ICCID KIC1 KID1 KIC2 KID2\n' + DIGITS_F + ' ' +
                K16A + ' ' + K16B + ' ' + K16C + ' ' + K16A + '\n')
        p = outfile.parse_outfile(text)['presets'][0]
        self.assertEqual([ks['kic'] for ks in p['keysets']], ['15', '25'])

    def test_a_keyset_missing_one_side_is_skipped(self):
        text = 'ICCID KIC1 KID1 KIC2\n' + DIGITS_F + ' ' + K16A + ' ' + K16B + ' ' + K16C + '\n'
        res = outfile.parse_outfile(text)
        self.assertEqual(len(res['presets'][0]['keysets']), 1)


class OptionTests(unittest.TestCase):
    def test_the_common_name_applies_to_every_card(self):
        res = outfile.parse_outfile(alfa_text(), name='Alfa DSA 0002')
        self.assertEqual([p['name'] for p in res['presets']],
                         ['Alfa DSA 0002', 'Alfa DSA 0002'])

    def test_the_tar_table_and_the_global_keyset(self):
        tars = outfile.default_tars() + [{'tar': 'B00011', 'msl': '0A', 'desc': 'push'}]
        res = outfile.parse_outfile(alfa_text(), tars=tars, tar_keyset=2)
        p = res['presets'][0]
        self.assertEqual(len(p['tars']), 4)
        self.assertEqual(p['tars'][3]['tar'], 'B00011')
        self.assertTrue(all(t['keyset'] == 2 for t in p['tars']))
        self.assertEqual(res['report']['tars'], 4)

    def test_a_malformed_tar_table_raises(self):
        with self.assertRaises(ValueError):
            outfile.parse_outfile(alfa_text(), tars=[{'tar': 'ZZ', 'msl': '16'}])
        with self.assertRaises(ValueError):
            outfile.parse_outfile(alfa_text(), tars=[
                {'tar': '000000', 'msl': '16'}, {'tar': '000000', 'msl': '16'}])

    def test_the_psk_column_sets_the_key_and_defaults_the_identity(self):
        psk = 'AB' * 16
        text = ('ICCID KIC1 KID1 PSK\n' + DIGITS_F + ' ' + K16A + ' ' + K16B +
                ' ' + psk + '\n')
        p = outfile.parse_outfile(text)['presets'][0]
        self.assertEqual(p['pskKey'], psk)
        self.assertEqual(p['pskIdentity'], DIGITS)
        # an explicit PSKID column wins
        text = ('ICCID PSKID KIC1 KID1 PSK\n' + DIGITS_F + ' 89390100000129506903 ' +
                K16A + ' ' + K16B + ' ' + psk + '\n')
        p = outfile.parse_outfile(text)['presets'][0]
        self.assertEqual(p['pskIdentity'], '89390100000129506903')
        # a wrong length is dropped with a report line
        res = outfile.parse_outfile('ICCID KIC1 KID1 PSK\n' + DIGITS_F + ' ' +
                                    K16A + ' ' + K16B + ' AB\n')
        self.assertNotIn('pskKey', res['presets'][0])
        self.assertTrue(any('PSK is not 16 bytes' in s for s in res['report']['skipped']))

    def test_short_rows_and_bad_optional_codes_are_reported(self):
        # a row shorter than the *used* columns (the keyset) is skipped
        text = ('ICCID KIC1 KID1\n' + DIGITS_F + ' ' + K16A + '\n')
        res = outfile.parse_outfile(text)
        self.assertEqual(res['presets'], [])
        self.assertTrue(any('too few columns' in s for s in res['report']['skipped']))
        # a missing optional column is simply absent, the card kept
        text = ('ICCID KIC1 KID1 PIN1 ADM\n' + DIGITS_F + ' ' + K16A + ' ' + K16B +
                ' 1234\n')
        res = outfile.parse_outfile(text)
        self.assertEqual(len(res['presets']), 1)
        self.assertEqual(res['presets'][0]['pin1'], '1234')
        self.assertNotIn('adm', res['presets'][0])
        # a present ADM is decoded from its ASCII-hex form
        text = ('ICCID KIC1 KID1 ADM\n' + DIGITS_F + ' ' + K16A + ' ' + K16B +
                ' 3632333239463337\n')
        res = outfile.parse_outfile(text)
        self.assertEqual(res['presets'][0]['adm'], '62329F37')
        # a present but invalid PIN is dropped, the card kept
        text = ('ICCID KIC1 KID1 PIN1\n' + DIGITS_F + ' ' + K16A + ' ' + K16B + ' 12\n')
        res = outfile.parse_outfile(text)
        self.assertEqual(len(res['presets']), 1)
        self.assertNotIn('pin1', res['presets'][0])
        self.assertTrue(any('PIN1' in s for s in res['report']['skipped']))
