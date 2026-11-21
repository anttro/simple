# coding=utf-8
"""Tests for :mod:`simple_server.sms` and :mod:`simple_server.cat`.

The SMS TPDU fixtures and their expected bytes are the ones the existing
server's tests pin (``tests/test_ota_helpers.py``), copied as literals so this
suite stays free of any pysim/pysim_simple_server import.  The SMS-PP download
envelope is pinned against the bytes pySim's ``SMSPPDownload`` builds for the
same objects, so both implementations produce identical envelopes.
"""

import unittest
from datetime import datetime, timedelta, timezone

from simple_server import cat
from simple_server import sms
from simple_server import text

# A live capture of an actual-response SMS-SUBMIT carrying the first segment of
# a RAM ELF listing (the public fixture of the existing suite, 151 octets).
SUBMIT_SEGMENT_1 = (
    '510005812143f57ff6058c070003130201710000e90a000000000000030600000263100b'
    'd276000005aaffcafe0001010007a000000151535001000bd276000005aaffcafe001001'
    '000bd276000005aaffcafe0304010010d2760001180002ff491ff3890000010101000bd2'
    '76000005aaffcafe0002010007a0000000620001010007a0000000620002010007a00000'
    '0062010101'
    '0006')
# The second segment (120 octets).
SUBMIT_SEGMENT_2 = (
    '510005812143f57ff6056d050003130202a00000015100010007a0000000620102010007'
    'a0000000620201010008a000000062020801010009a00000006202080101010010a00000'
    '00090003ffffffff8910710001010010a0000000090003ffffffff891071000201000bd2'
    '76000005aaffcafe00030100')
# The FETCH responses those TPDUs travelled in (BER long-form 8B length).
FETCH_SEGMENT_1 = (
    'd081ae81030113008202818305000607812143658719f28b8197510005812143f57ff6'
    '058c070003130201710000e90a000000000000030600000263100bd276000005aaffcafe'
    '0001010007a000000151535001000bd276000005aaffcafe001001000bd276000005aaff'
    'cafe0304010010d2760001180002ff491ff3890000010101000bd276000005aaffcafe00'
    '02010007a0000000620001010007a0000000620002010007a0000000620101010006')

# The second segment's FETCH response.
FETCH_SEGMENT_2 = (
    'd0818e81030113008202818305000607812143658719f28b78510005812143f57ff6'
    '056d050003130202a00000015100010007a0000000620102010007a000000062020101'
    '0008a000000062020801010009a00000006202080101010010a0000000090003ffffff'
    'ff8910710001010010a0000000090003ffffffff891071000201000bd276000005aaff'
    'cafe00030100')


