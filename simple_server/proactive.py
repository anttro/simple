# coding=utf-8
"""Proactive command (FETCH) decoding - pure, card-free.

A card announces a proactive command with STATUS ``91XX``; the terminal FETCHes
it (TS 102 221 §11.2.3) and receives a BER-TLV ``D0`` template holding the
command details (``01``/``81``), the device identities (``02``/``82``) and the
command-specific COMPREHENSION-TLVs (TS 102 223 §6.4/§6.6/§8).  This module turns
that template into the ``[{label, value}]`` rows the PWA's proactive log shows,
plus the pieces the FETCH/TERMINAL RESPONSE chain needs (the header, the SET UP
MENU cache, the GET INKEY/GET INPUT request, the BIP channel objects and the
poll-negotiation answer).

Provenance: a behavioural port of the existing server's private helpers
(``pysim_simple_server/server.py``: ``_parse_proactive_header``,
``_decode_cmd``, ``_parse_display_text``, ``_parse_select_item``,
``_parse_setup_menu_items``, ``_parse_setup_menu_command``,
``_parse_get_inkey``, ``_parse_get_input``, ``_decode_bip_cmd``,
``_decode_send_sm``, ``_decode_poll_negotiation`` and ``_find_duration``), with
the I/O switched from hex strings to bytes and the pysim/osmocom imports
replaced by :mod:`simple_server.tlv`, :mod:`simple_server.text`,
:mod:`simple_server.sms` and :mod:`simple_server.cat`.  The decoded shapes are
unchanged - the PWA log renders them.  The command objects are walked as
COMPREHENSION-TLVs throughout (the legacy ``find_duration`` and SET UP EVENT
LIST decode scanned raw bytes; the results are identical on every fixture the
existing suite pins), and the region is everything after the ``D0`` length - a
card's template length is sometimes off by a byte (see :func:`_proactive_body`).

Every TLV lookup accepts both tag styles: bit 8 of a COMPREHENSION-TLV tag is
the comprehension-required flag (TS 101 220 §7.1.1), and real cards mix the
plain and CR-set variants per command - and sometimes per TLV within one
command.

Spec references: TS 102 223 §6.4/§6.6 (the command procedures and the
templates), §8.6/§8.7 (command details, device identities), §8.8 (Duration),
§8.11 (Response length), §8.13 (3GPP-SMS TPDU), §8.15 (text string), §8.23
(Default text), §8.24 (Items Next Action Indicator), §8.25 (event list),
§8.37/§8.38 (timer id/value), §8.49-§8.70 (the BIP command objects), §8.61
(access technology), §8.97 (poll-interval-negotiation result).
"""

import codecs

from simple_server import cat
from simple_server import sms
from simple_server import text as text_codec
from simple_server import tlv

__all__ = [
    'PROACTIVE_TYPE_NAMES', 'PLI_QUALIFIER_NAMES', 'EVENT_NAMES', 'NAI_TYPES',
    'POLL_NEGOTIATION_RESULTS', 'TAG_BEARER', 'TAG_CHANNEL_DATA',
    'TAG_CHANNEL_DATA_LENGTH', 'TAG_CHANNEL_STATUS', 'TAG_BUFFER_SIZE',
    'TAG_TRANSPORT_LEVEL', 'TAG_OTHER_ADDRESS', 'TAG_NAA',
    'cmd_tlv', 'command_tlvs', 'command_tlv_map',
    'item_line', 'bcd_swap', 'duration_seconds', 'duration_text',
    'find_duration', 'parse_proactive_header', 'parse_setup_menu_command',
    'parse_display_text', 'parse_select_item', 'parse_setup_menu_items',
    'parse_get_inkey', 'parse_get_input', 'parse_other_address',
    'parse_transport_level', 'parse_udh', 'decode_sms_ud',
    'decode_proactive_command',
    'decode_send_sm', 'decode_bip_command', 'decode_poll_negotiation',
]

