# coding=utf-8
"""GSMTAP-SIM APDU streaming at the transmit level (pysim-free).

Streams the card exchange to a GSMTAP receiver (SIMtrace Analyser's
``--capture gsmtap``, Wireshark, simtrace2-sniff) live.  Enabled with
``--gsmtap [HOST[:PORT]]`` only (default ``127.0.0.1:4729``) - there is no UI
or API for it.

Packet form: the 16-byte big-endian header shared by libosmocore's
``gsmtap.h``, simtrace2-sniff (``host/lib/gsmtap.c``: version, ``hdr_len``,
type ``GSMTAP_TYPE_SIM``, sub-type, then the payload), sigrok-iso7816-stream
and the SIMtrace Analyser - version 2, ``hdr_len`` 4 (32-bit words), type
``0x04`` (SIM), sub-type ``0x00`` (``GSMTAP_SIM_APDU``) or ``0x01``
(``GSMTAP_SIM_ATR``; the coding and the field order are the ones in Wireshark's
``epan/dissectors/packet-gsmtap.h``, checked against the local header).  The
payload is the raw bytes.

**The tracer sits on ``PcscLink.send_tpdu``, so it records what
``SCardTransmit`` carries - the exchange the application performs - with no
reconstruction.**  Every transmit becomes one packet
``<command> + <response data> + <status word>``:

* on a **TPDU-mode** reader the transmit is the card's own TPDU: a case-4
  command without its ``Le`` byte, the card's ``61XX``, then the terminal's
  ``00C00000<Le>`` GET RESPONSE carrying the data and the final SW (including
  the second GET RESPONSE of a chained answer) - the wire capture;
* on an **APDU-mode reader running T=0** the reader performs the T=0 mapping
  itself (``SCardTransmit`` returns only the data and the SW), so the emitted
  packet is the command **without** its ``Le`` plus the response data and the
  final SW - neither a wire TPDU nor the logical C-APDU the caller built; the
  card's ``61XX`` and the reader's GET RESPONSE never reach the application;
* on a T=1 reader the transmit is the APDU and the packet is ``cmd + data +
  SW`` - the logical exchange.

The ATR is sent as its own packet (sub-type :data:`GSMTAP_SIM_ATR`) after every
successful connect/reset.  VCC/RST/PPS events are not streamed: PC/SC exposes
no line-level access.

**Reader-mode caveat:** an APDU-mode reader hides its T=0 framing, so a capture
from one pairs a command with the final data/SW (the packet above) while the
card may actually have run T=0; only a TPDU-mode reader yields the true TPDUs.
Nothing here re-encodes or "repairs" a packet - the trace is only as detailed
as the transport it observes.  For a guaranteed wire capture use simtrace2
hardware; this tracer is the application-side view.

Sending is fire-and-forget on a non-blocking UDP socket: a missing listener
must never affect card I/O, and no tracer hook may raise into the transport.

Nothing here imports ``pySim``, ``osmocom`` or ``construct``.
"""

import logging
import socket
import struct
import sys
import time

log = logging.getLogger(__name__)

__all__ = [
    'GSMTAP_VERSION', 'GSMTAP_HDR_LEN', 'GSMTAP_TYPE_SIM', 'GSMTAP_SIM_APDU',
    'GSMTAP_SIM_ATR', 'GSMTAP_UDP_PORT', 'DEFAULT_TARGET',
    'build_packet', 'parse_target', 'GsmtapSender', 'TpduTracer',
    'GsmtapTracer', 'StderrTracer', 'Fanout', 'SendTpduTracer', 'attach',
]

GSMTAP_VERSION = 0x02
GSMTAP_HDR_LEN = 4              # in 32-bit words (16 bytes)
GSMTAP_TYPE_SIM = 0x04

GSMTAP_SIM_APDU = 0x00
GSMTAP_SIM_ATR = 0x01

GSMTAP_UDP_PORT = 4729
DEFAULT_TARGET = '127.0.0.1:%d' % GSMTAP_UDP_PORT

#: The libosmocore header layout (all fields big-endian, 16 bytes).
_HDR_FMT = '!BBBBHBBIBBBB'


