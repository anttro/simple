# coding=utf-8
"""Tests for the GSMTAP-SIM APDU sender (gsmtap.py)."""

import socket
import struct
import unittest

from pysim_simple_server import gsmtap


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

    def test_atr_subtype_and_slot(self):
        pkt = gsmtap.build_packet(gsmtap.GSMTAP_SIM_ATR, b'\x3b\x00', slot_nr=2)
        self.assertEqual(pkt[12], gsmtap.GSMTAP_SIM_ATR)   # sub_type
        self.assertEqual(pkt[14], 2)                       # sub_slot
        self.assertEqual(pkt[16:], b'\x3b\x00')

    def test_parse_target_defaults_and_overrides(self):
        self.assertEqual(gsmtap.parse_target(None), ('127.0.0.1', 4729))
        self.assertEqual(gsmtap.parse_target(''), ('127.0.0.1', 4729))
        self.assertEqual(gsmtap.parse_target('127.0.0.1:4729'), ('127.0.0.1', 4729))
        self.assertEqual(gsmtap.parse_target('10.0.0.5'), ('10.0.0.5', 4729))
        self.assertEqual(gsmtap.parse_target('10.0.0.5:5000'), ('10.0.0.5', 5000))
        self.assertEqual(gsmtap.parse_target(':5000'), ('127.0.0.1', 5000))


class SenderTests(unittest.TestCase):
    def test_send_apdu_and_atr_reach_a_udp_listener(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(('127.0.0.1', 0))
        sock.settimeout(2.0)
        host, port = sock.getsockname()
        sender = gsmtap.GsmtapSender(host, port)
        try:
            sender.send_apdu(bytes.fromhex('00A40004023F00'))
            sender.send_atr(bytes.fromhex('3B00'))
            first, _addr = sock.recvfrom(2048)
            second, _addr = sock.recvfrom(2048)
        finally:
            sender.close()
            sock.close()
        self.assertEqual(first[12], gsmtap.GSMTAP_SIM_APDU)   # sub_type
        self.assertEqual(first[16:], bytes.fromhex('00A40004023F00'))
        self.assertEqual(second[12], gsmtap.GSMTAP_SIM_ATR)
        self.assertEqual(second[16:], bytes.fromhex('3B00'))

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
    def test_command_and_response_are_wire_shaped(self):
        rec = Recorder()
        tracer = gsmtap.GsmtapApduTracer(rec)
        tracer.trace_command('00A40004023F00')
        tracer.trace_response('00A40004023F00', '9000', '622982027821')
        self.assertEqual(rec.apdus, [
            bytes.fromhex('00A40004023F00'),
            bytes.fromhex('6229820278219000'),   # data + SW1SW2
        ])

    def test_response_without_data_is_the_sw(self):
        rec = Recorder()
        gsmtap.GsmtapApduTracer(rec).trace_response('00B000000A', '6A82', '')
        self.assertEqual(rec.apdus, [bytes.fromhex('6A82')])

    def test_malformed_hex_never_raises(self):
        rec = Recorder()
        tracer = gsmtap.GsmtapApduTracer(rec)
        tracer.trace_command('not-hex')
        tracer.trace_response('00A4', None, None)
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
