# coding=utf-8
"""Tests for :mod:`simple_server.scp80` (SCP80 secured packets).

The reference bytes are the vectors the existing server's tests pin
(``tests/test_ota_helpers.py``), copied here as literals so this suite stays
free of any pysim/pysim_simple_server import.  Keys are the public synthetic
material those tests use (pySim's own test vectors); no card-specific keys.
"""

import unittest

from simple_server import scp80

# Synthetic dummy keys (the existing suite's K).
K = '00112233445566778899AABBCCDDEEFF'
# Public sysmocom sample keys from pySim's tests/unittests/test_ota.py.
KIC3 = 'C21DD66ACAC13CB3BC8B331B24AFB57B'
KID3 = '12110C78E678C25408233076AA033615'
# Public synthetic AES keys from the same file.
KIC_AES = '200102030405060708090a0b0c0d0e0f'
KID_AES = '201102030405060708090a0b0c0d0e0f'

APDU = '00a40000023f00'
AES_APDU = '00a40004023f00'

# (spi1, spi2) -> secured packet, pinned against pySim's OtaDialectSms.encode_cmd.
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
# AES-128 vectors (public synthetic keys from pySim's test_ota.py).
AES_REFERENCE_VECTORS = {
    ('16', '19'):
        '00281516192222b000115a47655527e96e832f1a5c698655715d4331454a0d83952c0ed35245706976b1',
    ('12', '09'):
        '001d1512092222b0001100000000110029826122c7a0b79500a40004023f00',
    ('1e', '19'):
        '0028151e192222b0001118b202ee47a3203e7370861c383b4142e704157b36e5c0eb4bb33eb6036cbaf8',
}

# The public vector of pySim's test_cmd_3des_ciphered.
SYSMOKOM_VECTOR = '00180d04193535b00000e3ec80a849b554421276af3883927c20'


class TestSpiFromBytes(unittest.TestCase):
    def test_06_09_ciphered_cc(self):
        self.assertEqual(scp80.spi_from_bytes(0x06, 0x09), {
            'counter': 'no_counter',
            'ciphering': True,
            'rc_cc_ds': 'cc',
            'por_in_submit': False,
            'por_shall_be_ciphered': False,
            'por_rc_cc_ds': 'cc',
            'por': 'por_required',
        })

    def test_16_01_counter_must_be_higher(self):
        spi = scp80.spi_from_bytes(0x16, 0x01)
        self.assertEqual(spi['counter'], 'counter_must_be_higher')
        self.assertTrue(spi['ciphering'])
        self.assertEqual(spi['rc_cc_ds'], 'cc')
        self.assertEqual(spi['por_rc_cc_ds'], 'no_rc_cc_ds')

    def test_02_09_unciphered_cc(self):
        spi = scp80.spi_from_bytes(0x02, 0x09)
        self.assertFalse(spi['ciphering'])
        self.assertEqual(spi['rc_cc_ds'], 'cc')
        self.assertEqual(spi['por_rc_cc_ds'], 'cc')

    def test_04_19_ciphered_no_cc(self):
        spi = scp80.spi_from_bytes(0x04, 0x19)
        self.assertTrue(spi['ciphering'])
        self.assertEqual(spi['rc_cc_ds'], 'no_rc_cc_ds')
        self.assertTrue(spi['por_shall_be_ciphered'])
        self.assertEqual(spi['por_rc_cc_ds'], 'cc')

    def test_por_in_submit_bit(self):
        self.assertTrue(scp80.spi_from_bytes(0x16, 0x21)['por_in_submit'])
        self.assertFalse(scp80.spi_from_bytes(0x16, 0x01)['por_in_submit'])

    def test_hex_strings_are_accepted(self):
        self.assertEqual(scp80.spi_from_bytes('16', '01'),
                         scp80.spi_from_bytes(0x16, 0x01))

    def test_reserved_por_request_is_refused(self):
        # SPI2.b2b1 = 11 is reserved (TS 102 225 5.1.1)
        with self.assertRaises(scp80.Scp80Error):
            scp80.spi_from_bytes(0x16, 0x03)

    def test_every_assigned_combination_round_trips(self):
        for spi1 in range(256):
            for spi2 in range(256):
                if (spi2 & 0x03) == 3:
                    continue
                spi = scp80.spi_from_bytes(spi1, spi2)
                # SPI1.b8-b6 and SPI2.b8-b7 are reserved, so the map carries
                # only the assigned bits (SPI1 b5-b1, SPI2 b6-b1)
                self.assertEqual(scp80.spi_to_bytes(spi),
                                 bytes([spi1 & 0x1F, spi2 & 0x3F]))

    def test_every_assigned_combination_decodes(self):
        for spi1 in range(256):
            for spi2 in range(256):
                if (spi2 & 0x03) == 3:
                    continue
                if (spi1 & 0x03) == 3:
                    # RC/CC/DS = 11 (DS) has no signature length
                    with self.assertRaises(scp80.Scp80Error):
                        scp80.sig_len_for(scp80.spi_from_bytes(spi1, spi2))
                    continue
                spi = scp80.spi_from_bytes(spi1, spi2)
                self.assertEqual(scp80.sig_len_for(spi),
                                 0 if spi['rc_cc_ds'] == 'no_rc_cc_ds'
                                 else 4 if spi['rc_cc_ds'] == 'rc' else 8)


