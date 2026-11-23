# coding=utf-8
"""Tests for the pysim-independent ``simple_server`` foundation modules.

No card reader is needed: the transport is driven through an injected fake
connection and a patched ``smartcard.System.readers``.
"""

import unittest
from unittest import mock

from simple_server import text
from simple_server import tlv
from simple_server import transport
from simple_server import util
from simple_server import server as server_module


class AParse(object):
    """A class tree for all_subclasses()."""


class BParsed(AParse):
    pass


class CParsed(BParsed):
    pass


class DParsed(CParsed):
    pass


class UtilTests(unittest.TestCase):
    def test_h2b_accepts_internal_whitespace(self):
        self.assertEqual(util.h2b('deadbeef'), b'\xde\xad\xbe\xef')
        self.assertEqual(util.h2b(' de ad\nbe\tef '), b'\xde\xad\xbe\xef')
        self.assertEqual(util.h2b('   '), b'')

    def test_b2h_is_lowercase(self):
        self.assertEqual(util.b2h(b'\xDE\xAD\xBE\xEF'), 'deadbeef')
        self.assertEqual(util.b2h(bytearray(b'\x90\x00')), '9000')

    def test_rpad(self):
        self.assertEqual(util.rpad('12', 4), '12ff')
        self.assertEqual(util.rpad('1234', 4), '1234')
        self.assertEqual(util.rpad('12', 4, '0'), '1200')
        self.assertEqual(util.rpad('', 2), 'ff')

    def test_all_subclasses_includes_indirect_ones(self):
        self.assertEqual(util.all_subclasses(AParse), {BParsed, CParsed, DParsed})
        self.assertEqual(util.all_subclasses(DParsed), set())

    def test_sw_matches_exact_and_masked(self):
        self.assertTrue(util.sw_matches('9000', '9000'))
        self.assertTrue(util.sw_matches('9000', '9???'))
        self.assertTrue(util.sw_matches('91c3', '9???'))
        self.assertTrue(util.sw_matches('9f10', '9???'))
        self.assertTrue(util.sw_matches('6a82', '6A??'))
        self.assertFalse(util.sw_matches('6a82', '9???'))
        self.assertFalse(util.sw_matches('9000', '9001'))
        # Length mismatch and bytes arguments are handled too.
        self.assertFalse(util.sw_matches('900', '9???'))
        self.assertTrue(util.sw_matches(b'\x61\x03', '61??'))


class CommandApduTests(unittest.TestCase):
    def test_case1(self):
        self.assertEqual(util.parse_command_apdu(bytes.fromhex('00a40000')),
                         (1, 0, 0, b''))

    def test_case2(self):
        self.assertEqual(util.parse_command_apdu(bytes.fromhex('00b0000004')),
                         (2, 0, 4, b''))
        # Le == 00 means 256.
        self.assertEqual(util.parse_command_apdu(bytes.fromhex('00b0000000')),
                         (2, 0, 256, b''))

    def test_case3(self):
        self.assertEqual(util.parse_command_apdu(bytes.fromhex('00a40004023f00')),
                         (3, 2, 0, b'\x3f\x00'))

    def test_case4(self):
        self.assertEqual(
            util.parse_command_apdu(bytes.fromhex('00a40004023f0000')),
            (4, 2, 256, b'\x3f\x00'))
        self.assertEqual(
            util.parse_command_apdu(bytes.fromhex('00a40004023f0010')),
            (4, 2, 16, b'\x3f\x00'))

    def test_lc_zero_means_256(self):
        data = bytes(256)
        self.assertEqual(util.parse_command_apdu(b'\x00\xa4\x00\x00\x00' + data),
                         (3, 256, 0, data))
        self.assertEqual(
            util.parse_command_apdu(b'\x00\xa4\x00\x00\x00' + data + b'\x10'),
            (4, 256, 16, data))

    def test_invalid_lengths_raise(self):
        for raw in (b'', b'\x00\xa4\x00', bytes.fromhex('00a40004023f000000')):
            with self.assertRaises(ValueError):
                util.parse_command_apdu(raw)


class LengthTests(unittest.TestCase):
    def test_encode_ber_len(self):
        self.assertEqual(tlv.encode_ber_len(0), b'\x00')
        self.assertEqual(tlv.encode_ber_len(127), b'\x7f')
        self.assertEqual(tlv.encode_ber_len(128), b'\x81\x80')
        self.assertEqual(tlv.encode_ber_len(255), b'\x81\xff')
        self.assertEqual(tlv.encode_ber_len(256), b'\x82\x01\x00')
        self.assertEqual(tlv.encode_ber_len(0x1234), b'\x82\x12\x34')
        with self.assertRaises(ValueError):
            tlv.encode_ber_len(-1)

    def test_parse_ber_len(self):
        self.assertEqual(tlv.parse_ber_len(b'\x05', 0), (5, 1))
        self.assertEqual(tlv.parse_ber_len(b'\x81\x82', 0), (130, 2))
        self.assertEqual(tlv.parse_ber_len(b'\x82\x12\x34', 0), (0x1234, 3))
        # Truncated / indefinite forms yield no usable length.
        self.assertEqual(tlv.parse_ber_len(b'\x82\x12', 0), (0, 1))
        self.assertEqual(tlv.parse_ber_len(b'', 0), (0, 0))


