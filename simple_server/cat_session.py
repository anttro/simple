# coding=utf-8
"""The CAT session engine: FETCH/TERMINAL RESPONSE, ENVELOPE, menu, timers.

A UICC announces a proactive command with ``91XX`` instead of ``9000`` after a
command exchange (TS 102 223 §6.3: the same "OK", plus a command waiting); the
terminal FETCHes it (TS 102 221 §11.2.3) and must always answer with a
TERMINAL RESPONSE (§6.8), which may itself raise the next ``91XX``.
A terminal-initiated action (a secured packet, an event download) is an
ENVELOPE (TS 102 221 §11.2.2, CLA ``C2``) and may raise commands the same way.
This module owns that conversation for one card: the chain, the ENVELOPE
helpers, the PLI dictionary answers, the interactive menu state, the
TIMER MANAGEMENT timers and the proactive log the PWA renders.

Provenance: a behavioural port of the legacy server's CAT helpers
(the legacy 3.x ``server.py``: ``_send_status``,
``_send_terminal_profile``, ``_handle_proactive_chain`` /
``_run_proactive_chain``, ``_send_envelope`` / ``_envelope_get_response``,
``_send_event_download``, ``_pli_data_hex`` / ``PLI_DEFAULTS``,
``_build_tr``, ``_record_tr`` / ``_log_proactive`` /
``_reset_proactive_log`` / ``_mark_proactive_session``,
``_make_menu_fetch_handler`` / ``_menu_send_response`` /
``_finish_pending_menu``, ``_validate_input_response`` /
``_input_text_tlv``, ``_arm_menu_timeout`` / ``_menu_timeout_fire`` and the
``_handle_timer_command``/``_timer_*`` family).  The I/O moved from hex
strings + pySim's ``scc._tp.send_apdu`` to bytes + an injected transport
(``send_apdu(bytes) -> (bytes, bytes)``, :class:`simple_server.transport.PcscLink`),
and the module-level state became one :class:`CatSession` instance.  The
command decoding and the TR building live in :mod:`simple_server.proactive`
and :mod:`simple_server.tr` (Phase 3b) and are reused, never re-derived.

Spec references (checked against the pinned PDFs, TS 102 221 V18.4.0 and
TS 102 223 V18.3.0): TS 102 221 §11.2.1.2 (TERMINAL PROFILE: CLA, INS ``10``,
P1/P2 ``00``, Lc + data, no Le), §11.2.2.2 (ENVELOPE, INS ``C2``), §11.2.3.2
(FETCH, INS ``12``, Le = the length of the expected data), §11.2.4.2
(TERMINAL RESPONSE, INS ``14``, Lc + data, no Le), Table 10.5 (the STATUS
instruction ``F2`` and the four CAT instructions); TS 102 223 §6.3 (the
``91XX`` status), §6.4.2/§6.4.3 (GET INKEY/GET INPUT), §6.4.6 (POLL
INTERVAL), §6.4.14 (POLLING OFF), §6.4.15 (PROVIDE LOCAL INFORMATION, the
current date/time "as set by the user"), §6.4.21/§6.6.21 (TIMER MANAGEMENT),
§6.8.0 (the TERMINAL RESPONSE structure and object order), §6.8.1 (the echoed
command details), §6.8.4 (the POLL INTERVAL Duration), §6.8.5/§6.8.6 (the
Text string / Item identifier of a user answer), §7.4 (Timer expiration),
§8.7 (device identities), §8.12 (result), §8.15 (text string), §8.37/§8.38
(timer identifier/value), §8.39 (date-time and time zone); TS 101 220
Table 7.23 (the data-object tags: ``13`` location, ``1B`` location status,
``24`` timer id, ``25`` timer value, ``26`` date-time, ``0D`` text);
TS 123 040 §9.2.3.11 (the TP-SCTS semi-octet coding the date/time reuses);
TS 31.115 §4.2/§4.4 (the response packet of a secured packet: fetched from
the ENVELOPE exchange when SPI2 b6 = 0, else delivered by SEND SHORT MESSAGE).

BIP has no handler here: the chain calls the injected ``bip_handler`` (its
return value is the TR payload) and ``bip_flush`` after the chain - the
channel machinery belongs to the (later) BIP terminal module.
"""

import logging
import threading
import time
from datetime import datetime

from simple_server import cat
from simple_server import proactive
from simple_server import sms
from simple_server import text as text_codec
from simple_server import tlv
from simple_server import tr

log = logging.getLogger(__name__)

__all__ = [
    'CatSession', 'CatSessionError', 'PorSubmitCapture',
    'PLI_DEFAULTS', 'EVENT_DATA_MAX', 'EVENT_INNER_MAX', 'CHAIN_ROUNDS',
    'TIMER_COUNT', 'MENU_RESULTS', 'DIGITS_CHARSET', 'BIP_COMMAND_TYPES',
    'hms_bcd', 'validate_input_response', 'input_text_tlv',
]

#: The FETCH/TR answers one chain may send before it yields (the legacy
#: ``_POLL_CHAIN_MAX``): a card that re-queues a command immediately (a chatty
#: applet) must not hold the card forever from any call site (v3.25.1 - live
#: 2026-10-11: the EP BAP card re-queued the same PROVIDE LOCAL INFORMATION
#: 1100 times, blocking every card operation behind the chain).
CHAIN_ROUNDS = 10

#: The terminal keeps up to 8 timers per card session: §8.37 codes only the
#: identifiers '01'..'08', everything else being reserved.
TIMER_COUNT = 8

#: The single-ENVELOPE budget for event data.  The inner data (event list +
#: device identities + the event objects) is wrapped as ``D6`` and the APDU Lc
#: must stay within one octet (255), so the inner data is capped at 252 bytes
#: (244 bytes of event data + the 8-byte header).  Chained (multi-envelope)
#: delivery is not implemented.  The legacy 3.x ``events.EVENT_DATA_MAX``
#: carries the same value.
EVENT_INNER_MAX = 252
EVENT_DATA_MAX = EVENT_INNER_MAX - 8

#: The PROCEEDING/terminal-response codes of the interactive answer
#: (TS 102 223 §8.12.0).
MENU_RESULTS = {'ok': 0x00, 'cancel': 0x10, 'back': 0x11, 'timeout': 0x12,
                'help': 0x13}

#: TS 102 223 §6.4.3: the digits set (also the accepted set of a digits-only
#: GET INKEY, §6.4.2, and of a hidden GET INPUT).
DIGITS_CHARSET = '0123456789*#+'

#: PROVIDE LOCAL INFORMATION answers for an unconfigured dictionary - the
#: qualifiers whose objects the pinned ETSI specs define (TS 102 223 §8.x,
#: tags per TS 101 220 V18.3.0 Table 7.23, in the base CR=0 form: the TR's
#: response objects are Min = N and §6.8.0 says they "should set the CR flag
#: to comprehension not required" - v3.25.2).  They are the *base layer*: a
#: value set through :meth:`CatSession.set_pli_data` overrides its default,
#: and clearing the field restores it (v3.23.1).  Qualifier ``03`` stays empty
#: - the date/time is answered from the host clock (see
#: :meth:`CatSession.pli_data_hex`).  The 3GPP/3GPP2-specific qualifiers stay
#: empty: the ETSI specs define no contents for them, and a fabricated object
#: would mislead the application more than no answer.
#:
#: The values are hex strings (a legacy field: the dictionary is hex-keyed in
#: the API, and ``/api/pli-dict`` shows them verbatim).
PLI_DEFAULTS = {
    # Location Information (TS 31.111 §8.19.1, the GERAN form a PLI answer
    # uses): PLMN 001/01 (the test network, TS 23.003) + LAC 0001 + Cell ID
    # 0001.  Length 05 (no Cell ID) is the *Network Rejection event* form - a
    # PLI answer must carry the Cell ID (length 07), otherwise the location
    # reads as empty and a card applet may re-request it forever.
    0x00: '130700F11000010001',
    # IMEI (8.20): the classic test IMEI 490154203237518, coded as the value
    # part of the TS 24.008 Mobile Identity (§10.5.1.4): digit 1 + odd/even +
    # type 010 = '4A', then the remaining digits nibble-swapped BCD.
    0x01: '14084A09512430325781',
    # Language (8.45): 'en' in the SMS default alphabet (bit 8 clear).
    0x04: '2D02656E',
    # Timing Advance (3GPP; base tag 2E): ME status 00 + TA 00.
    0x05: '2E020000',
    # Access technology (8.61): E-UTRAN.
    0x06: '3F0108',
    # IMEISV (8.74, base tag 62): the test IMEI + SVN 08, coded as the Mobile
    # Identity value part - digit 1 + even + type 011 = '43', the digits
    # nibble-swapped BCD, F-padded.
    0x08: '62094309512430325701F8',
    # Network search mode (8.75, base tag 65): automatic.
    0x09: '650101',
    # Battery state (8.76, base tag 63): full.
    0x0A: '630104',
    # Access technologies (8.61): UTRAN + E-UTRAN.
    0x0E: '3F020308',
}

