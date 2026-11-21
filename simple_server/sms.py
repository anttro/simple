# coding=utf-8
"""SMS TPDUs and their user data (TS 23.040).

The SCP80 transport rides in SMS-PP downloads (TS 31.115 §4.2): the secured
packet is the user data of an SMS-DELIVER TPDU that is handed to the card in
an ENVELOPE.  This module owns the TPDU coding:

* :class:`UserDataHeader` - the TS 23.040 §9.2.3.24 user-data header (UDHL
  octet plus information elements), with the concatenation IE (§9.2.3.24.1,
  the 8-bit ``00`` and 16-bit ``08`` forms) and the RPI IE ``71`` the SCP80
  response packet starts with (TS 31.115 §4.2);
* :class:`SmsSubmit` - an SMS-SUBMIT TPDU (§9.2.2.2): the direction a card
  application uses in SEND SHORT MESSAGE, which the proactive log and the
  test-script ``sms`` check parse;
* :class:`SmsDeliver` - an SMS-DELIVER TPDU (§9.2.2.1): the direction of the
  SMS-PP download, so :func:`build_sms_tpdu` (the SCP80 download builder) can
  be parsed back;
* :func:`encode_oa`/:func:`encode_tp_address`, :func:`encode_scts`/
  :func:`decode_scts` - the TP address and TP-SCTS codings;
* :func:`find_sms_tpdu` - the SMS TPDU child (``8B``/``0B``) of a FETCH
  response;
* :func:`parse_sms_concat` - the concatenation IE and payload of a TPDU,
  used to reassemble a short message split over several SMs.

The byte coding is the port of the existing server's implementations
(``_encode_sms_oa``, ``_encode_scts``, ``_build_sms_tpdu``, ``_find_sms_tpdu``,
``_calc_ud_offset``, ``_parse_sms_concat``, ``_parse_udh`` and
``_decode_sms_ud``), so both produce identical bytes; the text codecs come
from :mod:`simple_server.text`.  Everything here is pure and card-free
testable.
"""

import codecs
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import gsm0338  # noqa: F401  registers the 'gsm03.38' codec

from simple_server import text as text_codec
from simple_server import tlv

__all__ = [
    'CONCAT_REF', 'SMS_USER_DATA', 'TON', 'NPI', 'VPF_FORMATS', 'SmsError',
    'InformationElement',
    'UserDataHeader', 'SmsSubmit', 'SmsDeliver', 'encode_oa',
    'encode_tp_address', 'decode_tp_address', 'decode_tp_address_value',
    'encode_scts', 'decode_scts',
    'decode_ud_text', 'build_sms_tpdu', 'find_sms_tpdu', 'ud_offset',
    'parse_sms_concat',
]

#: The fixed UDH concatenation reference the SCP80 splitter tags its segments
#: with (the existing server's ``SCP80_CONCAT_REF``).
CONCAT_REF = 0x01

#: The SMS user-data capacity of one short message (TS 23.040 §9.2.3.16: the
#: TP-UDL of an 8-bit coded SM counts octets; the SMS user data is 140 octets).
SMS_USER_DATA = 140

# TS 24.008 §10.5.4.7 type-of-number / numbering-plan values, as the address
# and TP-DA type octets carry them.
TON = {0: 'unknown', 1: 'international', 2: 'national',
       3: 'network_specific', 4: 'short_code', 5: 'alphanumeric',
       6: 'abbreviated'}
NPI = {0: 'unknown', 1: 'isdn_e164', 3: 'data_x121', 4: 'telex_f69',
       5: 'sc_specific_5', 6: 'sc_specific_6', 8: 'national', 9: 'private',
       10: 'ermes', 11: 'reserved_cts', 15: 'reserved_for_extension'}
_TON_CODES = {name: code for code, name in TON.items()}
_NPI_CODES = {name: code for code, name in NPI.items()}

# TS 23.040 §9.2.2.2 TP-VPF (bits b5 b4 of an SMS-SUBMIT's first octet) and
# the TP-VP field length of each format - the sizes ``_calc_ud_offset`` uses
# to find the TP-UD.
VPF_FORMATS = {0: 'none', 1: 'enhanced', 2: 'relative', 3: 'absolute'}
VPF_SIZES = {'none': 0, 'enhanced': 7, 'relative': 1, 'absolute': 7}