# TS 102 223 §9.4 command types (the values are also the proactive command
# template tags).
PROACTIVE_TYPE_NAMES = {
    0x01: 'REFRESH', 0x02: 'MORE TIME', 0x03: 'POLL INTERVAL',
    0x04: 'POLLING OFF', 0x05: 'SET UP EVENT LIST', 0x10: 'SET UP CALL',
    0x11: 'SEND SS', 0x12: 'SEND USSD', 0x13: 'SEND SHORT MESSAGE',
    0x14: 'SEND DTMF', 0x15: 'LAUNCH BROWSER',
    0x16: 'GEOGRAPHICAL LOCATION REQUEST', 0x20: 'PLAY TONE',
    0x21: 'DISPLAY TEXT', 0x22: 'GET INKEY', 0x23: 'GET INPUT',
    0x24: 'SELECT ITEM', 0x25: 'SET UP MENU',
    0x26: 'PROVIDE LOCAL INFORMATION', 0x27: 'TIMER MANAGEMENT',
    0x28: 'SET UP IDLE MODE TEXT', 0x30: 'PERFORM CARD APDU',
    0x31: 'POWER ON CARD', 0x32: 'POWER OFF CARD', 0x33: 'GET READER STATUS',
    0x34: 'RUN AT COMMAND', 0x35: 'LANGUAGE NOTIFICATION',
    0x40: 'OPEN CHANNEL', 0x41: 'CLOSE CHANNEL', 0x42: 'RECEIVE DATA',
    0x43: 'SEND DATA', 0x44: 'GET CHANNEL STATUS', 0x45: 'SERVICE SEARCH',
    0x46: 'GET SERVICE INFORMATION', 0x47: 'DECLARE SERVICE',
    0x50: 'SET FRAMES', 0x51: 'GET FRAMES STATUS',
    0x60: 'RETRIEVE MULTIMEDIA MESSAGE', 0x61: 'SUBMIT MULTIMEDIA MESSAGE',
    0x62: 'DISPLAY MULTIMEDIA MESSAGE', 0x70: 'ACTIVATE',
    0x71: 'CONTACTLESS STATE CHANGED', 0x72: 'COMMAND CONTAINER',
    0x73: 'ENCAPSULATED SESSION CONTROL', 0x79: 'LSI COMMAND',
    0x81: 'End of the proactive UICC session',
}

# PROVIDE LOCAL INFORMATION qualifier codings (TS 102 223 v18.3.0: the request
# is §6.4.15, the answer objects are §8.19 location information, §8.20 IMEI,
# §8.39 date-time, §8.45 language, §8.61 access technology, §8.74 IMEISV,
# §8.75 network search mode, §8.76 battery state; the remaining qualifiers are
# assigned by TS 31.111).
PLI_QUALIFIER_NAMES = {
    0x00: 'Location Information (MCC, MNC, LAC/TAC, Cell ID)',
    0x01: 'IMEI',
    0x02: 'Network Measurement results',
    0x03: 'Date, time and time zone',
    0x04: 'Language setting',
    0x05: 'Reserved for GSM (Timing Advance, TS 51 111)',
    0x06: 'Access Technology (single)',
    0x07: 'ESN of the terminal',
    0x08: 'IMEISV',
    0x09: 'Search Mode',
    0x0A: 'Battery charge state',
    0x0B: 'MEID of the terminal',
    0x0C: 'Current WSID',
    0x0D: 'Broadcast Network information',
    0x0E: 'Multiple Access Technologies',
    0x0F: 'Location Info (multi-RAT)',
    0x10: 'NMR (multi-RAT)',
    0x11: 'CSG ID list + HNB name',
    0x12: 'H(e)NB IP address',
    0x13: 'H(e)NB surrounding macrocells',
    0x14: 'Current WLAN identifier',
    0x15: 'Slices information',
    0x16: 'CAG information list',
    0x17: 'Rejected slices information',
    0x1A: 'Supported Radio Access Technologies',
}

# Event list codings (TS 102 223 v18.3.0 §8.25).  The values the CAT spec only
# points at 3GPP for carry their TS 31.111 §7.5 event name; "Reserved for
# 3GPP" is a spec cross-reference, not a usage restriction, so it never
# appears in a user-facing name.
EVENT_NAMES = {
    0x00: 'MT call', 0x01: 'Call connected', 0x02: 'Call disconnected',
    0x03: 'Location status', 0x04: 'User activity', 0x05: 'Idle screen available',
    0x06: 'Card reader status', 0x07: 'Language selection',
    0x08: 'Browser termination', 0x09: 'Data available',
    0x0A: 'Channel status', 0x0B: 'Access technology change (single access technology)',
    0x0C: 'Display parameters changed', 0x0D: 'Local connection',
    0x0E: 'Network search mode change', 0x0F: 'Browsing status',
    0x10: 'Frames information change',
    0x11: '(I-)WLAN access status',
    0x12: 'Network rejection',
    0x13: 'HCI connectivity',
    0x14: 'Access technology change (multiple access technologies)',
    0x15: 'CSG cell selection',
    0x16: 'Contactless state request',
    0x17: 'IMS registration',
    0x18: 'Incoming IMS data',
    0x19: 'Profile container', 0x1A: 'Void', 0x1B: 'Secured profile container',
    0x1C: 'Poll interval negotiation',
    0x1D: 'Data connection status change',
    0x1E: 'CAG cell selection',
    0x1F: 'Slices status change',
    0x20: 'Reserved (future usage)',
    0x21: 'Reserved (future usage)',
    0x22: 'Reserved (future usage)',
}