#: The command types the injected ``bip_handler`` answers (TS 102 223 §6.6.27
#: OPEN CHANNEL, §6.6.28 CLOSE CHANNEL, §6.6.29 RECEIVE DATA, §6.6.30 SEND
#: DATA, §6.6.31 GET CHANNEL STATUS).
BIP_COMMAND_TYPES = (0x40, 0x41, 0x42, 0x43, 0x44)


class CatSessionError(ValueError):
    """A CAT exchange the session refuses (no pending command, bad input)."""


def _as_bytes(value, what):
    """Bytes from a bytes-like object or a hex string (internal whitespace ok)."""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    try:
        return bytes.fromhex(''.join(str(value or '').split()))
    except ValueError:
        raise CatSessionError('%s is not valid hex: %r' % (what, value))


def _sw_bytes(sw):
    """Normalise a status word to two bytes (``b''`` when there is none)."""
    if isinstance(sw, (bytes, bytearray)):
        return bytes(sw)
    text = ''.join(str(sw or '').split())
    if not text:
        return b''
    try:
        return bytes.fromhex(text)
    except ValueError:
        return b''


def _is_91(sw):
    """True for the CAT success state: a proactive command is pending."""
    return sw[:1] == b'\x91'


def hms_bcd(seconds):
    """Encode seconds as hour/minute/second semi-octet BCD (TS 123 040)."""
    hours, rem = divmod(int(seconds), 3600)
    minutes, secs = divmod(rem, 60)
    return bytes([((hours % 10) << 4) | (hours // 10),
                  ((minutes % 10) << 4) | (minutes // 10),
                  ((secs % 10) << 4) | (secs // 10)])


def _normalise_cla(cat_cla):
    """The CAT CLA as a two-digit lowercase hex string (``80``/``a0``)."""
    if isinstance(cat_cla, int) and not isinstance(cat_cla, bool):
        value = cat_cla
    else:
        try:
            value = int(str(cat_cla).strip(), 16)
        except ValueError:
            raise CatSessionError('the CAT CLA is not valid hex: %r'
                                  % (cat_cla,))
    if not 0 <= value <= 0xFF:
        raise CatSessionError('the CAT CLA must be one octet: %r' % (cat_cla,))
    return '%02x' % value


def _digits_only(text):
    """True for a string in the TS 102 223 §6.4.3 digits set (empty allowed)."""
    return all(character in DIGITS_CHARSET for character in text)


def validate_input_response(pending, text):
    """Validate the user's answer against a GET INKEY / GET INPUT request.

    TS 102 223 §6.4.2 (GET INKEY: one character, a yes/no byte, the digits
    set) and §6.4.3 (GET INPUT: the Response length bounds, the digits set for
    a digits-only or hidden request).  Returns ``(text, error)`` - the port of
    the legacy ``_validate_input_response``.
    """
    text = '' if text is None else str(text)
    if pending['type'] == 'get_inkey':
        if pending.get('yes_no'):
            if text not in ('01', '00'):
                return None, ('yes/no response must be 01 (positive) or 00 '
                              '(negative)')
            return text, None
        if len(text) != 1:
            return None, 'GET INKEY needs exactly one character'
        if pending.get('digits_only') and not _digits_only(text):
            return None, 'digits only (0-9, *, #, +)'
        return text, None
    low = int(pending.get('min', 0) or 0)
    high = int(pending.get('max', 0xFF) or 0)
    if len(text) < low:
        return None, 'at least %d character(s) required' % low
    if high not in (0, 0xFF) and len(text) > high:
        return None, 'at most %d character(s) allowed' % high
    if (pending.get('digits_only') or pending.get('hidden')) \
            and not _digits_only(text):
        return None, 'digits only (0-9, *, #, +)'
    return text, None


def input_text_tlv(pending, text):
    """The Text string object (TS 102 223 §8.15) carrying a user's answer.

    §6.8.5: the GET INKEY/GET INPUT response is coded in the SMS default
    alphabet as 8-bit characters (DCS ``04``), as UCS2 (``08``) when the
    request asked for it, or packed (``00``) for a packed GET INPUT - and a
    yes/no answer is one octet, ``01`` positive / ``00`` negative.  An empty
    answer is the null text string (Length ``00``, no value part).  The length
    uses the BER coding (TS 101 220 §7.1.2 Table 7.6), so an answer of 128
    octets or more is coded ``81 nn``.
    """
    if text == '':
        return bytes([0x0D, 0x00])
    if pending.get('yes_no'):
        body = bytes([0x04, 0x01 if text == '01' else 0x00])
    elif pending.get('ucs2'):
        body = bytes([0x08]) + text.encode('utf-16-be')
    elif pending.get('packed'):
        body = bytes([0x00]) + text_codec.pack_gsm7(text.encode('gsm03.38'))
    else:
        body = bytes([0x04]) + text.encode('gsm03.38')
    # TS 101 220 §7.1.2 Table 7.6: a length of 128 octets or more takes the
    # BER long form (``81 nn``) - a raw octet would be read as one.
    return bytes([0x0D]) + tlv.encode_ber_len(len(body)) + body


class PorSubmitCapture(object):
    """The SEND SHORT MESSAGE capture of a PoR-in-submit packet.

    Port of the legacy ``PoRSubmitHandler`` minus its pySim base class: the
    same attribute names, so a caller written against the legacy handler keeps
    working.  ``send_envelope`` fills ``submit_tpdu_hex``/``submit_ud_hex``
    (and ``sms_segments`` for a concatenated response) from a SEND SHORT
    MESSAGE the card issues inside the ENVELOPE's chain.

    ``submit_ud_hex`` is the *submit* user data (no RPI UDH - the RPI is a UDH
    information element there), while the response packet decoder expects the
    DELIVER-style form, so :meth:`submit_response_hex` prepends the
    ``02 71 00`` UPI UDH (the legacy ``_sms_submit_por``).
    """

    def __init__(self):
        self.submit_tpdu_hex = None
        # Assembled user data of the actual-response SMS-SUBMIT(s), UDH
        # stripped: the DELIVER-style response packet is `02 71 00` + this.
        self.submit_ud_hex = None
        self.sms_segments = []          # [(ref, total, num, payload_hex)]

    def submit_response_hex(self):
        """The submit-carried response packet (``02 71 00`` + UD) as hex, or ''."""
        ud_hex = str(self.submit_ud_hex or '').strip()
        if not ud_hex:
            return ''
        try:
            bytes.fromhex(ud_hex)
        except ValueError:
            return ''
        return '027100' + ud_hex


class CatSession(object):
    """One card's CAT state: the chain, ENVELOPEs, the menu, timers, the log.

    ``transport`` is anything with ``send_apdu(bytes) -> (bytes, bytes)``
    (:class:`simple_server.transport.PcscLink`); every exchange is a single
    logical APDU - the T=0 chaining (``61xx``/``6Cxx``) is the transport's
    job.  ``cat_cla`` is the card class CLA (``80`` UICC, ``a0`` 2G SIM,
    TS 102 221 §11.2 / TS 151 011); it is normalised on assignment, so ``'A0'``
    and ``0xA0`` read back as ``'a0'``.

    **One instance per card slot.**  The PLI dictionary (``pli_data``) is the
    operator's configuration and survives :meth:`reset`; everything else is
    per card session, so the server calls :meth:`reset` on every card change
    and never shares an instance between two cards.

    **Card lock.**  ``card_lock`` serialises the card exchange with the rest of
    the server (the server passes its global lock; tests get a fresh
    ``threading.RLock``).  The two ``threading.Timer`` callbacks - the timer
    expiry and the menu watchdog - wait for a paused/running conversation
    *before* acquiring it and run their APDUs under it, so they can never
    interleave a FETCH/TERMINAL RESPONSE pair (the legacy
    ``_CARD_LOCK`` order).

    **Never leave a FETCH unanswered.**  A caller that pauses the chain
    (:meth:`menu_fetch_handler` returning ``'pause'``) owns the pending
    command: it must call :meth:`menu_send_response` (or
    :meth:`finish_pending_menu`) and, for the interactive kinds, set
    ``menu_active`` and call :meth:`arm_menu_timeout`.  :attr:`stk_pending` is
    JSON-safe (the ``/api/stk-status`` ``pending_data`` object is served
    verbatim): strings, numbers, booleans, lists and dicts only - no bytes.

    **Exception surface.**  :meth:`send_envelope` and
    :meth:`send_event_download` let :class:`simple_server.cat.CatError` and
    :class:`simple_server.sms.SmsError` escape (a malformed/too-large object);
    the session's own refusals raise :class:`CatSessionError`.  Unlike the
    legacy envelope builder the address is required to have at least one digit
    (:class:`simple_server.sms.SmsError`): the legacy ``Address('')`` emitted
    the "no SMSC" ``86 02 00 81`` form, which this port does not reproduce
    (pass a real service-centre address).

    The injected hooks:

    * ``test_running`` - ``callable() -> bool``; while it is true the chain
      stops before the next FETCH (a test run owns the card and the pending
      command stays for the script, v3.25.1).
    * ``bip_handler`` - ``callable(cmd_num, cmd_type, cmd_qual, raw, dev_src,
      dev_dst, cd_tag) -> bytes | None``; its return value is the TR payload
      for a BIP command, ``None``/empty falls through to the generic TR.
    * ``bip_flush`` - ``callable()``; called when the chain ends without a
      paused command (channel-status events must not be injected between a
      FETCH and its TERMINAL RESPONSE).
    * ``on_menu`` - ``callable(menu_dict)``; a SET UP MENU was seen (the cache
      is refreshed first).  An exception in a hook never breaks the chain.
    * ``clock`` - ``callable() -> float``; the elapsed-time source of the
      proactive log (tests freeze it).
    * ``pli_data`` - a dict the session uses as its PLI override store instead
      of a fresh one.  The server passes its own, so the operator's answers
      survive a card change (a new session object per equip, the legacy
      module-global dictionary).
    """

    def __init__(self, transport, cat_cla='80', *, test_running=None,
                 bip_handler=None, bip_flush=None, chain_rounds=CHAIN_ROUNDS,
                 on_menu=None, menu_timeout=60, poll_interval=30, clock=None,
                 card_lock=None, pli_data=None):
        self.transport = transport
        self.test_running = test_running
        self.bip_handler = bip_handler
        self.bip_flush = bip_flush
        try:
            self.chain_rounds = int(chain_rounds)
        except (TypeError, ValueError):
            raise CatSessionError('chain_rounds must be an integer: %r'
                                  % (chain_rounds,))
        if self.chain_rounds < 0:
            raise CatSessionError('chain_rounds cannot be negative: %r'
                                  % (chain_rounds,))
        self.on_menu = on_menu
        self.clock = clock or time.time
        self.menu_timeout = self._clamp_timeout(menu_timeout)
        # The card's polling wishes (TS 102 223 §6.4.6/§6.4.14): the session
        # records them, the background poll itself belongs to the server.  The
        # constructor value is the terminal's own default a reset returns to.
        self._poll_interval_default = max(0, min(255, int(poll_interval)))
        self.poll_interval = self._poll_interval_default
        self.poll_suspended = False
        # True while a FETCH/TR chain runs: terminal-initiated ENVELOPEs
        # (timer expirations) must wait for it.
        self.busy = False
        # The card-exchange lock (see the class docstring).
        self.card_lock = card_lock if card_lock is not None else threading.RLock()

        self._session_start = self.clock()
        self._entry_id = 0
        self.log = []
        self.sim_menu = None
        self.event_list = None
        self.stk_pending = None
        self.menu_active = False
        # The operator's PLI overrides only, keyed by qualifier code.  Kept
        # free of placeholders: the dictionary POST's guard tests the known
        # qualifiers, never "an override exists" (v3.25.5).  The server passes
        # its own dict, so the overrides belong to the operator (not to the
        # card session) and survive an equip.
        self.pli_data = pli_data if pli_data is not None else {}

        self._timers = {}
        self._timer_lock = threading.RLock()
        self._menu_timer = None
        self._cat_cla = None
        self.cat_cla = cat_cla

    @property
    def cat_cla(self):
        """The card class CLA as a lower-case two-digit hex string."""
        return self._cat_cla

    @cat_cla.setter
    def cat_cla(self, value):
        self._cat_cla = _normalise_cla(value)

    # ---- APDU plumbing ----------------------------------------------------

    def _send(self, apdu):
        """Send one APDU; returns ``(data, sw)`` as bytes."""
        data, sw = self.transport.send_apdu(bytes(apdu))
        return bytes(data or b''), _sw_bytes(sw)

    def _apdu(self, ins, p1, p2, data=b'', le=None):
        """A short command APDU (ISO/IEC 7816-4 case 2/3); ``le`` is 1..256."""
        apdu = bytes([int(self.cat_cla, 16), ins, p1, p2])
        if data:
            apdu += bytes([len(data)]) + bytes(data)
        if le is not None:
            apdu += bytes([0x00 if le == 0x100 else le & 0xFF])
        return apdu

    def _fetch_apdu(self, length):
        """FETCH (TS 102 221 §11.2.3): ``CLA 12 00 00 <Le>``."""
        return self._apdu(0x12, 0x00, 0x00, le=length)

    def _terminal_response_apdu(self, tr_tlv):
        """TERMINAL RESPONSE (TS 102 221 §11.2.4): ``CLA 14 00 00 <Lc> <data>``.

        An empty payload is refused: a TERMINAL RESPONSE is a case-3 APDU
        (``Lc`` + data) and a bare ``14`` header would leave the fetched
        command unanswered.  The chain falls back to the generic TR before it
        gets here.
        """
        tr_tlv = bytes(tr_tlv)
        if not tr_tlv:
            raise CatSessionError('a TERMINAL RESPONSE needs a payload')
        if len(tr_tlv) > 0xFF:
            raise CatSessionError('a TERMINAL RESPONSE of %d octets does not '
                                  'fit one APDU' % len(tr_tlv))
        return self._apdu(0x14, 0x00, 0x00, tr_tlv)

    # ---- STATUS / TERMINAL PROFILE ----------------------------------------

    def send_status(self):
        """STATUS (F2): P2 = 0C and P3 = 0x23 for a SIM / 0x00 for a UICC.

        P2 = ``0C`` asks for no response data (TS 102 221 §11.1.2 Table 11.9,
        p. 100: ``b4..b1 = 1100`` = "No data returned"), so the answer carries
        the application status instead of the selected DF's FCP (a plain
        ``F2 00 00`` echoes it).  A SIM needs the fixed ``0x23`` response
        length (TS 151 011), a UICC the ISO 7816 length-negotiation form
        ``00`` (the transport fetches ``6Cxx``).  The ``91XX`` answer means a
        proactive command is pending.

        This is the CAT status **poll**; reading a DF's FCP is
        :meth:`simple_server.commands.FileOps.status_fcp` (P2 ``00``).
        """
        p3 = 0x23 if self.cat_cla == 'a0' else 0x00
        return self._send(bytes([int(self.cat_cla, 16), 0xF2, 0x00, 0x0C, p3]))

    def fetch_apdu(self, length):
        """The FETCH command APDU (TS 102 221 §11.2.3) for ``length`` octets.

        ``length`` is the count the card's ``91XX`` answer named; ``0`` means
        "length unknown" (a bare ``9100``) and codes the full 256 octets as
        ``00``.  The runner logs the APDU and reuses the same form in
        :meth:`fetch`.
        """
        return self._fetch_apdu(0 if not length else int(length))

    def fetch(self, length):
        """FETCH the announced proactive command; ``(data, sw)`` as bytes."""
        return self._send(self.fetch_apdu(length))

    def send_terminal_response(self, tr_tlv):
        """Answer a fetched command (TERMINAL RESPONSE, TS 102 221 §11.2.4).

        Returns ``(data, sw)`` as bytes.  Raises :class:`CatSessionError` for
        an empty payload - a bare ``14`` header would leave the command
        unanswered.
        """
        return self._send(self._terminal_response_apdu(tr_tlv))

    def send_terminal_profile(self, tp_hex):
        """Send TERMINAL PROFILE and answer the commands it raises.

        ``tp_hex`` is the profile hex (bytes accepted too).  Returns
        ``(menu, event_list)``: the SET UP MENU and SET UP EVENT LIST found in
        the answers, and caches both on the session (``sim_menu`` /
        ``event_list``) - the legacy caller did that assignment itself.

        The chain is bounded by :attr:`chain_rounds` (the legacy loop was
        unbounded: a card that keeps re-queueing a command would hold the
        card lock forever - the same reason ``handle_chain`` took a bound in
        v3.25.1).  Its FETCH uses the ``91XX`` length (the ``0xFF`` fallback
        for a bare ``91``), exactly like the legacy ``_send_terminal_profile``
        - a fixed ``0xFF`` made a T=1 card answer ``6Cxx`` and broke the chain.
        The card's POLL INTERVAL / POLLING OFF wishes are adopted before the
        TERMINAL RESPONSE is built (§6.4.6/§6.4.14), so the echo states the
        interval the terminal will use (§6.8.4).
        """
        tp = _as_bytes(tp_hex, 'terminal profile')
        if not tp:
            raise CatSessionError('the terminal profile is empty')
        if len(tp) > 0xFF:
            raise CatSessionError('the terminal profile exceeds 255 bytes')
        _data, sw = self._send(self._apdu(0x10, 0x00, 0x00, tp))
        menu = None
        event_list = None
        rounds = 0
        while _is_91(sw):
            if rounds >= self.chain_rounds:
                log.info('TERMINAL PROFILE chain: %d rounds reached - yielding',
                         rounds)
                break
            rounds += 1
            fetch_len = sw[1] if len(sw) == 2 else 0xFF
            fdata, sw = self._send(self._fetch_apdu(fetch_len))
            raw = bytes(fdata) if fdata else None
            cmd_num, cmd_type = 1, 0
            cmd_qual = None
            cd_tag = None
            dev_src, dev_dst = 0x83, 0x81
            if raw:
                try:
                    cmd_num, cmd_type, dev_src, dev_dst, cmd_qual, cd_tag = \
                        proactive.parse_proactive_header(raw)
                except Exception as exc:                # a decoder bug
                    log.warning('TERMINAL PROFILE parse error: %s (raw=%s)',
                                exc, raw.hex())
                try:
                    # proactive's public BER-safe TLV walker (same package)
                    for tag, val in proactive.command_tlvs(raw):
                        if tag in (0x99, 0x19) and val:
                            event_list = list(val)
                    if cmd_type == 0x25:
                        found = proactive.parse_setup_menu_command(raw)
                        if found:
                            menu = found
                except Exception as exc:
                    log.warning('TERMINAL PROFILE decode error: %s (raw=%s)',
                                exc, raw.hex())
                if cmd_type in (0x03, 0x04):
                    try:
                        # Adopt before the TR is built: the POLL INTERVAL
                        # answer echoes the adopted value (§6.8.4).
                        self._handle_poll_command(cmd_type, raw)
                    except Exception as exc:
                        log.warning('TERMINAL PROFILE poll error: %s (raw=%s)',
                                    exc, raw.hex())
            entry = None
            if raw and cmd_type:
                entry = self.log_proactive(cmd_type, raw, cmd_qual, cmd_num)
            tr_tlv = self.build_chain_tr(cmd_num, cmd_type, dev_src, dev_dst,
                                         cmd_qual, cd_tag)
            _tdata, tr_sw = self._send(self._terminal_response_apdu(tr_tlv))
            self.record_tr(entry, tr_tlv, tr_sw)
            sw = tr_sw
            if sw == b'\x90\x00':
                _st_data, st_sw = self.send_status()
                if _is_91(st_sw):
                    sw = st_sw
        self.sim_menu = menu
        self.event_list = event_list
        if menu is not None:
            self._notify_menu(menu)
        return menu, event_list

    def resend_terminal_profile(self, tp_hex):
        """Re-send the profile as a new CAT session (keeps the log history).

        Port of the legacy ``_resend_terminal_profile``: a pending command is
        answered first (:meth:`finish_pending_menu` - re-sending the profile
        must never shadow a FETCHed command that awaits its TERMINAL
        RESPONSE), then the menu state is dropped, the proactive log keeps its
        history with a boundary marker (v3.23.7) and the profile's answers
        repopulate the menu/event-list cache.
        """
        self.finish_pending_menu()
        self.stk_pending = None
        self.menu_active = False
        self.cancel_menu_timeout()
        self.event_list = None
        self.mark_session('terminal-profile')
        menu, events = self.send_terminal_profile(tp_hex)
        return {'menu': menu is not None, 'events': events}

    # ---- the FETCH / TERMINAL RESPONSE chain ------------------------------

    def handle_chain(self, sw, on_fetch=None, status_poll=True,
                     max_rounds=None):
        """Run a FETCH/TERMINAL RESPONSE chain from an ``91XX`` answer.

        ``on_fetch(raw, cmd_num, cmd_type, dev_src, dev_dst)`` may return
        ``'pause'`` (stop *without* a TERMINAL RESPONSE - the menu browser
        answers later; the caller stores the pending command) or ``'exit'``
        (answer, then return).  ``status_poll=False`` skips the trailing
        STATUS (the profile switch must not query the card further).

        **A pause hands the fetched command to the caller.**  The caller then
        owns it: it must eventually answer (``menu_send_response`` /
        ``finish_pending_menu``) and, for the interactive kinds, set
        ``menu_active`` and call :meth:`arm_menu_timeout` - the invariant
        "never leave a FETCH unanswered" (a missing TERMINAL RESPONSE makes the
        card refuse further proactive commands).  Never run another FETCH
        while :attr:`stk_pending` is set: the second FETCH answers ``6F00``
        and the proactive session is left half-consumed.

        Every answer is guarded: a decoder bug logs and the TERMINAL RESPONSE
        is still sent (v3.6.57 - an IndexError in the FETCH path used to be a
        500 with the card's command left unanswered); an empty handler payload
        falls back to the generic TR (an empty APDU is not a TERMINAL
        RESPONSE).  The chain yields before
        the next FETCH when ``test_running()`` is true or ``max_rounds``
        (default :attr:`chain_rounds`) is reached.  Returns the last status
        word (bytes) when ``on_fetch`` asked to exit, else ``None``.
        """
        if max_rounds is None:
            max_rounds = self.chain_rounds
        self.busy = True
        try:
            return self._run_chain(_sw_bytes(sw), on_fetch, status_poll,
                                   max_rounds)
        finally:
            self.busy = False

    def _run_chain(self, sw, on_fetch, status_poll, max_rounds):
        paused = False
        rounds = 0
        while _is_91(sw):
            if self._test_running():
                log.info('91XX chain: stopping - a test run owns the card')
                break
            if max_rounds is not None and rounds >= max_rounds:
                log.info('91XX chain: %d rounds reached - yielding', rounds)
                break
            rounds += 1
            fetch_len = sw[1] if len(sw) == 2 else 0x100
            fdata, sw = self._send(self._fetch_apdu(fetch_len))
            log.debug('FETCH(%d): %s -> %s', fetch_len, fdata.hex() or '(none)',
                      sw.hex())
            raw = bytes(fdata) if fdata else None
            action = None
            cmd_num, cmd_type = 1, 0
            dev_src, dev_dst = 0x83, 0x81
            cmd_qual = None
            cd_tag = None
            entry = None
            if raw:
                try:
                    cmd_num, cmd_type, dev_src, dev_dst, cmd_qual, cd_tag = \
                        proactive.parse_proactive_header(raw)
                except Exception as exc:                # a decoder bug
                    log.warning('FETCH parse error: %s (raw=%s)', exc,
                                raw.hex())
                try:
                    entry = self.log_proactive(cmd_type, raw, cmd_qual,
                                               cmd_num)
                except Exception as exc:
                    log.warning('FETCH log error: %s (raw=%s)', exc,
                                raw.hex())
                try:
                    if cmd_type == 0x25:
                        # Keep the cached menu current: text-based menu
                        # selection and the Phone tab must see the menu the
                        # card sends now (an applet install changes it).
                        menu = proactive.parse_setup_menu_command(raw)
                        if menu:
                            self.sim_menu = menu
                            self._notify_menu(menu)
                except Exception as exc:
                    log.warning('SET UP MENU cache error: %s (raw=%s)', exc,
                                raw.hex())
            try:
                if raw and cmd_type in (0x03, 0x04):
                    self._handle_poll_command(cmd_type, raw)
                if on_fetch:
                    action = on_fetch(raw, cmd_num, cmd_type, dev_src, dev_dst)
            except Exception as exc:
                log.warning('FETCH handler error: %s (raw=%s)', exc,
                            raw.hex() if raw else '(none)')
            paused = action == 'pause'
            if not paused:
                tr_tlv = None
                try:
                    if raw and cmd_type in BIP_COMMAND_TYPES:
                        if self.bip_handler is not None:
                            tr_tlv = self.bip_handler(
                                cmd_num, cmd_type, cmd_qual, raw, dev_src,
                                dev_dst, cd_tag)
                    if cmd_type == 0x27:
                        tr_tlv = self.handle_timer_command(
                            cmd_num, cmd_type, cmd_qual, raw, dev_src, dev_dst,
                            cd_tag=cd_tag)
                except Exception as exc:
                    log.warning('FETCH handler error: %s (raw=%s)', exc,
                                raw.hex() if raw else '(none)')
                    tr_tlv = None
                if tr_tlv is None or not bytes(tr_tlv):
                    tr_tlv = self.build_chain_tr(cmd_num, cmd_type, dev_src,
                                                 dev_dst, cmd_qual, cd_tag)
                tr_tlv = bytes(tr_tlv)
                _tr_data, tr_sw = self._send(
                    self._terminal_response_apdu(tr_tlv))
                log.debug('TR: cmd=%02x type=%02x -> %s (%d bytes)', cmd_num,
                          cmd_type, tr_sw.hex(), len(tr_tlv))
                self.record_tr(entry, tr_tlv, tr_sw)
                sw = tr_sw
                if sw == b'\x90\x00' and status_poll:
                    log.debug('STATUS poll (chain ended)')
                    _st_data, st_sw = self.send_status()
                    log.debug('STATUS -> %s', st_sw.hex())
                    if _is_91(st_sw):
                        sw = st_sw
                if action == 'exit':
                    self._flush_bip()
                    return sw
        if not paused:
            # Never inject an ENVELOPE while a fetched command awaits its
            # TERMINAL RESPONSE (the menu browser answers it later).
            self._flush_bip()
        return None

    def _test_running(self):
        """The injected test-run flag (False without a hook)."""
        if self.test_running is None:
            return False
        try:
            return bool(self.test_running())
        except Exception as exc:
            log.warning('test_running hook failed: %s', exc)
            return False

    def _flush_bip(self):
        """Run the injected channel-event flush (never raises)."""
        if self.bip_flush is None:
            return
        try:
            self.bip_flush()
        except Exception as exc:
            log.warning('BIP flush error: %s', exc)

    def _notify_menu(self, menu):
        """Pass a fresh SET UP MENU to the injected hook (never raises)."""
        if self.on_menu is None:
            return
        try:
            self.on_menu(menu)
        except Exception as exc:
            log.warning('on_menu hook failed: %s', exc)

    def _handle_poll_command(self, cmd_type, raw):
        """Adopt the card's polling wishes before its TERMINAL RESPONSE.

        TS 102 223 §6.4.6 (POLL INTERVAL: the Duration is converted to seconds
        and clamped to 1..255, the echo in :meth:`build_chain_tr` carries the
        adopted value) and §6.4.14 (POLLING OFF suspends proactive polling
        until a POLL INTERVAL re-enables it).  The background STATUS timer
        itself is the server's; the session only records the state.
        """
        if cmd_type == 0x03:
            duration = proactive.find_duration(raw)
            if duration:
                unit, interval = duration
                self.poll_interval = proactive.duration_seconds(unit, interval)
                log.info('POLL INTERVAL: card requests %s -> background poll '
                         '%d s', proactive.duration_text(unit, interval),
                         self.poll_interval)
            self.poll_suspended = False
        elif cmd_type == 0x04:
            if not self.poll_suspended:
                log.info('POLLING OFF: card disabled proactive polling')
            self.poll_suspended = True

    def build_chain_tr(self, cmd_num, cmd_type, dev_src, dev_dst, cmd_qual,
                       cd_tag=None):
        """The chain's generic TERMINAL RESPONSE payload (the ``_build_tr`` port).

        Result ``00`` in the basic ``03 01`` form, plus the terminal's own
        answer objects (TS 102 223 §6.8.0 order: after the Result):
        the echoed Duration for POLL INTERVAL (§6.8.4) and the PLI data for
        PROVIDE LOCAL INFORMATION (§6.4.15).  Byte-identical to
        ``tr.build_tr(..., {}, general_result=False)``.
        """
        answer = b''
        if cmd_type == 0x03:
            answer = bytes([0x04, 0x02, 0x01, self.poll_interval & 0xFF])
        elif cmd_type == 0x26 and cmd_qual is not None:
            answer = self._pli_data_bytes(cmd_qual)
        return tr.build_tr(cmd_num, cmd_type, dev_src, dev_dst, cmd_qual,
                           cd_tag, {}, answer_tlvs=answer,
                           general_result=False)

    # ---- ENVELOPE ---------------------------------------------------------

    def send_envelope(self, tpdu, sm_sc='12345678912', submit_handler=None,
                      handle_proactive=True, poll_status=True):
        """Deliver an SMS-PP download TPDU and capture the response packet.

        Builds the SMS-PP-Download template (TS 31.111 §7.1.1.2: ``D1`` with
        the network -> UICC device identities, the service-centre address and
        the SMS TPDU) and sends it as ``CLA C2 00 00 Lc <data>``
        (TS 102 221 §11.2.2).

        The ``61xx`` answer promises response bytes and a ``62xx``/``63xx``
        *without data* may hold them too - the card quirk behind v3.25.7 (an
        SPI2 ``01`` packet's PoR is retrieved from the ENVELOPE exchange,
        TS 31.115 §4.2/§4.4 b6 = 0).  A ``91xx`` answer is the CAT success
        state, not an error: the chain runs, the delivered bytes are kept
        (v3.28.6) and the exchange reports ``9000``.

        ``submit_handler`` is the :class:`PorSubmitCapture`-shaped object that
        receives the SEND SHORT MESSAGE of a PoR-in-submit packet; with it and
        ``poll_status`` a late PoR (ENVELOPE answered ``9000``/``62xx``/
        ``63xx`` with no data and no submit captured) is polled for - the
        caller passes ``poll_status=False`` for every segment but the last
        (a concatenated download cannot have produced its PoR yet).

        Returns ``(data, sw)`` as bytes.
        """
        tpdu = _as_bytes(tpdu, 'TPDU')
        if not tpdu:
            raise CatSessionError('the SMS TPDU is empty')
        template = cat.SmsPpDownload.build(
            cat.DeviceIdentities('network', 'uicc'), tpdu,
            cat.Address(str(sm_sc or '')))
        data, sw = self._send(cat.envelope_apdu(template, self.cat_cla))
        data, sw = self._envelope_get_response(data, sw)
        on_fetch = self._submit_fetch_handler(submit_handler)
        if handle_proactive and _is_91(sw):
            self.handle_chain(sw, on_fetch)
            sw = b'\x90\x00'
        if (handle_proactive and poll_status and submit_handler is not None
                and not getattr(submit_handler, 'submit_tpdu_hex', None)
                and not data
                and (sw == b'\x90\x00' or sw[:1] in (b'\x62', b'\x63'))):
            # A warning answer (the live card answers 6200 to a low-counter
            # packet) can hold the PoR too: poll once so the card's verdict is
            # captured instead of a bare 'ENVELOPE failed' (v3.6.58).
            log.debug('STATUS poll (PoR not captured)')
            _st_data, st_sw = self.send_status()
            log.debug('STATUS -> %s', st_sw.hex())
            if _is_91(st_sw):
                self.handle_chain(st_sw, on_fetch)
        return data, sw

    def _envelope_get_response(self, data, sw):
        """Fetch response bytes the card holds after an ENVELOPE.

        ISO 7816-4: ``61xx`` promises response bytes - GET RESPONSE with the
        named length (Le 256 is coded ``00``).  A ``62xx``/``63xx`` answer
        *without* data can mean the same on cards that do not use ``61xx`` for
        it (the quirk the remote-command status words already acknowledge;
        live 2026-10-11: a secured packet answered ``63xx`` with the whole PoR
        behind one GET RESPONSE).  The fetched result is adopted when it
        delivered bytes (or for a ``61xx``, which promised them); otherwise
        the original answer is kept, so a real warning stays visible and the
        late-PoR STATUS poll still runs.

        :class:`simple_server.transport.PcscLink` already applies the T=0
        rules, so on a T=0 link this is a no-op; it covers a T=1 link and a
        transport that passes the status word through.
        """
        if not (sw[:1] == b'\x61'
                or (not data and sw[:1] in (b'\x62', b'\x63'))):
            return data, sw
        get_len = sw[1] if sw[:1] == b'\x61' and len(sw) == 2 else 0x100
        gdata, gsw = self._send(bytes([0x00, 0xC0, 0x00, 0x00,
                                       0x00 if get_len == 0x100 else get_len]))
        if gdata or sw[:1] == b'\x61':
            return gdata, gsw
        return data, sw

    def _submit_fetch_handler(self, submit_handler):
        """The on_fetch capture of a SEND SHORT MESSAGE (a no-op when absent)."""
        def _capture(raw, cmd_num, cmd_type, dev_src, dev_dst):
            if submit_handler is None or not raw:
                return None
            tpdu = sms.find_sms_tpdu(raw)
            if not tpdu:
                return None
            ref, total, num, payload = sms.parse_sms_concat(tpdu)
            tpdu_hex = bytes(tpdu).hex()
            if total is not None and num is not None:
                submit_handler.sms_segments.append(
                    (ref, total, num, payload.hex()))
                matching = [s for s in submit_handler.sms_segments
                            if s[0] == ref]
                if len(matching) >= total:
                    assembled = b''.join(
                        bytes.fromhex(seg[3])
                        for seg in sorted(matching, key=lambda s: s[2]))
                    submit_handler.submit_ud_hex = assembled.hex()
                    submit_handler.submit_tpdu_hex = (
                        submit_handler.submit_tpdu_hex or tpdu_hex)
                    log.info('SMS concat: assembled %d segments (ref=%s, %d B)',
                             total, ref, len(assembled))
            else:
                submit_handler.submit_ud_hex = payload.hex()
                submit_handler.submit_tpdu_hex = tpdu_hex
            return None
        return _capture

    def send_event_download(self, event_type, event_data=None, drain=True,
                            src=None):
        """Send ``ENVELOPE(Event Download)`` for an event (TS 102 223 §7.5).

        The envelope data is ``D6 <len> 99 01 <type> 82 02 <src> 81 [objects]``
        - the event list item, then the device identities (source -> UICC).
        ``src`` is the source device identity (``82`` terminal, the default;
        ``83`` network - the 3GPP-assigned events are network-sourced).  The
        D6 length uses the BER long form from 128 octets on: a raw length
        above ``0x7F`` would be read as a long-form indicator.

        ``event_data`` must fit one envelope (:data:`EVENT_DATA_MAX`);
        chained delivery is not implemented, so a larger value raises
        :class:`CatSessionError`.  A ``91xx`` answer is drained when ``drain``
        is set and reported as ``9000``.  Returns ``(data, sw)``.
        """
        event_data = _as_bytes(event_data, 'event data')
        if len(event_data) > EVENT_DATA_MAX:
            raise CatSessionError(
                'event data does not fit one ENVELOPE (max %d bytes; chained '
                'delivery is not implemented)' % EVENT_DATA_MAX)
        src_byte = 0x82
        if src is not None:
            try:
                src_byte = int(str(src), 16) & 0xFF
            except (TypeError, ValueError):
                src_byte = 0x82
        inner = bytes([0x99, 0x01, int(event_type) & 0xFF,
                       0x82, 0x02, src_byte, 0x81]) + event_data
        if len(inner) < 0x80:
            template = bytes([0xD6, len(inner)]) + inner
        else:
            template = bytes([0xD6, 0x81, len(inner)]) + inner
        data, sw = self._send(cat.envelope_apdu(template, self.cat_cla))
        data, sw = self._envelope_get_response(data, sw)
        if drain and _is_91(sw):
            self.handle_chain(sw)
            sw = b'\x90\x00'
        return data, sw

    # ---- PROVIDE LOCAL INFORMATION ---------------------------------------

    def set_pli_data(self, qualifier, value):
        """Set one PLI answer (hex or bytes); ``None``/``''`` restores the default."""
        qualifier = int(qualifier)
        if isinstance(value, (bytes, bytearray)):
            text = bytes(value).hex()
        else:
            text = str(value or '').strip()
        if not text:
            self.pli_data.pop(qualifier, None)
            return
        try:
            bytes.fromhex(text)
        except ValueError:
            raise CatSessionError('the PLI value is not valid hex: %r'
                                  % (value,))
        self.pli_data[qualifier] = text

    def pli_data_hex(self, qualifier, dt=None):
        """The PLI data object(s) for a TERMINAL RESPONSE, as hex ('' = none).

        The dictionary override wins, then the built-in default - clearing a
        field restores its default (v3.23.1).  Without either, the date/time
        qualifier is answered with the host clock (TS 102 223 §6.4.15: "The
        terminal shall return the current date and time as set by the user"),
        coded as the Date-Time and Time zone object (§8.39, TP-SCTS coding).
        ``dt`` is a test seam; the session's clock is read otherwise.

        The value is a hex string because the dictionary *is* hex (the legacy
        field ``/api/pli-dict`` shows).
        """
        qualifier = int(qualifier)
        value = str(self.pli_data.get(qualifier) or '').strip()
        if value:
            return value
        default = PLI_DEFAULTS.get(qualifier)
        if default:
            return default
        if qualifier == 0x03:
            stamp = dt if dt is not None else self._now()
            return '2607' + sms.encode_scts(stamp).hex().upper()
        return ''

    def _pli_data_bytes(self, qualifier, dt=None):
        """The PLI answer as bytes (``b''`` when there is none)."""
        value = self.pli_data_hex(qualifier, dt)
        return bytes.fromhex(value) if value else b''

    def _now(self):
        """The current local time for the date/time answer."""
        return datetime.now().astimezone()

    # ---- TIMER MANAGEMENT (TS 102 223 §6.4.21/§6.6.21/§7.4) --------------

    def handle_timer_command(self, cmd_num, cmd_type, cmd_qual, raw, dev_src,
                             dev_dst, cd_tag=None):
        """The terminal side of TIMER MANAGEMENT; returns the TR payload.

        Qualifier bits 1-2 select start / deactivate / get-current-value
        (§8.6).  A start uses the Timer value object (§8.38), a successful
        deactivate or get answers with the timer identifier (§8.37) and the
        remaining value; a get or deactivate of a timer that is not running
        answers result ``24`` ("action in contradiction with the current timer
        state", §8.12.0).

        **Known legacy deviation, kept for byte parity**: §6.8.0's TERMINAL
        RESPONSE table puts the Result third (command details, device
        identities, Result, then Duration/Text/Item/Timer objects), while the
        legacy builder emitted the timer objects *before* the Result - and the
        live cards accepted it (v3.25.2 shipped this form).  Changing the order
        alters what a live-tested card receives with no failure to point at, so
        the port keeps the legacy bytes (pinned by the drift test) for the
        maintainer to decide; the generic chain TR and
        :meth:`menu_send_response` do follow §6.8.0.

        TODO(Phase 3c cutover): fix the order per TS 102 223 §6.8.0 (p. 102 -
        Result third), then re-run the bench cards (the live SET UP MENU /
        timer captures) before trusting it; until then the bytes stay as the
        live cards accepted them.
        """
        # proactive's public walkers (the same package): the BER-safe
        # COMPREHENSION-TLV map both tag styles of a command
        tlvs = proactive.command_tlv_map(raw)
        timer = proactive.cmd_tlv(tlvs, 0x24)
        timer_id = timer[0] if timer else 1
        action = (cmd_qual or 0) & 0x03
        if cd_tag is None:
            cd_tag = self._cd_tag_of(raw)
        base = bytes([(cd_tag or 0x81), 0x03, cmd_num & 0xFF, cmd_type & 0xFF,
                      (cmd_qual or 0) & 0xFF, 0x02, 0x02,
                      dev_dst & 0xFF, dev_src & 0xFF])
        if action == 0x00:
            value = proactive.cmd_tlv(tlvs, 0x25)
            if len(value) >= 3 and 1 <= timer_id <= TIMER_COUNT:
                seconds = (proactive.bcd_swap(value[0]) * 3600
                           + proactive.bcd_swap(value[1]) * 60
                           + proactive.bcd_swap(value[2]))
                if seconds > 0:
                    self._timer_start(timer_id, seconds)
            return base + bytes([0x03, 0x01, 0x00])
        remaining = self._timer_remaining(timer_id)
        if remaining is None:
            return base + bytes([0x03, 0x01, 0x24])
        if action == 0x01:
            self._timer_cancel(timer_id)
        return (base + bytes([0x24, 0x01, timer_id & 0xFF, 0x25, 0x03])
                + hms_bcd(remaining) + bytes([0x03, 0x01, 0x00]))

    def _cd_tag_of(self, raw):
        """The command-details tag the card used (``01``/``81``), or None."""
        try:
            return proactive.parse_proactive_header(raw)[5]
        except Exception:
            return None

    def _timer_start(self, timer_id, seconds):
        """Start (or restart) a timer; False for an invalid identifier."""
        if not 1 <= timer_id <= TIMER_COUNT:
            return False
        self._timer_cancel(timer_id)
        timer = threading.Timer(seconds, self._timer_fire,
                                args=(timer_id, seconds))
        timer.daemon = True
        timer.start()
        with self._timer_lock:
            self._timers[timer_id] = {'timer': timer,
                                      'deadline': time.time() + seconds}
        return True

    def _timer_remaining(self, timer_id):
        """Seconds left on a running timer, or None."""
        with self._timer_lock:
            entry = self._timers.get(timer_id)
            if not entry:
                return None
            return max(0, int(round(entry['deadline'] - time.time())))

    def _timer_cancel(self, timer_id=None):
        """Cancel one timer, or all of them (a reset / a card change)."""
        with self._timer_lock:
            ids = list(self._timers) if timer_id is None else [timer_id]
            for tid in ids:
                entry = self._timers.pop(tid, None)
                if entry:
                    entry['timer'].cancel()

    def cancel_timers(self):
        """Cancel every running timer (a reset / a card removal)."""
        self._timer_cancel(None)

    def _timer_fire(self, timer_id, elapsed):
        """Timer callback: the timer is consumed on expiry (§7.4.1)."""
        with self._timer_lock:
            entry = self._timers.pop(timer_id, None)
        if entry is None:
            return
        try:
            self._timer_expired(timer_id, elapsed)
        except Exception as exc:                # never escape into the thread
            log.warning('TIMER-EXPIRATION error (timer %d): %s', timer_id, exc)

    def _timer_expired(self, timer_id, elapsed):
        """Pass an expired timer to the UICC with ENVELOPE (TIMER EXPIRATION).

        The envelope is ``D7 <len> 82 02 82 81 A4 01 <id> A5 03 <h:m:s>``
        (device identities terminal -> UICC, then the timer objects).  It must
        not be injected between a FETCH and its TERMINAL RESPONSE, so a paused
        command (or a running chain) defers it by 5 s; the wait happens
        *before* :attr:`card_lock` is taken and the state is re-checked under
        it (the legacy ``_CARD_LOCK`` order), so the ENVELOPE and its chain can
        never interleave the server's conversation.  A ``93 00`` (UICC busy) is
        retried (§7.4.1).  A test run owns the card, so the expiry waits for
        it too (v3.25.1).
        """
        if self._test_running():
            self._timer_start(timer_id, 5)
            return
        # Never inject the ENVELOPE while a fetched command awaits its
        # TERMINAL RESPONSE (a paused STK menu) or a chain is running.
        for _ in range(10):
            if not self.stk_pending and not self.busy:
                break
            time.sleep(2)
        with self.card_lock:
            if self.stk_pending or self.busy:
                self._timer_start(timer_id, 5)
                return
            inner = bytes([0x82, 0x02, 0x82, 0x81, 0xA4, 0x01, timer_id & 0xFF,
                           0xA5, 0x03]) + hms_bcd(elapsed)
            template = bytes([0xD7, len(inner)]) + inner
            apdu = cat.envelope_apdu(template, self.cat_cla)
            for _ in range(3):
                try:
                    _data, sw = self._send(apdu)
                except Exception as exc:
                    log.warning('TIMER-EXPIRATION send error: %s', exc)
                    return
                log.info('ENVELOPE(Timer Expiration): timer=%d elapsed=%ds -> '
                         '%s', timer_id, elapsed, sw.hex())
                if sw == b'\x93\x00':
                    # UICC busy: the terminal shall retry until accepted
                    # (§7.4.1).
                    time.sleep(1)
                    continue
                if _is_91(sw):
                    self.handle_chain(sw)
                break

    # ---- the interactive menu ---------------------------------------------

    def menu_fetch_handler(self, response):
        """An on_fetch callback that pauses on a user-interactive command.

        The pending command is stored on the session (:attr:`stk_pending`) and
        mirrored into ``response`` (the dict the HTTP layer returns, so the
        Phone tab renders the prompt/menu); the chain stops *without* a
        TERMINAL RESPONSE - :meth:`menu_send_response` answers later.
        Port of the legacy ``_make_menu_fetch_handler``.

        The pending dict is **JSON-safe** (the ``/api/stk-status``
        ``pending_data`` object is served as-is): its ``raw`` field is the
        command's hex text, not the fetched bytes.  A caller that pauses must
        keep ``menu_active`` in step and arm the watchdog - the "never leave a
        FETCH unanswered" invariant.
        """
        def _on_menu_fetch(raw, cmd_num, cmd_type, dev_src, dev_dst):
            cmd_qual = None
            cd_tag = None
            if raw:
                try:
                    header = proactive.parse_proactive_header(raw)
                    cmd_qual, cd_tag = header[4], header[5]
                except Exception:
                    cmd_qual = None
            if cmd_type == 0x21:
                text = proactive.parse_display_text(raw) if raw else None
                if text:
                    self.stk_pending = {
                        'type': 'display_text', 'cmd_num': cmd_num,
                        'cmd_type': cmd_type, 'cmd_qual': cmd_qual,
                        'cd_tag': cd_tag, 'dev_src': dev_src,
                        'dev_dst': dev_dst, 'text': text,
                        'raw': raw.hex() if raw else ''}
                    response.update(type='display_text', text=text)
                    return 'pause'
            elif cmd_type == 0x24:
                items = proactive.parse_select_item(raw) if raw else []
                self.stk_pending = {
                    'type': 'select_item', 'cmd_num': cmd_num,
                    'cmd_type': cmd_type, 'cmd_qual': cmd_qual,
                    'cd_tag': cd_tag, 'dev_src': dev_src,
                    'dev_dst': dev_dst, 'items': items,
                    'raw': raw.hex() if raw else ''}
                response.update(type='select_item', items=items)
                return 'pause'
            elif cmd_type == 0x25:
                # A SET UP MENU pauses as a select_item: the menu browser
                # answers it exactly like a SELECT ITEM (the legacy shape).
                items = proactive.parse_setup_menu_items(raw) if raw else []
                self.stk_pending = {
                    'type': 'select_item', 'cmd_num': cmd_num,
                    'cmd_type': cmd_type, 'cmd_qual': cmd_qual,
                    'cd_tag': cd_tag, 'dev_src': dev_src,
                    'dev_dst': dev_dst, 'items': items,
                    'raw': raw.hex() if raw else ''}
                response.update(type='select_item', items=items)
                return 'pause'
            elif cmd_type in (0x22, 0x23):
                # GET INKEY / GET INPUT: the user's answer is the TR's Text
                # string, so the panel must pause for it (v3.6.38).
                info = None
                if raw:
                    info = (proactive.parse_get_inkey(raw)
                            if cmd_type == 0x22
                            else proactive.parse_get_input(raw))
                if info:
                    kind = 'get_inkey' if cmd_type == 0x22 else 'get_input'
                    pending = dict(info, type=kind, cmd_num=cmd_num,
                                   cmd_type=cmd_type, cmd_qual=cmd_qual,
                                   cd_tag=cd_tag, dev_src=dev_src,
                                   dev_dst=dev_dst,
                                   raw=raw.hex() if raw else '')
                    duration = proactive.find_duration(raw)
                    pending['duration_unit'] = duration[0] if duration else None
                    pending['shown_at'] = time.monotonic()
                    self.stk_pending = pending
                    response.update(dict(info, type=kind))
                    return 'pause'
            return None
        return _on_menu_fetch

    def menu_send_response(self, result, item_id=None, text=None):
        """Answer the pending command and continue the chain.

        ``result`` is ``ok``/``cancel``/``back``/``timeout``/``help``
        (TS 102 223 §8.12.0); ``item_id`` is the SELECT ITEM choice and
        ``text`` the GET INKEY/GET INPUT answer (validated against the
        request, then coded per §6.8.5/:func:`input_text_tlv`).  The object
        order is §6.8.0: Result (general ``83`` form), Duration, Text string,
        Item identifier.

        Returns the response dict the HTTP layer serves: ``{'sw': <hex>,
        'type': ..., 'items': ..., 'text': ...}`` (``sw`` is hex because it is
        an API field).  A refusal raises :class:`CatSessionError` - the legacy
        returned an HTTP 400 payload instead.

        The Item identifier is sent only for a successful answer (`ok` on a
        SELECT ITEM / SET UP MENU), as the legacy did.  **Known deviation**:
        TS 102 223 §6.8.6 (p. 104) also requires the Item identifier for "Help
        information required by the user" (result `13`), which the legacy never
        implemented - `item_id` is already accepted here, so implementing it is
        part of the cutover together with the PWA's help path (the Phone tab
        must render the item's help for the `help` result before the TR goes
        out).
        """
        if not self.stk_pending:
            raise CatSessionError('no pending command')
        gr = MENU_RESULTS.get(result, 0x00)
        pending = self.stk_pending
        text_tlv = b''
        duration_tlv = b''
        input_types = ('get_inkey', 'get_input')
        if result == 'ok' and pending['type'] in input_types:
            answer, error = validate_input_response(pending, text)
            if error:
                raise CatSessionError(error)
            try:
                text_tlv = input_text_tlv(pending, answer)
            except UnicodeEncodeError as exc:
                raise CatSessionError('cannot encode the response text: %s'
                                      % exc)
            if (pending.get('duration_unit') is not None
                    and pending.get('shown_at') is not None):
                # §6.4.2/§6.4.3: a variable-timeout request gets the actual
                # display/entry duration back, in the requested unit (§8.8).
                elapsed = max(0.0, time.monotonic() - pending['shown_at'])
                factor = {0x00: 1 / 60.0, 0x01: 1.0, 0x02: 10.0}.get(
                    pending['duration_unit'], 1.0)
                value = max(1, min(255, int(round(elapsed * factor))))
                duration_tlv = bytes([0x04, 0x02, pending['duration_unit'],
                                      value])
        tr_data = bytes([
            (pending.get('cd_tag') or 0x81), 0x03,
            pending['cmd_num'] & 0xFF, pending['cmd_type'] & 0xFF,
            (pending.get('cmd_qual') or 0) & 0xFF,
            0x02, 0x02, pending['dev_dst'] & 0xFF, pending['dev_src'] & 0xFF,
            0x83, 0x02, gr, 0x00])
        tr_data += duration_tlv + text_tlv
        if (isinstance(item_id, int) and result == 'ok'
                and pending['type'] == 'select_item'):
            tr_data += bytes([0x10, 0x01, item_id & 0xFF])
        _data, sw = self._send(self._terminal_response_apdu(tr_data))
        log.debug('TR(menu): cmd=%02x type=%02x result=%02x -> %s',
                  pending['cmd_num'], pending['cmd_type'], gr, sw.hex())
        for entry in reversed(self.log):
            if (entry.get('cmd_num') == pending['cmd_num']
                    and entry.get('type_hex') == '%02x' % pending['cmd_type']
                    and 'tr_hex' not in entry):
                self.record_tr(entry, tr_data, sw)
                break
        response = {'sw': sw.hex()}
        if result == 'cancel':
            self.stk_pending = None
            self.menu_active = False
        else:
            self.stk_pending = None
            if _is_91(sw):
                self.handle_chain(sw, self.menu_fetch_handler(response))
            else:
                self.menu_active = False
                response['type'] = 'done'
        if self.stk_pending:
            self.arm_menu_timeout()
        else:
            self.cancel_menu_timeout()
        return response

    def finish_pending_menu(self):
        """Cancel a paused command before a new ENVELOPE (a menu selection).

        A new selection must never shadow a FETCHed command that awaits its
        TERMINAL RESPONSE: answer it with a cancel TR (``10``), then drain any
        follow-up proactive command so the card is ready (the legacy
        ``_finish_pending_menu``; the drain uses the generic chain - no pause).
        """
        pending = self.stk_pending
        if not pending:
            return
        log.info('MENU-SELECT: finishing pending cmd=%02x type=%02x with '
                 'cancel TR', pending['cmd_num'], pending['cmd_type'])
        response = self.menu_send_response('cancel')
        sw = str((response or {}).get('sw') or '')
        if sw.startswith('91'):
            self.handle_chain(_sw_bytes(sw))

    def _clamp_timeout(self, seconds):
        try:
            return max(0, min(3600, int(seconds)))
        except (TypeError, ValueError):
            return 0

    def cancel_menu_timeout(self):
        """Disarm the pending-command watchdog."""
        if self._menu_timer is not None:
            self._menu_timer.cancel()
            self._menu_timer = None

    def arm_menu_timeout(self):
        """Arm the watchdog: a paused command must always get a TERMINAL RESPONSE.

        Fires ``0x12`` ("no response from user") through the same path as an
        explicit answer.  ``menu_timeout`` seconds; 0 disables it.
        """
        self.cancel_menu_timeout()
        if self.menu_timeout <= 0:
            return
        timer = threading.Timer(self.menu_timeout, self.menu_timeout_fire)
        timer.daemon = True
        self._menu_timer = timer
        timer.start()

    def menu_timeout_fire(self):
        """The watchdog callback: answer the pending command with ``timeout``.

        Runs from a ``threading.Timer`` thread, so the card exchange happens
        under :attr:`card_lock` (the legacy ``_CARD_LOCK``) - a watchdog TR
        must never interleave a FETCH/TERMINAL RESPONSE pair.
        """
        self._menu_timer = None
        with self.card_lock:
            if not self.stk_pending:
                return
            pending = self.stk_pending
            log.info('MENU-TIMEOUT: auto TR timeout (cmd=%02x type=%02x)',
                     pending['cmd_num'], pending['cmd_type'])
            try:
                self.menu_send_response('timeout')
            except Exception as exc:
                log.warning('MENU-TIMEOUT error: %s', exc)

    # ---- the proactive log ------------------------------------------------

    def log_proactive(self, cmd_type, raw, qualifier=None, cmd_num=None):
        """Append one FETCHed command to the proactive log; returns the entry.

        The entry is the legacy shape the PWA renders: ``id``, ``type_hex``,
        ``type_name``, ``elapsed`` (seconds since the CAT session start),
        ``bytes``, ``raw`` (hex), ``cmd_num``/``qualifier`` (hex, when known)
        and ``cmd_decoded`` (the ``[{label, value}]`` rows).
        """
        self._entry_id += 1
        raw = bytes(raw or b'')
        entry = {
            'id': self._entry_id,
            'type_hex': '%02x' % (cmd_type & 0xFF),
            'type_name': proactive.PROACTIVE_TYPE_NAMES.get(cmd_type, 'UNKNOWN'),
            'elapsed': round(self.clock() - self._session_start, 1),
            'bytes': len(raw),
            'raw': raw.hex() if raw else None,
        }
        if cmd_num is not None:
            entry['cmd_num'] = cmd_num
        if qualifier is not None:
            entry['qualifier'] = '%02x' % (qualifier & 0xFF)
        try:
            entry['cmd_decoded'] = proactive.decode_proactive_command(
                cmd_type, raw, qualifier)
        except Exception:
            entry['cmd_decoded'] = []
        self.log.append(entry)
        return entry

    def record_tr(self, entry, tr_tlv, sw=None):
        """Attach a sent TERMINAL RESPONSE to a log entry.

        Stores the data part (``tr_hex``, the boilerplate stripped), the status
        word (``tr_sw``, hex - an API field), the decoded Result
        (``tr_result``/``tr_result_name``/``tr_result_info_name``) and the
        decoded objects (``tr_decoded``).  Port of ``_record_tr``.
        """
        if entry is None:
            return
        tr_tlv = bytes(tr_tlv or b'')
        entry['tr_hex'] = tr.tr_data_only(tr_tlv).hex()
        if sw is not None:
            entry['tr_sw'] = _sw_bytes(sw).hex()
        try:
            result = tr.extract_tr_result(tr_tlv)
            if result is not None:
                _tag, value = result
                entry['tr_result'] = value.hex()
                if value:
                    name = tr.tr_result_name(value[0])
                    if name:
                        entry['tr_result_name'] = name
                    if len(value) > 1:
                        info_name = tr.tr_result_add_info_name(value[0],
                                                               value[1])
                        if info_name:
                            entry['tr_result_info_name'] = info_name
            entry['tr_decoded'] = tr.decode_tr(
                int(entry['type_hex'], 16) if entry.get('type_hex') else None,
                int(entry['qualifier'], 16) if entry.get('qualifier') else None,
                bytes.fromhex(entry['tr_hex']))
        except Exception:
            entry['tr_decoded'] = []

    def entries(self, limit=50):
        """The log as the API serves it: newest first, at most ``limit`` rows."""
        if not limit or limit < 0:
            return list(reversed(self.log))
        return list(reversed(self.log[-limit:]))

    def mark_session(self, reason='terminal-profile'):
        """Start a new CAT session without losing the history (v3.23.7).

        Restarts the elapsed timer and appends a boundary marker entry - a
        TERMINAL PROFILE re-send keeps the log (a card change clears it,
        :meth:`reset`).
        """
        self._session_start = self.clock()
        self._entry_id += 1
        self.log.append({'id': self._entry_id, 'marker': reason,
                         'elapsed': 0.0, 'bytes': 0})

    def reset(self):
        """The per-card-session reset: log, menu, pending command, timers.

        The PLI dictionary is configuration and survives (a card change never
        discards the operator's answers); the log keeps its entry counter so
        the PWA's expanded rows stay distinct.  The card's polling wishes are
        cleared too: a new card has neither sent POLLING OFF nor requested an
        interval, so ``poll_suspended`` returns to False and ``poll_interval``
        to the constructor's default (the legacy equip path cleared
        ``_POLL_DISABLED_BY_CARD`` the same way).
        """
        self.cancel_timers()
        self.cancel_menu_timeout()
        self.log.clear()
        self._session_start = self.clock()
        self.sim_menu = None
        self.event_list = None
        self.stk_pending = None
        self.menu_active = False
        self.poll_suspended = False
        self.poll_interval = self._poll_interval_default