#: TS 23.040 §9.2.2.3 TP-MTI values.
MTI_DELIVER = 0
MTI_SUBMIT = 1
MTI_COMMAND = 2


class SmsError(ValueError):
    """A malformed or unsupported SMS TPDU / user-data header."""


# ---- user data header ------------------------------------------------------

@dataclass
class InformationElement(object):
    """One UDH information element (TS 23.040 §9.2.3.24)."""

    iei: int
    value: bytes = b''

    @property
    def length(self):
        return len(self.value)

    def to_bytes(self):
        return bytes([self.iei, len(self.value)]) + bytes(self.value)

    def __repr__(self):
        return 'InformationElement(iei=0x%02X, value=%s)' % (self.iei,
                                                             bytes(self.value).hex())


@dataclass
class UserDataHeader(object):
    """An SMS user-data header: the UDHL octet plus the information elements.

    :meth:`from_bytes` takes the whole TP-UD (the UDHL octet first) and
    returns the header with the remaining user data; :meth:`to_bytes` rebuilds
    the UDHL octet and every IE.
    """

    ies: list = field(default_factory=list)

    @classmethod
    def from_bytes(cls, data):
        """Parse ``data`` (UDHL octet first); returns ``(header, remainder)``.

        Raises :class:`SmsError` when the UDHL octet announces more header
        octets than the data carries.
        """
        data = bytes(data)
        if not data:
            raise SmsError('an empty user-data header')
        udhl = data[0]
        if 1 + udhl > len(data):
            raise SmsError('UDHL %d exceeds the %d available octet(s)'
                           % (udhl, len(data) - 1))
        header, off, end = [], 1, 1 + udhl
        while off + 2 <= end:
            iei, length = data[off], data[off + 1]
            if off + 2 + length > end:
                # A truncated trailing IE: keep the well-formed ones, exactly
                # like the display parser (_parse_udh) - the header length is
                # still authoritative for the reminder's offset.
                break
            header.append(InformationElement(iei, data[off + 2:off + 2 + length]))
            off += 2 + length
        return cls(header), data[end:]

    @classmethod
    def concat(cls, ref, total, sequence, sixteen_bit=False):
        """The concatenation IE as a header (TS 23.040 §9.2.3.24.1)."""
        if sixteen_bit:
            return cls([InformationElement(0x08, bytes([
                (int(ref) >> 8) & 0xFF, int(ref) & 0xFF,
                int(total) & 0xFF, int(sequence) & 0xFF]))])
        return cls([InformationElement(0x00, bytes([
            int(ref) & 0xFF, int(total) & 0xFF, int(sequence) & 0xFF]))])

    def to_bytes(self):
        """The UDHL octet followed by every IE."""
        body = b''.join(ie.to_bytes() for ie in self.ies)
        if len(body) > 0xFF:
            raise SmsError('the user-data header is %d octets (max 255)'
                           % len(body))
        return bytes([len(body)]) + body

    @property
    def octets(self):
        """The re-encoded length in octets (the UDHL octet plus the IEs)."""
        return len(self.to_bytes())

    @property
    def septets(self):
        """The header's length in 7-bit packed septets (UDH + fill bits)."""
        return (self.octets * 8 + 6) // 7

    def has_ie(self, iei):
        """Whether an IE with the identifier ``iei`` is present."""
        return any(ie.iei == iei for ie in self.ies)

    def get_ie(self, iei):
        """The first IE with identifier ``iei`` as bytes, or ``None``."""
        for ie in self.ies:
            if ie.iei == iei:
                return bytes(ie.value)
        return None

    def concat_info(self):
        """``(ref, total, sequence)`` of the concatenation IE, or ``None``.

        Both UDH forms are decoded: IEI ``00`` (3 octets, 8-bit reference) and
        IEI ``08`` (4 octets, 16-bit reference).
        """
        for ie in self.ies:
            value = bytes(ie.value)
            if ie.iei == 0x00 and len(value) == 3:
                return value[0], value[1], value[2]
            if ie.iei == 0x08 and len(value) == 4:
                return int.from_bytes(value[0:2], 'big'), value[2], value[3]
        return None

    def __repr__(self):
        return 'UserDataHeader(%r)' % self.ies


