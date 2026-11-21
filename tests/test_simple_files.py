# coding=utf-8
"""Tests for the Phase 1 ``simple_server`` modules.

Covers FCP/FCI parsing, the file model, the file operations and card-type
detection.  No reader is needed: every card exchange is driven through a fake
transport with scripted ``(data, sw)`` responses.
"""

import unittest

from simple_server import cards
from simple_server import commands
from simple_server import fcp
from simple_server import files


def ber(tag, value):
    """A short-form BER TLV (all test values are below 128 bytes)."""
    return bytes((tag, len(value))) + value


def fcp_template(*data_objects):
    """An FCP template (tag 62) around the given data objects."""
    return ber(0x62, b''.join(data_objects))


def fci_template(*data_objects):
    """An FCI template (tag 6F) around the given data objects/FCP template."""
    return ber(0x6F, b''.join(data_objects))


# TS 102 221 §11.1.1.4.3 file descriptors: byte 0, data coding 21, then the
# record length and record count for a record-structured file.
FD_TRANSPARENT = bytes.fromhex('4121')            # shareable, transparent EF
FD_LINEAR = bytes.fromhex('4221006E05')           # linear fixed, 110 x 5
FD_CYCLIC = bytes.fromhex('4621001E03')           # cyclic, 30 x 3
FD_BER_TLV = bytes.fromhex('3921')                # BER-TLV EF
FD_DF = bytes.fromhex('7821')                     # shareable DF

TRANSPARENT_FCP = fcp_template(ber(0x80, b'\x00\x10'),
                               ber(0x82, FD_TRANSPARENT),
                               ber(0x83, b'\x2f\xe2'))


class FakeLink(object):
    """A transport stand-in with scripted responses and a recorded APDU list.

    ``responses`` is a list of ``(data, sw)`` tuples; ``sw`` may be a hex
    string.  ``get_atr``/``reset_card`` make it usable for ``init_card``.
    """

    def __init__(self, responses=None, atr=b'\x3b\x8f\x80\x01'):
        self.responses = list(responses or [])
        self.sent = []
        self.calls = []
        self.atr = atr

    def send_apdu(self, apdu):
        self.sent.append(bytes(apdu))
        if not self.responses:
            raise AssertionError('unexpected APDU %s' % bytes(apdu).hex())
        data, sw = self.responses.pop(0)
        if isinstance(sw, str):
            sw = bytes.fromhex(sw)
        return data, sw

    def reset_card(self):
        self.calls.append('reset')

    def get_atr(self):
        return self.atr

    def sent_hex(self):
        return [apdu.hex() for apdu in self.sent]


# ---- FCP parsing ----------------------------------------------------------

