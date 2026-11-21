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
        with self.assertRaises(ValueError):
            tlv.ComprTlv(tag=0x01, value=b'\x00' * 256).to_tlv()
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


if __name__ == '__main__':
    unittest.main()
