# coding=utf-8
"""TERMINAL RESPONSE building and decoding - pure, card-free.

Every FETCHed proactive command must be answered with a TERMINAL RESPONSE
(TS 102 223 §6.8, TS 102 221 §11.2.4):

    CLA 14 00 00 <Lc> <command details> <device identities> <Result>
                       [command-specific objects] [extra objects]

The payload is a flat COMPREHENSION-TLV sequence - no ``D0`` BER wrapper (that
is the UICC -> terminal direction only).  The command details echo the command's
own tag form and qualifier (§6.8.1: "the object shall be identical to the
command's object, including the comprehension required flag") and the response
objects use the base CR=0 tag forms (§6.8.0: every response object is Min = N,
so "the terminal should set the CR flag to comprehension not required").

Provenance: a behavioural port of the existing server's ``_build_tr``,
``_decode_tr``, ``_tr_data_only``, ``_extract_tr_result``, ``_tr_result_name`` /
``_tr_result_add_info_name`` (``pysim_simple_server/server.py``) and
``testscript.build_tr`` / ``TR_RESULTS`` / ``TR_RESULT_ADD_INFO``, with the I/O
switched from hex strings to bytes.  The PWA's scripted-TERMINAL-RESPONSE
builder and the test runner keep the same byte output.

Spec references: TS 102 223 §6.8.0/§6.8.1 (structure and object order), §8.6
(command details, command qualifier), §8.7 (device identities), §8.8
(Duration), §8.12.0/§8.12.2 (result and additional information), §8.15 (text
string), §8.37/§8.38 (timer identifier/value), §8.39 (date-time and time zone);
TS 123 040 §9.2.3.11 (the TP-SCTS semi-octet BCD coding the date/time reuses).
"""

from simple_server import tlv
from simple_server.proactive import bcd_swap
from simple_server.text import encode_text

__all__ = [
    'TR_RESULT_NAMES', 'TR_RESULT_ADD_INFO_NAMES', 'ACCESSTECH_NAMES',
    'tr_result_name', 'tr_result_add_info_name', 'tr_data_only',
    'extract_tr_result', 'build_tr', 'decode_tr',
]

# The general result (TS 102 223 v18.3.0 §8.12.0).  The result byte has the
# same coding in the basic (03) and general (83) forms; only the general form
# carries additional information, which is named separately (§8.12.2).
TR_RESULT_NAMES = {
    '00': 'Command performed successfully',
    '01': 'Command performed with partial comprehension',
    '02': 'Command performed, with missing information',
    '03': 'REFRESH performed with additional EFs read',
    '04': 'Command performed successfully, but requested icon could not be displayed',
    '05': 'Command performed, but modified by call control by NAA',
    '06': 'Command performed successfully, limited service',
    '07': 'Command performed with modification',
    '08': 'REFRESH performed but indicated NAA was not active',
    '09': 'Command performed successfully, tone not played',
    '10': 'Proactive UICC session terminated by the user',
    '11': 'Backward move in the proactive UICC session requested by the user',
    '12': 'No response from user',
    '13': 'Help information required by the user',
    '14': 'Reserved for GSM/3G',
    '15': 'Reserved for 3GPP (for future usage)',
    '16': 'Reserved for 3GPP (for future usage)',
    '20': 'Terminal currently unable to process command',
    '21': 'Network currently unable to process command',
    '22': 'User did not accept the proactive command',
    '23': 'User cleared down call before connection or network release',
    '24': 'Action in contradiction with the current timer state',
    '25': 'Interaction with call control by NAA, temporary problem',
    '26': 'Launch browser generic error',
    '27': 'MMS temporary problem',
    '28': 'Reserved for 3GPP (BIP temporary error, T timer in the additional information)',
    '29': 'Reserved for 3GPP (for future usage)',
    '30': "Command beyond terminal's capabilities",
    '31': 'Command type not understood by terminal',
    '32': 'Command data not understood by terminal',
    '33': 'Command number not known by terminal',
    '34': 'Reserved for GSM/3G',
    '35': 'Reserved for GSM/3G',
    '36': 'Error, required values are missing',
    '37': 'Reserved for GSM/3G',
    '38': 'MultipleCard commands error',
    '39': 'Interaction with call control by NAA, permanent problem',
    '3A': 'Bearer Independent Protocol permanent error',
    '3B': 'Access Technology unable to process command',
    '3C': 'Frames error',
    '3D': 'MMS Error',
    '3E': 'Reserved for 3GPP (for future usage)',
    '3F': 'Reserved for 3GPP (for future usage)',
}