class FcpTests(unittest.TestCase):
    def test_transparent_ef(self):
        info = fcp.parse_fcp(TRANSPARENT_FCP)
        self.assertEqual(info.file_size, 16)
        self.assertEqual(info.file_identifier, '2FE2')
        self.assertEqual(info.structure, 'transparent')
        self.assertTrue(info.shareable)
        self.assertIsNone(info.record_len)
        self.assertIsNone(info.num_of_rec)
        self.assertEqual(info.raw, TRANSPARENT_FCP.hex().upper())

    def test_linear_fixed_records(self):
        data = fcp_template(ber(0x80, b'\x00\x6e'),
                            ber(0x82, FD_LINEAR),
                            ber(0x83, b'\x2f\xe2'))
        info = fcp.parse_fcp(data)
        self.assertEqual(info.file_size, 110)
        self.assertEqual(info.record_len, 110)
        self.assertEqual(info.num_of_rec, 5)
        self.assertEqual(info.structure, 'linear_fixed')

    def test_cyclic_records(self):
        info = fcp.parse_fcp(fcp_template(ber(0x82, FD_CYCLIC)))
        self.assertEqual(info.structure, 'cyclic')
        self.assertEqual((info.record_len, info.num_of_rec), (30, 3))

    def test_file_descriptor_structure_bits(self):
        # TS 102 221 Table 11.5: 41 transparent, 42 linear fixed, 46 cyclic,
        # 39 BER-TLV, 78 a DF; bit b7 (0x40) is the shareable bit.
        expected = {
            0x41: ('transparent', True),
            0x42: ('linear_fixed', True),
            0x46: ('cyclic', True),
            0x39: ('ber_tlv', False),
            0x78: ('no_info', True),
            0x01: ('transparent', False),
        }
        for descriptor, (structure, shareable) in expected.items():
            info = fcp.parse_fcp(
                fcp_template(ber(0x82, bytes((descriptor, 0x21)))))
            self.assertEqual(info.structure, structure, hex(descriptor))
            self.assertEqual(info.shareable, shareable, hex(descriptor))

    def test_ber_tlv_row_needs_both_bit_groups(self):
        # b6-b4 = 111 with b3-b1 = 001 is the BER-TLV row; 0x3F (bits 111)
        # is RFU and reports no information.
        self.assertEqual(fcp.parse_fcp(
            fcp_template(ber(0x82, b'\x39\x21'))).structure, 'ber_tlv')
        self.assertEqual(fcp.parse_fcp(
            fcp_template(ber(0x82, b'\x3f\x21'))).structure, 'no_info')

    def test_fci_template_wrapping_the_fcp(self):
        data = fci_template(ber(0x62, ber(0x80, b'\x00\x10') +
                                ber(0x82, FD_TRANSPARENT)))
        info = fcp.parse_fcp(data)
        self.assertEqual(info.file_size, 16)
        self.assertEqual(info.structure, 'transparent')

    def test_fci_template_with_direct_data_objects(self):
        data = fci_template(ber(0x84, bytes.fromhex('a0000000871002')),
                            ber(0x82, FD_DF))
        info = fcp.parse_fcp(data)
        self.assertEqual(info.df_name, 'A0000000871002')
        self.assertEqual(info.structure, 'no_info')

    def test_df_name_and_file_identifier(self):
        data = fcp_template(ber(0x84, bytes.fromhex('a0000000871002')),
                            ber(0x83, b'\x7f\x10'))
        info = fcp.parse_fcp(data)
        self.assertEqual(info.df_name, 'A0000000871002')
        self.assertEqual(info.file_identifier, '7F10')

    def test_hex_string_input_and_case(self):
        text = TRANSPARENT_FCP.hex()
        self.assertEqual(fcp.parse_fcp(text).file_size, 16)
        self.assertEqual(fcp.parse_fcp(' ' + text.upper() + ' ').file_size, 16)

    def test_missing_or_malformed_fcp_never_raises(self):
        for value in (b'', '', None, 'zz', '62', b'\x62\x05\x80\x02\x00',
                      b'\x00\x01\x02'):
            info = fcp.parse_fcp(value)
            self.assertIsNone(info.file_size, value)
            self.assertIsNone(info.structure, value)
            self.assertIsNone(info.shareable, value)

    def test_truncated_value_keeps_what_was_parsed(self):
        # The descriptor is complete, then a bogus TLV is cut short.
        data = bytes.fromhex('62') + bytes((6,)) + ber(0x82, FD_TRANSPARENT) \
            + b'\x80\x02\x00'
        info = fcp.parse_fcp(data)
        self.assertEqual(info.structure, 'transparent')
        self.assertIsNone(info.file_size)

    def test_select_response_helper(self):
        self.assertEqual(fcp.fcp_from_select_response(TRANSPARENT_FCP).file_size,
                         16)


# ---- file model -----------------------------------------------------------

ENTRIES = [
    {'path': 'MF/2FE2', 'name': 'EF.ICCID', 'sym': 'MF/EF.ICCID',
     'fid': '2FE2', 'kind': 'ef', 'root': 'MF'},
    {'path': 'MF/7F10', 'name': 'DF.TELECOM', 'sym': 'MF/DF.TELECOM',
     'fid': '7F10', 'kind': 'df', 'root': 'MF'},
    {'path': 'MF/7F10/6F3A', 'name': 'EF.ADN', 'sym': 'MF/DF.TELECOM/EF.ADN',
     'fid': '6F3A', 'kind': 'ef', 'root': 'MF'},
    {'path': 'MF/7F20', 'name': 'DF.GSM', 'sym': 'MF/DF.GSM',
     'fid': '7F20', 'kind': 'df', 'root': 'MF'},
    {'path': 'MF/7F20/6F07', 'name': 'EF.IMSI', 'sym': 'MF/DF.GSM/EF.IMSI',
     'fid': '6F07', 'kind': 'ef', 'root': 'MF'},
    {'path': 'ADF.USIM', 'name': 'ADF.USIM', 'sym': 'ADF.USIM', 'fid': '',
     'kind': 'adf', 'root': 'ADF.USIM', 'aid': 'A0000000871002'},
    {'path': 'ADF.USIM/6F07', 'name': 'EF.IMSI', 'sym': 'ADF.USIM/EF.IMSI',
     'fid': '6F07', 'kind': 'ef', 'root': 'ADF.USIM'},
]


