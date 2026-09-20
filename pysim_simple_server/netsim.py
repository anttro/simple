# coding=utf-8
"""Network-condition simulation for the SIMple lab.

Replays the card-facing write patterns of a real phone when the network
condition changes, following the trace study in ``projects/UICC_NAA.md``
(section 13): EPS attach, service loss / limited service, roaming denial,
2G fallback, SMS delivery, cell-broadcast reconfiguration and an
AUTHENTICATE exchange.

Only the observed vocabulary is used: UPDATE BINARY (D6), UPDATE RECORD (DC),
ENVELOPE (Event Download) and AUTHENTICATE.  FPLMN and the 5GS location files
are never written (no observed phone does - UICC_NAA.md section 12).

The ``build_*`` functions are pure and unit-tested (tests/test_netsim.py).
``NetSimRunner`` applies a scenario to a live card through the helpers of
``pysim_simple_server.server``; the caller holds the card lock.
"""

import random
import re
import threading
import time

# Candidate paths per logical file.  The first existing one is used, so a
# USIM card is served from ADF.USIM and a GSM SIM from DF.GSM/DF.TELECOM.
FILE_PATHS = {
    'imsi': ['ADF.USIM/6F07', 'DF.GSM/6F07'],
    'ehplmn': ['ADF.USIM/6FD9'],
    'spdi': ['ADF.USIM/6FCD'],
    'hplmnwact': ['ADF.USIM/6F62', 'DF.GSM/6F62'],
    'epsnsc': ['ADF.USIM/6FE4'],
    'loci': ['ADF.USIM/6F7E', 'DF.GSM/6F7E'],
    'psloci': ['ADF.USIM/6F73', 'DF.GSM/6F73'],
    'epsloci': ['ADF.USIM/6FE3'],
    'fplmn': ['ADF.USIM/6F7B', 'DF.GSM/6F7B'],
    'kc': ['ADF.USIM/4F20', 'DF.GSM/6F08'],
    'kcgprs': ['ADF.USIM/4F52', 'DF.GSM/6F09'],
    'smsstatus': ['ADF.USIM/6F43', 'DF.TELECOM/6F43', 'DF.GSM/6F43'],
    'cbmi': ['ADF.USIM/6F45', 'DF.GSM/6F45'],
    'cbmir': ['ADF.USIM/6F50', 'DF.GSM/6F50'],
}

# Which simulated service state each scenario establishes (Network state
# panel).  Scenarios not listed (churn, cb_reconfig, authenticate) do not
# touch the network registration and leave the previous state.
SCENARIO_SERVICE = {
    'cold_boot': 'none',
    'attach_eps': 'normal',
    'attach_2g': 'normal',
    'service_lost': 'none',
    'limited_service': 'limited',
    'roaming_denied': 'limited',
    'sms_received': 'normal',
}

# File status codes (LOCI/PSLOCI/EPSLOCI update status).
ST_UPDATED = 0x00
ST_NOT_UPDATED = 0x01
ST_PLMN_NOT_ALLOWED = 0x02
ST_LA_NOT_ALLOWED = 0x03

# Location status event (TS 102 223 section 8.27).
LOC_STATUS_NORMAL = 0x00
LOC_STATUS_LIMITED = 0x01
LOC_STATUS_NO_SERVICE = 0x02

EVENT_LOCATION_STATUS = 0x03


def _norm_hex(value, nbytes=None):
    """Normalize a hex string, optionally checking the byte length."""
    h = re.sub(r'[^0-9a-fA-F]', '', value or '').upper()
    if nbytes is not None and len(h) != nbytes * 2:
        raise ValueError('expected %d hex bytes, got %r' % (nbytes, value))
    return h


def _pad_ff(data, size):
    """Pad (or truncate) a byte string to `size` with 0xFF."""
    if size is None or len(data) >= size:
        return data[:size] if size else data
    return data + b'\xFF' * (size - len(data))


def rand_hex(nbytes, rng=None):
    r = rng or random
    return ''.join(r.choice('0123456789ABCDEF') for _ in range(nbytes * 2))


