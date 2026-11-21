# coding=utf-8
"""File operations over a duck-typed transport (SELECT/READ/UPDATE/STATUS).

APDU codings (TS 102 221 §11.1.1/.4/.5/.6, ISO/IEC 7816-4):

* SELECT by FID: ``00 A4 00 04 02 <fid> 00`` - P2 = ``04`` = "Return FCP
  template" (Table 11.2) and Le = ``00`` so the whole FCP is returned
  (§11.1.1.3.0: "In order to retrieve the entire FCP template, Le should be
  set to '00'").
* SELECT by DF name (AID): ``00 A4 04 04 <len> <aid> 00`` - P1 = ``04``
  (Table 11.1, selection by DF name); the trailing Le has the same purpose.
* READ BINARY: ``00 B0 <P1 P2> <Le>`` - P1 b7-b1 + P2 are the 15-bit offset
  (§11.1.4.2, Table 11.10).
* READ RECORD: ``00 B2 <rec> 04 <Le>`` - P1 is the record number, P2 ``04``
  is the absolute mode (Table 11.11).
* UPDATE BINARY/RECORD: ``00 D6``/``00 DC`` with the same addressing and the
  data as Lc + value, no Le.
* STATUS: ``80 F2 00 00 00`` (the CAT CLA, Le ``00``).

The transport only needs ``send_apdu(bytes) -> (bytes, bytes)`` (the status
word as two bytes); :class:`simple_server.transport.PcscLink` provides it, and
the tests drive a fake.  This module is otherwise pure.
"""

import re

from simple_server import fcp as fcp_module
from simple_server.util import sw_matches

__all__ = [
    'FileError', 'FileNotFound', 'UnexpectedStatusWord', 'FileOps',
    'FCP_SW', 'NOT_FOUND_SW',
]

# The SELECT answers that mean "this file is not here" (6A82 = file or
# application not found, 6A86 = incorrect P1-P2 - some cards use it for a
# selection form they refuse).
NOT_FOUND_SW = ('6a82', '6a86')
FCP_SW = '9000'

_HEX_RE = re.compile(r'^[0-9A-F]*$')


class FileError(Exception):
    """A file operation failed (wrong status word, bad parameter)."""


class FileNotFound(FileError):
    """SELECT answered 6A82/6A86: the file/application is not on the card."""

    def __init__(self, fid, sw):
        self.fid = fid
        self.sw = sw
        super().__init__('file %s not found (SW %s)' % (fid, sw))


class UnexpectedStatusWord(FileError):
    """The card answered a status word the operation did not expect."""

    def __init__(self, apdu, sw, expected='9000'):
        self.apdu = apdu
        self.sw = sw
        self.expected = expected
        super().__init__('APDU %s answered SW %s, expected %s'
                         % (apdu, sw, expected))


def _sw_hex(sw):
    """Normalise a returned status word to a lower-case hex string."""
    if isinstance(sw, (bytes, bytearray)):
        return bytes(sw).hex()
    return str(sw).strip().lower()


def _hex_value(value, what, exact_len=None):
    """Validate a hex payload and return its upper-case text."""
    text = str(value if value is not None else '').strip()
    text = re.sub(r'\s+', '', text).upper()
    if not text or not _HEX_RE.match(text) or len(text) % 2:
        raise ValueError('%s is not an even-length hex value: %r'
                         % (what, value))
    raw = bytes.fromhex(text)
    if exact_len is not None and len(raw) != exact_len:
        raise ValueError('%s must be %d byte(s), got %d: %r'
                         % (what, exact_len, len(raw), value))
    return text


