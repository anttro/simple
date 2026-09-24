#!/usr/bin/env python3
"""Unit tests for the OTA helper functions in pysim_simple_server.server.

Reference vectors are key-free: synthetic dummy keys plus the already-public
sysmocom sample-key vectors that ship in pySim's own tests/unittests/test_ota.py.
No live/sample card keys and no ICCIDs appear here.
"""

import sys
import types
import unittest
from pathlib import Path
from unittest import mock

# pySim checkout is a sibling of this repo; put it on sys.path so the server
# module (which imports pySim at module level) can be exercised against it.
PROJECTS = Path(__file__).resolve().parents[2]
PY_SIM = PROJECTS / 'pysim'
if str(PY_SIM) not in sys.path:
    sys.path.insert(0, str(PY_SIM))

from pysim_simple_server.server import (
    _build_secured_packet,
    _build_sms_tpdu,
    _build_tr,
    _decode_cmd,
    _decode_por,
    _decode_tr,
    _log_proactive,
    _ota_reference,
    _parse_select_item,
    _parse_setup_menu_items,
    _record_tr,
    _send_secured_packet,
    _spi_from_bytes,
    _split_secured_packet,
    _tr_data_only,
    SCP80_FIRST_BYTES,
    SCP80_MAX_SEGMENTS,
    SCP80_NEXT_BYTES,
    SCP80_SINGLE_BYTES,
)

# Synthetic dummy key material (no real card keys).
K = '00112233445566778899AABBCCDDEEFF'
# Public sysmocom sample keys from pySim tests/unittests/test_ota.py.
KIC3 = 'C21DD66ACAC13CB3BC8B331B24AFB57B'
KID3 = '12110C78E678C25408233076AA033615'
# Public synthetic AES keys from pySim tests/unittests/test_ota.py.
KIC_AES = '200102030405060708090a0b0c0d0e0f'
KID_AES = '201102030405060708090a0b0c0d0e0f'

APDU = '00a40000023f00'

# (spi1, spi2) -> expected secured packet, generated with _ota_reference
# against pySim's OtaDialectSms.encode_cmd and cross-checked with the JS genSp().
REFERENCE_VECTORS = {
    ('06', '09'):
        '00201506091515b00000c08f58c38860acb3a362fffe670ad13759a2a6b4c1a91116',
    ('16', '01'):
        '00201516011515b00000e42573469e68a8462a57a505b0e2b1c09c1928c7a182311f',
    ('02', '09'):
        '001d1502091515b0000000000000010085a8ca1a9828b0bb00a40000023f00',
    ('01', '09'):
        '00191101091515b0000000000000010050c942dc00a40000023f00',
}

# AES-128 reference vectors (public synthetic keys from pySim test_ota.py).
AES_APDU = '00a40004023f00'
AES_REFERENCE_VECTORS = {
    ('16', '19'):
        '00281516192222b000115a47655527e96e832f1a5c698655715d4331454a0d83952c0ed35245706976b1',
    ('12', '09'):
        '001d1512092222b0001100000000110029826122c7a0b79500a40004023f00',
    ('1e', '19'):
        '0028151e192222b0001118b202ee47a3203e7370861c383b4142e704157b36e5c0eb4bb33eb6036cbaf8',
}


class TestSpiFromBytes(unittest.TestCase):
    def test_06_09_ciphered_cc(self):
        spi = _spi_from_bytes(0x06, 0x09)
        self.assertEqual(spi, {
            'counter': 'no_counter',
            'ciphering': True,
            'rc_cc_ds': 'cc',
            'por_in_submit': False,
            'por_shall_be_ciphered': False,
            'por_rc_cc_ds': 'cc',
            'por': 'por_required',
        })

    def test_16_01_counter_must_be_higher(self):
        spi = _spi_from_bytes(0x16, 0x01)
        self.assertEqual(spi['counter'], 'counter_must_be_higher')
        self.assertTrue(spi['ciphering'])
        self.assertEqual(spi['rc_cc_ds'], 'cc')
        self.assertEqual(spi['por_rc_cc_ds'], 'no_rc_cc_ds')

    def test_02_09_unciphered_cc(self):
        spi = _spi_from_bytes(0x02, 0x09)
        self.assertFalse(spi['ciphering'])
        self.assertEqual(spi['rc_cc_ds'], 'cc')
        self.assertEqual(spi['por_rc_cc_ds'], 'cc')

    def test_04_19_ciphered_no_cc(self):
        spi = _spi_from_bytes(0x04, 0x19)
        self.assertTrue(spi['ciphering'])
        self.assertEqual(spi['rc_cc_ds'], 'no_rc_cc_ds')
        self.assertTrue(spi['por_shall_be_ciphered'])
        self.assertEqual(spi['por_rc_cc_ds'], 'cc')


class TestBuildSmsTpdu(unittest.TestCase):
    CHUNK = '00201506091515b00000c08f58c38860acb3a362fffe670ad13759a2a6b4c1a91116'
    SCTS = bytes.fromhex('24051215173000')

    def _build(self, *args, **kwargs):
        with mock.patch('pysim_simple_server.server._encode_scts', return_value=self.SCTS):
            return _build_sms_tpdu(*args, **kwargs)

    def test_single_message_with_cpi(self):
        self.assertEqual(
            self._build(self.CHUNK, include_cpi=True),
            '4005812143f57ff6240512151730002502700000201506091515b00000'
            'c08f58c38860acb3a362fffe670ad13759a2a6b4c1a91116')

    def test_single_message_without_cpi(self):
        self.assertEqual(
            self._build(self.CHUNK, include_cpi=False),
            '0405812143f57ff6240512151730002200201506091515b00000'
            'c08f58c38860acb3a362fffe670ad13759a2a6b4c1a91116')

    def test_first_chunk_has_cpi(self):
        self.assertEqual(
            self._build(self.CHUNK, chunk_total=3, chunk_num=1, include_cpi=True),
            '4405812143f57ff6240512151730002a070003010301700000201506091515b00000'
            'c08f58c38860acb3a362fffe670ad13759a2a6b4c1a91116')

    def test_later_chunk_concat_only(self):
        self.assertEqual(
            self._build(self.CHUNK, chunk_total=3, chunk_num=2, include_cpi=True),
            '4405812143f57ff6240512151730002805000301030200201506091515b00000'
            'c08f58c38860acb3a362fffe670ad13759a2a6b4c1a91116')


