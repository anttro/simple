# coding=utf-8
"""Tests for :mod:`simple_server.esim` (Phase 3a: the ES10 TLV layer).

No card is needed: every exchange is driven through a fake transport with
scripted ``(data, sw)`` responses.  The pinned APDU/TLV hex was captured from
pySim's ``pySim.euicc`` classes (``to_tlv()`` output, not an import: this
suite stays pysim-free) and cross-checked against the SGP.22 v2.6 clauses
named in each test.
"""

import unittest

from simple_server import esim
from simple_server.tlv import encode_tag


# A synthetic 20-digit ICCID (even digit count -> no BCD padding) and the
# 16-byte ISD-P AID SGP.22 §5.7.15's own example uses.
ICCID = '89493010000000000011'
ICCID_ODD = '8949301000000000001'
ISDP_AID = 'A0000005591010FFFFFFFF8900000100'


def ber(tag, value):
    """A short-form BER TLV (all test values are below 128 bytes)."""
    return encode_tag(tag) + bytes([len(value)]) + value


def fake_response(data, sw=b'\x90\x00'):
    return data, sw


class FakeTransport(object):
    """A transport stand-in: scripted responses + the recorded APDU list.

    ``responses`` is a list of ``(data, sw)`` tuples; ``sw`` may be bytes or a
    hex string, like :class:`simple_server.transport.PcscLink` returns.
    """

    def __init__(self, responses=()):
        self.responses = list(responses)
        self.sent = []

    def send_apdu(self, apdu):
        self.sent.append(bytes(apdu))
        if not self.responses:
            raise AssertionError('unexpected APDU %s' % bytes(apdu).hex())
        return self.responses.pop(0)


class TestErrors(unittest.TestCase):
    def test_esim_error_shape(self):
        error = esim.EsimError('catBusy', 'busy', sw='9300')
        self.assertEqual(error.code, 'catBusy')
        self.assertEqual(str(error), 'busy')
        self.assertEqual(error.details['sw'], '9300')
        self.assertEqual(esim.EsimError('x').code, 'x')
        self.assertEqual(str(esim.EsimError('x')), 'x')

    def test_result_text(self):
        self.assertEqual(esim.result_text('ok'), 'ok')
        self.assertEqual(esim.result_text('catBusy'),
                         'card is busy with a CAT session')
        # an unknown enumerant decodes to its integer -> undefined error
        self.assertEqual(esim.result_text(0x7E), 'undefined error')
        self.assertEqual(esim.result_text('undefinedError'), 'undefined error')
        # a code without a message falls back to itself
        self.assertEqual(esim.result_text('iccidNotFound'), 'iccidNotFound')
        for name in esim.RESULT_MESSAGES:
            self.assertIsInstance(esim.result_text(name), str)


class TestCodecs(unittest.TestCase):
    def test_bcd(self):
        # low nibble first: '89 49' <-> digits '8949'
        self.assertEqual(
            esim.decode_bcd(bytes.fromhex('98940301000000000011')), ICCID)
        self.assertEqual(esim.encode_bcd(ICCID),
                         bytes.fromhex('98940301000000000011'))
        # an odd digit count is padded with an F nibble (§5.7.15 Iccid)
        self.assertEqual(esim.encode_bcd(ICCID_ODD),
                         bytes.fromhex('989403010000000000f1'))
        self.assertEqual(esim.decode_bcd(
            bytes.fromhex('989403010000000000f1')), ICCID_ODD)
        self.assertEqual(esim.decode_bcd(b''), '')
        with self.assertRaises(ValueError):
            esim.encode_bcd('12g4')
        with self.assertRaises(ValueError):
            esim.swap_nibbles('123')

    def test_plmn(self):
        # TS 24.008 §10.5.1.3: 2-digit MNC pads the third octet's high nibble
        self.assertEqual(esim.decode_plmn(bytes.fromhex('62f220')), '262-02')
        self.assertEqual(esim.encode_plmn('262-02'),
                         bytes.fromhex('62f220'))
        # a 3-digit MNC swaps the last two digits
        self.assertEqual(esim.decode_plmn(bytes.fromhex('62f200')), '262-00')
        self.assertEqual(esim.decode_plmn(bytes.fromhex('62f201')), '262-10')
        self.assertEqual(esim.encode_plmn('262-10'), bytes.fromhex('62f201'))
        with self.assertRaises(ValueError):
            esim.decode_plmn(b'\x62\xf2')
        with self.assertRaises(ValueError):
            esim.encode_plmn('2620')

    def test_der_int(self):
        cases = {0: '00', 1: '01', 127: '7f', 128: '0080', 255: '00ff',
                 256: '0100', 5: '05', -1: 'ff', -128: '80', -129: 'ff7f'}
        for value, expected in cases.items():
            self.assertEqual(esim.encode_der_int(value).hex(), expected, value)
            self.assertEqual(esim.decode_der_int(
                bytes.fromhex(expected)), value, value)
        with self.assertRaises(ValueError):
            esim.decode_der_int(b'')

    def test_bit_string(self):
        names = ['a', 'b', 'c', 'd']
        # unused-bits octet 0x04 + flags: bit 0 set (MSB of the flags octet)
        self.assertEqual(esim.decode_bit_string(bytes.fromhex('0480'), names),
                         ['a'])
        self.assertEqual(esim.decode_bit_string(bytes.fromhex('0450'), names),
                         ['b', 'd'])
        self.assertEqual(esim.decode_bit_string(b'', names), [])
        # more bits than names: the extra bits are ignored
        self.assertEqual(esim.decode_bit_string(bytes.fromhex('00f0'), names),
                         ['a', 'b', 'c', 'd'])

    def test_version(self):
        self.assertEqual(esim.decode_version(bytes.fromhex('020200')),
                         '2.2.0')
        self.assertIsNone(esim.decode_version(b'\x02\x02'))