class FileOps(object):
    """SELECT/READ/UPDATE/STATUS over ``transport``.

    ``cla`` is the command class byte (``00`` for a UICC; ``A0`` for a 2G
    SIM's TS 51 011 command set, which also needs ``sel_fcp=False``).
    ``status_cla`` is the CAT class byte of STATUS (TS 102 221 §11.1.2:
    ``80`` on a UICC, changed to ``A0`` for a SIM by
    :class:`simple_server.cards.CardType`).  ``max_cmd_len`` is the chunk size
    of :meth:`read_file` (255 keeps every READ BINARY a short APDU).
    """

    def __init__(self, transport, cla='00', max_cmd_len=255, status_cla='80',
                 sel_fcp=True):
        self.transport = transport
        self.cla = _hex_value(cla, 'CLA', 1)
        self.max_cmd_len = int(max_cmd_len)
        if not 1 <= self.max_cmd_len <= 255:
            raise ValueError('max_cmd_len must be 1..255')
        self.status_cla = _hex_value(status_cla, 'STATUS CLA', 1)
        # True: the TS 102 221 SELECT (P2 = 04 + Le, the FCP comes back).
        # False: the TS 51 011 §9.2.1 SELECT (P2 = 00, no Le) a 2G SIM needs.
        self.sel_fcp = bool(sel_fcp)

    # ---- low level ------------------------------------------------------

    def _send(self, apdu):
        data, sw = self.transport.send_apdu(bytes(apdu))
        if isinstance(data, str):
            data = bytes.fromhex(data)
        return bytes(data or b''), _sw_hex(sw)

    def _require(self, apdu, expected=FCP_SW, not_found=None):
        data, sw = self._send(apdu)
        if not sw_matches(sw, expected):
            if not_found is not None and sw in NOT_FOUND_SW:
                raise FileNotFound(not_found, sw)
            raise UnexpectedStatusWord(apdu.hex().upper(), sw, expected)
        return data, sw

    def _apdu(self, ins, p1, p2, data=b'', le=None, cla=None):
        apdu = bytes.fromhex((cla or self.cla) + ins + p1 + p2)
        if data:
            if len(data) > 255:
                raise ValueError('command data is %d bytes; a short APDU '
                                 'carries at most 255' % len(data))
            apdu += bytes((len(data),)) + data
        if le is not None:
            apdu += bytes((0 if le == 256 else le,))
        return apdu

    # ---- SELECT ---------------------------------------------------------

    def _select(self, p1, value, not_found):
        payload = bytes.fromhex(value)
        apdu = self._apdu('A4', p1, '04', payload, le=256)
        data, _sw = self._require(apdu, not_found=not_found)
        return fcp_module.parse_fcp(data)

    def select_file(self, fid, fcp=None):
        """SELECT by FID; returns the FCP (``fields None`` when not an FCP).

        The default form is ``00A4 0004 02 <fid> 00`` (P2 = 04, "Return FCP
        template", TS 102 221 Table 11.2), or the TS 51 011 §9.2.1 form
        ``A0A4 0000 02 <fid>`` when the instance was built with
        ``sel_fcp=False`` («P1, P2 = '00', P3 = '02'»).  ``fcp`` overrides
        that per call.  The GSM response is not a BER FCP, so the returned
        :class:`~simple_server.fcp.Fcp` keeps ``fields None`` there - only the
        selection success matters.
        """
        fid = _hex_value(fid, 'FID', 2)
        use_fcp = self.sel_fcp if fcp is None else bool(fcp)
        if use_fcp:
            return self._select('00', fid, fid)
        apdu = self._apdu('A4', '00', '00', bytes.fromhex(fid))
        data, _sw = self._require(apdu, not_found=fid)
        return fcp_module.parse_fcp(data)

    def select_adf(self, aid):
        """SELECT by DF name (``00A4 0404 <len> <aid> 00``); returns the FCP."""
        aid = _hex_value(aid, 'AID')
        if not 1 <= len(aid) // 2 <= 16:
            raise ValueError('an AID is 1..16 bytes, got %d' % (len(aid) // 2))
        return self._select('04', aid, aid)

    def select_mf(self):
        """SELECT MF (``3F00``); returns the FCP."""
        return self.select_file('3F00')

    def select_path(self, chain):
        """Select each segment in order; returns the last FCP.

        A 2-byte (4 hex digit) segment is a FID, anything longer is an AID -
        exactly what :meth:`simple_server.files.FileModel.path_chain` emits.
        """
        segments = chain.split('/') if isinstance(chain, str) else list(chain)
        segments = [s for s in segments if str(s).strip()]
        if not segments:
            raise ValueError('select_path needs at least one segment')
        result = None
        for segment in segments:
            text = _hex_value(segment, 'path segment')
            if len(text) == 4:
                result = self.select_file(text)
            else:
                result = self.select_adf(text)
        return result

    # ---- READ -----------------------------------------------------------

    def _read_binary_at(self, offset, length):
        if not 0 <= offset <= 0x7FFF:
            raise ValueError('READ BINARY offset must be 0..0x7FFF (the '
                             '15-bit P1/P2 field), got %d' % offset)
        if not 1 <= length <= 256:
            raise ValueError('READ BINARY length must be 1..256, got %d'
                             % length)
        apdu = self._apdu('B0', '%02X' % ((offset >> 8) & 0xFF),
                          '%02X' % (offset & 0xFF), le=length)
        return self._require(apdu)

    def read_binary(self, offset, length):
        """READ BINARY ``length`` bytes at ``offset``; returns the bytes."""
        return self._read_binary_at(offset, length)[0]

    def _read_record_at(self, rec_num, length):
        rec_num = _record_number(rec_num)
        if not 1 <= length <= 256:
            raise ValueError('READ RECORD length must be 1..256, got %d'
                             % length)
        apdu = self._apdu('B2', '%02X' % rec_num, '04', le=length)
        return self._require(apdu)

    def read_record(self, rec_num, length):
        """READ RECORD ``rec_num`` (1-based) of ``length`` bytes; returns it."""
        return self._read_record_at(rec_num, length)[0]

    # ---- UPDATE ---------------------------------------------------------

    def update_binary(self, offset, data):
        """UPDATE BINARY at ``offset``; returns the status word."""
        if not 0 <= offset <= 0x7FFF:
            raise ValueError('UPDATE BINARY offset must be 0..0x7FFF, got %d'
                             % offset)
        payload = _data_bytes(data)
        apdu = self._apdu('D6', '%02X' % ((offset >> 8) & 0xFF),
                          '%02X' % (offset & 0xFF), payload)
        return self._require(apdu)[1]

    def update_record(self, rec_num, data):
        """UPDATE RECORD ``rec_num`` (1-based); returns the status word."""
        rec_num = _record_number(rec_num)
        payload = _data_bytes(data)
        apdu = self._apdu('DC', '%02X' % rec_num, '04', payload)
        return self._require(apdu)[1]

    # ---- STATUS ---------------------------------------------------------

    def status(self):
        """STATUS ``80F2000000``; returns the response FCP (TS 102 221 11.1.2)."""
        apdu = self._apdu('F2', '00', '00', le=256, cla=self.status_cla)
        data, _sw = self._require(apdu)
        return fcp_module.parse_fcp(data)

    # ---- FCP-driven read ------------------------------------------------

    def read_file(self, fcp, rec=None, length=None, offset=0):
        """Read the **selected** file, driven by its FCP; returns ``(data, sw)``.

        A transparent file is read with READ BINARY in ``max_cmd_len`` chunks
        from ``offset`` (the whole file when ``length`` is None, using
        ``fcp.file_size``).  A record-structured file reads record ``rec`` at
        ``fcp.record_len``; an explicit ``length`` covers an FCP that lacks
        the value (both raise a clear :class:`FileError`/``ValueError``
        otherwise, never a guessed read).  The caller has already selected the
        file, so no SELECT is issued.
        """
        if fcp is None:
            raise ValueError('read_file needs the selected file FCP')
        if rec is not None or fcp.structure in ('linear_fixed', 'cyclic'):
            if rec is None:
                raise ValueError(
                    'the selected file is %s-structured; pass the record '
                    'number' % fcp.structure)
            record_len = fcp.record_len or length
            if not record_len:
                raise ValueError('the FCP carries no record length; pass '
                                 'length')
            return self._read_record_at(rec, record_len)
        size = fcp.file_size
        if length is None:
            if size is None:
                raise ValueError('the FCP carries no file size; pass length')
            length = size - offset
        if length < 0:
            raise ValueError('offset %d is beyond the file size %s'
                             % (offset, size))
        if length == 0:
            return b'', FCP_SW
        collected = b''
        sw = FCP_SW
        position = 0
        while position < length:
            chunk = min(self.max_cmd_len, length - position)
            data, sw = self._read_binary_at(offset + position, chunk)
            collected += data
            position += chunk
        return collected, sw


def _record_number(rec_num):
    try:
        rec_num = int(rec_num)
    except (TypeError, ValueError):
        raise ValueError('record number must be an integer, got %r'
                         % (rec_num,))
    if not 1 <= rec_num <= 255:
        raise ValueError('record number must be 1..255, got %d' % rec_num)
    return rec_num


def _data_bytes(data):
    """Accept bytes or a hex string; validate the short-APDU length."""
    if isinstance(data, (bytes, bytearray)):
        raw = bytes(data)
    else:
        raw = bytes.fromhex(_hex_value(data, 'data'))
    if not 1 <= len(raw) <= 255:
        raise ValueError('the data field is %d bytes; a short APDU carries '
                         '1..255' % len(raw))
    return raw