class TestKeyset(unittest.TestCase):
    def test_algorithm_nibbles(self):
        cases = [(('01', '01'), ('single_des', 'single_des')),
                 (('05', '05'), ('triple_des_cbc2', 'triple_des_cbc2')),
                 (('19', '19'), ('triple_des_cbc3', 'triple_des_cbc3')),
                 (('02', '02'), ('aes_cbc', 'aes_cmac'))]
        for (kic, kid), (algo_crypt, algo_auth) in cases:
            ks = scp80.Keyset.from_bytes('16', '01', kic, kid, '0000000001',
                                         K, K)
            self.assertEqual((ks.algo_crypt, ks.algo_auth),
                             (algo_crypt, algo_auth), (kic, kid))
            self.assertEqual((ks.kic_idx, ks.kid_idx),
                             (int(kic, 16) >> 4, int(kid, 16) >> 4))
            self.assertEqual((ks.kic_byte, ks.kid_byte),
                             (int(kic, 16), int(kid, 16)))

    def test_keyset_number_is_the_high_nibble(self):
        ks = scp80.Keyset.from_bytes('16', '01', '15', '15', '0000000001', K, K)
        self.assertEqual((ks.kic_idx, ks.kid_idx), (1, 1))
        self.assertEqual((ks.kic_byte, ks.kid_byte), (0x15, 0x15))
        ks = scp80.Keyset.from_bytes('16', '01', 'A5', 'F5', '0000000001', K, K)
        self.assertEqual((ks.kic_idx, ks.kid_idx), (0xA, 0xF))

    def test_00_bytes_are_implicit_when_no_security_is_used(self):
        ks = scp80.Keyset.from_bytes('00', '01', '00', '00', '1', '', '')
        self.assertEqual((ks.algo_crypt, ks.algo_auth), ('implicit', 'implicit'))
        self.assertEqual((ks.kic_byte, ks.kid_byte), (0, 0))

    def test_unknown_nibble_is_refused_when_used(self):
        # ciphering on (SPI1 04) needs a ciphering algorithm
        with self.assertRaises(scp80.Scp80Error):
            scp80.Keyset.from_bytes('04', '01', '03', '15', '1', K, K)
        # RC/CC/DS on (SPI1 02) needs an authentication algorithm
        with self.assertRaises(scp80.Scp80Error):
            scp80.Keyset.from_bytes('02', '01', '15', '03', '1', K, K)

    def test_unknown_nibble_is_refused_even_when_unused(self):
        # a non-zero nibble the spec does not assign never passes
        with self.assertRaises(scp80.Scp80Error):
            scp80.Keyset.from_bytes('00', '01', '04', '15', '1', K, K)

    def test_counter_from_hex(self):
        ks = scp80.Keyset.from_bytes('16', '01', '15', '15', '0000000011', K, K)
        self.assertEqual(ks.cntr, 0x11)
        self.assertEqual(scp80.Keyset.from_bytes('16', '01', '15', '15', '', K, K).cntr, 0)

    def test_algorithms_carry_the_key(self):
        ks = scp80.Keyset.from_bytes('06', '09', '15', '15', '1', K, K)
        self.assertEqual(ks.crypt.blocksize, 8)
        self.assertEqual(ks.crypt.key, bytes.fromhex(K))
        self.assertEqual(ks.auth.sig_len, 8)
        self.assertEqual(ks.auth.key, bytes.fromhex(K))

    def test_implicit_algorithms_cannot_be_instantiated(self):
        ks = scp80.Keyset.from_bytes('00', '01', '00', '00', '1', '', '')
        with self.assertRaises(scp80.Scp80Error):
            ks.crypt
        with self.assertRaises(scp80.Scp80Error):
            ks.auth