# The terminal-problem additional information (TS 102 223 v18.3.0 §8.12.2).
TR_RESULT_ADD_INFO_NAMES = {
    '00': 'No specific cause can be given',
    '01': 'Screen is busy',
    '02': 'Terminal currently busy on call',
    '03': 'Reserved for GSM/3G',
    '04': 'No service',
    '05': 'Access control class bar',
    '06': 'Radio resource not granted',
    '07': 'Not in speech call',
    '08': 'Reserved for GSM/3G',
    '09': 'Terminal currently busy on SEND DTMF command',
    '0A': 'No NAA active',
}

# Access technology codings (TS 102 223 v18.3.0 §8.61) - the PROVIDE LOCAL
# INFORMATION answers 06 (single) and 0E (multiple).
ACCESSTECH_NAMES = {
    0: 'GSM', 1: 'GSM Compact', 2: 'TIA/EIA-533', 3: 'UTRAN',
    4: 'TETRA', 5: 'TIA/EIA-95-B', 6: 'CDMA2000 1x', 7: 'CDMA2000 HRPD',
    8: 'E-UTRAN', 9: 'eHRPD', 10: 'NG-RAN', 11: 'Satellite NG-RAN',
    12: 'Satellite E-UTRAN',
}

# The Command Details, Device Identities and Result CTLVs - the TERMINAL
# RESPONSE boilerplate, stripped when only the command-specific objects are of
# interest (the proactive log's decode).
_BOILERPLATE_TAGS = frozenset([0x01, 0x81, 0x02, 0x82, 0x03, 0x83])


def tr_result_name(result):
    """The name of a result octet (§8.12.0), or ``None`` for a reserved value."""
    return TR_RESULT_NAMES.get('%02X' % (result & 0xFF))


def tr_result_add_info_name(result, info):
    """The name of the general result's additional information.

    §8.12.2 defines the coding for the terminal problem (result ``20``); for
    the network problem (``21``) the coding is NAA-specific and only ``00`` is
    defined; elsewhere it is not defined (``None``).
    """
    if result == 0x20:
        return TR_RESULT_ADD_INFO_NAMES.get('%02X' % (info & 0xFF))
    if result == 0x21 and (info & 0xFF) == 0x00:
        return TR_RESULT_ADD_INFO_NAMES.get('00')
    return None


def _ctlv_at(raw, off):
    """``(tag, value, next_off)`` of the CTLV at ``raw[off]``, or ``None``.

    ``None`` for a truncated object (no tag/length octet, an incomplete BER
    long form, or a value running past the end) - the caller stops the walk
    instead of slicing garbage.  TS 101 220 Table 7.6: the COMPREHENSION-TLV
    length is short form below 128, else ``81 nn`` / ``82 nnnn`` / ...
    """
    if off >= len(raw) or off + 1 >= len(raw):
        return None
    tag = raw[off]
    first = raw[off + 1]
    if first < 0x80:
        length, voff = first, off + 2
    else:
        n = first & 0x7F
        if n < 1 or off + 2 + n > len(raw):
            return None
        length = int.from_bytes(raw[off + 2:off + 2 + n], 'big')
        voff = off + 2 + n
    end = voff + length
    if end > len(raw):
        return None
    return tag, raw[voff:end], end


def tr_data_only(tr_tlv):
    """Strip the boilerplate CTLVs (command details, device identities, Result).

    Returns the command-specific data objects of a TERMINAL RESPONSE (the
    proactive log stores this form: ``tr_hex``), or ``b''`` when there are
    none.  A truncated object ends the walk.  The lengths are BER (short or
    long form) - a text answer can exceed 127 octets (TS 101 220 Table 7.6).
    """
    tr_tlv = bytes(tr_tlv or b'')
    data = bytearray()
    off = 0
    while off < len(tr_tlv):
        parsed = _ctlv_at(tr_tlv, off)
        if parsed is None:
            break
        tag, _value, end = parsed
        if tag not in _BOILERPLATE_TAGS:
            data.extend(tr_tlv[off:end])
        off = end
    return bytes(data)


