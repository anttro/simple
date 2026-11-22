# coding=utf-8
"""The BIP terminal side for one card session (Bearer Independent Protocol).

A UICC opens a data channel with OPEN CHANNEL (``0x40``), moves bytes with
SEND DATA (``0x43``) and RECEIVE DATA (``0x42``), closes it with CLOSE CHANNEL
(``0x41``) and may ask for the open channels with GET CHANNEL STATUS
(``0x44``) - all as proactive commands the terminal answers with a TERMINAL
RESPONSE (TS 102 223 §6.4.27-§6.4.31, the command objects in
§6.6.27-§6.6.31).  The terminal also *initiates* two ENVELOPEs: Data available
(§7.5.10) when server data waits in a channel buffer, and Channel status
(§7.5.11) when a link changes outside a proactive command.

Provenance: a behavioural port of the legacy server's BIP helpers
(``pysim_simple_server/server.py``: ``_bip_channel_id``, ``_bip_tr``,
``_handle_bip_command``, ``_bip_listener_status``, ``_bip_data_available``,
``_bip_session_stop``, ``_bip_status_body``, ``_bip_control`` and
``_bip_flush_channel_events``, plus the ``_BIP``/``_BIP_MODE``/``_BIP_TARGET``/
``_BIP_LISTENER``/``_BIP_OWNER``/``_BIP_LINK_EVENTS`` module globals).  The
channel machinery itself is :class:`simple_server.shared.httpota.BipTerminal`
(pysim-free, moved here from the legacy tree) and the sink listener is
``shared.httpota.TcpDumpServer``.  The module
globals became one :class:`BipSession` per card, the "card is connected" global
became the :attr:`BipSession.card_connected` hook and the CAT exchanges go
through the injected :class:`simple_server.cat_session.CatSession` instead of
pySim's ``scc._tp.send_apdu``.

Spec references (checked against the pinned PDF, TS 102 223 V18.3.0):
§6.4.27-§6.4.31 (OPEN/CLOSE CHANNEL, SEND/RECEIVE DATA, GET CHANNEL STATUS and
which terminal response each expects), §6.4.29 (RECEIVE DATA: "if the
requested number of bytes is available in the buffer but the whole requested
data cannot be included in the TERMINAL RESPONSE because of APDU size limits,
the terminal shall return the maximum number of bytes possible according to
the length of other TLVs.  The terminal shall inform the UICC that the command
has been successfully executed"), §6.6.27-§6.6.31 (their object lists),
§6.8.0/§6.8.1 (TERMINAL RESPONSE: response objects are Min = N, the command
details echo the command's tag form), §7.5.10/§7.5.10.2 (Data available:
Channel status + Channel data length, ``FF`` above 255 bytes), §7.5.11/
§7.5.11.2 (Channel status: link established / link not established), §8.6 (the
OPEN CHANNEL command qualifier, bit 3 = background mode), §8.7 (the device
identities: destination ``21``-``27`` = channel 1-7), §8.52-§8.59 (Bearer
Description, Channel data, Channel data length ``FF`` = "more than 255 bytes",
Buffer size, Channel status coding, Other address, Transport level), §8.56
(the Channel status bytes: b1-b3 the channel identifier, b8 the link state,
byte 4 ``00`` no further info / ``05`` link dropped); TS 101 220 §7.1.1 (the
comprehension-required flag is bit 8 of a tag, so both tag styles occur) and
§7.1.2 Table 7.6 (a value of 128 octets or more takes the BER long-form
length - the live card's 237-byte TLS record is ``36 81 ED``).

One TERMINAL RESPONSE must fit one APDU (a case-3 ``14`` command carries Lc +
data, TS 102 221 §11.2.4; ``cat_session._terminal_response_apdu`` refuses more
than 255 payload octets).  The two places where a card can make the payload
grow past that are handled locally, because an unanswered FETCH is worse than
a shortened answer:

* RECEIVE DATA caps the returned data at :data:`CHANNEL_DATA_MAX` (§6.4.29
  above: return the maximum that fits and report success);
* OPEN CHANNEL caps the echoed Bearer Description at
  :data:`BEARER_ECHO_MAX` (§6.8.20 requires that echo in a successful *and* an
  unsuccessful answer, so it cannot simply be dropped).
"""

import logging
import threading

from simple_server.shared import httpota