# ---- address and timestamp codings -----------------------------------------

def _bcd_octets(digits):
    """Nibble-swapped BCD octets of ``digits``; an odd count pads with ``F``."""
    digits = digits.upper()
    if len(digits) % 2:
        digits += 'F'
    return bytes(int(digits[i + 1], 16) << 4 | int(digits[i], 16)
                 for i in range(0, len(digits), 2))


def _bcd_digits(octets, count):
    """The first ``count`` nibble-swapped BCD digits of ``octets``."""
    out = []
    for octet in octets:
        out.append('%X' % (octet & 0x0F))
        out.append('%X' % ((octet >> 4) & 0x0F))
    return ''.join(out[:count])


def encode_tp_address(number, ton='unknown', npi='isdn_e164'):
    """A TP address field: length octet, type octet, BCD digits.

    TS 24.008 §10.5.4.7: the length counts the *digits* (not octets), the
    digits are BCD with the low nibble first and an odd count is padded with
    ``F``; the type octet carries the TON (bits b7-b5) and the NPI (b4-b1)
    with b8 set (the extension bit).  Only the ASCII hex digits of ``number``
    are coded - a leading ``+`` or a separator is dropped, like the existing
    server's ``_encode_sms_oa``.

    The defaults (``unknown``/``isdn_e164`` = type octet ``81``) reproduce the
    byte the existing server writes for its SMS-PP download address; a real
    international number uses ``ton='international'`` (type octet ``91``).
    """
    digits = ''.join(c for c in str(number) if c in '0123456789abcdefABCDEF')
    if not digits:
        raise SmsError('an address needs at least one digit: %r' % (number,))
    if ton not in _TON_CODES:
        raise SmsError('unknown type of number %r (one of %s)'
                       % (ton, ', '.join(sorted(_TON_CODES))))
    if npi not in _NPI_CODES:
        raise SmsError('unknown numbering plan %r (one of %s)'
                       % (npi, ', '.join(sorted(_NPI_CODES))))
    type_octet = 0x80 | (_TON_CODES[ton] << 4) | _NPI_CODES[npi]
    return bytes([len(digits), type_octet]) + _bcd_octets(digits)


def encode_oa(number):
    """The originator address of the SMS-PP download (TS 31.115 §4.2).

    Type octet ``81`` (unknown type of number, ISDN numbering plan) - the
    port of the existing server's ``_encode_sms_oa``.
    """
    return encode_tp_address(number, 'unknown', 'isdn_e164')


def decode_tp_address(data):
    """Parse a TP address field: ``(digits, ton, npi, consumed_octets)``.

    The digits are uppercase hex without the odd-count ``F`` pad.  A truncated
    field raises :class:`SmsError`.
    """
    data = bytes(data)
    if len(data) < 2:
        raise SmsError('a TP address needs at least 2 octets')
    digits_count = data[0]
    octets = (digits_count + 1) // 2
    if 2 + octets > len(data):
        raise SmsError('TP address of %d digits needs %d octets, %d available'
                       % (digits_count, 2 + octets, len(data)))
    digits, ton, npi = decode_tp_address_value(data[1:2 + octets])
    return digits, ton, npi, 2 + octets


def decode_tp_address_value(value):
    """Parse a type octet + BCD digits form: ``(digits, ton, npi)``.

    This is the form a COMPREHENSION-TLV ``Address`` object carries (TS 102 223
    §8.11 has no digit-count octet), so the digit count is the octet count
    times two with a trailing ``F`` pad dropped.  A value shorter than two
    octets raises :class:`SmsError`.
    """
    value = bytes(value)
    if len(value) < 2:
        raise SmsError('an address value is a type octet plus BCD digits '
                       '(%d octet(s) given)' % len(value))
    type_octet = value[0]
    digits = _bcd_digits(value[1:], (len(value) - 1) * 2)
    if digits.endswith('F'):
        digits = digits[:-1]
    ton = TON.get((type_octet >> 4) & 0x07, (type_octet >> 4) & 0x07)
    npi = NPI.get(type_octet & 0x0F, type_octet & 0x0F)
    return digits, ton, npi


