# coding=utf-8
"""File Control Parameters (FCP) parsing for a SELECT response.

The response data of SELECT is the FCP template (TS 102 221 §11.1.1.3.0):
tag ``62`` with the data objects of §11.1.1.3.1/.3.2 - the file descriptor
(``82``, Table 11.5), the file identifier (``83``, §11.1.1.4.4), the DF name
(``84``, §11.1.1.4.5) and the file size (``80``, §11.1.1.4.1).  ISO/IEC
7816-4 cards and some UICCs wrap the same objects in an FCI template (``6F``)
whose children are the FCP template ``62`` and/or the FMD ``64``; a card may
also put the objects directly under ``6F``.

The parser never raises: a missing or malformed response yields an
:class:`Fcp` whose fields are ``None`` (only ``raw`` keeps the input hex), so a
caller can decide whether a missing size/record length is fatal.  The module
is pure and card-free testable.
"""

from dataclasses import dataclass

from simple_server.tlv import ber_tlv_list

__all__ = ['Fcp', 'STRUCTURES', 'parse_fcp', 'parse_gsm_select',
           'fcp_from_select_response']

# TS 102 221 Table 11.5, structure bits b3-b1 (b6-b4 = 111 with b3-b1 = 001 is
# the BER-TLV row).  Anything else (RFU) reports 'no_info'.
STRUCTURES = ('no_info', 'transparent', 'linear_fixed', 'cyclic', 'ber_tlv')

_TAG_FILE_SIZE = 0x80
_TAG_FILE_DESCRIPTOR = 0x82
_TAG_FILE_IDENTIFIER = 0x83
_TAG_DF_NAME = 0x84
_TAG_PROPRIETARY_INFO = 0xA5
_TAG_FCP_TEMPLATE = 0x62
_TAG_FCI_TEMPLATE = 0x6F

_STRUCTURE_BY_BITS = {
    0x00: 'no_info',
    0x01: 'transparent',
    0x02: 'linear_fixed',
    0x06: 'cyclic',
}

_DO_TAGS = (_TAG_FILE_SIZE, _TAG_FILE_DESCRIPTOR, _TAG_FILE_IDENTIFIER,
            _TAG_DF_NAME)


@dataclass
class Fcp(object):
    """The file control parameters of one SELECT/STATUS response.

    ``shareable``/``structure`` come from the file descriptor byte (Table
    11.5); ``record_len``/``num_of_rec`` are only present for a linear fixed or
    cyclic file (TS 102 221 §11.1.1.4.3: bytes 5-6 and 7 of the DO).
    """

    file_size: int = None
    record_len: int = None
    num_of_rec: int = None
    df_name: str = None
    file_identifier: str = None
    shareable: bool = None
    structure: str = None
    raw: str = ''
    #: ``ber`` for a TS 102 221 FCP/FCI, ``gsm`` for the TS 51 011 §9.2.1
    #: SELECT response data of a 2G SIM (the GET RESPONSE payload).
    form: str = 'ber'


def _as_bytes(data):
    """Accept a hex string or a bytes-like object; returns bytes."""
    if isinstance(data, (bytes, bytearray, memoryview)):
        return bytes(data)
    if isinstance(data, str):
        return bytes.fromhex(data.strip())
    raise TypeError('FCP data must be bytes or a hex string')


def _data_objects(raw):
    """The FCP data objects of a response, whatever template wraps them."""
    items = ber_tlv_list(raw)
    if not items:
        return []
    tag, value = items[0]
    if tag == _TAG_FCP_TEMPLATE:
        return ber_tlv_list(value)
    if tag == _TAG_FCI_TEMPLATE:
        inner = ber_tlv_list(value)
        dos = []
        for inner_tag, inner_value in inner:
            if inner_tag == _TAG_FCP_TEMPLATE:
                dos.extend(ber_tlv_list(inner_value))
        if dos:
            # Objects a card adds next to the FCP template are kept after it.
            dos.extend([(t, v) for t, v in inner if t != _TAG_FCP_TEMPLATE])
            return dos
        return inner
    # No template at all: treat the response as a bare data object list.
    return items


def _structure(file_descriptor):
    """``(shareable, structure)`` from file-descriptor byte 0 (Table 11.5).

    b7 is the file accessibility (1 = shareable, mask 0x40), b6-b4 the file
    type (mask 0x38) and b3-b1 the EF structure (mask 0x07).  The file type
    values 000 (working EF) and 001 (internal EF) use the structure bits;
    b6-b4 = 111 is a DF/ADF, except the BER-TLV row (b3-b1 = 001).
    """
    if not file_descriptor:
        return None, None
    byte = file_descriptor[0]
    shareable = bool(byte & 0x40)
    file_type = (byte >> 3) & 0x07
    bits = byte & 0x07
    if file_type == 0x07:
        structure = 'ber_tlv' if bits == 0x01 else 'no_info'
    else:
        structure = _STRUCTURE_BY_BITS.get(bits, 'no_info')
    return shareable, structure