# TS 102 223 §8.24 + Table 9.4: the Items Next Action Indicator reuses the
# Type of Command coding, but only the values the table marks "Used for Next
# Action Indicator".  The ToC-only values are reserved there and shall be
# ignored, as are '00' and any value not listed (§8.24).
NAI_TYPES = frozenset([
    0x10, 0x11, 0x12, 0x13, 0x15,             # SET UP CALL / SEND SS / SEND USSD /
                                              # SEND SM / LAUNCH BROWSER
    0x20, 0x21, 0x22, 0x23, 0x24, 0x25, 0x28,  # PLAY TONE .. SET UP IDLE MODE TEXT
    0x30, 0x31, 0x32, 0x33,                   # PERFORM CARD APDU .. GET READER STATUS
    0x40, 0x41, 0x42, 0x43, 0x44, 0x45, 0x46,  # BIP commands
    0x60, 0x61, 0x62,                         # MMS commands
    0x81,                                     # end of the proactive session
])

# BIP command-specific object tags (TS 102 223 §8.49 bearer, §8.53 channel
# data, §8.54 channel data length, §8.55 buffer size, §8.56 channel status,
# §8.58 other address, §8.59 transport level, §8.70 network access name); the
# base (CR=0) tag values - cmd_tlv also matches the CR-set variants.
TAG_BEARER = 0x35
TAG_CHANNEL_DATA = 0x36
TAG_CHANNEL_DATA_LENGTH = 0x37
TAG_CHANNEL_STATUS = 0x38
TAG_BUFFER_SIZE = 0x39
TAG_TRANSPORT_LEVEL = 0x3C
TAG_OTHER_ADDRESS = 0x3E
TAG_NAA = 0x47

_TIMER_ACTIONS = {0x00: 'Start', 0x01: 'Deactivate', 0x02: 'Get current value'}

# SMS TPDU type (TS 23.040 TP-MTI, bits 1-2 of the first octet).
_SMS_MTI = {0: 'SMS-DELIVER', 1: 'SMS-SUBMIT', 2: 'SMS-COMMAND', 3: 'Reserved'}

# Poll interval negotiation result (TS 102 223 §8.97).
POLL_NEGOTIATION_RESULTS = {0: 'accepted', 1: 'rejected', 2: 'modified'}


# ---- shared helpers -------------------------------------------------------

def _as_bytes(value):
    """Bytes from bytes-like or hex data (internal whitespace ignored)."""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if not value:
        return b''
    return bytes.fromhex(''.join(str(value).split()))


def _proactive_body(raw):
    """The TLV region of a D0 proactive command: everything after its BER length.

    The region is deliberately not clipped to the ``D0`` length: a card whose
    template length is off by a byte still delivers complete objects, and the
    walk stops at a truncated TLV anyway (the same tolerance the existing
    server's SMS-TPDU lookup documents).  A buffer without the ``D0`` wrapper
    has no command body.
    """
    raw = _as_bytes(raw)
    if not raw or raw[0] != 0xD0:
        return b''
    return raw[tlv.parse_ber_len(raw, 1)[1]:]


def _command_tlvs(raw):
    """``[(tag, value)]`` of the command's TLVs.

    The command-specific objects are COMPREHENSION-TLVs (TS 101 220 §7.1.1):
    one tag octet (bit 8 is the CR flag, not part of the tag number) and a BER
    length - the short form (``nn``) or the long form a FETCH template's big
    objects use (``81 nn``, e.g. the live card's 150-byte SMS TPDU coded
    ``8B 81 96``), so the walk never treats a tag as multi-octet and never
    reads a long-form length as the value's first octet.
    """
    return tlv.ber_tlv_list(_proactive_body(raw), multi_byte_tags=False)


def _command_tlv_map(raw):
    """Top-level ``{tag: value}`` map of the command's TLVs (first wins)."""
    return tlv.tlv_map(_proactive_body(raw), multi_byte_tags=False)


#: Public aliases: the CAT session (and any caller outside this module) must
#: not reach into private names (``_command_tlvs``/``_command_tlv_map``).
command_tlvs = _command_tlvs
command_tlv_map = _command_tlv_map