def extract_tr_result(tr_tlv):
    """``(tag, value)`` of the Result CTLV (``03`` basic / ``83`` general).

    ``None`` when the TERMINAL RESPONSE carries no Result object.  The walk
    uses BER lengths, so an object with a long-form length is skipped
    correctly.
    """
    tr_tlv = bytes(tr_tlv or b'')
    off = 0
    while off < len(tr_tlv):
        parsed = _ctlv_at(tr_tlv, off)
        if parsed is None:
            break
        tag, value, end = parsed
        if tag in (0x03, 0x83):
            return (tag, value)
        off = end
    return None


def _answer_tlvs(value):
    """Serialise the terminal's own answer objects.

    ``value`` is ``None``/``b''``, a hex string (the existing runners pass the
    PLI data that way), raw TLV bytes, or a list of those and/or
    ``(tag, value)`` pairs which are encoded as COMPREHENSION-TLVs.
    """
    if not value:
        return b''
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if isinstance(value, str):
        return bytes.fromhex(value)
    out = b''
    for item in value:
        if isinstance(item, (bytes, bytearray)):
            out += bytes(item)
        elif isinstance(item, str):
            out += bytes.fromhex(item)
        else:
            tag, item_value = item
            if isinstance(item_value, int):
                item_value = bytes([item_value & 0xFF])
            elif isinstance(item_value, str):
                item_value = bytes.fromhex(item_value)
            out += tlv.ComprTlv(tag=tag, value=bytes(item_value)).to_tlv()
    return out


def build_tr(cmd_num, cmd_type, dev_src, dev_dst, cmd_qual, cd_tag, respond,
             answer_tlvs=None, general_result=True):
    """Build the TERMINAL RESPONSE TLV payload for a fetched command.

    ``dev_src``/``dev_dst`` are the fetched command's device identities; the
    TR swaps them (TS 102 223 §6.8/§8.7).

    The object order follows TS 102 223 §6.8.0: command details, device
    identities, Result, then the command-specific objects (``answer_tlvs`` -
    the PLI data, the echoed Duration, ...), the Text string, the Item
    identifier and finally ``respond['raw']``.  The command details echo the
    command's tag form (``cd_tag``: ``01`` or ``81``) and qualifier per §6.8.1;
    the response objects use the base (CR=0) forms per §6.8.0.

    ``respond`` is the test runner's dict: ``result`` (int, default ``00``),
    ``additional_info`` (int, default ``00``), ``text``/``dcs`` (§8.15 codings:
    ``00`` packed GSM 7-bit, ``04`` 8-bit, ``08`` UCS2; an empty text is the
    null text string), ``item_id`` (only for a successful result) and ``raw``
    (extra TLVs, hex, appended last).  Every emitted length uses the BER coding
    (TS 101 220 §7.1.2 Table 7.6), so a Text string of 128 octets or more gets
    the ``81 nn`` form.

    ``general_result`` selects the Result object: the general form
    ``83 02 <result> <additional_info>`` (default - the scripted-TR shape) or
    the basic form ``03 01 <result>`` the terminal's own chain answers use;
    the basic form cannot carry additional information.

    ``answer_tlvs`` is the terminal's own answer object list: raw TLV bytes, a
    hex string, or ``(tag, value)`` pairs (see :func:`_answer_tlvs`).
    """
    respond = respond or {}
    out = bytearray([(cd_tag or 0x81) & 0xFF, 0x03,
                     cmd_num & 0xFF, cmd_type & 0xFF, (cmd_qual or 0) & 0xFF])
    out += bytes([0x02, 0x02, dev_dst & 0xFF, dev_src & 0xFF])
    result = int(respond.get('result', 0))
    additional_info = int(respond.get('additional_info', 0) or 0)
    if general_result:
        out += bytes([0x83, 0x02, result & 0xFF, additional_info & 0xFF])
    elif additional_info:
        raise ValueError('additional_info needs the general Result form')
    else:
        out += bytes([0x03, 0x01, result & 0xFF])
    out += _answer_tlvs(answer_tlvs)
    if respond.get('text') is not None:
        text = str(respond['text'])
        if text == '':
            # TS 102 223 §8.15: a null text string is Length 00, no value part.
            out += bytes([0x0D, 0x00])
        else:
            dcs = int(respond.get('dcs', 0x00))
            body = encode_text(text, dcs)
            # The Text string's length covers the DCS octet + the body.
            out += (bytes([0x0D]) + tlv.encode_ber_len(len(body) + 1)
                    + bytes([dcs]) + body)
    if respond.get('item_id') is not None and result == 0x00:
        out += bytes([0x10, 0x01, respond['item_id'] & 0xFF])
    if respond.get('raw'):
        out += bytes.fromhex(''.join(str(respond['raw']).split()))
    return bytes(out)