class TestCommonObjects(unittest.TestCase):
    def test_iccid(self):
        obj = esim.Iccid(decoded=ICCID)
        self.assertEqual(obj.to_tlv(), bytes.fromhex('5A0A'
                                                     '98940301000000000011'))
        decoded = esim.Iccid.from_bytes(obj.to_tlv())
        self.assertEqual(decoded.decoded, ICCID)
        self.assertEqual(decoded.to_tlv(), obj.to_tlv())

    def test_isdp_aid(self):
        obj = esim.IsdpAid(decoded=bytes.fromhex(ISDP_AID))
        self.assertEqual(obj.to_tlv(), bytes.fromhex('4F10' + ISDP_AID))
        decoded = esim.IsdpAid.from_bytes(obj.to_tlv())
        self.assertEqual(decoded.decoded, ISDP_AID.lower())
        # a hex string is accepted as the decoded value too
        self.assertEqual(esim.IsdpAid(decoded=ISDP_AID).to_tlv(), obj.to_tlv())

    def test_tag_list(self):
        self.assertEqual(esim.TagList(decoded=[0x5A]).to_tlv(),
                         bytes.fromhex('5C015A'))
        # multi-octet tags are flattened into their octets
        self.assertEqual(esim.TagList(decoded=[0x5A, 0x9F, 0x70]).to_tlv(),
                         bytes.fromhex('5C03' + '5A9F70'))
        decoded = esim.TagList.from_bytes(bytes.fromhex('5C035A9F70'))
        self.assertEqual(decoded.decoded, [0x5A, 0x9F, 0x70])
        self.assertEqual(decoded.to_tlv(), bytes.fromhex('5C035A9F70'))

    def test_refresh_flag(self):
        self.assertEqual(esim.RefreshFlag(decoded=True).to_tlv(),
                         bytes.fromhex('810101'))
        self.assertEqual(esim.RefreshFlag(decoded=0).to_tlv(),
                         bytes.fromhex('810100'))
        self.assertTrue(esim.RefreshFlag.from_bytes(b'\x81\x01\x01').decoded)
        with self.assertRaises(ValueError):
            esim.RefreshFlag.from_bytes(b'\x81\x00')