def cmd_tlv(tlvs, tag):
    """Fetch a command TLV, tolerating both tag styles.

    Bit 8 of the tag is the comprehension-required flag (TS 101 220 §7.1.1),
    so the other variant is ``tag ^ 0x80`` - covering both directions
    (``0x24``/``0xA4``, ``0x8D``/``0x0D``, ``0x05``/``0x85``).  The exact tag
    wins; a missing object is ``b''``.
    """
    return tlvs.get(tag) or tlvs.get(tag ^ 0x80) or b''


def bcd_swap(octet):
    """Semi-octet BCD digit pair (TS 123 040 TP-SCT): the low nibble is the tens."""
    return (octet & 0x0F) * 10 + ((octet >> 4) & 0x0F)


def duration_seconds(unit, interval):
    """A Duration TLV value (TS 102 223 §8.8) as seconds, clamped to 1..255."""
    if unit == 0x00:        # minutes
        secs = interval * 60
    elif unit == 0x02:      # tenths of seconds
        secs = int(round(interval / 10.0))
    else:                   # seconds
        secs = interval
    return max(1, min(255, secs))


def duration_text(unit, interval):
    """The human form of a Duration TLV value: '5 min' / '1.5 s' / '30 s'."""
    if unit == 0x00:
        return '%d min' % interval
    if unit == 0x02:
        return '%.1f s' % (interval / 10.0)
    return '%d s' % interval


def find_duration(raw):
    """``(unit, interval)`` of the command's Duration TLV, or None.

    Tag ``04`` or its CR-set variant ``84`` (TS 102 223 §8.8).  A TLV walk, so
    an object before it may carry a BER long-form length.
    """
    if not raw:
        return None
    for tag, val in _command_tlvs(raw):
        if tag in (0x04, 0x84) and len(val) >= 2:
            return val[0], val[1]
    return None


def _nai_name(value):
    """Items Next Action Indicator name (TS 102 223 Table 9.4); None = reserved."""
    if value in NAI_TYPES:
        return PROACTIVE_TYPE_NAMES.get(value)
    if 0xF0 <= value <= 0xFE:
        return 'Proprietary (0x%02X)' % value
    return None


def _attach_nai(items, nai):
    """Attach an Items Next Action Indicator list (TS 102 223 §8.24) to items.

    One byte per item in list order; a short list leaves the tail without an
    indicator, extra bytes are ignored, reserved values are skipped (§8.24).
    """
    if not items or not nai:
        return items
    for i, item in enumerate(items):
        if i >= len(nai):
            break
        name = _nai_name(nai[i])
        if name:
            item['nai'] = nai[i]
            item['nai_name'] = name
    return items


def item_line(item):
    """``'1. Menu'`` plus ``' → SET UP MENU'`` when the item has a next action."""
    line = '%s. %s' % (item['id'], item['text'])
    if item.get('nai_name'):
        line += ' \u2192 ' + item['nai_name']
    return line


# ---- BIP objects ----------------------------------------------------------

def parse_other_address(value):
    """Decode an 'Other address' object (TS 102 223 §8.58: 21 IPv4, 57 IPv6, F0 FQDN)."""
    if not value:
        return None
    kind = value[0]
    if kind == 0x21 and len(value) >= 5:
        return '.'.join(str(b) for b in value[1:5])
    if kind == 0x57 and len(value) >= 17:
        return ':'.join('%x' % int.from_bytes(value[i:i + 2], 'big')
                        for i in range(1, 17, 2))
    if kind == 0xF0:
        return value[1:].decode('ascii', 'replace')
    return None


def parse_transport_level(value):
    """Decode the UICC/terminal interface transport level object -> (proto, port).

    The value is the transport protocol octet followed by a 2-octet port
    (TS 102 223 §8.59); ``(None, None)`` for a truncated object.
    """
    if not value or len(value) < 3:
        return None, None
    return value[0], int.from_bytes(value[1:3], 'big')


# ---- command parsers ------------------------------------------------------

def parse_proactive_header(raw):
    """``(cmd_num, cmd_type, dev_src, dev_dst, cmd_qual, cd_tag)`` of a command.

    ``cmd_qual`` is the command-qualifier octet of the command details
    (TS 102 223 §8.6); ``cd_tag`` is the tag the card used for that object
    (``01`` plain or ``81`` comprehension-required) - the TERMINAL RESPONSE
    echoes it (§6.8.1).  Both tag styles are accepted, and the defaults
    (``1``, ``0``, UICC -> terminal, no qualifier, no tag) cover a template
    without a parseable header.
    """
    cmd_num, cmd_type = 1, 0
    cmd_qual = None
    dev_src, dev_dst = 0x83, 0x81
    cd_tag = None
    for tag, val in _command_tlvs(raw):
        if tag in (0x01, 0x81) and len(val) >= 3:
            cd_tag = tag
            cmd_num, cmd_type, cmd_qual = val[0], val[1], val[2]
        elif tag in (0x02, 0x82) and len(val) >= 2:
            dev_src, dev_dst = val[0], val[1]
    return cmd_num, cmd_type, dev_src, dev_dst, cmd_qual, cd_tag