from simple_server import proactive
from simple_server import tlv

log = logging.getLogger(__name__)

__all__ = [
    'BipSession', 'MODES', 'OWNER', 'DATA_AVAILABLE_EVENT',
    'CHANNEL_STATUS_EVENT', 'STATUS_LINK_ESTABLISHED',
    'STATUS_LINK_NOT_ESTABLISHED', 'INFO_NO_FURTHER_INFO',
    'INFO_LINK_DROPPED', 'CHANNEL_DATA_MAX', 'BEARER_ECHO_MAX',
    'bip_channel_id', 'bip_terminal_response',
]

#: The session modes the control accepts.  ``sink`` (the default) accepts the
#: card's channels on a local listener that only logs what arrives;
#: ``redirect`` pins one target every channel goes to, whatever address the
#: card requested; ``passthru`` has no listener and no target - each channel
#: dials the destination from the card's OPEN CHANNEL (TCP client only).
MODES = ('sink', 'redirect', 'passthru')

#: The owner a session started through :meth:`BipSession.start` reports.  The
#: legacy server distinguishes the SCP81 listener ('scp81') from the generic
#: BIP pill ('bip'); the caller passes its own name.
OWNER = 'bip'

#: The two events the terminal initiates (TS 102 223 §8.25).
DATA_AVAILABLE_EVENT = 0x09
CHANNEL_STATUS_EVENT = 0x0A

#: Channel status byte 3, bit 8 (TS 102 223 §8.56): the link is established.
STATUS_LINK_ESTABLISHED = 0x80
STATUS_LINK_NOT_ESTABLISHED = 0x00
#: Channel status byte 4 (§8.56): '00' no further info, '05' link dropped.
INFO_NO_FURTHER_INFO = 0x00
INFO_LINK_DROPPED = 0x05

#: The largest Channel data value a RECEIVE DATA TERMINAL RESPONSE can carry.
#: The payload is ``5`` (command details) + ``4`` (device identities) + ``3``
#: (Result) + ``N + 3`` (the Channel data TLV with its ``81 nn`` length) + ``3``
#: (Channel data length) = ``N + 18`` octets; one TERMINAL RESPONSE takes at
#: most 255 (TS 102 221 §11.2.4: Lc is one octet), so ``N <= 237``.
#: TS 102 223 §6.4.29 makes a shortened answer the specified behaviour (the
#: command counts as successfully executed) instead of an unanswered FETCH.
CHANNEL_DATA_MAX = 0xED

#: The largest Bearer Description an OPEN CHANNEL TR can echo.  The successful
#: payload is ``5 + 4 + 3`` (headers) ``+ 4`` (Channel status) ``+ 3 + N`` (the
#: Bearer Description with its ``81 nn`` length) ``+ 4`` (Buffer size) = ``23 +
#: N``, and the unsuccessful one (general Result, no Channel status) ``20 + N``;
#: 232 keeps both inside 255.  §6.8.20 requires the echo in a successful
#: *and* an unsuccessful TR, so an over-long value is cut, not dropped.
BEARER_ECHO_MAX = 0xE8


def bip_channel_id(dev_dst):
    """The channel identifier of a BIP command, or None.

    TS 102 223 §8.7: the device identities code a channel as the destination
    ``21``-``27`` for the identifiers 1-7; anything else carries none.
    """
    return (dev_dst & 0x07) if 0x21 <= dev_dst <= 0x27 else None


def bip_terminal_response(cmd_num, cmd_type, cmd_qual, dev_src, dev_dst,
                          result=0x00, info=None, extra=b'', cd_tag=None):
    """The TERMINAL RESPONSE payload of a BIP command (the ``_bip_tr`` port).

    The command details echo the command's tag form (§6.8.1: "including the
    comprehension required flag") and the response objects use the base (CR=0)
    tags per §6.8.0 (Min = N); the Result precedes the Channel status / data
    objects.  ``info`` is the general result's additional-information octet
    (``03 02 <result> <info>``), ``result`` alone is the basic ``03 01`` form.

    The device identities are fixed to ``82 81`` (terminal -> UICC) exactly as
    the live-tested legacy builder emitted them; ``dev_src``/``dev_dst`` are
    accepted for signature parity with the chain's ``bip_handler`` call.
    Changing them changes what a live card receives, so that is a cutover
    decision, not part of this port (``tests/test_simple_drift.py`` pins the
    parity).
    """
    tr = bytes([(cd_tag or 0x01), 0x03, cmd_num & 0xFF, cmd_type & 0xFF,
                (cmd_qual if cmd_qual is not None else 0) & 0xFF,
                0x02, 0x02, 0x82, 0x81])
    if info is None:
        tr += bytes([0x03, 0x01, result & 0xFF])
    else:
        tr += bytes([0x03, 0x02, result & 0xFF, info & 0xFF])
    return tr + extra


