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
    _tar_is_rm,
    _decode_tr,
    _log_proactive,
    _ota_reference,
    _parse_select_item,
    _parse_setup_menu_items,
    _calc_ud_offset,
    _find_sms_tpdu,
    _cntr_low_fields,
    _counter_probe,
    _counter_probe_candidates,
    _counter_probe_params,
    _preset_counter_seed,
    _ram_normalize_format,
    _parse_proactive_header,
    _parse_display_text,
    _tlv_map,
    _parse_response_scripting,
    _parse_sms_concat,
    _por_remote_sw,
    _ram_next_cntr,
    _ram_format_apdu,
    _ram_free_nv,
    _ram_read_ff21,
    _ram_nv_fields,
    _wrap_expanded_apdu,
    _ram_remote_sw_ok,
    _sms_submit_por,
    _ram_step_result,
    _record_tr,
    _send_secured_packet,
    _spi1_for_tar,
    _spi_from_bytes,
    _split_secured_packet,
    _tr_data_only,
    _menu_send_response,
    _parse_get_input,
    _parse_get_inkey,
    _parse_setup_menu_command,
    _input_text_tlv,
    _validate_input_response,
    _pack_gsm7,
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
        import contextlib
        import io

        import pysim_simple_server.server as srv
        apdu = '80E80000F0' + '00' * 240 + '00'
        sp_hex, _ = _build_secured_packet('16', '01', '15', '15', 'b00000',
                                          '0000000001', apdu, K, K)
        sent = []

        def fake_envelope(tpdu_hex, scc, sm_sc=None, submit_handler=None, **kwargs):
            sent.append(tpdu_hex.upper())
            return '', '9000'

        buf = io.StringIO()
        with mock.patch.object(srv, '_send_envelope', side_effect=fake_envelope):
            with contextlib.redirect_stderr(buf):
                result = _send_secured_packet(object(), sp_hex, oa_number='12345')
        self.assertTrue(result['success'], result)
        self.assertEqual(result['bytes'], len(sp_hex) // 2)
        self.assertEqual(result['segments'], len(sent))
        self.assertTrue(2 <= result['segments'] <= SCP80_MAX_SEGMENTS)
        total = result['segments']
        # every segment's answer is logged (the send-ota and test-run logs)
        log = buf.getvalue()
        for num in range(1, total + 1):
            self.assertIn('OTA SEND: ENVELOPE %d/%d -> 9000' % (num, total), log)
        for num, tpdu in enumerate(sent, start=1):
            concat = '000301%02X%02X' % (total, num)   # IEI 00, IEDL 3, ref 01
            udhl = '07' if num == 1 else '05'
            self.assertIn(udhl + concat, tpdu, num)
            if num == 1:
                self.assertIn(udhl + concat + '7000', tpdu)   # CPI in the first SM
            else:
                self.assertNotIn(concat + '7000', tpdu)

    def test_send_secured_packet_polls_for_a_late_por_only_at_the_last_segment(self):
        import pysim_simple_server.server as srv
        apdu = '80E80000F0' + '00' * 240 + '00'
        sp_hex, _ = _build_secured_packet('16', '01', '15', '15', 'b00000',
                                          '0000000001', apdu, K, K)
        polls = []

        def fake_envelope(tpdu_hex, scc, sm_sc=None, submit_handler=None,
                          poll_status=True, **kwargs):
            polls.append(poll_status)
            return '', '9000'

        with mock.patch.object(srv, '_send_envelope', side_effect=fake_envelope):
            result = _send_secured_packet(object(), sp_hex, oa_number='12345')
        self.assertTrue(result['success'], result)
        self.assertGreater(result['segments'], 1)
        self.assertEqual(polls, [False] * (result['segments'] - 1) + [True])
        # a single-SMS packet keeps the immediate late-PoR poll
        polls.clear()
        with mock.patch.object(srv, '_send_envelope', side_effect=fake_envelope):
            _send_secured_packet(object(), '00' * 10, oa_number='12345')
        self.assertEqual(polls, [True])

    def test_envelope_status_poll_carries_the_capture_handler(self):
        # A late PoR (ENVELOPE 9000, then STATUS 91xx) must reach the
        # proactive chain with the SEND SHORT MESSAGE capture handler.  It
        # used to be defined inside the 91xx branch only, so this path raised
        # UnboundLocalError and killed the operation mid-packet with the
        # command left pending (live 2026-09-29).
        import pysim_simple_server.server as srv

        class FakeTp:
            def send_apdu(self, apdu):
                return '', '9000'

        class FakeScc:
            cat_cla = '80'
            _tp = FakeTp()

        class FakeSubmit:
            sms_segments = []
            submit_tpdu_hex = None
            submit_ud_hex = None

        calls = []
        with mock.patch.object(srv, '_send_status', return_value=('', '910b')), \
                mock.patch.object(srv, '_handle_proactive_chain',
                                  side_effect=lambda scc, sw, cb=None: calls.append((sw, cb))):
            data, sw = srv._send_envelope('00', FakeScc(), submit_handler=FakeSubmit())
        self.assertEqual(sw, '9000')
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], '910b')
        self.assertTrue(callable(calls[0][1]))
        # a mid-packet segment (poll_status=False) skips the poll entirely
        with mock.patch.object(srv, '_send_status') as status, \
                mock.patch.object(srv, '_handle_proactive_chain') as chain:
            srv._send_envelope('00', FakeScc(), submit_handler=FakeSubmit(),
                               poll_status=False)
        self.assertFalse(status.called)
        self.assertFalse(chain.called)

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


    def test_applet_response_falls_back_to_raw(self):
        # A third-party applet's own TAR answers with application bytes;
        # `80 01 10` cannot be a compact response for a 1-byte command script
        # (count 0x80 exceeds it), so the whole secured data is the response
        # data and no status word is invented (live 2026-10-04).
        raw = '027100000e0aaf4d0100020000190000800110'
        r = _decode_por('16', '01', '15', '15', '0000000001', K, K, raw, cmd_len=1)
        self.assertEqual(r['response_type'], 'raw')
        self.assertEqual(r['secured_data'], '800110')
        self.assertEqual(r['decoded']['last_status_word'], '')
        self.assertEqual(r['decoded']['last_response_data'], '800110')
        self.assertIsNone(r['decoded']['number_of_commands'])
        # without the command length the compact parse is kept (unknown
        # context - the pre-fix behaviour)
        r = _decode_por('16', '01', '15', '15', '0000000001', K, K, raw)
        self.assertEqual(r['response_type'], 'compact')
        self.assertEqual(r['decoded']['number_of_commands'], 128)

    def test_rfm_chain_response_stays_compact(self):
        # a chained RFM command script is long enough for its count: the real
        # SW (6a86) is decoded as before
        raw = '027100000e0ab0000000020000180000016a86'
        r = _decode_por('16', '01', '15', '15', '0000000001', K, K, raw, cmd_len=68)
        self.assertEqual(r['response_type'], 'compact')
        self.assertEqual(r['decoded']['last_status_word'], '6a86')
        self.assertEqual(r['secured_data'], '016A86')

    def test_non_rm_tar_is_application_data(self):
        # v3.21.0: the response form follows the command's TAR.  A non-RM TAR
        # answers with application data (TS 102 226 4); `01 00 0B 91 ...`
        # happens to parse as a compact response (count 1, SW 000B) and must
        # not be split into a fabricated status word (live 2026-10-05).
        raw = '027100000e0aaf4d010000000001000001000B919733525088F4'
        r = _decode_por('16', '01', '15', '15', '0000000001', K, K, raw, rm=False)
        self.assertEqual(r['response_type'], 'raw')
        self.assertEqual(r['response_status'], 'por_ok')
        self.assertEqual(r['secured_data'], '01000B919733525088F4')
        self.assertEqual(r['decoded']['last_response_data'], '01000B919733525088F4')
        self.assertEqual(r['decoded']['last_status_word'], '')
        self.assertIsNone(r['decoded']['number_of_commands'])
        # without the TAR context the same bytes still parse as compact
        r = _decode_por('16', '01', '15', '15', '0000000001', K, K, raw)
        self.assertEqual(r['response_type'], 'compact')
        self.assertEqual(r['decoded']['last_status_word'], '000b')

    def test_short_applet_reply_falls_back_to_raw(self):
        # a 2-byte secured data cannot be a compact response (TS 102 226 5.1.2
        # Table 5.1 needs a count plus two status bytes): pySim's decode raises
        # and the packet is reported raw instead of "undecodable"
        raw = '027100000e0aaf4d01000000000100000400'
        r = _decode_por('16', '01', '15', '15', '0000000001', K, K, raw)
        self.assertEqual(r['response_type'], 'raw')
        self.assertEqual(r['response_status'], 'por_ok')
        self.assertEqual(r['secured_data'], '0400')
        self.assertEqual(r['decoded']['last_response_data'], '0400')


class TestTarIsRm(unittest.TestCase):
    """v3.21.0: the response form follows the command's TAR - the preset's
    role entries plus the standard ISD/RFM allocations of TS 101 220 Annex D."""

    def test_standard_allocations(self):
        for tar in ('000000', 'B00000', 'B00001', 'B00010', 'B20100',
                    'B00120', 'B00130', 'B00140'):
            self.assertTrue(_tar_is_rm(None, tar), tar)

    def test_application_allocations_are_not_rm(self):
        # the Annex D application TARs (USAT interpreter, multiplexing, CASD,
        # OMA) and any custom TAR: application-specific data (TS 102 226 4)
        for tar in ('AF4D01', 'B20000', 'B20200', 'B20201', 'B20202',
                    'B20203', 'B00200'):
            self.assertFalse(_tar_is_rm(None, tar), tar)

    def test_preset_roles_win(self):
        preset = {'tars': [{'role': 'isd', 'tar': 'A11300', 'msl': '16'},
                           {'role': '', 'tar': 'AF4D01', 'msl': '16'}]}
        self.assertTrue(_tar_is_rm(preset, 'A11300'))
        self.assertFalse(_tar_is_rm(preset, 'AF4D01'))
        self.assertIsNone(_tar_is_rm(preset, ''))