def plmn_bcd(mcc, mnc):
    """MCC/MNC digits -> 3-byte PLMN (TS 24.008 10.5.1.13).

    Nibble order: byte0 = MCC2 MCC1, byte1 = MNC3 MCC3, byte2 = MNC2 MNC1
    (MNC3 = 'F' for two-digit MNCs).  Cross-checked against the pySim
    PLMNsel test vector 228/06 -> 22 F8 60.
    """
    mcc = re.sub(r'\D', '', str(mcc or ''))
    mnc = re.sub(r'\D', '', str(mnc or ''))
    if len(mcc) != 3:
        raise ValueError('MCC must have 3 digits, got %r' % mcc)
    if len(mnc) not in (2, 3):
        raise ValueError('MNC must have 2 or 3 digits, got %r' % mnc)
    m = [int(c) for c in mcc]
    n = [int(c) for c in mnc] + ([0xF] if len(mnc) == 2 else [])
    return '%02X%02X%02X' % ((m[1] << 4) | m[0], (n[2] << 4) | m[2], (n[1] << 4) | n[0])


def _plmn_bytes(plmn_hex):
    return bytes.fromhex(_norm_hex(plmn_hex, 3))


def parse_imsi(data_hex):
    """EF.IMSI content -> IMSI digits, or None.  Byte 1 is the length; the
    high nibble of byte 2 is the parity/identity nibble (TS 31.102 4.2.2)."""
    h = _norm_hex(data_hex)
    if len(h) < 4:
        return None
    body = h[2:]
    swapped = ''.join(body[i + 1] + body[i]
                      for i in range(0, len(body) - 1, 2))
    digits = re.sub(r'F+$', '', swapped)
    return digits[1:] or None


# ---- EPS NAS Security Context (EF.EPSNSC, TS 31.102 4.2.92) ----


def build_epsnsc(ksi, kasme_hex, ul, dl, algo, size=54):
    """One A0 TLV record, padded to the card's record size with FF."""
    kasme = bytes.fromhex(_norm_hex(kasme_hex, 32))
    inner = (bytes([0x80, 0x01, ksi & 0xFF])
             + bytes([0x81, 0x20]) + kasme
             + bytes([0x82, 0x04]) + (ul & 0xFFFFFFFF).to_bytes(4, 'big')
             + bytes([0x83, 0x04]) + (dl & 0xFFFFFFFF).to_bytes(4, 'big')
             + bytes([0x84, 0x01, algo & 0xFF]))
    rec = bytes([0xA0, len(inner)]) + inner
    return _pad_ff(rec, size).hex().upper()


def build_epsnsc_invalidate(size=54, kasme_hex=None, algo=0x00):
    """Invalid context: KSI 07, optional old KASME kept (UICC_NAA.md 5.2)."""
    kasme = (bytes.fromhex(_norm_hex(kasme_hex, 32)) if kasme_hex
             else b'\xFF' * 32)
    inner = (bytes([0x80, 0x01, 0x07])
             + bytes([0x81, 0x20]) + kasme
             + bytes([0x82, 0x04]) + b'\xFF' * 4
             + bytes([0x83, 0x04]) + b'\xFF' * 4
             + bytes([0x84, 0x01, algo & 0xFF]))
    rec = bytes([0xA0, len(inner)]) + inner
    return _pad_ff(rec, size).hex().upper()


def parse_epsnsc_kasme(record_hex):
    """Extract the 32-byte KASME from an EPSNSC record, or None."""
    try:
        data = bytes.fromhex(_norm_hex(record_hex))
    except ValueError:
        return None
    # The record is one A0 container; KASME is the 81 TLV inside it.
    if len(data) >= 2 and data[0] == 0xA0:
        data = data[2:2 + data[1]]
    i = 0
    while i + 2 <= len(data):
        tag, ln = data[i], data[i + 1]
        value = data[i + 2:i + 2 + ln]
        if tag == 0x81 and ln == 32:
            if all(b == 0xFF for b in value):
                return None      # wiped key = invalid context
            return value.hex().upper()
        i += 2 + ln
    return None


# ---- Location files (TS 31.102 4.2.16 / 4.2.23 / 4.2.91) ----


def build_loci(tmsi_hex, plmn_hex, lac_hex, status=ST_UPDATED, rfu=0xFF):
    """LOCI: TMSI(4) + LAI(5) + RFU(1) + update status(1) = 11 bytes."""
    return (bytes.fromhex(_norm_hex(tmsi_hex, 4)) + _plmn_bytes(plmn_hex)
            + bytes.fromhex(_norm_hex(lac_hex, 2))
            + bytes([rfu & 0xFF, status & 0xFF])).hex().upper()