class FileModelTests(unittest.TestCase):
    def setUp(self):
        self.model = files.FileModel(ENTRIES)

    def test_resolve_by_name_fid_and_path(self):
        self.assertEqual(self.model.resolve('EF.ICCID').fid, '2FE2')
        self.assertEqual(self.model.resolve('ef.iccid').path, 'MF/2FE2')
        self.assertEqual(self.model.resolve('2FE2').name, 'EF.ICCID')
        self.assertEqual(self.model.resolve('MF/7F10/6F3A').name, 'EF.ADN')
        self.assertEqual(self.model.resolve('MF/DF.TELECOM/EF.ADN').fid,
                         '6F3A')
        self.assertEqual(self.model.resolve('A0000000871002').kind, 'adf')
        self.assertEqual(self.model.resolve('MF').kind, 'mf')

    def test_ambiguous_bare_fid_raises(self):
        with self.assertRaises(files.AmbiguousFile) as caught:
            self.model.resolve('6F07')
        self.assertEqual(len(caught.exception.candidates), 2)
        # The path disambiguates the two EF.IMSI copies.
        self.assertEqual(self.model.resolve('MF/7F20/6F07').root, 'MF')
        self.assertEqual(self.model.resolve('ADF.USIM/6F07').root, 'ADF.USIM')

    def test_ambiguous_name_raises(self):
        with self.assertRaises(files.AmbiguousFile):
            self.model.resolve('EF.IMSI')

    def test_unknown_selector_raises(self):
        with self.assertRaises(files.UnknownFile):
            self.model.resolve('EF.NOPE')
        with self.assertRaises(files.UnknownFile):
            self.model.resolve_path('MF/7F10/EF.NOPE')

    def test_add_custom_marks_the_source_and_path(self):
        info = self.model.add_custom('EF.MY', '7F99', 'DF.TELECOM')
        self.assertEqual(info.source, 'custom')
        self.assertEqual(info.path, 'MF/7F10/7F99')
        self.assertEqual(info.parent, 'DF.TELECOM')
        self.assertTrue(info.parent_known)
        self.assertEqual(self.model.resolve('EF.MY').fid, '7F99')
        self.assertIn(info, self.model.children('DF.TELECOM'))

    def test_add_custom_under_an_unknown_parent_is_flagged(self):
        info = self.model.add_custom('EF.ORPHAN', '7F98', 'MF/DF.UNSEEN')
        self.assertEqual(info.source, 'custom')
        self.assertEqual(info.path, 'MF/DF.UNSEEN/7F98')
        self.assertFalse(info.parent_known)
        with self.assertRaises(files.FileModelError):
            self.model.path_chain('EF.ORPHAN')

    def test_add_custom_under_a_name_path_resolves(self):
        # 'MF/DF.TELECOM' is a mixed name/FID path, not a _by_path key: the
        # parent is resolved segment by segment, so the file is selectable.
        info = self.model.add_custom('EF.NAMED', '2F99', 'MF/DF.TELECOM')
        self.assertTrue(info.parent_known)
        self.assertEqual(info.parent, 'DF.TELECOM')
        self.assertEqual(info.path, 'MF/7F10/2F99')
        self.assertEqual(self.model.path_chain('EF.NAMED'),
                         ['3F00', '7F10', '2F99'])
        self.assertIn(info, self.model.children('DF.TELECOM'))

    def test_add_custom_under_a_fid_path_still_works(self):
        info = self.model.add_custom('EF.FIDPATH', '2F9A', 'MF/7F10')
        self.assertTrue(info.parent_known)
        self.assertEqual(info.path, 'MF/7F10/2F9A')
        self.assertEqual(self.model.path_chain('EF.FIDPATH'),
                         ['3F00', '7F10', '2F9A'])
        self.assertIn(info, self.model.children('7F10'))

    def test_a_mixed_path_naming_an_ef_is_not_a_parent(self):
        info = self.model.add_custom('EF.NOTDF', '2F9B', 'MF/EF.ICCID')
        self.assertFalse(info.parent_known)
        with self.assertRaises(files.FileModelError):
            self.model.path_chain('EF.NOTDF')

    def test_add_custom_validates_the_fid(self):
        with self.assertRaises(ValueError):
            self.model.add_custom('EF.BAD', '123', 'DF.TELECOM')
        with self.assertRaises(ValueError):
            self.model.add_custom('EF.BAD', 'ZZZZ', 'DF.TELECOM')

    def test_path_chain_for_a_nested_file(self):
        self.assertEqual(self.model.path_chain('EF.ADN'),
                         ['3F00', '7F10', '6F3A'])
        self.assertEqual(self.model.path_chain('MF/7F10/6F3A'),
                         ['3F00', '7F10', '6F3A'])

    def test_path_chain_under_an_adf_starts_with_the_aid(self):
        self.assertEqual(self.model.path_chain('ADF.USIM/6F07'),
                         ['A0000000871002', '6F07'])

    def test_children_and_iter_all(self):
        names = [info.name for info in self.model.children('MF')]
        self.assertIn('DF.TELECOM', names)
        self.assertIn('EF.ICCID', names)
        adn = [info.name for info in self.model.children('DF.TELECOM')]
        self.assertEqual(adn, ['EF.ADN'])
        self.assertEqual(self.model.children('EF.ICCID'), [])
        self.assertEqual(len(list(self.model.iter_all())), len(ENTRIES) + 1)

    def test_from_asset_real_file(self):
        try:
            model = files.FileModel.from_asset()
        except (OSError, ValueError) as exc:
            self.skipTest('no committed asset: %s' % exc)
        self.assertEqual(model.resolve('EF.ICCID').fid, '2FE2')
        self.assertEqual(model.resolve('DF.TELECOM').fid, '7F10')
        self.assertEqual(model.resolve('MF/7F10/5F3A/4F30').fid, '4F30')
        self.assertEqual(model.path_chain('ADF.USIM/6F07')[0],
                         'A0000000871002')


