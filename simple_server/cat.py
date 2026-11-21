# coding=utf-8
"""ENVELOPE builders for SMS-PP download (TS 102 223 §7.1, TS 31.115 §4.2).

An SMS-PP download is delivered to the card as

    ENVELOPE (TS 102 221 §11.2.2): CLA C2 00 00 Lc <data>

whose data is an SMS-PP-Download BER-TLV (TS 101 220 Table 7.17, tag ``D1``)
holding the device identities (``82``), an optional address (``86``) and the
SMS TPDU (``8B``)::

    D1 <len> 82 02 83 81 [86 <len> <type><BCD digits>] 8B <len> <TPDU>

The nesting and the object tags follow TS 31.111 §7.1.1.2; the address object
is conditional (``_decode_send_sm`` reads it the same way for a card-issued
SEND SHORT MESSAGE).  The existing server builds the same envelope with
pySim's ``SMSPPDownload``/``DeviceIdentities``/``Address`` objects plus a raw
TPDU child; this module is the equivalent without pysim:

* :class:`DeviceIdentities` - the ``82`` object (TS 102 223 §8.7);
* :class:`Address` - the ``86`` object (TS 102 223 §8.11), a type octet plus
  BCD digits, reusing :mod:`simple_server.sms`'s TP address coding;
* :class:`SmsPpDownload` - the ``D1`` template and its children;
* :func:`envelope_apdu` - the ENVELOPE command APDU around the template.

Everything is pure; the TPDU itself comes from
:func:`simple_server.sms.build_sms_tpdu`.
"""

from dataclasses import dataclass

from simple_server import sms
from simple_server.tlv import ComprTlv, encode_ber_len

__all__ = [
    'DEVICE_IDS', 'ENVELOPE_C2', 'SMS_PP_DOWNLOAD_TAG', 'CatError',
    'DeviceIdentities', 'Address', 'SmsPpDownload', 'envelope_apdu',
]

# TS 102 223 §8.7 device identities
DEVICE_IDS = {
    0x01: 'keypad',
    0x02: 'display',
    0x03: 'earpiece',
    0x10: 'addl_card_reader_0', 0x11: 'addl_card_reader_1',
    0x12: 'addl_card_reader_2', 0x13: 'addl_card_reader_3',
    0x14: 'addl_card_reader_4', 0x15: 'addl_card_reader_5',
    0x16: 'addl_card_reader_6', 0x17: 'addl_card_reader_7',
    0x21: 'channel_1', 0x22: 'channel_2', 0x23: 'channel_3',
    0x24: 'channel_4', 0x25: 'channel_5', 0x26: 'channel_6',
    0x27: 'channel_7',
    0x31: 'ecat_client_1', 0x32: 'ecat_client_2', 0x33: 'ecat_client_3',
    0x34: 'ecat_client_4', 0x35: 'ecat_client_5', 0x36: 'ecat_client_6',
    0x37: 'ecat_client_7', 0x38: 'ecat_client_8', 0x39: 'ecat_client_9',
    0x3A: 'ecat_client_a', 0x3B: 'ecat_client_b', 0x3C: 'ecat_client_c',
    0x3D: 'ecat_client_d', 0x3E: 'ecat_client_e', 0x3F: 'ecat_client_f',
    0x81: 'uicc',
    0x82: 'terminal',
    0x83: 'network',
}
_DEVICE_ID_CODES = {name: code for code, name in DEVICE_IDS.items()}

#: The SMS-PP-Download BER-TLV tag (TS 101 220 Table 7.17).
SMS_PP_DOWNLOAD_TAG = 0xD1
#: The ENVELOPE instruction byte (TS 102 221 §11.2.2).
ENVELOPE_C2 = 0xC2

#: The maximum data field of a short APDU (one Lc octet).
_APDU_DATA_MAX = 0xFF


class CatError(ValueError):
    """A malformed or unbuildable CAT/ENVELOPE object."""


def _device_id(value, what):
    """A device identity octet from a name or an integer."""
    if isinstance(value, int) and not isinstance(value, bool):
        code = value
    else:
        name = str(value).strip().lower()
        if name not in _DEVICE_ID_CODES:
            raise CatError('unknown %s device identity %r (one of %s)'
                           % (what, value, ', '.join(sorted(_DEVICE_ID_CODES))))
        code = _DEVICE_ID_CODES[name]
    if code not in DEVICE_IDS:
        raise CatError('unknown %s device identity 0x%02X' % (what, code))
    return code