def build_loci_dummy(plmn_hex, status=ST_NOT_UPDATED):
    """Service lost: TMSI FF, PLMN kept, LAC FFFE, status 01 (010 = PLMN not
    allowed on a permanent rejection; UICC_NAA.md C3/C3a)."""
    return build_loci('FFFFFFFF', plmn_hex, 'FFFE', status)


def build_psloci(ptmsi_hex, sig_hex, plmn_hex, lac_hex, rac_hex,
                 status=ST_UPDATED):
    """PSLOCI: P-TMSI(4) + signature(3) + RAI(6) + status(1) = 14 bytes."""
    return (bytes.fromhex(_norm_hex(ptmsi_hex, 4))
            + bytes.fromhex(_norm_hex(sig_hex, 3)) + _plmn_bytes(plmn_hex)
            + bytes.fromhex(_norm_hex(lac_hex, 2))
            + bytes.fromhex(_norm_hex(rac_hex, 1))
            + bytes([status & 0xFF])).hex().upper()


def build_psloci_dummy(plmn_hex, status=ST_NOT_UPDATED):
    return build_psloci('FFFFFFFF', 'FFFFFF', plmn_hex, 'FFFE', 'FF', status)


def build_epsloci(guti_hex, plmn_hex, tac_hex, status=ST_UPDATED):
    """EPSLOCI: GUTI(12) + TAI(5) + EPS update status(1) = 18 bytes."""
    return (bytes.fromhex(_norm_hex(guti_hex, 12)) + _plmn_bytes(plmn_hex)
            + bytes.fromhex(_norm_hex(tac_hex, 2))
            + bytes([status & 0xFF])).hex().upper()


def build_epsloci_dummy(status=ST_NOT_UPDATED, keep_plmn=None):
    """EPSLOCI dummy: the EPS-mobile-identity pair `0B F6` (content length +
    GUTI type octet) is kept and the 12-byte GUTI is fully wiped; the TAC is
    `FF FE` and the last byte is the EPS update status (`01` = not updated on
    service loss, `02` = roaming not allowed on a permanent rejection - C3a,
    the spec model; the corpus never captured a 6FE3 rejection write).
    `keep_plmn` selects the NMR style that preserves the last visited TAI PLMN
    instead of wiping it - the guest style wipes GUTI *and* TAI PLMN
    (UICC_NAA.md 6.3/C3)."""
    plmn = _norm_hex(keep_plmn, 3) if keep_plmn else None
    tail = 'FF' * 10 + plmn if plmn else 'FF' * 13
    return '0BF6' + tail + 'FFFE' + '%02X' % (status & 0xFF)


def fplmn_entries(data_hex):
    """EF.FPLMN content as 3-byte PLMN entries; 'FFFFFF' marks an empty slot
    (TS 31.102 4.2.16: valid in any position, never a terminator)."""
    h = _norm_hex(data_hex)
    return [h[i:i + 6] for i in range(0, len(h) - 5, 6)]


def insert_fplmn(data_hex, plmn_hex):
    """Store a denied PLMN per TS 31.102 4.2.16: fill the first empty slot,
    otherwise shift the list left and append (the longest-held entry is
    lost).  Returns the full updated EF content, or None when the PLMN is
    already listed (a duplicate entry is meaningless)."""
    plmn = _norm_hex(plmn_hex, 3)
    entries = fplmn_entries(data_hex)
    if plmn in entries:
        return None
    if not entries:
        return plmn
    try:
        idx = entries.index('FFFFFF')
    except ValueError:
        entries = entries[1:] + [plmn]
    else:
        entries[idx] = plmn
    return ''.join(entries)


def remove_fplmn(data_hex, plmn_hex):
    """Clear every occurrence of a PLMN from EF.FPLMN (a successful manual
    selection removes the entry, TS 23.122).  Returns the full updated EF
    content, or None when the PLMN is not listed."""
    plmn = _norm_hex(plmn_hex, 3)
    entries = fplmn_entries(data_hex)
    if plmn not in entries:
        return None
    return ''.join('FFFFFF' if e == plmn else e for e in entries)


# ---- Ciphering keys and CB/SMS files ----