# ---- file operations ------------------------------------------------------

class FileOpsTests(unittest.TestCase):
    def test_select_file_returns_the_parsed_fcp(self):
        link = FakeLink([(TRANSPARENT_FCP, '9000')])
        info = commands.FileOps(link).select_file('2fe2')
        self.assertEqual(info.file_size, 16)
        self.assertEqual(info.structure, 'transparent')
        self.assertEqual(link.sent_hex(), ['00a40004022fe200'])

    def test_select_file_not_found(self):
        for sw in ('6A82', '6A86'):
            link = FakeLink([(b'', sw)])
            with self.assertRaises(commands.FileNotFound) as caught:
                commands.FileOps(link).select_file('1234')
            self.assertEqual(caught.exception.sw, sw.lower())
            self.assertEqual(caught.exception.fid, '1234')

    def test_select_file_other_sw_raises(self):
        link = FakeLink([(b'', '6982')])
        with self.assertRaises(commands.UnexpectedStatusWord):
            commands.FileOps(link).select_file('1234')

    def test_select_file_sim_form(self):
        link = FakeLink([(b'', '9000')])
        commands.FileOps(link, cla='a0', sel_fcp=False).select_mf()
        self.assertEqual(link.sent_hex(), ['a0a40000023f00'])

    def test_select_adf(self):
        link = FakeLink([(b'', '9000')])
        commands.FileOps(link).select_adf('A0000000871002')
        self.assertEqual(link.sent_hex(),
                         ['00a4040407a000000087100200'])

    def test_select_path_selects_each_segment(self):
        link = FakeLink([(b'', '9000'), (b'', '9000'), (b'', '9000')])
        commands.FileOps(link).select_path(['3F00', '7F10', '6F3A'])
        self.assertEqual(link.sent_hex(), [
            '00a40004023f0000',
            '00a40004027f1000',
            '00a40004026f3a00',
        ])

    def test_select_path_takes_a_slash_string(self):
        link = FakeLink([(b'', '9000'), (b'', '9000')])
        commands.FileOps(link).select_path('3F00/7F10')
        self.assertEqual(len(link.sent), 2)

    def test_read_binary_and_record_apdus(self):
        link = FakeLink([(b'\xaa\xbb', '9000'), (b'\x01\x02', '9000')])
        ops = commands.FileOps(link)
        self.assertEqual(ops.read_binary(0x10, 4), b'\xaa\xbb')
        self.assertEqual(ops.read_record(3, 2), b'\x01\x02')
        self.assertEqual(link.sent_hex(), ['00b0001004', '00b2030402'])

    def test_update_apdus(self):
        link = FakeLink([(b'', '9000'), (b'', '9000')])
        ops = commands.FileOps(link)
        self.assertEqual(ops.update_binary(0x10, 'aabb'), '9000')
        self.assertEqual(ops.update_record(3, b'\x01\x02'), '9000')
        self.assertEqual(link.sent_hex(), ['00d6001002aabb',
                                           '00dc0304020102'])

    def test_status_uses_the_cat_cla(self):
        link = FakeLink([(b'', '9000')])
        commands.FileOps(link).status()
        self.assertEqual(link.sent_hex(), ['80f2000000'])
        link = FakeLink([(b'', '9000')])
        commands.FileOps(link, status_cla='a0').status()
        self.assertEqual(link.sent_hex(), ['a0f2000000'])

    def test_read_file_chunks_a_transparent_file(self):
        data = b'\xaa' * 255 + b'\xbb' * 45
        link = FakeLink([(b'\xaa' * 255, '9000'), (b'\xbb' * 45, '9000')])
        info = fcp.parse_fcp(fcp_template(ber(0x80, b'\x01\x2c'),
                                          ber(0x82, FD_TRANSPARENT)))
        payload, sw = commands.FileOps(link).read_file(info)
        self.assertEqual(payload, data)
        self.assertEqual(sw, '9000')
        self.assertEqual(link.sent_hex(), ['00b00000ff', '00b000ff2d'])

    def test_read_file_uses_the_fcp_size_and_offset(self):
        link = FakeLink([(b'\x01' * 200, '9000')])
        info = fcp.parse_fcp(fcp_template(ber(0x80, b'\x01\x2c'),
                                          ber(0x82, FD_TRANSPARENT)))
        payload, _sw = commands.FileOps(link).read_file(info, offset=100)
        self.assertEqual(len(payload), 200)
        self.assertEqual(link.sent_hex(), ['00b00064c8'])

    def test_read_file_reads_a_record_at_the_fcp_length(self):
        link = FakeLink([(b'\x01\x02\x03\x04', '9000')])
        info = fcp.parse_fcp(
            fcp_template(ber(0x82, bytes.fromhex('4221000402'))))
        payload, sw = commands.FileOps(link).read_file(info, rec=2)
        self.assertEqual(payload, b'\x01\x02\x03\x04')
        self.assertEqual(sw, '9000')
        self.assertEqual(link.sent_hex(), ['00b2020404'])

    def test_read_file_needs_a_size_or_an_explicit_length(self):
        info = fcp.parse_fcp(fcp_template(ber(0x82, FD_TRANSPARENT)))
        with self.assertRaises(ValueError):
            commands.FileOps(FakeLink([])).read_file(info)
        link = FakeLink([(b'\x99' * 4, '9000')])
        self.assertEqual(commands.FileOps(link).read_file(info, length=4)[0],
                         b'\x99' * 4)

    def test_read_file_needs_a_record_number_for_a_record_file(self):
        info = fcp.parse_fcp(fcp_template(ber(0x82, FD_LINEAR)))
        with self.assertRaises(ValueError):
            commands.FileOps(FakeLink([])).read_file(info)

    def test_arguments_are_validated(self):
        ops = commands.FileOps(FakeLink([]))
        with self.assertRaises(ValueError):
            ops.select_file('123')
        with self.assertRaises(ValueError):
            ops.select_file('ZZZZ')
        with self.assertRaises(ValueError):
            ops.read_binary(0, 0)
        with self.assertRaises(ValueError):
            ops.read_record(0, 4)
        with self.assertRaises(ValueError):
            ops.update_binary(0, b'')
        with self.assertRaises(ValueError):
            ops.update_binary(0, b'x' * 256)
        with self.assertRaises(ValueError):
            ops.read_binary(0x8000, 1)
        with self.assertRaises(ValueError):
            commands.FileOps(FakeLink([]), max_cmd_len=0)