@dataclass
class DeviceIdentities(object):
    """The device-identities COMPREHENSION-TLV (TS 102 223 §8.7, tag ``82``).

    ``source``/``dest`` are device identity names (``network`` = ``83``,
    ``uicc`` = ``81``, ``terminal`` = ``82``) or their octets.
    """

    source: object = 'network'
    dest: object = 'uicc'

    def __post_init__(self):
        self.source_id = _device_id(self.source, 'source')
        self.dest_id = _device_id(self.dest, 'destination')

    @classmethod
    def from_bytes(cls, value):
        """Parse the object's 2-octet value (source, destination)."""
        value = bytes(value)
        if len(value) != 2:
            raise CatError('device identities are 2 octets (%d given)'
                           % len(value))
        return cls(value[0], value[1])

    @property
    def value(self):
        """The object's value: the source and destination identity octets."""
        return bytes([self.source_id, self.dest_id])

    def to_tlv(self):
        """``82 02 <source> <destination>``."""
        return ComprTlv(tag=0x82, value=self.value).to_tlv()

    def __repr__(self):
        return 'DeviceIdentities(%s -> %s)' % (
            DEVICE_IDS.get(self.source_id, '%02X' % self.source_id),
            DEVICE_IDS.get(self.dest_id, '%02X' % self.dest_id))


@dataclass
class Address(object):
    """The address COMPREHENSION-TLV (TS 102 223 §8.11, tag ``86``).

    The value is a type octet (TON/NPI) plus the BCD digits - the TPDU address
    form of :func:`simple_server.sms.encode_tp_address` without its digit-count
    octet, which the TLV does not carry.  The defaults give the type octet
    ``81`` (unknown TON, ISDN NPI), the byte the existing server's envelope
    carries.
    """

    number: str = ''
    ton: str = 'unknown'
    npi: str = 'isdn_e164'

    @classmethod
    def from_bytes(cls, value):
        """Parse the object's value: a type octet plus BCD digits."""
        digits, ton, npi = sms.decode_tp_address_value(value)
        return cls(digits, ton, npi)

    @property
    def value(self):
        """The type octet followed by the BCD digits."""
        return sms.encode_tp_address(self.number, self.ton, self.npi)[1:]

    def to_tlv(self):
        """``86 <len> <type octet> <BCD digits>``."""
        return ComprTlv(tag=0x86, value=self.value).to_tlv()


@dataclass
class SmsPpDownload(object):
    """The SMS-PP-Download ENVELOPE data (TS 31.111 §7.1.1.2, tag ``D1``).

    ``tpdu`` is the SMS-DELIVER TPDU (:func:`simple_server.sms.build_sms_tpdu`)
    and ``address`` the optional originating address object.
    """

    device_identities: DeviceIdentities = None
    tpdu: bytes = b''
    address: Address = None

    def to_bytes(self):
        """The children: device identities, the optional address, the TPDU.

        The device identities and the SMS TPDU are mandatory (TS 31.111
        §7.1.1.2: ``Min = Y`` for both), so a missing one raises
        :class:`CatError` instead of emitting an incomplete envelope.
        """
        if self.device_identities is None:
            raise CatError('an SMS-PP download needs the device identities')
        if not self.tpdu:
            raise CatError('an SMS-PP download needs an SMS TPDU')
        out = self.device_identities.to_tlv()
        if self.address is not None:
            out += self.address.to_tlv()
        out += ComprTlv(tag=0x8B, value=bytes(self.tpdu)).to_tlv()
        return out

    def to_tlv(self):
        """``D1 <len> <children>`` (a BER-TLV, so the length may be long-form)."""
        body = self.to_bytes()
        return bytes([SMS_PP_DOWNLOAD_TAG]) + encode_ber_len(len(body)) + body

    @classmethod
    def build(cls, device_identities, tpdu, address=None):
        """The complete ``D1 ...`` template as bytes.

        ``device_identities`` may be a :class:`DeviceIdentities` or a
        ``(source, destination)`` pair of names; ``address`` is optional (the
        object is conditional, TS 31.111 §7.1.1.2) and may be an
        :class:`Address` or a number string.
        """
        if not isinstance(device_identities, DeviceIdentities):
            source, dest = device_identities
            device_identities = DeviceIdentities(source, dest)
        if address is not None and not isinstance(address, Address):
            address = Address(str(address))
        return cls(device_identities, tpdu, address).to_tlv()


def envelope_apdu(data, cla=0x80):
    """The ENVELOPE command APDU carrying ``data`` (TS 102 221 §11.2.2).

    ``CLA C2 00 00 Lc <data>`` with the card-class CLA (``80`` for a UICC,
    ``A0`` for a 2G SIM session, TS 151 011) - the same form the existing
    server builds for its secured packets.
    """
    data = bytes(data)
    if isinstance(cla, str):
        try:
            cla = int(cla, 16)
        except ValueError:
            raise CatError('the CLA is not valid hex: %r' % (cla,))
    if not 0 <= int(cla) <= 0xFF:
        raise CatError('the CLA must be one octet: %r' % (cla,))
    if len(data) > _APDU_DATA_MAX:
        raise CatError('the ENVELOPE data is %d octets (max %d for one Lc)'
                       % (len(data), _APDU_DATA_MAX))
    return bytes([int(cla), ENVELOPE_C2, 0x00, 0x00, len(data)]) + data