class TestUserDataHeader(unittest.TestCase):
    def test_concat_and_payload(self):
        header, remainder = sms.UserDataHeader.from_bytes(
            bytes.fromhex('050003040401aabbcc'))
        self.assertEqual(header.concat_info(), (0x04, 4, 1))
        self.assertTrue(header.has_ie(0x00))
        self.assertEqual(remainder, bytes.fromhex('aabbcc'))
        self.assertEqual(header.to_bytes(), bytes.fromhex('050003040401'))
        self.assertEqual(header.octets, 6)
        self.assertEqual(header.septets, (6 * 8 + 6) // 7)

    def test_sixteen_bit_concat(self):
        header, remainder = sms.UserDataHeader.from_bytes(
            bytes.fromhex('06080401020304ff'))
        self.assertEqual(header.concat_info(), (0x0102, 3, 4))
        self.assertEqual(remainder, bytes.fromhex('ff'))

    def test_rpi_ie(self):
        header, remainder = sms.UserDataHeader.from_bytes(
            bytes.fromhex('027100') + bytes.fromhex('000e'))
        self.assertTrue(header.has_ie(0x71))
        self.assertEqual(header.get_ie(0x71), b'')
        self.assertEqual(remainder, bytes.fromhex('000e'))
        self.assertIsNone(header.concat_info())

    def test_no_concat_ie(self):
        header, _ = sms.UserDataHeader.from_bytes(bytes.fromhex('027000'))
        self.assertIsNone(header.concat_info())
        self.assertFalse(header.has_ie(0x00))

    def test_concat_builder(self):
        self.assertEqual(sms.UserDataHeader.concat(1, 3, 2).to_bytes(),
                         bytes.fromhex('050003010302'))
        self.assertEqual(sms.UserDataHeader.concat(0x102, 3, 2, sixteen_bit=True)
                         .to_bytes(), bytes.fromhex('06080401020302'))

    def test_truncated_header_raises(self):
        with self.assertRaises(sms.SmsError):
            sms.UserDataHeader.from_bytes(b'')
        with self.assertRaises(sms.SmsError):
            sms.UserDataHeader.from_bytes(bytes.fromhex('0500030404'))

    def test_empty_header(self):
        header, remainder = sms.UserDataHeader.from_bytes(bytes.fromhex('00aabb'))
        self.assertEqual(header.ies, [])
        self.assertEqual(remainder, bytes.fromhex('aabb'))


class TestSmsSubmit(unittest.TestCase):
    def test_live_actual_response_segment(self):
        submit = sms.SmsSubmit.from_bytes(SUBMIT_SEGMENT_1)
        self.assertEqual(submit.da, '12345')
        self.assertEqual(submit.pid, 0x7F)
        self.assertEqual(submit.dcs, 0xF6)
        self.assertEqual(submit.udl, 140)
        self.assertEqual(submit.vpf, 'relative')
        self.assertEqual(submit.vp, b'\x05')
        self.assertTrue(submit.udhi)
        self.assertEqual(len(submit.ud), 140)
        self.assertEqual(submit.udh.concat_info(), (0x13, 2, 1))
        self.assertTrue(submit.udh.has_ie(0x71))
        # PID 7F is a secured packet (TS 31.115), not text
        self.assertEqual(submit.text, '')
        self.assertEqual(len(submit.secured_packet), 132)
        self.assertTrue(submit.secured_packet.hex().startswith('00e90a'))

    def test_packed_text(self):
        body = text.pack_gsm7('hellohello'.encode('gsm03.38'))
        tpdu = bytes.fromhex('010005902143F5' + '0000' + '0a') + body
        submit = sms.SmsSubmit.from_bytes(tpdu)
        self.assertEqual(submit.da, '12345')
        self.assertEqual((submit.pid, submit.dcs, submit.udl), (0, 0, 10))
        self.assertIsNone(submit.udh)
        self.assertEqual(submit.text, 'hellohello')

    def test_eight_bit_text_with_udh(self):
        ud = bytes.fromhex('050003010201') + b'hello'
        tpdu = (bytes.fromhex('4100') + bytes.fromhex('05902143f5')
                + bytes.fromhex('0004') + bytes([len(ud)]) + ud)
        submit = sms.SmsSubmit.from_bytes(tpdu)
        self.assertTrue(submit.udhi)
        self.assertEqual(submit.udh.concat_info(), (0x01, 2, 1))
        self.assertEqual(submit.ud, ud)
        self.assertEqual(submit.text, 'hello')

    def test_ucs2_text(self):
        payload = 'привет'.encode('utf-16-be')
        ud = bytes([len(payload)]) + payload
        tpdu = (bytes.fromhex('010005902143f5') + bytes.fromhex('0008')
                + ud)
        submit = sms.SmsSubmit.from_bytes(tpdu)
        self.assertEqual(submit.text, 'привет')

    def test_build_round_trip(self):
        original = sms.SmsSubmit(da='123456', da_ton='international',
                                 da_npi='isdn_e164', mr=7, pid=0x7F, dcs=0xF6,
                                 ud=bytes.fromhex('050003010201deadbeef'),
                                 udhi=True, rd=True, srr=True,
                                 vpf='relative', vp=b'\x0a')
        encoded = original.to_bytes()
        parsed = sms.SmsSubmit.from_bytes(encoded)
        self.assertEqual(parsed.da, '123456')
        self.assertEqual(parsed.da_ton, 'international')
        self.assertEqual((parsed.mr, parsed.pid, parsed.dcs), (7, 0x7F, 0xF6))
        self.assertEqual((parsed.ud, parsed.udl), (original.ud, len(original.ud)))
        self.assertTrue((parsed.udhi, parsed.rd, parsed.srr))
        self.assertEqual((parsed.vpf, parsed.vp), ('relative', b'\x0a'))
        self.assertEqual(parsed.udh.concat_info(), (0x01, 2, 1))
        self.assertEqual(parsed.to_bytes(), encoded)

    def test_build_derives_udl(self):
        submit = sms.SmsSubmit(da='1', ud=b'\x01\x02\x03')
        # flags MR DA-len DA-type DA PID DCS UDL UD
        self.assertEqual(submit.to_bytes()[7], 3)

    def test_other_mti_raises(self):
        with self.assertRaises(sms.SmsError):
            sms.SmsSubmit.from_bytes(sms.build_sms_tpdu(b''))   # SMS-DELIVER
        with self.assertRaises(sms.SmsError):
            sms.SmsSubmit.from_bytes(b'\x01')
        with self.assertRaises(sms.SmsError):
            sms.SmsSubmit.from_bytes('zz')

    def test_bad_vpf_is_refused(self):
        with self.assertRaises(sms.SmsError):
            sms.SmsSubmit(da='1', vpf='nonsense').to_bytes()
        with self.assertRaises(sms.SmsError):
            sms.SmsSubmit(da='1', vpf='relative', vp=b'').to_bytes()


class TestSmsDeliver(unittest.TestCase):
    def test_parse_the_download_tpdu(self):
        tpdu = sms.build_sms_tpdu(bytes.fromhex('00112233'), 3, 2,
                                  scts=bytes.fromhex('42502151710340'))
        deliver = sms.SmsDeliver.from_bytes(tpdu)
        self.assertEqual(deliver.oa, '12345')
        self.assertEqual(deliver.oa_ton, 'unknown')
        self.assertEqual((deliver.pid, deliver.dcs), (0x7F, 0xF6))
        self.assertTrue(deliver.udhi)
        self.assertTrue(deliver.mms)
        self.assertEqual(deliver.udh.concat_info(), (0x01, 3, 2))
        self.assertEqual(deliver.secured_packet, bytes.fromhex('00112233'))
        self.assertEqual(deliver.text, '')
        self.assertEqual(deliver.timestamp,
                         datetime(2024, 5, 12, 15, 17, 30,
                                  tzinfo=timezone(timedelta(hours=1))))

    def test_round_trip(self):
        deliver = sms.SmsDeliver(oa='79161234567', pid=0, dcs=0x04,
                                 scts=bytes.fromhex('42502151710300'),
                                 ud=b'hello', mms=False)
        encoded = deliver.to_bytes()
        self.assertEqual(sms.SmsDeliver.from_bytes(encoded).to_bytes(), encoded)
        self.assertEqual(encoded[0], 0x00)
        self.assertEqual(sms.SmsDeliver.from_bytes(encoded).text, 'hello')


class TestAddress(unittest.TestCase):
    def test_encode_oa_odd_length(self):
        self.assertEqual(sms.encode_oa('12345').hex(), '05812143f5')

    def test_encode_oa_even_length(self):
        self.assertEqual(sms.encode_oa('123456').hex(), '0681214365')

    def test_encode_oa_drops_separators(self):
        self.assertEqual(sms.encode_oa('+7 916 123-45-67').hex(),
                         '0b819761214365f7')
        self.assertEqual(sms.encode_oa('12345'), sms.encode_tp_address('12345'))

    def test_encode_oa_needs_digits(self):
        with self.assertRaises(sms.SmsError):
            sms.encode_oa('')

    def test_type_octet_follows_ton_and_npi(self):
        self.assertEqual(sms.encode_tp_address('12345', 'international',
                                               'isdn_e164')[:2].hex(), '0591')
        self.assertEqual(sms.encode_tp_address('12345', 'national',
                                               'national')[:2].hex(), '05a8')
        with self.assertRaises(sms.SmsError):
            sms.encode_tp_address('12345', 'nonsense')

    def test_decode_tp_address(self):
        self.assertEqual(sms.decode_tp_address(bytes.fromhex('05812143f5')),
                         ('12345', 'unknown', 'isdn_e164', 5))
        self.assertEqual(sms.decode_tp_address(bytes.fromhex('0591214365ff')),
                         ('123456', 'international', 'isdn_e164', 5))
        with self.assertRaises(sms.SmsError):
            sms.decode_tp_address(bytes.fromhex('0581'))

    def test_decode_tp_address_value_without_a_length_octet(self):
        # the COMPREHENSION-TLV Address value form (TS 102 223 8.11)
        self.assertEqual(sms.decode_tp_address_value(bytes.fromhex('812143f5')),
                         ('12345', 'unknown', 'isdn_e164'))
        self.assertEqual(sms.decode_tp_address_value(bytes.fromhex('9121436587')),
                         ('12345678', 'international', 'isdn_e164'))
        with self.assertRaises(sms.SmsError):
            sms.decode_tp_address_value(b'\x81')


class TestTimestamps(unittest.TestCase):
    def test_encode_scts_bytes(self):
        dt = datetime(2024, 5, 12, 15, 17, 30, tzinfo=timezone.utc)
        self.assertEqual(sms.encode_scts(dt).hex(), '42502151710300')

    def test_positive_timezone_sign(self):
        dt = datetime(2024, 5, 12, 15, 17, 30,
                      tzinfo=timezone(timedelta(hours=1)))
        # 1 h = 4 quarter-hours -> the low nibble 4 with the sign bit clear
        self.assertEqual(sms.encode_scts(dt).hex(), '42502151710340')

    def test_round_trip(self):
        for dt in (datetime(2024, 5, 12, 15, 17, 30, tzinfo=timezone.utc),
                   datetime(2023, 1, 2, 3, 4, 5,
                            tzinfo=timezone(-timedelta(hours=4))),
                   datetime(2020, 12, 31, 23, 59, 59,
                            tzinfo=timezone(timedelta(hours=5, minutes=30)))):
            self.assertEqual(sms.decode_scts(sms.encode_scts(dt)), dt)

    def test_a_real_world_timestamp(self):
        # the well-known SMS-DELIVER example 2019-06-15 15:56:08 +01:00
        self.assertEqual(sms.decode_scts(bytes.fromhex('91605151658040')),
                         datetime(2019, 6, 15, 15, 56, 8,
                                  tzinfo=timezone(timedelta(hours=1))))

    def test_invalid_timestamps_raise(self):
        with self.assertRaises(sms.SmsError):
            sms.decode_scts(bytes.fromhex('4250215171'))
        with self.assertRaises(sms.SmsError):
            sms.decode_scts(bytes.fromhex('42135051710300'))   # month 31


class TestBuildSmsTpdu(unittest.TestCase):
    CHUNK = ('00201506091515b00000c08f58c38860acb3a362fffe670ad13759a2a6b4'
             'c1a91116')
    SCTS = bytes.fromhex('24051215173000')

    def test_single_message_with_cpi(self):
        self.assertEqual(
            sms.build_sms_tpdu(self.CHUNK, include_cpi=True,
                               scts=self.SCTS).hex(),
            '4005812143f57ff6240512151730002502700000201506091515b00000'
            'c08f58c38860acb3a362fffe670ad13759a2a6b4c1a91116')

    def test_single_message_without_cpi(self):
        self.assertEqual(
            sms.build_sms_tpdu(self.CHUNK, include_cpi=False,
                               scts=self.SCTS).hex(),
            '0405812143f57ff6240512151730002200201506091515b00000'
            'c08f58c38860acb3a362fffe670ad13759a2a6b4c1a91116')

    def test_first_chunk_has_the_cpi_ie(self):
        self.assertEqual(
            sms.build_sms_tpdu(self.CHUNK, chunk_total=3, chunk_num=1,
                               include_cpi=True, scts=self.SCTS).hex(),
            '4405812143f57ff6240512151730002a070003010301700000201506091515b00000'
            'c08f58c38860acb3a362fffe670ad13759a2a6b4c1a91116')

    def test_later_chunk_carries_only_the_concat_ie(self):
        self.assertEqual(
            sms.build_sms_tpdu(self.CHUNK, chunk_total=3, chunk_num=2,
                               include_cpi=True, scts=self.SCTS).hex(),
            '4405812143f57ff6240512151730002805000301030200201506091515b00000'
            'c08f58c38860acb3a362fffe670ad13759a2a6b4c1a91116')

    def test_user_data_budget(self):
        # a single SM with the CPI IE carries at most 137 octets
        sms.build_sms_tpdu(b'\x00' * 137, scts=self.SCTS)
        with self.assertRaises(sms.SmsError):
            sms.build_sms_tpdu(b'\x00' * 138, scts=self.SCTS)
        # a concatenated first SM: 140 - UDHL - concat(5) - CPI(2) = 132
        sms.build_sms_tpdu(b'\x00' * 132, chunk_total=2, chunk_num=1,
                           scts=self.SCTS)
        with self.assertRaises(sms.SmsError):
            sms.build_sms_tpdu(b'\x00' * 133, chunk_total=2, chunk_num=1,
                               scts=self.SCTS)

    def test_chunk_accepts_bytes(self):
        self.assertEqual(sms.build_sms_tpdu(b'\x00\x01', scts=self.SCTS),
                         sms.build_sms_tpdu('0001', scts=self.SCTS))


class TestFetchParsing(unittest.TestCase):
    def test_find_tpdu_with_ber_long_form_lengths(self):
        tpdu = sms.find_sms_tpdu(bytes.fromhex(FETCH_SEGMENT_1))
        self.assertEqual(len(tpdu), 0x97)          # 8B 81 97 <151 octets>
        self.assertEqual(tpdu.hex(), SUBMIT_SEGMENT_1)
        tpdu2 = sms.find_sms_tpdu(bytes.fromhex(FETCH_SEGMENT_2))
        self.assertEqual(len(tpdu2), 0x78)         # 8B 78 <120 octets>
        self.assertEqual(tpdu2.hex(), SUBMIT_SEGMENT_2)

    def test_plain_tag_variant(self):
        # the 0B tag form of the command-specific data object
        d0 = bytes.fromhex('d00981030113008b020102')
        self.assertEqual(sms.find_sms_tpdu(d0), bytes.fromhex('0102'))

    def test_no_tpdu(self):
        self.assertIsNone(sms.find_sms_tpdu(b'\x00\x00'))
        self.assertIsNone(sms.find_sms_tpdu(bytes.fromhex('d00481030113')))
        self.assertIsNone(sms.find_sms_tpdu(b''))


class TestUdOffset(unittest.TestCase):
    def test_submit_with_relative_validity(self):
        tpdu = bytes.fromhex(SUBMIT_SEGMENT_1)
        self.assertEqual(sms.ud_offset(tpdu), 11)
        self.assertEqual(len(tpdu) - len(sms.SmsSubmit.from_bytes(tpdu).ud), 11)

    def test_submit_with_absolute_validity(self):
        tpdu = bytes.fromhex('5900048111227ff6' + '00' * 7 + '00' + '00' * 8)
        self.assertEqual(sms.ud_offset(tpdu), 16)

    def test_deliver(self):
        tpdu = sms.build_sms_tpdu(b'\x00' * 4,
                                  scts=bytes.fromhex('24051215173000'))
        deliver = sms.SmsDeliver.from_bytes(tpdu)
        self.assertEqual(sms.ud_offset(tpdu), len(tpdu) - len(deliver.ud))
        # flags + OA length + OA type + OA digits + PID + DCS + TP-SCTS + UDL
        self.assertEqual(sms.ud_offset(tpdu), 16)

    def test_malformed(self):
        self.assertIsNone(sms.ud_offset(b''))
        self.assertIsNone(sms.ud_offset(bytes.fromhex('01')))
        self.assertIsNone(sms.ud_offset(bytes.fromhex('0100')))
        self.assertIsNone(sms.ud_offset(bytes.fromhex('05')))    # SMS-COMMAND


class TestParseSmsConcat(unittest.TestCase):
    def test_live_segments(self):
        ref, total, num, payload = sms.parse_sms_concat(
            bytes.fromhex(SUBMIT_SEGMENT_1))
        self.assertEqual((ref, total, num), (0x13, 2, 1))
        self.assertEqual(len(payload), 132)
        self.assertTrue(payload.hex().startswith('00e90a'))
        ref, total, num, payload = sms.parse_sms_concat(
            bytes.fromhex(SUBMIT_SEGMENT_2))
        self.assertEqual((ref, total, num), (0x13, 2, 2))
        self.assertEqual(len(payload), 103)
        # both payloads are the card's listing data, the UDH stripped
        first = sms.parse_sms_concat(bytes.fromhex(SUBMIT_SEGMENT_1))[3]
        self.assertEqual(len(first + payload), 235)
        self.assertIn(b'\xaa\xff\xca\xfe', first + payload)

    def test_no_udh_is_the_payload(self):
        tpdu = bytes.fromhex('0100' '05' '90' '2143F5' '0004' '03' 'AABBCC')
        ref, total, num, payload = sms.parse_sms_concat(tpdu)
        self.assertEqual((ref, total, num), (None, None, None))
        self.assertEqual(payload.hex(), 'aabbcc')

    def test_deliver_with_a_udh(self):
        # the SMS-PP download direction: the TP-UD offset includes the OA type
        tpdu = sms.build_sms_tpdu(bytes.fromhex('aabb'), chunk_total=2,
                                  chunk_num=1,
                                  scts=bytes.fromhex('24051215173000'))
        ref, total, num, payload = sms.parse_sms_concat(tpdu)
        self.assertEqual((ref, total, num), (0x01, 2, 1))
        self.assertEqual(payload, bytes.fromhex('aabb'))

    def test_short_input(self):
        for tpdu in (b'', b'\x01', b'\x44', None):
            ref, total, num, payload = sms.parse_sms_concat(tpdu)
            self.assertIsNone(ref)
            self.assertIsNone(total)
            self.assertIsNone(num)
            self.assertIsInstance(payload, bytes)


class TestCatEnvelope(unittest.TestCase):
    def test_device_identities(self):
        dev = cat.DeviceIdentities('network', 'uicc')
        self.assertEqual(dev.to_tlv().hex(), '82028381')
        self.assertEqual(cat.DeviceIdentities('terminal', 'uicc')
                         .to_tlv().hex(), '82028281')
        self.assertEqual(cat.DeviceIdentities(0x83, 0x81).to_tlv(),
                         dev.to_tlv())
        self.assertEqual(cat.DeviceIdentities.from_bytes(b'\x83\x81')
                         .to_tlv(), dev.to_tlv())
        with self.assertRaises(cat.CatError):
            cat.DeviceIdentities('nonsense', 'uicc')
        with self.assertRaises(cat.CatError):
            cat.DeviceIdentities.from_bytes(b'\x83')

    def test_address_object(self):
        self.assertEqual(cat.Address('12345').to_tlv().hex(), '8604812143f5')
        self.assertEqual(cat.Address('123456').to_tlv().hex(),
                         '860481214365')
        parsed = cat.Address.from_bytes(bytes.fromhex('812143f5'))
        self.assertEqual((parsed.number, parsed.ton, parsed.npi),
                         ('12345', 'unknown', 'isdn_e164'))
        self.assertEqual(parsed.to_tlv(), cat.Address('12345').to_tlv())
        intl = cat.Address('12345', 'international', 'isdn_e164')
        self.assertEqual(intl.to_tlv().hex(), '8604912143f5')
        self.assertEqual(cat.Address.from_bytes(intl.value).ton, 'international')

    def test_sms_pp_download_without_address(self):
        self.assertEqual(
            cat.SmsPpDownload.build(('network', 'uicc'),
                                    bytes.fromhex('0102')).hex(),
            'd108' '82028381' '8b020102')

    def test_sms_pp_download_with_address(self):
        self.assertEqual(
            cat.SmsPpDownload.build(('network', 'uicc'),
                                    bytes.fromhex('0102'),
                                    cat.Address('12345')).hex(),
            'd10e' '82028381' '8604812143f5' '8b020102')

    def test_sms_pp_download_ber_long_form(self):
        tpdu = b'\x00' * 140
        built = cat.SmsPpDownload.build(('network', 'uicc'), tpdu)
        # 4 (device identities) + 2 + 140 = 146 = 0x92, so D1 81 92
        self.assertEqual(built[:3].hex(), 'd18192')
        self.assertEqual(built[7:9].hex(), '8b8c')
        self.assertEqual(len(built), 3 + 146)
        self.assertEqual(built[3:5].hex(), '8202')

    def test_sms_pp_download_class_round_trip(self):
        built = cat.SmsPpDownload.build(('network', 'uicc'),
                                        bytes.fromhex('0102'))
        obj = cat.SmsPpDownload(cat.DeviceIdentities('network', 'uicc'),
                                bytes.fromhex('0102'))
        self.assertEqual(obj.to_tlv(), built)
        self.assertEqual(obj.to_bytes(), built[2:])

    def test_mandatory_children_are_checked(self):
        with self.assertRaises(cat.CatError):
            cat.SmsPpDownload.build(('network', 'uicc'), b'')
        with self.assertRaises(cat.CatError):
            cat.SmsPpDownload(None, bytes.fromhex('0102')).to_tlv()

    def test_envelope_apdu(self):
        self.assertEqual(cat.envelope_apdu(b'', cla=0x80).hex(),
                         '80c2000000')
        self.assertEqual(cat.envelope_apdu(b'\x01\x02', cla='a0').hex(),
                         'a0c20000020102')
        with self.assertRaises(cat.CatError):
            cat.envelope_apdu(b'\x00' * 256)
        with self.assertRaises(cat.CatError):
            cat.envelope_apdu(b'', cla='zz')


if __name__ == '__main__':
    unittest.main()