class TestStoreData(unittest.TestCase):
    def test_apdu_header(self):
        # SGP.22 v2.6 §5.7.2 Table 47: 80 E2 91 00 Lc <DO> 00
        self.assertEqual(esim.store_data_apdu(b'').hex(),
                         '80e291000000')
        self.assertEqual(esim.store_data_apdu('BF2800').hex(),
                         '80e2910003bf280000')

    def test_single_block_bound(self):
        # 255 data-object octets fit one block, 256 need the 11/91 sequence
        payload = b'\xBF\x43\x81\xFE' + b'\x00' * 251   # 255 octets
        self.assertEqual(len(esim.store_data_apdu(payload)), 5 + 255 + 1)
        with self.assertRaises(esim.EsimError) as ctx:
            esim.store_data_apdu(b'\xBF\x43\x82' + b'\x00' * 253)
        self.assertEqual(ctx.exception.code, 'do_too_long')

    def test_store_data_sends_and_checks_sw(self):
        transport = FakeTransport([(b'\xBF\x31\x03\x80\x01\x00',
                                     b'\x90\x00')])
        data, sw = esim.store_data(transport, b'\xBF\x31\x00')
        self.assertEqual(transport.sent,
                         [bytes.fromhex('80e2910003bf310000')])
        self.assertEqual(data, bytes.fromhex('BF3103800100'))
        self.assertEqual(sw, '9000')

    def test_status_word_mismatch(self):
        transport = FakeTransport([(b'', '6A88')])
        with self.assertRaises(esim.EsimError) as ctx:
            esim.store_data(transport, b'\xBF\x31\x00')
        self.assertEqual(ctx.exception.code, 'unexpected_sw')
        self.assertEqual(ctx.exception.details['sw'], '6a88')

    def test_expectation_pattern_accepts_91xx(self):
        # SGP.22 §5.7.2: a profile switch answers 91XX with a pending command
        transport = FakeTransport([(b'', '910B')])
        _data, sw = esim.store_data(transport, b'\xBF\x31\x00',
                                    exp_sw='9???')
        self.assertEqual(sw, '910b')

    def test_store_data_tlv_without_response_class(self):
        transport = FakeTransport([(b'\xBF\x3C\x00', '9000')])
        self.assertEqual(esim.store_data_tlv(transport, b'\xBF\x3C\x00'),
                         b'\xBF\x3C\x00')

    def test_store_data_tlv_empty_response(self):
        transport = FakeTransport([(b'', '9000')])
        self.assertIsNone(
            esim.store_data_tlv(transport, esim.EuiccConfiguredAddresses(),
                                esim.EuiccConfiguredAddresses))

    def test_store_data_tlv_empty_request(self):
        # a function without command data still sends the APDU (Lc 00)
        transport = FakeTransport([(b'', '9000')])
        esim.store_data_tlv(transport, None, None)
        self.assertEqual(transport.sent,
                         [bytes.fromhex('80e291000000')])