def build_kc(kc_hex, algo, size):
    """Kc: 9-byte USIM form (Kc + algo) or the 33-byte GSM record form."""
    kc = bytes.fromhex(_norm_hex(kc_hex, 8))
    return _pad_ff(kc + bytes([algo & 0xFF]), size).hex().upper()


def build_kc_invalidate(size):
    """Invalid Kc: 1-byte 07, USIM 9-byte FF*8+07, GSM 33-byte 07+FF*32."""
    if size <= 1:
        return '07'
    if size == 9:
        return (b'\xFF' * 8 + b'\x07').hex().upper()
    return (b'\x07' + b'\xFF' * (size - 1)).hex().upper()


def build_smsstatus(count):
    """EF.SMSstatus: 2-byte big-endian counter (TS 51.011 10.5.9)."""
    return '%04X' % (count & 0xFFFF)


def parse_smsstatus(data_hex):
    """Current counter value, or None when unreadable/erased."""
    try:
        data = bytes.fromhex(_norm_hex(data_hex))
    except ValueError:
        return None
    if len(data) < 2 or all(b == 0xFF for b in data):
        return None
    return int.from_bytes(data[:2], 'big')


def bump_smsstatus(count):
    value = parse_smsstatus(count)
    return build_smsstatus((value + 1) if value is not None else 0x01FF)


def build_cbmi(ids, size=None):
    """CBMI: 2-byte message IDs; pad to the file size with FF when known."""
    out = b''.join((i & 0xFFFF).to_bytes(2, 'big') for i in ids)
    return _pad_ff(out, size).hex().upper()


def build_cbmir(ranges, size=None):
    """CBMIR: 4-byte ranges (low, high), most-significant byte first."""
    out = b''.join((lo & 0xFFFF).to_bytes(2, 'big')
                   + (hi & 0xFFFF).to_bytes(2, 'big') for lo, hi in ranges)
    return _pad_ff(out, size).hex().upper()


# ---- Event Download and AUTHENTICATE ----


def build_location_status_event(status, plmn_hex=None, lac_hex=None,
                                cell_id_hex=None):
    """Location status data object (TS 102 223 8.27), with the optional
    Location information object (8.19) only for normal service."""
    out = bytes([0x9B, 0x01, status & 0xFF])
    if (status == LOC_STATUS_NORMAL and plmn_hex and lac_hex
            and cell_id_hex):
        info = (_plmn_bytes(plmn_hex) + bytes.fromhex(_norm_hex(lac_hex, 2))
                + bytes.fromhex(_norm_hex(cell_id_hex, 2)))
        out += bytes([0x13, len(info)]) + info
    return out.hex().upper()


def build_auth_apdu(rand_hex, autn_hex, cla='00'):
    """AUTHENTICATE (3G/EPS/5G, P2 81), TS 31.102 7.1.2.1.

    Command data: L1 RAND L2 AUTN = 1+16+1+16 = 34 bytes (Lc 0x22).
    """
    rand = bytes.fromhex(_norm_hex(rand_hex, 16))
    autn = bytes.fromhex(_norm_hex(autn_hex, 16))
    body = bytes([len(rand)]) + rand + bytes([len(autn)]) + autn
    return '%s880081%02X%s' % (cla, len(body), body.hex().upper())


def parse_auth_response(data_hex):
    """Parse the AUTHENTICATE response: DB (success) or DC (sync failure)."""
    try:
        data = bytes.fromhex(_norm_hex(data_hex or ''))
    except ValueError:
        return None
    if not data or data[0] not in (0xDB, 0xDC):
        return None
    out = {'type': 'success' if data[0] == 0xDB else 'synchronisation_failure'}
    i = 1
    while i + 2 <= len(data):
        ln = data[i]
        out.setdefault('objects', []).append(data[i + 1:i + 1 + ln].hex().upper())
        i += 1 + ln
    return out


# ---- scenario runner ----


class StepError(Exception):
    """A write/select step failed; the scenario stops at that step."""


def _default(value, rng=None):
    return value if value not in (None, '') else None


def _hexint(value, default=0):
    """Accept '0x1f'/1f/'31'/'3' style parameters as an integer."""
    if value in (None, ''):
        return default
    if isinstance(value, int):
        return value
    text = str(value).strip()
    try:
        return int(text, 16) if re.fullmatch(r'(0[xX])?[0-9a-fA-F]+', text) else int(text)
    except ValueError:
        return default