class TlvObjectTests(unittest.TestCase):
    def test_short_form_round_trip(self):
        obj = tlv.BerTlv(tag=0x8B, value=b'\x01\x02')
        self.assertEqual(obj.to_tlv(), b'\x8b\x02\x01\x02')
        self.assertEqual(obj.to_bytes(), b'\x01\x02')
        parsed = tlv.BerTlv.from_bytes(obj.to_tlv())
        self.assertEqual((parsed.tag, parsed.value), (0x8B, b'\x01\x02'))

    def test_long_form_128_and_above(self):
        value = b'\x5a' * 130
        obj = tlv.BerTlv(tag=0x8B, value=value)
        encoded = obj.to_tlv()
        self.assertEqual(encoded[:3], b'\x8b\x81\x82')
        self.assertEqual(encoded[3:], value)
        self.assertEqual(tlv.BerTlv.from_bytes(encoded).value, value)

        value = b'\x5a' * 300
        obj = tlv.BerTlv(tag=0x8B, value=value)
        encoded = obj.to_tlv()
        self.assertEqual(encoded[:4], b'\x8b\x82\x01\x2c')
        self.assertEqual(tlv.BerTlv.from_bytes(encoded).value, value)

    def test_multi_octet_tag(self):
        obj = tlv.BerTlv(tag=0xBF43, value=b'\x01')
        self.assertEqual(obj.to_tlv(), b'\xbf\x43\x01\x01')
        self.assertEqual(tlv.ber_tlv_list(b'\xbf\x43\x01\x01'),
                         [(0xBF43, b'\x01')])
        parsed = tlv.BerTlv.from_bytes(b'\xbf\x43\x01\x01')
        self.assertEqual((parsed.tag, parsed.value), (0xBF43, b'\x01'))

    def test_bytes_tag_is_verbatim(self):
        self.assertEqual(tlv.encode_tag(b'\xbf\x43'), b'\xbf\x43')
        self.assertEqual(tlv.BerTlv(tag=b'\xbf\x43', value=b'').to_tlv(),
                         b'\xbf\x43\x00')

    def test_nested_children(self):
        inner1 = tlv.BerTlv(tag=0x80, value=b'\x01\x02')
        inner2 = tlv.BerTlv(tag=0x82, value=b'\x83\x02\x2f\x06')
        outer = tlv.BerTlv(tag=0xA5, children=[inner1, inner2])
        body = inner1.to_tlv() + inner2.to_tlv()
        self.assertEqual(outer.to_bytes(), body)
        self.assertEqual(outer.to_tlv(), b'\xa5' + bytes([len(body)]) + body)
        parsed, rest = tlv.BerTlv.parse(outer.to_tlv())
        self.assertEqual((parsed.tag, parsed.value, rest), (0xA5, body, b''))

    def test_subclass_and_constructor_tags(self):
        class Result(tlv.ComprTlv, tag=0x03):
            pass

        self.assertEqual(Result(value=b'\x00').to_tlv(), b'\x03\x01\x00')
        # The constructor argument wins over the class tag.
        self.assertEqual(Result(tag=0x83, value=b'\x00').to_tlv(),
                         b'\x83\x01\x00')

    def test_comprehension_tlv_is_one_octet(self):
        obj = tlv.ComprTlv(tag=0xD0, children=[
            tlv.ComprTlv(tag=0x81, value=b'\x03\x25\x00'),
        ])
        self.assertEqual(obj.to_tlv(), b'\xd0\x05\x81\x03\x03\x25\x00')
        # 0xBF is a plain one-octet comprehension tag (bit 8 is the CR flag).
        self.assertEqual(tlv.ComprTlv(tag=0xBF, value=b'').to_tlv(),
                         b'\xbf\x00')
        parsed = tlv.ComprTlv.from_bytes(b'\xbf\x01\x00')
        self.assertEqual((parsed.tag, parsed.value), (0xBF, b'\x00'))

    def test_comprehension_tlv_limits(self):
        # 128..255 must use the BER long form (TS 101 220 §7.1.2 Table 7.6);
        # 65535 is the cap (a longer value is refused).
        self.assertEqual(tlv.ComprTlv(tag=0x8B, value=b'\x00' * 128).to_tlv()[:3],
                         b'\x8b\x81\x80')
        self.assertEqual(tlv.ComprTlv(tag=0x8B, value=b'\x00' * 255).to_tlv()[:3],
                         b'\x8b\x81\xff')
        self.assertEqual(tlv.ComprTlv(tag=0x8B, value=b'\x00' * 256).to_tlv()[:4],
                         b'\x8b\x82\x01\x00')
        with self.assertRaises(ValueError):
            tlv.ComprTlv(tag=0x01, value=b'\x00' * 65536).to_tlv()
        with self.assertRaises(ValueError):
            tlv.ComprTlv(tag=0xBF43, value=b'').to_tlv()

    def test_tag_required_and_value_vs_children(self):
        with self.assertRaises(ValueError):
            tlv.BerTlv()
        with self.assertRaises(ValueError):
            tlv.BerTlv(tag=0x80, value=b'\x00',
                       children=[tlv.BerTlv(tag=0x81, value=b'')])

    def test_from_bytes_rejects_trailing_bytes(self):
        with self.assertRaises(ValueError):
            tlv.BerTlv.from_bytes(b'\x8b\x01\x00\xff')


class TlvSequenceTests(unittest.TestCase):
    def test_ber_tlv_list(self):
        data = b'\x80\x01\xaa\x82\x02\x11\x22'
        self.assertEqual(tlv.ber_tlv_list(data),
                         [(0x80, b'\xaa'), (0x82, b'\x11\x22')])

    def test_ber_tlv_list_stops_on_truncated_value(self):
        self.assertEqual(tlv.ber_tlv_list(b'\x8b\x05\x01\x02'), [])
        self.assertEqual(tlv.ber_tlv_list(b'\x80\x01\xaa' + b'\x8b\x05\x01'),
                         [(0x80, b'\xaa')])
        self.assertEqual(tlv.ber_tlv_list(b''), [])

    def test_ber_tlv_list_stops_on_a_truncated_length(self):
        # An 81/82 continuation without its octets carries no usable length, so
        # no (tag, b'') entry is invented (the docstring's "stops at the first
        # truncated TLV").
        self.assertEqual(tlv.ber_tlv_list(b'\x8b\x81'), [])
        self.assertEqual(tlv.ber_tlv_list(b'\x8b\x82\x01'), [])
        self.assertEqual(tlv.ber_tlv_list(b'\x80\x80'), [])
        self.assertEqual(tlv.ber_tlv_list(b'\x80\x01\xaa\x8b\x81'),
                         [(0x80, b'\xaa')])

    def test_comprehension_data_keeps_one_octet_tags(self):
        # With multi_byte_tags=False a 0xBF octet stays tag 0xBF.
        data = b'\xbf\x01\x00\x01\x01\x01'
        self.assertEqual(tlv.ber_tlv_list(data, multi_byte_tags=False),
                         [(0xBF, b'\x00'), (0x01, b'\x01')])

    def test_tlv_map_first_occurrence_wins(self):
        self.assertEqual(tlv.tlv_map(b'\x80\x01\x01\x80\x01\x02'),
                         {0x80: b'\x01'})

    def test_parse_one_returns_the_rest(self):
        self.assertEqual(tlv.parse_one(b'\x80\x02\xaa\xbb\xff'),
                         (0x80, 2, b'\xaa\xbb', b'\xff'))
        self.assertEqual(tlv.parse_one(b'\x80\x01\xaa'),
                         (0x80, 1, b'\xaa', b''))

    def test_parse_one_rejects_truncated(self):
        for raw in (b'\x80', b'\x80\x05\xaa', b'\x82', b'\x80\x81'):
            with self.assertRaises(ValueError):
                tlv.parse_one(raw)