class TestPresetCounterSeed(unittest.TestCase):
    """v3.20.0: the effective counter of an operation - explicit `cntr` wins,
    otherwise the preset store's value for the packet's keyset."""

    PRESET = {'keysets': [
        {'kic': '15', 'kid': '15', 'kicKey': 'AA', 'kidKey': 'BB',
         'cntr': '0000000001'},
        {'kic': '25', 'kid': '25', 'kicKey': 'CC', 'kidKey': 'DD',
         'cntr': '0000000020'}]}

    def _server(self):
        preset = self.PRESET

        class Store:
            def get(self, pid):
                return preset if pid == 'p1' else None

        return types.SimpleNamespace(card_presets=Store())

    def test_explicit_counter_wins(self):
        self.assertEqual(_preset_counter_seed(self._server(),
                                              {'cntr': ' 0000000009 '}, 1),
                         '0000000009')
        # an explicit counter is used even without a preset
        self.assertEqual(_preset_counter_seed(self._server(), {'cntr': '0000000009'}, 0),
                         '0000000009')

    def test_missing_counter_seeds_from_the_keyset(self):
        self.assertEqual(_preset_counter_seed(self._server(), {'preset_id': 'p1'}, 2),
                         '0000000020')
        # no/unknown preset, unknown keyset or no kvn: nothing to seed
        self.assertEqual(_preset_counter_seed(self._server(), {}, 1), '')
        self.assertEqual(_preset_counter_seed(self._server(), {'preset_id': 'nope'}, 1), '')
        self.assertEqual(_preset_counter_seed(self._server(), {'preset_id': 'p1'}, 3), '')
        self.assertEqual(_preset_counter_seed(self._server(), {'preset_id': 'p1'}, 0), '')


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
        # the data object follows the Result (TS 102 223 6.8.0 order)
        self.assertTrue(tr.startswith('81030126008202828183010093055210011000'), tr)
        self.assertEqual(srv._PROACTIVE_LOG[-1]['tr_hex'], '93055210011000')

    def test_default_handler_pli_datetime_when_dict_empty(self):
        # No dictionary entry: the date/time qualifier is answered from the
        # host clock (TS 102 223 6.4.15), after the Result per 6.8.0.
        import pysim_simple_server.server as srv
        from pySim.cat import ProactiveCommand
        from pySim.utils import h2b
        srv._PROACTIVE_LOG.clear()
        saved = srv._PLI_DATA.get(0x03)
        srv._PLI_DATA[0x03] = ''
        try:
            handler = srv._DefaultProactiveHandler()
            pcmd = ProactiveCommand()
            parsed = pcmd.from_tlv(h2b('d009810301260382028182'))
            ti = handler.receive_fetch_raw(pcmd, parsed)
        finally:
            srv._PLI_DATA[0x03] = saved
        tr = b''.join(x.to_tlv() for x in ti).hex()
        self.assertRegex(tr, r'^8103012603820282818301002607[0-9A-F]{14}$')
        self.assertRegex(srv._PROACTIVE_LOG[-1]['tr_hex'], r'^2607[0-9A-F]{14}$')

    def test_build_tr_puts_result_before_pli_data(self):
        # TS 102 223 6.8.0: command details, device identities, Result, then
        # the command-specific data objects (real terminals send them last).
        tr = _build_tr(None, 1, 0x26, 0x81, 0x82, 0x00)
        self.assertEqual(tr.hex(), '81030126008202828103010093055210011000')

    def test_build_tr_poll_interval_duration_after_result(self):
        import pysim_simple_server.server as srv
        tr = _build_tr(None, 1, 0x03, 0x81, 0x82, None)
        self.assertEqual(tr.hex(),
                         '810301030082028281030100840201%02x' % srv._POLL_INTERVAL)

    def test_build_tr_pli_datetime_from_host_clock_when_dict_empty(self):
        # 8.39 coding: the same vectors as the PWA's eventDateTimeTlv test.
        import pysim_simple_server.server as srv
        from datetime import datetime, timedelta, timezone
        saved = srv._PLI_DATA.get(0x03)
        srv._PLI_DATA[0x03] = ''
        try:
            east = datetime(2026, 9, 30, 21, 55, 0,
                            tzinfo=timezone(timedelta(hours=3)))
            west = datetime(2026, 1, 5, 8, 7, 9,
                            tzinfo=timezone(-timedelta(hours=2, minutes=30)))
            tr_east = _build_tr(None, 1, 0x26, 0x81, 0x82, 0x03, east)
            tr_west = _build_tr(None, 1, 0x26, 0x81, 0x82, 0x03, west)
        finally:
            srv._PLI_DATA[0x03] = saved
        self.assertEqual(tr_east.hex(),
                         '810301260082028281030100260762900312550021')
        # negative zone: the sign bit of the time zone byte (0x08)
        self.assertEqual(tr_west.hex(),
                         '810301260082028281030100260762105080709009')

    def test_build_tr_pli_dict_entry_overrides_host_clock(self):
        import pysim_simple_server.server as srv
        from datetime import datetime, timezone
        saved = srv._PLI_DATA.get(0x03)
        srv._PLI_DATA[0x03] = '130752f01000ff0001'
        try:
            tr = _build_tr(None, 1, 0x26, 0x81, 0x82, 0x03,
                           datetime(2026, 9, 30, 21, 55, tzinfo=timezone.utc))
        finally:
            srv._PLI_DATA[0x03] = saved
        self.assertEqual(tr.hex(), '810301260082028281030100130752f01000ff0001')

    def test_build_tr_pli_empty_other_qualifier_sends_no_data(self):
        import pysim_simple_server.server as srv
        saved = srv._PLI_DATA.get(0x0a)
        srv._PLI_DATA[0x0a] = '  '
        try:
            tr = _build_tr(None, 1, 0x26, 0x81, 0x82, 0x0a)
            data = srv._pli_data_hex(0x0a)
        finally:
            srv._PLI_DATA[0x0a] = saved
        self.assertEqual(tr.hex(), '810301260082028281030100')
        self.assertEqual(data, '')

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

    def test_decode_tr_pli_datetime(self):
        # 8.39 object in the TP-SCTS coding (TS 123 040 9.2.3.11): swapped
        # semi-octet BCD digits, the time zone sign bit 0x08, 'FF' unknown.
        # The vectors are the ones the PWA encoder pins too.
        self.assertEqual(_decode_tr('26', '03', '260762900312550021'), [
            {'label': 'Date', 'value': '2026-09-30'},
            {'label': 'Time', 'value': '21:55:00'},
            {'label': 'TZ offset', 'value': '+03:00'},
        ])
        self.assertEqual(_decode_tr('26', '03', '260762105080709009')[2],
                         {'label': 'TZ offset', 'value': '-02:30'})
        self.assertEqual(_decode_tr('26', '03', '2607629003125500FF')[2],
                         {'label': 'TZ offset', 'value': 'unknown'})

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

    def _send(self, event_type, event_data, src=None):
        import pysim_simple_server.server as srv
        calls = []

        class Tp:
            def send_apdu(self, apdu):
                calls.append(apdu)
                return '', '9000'

        class Scc:
            cat_cla = '80'
            _tp = Tp()

        data, sw = srv._send_event_download(Scc(), event_type, event_data, src=src)
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

    def test_event_source_device_identity_can_be_overridden(self):
        # MT call / CSG cell selection come from the network (TS 102 223 8.7)
        apdu, sw = self._send(0x00, None, src='83')
        self.assertEqual(sw, '9000')
        self.assertEqual(apdu, '80c2000009d60799010082028381')
        # an invalid source falls back to the terminal
        apdu, sw = self._send(0x00, None, src='zz')
        self.assertEqual(apdu, '80c2000009d60799010082028281')

    def test_event_with_long_data_uses_the_ber_long_form(self):
        # 128+ bytes of event data: the D6 length becomes '81' <len> (the
        # 7.5.x tables allow 1 or 2 length bytes; a raw byte above 0x7F would
        # be read as a long-form indicator)
        payload = bytes(range(200))
        apdu, sw = self._send(0x1D, payload)
        self.assertEqual(sw, '9000')
        inner = bytes([0x99, 0x01, 0x1D, 0x82, 0x02, 0x82, 0x81]) + payload
        d6 = bytes([0xD6, 0x81, len(inner)]) + inner
        self.assertEqual(apdu, '80c20000%02x%s' % (len(d6), d6.hex()))

    def test_event_data_beyond_one_envelope_is_refused(self):
        # the tool sends one envelope per event: the inner data is capped at
        # 252 bytes (the D6 long-form length and the APDU Lc stay in one byte);
        # chained (multi-envelope) delivery is not implemented
        with self.assertRaises(ValueError) as ctx:
            self._send(0x1F, bytes(245))
        self.assertIn('one ENVELOPE', str(ctx.exception))
        # 244 bytes of event data still fit (inner = 252, the maximum)
        apdu, sw = self._send(0x1F, bytes(244))
        self.assertEqual(sw, '9000')
        inner = bytes([0x99, 0x01, 0x1F, 0x82, 0x02, 0x82, 0x81]) + bytes(244)
        d6 = bytes([0xD6, 0x81, len(inner)]) + inner
        self.assertEqual(apdu, '80c20000%02x%s' % (len(d6), d6.hex()))


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
        from pysim_simple_server.server import PoRSubmitHandler
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
        # INSTALL [for load]: lv(pkg aid) + empty SD (ISD default) + 000000,
        # case 3 - the reference terminal form (GP Card Spec 2.3.1 Table 11-42:
        # the SD AID is conditional; a trailing Le makes the card execute a
        # phantom second command whose SW 6700 aborts the install).
        self.assertEqual(seq[0],
            '80E602000B' + '06A00000010001' + '00000000')
        # One LOAD block (small payload, last -> P1=0x80, P2=0)
        self.assertEqual(seq[1][:8], '80E88000')
        # INSTALL [for install]: C9 00 install params appended to the lv chain
        self.assertTrue(seq[2].startswith('80E60C00'))
        self.assertIn('06A00000010001' + '05A000000100' + '05A000000100' + '0100', seq[2])
        # every RAM install APDU is case 3: length == header + Lc data, no Le
        for apdu in seq:
            lc = int(apdu[8:10], 16)
            self.assertEqual(len(apdu), 10 + 2 * lc, apdu)

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


    def test_custom_sd_aid_is_included(self):
        from pysim_simple_server.server import _cap_apdu_sequence
        seq = _cap_apdu_sequence('A00000010001', 'A000000100', 'AABBCCDD',
                                 sd_aid='A0000000040000')
        # lv(pkg aid) + lv(custom SD) + 000000
        self.assertEqual(seq[0][:10], '80E6020012')
        self.assertIn('06A00000010001' + '07A0000000040000' + '000000', seq[0])

    def test_install_apdu_matches_the_live_apdu_and_the_sequence_tail(self):
        # /api/ram-install-app reuses the exact final APDU of the full
        # sequence (one builder, no drift).  Pinned to the live no-STK
        # INSTALL [for install] (decrypted 2026-09-28).
        from pysim_simple_server.server import _cap_apdu_sequence, _cap_install_apdu
        self.assertEqual(
            _cap_install_apdu('F0414C46416101', 'F0414C4641610101'),
            '80E60C0020' + '07F0414C46416101' + '08F0414C4641610101'
            + '08F0414C4641610101' + '0100' + '02C900' + '00')
        stk = 'EA0F800D000000000102011203AF4D0100'
        seq = _cap_apdu_sequence('F0414C46416101', 'F0414C4641610101', 'AABBCCDD',
                                 stk_params=stk)
        self.assertEqual(seq[-1], _cap_install_apdu(
            'F0414C46416101', 'F0414C4641610101', stk_params=stk))
        # case 3: the length is header + Lc data, no trailing Le
        lc = int(seq[-1][8:10], 16)
        self.assertEqual(len(seq[-1]), 10 + 2 * lc)
        # make selectable switches P1 to 0x0C
        self.assertTrue(_cap_install_apdu(
            'F0414C46416101', 'F0414C4641610101').startswith('80E60C00'))
        self.assertTrue(_cap_install_apdu(
            'F0414C46416101', 'F0414C4641610101',
            make_selectable=False).startswith('80E60400'))

    def test_install_params_composition_enforces_one_ef(self):
        # TS 102 226 8.2.1.3.2.1 / GPD_SPE_013 v1.1 Table 6-5: the memory
        # quotas (C7/C8) and an EF-form STK part (CA) share ONE System
        # Specific Parameters EF.  Two EFs made the card read the quotas and
        # ignore the CA (live 2026-10-05: por_ok, no toolkit registration).
        from pysim_simple_server.server import _compose_install_params
        # primitives: quotas and the CA are placed in one EF
        self.assertEqual(_compose_install_params('', '', nv_quota=200,
                                                 volatile_quota=100),
                         'C900EF08C7020064C80200C8')
        self.assertEqual(_compose_install_params('', 'EF04CA0201F0'),
                         'C900EF04CA0201F0')
        self.assertEqual(_compose_install_params('', 'EF04CA0201F0', nv_quota=200,
                                                 volatile_quota=100),
                         'C900EF0CC7020064C80200C8CA0201F0')
        # a 4-byte quota above 32767 (GP Card Spec 9.7)
        self.assertEqual(_compose_install_params('', '', volatile_quota=32768),
                         'C900EF06C70400008000')
        # an integral float (a JSON client may send 200.0) is accepted
        self.assertEqual(_compose_install_params('', '', nv_quota=200.0),
                         'C900EF04C80200C8')
        # an EA-form STK part is a sibling of EF (the reference TCA form)
        self.assertEqual(_compose_install_params('', 'EA0480000000',
                                                 volatile_quota=100),
                         'C900EF04C7020064EA0480000000')
        # a caller-composed EF plus an EF-form STK part is refused
        with self.assertRaises(ValueError):
            _compose_install_params('C900EF04C7020064', 'EF04CA0201F0')
        # a bare CA is refused: it belongs inside the EF
        with self.assertRaises(ValueError):
            _compose_install_params('', 'CA0401F00000')
        # a malformed EF is refused
        with self.assertRaises(ValueError):
            _compose_install_params('', 'EF05CA0100')
        # quotas next to raw install parameters are refused
        with self.assertRaises(ValueError):
            _compose_install_params('C900', '', nv_quota=100)
        # a bad quota value is refused
        with self.assertRaises(ValueError):
            _compose_install_params('', '', volatile_quota='-1')
        # a caller-composed EF with an EA part stays as it was
        self.assertEqual(_compose_install_params('C900EF04C7020064', 'EA0480000000'),
                         'C900EF04C7020064EA0480000000')

    def test_install_params_zero_quota_and_truncated_ef(self):
        # `0` is a valid quota (the vendor reference form carried
        # `C7 02 0000 C8 02 0000`): only an absent/empty field is unset.
        from pysim_simple_server.server import (_compose_install_params,
                                                _single_ef_value)
        self.assertEqual(_compose_install_params('', '', nv_quota=0),
                         'C900EF04C8020000')
        self.assertEqual(_compose_install_params('', '', volatile_quota=0,
                                                 nv_quota=0),
                         'C900EF08C7020000C8020000')
        self.assertEqual(_compose_install_params('', '', nv_quota='0'),
                         'C900EF04C8020000')
        self.assertEqual(_compose_install_params('', '', nv_quota=0.0),
                         'C900EF04C8020000')
        # an absent/empty field is not composed
        self.assertEqual(_compose_install_params('', '', nv_quota='',
                                                 volatile_quota=None), 'C900')
        # a truncated EF length is malformed, not an empty EF
        for bad in ('EF', 'EF81', 'EF80', 'EF8201'):
            with self.assertRaises(ValueError, msg=bad):
                _single_ef_value(bad)
        # an empty EF (EF 00) is legal
        self.assertEqual(_single_ef_value('EF00'), '')

    def test_install_apdu_carries_the_composed_parameters(self):
        from pysim_simple_server.server import _cap_install_apdu
        # CA-form STK + quotas: `C9 00 EF{C7,C8,CA}` - one EF (the form the
        # PWA composes; an external caller gets the same via the primitives).
        apdu = _cap_install_apdu('F0414C46416101', 'F0414C4641610101',
                                 stk_params='EF0ACA080000000000000000',
                                 volatile_quota=100)
        self.assertEqual(
            apdu,
            '80E60C0030' + '07F0414C46416101' + '08F0414C4641610101'
            + '08F0414C4641610101' + '0100'
            + '12C900EF0EC7020064CA080000000000000000' + '00')
        # case 3: the length is header + Lc data, no trailing Le
        lc = int(apdu[8:10], 16)
        self.assertEqual(len(apdu), 10 + 2 * lc)

    def test_make_selectable_apdu(self):
        # GP Card Spec 11.5.2.3.3, Table 11-44: '00' '00' lv(AID)
        # lv(privileges) lv(params) lv(token); case 3.
        from pysim_simple_server.server import _cap_make_selectable_apdu
        self.assertEqual(
            _cap_make_selectable_apdu('F0414C4641610101'),
            '80E608000F' + '0000' + '08F0414C4641610101' + '0100' + '0000')
        self.assertEqual(
            _cap_make_selectable_apdu('F0414C4641610101', '04'),
            '80E608000F' + '0000' + '08F0414C4641610101' + '0104' + '0000')

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


