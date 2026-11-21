# coding=utf-8
"""Pure hex/byte and command-APDU helpers.

The semantics are the ones the existing server relies on (``pySim.utils``'s
``h2b``/``b2h``/``parse_command_apdu`` and ``osmocom.utils.rpad``), minus the
pysim dependency.  Every function here is pure and card-free testable.
"""

__all__ = [
    'h2b', 'b2h', 'rpad', 'all_subclasses', 'parse_command_apdu',
    'sw_matches',
]


def h2b(s):
    """Hex string -> :class:`bytes`.

    Internal whitespace (spaces, tabs, newlines) is ignored, exactly like
    :func:`bytes.fromhex`; unlike pySim's helper the result is an immutable
    ``bytes`` object.
    """
    return bytes.fromhex(s.strip())


def b2h(b):
    """Bytes-like object -> lowercase hex string."""
    return bytes(b).hex()


def rpad(s, l, c='f'):
    """Right-pad string ``s`` with character ``c`` up to length ``l``.

    A string that is already at least ``l`` characters long is returned
    unchanged (no truncation), matching ``osmocom.utils.rpad``.
    """
    return s + c * (l - len(s))


def all_subclasses(cls):
    """Recursive set of every subclass of ``cls`` (indirect ones included)."""
    out = set()
    pending = list(cls.__subclasses__())
    while pending:
        sub = pending.pop()
        if sub in out:
            continue
        out.add(sub)
        pending.extend(sub.__subclasses__())
    return out


def parse_command_apdu(apdu):
    """Parse a command APDU per ISO/IEC 7816-3, Table 12.

    Returns ``(case, lc, le, data)``:

    * case 1 (4 bytes): no data, no response -> ``(1, 0, 0, b'')``;
    * case 2 (5 bytes): no data, response field -> ``(2, 0, le, b'')``;
    * case 3 (5 + Lc bytes): data, no response field -> ``(3, lc, 0, data)``;
    * case 4 (5 + Lc + 1 bytes): data and response field -> ``(4, lc, le, data)``.

    An ``Lc`` or ``Le`` byte of ``00`` means 256.  Any other length is
    malformed and raises :class:`ValueError`.

    Byte-for-byte behavioural port of ``pySim.utils.parse_command_apdu``.
    """
    apdu = bytes(apdu)
    if len(apdu) == 4:
        # Case 1: no command data, no response data.
        return (1, 0, 0, b'')
    if len(apdu) == 5:
        # Case 2: no command data, response data field present.
        le = apdu[4]
        if le == 0:
            le = 256
        return (2, 0, le, b'')
    if len(apdu) > 5:
        lc = apdu[4]
        if lc == 0:
            lc = 256
        data = apdu[5:5 + lc]
        if len(apdu) == 5 + lc:
            # Case 3: command data present, no response data field.
            return (3, lc, 0, data)
        if len(apdu) == 5 + lc + 1:
            # Case 4: command data and response data field present.
            le = apdu[5 + lc]
            if le == 0:
                le = 256
            return (4, lc, le, data)
        raise ValueError(
            'invalid APDU (%s): Lc=0x%02x (%d) does not match the data field '
            '(%d bytes)' % (b2h(apdu), lc, lc, len(apdu[5:])))
    raise ValueError('invalid APDU (%s): too short' % b2h(apdu))


def sw_matches(sw, pattern):
    """True if the status word ``sw`` matches ``pattern`` (case-insensitive).

    ``pattern`` is a hex status word where ``?`` matches one hex digit -
    ``'9???'`` matches ``9000``/``91XX``/``9FXX``, ``'6A??'`` matches every
    ``6Axx``.  An exact pattern (no ``?``) compares the strings, which is the
    common fast path.  Both arguments may be bytes or hex strings.
    """
    if isinstance(sw, (bytes, bytearray)):
        sw = bytes(sw).hex()
    if isinstance(pattern, (bytes, bytearray)):
        pattern = bytes(pattern).hex()
    sw = str(sw).strip().lower()
    pattern = str(pattern).strip().lower()
    if len(sw) != len(pattern):
        return False
    if '?' not in pattern:
        return sw == pattern
    return all(want == '?' or got == want
               for got, want in zip(sw, pattern))