class Gsm7Tests(unittest.TestCase):
    def test_pack_hello_vector(self):
        self.assertEqual(text.pack_gsm7(b'hello'), b'\xe8\x32\x9b\xfd\x06')

    def test_round_trip(self):
        for raw in (b'', b'hello', b'Test', b'12345', b'1234567', b'@'):
            self.assertEqual(text.unpack_gsm7(text.pack_gsm7(raw)), raw,
                             'round trip of %r' % raw)

    def test_fill_septet_is_dropped(self):
        # Seven real septets pack into seven octets; unpacking those octets
        # would expose the zero fill septet, which is dropped.
        self.assertEqual(text.unpack_gsm7(text.pack_gsm7(b'1234567')),
                         b'1234567')


class StkTextTests(unittest.TestCase):
    def test_all_ff_is_empty(self):
        self.assertEqual(text.decode_stk_text(b'\xff'), '')
        self.assertEqual(text.decode_stk_text(b'\xff\xff\xff'), '')
        self.assertEqual(text.decode_stk_text(b''), '')

    def test_default_alphabet_is_one_octet_per_char(self):
        # The live card's SET UP MENU title "Alfa Mobile" (8-bit septets).
        self.assertEqual(text.decode_stk_text(
            bytes.fromhex('416c6661204d6f62696c65')), 'Alfa Mobile')
        self.assertEqual(text.decode_stk_text(b'hello'), 'hello')

    def test_packed_form(self):
        self.assertEqual(text.decode_stk_text(text.pack_gsm7(b'hello'),
                                              packed=True), 'hello')

    def test_annex_a_variant1(self):
        raw = b'\x80' + 'Hi'.encode('utf-16-be')
        self.assertEqual(text.decode_stk_text(raw), 'Hi')

    def test_annex_a_variant2_live_card_item(self):
        # The live card's SET UP MENU item text (UCS2 variant 2): the count
        # 0x12, base pointer 0x08 << 7 and the Cyrillic half-page offsets.
        self.assertEqual(text.decode_stk_text(bytes.fromhex(
            '8112089db0c1c2c0beb9bab82f53657474696e6773')),
            '\u041d\u0430\u0441\u0442\u0440\u043e\u0439\u043a\u0438/Settings')

    def test_annex_a_variant3(self):
        # Base pointer 0x0400; 0x80 is offset 0 -> U+0400, 0x41 is a GSM 'A'.
        self.assertEqual(text.decode_stk_text(bytes.fromhex('820204008041')),
                         '\u0400A')

    def test_error_falls_back_to_hex(self):
        # An odd-length UTF-16-BE payload cannot decode.
        self.assertEqual(text.decode_stk_text(bytes.fromhex('80044800')), '80044800')
        self.assertEqual(text.decode_stk_text(b'\x81\x05'), '8105')


class DcsTextTests(unittest.TestCase):
    def test_ucs2(self):
        self.assertEqual(text.decode_dcs_text(
            b'\x08' + 'Привет'.encode('utf-16-be')), 'Привет')

    def test_eight_bit(self):
        self.assertEqual(text.decode_dcs_text(b'\x04abc'), 'abc')

    def test_default_alphabet(self):
        self.assertEqual(text.decode_dcs_text(b'\x00hello'), 'hello')

    def test_short_and_empty_input(self):
        self.assertEqual(text.decode_dcs_text(b''), '')
        self.assertEqual(text.decode_dcs_text(b'\x00'), '00')

    def test_error_falls_back_to_hex(self):
        self.assertEqual(text.decode_dcs_text(b'\x08\x04'), '0804')


class EncodeTextTests(unittest.TestCase):
    def test_gsm7_packed_for_dcs_00(self):
        self.assertEqual(text.encode_text('hello', 0x00),
                         text.pack_gsm7(b'hello'))

    def test_ucs2(self):
        self.assertEqual(text.encode_text('Hi', 0x08), b'\x00H\x00i')
        self.assertEqual(text.encode_text('Hi', 0x18), b'\x00H\x00i')

    def test_eight_bit(self):
        self.assertEqual(text.encode_text('hello', 0x04), b'hello')

    def test_non_gsm_character_in_gsm7_raises(self):
        with self.assertRaises(UnicodeEncodeError):
            text.encode_text('П', 0x00)


# ---- transport ------------------------------------------------------------

class FakeConnection(object):
    """A pyscard CardConnection stand-in with scripted responses."""

    def __init__(self, responses, atr=None):
        self.responses = list(responses)
        self.sent = []
        self.atr = list(atr) if atr is not None else [0x3B, 0x00]
        self.protocol = None
        self.connects = 0
        self.disconnects = 0

    def connect(self):
        self.connects += 1

    def disconnect(self):
        self.disconnects += 1

    def getATR(self):
        return list(self.atr)

    def setProtocol(self, protocol):
        self.protocol = protocol

    def transmit(self, apdu):
        self.sent.append(bytes(apdu))
        if not self.responses:
            raise AssertionError('unexpected TPDU %s' % bytes(apdu).hex())
        data, sw = self.responses.pop(0)
        return list(data), sw[0], sw[1]