def build_packet(sub_type, data, flags=0, slot_nr=0):
    """Build a complete GSMTAP-SIM packet (16-byte header + payload) as bytes."""
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
    """``'HOST[:PORT]'`` -> ``(host, port)``; empty/None -> the default target.

    The port is parsed strictly (``ValueError`` on garbage, an empty port or a
    value outside 0-65535) and the port is validated here rather than left to
    ``socket``: ``sendto`` with an out-of-range port raises ``OverflowError``,
    not ``OSError``, so the sender's fire-and-forget catch would miss it.
    """
    text = str(target or '').strip()
    if not text:
        text = DEFAULT_TARGET
    if ':' in text:
        host, _sep, port = text.rpartition(':')
        if not port:
            raise ValueError('missing port in %r' % text)
        try:
            number = int(port)
        except ValueError:
            raise ValueError('invalid port %r in %r' % (port, text))
        if not 0 <= number <= 65535:
            raise ValueError('port %d out of range (0-65535) in %r'
                             % (number, text))
        return host or '127.0.0.1', number
    return text, GSMTAP_UDP_PORT


class GsmtapSender(object):
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
        except (OSError, OverflowError):
            # A bad port (OverflowError from the C socket call) must not raise
            # into card I/O either - the constructor validates normally.
            pass

    def send_apdu(self, data, slot_nr=0):
        """Stream one wire-shaped TPDU (command, response data and SW)."""
        self.send(GSMTAP_SIM_APDU, data, slot_nr=slot_nr)

    def send_atr(self, atr, slot_nr=0):
        """Stream the card's ATR (sub-type :data:`GSMTAP_SIM_ATR`)."""
        self.send(GSMTAP_SIM_ATR, atr, slot_nr=slot_nr)

    def close(self):
        try:
            self._sock.close()
        except OSError:
            pass


class TpduTracer(object):
    """The transmit-level tracer hook.

    ``trace_tpdu`` is called after every real transmit with the command TPDU,
    the response data, the two status bytes and the elapsed milliseconds;
    ``trace_atr`` after a successful connect/reset and ``trace_reset`` after a
    power cycle.  The callbacks take bytes and must never raise - the wrapper
    swallows an exception so a tracer can never break card I/O.
    """

    def trace_tpdu(self, tpdu, data, sw, elapsed_ms=0.0):
        """One transmit: the command, the response data and the SW."""

    def trace_atr(self, atr):
        """The card's ATR (after connect/reset)."""

    def trace_reset(self):
        """The card was power-cycled."""


class GsmtapTracer(TpduTracer):
    """Send every real transmit and the ATR as GSMTAP-SIM packets."""

    def __init__(self, sender):
        self.sender = sender
        self.packets = 0

    def trace_tpdu(self, tpdu, data, sw, elapsed_ms=0.0):
        payload = bytes(tpdu) + bytes(data) + bytes(sw)
        self.sender.send_apdu(payload)
        self.packets += 1

    def trace_atr(self, atr):
        self.sender.send_atr(bytes(atr))
        self.packets += 1


class StderrTracer(TpduTracer):
    """The ``--apdu-trace`` view: one ASCII line per real transmit.

    The line literals are ASCII only (hex, digits, ``->``) - the console text
    layer's rule (v3.30.3) - so it can be written through ``sys.stderr`` and
    cannot raise ``UnicodeEncodeError``; a closed stream is ignored.
    """

    def __init__(self, stream=None):
        self.stream = stream

    def _write(self, message):
        stream = self.stream if self.stream is not None else sys.stderr
        try:
            stream.write(message + '\n')
        except (OSError, ValueError):
            pass

    def trace_tpdu(self, tpdu, data, sw, elapsed_ms=0.0):
        message = 'APDU-TRACE(%dms): %s -> SW: %s' % (
            int(elapsed_ms), bytes(tpdu).hex(), bytes(sw).hex())
        if data:
            message += ' RESP: %s' % bytes(data).hex()
        self._write(message)

    def trace_atr(self, atr):
        self._write('APDU-TRACE: ATR %s' % bytes(atr).hex())

    def trace_reset(self):
        self._write('APDU-TRACE: RESET')


