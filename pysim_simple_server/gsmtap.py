# coding=utf-8
"""GSMTAP-SIM UDP sender for live APDU capture.

Streams every APDU the server sends/receives as GSMTAP-SIM packets, so a
GSMTAP receiver (SIMtrace Analyser ``--capture gsmtap``, Wireshark, or
simtrace2-sniff) can follow the card dialogue live.  Enabled with
``--gsmtap [HOST[:PORT]]`` only (default ``127.0.0.1:4729``) - there is no
UI or API for it.

The packet format is the one shared by libosmocore's ``gsmtap.h``,
simtrace2-sniff, sigrok-iso7816-stream and the SIMtrace Analyser: a 16-byte
big-endian header (version 2, ``hdr_len`` 4, type 0x04 = SIM, sub_type,
``res`` flags) followed by the raw APDU/TPDU bytes.  A response is sent as
``data + SW1SW2`` (the wire form); the receiver infers the direction from
the ISO 7816 case, exactly like a sniffer capture.

Sending is fire-and-forget on a non-blocking socket: a missing listener must
never affect card I/O.
"""

import socket
import struct

from pySim.transport import ApduTracer

GSMTAP_VERSION = 0x02
GSMTAP_HDR_LEN = 4              # in 32-bit words (16 bytes)
GSMTAP_TYPE_SIM = 0x04

GSMTAP_SIM_APDU = 0x00
GSMTAP_SIM_ATR = 0x01

GSMTAP_UDP_PORT = 4729
DEFAULT_TARGET = '127.0.0.1:%d' % GSMTAP_UDP_PORT

_HDR_FMT = '!BBBBHBBIBBBB'      # 16 bytes, big-endian
_HDR_SIZE = struct.calcsize(_HDR_FMT)


def build_packet(sub_type, data, flags=0, slot_nr=0):
    """Build a complete GSMTAP-SIM packet (header + payload) as bytes."""
    hdr = struct.pack(
        _HDR_FMT,
        GSMTAP_VERSION,   # version
        GSMTAP_HDR_LEN,   # hdr_len (in 32-bit words)
        GSMTAP_TYPE_SIM,  # type
        0,                # timeslot
        0,                # arfcn
        0,                # signal_dbm
        0,                # snr_db
        0,                # frame_number
        sub_type,         # sub_type
        0,                # antenna_nr
        slot_nr,          # sub_slot
        flags,            # res (GSMTAP_FLAG_*; 0 here)
    )
    return hdr + bytes(data)


def parse_target(target):
    """'HOST[:PORT]' -> (host, port); empty/None -> the default target."""
    text = str(target or '').strip()
    if not text:
        text = DEFAULT_TARGET
    if ':' in text:
        host, _sep, port = text.rpartition(':')
        return host or '127.0.0.1', int(port)
    return text, GSMTAP_UDP_PORT


class GsmtapSender:
    """Fire-and-forget GSMTAP-SIM UDP sender (never raises on send)."""

    def __init__(self, host='127.0.0.1', port=GSMTAP_UDP_PORT):
        self._addr = (host, int(port))
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # A full socket buffer must never stall a card operation.
        self._sock.setblocking(False)

    @property
    def target(self):
        return '%s:%d' % self._addr

    def send(self, sub_type, data, flags=0, slot_nr=0):
        try:
            self._sock.sendto(build_packet(sub_type, data, flags, slot_nr),
                              self._addr)
        except OSError:
            pass

    def send_apdu(self, data, slot_nr=0):
        self.send(GSMTAP_SIM_APDU, data, slot_nr=slot_nr)

    def send_atr(self, data, slot_nr=0):
        self.send(GSMTAP_SIM_ATR, data, slot_nr=slot_nr)

    def close(self):
        try:
            self._sock.close()
        except OSError:
            pass


class GsmtapApduTracer(ApduTracer):
    """pySim APDU tracer that streams every APDU as a GSMTAP-SIM packet.

    Commands are sent as-is; a response is sent as ``data + SW1SW2`` so the
    receiver sees the same wire TPDU a hardware sniffer would capture.
    Malformed hex never raises into pySim's transport.
    """

    def __init__(self, sender):
        super().__init__()
        self.sender = sender

    def trace_command(self, cmd):
        if not cmd:
            return
        try:
            self.sender.send_apdu(bytes.fromhex(cmd))
        except (ValueError, TypeError):
            pass

    def trace_response(self, cmd, sw, resp):
        data = (resp or '') + (sw or '')
        if not data:
            return
        try:
            self.sender.send_apdu(bytes.fromhex(data))
        except (ValueError, TypeError):
            pass


class FanoutApduTracer(ApduTracer):
    """Forward tracer callbacks to several tracers (e.g. stderr + GSMTAP)."""

    def __init__(self, tracers):
        super().__init__()
        self.tracers = list(tracers)

    def trace_command(self, cmd):
        for tracer in self.tracers:
            tracer.trace_command(cmd)

    def trace_response(self, cmd, sw, resp):
        for tracer in self.tracers:
            tracer.trace_response(cmd, sw, resp)

    def trace_reset(self):
        for tracer in self.tracers:
            tracer.trace_reset()