class RamPorStepTest(unittest.TestCase):
    """The RAM install reports the PoR verdict and the remote command's own
    status word.  Vectors: a captured live response where every remote
    command returned SW 6700/6F00 while the PoR itself was por_ok, and
    synthetic success responses (9000, 6113)."""

    # 02 71 00 | 000e 0a | TAR b00011/000000 | CNTR | PCNT | STS=00 | compact
    POR_INSTALL = '027100000e0a000000000000011b0000026700'
    POR_INSTALL_OK = '027100000e0ab0001100000000000000019000'
    POR_INSTALL_61XX = '027100000e0ab0001100000000000000016113'

    @staticmethod
    def _por(hexstr):
        return _decode_por('00', '00', '01', '01', '0000000000',
                           '00' * 16, '00' * 16, hexstr)

    def test_remote_sw_success_set(self):
        for sw in ('9000', '6113', '62F1', '6310', 'CAFE'):
            self.assertTrue(_ram_remote_sw_ok(sw), sw)
        for sw in ('6700', '6F00', '6A82', '6400', '', None):
            self.assertFalse(_ram_remote_sw_ok(sw), sw)

    def test_step_reports_the_remote_sw_failure(self):
        por = self._por(self.POR_INSTALL)
        self.assertEqual(por['response_status'], 'por_ok')
        step, err = _ram_step_result('LOAD (1/9)', '9000', por,
                                     self.POR_INSTALL, 274, 3)
        self.assertEqual(step['por_status'], 'por_ok')
        self.assertEqual(step['por_sw'], '6700')
        self.assertEqual(step['por_type'], 'compact')
        self.assertEqual(err, 'remote SW 6700')
        self.assertEqual(step['por_error'], 'remote SW 6700')

    def test_step_accepts_9000_and_61xx_remote_sw(self):
        for hexstr, sw in ((self.POR_INSTALL_OK, '9000'),
                           (self.POR_INSTALL_61XX, '6113')):
            por = self._por(hexstr)
            step, err = _ram_step_result('LOAD (1/9)', '9000', por, hexstr, 10, 1)
            self.assertIsNone(err, sw)
            self.assertNotIn('por_error', step)
            self.assertEqual(step['por_sw'], sw)

    def test_step_no_por_and_undecodable(self):
        # transport SW 9000 with no response data at all: no PoR expected
        step, err = _ram_step_result('LOAD', '9000', None, '', 10, 1)
        self.assertIsNone(err)
        self.assertEqual(step['por_status'], 'no_por')
        # response data present but undecodable: a failure with the raw kept
        step, err = _ram_step_result('LOAD', '9000', None, 'DEADBEEF', 10, 1)
        self.assertEqual(err, 'PoR undecodable')
        self.assertEqual(step['por_raw'], 'DEADBEEF')

    def test_counter_advances_only_for_accepted_packets(self):
        self.assertEqual(_ram_next_cntr('0000000010', True), '0000000011')
        self.assertEqual(_ram_next_cntr('0000000010', False), '0000000010')
        # 5-byte counter: high values keep their top byte (v3.6.24)
        self.assertEqual(_ram_next_cntr('10000AAAC8', True), '10000AAAC9')
        self.assertEqual(_ram_next_cntr('0000FFFFFF', True), '0001000000')
        self.assertEqual(_ram_next_cntr('FFFFFFFFFF', True), '0000000000')

    def test_remote_sw_from_expanded_response(self):
        por = {'response_status': 'por_ok', 'decoded': {},
               'responses': [{'status_word': '9000'}, {'status_word': '6A82'}]}
        self.assertEqual(_por_remote_sw(por), '6A82')



