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

    def test_a_header_without_a_usable_column_is_not_the_column_line(self):
        # a line carrying ICCID and nothing the import can consume is not the
        # header (every card needs a KICn/KIDn pair)
        text = ('ICCID CUSTOMER_NAME\nICCID KIC1 KID1\n' + DIGITS_F + ' ' +
                K16A + ' ' + K16B + '\n')
        cols, i = outfile.find_columns(text)
        self.assertEqual(i, 1)
        self.assertEqual(cols, ['ICCID', 'KIC1', 'KID1'])
        # ...while a long unknown column name next to a real one is fine
        text = ('ICCID TRANSPORT_KEY_IDENTIFIER KIC1 KID1\n' + DIGITS_F + ' ' +
                'AB ' + K16A + ' ' + K16B + '\n')
        cols, i = outfile.find_columns(text)
        self.assertEqual(i, 0)
        self.assertEqual(len(cols), 4)


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
        self.assertEqual(p['adm'], '3632333239463337')   # the key's hex value as-is
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

    def test_the_per_row_keysets_win(self):
        """A TAR entry's own keyset (chosen in the preview) is used; the global
        default fills the entries without one, then the card's first keyset
        (v3.29.1)."""
        text = ('ICCID KIC1 KID1 KIC2 KID2\n' + DIGITS_F + ' ' + K16A + ' ' + K16B +
                ' ' + K16C + ' ' + K16A + '\n')
        tars = [{'role': 'isd', 'tar': '000000', 'msl': '16', 'keyset': 2},
                {'role': 'uiccRfm', 'tar': 'B00000', 'msl': '16'},
                {'tar': 'B00011', 'msl': '1A', 'desc': 'push', 'keyset': 1}]
        res = outfile.parse_outfile(text, tars=tars)
        got = [(t['tar'], t['keyset']) for t in res['presets'][0]['tars']]
        self.assertEqual(got[0], ('000000', 2))          # its own keyset
        self.assertEqual(got[-1], ('B00011', 1))         # its own keyset
        self.assertEqual(got[1], ('B00000', 1))          # the card's first keyset
        # the global default fills the entries without one; an entry's own
        # keyset still wins over it
        res = outfile.parse_outfile(text, tars=tars, tar_keyset=2)
        self.assertEqual([t['keyset'] for t in res['presets'][0]['tars']], [2, 2, 1])
        # an unusable keyset is the caller's error
        with self.assertRaises(ValueError):
            outfile.parse_outfile(text, tars=[{'tar': '000000', 'msl': '16', 'keyset': 99}])
        with self.assertRaises(ValueError):
            outfile.parse_outfile(text, tars=[{'tar': '000000', 'msl': '16', 'keyset': 'x'}])

    def test_the_start_counter_is_written_into_every_keyset(self):
        """The import form's "Start counter value": every created keyset
        carries it (the store pads it to the protocol's 10-digit form); without
        it the store's default ('1') applies.  An unusable value is the
        caller's error, never a silent default (v3.32.1)."""
        res = outfile.parse_outfile(alfa_text(), start_counter='1a')
        ks = res['presets'][0]['keysets']
        self.assertGreaterEqual(len(ks), 2)
        self.assertEqual([k.get('cntr') for k in ks], ['1A'] * len(ks))
        # normalised: whitespace stripped, uppercase
        res = outfile.parse_outfile(alfa_text(), start_counter=' a1 ')
        self.assertEqual(res['presets'][0]['keysets'][0]['cntr'], 'A1')
        # unset: no field at all - the store fills its default
        res = outfile.parse_outfile(alfa_text())
        self.assertNotIn('cntr', res['presets'][0]['keysets'][0])
        # an unusable value refuses the whole parse
        for bad in ('XYZ', '1G', '1234567890A', '   '):
            with self.assertRaises(ValueError):
                outfile.parse_outfile(alfa_text(), start_counter=bad)

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

    def test_a_row_with_a_missing_or_extra_value_is_a_file_error(self):
        """The file is positional: an empty value drops its token, so a token
        count other than the header's column count means an empty (or extra)
        value - the whole file is refused (v3.29.2)."""
        # a missing trailing value
        text = ('ICCID KIC1 KID1\n' + DIGITS_F + ' ' + K16A + '\n')
        res = outfile.parse_outfile(text)
        self.assertEqual(res['presets'], [])
        self.assertIn('line 2 has 2 values, the header declares 3 columns',
                      res['report']['error'])
        # ...a missing middle value (which used to shift the columns silently)
        text = ('ICCID KIC1 KID1 ADM\n' + DIGITS_F + ' ' + K16A + ' ' + K16B + '\n')
        res = outfile.parse_outfile(text)
        self.assertEqual(res['presets'], [])
        self.assertIn('the file is invalid', res['report']['error'])
        # ...and an extra value
        text = ('ICCID KIC1 KID1\n' + DIGITS_F + ' ' + K16A + ' ' + K16B + ' EXTRA\n')
        res = outfile.parse_outfile(text)
        self.assertEqual(res['presets'], [])
        self.assertIn('4 values', res['report']['error'])
        # a declared keyset without a usable value is a file error too
        text = ('ICCID KIC1 KID1 KIC2 KID2\n' + DIGITS_F + ' ' + K16A + ' ' + K16B +
                ' - ' + K16C + '\n')
        res = outfile.parse_outfile(text)
        self.assertEqual(res['presets'], [])
        self.assertIn('keyset 2 has no usable KIC/KID value', res['report']['error'])
        # a valid file keeps parsing (an exact count everywhere)
        text = ('ICCID KIC1 KID1 PIN1 ADM\n' + DIGITS_F + ' ' + K16A + ' ' + K16B +
                ' 1234 3632333239463337\n')
        res = outfile.parse_outfile(text)
        self.assertEqual(len(res['presets']), 1)
        self.assertEqual(res['presets'][0]['pin1'], '1234')
        self.assertEqual(res['presets'][0]['adm'], '3632333239463337')

    def test_bad_optional_codes_are_reported_per_row(self):
        # the ADM column is the key's hex value, stored as-is (v3.28.2): the
        # length varies, and the value is never ASCII-decoded
        text = ('ICCID KIC1 KID1 ADM\n' + DIGITS_F + ' ' + K16A + ' ' + K16B +
                ' 3632333239463337\n')
        res = outfile.parse_outfile(text)
        self.assertEqual(res['presets'][0]['adm'], '3632333239463337')
        # ...an 8-byte binary-looking value (the AM0027 sample's shape)
        text = ('ICCID KIC1 KID1 ADM\n' + DIGITS_F + ' ' + K16A + ' ' + K16B +
                ' CFA3DD9C6B7F0F8C\n')
        res = outfile.parse_outfile(text)
        self.assertEqual(res['presets'][0]['adm'], 'CFA3DD9C6B7F0F8C')
        # ...a shorter key (4 hex digits) and the lowercase form
        text = ('ICCID KIC1 KID1 ADM\n' + DIGITS_F + ' ' + K16A + ' ' + K16B +
                ' 1a2b3c4d\n')
        res = outfile.parse_outfile(text)
        self.assertEqual(res['presets'][0]['adm'], '1A2B3C4D')
        # a non-hex or too-short ADM is dropped with its reason
        text = ('ICCID KIC1 KID1 ADM\n' + DIGITS_F + ' ' + K16A + ' ' + K16B +
                ' ZZZZ\n')
        res = outfile.parse_outfile(text)
        self.assertNotIn('adm', res['presets'][0])
        self.assertTrue(any('ADM is not 4-16 hex digits' in s
                            for s in res['report']['skipped']))
        text = ('ICCID KIC1 KID1 ADM\n' + DIGITS_F + ' ' + K16A + ' ' + K16B +
                ' 123\n')
        res = outfile.parse_outfile(text)
        self.assertNotIn('adm', res['presets'][0])
        self.assertTrue(any('ADM is not 4-16 hex digits' in s
                            for s in res['report']['skipped']))
        # an odd length is a half byte: /api/verify-adm would always refuse the
        # stored value, so it is dropped at import time (v3.28.3)
        text = ('ICCID KIC1 KID1 ADM\n' + DIGITS_F + ' ' + K16A + ' ' + K16B +
                ' 12345\n')
        res = outfile.parse_outfile(text)
        self.assertNotIn('adm', res['presets'][0])
        self.assertTrue(any('ADM is not 4-16 hex digits in byte pairs' in s
                            for s in res['report']['skipped']))
        # ...and so is a value over the 16-digit limit (the verifier pads to 8
        # CHV bytes, a longer value would overflow)
        text = ('ICCID KIC1 KID1 ADM\n' + DIGITS_F + ' ' + K16A + ' ' + K16B +
                ' ' + 'AB' * 9 + '\n')
        res = outfile.parse_outfile(text)
        self.assertNotIn('adm', res['presets'][0])
        self.assertTrue(any('ADM is not 4-16 hex digits in byte pairs' in s
                            for s in res['report']['skipped']))
        # a present but invalid PIN is dropped, the card kept
        text = ('ICCID KIC1 KID1 PIN1\n' + DIGITS_F + ' ' + K16A + ' ' + K16B + ' 12\n')
        res = outfile.parse_outfile(text)
        self.assertEqual(len(res['presets']), 1)
        self.assertNotIn('pin1', res['presets'][0])
        self.assertTrue(any('PIN1' in s for s in res['report']['skipped']))
        # a present but unusable value is named with its raw text (a value that
        # strips to nothing must not vanish silently)
        text = ('ICCID KIC1 KID1 PUK1\n' + DIGITS_F + ' ' + K16A + ' ' + K16B + ' abcd\n')
        res = outfile.parse_outfile(text)
        self.assertNotIn('puk1', res['presets'][0])
        self.assertTrue(any("PUK1 'abcd'" in s for s in res['report']['skipped']))
        # ...while an *empty* PUK1 token cannot exist in a whitespace-separated
        # row: the count would change, which is a file error (v3.29.2)
        text = ('ICCID KIC1 KID1 PIN1 PUK1\n' + DIGITS_F + ' ' + K16A + ' ' + K16B + ' 1234\n')
        res = outfile.parse_outfile(text)
        self.assertEqual(res['presets'], [])
        self.assertIn('the file is invalid', res['report']['error'])