class TestCrypto(unittest.TestCase):
    def test_padding_rules(self):
        # TS 102 225 5.1.1: the padding is 00 octets up to the block size, and
        # an aligned length gets no full block of padding.
        self.assertEqual(scp80.get_padding(16, 8), b'')
        self.assertEqual(scp80.get_padding(17, 8), b'\x00' * 7)
        self.assertEqual(scp80.get_padding(1, 16), b'\x00' * 15)
        self.assertEqual(scp80.pad_to_multiple(b'\x01' * 8, 8), b'\x01' * 8)

    def test_encrypt_pads_decrypt_does_not_unpad(self):
        algo = scp80.DesCrypto(bytes.fromhex(K)[:8])
        self.assertEqual(len(algo.encrypt(b'\x01' * 9)), 16)
        self.assertEqual(len(algo.decrypt(algo.encrypt(b'\x01' * 9))), 16)
        self.assertEqual(algo.decrypt(algo.encrypt(b'\x01' * 9))[:9], b'\x01' * 9)

    def test_block_sizes(self):
        self.assertEqual(scp80.DesCrypto(bytes.fromhex(K)[:8]).blocksize, 8)
        self.assertEqual(scp80.Des3Crypto(bytes(24)).blocksize, 8)
        self.assertEqual(scp80.AesCrypto(bytes(16)).blocksize, 16)
        self.assertEqual(scp80.AesCmac(bytes(16)).sig_len, 8)

    def test_key_lengths_are_validated(self):
        with self.assertRaises(scp80.Scp80Error):
            scp80.DesCrypto(b'\x00' * 16).encrypt(b'\x00' * 8)
        with self.assertRaises(scp80.Scp80Error):
            scp80.Des3Crypto(b'\x00' * 8).encrypt(b'\x00' * 8)
        with self.assertRaises(scp80.Scp80Error):
            scp80.AesCrypto(b'\x00' * 8).encrypt(b'\x00' * 16)

    def test_cbc_mac_is_the_last_block(self):
        algo = scp80.DesCbcMac(bytes.fromhex(K)[:8])
        sig = algo.sign(b'\x00' * 16)
        self.assertEqual(len(sig), 8)
        algo.check_sig(b'\x00' * 16, sig)
        with self.assertRaises(scp80.Scp80Error):
            algo.check_sig(b'\x00' * 16, b'\x00' * 8)

    def test_cmac_does_not_pad(self):
        # AES-CMAC needs no block padding of its own (blocksize 1)
        algo = scp80.AesCmac(bytes(16))
        self.assertEqual(len(algo.sign(b'\x01')), 8)
        self.assertEqual(algo.sign(b'\x01'), algo.sign(b'\x01'))