class BipSession(object):
    """One card's BIP state: the command handler, the session, the events.

    **The command handler** is :meth:`handle_command`, the ``bip_handler`` the
    CAT session's chain calls (``callable(cmd_num, cmd_type, cmd_qual, raw,
    dev_src, dev_dst, cd_tag) -> bytes | None``): its return value is the
    TERMINAL RESPONSE payload for a BIP command, ``None`` falls through to the
    generic TR (a command type this class does not own).

    **The session** is :meth:`start`/:meth:`stop`/:meth:`status` - one BIP
    session at a time, whichever control started it: a start replaces the
    previous session and reports it as ``replaced``.  ``terminal`` is the
    channel machinery (default a fresh :class:`httpota.BipTerminal`, shared
    with the rest of the server so one card has one terminal); ``sink`` is the
    ``sink``-mode listener factory (default ``httpota.TcpDumpServer``, called
    ``(host, port, on_rx=..., on_log=...)`` and required to expose ``host``,
    ``port``, ``accepted`` and ``stop()``).

    The session object can be **server-owned** and outlive a card: the
    listener is the terminal side, so :meth:`attach_card`/:meth:`detach_card`
    (called by the server on a re-init/removal) swap the card's CAT session
    and close the old card's channels while the listener, mode, target and
    owner stay - a listener started before a card, or kept across a swap,
    serves the next card's OPEN CHANNEL.  :meth:`start_listener` starts a
    session around a listener built elsewhere (the SCP81 PSK TLS server,
    :mod:`simple_server.scp81`), so the one-session rule covers both controls.

    **The events** are :meth:`data_available` (the terminal's ``on_data``
    monitor callback: tells the card that server bytes wait, §7.5.10) and
    :meth:`flush_channel_events` (the CAT session's ``bip_flush`` target: the
    link changes the terminal detected outside a proactive command, §7.5.11).
    Both run under ``card_lock`` (the server passes its global lock, so the same
    lock serialises the whole card exchange) and both re-check the state under
    it.

    ``card_lock`` **must be reentrant** (a ``threading.RLock``): while
    :meth:`data_available` holds it, the ENVELOPE it sends runs the card chain,
    whose end calls :meth:`flush_channel_events` - which takes the same lock
    again on the same thread.  With a plain ``threading.Lock`` that would
    deadlock with the card held.  It defaults to the injected ``cat_session``'s
    own ``card_lock`` when that session has one (one lock per card, so a BIP
    ENVELOPE can never interleave a FETCH/TERMINAL RESPONSE pair), else to a
    fresh ``threading.RLock``.  :meth:`start` and :meth:`stop` take it too: they
    mutate the terminal (``disable`` closes the channels), which must not race
    a BIP command being answered.

    ``link_events`` gates the Channel status event (the legacy
    ``_BIP_LINK_EVENTS``: off drains the queue without sending);
    :meth:`start` sets it from the control's ``link_events`` field.

    ``card_connected`` is a ``callable() -> bool`` the server supplies for the
    legacy ``_CARD_CONNECTED`` check; without it a present ``cat_session`` is
    the card session (a BIP session is only created/attached for an equipped
    card).

    ``cat_session`` must provide ``event_list`` (the card's SET UP EVENT LIST),
    ``busy``, ``stk_pending`` and
    ``send_event_download(event_type, data, drain=True)`` - the real
    :class:`simple_server.cat_session.CatSession` plus a small fake for tests.
    """

    def __init__(self, cat_session=None, *, card_lock=None, link_events=True,
                 terminal=None, sink=None, card_connected=None):
        if card_lock is None and cat_session is not None:
            # One lock per card: the session's own, so the events cannot
            # interleave the chain's FETCH/TERMINAL RESPONSE pair.
            card_lock = getattr(cat_session, 'card_lock', None)
        self.cat_session = cat_session
        self.card_lock = (card_lock if card_lock is not None
                          else threading.RLock())
        self.link_events = bool(link_events)
        self.terminal = (terminal if terminal is not None
                         else httpota.BipTerminal())
        self._sink_factory = (sink if sink is not None
                              else httpota.TcpDumpServer)
        self.card_connected = card_connected
        # The active session (see start/stop/status).
        self.owner = None
        self.mode = None
        self.target = None
        self.listener = None
        self._state_lock = threading.RLock()
        self._flushing = False

    # ---- the command handler ---------------------------------------------

    def handle_command(self, cmd_num, cmd_type, cmd_qual, raw, dev_src,
                       dev_dst, cd_tag=None):
        """Answer one BIP proactive command; ``None`` = the generic TR.

        Port of the legacy ``_handle_bip_command``.  OPEN CHANNEL echoes the
        requested Bearer Description and Buffer size and reports the Channel
        status, with the deliberately permissive destination handling the live
        card needs (a truncated or empty Other address / Transport level is
        informational only - the emulation redirects every channel to its
        configured target and logs ``open-relaxed``).  Every failure is the
        spec's "Bearer Independent Protocol error" result ``3A`` (§8.12.0) -
        except that a failed OPEN CHANNEL still echoes the Bearer Description
        and Buffer size, the objects its successful TR carries.
        """
        if cd_tag is None:
            cd_tag = self._cd_tag_of(raw)

        def _tr(result=0x00, info=None, extra=b''):
            return bip_terminal_response(cmd_num, cmd_type, cmd_qual, dev_src,
                                         dev_dst, result, info, extra,
                                         cd_tag=cd_tag)

        tlvs = proactive.command_tlv_map(raw)
        channel = bip_channel_id(dev_dst)
        if cmd_type == 0x40:
            # Buffer size (8.55) and Bearer Description (8.52): the terminal
            # reports what it offers in the TR - the requested value is echoed
            # (the reference traces' TR carries both objects).  §6.8.20 makes
            # the Bearer Description mandatory in a successful *and* an
            # unsuccessful answer, so an over-long value is cut to what one
            # APDU can carry instead of being dropped (only a nonsensically
            # large bearer reaches that size within the command's own 255
            # octets).
            bs = (proactive.cmd_tlv(tlvs, proactive.TAG_BUFFER_SIZE)
                  or b'\x02\x00')
            buffer_size = (int.from_bytes(bs[:2], 'big')
                           if len(bs) >= 2 else 0x0200)
            bearer = proactive.cmd_tlv(tlvs, proactive.TAG_BEARER) or b'\x03'
            echo = bearer[:BEARER_ECHO_MAX]
            if len(echo) != len(bearer):
                self.terminal.log(
                    'open-bearer-capped', bytes=len(bearer),
                    echoed=len(echo),
                    note='the bearer echo does not fit one TERMINAL RESPONSE')
            extra = (bytes([proactive.TAG_BEARER])
                     + tlv.encode_ber_len(len(echo)) + echo)
            extra += bytes([proactive.TAG_BUFFER_SIZE, 0x02]) + \
                buffer_size.to_bytes(2, 'big')
            if not self.terminal.enabled:
                self.terminal.log('open-unavailable', reason='BIP not enabled')
                return _tr(0x3A, 0x00, extra)
            addr = proactive.parse_other_address(
                proactive.cmd_tlv(tlvs, proactive.TAG_OTHER_ADDRESS))
            proto, port = proactive.parse_transport_level(
                proactive.cmd_tlv(tlvs, proactive.TAG_TRANSPORT_LEVEL))
            if not addr or port is None:
                # Emulation is deliberately permissive: the APN, destination
                # and transport are informational, the channel always goes to
                # the configured target (the live card emits truncated/empty
                # destination TLVs).
                self.terminal.log('open-relaxed', address=addr, port=port,
                                  note='destination/transport not fully '
                                       'specified')
            cid, _err = self.terminal.open(addr or '-', port or 0, buffer_size,
                                           proto=proto)
            if cid is None:
                return _tr(0x3A, 0x00, extra)
            if cmd_qual and (cmd_qual & 0x04):
                # Background mode (§8.6 bit 3): the terminal reports the link
                # via the Channel status event, not only in the TR (§6.4.27).
                self.terminal._queue_link_status(
                    cid, status=STATUS_LINK_ESTABLISHED | (cid & 0x07),
                    info=INFO_NO_FURTHER_INFO)
            return _tr(0x00, None, self._channel_status(cid) + extra)
        if cmd_type == 0x43:
            data = proactive.cmd_tlv(tlvs, proactive.TAG_CHANNEL_DATA)
            if channel is None or not self.terminal.send(channel, data):
                return _tr(0x3A, 0x00)
            # §6.4.30: the successful TR carries the free space of the buffer
            # for data *to* the card (8.54, FF = more than 255 bytes).
            # ``send_capacity`` reports the Rx buffer's free space (the legacy
            # terminal's metric) - kept for parity; an under-reported value
            # only makes the card split its next send.  TODO: report the Tx
            # space the spec names.
            length = self.terminal.send_capacity(channel)
            return _tr(0x00, None, bytes([proactive.TAG_CHANNEL_DATA_LENGTH,
                                          0x01, length & 0xFF]))
        if cmd_type == 0x42:
            req = (proactive.cmd_tlv(tlvs,
                                    proactive.TAG_CHANNEL_DATA_LENGTH)
                   or b'\x00')
            n = req[0] if req else 0
            if channel is None:
                return _tr(0x3A, 0x00)
            # §6.4.29: when the whole requested amount does not fit the
            # TERMINAL RESPONSE, return what fits and report success (the
            # answer must never be skipped - an unanswered FETCH is worse).
            n = min(n, CHANNEL_DATA_MAX)
            data = self.terminal.receive(channel, n)
            if data is None:
                return _tr(0x3A, 0x00)
            # Belt and braces: a terminal that ignores ``maxlen`` must not be
            # able to make the payload overflow either.
            data = bytes(data)[:CHANNEL_DATA_MAX]
            remaining = self.terminal.available(channel)
            extra = b''
            # Deliberate deviation (§6.4.29 asks "Command performed with
            # missing information", 02, when fewer than the requested bytes are
            # buffered): an empty or partial answer is still reported 00, the
            # live-tested legacy behaviour (docs/pysim-phaseout.md).
            if data:
                # §8.53/§8.54 with the TS 101 220 §7.1.2 Table 7.6 length: a
                # single length octet only up to 127, then the long form (the
                # live card's 237-byte record is `36 81 ed`; a raw octet above
                # 0x7F is read as a long-form indicator, the bytes never reach
                # the card's TLS layer).
                if len(data) <= 0x7F:
                    extra = bytes([proactive.TAG_CHANNEL_DATA,
                                   len(data)]) + data
                else:
                    extra = bytes([proactive.TAG_CHANNEL_DATA, 0x81,
                                   len(data)]) + data
            extra += bytes([proactive.TAG_CHANNEL_DATA_LENGTH, 0x01,
                            0xFF if remaining > 0xFF else remaining])
            return _tr(0x00, None, extra)
        if cmd_type == 0x41:
            if channel is None or not self.terminal.close(channel):
                return _tr(0x3A, 0x00)
            return _tr(0x00)
        if cmd_type == 0x44:
            # §6.4.31: one Channel status object per dedicated channel.
            extra = b''.join(self._channel_status(cid)
                             for cid in sorted(self.terminal.channels))
            return _tr(0x00, None, extra)
        return None

    @staticmethod
    def _cd_tag_of(raw):
        """The command-details tag the card used (``01``/``81``), or None."""
        if not raw:
            return None
        try:
            return proactive.parse_proactive_header(raw)[5]
        except Exception as exc:                    # a decoder bug
            log.warning('BIP command header parse failed: %s (raw=%s)', exc,
                        bytes(raw).hex())
            return None

    @staticmethod
    def _channel_status(channel_id, info=INFO_NO_FURTHER_INFO):
        """One Channel status object of an established link (§8.56)."""
        return bytes([proactive.TAG_CHANNEL_STATUS, 0x02,
                      STATUS_LINK_ESTABLISHED | (channel_id & 0x07),
                      info & 0xFF])

    # ---- the session lifecycle -------------------------------------------

    def status(self):
        """The session state: owner, terminal, listener (the legacy shape)."""
        return {'owner': self.owner, 'bip': self.terminal.status(),
                'listener': self._listener_status()}

    def _listener_status(self):
        """The listener block of :meth:`status`.

        The legacy ``_bip_listener_status``: without a listener object a
        redirect session names its pinned target and a passthru session reports
        the mode only (the per-channel targets are in the BIP block).  A sink
        listener exposes its bound port - the control's ``port`` 0 means
        ephemeral, so this is where the operator finds the real one.  The
        legacy SCP81 TLS branch belongs to the SCP81 listener module
        (:mod:`simple_server.scp81`), not here.
        """
        if not self.listener:
            if self.mode == 'redirect' and self.target:
                return {'mode': 'redirect', 'host': self.target[0],
                        'port': self.target[1],
                        'target': '%s:%d' % self.target}
            if self.mode == 'passthru':
                return {'mode': 'passthru'}
            return None
        # The sink/dump listener counts its accepted connections; a listener
        # built elsewhere (the SCP81 PSK TLS server) may not expose that, and
        # its own block (identities, handshake) belongs to the SCP81 control.
        status = {'mode': self.mode, 'host': self.listener.host,
                  'port': self.listener.port}
        accepted = getattr(self.listener, 'accepted', None)
        if accepted is not None:
            status['connections'] = accepted
        return status

    def attach_card(self, cat_session):
        """Bind the session to a card's CAT session (an equip/re-init).

        The session object is server-owned and survives a card change (the
        listener is the terminal side, not per-card state); the card-specific
        half is this hook and :meth:`detach_card`.  A no-op for the same
        session (a re-report of the current card must not disturb the state
        the running events read).
        """
        with self.card_lock:
            with self._state_lock:
                if self.cat_session is cat_session:
                    return
                self.cat_session = cat_session

    def detach_card(self):
        """A card change: keep the session, drop the old card's channels.

        The channels belong to the card that opened them: they are closed and
        their queued Channel status events dropped (there is no card to
        notify), while the listener, mode, target, owner and ``link_events``
        stay - a listener started before the card, or kept across a swap,
        simply serves the next card's OPEN CHANNEL.  The two legacy controls
        shared one terminal and never reset it on a card change; the port
        closes the stale sockets explicitly (the legacy leaked them until the
        next control call).

        The close runs **before** the drain, and every channel is marked
        ``closed_reported`` first: the terminal's 250 ms monitor may still
        hold a snapshot of the channel list after this method removed them,
        and its peer-close branch would otherwise queue a fresh link-status
        event for the gone card into the next card's session.
        """
        with self.card_lock:
            with self._state_lock:
                self.cat_session = None
                for channel in list(self.terminal.channels.values()):
                    channel.closed_reported = True
                self.terminal.close_all(link_lost=False)
                self.terminal.take_pending_events()

    def start_listener(self, listener, owner=OWNER, mode=None,
                       link_events=None):
        """Start (or replace) a session around a listener built by the caller.

        The SCP81 control builds its PSK TLS / dump listener itself
        (:mod:`simple_server.scp81`) and hands it here, so the one-session
        rule and the terminal wiring stay in this class: the previous session
        (whichever control started it) is stopped and its description
        returned for the ``replaced`` field.  The listener is assumed bound
        already; ``owner`` names the control (``scp81``/``bip``) and ``mode``
        the listener kind (``tls``/``dump``/``sink``).  ``link_events``
        overrides the Channel status events when given.  Returns the replaced
        session description (or ``None``); the caller reads the new state
        from :meth:`status` (which knows the caller's own listener block).
        """
        host, port = listener.host, int(listener.port)
        with self.card_lock:
            with self._state_lock:
                replaced = self.stop()
                if link_events is not None:
                    self.link_events = bool(link_events)
                self.terminal.on_data = self.data_available
                self.listener = listener
                self.mode = mode
                self.target = (host, port)
                self.terminal.enable(host, port, mode='redirect')
                self.owner = owner
                return replaced

    def start(self, body=None, owner=OWNER):
        """Start (or replace) the BIP session; returns the status + ``ok``.

        Port of the legacy ``_bip_control``: ``action: "stop"`` stops and
        reports, otherwise ``mode`` (``sink`` default / ``redirect`` /
        ``passthru``), ``host``/``port`` and ``link_events`` configure the new
        session.  A validation error leaves a running session untouched (the
        legacy order: validate first, then replace).  The new session's
        terminal gets :meth:`data_available` as its ``on_data`` callback.

        A listener bind failure (the port is taken) is answered as
        ``{'ok': False, 'error': ...}`` instead of the legacy handler's
        uncaught ``OSError``; the started listener is stopped and the session
        cleared either way.

        The state change runs under ``card_lock``: it disables the terminal
        (closing its channels), which must not race a BIP command that is
        being answered (the legacy got this from the HTTP handler's blanket
        card lock).
        """
        body = body or {}
        action = body.get('action', 'start')
        if action == 'stop':
            replaced = self.stop()
            resp = self.status()
            resp.update({'ok': True, 'replaced': replaced})
            return resp
        mode = body.get('mode', 'sink')
        if mode not in MODES:
            return {'ok': False, 'error': 'unsupported mode: %s' % mode}
        host = (body.get('host') or '').strip() or '127.0.0.1'
        raw_port = body.get('port')
        port = None
        if mode == 'redirect':
            if not body.get('host') or raw_port in (None, ''):
                return {'ok': False,
                        'error': 'redirect mode requires the target host '
                                 'and port'}
            try:
                port = int(raw_port)
            except (TypeError, ValueError):
                return {'ok': False, 'error': 'port is not a number'}
            if not 0 < port <= 0xFFFF:
                return {'ok': False, 'error': 'port must be 1-65535'}
        elif mode == 'sink':
            try:
                port = int(raw_port) if raw_port not in (None, '') else 0
            except (TypeError, ValueError):
                return {'ok': False, 'error': 'port is not a number'}
            if not 0 <= port <= 0xFFFF:
                return {'ok': False,
                        'error': 'port must be 0-65535 (0 = ephemeral)'}
        with self.card_lock:
            with self._state_lock:
                replaced = self.stop()
                self.link_events = bool(body.get('link_events', True))
                self.terminal.on_data = self.data_available
                try:
                    if mode == 'passthru':
                        # No listener and no pinned target: every channel
                        # dials the destination the card requests in OPEN
                        # CHANNEL.
                        self.mode = 'passthru'
                        self.target = None
                        self.terminal.enable(mode='passthru')
                    elif mode == 'redirect':
                        self.mode = 'redirect'
                        self.target = (host, port)
                        self.terminal.enable(host, port, mode='redirect')
                    else:
                        self.listener = self._sink_factory(
                            host, port,
                            on_rx=lambda peer, data: self.terminal.log(
                                'sink-rx', peer=peer, bytes=len(data),
                                hex=data.hex().upper()[:2000]),
                            on_log=lambda kind, **fields: self.terminal.log(
                                kind, **fields))
                        self.mode = 'sink'
                        self.target = (self.listener.host, self.listener.port)
                        self.terminal.enable(self.listener.host,
                                             self.listener.port,
                                             mode='redirect')
                except OSError as exc:
                    # A listener that bound but could not be wired up must be
                    # released, not just forgotten (its thread holds the port).
                    if self.listener is not None:
                        self.listener.stop()
                    self.listener = None
                    self.mode = None
                    self.target = None
                    self.terminal.disable()
                    return {'ok': False, 'error': str(exc)}
                self.owner = owner
                resp = self.status()
                resp.update({'ok': True, 'replaced': replaced})
                return resp

    def stop(self, lock=True):
        """Stop the active session; returns what was replaced (or None).

        Port of the legacy ``_bip_session_stop``: the listener is stopped, the
        mode/target/owner cleared and the terminal disabled (which closes its
        channels and queues their Channel status events, exactly like the
        legacy ``_BIP.disable()``).  Runs under ``card_lock`` (see
        :meth:`start`); it nests fine inside a caller that already holds it
        when that lock is reentrant.

        ``lock=False`` skips the card lock for a shutdown
        (:meth:`~simple_server.server.ServerState.close`): the lock is
        acquired with a timeout there, and a listener stop must not block the
        process exit behind a blocking card exchange.  It is safe because
        ``_stop_locked``'s channel cleanup is the terminal's ``disable()`` - a
        local socket close, not a card exchange - and the teardown's
        ``detach_card`` repeats the same cleanup idempotently.
        """
        if not lock:
            return self._stop_locked()
        with self.card_lock:
            return self._stop_locked()

    def _stop_locked(self):
        with self._state_lock:
            replaced = None
            if self.owner:
                replaced = {'owner': self.owner, 'mode': self.mode}
            if self.listener is not None:
                self.listener.stop()
                self.listener = None
            self.mode = None
            self.target = None
            self.owner = None
            self.terminal.disable()
            return replaced

    # ---- the terminal-initiated events -----------------------------------

    def data_available(self, ch):
        """The ``on_data`` callback: tell the card there is data to fetch.

        Port of the legacy ``_bip_data_available``.  TS 102 223 §7.5.10: when
        the event is in the card's list and the channel buffer was empty when
        new data arrived, the terminal sends ENVELOPE (Data available) with
        the Channel status and the number of bytes waiting (§7.5.10.2:
        Channel data length, ``FF`` above 255 bytes); the card then issues
        RECEIVE DATA.  Returns True when the event was sent - the terminal
        then marks the bytes as notified and does not repeat it until the
        buffer grows or two seconds pass.

        The card must be idle: an ENVELOPE never interleaves a FETCH/TERMINAL
        RESPONSE pair (the legacy order - the cheap checks first, then the
        lock, then the same checks again under it).  The pre-lock ``busy`` /
        ``stk_pending`` return is a deliberate lock-avoidance the legacy did
        not have (it checked only under its lock): a background monitor thread
        must not queue behind a long card operation just to find the card busy.
        """
        cat = self.cat_session
        if cat is None or ch.peer_closed:
            return False
        if not self._card_connected():
            return False
        if DATA_AVAILABLE_EVENT not in (cat.event_list or []) or not ch.rx:
            return False
        if cat.busy or cat.stk_pending:
            return False
        with self.card_lock:
            if not self._card_connected() or cat.busy or cat.stk_pending:
                return False
            length = min(len(ch.rx), 0xFF)
            tlv = bytes([proactive.TAG_CHANNEL_STATUS | 0x80, 0x02,
                         STATUS_LINK_ESTABLISHED | (ch.id & 0x07),
                         INFO_NO_FURTHER_INFO,
                         proactive.TAG_CHANNEL_DATA_LENGTH | 0x80, 0x01,
                         length])
            try:
                cat.send_event_download(DATA_AVAILABLE_EVENT, tlv)
            except Exception as exc:
                self.terminal.log('data-available-skip', channel=ch.id,
                                  reason=str(exc))
                log.warning('BIP data available skipped: %s', exc)
                return False
            self.terminal.log('data-available', channel=ch.id, length=length)
            return True

    def flush_channel_events(self):
        """Send the queued Channel status events (the ``bip_flush`` target).

        Port of the legacy ``_bip_flush_channel_events``.  TS 102 223 §7.5.11:
        a link change detected outside a proactive command (a dropped peer, a
        closed channel) is reported as ENVELOPE (Channel status) - only when
        the card subscribed and ``link_events`` is on; with ``link_events``
        off the queue is drained without sending.  The caller calls this when
        the chain is idle (never while a fetched command awaits its TERMINAL
        RESPONSE), and the ``_flushing`` guard keeps a nested call from
        re-entering.  A failing send drops the rest of the batch (one card
        error must not turn into a retry loop) and never raises.

        The ``link_events`` check comes before the session check (the legacy
        order): with the events switched off the queue is drained even while
        there is no card session, so nothing stale survives into the next one.
        """
        if self._flushing:
            return
        if not self.link_events:
            self.terminal.take_pending_events()
            return
        cat = self.cat_session
        if cat is None:
            return
        if CHANNEL_STATUS_EVENT not in (cat.event_list or []):
            return
        with self.card_lock:
            if self._flushing:
                return
            events = self.terminal.take_pending_events()
            if not events:
                return
            self._flushing = True
            try:
                for ev in events:
                    cat.send_event_download(
                        CHANNEL_STATUS_EVENT,
                        bytes([proactive.TAG_CHANNEL_STATUS | 0x80, 0x02,
                               ev['status'] & 0xFF, ev['info'] & 0xFF]))
            except Exception as exc:
                log.warning('Channel status event failed: %s', exc)
            finally:
                self._flushing = False

    def _card_connected(self):
        """The injected card-session check (a present session by default)."""
        if self.card_connected is None:
            return self.cat_session is not None
        try:
            return bool(self.card_connected())
        except Exception as exc:
            log.warning('card_connected hook failed: %s', exc)
            return False
