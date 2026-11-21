# coding=utf-8
"""BER-TLV and COMPREHENSION-TLV construction and parsing.

Two object flavours share one implementation:

* :class:`BerTlv` - BER-TLV (ISO/IEC 7816-4 §5.2 / TS 101 220 §7.1.2): a tag
  may be multi-octet (``BF 43``), and the length is the BER short/long form;
* :class:`ComprTlv` - COMPREHENSION-TLV (TS 101 220 §7.1.1): exactly one tag
  octet (bit 8 is the comprehension-required flag; it is *not* interpreted
  here) and exactly one length octet.

The module functions reproduce the parsing semantics the existing server uses
(``pysim_simple_server/server.py``), extended to walk multi-octet BER tags.
They are pure and card-free testable.
"""

__all__ = [
    'BerTlv', 'ComprTlv', 'encode_ber_len', 'encode_tag',
    'parse_ber_len', 'ber_tlv_list', 'tlv_map', 'parse_one',
]


# ---- tags and lengths ------------------------------------------------------

def encode_tag(tag):
    """Encode a tag number as BER tag octets.

    ``int`` values up to ``0xFF`` are one octet; larger integers are emitted
    big-endian (``0xBF43`` -> ``bf 43``).  A ``bytes``/``bytearray`` tag is
    used verbatim, which is the escape hatch for tag octets that are not a
    plain integer (e.g. a multi-octet tag written out by hand).

    Note: a bare one-octet tag whose low five bits are all set (``1F``,
    ``3F``, ``BF``, ...) cannot stand alone in BER - supply the continuation
    octets (``0xBF43`` or ``b'\\xbf\\x43'``) for a real multi-octet tag.
    """
    if isinstance(tag, (bytes, bytearray)):
        if not tag:
            raise ValueError('a TLV tag cannot be empty')
        return bytes(tag)
    if not isinstance(tag, int) or isinstance(tag, bool):
        raise TypeError('a TLV tag must be an int, bytes or bytearray')
    if tag < 0:
        raise ValueError('a TLV tag cannot be negative')
    if tag <= 0xFF:
        return bytes([tag])
    return tag.to_bytes((tag.bit_length() + 7) // 8, 'big')


def encode_ber_len(length):
    """Encode a BER length: short form below 128, else ``81 nn``/``82 nnnn``/..."""
    if length < 0:
        raise ValueError('a TLV length cannot be negative')
    if length < 0x80:
        return bytes([length])
    for n in (1, 2, 3):
        if length < (1 << (8 * n)):
            return bytes([0x80 | n]) + length.to_bytes(n, 'big')
    raise ValueError('BER length too large: %d' % length)


def parse_ber_len(raw, off):
    """Read a BER length at ``raw[off]``; returns ``(length, value_offset)``.

    Short form (``< 0x80``) and long form (``0x81 nn`` ... ``0x8n ...``) are
    supported.  A truncated long form yields ``(0, off + 1)`` and an offset
    past the end yields ``(0, off)``, so a caller stops instead of walking
    into the value.

    Port of ``pysim_simple_server.server._ber_len_at``.
    """
    raw = bytes(raw)
    if off >= len(raw):
        return 0, off
    first = raw[off]
    if first < 0x80:
        return first, off + 1
    n = first & 0x7F
    if n < 1 or off + 1 + n > len(raw):
        # 0x80 (indefinite) and truncated long forms carry no usable length.
        return 0, off + 1
    return int.from_bytes(raw[off + 1:off + 1 + n], 'big'), off + 1 + n


def _read_tag(raw, off, multi_byte=True):
    """Return ``(tag, next_offset)`` for the tag at ``raw[off]``.

    ``tag`` is ``None`` when a multi-octet tag is truncated.  With
    ``multi_byte=False`` only one octet is consumed (COMPREHENSION-TLV style).
    """
    if off >= len(raw):
        return None, off
    first = raw[off]
    if not multi_byte or (first & 0x1F) != 0x1F:
        return first, off + 1
    # Multi-octet form: subsequent octets continue while bit 8 is set.
    tag = bytearray([first])
    pos = off + 1
    while pos < len(raw):
        octet = raw[pos]
        tag.append(octet)
        pos += 1
        if not (octet & 0x80):
            return int.from_bytes(bytes(tag), 'big'), pos
    return None, off


def _strict_len(raw, off):
    """``parse_ber_len`` for the single-TLV peelers: truncation raises."""
    if off >= len(raw):
        raise ValueError('truncated TLV length at offset %d' % off)
    length, voff = parse_ber_len(raw, off)
    if raw[off] >= 0x80 and voff == off + 1:
        raise ValueError('truncated TLV length at offset %d' % off)
    return length, voff


# ---- TLV objects -----------------------------------------------------------

class BerTlv(object):
    """A BER-TLV object.

    ``tag`` may be fixed by a subclass (``class Result(BerTlv, tag=0x8B)``),
    by the constructor (``BerTlv(tag=0x8B, value=b'...')``) or both - the
    constructor argument wins.  A TLV carries either raw ``value`` bytes or
    ``children`` TLVs (never both); ``children`` are serialised recursively.

    ``to_tlv()`` emits tag + BER length + value; ``to_bytes()`` emits just the
    value field.
    """

    tag = None

    def __init_subclass__(cls, tag=None, **kwargs):
        super().__init_subclass__(**kwargs)
        if tag is not None:
            cls.tag = tag

    def __init__(self, tag=None, value=None, children=None):
        if tag is not None:
            self.tag = tag
        if self.tag is None:
            raise ValueError('%s needs a tag' % type(self).__name__)
        children = list(children) if children else []
        if children and value is not None:
            raise ValueError('a TLV takes raw value bytes or children, not both')
        self.children = children
        self.value = bytes(value) if value is not None else b''

    def to_bytes(self):
        """The value field: the children's TLVs, or the raw value bytes."""
        if self.children:
            return b''.join(child.to_tlv() for child in self.children)
        return self.value

    def to_tlv(self):
        """The complete TLV: tag + length + value."""
        body = self.to_bytes()
        return encode_tag(self.tag) + encode_ber_len(len(body)) + body

    @classmethod
    def parse(cls, data, off=0, multi_byte_tags=True):
        """Decode the first TLV at ``data[off]``; returns ``(instance, rest)``."""
        data = bytes(data)
        tag, toff = _read_tag(data, off, multi_byte_tags)
        if tag is None:
            raise ValueError('truncated TLV tag at offset %d' % off)
        length, voff = _strict_len(data, toff)
        end = voff + length
        if end > len(data):
            raise ValueError('truncated TLV value: %d bytes declared, %d available'
                             % (length, len(data) - voff))
        return cls(tag=tag, value=data[voff:end]), data[end:]

    @classmethod
    def from_bytes(cls, data):
        """Decode exactly one TLV; trailing bytes are an error."""
        obj, rest = cls.parse(data)
        if rest:
            raise ValueError('%d trailing byte(s) after the TLV' % len(rest))
        return obj

    def __repr__(self):
        return '%s(tag=0x%X, value=%s)' % (
            type(self).__name__, self.tag, self.to_bytes().hex())


class ComprTlv(BerTlv):
    """A COMPREHENSION-TLV object (TS 101 220 §7.1.1).

    One tag octet (the comprehension-required flag is just part of the tag
    value and is neither set nor stripped) and one length octet, so the value
    is limited to 255 bytes.
    """

    @classmethod
    def parse(cls, data, off=0):
        return super().parse(data, off, multi_byte_tags=False)

    def to_tlv(self):
        body = self.to_bytes()
        tag = encode_tag(self.tag)
        if len(tag) != 1:
            raise ValueError('a COMPREHENSION-TLV tag is exactly one octet')
        if len(body) > 0xFF:
            raise ValueError('COMPREHENSION-TLV value is %d bytes (max 255)'
                             % len(body))
        return tag + bytes([len(body)]) + body


# ---- module-level parsing --------------------------------------------------

def ber_tlv_list(data, multi_byte_tags=True):
    """``[(tag, value)]`` of a BER-TLV sequence.

    Stops at the first truncated or malformed TLV instead of walking into its
    value (a mis-read length is how the live FETCH path once indexed past the
    end of the buffer).  Multi-octet tags (``0xBFxx``/``1F``-style
    continuation) are consumed as one tag number; pass
    ``multi_byte_tags=False`` for COMPREHENSION-TLV data, where bit 8 of a
    one-octet tag is just the CR flag.

    Port of ``pysim_simple_server.server._ber_tlv_list`` plus the BER tag
    continuation.
    """
    data = bytes(data)
    out = []
    off = 0
    while off + 1 < len(data):
        tag, toff = _read_tag(data, off, multi_byte_tags)
        if tag is None:
            break
        tlen, voff = parse_ber_len(data, toff)
        if voff + tlen > len(data):
            break
        out.append((tag, data[voff:voff + tlen]))
        if voff + tlen <= off:      # no forward progress: stop
            break
        off = voff + tlen
    return out


def tlv_map(data, multi_byte_tags=True):
    """Top-level ``{tag: value}`` map; the first occurrence of a tag wins."""
    out = {}
    for tag, value in ber_tlv_list(data, multi_byte_tags=multi_byte_tags):
        out.setdefault(tag, value)
    return out


def parse_one(data, off=0, multi_byte_tags=True):
    """Peel one TLV off ``data``: ``(tag, length, value, rest)``.

    Raises :class:`ValueError` when the TLV is truncated.
    """
    data = bytes(data)
    tag, toff = _read_tag(data, off, multi_byte_tags)
    if tag is None:
        raise ValueError('truncated TLV tag at offset %d' % off)
    length, voff = _strict_len(data, toff)
    end = voff + length
    if end > len(data):
        raise ValueError('truncated TLV value: %d bytes declared, %d available'
                         % (length, len(data) - voff))
    return tag, length, data[voff:end], data[end:]