class NetSimRunner:
    """Apply one scenario to a live card.  The caller holds _CARD_LOCK."""

    def __init__(self, srv, app, params=None, event_list=None, sleep=time.sleep):
        self.srv = srv
        self.app = app
        self.params = params or {}
        self.event_list = list(event_list or [])
        self.sleep = sleep
        self.lchan = app.rs.lchan[0]
        self.steps = []
        self._cleanups = []
        scc = getattr(getattr(srv, '_server_ref', None), 'scc', None)
        self.scc = scc

    # -- parameter helpers

    def p(self, key, default=None):
        v = self.params.get(key)
        return default if v in (None, '') else v

    @property
    def mcc(self):
        return self.p('mcc', '001')

    @property
    def mnc(self):
        return self.p('mnc', '01')

    @property
    def plmn(self):
        return self.p('plmn') or plmn_bcd(self.mcc, self.mnc)

    @property
    def lac(self):
        return self.p('lac') or rand_hex(2)

    @property
    def cell_id(self):
        return self.p('cell_id') or rand_hex(2)

    @property
    def tac(self):
        return self.p('tac') or self.lac

    @property
    def rac(self):
        return self.p('rac') or rand_hex(1)

    @property
    def kasme(self):
        return self.p('kasme') or rand_hex(32)

    def status(self, default):
        v = self.p('location_status')
        return int(v, 16) if isinstance(v, str) else (int(v) if v is not None else default)

    # -- step plumbing

    def _add(self, action, **kw):
        step = {'action': action}
        step.update(kw)
        self.steps.append(step)
        return step

    def _cleanup(self):
        for c in self._cleanups:
            try:
                c()
            except Exception:
                pass
        self._cleanups = []

    def select(self, path):
        cur, cleanup = self.srv._select_path(self.lchan, path, self.app)
        if cleanup:
            self._cleanups.append(cleanup)
        return cur

    def _open(self, key):
        last = None
        for path in FILE_PATHS[key]:
            try:
                self.select(path)
                return path
            except Exception as e:
                last = e
        raise StepError('no candidate file for %s (%s)' % (key, last))

    def _meta(self):
        l = self.lchan
        return {'file_size': l.selected_file_size(),
                'record_len': l.selected_file_record_len(),
                'num_of_rec': l.selected_file_num_of_rec()}

    def _check(self, data, sw, **kw):
        ok = sw == '9000'
        step = self._add(**kw, data=data, sw=sw, ok=ok)
        if not ok:
            raise StepError('%s returned SW %s' % (kw.get('action'), sw))
        return step

    def write_binary(self, key, data_hex, pad=True, label=None, optional=False):
        try:
            path = self._open(key)
        except StepError as e:
            if optional:
                self._add('skip', file=label or key, note=str(e))
                return None
            raise
        size = self.lchan.selected_file_size()
        data = data_hex
        if pad and size:
            data = _pad_ff(bytes.fromhex(data_hex), size).hex().upper()
        _out, sw = self.lchan.update_binary(data)
        return self._check(data, sw, action='update_binary', file=label or key,
                           key=key, path=path)

    def write_record(self, key, data_hex, record=1, pad=True, label=None, optional=False):
        try:
            path = self._open(key)
        except StepError as e:
            if optional:
                self._add('skip', file=label or key, note=str(e))
                return None
            raise
        size = self.lchan.selected_file_record_len()
        data = data_hex
        if pad and size:
            data = _pad_ff(bytes.fromhex(data_hex), size).hex().upper()
        _out, sw = self.lchan.update_record(record, data)
        return self._check(data, sw, action='update_record', file=label or key,
                           key=key, path=path, record=record)

    def read_binary_current(self):
        data, sw = self.lchan.read_binary()
        return (data or ''), sw

    # -- forbidden PLMNs (permanent #11 rejection, UICC_NAA.md C3a)

    def home_plmns(self):
        """3-byte HPLMN/EHPLMN entries from the cached Network-state monitor;
        TS 23.122: the home network is never stored in EF.FPLMN."""
        out = set()
        files = ((getattr(self.srv, 'net_state', None) or {}).get('files') or {})

        def flat(key):
            f = files.get(key) or {}
            if f.get('present') and f.get('kind') == 'transparent':
                return _norm_hex(f.get('data'))
            return None

        hp = flat('hplmnwact')
        if hp:
            # HPLMNwAcT records are 5 bytes (PLMN + access technology); the
            # first record is the HPLMN (TS 31.102 4.2.5).
            out.add(hp[0:6])
        ehp = flat('ehplmn')
        if ehp:
            out.update(fplmn_entries(ehp))
        if not out:
            # Fallback: the HPLMN is the IMSI's MCC/MNC.  The IMSI does not
            # encode the MNC length, so both interpretations are guarded.
            imsi = parse_imsi(flat('imsi'))
            if imsi and len(imsi) >= 5 and imsi[:3].isdigit():
                try:
                    out.add(plmn_bcd(imsi[0:3], imsi[3:5]))
                    if len(imsi) >= 6 and imsi[5].isdigit():
                        out.add(plmn_bcd(imsi[0:3], imsi[3:6]))
                except ValueError:
                    pass
        return {h for h in out if h and h != 'FFFFFF'}

    def write_fplmn(self, plmn_hex, optional=True):
        path = None
        try:
            path = self._open('fplmn')
        except StepError as e:
            if optional:
                self._add('skip', file='fplmn', note=str(e))
                return None
            raise
        plmn = _norm_hex(plmn_hex, 3)
        if plmn in self.home_plmns():
            self._add('skip', file='fplmn',
                      note='home PLMN is never stored (TS 23.122)')
            return None
        size = self.lchan.selected_file_size()
        data, sw = self.read_binary_current()
        if sw != '9000' or not data:
            data = 'FF' * (size or 12)
        new_data = insert_fplmn(data, plmn)
        if new_data is None:
            self._add('skip', file='fplmn', note='PLMN already listed')
            return None
        return self.write_binary('fplmn', new_data, pad=False, label='fplmn')

    def clear_fplmn(self, plmn_hex):
        """A successful manual selection removes the PLMN from EF.FPLMN
        (TS 23.122); every occurrence is cleared and nothing is written when
        the PLMN is not listed."""
        try:
            self._open('fplmn')
        except StepError as e:
            self._add('skip', file='fplmn', note=str(e))
            return None
        data, sw = self.read_binary_current()
        if sw != '9000' or not data:
            self._add('skip', file='fplmn', note='read failed (SW %s)' % sw)
            return None
        new_data = remove_fplmn(data, plmn_hex)
        if new_data is None:
            return None
        return self.write_binary('fplmn', new_data, pad=False, label='fplmn')

    def read_record_current(self, record=1):
        data, sw = self.lchan.read_record(record)
        return (data or ''), sw

    def send_location_status(self, status):
        if EVENT_LOCATION_STATUS not in self.event_list:
            self._add('skip', file='location_status',
                      note='event 0x03 not in SET UP EVENT LIST')
            return
        data = build_location_status_event(
            status,
            self.plmn if status == LOC_STATUS_NORMAL else None,
            self.lac if status == LOC_STATUS_NORMAL else None,
            self.cell_id if status == LOC_STATUS_NORMAL else None)
        if not self.scc:
            raise StepError('card session not available')
        _d, sw = self.srv._send_event_download(self.scc, EVENT_LOCATION_STATUS,
                                               bytes.fromhex(data))
        self._add('event', file='location_status', data=data, sw=sw,
                  ok=(sw == '9000'))

    # -- scenario building blocks

    def invalidate_epsnsc(self, keep_key=True):
        try:
            self._open('epsnsc')
        except StepError as e:
            self._add('skip', file='epsnsc', note=str(e))
            return
        size = self.lchan.selected_file_record_len() or 54
        kasme = None
        if keep_key:
            data, sw = self.read_record_current(1)
            if sw == '9000':
                kasme = parse_epsnsc_kasme(data)
        self.write_record('epsnsc', build_epsnsc_invalidate(size, kasme),
                          label='epsnsc')

    def store_epsnsc(self):
        self.write_record('epsnsc', build_epsnsc(
            _hexint(self.p('ksi', '03'), 0x03),
            self.kasme,
            _hexint(self.p('ul', 0)), _hexint(self.p('dl', 0)),
            _hexint(self.p('algo', '02'), 0x02)),
            label='epsnsc', optional=True)

    def write_real_locations(self, status=ST_UPDATED):
        # A successful attach is a manual selection of this PLMN: it is
        # removed from EF.FPLMN first (TS 23.122).
        self.clear_fplmn(self.plmn)
        self.write_binary('loci', build_loci(
            self.p('tmsi') or rand_hex(4), self.plmn, self.lac, status),
            label='loci', optional=True)
        self.write_binary('psloci', build_psloci(
            self.p('ptmsi') or rand_hex(4), self.p('ptmsi_sig') or rand_hex(3),
            self.plmn, self.lac, self.rac, status), label='psloci', optional=True)
        self.write_binary('epsloci', build_epsloci(
            self.p('guti') or rand_hex(12), self.plmn, self.tac, status),
            label='epsloci', optional=True)

    def write_dummy_locations(self, status=ST_NOT_UPDATED, keep_plmn=None):
        """Service loss: LOCI/PSLOCI keep the PLMN with the dummy status 01
        and EPSLOCI is wiped to `0B F6` + FF*13 + `FF FE 01` (UICC_NAA.md C3).
        A rejection status (010 = PLMN not allowed) is written to all three
        (EPSLOCI status 02 = roaming not allowed; C3a spec model).
        `keep_plmn` keeps the last visited TAI PLMN in the EPSLOCI dummy
        (NMR style) instead of wiping it."""
        self.write_binary('loci', build_loci_dummy(self.plmn, status),
                          label='loci', optional=True)
        self.write_binary('psloci', build_psloci_dummy(self.plmn, status),
                          label='psloci', optional=True)
        self.write_binary('epsloci',
                          build_epsloci_dummy(status, keep_plmn=keep_plmn),
                          label='epsloci', optional=True)

    def invalidate_kc(self):
        for key in ('kc', 'kcgprs'):
            try:
                self._open(key)
            except StepError as e:
                self._add('skip', file=key, note=str(e))
                continue
            size = self.lchan.selected_file_size() or 9
            self.write_binary(key, build_kc_invalidate(size), pad=False,
                              label=key)

    def write_real_kc(self):
        kc = self.p('kc') or rand_hex(8)
        algo = int(self.p('kc_algo', 1))
        for key in ('kc', 'kcgprs'):
            try:
                self._open(key)
            except StepError as e:
                self._add('skip', file=key, note=str(e))
                continue
            size = self.lchan.selected_file_size() or 9
            self.write_binary(key, build_kc(kc, algo, size), pad=False,
                              label=key)

    def bump_sms_counter(self):
        try:
            self._open('smsstatus')
        except StepError as e:
            self._add('skip', file='smsstatus', note=str(e))
            return
        data, sw = self.read_binary_current()
        if sw != '9000':
            data = ''
        self.write_binary('smsstatus', bump_smsstatus(data), label='smsstatus')

    # -- scenarios (UICC_NAA.md section 13)

    def sc_cold_boot(self):
        self.invalidate_epsnsc(keep_key=False)
        if self.p('dummy_locations', True):
            self.write_dummy_locations()

    def sc_attach_eps(self):
        self.store_epsnsc()
        self.write_real_locations(ST_UPDATED)

    def sc_service_lost(self):
        if self.p('send_event', True):
            self.send_location_status(self.status(LOC_STATUS_NO_SERVICE))
        if self.p('invalidate_epsnsc', True):
            self.invalidate_epsnsc(keep_key=bool(self.p('keep_kasme', True)))
        if self.p('dummy_locations', True):
            self.write_dummy_locations()
        if self.p('write_kc', True):
            self.invalidate_kc()

    def sc_limited_service(self):
        if self.p('send_event', True):
            self.send_location_status(self.status(LOC_STATUS_LIMITED))
        if self.p('invalidate_epsnsc', True):
            self.invalidate_epsnsc(keep_key=bool(self.p('keep_kasme', True)))
        if self.p('dummy_locations', True):
            self.write_dummy_locations()

    def sc_roaming_denied(self):
        # Permanent rejection (NAS cause #11): the Location status event is
        # indistinguishable from limited service (TS 102 223 8.27) - the
        # difference is the 010 status bytes and the EF.FPLMN entry
        # (UICC_NAA.md C3a).
        if self.p('send_event', True):
            self.send_location_status(self.status(LOC_STATUS_LIMITED))
        if self.p('invalidate_epsnsc', True):
            self.invalidate_epsnsc(keep_key=False)
        if self.p('dummy_locations', True):
            self.write_dummy_locations(status=self.status(ST_PLMN_NOT_ALLOWED))
        if self.p('write_fplmn', True):
            self.write_fplmn(self.plmn)

    def sc_churn(self):
        count = int(self.p('churn_count', 3))
        delay = int(self.p('churn_delay_ms', 150)) / 1000.0
        real = build_epsnsc(
            _hexint(self.p('ksi', '01'), 0x01),
            self.kasme,
            _hexint(self.p('ul', 0)), _hexint(self.p('dl', 0)),
            _hexint(self.p('algo', '02'), 0x02))
        try:
            self._open('epsnsc')
        except StepError as e:
            self._add('skip', file='epsnsc', note=str(e))
            return
        size = self.lchan.selected_file_record_len() or 54
        invalid = build_epsnsc_invalidate(size, self.kasme)
        for _i in range(max(1, count)):
            self.write_record('epsnsc', real, label='epsnsc')
            if delay:
                self.sleep(delay)
            self.write_record('epsnsc', invalid, label='epsnsc')
            if delay:
                self.sleep(delay)

    def sc_attach_2g(self):
        self.write_real_kc()
        self.write_real_locations(ST_UPDATED)

    def sc_sms_received(self):
        self.bump_sms_counter()
        if self.p('sms_location', True):
            self.write_real_locations(ST_UPDATED)

    def sc_cb_reconfig(self):
        clear = bool(self.p('cb_clear', False))
        try:
            self._open('cbmi')
        except StepError as e:
            self._add('skip', file='cbmi', note=str(e))
            clear = None
        if clear is not None:
            size = self.lchan.selected_file_size()
            if clear:
                self.write_binary('cbmi', 'FF' * (size or 20), pad=False,
                                  label='cbmi')
            else:
                ids = self.p('cbmi_ids') or [0x111F, 0x1112]
                self.write_binary('cbmi', build_cbmi(ids, size), pad=False,
                                  label='cbmi')
        try:
            self._open('cbmir')
        except StepError:
            return
        size = self.lchan.selected_file_size()
        if clear:
            self.write_binary('cbmir', 'FF' * (size or 40), pad=False,
                              label='cbmir')
        else:
            ranges = self.p('cbmir_ranges') or [[0x111F, 0x111F],
                                                [0x1112, 0x1112]]
            self.write_binary('cbmir', build_cbmir(ranges, size), pad=False,
                              label='cbmir')

    def sc_authenticate(self):
        rand = self.p('rand') or rand_hex(16)
        autn = self.p('autn') or rand_hex(16)
        if not self.scc:
            raise StepError('card session not available')
        apdu = build_auth_apdu(rand, autn)
        data, sw = self.scc._tp.send_apdu(apdu)
        if sw.startswith('61'):
            data, sw = self.scc._tp.send_apdu('00C00000' + sw[2:4])
        parsed = parse_auth_response(data)
        self._add('authenticate', data=apdu, response=(data or '').upper(),
                  parsed=parsed, sw=sw, ok=True)

    SCENARIOS = {
        'cold_boot': sc_cold_boot,
        'attach_eps': sc_attach_eps,
        'service_lost': sc_service_lost,
        'limited_service': sc_limited_service,
        'roaming_denied': sc_roaming_denied,
        'churn': sc_churn,
        'attach_2g': sc_attach_2g,
        'sms_received': sc_sms_received,
        'cb_reconfig': sc_cb_reconfig,
        'authenticate': sc_authenticate,
    }

    def run(self, scenario):
        fn = self.SCENARIOS.get(scenario)
        if not fn:
            raise ValueError('unknown scenario %r' % scenario)
        error = None
        try:
            fn(self)
        except StepError as e:
            error = str(e)
        except Exception as e:
            error = '%s: %s' % (type(e).__name__, e)
        finally:
            self._cleanup()
        return {'scenario': scenario, 'success': error is None, 'error': error,
                'steps': self.steps}


def run_scenario(srv, app, scenario, params=None, event_list=None,
                 sleep=time.sleep):
    """Run `scenario` against the equipped card; returns the step log."""
    runner = NetSimRunner(srv, app, params=params, event_list=event_list,
                          sleep=sleep)
    return runner.run(scenario)