class Fanout(TpduTracer):
    """Forward every hook to several tracers (e.g. stderr + GSMTAP).

    One tracer failing must not stop the others or reach the transport.
    """

    def __init__(self, tracers):
        self.tracers = [tracer for tracer in tracers if tracer is not None]

    def _forward(self, name, *args):
        for tracer in self.tracers:
            callback = getattr(tracer, name, None)
            if callback is None:
                continue
            try:
                callback(*args)
            except Exception as exc:            # isolation between sinks
                log.debug('%s hook failed: %s', name, exc)

    def trace_tpdu(self, tpdu, data, sw, elapsed_ms=0.0):
        self._forward('trace_tpdu', tpdu, data, sw, elapsed_ms)

    def trace_atr(self, atr):
        self._forward('trace_atr', atr)

    def trace_reset(self):
        self._forward('trace_reset')


class SendTpduTracer(object):
    """Trace the link's real transmits: wrap ``send_tpdu`` and ``connect``.

    Attaching replaces the instance's ``send_tpdu`` (the one method every
    exchange goes through: :meth:`~simple_server.transport.PcscLink.send_apdu`
    passes the T=1 APDU or calls the T=0 transform, and both end in
    ``send_tpdu``), ``connect`` (for the ATR) and ``reset_card`` (for the reset
    marker).  Nothing else is touched, so the exchange stays the reader's own.

    Attaching twice is a no-op - a rebuilt link gets a fresh wrapper, the same
    way the legacy server re-attaches its tracer after every equip.
    """

    def __init__(self, tracer):
        self.tracer = tracer
        self.transmits = 0
        self.resets = 0

    def attach(self, link):
        """Install the wrapper on ``link``; returns ``link``."""
        if link is None or self.tracer is None:
            return link
        if getattr(link, '_tpdu_send_tracer', None) is not None:
            return link
        original_send_tpdu = link.send_tpdu

        def send_tpdu(tpdu):
            started = time.monotonic()
            data, sw = original_send_tpdu(tpdu)
            elapsed_ms = (time.monotonic() - started) * 1000.0
            self.transmits += 1
            self._call('trace_tpdu', bytes(tpdu), bytes(data), bytes(sw),
                       elapsed_ms)
            return data, sw

        link.send_tpdu = send_tpdu
        original_connect = getattr(link, 'connect', None)
        if original_connect is not None:
            def connect():
                result = original_connect()
                self._atr(link)
                return result
            link.connect = connect
        original_reset = getattr(link, 'reset_card', None)
        if original_reset is not None:
            def reset_card():
                # The marker precedes the reset: the power cycle's own ATR
                # (emitted by the wrapped ``connect``) follows it, so the trace
                # reads RESET then ATR.
                self.resets += 1
                self._call('trace_reset')
                return original_reset()
            link.reset_card = reset_card
        link._tpdu_send_tracer = self
        return link

    def _atr(self, link):
        getter = getattr(link, 'get_atr', None)
        if getter is None:
            return
        try:
            atr = getter()
        except Exception as exc:                # not connected / parsed away
            log.debug('ATR read for the trace failed: %s', exc)
            return
        if atr is None:
            return
        self._call('trace_atr', bytes(atr))

    def _call(self, name, *args):
        callback = getattr(self.tracer, name, None)
        if callback is None:
            return
        try:
            callback(*args)
        except Exception as exc:                # a tracer never breaks card I/O
            log.debug('%s hook failed: %s', name, exc)


def attach(link, tracer):
    """Attach the transmit-level tracing to ``link`` (no-op without a tracer).

    ``tracer`` is one :class:`TpduTracer` or a list (the ``--apdu-trace`` /
    ``--gsmtap`` combination is built by the caller, usually as a
    :class:`Fanout`).  Returns ``link`` for chaining.
    """
    if link is None or tracer is None:
        return link
    if isinstance(tracer, (list, tuple)):
        tracer = Fanout(list(tracer))
    return SendTpduTracer(tracer).attach(link)