class TestEncodeCmd(unittest.TestCase):
    def test_ciphered_cc_vector(self):
        out, spi = scp80.build_secured_packet('06', '09', '15', '15', 'b00000',
                                              '0000000001', APDU, K, K)
        self.assertEqual(out.hex(), REFERENCE_VECTORS[('06', '09')])
        self.assertTrue(spi['ciphering'])
        self.assertEqual(spi['rc_cc_ds'], 'cc')

    def test_ciphered_counter_must_be_higher_vector(self):
        out, _ = scp80.build_secured_packet('16', '01', '15', '15', 'b00000',
                                            '0000000001', APDU, K, K)
        self.assertEqual(out.hex(), REFERENCE_VECTORS[('16', '01')])

    def test_unciphered_cc_vector(self):
        out, _ = scp80.build_secured_packet('02', '09', '15', '15', 'b00000',
                                            '0000000001', APDU, K, K)
        self.assertEqual(out.hex(), REFERENCE_VECTORS[('02', '09')])
        # CPL counts CHL..secured data (29 octets here), not len(out) - 2
        self.assertEqual(out[:2].hex(), '001d')
        self.assertEqual(len(out), 31)

    def test_unciphered_rc_vector(self):
        out, spi = scp80.build_secured_packet('01', '09', '15', '15', 'b00000',
                                              '0000000001', APDU, K, K)
        self.assertEqual(out.hex(), REFERENCE_VECTORS[('01', '09')])
        self.assertEqual(spi['rc_cc_ds'], 'rc')

    def test_sysmocom_3des_vector(self):
        out, _ = scp80.build_secured_packet('04', '19', '35', '35', 'b00000',
                                            '0000000000', APDU, KIC3, KID3)
        self.assertEqual(out.hex(), SYSMOKOM_VECTOR)

    def test_aes128_ciphered_cc_vector(self):
        out, _ = scp80.build_secured_packet('16', '19', '22', '22', 'b00011',
                                            '0000000011', AES_APDU, KIC_AES,
                                            KID_AES)
        self.assertEqual(out.hex(), AES_REFERENCE_VECTORS[('16', '19')])

    def test_aes128_unciphered_cc_vector(self):
        out, _ = scp80.build_secured_packet('12', '09', '22', '22', 'b00011',
                                            '0000000011', AES_APDU, KIC_AES,
                                            KID_AES)
        self.assertEqual(out.hex(), AES_REFERENCE_VECTORS[('12', '09')])

    def test_aes128_counter_plus_one_vector(self):
        out, spi = scp80.build_secured_packet('1e', '19', '22', '22', 'b00011',
                                              '0000000011', AES_APDU, KIC_AES,
                                              KID_AES)
        self.assertEqual(out.hex(), AES_REFERENCE_VECTORS[('1e', '19')])
        self.assertEqual(spi['counter'], 'counter_must_be_one_higher')

    def test_no_security_packet(self):
        # KIc/KID 00 with SPI1 00 (no ciphering, no counter, no RC/CC/DS)
        out, _ = scp80.build_secured_packet('00', '01', '00', '00', 'b00000',
                                            '0000000001', APDU, '', '')
        # CHL 13, SPI 00 01, KIc 00, KID 00, TAR b00000, CNTR, PCNTR 00, APDU
        self.assertEqual(out.hex(),
                         '0d000100' + '00b00000' + '0000000001' + '00' + APDU)

    def test_header_layout_of_the_cc_form(self):
        keyset = scp80.Keyset.from_bytes('16', '01', '15', '15', '0000000001',
                                         K, K)
        spi = scp80.spi_from_bytes(0x16, 0x01)
        out = scp80.encode_cmd(keyset, spi, bytes.fromhex('b00000'),
                               bytes.fromhex(APDU))
        self.assertEqual(out[:2].hex(), '0020')       # CPL
        self.assertEqual(out[2], 0x15)                # CHL = 13 + 8
        self.assertEqual(out[3:5].hex(), '1601')      # SPI1 SPI2
        self.assertEqual(out[5:7].hex(), '1515')      # KIc KID
        self.assertEqual(out[7:10].hex(), 'b00000')   # TAR

    def test_extended_packet_is_not_clamped(self):
        apdu = '80e80000f0' + '00' * 240 + '00'
        out, _ = scp80.build_secured_packet('16', '01', '15', '15', 'b00000',
                                            '0000000001', apdu, K, K)
        self.assertGreater(len(out), 140)

    def test_ds_is_refused(self):
        # RC/CC/DS = 11 (DS) has no signature scheme in the specs
        with self.assertRaises(scp80.Scp80Error):
            scp80.build_secured_packet('07', '09', '15', '15', 'b00000',
                                       '0000000001', APDU, K, K)

    def test_tar_must_be_three_octets(self):
        keyset = scp80.Keyset.from_bytes('16', '01', '15', '15', '1', K, K)
        spi = scp80.spi_from_bytes(0x16, 0x01)
        with self.assertRaises(scp80.Scp80Error):
            scp80.encode_cmd(keyset, spi, b'\xb0\x00', b'\x00')