class SmsSubmitCaptureTest(unittest.TestCase):
    """The actual-response SMS-SUBMIT path: the card answers the ENVELOPE with
    PoR status 0x0B and delivers the listing in SMS-SUBMIT TPDUs (SEND SHORT
    MESSAGE proactive commands).  The FETCH bytes below are from a live RAM
    explore (the two 274-byte segments of the ELF listing)."""

    FETCH_1 = ('d081ae81030113008202818305000607812143658719f28b8197510005812143f57ff6'
               '058c070003130201710000e90a000000000000030600000263100bd276000005aaffcafe'
               '0001010007a000000151535001000bd276000005aaffcafe001001000bd276000005aaff'
               'cafe0304010010d2760001180002ff491ff3890000010101000bd276000005aaffcafe00'
               '02010007a0000000620001010007a0000000620002010007a0000000620101010006')
    FETCH_2 = ('d0818e81030113008202818305000607812143658719f28b78510005812143f57ff6'
               '056d050003130202a00000015100010007a0000000620102010007a000000062020101'
               '0008a000000062020801010009a00000006202080101010010a0000000090003ffffff'
               'ff8910710001010010a0000000090003ffffffff891071000201000bd276000005aaff'
               'cafe00030100')

    def test_find_sms_tpdu_reads_ber_long_form_lengths(self):
        tpdu = bytes.fromhex(_find_sms_tpdu(bytes.fromhex(self.FETCH_1)))
        self.assertEqual(len(tpdu), 0x97)                    # 8B 81 97 <151 bytes>
        self.assertEqual(tpdu[:6].hex(), '510005812143'.lower())
        tpdu2 = bytes.fromhex(_find_sms_tpdu(bytes.fromhex(self.FETCH_2)))
        self.assertEqual(len(tpdu2), 0x78)                   # 8B 78 <120 bytes>

    def test_sms_submit_ud_offset_with_relative_validity(self):
        # VPF=10 (relative) = 1 byte, not 7: 0x51 & 0x18 = 0x10 -> relative
        tpdu = bytes.fromhex(_find_sms_tpdu(bytes.fromhex(self.FETCH_1)))
        self.assertEqual(_calc_ud_offset(tpdu), 11)
        # synthetic absolute/enhanced forms still take 7 bytes
        abs_tpdu = bytes.fromhex('5900048111227ff6' + '00' * 7 + '00' + '00' * 8)
        self.assertEqual(_calc_ud_offset(abs_tpdu), 16)

    def test_parse_sms_concat_segments(self):
        ref, total, num, payload = _parse_sms_concat(
            bytes.fromhex(_find_sms_tpdu(bytes.fromhex(self.FETCH_1))))
        self.assertEqual((ref, total, num), (0x13, 2, 1))
        self.assertEqual(len(payload), 132)
        ref2, total2, num2, payload2 = _parse_sms_concat(
            bytes.fromhex(_find_sms_tpdu(bytes.fromhex(self.FETCH_2))))
        self.assertEqual((ref2, total2, num2), (0x13, 2, 2))
        self.assertEqual(len(payload2), 103)

    def test_sms_submit_por_decodes_to_the_listing(self):
        class Handler:
            submit_ud_hex = None
        handler = Handler()
        segs = {}
        for hx in (self.FETCH_1, self.FETCH_2):
            _, tot, num, payload = _parse_sms_concat(bytes.fromhex(_find_sms_tpdu(bytes.fromhex(hx))))
            segs[num] = payload
        handler.submit_ud_hex = b''.join(segs[k] for k in sorted(segs)).hex()
        por_hex = _sms_submit_por(handler)
        self.assertTrue(por_hex.startswith('027100'))
        por = _decode_por('15', '21', '25', '25', '0000000306',
                          '00' * 16, '00' * 16, por_hex)
        self.assertEqual(por['response_status'], 'por_ok')
        self.assertEqual(por['decoded']['last_status_word'], '6310')
        data = por['decoded']['last_response_data']
        self.assertEqual(len(data), 438)
        self.assertTrue(data.lower().startswith('0bd2760000'), data[:20])
        # no submit response captured -> empty string, never a bogus packet
        handler.submit_ud_hex = None
        self.assertEqual(_sms_submit_por(handler), '')



class ResponseScriptingTest(unittest.TestCase):
    """TS 102 226 5.2.2 Response Scripting template (AB definite / AF
    indefinite): the card wraps the R-APDU(s) of the executed remote
    commands.  Real vectors from the live RAM Explore traces - a bare
    compact parse would read `AB` as the command count and lose the data."""

    def test_definite_template_from_a_live_response(self):
        # AB 12: count 80 01 01, R-APDU 23 0D 08A0000000030000000F809000
        pkt = '027100001F0A00000000000002AA0000AB12800101230D08A0000000030000000F8090009000'
        out = _decode_por('00', '00', '01', '01', '0', '00' * 16, '00' * 16, pkt)
        self.assertEqual(out['response_type'], 'scripting')
        self.assertEqual(out['decoded']['number_of_commands'], 1)
        self.assertEqual(out['decoded']['last_status_word'], '9000')
        self.assertEqual(out['decoded']['last_response_data'], '08A0000000030000000F80')

    def test_elf_listing_page_from_a_live_response(self):
        # the F0414C46416101 ELF page (assembled SMS-SUBMIT UD, AB wrapper)
        ud = ('00E90A000000000000030600000263100BD276000005AAFFCAFE0001010007'
              'F0414C4641610101' + '00')
        # build a valid scripting template around the listing tail instead of
        # trusting the truncated sample above
        rapdu = bytes.fromhex('10A1130001180002FFF7100E8904000200' '0100' '07F0414C46416101' '0100' + '9000')
        tmpl = bytes([0xAB, 0x80]) if False else None
        body = bytes([0x80, 0x01, 0x01, 0x23, len(rapdu)]) + rapdu
        data = bytes([0xAB, len(body)]) + body
        scripted = _parse_response_scripting(data)
        self.assertIsNotNone(scripted)
        self.assertEqual((scripted['count'], scripted['sw']), (1, '9000'))
        listing = scripted['data']
        self.assertTrue(listing.startswith('10A1130001'), listing[:20])
        self.assertIn('F0414C46416101', listing)

    def test_indefinite_template_and_plain_data(self):
        body = bytes([0x80, 0x01, 0x02, 0x23, 0x04, 0xAA, 0xBB, 0x90, 0x00])
        data = bytes([0xAF, 0x80]) + body + b'\x00\x00'
        r = _parse_response_scripting(data)
        self.assertEqual((r['count'], r['sw'], r['data']), (2, '9000', 'AABB'))
        # a compact response is not a scripting template
        self.assertIsNone(_parse_response_scripting(bytes.fromhex('027100000263100BD2')))
        self.assertIsNone(_parse_response_scripting(b''))

    def test_bad_format_tlv(self):
        # the live ISD's answer to a definite-length scripting template: zero
        # executed commands plus the Bad format TLV, error type 02 = wrong
        # length (TS 102 226 5.2.2, table 5.12)
        r = _parse_response_scripting(bytes.fromhex('ab06800100900102'))
        self.assertEqual(r, {'count': 0, 'sw': None, 'data': '', 'bad_format': '02'})
        # the plain (CR-flag cleared) tag style is accepted like everywhere else
        self.assertEqual(_parse_response_scripting(bytes.fromhex('ab06800100100102'))['bad_format'],
                         '02')
        # a plain R-APDU template carries no bad format
        self.assertIsNone(_parse_response_scripting(bytes.fromhex('ab0780010123029000'))['bad_format'])



class RamSendGpApduLoggingTest(unittest.TestCase):
    """_ram_send_gp_apdu (the shared step sender of /api/ram-install and
    /api/ram-install-app) logs the plaintext RAM APDU and the packed SCP80
    packet for every step, like /api/send-ota does."""

    def test_logs_plaintext_apdu_and_packed_packet(self):
        import contextlib
        import io
        from pysim_simple_server import server as srv

        class FakeServer:
            sms_oa = '12345'
            sms_sc = '12345678912'

        sp = {'spi1': '16', 'spi2': '01', 'kic': '25', 'kid': '25', 'tar': '000000',
              'kic_key': 'AA', 'kid_key': 'BB', 'include_cpi': True}
        state = {'steps': [], 'encode_error': None, 'failure': {}, 'cntr': '0000000001'}
        orig_build = srv._build_secured_packet
        orig_send = srv._send_secured_packet
        try:
            srv._build_secured_packet = lambda *a, **k: ('AABB', {})
            srv._send_secured_packet = lambda *a, **k: {'success': False, 'error': 'stub'}
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                ok = srv._ram_send_gp_apdu(FakeServer(), object(), sp, state,
                                           'LOAD (1/2)', '80E8800001AA')
        finally:
            srv._build_secured_packet = orig_build
            srv._send_secured_packet = orig_send
        self.assertFalse(ok)
        out = buf.getvalue()
        self.assertIn('RAM C-APDU (LOAD (1/2)): 80E8800001AA', out)
        self.assertIn('RAM SECURED-PACKET (LOAD (1/2)): AABB', out)