def parse_fcp(data):
    """Parse an FCP (``62``) or FCI (``6F``) response into an :class:`Fcp`.

    ``data`` is the full response data (hex string or bytes).  Both templates
    are accepted, and FCI children may sit under the FCP template or directly
    under ``6F``.  A missing or malformed response never raises.
    """
    try:
        raw = _as_bytes(data)
    except (TypeError, ValueError):
        return Fcp()
    result = Fcp(raw=raw.hex().upper())
    if not raw:
        return result
    try:
        dos = _data_objects(raw)
        if not any(tag in _DO_TAGS for tag, _value in dos):
            # Tolerate a card that wraps the objects in the proprietary
            # information DO (A5).  A real FCP always carries '82' at the top
            # level, so this only fires for the unusual wrapper - never for a
            # regular FCP's A5 (whose '80' is the SFI, not the file size).
            proprietary = None
            for tag, value in dos:
                if tag == _TAG_PROPRIETARY_INFO:
                    proprietary = value
                    break
            if proprietary is not None:
                inner = ber_tlv_list(proprietary)
                if any(tag in _DO_TAGS for tag, _value in inner):
                    dos = inner
        values = {}
        for tag, value in dos:
            values.setdefault(tag, value)

        size = values.get(_TAG_FILE_SIZE)
        if size:
            result.file_size = int.from_bytes(size, 'big')

        identifier = values.get(_TAG_FILE_IDENTIFIER)
        if identifier:
            result.file_identifier = identifier.hex().upper()

        df_name = values.get(_TAG_DF_NAME)
        if df_name:
            result.df_name = df_name.hex().upper()

        descriptor = values.get(_TAG_FILE_DESCRIPTOR)
        result.shareable, result.structure = _structure(descriptor)
        if descriptor and result.structure in ('linear_fixed', 'cyclic'):
            if len(descriptor) >= 4:
                record_len = int.from_bytes(descriptor[2:4], 'big')
                result.record_len = record_len or None
            if len(descriptor) >= 5:
                result.num_of_rec = descriptor[4] or None
    except (IndexError, ValueError):
        # A truncated/mis-framed response keeps whatever was parsed so far.
        pass
    return result


def fcp_from_select_response(fcp_bytes):
    """Alias for :func:`parse_fcp` - the SELECT response is the FCP/FCI."""
    return parse_fcp(fcp_bytes)


#: TS 51 011 §9.3: the EF structure codings (byte 14 of the SELECT response
#: data).  There is no BER-TLV row in a 2G SIM's list.
_GSM_STRUCTURE = {
    0x00: 'transparent',
    0x01: 'linear_fixed',
    0x03: 'cyclic',
}


def parse_gsm_select(data):
    """Parse a TS 51 011 §9.2.1 SELECT response (the 2G SIM file information).

    A 2G SIM's SELECT returns no TS 102 221 FCP; its response data is the
    GSM "file information": bytes 1-2 RFU, **3-4 file size**, 5-6 file
    identifier, 7 type of file, 8-11 access conditions, 12 file status,
    13 length of the following data, **14 structure** (TS 51 011 §9.3:
    ``00`` transparent, ``01`` linear fixed, ``03`` cyclic) and **15 record
    length** (mandatory for a linear fixed or cyclic EF, optional for a
    transparent one).  Bytes 1-14 are mandatory; a shorter answer yields
    all-``None`` fields, like a malformed BER FCP.

    The record count is not in the answer: it is the file size divided by
    the record length.

    Some cards answer a BER FCP (``62``/``6F`` template) even to the TS 51
    011 select form; those go through :func:`parse_fcp` unchanged.
    """
    raw = _as_bytes(data)
    if len(raw) >= 2 and raw[0] in (_TAG_FCP_TEMPLATE, _TAG_FCI_TEMPLATE):
        return parse_fcp(raw)
    result = Fcp(raw=raw.hex().upper(), form='gsm')
    if len(raw) < 14:
        return result
    result.file_size = int.from_bytes(raw[2:4], 'big')
    result.file_identifier = raw[4:6].hex().upper()
    result.structure = _GSM_STRUCTURE.get(raw[13])
    if result.structure in ('linear_fixed', 'cyclic') and len(raw) >= 15:
        result.record_len = raw[14] or None
    if result.record_len:
        result.num_of_rec = result.file_size // result.record_len
    return result