class TestEnableDisable(unittest.TestCase):
    # Captured from pySim's EnableProfileReq(...).to_tlv() and
    # store_data()'s 80E29100%02x%s00 form.
    ENABLE_ICCID_TLV = 'BF3111A00C5A0A98940301000000000011810101'
    ENABLE_ICCID_APDU = (
        '80E2910014BF3111A00C5A0A9894030100000000001181010100')
    ENABLE_ICCID_ODD_TLV = 'BF3111A00C5A0A989403010000000000F1810100'
    DISABLE_AID_TLV = 'BF3217A0124F10A0000005591010FFFFFFFF8900000100810101'
    DISABLE_AID_APDU = (
        '80E291001ABF3217A0124F10A0000005591010FFFFFFFF8900000100810101'
        '00')

    def _enable(self, iccid=ICCID, refresh=1):
        return esim.EnableProfileReq(children=[
            esim.ProfileIdentifier(children=[esim.Iccid(decoded=iccid)]),
            esim.RefreshFlag(decoded=refresh)])

    def test_enable_request_encoding(self):
        request = self._enable()
        self.assertEqual(request.to_tlv().hex().upper(), self.ENABLE_ICCID_TLV)
        self.assertEqual(esim.store_data_apdu(request.to_tlv()).hex().upper(),
                         self.ENABLE_ICCID_APDU)

    def test_odd_iccid_is_padded(self):
        self.assertEqual(self._enable(ICCID_ODD, refresh=0).to_tlv().hex()
                         .upper(), self.ENABLE_ICCID_ODD_TLV)

    def test_disable_request_by_aid(self):
        request = esim.DisableProfileReq(children=[
            esim.ProfileIdentifier(children=[
                esim.IsdpAid(decoded=ISDP_AID)]),
            esim.RefreshFlag(decoded=1)])
        self.assertEqual(request.to_tlv().hex().upper(), self.DISABLE_AID_TLV)
        self.assertEqual(esim.store_data_apdu(request.to_tlv()).hex().upper(),
                         self.DISABLE_AID_APDU)

    def test_enable_request_round_trip(self):
        request = self._enable()
        raw = request.to_tlv()
        decoded = esim.EnableProfileReq.from_bytes(raw)
        self.assertEqual(decoded.to_tlv(), raw)
        ident = decoded.child_by_type(esim.ProfileIdentifier)
        self.assertEqual(ident.child_by_type(esim.Iccid).decoded, ICCID)
        self.assertIsNone(ident.child_by_type(esim.IsdpAid))
        self.assertEqual(decoded.child_by_type(esim.RefreshFlag).decoded, 1)
        self.assertEqual(decoded.to_dict(), {
            'enable_profile_req': [
                {'profile_identifier': [{'iccid': ICCID}]},
                {'refresh_flag': 1},
            ]})

    def test_enable_response(self):
        resp = esim.EnableProfileResp.from_bytes(
            bytes.fromhex('BF3103800100'))
        self.assertEqual(resp.child_by_type(esim.EnableResult).decoded, 'ok')
        self.assertEqual(resp.to_tlv(), bytes.fromhex('BF3103800100'))
        for code, name in ((1, 'iccidOrAidNotFound'),
                           (2, 'profileNotInDisabledState'),
                           (3, 'disallowedByPolicy'),
                           (4, 'wrongProfileReenabling'),
                           (5, 'catBusy'),
                           (127, 'undefinedError')):
            resp = esim.EnableProfileResp.from_bytes(
                bytes.fromhex('BF31038001%02X' % code))
            self.assertEqual(resp.child_by_type(esim.EnableResult).decoded,
                             name)

    def test_disable_response(self):
        resp = esim.DisableProfileResp.from_bytes(
            bytes.fromhex('BF3203800102'))
        self.assertEqual(resp.child_by_type(esim.DisableResult).decoded,
                         'profileNotInEnabledState')
        # an unassigned code decodes to the integer and names no profile state
        resp = esim.DisableProfileResp.from_bytes(
            bytes.fromhex('BF320380017E'))
        self.assertEqual(resp.child_by_type(esim.DisableResult).decoded, 0x7E)
        self.assertEqual(esim.result_text(
            resp.child_by_type(esim.DisableResult).decoded),
            'undefined error')

    def test_unknown_result_object_is_preserved(self):
        raw = bytes.fromhex('BF3106' '800100' '8501FF')
        resp = esim.EnableProfileResp.from_bytes(raw)
        unknown = resp.children_by_type(esim.RawTlv)
        self.assertEqual([child.tag for child in unknown], [0x85])
        self.assertEqual(unknown[0].to_bytes(), b'\xff')
        self.assertEqual(resp.to_tlv(), raw)