class TestSplit(unittest.TestCase):
    def test_single_sm_is_one_part(self):
        self.assertEqual(scp80.split_secured_packet(b'\x01' * 137), [b'\x01' * 137])

    def test_first_and_next_capacities(self):
        pkt = bytes(range(256)) * 2
        parts = scp80.split_secured_packet(pkt)
        self.assertEqual(len(parts[0]), scp80.SCP80_FIRST_BYTES)
        for part in parts[1:-1]:
            self.assertEqual(len(part), scp80.SCP80_NEXT_BYTES)
        self.assertLessEqual(len(parts[-1]), scp80.SCP80_NEXT_BYTES)
        self.assertEqual(b''.join(parts), pkt)

    def test_without_cpi(self):
        pkt = bytes(range(256))
        parts = scp80.split_secured_packet(pkt, include_cpi=False)
        # without the CPI IE every part's UDH is the concat IE only
        self.assertEqual(len(parts[0]), scp80.SCP80_NEXT_BYTES)
        self.assertEqual(b''.join(parts), pkt)
        self.assertEqual(scp80.split_secured_packet(b'\x00' * 140,
                                                    include_cpi=False),
                         [b'\x00' * 140])

    def test_constants_match_the_udh_sizes(self):
        # 140 = UDHL(1) + CPI(2) + 137 / UDHL(1) + concat(5) + CPI(2) + 132
        self.assertEqual(scp80.SCP80_SINGLE_BYTES, 140 - 1 - 2)
        self.assertEqual(scp80.SCP80_FIRST_BYTES, 140 - 1 - 5 - 2)
        self.assertEqual(scp80.SCP80_NEXT_BYTES, 140 - 1 - 5)
        self.assertEqual(scp80.SCP80_CONCAT_REF, 0x01)
        self.assertEqual(scp80.SCP80_MAX_SEGMENTS, 5)


# PoR vectors: the whole response packet, as the card delivers it.
POR_PLAINTEXT = '027100000e0ab0000000000000010000016e00'
POR_SIGNED = '027100001612b000110000000000000055f47118381175fb01612f'
POR_CIPHERED = ('027100001c12b000119660ebdb81be189b5e4389e9e7ab2bc0954f'
                '963ad869ed7c')
POR_NO_CC = '027100000e0ab000110000000000000001612f'
POR_CNTR_LOW = '027100000b0ab0000000000000070002'
POR_AES_CIPHERED = ('027100002412b00011ebc6b497e2cad7aedf36ace0e3a29b3885'
                    '3f0fe9ccde81913be5702b73abce1f')


def _response(spi1, spi2, kic, kid, cntr, kic_key, kid_key, data, cmd_len=None):
    keyset = scp80.Keyset.from_bytes(spi1, spi2, kic, kid, cntr, kic_key, kid_key)
    return scp80.decode_resp(keyset, scp80.spi_from_bytes(spi1, spi2), data)