class RecordingTracer(transport.ApduTracer):
    def __init__(self):
        self.events = []

    def trace_command(self, cmd):
        self.events.append(('cmd', cmd))

    def trace_response(self, cmd, sw, resp):
        self.events.append(('resp', cmd, sw, resp))

    def trace_reset(self):
        self.events.append(('reset',))


class FakeReader(object):
    def __init__(self, name):
        self.name = name

    def createConnection(self):
        raise AssertionError('the fake reader connection must not be used')


class LoopingConnection(FakeConnection):
    """A pathological card: every exchange re-answers the same status word.

    The default ``62 00`` is the "dummy GET RESPONSE" request of
    TS 102 221 §7.3.1.1.3; ``61xx``/``63xx`` are reachable through ``sw``.
    """

    def __init__(self, sw=b'\x62\x00'):
        super().__init__([])
        self.sw = bytes(sw)

    def transmit(self, apdu):
        self.sent.append(bytes(apdu))
        return [], self.sw[0], self.sw[1]


class TransportExchangeTests(unittest.TestCase):
    def link(self, responses, tracer=None, atr=None):
        conn = FakeConnection(responses, atr=atr)
        link = transport.PcscLink(connection=conn, tracer=tracer)
        link.connect()
        return link, conn

    def test_t0_select_case4_gets_the_response(self):
        link, conn = self.link([
            (b'\x01\x02', b'\x61\x04'),
            (b'\x03\x04', b'\x90\x00'),
        ])
        data, sw = link.send_apdu(bytes.fromhex('00a40004023f0000'))
        self.assertEqual(data, b'\x01\x02\x03\x04')
        self.assertEqual(sw, b'\x90\x00')
        self.assertEqual(conn.sent, [
            bytes.fromhex('00a40004023f00'),      # Le dropped for case 4
            bytes.fromhex('00c0000004'),          # GET RESPONSE, Le from 61xx
        ])

    def test_case2_passes_through(self):
        link, conn = self.link([(b'\xaa\xbb', b'\x90\x00')])
        data, sw = link.send_apdu(bytes.fromhex('00b0000004'))
        self.assertEqual((data, sw), (b'\xaa\xbb', b'\x90\x00'))
        self.assertEqual(conn.sent, [bytes.fromhex('00b0000004')])

    def test_case1_attaches_an_le_octet(self):
        # TS 102 221 §7.3.1.1.1: a case 1 APDU gets an Le octet on T=0.
        link, conn = self.link([(b'\xaa', b'\x90\x00')])
        data, sw = link.send_apdu(bytes.fromhex('00a40000'))
        self.assertEqual((data, sw), (b'\xaa', b'\x90\x00'))
        self.assertEqual(conn.sent, [bytes.fromhex('00a4000000')])

    def test_case3_passes_the_data_through_without_an_le(self):
        link, conn = self.link([(b'', b'\x90\x00')])
        link.send_apdu(bytes.fromhex('00a40004023f00'))
        self.assertEqual(conn.sent, [bytes.fromhex('00a40004023f00')])

    def test_multiple_get_response_rounds(self):
        link, conn = self.link([
            (b'\x01', b'\x61\x02'),
            (b'\x02', b'\x61\x03'),
            (b'\x03\x04\x05', b'\x90\x00'),
        ])
        data, sw = link.send_apdu(bytes.fromhex('00a40004023f0000'))
        self.assertEqual((data, sw), (b'\x01\x02\x03\x04\x05', b'\x90\x00'))
        self.assertEqual(conn.sent[1:], [
            bytes.fromhex('00c0000002'),
            bytes.fromhex('00c0000003'),
        ])

    def test_9fxx_fetches_the_response(self):
        link, conn = self.link([
            (b'', b'\x9f\x10'),
            (b'\xee', b'\x90\x00'),
        ])
        data, sw = link.send_apdu(bytes.fromhex('00b0000004'))
        self.assertEqual((data, sw), (b'\xee', b'\x90\x00'))
        self.assertEqual(conn.sent[1], bytes.fromhex('00c0000010'))

    def test_62xx_sends_a_dummy_get_response(self):
        link, conn = self.link([
            (b'', b'\x62\x00'),
            (b'\x07', b'\x90\x00'),
        ])
        data, sw = link.send_apdu(bytes.fromhex('00b0000004'))
        self.assertEqual((data, sw), (b'\x07', b'\x90\x00'))
        self.assertEqual(conn.sent[1], bytes.fromhex('00c0000000'))

    def test_63xx_sends_a_dummy_get_response_too(self):
        link, conn = self.link([
            (b'', b'\x63\x00'),
            (b'\x07', b'\x90\x00'),
        ])
        data, sw = link.send_apdu(bytes.fromhex('00b0000004'))
        self.assertEqual((data, sw), (b'\x07', b'\x90\x00'))
        self.assertEqual(conn.sent[1], bytes.fromhex('00c0000000'))

    def test_case3_63xx_dummy_get_response_is_a_compatibility_fallback(self):
        # A **behaviour pin**: it passes on the pre-v4.2.0 code by design
        # (cases 2/3 were never narrowed) - only the case-1 test guards the
        # change; this one keeps the fallback from being "fixed" away.
        #
        # TS 102 221 §7.3.1.1.0 prescribes the dummy GET RESPONSE **"for case 4
        # commands only"**, and §7.3.1.1.2/.3 make the transport discontinue on
        # a status word for cases 2/3.  Following it for a case-3 APDU is a
        # deliberate *compatibility fallback*, not a spec rule: the live cards
        # need it (the case-2 file reads this rule came from, and v3.25.7 - an
        # ENVELOPE answering 63xx with the PoR behind a GET RESPONSE).  Pinned
        # so nobody "fixes" it as a spec violation.
        link, conn = self.link([
            (b'', b'\x63\x00'),
            (b'\x07', b'\x90\x00'),
        ])
        data, sw = link.send_apdu(bytes.fromhex('00a40004023f00'))   # case 3
        self.assertEqual((data, sw), (b'\x07', b'\x90\x00'))
        self.assertEqual(conn.sent[0], bytes.fromhex('00a40004023f00'))
        self.assertEqual(conn.sent[1], bytes.fromhex('00c0000000'))

    def test_9100_breaks_without_a_get_response(self):
        # The CAT success state: no response data is pending.
        link, conn = self.link([(b'', b'\x91\x00')])
        self.assertEqual(link.send_apdu(bytes.fromhex('00b0000004')),
                         (b'', b'\x91\x00'))
        self.assertEqual(len(conn.sent), 1)

    def test_case1_63xx_is_final_without_a_get_response(self):
        # A case-1 APDU has no Le and cannot carry response data, so its
        # status word is final: VERIFY PIN with an empty data field answers
        # the retry counter this way ('63CX', TS 102 221 §11.1.9.1.2) - a
        # dummy GET RESPONSE would lose it (and could not return data).
        link, conn = self.link([(b'', b'\x63\xc3')])
        data, sw = link.send_apdu(bytes.fromhex('00200001'))
        self.assertEqual((data, sw), (b'', b'\x63\xc3'))
        self.assertEqual(conn.sent, [bytes.fromhex('0020000100')])

    def test_case1_6983_is_final_too(self):
        link, conn = self.link([(b'', b'\x69\x83')])
        self.assertEqual(link.send_apdu(bytes.fromhex('00200081')),
                         (b'', b'\x69\x83'))
        self.assertEqual(len(conn.sent), 1)

    def test_6cxx_resends_with_the_correct_le(self):
        link, conn = self.link([
            (b'', b'\x6c\x10'),
            (b'\x01', b'\x90\x00'),
        ])
        data, sw = link.send_apdu(bytes.fromhex('00b00000ff'))
        self.assertEqual((data, sw), (b'\x01', b'\x90\x00'))
        self.assertEqual(conn.sent, [
            bytes.fromhex('00b00000ff'),
            bytes.fromhex('00b0000010'),
        ])

    def test_6cxx_after_a_get_response_round_resends_that_tpdu(self):
        link, conn = self.link([
            (b'\x01', b'\x61\x04'),
            (b'', b'\x6c\x02'),
            (b'\x02\x03', b'\x90\x00'),
        ])
        data, sw = link.send_apdu(bytes.fromhex('00a40004023f0000'))
        self.assertEqual((data, sw), (b'\x02\x03', b'\x90\x00'))
        self.assertEqual(conn.sent, [
            bytes.fromhex('00a40004023f00'),
            bytes.fromhex('00c0000004'),      # GET RESPONSE, Le from 61xx
            bytes.fromhex('00c0000002'),      # the 6Cxx re-send of that TPDU
        ])

    def test_a_card_that_never_stops_chaining_is_bounded(self):
        # A pathological card re-answering 62 00 must not hold the caller: the
        # chain stops at T0_MAX_ROUNDS and the last (data, sw) is returned.
        conn = LoopingConnection()
        link = transport.PcscLink(connection=conn)
        link.connect()
        with self.assertLogs('simple_server.transport', level='WARNING') as logs:
            data, sw = link.send_apdu(bytes.fromhex('00b0000004'))
        self.assertEqual((data, sw), (b'', b'\x62\x00'))
        self.assertEqual(len(conn.sent), 1 + transport.T0_MAX_ROUNDS)
        self.assertIn('chaining', '\n'.join(logs.output))

    def test_a_non_draining_61xx_chain_is_bounded_too(self):
        conn = LoopingConnection(b'\x61\x04')
        link = transport.PcscLink(connection=conn)
        link.connect()
        with self.assertLogs('simple_server.transport', level='WARNING'):
            data, sw = link.send_apdu(bytes.fromhex('00b0000004'))
        self.assertEqual(sw, b'\x61\x04')
        self.assertEqual(len(conn.sent), 1 + transport.T0_MAX_ROUNDS)

    def test_max_rounds_can_be_lowered(self):
        conn = LoopingConnection()
        link = transport.PcscLink(connection=conn)
        link.connect()
        with self.assertLogs('simple_server.transport', level='WARNING'):
            data, sw = link._send_t0(bytes.fromhex('00b0000004'), 2,
                                     max_rounds=2)
        self.assertEqual(sw, b'\x62\x00')
        self.assertEqual(len(conn.sent), 3)

    def test_t1_passes_the_apdu_through(self):
        link, conn = self.link([(b'\x01', b'\x90\x00')])
        link.protocol = 1
        data, sw = link.send_apdu(bytes.fromhex('00a40004023f0000'))
        self.assertEqual((data, sw), (b'\x01', b'\x90\x00'))
        self.assertEqual(conn.sent, [bytes.fromhex('00a40004023f0000')])

    def test_tracer_callbacks_in_order_with_hex(self):
        tracer = RecordingTracer()
        link, _conn = self.link([
            (b'\x01\x02', b'\x61\x02'),
            (b'\x03\x04', b'\x90\x00'),
        ], tracer=tracer)
        link.send_apdu(bytes.fromhex('00a40004023f0000'))
        self.assertEqual(tracer.events, [
            ('cmd', '00a40004023f0000'),
            ('resp', '00a40004023f0000', '9000', '01020304'),
        ])

    def test_malformed_apdu_never_reaches_the_card(self):
        link, conn = self.link([])
        with self.assertRaises(ValueError):
            link.send_apdu(b'\x00\xa4')
        self.assertEqual(conn.sent, [])

    def test_send_apdu_checksw(self):
        link, _conn = self.link([
            (b'', b'\x90\x00'),
            (b'', b'\x90\x00'),
            (b'', b'\x6a\x82'),
        ])
        self.assertEqual(link.send_apdu_checksw(b'\x00\xb0\x00\x00\x04'),
                         (b'', b'\x90\x00'))
        # Uppercase input is accepted; only the returned SW must match.
        self.assertEqual(link.send_apdu_checksw(b'\x00\xb0\x00\x00\x04',
                                               sw_match='9000')[1], b'\x90\x00')
        with self.assertRaises(transport.SwMatchError) as caught:
            link.send_apdu_checksw(b'\x00\xb0\x00\x00\x04')
        self.assertEqual(caught.exception.sw, '6a82')
        self.assertEqual(caught.exception.expected, '9000')

    def test_send_apdu_checksw_mask(self):
        link, _conn = self.link([
            (b'', b'\x90\x00'),
            (b'', b'\x91\xc3'),
            (b'', b'\x6a\x82'),
        ])
        self.assertEqual(link.send_apdu_checksw(b'\x00\xb0\x00\x00\x04',
                                               sw_match='9???')[1],
                         b'\x90\x00')
        self.assertEqual(link.send_apdu_checksw(b'\x00\xb0\x00\x00\x04',
                                               sw_match='9???')[1],
                         b'\x91\xc3')
        self.assertEqual(link.send_apdu_checksw(b'\x00\xb0\x00\x00\x04',
                                               sw_match='6A??')[1],
                         b'\x6a\x82')
        # A non-matching mask still raises.
        link, _conn = self.link([(b'', b'\x6a\x82')])
        with self.assertRaises(transport.SwMatchError):
            link.send_apdu_checksw(b'\x00\xb0\x00\x00\x04', sw_match='9???')