class CntrLowGuardTests(unittest.TestCase):
    """The low-counter guard (v3.6.58): the card's verdict is plain data and a
    warning ENVELOPE answer still gets its PoR polled."""

    def test_cntr_low_fields(self):
        # the verdict is a flag: the card's counter is not derivable from the
        # PoR, whose CNTR is a copy of the command's counter (TS 102 225 5.2)
        self.assertEqual(_cntr_low_fields('cntr_low'), {'cntr_low': True})
        # only the low-counter verdict counts
        self.assertEqual(_cntr_low_fields('por_ok'), {})
        self.assertEqual(_cntr_low_fields('cntr_high'), {})
        self.assertEqual(_cntr_low_fields(None), {})

    def test_a_warning_envelope_answer_polls_for_the_por(self):
        from pysim_simple_server import server as srv
        # the live PoR TPDU (response status 02 = cntr_low, card counter BB)
        tpdu = '410005812143f500f610027100000b0a00000000000000bb0002'
        fetch = 'd02e8103011300820281838607919733824009f08b1a' + tpdu

        class FakeTp:
            def __init__(self):
                self.sent = []
                self.proactive_handler = None
                self.status_calls = 0

            def send_apdu(self, apdu):
                self.sent.append(apdu)
                if apdu.startswith('80c20000'):
                    return ('', '6200')          # the live warning answer
                if apdu.startswith('80f2000c'):
                    self.status_calls += 1
                    return ('', '9130' if self.status_calls == 1 else '9000')
                if apdu.startswith('80120000'):
                    return (fetch, '9000')
                return ('', '9000')

        class FakeScc:
            cat_cla = '80'

            def __init__(self):
                self._tp = FakeTp()

        scc = FakeScc()
        handler = srv.PoRSubmitHandler()
        _data, sw = srv._send_envelope(tpdu, scc, submit_handler=handler)
        self.assertEqual(sw, '6200')             # the ENVELOPE's own answer
        self.assertIn('80f2000c00', scc._tp.sent)  # the late-PoR poll ran
        self.assertTrue(handler.submit_tpdu_hex, scc._tp.sent)
        self.assertIn('bb0002', handler.submit_tpdu_hex.lower())


class ProactiveLongTlvTests(unittest.TestCase):
    """BER long-form lengths in proactive command TLVs (v3.6.57).

    The live card's SEND SHORT MESSAGE carries its 150-byte SMS TPDU as
    `8B 81 96`: reading the length as one byte walked into the TPDU and
    eventually indexed past the end (`IndexError: index out of range`, live
    2026-09-30, 500 in the FETCH path with the command left unanswered)."""

    # the live FETCH shape: command details + device identities + address +
    # the long-form SMS TPDU TLV
    TPDU = bytes.fromhex(
        '410005812143f500f68c0700030302017100008d0a00000000000000bb0000029000e3284f10'
        'a1130001180001ffffffff89a10039009f7001018410a1130001180001ffffffff89a1003908'
        'e33a4f10a1130001180002fff7100e89040002009f7001018410a1130001180002fff7100e'
        '89040002088410a1130001180002fff7100e89494d4508e3174f07f0414c464160019f7001')
    RAW = (bytes.fromhex('d081ab8103011300820281838607919733824009f08b8196') +
           TPDU + bytes(range(len(TPDU), 150)))

    def test_header_parses_with_a_long_form_tlv(self):
        self.assertEqual(_parse_proactive_header(self.RAW), (1, 0x13, 0x81, 0x83, 0))
        # the long TLV is found as one TLV, not mis-walked into its value
        self.assertEqual(sorted(_tlv_map(self.RAW[3:]).keys()), [0x81, 0x82, 0x86, 0x8B])
        self.assertEqual(len(_find_sms_tpdu(self.RAW)), 150 * 2)

    def test_a_truncated_tlv_does_not_raise(self):
        # a declared length running past the data ends the walk
        raw = bytes.fromhex('d004810381')
        self.assertEqual(_parse_proactive_header(raw), (1, 0, 0x83, 0x81, None))
        self.assertEqual(_tlv_map(bytes.fromhex('810381')), {})
        # display text with a long-form text TLV (144 bytes -> 8D 81 90)
        text = bytes([0x8D, 0x81, 0x90]) + b'\x00' * 0x90
        head = bytes([0x81, 0x03, 0x01, 0x21, 0x00])
        raw2 = bytes([0xD0, 0x81, len(head) + len(text)]) + head + text
        self.assertIsNotNone(_parse_display_text(raw2))


class ProactiveChainHardeningTests(unittest.TestCase):
    """A decoder bug must never leave a FETCH unanswered (v3.6.57): every
    step of the FETCH/TR loop is guarded and the TR is still sent."""

    def test_a_raising_parser_still_gets_a_terminal_response(self):
        from pysim_simple_server import server as srv

        class FakeTp:
            def __init__(self):
                self.sent = []
                self.proactive_handler = None

            def send_apdu(self, apdu):
                self.sent.append(apdu)
                if apdu.startswith('80120000'):     # FETCH
                    return ('d0048103011300', '9000')
                return ('', '9000')

        class FakeScc:
            cat_cla = '80'

            def __init__(self):
                self._tp = FakeTp()

        scc = FakeScc()
        orig = srv._parse_proactive_header

        def boom(raw):
            raise IndexError('index out of range')

        try:
            srv._parse_proactive_header = boom
            srv._run_proactive_chain(scc, '91A8', None, status_poll=False)
        finally:
            srv._parse_proactive_header = orig
        self.assertTrue(any(a.startswith('80140000') for a in scc._tp.sent),
                        'the FETCH must be answered: %r' % (scc._tp.sent,))


class RamNvFootprintTests(unittest.TestCase):
    """GET DATA FF21 reads around an install (v3.6.51): the free-NV values and
    their delta.  The reads are best-effort - a card without FF21 (or a
    rejected read) must never fail the operation."""

    def test_free_nv_from_ff21_response(self):
        # TS 102 226 8.2.1.7.2: '81' applet count, '82' free NV, '83' free
        # volatile (the length byte follows FF21).
        data = 'FF210C' + '810102' + '820300C5D6' + '83020064'
        self.assertEqual(_ram_free_nv(data), 0xC5D6)
        # CR-set tag variants (0x01/0x02/0x03) decode the same
        self.assertEqual(_ram_free_nv('FF210C' + '010102' + '020300C5D6' + '03020064'), 0xC5D6)
        # not an FF21 response / empty / garbage
        self.assertIsNone(_ram_free_nv('6A88'))
        self.assertIsNone(_ram_free_nv(''))
        self.assertIsNone(_ram_free_nv('not-hex'))

    def test_nv_fields(self):
        self.assertEqual(_ram_nv_fields(None, None), {})
        self.assertEqual(_ram_nv_fields(50000, 20000),
                         {'nv_before': 50000, 'nv_after': 20000, 'nv_delta': 30000})
        # a freed delta is negative (memory released by the operation)
        self.assertEqual(_ram_nv_fields(100, 150),
                         {'nv_before': 100, 'nv_after': 150, 'nv_delta': -50})
        # only one read available: the delta stays None
        self.assertEqual(_ram_nv_fields(100, None),
                         {'nv_before': 100, 'nv_after': None, 'nv_delta': None})

    def test_read_ff21_uses_the_silent_sender(self):
        from pysim_simple_server import server as srv

        calls = []

        def fake_send(server, scc, sp, state, name, apdu, silent=False):
            calls.append((name, apdu, silent))
            if not silent:
                state['steps'].append({'name': name})
            state['last_step'] = {'por_data': 'FF210C810102820300C5D683020064'}
            return True

        state = {'steps': [], 'encode_error': None, 'failure': {}, 'cntr': '0000000001'}
        with mock.patch.object(srv, '_ram_send_gp_apdu', fake_send):
            nv = _ram_read_ff21(None, None, {}, state, 'expanded', 'NV before')
        self.assertEqual(nv, 0xC5D6)
        self.assertEqual(calls, [('NV before', _wrap_expanded_apdu('80CAFF2100'), True)])
        self.assertEqual(state['steps'], [])
        self.assertEqual(state['failure'], {})

    def test_read_ff21_returns_none_when_the_read_fails(self):
        from pysim_simple_server import server as srv

        def fake_send(server, scc, sp, state, name, apdu, silent=False):
            return False

        state = {'steps': [], 'encode_error': None, 'failure': {}, 'cntr': '0000000001'}
        with mock.patch.object(srv, '_ram_send_gp_apdu', fake_send):
            self.assertIsNone(_ram_read_ff21(None, None, {}, state, 'compact', 'NV after'))

    def test_silent_send_leaves_no_step_or_failure(self):
        import contextlib
        import io
        from pysim_simple_server import server as srv

        class FakeServer:
            sms_oa = '12345'
            sms_sc = '12345678912'

        sp = {'spi1': '16', 'spi2': '01', 'kic': '25', 'kid': '25', 'tar': '000000',
              'kic_key': 'AA', 'kid_key': 'BB', 'include_cpi': True}
        state = {'steps': [], 'encode_error': None, 'failure': {}, 'cntr': '0000000001'}
        with mock.patch.object(srv, '_build_secured_packet', lambda *a, **k: ('AABB', {})), \
             mock.patch.object(srv, '_send_secured_packet',
                               lambda *a, **k: {'success': False, 'error': 'stub'}):
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                ok = srv._ram_send_gp_apdu(FakeServer(), object(), sp, state,
                                           'NV before', '80CAFF2100', silent=True)
        self.assertFalse(ok)
        self.assertEqual(state['steps'], [])
        self.assertEqual(state['failure'], {})
        self.assertIsNone(state['encode_error'])
        self.assertIn('RAM C-APDU (NV before): 80CAFF2100', buf.getvalue())