class TestDecodeResp(unittest.TestCase):
    def test_plaintext_without_check(self):
        res = _response('02', '01', '15', '15', '0000000001', K, K,
                        POR_PLAINTEXT)
        self.assertEqual(res.response_status, 'por_ok')
        self.assertEqual(res.tar_hex, 'B00000')
        self.assertEqual(res.cntr_hex, '0000000001')
        self.assertEqual(res.pcntr, 0)
        self.assertEqual(res.rpl, 14)
        self.assertEqual(res.rhl, 10)
        self.assertEqual(res.cc_rc, b'')
        self.assertEqual(res.secured_hex, '016E00')
        self.assertEqual(res.raw.hex(), POR_PLAINTEXT)

    def test_signed_cc_is_verified(self):
        res = _response('06', '09', '35', '35', '0000000001', KIC3, KID3,
                        POR_SIGNED)
        self.assertEqual(res.response_status, 'por_ok')
        self.assertEqual(res.tar_hex, 'B00011')
        self.assertEqual(res.cc_rc_hex, '55F47118381175FB')
        self.assertEqual(res.rhl, 18)
        self.assertEqual(res.secured_hex, '01612F')

    def test_ciphered_cc_is_verified_and_unpadded(self):
        res = _response('06', '19', '35', '35', '0000000001', KIC3, KID3,
                        POR_CIPHERED)
        self.assertEqual(res.response_status, 'por_ok')
        self.assertEqual(res.pcntr, 6)
        self.assertEqual(res.secured_hex, '01612F')
        self.assertEqual(res.rhl, 18)

    def test_aes_ciphered_cc(self):
        res = _response('06', '19', '22', '22', '0000000001', KIC_AES, KID_AES,
                        POR_AES_CIPHERED)
        self.assertEqual(res.response_status, 'por_ok')
        self.assertEqual(res.pcntr, 14)
        self.assertEqual(res.secured_hex, '016132')

    def test_no_signature_present(self):
        res = _response('06', '01', '35', '35', '0000000001', KIC3, KID3,
                        POR_NO_CC)
        self.assertEqual(res.response_status, 'por_ok')
        self.assertEqual(res.secured_hex, '01612F')

    def test_cntr_low(self):
        res = _response('02', '01', '15', '15', '0000000001', K, K,
                        POR_CNTR_LOW)
        self.assertEqual(res.response_status, 'cntr_low')
        self.assertEqual(res.cntr_hex, '0000000007')
        self.assertEqual(res.secured_data, b'')

    def test_bad_cc_raises(self):
        tampered = POR_SIGNED[:-4] + '02' + POR_SIGNED[-2:]
        with self.assertRaises(scp80.Scp80Error):
            _response('06', '09', '35', '35', '0000000001', KIC3, KID3,
                      tampered)

    def test_a_signature_is_refused_when_none_was_requested(self):
        with self.assertRaises(scp80.Scp80Error):
            _response('06', '01', '35', '35', '0000000001', KIC3, KID3,
                      POR_SIGNED)

    def test_a_misaligned_ciphered_packet_is_an_scp80_error(self):
        # SPI2 19 asks for a ciphered response; 7 octets from CNTR on are not
        # a DES block multiple, so Cryptodome raises ValueError - the module's
        # contract is Scp80Error for a malformed packet.
        with self.assertRaises(scp80.Scp80Error) as caught:
            _response('06', '19', '35', '35', '0000000001', KIC3, KID3,
                      '027100000d0ab0000001020304050607')
        self.assertIn('decipher', str(caught.exception))

    def test_malformed_packets_raise(self):
        for bad in ['', '00', '00027100000e0a', '027100000e0ab00000',
                    '027101000e0ab000110000000000000001612f']:
            with self.assertRaises(scp80.Scp80Error, msg=bad):
                _response('02', '01', '15', '15', '0000000001', K, K, bad)

    def test_hex_and_bytes_input_agree(self):
        as_bytes = _response('02', '01', '15', '15', '0000000001', K, K,
                             bytes.fromhex(POR_PLAINTEXT))
        self.assertEqual(as_bytes.secured_hex, '016E00')

    def test_dict_style_access(self):
        res = _response('02', '01', '15', '15', '0000000001', K, K,
                        POR_PLAINTEXT)
        self.assertEqual(res['response_status'], 'por_ok')
        self.assertEqual(res['secured_data'], b'\x01\x6e\x00')
        with self.assertRaises(KeyError):
            res['nope']

    def test_rc_verification(self):
        # Self-consistency of the RC path only: the RC is the CRC-32 of the
        # same range the CC covers.  The corpus has no card RC vector, so this
        # pins our own coverage, not a card's behaviour.
        import zlib
        udh = bytes.fromhex('027100')
        header = (bytes.fromhex('000e') + b'\x0e' + bytes.fromhex('b00011')
                  + bytes.fromhex('0000000000') + b'\x00' + b'\x00')
        secured = bytes.fromhex('01612f')
        rc = zlib.crc32(udh + header + secured) & 0xffffffff
        packet = udh + header + rc.to_bytes(4, 'big') + secured
        # SPI2 05: the response RC/CC/DS is 'rc', the PoR is required.
        res = _response('02', '05', '15', '15', '0000000001', K, K, packet)
        self.assertEqual(res.response_status, 'por_ok')
        self.assertEqual(res.rhl, 14)
        self.assertEqual(res.cc_rc, rc.to_bytes(4, 'big'))
        self.assertEqual(res.secured_hex, '01612F')
        broken = packet[:-1] + bytes([packet[-1] ^ 0xFF])
        with self.assertRaises(scp80.Scp80Error):
            _response('02', '05', '15', '15', '0000000001', K, K, broken)