class TransportConnectionTests(unittest.TestCase):
    def test_connect_selects_t0_from_the_atr(self):
        conn = FakeConnection([], atr=[0x3B, 0x00])
        link = transport.PcscLink(connection=conn)
        link.connect()
        self.assertEqual(link.protocol, 0)
        self.assertEqual(conn.protocol, transport.CardConnection.T0_protocol)

    def test_unparseable_atr_keeps_the_default(self):
        conn = FakeConnection([], atr=[])
        link = transport.PcscLink(connection=conn)
        link.connect()
        self.assertEqual(link.protocol, 0)
        self.assertIsNone(conn.protocol)

    def test_no_card_raises_reader_error(self):
        class NoCardConnection(FakeConnection):
            def connect(self):
                raise transport.NoCardException('no card', 0x8010000C)

        link = transport.PcscLink(connection=NoCardConnection([]))
        with self.assertRaises(transport.ReaderError):
            link.connect()

    def test_get_atr_and_reset(self):
        tracer = RecordingTracer()
        conn = FakeConnection([], atr=[0x3B, 0x00])
        link = transport.PcscLink(connection=conn, tracer=tracer)
        link.connect()
        self.assertEqual(link.get_atr(), b'\x3b\x00')
        link.reset_card()
        self.assertEqual(conn.connects, 2)
        self.assertGreaterEqual(conn.disconnects, 1)
        self.assertIn(('reset',), tracer.events)

    def test_disconnect_tolerates_a_dead_handle(self):
        class DeadConnection(FakeConnection):
            def disconnect(self):
                raise RuntimeError('returned NULL without setting an exception')

        link = transport.PcscLink(connection=DeadConnection([]))
        link.disconnect()          # must not raise

    def test_not_connected_operations_raise(self):
        with mock.patch('smartcard.System.readers',
                        return_value=[FakeReader('Reader A')]):
            link = transport.PcscLink()
        with self.assertRaises(transport.ReaderError):
            link.send_tpdu(b'\x00\xa4\x00\x00')
        with self.assertRaises(transport.ReaderError):
            link.get_atr()

    def test_wait_for_card_with_an_injected_connection(self):
        conn = FakeConnection([])
        link = transport.PcscLink(connection=conn)
        link.wait_for_card()
        self.assertEqual(conn.connects, 1)