def parse_setup_menu_command(raw):
    """Parse a SET UP MENU: command number, title and items (with their NAI).

    The title is the Alpha identifier (``85``/``05``) and the items are the
    Item objects (``8F``/``0F``); both tag styles must be accepted (§7.1.1 of
    TS 101 220) - real cards send the title as ``85`` while the items use the
    plain ``8F``.  Returns the menu dict or ``None`` when the template is not a
    SET UP MENU.  The walk is not bounded by the ``D0`` length, so a card whose
    template length is off by one still delivers its complete menu; a
    truncated TLV ends the walk.
    """
    raw = _as_bytes(raw)
    if not raw or raw[0] != 0xD0:
        return None
    tlvs = tlv.tlv_map(_proactive_body(raw), multi_byte_tags=False)
    cd = cmd_tlv(tlvs, 0x81)
    if len(cd) < 3 or cd[1] != 0x25:
        return None
    menu = {'command_number': cd[0], 'items': []}
    nai = None
    for tag, val in _command_tlvs(raw):
        if tag in (0x85, 0x05) and val:
            menu['title'] = text_codec.decode_stk_text(val)
        elif tag in (0x8F, 0x0F) and len(val) >= 2:
            menu['items'].append({'id': val[0],
                                  'text': text_codec.decode_stk_text(val[1:])})
        elif tag in (0x18, 0x98) and val:
            nai = val
    _attach_nai(menu['items'], nai)
    return menu


def parse_display_text(raw):
    """The DISPLAY TEXT text string (the Text object ``8D``/``0D``), or None."""
    for tag, val in _command_tlvs(raw):
        if tag in (0x8D, 0x0D) and val:
            return text_codec.decode_dcs_text(val)
    return None


def _item_objects(raw):
    """``(items, nai)`` of a SELECT ITEM / SET UP MENU command."""
    items = []
    nai = None
    for tag, val in _command_tlvs(raw):
        if tag in (0x8F, 0x0F) and len(val) >= 2:
            items.append({'id': val[0],
                          'text': text_codec.decode_stk_text(val[1:])})
        elif tag in (0x18, 0x98) and val:
            nai = val
    return _attach_nai(items, nai), nai


def parse_select_item(raw):
    """The SELECT ITEM items: ``[{'id', 'text', ['nai', 'nai_name']}]``."""
    items, _nai = _item_objects(raw)
    return items


def parse_setup_menu_items(raw):
    """The SET UP MENU items: ``[{'id', 'text', ['nai', 'nai_name']}]``."""
    items, _nai = _item_objects(raw)
    return items


def _cmd_qualifier(tlvs):
    """Command Qualifier octet of the Command details TLV (TS 102 223 §8.6)."""
    cd = cmd_tlv(tlvs, 0x81)
    return cd[2] if len(cd) >= 3 else None


def parse_get_inkey(raw):
    """GET INKEY (``0x22``) fields: prompt text and the qualifier's request flags.

    TS 102 223 §6.6.2, §8.6, §8.15.  Returns ``None`` when the template is not
    a parseable GET INKEY (no Text string).
    """
    tlvs = _command_tlv_map(raw)
    text = cmd_tlv(tlvs, 0x8D)
    if not text:
        return None
    qual = _cmd_qualifier(tlvs)
    return {
        'text': text_codec.decode_dcs_text(text),
        'digits_only': bool(qual is not None and not (qual & 0x01)),
        'ucs2': bool(qual is not None and (qual & 0x02)),
        'yes_no': bool(qual is not None and (qual & 0x04)),
        'immediate': bool(qual is not None and (qual & 0x08)),
        'help': bool(qual is not None and (qual & 0x80)),
    }


def parse_get_input(raw):
    """GET INPUT (``0x23``) fields: prompt, Response length, Default Text and flags.

    TS 102 223 §6.6.3, §8.6, §8.11, §8.23.  Returns ``None`` when the template
    is not a parseable GET INPUT (no Text string).
    """
    tlvs = _command_tlv_map(raw)
    text = cmd_tlv(tlvs, 0x8D)
    if not text:
        return None
    qual = _cmd_qualifier(tlvs)
    response_length = cmd_tlv(tlvs, 0x91)
    default = cmd_tlv(tlvs, 0x9D)
    return {
        'text': text_codec.decode_dcs_text(text),
        'min': response_length[0] if len(response_length) >= 1 else 0,
        'max': response_length[1] if len(response_length) >= 2 else 0xFF,
        'default': text_codec.decode_dcs_text(default) if default else None,
        'digits_only': bool(qual is not None and not (qual & 0x01)),
        'ucs2': bool(qual is not None and (qual & 0x02)),
        'hidden': bool(qual is not None and (qual & 0x04)),
        'packed': bool(qual is not None and (qual & 0x08)),
        'help': bool(qual is not None and (qual & 0x80)),
    }


