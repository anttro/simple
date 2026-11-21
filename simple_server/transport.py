# coding=utf-8
"""Minimal PC/SC transport (pyscard) with a T=0 logical APDU exchange.

This module is the only part of :mod:`simple_server` that touches ``pyscard``.
It replaces pySim's ``LinkBase``/``PcscSimLink`` for the parts the server
needs: exclusive reader connect/disconnect, a raw TPDU transmit, and the
logical ``send_apdu`` that applies the T=0 rules of TS 102 221 §7.3.1.1
(TPDU case transform, GET RESPONSE chaining, ``6Cxx`` re-send).

Everything else in the package is pure; the tests drive this module with a
fake connection (``PcscLink(connection=...)``) and a patched
``smartcard.System.readers``.
"""

import logging

import smartcard.System
from smartcard.ATR import ATR
from smartcard.CardConnection import CardConnection
from smartcard.CardRequest import CardRequest
from smartcard.Exceptions import (CardConnectionException,
                                  CardRequestTimeoutException, NoCardException)
from smartcard.ExclusiveConnectCardConnection import ExclusiveConnectCardConnection

from simple_server.util import parse_command_apdu, sw_matches

log = logging.getLogger(__name__)

__all__ = [
    'ApduTracer', 'ProactiveHandler', 'ReaderError', 'SwMatchError',
    'PcscLink', 'detect_reader', 'T0_MAX_ROUNDS',
]

#: The chaining bound of the T=0 exchange.  TS 102 221 §7.3.1.1 makes a
#: conforming card answer ``61xx``/``9Fxx`` with the remaining bytes and
#: ``62xx``/``63xx`` with one dummy GET RESPONSE, so a real exchange needs one
#: or two rounds; 8 is far above that.  The cap keeps a card that never
#: finishes chaining (a non-draining ``61xx`` or a repeating ``62xx``/``63xx``)
#: from holding the caller - and the card lock - forever.
T0_MAX_ROUNDS = 8


class ApduTracer(object):
    """No-op APDU tracer hook.

    The callbacks take lowercase hex strings (``cmd``/``sw``/``resp``), the
    same shape the existing ``StderrApduTracer``/``GsmtapApduTracer`` use.
    """

    def trace_command(self, cmd):
        """Called before a command APDU is sent (hex string)."""

    def trace_response(self, cmd, sw, resp):
        """Called after the exchange (command hex, SW hex, response hex)."""

    def trace_reset(self):
        """Called when the card is reset."""


class ProactiveHandler(object):
    """No-op proactive-command hook; the server subclasses it later."""

    def receive_fetch_raw(self, pcmd, parsed):
        """Handle a fetched proactive command; None = nothing to add."""
        return None

    def prepare_response(self, pcmd, general_result='performed_successfully'):
        """Build the response TLV objects; [] = none."""
        return []


class ReaderError(RuntimeError):
    """No reader, no card, an unsupported protocol or an unusable link."""


class SwMatchError(Exception):
    """The response status word does not match the expected one."""

    def __init__(self, sw, expected):
        self.sw = sw
        self.expected = expected
        super().__init__('Status word %s does not match the expected %s'
                         % (sw, expected))


def detect_reader():
    """Return the name of the first PC/SC reader, or raise :class:`ReaderError`."""
    available = smartcard.System.readers()
    if not available:
        raise ReaderError('No PC/SC reader found; check pcscd and the reader '
                          'connection')
    return available[0].name