class ReaderSelectionTests(unittest.TestCase):
    def test_detect_reader(self):
        with mock.patch('smartcard.System.readers',
                        return_value=[FakeReader('Reader A'), FakeReader('Reader B')]):
            self.assertEqual(transport.detect_reader(), 'Reader A')

    def test_detect_reader_without_readers_raises(self):
        with mock.patch('smartcard.System.readers', return_value=[]):
            with self.assertRaises(transport.ReaderError):
                transport.detect_reader()

    def test_auto_first_reader(self):
        with mock.patch('smartcard.System.readers',
                        return_value=[FakeReader('Reader A'), FakeReader('Reader B')]):
            link = transport.PcscLink()
        self.assertEqual(link.reader_name, 'Reader A')

    def test_reader_by_index_and_name(self):
        available = [FakeReader('Reader A'), FakeReader('Reader B')]
        with mock.patch('smartcard.System.readers', return_value=available):
            self.assertEqual(transport.PcscLink(reader=1).reader_name, 'Reader B')
            self.assertEqual(transport.PcscLink(reader='Reader A').reader_name,
                             'Reader A')

    def test_bad_reader_selections(self):
        with mock.patch('smartcard.System.readers', return_value=[FakeReader('Reader A')]):
            with self.assertRaises(transport.ReaderError):
                transport.PcscLink(reader=3)
            with self.assertRaises(transport.ReaderError):
                transport.PcscLink(reader='Nope')
            with self.assertRaises(TypeError):
                transport.PcscLink(reader=1.5)

    def test_no_readers_raises(self):
        with mock.patch('smartcard.System.readers', return_value=[]):
            with self.assertRaises(transport.ReaderError):
                transport.PcscLink()


class HookBaseTests(unittest.TestCase):
    def test_apdu_tracer_is_a_no_op(self):
        tracer = transport.ApduTracer()
        tracer.trace_command('00a40000')
        tracer.trace_response('00a40000', '9000', '')
        tracer.trace_reset()

    def test_proactive_handler_is_a_no_op(self):
        handler = transport.ProactiveHandler()
        self.assertIsNone(handler.receive_fetch_raw(None, None))
        self.assertEqual(handler.prepare_response(None), [])


# ---- the per-PIN state readouts (v4.2.0) ----------------------------------