# ---- SEND SHORT MESSAGE ---------------------------------------------------

def parse_udh(ud):
    """Parse an SMS user-data header (TS 23.040 §9.2.3.24).

    Returns ``(fields, octets, septets)``: the decoded IEs, the UDH length in
    octets and the number of septets it occupies in a 7-bit packed UD.
    """
    ud = _as_bytes(ud)
    if len(ud) < 2:
        return [], 0, 0
    udhl = ud[0]
    if udhl < 1 or 1 + udhl > len(ud):
        return [], 0, 0
    out = []
    off = 1
    while off + 2 <= 1 + udhl:
        iei, iei_len = ud[off], ud[off + 1]
        val = ud[off + 2: off + 2 + iei_len]
        if iei == 0x00 and iei_len == 3:
            out.append({'label': 'Concat (8-bit ref)',
                        'value': '%d, part %d/%d' % (val[0], val[1], val[2])})
        elif iei == 0x08 and iei_len == 4:
            out.append({'label': 'Concat (16-bit ref)',
                        'value': '%d, part %d/%d' % (int.from_bytes(val[:2], 'big'),
                                                     val[2], val[3])})
        else:
            out.append({'label': 'UDH IE 0x%02X' % iei, 'value': val.hex().upper()})
        off += 2 + iei_len
    octets = 1 + udhl
    return out, octets, (octets * 8 + 6) // 7


def _unpack_septets(data, count):
    """Unpack ``count`` 7-bit septets from the packed GSM default alphabet."""
    out = bytearray()
    acc, bits = 0, 0
    for octet in data:
        acc |= octet << bits
        bits += 8
        while bits >= 7 and len(out) < count:
            out.append(acc & 0x7F)
            acc >>= 7
            bits -= 7
    return bytes(out)