def _bcd_pair(value):
    """One BCD octet: units in the high nibble, tens in the low nibble."""
    return (((value % 10) << 4) | ((value // 10) % 10)) & 0xFF


def _bcd_value(octet):
    """The decimal value of :func:`_bcd_pair`'s octet (low nibble = tens)."""
    return (octet & 0x0F) * 10 + ((octet >> 4) & 0x0F)


def encode_scts(dt=None):
    """A TP-SCTS timestamp (TS 23.040 §9.2.3.11): 7 semi-octet BCD octets.

    Year (two digits), month, day, hour, minute, second, then the timezone in
    quarter-hours with the sign in bit 3 of its low nibble (``1`` = negative).
    ``dt`` defaults to the current local time with its UTC offset (the port of
    the existing server's ``_encode_scts``).
    """
    dt = dt or datetime.now().astimezone()
    offset = dt.utcoffset() or timedelta()
    quarters = int(offset.total_seconds() // 900)
    tz = _bcd_pair(abs(quarters)) | (0x08 if quarters < 0 else 0x00)
    return bytes([
        _bcd_pair(dt.year % 100),
        _bcd_pair(dt.month),
        _bcd_pair(dt.day),
        _bcd_pair(dt.hour),
        _bcd_pair(dt.minute),
        _bcd_pair(dt.second),
        tz,
    ])


def decode_scts(data):
    """Parse a 7-octet TP-SCTS timestamp into a timezone-aware datetime.

    The inverse of :func:`encode_scts`.  The field carries the year's last two
    digits only, so the century is assumed to be 20xx (the encoder accepts any
    datetime); values 90-99 are therefore ambiguous by construction.
    """
    data = bytes(data)
    if len(data) < 7:
        raise SmsError('a TP-SCTS is 7 octets (%d given)' % len(data))
    year = 2000 + _bcd_value(data[0])
    month = _bcd_value(data[1])
    day = _bcd_value(data[2])
    hour = _bcd_value(data[3])
    minute = _bcd_value(data[4])
    second = _bcd_value(data[5])
    quarters = _bcd_value(data[6] & 0xF7)
    offset = timedelta(minutes=15 * quarters)
    if data[6] & 0x08:
        offset = -offset
    try:
        return datetime(year, month, day, hour, minute, second,
                        tzinfo=timezone(offset))
    except ValueError as exc:
        raise SmsError('invalid TP-SCTS %s: %s' % (data.hex(), exc)) from exc


# ---- user-data text --------------------------------------------------------

def _unpack_septets(data, count):
    """Unpack ``count`` 7-bit septets from packed GSM default alphabet data.

    TP-UDL counts septets, so the count decides where the text ends - the port
    of the existing server's ``_unpack_septets``.
    """
    out = bytearray()
    acc = bits = 0
    for octet in data:
        acc |= octet << bits
        bits += 8
        while bits >= 7 and len(out) < count:
            out.append(acc & 0x7F)
            acc >>= 7
            bits -= 7
    return bytes(out)


def _udh_length(ud):
    """The UDH's length in octets (UDHL octet + IEs), 0 when there is none.

    Tolerant port of the existing server's ``_parse_udh`` length: a UDHL of 0
    or one that overruns the user data means "no header".
    """
    ud = bytes(ud)
    if len(ud) < 2:
        return 0
    udhl = ud[0]
    if udhl < 1 or 1 + udhl > len(ud):
        return 0
    return 1 + udhl


def decode_ud_text(pid, dcs, ud, udhi=False, udl=None):
    """Decode the TP-UD of an SMS TPDU (TS 23.038).

    A PID of ``7F`` is a SIM data download whose user data is a secured packet
    (TS 31.115), not text, so the result is ``''``.  Otherwise the DCS decides:
    class ``00`` is the packed GSM default alphabet (``udl`` septets, minus the
    septets the UDH occupies), ``08`` is UCS2 and ``04`` 8-bit - the latter
    two are delegated to :func:`simple_server.text.decode_dcs_text`.  A
    decode failure returns the payload's hex, never raises.
    """
    ud = bytes(ud)
    if not ud:
        return ''
    header_octets = _udh_length(ud) if udhi else 0
    header_septets = (header_octets * 8 + 6) // 7
    data = ud[header_octets:]
    if pid == 0x7F:
        return ''
    try:
        if (dcs & 0x0C) == 0x00:
            septets = udl if udl else (len(ud) * 8) // 7
            octets = (septets * 7 + 7) // 8
            packed = _unpack_septets(ud[:octets], septets)[header_septets:]
            return codecs.decode(packed, 'gsm03.38')
        if (dcs & 0x0C) in (0x04, 0x08):
            return text_codec.decode_dcs_text(bytes([dcs]) + data)
        return data.hex()
    except Exception:
        return data.hex()


# ---- TPDUs ----------------------------------------------------------------

def _as_tpdu(value, what='TPDU'):
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    try:
        return bytes.fromhex(str(value or '').strip())
    except ValueError:
        raise SmsError('%s is not valid hex: %r' % (what, value))


def _check_octet(value, what):
    if not 0 <= int(value) <= 0xFF:
        raise SmsError('%s must be one octet (0..255): %r' % (what, value))
    return int(value)


@dataclass
class SmsSubmit(object):
    """An SMS-SUBMIT TPDU (TS 23.040 §9.2.2.2).

    ``da`` is the destination address in digits (without the odd-count ``F``
    pad), ``pid``/``dcs``/``ud`` the protocol identifier, data coding scheme
    and the whole TP-UD (the UDH included when ``udhi``), ``vp`` the
    TP-VP bytes when the VPF is not ``none`` and ``udl`` the TP-UDL - for the
    7-bit coded alphabet that is the *septet* count, so a caller building a
    packed SM must set it (when left ``None`` the octet count of ``ud`` is
    written).  :attr:`udh` and :attr:`text` decode the user data on demand.
    """

    da: str = ''
    da_ton: str = 'unknown'
    da_npi: str = 'isdn_e164'
    mr: int = 0
    pid: int = 0
    dcs: int = 0
    ud: bytes = b''
    udhi: bool = False
    rd: bool = False
    rp: bool = False
    srr: bool = False
    vpf: str = 'none'
    vp: bytes = b''
    udl: int = None

    @classmethod
    def from_bytes(cls, tpdu):
        """Parse an SMS-SUBMIT TPDU from bytes or hex.

        Raises :class:`SmsError` for another TP-MTI or a truncated field.
        """
        data = _as_tpdu(tpdu)
        if len(data) < 2:
            raise SmsError('an SMS-SUBMIT needs at least 2 octets')
        flags = data[0]
        mti = flags & 0x03
        if mti != MTI_SUBMIT:
            raise SmsError('not an SMS-SUBMIT TPDU (TP-MTI %d)' % mti)
        vpf = VPF_FORMATS[(flags >> 3) & 0x03]
        da, ton, npi, off = decode_tp_address(data[2:])
        off += 2
        if off + 2 > len(data):
            raise SmsError('the SMS-SUBMIT ends inside the PID/DCS fields')
        pid, dcs = data[off], data[off + 1]
        off += 2
        vp = b''
        if VPF_SIZES[vpf]:
            size = VPF_SIZES[vpf]
            if off + size > len(data):
                raise SmsError('the SMS-SUBMIT ends inside the TP-VP field')
            vp = data[off:off + size]
            off += size
        if off >= len(data):
            raise SmsError('the SMS-SUBMIT ends inside the TP-UDL field')
        udl = data[off]
        off += 1
        return cls(da=da, da_ton=ton, da_npi=npi, mr=data[1], pid=pid, dcs=dcs,
                   ud=data[off:], udhi=bool(flags & 0x40),
                   rd=bool(flags & 0x04), rp=bool(flags & 0x80),
                   srr=bool(flags & 0x20), vpf=vpf, vp=vp, udl=udl)

    def to_bytes(self):
        """Encode the TPDU (the inverse of :meth:`from_bytes`)."""
        if self.vpf not in VPF_SIZES:
            raise SmsError('unknown TP-VPF %r' % (self.vpf,))
        flags = MTI_SUBMIT
        if self.rd:
            flags |= 0x04
        if self.srr:
            flags |= 0x20
        if self.udhi:
            flags |= 0x40
        if self.rp:
            flags |= 0x80
        flags |= [code for code, name in VPF_FORMATS.items()
                  if name == self.vpf][0] << 3
        vp = _as_tpdu(self.vp, 'TP-VP')
        if len(vp) != VPF_SIZES[self.vpf]:
            raise SmsError('TP-VPF %s needs %d TP-VP octets (%d given)'
                           % (self.vpf, VPF_SIZES[self.vpf], len(vp)))
        return (bytes([flags, _check_octet(self.mr, 'mr')])
                + encode_tp_address(self.da, self.da_ton, self.da_npi)
                + bytes([_check_octet(self.pid, 'pid'),
                         _check_octet(self.dcs, 'dcs')]) + vp
                + bytes([_check_octet(self.udl if self.udl is not None
                                      else len(self.ud), 'udl')])
                + bytes(self.ud))

    @property
    def udh(self):
        """The :class:`UserDataHeader` of the user data, or ``None``."""
        if not self.udhi:
            return None
        try:
            return UserDataHeader.from_bytes(self.ud)[0]
        except SmsError:
            return None

    @property
    def text(self):
        """The decoded TP-UD text (``''`` for a PID 7F secured packet)."""
        return decode_ud_text(self.pid, self.dcs, self.ud, self.udhi, self.udl)

    @property
    def secured_packet(self):
        """The secured packet in the user data (PID ``7F``), or ``None``."""
        if self.pid != 0x7F:
            return None
        ud = bytes(self.ud)
        return ud[_udh_length(ud):] if self.udhi else ud


@dataclass
class SmsDeliver(object):
    """An SMS-DELIVER TPDU (TS 23.040 §9.2.2.1).

    The SMS-PP download direction (TS 31.115 §4.2): ``oa`` the originating
    address in digits, ``scts`` the 7 TP-SCTS octets, ``ud`` the TP-UD (UDH
    included when ``udhi``).  :attr:`udh` and :attr:`text` decode the user
    data on demand.
    """

    oa: str = ''
    oa_ton: str = 'unknown'
    oa_npi: str = 'isdn_e164'
    pid: int = 0
    dcs: int = 0
    scts: bytes = b''
    ud: bytes = b''
    udhi: bool = False
    mms: bool = True
    lp: bool = False
    sri: bool = False
    rp: bool = False
    udl: int = None

    @classmethod
    def from_bytes(cls, tpdu):
        """Parse an SMS-DELIVER TPDU from bytes or hex."""
        data = _as_tpdu(tpdu)
        if len(data) < 2:
            raise SmsError('an SMS-DELIVER needs at least 2 octets')
        flags = data[0]
        mti = flags & 0x03
        if mti != MTI_DELIVER:
            raise SmsError('not an SMS-DELIVER TPDU (TP-MTI %d)' % mti)
        oa, ton, npi, off = decode_tp_address(data[1:])
        off += 1
        if off + 2 + 7 + 1 > len(data):
            raise SmsError('the SMS-DELIVER ends before its TP-UDL')
        pid, dcs = data[off], data[off + 1]
        off += 2
        scts = data[off:off + 7]
        off += 7
        udl = data[off]
        off += 1
        return cls(oa=oa, oa_ton=ton, oa_npi=npi, pid=pid, dcs=dcs, scts=scts,
                   ud=data[off:], udhi=bool(flags & 0x40),
                   mms=bool(flags & 0x04), lp=bool(flags & 0x10),
                   sri=bool(flags & 0x20), rp=bool(flags & 0x80), udl=udl)

    def to_bytes(self):
        """Encode the TPDU (the inverse of :meth:`from_bytes`)."""
        scts = _as_tpdu(self.scts, 'TP-SCTS')
        if len(scts) != 7:
            raise SmsError('a TP-SCTS is 7 octets (%d given)' % len(scts))
        flags = MTI_DELIVER
        if self.mms:
            flags |= 0x04
        if self.lp:
            flags |= 0x10
        if self.sri:
            flags |= 0x20
        if self.udhi:
            flags |= 0x40
        if self.rp:
            flags |= 0x80
        return (bytes([flags])
                + encode_tp_address(self.oa, self.oa_ton, self.oa_npi)
                + bytes([_check_octet(self.pid, 'pid'),
                         _check_octet(self.dcs, 'dcs')]) + scts
                + bytes([_check_octet(self.udl if self.udl is not None
                                      else len(self.ud), 'udl')])
                + bytes(self.ud))

    @property
    def udh(self):
        """The :class:`UserDataHeader` of the user data, or ``None``."""
        if not self.udhi:
            return None
        try:
            return UserDataHeader.from_bytes(self.ud)[0]
        except SmsError:
            return None

    @property
    def text(self):
        """The decoded TP-UD text (``''`` for a PID 7F secured packet)."""
        return decode_ud_text(self.pid, self.dcs, self.ud, self.udhi, self.udl)

    @property
    def secured_packet(self):
        """The secured packet in the user data (PID ``7F``), or ``None``."""
        if self.pid != 0x7F:
            return None
        ud = bytes(self.ud)
        return ud[_udh_length(ud):] if self.udhi else ud

    @property
    def timestamp(self):
        """The decoded :func:`decode_scts` datetime of the TPDU."""
        return decode_scts(self.scts)


# ---- the SMS-PP download builder ------------------------------------------

def build_sms_tpdu(chunk, chunk_total=1, chunk_num=1, oa_number='12345',
                   include_cpi=True, chunk_ref=CONCAT_REF, scts=None):
    """The SMS-DELIVER TPDU of one SMS-PP download segment (bytes).

    TS 31.115 §4.2/§4.3: a concatenated command carries the concatenation IE
    in every SM and the OTA CPI IE (IEIa ``70``, IEIDLa ``00``) in the first
    one; a single-SM download may carry the CPI IE alone.  The TP-UD is
    ``UDHL + UDH + chunk`` and its length is the TP-UDL.  The PID is ``7F``
    (SIM data download) and the DCS ``F6``.

    The first octet is the existing server's coding: ``04`` without a UDH,
    ``40`` for a single SM with the CPI IE and ``44`` for a concatenated one
    (TP-MMS set when the data is not preceded by a concatenation UDH, TP-UDHI
    when a UDH is present - the port keeps those bytes).

    ``scts`` overrides the TP-SCTS octets (the 7-octet form, as bytes or a hex
    string; tests pin them); by default the current local time is used.
    """
    chunk = _as_tpdu(chunk, 'chunk')
    chunk_total = int(chunk_total)
    chunk_num = int(chunk_num)
    udh = b''
    if chunk_total > 1:
        udh = bytes([0x00, 0x03, int(chunk_ref) & 0xFF,
                     chunk_total & 0xFF, chunk_num & 0xFF])
        if chunk_num == 1 and include_cpi:
            udh += bytes([0x70, 0x00])
    elif include_cpi:
        udh = bytes([0x70, 0x00])
    tp_ud = (bytes([len(udh)]) + udh + chunk) if udh else chunk
    # TP-UDL counts the whole user data (the UDHL octet + UDH + payload) and
    # the SMS limit is 140 octets; the splitter sizes every part for its UDH.
    budget = SMS_USER_DATA - len(udh) - (1 if udh else 0)
    if len(chunk) > budget:
        raise SmsError('SMS user data overflow: %d > %d octets'
                       % (len(chunk), budget))
    if udh and chunk_total > 1:
        first_byte = 0x44
    elif udh:
        first_byte = 0x40
    else:
        first_byte = 0x04
    return SmsDeliver(
        oa=oa_number, pid=0x7F, dcs=0xF6,
        scts=_as_tpdu(scts, 'scts') if scts is not None else encode_scts(),
        ud=tp_ud, udhi=bool(udh),
        # The first octet is the existing server's coding: TP-MMS (b3) is set
        # whenever no concatenation UDH precedes the data, which yields 0x44
        # for a concatenated SM, 0x40 for a single SM with the CPI IE and 0x04
        # for one without a UDH (TP-UDHI, b7, follows the UDH).
        mms=(first_byte & 0x04) != 0).to_bytes()


# ---- FETCH response parsing -----------------------------------------------

def find_sms_tpdu(raw):
    """The SMS TPDU child (tag ``8B``/``0B``) of a FETCH response, as bytes.

    A FETCH response is a BER-TLV ``D0`` template; its command-specific
    objects use BER lengths, so a 150-octet TPDU is ``8B 81 96 ...`` and must
    not be read with a single-octet length (that truncates the TPDU and loses
    the data).  Returns ``None`` when the command carries no TPDU.

    The walk is not bounded by the ``D0`` length: a card whose template length
    is off by one still delivers a complete TPDU, and the existing server's
    parser walks to the end of the buffer for the same reason.  A truncated
    child TLV is skipped instead of walking into its value.
    """
    raw = bytes(raw)
    if not raw or raw[0] != 0xD0:
        return None
    off = tlv.parse_ber_len(raw, 1)[1]       # skip the D0 length field
    while off + 1 < len(raw):
        tag = raw[off]
        length, value_off = tlv.parse_ber_len(raw, off + 1)
        if tag in (0x8B, 0x0B) and length >= 1:
            return raw[value_off:value_off + length]
        next_off = value_off + length
        if next_off <= off:                  # no forward progress: stop
            break
        off = next_off
    return None


def ud_offset(tpdu):
    """The offset of TP-UD inside an SMS TPDU, or ``None``.

    Handles SMS-SUBMIT (TP-MTI 01) and SMS-DELIVER (00); the TP-VP length
    follows the submit's TP-VPF (``none`` 0, ``relative`` 1, ``enhanced`` and
    ``absolute`` 7 octets).  Port of the existing server's ``_calc_ud_offset``.
    """
    tpdu = bytes(tpdu)
    if not tpdu:
        return None
    first = tpdu[0]
    mti = first & 0x03
    vpf = (first >> 3) & 0x03
    if mti == MTI_SUBMIT:
        if len(tpdu) < 3:
            return None
        da_data_bytes = (tpdu[2] + 1) // 2
        off = 1 + 1 + 1 + 1 + da_data_bytes + 1 + 1
        off += VPF_SIZES[VPF_FORMATS[vpf]]
        if off >= len(tpdu):
            return None
        return off + 1    # skip the TP-UDL octet
    if mti == MTI_DELIVER:
        if len(tpdu) < 2:
            return None
        oa_data_bytes = (tpdu[1] + 1) // 2
        # flags + OA length + OA type + digits + PID + DCS + TP-SCTS.  The
        # existing server's `_calc_ud_offset` omits the OA type octet here, so
        # its TP-UD offset is one octet short for every SMS-DELIVER; the
        # submit branch below (same formula, type octet included) is the
        # reference for this one.
        off = 1 + 1 + 1 + oa_data_bytes + 1 + 1 + 7
        if off >= len(tpdu):
            return None
        return off + 1    # skip the TP-UDL octet
    return None


def parse_sms_concat(tpdu):
    """The concatenation IE and payload of an SMS TPDU.

    Returns ``(ref, total, sequence, payload)`` with the payload as bytes
    after the UDH; ``(None, None, None, payload)`` when the TPDU carries no
    concatenation IE, and ``(None, None, None, tpdu)`` when the TP-UD cannot
    be located.  Port of the existing server's ``_parse_sms_concat``.
    """
    tpdu = bytes(tpdu) if tpdu else b''
    if len(tpdu) < 2:
        return None, None, None, tpdu
    udhi = bool(tpdu[0] & 0x40)
    off = ud_offset(tpdu)
    if off is None or off >= len(tpdu):
        return None, None, None, tpdu
    if not udhi:
        return None, None, None, tpdu[off:]
    try:
        header, remainder = UserDataHeader.from_bytes(tpdu[off:])
    except SmsError:
        return None, None, None, tpdu[off:]
    concat = header.concat_info()
    if concat is None:
        return None, None, None, remainder
    return concat[0], concat[1], concat[2], remainder