class TestProfileInfo(unittest.TestCase):
    # The tag list pySim's ProfileInfo.nested produces (and pySim's `--all`
    # request): BF2D 10 5C 0E 5A 4F 9F 70 90 91 92 93 94 95 99 B6 B7 B8.
    TAG_LIST_TLV = 'BF2D105C0E5A4F9F7090919293949599B6B7B8'

    @staticmethod
    def _profile(**fields):
        """A ProfileInfo (E3) TLV from named data objects."""
        body = b''
        if 'iccid' in fields:
            body += ber(0x5A, bytes.fromhex('98940301000000000011'))
        if 'aid' in fields:
            body += ber(0x4F, bytes.fromhex(ISDP_AID))
        if 'state' in fields:
            body += ber(b'\x9f\x70', bytes([fields['state']]))
        if 'nickname' in fields:
            body += ber(0x90, fields['nickname'].encode('utf-8'))
        if 'name' in fields:
            body += ber(0x92, fields['name'].encode('utf-8'))
        if 'klass' in fields:
            body += ber(0x95, bytes([fields['klass']]))
        if 'owner' in fields:
            body += ber(0xB7, ber(0x80, bytes.fromhex(fields['owner'])))
        if 'extra' in fields:
            body += ber(b'\xbf\x22', fields['extra'])
        return ber(0xE3, body)

    def test_request(self):
        request = esim.ProfileInfoListReq(
            children=[esim.profile_info_tag_list()])
        self.assertEqual(request.to_tlv().hex().upper(), self.TAG_LIST_TLV)
        self.assertEqual(esim.store_data_apdu(request.to_tlv()).hex().upper(),
                         '80E2910013' + self.TAG_LIST_TLV + '00')

    def test_tag_list_matches_the_profile_info_children(self):
        flattened = []
        for cls in esim.ProfileInfo.nested:
            if cls.tag <= 0xFF:
                flattened.append(cls.tag)
            else:
                flattened.append(cls.tag >> 8)
                flattened.append(cls.tag & 0xFF)
        self.assertEqual(
            list(esim.profile_info_tag_list().to_bytes()), flattened)

    def _response(self, *profiles, error=None):
        if error is not None:
            body = ber(0x81, bytes([error]))
        else:
            body = ber(0xA0, b''.join(profiles))
        return ber(0xBF2D, body)

    def test_response_round_trip(self):
        raw = self._response(self._profile(iccid=True, aid=True, state=1,
                                           nickname='Test', name='Name',
                                           klass=2, owner='62f220'))
        resp = esim.ProfileInfoListResp.from_bytes(raw)
        self.assertEqual(resp.to_tlv(), raw)
        seq = resp.child_by_type(esim.ProfileInfoSeq)
        self.assertIsNotNone(seq)
        profiles = seq.children_by_type(esim.ProfileInfo)
        self.assertEqual(len(profiles), 1)
        profile = profiles[0]
        self.assertEqual(profile.child_by_type(esim.Iccid).decoded, ICCID)
        self.assertEqual(profile.child_by_type(esim.IsdpAid).decoded,
                         ISDP_AID.lower())
        self.assertEqual(profile.child_by_type(esim.ProfileState).decoded,
                         'enabled')
        self.assertEqual(profile.child_by_type(esim.ProfileNickname).decoded,
                         'Test')
        self.assertEqual(profile.child_by_type(esim.ProfileName).decoded,
                         'Name')
        self.assertEqual(profile.child_by_type(esim.ProfileClass).decoded,
                         'operational')
        owner = profile.child_by_type(esim.ProfileOwner)
        self.assertEqual(owner.child_by_type(esim.ProfileOwnerPLMN).decoded,
                         '262-02')

    def test_response_with_several_profiles_and_unknown_object(self):
        raw = self._response(
            self._profile(iccid=True),
            self._profile(iccid=True, extra=b'\x0a\x01\x02'))
        resp = esim.ProfileInfoListResp.from_bytes(raw)
        self.assertEqual(resp.to_tlv(), raw)
        profiles = resp.child_by_type(
            esim.ProfileInfoSeq).children_by_type(esim.ProfileInfo)
        self.assertEqual(len(profiles), 2)
        # SGP.22 §5.7.15 also allows BF22 (service specific data), which pySim
        # does not model: it must survive as a raw child.
        unknown = profiles[1].children_by_type(esim.RawTlv)
        self.assertEqual([obj.tag for obj in unknown], [0xBF22])

    def test_response_error(self):
        for code, text in ((1, 'incorrectInputValues'),
                           (127, 'undefinedError')):
            resp = esim.ProfileInfoListResp.from_bytes(
                self._response(error=code))
            result = resp.child_by_type(esim.ProfileInfoListError)
            self.assertEqual(result.decoded, text)
            self.assertEqual(resp.to_tlv(), self._response(error=code))

    def test_empty_profile_list(self):
        raw = self._response()
        resp = esim.ProfileInfoListResp.from_bytes(raw)
        seq = resp.child_by_type(esim.ProfileInfoSeq)
        self.assertEqual(seq.children, [])
        self.assertEqual(resp.to_tlv(), raw)