class ChvProbeResultTests(unittest.TestCase):
    """``_chv_probe_result``: a UICC's empty-data VERIFY PIN answer.

    TS 102 221 §11.1.9.1.2 (63CX = retries left, 63C0/6983 for a blocked PIN),
    ISO 7816-4 §7.5.6 (9000 = verification not required), §7.5.1 (6A88) and
    Table 6 (6984 "Reference data not usable").
    """

    def test_the_state_table(self):
        cases = (
            ('9000', 'not_required', None),
            ('63C3', 'required', 3),
            ('63CA', 'required', 10),
            ('63C0', 'blocked', 0),
            ('6983', 'blocked', 0),
            ('6A88', 'not_initialized', None),
            ('6984', 'not_usable', None),
        )
        for sw, state, attempts in cases:
            with self.subTest(sw=sw):
                result = server_module._chv_probe_result(sw)
                self.assertEqual(result['state'], state)
                self.assertEqual(result['attempts_left'], attempts)
                self.assertEqual(result['sw'], sw)

    def test_an_unknown_answer_keeps_the_status_word(self):
        for sw in ('6A82', '6985', '6D00', '0000', ''):
            with self.subTest(sw=sw):
                result = server_module._chv_probe_result(sw)
                self.assertEqual(result['state'], 'unknown')
                self.assertIsNone(result['attempts_left'])
                self.assertEqual(result['sw'], sw)

    def test_the_input_is_normalised_and_63c0_is_not_required(self):
        self.assertEqual(server_module._chv_probe_result(' 63c3 ')['attempts_left'], 3)
        # 63C0 is 0 retries left: blocked, never 'required' with 0
        self.assertEqual(server_module._chv_probe_result('63C0')['state'], 'blocked')

    def test_every_state_is_in_the_vocabulary(self):
        for sw in ('9000', '63C3', '63C0', '6983', '6A88', '6984', '6A82'):
            self.assertIn(server_module._chv_probe_result(sw)['state'],
                          server_module.CHV_STATES)


class ChvSimStateTests(unittest.TestCase):
    """``_chv_sim_state``: a 2G SIM's MF/DF SELECT block (TS 51 011 §9.2.1).

    Byte 14 (Detail 1) b8 = CHV1 disabled; byte 19 / 21 (Detail 2) b8 =
    secret code initialised, b4-b1 = remaining false presentations ('0' =
    blocked).  Indices are byte-1 (byte 14 -> raw[13]).
    """

    @staticmethod
    def block(chv1_enabled=True, chv1_status=0x83, chv2_status=0x8A):
        raw = bytearray(22)
        raw[13] = 0x00 if chv1_enabled else 0x80     # byte 14
        raw[18] = chv1_status                        # byte 19
        raw[20] = chv2_status                        # byte 21
        return bytes(raw)

    def test_chv1_required_with_the_remaining_count(self):
        result = server_module._chv_sim_state(self.block(), 'pin1')
        self.assertEqual(result, {'state': 'required', 'attempts_left': 3})

    def test_chv1_disabled_needs_no_verification(self):
        # The disabled bit wins over the status byte (a disabled CHV1 needs no
        # verification whatever its counter says).
        raw = self.block(chv1_enabled=False, chv1_status=0x83)
        self.assertEqual(server_module._chv_sim_state(raw, 'pin1'),
                         {'state': 'not_required', 'attempts_left': None})
        raw = self.block(chv1_enabled=False, chv1_status=0x00)
        self.assertEqual(server_module._chv_sim_state(raw, 'pin1')['state'],
                         'not_required')

    def test_chv1_blocked(self):
        raw = self.block(chv1_status=0x80)           # initialised, 0 remaining
        self.assertEqual(server_module._chv_sim_state(raw, 'pin1'),
                         {'state': 'blocked', 'attempts_left': 0})

    def test_chv2_is_not_initialised(self):
        raw = self.block(chv2_status=0x00)
        self.assertEqual(server_module._chv_sim_state(raw, 'pin2'),
                         {'state': 'not_initialized', 'attempts_left': None})

    def test_chv2_reads_its_own_byte(self):
        # CHV1's status/disabled bits must not leak into CHV2 and vice versa.
        raw = self.block(chv1_enabled=False, chv1_status=0x80, chv2_status=0x8A)
        self.assertEqual(server_module._chv_sim_state(raw, 'pin2'),
                         {'state': 'required', 'attempts_left': 10})
        raw = self.block(chv1_status=0x83, chv2_status=0x00)
        self.assertEqual(server_module._chv_sim_state(raw, 'pin1')['state'],
                         'required')

    def test_a_short_block_is_unknown_without_a_status_word(self):
        for length in (0, 14, 21):
            with self.subTest(length=length):
                result = server_module._chv_sim_state(bytes(length), 'pin1')
                self.assertEqual(result, {'state': 'unknown',
                                          'attempts_left': None})
                self.assertNotIn('sw', result)

    def test_the_hex_form_is_accepted(self):
        raw = self.block()
        self.assertEqual(server_module._chv_sim_state(raw.hex().upper(), 'pin2'),
                         {'state': 'required', 'attempts_left': 10})
        self.assertEqual(server_module._chv_sim_state(' '.join(
            '%02X' % b for b in raw), 'pin2')['attempts_left'], 10)

    def test_a_bad_code_is_refused(self):
        with self.assertRaises(ValueError):
            server_module._chv_sim_state(self.block(), 'pin3')


class ChvStateSummaryTests(unittest.TestCase):
    """``_chv_state_summary``: the ``/api/status`` / ``/api/chv-status`` block."""

    def test_both_codes_are_present_with_uniform_fields(self):
        summary = server_module._chv_state_summary({
            'pin1': {'state': 'required', 'attempts_left': 3, 'sw': '63C3',
                     'enabled': True},
            'pin2': {'state': 'not_required', 'attempts_left': None,
                     'sw': '9000', 'enabled': False},
        })
        self.assertEqual(summary, {
            'pin1': {'state': 'required', 'attempts_left': 3, 'sw': '63C3',
                     'enabled': True},
            'pin2': {'state': 'not_required', 'attempts_left': None,
                     'sw': '9000', 'enabled': False},
        })

    def test_a_missing_code_is_unknown(self):
        summary = server_module._chv_state_summary({
            'pin1': {'state': 'blocked', 'attempts_left': 0},
        })
        self.assertEqual(summary['pin1'],
                         {'state': 'blocked', 'attempts_left': 0, 'sw': None,
                          'enabled': None})
        self.assertEqual(summary['pin2'],
                         {'state': 'unknown', 'attempts_left': None, 'sw': None,
                          'enabled': None})

    def test_nothing_is_dropped(self):
        # A SIM readout has no sw; a probe's sw/enabled survive verbatim.
        for states in (None, {}, {'pin2': {'state': 'required',
                                           'attempts_left': 10,
                                           'enabled': None}}):
            summary = server_module._chv_state_summary(states)
            self.assertEqual(sorted(summary), ['pin1', 'pin2'])
            for code in ('pin1', 'pin2'):
                self.assertEqual(sorted(summary[code]),
                                 ['attempts_left', 'enabled', 'state', 'sw'])
                self.assertIn(summary[code]['state'], server_module.CHV_STATES)