# ---- TERMINAL RESPONSE decoding -------------------------------------------

def _dec_plmn(value):
    """``(mcc, mnc)`` of a 3-octet nibble-swapped PLMN (TS 24.008 §10.5.1.3).

    A shorter value is not a PLMN - it raises instead of fabricating one (the
    decode paths guard the length, so this is unreachable today).
    """
    if len(value) < 3:
        raise ValueError('a PLMN is 3 octets (%d given)' % len(value))
    b1, b2, b3 = value[0], value[1], value[2]
    mcc = '%d%d%d' % (b1 & 0xF, (b1 >> 4) & 0xF, b2 & 0xF)
    mnc = '%d' % (b3 & 0xF)
    if (b2 >> 4) != 0xF:
        mnc += '%d' % ((b2 >> 4) & 0xF)
    return mcc, mnc


def _dec_imei(value):
    """Decode an IMEI/IMEISV coded as the TS 24.008 Mobile Identity value part.

    §10.5.1.4: the first byte's high nibble is identity digit 1 (odd/even +
    type 010/011 in the low nibble), the remaining digits are nibble-swapped
    BCD, stopping at the end-mark nibble ``F``.
    """
    if len(value) < 2:
        return ''
    digits = str((value[0] >> 4) & 0xF)
    for octet in value[1:]:
        low = octet & 0xF
        if low == 0xF:
            break
        digits += str(low)
        high = (octet >> 4) & 0xF
        if high == 0xF:
            break
        digits += str(high)
    return digits


def decode_tr(cmd_type, qualifier, tr_data):
    """Decode the command-specific objects of a TERMINAL RESPONSE.

    Returns ``[{label, value}]`` rows (empty when the response carried no
    data).  ``tr_data`` may be the complete TERMINAL RESPONSE payload or only
    its data part (the boilerplate is stripped either way) - a superset of the
    legacy ``_decode_tr``, which takes only the data part; the fallback row
    prints the stripped bytes, and a data part too short for one object has no
    rows at all.  ``cmd_type`` and ``qualifier`` are the command's, as
    integers.
    """
    tr_data = bytes(tr_data or b'')
    if not tr_data:
        return []
    data = tr_data_only(tr_data)
    if not data:
        return []
    if cmd_type == 0x03 and len(data) >= 4 and data[0] in (0x84, 0x04):
        # POLL INTERVAL: the Duration object's unit + interval (§8.8/§6.8.4).
        return [{'label': 'Interval', 'value': '%d s' % data[3]}]
    if cmd_type == 0x27:
        tlvs = tlv.tlv_map(data, multi_byte_tags=False)
        out = []
        timer_id = _get_tlv(tlvs, 0x24)
        if timer_id:
            out.append({'label': 'Timer', 'value': str(timer_id[0])})
        value = _get_tlv(tlvs, 0x25)
        if len(value) >= 3:
            out.append({'label': 'Remaining', 'value': '%02d:%02d:%02d' % (
                bcd_swap(value[0]), bcd_swap(value[1]), bcd_swap(value[2]))})
        return out or [{'label': 'Data', 'value': data.hex()}]
    if cmd_type == 0x26 and qualifier is not None:
        return _decode_pli(qualifier, data) or [{'label': 'Data', 'value': data.hex()}]
    return [{'label': 'Data', 'value': data.hex()}]