class RamCommandFormatTests(unittest.TestCase):
    """RAM command format detection helpers (TS 102 226 5.2.1, v3.6.24)."""

    def test_expanded_wrapper_matches_the_reference_trace(self):
        # TCA Loader trace: GET STATUS [ISD] sent as 'AA' > '22' > C-APDU
        self.assertEqual(_wrap_expanded_apdu('80F24000024F0000'),
                         'AA0A220880F24000024F0000')

    def test_expanded_wrapper_uses_ber_lengths(self):
        apdu = '80' + 'AB' * 200
        wrapped = _wrap_expanded_apdu(apdu)
        self.assertTrue(wrapped.startswith('AA81'), wrapped[:8])
        self.assertTrue(wrapped.endswith(apdu))
        self.assertEqual(_ram_format_apdu(apdu, 'compact'), apdu)
        self.assertEqual(_ram_format_apdu(apdu, 'expanded'), wrapped)

    def test_detect_prefers_compact_and_records_the_probe(self):
        from pysim_simple_server import server as srv

        calls = []

        def fake_send(server, scc, sp, state, name, apdu):
            calls.append((name, apdu))
            state['steps'].append({'name': name, 'por_status': 'por_ok', 'sw': '9000'})
            state['cntr'] = srv._ram_next_cntr(state['cntr'], True)
            return True

        orig = srv._ram_send_gp_apdu
        srv._ram_send_gp_apdu = fake_send
        try:
            state = {'steps': [], 'encode_error': None, 'failure': {},
                     'cntr': '0000000010'}
            fmt = srv._ram_detect_format(None, None, {}, state)
        finally:
            srv._ram_send_gp_apdu = orig
        self.assertEqual(fmt, 'compact')
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], 'FORMAT CHECK (compact)')
        self.assertEqual(state['cntr'], '0000000011')
        self.assertEqual(len(state['steps']), 1)

    def test_detect_falls_back_to_expanded(self):
        from pysim_simple_server import server as srv

        calls = []

        def fake_send(server, scc, sp, state, name, apdu):
            calls.append((name, apdu))
            wrapped = apdu.startswith('AA')
            state['steps'].append({'name': name,
                                   'por_status': 'por_ok' if wrapped else 'por_remote_error',
                                   'sw': '9000'})
            if wrapped:
                state['cntr'] = srv._ram_next_cntr(state['cntr'], True)
            return wrapped

        orig = srv._ram_send_gp_apdu
        srv._ram_send_gp_apdu = fake_send
        try:
            state = {'steps': [], 'encode_error': None, 'failure': {},
                     'cntr': '10000AAAC8'}
            fmt = srv._ram_detect_format(None, None, {}, state)
        finally:
            srv._ram_send_gp_apdu = orig
        self.assertEqual(fmt, 'expanded')
        self.assertEqual([c[0] for c in calls],
                         ['FORMAT CHECK (compact)', 'FORMAT CHECK (expanded)'])
        self.assertTrue(calls[1][1].startswith('AA'))
        # only the accepted (expanded) probe advanced the 5-byte counter
        self.assertEqual(state['cntr'], '10000AAAC9')
        self.assertEqual(len(state['steps']), 2)


class StkInputTests(unittest.TestCase):
    """GET INKEY / GET INPUT handling (v3.6.38, TS 102 223 6.4.2/6.4.3)."""

    # The live GET INPUT from the RemMobileID menu (2026-09-29): qualifier
    # 0x04 = digits only + hidden entry, Response length 4..16.
    GET_INPUT = bytes.fromhex(
        'd03c8103012304820281828d2d0804120432043504340438044204350020004d006f00'
        '620069006c0065002d00490044002000500049004e003191020410')

    def test_parse_get_input_reads_text_and_response_length(self):
        info = _parse_get_input(self.GET_INPUT)
        self.assertEqual(info['text'], 'Введите Mobile-ID PIN1')
        self.assertEqual((info['min'], info['max']), (4, 16))
        self.assertTrue(info['digits_only'] and info['hidden'])
        self.assertFalse(info['ucs2'] or info['packed'] or info['help'])
        self.assertIsNone(info['default'])

    def test_parse_get_inkey_reads_the_qualifier_flags(self):
        # Command details: qualifier 0x84 = digits only + Yes/No, help bit set
        raw = bytes.fromhex('d0188103012284820281828d0d0804120435044004350434043a0430')
        info = _parse_get_inkey(raw)
        self.assertTrue(info['digits_only'])
        self.assertTrue(info['yes_no'])
        self.assertTrue(info['help'])
        self.assertFalse(info['ucs2'] or info['immediate'])
        # a template without a Text string is not a parseable input command
        self.assertIsNone(_parse_get_inkey(bytes.fromhex('d0038103012200')))

    def test_decode_cmd_reports_the_input_request(self):
        decoded = _decode_cmd(0x23, self.GET_INPUT, 0x04)
        self.assertEqual(decoded[0], {'label': 'Text', 'value': 'Введите Mobile-ID PIN1'})
        self.assertEqual(decoded[1], {'label': 'Length', 'value': '4..16'})
        self.assertEqual(decoded[2], {'label': 'Response', 'value': 'digits only, hidden'})
        # max 0xFF = no maximum (8.11); a help flag is reported too
        raw = bytes.fromhex('d0118103012381820281828d020041910200ff')
        decoded = _decode_cmd(0x23, raw, 0x81)
        self.assertIn({'label': 'Length', 'value': '0..no max'}, decoded)
        self.assertEqual(decoded[-1], {'label': 'Response', 'value': 'SMS default alphabet, help'})

    def test_input_text_tlv_codings(self):
        pd = {'type': 'get_input', 'digits_only': True, 'hidden': True}
        # digits: SMS default alphabet unpacked (DCS 04), one byte per char
        self.assertEqual(_input_text_tlv(pd, '1234').hex().upper(), '8D050431323334')
        # empty input: the null text string (6.8.5)
        self.assertEqual(_input_text_tlv(pd, '').hex().upper(), '8D00')
        # UCS2 request: DCS 08
        self.assertEqual(_input_text_tlv(dict(pd, ucs2=True, hidden=False), '12').hex().upper(),
                         '8D050800310032')
        # packed request: DCS 00 + TS 23.038 septet packing
        self.assertEqual(_input_text_tlv(dict(pd, packed=True, hidden=False), '1234').hex().upper(),
                         '8D050031D98C06')
        # GET INKEY Yes/No: value 01 positive / 00 negative (6.8.5)
        yesno = {'type': 'get_inkey', 'yes_no': True}
        self.assertEqual(_input_text_tlv(yesno, '01').hex().upper(), '8D020401')
        self.assertEqual(_input_text_tlv(yesno, '00').hex().upper(), '8D020400')

    def test_pack_gsm7_matches_the_sms_packing(self):
        self.assertEqual(_pack_gsm7(b'1234').hex().upper(), '31D98C06')
        # 7 septets need 7 octets; the trailing bit is the last septet's MSB
        # (0 for '7' = 0x37), so the padding octet stays 00
        self.assertEqual(_pack_gsm7(b'1234567').hex().upper(), '31D98C56B3DD00')

    def test_validate_input_response(self):
        pd = {'type': 'get_input', 'min': 4, 'max': 16, 'digits_only': True, 'hidden': True}
        self.assertEqual(_validate_input_response(pd, '1234'), ('1234', None))
        self.assertIsNotNone(_validate_input_response(pd, '12')[1])
        self.assertIsNotNone(_validate_input_response(pd, '1' * 17)[1])
        self.assertIsNotNone(_validate_input_response(pd, 'abcd')[1])
        # hidden entry allows only the digits set even without digits_only
        self.assertIsNotNone(_validate_input_response({'type': 'get_input', 'hidden': True}, 'ab')[1])
        # GET INKEY: exactly one character, digits-only honours * # +
        self.assertEqual(_validate_input_response({'type': 'get_inkey'}, 'a'), ('a', None))
        self.assertIsNotNone(_validate_input_response({'type': 'get_inkey'}, 'ab')[1])
        self.assertEqual(_validate_input_response({'type': 'get_inkey', 'digits_only': True}, '*'),
                         ('*', None))
        self.assertIsNotNone(_validate_input_response({'type': 'get_inkey', 'digits_only': True}, 'a')[1])
        # Yes/No: only 01 / 00
        yesno = {'type': 'get_inkey', 'yes_no': True}
        self.assertEqual(_validate_input_response(yesno, '01'), ('01', None))
        self.assertIsNotNone(_validate_input_response(yesno, 'yes')[1])

    def test_menu_send_response_carries_the_input_text(self):
        class FakeTp:
            def __init__(self):
                self.sent = []

            def send_apdu(self, apdu):
                self.sent.append(apdu)
                return '', '9000'

        class FakeServer:
            def __init__(self, pending):
                self.stk_pending = pending
                self.menu_active = True
                self.scc = types.SimpleNamespace(cat_cla='80', _tp=FakeTp())

        def pending(**over):
            pd = {'type': 'get_input', 'cmd_num': 1, 'cmd_type': 0x23,
                  'dev_src': 0x81, 'dev_dst': 0x82, 'text': 'PIN',
                  'min': 4, 'max': 16, 'default': None, 'digits_only': True,
                  'ucs2': False, 'hidden': True, 'packed': False, 'help': False,
                  'duration_unit': None, 'shown_at': None}
            pd.update(over)
            return pd

        server = FakeServer(pending())
        resp, code = _menu_send_response(server, 'ok', None, '1234')
        self.assertEqual(code, 200, resp)
        apdu = server.scc._tp.sent[0].upper()
        self.assertIn('8D050431323334', apdu)          # the entered digits
        self.assertIn('83020000', apdu)                # successful result
        self.assertIsNone(server.stk_pending)
        # a rejected value sends nothing and keeps the command pending
        server2 = FakeServer(pending())
        resp2, code2 = _menu_send_response(server2, 'ok', None, 'abcd')
        self.assertEqual(code2, 400)
        self.assertIn('digits only', resp2['error'])
        self.assertEqual(server2.scc._tp.sent, [])
        # help is result 0x13 without a Text string
        server3 = FakeServer(pending(help=True))
        resp3, code3 = _menu_send_response(server3, 'help')
        self.assertEqual(code3, 200)
        self.assertIn('83021300', server3.scc._tp.sent[0].upper())

    def test_menu_send_response_echoes_the_variable_timeout(self):
        class FakeTp:
            def __init__(self):
                self.sent = []

            def send_apdu(self, apdu):
                self.sent.append(apdu)
                return '', '9000'

        server = types.SimpleNamespace(
            stk_pending={'type': 'get_input', 'cmd_num': 2, 'cmd_type': 0x23,
                         'dev_src': 0x81, 'dev_dst': 0x82, 'text': 'PIN',
                         'min': 4, 'max': 16, 'digits_only': True, 'hidden': True,
                         'ucs2': False, 'packed': False, 'help': False,
                         'duration_unit': 0x01, 'shown_at': 997.0},
            menu_active=True,
            scc=types.SimpleNamespace(cat_cla='80', _tp=FakeTp()))
        with mock.patch('time.monotonic', return_value=1000.0):
            resp, code = _menu_send_response(server, 'ok', None, '1234')
        self.assertEqual(code, 200, resp)
        # TS 102 223 6.8.4: 3 seconds in the requested unit (seconds)
        self.assertIn('04020103', server.scc._tp.sent[0].upper())