class TestNotifications(unittest.TestCase):
    def test_request(self):
        self.assertEqual(esim.ListNotificationReq().to_tlv().hex(),
                         'bf2800')
        self.assertEqual(
            esim.store_data_apdu(
                esim.ListNotificationReq().to_tlv()).hex(),
            '80e2910003bf280000')

    def test_notification_metadata_decode(self):
        metadata = (ber(0x80, b'\x05')
                    + ber(0x81, b'\x00\x80')
                    + ber(0x0C, b'notify.example')
                    + ber(0x5A, bytes.fromhex('1234')))
        raw = ber(0xBF28, ber(0xA0, ber(b'\xbf\x2f', metadata)))
        resp = esim.ListNotificationResp.from_bytes(raw)
        self.assertEqual(resp.to_tlv(), raw)
        lst = resp.child_by_type(esim.NotificationMetadataList)
        entry = lst.children_by_type(esim.NotificationMetadata)[0]
        self.assertEqual(entry.child_by_type(esim.SeqNumber).decoded, 5)
        self.assertEqual(entry.child_by_type(esim.NotificationAddress).decoded,
                         'notify.example')
        self.assertEqual(entry.child_by_type(esim.Iccid).decoded, '2143')
        operation = entry.child_by_type(esim.ProfileMgmtOperation)
        self.assertEqual(operation.decoded,
                         {'install': True, 'enable': False,
                          'disable': False, 'delete': False})
        self.assertEqual(esim.profile_operations(operation.decoded),
                         ['install'])
        # and pySim's nested {'pmo': {...}} shape is accepted too
        self.assertEqual(
            esim.profile_operations({'pmo': {'enable': True}}), ['enable'])
        self.assertEqual(esim.profile_operations(None), [])

    def test_operation_flags_encode(self):
        self.assertEqual(
            esim.ProfileMgmtOperation(
                decoded={'disable': True}).to_tlv().hex(), '81020020')
        self.assertEqual(
            esim.ProfileMgmtOperation(
                decoded={'install': True, 'delete': True}).to_tlv().hex(),
            '81020090')
        decoded = esim.ProfileMgmtOperation.from_bytes(
            bytes.fromhex('81020040'))
        self.assertEqual(esim.profile_operations(decoded.decoded),
                         ['enable'])

    def test_response_error(self):
        resp = esim.ListNotificationResp.from_bytes(
            bytes.fromhex('BF280381017F'))
        self.assertEqual(
            resp.child_by_type(esim.ListNotificationsResultError).decoded,
            'undefinedError')

    def test_empty_metadata_list(self):
        raw = bytes.fromhex('BF2802A000')
        resp = esim.ListNotificationResp.from_bytes(raw)
        self.assertEqual(
            resp.child_by_type(esim.NotificationMetadataList).children, [])
        self.assertEqual(resp.to_tlv(), raw)


class TestConfiguredAddresses(unittest.TestCase):
    def test_request(self):
        self.assertEqual(esim.EuiccConfiguredAddresses().to_tlv().hex(),
                         'bf3c00')
        self.assertEqual(
            esim.store_data_apdu(
                esim.EuiccConfiguredAddresses().to_tlv()).hex(),
            '80e2910003bf3c0000')

    def test_response(self):
        default = b'smdp.example.com'
        root = b'root.example.com'
        body = ber(0x80, default) + ber(0x81, root)
        raw = ber(0xBF3C, body)
        resp = esim.EuiccConfiguredAddresses.from_bytes(raw)
        self.assertEqual(resp.to_tlv(), raw)
        self.assertEqual(
            resp.child_by_type(esim.DefaultDpAddress).decoded,
            'smdp.example.com')
        self.assertEqual(resp.child_by_type(esim.RootDsAddress).decoded,
                         'root.example.com')
        decoded = esim.decode_addresses(raw)
        self.assertEqual(decoded['default_dp_address'], 'smdp.example.com')
        self.assertEqual(decoded['root_ds_address'], 'root.example.com')
        self.assertNotIn('raw_tlvs', decoded)

    def test_response_without_default_address(self):
        raw = ber(0xBF3C, ber(0x81, b'root.example.com'))
        resp = esim.EuiccConfiguredAddresses.from_bytes(raw)
        self.assertIsNone(resp.child_by_type(esim.DefaultDpAddress))
        self.assertEqual(esim.decode_addresses(raw)['default_dp_address'],
                         None)