class ChvSwResultTests(unittest.TestCase):
    """``_chv_sw_result``: the action-aware status-word mapping.

    TS 102 221 §10.2.1.3 Table 10.9 note (printed p. 82): 63CX is the
    remaining attempts for VERIFY only; for the other commands it is the
    card's internal retries.  UNBLOCK is the explicit exception (§11.1.13.1.1).
    """

    def test_9000_is_ok_for_every_action(self):
        for action in ('verify', 'change', 'unblock', 'enable', 'disable'):
            self.assertEqual(server_module._chv_sw_result('9000', action),
                             {'ok': True, 'sw': '9000'})

    def test_verify_and_unblock_report_the_attempts(self):
        self.assertEqual(server_module._chv_sw_result('63C2', 'verify'),
                         {'ok': False, 'sw': '63C2', 'attempts_left': 2})
        self.assertEqual(server_module._chv_sw_result('63C2'), {'ok': False,
                         'sw': '63C2', 'attempts_left': 2})
        self.assertEqual(server_module._chv_sw_result('63C2', 'unblock'),
                         {'ok': False, 'sw': '63C2', 'attempts_left': 2})

    def test_enable_and_disable_succeed_with_internal_retries(self):
        # TS 102 221 Table 10.9 (printed p. 82): 63CX is "Command successful
        # but after using an internal update retry routine 'X' times" for
        # every command except VERIFY - so ENABLE/DISABLE succeeded.
        for action in ('enable', 'disable'):
            with self.subTest(action=action):
                result = server_module._chv_sw_result('63C2', action)
                self.assertTrue(result['ok'])
                self.assertNotIn('attempts_left', result)
                self.assertEqual(result['internal_retries'], 2)
                self.assertIn('internal retries', result['warning'])

    def test_change_63cx_keeps_both_readings(self):
        # §11.1.10.1 prints the counter decrement for a false old PIN but no
        # status word, so a 63CX stays a failure with both readings.
        result = server_module._chv_sw_result('63C2', 'change')
        self.assertFalse(result['ok'])
        self.assertNotIn('attempts_left', result)
        self.assertEqual(result['internal_retries'], 2)
        self.assertIn('internal retries', result['warning'])
        self.assertIn('wrong old PIN', result['warning'])

    def test_63c0_is_blocked_for_every_action(self):
        # Table 10.9's "internal retries" framing does not override an
        # exhausted counter: 0 retries left is the blocked state.
        for action in ('verify', 'change', 'unblock', 'enable', 'disable'):
            result = server_module._chv_sw_result('63C0', action)
            self.assertTrue(result['blocked'])
            self.assertEqual(result['attempts_left'], 0)
            self.assertFalse(result['ok'])

    def test_the_shared_answers_are_action_independent(self):
        for sw, key in (('6983', 'blocked'), ('9840', 'blocked'),
                        ('9804', 'failed'), ('6984', 'invalidated')):
            for action in ('verify', 'change', 'unblock', 'enable', 'disable'):
                self.assertTrue(server_module._chv_sw_result(sw, action)[key],
                                (sw, action))


class ChvCombinedStateTests(unittest.TestCase):
    """``_chv_combined_state``: the FCP's C6 flag decides over the probe.

    TS 102 221 §9.5.2 ("for each bit set to '1' the corresponding key
    reference (PIN) is enabled") vs §11.1.9.1.2 (the retry counter): the probe
    alone cannot tell a disabled PIN from a locked one.  The precedence is
    **hardware-confirmed** on the bench card (2026-11-23): its ADF.USIM ``C6``
    reports PIN1 ``01`` disabled, yet the empty-data VERIFY PIN1 answers
    ``63C3`` - the probe alone would have read "PIN1 locked, 3 tries left".
    """

    def test_a_disabled_pin_is_not_required(self):
        # The bench card's actual answer for its disabled PIN1: the probe's
        # 63C3 (3 retries, no attempt consumed) with C6 saying "disabled" -
        # the flag wins, the counter is kept as information only.
        probe = {'state': 'required', 'attempts_left': 3, 'sw': '63C3'}
        entry = server_module._chv_combined_state(probe, False)
        self.assertEqual(entry, {'state': 'not_required', 'attempts_left': 3,
                                 'sw': '63C3', 'enabled': False})

    def test_an_enabled_pin_keeps_the_probe_state(self):
        for probe_state, attempts in (('required', 3), ('blocked', 0),
                                      ('not_required', None),
                                      ('not_usable', None),
                                      ('not_initialized', None),
                                      ('unknown', None)):
            probe = {'state': probe_state, 'attempts_left': attempts,
                     'sw': '63C3'}
            entry = server_module._chv_combined_state(probe, True)
            self.assertEqual(entry['state'], probe_state)
            self.assertEqual(entry['attempts_left'], attempts)
            self.assertIs(entry['enabled'], True)

    def test_no_flag_is_probe_only(self):
        probe = {'state': 'required', 'attempts_left': 2, 'sw': '63C2'}
        entry = server_module._chv_combined_state(probe, None)
        self.assertEqual(entry, {'state': 'required', 'attempts_left': 2,
                                 'sw': '63C2', 'enabled': None})

    def test_an_unknown_state_never_leaks_the_probe(self):
        entry = server_module._chv_combined_state({'state': 'bogus'}, None)
        self.assertEqual(entry['state'], 'unknown')
        entry = server_module._chv_combined_state({}, True)
        self.assertEqual(entry['state'], 'unknown')
        self.assertEqual(entry, {'state': 'unknown', 'attempts_left': None,
                                 'sw': None, 'enabled': True})


if __name__ == '__main__':
    unittest.main()