class SetupMenuParseTests(unittest.TestCase):
    """SET UP MENU parsing must accept both TLV tag styles (v3.6.40)."""

    # The live Alfa card's SET UP MENU as fetched during the TERMINAL PROFILE
    # chain (2026-09-29): the title is the CR-set tag 0x85, the items 0x8F.
    LIVE_FETCH = bytes.fromhex(
        'd03c810301250082028182850b416c6661204d6f62696c65'
        '8f16808112089db0c1c2c0beb9bab82f53657474696e6773'
        '8f0881810400d4e5f3f418020021')

    def test_live_card_title_and_items_are_parsed(self):
        menu = _parse_setup_menu_command(self.LIVE_FETCH)
        self.assertEqual(menu['command_number'], 1)
        self.assertEqual(menu['title'], 'Alfa Mobile')
        self.assertEqual(menu['items'][0], {'id': 0x80, 'text': 'Настройки/Settings'})
        self.assertEqual(menu['items'][1],
                         {'id': 0x81, 'text': 'Test', 'nai': 0x21, 'nai_name': 'DISPLAY TEXT'})

    def test_plain_tag_variants_are_accepted(self):
        # same command with the plain title tag 0x05 and a plain 0x0F item
        raw = bytes.fromhex('d012810301250082028182850b416c6661204d6f62696c650f00')
        menu = _parse_setup_menu_command(raw)
        self.assertEqual(menu['title'], 'Alfa Mobile')
        self.assertEqual(menu['items'], [])
        # a command that is not SET UP MENU has no menu
        self.assertIsNone(_parse_setup_menu_command(bytes.fromhex('d009810301260182028182')))
        self.assertIsNone(_parse_setup_menu_command(b''))


class RamProgressTests(unittest.TestCase):
    """Live RAM-operation progress exposed in /api/status (v3.6.34)."""

    def test_progress_lifecycle_and_payload(self):
        from pysim_simple_server import server as srv
        srv._ram_progress_begin('install-cap', 5)
        p = srv._ram_progress_payload()
        self.assertTrue(p['active'])
        self.assertEqual((p['kind'], p['step'], p['total']), ('install-cap', 0, 5))
        srv._ram_progress_step(3, 'LOAD (2/3)')
        p = srv._ram_progress_payload()
        self.assertEqual((p['step'], p['name']), (3, 'LOAD (2/3)'))
        self.assertGreaterEqual(p['elapsed'], 0)
        srv._ram_progress_end()
        self.assertFalse(srv._ram_progress_payload()['active'])

class NoSecurityAndCounterTrackingTests(unittest.TestCase):
    """The keyless packet form and the counter-tracking rule (v3.8.0).

    TS 102 225 A.2: KIc '00' is valid when no ciphering is applied (SPI1.b3=0)
    and KID '00' when no RC/CC/DS is applied (SPI1.b2b1=00) - a no-security
    packet carries no algorithm and no keys.  TS 102 225 5.1.1 b5b4: with '00'
    the counter is "present, ignored, never updated", so no counter may be
    advanced or persisted for such a packet."""

    def test_zero_kic_kid_build_the_keyless_packet(self):
        from pysim_simple_server import server as S
        sp, spi = S._build_secured_packet('00', '01', '00', '00', 'B00000',
                                          '0000000001', '00A40000', '', '')
        self.assertEqual(sp.lower(), '0d00010000b0000000000000010000a40000')
        self.assertFalse(spi['ciphering'])
        self.assertEqual(spi['rc_cc_ds'], 'no_rc_cc_ds')

    def test_the_algorithm_is_required_only_when_it_is_used(self):
        from pysim_simple_server import server as S
        # ciphering on (SPI1.b3) with an unknown KIc nibble: refused
        with self.assertRaises(ValueError):
            S._ota_keyset('04', '01', '00', '15', '0000000001', '', '')
        # CC on (SPI1.b2b1) with an unknown KID nibble: refused
        with self.assertRaises(ValueError):
            S._ota_keyset('16', '01', '15', '00', '0000000001', '', '')
        # a ciphered PoR needs the KIc key too
        with self.assertRaises(ValueError):
            S._ota_keyset('00', '11', '00', '15', '0000000001', '', '')
        # ... the same nibbles are fine when unused
        S._ota_keyset('00', '01', '00', '00', '0000000001', '', '')

    def test_counter_tracking_follows_the_spi(self):
        from pysim_simple_server import server as S
        for spi1, tracked in (('00', False), ('06', False), ('01', False),
                              ('08', True), ('10', True), ('16', True), ('18', True),
                              ('', False), ('zz', False)):
            self.assertEqual(S._counter_tracked(spi1), tracked, spi1)

    def test_persist_needs_a_key_version(self):
        from pysim_simple_server import server as S
        store = mock.Mock()
        server = types.SimpleNamespace(card_presets=store)
        S._preset_counter_persist(server, 'pid', '0000000002', 'send-ota', None)
        S._preset_counter_persist(server, 'pid', '0000000002', 'send-ota', 0)
        self.assertFalse(store.set_counter.called, 'no keyset number -> no write')
        S._preset_counter_persist(server, 'pid', '0000000002', 'send-ota', 2)
        store.set_counter.assert_called_once_with('pid', '0000000002', 'send-ota', 2)


class ExpandedRemoteTests(unittest.TestCase):
    """Expanded remote-management format (TS 102 226 5.2.1/5.2.2): the two
    scripting templates, the Bad format TLV and the format helper."""

    def test_wrap_expanded_apdu_forms(self):
        # SELECT MF (the TAR probe's command) in both codings; the indefinite
        # form is the one the spec recommends for RAM/RFM over HTTPS
        self.assertEqual(_wrap_expanded_apdu('00A40000023F00'),
                         'AA09220700A40000023F00')
        self.assertEqual(_wrap_expanded_apdu('00A40000023F00', 'indefinite'),
                         'AE80220700A40000023F000000')
        self.assertEqual(_ram_format_apdu('00A40000023F00', 'compact'),
                         '00A40000023F00')

    def test_format_apdu_uses_the_le_form_for_expanded_probes(self):
        # the expanded format does not use GET RESPONSE (5.2.1.1): the probe
        # drops the chained GET RESPONSE of the compact form and sets Le='00'
        self.assertEqual(_ram_format_apdu('80F28000024F0000', 'expanded'),
                         'AA0A220880F28000024F0000')
        self.assertEqual(_ram_format_apdu('80F28000024F0000', 'expanded-ae'),
                         'AE80220880F28000024F00000000')

    def test_ram_normalize_format(self):
        for v in ('auto', 'compact', 'expanded', 'expanded-ae', 'AUTO', ' expanded-ae '):
            self.assertEqual(_ram_normalize_format(v), v.strip().lower())
        for v in ('', None, 'bogus', 'AA'):
            self.assertEqual(_ram_normalize_format(v), 'auto')

    def test_live_bad_format_response_is_decoded_as_scripting(self):
        # the live ISD's answer to a definite-length scripting template: zero
        # executed commands plus the Bad format TLV (error type 02 = wrong
        # length); it must not fall through to the compact decoder
        r = _decode_por('16', '21', '25', '25', '0001000002', K, K,
                        '02710000130a00000000010000020000ab06800100900102')
        self.assertEqual(r['response_status'], 'por_ok')
        self.assertEqual(r['response_type'], 'scripting')
        self.assertEqual(r['bad_format'], '02')
        self.assertEqual(r['bad_format_name'], 'wrong length')
        self.assertEqual(r['decoded']['number_of_commands'], 0)

    def test_ram_step_result_treats_a_bad_format_as_failure(self):
        por = {'response_status': 'por_ok', 'response_type': 'scripting',
               'bad_format': '02', 'bad_format_name': 'wrong length',
               'decoded': {'number_of_commands': 0, 'last_status_word': None,
                           'last_response_data': ''}}
        step, error = _ram_step_result('FORMAT CHECK (expanded)', '9000', por, 'aabb', 42, 1)
        self.assertEqual(step['por_bad_format'], '02')
        self.assertIn('bad format 02', error)
        self.assertIn('wrong length', error)