# ---- card detection and init ----------------------------------------------

def uicc_script():
    """USIM present, ISD-R absent: a plain UICC."""
    return [(b'', '9000'), (b'', '6A82')]


class CardDetectionTests(unittest.TestCase):
    def test_detect_uicc(self):
        link = FakeLink(uicc_script())
        card = cards.detect_card(link)
        self.assertEqual(card.kind, 'uicc')
        self.assertEqual(card.cla, '80')
        self.assertEqual(card.aids, [cards.AID_USIM])
        self.assertEqual(link.sent_hex(), [
            '00a4040407a000000087100200',
            '00a4040410a0000005591010ffffffff890000010000',
        ])

    def test_detect_euicc(self):
        link = FakeLink([(b'', '9000'), (b'', '9000')])
        card = cards.detect_card(link)
        self.assertEqual(card.kind, 'euicc')
        self.assertEqual(card.cla, '80')
        self.assertEqual(card.aids, [cards.AID_USIM, cards.AID_ISD_R])

    def test_detect_uicc_when_only_the_iso_mf_select_works(self):
        # A UICC without a selectable USIM still answers the TS 102 221
        # SELECT form, so it is UICC-class (never a 2G SIM): the TS 51 011
        # SIM form is the only one that means CLA A0.
        link = FakeLink([(b'', '6A82'), (b'', '9000'), (b'', '6A82')])
        card = cards.detect_card(link)
        self.assertEqual(card.kind, 'uicc')
        self.assertEqual(card.cla, '80')
        self.assertEqual(card.aids, [])
        self.assertEqual(link.sent_hex()[1], '00a40004023f0000')

    def test_detect_sim_by_the_ts_51_011_select(self):
        link = FakeLink([(b'', '6A82'), (b'', '6A86'), (b'', '9000')])
        card = cards.detect_card(link)
        self.assertEqual(card.kind, 'sim')
        self.assertEqual(card.cla, 'a0')
        self.assertEqual(link.sent_hex()[2], 'a0a40000023f00')

    def test_detect_fails_when_nothing_answers(self):
        link = FakeLink([(b'', '6A82'), (b'', '6A82'), (b'', '6A82')])
        with self.assertRaises(cards.CardError):
            cards.detect_card(link)