class TestParseCompact(unittest.TestCase):
    def test_compact_response(self):
        # TS 102 226 5.1.2 Table 5.1: count, the last status word, data
        self.assertEqual(scp80.parse_compact(bytes.fromhex('016E00')),
                         {'number_of_commands': 1,
                          'last_status_word': '6e00',
                          'last_response_data': ''})
        self.assertEqual(scp80.parse_compact(bytes.fromhex('02612f41')),
                         {'number_of_commands': 2,
                          'last_status_word': '612f',
                          'last_response_data': '41'})

    def test_too_short_is_not_a_compact_response(self):
        self.assertIsNone(scp80.parse_compact(b''))
        self.assertIsNone(scp80.parse_compact(b'\x01'))
        self.assertIsNone(scp80.parse_compact(b'\x01\x6e'))

    def test_a_count_above_the_command_length_is_not_compact(self):
        # `80 01 10` cannot answer a one-byte command script (live 2026-10-04)
        self.assertIsNone(scp80.parse_compact(bytes.fromhex('800110'), cmd_len=1))
        self.assertIsNotNone(scp80.parse_compact(bytes.fromhex('800110')))
        self.assertIsNone(scp80.parse_compact(bytes.fromhex('02612f41'), cmd_len=1))

    def test_a_long_script_keeps_the_compact_parse(self):
        self.assertEqual(scp80.parse_compact(bytes.fromhex('016A86'), cmd_len=68)
                         ['last_status_word'], '6a86')


class TestCounters(unittest.TestCase):
    def test_counter_tracking_follows_spi1(self):
        for spi1, expected in [('00', False), ('08', True), ('10', True),
                               ('18', True), ('04', False), ('16', True)]:
            self.assertEqual(scp80.counter_tracked(spi1), expected, spi1)

    def test_counter_tracking_of_bad_input(self):
        self.assertFalse(scp80.counter_tracked('zz'))
        self.assertFalse(scp80.counter_tracked(None))

    def test_increment(self):
        self.assertEqual(scp80.increment_counter_hex('0000000001'), '0000000002')
        self.assertEqual(scp80.increment_counter_hex('0000000009'), '000000000A')
        self.assertEqual(scp80.increment_counter_hex('000000000F'), '0000000010')

    def test_increment_wraps_at_forty_bits(self):
        self.assertEqual(scp80.increment_counter_hex('FFFFFFFFFF'), '0000000000')
        self.assertEqual(scp80.increment_counter_hex('0000FFFFFF'), '0001000000')

    def test_increment_keeps_the_width(self):
        self.assertEqual(scp80.increment_counter_hex('01'), '02')
        self.assertEqual(scp80.increment_counter_hex('ff'), '00')
        self.assertEqual(scp80.increment_counter_hex('abc'), 'ABD')

    def test_increment_of_a_bad_value_is_echoed(self):
        self.assertEqual(scp80.increment_counter_hex('zz'), 'zz')
        self.assertEqual(scp80.increment_counter_hex(''), '')


if __name__ == '__main__':
    unittest.main()
