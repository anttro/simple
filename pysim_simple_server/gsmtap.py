# coding=utf-8
"""GSMTAP-SIM UDP sender for live APDU capture.

Streams the card APDUs the server sends/receives as GSMTAP-SIM packets, so a
GSMTAP receiver (SIMtrace Analyser ``--capture gsmtap``, Wireshark, or
simtrace2-sniff) can follow the card dialogue live.  Enabled with
``--gsmtap [HOST[:PORT]]`` only (default ``127.0.0.1:4729``) - there is no UI
or API for it.

The packet format is the one shared by libosmocore's ``gsmtap.h``,
simtrace2-sniff, sigrok-iso7816-stream and the SIMtrace Analyser: a 16-byte
big-endian header (version 2, ``hdr_len`` 4, type 0x04 = SIM, sub_type 0x00 =
APDU, ``res`` flags) followed by the raw TPDU bytes.

The tracer emits **wire-shaped TPDUs**, not logical APDUs, because that is
what a receiver pairs and decodes:

* case 4 (command with data + Le): the command without its Le byte plus the
  "61XX bytes available" status word, then the response data as a GET
  RESPONSE TPDU (``00C00000<len> + data + SW``);
* case 2 / 1 / 3: one packet with the command, the response data and the SW;
* unparseable APDUs fall back to a raw command packet and a raw response one.

The APDU data is always the real one - only the T=0 framing (which the PC/SC
reader hides) is reconstructed, so the ``61XX`` length is the final response
length and internal retries are not visible.  No ATR/VCC/RST/PPS events are
sent (the server has no line-level access).

Sending is fire-and-forget on a non-blocking socket: a missing listener must
never affect card I/O.
"""

import socket
import struct

from pySim.transport import ApduTracer
from pySim.utils import h2b, parse_command_apdu

GSMTAP_VERSION = 0x02
GSMTAP_HDR_LEN = 4              # in 32-bit words (16 bytes)
GSMTAP_TYPE_SIM = 0x04

GSMTAP_SIM_APDU = 0x00
GSMTAP_SIM_ATR = 0x01           # not sent; kept for reference

GSMTAP_UDP_PORT = 4729
DEFAULT_TARGET = '127.0.0.1:%d' % GSMTAP_UDP_PORT

_HDR_FMT = '!BBBBHBBIBBBB'      # 16 bytes, big-endian
_HDR_SIZE = struct.calcsize(_HDR_FMT)

_GET_RESPONSE_CHUNK = 255       # max GET RESPONSE payload per TPDU


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

    def close(self):
        try:
            self._sock.close()
        except OSError:
            pass


def _apdu_case(cmd):
    """ISO 7816-3 case of a command APDU, or None when unparseable."""
    try:
        case, _lc, _le, _data = parse_command_apdu(h2b(cmd))
        return case
    except Exception:
        return None


class GsmtapApduTracer(ApduTracer):
    """pySim APDU tracer streaming wire-shaped TPDUs as GSMTAP-SIM packets.

    See the module docstring for the emitted forms.  Malformed hex never
    raises into pySim's transport.
    """

    def __init__(self, sender):
        super().__init__()
        self.sender = sender

    def trace_command(self, cmd):
        """Nothing is sent here: the wire packets need the response."""

    def trace_response(self, cmd, sw, resp):
        try:
            raw = bytes.fromhex(cmd or '')
            resp_bytes = bytes.fromhex(resp or '')
            sw_bytes = bytes.fromhex(sw or '')
        except (ValueError, TypeError):
            return

        case = _apdu_case(cmd)
        if case is None or not raw:
            # Unparseable APDU: keep the raw command / raw response form.
            if raw:
                self.sender.send_apdu(raw)
                if resp_bytes or sw_bytes:
                    self.sender.send_apdu(resp_bytes + sw_bytes)
            return

        if case == 4 and len(raw) > 1:
            # The wire TPDU carries no Le byte for case 4.
            body = raw[:-1]
            if not resp_bytes:
                self.sender.send_apdu(body + sw_bytes)
                return
            size = len(resp_bytes)
            # T=0: "61XX bytes available", then the data via GET RESPONSE.
            self.sender.send_apdu(body + bytes([0x61, min(size, 0xFF)]))
            offset = 0
            while offset < size:
                chunk = min(size - offset, _GET_RESPONSE_CHUNK)
                last = offset + chunk >= size
                self.sender.send_apdu(
                    bytes([0x00, 0xC0, 0x00, 0x00, chunk])
                    + resp_bytes[offset:offset + chunk]
                    + (sw_bytes if last else b''))
                offset += chunk
            return

        # case 1/2/3: the command TPDU and its response share one packet.
        self.sender.send_apdu(raw + resp_bytes + sw_bytes)


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