class CardInitTests(unittest.TestCase):
    def test_init_card_detects_and_selects_mf(self):
        link = FakeLink([(b'', '9000'), (b'', '6A82'), (b'', '9000')])
        card = cards.init_card(link)
        self.assertEqual(card.kind, 'uicc')
        self.assertEqual(card.cla, '80')
        self.assertEqual(card.atr, b'\x3b\x8f\x80\x01')
        self.assertEqual(link.calls, ['reset'])
        self.assertEqual(link.sent_hex(), [
            '00a4040407a000000087100200',
            '00a4040410a0000005591010ffffffff890000010000',
            '00a40004023f0000',
        ])

    def test_init_card_skip_init_only_connects_and_reads_the_atr(self):
        link = FakeLink([])
        card = cards.init_card(link, skip_init=True)
        self.assertEqual(card.kind, 'unknown')
        self.assertEqual(card.atr, b'\x3b\x8f\x80\x01')
        self.assertEqual(link.calls, ['reset'])
        self.assertEqual(link.sent, [])

    def test_init_card_reuses_the_sim_command_form(self):
        link = FakeLink([(b'', '6A82'), (b'', '6A86'), (b'', '9000'),
                         (b'', '9000')])
        card = cards.init_card(link)
        self.assertEqual(card.kind, 'sim')
        self.assertEqual(link.sent_hex()[-1], 'a0a40000023f00')

    def test_init_card_retries_mf_after_a_reset(self):
        link = FakeLink([(b'', '9000'), (b'', '9000'), (b'', '6A82'),
                         (b'', '9000')])
        card = cards.init_card(link)
        self.assertEqual(card.kind, 'euicc')
        # Initial reset, then the MF-retry reset.
        self.assertEqual(link.calls, ['reset', 'reset'])
        self.assertEqual(link.sent_hex()[-1], '00a40004023f0000')


if __name__ == '__main__':
    unittest.main()
