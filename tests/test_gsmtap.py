# coding=utf-8
"""Tests for the GSMTAP-SIM APDU sender (gsmtap.py)."""

import socket
import struct
import unittest

from pysim_simple_server import gsmtap

FCI = ('62278202782183023F00A50A8001718302C0F28701018A01058B032F0601'
       'C60990014083010183010A')


class PacketTests(unittest.TestCase):
    def test_header_and_payload(self):
        apdu = bytes.fromhex('00A40004023F00')
        pkt = gsmtap.build_packet(gsmtap.GSMTAP_SIM_APDU, apdu)
        self.assertEqual(len(pkt), 16 + len(apdu))
        (version, hdr_len, pkt_type, timeslot, arfcn, signal, snr, frame,
         sub_type, antenna, slot, res) = struct.unpack('!BBBBHBBIBBBB', pkt[:16])
        self.assertEqual(version, gsmtap.GSMTAP_VERSION)
        self.assertEqual(hdr_len, 4)
        self.assertEqual(pkt_type, 0x04)          # GSMTAP_TYPE_SIM
        self.assertEqual((timeslot, arfcn, signal, snr, frame), (0, 0, 0, 0, 0))
        self.assertEqual(sub_type, gsmtap.GSMTAP_SIM_APDU)
        self.assertEqual((antenna, slot, res), (0, 0, 0))
        self.assertEqual(pkt[16:], apdu)

    def test_parse_target_defaults_and_overrides(self):
        self.assertEqual(gsmtap.parse_target(None), ('127.0.0.1', 4729))
        self.assertEqual(gsmtap.parse_target(''), ('127.0.0.1', 4729))
        self.assertEqual(gsmtap.parse_target('127.0.0.1:4729'), ('127.0.0.1', 4729))
        self.assertEqual(gsmtap.parse_target('10.0.0.5'), ('10.0.0.5', 4729))
        self.assertEqual(gsmtap.parse_target('10.0.0.5:5000'), ('10.0.0.5', 5000))
        self.assertEqual(gsmtap.parse_target(':5000'), ('127.0.0.1', 5000))


class SenderTests(unittest.TestCase):
    def test_send_apdu_reaches_a_udp_listener(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(('127.0.0.1', 0))
        sock.settimeout(2.0)
        host, port = sock.getsockname()
        sender = gsmtap.GsmtapSender(host, port)
        try:
            sender.send_apdu(bytes.fromhex('00A40004023F00'))
            data, _addr = sock.recvfrom(2048)
        finally:
            sender.close()
            sock.close()
        self.assertEqual(data[12], gsmtap.GSMTAP_SIM_APDU)   # sub_type
        self.assertEqual(data[16:], bytes.fromhex('00A40004023F00'))

    def test_send_never_raises_without_a_listener(self):
        sender = gsmtap.GsmtapSender('127.0.0.1', 1)   # nothing listening
        sender.send_apdu(b'\x00\xa4')                  # must not raise
        sender.close()


class Recorder:
    """Stand-in for GsmtapSender recording the APDU payloads."""

    def __init__(self):
        self.apdus = []

    def send_apdu(self, data, slot_nr=0):
        self.apdus.append(bytes(data))


class TracerTests(unittest.TestCase):
    def trace(self, cmd, sw='9000', resp=''):
        rec = Recorder()
        gsmtap.GsmtapApduTracer(rec).trace_response(cmd, sw, resp)
        return rec.apdus

    def test_case4_data_becomes_command_plus_get_response(self):
        # A case-4 command goes on the wire without its Le byte and the data
        # follows via GET RESPONSE (T=0).
        self.assertEqual(self.trace('00A40004023F0000', resp=FCI), [
            bytes.fromhex('00A40004023F00') + bytes([0x61, len(FCI) // 2]),
            bytes.fromhex('00C00000') + bytes([len(FCI) // 2])
            + bytes.fromhex(FCI) + bytes.fromhex('9000'),
        ])

    def test_case4_error_is_command_plus_sw(self):
        self.assertEqual(
            self.trace('00A4040410A0000005591010FFFFFFFF890000010000', sw='6A82'),
            [bytes.fromhex('00A4040410A0000005591010FFFFFFFF8900000100' + '6A82')])

    def test_case2_merges_the_response_into_the_command_packet(self):
        self.assertEqual(self.trace('00B000000A', resp='980711090000640070F2'),
                         [bytes.fromhex('00B000000A980711090000640070F29000')])

    def test_case2_error_keeps_the_status_word(self):
        self.assertEqual(self.trace('80F2000C00', sw='6A82'),
                         [bytes.fromhex('80F2000C006A82')])

    def test_case3_merges_the_status_word(self):
        tp = '8010000022' + 'FF' * 34
        self.assertEqual(self.trace(tp, sw='9130'),
                         [bytes.fromhex(tp + '9130')])

    def test_long_case4_response_is_chunked(self):
        data = 'AA' * 300
        packets = self.trace('00A40004023F0000', resp=data)
        self.assertEqual(len(packets), 3)          # command + 2 GET RESPONSEs
        self.assertEqual(packets[0],
                         bytes.fromhex('00A40004023F00') + bytes([0x61, 255]))
        self.assertEqual(packets[1][:5], bytes([0x00, 0xC0, 0x00, 0x00, 255]))
        self.assertEqual(packets[1][5:], bytes.fromhex(data[:255 * 2]))
        self.assertEqual(packets[2][:5], bytes([0x00, 0xC0, 0x00, 0x00, 45]))
        self.assertEqual(packets[2][5:],
                         bytes.fromhex(data[255 * 2:]) + bytes.fromhex('9000'))

    def test_unparseable_apdu_falls_back_to_raw_packets(self):
        self.assertEqual(self.trace('00A4', resp='AA'),
                         [bytes.fromhex('00A4'), bytes.fromhex('AA9000')])

    def test_malformed_hex_sends_nothing(self):
        self.assertEqual(self.trace('zz', resp='AA'), [])
        self.assertEqual(self.trace('', resp=''), [])

    def test_trace_command_sends_nothing(self):
        rec = Recorder()
        gsmtap.GsmtapApduTracer(rec).trace_command('00A40004023F0000')
        self.assertEqual(rec.apdus, [])

    def test_fanout_forwards_every_callback(self):
        class T:
            def __init__(self):
                self.calls = []

            def trace_command(self, cmd):
                self.calls.append(('cmd', cmd))

            def trace_response(self, cmd, sw, resp):
                self.calls.append(('rsp', cmd, sw, resp))

            def trace_reset(self):
                self.calls.append(('reset',))

        a, b = T(), T()
        fan = gsmtap.FanoutApduTracer([a, b])
        fan.trace_command('00A4')
        fan.trace_response('00A4', '9000', '')
        fan.trace_reset()
        self.assertEqual(a.calls, b.calls)
        self.assertEqual(len(a.calls), 3)