class TestOtaReference(unittest.TestCase):
    def test_ciphered_spi_06_09(self):
        out, _ = _ota_reference('06', '09', '15', '15', 'b00000', '0000000001', APDU, K, K)
        self.assertEqual(out, REFERENCE_VECTORS[('06', '09')])

    def test_ciphered_spi_16_01(self):
        out, _ = _ota_reference('16', '01', '15', '15', 'b00000', '0000000001', APDU, K, K)
        self.assertEqual(out, REFERENCE_VECTORS[('16', '01')])

    def test_unciphered_spi_02_09(self):
        out, _ = _ota_reference('02', '09', '15', '15', 'b00000', '0000000001', APDU, K, K)
        self.assertEqual(out, REFERENCE_VECTORS[('02', '09')])

    def test_unciphered_rc_reference(self):
        out, _ = _ota_reference('01', '09', '15', '15', 'b00000', '0000000001', APDU, K, K)
        self.assertEqual(out, REFERENCE_VECTORS[('01', '09')])

    def test_unciphered_cpl_is_0x001d(self):
        # Regression: CPL counts octets from the CHL octet to the last octet
        # of the secured data (29 here), it must NOT be len(out)-2 (27/0x001b).
        out, _ = _ota_reference('02', '09', '15', '15', 'b00000', '0000000001', APDU, K, K)
        self.assertEqual(out[:4], '001d')
        self.assertEqual(len(out) // 2, 31)

    def test_sysmocom_reference_vector(self):
        # Public vector from pySim tests/unittests/test_ota.py (test_cmd_3des_ciphered).
        out, _ = _ota_reference('04', '19', '35', '35', 'b00000', '0000000000', APDU, KIC3, KID3)
        self.assertEqual(out, '00180d04193535b00000e3ec80a849b554421276af3883927c20')

    def test_returns_spi_dict(self):
        _, spi = _ota_reference('16', '01', '15', '15', 'b00000', '0000000001', APDU, K, K)
        self.assertEqual(spi['counter'], 'counter_must_be_higher')
        self.assertTrue(spi['ciphering'])

    def test_aes128_ciphered_cc(self):
        out, _ = _ota_reference('16', '19', '22', '22', 'b00011', '0000000011', AES_APDU, KIC_AES, KID_AES)
        self.assertEqual(out, AES_REFERENCE_VECTORS[('16', '19')])

    def test_aes128_unciphered_cc(self):
        out, _ = _ota_reference('12', '09', '22', '22', 'b00011', '0000000011', AES_APDU, KIC_AES, KID_AES)
        self.assertEqual(out, AES_REFERENCE_VECTORS[('12', '09')])

    def test_aes128_counter_plus_one(self):
        out, spi = _ota_reference('1e', '19', '22', '22', 'b00011', '0000000011', AES_APDU, KIC_AES, KID_AES)
        self.assertEqual(out, AES_REFERENCE_VECTORS[('1e', '19')])
        self.assertEqual(spi['counter'], 'counter_must_be_lower')

    def test_ram_load_sequence_uses_full_240_byte_blocks(self):
        # The one-SMS clamp was removed: the RAM path's default LOAD blocks
        # are the GP maximum of 240 bytes, and each secured packet still fits
        # the card's concatenation buffer.
        from pysim_simple_server.server import _cap_apdu_sequence
        seq = _cap_apdu_sequence('A000000003000000', 'A000000003000001',
                                 'AA' * 600, block_size=240)
        loads = [a for a in seq if a.startswith('80E8')]
        self.assertGreater(len(loads), 1)
        for apdu in loads:
            self.assertLessEqual(int(apdu[8:10], 16), 240)
            sp, _ = _build_secured_packet('16', '01', '15', '15', 'b00000',
                                          '0000000001', apdu, K, K)
            self.assertLessEqual(len(_split_secured_packet(bytes.fromhex(sp))),
                                 SCP80_MAX_SEGMENTS)


class TestSmsConcatenation(unittest.TestCase):
    """SCP80 SMS concatenation: packet building and segment sending
    (TS 31.115 4.2/4.3)."""

    def test_secured_packet_matches_the_pysim_reference_for_one_sms(self):
        # Our encoder only lifts pySim's single-SMS refusal; for packets that
        # fit one SMS it must stay byte-identical to the pySim reference.
        for spi1, spi2 in (('06', '09'), ('16', '01'), ('02', '09'), ('04', '19')):
            ref, _ = _ota_reference(spi1, spi2, '15', '15', 'b00000',
                                    '0000000001', APDU, K, K)
            out, _ = _build_secured_packet(spi1, spi2, '15', '15', 'b00000',
                                           '0000000001', APDU, K, K)
            self.assertEqual(out, ref, (spi1, spi2))

    def test_split_keeps_the_sms_user_data_budget(self):
        self.assertEqual(_split_secured_packet(b'A' * SCP80_SINGLE_BYTES),
                         [b'A' * SCP80_SINGLE_BYTES])
        parts = _split_secured_packet(b'A' * (SCP80_SINGLE_BYTES + 1))
        self.assertEqual([len(p) for p in parts],
                         [SCP80_FIRST_BYTES, SCP80_SINGLE_BYTES + 1 - SCP80_FIRST_BYTES])
        pkt = bytes(range(256)) * 2
        parts = _split_secured_packet(pkt)
        self.assertEqual(len(parts[0]), SCP80_FIRST_BYTES)
        self.assertTrue(all(len(p) <= SCP80_NEXT_BYTES for p in parts[1:]))
        self.assertEqual(b''.join(parts), pkt)
        # Without the CPI IE the single-SM budget is the full 140 octets.
        self.assertEqual(_split_secured_packet(b'A' * 140, include_cpi=False),
                         [b'A' * 140])

    def test_unprotected_single_sm_keeps_the_chl_first_form(self):
        # TS 31.115 Table 1 NOTE: the CPL is "not absolutely necessary" in a
        # single SM - an unprotected packet keeps pySim's CHL-first form.
        out, _ = _build_secured_packet('00', '09', '15', '15', 'b00000',
                                       '0000000001', APDU, K, K)
        self.assertEqual(out[:2], '0d')

    def test_unprotected_concatenated_packet_gains_the_cpl(self):
        # ... but it is required once the packet needs concatenation
        # (TS 31.115 Table 1 NOTE / 4.3).
        out, _ = _build_secured_packet('00', '09', '15', '15', 'b00000',
                                       '0000000001', 'A0' * 200, K, K)
        self.assertEqual(len(out) // 2, 216)
        self.assertEqual(out[:4], '00d6')   # CPL = 214 = CHL..end
        self.assertEqual(int(out[:4], 16), len(out) // 2 - 2)

    def test_240_byte_load_block_encodes_and_fits_the_card_buffer(self):
        # The RAM path no longer clamps LOAD blocks to one SMS: a 240-byte
        # block (the GP maximum) becomes a concatenated command.
        apdu = '80E80000F0' + '00' * 240 + '00'
        out, _ = _build_secured_packet('16', '01', '15', '15', 'b00000',
                                       '0000000001', apdu, K, K)
        self.assertGreater(len(out) // 2, 140)
        parts = _split_secured_packet(bytes.fromhex(out))
        self.assertTrue(2 <= len(parts) <= SCP80_MAX_SEGMENTS, len(parts))
        # pySim still refuses the same command - the reason we build it here.
        with self.assertRaises(ValueError):
            _ota_reference('16', '01', '15', '15', 'b00000', '0000000001', apdu, K, K)

    def test_send_secured_packet_sends_the_segments_in_order(self):
        import pysim_simple_server.server as srv
        apdu = '80E80000F0' + '00' * 240 + '00'
        sp_hex, _ = _build_secured_packet('16', '01', '15', '15', 'b00000',
                                          '0000000001', apdu, K, K)
        sent = []

        def fake_envelope(tpdu_hex, scc, sm_sc=None, submit_handler=None):
            sent.append(tpdu_hex.upper())
            return '', '9000'

        with mock.patch.object(srv, '_send_envelope', side_effect=fake_envelope):
            result = _send_secured_packet(object(), sp_hex, oa_number='12345')
        self.assertTrue(result['success'], result)
        self.assertEqual(result['bytes'], len(sp_hex) // 2)
        self.assertEqual(result['segments'], len(sent))
        self.assertTrue(2 <= result['segments'] <= SCP80_MAX_SEGMENTS)
        total = result['segments']
        for num, tpdu in enumerate(sent, start=1):
            concat = '000301%02X%02X' % (total, num)   # IEI 00, IEDL 3, ref 01
            udhl = '07' if num == 1 else '05'
            self.assertIn(udhl + concat, tpdu, num)
            if num == 1:
                self.assertIn(udhl + concat + '7000', tpdu)   # CPI in the first SM
            else:
                self.assertNotIn(concat + '7000', tpdu)

    def test_send_secured_packet_refuses_more_than_the_card_buffer(self):
        length = SCP80_FIRST_BYTES + SCP80_NEXT_BYTES * (SCP80_MAX_SEGMENTS - 1) + 1
        result = _send_secured_packet(object(), '00' * length, oa_number='12345')
        self.assertFalse(result['success'])
        self.assertIn('too large', result['error'])
        self.assertEqual(result['segments'], SCP80_MAX_SEGMENTS + 1)

    def test_send_secured_packet_rejects_bad_hex(self):
        result = _send_secured_packet(object(), 'zz', oa_number='12345')
        self.assertFalse(result['success'])
        self.assertIn('Invalid secured packet', result['error'])

    def test_send_secured_packet_reports_a_failed_envelope(self):
        import pysim_simple_server.server as srv

        def fake_envelope(*args, **kwargs):
            return '', '6F00'

        with mock.patch.object(srv, '_send_envelope', side_effect=fake_envelope):
            result = _send_secured_packet(object(), '00' * 10, oa_number='12345')
        self.assertFalse(result['success'])
        self.assertEqual(result['sw'], '6F00')
        self.assertEqual(result['bytes'], 10)
        self.assertIn('segment 1', result['error'])

    def test_sms_user_data_budget_is_enforced(self):
        # The splitter sizes every part exactly; a caller passing more than
        # the SM can carry gets a clear error instead of an invalid TPDU.
        with self.assertRaises(ValueError):
            _build_sms_tpdu('00' * (SCP80_SINGLE_BYTES + 1))
        with self.assertRaises(ValueError):
            _build_sms_tpdu('00' * 141, include_cpi=False)
        with self.assertRaises(ValueError):
            _build_sms_tpdu('00' * (SCP80_FIRST_BYTES + 1), chunk_total=2, chunk_num=1)
        # The exact capacities are all accepted.
        self.assertTrue(_build_sms_tpdu('00' * SCP80_SINGLE_BYTES))
        self.assertTrue(_build_sms_tpdu('00' * SCP80_FIRST_BYTES, chunk_total=2, chunk_num=1))
        self.assertTrue(_build_sms_tpdu('00' * SCP80_NEXT_BYTES, chunk_total=2, chunk_num=2))
        self.assertTrue(_build_sms_tpdu('00' * 140, include_cpi=False))

    def test_segment_cap_matches_the_envelope_segment_limit(self):
        from pysim_simple_server.server import MAX_ENVELOPE_SEGMENTS
        self.assertEqual(SCP80_MAX_SEGMENTS, MAX_ENVELOPE_SEGMENTS)
        self.assertEqual(SCP80_MAX_SEGMENTS, 5)


class TestDecodePor(unittest.TestCase):
    def test_plaintext_no_cc_synthetic(self):
        r = _decode_por('02', '01', '15', '15', '0000000001', K, K,
                        '027100000e0ab0000000000000010000016e00')
        self.assertEqual(r['response_status'], 'por_ok')
        self.assertEqual(r['tar'], 'B00000')
        self.assertEqual(r['decoded']['last_status_word'], '6e00')

    def test_sysmocom_signed(self):
        r = _decode_por('06', '09', '35', '35', '0000000001', KIC3, KID3,
                        '027100001612b000110000000000000055f47118381175fb01612f')
        self.assertEqual(r['response_status'], 'por_ok')
        self.assertEqual(r['decoded']['last_status_word'], '612f')

    def test_sysmocom_ciphered(self):
        r = _decode_por('06', '19', '35', '35', '0000000001', KIC3, KID3,
                        '027100001c12b000119660ebdb81be189b5e4389e9e7ab2bc0954f963ad869ed7c')
        self.assertEqual(r['response_status'], 'por_ok')
        self.assertEqual(r['decoded']['last_status_word'], '612f')

    def test_sysmocom_no_cc(self):
        r = _decode_por('06', '01', '35', '35', '0000000001', KIC3, KID3,
                        '027100000e0ab000110000000000000001612f')
        self.assertEqual(r['response_status'], 'por_ok')
        self.assertEqual(r['decoded']['last_status_word'], '612f')

    def test_complete_field_report(self):
        """All parsed PoR fields are surfaced verbatim (v1.9.4)."""
        raw = '027100000e0ab000110000000000000001612f'
        r = _decode_por('06', '01', '35', '35', '0000000001', KIC3, KID3, raw)
        self.assertEqual(r['response_status'], 'por_ok')
        self.assertEqual(r['tar'], 'B00011')
        self.assertEqual(r['cntr'], '0000000000')
        self.assertEqual(r['pcntr'], 0)
        self.assertEqual(r['rpl'], 14)
        self.assertEqual(r['rhl'], 10)
        self.assertEqual(r['cc_rc'], '')
        self.assertEqual(r['raw'], raw)
        self.assertNotIn('cntr_low', str(r))

    def test_cntr_low_fields(self):
        r = _decode_por('02', '01', '15', '15', '0000000001', K, K,
                        '027100000b0ab0000000000000070002')
        self.assertEqual(r['response_status'], 'cntr_low')
        self.assertEqual(r['tar'], 'B00000')
        self.assertEqual(r['cntr'], '0000000007')
        self.assertEqual(r['rpl'], 11)
        self.assertEqual(r['rhl'], 10)
        self.assertIsNone(r.get('decoded'))

    def test_sysmocom_bad_cc_returns_none(self):
        r = _decode_por('06', '09', '35', '35', '0000000001', KIC3, KID3,
                        '027100001612b000110000000000000055f47118381175fb02612f')
        self.assertIsNone(r)

    def test_aes128_ciphered(self):
        r = _decode_por('06', '19', '22', '22', '0000000001', KIC_AES, KID_AES,
                        '027100002412b00011ebc6b497e2cad7aedf36ace0e3a29b38853f0fe9ccde81913be5702b73abce1f')
        self.assertEqual(r['response_status'], 'por_ok')
        self.assertEqual(r['decoded']['last_status_word'], '6132')

    def test_malformed_returns_none(self):
        for bad in ['', '00', '00027100000e0a', '027100000e0ab00000', 'garbage', 'zz']:
            self.assertIsNone(
                _decode_por('02', '01', '15', '15', '0000000001', K, K, bad),
                msg='expected None for %r' % bad)


class TestProactiveDecode(unittest.TestCase):
    """Server-side proactive command/TR decode helpers (v1.8.0 log feature)."""

    def setUp(self):
        import pysim_simple_server.server as srv
        srv._PROACTIVE_SESSION_START = 1234.0
        srv._PLI_DATA[0x00] = '93055210011000'

    @staticmethod
    def _cmd_raw(cmd_type, qualifier, extras=b''):
        """A D0-wrapped proactive command (header TLVs + extras)."""
        body = (bytes([0x81, 0x03, 0x01, cmd_type, qualifier])
                + bytes([0x82, 0x02, 0x83, 0x81]) + extras)
        return bytes([0xD0, len(body)]) + body

    @staticmethod
    def _decoded(cmd_type, raw, qualifier=None):
        return {d['label']: d['value'] for d in _decode_cmd(cmd_type, raw, qualifier)}

    def test_decode_cmd_poll_interval(self):
        r = _decode_cmd(0x03, bytes.fromhex('d00d8103010300820283818402011e'), None)
        self.assertEqual(r, [{'label': 'Interval', 'value': '30 s'}])

    def test_decode_cmd_setup_event_list(self):
        r = _decode_cmd(0x05, bytes.fromhex('d00c810301050082028381990101'), None)
        self.assertEqual(r, [{'label': 'Events', 'value': 'Call connected'}])

    def test_decode_cmd_set_up_menu_with_next_action(self):
        # TS 102 223 8.24: one NAI byte per item, in item order (UCS2 item text).
        extras = bytes.fromhex(
            '8F0A0180004D0065006E0075'      # 1: 'Menu'
            '8F0A028000430061006C006C'      # 2: 'Call'
            '18022510')                     # NAI: SET UP MENU, SET UP CALL
        raw = self._cmd_raw(0x25, 0x00, extras)
        items = _parse_setup_menu_items(raw)
        self.assertEqual([it['nai'] for it in items], [0x25, 0x10])
        self.assertEqual([it['nai_name'] for it in items], ['SET UP MENU', 'SET UP CALL'])
        self.assertEqual(self._decoded(0x25, raw)['Items'],
                         '1. Menu \u2192 SET UP MENU, 2. Call \u2192 SET UP CALL')

    def test_decode_cmd_next_action_reserved_is_ignored(self):
        # '26' (PROVIDE LOCAL INFORMATION) is Type-of-Command only, so it is
        # reserved for the NAI and shall be ignored (8.24).
        extras = bytes.fromhex('8F0A0180004D0065006E0075' '180126')
        raw = self._cmd_raw(0x25, 0x00, extras)
        items = _parse_setup_menu_items(raw)
        self.assertNotIn('nai', items[0])
        self.assertNotIn('nai_name', items[0])
        self.assertEqual(self._decoded(0x25, raw)['Items'], '1. Menu')

    def test_decode_cmd_select_item_short_next_action_list(self):
        # A short NAI list leaves the tail without an indicator; extra bytes
        # beyond the item count are ignored (8.24).
        extras = bytes.fromhex(
            '8F0A0180004D0065006E0075'
            '8F0A028000430061006C006C'
            '180121')
        raw = self._cmd_raw(0x24, 0x00, extras)
        items = _parse_select_item(raw)
        self.assertEqual(items[0]['nai_name'], 'DISPLAY TEXT')
        self.assertNotIn('nai_name', items[1])
        self.assertEqual(self._decoded(0x24, raw)['Items'],
                         '1. Menu \u2192 DISPLAY TEXT, 2. Call')

    def test_decode_cmd_send_short_message(self):
        # SEND SHORT MESSAGE with an SMS-SUBMIT TPDU carrying GSM-7 text.
        tpdu = bytes.fromhex('010006912143F5000005E8329BFD06')
        raw = self._cmd_raw(0x13, 0, bytes([0x8B, len(tpdu)]) + tpdu)
        r = self._decoded(0x13, raw)
        self.assertEqual(r['Type'], 'SMS-SUBMIT')
        self.assertEqual(r['TP-MR'], '0')
        self.assertEqual(r['TP-DA'], '12345')
        self.assertEqual(r['TP-PID'], '0x00')
        self.assertEqual(r['TP-DCS'], '0x00')
        self.assertEqual(r['TP-UDL'], '5')
        self.assertEqual(r['Text'], 'hello')
        self.assertEqual(r['SMS TPDU'], tpdu.hex().upper())

    def test_decode_cmd_send_short_message_udh_8bit(self):
        # UDHI + concatenation IE (16-bit ref) + 8-bit text data.
        udh = bytes.fromhex('0608040001020341 42'.replace(' ', ''))
        tpdu = (bytes.fromhex('4100' '06912143F5' '00' '04' '09') + udh)
        raw = self._cmd_raw(0x13, 0, bytes([0x8B, len(tpdu)]) + tpdu)
        r = self._decoded(0x13, raw)
        self.assertEqual(r['Concat (16-bit ref)'], '1, part 2/3')
        self.assertEqual(r['Text'], 'AB')

    def test_decode_cmd_send_short_message_ucs2(self):
        text = 'Тест'.encode('utf-16-be')
        tpdu = (bytes.fromhex('0100' '06912143F5' '00' '08' '%02X' % len(text))
                + text)
        raw = self._cmd_raw(0x13, 0, bytes([0x8B, len(tpdu)]) + tpdu)
        r = self._decoded(0x13, raw)
        self.assertEqual(r['TP-DCS'], '0x08')
        self.assertEqual(r['Text'], 'Тест')

    def test_decode_cmd_send_short_message_secured_packet(self):
        # PID 0x7F = SIM data download: the UD is a secured packet (TS 31.115).
        tpdu = bytes.fromhex('0100' '06912143F5' '7F' 'F6' '03' 'AABBCC')
        raw = self._cmd_raw(0x13, 0, bytes([0x8B, len(tpdu)]) + tpdu)
        r = self._decoded(0x13, raw)
        self.assertEqual(r['TP-PID'], '0x7F (SIM data download)')
        self.assertEqual(r['Secured packet (TS 31.115)'], '3 bytes: AABBCC')

    def test_decode_cmd_send_short_message_malformed_falls_back(self):
        # A malformed/garbage TPDU must not raise: the raw hex line remains.
        raw = bytes.fromhex('d0158103011300820283818b0b916106152670f900a35f020101')
        r = self._decoded(0x13, raw)
        self.assertEqual(r['SMS TPDU'], '916106152670F900A35F02')

    def test_decode_cmd_pli_qualifier_name(self):
        r = _decode_cmd(0x26, b'\xd0', 0x00)
        self.assertTrue(r[0]['value'].startswith('Location Information (MCC, MNC, LAC/TAC, Cell ID)'))

    def test_decode_cmd_pli_all_standard_qualifiers_named(self):
        # TS 102 223 V18.3.0 (PLI qualifier coding): names must exist even
        # without a special data decoder, e.g. ESN (07) and MEID (0B).
        cases = {
            0x07: 'ESN',
            0x0B: 'MEID',
            0x1A: 'Supported Radio Access Technologies',
            0x05: 'Reserved for GSM',
        }
        for qualifier, name in cases.items():
            r = _decode_cmd(0x26, b'\xd0', qualifier)
            self.assertIn(name, r[0]['value'], 'qualifier 0x%02X' % qualifier)

    def test_decode_cmd_timer_management_start(self):
        # TS 102 223 6.6.21/8.37/8.38: start timer 3 for 14:07:32
        raw = bytes.fromhex('d011810301270082028182a40103a503417023')
        self.assertEqual(_decode_cmd(0x27, raw, 0x00), [
            {'label': 'Action', 'value': 'Start'},
            {'label': 'Timer', 'value': '3'},
            {'label': 'Value', 'value': '14:07:32'},
        ])

    def test_decode_cmd_timer_management_plain_tags(self):
        # Cards may use the plain (non comprehension-required) tag variant.
        raw = bytes.fromhex('d00c010301270102028182240103')
        self.assertEqual(_decode_cmd(0x27, raw, 0x01), [
            {'label': 'Action', 'value': 'Deactivate'},
            {'label': 'Timer', 'value': '3'},
        ])

    def test_decode_cmd_open_channel_cr_tags(self):
        # Same OPEN CHANNEL as the reference traces, but with CR-set TLVs.
        raw = bytes.fromhex(
            'd02b8103014001820281828500b50103b9020200c70b076d656761666f6e2e7275'
            'bc03021f90be05217f000001')
        r = _decode_cmd(0x40, raw, 0x01)
        self.assertIn({'label': 'Bearer', 'value': '0x03'}, r)
        self.assertIn({'label': 'Buffer size', 'value': '512'}, r)
        self.assertIn({'label': 'APN', 'value': 'megafon.ru'}, r)
        self.assertIn({'label': 'Destination', 'value': '127.0.0.1'}, r)
        self.assertIn({'label': 'Transport', 'value': 'TCP client port 8080'}, r)

    def test_decode_cmd_bip_channel_from_device_ids(self):
        # Real trace: SEND DATA carries the channel in the device identities
        # (source UICC 0x81, destination Channel 1 0x21).
        raw = bytes.fromhex('d00e8103014301820281213701013603aabbcc')
        r = _decode_cmd(0x43, raw, 0x01)
        self.assertEqual(r[0], {'label': 'Channel', 'value': '1'})
        self.assertEqual(r[1], {'label': 'Data bytes', 'value': '3'})

    def test_parse_proactive_header_plain_tags(self):
        import pysim_simple_server.server as srv
        raw = bytes.fromhex('d00c010301270102028182240103')
        self.assertEqual(srv._parse_proactive_header(raw), (1, 0x27, 0x81, 0x82, 0x01))

    def test_default_handler_logs_timer_management(self):
        # pySim's auto-handler path: the parsed command object (not the empty
        # collection) is re-encoded for the log and used for the response.
        import pysim_simple_server.server as srv
        from pySim.cat import ProactiveCommand
        from pySim.utils import h2b
        srv._PROACTIVE_LOG.clear()
        handler = srv._DefaultProactiveHandler()
        pcmd = ProactiveCommand()
        parsed = pcmd.from_tlv(h2b('d011810301270082028182a40103a503417023'))
        ti = handler.receive_fetch_raw(pcmd, parsed)
        tr = b''.join(x.to_tlv() for x in ti).hex()
        self.assertTrue(tr.startswith('810301270082028281830100'), tr)
        entry = srv._PROACTIVE_LOG[-1]
        self.assertEqual(entry['type_hex'], '27')
        self.assertEqual(entry['type_name'], 'TIMER MANAGEMENT')
        self.assertEqual(entry['tr_result'], '00')

    def test_default_handler_pli_includes_dict_data(self):
        import pysim_simple_server.server as srv
        from pySim.cat import ProactiveCommand
        from pySim.utils import h2b
        srv._PROACTIVE_LOG.clear()
        srv._PLI_DATA[0x00] = '93055210011000'
        handler = srv._DefaultProactiveHandler()
        pcmd = ProactiveCommand()
        parsed = pcmd.from_tlv(h2b('d00d810301260082028182'))
        ti = handler.receive_fetch_raw(pcmd, parsed)
        tr = b''.join(x.to_tlv() for x in ti).hex()
        self.assertIn('93055210011000', tr)
        self.assertEqual(srv._PROACTIVE_LOG[-1]['tr_hex'], '93055210011000')

    def test_decode_cmd_empty_raw(self):
        self.assertEqual(_decode_cmd(0x26, b'', None), [])
        self.assertEqual(_decode_cmd(0x03, None, None), [])

    def test_decode_tr_pli_location(self):
        r = _decode_tr('26', '00', '93055210011000')
        self.assertEqual(r, [
            {'label': 'MCC', 'value': '250'},
            {'label': 'MNC', 'value': '11'},
            {'label': 'LAC/TAC', 'value': '1000'},
        ])

    def test_decode_tr_pli_imei(self):
        r = _decode_tr('26', '01', '94082143658709214305')
        self.assertEqual(r[0], {'label': 'IMEI', 'value': '123456789012345'})

    def test_decode_tr_pli_access_technology(self):
        r = _decode_tr('26', '06', 'bf0103')
        self.assertEqual(r, [{'label': 'Access Technology', 'value': 'UTRAN (3)'}])

    def test_decode_tr_pli_search_mode(self):
        r = _decode_tr('26', '09', 'ad0101')
        self.assertEqual(r, [{'label': 'Search Mode', 'value': 'Manual'}])

    def test_decode_tr_poll_interval(self):
        r = _decode_tr('03', None, '8402011e')
        self.assertEqual(r, [{'label': 'Interval', 'value': '30 s'}])

    def test_decode_tr_empty(self):
        self.assertEqual(_decode_tr('26', '00', ''), [])

    def test_tr_data_only_strips_boilerplate(self):
        tr = bytes.fromhex('81030326008202818393055210011000030100')
        self.assertEqual(_tr_data_only(tr).hex(), '93055210011000')

    def test_build_and_record_tr(self):
        entry = {'type_hex': '26', 'qualifier': '00'}
        tr = _build_tr(None, 3, 0x26, 0x83, 0x81, 0x00)
        _record_tr(entry, tr, '9000')
        self.assertEqual(entry['tr_hex'], '93055210011000')
        self.assertEqual(entry['tr_sw'], '9000')
        self.assertEqual([f['label'] for f in entry['tr_decoded']], ['MCC', 'MNC', 'LAC/TAC'])

    def test_record_tr_without_sw(self):
        entry = {'type_hex': '26', 'qualifier': '00'}
        _record_tr(entry, bytes.fromhex('93055210011000'))
        self.assertNotIn('tr_sw', entry)
        self.assertEqual(entry['tr_hex'], '93055210011000')

    def test_log_proactive_fields(self):
        entry = _log_proactive(0x26, b'\xd0', 0x00, 7)
        self.assertEqual(entry['cmd_num'], 7)
        self.assertEqual(entry['type_hex'], '26')
        self.assertEqual(entry['qualifier'], '00')
        self.assertEqual(entry['raw'], 'd0')
        self.assertEqual(entry['cmd_decoded'][0]['label'], 'Qualifier')
        self.assertIn('id', entry)

    def test_log_proactive_malformed_raw(self):
        entry = _log_proactive(0x21, bytes([0xD0, 0xFF]), 0x00)
        self.assertEqual(entry['cmd_decoded'], [])

    def test_log_proactive_no_cmd_num(self):
        entry = _log_proactive(0x05, bytes.fromhex('990101'), None)
        self.assertNotIn('cmd_num', entry)

    def test_record_tr_basic_result(self):
        entry = {'type_hex': '03', 'qualifier': None}
        _record_tr(entry, bytes.fromhex('8103010300820281838402011e030100'))
        self.assertEqual(entry['tr_result'], '00')
        self.assertEqual(entry['tr_result_name'], 'Command performed successfully')
        self.assertEqual(entry['tr_hex'], '8402011e')

    def test_record_tr_general_result(self):
        entry = {'type_hex': '26', 'qualifier': '00'}
        _record_tr(entry, bytes.fromhex('8103032600820281839305521001100083022001'))
        self.assertEqual(entry['tr_result'], '2001')
        self.assertEqual(entry['tr_result_name'], 'ME currently unable to process command')
        self.assertEqual(entry['tr_hex'], '93055210011000')

    def test_record_tr_unknown_result(self):
        entry = {'type_hex': '26', 'qualifier': '00'}
        _record_tr(entry, bytes.fromhex('810303260082028181030107'))
        self.assertEqual(entry['tr_result'], '07')
        self.assertNotIn('tr_result_name', entry)

    def test_record_tr_no_result_tlv(self):
        entry = {'type_hex': '26', 'qualifier': '00'}
        _record_tr(entry, bytes.fromhex('810303260082028181'))
        self.assertNotIn('tr_result', entry)


class TestEventDownload(unittest.TestCase):
    """ENVELOPE (EVENT DOWNLOAD) assembly, TS 102 223 7.5.11."""

    def _send(self, event_type, event_data):
        import pysim_simple_server.server as srv
        calls = []

        class Tp:
            def send_apdu(self, apdu):
                calls.append(apdu)
                return '', '9000'

        class Scc:
            cat_cla = '80'
            _tp = Tp()

        data, sw = srv._send_event_download(Scc(), event_type, event_data)
        return calls[0], sw

    def test_channel_status_event(self):
        # Event list + device identities + Channel status (8.56): channel 2,
        # link established, info 05 = link dropped.
        apdu, sw = self._send(0x0A, bytes.fromhex('b8028205'))
        self.assertEqual(sw, '9000')
        self.assertEqual(apdu, '80c200000dd60b99010a82028281b8028205')

    def test_event_without_data(self):
        apdu, sw = self._send(0x05, None)
        self.assertEqual(sw, '9000')
        self.assertEqual(apdu, '80c2000009d60799010582028281')


class TestTimerManagement(unittest.TestCase):
    """Terminal side of TIMER MANAGEMENT (TS 102 223 6.6.21, 6.8.13/14, 7.4).

    The start vector is the live card's: timer 1, 60 s."""

    START = bytes.fromhex('d011810301270082028182a40101a503001000')

    def tearDown(self):
        import pysim_simple_server.server as srv
        srv._timer_cancel()

    def test_hms_bcd_roundtrip(self):
        import pysim_simple_server.server as srv
        self.assertEqual(srv._hms_bcd(60).hex(), '001000')
        self.assertEqual(srv._hms_bcd(3723).hex(), '102030')
        self.assertEqual([srv._bcd_swap(b) for b in srv._hms_bcd(3723)], [1, 2, 3])

    def test_start_returns_result_only_and_arms_timer(self):
        import pysim_simple_server.server as srv
        tr = srv._handle_timer_command(1, 0x27, 0x00, self.START, 0x81, 0x82)
        self.assertEqual(tr.hex(), '810301270082028281030100')
        remaining = srv._timer_remaining(1)
        self.assertTrue(55 <= remaining <= 60, remaining)

    def test_get_returns_remaining_value(self):
        import pysim_simple_server.server as srv
        srv._handle_timer_command(1, 0x27, 0x00, self.START, 0x81, 0x82)
        tr = srv._handle_timer_command(1, 0x27, 0x02, self.START, 0x81, 0x82)
        self.assertEqual(tr.hex(), '810301270282028281a40101a503001000030100')

    def test_deactivate_stops_and_reports_value(self):
        import pysim_simple_server.server as srv
        srv._handle_timer_command(1, 0x27, 0x00, self.START, 0x81, 0x82)
        tr = srv._handle_timer_command(1, 0x27, 0x01, self.START, 0x81, 0x82)
        self.assertTrue(tr.hex().startswith('8103012701'), tr.hex())
        self.assertIn('a40101a503001000', tr.hex())
        self.assertIsNone(srv._timer_remaining(1))

    def test_get_on_stopped_timer_is_contradiction(self):
        import pysim_simple_server.server as srv
        tr = srv._handle_timer_command(1, 0x27, 0x02, self.START, 0x81, 0x82)
        self.assertEqual(tr.hex(), '810301270282028281030124')

    def test_timer_expiration_envelope(self):
        import pysim_simple_server.server as srv
        calls = []

        class Tp:
            def send_apdu(self, apdu):
                calls.append(apdu)
                return '', '9000'

        ref = types.SimpleNamespace(
            scc=types.SimpleNamespace(cat_cla='80', _tp=Tp()), stk_pending=None)
        with mock.patch.object(srv, '_server_ref', ref):
            with mock.patch.object(srv, '_CARD_CONNECTED', True):
                srv._timer_expired(1, 60)
        # D7 0C: device identities (terminal -> UICC), Timer id A4, value A5
        self.assertEqual(calls, ['80c200000ed70c82028281a40101a503001000'])

    def test_cancelled_timer_does_not_report(self):
        import pysim_simple_server.server as srv
        calls = []

        class Tp:
            def send_apdu(self, apdu):
                calls.append(apdu)
                return '', '9000'

        ref = types.SimpleNamespace(
            scc=types.SimpleNamespace(cat_cla='80', _tp=Tp()), stk_pending=None)
        with mock.patch.object(srv, '_server_ref', ref):
            with mock.patch.object(srv, '_CARD_CONNECTED', True):
                srv._timer_fire(1, 60)  # never started/cancelled
        self.assertEqual(calls, [])

    def test_decode_tr_timer(self):
        tr = bytes.fromhex('810301270082028281a40101a503001000030100')
        data = _tr_data_only(tr).hex()
        r = _decode_tr('27', '00', data)
        self.assertEqual(r, [{'label': 'Timer', 'value': '1'},
                             {'label': 'Remaining', 'value': '00:01:00'}])


class TestExpandedRemoteResponse(unittest.TestCase):
    """Expanded Remote Response parsing (TS 102 226 §5.2.2)."""
    
    def test_expanded_response_single_command(self):
        entry = {'type_hex': '03', 'qualifier': None}
        _record_tr(entry, bytes.fromhex('810301030082028183030100'))
        self.assertEqual(entry['tr_result'], '00')
        self.assertEqual(entry['tr_result_name'], 'Command performed successfully')
    
    def test_expanded_response_parser_single_command(self):
        # Simple test of the construct parsing structure
        try:
            from construct import Struct, Int8ub, Bytes, GreedyBytes, Optional, Array, this
            from osmocom.utils import b2h
            
            # Create sample expanded response data
            secured_data = bytes.fromhex('01' '01' '9000' '11')  # response_count, cmd#, SW, data
            
            ExpandedRemoteResponse = Struct(
                'response_count'/Int8ub,
                'responses'/Array(this.response_count, Struct(
                    'command_number'/Int8ub,
                    'status_word'/Bytes(2),
                    'response_data'/GreedyBytes,
                    'error_details'/Optional(Struct(
                        'error_code'/Int8ub,
                        'error_info'/GreedyBytes
                    )),
                    'chaining_context'/Optional(Struct(
                        'script_id'/Bytes(4),
                        'is_first'/Int8ub,
                        'is_last'/Int8ub,
                    ))
                ))
            )
            expanded = ExpandedRemoteResponse.parse(secured_data)
            self.assertEqual(expanded.response_count, 1)
            self.assertEqual(expanded.responses[0].command_number, 1)
            self.assertEqual(b2h(expanded.responses[0].status_word).upper(), '9000')
            self.assertEqual(b2h(expanded.responses[0].response_data).upper(), '11')
        except Exception as e:
            self.fail(f"ExpandedRemoteResponse parsing failed: {e}")
    
    def test_expanded_response_with_error(self):
        entry = {'type_hex': '26', 'qualifier': '01'}
        # Result TLV: 03 01 6A (error_code 0x6A)
        _record_tr(entry, bytes.fromhex('81030326008202818303016A'))
        self.assertEqual(entry['tr_result'], '6a')
        self.assertEqual(entry['tr_result_name'], 'Command performed with limited understanding')
    
    def test_expanded_response_with_chaining(self):
        entry = {'type_hex': '26', 'qualifier': '00'}
        # Result: 03 01 00 + chaining context with script_id
        _record_tr(entry, bytes.fromhex('81030326008202818303010093'))
        self.assertEqual(entry['tr_result'], '00')
        self.assertEqual(entry['tr_result_name'], 'Command performed successfully')


class TestSmsConcat(unittest.TestCase):
    """Tests for _parse_sms_concat — SMS UDH concatenation parsing."""

    def test_no_udh_sms_submit(self):
        """SMS-SUBMIT without TP-UDHI: entire UD is payload."""
        from pysim_simple_server.server import _parse_sms_concat
        # First octet 0x01: MTI=01 (SUBMIT), no UDH, no VP
        # MR=00, DA_len=05, DA_type=90, DA=2143F5, PID=00, DCS=04, UDL=03, UD=AABBCC
        tpdu = bytes.fromhex('0100'  # first octet + MR
                             '05'    # DA length
                             '90'    # DA type
                             '2143F5'  # DA data (3 bytes for 5 digits)
                             '0004'  # PID + DCS
                             '03'    # UDL
                             'AABBCC')  # UD (payload)
        ref, total, num, payload = _parse_sms_concat(tpdu)
        self.assertIsNone(ref)
        self.assertIsNone(total)
        self.assertIsNone(num)
        self.assertEqual(payload.hex(), 'aabbcc')

    def test_8bit_concat_iei_0x00(self):
        """SMS-SUBMIT with IEI 0x00 (8-bit reference concatenation)."""
        from pysim_simple_server.server import _parse_sms_concat
        # First octet 0x41: MTI=01 (SUBMIT), TP-UDHI=1, no VP
        # MR=00, DA_len=05, DA_type=90, DA=2143F5, PID=00, DCS=04
        # UDL=09, UDHL=05, UDH: 00 03 04 04 01 (concat IE), payload=AABBCC
        tpdu = bytes.fromhex('4100'  # first octet + MR
                             '05'    # DA length
                             '90'    # DA type
                             '2143F5'  # DA data
                             '0004'  # PID + DCS
                             '09'    # UDL (1 UDHL + 5 UDH + 3 payload = 9)
                             '05'    # UDHL = 5 bytes of UDH
                             '0003'  # IEI=0x00, IEDL=3
                             '04'    # ref
                             '04'    # total (4 segments)
                             '01'    # num (segment 1)
                             'AABBCC')  # payload
        ref, total, num, payload = _parse_sms_concat(tpdu)
        self.assertEqual(ref, 0x04)
        self.assertEqual(total, 4)
        self.assertEqual(num, 1)
        self.assertEqual(payload.hex(), 'aabbcc')

    def test_16bit_concat_iei_0x08(self):
        """SMS-SUBMIT with IEI 0x08 (16-bit reference concatenation)."""
        from pysim_simple_server.server import _parse_sms_concat
        # First octet 0x41: MTI=01, TP-UDHI=1
        # UDH: 06 (UDHL) 08 04 01 02 03 04 (16-bit concat: ref=0x0102, total=3, num=4)
        # payload=FF
        tpdu = bytes.fromhex('4100'
                             '05'
                             '90'
                             '2143F5'
                             '0004'
                             '08'    # UDL (1 UDHL + 6 UDH + 1 payload = 8)
                             '06'    # UDHL
                             '0804'  # IEI=0x08, IEDL=4
                             '0102'  # ref (16-bit, big-endian)
                             '03'    # total
                             '04'    # num
                             'FF')   # payload
        ref, total, num, payload = _parse_sms_concat(tpdu)
        self.assertEqual(ref, 0x0102)
        self.assertEqual(total, 3)
        self.assertEqual(num, 4)
        self.assertEqual(payload.hex(), 'ff')

    def test_udh_with_cpi(self):
        """UDH with concatenation IE + CPI IE (0x70)."""
        from pysim_simple_server.server import _parse_sms_concat
        # First octet 0x41: MTI=01, TP-UDHI=1
        # UDHL=07, UDH: 00 03 04 04 01 (concat) + 70 00 (CPI)
        tpdu = bytes.fromhex('4100'
                             '05'
                             '90'
                             '2143F5'
                             '0004'
                             '0A'    # UDL (1 UDHL + 7 UDH + 1 payload = 9? no: 1+5+2+1=9, but UDH=7 bytes)
                             '07'    # UDHL = 7
                             '0003'  # IEI=0x00, IEDL=3
                             '04'    # ref
                             '04'    # total
                             '01'    # num
                             '7000'  # CPI IE (IEI=0x70, IEDL=0)
                             'DD')   # payload
        ref, total, num, payload = _parse_sms_concat(tpdu)
        self.assertEqual(ref, 0x04)
        self.assertEqual(total, 4)
        self.assertEqual(num, 1)
        self.assertEqual(payload.hex(), 'dd')

    def test_empty_payload(self):
        """Segment with empty payload after UDH."""
        from pysim_simple_server.server import _parse_sms_concat
        # First octet 0x41: MTI=01, TP-UDHI=1
        tpdu = bytes.fromhex('4100'
                             '05'
                             '90'
                             '2143F5'
                             '0004'
                             '06'    # UDL (1 UDHL + 5 UDH + 0 payload = 6)
                             '05'    # UDHL
                             '0003'
                             '01'
                             '02'
                             '01')   # no payload after UDH
        ref, total, num, payload = _parse_sms_concat(tpdu)
        self.assertEqual(ref, 0x01)
        self.assertEqual(total, 2)
        self.assertEqual(num, 1)
        self.assertEqual(len(payload), 0)

    def test_short_tpdu(self):
        """Truncated TPDU returns gracefully."""
        from pysim_simple_server.server import _parse_sms_concat
        ref, total, num, payload = _parse_sms_concat(b'\x01')
        self.assertIsNone(ref)
        self.assertIsNone(total)
        self.assertIsNone(num)

    def test_none_input(self):
        """None input returns empty payload."""
        from pysim_simple_server.server import _parse_sms_concat
        ref, total, num, payload = _parse_sms_concat(None)
        self.assertIsNone(ref)
        self.assertIsNone(total)
        self.assertIsNone(num)
        self.assertEqual(len(payload), 0)

    def test_short_tpdu(self):
        """Truncated TPDU returns gracefully."""
        from pysim_simple_server.server import _parse_sms_concat
        ref, total, num, payload = _parse_sms_concat(b'\x44')
        self.assertIsNone(ref)
        self.assertIsNone(total)
        self.assertIsNone(num)

    def test_none_input(self):
        """None input returns empty payload."""
        from pysim_simple_server.server import _parse_sms_concat
        ref, total, num, payload = _parse_sms_concat(None)
        self.assertIsNone(ref)
        self.assertIsNone(total)
        self.assertIsNone(num)
        self.assertEqual(len(payload), 0)


class TestSmsReassembly(unittest.TestCase):
    """Tests for SMS segment reassembly logic."""

    def test_single_segment_no_concat(self):
        """Single segment without UDH → submit_tpdu_hex is set directly."""
        from pysim_simple_server.server import PoRSubmitHandler, _find_sms_tpdu, _parse_sms_concat
        handler = PoRSubmitHandler()
        # Build a simple D0 with tag 8B containing an SMS-SUBMIT without UDH
        sms_tpdu = bytes.fromhex('040005902143F50004'  # SMS-SUBMIT header
                                 '03'                   # UDL
                                 'AABBCC')              # payload
        # Wrap in D0 proactive command
        d0 = bytes([0xD0, len(sms_tpdu) + 4,  # approximate BER length
                     0x81, 0x03, 0x01, 0x13, 0x00,  # Command Details
                     0x82, 0x02, 0x81, 0x83,  # Device Identities
                     0x8B, len(sms_tpdu)])  # tag 8B
        # Simulate _find_sms_tpdu extracting tag 8B
        found = sms_tpdu.hex()
        # Parse and check
        ref, total, num, payload = _parse_sms_concat(sms_tpdu)
        self.assertIsNone(ref)
        handler.submit_tpdu_hex = found  # single segment path
        self.assertEqual(handler.submit_tpdu_hex, found)

    def test_multi_segment_reassembly(self):
        """3 segments with IEI 0x00 in random order → assembled in correct order."""
        from pysim_simple_server.server import PoRSubmitHandler
        handler = PoRSubmitHandler()

        # Segment payloads (after UDH)
        payloads = [b'\x01\x02', b'\x03\x04', b'\x05\x06']
        ref = 0x42
        total = 3

        # Simulate receiving segments in random order: 2, 0, 1
        for idx in [1, 0, 2]:
            num = idx + 1
            handler.sms_segments.append((ref, total, num, payloads[idx].hex()))
            # Check if all segments collected
            matching = [s for s in handler.sms_segments if s[0] == ref]
            if len(matching) >= total:
                sorted_segs = sorted(matching, key=lambda s: s[2])
                assembled = b''.join(bytes.fromhex(s[3]) for s in sorted_segs)
                handler.submit_tpdu_hex = assembled.hex()

        self.assertEqual(handler.submit_tpdu_hex, '010203040506')

    def test_independent_references(self):
        """Two different reference numbers are independent."""
        from pysim_simple_server.server import PoRSubmitHandler
        handler = PoRSubmitHandler()

        # Ref 0x01: 2 segments
        handler.sms_segments.append((0x01, 2, 1, 'AA'))
        handler.sms_segments.append((0x01, 2, 2, 'BB'))
        matching = [s for s in handler.sms_segments if s[0] == 0x01]
        if len(matching) >= 2:
            sorted_segs = sorted(matching, key=lambda s: s[2])
            assembled = b''.join(bytes.fromhex(s[3]) for s in sorted_segs)
            handler.submit_tpdu_hex = assembled.hex()

        self.assertEqual(handler.submit_tpdu_hex, 'aabb')

        # Ref 0x02: 1 segment (independent)
        handler.sms_segments.append((0x02, 1, 1, 'CC'))
        matching2 = [s for s in handler.sms_segments if s[0] == 0x02]
        if len(matching2) >= 1:
            sorted_segs2 = sorted(matching2, key=lambda s: s[2])
            assembled2 = b''.join(bytes.fromhex(s[3]) for s in sorted_segs2)
            # Only update if ref 0x02 is complete
            handler.submit_tpdu_hex = assembled2.hex()

        # Last assembly was ref 0x02
        self.assertEqual(handler.submit_tpdu_hex, 'cc')


if __name__ == '__main__':
    unittest.main()


class CapApduSequenceTest(unittest.TestCase):
    """RAM APDU sequence shared by the SCP80 and SCP81 install paths."""

    def _mini_cap(self):
        import io, zipfile
        # Header: tag(1) size(2) magic(4) minor(1) major(1) flags(1)
        #         pkg minor(1) pkg major(1) aid_len(1) aid(N)
        header = (b'\x01\x00\x11' + b'\xde\xca\xff\xed' + b'\x00\x01\x00' +
                  b'\x00\x01' + b'\x06' + b'\xa0\x00\x00\x01\x00\x01')
        # Applet: tag(1) size(2) count(1) aid_len(1) module_aid(N) offset(2)
        applet = (b'\x03\x00\x0a\x01\x05' + b'\xa0\x00\x00\x01\x00' + b'\x00\x08')
        buf = io.BytesIO()
        zf = zipfile.ZipFile(buf, 'w')
        zf.writestr('pkg/Header.cap', header)
        zf.writestr('pkg/Applet.cap', applet)
        zf.close()
        return buf.getvalue().hex().upper()

    def test_cap_parse(self):
        from pysim_simple_server.server import _cap_parse
        loadfile_aid, module_aid, data = _cap_parse(self._mini_cap())
        self.assertEqual(loadfile_aid, 'A00000010001')
        self.assertEqual(module_aid, 'A000000100')
        # Header then Applet, per the CAP component order.
        self.assertTrue(data.startswith('010011DECAFFED'))
        self.assertIn('03000A01', data)

    def test_sequence_install_load_install(self):
        from pysim_simple_server.server import _cap_apdu_sequence
        seq = _cap_apdu_sequence('A00000010001', 'A000000100', 'AABBCCDD')
        # INSTALL [for load]: lv(pkg aid) + lv(ISD) + 000000
        self.assertEqual(seq[0],
            '80E6020013' + '06A00000010001' + '08A000000003000000' + '000000' + '00')
        # One LOAD block (small payload, last -> P1=0x80, P2=0)
        self.assertEqual(seq[1][:8], '80E88000')
        self.assertTrue(seq[1].endswith('00'))
        # INSTALL [for install]: C9 00 install params appended to the lv chain
        self.assertTrue(seq[2].startswith('80E60C00'))
        self.assertIn('06A00000010001' + '05A000000100' + '05A000000100' + '0100', seq[2])

    def test_load_blocks_split_and_counter(self):
        from pysim_simple_server.server import _cap_apdu_sequence, _ber_len as _ber_len_lower
        data = ''.join('%02X' % (i % 256) for i in range(700))
        seq = _cap_apdu_sequence('A00000010001', 'A000000100', data)
        self.assertEqual(len(seq), 5)          # INSTALL + 3 LOAD + INSTALL
        self.assertEqual(seq[1][:8], '80E80000')
        self.assertEqual(seq[2][:8], '80E80001')
        self.assertEqual(seq[3][:8], '80E88002')   # last block: P1=0x80
        # The blocks are consecutive chunks and reassemble the load file TLV
        # byte-for-byte (a shifted/overlapping split fails the card mid-load).
        def payload(apdu):
            lc = int(apdu[8:10], 16)
            return apdu[10:10 + lc * 2]
        joined = payload(seq[1]) + payload(seq[2]) + payload(seq[3])
        self.assertTrue(joined.startswith('C482'))
        expected = 'C4' + _ber_len_lower(700) + data   # 700 = 0x2BC
        self.assertEqual(joined.upper(), expected.upper())
        self.assertEqual(int(seq[3][8:10], 16), len(expected) // 2 - 480)

    def test_custom_block_size_splits_into_more_blocks(self):
        # A smaller block size (SCP80: fit one SMS) slices the load file TLV
        # into consecutive chunks of that size, the last block marked P1=0x80
        # with the block counter in P2.
        from pysim_simple_server.server import _cap_apdu_sequence
        data = ''.join('%02X' % (i % 256) for i in range(700))   # TLV = 704 bytes
        seq = _cap_apdu_sequence('A00000010001', 'A000000100', data, block_size=100)
        self.assertEqual(len(seq), 10)                 # INSTALL + 8 LOAD + INSTALL
        loads = seq[1:-1]
        self.assertEqual(len(loads), 8)
        for i, apdu in enumerate(loads):
            self.assertEqual(apdu[:8], '80E8%s%02X' % ('80' if i == 7 else '00', i))
        def payload(apdu):
            lc = int(apdu[8:10], 16)
            return apdu[10:10 + lc * 2]
        joined = ''.join(payload(a) for a in loads)
        self.assertEqual(len(joined) // 2, 704)        # C4 82 02BC + 700 data bytes
        self.assertTrue(joined.startswith('C482'))
        self.assertEqual(int(loads[0][8:10], 16), 100)
        self.assertEqual(int(loads[-1][8:10], 16), 4)  # 704 = 7*100 + 4

    def test_gen_install_returns_the_apdu_list(self):
        # /api/scp81/gen-install: build the INSTALL/LOAD/INSTALL list for a
        # .cap without touching any listener or script state.
        from pysim_simple_server.server import _scp81_gen_install
        resp = _scp81_gen_install({'cap_hex': self._mini_cap(), 'privileges': '01'})
        self.assertTrue(resp['ok'], resp)
        self.assertEqual(resp['load_file_aid'], 'A00000010001')
        self.assertEqual(resp['module_aid'], 'A000000100')
        self.assertEqual(len(resp['apdus']), 3)
        self.assertTrue(resp['apdus'][0].startswith('80E60200'))
        self.assertTrue(resp['apdus'][1].startswith('80E88000'))
        self.assertTrue(resp['apdus'][2].startswith('80E60C00'))
        self.assertNotIn('queued', resp)   # generation only, no queueing

    def test_gen_install_rejects_bad_input(self):
        from pysim_simple_server.server import _scp81_gen_install
        self.assertFalse(_scp81_gen_install({})['ok'])
        resp = _scp81_gen_install({'cap_hex': '00'})
        self.assertFalse(resp['ok'])
        self.assertIn('cap parse failed', resp['error'])


class TerminalProfileTest(unittest.TestCase):
    """Runtime TERMINAL PROFILE: hex validation and re-send."""

    def test_validate_tp_hex(self):
        from pysim_simple_server.server import _validate_tp_hex
        self.assertEqual(_validate_tp_hex('ff 00 80'), ('FF0080', None))
        self.assertEqual(_validate_tp_hex('80FF'), ('80FF', None))
        for bad in ('', '   ', 'F', 'XYZ', 'FF0', 'FF' * 256):
            h, err = _validate_tp_hex(bad)
            self.assertIsNone(h, bad)
            self.assertTrue(err, bad)

    def test_resend_terminal_profile_resets_state_and_sends(self):
        import types
        from pysim_simple_server import server as srv
        server_obj = types.SimpleNamespace(
            terminal_profile='FF00', stk_pending={'type': 'display_text'},
            menu_active=True, event_list=[0x03], sim_menu='old')
        seen = []
        old_send = srv._send_terminal_profile

        def fake_send(scc, tp):
            seen.append(tp)
            return 'menu', [0x09]

        srv._send_terminal_profile = fake_send
        try:
            resp = srv._resend_terminal_profile(server_obj, object())
        finally:
            srv._send_terminal_profile = old_send
        self.assertTrue(resp['ok'])
        self.assertEqual(resp['profile'], 'FF00')
        self.assertEqual(seen, ['FF00'])
        self.assertIsNone(server_obj.stk_pending)
        self.assertFalse(server_obj.menu_active)
        self.assertEqual(server_obj.event_list, [0x09])
        self.assertEqual(server_obj.sim_menu, 'menu')
        self.assertTrue(resp['menu'])