class PcscLink(object):
    """A PC/SC card link with the T=0 APDU rules.

    ``reader`` is a reader name, an integer index into
    ``smartcard.System.readers()`` or ``None`` (the first reader).  ``tracer``
    is an :class:`ApduTracer`-shaped object (duck-typed) and
    ``proactive_handler`` a :class:`ProactiveHandler`-shaped one.

    ``connection`` injects an already-built connection object (the tests'
    fake); when it is given, no reader is resolved and the caller owns the
    connection's lifetime.  This keeps the link testable without a reader.
    """

    def __init__(self, reader=None, tracer=None, proactive_handler=None,
                 connection=None):
        self.apdu_tracer = tracer
        self.proactive_handler = proactive_handler
        self.protocol = 0               # 0 = T=0 (TPDU), 1 = T=1 (APDU)
        self._con = connection
        self._reader = None
        if connection is None:
            self._reader = self._pick_reader(reader)
        self.reader_name = self._reader.name if self._reader is not None else None

    @staticmethod
    def _pick_reader(reader):
        available = smartcard.System.readers()
        if reader is None:
            if not available:
                raise ReaderError('No PC/SC reader found; check pcscd and the '
                                  'reader connection')
            return available[0]
        if isinstance(reader, int) and not isinstance(reader, bool):
            if not 0 <= reader < len(available):
                raise ReaderError('No PC/SC reader at index %d (%d found)'
                                  % (reader, len(available)))
            return available[reader]
        if isinstance(reader, str):
            for candidate in available:
                if candidate.name == reader:
                    return candidate
            raise ReaderError('PC/SC reader %r not found (available: %s)'
                              % (reader,
                                 ', '.join(r.name for r in available) or 'none'))
        raise TypeError('reader must be a name string, an index or None')

    # ---- connection management ------------------------------------------

    def _ensure_connection(self):
        if self._con is None:
            if self._reader is None:
                raise ReaderError('No PC/SC reader selected')
            self._con = ExclusiveConnectCardConnection(
                self._reader.createConnection())
        return self._con

    def connect(self):
        """Connect to the card exclusively and prefer T=0 when the ATR has it."""
        con = self._ensure_connection()
        # Release a previous handle first, like pySim's PcscSimLink: a second
        # SCardConnect without a disconnect would leak the old card handle.
        self.disconnect()
        try:
            con.connect()
        except NoCardException as exc:
            raise ReaderError('No card in the reader') from exc
        except CardConnectionException as exc:
            raise ReaderError('PC/SC connection failed: %s' % exc) from exc
        self._select_protocol(con)
        return self

    def _select_protocol(self, con):
        """Select T=0/T=1 from the ATR; keep the reader default if unparseable."""
        try:
            atr = ATR(con.getATR())
        except Exception as exc:
            # A mocked or unusual connection: the card is already connected,
            # so keep the reader's negotiated protocol and stay on T=0 rules.
            log.debug('ATR could not be parsed (%s); keeping the default '
                      'protocol', exc)
            return
        if atr.isT0Supported():
            con.setProtocol(CardConnection.T0_protocol)
            self.protocol = 0
        elif atr.isT1Supported():
            con.setProtocol(CardConnection.T1_protocol)
            self.protocol = 1
        else:
            raise ReaderError('Unsupported card protocol: the ATR advertises '
                              'neither T=0 nor T=1')

    def disconnect(self):
        """Disconnect; a removed card's dead handle is not an error."""
        if self._con is None:
            return
        try:
            self._con.disconnect()
        except Exception as exc:
            log.debug('PC/SC disconnect failed: %s', exc)

    def wait_for_card(self, timeout=None, newcardonly=False):
        """Wait for a card (``timeout`` in seconds, None = forever) and connect."""
        if self._reader is None:
            # An injected connection cannot poll for a card; just connect.
            self.connect()
            return
        request = CardRequest(readers=[self._reader], timeout=timeout,
                              newcardonly=newcardonly)
        try:
            request.waitforcard()
        except CardRequestTimeoutException as exc:
            raise ReaderError('No card inserted within %s s' % timeout) from exc
        self.connect()

    def get_atr(self):
        """The card's ATR as :class:`bytes`."""
        con = self._con
        if con is None:
            raise ReaderError('PC/SC link is not connected')
        return bytes(con.getATR())

    def reset_card(self):
        """Power-cycle the card (disconnect + connect), like pySim's reset."""
        if self.apdu_tracer:
            self.apdu_tracer.trace_reset()
        self.disconnect()
        self.connect()
        return 1

    # ---- APDU exchange ---------------------------------------------------

    def send_tpdu(self, tpdu):
        """Transmit one raw TPDU; returns ``(data, sw)`` with a 2-byte SW."""
        con = self._con
        if con is None:
            raise ReaderError('PC/SC link is not connected')
        data, sw1, sw2 = con.transmit(list(bytes(tpdu)))
        return bytes(data), bytes((sw1, sw2))

    def send_apdu(self, apdu):
        """Send a command APDU and return ``(data, sw)`` (both :class:`bytes`).

        The APDU is parsed per ISO/IEC 7816-3 Table 12 first (a malformed one
        raises before anything reaches the card), traced, exchanged, and the
        response traced.  With T=0 the TPDU transform/GET RESPONSE rules of
        TS 102 221 §7.3.1.1 apply; with T=1 the APDU is passed through.
        """
        apdu = bytes(apdu)
        case, _lc, _le, _data = parse_command_apdu(apdu)
        if self.apdu_tracer:
            self.apdu_tracer.trace_command(apdu.hex())
        if self.protocol == 1:
            data, sw = self.send_tpdu(apdu)
        else:
            data, sw = self._send_t0(apdu, case)
        if self.apdu_tracer:
            self.apdu_tracer.trace_response(apdu.hex(), sw.hex(), data.hex())
        return data, sw

    def _send_t0(self, apdu, case, max_rounds=None):
        """The T=0 TPDU transform and response chaining (TS 102 221 §7.3.1.1).

        ``max_rounds`` bounds the GET RESPONSE rounds (default
        :data:`T0_MAX_ROUNDS`): when it is exhausted the last ``(data, sw)``
        is returned with a warning instead of following the card forever.
        """
        if max_rounds is None:
            max_rounds = T0_MAX_ROUNDS
        if case == 1:
            # 7.3.1.1.1: attach an Le octet to a case 1 APDU.
            tpdu = apdu + b'\x00'
        elif case == 4:
            # 7.3.1.1.4: remove the Le octet; the data comes via GET RESPONSE.
            tpdu = apdu[:-1]
        else:
            tpdu = apdu
        previous = tpdu
        data, sw = self.send_tpdu(tpdu)
        rounds = 0
        while True:
            if sw in (b'\x90\x00', b'\x91\x00'):
                # 9000 / 9100: no (more) response data available.
                break
            if sw[:1] in (b'\x61', b'\x9f'):
                # 61XX / 9FXX: XX bytes available; fetch them.
                le = sw[1:2]
            elif sw[:1] in (b'\x62', b'\x63'):
                # 62XX / 63XX: the card asks for a dummy GET RESPONSE (Le 00).
                le = b'\x00'
            else:
                break
            if rounds >= max_rounds:
                log.warning('T=0 chaining still answers %s after %d GET '
                            'RESPONSE round(s); returning the last response',
                            sw.hex(), rounds)
                break
            rounds += 1
            previous = tpdu[:1] + b'\xc0\x00\x00' + le
            more, sw = self.send_tpdu(previous)
            data += more
        if sw[:1] == b'\x6c':
            # 6CXX: wrong Le; repeat the TPDU with the SW's length.
            data, sw = self.send_tpdu(previous[:4] + sw[1:2])
        return data, sw

    def send_apdu_checksw(self, apdu, sw_match='9000'):
        """Send an APDU and require a status word (case-insensitive).

        ``sw_match`` is an exact status word (``'9000'``) or a ``?`` mask
        where ``?`` matches one hex digit - ``'9???'`` accepts ``9000``,
        ``91XX`` and ``9FXX``; ``'6A??'`` accepts any ``6Axx``.  A mismatch
        raises :class:`SwMatchError`.
        """
        data, sw = self.send_apdu(apdu)
        expected = sw_match.lower() if isinstance(sw_match, str) else sw_match
        if not sw_matches(sw, expected):
            raise SwMatchError(sw.hex(), expected)
        return data, sw
