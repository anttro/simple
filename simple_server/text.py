# coding=utf-8
"""GSM 03.38 / UCS2 / TS 102 221 Annex A text codecs.

The GSM default alphabet is handled by the ``gsm0338`` package, which
registers the ``gsm03.38`` codec (one character per octet, i.e. the 8-bit
form of TS 23.038 §6.1.2.2).  Packed 7-bit data (TS 23.038 §6.1.2.1) is
packed/unpacked by :func:`pack_gsm7`/:func:`unpack_gsm7`.

Alpha-field UCS2 text uses the three TS 102 221 Annex A forms; all failed
decodes return the raw hex instead of raising, matching the existing
server's ``_decode_stk_text`` fallback.
"""

import codecs

import gsm0338  # noqa: F401  registers the 'gsm03.38' codec

__all__ = [
    'pack_gsm7', 'unpack_gsm7', 'decode_stk_text', 'decode_dcs_text',
    'encode_text',
]


def pack_gsm7(septets):
    """Pack 7-bit septets into octets (TS 23.038 §6.1.2.1).

    The unused high bits of the last octet are zero, as the spec requires.
    Behavioural port of ``pysim_simple_server.testscript.pack_gsm7``.
    """
    out = bytearray()
    acc = 0
    bits = 0
    for s in septets:
        acc |= (s & 0x7F) << bits
        bits += 7
        while bits >= 8:
            out.append(acc & 0xFF)
            acc >>= 8
            bits -= 8
    if bits:
        out.append(acc & 0xFF)
    return bytes(out)


def unpack_gsm7(octets):
    """Unpack 7-bit packed octets back into septets (TS 23.038 §6.1.2.1).

    An octet count that is a multiple of seven carries one fill septet (the
    packing of seven septets uses exactly seven octets, the eighth septet
    being the zero fill); it is dropped when it is zero.  The ambiguity
    (a genuine trailing NUL character) is inherent to packed GSM 7-bit data -
    SMS resolves it with the TP-UDL septet count.
    """
    out = bytearray()
    acc = 0
    bits = 0
    for b in octets:
        acc |= b << bits
        bits += 8
        while bits >= 7:
            out.append(acc & 0x7F)
            acc >>= 7
            bits -= 7
    # 8n mod 7 == n mod 7, so an extra fill septet only appears when n is a
    # multiple of 7.  TS 23.038 §6.1.2.1: the fill bits are set to 0.
    if len(octets) % 7 == 0 and out and out[-1] == 0:
        out.pop()
    return bytes(out)


def _annex_a_char(octet, base):
    """One TS 102 221 Annex A variant 2/3 character at a UCS2 base pointer."""
    if octet & 0x80:
        codepoint = (octet & 0x7F) + base
        return codecs.decode(codepoint.to_bytes(2, 'big'), 'utf_16_be')
    return codecs.decode(bytes([octet]), 'gsm03.38')


def decode_stk_text(raw, packed=False):
    """Decode a TS 102 221 Annex A alpha/text string.

    * all ``FF`` octets -> ``''`` (the spec's "unused bytes");
    * first octet ``80``: variant 1 - the remaining octets are UTF-16-BE
      (unused-byte padding decodes as U+FFFF, as in pySim's adapter);
    * first octet ``81``: variant 2 - character count, a base pointer high
      byte and GSM/offset characters (Annex A example 2);
    * first octet ``82``: variant 3 - character count, a 16-bit base pointer
      and GSM/offset characters;
    * otherwise the GSM default alphabet, one character per octet (the 8-bit
      form, TS 23.038 §6.1.2.2).  Pass ``packed=True`` when the caller knows
      the text is 7-bit packed (TS 23.038 §6.1.2.1, e.g. an SMS user data).

    Any decoding failure returns ``raw.hex()`` - the existing server's
    ``_decode_stk_text`` fallback; this function never raises.
    """
    raw = bytes(raw)
    if not raw:
        return ''
    if raw == b'\xff' * len(raw):
        return ''
    try:
        first = raw[0]
        if first == 0x80:
            # Annex A variant 1: UTF-16-BE from the second octet on.
            return codecs.decode(raw[1:], 'utf_16_be')
        if first == 0x81:
            # Annex A variant 2: count, base-pointer high octet, characters.
            if len(raw) < 3:
                raise ValueError('truncated variant 2 alpha string')
            count = raw[1]
            base = raw[2] << 7
            return ''.join(_annex_a_char(octet, base)
                           for octet in raw[3:3 + count])
        if first == 0x82:
            # Annex A variant 3: count, 16-bit base pointer, characters.
            if len(raw) < 4:
                raise ValueError('truncated variant 3 alpha string')
            count = raw[1]
            base = (raw[2] << 8) | raw[3]
            return ''.join(_annex_a_char(octet, base)
                           for octet in raw[4:4 + count])
        data = unpack_gsm7(raw) if packed else raw
        return codecs.decode(data, 'gsm03.38')
    except Exception:
        return raw.hex()


def decode_dcs_text(raw):
    """Decode a text string whose first octet is a TS 23.038 DCS.

    ``(dcs & 0x0C) == 0x08`` is UCS2, ``== 0x04`` is 8-bit (latin-1), anything
    else is the GSM default alphabet as 8-bit characters.  A failure returns
    ``raw.hex()``.  Port of ``pysim_simple_server.server._decode_dcs_text``.
    """
    raw = bytes(raw)
    if not raw or len(raw) < 2:
        return raw.hex() if raw else ''
    try:
        dcs = raw[0]
        data = raw[1:]
        if (dcs & 0x0C) == 0x08:
            return codecs.decode(data, 'utf_16_be')
        if (dcs & 0x0C) == 0x04:
            return data.decode('latin-1', errors='replace')
        return codecs.decode(data, 'gsm03.38')
    except Exception:
        return raw.hex()


def encode_text(text, dcs):
    """Encode a scripted TERMINAL RESPONSE text string (TS 102 223 §8.15).

    ``08`` (any ``(dcs & 0x0C) == 0x08``) is UCS2, ``00`` is packed GSM 7-bit
    and anything else (e.g. ``04``) is 8-bit GSM default alphabet.  Port of
    ``pysim_simple_server.testscript._encode_text``.
    """
    if (dcs & 0x0C) == 0x08:
        return text.encode('utf-16-be')
    if dcs == 0x00:
        return pack_gsm7(text.encode('gsm03.38'))
    return text.encode('gsm03.38')