class TestGetEid(unittest.TestCase):
    EID = '89049032000000000000000000000000'

    def test_request_encoding(self):
        # SGP.22 v2.6 §5.7.20: BF3E 03 5C 01 5A
        request = esim.GetEuiccData(
            children=[esim.TagList(decoded=[0x5A])])
        self.assertEqual(request.to_tlv().hex(), 'bf3e035c015a')
        self.assertEqual(esim.store_data_apdu(request.to_tlv()).hex(),
                         '80e2910006bf3e035c015a00')

    def test_get_eid(self):
        response = bytes.fromhex('BF3E125A10' + self.EID)
        transport = FakeTransport([(response, b'\x90\x00')])
        self.assertEqual(esim.get_eid(transport), self.EID.lower())
        self.assertEqual(transport.sent,
                         [bytes.fromhex('80e2910006bf3e035c015a00')])

    def test_get_eid_ignores_command_response_objects(self):
        # a card that also answers the TagList object: 5C then 5A
        response = bytes.fromhex('BF3E0A5C015A5A051122334455')
        transport = FakeTransport([(response, '9000')])
        self.assertEqual(esim.get_eid(transport), '1122334455')

    def test_missing_eid_is_an_error(self):
        transport = FakeTransport([(bytes.fromhex('BF3E035C015A'), '9000')])
        with self.assertRaises(esim.EsimError) as ctx:
            esim.get_eid(transport)
        self.assertEqual(ctx.exception.code, 'eid_missing')

    def test_empty_response_is_an_error(self):
        transport = FakeTransport([(b'', '9000')])
        with self.assertRaises(esim.EsimError) as ctx:
            esim.get_eid(transport)
        self.assertEqual(ctx.exception.code, 'empty_response')

    def test_truncated_response_is_an_esim_error(self):
        # BF3E declares 3 value octets but only 1 follows
        transport = FakeTransport([(bytes.fromhex('BF3E03A0'), '9000')])
        with self.assertRaises(esim.EsimError) as ctx:
            esim.get_eid(transport)
        self.assertEqual(ctx.exception.code, 'decode_error')

    def test_trailing_ff_padding_is_tolerated(self):
        response = bytes.fromhex('BF3E125A10' + self.EID) + b'\xff\xff'
        transport = FakeTransport([(response, '9000')])
        self.assertEqual(esim.get_eid(transport), self.EID.lower())

    def test_non_padding_trailer_is_an_error(self):
        response = bytes.fromhex('BF3E125A10' + self.EID) + b'\x00'
        transport = FakeTransport([(response, '9000')])
        with self.assertRaises(esim.EsimError) as ctx:
            esim.get_eid(transport)
        self.assertEqual(ctx.exception.code, 'decode_error')

    def test_wrong_response_object_is_an_error(self):
        # a BF3C answer to GetEuiccData is not a GetEuiccData response
        transport = FakeTransport([(bytes.fromhex('BF3C00'), '9000')])
        with self.assertRaises(esim.EsimError) as ctx:
            esim.get_eid(transport)
        self.assertEqual(ctx.exception.code, 'decode_error')

    def test_wrong_status_word_is_an_error(self):
        transport = FakeTransport([(b'', '6A88')])
        with self.assertRaises(esim.EsimError) as ctx:
            esim.get_eid(transport)
        self.assertEqual(ctx.exception.code, 'unexpected_sw')