def _get_tlv(tlvs, tag):
    """Fetch a response TLV, tolerating both tag styles (TS 101 220 §7.1.1)."""
    return tlvs.get(tag) or tlvs.get(tag ^ 0x80) or b''


def _decode_pli(qualifier, data):
    """Decode a PROVIDE LOCAL INFORMATION answer.

    The answer objects are the individual data objects: location information
    §8.19, IMEI §8.20, date-time and time zone §8.39, language §8.45, access
    technology §8.61, IMEISV §8.74, network search mode §8.75, battery state
    §8.76 (TS 102 223 v18.3.0).
    """
    # The data object is tag + length + value, so its value starts at octet 2.
    value = data[2:]
    if qualifier == 0x00 and len(value) >= 5:
        mcc, mnc = _dec_plmn(value[:3])
        return [{'label': 'MCC', 'value': mcc}, {'label': 'MNC', 'value': mnc},
                {'label': 'LAC/TAC', 'value': value[3:5].hex().upper()}]
    if qualifier == 0x01 and len(value) >= 8:
        # The value part is the TS 24.008 Mobile Identity (10.5.1.4): the
        # leading parity/type octet is part of it, not a prefix.
        return [{'label': 'IMEI', 'value': _dec_imei(value)}]
    if qualifier == 0x03 and len(value) >= 7:
        # Date-Time and Time zone (TS 102 223 §8.39): TP-SCTS coding
        # (TS 123 040 §9.2.3.11) - swapped semi-octet BCD digits, the time
        # zone sign in bit 0x08 of the last octet, 'FF' unknown.
        year = bcd_swap(value[0])
        full_year = 2000 + year if year < 70 else 1900 + year
        out = [{'label': 'Date', 'value': '%04d-%02d-%02d' % (
                    full_year, bcd_swap(value[1]), bcd_swap(value[2]))},
               {'label': 'Time', 'value': '%02d:%02d:%02d' % (
                    bcd_swap(value[3]), bcd_swap(value[4]), bcd_swap(value[5]))}]
        tz = value[6]
        if tz == 0xFF:
            out.append({'label': 'TZ offset', 'value': 'unknown'})
        else:
            quarters = (tz & 0x07) * 10 + (tz >> 4)
            out.append({'label': 'TZ offset',
                        'value': '%s%02d:%02d' % ('-' if tz & 0x08 else '+',
                                                  quarters // 4, (quarters % 4) * 15)})
        return out
    if qualifier == 0x04 and len(value) >= 2:
        try:
            language = value[:2].decode('ascii')
        except Exception:
            language = value[:2].hex()
        return [{'label': 'Language', 'value': language}]
    if qualifier == 0x05 and len(value) >= 2:
        return [{'label': 'ME Status', 'value': str(value[0])},
                {'label': 'Timing Advance', 'value': str(value[1])}]
    if qualifier == 0x06 and len(value) >= 1:
        tech = value[0]
        return [{'label': 'Access Technology',
                 'value': '%s (%d)' % (ACCESSTECH_NAMES.get(tech, 'Unknown'), tech)}]
    if qualifier == 0x08 and len(value) >= 9:
        # The Mobile Identity form carries the SVN (16 digits).
        return [{'label': 'IMEISV', 'value': _dec_imei(value)}]
    if qualifier == 0x09 and len(value) >= 1:
        mode = value[0]
        return [{'label': 'Search Mode',
                 'value': 'Manual' if mode == 1 else ('Automatic' if mode == 0 else str(mode))}]
    if qualifier == 0x0A and len(value) >= 1:
        return [{'label': 'Charge state (%)', 'value': str(value[0])}]
    if qualifier == 0x0E:
        techs = list(value)
        return [{'label': 'Access Technologies',
                 'value': ', '.join(ACCESSTECH_NAMES.get(t, 'Unknown') for t in techs)}]
    return []