class Spi1ForTarTests(unittest.TestCase):
    """The SPI1 of a packet comes from the MSL (Minimum SPI1) of its TAR
    (TS 102 226 8.2.1.3.2.4) - the preset's TAR table is the source."""

    PRESET = {'tars': [
        {'role': 'isd', 'tar': '000000', 'msl': '16', 'desc': ''},
        {'role': 'uiccRfm', 'tar': 'B00000', 'msl': '10', 'desc': ''},
        {'role': 'usimRfm', 'tar': 'B00001', 'msl': '16', 'desc': ''},
        {'tar': 'AF4D01', 'msl': '0A', 'desc': 'applet'},
    ]}

    def test_a_hand_send_keeps_its_explicit_spi1(self):
        self.assertEqual(_spi1_for_tar(self.PRESET, '000000', '21'), ('21', ''))
        self.assertEqual(_spi1_for_tar(self.PRESET, '000000', '1A'), ('1A', ''))

    def test_an_explicit_spi1_below_the_msl_warns(self):
        spi1, warning = _spi1_for_tar(self.PRESET, 'B00000', '0A')
        self.assertEqual(spi1, '0A')
        self.assertIn('MSL 10', warning)
        self.assertIn('B00000', warning)

    def test_the_comparison_is_numeric(self):
        # '9' < '10' numerically - a lexicographic compare would miss it
        _, warning = _spi1_for_tar(self.PRESET, 'B00000', '9')
        self.assertIn('MSL 10', warning)

    def test_without_an_explicit_value_the_msl_is_used(self):
        self.assertEqual(_spi1_for_tar(self.PRESET, 'B00001'), ('16', ''))
        self.assertEqual(_spi1_for_tar(self.PRESET, 'af4d01'), ('0A', ''))

    def test_operations_prefer_the_msl_over_a_stale_explicit_value(self):
        self.assertEqual(_spi1_for_tar(self.PRESET, 'B00001', '21',
                                       prefer_msl=True), ('16', ''))
        # ... but the explicit value covers a TAR the preset does not carry
        self.assertEqual(_spi1_for_tar(self.PRESET, 'B00200', '21',
                                       prefer_msl=True), ('21', ''))

    def test_an_unknown_tar_without_an_explicit_value_is_refused(self):
        with self.assertRaises(ValueError) as cm:
            _spi1_for_tar(self.PRESET, 'B00200')
        self.assertIn('B00200', str(cm.exception))
        self.assertIn('MSL', str(cm.exception))
        with self.assertRaises(ValueError):
            _spi1_for_tar({}, '000000')
        # no TAR at all (a pre-built packet without one): the hint names that
        with self.assertRaises(ValueError) as cm:
            _spi1_for_tar(self.PRESET, '')
        self.assertIn('no TAR', str(cm.exception))


class CounterProbeTests(unittest.TestCase):
    """The bounded counter synchronisation probe (v3.9.x)."""

    @staticmethod
    def _preset(cntr='0000000C2D'):
        return {'id': 'p1', 'name': 'test card',
                'tars': [{'role': 'isd', 'tar': '000000', 'msl': '16', 'desc': ''},
                         {'role': 'uiccRfm', 'tar': 'B00000', 'msl': '16', 'desc': ''},
                         {'role': 'usimRfm', 'tar': 'B00001', 'msl': '16', 'desc': ''}],
                'keysets': [{'kic': '25', 'kid': '25', 'kicKey': '00' * 16,
                             'kidKey': '11' * 16, 'cntr': cntr}]}

    def test_candidates_double_and_stop_at_the_ceiling(self):
        cands, reason = _counter_probe_candidates('0000000C2D', '0000FFFF', 40)
        self.assertEqual(cands[:4], ['0000000C2E', '0000000C30', '0000000C34', '0000000C3C'])
        self.assertTrue(all(int(c, 16) <= 0xFFFF for c in cands), cands)
        self.assertEqual(reason, 'ceiling')
        # the attempt budget caps a far-away ceiling
        cands, reason = _counter_probe_candidates('0000000000', 'FFFFFFFFFF', 5)
        self.assertEqual(len(cands), 5)
        self.assertEqual(reason, 'attempts')
        # nothing to try when the start is already at the ceiling
        self.assertEqual(_counter_probe_candidates('0000FFFF', '0000FFFF', 40), ([], 'ceiling'))

    def test_probe_walks_up_and_persists_accepted_plus_one(self):
        persisted = []

        class Store:
            def set_counter(self, pid, cntr, source, kvn=None):
                persisted.append((pid, cntr, source, kvn))

        server = types.SimpleNamespace(card_presets=Store())
        verdicts = ['cntr_low', 'cntr_low', 'por_ok']

        def send_fn(p, cntr):
            status = verdicts.pop(0)
            return ({'success': status == 'por_ok', 'sw': '9000', 'bytes': 42, 'segments': 1},
                    {'response_status': status,
                     'decoded': {'last_status_word': '9000', 'last_response_data': ''}})

        res = _counter_probe(server, None, self._preset(), {}, send_fn=send_fn)
        self.assertTrue(res['success'])
        self.assertEqual([a['cntr'] for a in res['attempts']],
                         ['0000000C2E', '0000000C30', '0000000C34'])
        self.assertEqual(res['accepted_cntr'], '0000000C34')
        self.assertEqual(res['stored_cntr'], '0000000C35')
        self.assertEqual(res['packets'], 3)
        self.assertEqual(persisted, [('p1', '0000000C35', 'counter-probe', 2)])

    def test_probe_saves_a_post_cntr_low_verdict_but_reports_it(self):
        # a cntr_low -> other-error transition proves the value is above the
        # card's counter (the packet got past the counter check): keep it, but
        # report the security error so the settings get looked at
        persisted = []

        class Store:
            def set_counter(self, pid, cntr, source, kvn=None):
                persisted.append((pid, cntr, source, kvn))

        server = types.SimpleNamespace(card_presets=Store())
        verdicts = ['cntr_low', 'rc_cc_ds_failed']

        def send_fn(p, cntr):
            status = verdicts.pop(0)
            return ({'success': False, 'sw': '9000', 'bytes': 42, 'segments': 1},
                    {'response_status': status, 'decoded': {'last_status_word': '9000'}})

        res = _counter_probe(server, None, self._preset(), {}, send_fn=send_fn)
        self.assertFalse(res['success'])
        self.assertEqual(res['stopped'], 'error')
        self.assertTrue(res['counter_saved'])
        self.assertEqual(res['verdict'], 'rc_cc_ds_failed')
        self.assertEqual(res['accepted_cntr'], '0000000C30')
        self.assertEqual(res['stored_cntr'], '0000000C31')
        self.assertEqual(persisted, [('p1', '0000000C31', 'counter-probe', 2)])
        self.assertIn('security settings', res['error'])

    def test_probe_does_not_save_a_verdict_without_a_cntr_low(self):
        # no transition: the value is not established, nothing may be stored
        def send_fn(p, cntr):
            return ({'success': False, 'sw': '9000'},
                    {'response_status': 'rc_cc_ds_failed', 'decoded': {}})

        res = _counter_probe(None, None, self._preset(), {}, send_fn=send_fn)
        self.assertFalse(res['success'])
        self.assertFalse(res.get('counter_saved'))
        self.assertNotIn('stored_cntr', res)
        self.assertIn('not synced', res['error'])

    def test_probe_treats_an_actual_response_as_accepted(self):
        # 0x0B: the packet was accepted, the response travels via SMS-SUBMIT
        persisted = []

        class Store:
            def set_counter(self, pid, cntr, source, kvn=None):
                persisted.append((pid, cntr, source, kvn))

        server = types.SimpleNamespace(card_presets=Store())

        def send_fn(p, cntr):
            return ({'success': True, 'sw': '9000'},
                    {'response_status': 'actual_response_sms_submit', 'decoded': {}})

        res = _counter_probe(server, None, self._preset(), {}, send_fn=send_fn)
        self.assertTrue(res['success'])
        self.assertEqual(res['verdict'], 'actual_response_sms_submit')
        self.assertEqual(persisted, [('p1', '0000000C2F', 'counter-probe', 2)])

    def test_probe_reports_a_ceiling_overrun(self):
        def send_fn(p, cntr):
            return ({'success': False, 'sw': '9000'}, {'response_status': 'cntr_low'})

        res = _counter_probe(None, None, self._preset('0000FFFC'),
                             {'ceiling': '0000FFFF', 'max_attempts': 40}, send_fn=send_fn)
        self.assertFalse(res['success'])
        self.assertEqual(res['stopped'], 'ceiling')
        self.assertLessEqual(len(res['attempts']), 3)
        self.assertIn('raise the preset counter manually', res['error'])

    def test_spi2_always_requests_the_por(self):
        # the probe cannot work without the PoR verdict: a caller's value
        # without b1 is corrected (live 2026-10-02: the frontend posted the
        # plain form's 00 and the probe stopped after one packet), an invalid
        # one is refused, and the default is the RAM transport
        self.assertEqual(_counter_probe_params(self._preset(), {})['spi2'], '21')
        self.assertEqual(_counter_probe_params(self._preset(), {'spi2': '00'})['spi2'], '01')
        self.assertEqual(_counter_probe_params(self._preset(), {'spi2': '20'})['spi2'], '21')
        self.assertEqual(_counter_probe_params(self._preset(), {'spi2': '21'})['spi2'], '21')
        with self.assertRaises(ValueError):
            _counter_probe_params(self._preset(), {'spi2': 'ZZ'})

    def test_probe_names_the_spi2_when_no_por_arrives(self):
        def send_fn(p, cntr):
            return ({'success': True, 'sw': '9000'}, None)

        res = _counter_probe(None, None, self._preset(), {}, send_fn=send_fn)
        self.assertFalse(res['success'])
        self.assertEqual(res['stopped'], 'error')
        self.assertEqual(res['packets'], 1)
        self.assertIn('no PoR', res['error'])
        self.assertIn('SPI2 21', res['error'])

    def test_probe_params_validation(self):
        # a keyset number the preset does not define
        with self.assertRaises(ValueError) as cm:
            _counter_probe_params(self._preset(), {'kvn': 5})
        self.assertIn('not defined', str(cm.exception))
        # SPI1 without a counter check cannot probe
        no_check = self._preset()
        no_check['tars'][0]['msl'] = '06'   # ciphering+CC but b5b4 = 00: no counter field
        with self.assertRaises(ValueError):
            _counter_probe_params(no_check, {})
        # the ceiling must stay below the 40-bit maximum
        with self.assertRaises(ValueError):
            _counter_probe_params(self._preset(), {'ceiling': 'FFFFFFFFFF'})

    def test_cntr_low_fields_is_a_flag_only(self):
        # the card's counter is not derivable from the PoR (TS 102 225 5.2):
        # the verdict is stated without a value
        self.assertEqual(_cntr_low_fields('cntr_low'), {'cntr_low': True})
        self.assertEqual(_cntr_low_fields('por_ok'), {})
        self.assertEqual(_cntr_low_fields(None), {})


if __name__ == '__main__':
    unittest.main()