class TestInfoDecoders(unittest.TestCase):
    def test_info1(self):
        raw = ber(0xBF20, ber(0x82, b'\x02\x02\x00')
                  + ber(0xA9, ber(0x04, b'\x11\x22'))
                  + ber(0xAA, ber(0x04, b'\x33\x44'))
                  + ber(0x85, b'\x00'))
        decoded = esim.decode_info1(raw)
        self.assertEqual(decoded['svn'], '2.2.0')
        self.assertEqual(decoded['euicc_ci_pki_list_for_verification'],
                         ['1122'])
        self.assertEqual(decoded['euicc_ci_pki_list_for_signing'], ['3344'])
        self.assertEqual(decoded['raw_tlvs'], {'85': '00'})
        # the TLV class parses the same object
        info = esim.EuiccInfo1.from_bytes(raw)
        self.assertEqual(info.to_tlv(), raw)
        self.assertEqual(info.child_by_type(esim.SVN).decoded, '2.2.0')
        self.assertEqual(
            len(info.child_by_type(
                esim.EuiccCiPkiListForVerification).children), 1)

    def test_info1_request(self):
        self.assertEqual(esim.EuiccInfo1().to_tlv().hex(), 'bf2000')

    def test_info2(self):
        # contactlessSupport + akaTuak128 (bit 0 and bit 6, one unused bit)
        uicc_caps = bytes([1, 0x80 | 0x02])
        rsp_caps = bytes([6, 0x40])              # crlSupport
        body = (ber(0x81, b'\x02\x02\x00')       # profileVersion
                + ber(0x82, b'\x02\x02\x00')     # svn
                + ber(0x83, b'\x03\x00\x00')     # euiccFirmwareVer
                + ber(0x84, ber(0x81, b'\x00\x05')
                      + ber(0x82, b'\x00\x64')
                      + ber(0x83, b'\x00\x0A'))  # extCardResource
                + ber(0x85, uicc_caps)
                + ber(0x88, rsp_caps)
                + ber(0x8B, b'\x02')             # euiccCategory
                + ber(0x04, b'\x02\x02\x00')     # ppVersion
                + ber(0x0C, b'TEST')             # sasAcreditationNumber
                + ber(0xAC, ber(0x80, b'PLATFORM'))
                + ber(0xAD, bytes([5, 0x80]))    # treProperties
                + ber(0xAE, b'REF')
                + ber(0xAF, ber(0x82, b'\x02\x02\x00'))
                + ber(0x99, bytes([4, 0x80])))  # unmodelled by the class
        raw = ber(0xBF22, body)
        decoded = esim.decode_info2(raw)
        self.assertEqual(decoded['profile_version'], '2.2.0')
        self.assertEqual(decoded['svn'], '2.2.0')
        self.assertEqual(decoded['euicc_firmware_ver'], '3.0.0')
        self.assertEqual(decoded['ext_card_resource'], {
            'installed_application': 5,
            'free_non_volatile_memory': 100,
            'free_volatile_memory': 10})
        self.assertEqual(decoded['uicc_capability'],
                         ['contactlessSupport', 'akaTuak128'])
        self.assertEqual(decoded['rsp_capability'], ['crlSupport'])
        self.assertEqual(decoded['euicc_category'], 'mediumEuicc')
        self.assertEqual(decoded['pp_version'], '2.2.0')
        self.assertEqual(decoded['ss_acreditation_number'], 'TEST')
        self.assertEqual(decoded['certification_data_object'],
                         {'platform_label': 'PLATFORM'})
        self.assertEqual(decoded['tre_properties'], ['isDiscrete'])
        self.assertEqual(decoded['tre_product_reference'], 'REF')
        self.assertEqual(
            decoded['additional_euicc_profile_package_versions'], ['2.2.0'])
        self.assertEqual(decoded['forbidden_profile_policy_rules'],
                         ['pprUpdateControl'])

        # the TLV class keeps the capability objects raw and preserves the
        # unmodelled object
        info = esim.EuiccInfo2.from_bytes(raw)
        self.assertEqual(info.to_tlv(), raw)
        self.assertEqual(info.child_by_type(esim.UiccCapability).decoded,
                         uicc_caps)
        self.assertEqual(info.child_by_type(esim.EuiccCategory).decoded,
                         'mediumEuicc')
        # pySim's EuiccInfo2 list stops at the SGP.32 iotSpecificInfo, so the
        # certification/TRE/additional-version/PPR objects stay raw - that is
        # the documented "decode raw" behaviour, and re-encoding is exact.
        self.assertEqual([obj.tag for obj in info.children_by_type(
            esim.RawTlv)], [0xAC, 0xAD, 0xAE, 0xAF, 0x99])

    def test_info2_explicit_category_tag(self):
        raw = ber(0xBF22, ber(0xAB, b'\x01'))
        self.assertEqual(esim.decode_info2(raw)['euicc_category'],
                         'basicEuicc')
        info = esim.EuiccInfo2.from_bytes(raw)
        self.assertEqual(info.child_by_type(esim.EuiccCategory).decoded,
                         'basicEuicc')
        # the original tag form is kept when re-encoding a parsed object
        self.assertEqual(info.to_tlv(), raw)

    def test_info2_malformed_bits_are_tolerated(self):
        # a capability object with no content: decode_bit_string returns []
        raw = ber(0xBF22, ber(0x85, b''))
        self.assertEqual(esim.decode_info2(raw)['uicc_capability'], [])

    def test_rat(self):
        operator = ber(0x80, bytes.fromhex('62f220'))
        rule = (ber(0x80, bytes([4, 0x80]))
                + ber(0xA1, ber(0xA0, operator))
                + ber(0x82, bytes([7, 0x80])))
        raw = ber(0xBF43, ber(0xA0, ber(0xA0, rule)))
        self.assertEqual(esim.decode_rat(raw), [{
            'ppr_ids': ['pprUpdateControl'],
            'allowed_operators': [{'plmn': '62f220', 'gid1': None,
                                   'gid2': None}],
            'ppr_flags': ['consentRequired']}])
        # our GetRat request has no input data (SGP.22 v2.6 §5.7.22)
        self.assertEqual(esim._GetRatRequest().to_tlv().hex(), 'bf4300')


class TestRawTlvPreservation(unittest.TestCase):
    def test_unknown_child(self):
        raw = bytes.fromhex('BF3C03' '850102')
        obj = esim.EuiccConfiguredAddresses.from_bytes(raw)
        unknown = obj.children_by_type(esim.RawTlv)
        self.assertEqual([child.tag for child in unknown], [0x85])
        self.assertEqual(obj.to_tlv(), raw)
        self.assertEqual(unknown[0].to_dict(), {'unknown_85': '02'})


if __name__ == '__main__':
    unittest.main()