def decode_sms_ud(pid, dcs, ud, udhi=False, udl=None):
    """Decode the TP-UD of an SMS TPDU into log rows.

    The UDH (when ``udhi``), a secured packet for PID ``7F`` (TS 31.115) or
    the text message body for a text DCS (TS 23.038).  A decode failure keeps
    the raw user data instead of raising.
    """
    ud = _as_bytes(ud)
    out = []
    header_octets, header_septets = 0, 0
    if udhi:
        fields, header_octets, header_septets = parse_udh(ud)
        out.extend(fields)
    data = ud[header_octets:]
    if pid == 0x7F:
        # SIM data download: the user data is a secured packet (TS 31.115).
        out.append({'label': 'Secured packet (TS 31.115)',
                    'value': '%d bytes: %s' % (len(data), data.hex().upper())})
        return out
    cls = dcs & 0x0C
    try:
        if cls == 0x00:
            # GSM 7-bit default alphabet, packed septets; the UDH (if any)
            # occupies whole septets at the start of the packed data.
            n = udl if udl else (len(ud) * 8) // 7
            septets = _unpack_septets(ud[: (n * 7 + 7) // 8], n)[header_septets:]
            text = codecs.decode(septets, 'gsm03.38')
        elif cls == 0x08:
            text = codecs.decode(data, 'utf_16_be')
        elif cls == 0x04:
            text = data.decode('latin-1', errors='replace')
        else:
            out.append({'label': 'User data',
                        'value': '%d bytes: %s' % (len(data), data.hex().upper())})
            return out
        out.append({'label': 'Text', 'value': text})
    except Exception:
        out.append({'label': 'User data',
                    'value': '%d bytes: %s' % (len(data), data.hex().upper())})
    return out


def decode_send_sm(raw):
    """Decode a SEND SHORT MESSAGE command (TS 102 223 §6.4.10).

    The Alpha identifier, the address object and the 3GPP-SMS TPDU with its
    user data; a malformed TPDU keeps the raw TPDU row and never breaks the
    log.
    """
    out = []
    tlvs = _command_tlv_map(raw)
    alpha = cmd_tlv(tlvs, 0x05)
    if alpha:
        try:
            out.append({'label': 'Alpha', 'value': text_codec.decode_stk_text(alpha)})
        except Exception:
            pass
    addr = cmd_tlv(tlvs, 0x06)
    if addr:
        try:
            address = cat.Address.from_bytes(addr)
            value = address.number
            if address.ton:
                value += ' (%s)' % address.ton
            out.append({'label': 'Address', 'value': value})
        except Exception:
            out.append({'label': 'Address', 'value': addr.hex().upper()})
    tpdu = cmd_tlv(tlvs, 0x0B)
    if not tpdu:
        return out
    out.append({'label': 'SMS TPDU', 'value': tpdu.hex().upper()})
    try:
        mti = tpdu[0] & 0x03
        out.append({'label': 'Type', 'value': _SMS_MTI.get(mti, 'Reserved')})
        if mti == 1:
            submit = sms.SmsSubmit.from_bytes(tpdu)
            out.append({'label': 'TP-MR', 'value': str(submit.mr)})
            if submit.da:
                out.append({'label': 'TP-DA', 'value': submit.da})
            out.append({'label': 'TP-PID', 'value': '0x%02X%s' % (
                submit.pid, ' (SIM data download)' if submit.pid == 0x7F else '')})
            out.append({'label': 'TP-DCS', 'value': '0x%02X' % submit.dcs})
            if submit.vp:
                out.append({'label': 'TP-VP', 'value': bytes(submit.vp).hex().upper()})
            out.append({'label': 'TP-UDL', 'value': str(submit.udl)})
            out.extend(decode_sms_ud(submit.pid, submit.dcs, submit.ud,
                                     bool(submit.udhi), submit.udl))
    except Exception:
        # Malformed TPDU: keep the raw hex line above, never break the log.
        pass
    return out


# ---- BIP commands ---------------------------------------------------------

def decode_bip_command(cmd_type, raw):
    """Decode OPEN CHANNEL (0x40) / RECEIVE DATA (0x42) / SEND DATA (0x43)."""
    tlvs = _command_tlv_map(raw)
    out = []
    dev = cmd_tlv(tlvs, 0x02)
    if len(dev) >= 2 and 0x21 <= dev[1] <= 0x27:
        out.append({'label': 'Channel', 'value': str(dev[1] & 0x07)})
    if cmd_type == 0x40:
        bearer = cmd_tlv(tlvs, TAG_BEARER)
        if bearer:
            out.append({'label': 'Bearer', 'value': '0x%02X' % bearer[0]})
        buffer_size = cmd_tlv(tlvs, TAG_BUFFER_SIZE)
        if len(buffer_size) >= 2:
            out.append({'label': 'Buffer size',
                        'value': str(int.from_bytes(buffer_size[:2], 'big'))})
        naa = cmd_tlv(tlvs, TAG_NAA)
        if naa:
            out.append({'label': 'APN', 'value': naa[1:].decode('ascii', 'replace')})
        address = parse_other_address(cmd_tlv(tlvs, TAG_OTHER_ADDRESS))
        if address:
            out.append({'label': 'Destination', 'value': address})
        proto, port = parse_transport_level(cmd_tlv(tlvs, TAG_TRANSPORT_LEVEL))
        if port is not None:
            out.append({'label': 'Transport',
                        'value': '%s port %d' % ({0x02: 'TCP client'}.get(
                            proto, 'proto 0x%02X' % (proto or 0)), port)})
    elif cmd_type == 0x42:
        requested = cmd_tlv(tlvs, TAG_CHANNEL_DATA_LENGTH)
        if requested:
            out.append({'label': 'Requested bytes', 'value': str(requested[0])})
    elif cmd_type == 0x43:
        data = cmd_tlv(tlvs, TAG_CHANNEL_DATA)
        out.append({'label': 'Data bytes', 'value': str(len(data))})
        if data:
            out.append({'label': 'Data', 'value': data.hex()[:120]})
    return out


# ---- the command decoder --------------------------------------------------

def decode_proactive_command(cmd_type, raw, qualifier=None):
    """Decode a fetched proactive command into ``[{label, value}]`` rows.

    ``qualifier`` is the command-qualifier octet (it names the PROVIDE LOCAL
    INFORMATION request and the TIMER MANAGEMENT action).  A command type
    without a dedicated decoder - and a value from ``PROACTIVE_TYPE_NAMES`` that
    has no sub-decoder - yields the raw ``{'Data': <hex>}`` row.

    The log path may never raise: ``raw`` is bytes or hex, and hex that does
    not parse is treated like an empty buffer (``[]``).
    """
    try:
        raw = _as_bytes(raw)
    except ValueError:
        raw = b''
    if not raw:
        return []
    if cmd_type == 0x03:
        duration = find_duration(raw)
        if duration:
            text = duration_text(duration[0], duration[1])
            if duration[0] != 0x01:
                text += ' (%d s)' % duration_seconds(duration[0], duration[1])
            return [{'label': 'Interval', 'value': text}]
        return []
    if cmd_type == 0x05:
        for tag, val in _command_tlvs(raw):
            if tag in (0x99, 0x19) and val:
                names = [EVENT_NAMES.get(b, 'Event 0x%02X' % b) for b in val]
                return [{'label': 'Events', 'value': ', '.join(names)}]
        return []
    if cmd_type == 0x13:
        return decode_send_sm(raw)
    if cmd_type == 0x21:
        text = parse_display_text(raw)
        return [{'label': 'Text', 'value': text}] if text else []
    if cmd_type in (0x22, 0x23):
        info = (parse_get_inkey(raw) if cmd_type == 0x22
                else parse_get_input(raw))
        if not info:
            return []
        out = [{'label': 'Text', 'value': info['text']}]
        if cmd_type == 0x22:
            kind = ('Yes/No' if info['yes_no'] else
                    'digits only' if info['digits_only'] else
                    'UCS2' if info['ucs2'] else 'SMS default alphabet')
            if info['immediate']:
                kind += ', immediate'
        else:
            kind = ('digits only' if info['digits_only'] else
                    'UCS2' if info['ucs2'] else 'SMS default alphabet')
            if info['hidden']:
                kind += ', hidden'
            if info['packed']:
                kind += ', packed'
            max_text = 'no max' if info['max'] in (0, 0xFF) else str(info['max'])
            out.append({'label': 'Length', 'value': '%d..%s' % (info['min'], max_text)})
            if info.get('default'):
                out.append({'label': 'Default', 'value': info['default']})
        if info['help']:
            kind += ', help'
        out.append({'label': 'Response', 'value': kind})
        return out
    if cmd_type == 0x24:
        items = parse_select_item(raw)
        if items:
            return [{'label': 'Items', 'value': ', '.join(item_line(it) for it in items)}]
        return []
    if cmd_type == 0x25:
        items = parse_setup_menu_items(raw)
        if items:
            return [{'label': 'Items', 'value': ', '.join(item_line(it) for it in items)}]
        return []
    if cmd_type in (0x40, 0x42, 0x43):
        return decode_bip_command(cmd_type, raw)
    if cmd_type == 0x26 and qualifier is not None:
        name = PLI_QUALIFIER_NAMES.get(qualifier, 'Unknown')
        return [{'label': 'Qualifier', 'value': '%s (0x%02X)' % (name, qualifier)}]
    if cmd_type == 0x27:
        out = []
        if qualifier is not None:
            action = _TIMER_ACTIONS.get(qualifier & 0x03)
            out.append({'label': 'Action',
                        'value': action or 'Reserved (0x%02X)' % qualifier})
        tlvs = _command_tlv_map(raw)
        timer = cmd_tlv(tlvs, 0x24)
        if timer:
            timer_id = timer[0]
            out.append({'label': 'Timer',
                        'value': str(timer_id) if 1 <= timer_id <= 8
                        else 'Invalid (0x%02X)' % timer_id})
        value = cmd_tlv(tlvs, 0x25)
        if len(value) >= 3:
            out.append({'label': 'Value', 'value': '%02d:%02d:%02d' % (
                bcd_swap(value[0]), bcd_swap(value[1]), bcd_swap(value[2]))})
        return out
    return [{'label': 'Data', 'value': raw.hex()}]


# ---- poll interval negotiation --------------------------------------------

def decode_poll_negotiation(data):
    """Decode a Poll Interval Negotiation answer (TS 102 223 §7.5.22.2/§8.97).

    ``data`` is the ENVELOPE response data (bytes or hex); no response data
    means ``accepted``.  Returns ``None`` for malformed data or when the result
    object is missing.
    """
    try:
        raw = _as_bytes(data)
    except ValueError:
        return None
    if not raw:
        return {'result': 0, 'result_name': 'accepted', 'unit': None,
                'interval': None, 'seconds': None}
    result = unit = interval = None
    for tag, val in tlv.ber_tlv_list(raw, multi_byte_tags=False):
        if tag in (0x11, 0x91) and len(val) >= 1:
            result = val[0]
        elif tag in (0x04, 0x84) and len(val) >= 2:
            unit, interval = val[0], val[1]
    if result is None:
        return None
    out = {'result': result,
           'result_name': POLL_NEGOTIATION_RESULTS.get(result, 'unknown'),
           'unit': unit, 'interval': interval, 'seconds': None}
    if unit is not None:
        out['seconds'] = duration_seconds(unit, interval)
    return out
