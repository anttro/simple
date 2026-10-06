import json
import copy
import random
import sys
import os
import time
import threading
import traceback
import re
import codecs
import zlib
from urllib.parse import unquote_plus
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler
from io import StringIO
from pySim.transport import ApduTracer, ProactiveHandler
from pySim.cards import UiccCardBase
from pysim_simple_server import httpota
from pysim_simple_server import netsim
from pysim_simple_server import netstate
from pysim_simple_server import scp81
from pysim_simple_server import esim
from pysim_simple_server import capmem
from pysim_simple_server import testscript
from pysim_simple_server import presets
from pysim_simple_server import test_scripts
from pysim_simple_server import test_suites
from pysim_simple_server import events
from smartcard.CardMonitoring import CardMonitor, CardObserver
from cmd2.exceptions import CommandSetRegistrationError

import gsm0338  # registers 'gsm03.38' codec
from construct import GreedyBytes
from osmocom.construct import GsmOrUcs2Adapter
from osmocom.tlv import BER_TLV_IE
from osmocom.utils import rpad


VERSION = '3.22.2'

MAX_ENVELOPE_SEGMENTS = 5  # max SMS segments for outgoing C-APDU in ENVELOPE

# --- SCP80 SMS concatenation (TS 31.115 4.2/4.3) -------------------------
#
# SMS user data budget: 140 octets.  A packet that fits one SM carries only
# the CPI IE in the UDH; a concatenated command carries the concatenation IE
# (5 octets) in every SM plus the CPI IE (2 octets) in the first one, so the
# payload capacities differ.  All figures include the UDHL octet.
SCP80_SINGLE_BYTES = 137    # 140 - UDHL(1) - CPI IE(2)
SCP80_FIRST_BYTES = 132     # 140 - UDHL(1) - concat IE(5) - CPI IE(2)
SCP80_NEXT_BYTES = 134      # 140 - UDHL(1) - concat IE(5)
# Fixed concatenation reference (TS 23.040 9.2.3.24.1): the server is the
# only sender and only one concatenated message is ever in flight, so there
# are no segments of another message to tell apart.
SCP80_CONCAT_REF = 0x01
# Segment cap: the card's concatenation buffer is limited (5-7 SMs is
# typical for UICCs), so a longer packet could never be reassembled anyway.
SCP80_MAX_SEGMENTS = MAX_ENVELOPE_SEGMENTS




# Static file serving (the PWA lives in <repo>/frontend, served by this server
# so the UI and the API share an origin and no CORS/PNA is involved).
_STATIC_MIME = {
    '.html': 'text/html; charset=utf-8',
    '.css': 'text/css',
    '.js': 'application/javascript',
    '.json': 'application/json',
    '.png': 'image/png',
    '.svg': 'image/svg+xml',
    '.ico': 'image/x-icon',
    '.webmanifest': 'application/manifest+json',
    '.map': 'application/json',
    '.wasm': 'application/wasm',
    '.woff': 'font/woff',
    '.woff2': 'font/woff2',
}


_T0 = time.time()
_TIMING = False
_APDU_N = 0
_RESET_N = 0

def _timing_on():
    global _TIMING
    _TIMING = True

def _tlog(msg):
    if not _TIMING:
        return
    sys.stderr.write('TIMING [+%7.3fs] %s\n' % (time.time() - _T0, msg))


class _LineFilter:
    """Text stream that drops whole lines matching any of the given substrings
    and forwards everything else to the wrapped stream. Used to mute pySim/
    pySim-shell internals we report ourselves (e.g. 'Waiting for card...' or
    'pySim-shell not equipped!'). Lines arrive through write(); the dropped
    lines are written as one call each by pySim's print/logger and by cmd2's
    Rich console, so a substring check per line is reliable here."""

    def __init__(self, stream, patterns):
        self._stream = stream
        self._patterns = list(patterns)
        self._pending = ''

    def write(self, text):
        text = self._pending + text
        self._pending = ''
        if not text:
            return
        if not text.endswith('\n'):
            # Keep a trailing partial line so a match is not missed when the
            # line is completed by the next write().
            nl = text.rfind('\n')
            if nl < 0:
                self._pending = text
                return
            self._pending = text[nl + 1:]
            text = text[:nl + 1]
        for line in text.splitlines(True):
            if not any(p in line for p in self._patterns):
                self._stream.write(line)
        self._stream.flush()

    def flush(self):
        if self._pending:
            line, self._pending = self._pending, ''
            if not any(p in line for p in self._patterns):
                self._stream.write(line)
        self._stream.flush()

    def __getattr__(self, name):
        # encoding/isatty/fileno: the Rich console probes these on the stream
        # (cmd2's self.stdout), so they must keep working.
        return getattr(self._stream, name)


_APDU_TIMES = []
_APDU_TIME_COLLECT = False

def _classify_apdu(cmd):
    """Map a command APDU to a snapshot timing category by instruction byte."""
    if not cmd or len(cmd) < 4:
        return None
    return {'A4': 'select', 'B0': 'read_binary', 'B2': 'read_record'}.get(cmd[2:4].upper())


def _collect_apdu_times():
    """Start collecting per-command times. (Re)attaches our tracer if pySim
    nulled it (equip does). Callers hold _CARD_LOCK, so collection cannot be
    interleaved by the background poll thread."""
    global _APDU_TIME_COLLECT
    scc = getattr(_server_ref, 'scc', None) if _server_ref else None
    tp = getattr(scc, '_tp', None) if scc else None
    if tp is not None and tp.apdu_tracer is None:
        tp.apdu_tracer = _LoggingApduTracer()
    _APDU_TIMES.clear()
    _APDU_TIME_COLLECT = True


def _end_apdu_time_collection():
    """Stop collecting and return the collected [{type, ms}, ...] list."""
    global _APDU_TIME_COLLECT
    _APDU_TIME_COLLECT = False
    times = list(_APDU_TIMES)
    _APDU_TIMES.clear()
    return times


class StderrApduTracer(ApduTracer):
    def __init__(self):
        super().__init__()
        self._cmd_start = 0

    def trace_command(self, cmd):
        self._cmd_start = time.time()

    def trace_reset(self):
        global _RESET_N
        _RESET_N += 1
        if _TIMING:
            sys.stderr.write('TIMING [+%7.3fs] RESET #%d\n' % (time.time() - _T0, _RESET_N))

    def trace_response(self, cmd, sw, resp):
        global _APDU_N
        _APDU_N += 1
        elapsed = int((time.time() - self._cmd_start) * 1000)
        if _APDU_TIME_COLLECT:
            category = _classify_apdu(cmd)
            if category:
                _APDU_TIMES.append({'type': category, 'ms': elapsed})
        if _TIMING:
            msg = 'APDU-TRACE(+%7.3fs #%d, %dms): %s → SW: %s' % (time.time() - _T0, _APDU_N, elapsed, cmd, sw)
        else:
            msg = 'APDU-TRACE(%dms): %s → SW: %s' % (elapsed, cmd, sw)
        if resp:
            msg += ' RESP: %s' % resp
        os.write(2, (msg + '\n').encode())


class _LoggingApduTracer(StderrApduTracer):
    """StderrApduTracer that additionally records the SW of TERMINAL RESPONSE
    APDUs sent by pySim's auto-handler (which happen outside our own chain code)
    onto the most recent log entry that is still missing its tr_sw."""

    def trace_response(self, cmd, sw, resp):
        super().trace_response(cmd, sw, resp)
        if len(cmd) >= 4 and cmd[2:4] == '14':
            for entry in reversed(_PROACTIVE_LOG):
                if 'tr_hex' in entry and 'tr_sw' not in entry:
                    entry['tr_sw'] = sw
                    break


ERROR_MSGS = {
    'en': {
        'app_not_init': 'Server not initialized',
        'no_card_state': 'No card state available',
        'reader_not_init': 'Reader not initialized',
        'not_found': 'Not found',
        'not_an_euicc': 'The equipped card is not an eUICC',
    },
    'ru': {
        'app_not_init': 'Сервер не инициализирован',
        'no_card_state': 'Состояние карты недоступно',
        'reader_not_init': 'Считыватель не инициализирован',
        'not_found': 'Не найдено',
        'not_an_euicc': 'Подключённая карта — не eUICC',
    },
}


def _get_lang(headers):
    lang = headers.get('Accept-Language', 'en')
    if lang not in ('en', 'ru'):
        lang = 'en'
    return lang


def _err(key, lang):
    return ERROR_MSGS.get(lang, ERROR_MSGS['en']).get(key, key)


def _strip_ansi(text):
    return re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', text)


def _parse_help_text(text):
    result = {'usage': '', 'description': '', 'args': []}
    lines = text.split('\n')
    in_usage = False
    in_pos = False
    in_opt = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith('usage:'):
            result['usage'] = stripped[6:].strip()
            in_usage = True
            in_pos = in_opt = False
        elif stripped.startswith('positional arguments:'):
            in_pos = True
            in_opt = in_usage = False
        elif stripped.startswith('options:') or stripped.startswith('optional arguments:'):
            in_opt = True
            in_pos = in_usage = False
        elif in_pos and stripped and not stripped.startswith('usage:'):
            m = re.match(r'^(\S+)\s+(.+)$', stripped)
            if m:
                result['args'].append({'name': m.group(1), 'type': 'positional', 'help': m.group(2)})
        elif in_opt and stripped and not stripped.startswith('usage:'):
            m = re.match(r'^(\S+(?:,\s*\S+)?)\s+(\S+\s+)?(.+)?$', stripped)
            if m:
                names = m.group(1)
                first_name = names.split(',')[0].strip()
                result['args'].append({'name': first_name, 'type': 'optional', 'help': (m.group(3) or '').strip()})
        elif not in_pos and not in_opt and not in_usage:
            if stripped:
                result['description'] = (result['description'] + ' ' + stripped).strip()
    return result


def _get_file_type(lchan, cur_file):
    try:
        if cur_file and cur_file.name:
            if cur_file.name.startswith('EF.'):
                if lchan and lchan.selected_file_fcp:
                    ft = lchan.selected_file_type()
                    if ft != 'df':
                        return lchan.selected_file_structure()
                return 'transparent'
            if cur_file.name.startswith('DF.') or cur_file.name.startswith('ADF.') or cur_file.name == 'MF':
                return 'df'
        if lchan and lchan.selected_file_fcp:
            return lchan.selected_file_structure()
    except Exception:
        # No usable FCP (an ADF, or a failed select while the card has no
        # active profile) - report no file type instead of crashing.
        return None
    return None


def _fcp_value(lchan, name):
    """One FCP-derived selection value, or None when the card delivered no
    usable FCP (an ADF, or a failed select) - never raises."""
    try:
        return getattr(lchan, name)()
    except Exception:
        return None


def _fid4(sel):
    """True if sel is a 4-digit hex FID."""
    return bool(re.fullmatch(r'[0-9a-fA-F]{4}', str(sel or '')))


def _file_by_sel(parent, sel):
    """Resolve sel (FID or symbolic name, case-insensitive) among parent's direct children."""
    if parent is None or not sel:
        return None
    s = str(sel).strip().lower()
    for f in (getattr(parent, 'children', None) or {}).values():
        if f.fid and f.fid.lower() == s:
            return f
        if f.name and f.name.lower() == s:
            return f
    return None


def _app_by_sel(rs, sel):
    """Resolve an ADF by AID or application name (case-insensitive)."""
    if rs is None or not sel:
        return None
    s = str(sel).strip().lower()
    for aid, adf in (rs.mf.applications or {}).items():
        if aid.lower() == s or (adf.name and adf.name.lower() == s):
            return adf
    return None


def _find_in_tree(root, sel):
    """All model files matching sel (fid or name) below root; unique-match helper."""
    s = str(sel or '').strip().lower()
    found = []
    seen = set()
    stack = [root]
    while stack:
        cur = stack.pop()
        if id(cur) in seen:
            continue
        seen.add(id(cur))
        candidates = list((getattr(cur, 'children', None) or {}).values())
        candidates += list((getattr(cur, 'applications', None) or {}).values())
        for f in candidates:
            if (f.fid and f.fid.lower() == s) or (f.name and f.name.lower() == s):
                found.append(f)
            stack.append(f)
    return found


def _select_with_parent(lchan, name, parent_sel, app, parent_path=None, allow_probe=False):
    """Select name strictly within the requested parent.

    Every model-known FID/name is resolved through the parent's children and
    selected with lchan.select_file(); pySim's global selectables and its
    probe_file() fallback are never used for model files, so a same-FID file
    under a different parent can no longer be picked and the model is not
    mutated. Model-unknown 4-hex segments (custom files) are probed only when
    allow_probe is set and are detached again via the returned cleanup.

    Returns (selected_file, cleanup): cleanup is None unless a probe happened;
    handlers must call it in a finally block after using the selection.
    """
    rs = app.rs
    prev = lchan.selected_file
    probes = []
    parent = rs.mf
    lchan.select_file(parent, app)
    segs = [s for s in (parent_path or ([parent_sel] if parent_sel else [])) if s]
    for seg in segs:
        if str(seg).upper() in ('MF', '3F00'):
            continue
        f = _app_by_sel(rs, seg) or _file_by_sel(parent, seg)
        if f is None and not parent_path:
            matches = _find_in_tree(rs.mf, seg)
            if len(matches) > 1:
                raise RuntimeError('Ambiguous parent selector: %s' % seg)
            if matches:
                f = matches[0]
        if f is None:
            if allow_probe and _fid4(seg):
                fid = str(seg).lower()
                probes.append((parent, fid))
                lchan.probe_file(fid, app)
                parent = lchan.selected_file
                continue
            raise RuntimeError('File not found: %s' % seg)
        lchan.select_file(f, app)
        parent = f
    target = None
    if str(name).upper() in ('MF', '3F00'):
        target = rs.mf
    if target is None:
        target = _file_by_sel(parent, name) or _app_by_sel(rs, name)
    if target is None and not parent_path and not parent_sel:
        matches = _find_in_tree(rs.mf, name)
        if len(matches) > 1:
            raise RuntimeError('Ambiguous file selector: %s' % name)
        if matches:
            target = matches[0]
    if target is not None:
        lchan.select_file(target, app)
    elif allow_probe and _fid4(name):
        fid = str(name).lower()
        probes.append((parent, fid))
        lchan.probe_file(fid, app)
    else:
        raise RuntimeError('File not found: %s' % name)
    cleanup = None
    if probes:
        def cleanup():
            for p, fid in reversed(probes):
                try:
                    (getattr(p, 'children', None) or {}).pop(fid, None)
                except Exception:
                    pass
            try:
                lchan.select_file(prev, app)
            except Exception:
                try:
                    lchan.select_file(rs.mf, app)
                except Exception:
                    pass
    return lchan.selected_file, cleanup


def _select_path(lchan, path, app):
    """Select a file described by a full path.

    Path is '/' separated; the first element is 'MF' (or '3F00'), an ADF AID,
    or an ADF name; remaining elements are FIDs or file names. Resolution is
    strictly parent-scoped (see _select_with_parent); unknown 4-hex segments
    are custom files and are probed without touching the model tree.
    """
    parts = [p for p in (path or '').split('/') if p]
    if not parts:
        raise RuntimeError('Empty path')
    return _select_with_parent(lchan, parts[-1], None, app, parent_path=parts[:-1], allow_probe=True)


def _decode_iccid(data_hex):
    """Decode EF.ICCID content: nibble-swapped E.118 digits with an optional
    trailing 'F' pad (TS 102 221 13.2 / TS 151 011 10.2). Returns the digit
    string, or None when the bytes are not a plausible ICCID."""
    h = re.sub(r'[^0-9a-fA-F]', '', data_hex or '').upper()
    if len(h) < 2 or len(h) % 2:
        return None
    digits = ''.join(h[i + 1] + h[i] for i in range(0, len(h), 2))
    digits = re.sub(r'F+$', '', digits)
    if not digits or not digits.isdigit():
        return None
    return digits


def _read_iccid(app):
    """Best-effort EF.ICCID (MF/2FE2) read: the E.118 digit string or None.

    EF.ICCID is a mandatory transparent EF, but a card may protect it or the
    generic profile may lack it, so the read is optional and never raises.
    The previous selection is restored by the _select_path cleanup."""
    if not app or not getattr(app, 'rs', None):
        return None
    lchan = app.rs.lchan[0]
    cleanup = None
    try:
        _, cleanup = _select_path(lchan, 'MF/2FE2', app)
        data, _sw = lchan.read_binary()
        return _decode_iccid(data)
    except Exception:
        return None
    finally:
        if cleanup:
            cleanup()


_MCC_MNC_CACHE = {'path': None, 'data': None}


def _mcc_mnc_load(path):
    """Load the optional MCC/MNC operator list (JSON) once; None if absent."""
    if not path:
        return None
    if _MCC_MNC_CACHE['path'] == path:
        return _MCC_MNC_CACHE['data']
    data = None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            loaded = json.load(f)
        if isinstance(loaded, list):
            data = loaded
    except Exception:
        data = None
    _MCC_MNC_CACHE['path'] = path
    _MCC_MNC_CACHE['data'] = data
    return data


def _mcc_mnc_is_mvno(e):
    """True for MVNO entries: `bands` marks them (e.g. 'MVNO', 'Satellite MVNO').

    MVNOs do not operate their own radio network, so the network-simulation
    picker hides them; the full list is kept for operator-name resolution.
    """
    return 'mvno' in str(e.get('bands') or '').lower()


def _mcc_mnc_search(data, query, limit=50):
    """Compact substring search over country/brand/operator/MCC/MNC."""
    q = (query or '').strip().lower()
    if not q:
        return []
    out = []
    for e in data or []:
        if _mcc_mnc_is_mvno(e):
            continue
        hay = ' '.join(str(e.get(k) or '') for k in
                       ('countryName', 'countryCode', 'mcc', 'mnc', 'brand', 'operator')).lower()
        if q not in hay:
            continue
        out.append({k: e.get(k) for k in
                    ('countryName', 'countryCode', 'mcc', 'mnc', 'brand', 'operator', 'status')})
        if len(out) >= limit:
            break
    return out


def _mcc_mnc_random(data, exclude=None):
    """One random operator entry (optionally excluding 'mccmnc' digits)."""
    pool = [e for e in (data or [])
            if (str(e.get('mcc') or '') + str(e.get('mnc') or '')) != (exclude or '')
            and not _mcc_mnc_is_mvno(e)]
    if not pool:
        pool = [e for e in (data or []) if not _mcc_mnc_is_mvno(e)]
    if not pool:
        return None
    e = random.choice(pool)
    return {k: e.get(k) for k in
            ('countryName', 'countryCode', 'mcc', 'mnc', 'brand', 'operator', 'status')}


def _netstate_read(app, keys=None):
    """Read the monitored network-state EFs (best effort per file).

    The Network state panel caches them: the full set is read once at equip
    (after a readable ICCID), on demand via /api/net-state-refresh, and only
    the card-side files (EF.IMSI) after net-sim / Location-status operations.
    """
    rs = getattr(app, 'rs', None) if app else None
    if not app or not rs:
        return {}
    lchan = rs.lchan[0]
    out = {}
    for d in netstate.FILE_DEFS:
        key = d['key']
        if keys and key not in keys:
            continue
        entry = {'name': d['name'], 'fid': d['fid'], 'present': False,
                 'kind': None, 'source': 'refresh', 'updated': time.time()}
        for path in d['paths']:
            cleanup = None
            try:
                _, cleanup = _select_path(lchan, path, app)
                ft = _get_file_type(lchan, lchan.selected_file)
                entry['path'] = path
                if ft in ('linear_fixed', 'cyclic'):
                    records = []
                    n = lchan.selected_file_num_of_rec() or 1
                    for i in range(1, n + 1):
                        rv = lchan.read_record(i)
                        data = rv[0] if isinstance(rv, tuple) else rv
                        records.append({'num': i,
                                        'data': (data or '').upper()})
                    entry.update({'present': True, 'kind': 'record',
                                  'records': records})
                else:
                    rv = lchan.read_binary()
                    data = rv[0] if isinstance(rv, tuple) else rv
                    entry.update({'present': True, 'kind': 'transparent',
                                  'data': (data or '').upper()})
                break
            except Exception:
                continue
            finally:
                if cleanup:
                    try:
                        cleanup()
                    except Exception:
                        pass
        out[key] = entry
    return out


def _netstate_compute(server):
    state = getattr(server, 'net_state', None)
    if state is None:
        return None
    data = _mcc_mnc_load(getattr(server, 'mcc_mnc_path', None))
    return netstate.compute_network(state, data)


def _netstate_install(server, files, source='init'):
    """Install a freshly read monitor state (None/{} = nothing readable)."""
    state = netstate.new_state()
    netstate.merge_read(state, files or {}, source=source)
    netstate.set_read_time(state)
    server.net_state = state
    _netstate_compute(server)


def _netstate_init(server):
    """Create the monitor state from a fresh read (best effort)."""
    try:
        _netstate_install(server, _netstate_read(server.app))
    except Exception as e:
        server.net_state = None
        _tlog('network state read failed: %s' % e)


def _netstate_ensure(server):
    """Monitor state of the current session, created on demand for equip
    paths that predate it (e.g. the startup init)."""
    state = getattr(server, 'net_state', None)
    if state is None and getattr(server, 'iccid', None):
        _netstate_init(server)
        state = getattr(server, 'net_state', None)
    return state


def _netstate_after_net_sim(server, scenario, result):
    """Update the cached Network state after a scenario run: the bytes we
    wrote are known without re-reading; the card may update EF.IMSI itself."""
    state = _netstate_ensure(server)
    if state is None:
        return
    netstate.apply_steps(state, (result or {}).get('steps') or [])
    service = netsim.SCENARIO_SERVICE.get(scenario)
    if service and (result or {}).get('success'):
        netstate.set_service(state, service, 'net-sim:' + scenario)
    netstate.merge_read(state, _netstate_read(server.app, ['imsi']),
                        source='read')
    _netstate_compute(server)


def _netstate_after_event(server, event_type, event_data):
    """Location status changes the simulated service state; the card may also
    switch EF.IMSI (multi-IMSI applets) after such an event."""
    state = _netstate_ensure(server)
    if state is None:
        return
    try:
        ev = int(event_type)
    except (TypeError, ValueError):
        ev = None
    if ev == netsim.EVENT_LOCATION_STATUS and event_data:
        status = None
        if (len(event_data) >= 3 and event_data[0] == 0x9B
                and event_data[1] == 0x01):
            status = event_data[2]
        service = {0x00: netstate.SERVICE_NORMAL,
                   0x01: netstate.SERVICE_LIMITED,
                   0x02: netstate.SERVICE_NONE}.get(status)
        if service:
            netstate.set_service(state, service, 'event:location-status')
    netstate.merge_read(state, _netstate_read(server.app, ['imsi']),
                        source='read')
    _netstate_compute(server)


def _parse_tree_output(output):
    lines = (output or '').split('\n')
    children = []
    for line in lines:
        if line.startswith(' '):
            continue
        m = re.match(r'^(\S+)\s+([0-9a-fA-F]{4})?(?:\s|$)', line)
        if m:
            cname = m.group(1)
            cfid = m.group(2).lower() if m.group(2) else None
            children.append({
                'name': cname,
                'fid': cfid,
                'isDir': cname.startswith('ADF.') or cname.startswith('DF.') or cname == 'MF',
            })
    return children


def _encode_sms_oa(number):
    digits = [int(c) for c in number if c.isdigit()]
    oa = bytes([len(digits), 0x81])
    for i in range(0, len(digits), 2):
        first = digits[i]
        second = digits[i + 1] if i + 1 < len(digits) else 0xF
        oa += bytes([(second << 4) | first])
    return oa


def _bcd_pair(value):
    return ((value % 10) << 4) | ((value // 10) % 10)


def _encode_scts(dt=None):
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


def _single_ef_value(hex_str):
    """The value of a lone EF (System Specific Parameters) TLV, or None when
    the input is not EF-form at all.  A blob that starts with the EF tag but
    does not parse as exactly one TLV is refused - appending it could add a
    second EF object (TS 102 226 8.2.1.3.2.1), and a bare CA is refused too:
    the CA belongs *inside* the EF."""
    if not hex_str:
        return None
    try:
        data = bytes.fromhex(hex_str)
    except ValueError:
        raise ValueError('stk_params is not valid hex')
    if data[0] == 0xCA:
        raise ValueError('the CA (SIM file access and toolkit parameters) '
                         'belongs inside the System Specific Parameters EF '
                         '(TS 102 226 8.2.1.3.2.1): send EF <len> CA ...')
    if data[0] != 0xEF:
        return None
    if len(data) < 2:
        raise ValueError('stk_params: malformed EF TLV (the length byte is '
                         'missing)')
    first = data[1]
    if first == 0x80:
        raise ValueError('stk_params: malformed EF TLV (indefinite length)')
    if first >= 0x80 and len(data) < 2 + (first & 0x7F):
        raise ValueError('stk_params: malformed EF TLV (truncated length)')
    ln, off = _ber_len_at(data, 1)
    if off + ln != len(data):
        raise ValueError('stk_params: malformed EF TLV (the length does not '
                         'match the value)')
    return data[off:off + ln].hex().upper()


def _quota_tlv(tag, value):
    """A C7/C8 memory-quota TLV: the value is a 2-byte integer up to 32767
    and a 4-byte integer above (GP Card Spec 9.7 / Table 11-49; pySim's
    ``StripHeaderAdapter(GreedyBytes, 4, steps=[2,4])``).  An integral float
    is accepted (a JSON client may send ``200.0``)."""
    try:
        num = float(str(value).strip())
        if not num.is_integer():
            raise ValueError
        v = int(num)
    except (TypeError, ValueError):
        raise ValueError('%s quota must be an integer in bytes' % tag)
    if not 0 <= v <= 0xFFFFFFFF:
        raise ValueError('%s quota out of range (0..4294967295): %d' % (tag, v))
    return tag + ('%02X%04X' % (2, v) if v <= 0x7FFF else '%02X%08X' % (4, v))


def _quota_provided(value):
    """Whether a memory-quota field was given: ``None`` and blank strings
    mean "not requested", while ``0`` is a valid quota (the vendor reference
    form carried ``C7 02 0000 C8 02 0000``), so an integral zero must be
    composed, not dropped."""
    return value is not None and str(value).strip() != ''


def _compose_install_params(install_params, stk_params, nv_quota=None,
                            volatile_quota=None):
    """The INSTALL [for install] parameter field, with the composition the
    spec mandates enforced:

    - the memory quotas (C7 volatile / C8 non-volatile) and an EF-form STK
      part (the CA) share **one** System Specific Parameters EF (TS 102 226
      8.2.1.3.2.1; GPD_SPE_013 v1.1 Table 6-5 - pySim's
      ``gen_install_parameters``);
    - an EF-form STK part next to a caller-composed EF is refused: the card
      takes the first EF (quotas) and the CA is ignored - live 2026-10-05:
      por_ok but no toolkit registration, no menu;
    - an EA-form (or opaque) STK part is a sibling of EF (the reference TCA
      loader sends ``C9 00 EF 00 EA ...``);
    - the quotas are composed from the primitives only when no
      ``install_params`` were given (there is no EF to place them into
      otherwise).

    Raises ValueError (-> HTTP 400) on a refusal.
    """
    raw = (install_params or '').replace(' ', '')
    stk = (stk_params or '').replace(' ', '')
    has_quota = _quota_provided(nv_quota) or _quota_provided(volatile_quota)
    stk_ef = _single_ef_value(stk)
    if raw:
        if has_quota:
            raise ValueError('memory quotas are composed only when '
                             'install_params is empty; merge them into the '
                             'install EF instead')
        try:
            tlvs = _ber_tlv_list(bytes.fromhex(raw))
        except ValueError:
            raise ValueError('install_params is not valid hex')
        if stk_ef is not None and any(tag == 0xEF for tag, _ in tlvs):
            raise ValueError('the EF-form STK part would add a second System '
                             'Specific Parameters (EF) object (TS 102 226 '
                             '8.2.1.3.2.1): merge the CA into the install '
                             'parameters\' EF, or pass the quotas via '
                             'volatile_quota/nv_quota with an empty '
                             'install_params')
        return (raw + stk).upper()
    # Compose from the primitives: C9 is mandatory (GP Table 11-49).
    inner = ''
    if _quota_provided(volatile_quota):
        inner += _quota_tlv('C7', volatile_quota)
    if _quota_provided(nv_quota):
        inner += _quota_tlv('C8', nv_quota)
    if stk_ef is not None:
        inner += stk_ef
        stk = ''
    params = 'C900'
    if inner:
        params += 'EF' + _ber_len(len(inner) // 2) + inner
    return (params + stk).upper()


def _cap_install_apdu(loadfile_aid, module_aid, instance_aid='', privileges='00',
                      install_params='', stk_params='', make_selectable=True,
                      nv_quota=None, volatile_quota=None):
    """INSTALL [for install] APDU (GP Card Spec 11.5.2.3.2, Table 11-43) - the
    final step of `_cap_apdu_sequence` and the /api/ram-install-app operation.
    Case 3: no trailing Le (a trailing byte becomes a phantom command on the
    card's SCP80 layer)."""
    instance = instance_aid or module_aid
    params = _compose_install_params(install_params, stk_params,
                                     nv_quota=nv_quota,
                                     volatile_quota=volatile_quota)
    p1 = 0x0C if make_selectable else 0x04
    data = (_lv(loadfile_aid) + _lv(module_aid) + _lv(instance) +
            _lv(privileges or '00') + _lv(params) + '00')
    return '80E6%02X00%02X%s' % (p1, len(data) // 2, data)


def _cap_make_selectable_apdu(instance_aid, privileges='00'):
    """INSTALL [for make selectable] APDU (GP Card Spec 11.5.2.3.3, Table
    11-44): '00' '00' lv(AID) lv(privileges) lv(params) lv(token); the
    parameters and token are empty here.  Case 3 (no Le)."""
    data = '00' + '00' + _lv(instance_aid) + _lv(privileges or '00') + '00' + '00'
    return '80E60800%02X%s' % (len(data) // 2, data)


def _cap_apdu_sequence(loadfile_aid, module_aid, loadfile_data, sd_aid='',
                       privileges='00', install_params='', stk_params='',
                       make_selectable=True, block_size=240, instance_aid=None,
                       nv_quota=None, volatile_quota=None):
    """RAM (GP) APDU sequence for a parsed .cap: INSTALL [for load], LOAD
    blocks (240-byte payloads, block counter in P2, last block P1=0x80),
    INSTALL [for install]. Shared by the SCP80 delivery path and the SCP81
    command-script path; keep byte-compatible with /api/ram-install."""
    # INSTALL/LOAD APDUs follow the reference terminal form: the Security
    # Domain AID is conditional (GP Card Spec v2.3.1 Table 11-42) and is only
    # sent when one was supplied (empty -> '00', the card defaults to the
    # ISD), and the commands are case 3 (no trailing Le).  A trailing Le made
    # the card execute an extra (phantom) command whose SW 6700 masked the
    # real result and, with the remote-SW check, aborted the install.
    ifl_data = _lv(loadfile_aid) + (_lv(sd_aid) if sd_aid else '00') + '00' + '00' + '00'
    apdus = ['80E60200%02X%s' % (len(ifl_data) // 2, ifl_data)]
    loadfile_tlv = 'C4' + _ber_len(len(loadfile_data) // 2) + loadfile_data
    # Split the TLV into consecutive 240-byte blocks (char offsets, 2 per
    # byte). The earlier form indexed with the block number ('i * 2'), which
    # produced overlapping 1-byte-shifted copies - the card failed mid-load
    # with SW 6400 (live 2026-09-16).
    blocks = [loadfile_tlv[off:off + block_size * 2]
              for off in range(0, len(loadfile_tlv), block_size * 2)]
    for i, block in enumerate(blocks):
        p1 = 0x80 if i == len(blocks) - 1 else 0x00
        apdus.append('80E8%02X%02X%02X%s' % (p1, i % 256, len(block) // 2, block))
    apdus.append(_cap_install_apdu(
        loadfile_aid, module_aid, instance_aid=instance_aid,
        privileges=privileges, install_params=install_params,
        stk_params=stk_params, make_selectable=make_selectable,
        nv_quota=nv_quota, volatile_quota=volatile_quota))
    return apdus


def _lv(hex_str):
    """Length-prefix a hex string (1-byte length)."""
    n = len(hex_str) // 2
    return '%02x%s' % (n, hex_str)


def _ber_len(n):
    """BER-TLV length encoding."""
    if n < 0x80:
        return '%02x' % n
    elif n < 0x100:
        return '81%02x' % n
    else:
        return '82%04x' % n


def _cap_parse(cap_hex):
    """Parse a .cap file (as hex string) and return (loadfile_aid, module_aid, loadfile_data) as hex strings.

    The .cap file is a ZIP archive containing nested .cap component files.
    We extract the Header (for package AID / Load File AID) and Applet component
    (for applet AID / Module AID), then concatenate all components for the loadfile data.
    """
    import zipfile, io, struct

    cap_bytes = bytes.fromhex(cap_hex)
    zf = zipfile.ZipFile(io.BytesIO(cap_bytes))
    components = {}
    for name in zf.namelist():
        if name.lower().endswith('.cap') and not name.lower().endswith('.capx'):
            key = name.split('/')[-1].removesuffix('.cap')
            components[key] = zf.read(name)
    zf.close()

    if 'Header' not in components:
        raise ValueError('.cap file missing Header component')
    if 'Applet' not in components:
        raise ValueError('.cap file missing Applet component')

    # Header component: tag(1) size(2) magic(4) minor(1) major(1) flags(1) package(minor(1) major(1) aid(LV))
    hdr = components['Header']
    magic = struct.unpack('>I', hdr[3:7])[0]
    if magic != 0xDECAFFED:
        raise ValueError('Invalid .cap Header magic: 0x%08X (expected 0xDECAFFED)' % magic)
    aid_len = hdr[12]
    loadfile_aid = hdr[13:13 + aid_len].hex().upper()

    # Applet component: tag(1) size(2) count(1) [aid_len(1) aid(N) install_offset(2)]*
    app = components['Applet']
    num_applets = app[3]
    if num_applets < 1:
        raise ValueError('.cap file has no applets')
    off = 4
    aid_len2 = app[off]
    module_aid = app[off + 1:off + 1 + aid_len2].hex().upper()

    # Concatenate all components for the loadfile (GP spec order)
    order = ['Header', 'Directory', 'Import', 'Applet', 'Class', 'Method',
             'StaticField', 'Export', 'ConstantPool', 'RefLocation', 'Descriptor']
    loadfile_parts = []
    for comp in order:
        if comp in components:
            loadfile_parts.append(components[comp])
    loadfile_data = b''.join(loadfile_parts).hex().upper()

    return loadfile_aid, module_aid, loadfile_data


def _version_pair(text):
    """'x.y' -> (minor, major) as the CAP stores an import version."""
    try:
        major, minor = str(text).strip().split('.')
        major, minor = int(major), int(minor)
    except (TypeError, ValueError):
        raise ValueError('invalid version %r (expected major.minor)' % (text,))
    if not (0 <= major <= 255 and 0 <= minor <= 255):
        raise ValueError('version out of range: %r' % (text,))
    return minor, major


def _cap_import_end(load_file_hex):
    """Offset just past the Import component's last byte in the load file."""
    data = bytes.fromhex(load_file_hex or '')
    off = 0
    while off + 3 <= len(data):
        tag = data[off]
        size = int.from_bytes(data[off + 1:off + 3], 'big')
        if tag == 0x04:
            return off + 3 + size
        off += 3 + size
    raise ValueError('no Import component in the load file')


def _cap_import_aids(load_file_hex):
    """The AIDs of the load file's Import component (upper-case hex)."""
    data = bytes.fromhex(load_file_hex or '')
    off = 0
    while off + 3 <= len(data):
        tag = data[off]
        size = int.from_bytes(data[off + 1:off + 3], 'big')
        if tag == 0x04:
            start = off + 3
            end = start + size
            out = []
            p = start + 1
            for _ in range(data[start]):
                if p + 3 > end:
                    break
                aid_len = data[p + 2]
                if p + 3 + aid_len > end:
                    break
                out.append(bytes(data[p + 3:p + 3 + aid_len]).hex().upper())
                p += 3 + aid_len
            return out
        off += 3 + size
    return []


def _probe_unmatched(probe, known):
    """Import-probe override AIDs that matched no Import entry: the CAP does
    not import them, so the card is never asked about them.  Returns
    [{'aid': hex, 'version': 'x.y'}] (the version the probe requested), sorted
    by AID; the 'all' override is skipped."""
    out = []
    for key, version in (probe or {}).items():
        k = str(key).replace(' ', '')
        if k.lower() == 'all':
            continue
        if k.upper() in known:
            continue
        out.append({'aid': k.upper(), 'version': str(version)})
    out.sort(key=lambda e: e['aid'])
    return out


def _cap_compat_blocks(load_file_hex, block_size):
    """(boundary, total) LOAD block numbers for the compatibility test.

    `boundary` is the block whose payload completes the Import component: the
    JCRE verifies the import list when that component ends (the observed LOAD
    rejections land exactly in that block), so a test stopping after it
    answers the compatibility question without committing a load file.
    `total` is the block count of the full load (for the report)."""
    import_end = _cap_import_end(load_file_hex)
    tlv = 'C4' + _ber_len(len(load_file_hex) // 2) + load_file_hex
    header_chars = len(tlv) - len(load_file_hex)          # the C4 TLV header
    last_byte = header_chars + (import_end - 1) * 2       # Import's last byte
    boundary = last_byte // (block_size * 2) + 1
    total = (len(tlv) + block_size * 2 - 1) // (block_size * 2)
    return boundary, total


def _probe_import_versions(load_file_hex, overrides, additions=None):
    """Diagnostic probe: rewrite (and optionally append) the Import component
    of the load file (JC VM spec 4.5.2 - the card must export every imported
    package at >= the recorded version).

    The JCRE checks the import list while it verifies the load file, so a card
    that cannot satisfy one of the CAP's export versions rejects the LOAD as
    soon as the Import component is complete (live 2026-09-29: remote SW
    6438/6985 exactly in the block containing the component's last byte).
    Lowering the versions shows whether that list is the cause and, by
    bisection, which package the card refuses; appending a synthetic entry
    (`additions`) queries the card for a package/version the CAP does not use.

    `overrides` is {'all': 'x.y'} or {aid_hex: 'x.y'}; `additions` is a list
    of {'aid': hex, 'version': 'x.y'} appended as new entries (append-only:
    existing entries keep their positions, so the applet's constant-pool
    import indices stay valid - an AID that is already present is treated as a
    version override).  A version-only probe keeps the load file length
    untouched; appends patch the component's size field and the Directory's
    Import size.  Returns (patched_hex, applied) where `applied` lists the
    entries changed or added ('from' is null for an appended entry).
    Diagnostic only - never used for a real install."""
    data = bytearray.fromhex(load_file_hex or '')
    if not data:
        raise ValueError('empty load file')

    norm = {}
    for key, value in (overrides or {}).items():
        k = str(key).replace(' ', '')
        norm['all' if k.lower() == 'all' else k.upper()] = value

    # Walk the components to the Import one (tag 0x04; sizes exclude the
    # 3-byte component header).
    start = None
    off = 0
    while off + 3 <= len(data):
        tag = data[off]
        size = int.from_bytes(data[off + 1:off + 3], 'big')
        if tag == 0x04:
            start = off + 3
            end = start + size
            break
        off += 3 + size
    if start is None:
        raise ValueError('no Import component in the load file')
    if end > len(data):
        raise ValueError('truncated Import component')

    # parse the entries once (bounds-checked)
    entries = []
    p = start + 1
    for _ in range(data[start]):
        if p + 3 > end:
            raise ValueError('truncated Import entry')
        minor, major, aid_len = data[p], data[p + 1], data[p + 2]
        if p + 3 + aid_len > end:
            raise ValueError('truncated Import entry AID')
        aid_hex = bytes(data[p + 3:p + 3 + aid_len]).hex().upper()
        entries.append([minor, major, aid_hex])
        p += 3 + aid_len

    matched = 0
    applied = []
    for entry in entries:
        override = norm.get(entry[2], norm.get('all'))
        if override is None:
            continue
        matched += 1
        new_minor, new_major = _version_pair(override)
        if (new_minor, new_major) != (entry[0], entry[1]):
            applied.append({'aid': entry[2],
                            'from': '%d.%d' % (entry[1], entry[0]),
                            'to': '%d.%d' % (new_major, new_minor)})
            entry[0], entry[1] = new_minor, new_major

    known = {e[2] for e in entries}
    for item in (additions or []):
        aid = str(item.get('aid', '')).replace(' ', '').upper()
        if not aid or len(aid) % 2 or not re.fullmatch(r'[0-9A-F]+', aid):
            raise ValueError('invalid AID in the additions: %r' % (item.get('aid'),))
        if not 10 <= len(aid) <= 32:
            raise ValueError('addition AID must be 5..16 bytes: %s' % aid)
        new_minor, new_major = _version_pair(item.get('version'))
        matched += 1
        if aid in known:
            # already imported: apply as a version override
            for entry in entries:
                if entry[2] == aid and (entry[0], entry[1]) != (new_minor, new_major):
                    applied.append({'aid': aid,
                                    'from': '%d.%d' % (entry[1], entry[0]),
                                    'to': '%d.%d' % (new_major, new_minor)})
                    entry[0], entry[1] = new_minor, new_major
                    break
            continue
        known.add(aid)
        entries.append([new_minor, new_major, aid])
        applied.append({'aid': aid, 'from': None,
                        'to': '%d.%d' % (new_major, new_minor)})

    if not matched:
        raise ValueError('no import matched the probe (give AIDs from the '
                         'CAP analysis, "all" or "+AID=version" additions)')

    rebuilt = bytes([len(entries)]) + b''.join(
        bytes([e[0], e[1], len(e[2]) // 2]) + bytes.fromhex(e[2]) for e in entries)
    if rebuilt != bytes(data[start:end]):
        # sizes: the component header field and the Directory's array entry
        # (index 3 = tag 4) carry the Import's data length
        new_size = len(rebuilt)
        if new_size > 0xFFFF:
            raise ValueError('Import component too large')
        data[start - 2:start] = new_size.to_bytes(2, 'big')
        off = 0
        while off + 3 <= len(data):
            tag = data[off]
            size = int.from_bytes(data[off + 1:off + 3], 'big')
            if tag == 0x02:                      # Directory: sizes by tag
                entry = off + 3 + 3 * 2          # index 3 = tag 4 (Import)
                if entry + 2 <= off + 3 + size:
                    data[entry:entry + 2] = new_size.to_bytes(2, 'big')
                break
            off += 3 + size
        data[start:end] = rebuilt
    return data.hex().upper(), applied


def _build_sms_tpdu(chunk_hex, chunk_total=1, chunk_num=1, oa_number='12345', include_cpi=True,
                    chunk_ref=SCP80_CONCAT_REF):
    chunk = bytes.fromhex(chunk_hex)
    # TS 23.040 UDH: first octet is UDHL, then the information elements.
    # TS 31.115 4.2/4.3: a concatenated command carries the concatenation IE
    # in every SM and the OTA CPI (IEIa='70', IEIDLa='00') in the first one.
    udh = b''
    if chunk_total > 1:
        udh = bytes([0x00, 0x03, chunk_ref, chunk_total, chunk_num])
        if chunk_num == 1 and include_cpi:
            udh += bytes([0x70, 0x00])
    elif include_cpi:
        udh = bytes([0x70, 0x00])
    tp_ud = (bytes([len(udh)]) + udh + chunk) if udh else chunk
    # TP-UDL counts the whole user data (UDHL octet + UDH + payload) and the
    # SMS limit is 140 octets; the splitter sizes every part for its own UDH.
    budget = 140 - len(udh) - (1 if udh else 0)
    if len(chunk) > budget:
        raise ValueError('SMS user data overflow: %d > %d octets' % (len(chunk), budget))
    first_byte = 0x44 if udh and chunk_total > 1 else (0x40 if udh else 0x04)
    tpdu = bytes([first_byte]) + _encode_sms_oa(oa_number) + bytes([0x7F, 0xF6]) + _encode_scts() + bytes([len(tp_ud)]) + tp_ud
    return tpdu.hex()


def _send_envelope(tpdu_hex, scc, sm_sc='12345678912', submit_handler=None,
                   handle_proactive=True, poll_status=True):
    from pySim.ts_31_102 import SMSPPDownload
    from pySim.cat import DeviceIdentities, Address
    from osmocom.tlv import COMPR_TLV_IE
    from pySim.utils import b2h

    class RawTpdu(COMPR_TLV_IE, tag=0x8B):
        comprehension = False
        def __init__(self, data_hex):
            super().__init__()
            self._raw = bytes.fromhex(data_hex)
        def to_bytes(self, context={}):
            return self._raw

    # Handler for a proactive chain that carries the PoR: the ENVELOPE may
    # answer 9000 and the card delivers SEND SHORT MESSAGE on a later exchange
    # (the STATUS poll below).  v3.6.37: this used to be defined inside the
    # 91xx branch, so the poll path raised UnboundLocalError and the RAM
    # operation died mid-packet with the command left pending (live 500).
    def _capture_sms_tpdu(raw, cmd_num, cmd_type, dev_src, dev_dst):
        if submit_handler:
            tpdu_hex = _find_sms_tpdu(raw)
            if tpdu_hex:
                ref, total, num, payload = _parse_sms_concat(bytes.fromhex(tpdu_hex))
                if total is not None and num is not None:
                    submit_handler.sms_segments.append((ref, total, num, payload.hex()))
                    matching = [s for s in submit_handler.sms_segments if s[0] == ref]
                    if len(matching) >= total:
                        sorted_segs = sorted(matching, key=lambda s: s[2])
                        assembled = b''.join(bytes.fromhex(s[3]) for s in sorted_segs)
                        submit_handler.submit_ud_hex = assembled.hex()
                        submit_handler.submit_tpdu_hex = submit_handler.submit_tpdu_hex or tpdu_hex
                        sys.stderr.write('SMS concat: assembled %d segments (ref=%s, %d B)\n' % (
                            total, ref, len(assembled)))
                else:
                    submit_handler.submit_ud_hex = payload.hex()
                    submit_handler.submit_tpdu_hex = tpdu_hex

    address = Address()
    oa_raw = _encode_sms_oa(sm_sc)
    address.from_bytes(oa_raw[1:])

    dev_ids = DeviceIdentities(decoded={'source_dev_id': 'network', 'dest_dev_id': 'uicc'})
    raw_tpdu = RawTpdu(tpdu_hex)
    sms_dl = SMSPPDownload(children=[dev_ids, address, raw_tpdu])
    env_hex = '%sc20000%02x%s' % (scc.cat_cla, len(sms_dl.to_tlv()), b2h(sms_dl.to_tlv()))
    data, sw = scc._tp.send_apdu(env_hex)
    if sw.startswith('61'):
        get_len = int(sw[2:], 16) if len(sw) == 4 else 0x100
        data, sw = scc._tp.send_apdu('00c00000%02x' % get_len)
    elif handle_proactive and sw.startswith('91'):
        _handle_proactive_chain(scc, sw, _capture_sms_tpdu)
        data, sw = '', '9000'
    # Late-PoR fallback: only after the last segment of a packet (a
    # concatenated download cannot have produced its PoR before the card got
    # every part, and polling mid-packet made spurious STATUS exchanges).
    if (handle_proactive and poll_status and submit_handler
            and not submit_handler.submit_tpdu_hex and not data
            and (sw == '9000' or sw[:2] in ('62', '63'))):
        # A warning answer (the live card answers 6200 to a low-counter
        # packet) can hold the PoR too: poll once so the card's verdict is
        # captured instead of a bare 'ENVELOPE failed' (v3.6.58).
        sys.stderr.write('STATUS poll (PoR not captured)\n')
        st_data, st_sw = _send_status(scc)
        sys.stderr.write('STATUS -> %s\n' % st_sw)
        if st_sw.startswith('91'):
            _handle_proactive_chain(scc, st_sw, _capture_sms_tpdu)
    return data, sw


# TS 03.48 / TS 102 225 SPI coding
_RC_CC_DS = {0: 'no_rc_cc_ds', 1: 'rc', 2: 'cc', 3: 'ds'}
_CNTR_REQ = {0: 'no_counter', 1: 'counter_no_replay_or_seq', 2: 'counter_must_be_higher', 3: 'counter_must_be_lower'}
_POR_REQ = {0: 'no_por', 1: 'por_required', 2: 'por_only_when_error'}
_CRYPT_ALGO = {1: 'single_des', 5: 'triple_des_cbc2', 9: 'triple_des_cbc3', 2: 'aes_cbc'}
_AUTH_ALGO = {1: 'single_des', 5: 'triple_des_cbc2', 9: 'triple_des_cbc3', 2: 'aes_cmac'}


def _spi_from_bytes(spi1, spi2):
    return {
        'counter': _CNTR_REQ[(spi1 >> 3) & 0x03],
        'ciphering': bool(spi1 & 0x04),
        'rc_cc_ds': _RC_CC_DS[spi1 & 0x03],
        'por_in_submit': bool(spi2 & 0x20),
        'por_shall_be_ciphered': bool(spi2 & 0x10),
        'por_rc_cc_ds': _RC_CC_DS[(spi2 >> 2) & 0x03],
        'por': _POR_REQ[spi2 & 0x03],
    }


def _ota_keyset(spi1, spi2, kic, kid, cntr_hex, kic_key_hex, kid_key_hex):
    from pySim.ota import OtaKeyset
    from osmocom.utils import h2b
    kic_b = int(kic, 16)
    kid_b = int(kid, 16)
    spi = _spi_from_bytes(int(spi1, 16), int(spi2, 16))
    algo_crypt = _CRYPT_ALGO.get(kic_b & 0x0F)
    algo_auth = _AUTH_ALGO.get(kid_b & 0x0F)
    # TS 102 225 A.2: '00' is a valid KIc value when no ciphering is applied
    # (SPI1.b3=0) and a valid KID value when no RC/CC/DS is applied
    # (SPI1.b2b1=00) - a no-security packet carries no algorithm at all.  The
    # algorithm is only *needed* when it is actually used, so an unknown nibble
    # is refused only then and a placeholder is passed to pySim otherwise (its
    # key is never touched).
    needs_crypt = spi['ciphering'] or spi['por_shall_be_ciphered']
    needs_auth = (spi['rc_cc_ds'] != 'no_rc_cc_ds'
                  or spi['por_rc_cc_ds'] != 'no_rc_cc_ds')
    if algo_crypt is None:
        if needs_crypt or (kic_b & 0x0F):
            raise ValueError('Unsupported KIc algorithm nibble %02X' % (kic_b & 0x0F))
        # 'implicit' codes the nibble as 0, which is exactly the spec's value
        algo_crypt = 'implicit'
    if algo_auth is None:
        if needs_auth or (kid_b & 0x0F):
            raise ValueError('Unsupported KID algorithm nibble %02X' % (kid_b & 0x0F))
        algo_auth = 'implicit'
    return OtaKeyset(algo_crypt=algo_crypt, kic_idx=kic_b >> 4, kic=h2b(kic_key_hex),
                     algo_auth=algo_auth, kid_idx=kid_b >> 4, kid=h2b(kid_key_hex),
                     cntr=int(cntr_hex, 16) if cntr_hex else 0)


def _counter_tracked(spi1_hex):
    """True when the packet asks the card to check the counter (TS 102 225
    5.1.1, SPI1.b5b4 = 01/10/11).  With b5b4 = 00 the field is "present,
    ignored, never updated" - the card keeps no replay value for the packet,
    so no counter may be advanced or persisted (a keyless SPI1 0x00 send must
    leave every preset counter untouched)."""
    try:
        return ((int(spi1_hex, 16) >> 3) & 0x03) != 0
    except (TypeError, ValueError):
        return False


def _ota_reference(spi1, spi2, kic, kid, tar_hex, cntr_hex, apdu_hex, kic_key_hex, kid_key_hex):
    from pySim.ota import OtaDialectSms
    from osmocom.utils import h2b, b2h
    otak = _ota_keyset(spi1, spi2, kic, kid, cntr_hex, kic_key_hex, kid_key_hex)
    spi = _spi_from_bytes(int(spi1, 16), int(spi2, 16))
    out = OtaDialectSms().encode_cmd(otak, h2b(tar_hex), spi, h2b(apdu_hex))
    if not spi['ciphering'] and (spi['rc_cc_ds'] != 'no_rc_cc_ds'
                                 or len(out) > SCP80_SINGLE_BYTES):
        # pySim drops the CPL octets from its unciphered output; re-add them
        # per TS 31.115 4.2 (they are part of the RC/CC/DS input) and per
        # Table 1 NOTE / 4.3 (required for concatenation - the CHL-to-end
        # range exceeds one SM).
        # CPL counts octets from the CHL octet to the last octet of the
        # Secured Data (incl. padding); pySim's unciphered output is exactly
        # that range, so the CPL value equals its length.
        cpl = len(out)
        out = cpl.to_bytes(2, 'big') + out
    return b2h(out), spi


def _split_secured_packet(pkt, include_cpi=True):
    """Split a command packet into SMS user-data parts (TS 31.115 4.3).

    Every part plus its UDH stays within the 140-octet SMS user data; the
    first part of a concatenated command is smaller because its UDH also
    carries the CPI IE."""
    single_max = SCP80_SINGLE_BYTES if include_cpi else 140
    if len(pkt) <= single_max:
        return [pkt]
    first = SCP80_FIRST_BYTES if include_cpi else SCP80_NEXT_BYTES
    parts = [pkt[:first]]
    rest = pkt[first:]
    while rest:
        parts.append(rest[:SCP80_NEXT_BYTES])
        rest = rest[SCP80_NEXT_BYTES:]
    return parts


def _encode_cmd_unlimited(otak, spi, tar, apdu):
    """SCP80 command packet (TS 102 225 5.1.1 / TS 31.115 4.2) of any size.

    pySim's OtaDialectSms.encode_cmd refuses packets above 140 octets
    ("Fragmentation not implemented") - exactly the packets that need SMS
    concatenation.  The coding is identical, so the packet is built here
    with pySim's key material and header constructor and without the length
    limit; the tests pin our output to pySim's byte-for-byte for packets
    that fit one SMS (/api/sp-verify keeps using pySim as the reference)."""
    from pySim.ota import OtaDialectSms
    dialect = OtaDialectSms()
    len_sig = dialect._compute_sig_len(spi)
    pad_cnt = 0
    apdu = bytes(apdu)
    if spi['ciphering']:
        # Append padding bytes to end up with blocksize.
        len_cipher = 6 + len_sig + len(apdu)
        padding = otak.crypt._get_padding(len_cipher, otak.crypt.blocksize)
        pad_cnt = len(padding)
        apdu += padding

    kic = {'key': otak.kic_idx, 'algo': otak.algo_crypt}
    kid = {'key': otak.kid_idx, 'algo': otak.algo_auth}
    # CHL = octets from (and including) SPI to the end of RC/CC/DS:
    # 13 == SPI(2) + KIc(1) + KID(1) + TAR(3) + CNTR(5) + PCNTR(1).
    chl = 13 + len_sig
    part_head = dialect.hdr_construct.build({'chl': chl, 'spi': spi, 'kic': kic,
                                             'kid': kid, 'tar': tar})
    part_cnt = otak.cntr.to_bytes(5, 'big') + pad_cnt.to_bytes(1, 'big')
    envelope_data = part_head + part_cnt + apdu
    cpl = len(envelope_data) + len_sig
    envelope_data = cpl.to_bytes(2, 'big') + envelope_data

    if spi['rc_cc_ds'] == 'cc':
        cc = otak.auth.sign(envelope_data)
        envelope_data = part_cnt + cc + apdu
    elif spi['rc_cc_ds'] == 'rc':
        crc32 = zlib.crc32(envelope_data) & 0xffffffff
        envelope_data = part_cnt + crc32.to_bytes(4, 'big') + apdu
    elif spi['rc_cc_ds'] == 'no_rc_cc_ds':
        envelope_data = part_cnt + apdu
    else:
        raise ValueError('Invalid rc_cc_ds: %s' % spi['rc_cc_ds'])

    if spi['ciphering']:
        ciph = otak.crypt.encrypt(envelope_data)
        envelope_data = part_head + ciph
        cpl = len(envelope_data)
        envelope_data = cpl.to_bytes(2, 'big') + envelope_data
    else:
        envelope_data = part_head + envelope_data
    return envelope_data


def _build_secured_packet(spi1, spi2, kic, kid, tar_hex, cntr_hex, apdu_hex,
                          kic_key_hex, kid_key_hex):
    """SCP80 command packet of any size; returns (hex, spi).

    The CPL fix-up for the unciphered case mirrors _ota_reference: pySim
    drops the CPL octets there, but they are part of the RC/CC/DS
    calculation (TS 31.115 4.2)."""
    from osmocom.utils import h2b, b2h
    otak = _ota_keyset(spi1, spi2, kic, kid, cntr_hex, kic_key_hex, kid_key_hex)
    spi = _spi_from_bytes(int(spi1, 16), int(spi2, 16))
    out = _encode_cmd_unlimited(otak, spi, h2b(tar_hex), h2b(apdu_hex))
    if not spi['ciphering'] and (spi['rc_cc_ds'] != 'no_rc_cc_ds'
                                 or len(out) > SCP80_SINGLE_BYTES):
        # CPL counts octets from the CHL octet to the last octet of the
        # Secured Data (incl. padding) - exactly the length of the
        # unciphered range.  Added for the RC/CC/DS input and for
        # concatenation (TS 31.115 Table 1 NOTE / 4.3).
        cpl = len(out)
        out = cpl.to_bytes(2, 'big') + out
    return b2h(out), spi


def _send_secured_packet(scc, sp_hex, oa_number, sm_sc=None, include_cpi=True,
                         submit_handler=None, max_segments=SCP80_MAX_SEGMENTS,
                         handle_proactive=True):
    """Send a secured packet as SMS-PP download ENVELOPEs, one per segment.

    TS 31.115 4.3: the whole command packet is split into SMS user-data
    parts (the first SM carries the concatenation IE plus the CPI IE, the
    following ones only the concatenation IE) and the card reassembles them.
    Returns a dict with success/bytes/segments/sw/response_data or error."""
    try:
        pkt = bytes.fromhex(sp_hex or '')
    except ValueError as e:
        return {'success': False, 'bytes': 0, 'segments': 0,
                'error': 'Invalid secured packet: %s' % e}
    if not pkt:
        return {'success': False, 'bytes': 0, 'segments': 0,
                'error': 'Empty secured packet'}
    parts = _split_secured_packet(pkt, include_cpi=include_cpi)
    total = len(parts)
    if total > max_segments:
        return {'success': False, 'bytes': len(pkt), 'segments': total,
                'error': 'Secured packet too large: %d segments (max %d - the '
                         'card concatenation buffer)' % (total, max_segments)}
    data = None
    sw = None
    sys.stderr.write('OTA SEND: %d SMS segment(s), %d bytes\n' % (total, len(pkt)))
    for i, part in enumerate(parts):
        try:
            tpdu = _build_sms_tpdu(part.hex(), total, i + 1, oa_number=oa_number,
                                   include_cpi=include_cpi)
        except ValueError as e:
            return {'success': False, 'bytes': len(pkt), 'segments': total,
                    'error': str(e)}
        if total > 1:
            sys.stderr.write('OTA SEND: ENVELOPE %d/%d (%d B)%s\n' % (
                i + 1, total, len(part), ' + CPI' if i == 0 and include_cpi else ''))
        data, sw = _send_envelope(tpdu, scc, sm_sc=sm_sc or '12345678912',
                                  submit_handler=submit_handler,
                                  handle_proactive=handle_proactive,
                                  poll_status=(i == total - 1))
        sys.stderr.write('OTA SEND: ENVELOPE %d/%d -> %s\n' % (i + 1, total, sw))
        if sw != '9000' and not sw.startswith('91'):
            # A warning (62xx/63xx, e.g. the live card's 6200 for a low
            # counter) may still hold the PoR: pass it on so the caller can
            # name the card's verdict instead of a bare ENVELOPE failure.
            late_por = _sms_submit_por(submit_handler) if submit_handler else ''
            return {'success': False, 'sw': sw, 'bytes': len(pkt),
                    'segments': total,
                    'response_data': (data or late_por or None),
                    'error': 'ENVELOPE failed at segment %d' % (i + 1)}
    return {'success': True, 'bytes': len(pkt), 'segments': total, 'sw': sw,
            'response_data': data if data else None}


# Remote-command status words that count as success in the RAM dialog:
# 9000 = normal; 61xx = more data available (GET RESPONSE); 62xx/63xx =
# warnings (63xx/63Cx often carry "more data available" too); CAFE =
# GlobalPlatform "more data available" (GET STATUS pages).
_RAM_REMOTE_SW_OK = ('9000', 'CAFE')


def _ram_remote_sw_ok(sw):
    s = str(sw or '').upper()
    return bool(s) and (s in _RAM_REMOTE_SW_OK or s[:2] in ('61', '62', '63'))


def _por_remote_sw(por):
    """Last remote-command status word from a decoded PoR, '' when none:
    compact responses carry last_status_word, expanded ones a status word
    per command."""
    dec = (por or {}).get('decoded') or {}
    sw = str(dec.get('last_status_word') or '').upper()
    if sw:
        return sw
    for r in reversed((por or {}).get('responses') or []):
        if r.get('status_word'):
            return str(r['status_word']).upper()
    return ''


def _cntr_low_fields(status):
    """The card's `cntr_low` verdict as a flag - the packet's counter was not
    above the card's for that key version - or {} for any other verdict.

    The card's own counter is deliberately not reported: the response
    packet's CNTR is "a copy of the contents of the CNTR in the Command
    Packet" (TS 102 225 §5.2, Table 3) and a cntr_low rejection leaves it
    zeroed, so no usable value can be derived from it (v3.6.58).  A low
    counter is not recoverable by retrying: the operation stops and the user
    raises the preset counter above the card's (the `counter-probe` endpoint
    can do that in bounded steps)."""
    if str(status or '') != 'cntr_low':
        return {}
    return {'cntr_low': True}


def _ram_next_cntr(cntr, advance):
    """Advance the SCP80 counter by one only when the card accepted the
    packet (PoR ok / no PoR expected): a rejected packet (cntr_low,
    rc_cc_ds_failed, ...) leaves the card's expectation and the preset
    counter untouched.  The counter is 5 bytes (40 bits); a 32-bit wrap
    dropped the top byte of high counters (v3.6.24)."""
    if not advance:
        return cntr
    return '%010X' % ((int(cntr, 16) + 1) % (2 ** 40))


def _counter_valid(cntr):
    """True when an SCP80 counter is usable (1-10 hex digits).  A caller
    sending a pre-built packet may have no counter at all - the empty value
    must never reach int() (v3.8.0 review fix)."""
    return bool(re.fullmatch(r'[0-9A-Fa-f]{1,10}', str(cntr or '')))


def _kvn_of(kic_hex, kid_hex):
    """The keyset number a packet uses: the b8..b5 nibble of KIc/KID (TS 102 225
    5.1.2/A.2).  Returns ``(kvn, error)``: kvn is 0 when neither byte carries
    one ('00' = no security, legal per A.2), error names a mismatch - A.2: the
    versions shall be identical when different from 0, else the card rejects
    the message - or a malformed byte."""
    kic = str(kic_hex or '').strip().upper()
    kid = str(kid_hex or '').strip().upper()
    for name, value in (('KIc', kic), ('KID', kid)):
        if value and not re.fullmatch(r'[0-9A-F]{2}', value):
            return None, '%s must be one hex byte: %r' % (name, value)
    kic_v = int(kic, 16) >> 4 if kic else 0
    kid_v = int(kid, 16) >> 4 if kid else 0
    if kic_v and kid_v and kic_v != kid_v:
        return None, ('KIc and KID must carry the same keyset number '
                      '(TS 102 225 A.2): %s / %s' % (kic, kid))
    return (kic_v or kid_v), None


def _preset_keyset_check(server, preset_id, kic, kid):
    """The "the keyset number must be defined in the preset" rule: an error
    string when the packet's (non-zero) keyset number has no keyset in the
    named preset, else None.  Without a preset_id (a raw API caller) there is
    nothing to check; '00'/'00' (no security) needs no keyset either."""
    if not preset_id:
        return None
    kvn, err = _kvn_of(kic, kid)
    if err:
        return err
    if not kvn:
        return None
    store = getattr(server, 'card_presets', None)
    if store is None:
        return None
    if store.find_keyset(preset_id, kvn) is None:
        preset = store.get(preset_id) or {}
        return ('keyset %d is not defined in preset %s - add it in the '
                'Cards tab' % (kvn, preset.get('name') or preset_id))
    return None


def _preset_counter_persist(server, preset_id, cntr, source, kvn=None):
    """Persist the counter an operation accepted into the server-side preset
    store (v3.8.0), into the keyset it used (each keyset has its own dedicated
    counter, TS 102 225 A.1).  The counter is
    card state, so the server writes it with the operation that consumed it - a
    closed tab, a lost response or a second browser window can no longer lose
    an increment.  The store itself is monotonic (a stale value never regresses
    it).  A keyset number is mandatory: a packet with no number (KIc/KID
    '00', or a SPI1 without a counter check) has no counter to write.  Best
    effort: a store failure must never fail the card operation."""
    if not preset_id or not cntr or not kvn:
        return
    store = getattr(server, 'card_presets', None)
    if store is None:
        return
    try:
        store.set_counter(preset_id, cntr, source, kvn)
    except Exception as e:
        sys.stderr.write('PRESETS: counter persist failed (%s): %s\n' % (source, e))


def _preset_by_id(server, preset_id):
    """The named card preset, or None (no store configured / unknown id)."""
    store = getattr(server, 'card_presets', None)
    if store is None or not preset_id:
        return None
    return store.get(preset_id)


def _preset_counter_seed(server, body, kvn):
    """The effective SCP80 counter for an operation: an explicit `cntr` in the
    request wins (a hand send or a probe may deliberately pick one); otherwise
    the counter the preset store holds for the packet's keyset - the operation
    persists the next value on acceptance, so the store *is* the next counter
    (v3.20.0).  '' when neither is available (the packet then carries 0 and
    nothing is tracked)."""
    explicit = str((body or {}).get('cntr') or '').strip().upper()
    if explicit:
        return explicit
    preset = _preset_by_id(server, (body or {}).get('preset_id'))
    if preset is None or not kvn:
        return ''
    keyset = next((ks for ks in _preset_keysets(preset)
                   if presets.keyset_kvn(ks) == kvn), None)
    return str((keyset or {}).get('cntr') or '').strip().upper()


def _hex_lt(a, b):
    """Numeric ``a < b`` for two hex byte strings; invalid input is never a
    comparison (the packet builder's own validation reports it)."""
    try:
        return int(a, 16) < int(b, 16)
    except (TypeError, ValueError):
        return False


def _spi1_for_tar(preset, tar_hex, explicit='', prefer_msl=False):
    """Resolve the SPI1 of a packet addressed to `tar_hex` from the preset's
    TAR table.

    Each TAR carries its **MSL** (Minimum SPI1, TS 102 226 8.2.1.3.2.4): the
    card checks it before the security processing and answers response status
    0A "Insufficient security level" when the packet's SPI1 is below it, so the
    MSL is the natural value for packets to that TAR.

    ``prefer_msl=False`` (a hand send): the caller's explicit SPI1 wins - the
    operator may deliberately send more security than the minimum - and a value
    below the TAR's MSL only produces a warning.  ``prefer_msl=True`` (an
    operation): the TAR's MSL wins; the explicit value is the fallback for a
    TAR the preset does not carry (e.g. the TAR probe's checklist).  Raises
    ValueError when neither exists - guessing would either be refused by the
    card or send more security than the application expects.
    Returns ``(spi1, warning)``."""
    explicit = str(explicit or '').strip().upper()
    tar = str(tar_hex or '').strip().upper()
    msl = presets.tar_msl(preset, tar)
    if prefer_msl and msl:
        return msl, ''
    if explicit:
        warning = ''
        if msl and _hex_lt(explicit, msl):
            warning = ('SPI1 %s is below the MSL %s of TAR %s - the card may '
                       'answer 0A (insufficient security level)'
                       % (explicit, msl, tar))
        return explicit, warning
    if msl:
        return msl, ''
    if not tar:
        raise ValueError('no TAR for the packet and no spi1 given - pass the '
                         'TAR (add its MSL to the card preset) or spi1')
    raise ValueError('no MSL for TAR %s in the card preset - set it in the '
                     'Cards tab or pass spi1' % tar)


# RAM command formats (TS 102 226 5.2.1): the bare C-APDU (compact) or the
# Command TLV '22' inside the 'AA' scripting template (expanded) - the form
# the reference terminal traces use.  The format is detected per operation
# and never stored: cards differ batch to batch and may behave differently
# later, so every operation re-checks (v3.6.24).
_RAM_PROBE_APDU = '80F28000024F0000C0000000'   # GET STATUS [ISD], read-only
# The expanded format does not use GET RESPONSE (TS 102 226 5.2.1.1): with
# Le='00' the whole response comes back in the R-APDU, so the expanded probes
# drop the chained GET RESPONSE of the compact form.
_RAM_PROBE_APDU_LE = '80F28000024F0000'


def _wrap_expanded_apdu(apdu_hex, form='definite'):
    """Wrap a C-APDU in the expanded remote-management format (TS 102 226
    5.2.1): the Command TLV '22' inside the Command Scripting template.

    form='definite'   -> `AA <len> 22 <len> <apdu>` (TS 101 220 table 7.19
                         definite length coding; the reference trace's form
                         `AA0A220880F24000024F0000`)
    form='indefinite' -> `AE 80 22 <len> <apdu> 00 00` (indefinite length
                         coding, the variant the spec recommends for RAM/RFM
                         over HTTPS; some cards accept only this one)."""
    cmd = '22' + _ber_len(len(apdu_hex) // 2) + apdu_hex
    if form == 'indefinite':
        return ('AE80' + cmd + '0000').upper()
    return ('AA' + _ber_len(len(cmd) // 2) + cmd).upper()


def _ram_format_apdu(apdu_hex, ram_format):
    """Apply the RAM command format (TS 102 226 5.2.1): the bare C-APDU
    (compact), the definite-length scripting template (expanded) or the
    indefinite-length one (expanded-ae)."""
    if ram_format == 'expanded':
        return _wrap_expanded_apdu(apdu_hex, 'definite')
    if ram_format == 'expanded-ae':
        return _wrap_expanded_apdu(apdu_hex, 'indefinite')
    return apdu_hex


def _ram_normalize_format(value):
    """'auto' | 'compact' | 'expanded' | 'expanded-ae'; anything else (or
    missing) is 'auto' for callers that opted in to the format handling."""
    v = str(value or 'auto').strip().lower()
    return v if v in ('auto', 'compact', 'expanded', 'expanded-ae') else 'auto'


def _ram_detect_format(server, scc, sp, state):
    """Detect the card's RAM command format for one operation: send the
    read-only GET STATUS [ISD] probe compact, then in the two expanded
    codings; the first format whose remote SW succeeds wins (compact
    preferred).  Cards implement different subsets: the reference TCA loader
    sends the listing queries wrapped and unchained, while the live card needs
    the compact chain (a length-mismatched template draws a Bad format TLV,
    which the step result names).  Probe packets are recorded as steps and
    consume counters only when accepted;
    the result is used for this operation only - the next operation re-checks
    (v3.6.24)."""
    for fmt, apdu in (('compact', _RAM_PROBE_APDU),
                      ('expanded', _wrap_expanded_apdu(_RAM_PROBE_APDU_LE, 'definite')),
                      ('expanded-ae', _wrap_expanded_apdu(_RAM_PROBE_APDU_LE, 'indefinite'))):
        scratch = {'steps': [], 'encode_error': None, 'failure': {},
                   'cntr': state['cntr'], 'preset_id': state.get('preset_id')}
        ok = _ram_send_gp_apdu(server, scc, sp, scratch,
                               'FORMAT CHECK (%s)' % fmt, apdu)
        state['steps'].extend(scratch['steps'])
        state['cntr'] = scratch['cntr']
        if ok:
            return fmt
    return 'compact'


# Counter synchronisation probe (v3.9.x): a bounded upward search for a
# counter value the card accepts.  The card only accepts a counter *above* its
# own (TS 102 225 5.1.1 b5b4 = 10) and updates it only when the packet is
# accepted, so rejected attempts change nothing; the search walks a doubling
# ladder (start+1, +2, +4, ...) and stops at a ceiling far below the 40-bit
# maximum, where the counter would get blocked.
COUNTER_PROBE_DEFAULT_APDU = '00A40000023F00'         # SELECT MF, as TAR_PROBE_DEFAULT_APDU
                                                     # (a literal: that constant is defined later)
COUNTER_PROBE_CEILING = 'FFFFFFFF'                    # 32-bit default
COUNTER_PROBE_MAX_ATTEMPTS = 40                       # doubling covers 40 bits
# The probe's SPI2 default: the RAM listing transport (PoR via SMS-SUBMIT).
# The probe cannot work without the PoR verdict - with b1 clear a card that
# honours the bit sends nothing at all (live 2026-10-02: the frontend posted
# the plain form's SPI2 00 and the probe stopped after one packet), and the
# live cards answer '01' with actual_response_sms_submit that never arrives.
COUNTER_PROBE_SPI2 = '21'


def _counter_probe_candidates(start_hex, ceiling_hex, max_attempts):
    """The cautious counter ladder: start + 1, +2, +4, ... (doubling).

    Returns ``(candidates, reason)``: reason is 'ceiling' when the next value
    would exceed the ceiling, else 'attempts' when the attempt budget ran
    out.  Doubling keeps the packet count low (<= ~40 across the whole 40-bit
    range) and the accepted value below ~2x the card's counter - only the
    accepted packet advances anything, every rejected one is a no-op."""
    try:
        value = int(str(start_hex or '0'), 16)
        ceiling = int(str(ceiling_hex or COUNTER_PROBE_CEILING), 16)
        max_attempts = int(max_attempts)
    except (TypeError, ValueError):
        return [], 'attempts'
    candidates = []
    step = 1
    while len(candidates) < max_attempts:
        value += step
        if value > ceiling:
            return candidates, 'ceiling'
        candidates.append('%010X' % value)
        step *= 2
    return candidates, 'attempts'


def _counter_probe_params(preset, body):
    """Resolve a counter probe's parameters from the named preset: the keyset
    (by `kvn`, else the first), its keys and counter, the preset's SPI/TAR
    (overridable), the ceiling and the attempt budget.  Raises ValueError
    with a user-facing message."""
    keysets = _preset_keysets(preset)
    if not keysets:
        raise ValueError('the preset defines no keyset')
    want = body.get('kvn')
    if want not in (None, ''):
        try:
            want = int(want)
        except (TypeError, ValueError):
            raise ValueError('invalid keyset number')
        keyset = next((k for k in keysets if presets.keyset_kvn(k) == want), None)
        if keyset is None:
            raise ValueError('keyset %d is not defined in the preset' % want)
    else:
        keyset = keysets[0]
    kic = str(keyset.get('kic') or '').strip().upper()
    kid = str(keyset.get('kid') or '').strip().upper()
    kic_key = str(keyset.get('kicKey') or '').strip()
    kid_key = str(keyset.get('kidKey') or '').strip()
    if not (kic and kid and kic_key and kid_key):
        raise ValueError('the keyset needs KIc, KID and both keys')
    kvn, err = _kvn_of(kic, kid)
    if err:
        raise ValueError(err)
    start = str(body.get('cntr') or keyset.get('cntr') or '').strip().upper()
    if not _counter_valid(start):
        raise ValueError('the keyset needs a counter to start from')
    ceiling = str(body.get('ceiling') or COUNTER_PROBE_CEILING).strip().upper()
    try:
        ceiling_value = int(ceiling, 16)
    except ValueError:
        raise ValueError('invalid ceiling')
    if ceiling_value < int(start, 16):
        raise ValueError('the ceiling is below the current counter')
    if ceiling_value > 0xFFFFFFFFFE:
        raise ValueError('the ceiling must stay below the 40-bit maximum')
    try:
        max_attempts = int(body.get('max_attempts') or COUNTER_PROBE_MAX_ATTEMPTS)
    except (TypeError, ValueError):
        raise ValueError('invalid attempt budget')
    max_attempts = max(1, min(64, max_attempts))
    tar = str(body.get('tar') or presets.role_tar(preset, 'isd') or '').strip().upper()
    spi1, _ = _spi1_for_tar(preset, tar, body.get('spi1'), prefer_msl=True)
    if not _counter_tracked(spi1):
        raise ValueError('the probe needs a counter check - SPI1 %s has none '
                         '(b5b4 = 00)' % spi1)
    spi2 = str(body.get('spi2') or COUNTER_PROBE_SPI2).strip().upper()
    if not re.fullmatch(r'[0-9A-F]{2}', spi2):
        raise ValueError('invalid SPI2')
    # b1 (PoR required) is forced - the probe is blind without the verdict;
    # the caller's transport bits are kept
    spi2 = '%02X' % (int(spi2, 16) | 0x01)
    return {
        'kvn': kvn, 'kic': kic, 'kid': kid,
        'kic_key': kic_key, 'kid_key': kid_key,
        'start': start, 'ceiling': ceiling,
        'max_attempts': max_attempts,
        'spi1': spi1,
        'spi2': spi2,
        'tar': tar,
        # the probe command is fixed: read-only SELECT MF, which every applet
        # answers (an error SW is fine - the counter advances on the packet's
        # security acceptance, not on the command's own result)
        'apdu': COUNTER_PROBE_DEFAULT_APDU,
    }


def _counter_probe(server, scc, preset, body, send_fn=None):
    """Sync a preset's counter with the card: send a read-only probe command
    with increasing counter values until the card accepts one (PoR `por_ok`),
    then persist accepted+1 (monotonic).

    Every rejected attempt is a no-op on the card; the ceiling and the attempt
    budget bound the search away from the 40-bit maximum, where the counter
    would be blocked.  Returns a result dict with the attempt list - the
    caller (UI) retries the failed operation afterwards.  `send_fn` is a test
    seam (defaults to the `_send_secured_packet` path)."""
    p = _counter_probe_params(preset, body)
    candidates, stop = _counter_probe_candidates(p['start'], p['ceiling'],
                                                 p['max_attempts'])
    out = {'success': False, 'preset_id': preset.get('id'),
           'preset_name': preset.get('name'), 'kvn': p['kvn'],
           'start': p['start'], 'ceiling': p['ceiling'],
           'max_attempts': p['max_attempts'], 'tar': p['tar'],
           'spi1': p['spi1'], 'spi2': p['spi2'], 'apdu': p['apdu'],
           'packets': 0, 'attempts': [], 'stopped': stop}
    if not candidates:
        out['error'] = 'the counter is already at the ceiling %s' % p['ceiling']
        return out
    saw_low = False
    for cntr in candidates:
        if send_fn is not None:
            send, por = send_fn(p, cntr)
        else:
            sp_hex, _ = _build_secured_packet(p['spi1'], p['spi2'], p['kic'], p['kid'],
                                              p['tar'], cntr, p['apdu'],
                                              p['kic_key'], p['kid_key'])
            submit_handler = PoRSubmitHandler()
            old_proactive = None
            if hasattr(scc, '_tp'):
                old_proactive = scc._tp.proactive_handler
                scc._tp.proactive_handler = submit_handler
            try:
                send = _send_secured_packet(scc, sp_hex, oa_number=server.sms_oa,
                                            sm_sc=server.sms_sc, include_cpi=True,
                                            submit_handler=submit_handler)
            finally:
                if hasattr(scc, '_tp'):
                    scc._tp.proactive_handler = old_proactive
            por_hex = send.get('response_data') or ''
            submit_hex = _sms_submit_por(submit_handler)
            if submit_hex:
                por_hex = submit_hex
            por = _decode_por(p['spi1'], p['spi2'], p['kic'], p['kid'], cntr,
                              p['kic_key'], p['kid_key'], por_hex,
                              cmd_len=len(p['apdu']) // 2,
                              rm=_tar_is_rm(preset, p['tar'])) if por_hex else None
        pstatus = str((por or {}).get('response_status') or '')
        out['packets'] += 1
        attempt = {'cntr': cntr, 'por_status': pstatus or None,
                   'sw': send.get('sw'), 'bytes': send.get('bytes'),
                   'segments': send.get('segments')}
        remote_sw = _por_remote_sw(por)
        if remote_sw:
            attempt['remote_sw'] = remote_sw
        if (por or {}).get('bad_format'):
            attempt['bad_format'] = por['bad_format']
        out['attempts'].append(attempt)
        sys.stderr.write('COUNTER-PROBE: %s -> %s\n'
                         % (cntr, pstatus or ('ENVELOPE %s' % (send.get('sw') or '?'))))
        if pstatus == 'cntr_low':
            # the only verdict that keeps the ladder going: the card says the
            # attempted counter is not above its own
            saw_low = True
            continue
        if pstatus in ('por_ok', 'actual_response_sms_submit', 'actual_response_ussd'):
            # accepted (an actual-response status means the response travels
            # via SMS/USSD - the packet itself was accepted)
            stored = _ram_next_cntr(cntr, True)
            _preset_counter_persist(server, preset.get('id'), stored,
                                    'counter-probe', p['kvn'])
            out.update({'success': True, 'stopped': 'accepted', 'verdict': pstatus,
                        'counter_saved': True,
                        'accepted_cntr': cntr, 'stored_cntr': stored})
            sys.stderr.write('COUNTER-PROBE: accepted %s (%s), preset stores %s\n'
                             % (cntr, pstatus, stored))
            return out
        out['stopped'] = 'error'
        if pstatus:
            if saw_low:
                # A cntr_low rejection earlier proved the card's counter is
                # below this value, so the packet got past the counter check
                # and failed a later security/permission one (wrong keys,
                # TAR, level).  Keep the value - it is the sync point the
                # probe was looking for - but report the error so the
                # security settings get looked at.
                stored = _ram_next_cntr(cntr, True)
                _preset_counter_persist(server, preset.get('id'), stored,
                                        'counter-probe', p['kvn'])
                out.update({'counter_saved': True, 'verdict': pstatus,
                            'accepted_cntr': cntr, 'stored_cntr': stored,
                            'error': 'card verdict: %s - the counter value was still '
                                     'saved (accepted %s, stored %s); something is '
                                     'likely wrong with the security settings'
                                     % (pstatus, cntr, stored)})
                sys.stderr.write('COUNTER-PROBE: %s -> %s, value saved (preset stores %s)\n'
                                 % (cntr, pstatus, stored))
            else:
                out['error'] = send.get('error') or (
                    'card verdict: %s - the counter was not synced '
                    '(the search never saw a cntr_low rejection)' % pstatus)
        else:
            out['error'] = send.get('error') or (
                'card verdict: no PoR (the card sent none for SPI2 %s)' % p['spi2'])
        return out
    if stop == 'ceiling':
        out['error'] = ('no counter up to the ceiling %s was accepted - the card\'s '
                        'counter is above it, raise the preset counter manually'
                        % p['ceiling'])
    else:
        out['error'] = ('no counter above %s was accepted in %d attempts'
                        % (p['start'], p['max_attempts']))
    return out


# Live progress of the RAM operation currently running.  The long install
# chains hold _CARD_LOCK for the whole request, so the PWA cannot learn the
# step from the response - /api/status (lock-free cached state, polled every
# 2 s) carries this dict instead (v3.6.34).
_RAM_PROGRESS = {'active': False, 'kind': '', 'step': 0, 'total': 0,
                 'name': '', 'started': 0.0}


def _ram_progress_begin(kind, total):
    _RAM_PROGRESS.update({'active': True, 'kind': str(kind),
                          'step': 0, 'total': int(total or 0), 'name': '',
                          'started': time.time()})


def _ram_progress_step(step, name):
    _RAM_PROGRESS.update({'step': int(step), 'name': str(name or '')})


def _ram_progress_end():
    _RAM_PROGRESS['active'] = False


def _ram_progress_payload():
    pr = _RAM_PROGRESS
    started = pr.get('started') or 0
    return {'active': bool(pr.get('active')), 'kind': pr.get('kind', ''),
            'step': int(pr.get('step') or 0), 'total': int(pr.get('total') or 0),
            'name': pr.get('name', ''),
            'elapsed': round(time.time() - started, 1) if started else 0}


def _ram_step_result(step_name, last_sw, por, por_hex, bytes_, segments):
    """Assemble a RAM-install step record and its failure reason.

    The PoR verdict is the top-level `response_status`; the remote command's
    own status word lives in `decoded.last_status_word` (compact) or in the
    expanded `responses` list.  A step fails on a non-por_ok PoR, on a remote
    SW outside `_RAM_REMOTE_SW_OK`, or when a PoR arrived but could not be
    decoded (a 9000 transport SW with no PoR at all is 'no_por', not a
    failure)."""
    step = {'name': step_name, 'sw': last_sw, 'bytes': bytes_, 'segments': segments}
    error = None
    if por:
        pstatus = str(por.get('response_status') or '')
        remote_sw = _por_remote_sw(por)
        step['por_status'] = pstatus or 'unknown'
        step['por_type'] = por.get('response_type')
        step['por_cntr'] = por.get('cntr')
        step['por_data'] = (por.get('decoded') or {}).get('last_response_data', '')
        step['por_raw'] = por.get('raw')
        if remote_sw:
            step['por_sw'] = remote_sw
        if por.get('bad_format'):
            # the expanded script was rejected as malformed before executing:
            # there is no remote status word
            step['por_bad_format'] = por['bad_format']
            step['por_bad_format_name'] = por.get('bad_format_name')
            error = 'bad format %s (%s)' % (por['bad_format'],
                                            por.get('bad_format_name') or '')
        elif pstatus != 'por_ok':
            error = 'PoR %s' % (pstatus or 'unknown')
        elif remote_sw and not _ram_remote_sw_ok(remote_sw):
            error = 'remote SW %s' % remote_sw
    elif last_sw == '9000' and not por_hex:
        step['por_status'] = 'no_por'
    else:
        step['por_status'] = 'unknown'
        if por_hex:
            step['por_raw'] = por_hex
        error = 'PoR undecodable'
    if error:
        step['por_error'] = error
    return step, error


def _ram_send_gp_apdu(server, scc, sp, state, step_name, apdu_hex, silent=False):
    """Send one GP APDU as an SCP80 secured packet and append its step result
    to `state['steps']`.  `sp` carries the SCP80 parameters (spi1, spi2, kic,
    kid, tar, kic_key, kid_key, include_cpi); `state` carries the mutable run
    state ({'steps', 'encode_error', 'failure', 'cntr'}) and the counter is
    advanced only for a packet the card accepted.  Returns True on success
    (the step record is the last element of state['steps']).

    `silent=True` (the NV footprint reads) leaves no step record and no
    failure note behind: the card's answer is only used by the caller, and a
    rejected read must never fail the operation.  The decoded step record is
    still available as `state['last_step']`."""
    spi1, spi2 = sp['spi1'], sp['spi2']
    try:
        sp_hex, _ = _build_secured_packet(spi1, spi2, sp['kic'], sp['kid'], sp['tar'],
                                          state['cntr'], apdu_hex,
                                          sp['kic_key'], sp['kid_key'])
        # Log the plaintext APDU and the packed packet like /api/send-ota does
        # (the step name keeps the sequence readable).
        sys.stderr.write('RAM C-APDU (%s): %s\n' % (step_name, apdu_hex))
        sys.stderr.write('RAM SECURED-PACKET (%s): %s\n' % (step_name, sp_hex))
    except ValueError as e:
        if not silent:
            state['encode_error'] = str(e)
            state['steps'].append({'name': step_name, 'por_status': 'encode_error',
                                   'sw': str(e)})
        sys.stderr.write('RAM-INSTALL: %s encode failed: %s\n' % (step_name, e))
        return False
    # Capture the PoR from either transport: inline in the ENVELOPE response
    # or as a proactive SEND SHORT MESSAGE.  Cards differ - some always submit,
    # whatever the SPI2 request bit says - so the capture is unconditional
    # (v3.6.24).
    submit_handler = None
    old_proactive = None
    if hasattr(scc, '_tp'):
        submit_handler = PoRSubmitHandler()
        old_proactive = scc._tp.proactive_handler
        scc._tp.proactive_handler = submit_handler
    try:
        result = _send_secured_packet(
            scc, sp_hex, oa_number=server.sms_oa, sm_sc=server.sms_sc,
            include_cpi=sp['include_cpi'], submit_handler=submit_handler)
        if not result['success']:
            if not silent:
                state['steps'].append({'name': step_name, 'por_status': 'envelope_error',
                                       'sw': result.get('sw') or result.get('error'),
                                       'bytes': result.get('bytes'),
                                       'segments': result.get('segments')})
            sys.stderr.write('RAM-INSTALL: %s send failed: %s\n' % (
                step_name, result.get('error')))
            return False
        por_hex = result['response_data']
        last_sw = result['sw']
        por_src = 'envelope'
        submit_hex = _sms_submit_por(submit_handler)
        if submit_hex:
            por_hex = submit_hex
            por_src = 'sms-submit'
        elif submit_handler and submit_handler.submit_tpdu_hex:
            # no assembled UD: fall back to a raw RPI packet
            tpdu_b = bytes.fromhex(submit_handler.submit_tpdu_hex)
            idx = tpdu_b.find(b'\x02\x71\x00')
            if idx >= 0:
                por_hex = tpdu_b[idx:].hex()
                por_src = 'sms-submit'
        por = _decode_por(spi1, spi2, sp['kic'], sp['kid'], state['cntr'],
                          sp['kic_key'], sp['kid_key'], por_hex,
                          cmd_len=len(apdu_hex) // 2,
                          rm=_tar_is_rm(_preset_by_id(server, state.get('preset_id')),
                                        sp.get('tar')))
        step, step_error = _ram_step_result(step_name, last_sw, por, por_hex,
                                            result['bytes'], result['segments'])
        state['last_step'] = step
        if not silent:
            state['steps'].append(step)
        sys.stderr.write('RAM-INSTALL: %s PoR[%s] status=%s remote_sw=%s%s (%d B, %d SM)\n' % (
            step_name, por_src, step.get('por_status', '?'), step.get('por_sw', '-'),
            (' error=%s' % step_error) if step_error else '',
            result['bytes'], result['segments']))
        # Advance the counter only for an accepted packet, and only when the
        # packet asks the card to check it (TS 102 225 5.1.1 b5b4): a SPI1
        # without a counter check leaves the card's replay value untouched, so
        # the tool must not advance or persist anything either.
        if step.get('por_status') in ('por_ok', 'no_por') and _counter_tracked(sp.get('spi1')):
            state['cntr'] = _ram_next_cntr(state['cntr'], True)
            # ... and persist it right away: the server owns the counter now,
            # so a crash mid-install cannot leave the preset behind the card.
            _preset_counter_persist(server, state.get('preset_id'),
                                    state['cntr'], 'ram-%s' % step_name.split()[0].lower(),
                                    state.get('kvn'))
        if step_error:
            if not silent:
                state['failure']['error'] = '%s: %s' % (step_name, step_error)
            return False
        return True
    finally:
        if submit_handler and hasattr(scc, '_tp'):
            scc._tp.proactive_handler = old_proactive


def _ram_free_nv(por_data_hex):
    """Free non-volatile memory (bytes) from a GET DATA FF21 R-APDU, or None.
    Reuses the FF21 decoder (TS 102 226 8.2.1.7.2: '81' applet count /
    '82' free NV / '83' free volatile)."""
    try:
        rapdu = bytes.fromhex(por_data_hex or '')
    except ValueError:
        return None
    mem = _scp81_decode_memory(rapdu)
    return mem.get('free_nv') if mem else None


def _ram_read_ff21(server, scc, sp, state, ram_format, label):
    """Best-effort GET DATA FF21 (Extended Card Resources) read around an
    install: returns the free non-volatile memory in bytes, or None (a card
    without FF21 or a rejected read must never fail the operation).  The read
    is sent through the normal step sender with `silent=True`, so it consumes
    and advances the counter correctly but leaves no step record or failure
    note behind."""
    apdu = _ram_format_apdu('80CAFF2100', ram_format)
    if not _ram_send_gp_apdu(server, scc, sp, state, label, apdu, silent=True):
        return None
    step = state.get('last_step') or {}
    return _ram_free_nv(step.get('por_data'))


def _ram_nv_fields(nv_before, nv_after):
    """NV footprint fields for the install responses: the free-NV values and
    their delta (None when the card has no FF21 readout)."""
    if nv_before is None and nv_after is None:
        return {}
    delta = (nv_before - nv_after) if (nv_before is not None and nv_after is not None) else None
    return {'nv_before': nv_before, 'nv_after': nv_after, 'nv_delta': delta}


_BAD_FORMAT_NAMES = {
    '01': 'unknown tag',
    '02': 'wrong length',
    '03': 'length not found',
}


def _parse_response_scripting(data):
    """Parse a Response Scripting template (TS 102 226 5.2.2, tables
    5.10/5.10a): `AB <len>` (definite) or `AF 80 ... 00 00` (indefinite),
    containing the executed-command-count TLV `80`, one or more R-APDU TLVs
    `23` (COMPREHENSION-TLV; the last two bytes are SW1 SW2) and/or the Bad
    format TLV `90` (TS 101 220 table 7.20; error type 01 unknown tag,
    02 wrong length, 03 length not found - TS 102 226 table 5.12).

    Returns {'count', 'sw', 'data', 'bad_format'} or None when the data is
    not a scripting template.  `bad_format` is the error type hex when the
    card aborted the script on a malformed command (then `sw` is None and
    `data` ''); an R-APDU is not required for a bad-format answer."""
    if not data:
        return None
    if data[0] == 0xAF:
        if len(data) < 4 or data[1] != 0x80 or data[-2:] != b'\x00\x00':
            return None
        body = data[2:-2]
    elif data[0] == 0xAB:
        ln, voff = _ber_len_at(data, 1)
        if ln <= 0 or voff + ln > len(data):
            return None
        body = data[voff:voff + ln]
    else:
        return None
    count = None
    last = None
    bad = None
    off = 0
    while off < len(body) - 1:
        tag = body[off]
        ln, voff = _ber_len_at(body, off + 1)
        if ln < 0 or voff + ln > len(body):
            break
        val = body[voff:voff + ln]
        if tag == 0x80 and val:
            count = int.from_bytes(val, 'big')
        elif tag in (0x23, 0xA3) and len(val) >= 2:
            last = (val[-2:].hex().upper(), val[:-2].hex().upper())
        elif tag in (0x90, 0x10) and val:
            # both tag styles: the spec pins the CR flag to 0, cards mix them
            bad = val[:1].hex().upper()
        off = voff + ln
    if last is None and bad is None:
        return None
    return {'count': count,
            'sw': last[0] if last else None,
            'data': last[1] if last else '',
            'bad_format': bad}


def _sms_submit_por(submit_handler):
    """Response packet carried by an actual-response SMS-SUBMIT, in the
    DELIVER-style form `_decode_por` expects.

    The submit UD has no RPI UDH (the RPI is a UDH IE there) and starts at
    RPL/RHL/TAR/CNTR/PCNTR/STS, so the `02 71 00` RPI UDH is prepended.  Returns
    '' when no submit response was captured."""
    ud_hex = (getattr(submit_handler, 'submit_ud_hex', None) or '').strip()
    if not ud_hex:
        return ''
    try:
        bytes.fromhex(ud_hex)
    except ValueError:
        return ''
    return '027100' + ud_hex


def _por_header_parse(data, otak, spi):
    """Header-only parse of an SMS response packet (TS 102 225 5.2 Table 3).

    Everything `OtaDialectSms.decode_resp` validates *before* the compact
    response parse: the UDH/RPI check, the response header, deciphering, the
    PCNTR padding and the RC/CC/DS verification.  Used when the secured data
    is not a compact/scripting RM response (TS 102 226 4: the receiving
    application's own format) - a valid packet is then reported raw instead
    of "undecodable".  Returns the parsed response or None."""
    from pySim.ota import OtaDialectSms
    from pySim.sms import UserDataHeader
    try:
        if not data or data[0] != 0x02:
            return None
        udhd, remainder = UserDataHeader.from_bytes(data)
        if not udhd.has_ie(0x71):
            return None
        res = OtaDialectSms.SmsResponsePacket.parse(remainder)
        if spi['por_shall_be_ciphered']:
            deciph = otak.crypt.decrypt(remainder[6:])
            temp_data = remainder[:6] + deciph
            res = OtaDialectSms.SmsResponsePacket.parse(temp_data)
            if res['pcntr'] != 0:
                res['secured_data'] = res['secured_data'][:-res['pcntr']]
            remainder = temp_data
        len_sig = res['rhl'] - 10
        if spi['por_rc_cc_ds'] == 'no_rc_cc_ds':
            if len_sig:
                return None
        elif spi['por_rc_cc_ds'] == 'cc':
            # UDH + RPL/RHL/TAR/CNTR/PCNTR/STS are part of the CC input
            udh = data[:3]
            header = remainder[:13]
            otak.auth.check_sig(udh + header + remainder[13 + len_sig:],
                                res['cc_rc'])
        else:
            return None
    except Exception:
        # a malformed packet or a failed check is not a response packet
        return None
    return res


def _decode_por(spi1, spi2, kic, kid, cntr_hex, kic_key_hex, kid_key_hex,
                response_hex, cmd_len=None, rm=None):
    """Decode a response packet (PoR).

    `rm` tells whether the command was addressed to a remote-management TAR
    (TS 102 226 4: only those answer with the compact/scripting structures of
    5.1.2/5.2.2):

    - ``True``: decode as an RM response;
    - ``False``: any other TAR - the secured data is the receiving
      application's own (application specific, "not defined" in the spec) and
      is reported raw; no status word is invented;
    - ``None`` (unknown): decode, falling back to raw when the compact parse
      cannot apply.

    `cmd_len` is the length in bytes of the plaintext command script the
    packet carried (None when unknown): the TS 102 226 5.1.2 compact
    response's command count cannot exceed it, so a larger count means the
    data is the application's own and is reported as `raw`."""
    from pySim.ota import OtaDialectSms
    from osmocom.utils import h2b
    if not response_hex:
        return None
    try:
        otak = _ota_keyset(spi1, spi2, kic, kid, cntr_hex, kic_key_hex, kid_key_hex)
        spi = _spi_from_bytes(int(spi1, 16), int(spi2, 16))
        data = h2b(response_hex)
    except Exception:
        # a malformed counter/key/hex (e.g. a hand-made request) is not a PoR
        return None
    dec = None
    if rm is False:
        res = _por_header_parse(data, otak, spi)
        if res is None:
            return None
    else:
        try:
            res, dec = OtaDialectSms().decode_resp(otak, spi, data)
        except Exception:
            # pySim's decode fails when the secured data is not a compact
            # response (e.g. a third-party applet's short reply): fall back
            # to the header parse and report the data raw, not lost.
            res = _por_header_parse(data, otak, spi)
            if res is None:
                return None
    out = {
        'response_status': str(res['response_status']),
        'tar': res['tar'].hex().upper(),
        'cntr': res['cntr'].hex().upper(),
        'pcntr': res['pcntr'],
        'rpl': res['rpl'],
        'rhl': res['rhl'],
        'cc_rc': res['cc_rc'].hex().upper(),
        'raw': response_hex,
    }
    secured_hex = (bytes(res['secured_data']).hex().upper()
                   if len(res['secured_data']) else '')
    if secured_hex:
        out['secured_data'] = secured_hex
    # The compact structure's first byte (TS 102 226 5.1.2 Table 5.1) is the
    # number of commands executed within the command script; each command
    # needs at least one byte, so a count above the script's length proves
    # the data is not a compact remote response.
    compact_ok = (dec is not None
                  and (cmd_len is None
                       or int(dec.number_of_commands) <= int(cmd_len)))

    # TS 102 226 5.2.2 Response Scripting template (AB/AF): cards wrap the
    # R-APDU(s) of the executed remote command(s) this way instead of the
    # plain compact response.  The R-APDU's own SW and data are the useful
    # result (a bare CompactRemoteResp parse would read `AB` as the command
    # count and produce garbage).  A non-RM TAR's payload is application data
    # (rm False): never parse it as an RM structure.
    if res.response_status == 'por_ok' and len(res['secured_data']):
        scripted = (_parse_response_scripting(bytes(res['secured_data']))
                    if rm is not False else None)
        if scripted is not None:
            out['response_type'] = 'scripting'
            out['decoded'] = {
                'number_of_commands': scripted['count'],
                'last_status_word': scripted['sw'],
                'last_response_data': scripted['data'],
            }
            if scripted['bad_format']:
                # the card parsed the scripting template but aborted on a
                # malformed command (TS 102 226 5.2.2 Bad format TLV)
                out['bad_format'] = scripted['bad_format']
                out['bad_format_name'] = _BAD_FORMAT_NAMES.get(scripted['bad_format'],
                                                               'unknown error type')
        elif compact_ok:
            out['response_type'] = 'compact'
            out['decoded'] = {
                'number_of_commands': dec.number_of_commands,
                'last_status_word': str(dec.last_status_word),
                'last_response_data': str(dec.last_response_data),
            }
        else:
            # Not a compact remote response - the application's own bytes
            # (e.g. a third-party applet's TAR); expose them as the response
            # data, with no status word.
            out['response_type'] = 'raw'
            out['decoded'] = {
                'number_of_commands': None,
                'last_status_word': '',
                'last_response_data': secured_hex,
            }
    else:
        out['response_type'] = 'none'

    return out


class PoRSubmitHandler(ProactiveHandler):
    """Captures the SMS-SUBMIT TPDU from a SendShortMessage proactive command
    issued by the SIM in response to PoR-in-submit (SPI2 bit 0x20).
    The 91XX path in _send_envelope scans the FETCH response directly for
    the SMS_TPDU child (tag 0x8B) and populates submit_tpdu_hex.
    Supports concatenated SMS: when UDH contains IEI 0x00 or 0x08, segments
    are accumulated and reassembled once all parts arrive."""
    def __init__(self):
        super().__init__()
        self.submit_tpdu_hex = None
        # Assembled UD data of the actual-response SMS-SUBMIT(s), UDH stripped:
        # the DELIVER-style response packet is `02 71 00` + this data.
        self.submit_ud_hex = None
        self.sms_segments = []  # [(ref, total, num, payload_hex), ...]


class _DefaultProactiveHandler(ProactiveHandler):
    """Catch-all for any proactive command not explicitly handled. Logs the
    fetch and its TERMINAL RESPONSE into _PROACTIVE_LOG and answers
    PROVIDE LOCAL INFORMATION (0x26) with data from the PLI dictionary,
    so the card does not re-request."""

    def receive_fetch_raw(self, pcmd, parsed):
        cmd_num, cmd_type, dev_src, dev_dst, cmd_qual = 1, 0, 0x83, 0x81, None
        entry = None
        # pySim parses the FETCH response into a command-specific object
        # ('parsed'); the 'pcmd' collection stays empty and is only useful as
        # a fallback. Use the parsed object for both the log and the response.
        cmd_obj = parsed if getattr(parsed, 'children', None) else pcmd
        try:
            raw = cmd_obj.to_tlv()
            if raw:
                cmd_num, cmd_type, dev_src, dev_dst, cmd_qual = _parse_proactive_header(raw)
            entry = _log_proactive(cmd_type, raw, cmd_qual, cmd_num)
        except Exception:
            pass
        ti_list = self.prepare_response(cmd_obj, 'performed_successfully')
        if cmd_type == 0x26 and cmd_qual is not None:
            pli_hex = _pli_data_hex(cmd_qual)
            if pli_hex:
                try:
                    # After the Result, per the TS 102 223 6.8.0 object order.
                    ti_list.append(_RawBerTlv(pli_hex))
                except Exception:
                    pass
        if entry:
            _record_tr(entry, b''.join(x.to_tlv() for x in ti_list))
        return ti_list


class _RawBerTlv(BER_TLV_IE):
    """Emits pre-encoded TLV bytes verbatim (used to inject PLI data TLVs
    into the TERMINAL RESPONSE built by pySim's auto-handler)."""

    def __init__(self, data_hex):
        super().__init__()
        self._raw = bytes.fromhex(data_hex)

    def to_bytes(self, context={}):
        return self._raw

    def to_tlv(self):
        return self._raw


_STK_DECODE = GsmOrUcs2Adapter(GreedyBytes)

_PROACTIVE_LOG = []
_PROACTIVE_SESSION_START = None
_PROACTIVE_ENTRY_ID = 0

# TS 102 223 §9.4 command types (the values are also the proactive command
# template tags).  Unknown values fall back to 'UNKNOWN' in the log.
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

_PLI_DATA = {q: '' for q in PLI_QUALIFIER_NAMES}

_BIP = httpota.BipTerminal()
# Active BIP session, shared by the SCP81 listener and the Simulator's
# generic BIP pill - only one can run at a time:
#   'tls' | 'dump'  SCP81 PSK TLS / capture listener
#   'sink'          generic BIP local sink (accept + log, never answers)
#   'redirect'      pinned target, no listener object: every channel connects
#                   to the configured platform (TLS terminated there)
#   'passthru'      no listener and no target: each channel dials the
#                   destination the card requests in OPEN CHANNEL
# _BIP_OWNER records which control started it ('scp81' | 'bip') so both
# status views can show who owns the terminal.
_BIP_LISTENER = None
_BIP_MODE = None
_BIP_TARGET = None
_BIP_OWNER = None
# PSK table of the TLS listener: identity -> key (memory only, never logged or
# persisted; the PWA sends it from the card presets at listener start).
# _SCP81_PSK_LEGACY keeps a single-key start (psk_hex [+ psk_identity]) so an
# API restart without psk_map/psk_hex can reuse it.
_SCP81_PSKS = {}
_SCP81_PSK_LEGACY = None

# Background STATUS polling is on by default: a CAT terminal shall poll
# during idle at the negotiated (or default) interval (TS 102 221 14.6.2).
# The operator can turn it off, --poll-interval 0 disables it and a card can
# suspend it with POLLING OFF (see _POLL_DISABLED_BY_CARD).
_POLL_ENABLED = True
_POLL_INTERVAL = 30
_POLL_TIMER = None
# Set when the card sent POLLING OFF (TS 102 223 6.4.14): proactive polling
# stays suspended until a POLL INTERVAL re-enables it.  Presence detection and
# the manual "Send STATUS" button are not affected.
_POLL_DISABLED_BY_CARD = False
_CARD_LOCK = threading.RLock()
_CARD_CONNECTED = False
# True when the current PC/SC transport is unusable (service failure): the
# next equip recreates it via server.transport_factory (_ensure_transport).
_TRANSPORT_STALE = False

def _set_poll_interval(seconds):
    global _POLL_INTERVAL
    _POLL_INTERVAL = max(0, min(255, int(seconds)))

def _duration_seconds(unit, interval):
    """Duration TLV value (TS 102 223 8.8) as seconds for the background poll,
    clamped to 1..255 s (the TERMINAL RESPONSE Duration carries seconds)."""
    if unit == 0x00:        # minutes
        secs = interval * 60
    elif unit == 0x02:      # tenths of seconds
        secs = int(round(interval / 10.0))
    else:                   # seconds
        secs = interval
    return max(1, min(255, secs))


def _duration_text(unit, interval):
    if unit == 0x00:
        return '%d min' % interval
    if unit == 0x02:
        return '%.1f s' % (interval / 10.0)
    return '%d s' % interval


def _find_duration(raw):
    """(unit, interval) of the Duration TLV in a proactive command (tag '04'
    or its CR-set variant '84', TS 102 223 8.8), or None."""
    if not raw:
        return None
    off = _skip_ber_len(raw, 1) if raw[0] == 0xD0 else 0
    while off + 1 < len(raw):
        tag, tlen = raw[off], raw[off + 1]
        val = raw[off + 2: off + 2 + tlen]
        if tag in (0x04, 0x84) and len(val) >= 2:
            return val[0], val[1]
        off += 2 + tlen
    return None


def _handle_card_poll_command(cmd_type, raw):
    """Adopt the card's polling wishes before its TERMINAL RESPONSE is built
    (TS 102 223 6.4.6 POLL INTERVAL / 6.4.14 POLLING OFF)."""
    global _POLL_DISABLED_BY_CARD, _POLL_TIMER
    if cmd_type == 0x03:
        dur = _find_duration(raw)
        if dur:
            unit, interval = dur
            secs = _duration_seconds(unit, interval)
            _set_poll_interval(secs)
            sys.stderr.write('POLL INTERVAL: card requests %s -> background poll %d s\n'
                             % (_duration_text(unit, interval), secs))
        _POLL_DISABLED_BY_CARD = False
        if _POLL_ENABLED:
            _reset_poll_timer()
    elif cmd_type == 0x04:
        if not _POLL_DISABLED_BY_CARD:
            sys.stderr.write('POLLING OFF: card disabled proactive polling\n')
        _POLL_DISABLED_BY_CARD = True
        if _POLL_TIMER is not None:
            _POLL_TIMER.cancel()
            _POLL_TIMER = None


def _reset_poll_timer():
    global _POLL_TIMER
    if _POLL_TIMER is not None:
        _POLL_TIMER.cancel()
        _POLL_TIMER = None
    if _POLL_ENABLED and _POLL_INTERVAL > 0 and not _POLL_DISABLED_BY_CARD:
        _POLL_TIMER = threading.Timer(_POLL_INTERVAL, _do_status_poll)
        _POLL_TIMER.daemon = True
        _POLL_TIMER.start()

def _do_status_poll():
    global _POLL_TIMER
    _POLL_TIMER = None
    if not _POLL_ENABLED or _POLL_DISABLED_BY_CARD or _TEST_RUNNING:
        return
    with _CARD_LOCK:
        try:
            scc = getattr(_server_ref, 'scc', None) if _server_ref else None
            if scc:
                st_data, st_sw = _send_status(scc)
                sys.stderr.write('AUTO-STATUS -> %s\n' % st_sw)
                if st_sw.startswith('91'):
                    _handle_proactive_chain(scc, st_sw)
        except Exception as e:
            sys.stderr.write('AUTO-STATUS error: %s\n' % e)
            _handle_card_disconnect(stale=_is_transport_fatal(e))
    # Keep ticking while enabled even without a session (a cardless start or
    # the window between removal and the next equip); _handle_card_disconnect
    # disables polling, which stops the chain.
    _reset_poll_timer()

def _poll_enable():
    global _POLL_ENABLED
    if _POLL_INTERVAL <= 0:
        _POLL_ENABLED = False
        return
    _POLL_ENABLED = True
    _reset_poll_timer()

_MENU_TIMEOUT = 60
_MENU_TIMER = None

def _set_menu_timeout(seconds):
    global _MENU_TIMEOUT
    _MENU_TIMEOUT = max(0, min(3600, int(seconds)))

def _cancel_menu_timeout():
    global _MENU_TIMER
    if _MENU_TIMER is not None:
        _MENU_TIMER.cancel()
        _MENU_TIMER = None

def _arm_menu_timeout():
    """Watchdog: a paused proactive command must always get a TERMINAL RESPONSE,
    even if the user never answers. Fires 0x12 ('timeout') via the same path as
    an explicit user response."""
    global _MENU_TIMER
    _cancel_menu_timeout()
    if _MENU_TIMEOUT <= 0:
        return
    _MENU_TIMER = threading.Timer(_MENU_TIMEOUT, _menu_timeout_fire)
    _MENU_TIMER.daemon = True
    _MENU_TIMER.start()

def _menu_timeout_fire():
    global _MENU_TIMER
    _MENU_TIMER = None
    with _CARD_LOCK:
        server = _server_ref
        if not server or not getattr(server, 'stk_pending', None) or not getattr(server, 'scc', None):
            return
        pd = server.stk_pending
        sys.stderr.write('MENU-TIMEOUT: auto TR timeout (cmd=%02x type=%02x)\n' % (pd['cmd_num'], pd['cmd_type']))
        try:
            _menu_send_response(server, 'timeout', None)
        except Exception as e:
            sys.stderr.write('MENU-TIMEOUT error: %s\n' % e)

def _poll_disable():
    global _POLL_ENABLED, _POLL_TIMER
    _POLL_ENABLED = False
    if _POLL_TIMER is not None:
        _POLL_TIMER.cancel()
        _POLL_TIMER = None


POLL_NEGOTIATION_RESULTS = {0: 'accepted', 1: 'rejected', 2: 'modified'}


def _decode_poll_negotiation(data_hex):
    """Decode the ENVELOPE response data of a Poll Interval Negotiation event
    (TS 102 223 7.5.22.2 / 8.97).  No response data means 'accepted'."""
    s = re.sub(r'\s', '', data_hex or '')
    if not s:
        return {'result': 0, 'result_name': 'accepted', 'unit': None,
                'interval': None, 'seconds': None}
    try:
        data = bytes.fromhex(s)
    except ValueError:
        return None
    result = None
    unit = interval = None
    for tag, val in _ber_tlv_list(data):
        if tag in (0x11, 0x91) and len(val) >= 1:
            result = val[0]
        elif tag in (0x04, 0x84) and len(val) >= 2:
            unit, interval = val[0], val[1]
    if result is None:
        return None
    out = {'result': result, 'result_name': POLL_NEGOTIATION_RESULTS.get(result, 'unknown'),
           'unit': unit, 'interval': interval, 'seconds': None}
    if unit is not None:
        out['seconds'] = _duration_seconds(unit, interval)
    return out


def _apply_poll_negotiation(neg):
    """Act on the card's answer to our poll interval proposal: a 'modified'
    result carries the duration the terminal shall use from now on."""
    global _POLL_DISABLED_BY_CARD
    if not neg or neg.get('result') != 2 or not neg.get('seconds'):
        return
    _set_poll_interval(neg['seconds'])
    _POLL_DISABLED_BY_CARD = False
    sys.stderr.write('POLL NEGOTIATION: modified -> background poll %d s\n' % neg['seconds'])
    if _POLL_ENABLED:
        _reset_poll_timer()

_server_ref = None


def _tr_data_only(tr_tlv):
    """Strip boilerplate CTLVs (Command Details, Device IDs, Result) from TR,
    returning only command-specific data TLVs or empty bytes."""
    skip_tags = {0x81, 0x82, 0x03, 0x83}
    data = bytearray()
    off = 0
    while off < len(tr_tlv) - 1:
        tag, tlen = tr_tlv[off], tr_tlv[off + 1]
        val = tr_tlv[off + 2: off + 2 + tlen]
        off += 2 + tlen
        if tag not in skip_tags:
            data.extend(tr_tlv[off - 2 - tlen: off])
    return bytes(data)


# Event list codings (TS 102 223 v18.3.0 8.25).  The values the CAT spec only
# points at 3GPP for carry their TS 31.111 7.5 event name; "Reserved for
# 3GPP" is a spec cross-reference, not a usage restriction, so it never
# appears in the user-facing names.
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

ACCESSTECH_NAMES = {
    0: 'GSM', 1: 'GSM Compact', 2: 'TIA/EIA-533', 3: 'UTRAN',
    4: 'TETRA', 5: 'TIA/EIA-95-B', 6: 'CDMA2000 1x', 7: 'CDMA2000 HRPD',
    8: 'E-UTRAN', 9: 'eHRPD', 10: 'NG-RAN', 11: 'Satellite NG-RAN',
    12: 'Satellite E-UTRAN',
}

PLI_QUALIFIER_SHORT = {
    0x00: 'Loc', 0x01: 'IMEI', 0x02: 'NMR', 0x03: 'Time', 0x04: 'Lang',
    0x05: 'TA', 0x06: 'AccTech', 0x08: 'IMEISV', 0x09: 'Search', 0x0A: 'Batt',
    0x0C: 'WSID', 0x0D: 'BCInfo', 0x0E: 'MultiAT', 0x0F: 'MultiLoc',
    0x10: 'MultiNMR', 0x11: 'CSG', 0x12: 'HNB-IP', 0x13: 'HNB-Macro',
    0x14: 'WLAN', 0x15: 'Slices', 0x16: 'CAG', 0x17: 'RejSlice',
}


def _dec_plmn(hex6):
    """Decode 3-byte MCC/MNC nibble-swapped PLMN, e.g. '25001' from 'F50010'."""
    h = re.sub(r'\s', '', hex6)[:6]
    if len(h) < 6:
        return '250', '01'
    b1, b2, b3 = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    mcc = '%d%d%d' % (b1 & 0xF, (b1 >> 4) & 0xF, b2 & 0xF)
    mnc = '%d' % (b3 & 0xF)
    if (b2 >> 4) != 0xF:
        mnc += '%d' % ((b2 >> 4) & 0xF)
    return mcc, mnc


def _dec_imei(hex8):
    """Decode 8-byte IMEI (nibble-swapped, 15 digits)."""
    h = re.sub(r'\s', '', hex8)[:16]
    if len(h) < 16:
        return ''
    s = ''
    for i in range(0, 15, 2):
        b = int(h[i:i + 2], 16)
        s += '%d%d' % (b & 0xF, (b >> 4) & 0xF)
    return s[:15]


def _cmd_tlv(tlvs, tag):
    """Fetch a command TLV, tolerating both tag styles: bit 8 of the tag is
    the comprehension-required flag (TS 101 220 7.1.1), so the other variant
    is `tag ^ 0x80` - covering both directions (0x24/0xA4, 0x8D/0x0D,
    0x05/0x85).  The exact tag wins.  Every proactive-TLV lookup goes through
    this helper; tests/test_tag_variants.py enforces it for all parsers."""
    return tlvs.get(tag) or tlvs.get(tag ^ 0x80) or b''


def _tlv_map(data):
    """Top-level COMPREHENSION-TLV map {tag: value} of a payload without a
    D0 wrapper (e.g. the command-specific TLVs of a TERMINAL RESPONSE)."""
    out = {}
    for tag, val in _ber_tlv_list(data or b''):
        out.setdefault(tag, val)
    return out


def _bcd_swap(b):
    """Semi-octet BCD digit pair (TS 123 040 TP-SCT): low nibble first."""
    return (b & 0x0F) * 10 + ((b >> 4) & 0x0F)


def _hms_bcd(seconds):
    """Encode seconds as hour/minute/second semi-octet BCD (TS 123 040)."""
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return bytes([((h % 10) << 4) | (h // 10),
                  ((m % 10) << 4) | (m // 10),
                  ((s % 10) << 4) | (s // 10)])


_TIMER_ACTIONS = {0x00: 'Start', 0x01: 'Deactivate', 0x02: 'Get current value'}

# SMS TPDU type (TS 23.040 TP-MTI, bits 1-2 of the first octet).
_SMS_MTI = {0: 'SMS-DELIVER', 1: 'SMS-SUBMIT', 2: 'SMS-COMMAND', 3: 'Reserved'}


def _unpack_septets(data, count):
    """Unpack `count` 7-bit septets from the packed GSM default alphabet
    (TS 23.038): septet n lives in bits 7n..7n+6 of the octet string."""
    out = bytearray()
    acc, bits = 0, 0
    for b in data:
        acc |= b << bits
        bits += 8
        while bits >= 7 and len(out) < count:
            out.append(acc & 0x7F)
            acc >>= 7
            bits -= 7
    return bytes(out)


def _parse_udh(ud):
    """Parse an SMS user-data header (TS 23.040 9.2.3.24).

    Returns (fields, octets, septets): the decoded IEs, the UDH length in
    octets and the number of septets it occupies in a 7-bit packed UD."""
    if len(ud) < 2:
        return [], 0, 0
    udhl = ud[0]
    if udhl < 1 or 1 + udhl > len(ud):
        return [], 0, 0
    out = []
    off = 1
    while off + 2 <= 1 + udhl:
        iei, ielen = ud[off], ud[off + 1]
        val = ud[off + 2: off + 2 + ielen]
        if iei == 0x00 and ielen == 3:
            out.append({'label': 'Concat (8-bit ref)',
                        'value': '%d, part %d/%d' % (val[0], val[1], val[2])})
        elif iei == 0x08 and ielen == 4:
            out.append({'label': 'Concat (16-bit ref)',
                        'value': '%d, part %d/%d' % (int.from_bytes(val[:2], 'big'),
                                                     val[2], val[3])})
        else:
            out.append({'label': 'UDH IE 0x%02X' % iei, 'value': val.hex().upper()})
        off += 2 + ielen
    octets = 1 + udhl
    return out, octets, (octets * 8 + 6) // 7


def _decode_sms_ud(pid, dcs, ud, udhi, udl):
    """Decode the TP-UD of an SMS TPDU: the UDH, a secured packet (PID 0x7F,
    TS 31.115) or the text message body for a text DCS (TS 23.038)."""
    out = []
    header_octets, header_septets = 0, 0
    if udhi:
        fields, header_octets, header_septets = _parse_udh(ud)
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


def _sms_tpdu_fields(raw):
    """Parse the TPDU of a SEND SHORT MESSAGE command into the fields a test
    script can assert (the same SMS_SUBMIT parse the log uses): the message
    type, DA digits, PID/DCS, UDL and the raw user data.  None when the
    command carries no TPDU."""
    tpdu_hex = _find_sms_tpdu(raw)
    if not tpdu_hex:
        return None
    try:
        tpdu = bytes.fromhex(tpdu_hex)
        mti = tpdu[0] & 0x03
        out = {'mti': mti, 'tpdu': tpdu_hex.upper()}
        if mti != 1:
            return out
        from pySim.sms import SMS_SUBMIT
        s = SMS_SUBMIT.from_bytes(tpdu)
        out.update({
            'da': str(getattr(s.tp_da, 'digits', '') or '').rstrip('fF'),
            'pid': '%02X' % s.tp_pid,
            'dcs': '%02X' % s.tp_dcs,
            'udl': int(s.tp_udl),
            'ud': bytes(s.tp_ud).hex().upper(),
        })
        return out
    except Exception:
        return None


def _decode_send_sm(raw):
    """Decode a SEND SHORT MESSAGE command (TS 102 223 6.4.10): alpha
    identifier, address and the 3GPP-SMS TPDU with its user data."""
    out = []
    tlvs = httpota.proactive_tlvs(raw)
    alpha = _cmd_tlv(tlvs, 0x05)
    if alpha:
        try:
            out.append({'label': 'Alpha', 'value': _STK_DECODE._decode(alpha, {}, 'stk')})
        except Exception:
            pass
    addr = _cmd_tlv(tlvs, 0x06)
    if addr:
        try:
            from pySim.cat import Address
            a = Address().from_bytes(addr)
            num = str(a.get('call_number') or '').rstrip('fF')
            ton = (a.get('ton_npi') or {}).get('type_of_number')
            out.append({'label': 'Address',
                        'value': num + (' (%s)' % ton if ton else '')})
        except Exception:
            out.append({'label': 'Address', 'value': addr.hex().upper()})
    tpdu = _cmd_tlv(tlvs, 0x0B)
    if not tpdu:
        return out
    out.append({'label': 'SMS TPDU', 'value': tpdu.hex().upper()})
    try:
        mti = tpdu[0] & 0x03
        out.append({'label': 'Type', 'value': _SMS_MTI.get(mti, 'Reserved')})
        if mti == 1:
            from pySim.sms import SMS_SUBMIT
            s = SMS_SUBMIT.from_bytes(tpdu)
            out.append({'label': 'TP-MR', 'value': str(s.tp_mr)})
            if s.tp_da is not None:
                num = str(getattr(s.tp_da, 'digits', '')).rstrip('fF')
                out.append({'label': 'TP-DA', 'value': num})
            out.append({'label': 'TP-PID', 'value': '0x%02X%s' % (
                s.tp_pid, ' (SIM data download)' if s.tp_pid == 0x7F else '')})
            out.append({'label': 'TP-DCS', 'value': '0x%02X' % s.tp_dcs})
            if s.tp_vp is not None:
                out.append({'label': 'TP-VP', 'value': bytes(s.tp_vp).hex().upper()})
            out.append({'label': 'TP-UDL', 'value': str(s.tp_udl)})
            out.extend(_decode_sms_ud(s.tp_pid, s.tp_dcs, bytes(s.tp_ud),
                                      bool(s.tp_udhi), s.tp_udl))
    except Exception:
        # Malformed TPDU: keep the raw hex line above, never break the log.
        pass
    return out


def _decode_cmd(cmd_type, raw, qualifier):
    """Decode a fetched proactive command into [{label, value}] pairs."""
    if not raw:
        return []
    if cmd_type == 0x03:
        dur = _find_duration(raw)
        if dur:
            text = _duration_text(dur[0], dur[1])
            if dur[0] != 0x01:
                text += ' (%d s)' % _duration_seconds(dur[0], dur[1])
            return [{'label': 'Interval', 'value': text}]
        return []
    if cmd_type == 0x05:
        for tag in (0x99, 0x19):
            idx = raw.find(bytes([tag]))
            if idx >= 0 and idx + 1 < len(raw):
                tlen = raw[idx + 1]
                names = [EVENT_NAMES.get(b, 'Event 0x%02X' % b) for b in raw[idx + 2: idx + 2 + tlen]]
                return [{'label': 'Events', 'value': ', '.join(names)}]
        return []
    if cmd_type == 0x13:
        return _decode_send_sm(raw)
    if cmd_type == 0x21:
        text = _parse_display_text(raw)
        return [{'label': 'Text', 'value': text}] if text else []
    if cmd_type in (0x22, 0x23):
        info = (_parse_get_inkey(raw) if cmd_type == 0x22
                else _parse_get_input(raw))
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
        items = _parse_select_item(raw)
        if items:
            return [{'label': 'Items', 'value': ', '.join(_item_line(it) for it in items)}]
        return []
    if cmd_type == 0x25:
        items = _parse_setup_menu_items(raw)
        if items:
            return [{'label': 'Items', 'value': ', '.join(_item_line(it) for it in items)}]
        return []
    if cmd_type in (0x40, 0x42, 0x43):
        return _decode_bip_cmd(cmd_type, raw)
    if cmd_type == 0x26 and qualifier is not None:
        name = PLI_QUALIFIER_NAMES.get(qualifier, 'Unknown')
        return [{'label': 'Qualifier', 'value': '%s (0x%02X)' % (name, qualifier)}]
    if cmd_type == 0x27:
        out = []
        if qualifier is not None:
            action = _TIMER_ACTIONS.get(qualifier & 0x03)
            out.append({'label': 'Action',
                        'value': action or 'Reserved (0x%02X)' % qualifier})
        tlvs = httpota.proactive_tlvs(raw)
        timer = _cmd_tlv(tlvs, 0x24)
        if timer:
            tv = timer[0]
            out.append({'label': 'Timer',
                        'value': str(tv) if 1 <= tv <= 8 else 'Invalid (0x%02X)' % tv})
        value = _cmd_tlv(tlvs, 0x25)
        if len(value) >= 3:
            out.append({'label': 'Value', 'value': '%02d:%02d:%02d' % (
                _bcd_swap(value[0]), _bcd_swap(value[1]), _bcd_swap(value[2]))})
        return out
    return [{'label': 'Data', 'value': raw.hex()}]


def _decode_tr(type_hex, qual_hex, tr_hex):
    """Decode the command-specific payload of a TERMINAL RESPONSE into
    [{label, value}] pairs (empty list when the TR carried no data)."""
    if not tr_hex:
        return []
    h = re.sub(r'\s', '', tr_hex)
    try:
        cmd_type = int(type_hex, 16) if type_hex else None
    except ValueError:
        return [{'label': 'Data', 'value': h}]
    if cmd_type == 0x03 and len(h) >= 8 and h[0:2] == '84':
        return [{'label': 'Interval', 'value': '%d s' % int(h[6:8], 16)}]
    if cmd_type == 0x27:
        tlvs = _tlv_map(bytes.fromhex(h))
        out = []
        tid = _cmd_tlv(tlvs, 0x24)
        if tid:
            out.append({'label': 'Timer', 'value': str(tid[0])})
        val = _cmd_tlv(tlvs, 0x25)
        if len(val) >= 3:
            out.append({'label': 'Remaining', 'value': '%02d:%02d:%02d' % (
                _bcd_swap(val[0]), _bcd_swap(val[1]), _bcd_swap(val[2]))})
        return out or [{'label': 'Data', 'value': h}]
    if cmd_type == 0x26 and qual_hex:
        try:
            qual = int(qual_hex, 16)
        except ValueError:
            qual = None
        if qual in (0x00, 0x01, 0x03, 0x04, 0x05, 0x06, 0x08, 0x09, 0x0A, 0x0E):
            v = h[4:] if len(h) >= 4 else ''
            if qual == 0x00 and len(v) >= 10:
                mcc, mnc = _dec_plmn(v[:6])
                return [{'label': 'MCC', 'value': mcc}, {'label': 'MNC', 'value': mnc},
                        {'label': 'LAC/TAC', 'value': v[6:10].upper()}]
            if qual == 0x01 and len(v) >= 16:
                return [{'label': 'IMEI', 'value': _dec_imei(v[:16])}]
            if qual == 0x03 and len(v) >= 14:
                # Date-Time and Time zone (TS 102 223 8.39): TP-SCTS coding
                # (TS 123 040 9.2.3.11) - swapped semi-octet BCD digits, the
                # time zone sign in bit 0x08 of the last byte, 'FF' unknown.
                b = bytes.fromhex(v[:14])
                y = _bcd_swap(b[0])
                yr = 2000 + y if y < 70 else 1900 + y
                out = [{'label': 'Date',
                        'value': '%04d-%02d-%02d' % (yr, _bcd_swap(b[1]), _bcd_swap(b[2]))},
                       {'label': 'Time',
                        'value': '%02d:%02d:%02d' % (_bcd_swap(b[3]), _bcd_swap(b[4]), _bcd_swap(b[5]))}]
                if b[6] == 0xFF:
                    out.append({'label': 'TZ offset', 'value': 'unknown'})
                else:
                    q = (b[6] & 0x07) * 10 + (b[6] >> 4)
                    out.append({'label': 'TZ offset',
                                'value': '%s%02d:%02d' % ('-' if b[6] & 0x08 else '+',
                                                          q // 4, (q % 4) * 15)})
                return out
            if qual == 0x04 and len(v) >= 4:
                try:
                    lang = bytes.fromhex(v[:4]).decode('ascii')
                except Exception:
                    lang = v[:4]
                return [{'label': 'Language', 'value': lang}]
            if qual == 0x05 and len(v) >= 4:
                return [{'label': 'ME Status', 'value': str(int(v[0:2], 16))},
                        {'label': 'Timing Advance', 'value': str(int(v[2:4], 16))}]
            if qual == 0x06 and len(v) >= 2:
                tech = int(v[0:2], 16)
                return [{'label': 'Access Technology',
                         'value': '%s (%d)' % (ACCESSTECH_NAMES.get(tech, 'Unknown'), tech)}]
            if qual == 0x08 and len(v) >= 16:
                return [{'label': 'IMEISV', 'value': _dec_imei(v[:16]) + v[14:16].upper()}]
            if qual == 0x09 and len(v) >= 2:
                mode = int(v[0:2], 16)
                return [{'label': 'Search Mode', 'value': 'Manual' if mode == 1 else ('Automatic' if mode == 0 else str(mode))}]
            if qual == 0x0A and len(v) >= 2:
                return [{'label': 'Charge state (%)', 'value': str(int(v[0:2], 16))}]
            if qual == 0x0E:
                techs = [int(v[i:i + 2], 16) for i in range(0, len(v), 2)]
                return [{'label': 'Access Technologies',
                         'value': ', '.join(ACCESSTECH_NAMES.get(t, 'Unknown') for t in techs)}]
            return [{'label': 'Data', 'value': h}]
    return [{'label': 'Data', 'value': h}]


def _bip_channel_id(dev_dst):
    return (dev_dst & 0x07) if 0x21 <= dev_dst <= 0x27 else None


def _bip_tr(cmd_num, cmd_type, cmd_qual, dev_src, dev_dst, result=0x00, info=None, extra=b''):
    """TERMINAL RESPONSE payload for a BIP command.

    Tags follow the captured real-terminal traces (comprehension TLVs 01/02/03)
    and the result precedes the optional Channel status / data TLVs.
    """
    tr = bytes([0x01, 0x03, cmd_num & 0xFF, cmd_type & 0xFF, (cmd_qual if cmd_qual is not None else 0) & 0xFF,
                0x02, 0x02, 0x82, 0x81])
    if info is None:
        tr += bytes([0x03, 0x01, result & 0xFF])
    else:
        tr += bytes([0x03, 0x02, result & 0xFF, info & 0xFF])
    return tr + extra


def _decode_bip_cmd(cmd_type, raw):
    tlvs = httpota.proactive_tlvs(raw)
    out = []
    dev = _cmd_tlv(tlvs, 0x02)
    if len(dev) >= 2 and 0x21 <= dev[1] <= 0x27:
        out.append({'label': 'Channel', 'value': str(dev[1] & 0x07)})
    if cmd_type == 0x40:
        bearer = _cmd_tlv(tlvs, httpota.TAG_BEARER)
        if bearer:
            out.append({'label': 'Bearer', 'value': '0x%02X' % bearer[0]})
        bs = _cmd_tlv(tlvs, httpota.TAG_BUFFER_SIZE)
        if len(bs) >= 2:
            out.append({'label': 'Buffer size', 'value': str(int.from_bytes(bs[:2], 'big'))})
        naa = _cmd_tlv(tlvs, httpota.TAG_NAA)
        if naa:
            out.append({'label': 'APN', 'value': naa[1:].decode('ascii', 'replace')})
        addr = httpota.parse_other_address(_cmd_tlv(tlvs, httpota.TAG_OTHER_ADDRESS))
        if addr:
            out.append({'label': 'Destination', 'value': addr})
        proto, port = httpota.parse_transport_level(_cmd_tlv(tlvs, httpota.TAG_TRANSPORT_LEVEL))
        if port is not None:
            out.append({'label': 'Transport', 'value': '%s port %d' % ({0x02: 'TCP client'}.get(proto, 'proto 0x%02X' % (proto or 0)), port)})
    elif cmd_type == 0x42:
        req = _cmd_tlv(tlvs, httpota.TAG_CHANNEL_DATA_LENGTH)
        if req:
            out.append({'label': 'Requested bytes', 'value': str(req[0])})
    elif cmd_type == 0x43:
        data = _cmd_tlv(tlvs, httpota.TAG_CHANNEL_DATA)
        out.append({'label': 'Data bytes', 'value': str(len(data))})
        if data:
            out.append({'label': 'Data', 'value': data.hex()[:120]})
    return out


def _handle_bip_command(scc, cmd_num, cmd_type, cmd_qual, raw, dev_src, dev_dst):
    """Handle a BIP proactive command. Returns TR payload bytes, or None for the generic path."""
    tlvs = httpota.proactive_tlvs(raw)
    channel = _bip_channel_id(dev_dst)
    if cmd_type == 0x40:
        bs = _cmd_tlv(tlvs, httpota.TAG_BUFFER_SIZE) or b'\x02\x00'
        buffer_size = int.from_bytes(bs[:2], 'big') if len(bs) >= 2 else 0x0200
        bearer = _cmd_tlv(tlvs, httpota.TAG_BEARER) or b'\x03'
        extra = bytes([httpota.TAG_BEARER, len(bearer)]) + bearer
        extra += bytes([httpota.TAG_BUFFER_SIZE, 0x02]) + buffer_size.to_bytes(2, 'big')
        if not _BIP.enabled:
            _BIP.log('open-unavailable', reason='BIP not enabled')
            return _bip_tr(cmd_num, cmd_type, cmd_qual, dev_src, dev_dst, 0x3A, 0x00, extra)
        addr = httpota.parse_other_address(_cmd_tlv(tlvs, httpota.TAG_OTHER_ADDRESS))
        proto, port = httpota.parse_transport_level(_cmd_tlv(tlvs, httpota.TAG_TRANSPORT_LEVEL))
        if not addr or port is None:
            # Emulation is deliberately permissive: the APN, destination and
            # transport are informational, the channel always goes to the
            # configured local target (the live card emits truncated/empty
            # destination TLVs - see the AGENTS.md HTTP OTA notes).
            _BIP.log('open-relaxed', address=addr, port=port,
                     note='destination/transport not fully specified')
        cid, err = _BIP.open(addr or '-', port or 0, buffer_size, proto=proto)
        if cid is None:
            return _bip_tr(cmd_num, cmd_type, cmd_qual, dev_src, dev_dst, 0x3A, 0x00, extra)
        if cmd_qual and (cmd_qual & 0x04):
            # Background mode: the terminal shall inform the UICC that the
            # link was established (TS 102 223 7.5.11).
            _BIP._queue_link_status(cid, status=0x80 | (cid & 0x07), info=0x00)
        status = bytes([httpota.TAG_CHANNEL_STATUS, 0x02, 0x80 | (cid & 0x07), 0x00])
        return _bip_tr(cmd_num, cmd_type, cmd_qual, dev_src, dev_dst, 0x00, None, status + extra)
    if cmd_type == 0x43:
        data = _cmd_tlv(tlvs, httpota.TAG_CHANNEL_DATA)
        if channel is None or not _BIP.send(channel, data):
            return _bip_tr(cmd_num, cmd_type, cmd_qual, dev_src, dev_dst, 0x3A, 0x00)
        length = _BIP.send_capacity(channel)
        return _bip_tr(cmd_num, cmd_type, cmd_qual, dev_src, dev_dst, 0x00, None,
                       bytes([httpota.TAG_CHANNEL_DATA_LENGTH, 0x01, length & 0xFF]))
    if cmd_type == 0x42:
        req = _cmd_tlv(tlvs, httpota.TAG_CHANNEL_DATA_LENGTH) or b'\x00'
        n = req[0] if req else 0
        if channel is None:
            return _bip_tr(cmd_num, cmd_type, cmd_qual, dev_src, dev_dst, 0x3A, 0x00)
        data = _BIP.receive(channel, n)
        if data is None:
            return _bip_tr(cmd_num, cmd_type, cmd_qual, dev_src, dev_dst, 0x3A, 0x00)
        remaining = _BIP.available(channel)
        # The channel data TLV length is BER-encoded: a single byte only up
        # to 127, then the 0x81 long form (the reference terminal traces use
        # `36 81 ed` for a 237-byte chunk). With a raw length byte >0x7F the
        # card reads a malformed TLV and the record bytes never reach its
        # TLS layer (it fetches, accepts, and never processes the response).
        extra = b''
        if data:
            if len(data) <= 0x7F:
                extra = bytes([httpota.TAG_CHANNEL_DATA, len(data)]) + data
            else:
                extra = bytes([httpota.TAG_CHANNEL_DATA, 0x81, len(data)]) + data
        extra += bytes([httpota.TAG_CHANNEL_DATA_LENGTH, 0x01, 0xFF if remaining > 0xFF else remaining])
        return _bip_tr(cmd_num, cmd_type, cmd_qual, dev_src, dev_dst, 0x00, None, extra)
    if cmd_type == 0x41:
        if channel is None or not _BIP.close(channel):
            return _bip_tr(cmd_num, cmd_type, cmd_qual, dev_src, dev_dst, 0x3A, 0x00)
        return _bip_tr(cmd_num, cmd_type, cmd_qual, dev_src, dev_dst, 0x00)
    if cmd_type == 0x44:
        extra = b''
        for cid in sorted(_BIP.channels):
            extra += bytes([httpota.TAG_CHANNEL_STATUS, 0x02, 0x80 | (cid & 0x07), 0x00])
        return _bip_tr(cmd_num, cmd_type, cmd_qual, dev_src, dev_dst, 0x00, None, extra)
    return None


def _bip_listener_status():
    if not _BIP_LISTENER:
        if _BIP_MODE == 'redirect' and _BIP_TARGET:
            return {'mode': 'redirect', 'host': _BIP_TARGET[0],
                    'port': _BIP_TARGET[1],
                    'target': '%s:%d' % _BIP_TARGET}
        if _BIP_MODE == 'passthru':
            # No listener and no pinned target: every channel dials the
            # destination the card requests (per-channel targets in the
            # BIP status).
            return {'mode': 'passthru'}
        return None
    if isinstance(_BIP_LISTENER, scp81.PskTlsServer):
        return {'mode': 'tls', 'host': _BIP_LISTENER.host, 'port': _BIP_LISTENER.port,
                'psk_identities': _BIP_LISTENER.psk_identities,
                'psk_wildcard': _BIP_LISTENER.wildcard_psk is not None,
                'identity_seen': _BIP_LISTENER.identity_seen,
                'identity_matched': _BIP_LISTENER.identity_matched,
                'version_seen': _BIP_LISTENER.version_seen,
                'cipher_seen': _BIP_LISTENER.cipher_seen,
                'chunked': _BIP_LISTENER.chunked,
                'chunk_size': _BIP_LISTENER.chunk_size,
                'compact_headers': _BIP_LISTENER.compact_headers,
                'tls_version': _BIP_LISTENER.tls_version,
                'cipher': _BIP_LISTENER.cipher}
    return {'mode': _BIP_MODE or 'dump', 'host': _BIP_LISTENER.host,
            'port': _BIP_LISTENER.port,
            'connections': _BIP_LISTENER.accepted}


def _bip_data_available(ch):
    """Monitor-thread callback: tell the card there is server data to fetch.

    TS 102 223 7.5.10 - ENVELOPE (Event Download - Data available) carries the
    Channel status and the number of bytes waiting; the card then issues
    RECEIVE DATA. Returns True when the event was sent (the caller then marks
    the bytes as notified)."""
    server = _server_ref
    if not server or not _CARD_CONNECTED or ch.peer_closed:
        return False
    ev_list = getattr(server, 'event_list', None) or []
    if 0x09 not in ev_list or not ch.rx:
        return False
    with _CARD_LOCK:
        server = _server_ref
        if not server or not _CARD_CONNECTED or not getattr(server, 'scc', None):
            return False
        if _PROACTIVE_BUSY or getattr(server, 'stk_pending', None):
            return False
        length = min(len(ch.rx), 0xFF)
        tlv = bytes([0xB8, 0x02, 0x80 | (ch.id & 0x07), 0x00,
                     0xB7, 0x01, length])
        try:
            _send_event_download(server.scc, 0x09, tlv)
        except Exception as e:
            _BIP.log('data-available-skip', channel=ch.id, reason=str(e))
            return False
        _BIP.log('data-available', channel=ch.id, length=length)
        return True


# ---- SCP81 command scripting (GP RAM over HTTP, TS 102 226 5.2) -----------
# The administration server answers the card's POST with one C-APDU per
# request (Command Scripting template 'AE 80 22 <len> <apdu> 00 00',
# indefinite length as recommended for RAM over HTTPS) and reads the R-APDU
# from the next POST's Response Scripting template ('AB'/'AF', with '80'
# executed-count and '23' R-APDU TLVs whose last two bytes are SW1 SW2).

# The command script served to the card, set by the PWA at listener start (an
# explicit APDU list) or via /api/scp81/queue. The server is agnostic to what
# the APDUs do: it serves them one per administration POST and tracks
# execution so a resumed session sends only the leftover APDUs.
#   _SCP81_SCRIPT_BASE   the configured APDU list (never mutated)
#   _SCP81_SCRIPT_NEXT   index in BASE of the next APDU to send
#   _SCP81_SCRIPT_DONE   BASE indices the card reported (executed, any result)
#   _SCP81_SCRIPT_PENDING the APDU sent in the previous POST, waiting for the
#                        card's X-Admin-Script-Status report (None = none); a
#                        session that dies before the report resends it
#   _SCP81_SCRIPT_RESULTS one entry per reported APDU
#                        {index, pos, page, apdu, rapdu, sw}
#   _SCP81_SCRIPT_PAGE_QUEUE continuation pages auto-inserted for truncated
#                        GET STATUS listings (sent before BASE[NEXT])
_SCP81_SCRIPT_BASE = []
_SCP81_SCRIPT_NEXT = 0
_SCP81_SCRIPT_DONE = set()
_SCP81_SCRIPT_PENDING = None
_SCP81_SCRIPT_RESULTS = []
_SCP81_SCRIPT_PAGE_QUEUE = []
_SCP81_SCRIPT_SENT_NO = 0
_SCP81_PAGES = 0
SCP81_MAX_PAGES = 24
# What the queued script is ('none', 'custom' or the name the PWA sent).
_SCP81_SCRIPT_KIND = 'none'


def _scp81_restart_run():
    """Start a new run over the configured script (fresh dialog): clear the
    execution marks, the continuation pages and the results."""
    global _SCP81_SCRIPT_NEXT, _SCP81_SCRIPT_DONE, _SCP81_SCRIPT_PENDING
    global _SCP81_SCRIPT_RESULTS, _SCP81_SCRIPT_PAGE_QUEUE, _SCP81_PAGES
    global _SCP81_SCRIPT_SENT_NO
    _SCP81_SCRIPT_NEXT = 0
    _SCP81_SCRIPT_DONE = set()
    _SCP81_SCRIPT_PENDING = None
    _SCP81_SCRIPT_RESULTS = []
    _SCP81_SCRIPT_PAGE_QUEUE = []
    _SCP81_SCRIPT_SENT_NO = 0
    _SCP81_PAGES = 0


def _scp81_script_mid_run():
    """True while a run has started and not finished (queue replaces it only
    with force)."""
    return (0 < _SCP81_SCRIPT_NEXT < len(_SCP81_SCRIPT_BASE)
            or _SCP81_SCRIPT_PENDING is not None
            or bool(_SCP81_SCRIPT_PAGE_QUEUE))


def _scp81_reset_script(script, kind):
    """Install a new APDU script and clear all execution progress."""
    global _SCP81_SCRIPT_BASE, _SCP81_SCRIPT_KIND
    _SCP81_SCRIPT_BASE = [re.sub(r'\s', '', a).upper() for a in script if a]
    _SCP81_SCRIPT_KIND = kind
    _scp81_restart_run()
    _BIP.log('script-queued', script_kind=kind, apdus=len(_SCP81_SCRIPT_BASE))


def _scp81_queue_script(apdus, kind='custom', force=False):
    """Replace the SCP81 command script with a new APDU list. Refuses while
    a script is mid-run unless forced; the list runs on the card's next POST."""
    if not force and _scp81_script_mid_run():
        return {'queued': False, 'reason': 'script in progress',
                'next': _SCP81_SCRIPT_NEXT, 'of': len(_SCP81_SCRIPT_BASE)}
    _scp81_reset_script(apdus, kind)
    return {'queued': True, 'apdus': len(_SCP81_SCRIPT_BASE)}
_SCP81_SCRIPT_TEMPLATE = 'indefinite'
_SCP81_SCRIPT_CR_TAG = False
# None = short per-command Next-URI ('/N'); '' = omit the header (spec: the
# card executes the script, sends no response string and closes the session).
_SCP81_NEXT_URI = None
# Optional X-Admin-Targeted-Application header (spec syntax //aid/<RID>/<PIX>).
# When it names an application that does not exist on the card, the SD answers
# with X-Admin-Script-Status: unknown-application instead of executing.
_SCP81_TARGETED_APP = None
# The listener's chunked flag (mirrored here: a chunked response must not
# carry Content-Length - invalid HTTP; the Transfer-Encoding header itself is
# emitted by build_http_response).
_SCP81_CHUNKED = False
# Send automatic Channel status (link dropped) events to the card. Suppress
# while testing flows where the terminal closes the connection on purpose:
# the card must drain the buffered response and resume on a new connection.
_BIP_LINK_EVENTS = True


def _ber_len_bytes(n):
    """BER-TLV length as bytes (short form up to 127, then 0x81/0x82)."""
    if n < 0x80:
        return bytes([n])
    if n < 0x100:
        return bytes([0x81, n])
    return bytes([0x82, n >> 8, n & 0xFF])


def _scp81_command_body(apdu_hex, definite=False, cr_tag=False):
    """Command Scripting template with one C-APDU TLV: the indefinite-length
    variant ('AE 80 22 <len> <apdu> 00 00', recommended for RAM over HTTPS) or
    the definite-length one ('AA <len> 22 <len> <apdu>'). The C-APDU TLV tag
    is '22' per TS 101 220 (CR flag 0); some cards expect the CR-set 'A2'
    instead, so it is configurable. Both lengths are BER-encoded: a raw byte
    above 0x7F is read as a long-form marker by the card, which garbles the
    script (the 245-byte LOAD commands of a RAM install, live 2026-09-16)."""
    apdu = bytes.fromhex(re.sub(r'\s', '', apdu_hex))
    cmd_tlv = bytes([0xA2 if cr_tag else 0x22]) + _ber_len_bytes(len(apdu)) + apdu
    if definite:
        return bytes([0xAA]) + _ber_len_bytes(len(cmd_tlv)) + cmd_tlv
    return bytes([0xAE, 0x80]) + cmd_tlv + b'\x00\x00'


def _scp81_parse_response(body):
    """Parse a Response Scripting template (TS 102 226 5.2.2, definite 'AB'
    or indefinite 'AF'); returns (executed_count, [(rapdu, sw_hex), ...])."""
    if not body:
        return 0, []
    if body[0] == 0xAF and len(body) >= 2 and body[1] == 0x80:
        content = body[2:-2] if body.endswith(b'\x00\x00') else body[2:]
    elif body[0] == 0xAB:
        ln, off = httpota.ber_len_read(body, 1)
        content = body[off:off + ln]
    else:
        content = body
    count, out = 0, []
    off = 0
    while off < len(content):
        tag = content[off]
        # BER lengths: an R-APDU TLV above 127 bytes is `23 81 FC ...`; a raw
        # byte >0x7F used as a length silently truncated every listing page
        # to 127 bytes (live 2026-09-16), hiding later registry entries.
        tlen, voff = httpota.ber_len_read(content, off + 1)
        val = content[voff:voff + tlen]
        if len(val) < tlen:
            break
        off = voff + tlen
        if tag == 0x80:
            count = int.from_bytes(val, 'big') if val else 0
        elif tag in (0x23, 0xA3) and len(val) >= 2:
            out.append((val[:-2], val[-2:].hex().upper()))
    return count, out


def _scp81_decode_memory(rapdu):
    """GET DATA FF21 value: 81 applet count, 82 free NV (3 B), 83 free volatile."""
    if len(rapdu) < 5 or rapdu[0] != 0xFF or rapdu[1] != 0x21:
        return None
    content = rapdu[3:3 + rapdu[2]]
    out = {}
    off = 0
    while off + 1 < len(content):
        tag, tlen = content[off], content[off + 1]
        val = content[off + 2:off + 2 + tlen]
        off += 2 + tlen
        if tag in (0x81, 0x01):
            out['applets'] = int.from_bytes(val, 'big')
        elif tag in (0x82, 0x02):
            out['free_nv'] = int.from_bytes(val, 'big')
        elif tag in (0x83, 0x03):
            out['free_volatile'] = int.from_bytes(val, 'big')
    return out or None


def _scp81_continuation(apdu):
    """Continuation APDU for a truncated GET STATUS page, or None.

    GET STATUS P2.b1 distinguishes first/all (0) from the *next* batch (1)
    of the matches for the SAME search criteria; the pagination state lives
    in the card, so the continuation is the same command with P2.b1 set.
    Using a changed search criterion (the last returned AID) was rejected
    with SW 6A80 - the criterion is a match filter, not a position."""
    u = apdu.upper()
    if not u.startswith('80F2') or len(u) < 8:
        return None
    p2 = int(u[6:8], 16) | 0x01
    return '%s%02X%s' % (u[:6], p2, u[8:])


def _scp81_script_state():
    """State of the configured command script for `GET /api/scp81/script`.

    Progress counts only the configured APDUs (`total`/`done`); the C-APDU
    awaiting the card's report is reported as `pending` ({index, pos, page,
    apdu} or null) and the auto-inserted listing continuation pages as
    `pages`/`pages_queued`, so a run whose script APDUs are all executed is
    not mistaken for 'still pending' while a page is in flight."""
    pending = None
    if _SCP81_SCRIPT_PENDING is not None:
        p = _SCP81_SCRIPT_PENDING
        pending = {'index': p['index'], 'pos': p.get('pos'),
                   'page': bool(p.get('page')), 'apdu': p['apdu']}
    return {'script': list(_SCP81_SCRIPT_BASE),
            'next': _SCP81_SCRIPT_NEXT,
            'total': len(_SCP81_SCRIPT_BASE),
            'done': sorted(_SCP81_SCRIPT_DONE),
            'pending': pending,
            'pages': _SCP81_PAGES,
            'pages_queued': len(_SCP81_SCRIPT_PAGE_QUEUE),
            'complete': (len(_SCP81_SCRIPT_DONE) >= len(_SCP81_SCRIPT_BASE)
                         and _SCP81_SCRIPT_PENDING is None
                         and not _SCP81_SCRIPT_PAGE_QUEUE),
            'kind': _SCP81_SCRIPT_KIND,
            'template': _SCP81_SCRIPT_TEMPLATE,
            'cr_tag': _SCP81_SCRIPT_CR_TAG,
            'results': _SCP81_SCRIPT_RESULTS}


def _scp81_script_responder(method, target, headers, body):
    """Remote Administration Server side of the administration session: send
    the next scripted C-APDU or close the session (TS 102 226 / GP 4.4.2).

    Execution tracking: an APDU counts as executed only when the card reports
    it in the next POST (X-Admin-Script-Status, with the Response Scripting
    template on success). A session that dies before the report leaves the
    APDU pending: a resumed dialog (X-Admin-Resume) resends it, while a POST
    without that header is a fresh dialog where the script runs from the
    start (so a completed script runs again on a new trigger)."""
    global _SCP81_SCRIPT_NEXT, _SCP81_SCRIPT_DONE, _SCP81_SCRIPT_PENDING
    global _SCP81_SCRIPT_RESULTS, _SCP81_SCRIPT_PAGE_QUEUE, _SCP81_PAGES
    global _SCP81_SCRIPT_SENT_NO
    status = headers.get('x-admin-script-status')
    resume = headers.get('x-admin-resume')
    pending = _SCP81_SCRIPT_PENDING
    if pending is not None:
        _SCP81_SCRIPT_PENDING = None
        index = pending['index']
        if status is not None:
            # The card reports the outcome of the pending C-APDU.
            if status != 'ok':
                _BIP.log('script-status', index=index, status=status)
            else:
                count, rapdus = _scp81_parse_response(body)
                for rapdu, sw in rapdus:
                    _BIP.log('script-rapdu', index=index, sw=sw, bytes=len(rapdu),
                             hex=rapdu.hex().upper()[:2000])
                    _SCP81_SCRIPT_RESULTS.append(
                        {'index': index, 'pos': pending.get('pos'),
                         'page': bool(pending.get('page')), 'sw': sw,
                         'apdu': pending['apdu'],
                         'rapdu': rapdu.hex().upper()})
                if rapdus:
                    if pending['apdu'].startswith('80CAFF21'):
                        decoded = _scp81_decode_memory(rapdus[-1][0])
                        if decoded:
                            _BIP.log('script-memory', **decoded)
                    # '63 10' = "more data available" (GP Table 11-38); the
                    # live card uses a proprietary 'CA FE' for the same case.
                    if (rapdus[-1][1].upper() in ('CAFE', '6310')
                            and _SCP81_PAGES < SCP81_MAX_PAGES):
                        cont = _scp81_continuation(pending['apdu'])
                        if cont:
                            _SCP81_PAGES += 1
                            _SCP81_SCRIPT_PAGE_QUEUE.append(cont)
                            _BIP.log('script-page', index=index,
                                     page=_SCP81_PAGES, apdu=cont)
            if pending.get('pos') is not None:
                _SCP81_SCRIPT_DONE.add(pending['pos'])
        elif resume:
            # Resumed dialog: the pending APDU was never reported, so it is
            # still unexecuted - put it back in line and resend it.
            if pending.get('page'):
                _SCP81_SCRIPT_PAGE_QUEUE.insert(0, pending['apdu'])
            else:
                _SCP81_SCRIPT_NEXT = pending['pos']
                _SCP81_SCRIPT_DONE.discard(pending['pos'])
            _BIP.log('script-resend', index=index, apdu=pending['apdu'])
        else:
            # Fresh dialog (new trigger): the script runs from the start.
            _scp81_restart_run()
    elif status is not None:
        # A report without a pending APDU (the server may have restarted
        # mid-session): log the R-APDUs but do not advance anything.
        if status != 'ok':
            _BIP.log('script-status', index=None, status=status)
        else:
            count, rapdus = _scp81_parse_response(body)
            for rapdu, sw in rapdus:
                _BIP.log('script-rapdu', index=None, sw=sw, bytes=len(rapdu),
                         hex=rapdu.hex().upper()[:2000])
    elif not resume:
        # First POST of a fresh dialog: reset a previous run.
        _scp81_restart_run()
    apdu = None
    if _SCP81_SCRIPT_PAGE_QUEUE:
        apdu = _SCP81_SCRIPT_PAGE_QUEUE.pop(0)
        _SCP81_SCRIPT_PENDING = {'index': _SCP81_SCRIPT_SENT_NO + 1,
                                 'pos': None, 'page': True, 'apdu': apdu}
    elif _SCP81_SCRIPT_NEXT < len(_SCP81_SCRIPT_BASE):
        pos = _SCP81_SCRIPT_NEXT
        apdu = _SCP81_SCRIPT_BASE[pos]
        _SCP81_SCRIPT_NEXT = pos + 1
        _SCP81_SCRIPT_PENDING = {'index': _SCP81_SCRIPT_SENT_NO + 1,
                                 'pos': pos, 'page': False, 'apdu': apdu}
    if apdu is not None:
        index = _SCP81_SCRIPT_PENDING['index']
        _SCP81_SCRIPT_SENT_NO = index
        _BIP.log('script-send', index=index, apdu=apdu)
        headers = _scp81_response_headers()
        if _SCP81_TARGETED_APP:
            headers['X-Admin-Targeted-Application'] = _SCP81_TARGETED_APP
        # The working reference session (samples/HTTPOTA_session_3311_success1)
        # answers with a relative URI plus a QUERY (/Download?req=N): a
        # query-less Next-URI makes the card abort the TLS session. A '%d'
        # in the configured/default URI is replaced with the command number.
        template = _SCP81_NEXT_URI if _SCP81_NEXT_URI is not None else '/api/scp81?req=%d'
        next_uri = template % index if '%d' in template else template
        if next_uri:
            headers['X-Admin-Next-URI'] = next_uri
        u = apdu.upper()
        if u.startswith('AA') or u.startswith('AE80'):
            # Already a Command Scripting template (expanded format): send it
            # verbatim instead of wrapping it again.
            body_out = bytes.fromhex(u)
        else:
            body_out = _scp81_command_body(
                apdu, definite=(_SCP81_SCRIPT_TEMPLATE == 'definite'),
                cr_tag=_SCP81_SCRIPT_CR_TAG)
        # Transfer-Encoding / Content-Length are emitted by the HTTP builder
        # (chunked never carries a Content-Length).
        headers['Content-Type'] = scp81.GP_CT_COMMAND
        return 200, headers, body_out
    _BIP.log('script-done', sent=_SCP81_SCRIPT_SENT_NO,
             results=len(_SCP81_SCRIPT_RESULTS))
    return 204, _scp81_response_headers(), b''


def _scp81_response_headers():
    """Base response headers: only what the administration dialog needs."""
    return {'X-Admin-Protocol': scp81.GP_PROTOCOL}


def _parse_psk_map(raw):
    """Parse a PSK lookup table from an API request.

    Accepts a list of {identity, psk_hex} objects (the PWA form) or a
    {identity: psk_hex} map; entries without a usable identity or key are
    skipped. Returns (table, error)."""
    if raw is None:
        return None, None
    if isinstance(raw, dict):
        raw = [{'identity': k, 'psk_hex': v} for k, v in raw.items()]
    if not isinstance(raw, list):
        return None, 'psk_map must be a list of {identity, psk_hex}'
    table = {}
    for item in raw:
        if not isinstance(item, dict):
            return None, 'psk_map entries must be {identity, psk_hex} objects'
        ident = str(item.get('identity') or '').strip()
        key_hex = re.sub(r'\s', '', str(item.get('psk_hex') or ''))
        if not key_hex or not ident:
            continue
        try:
            key = bytes.fromhex(key_hex)
        except ValueError:
            return None, 'psk_map[%s]: psk_hex is not valid hex' % ident
        if key:
            table[ident] = key
    return table, None


def _redact_psk_fields(body):
    """Copy of a request body with key material masked (keys must never
    reach the logs; identities stay visible for diagnostics)."""
    if not isinstance(body, dict):
        return body
    out = dict(body)
    if out.get('psk_hex'):
        out['psk_hex'] = '<redacted>'
    if out.get('adm'):
        out['adm'] = '<redacted>'
    psk_map = out.get('psk_map')
    if isinstance(psk_map, dict):
        out['psk_map'] = {k: '<redacted>' for k in psk_map}
    elif isinstance(psk_map, list):
        out['psk_map'] = [
            dict(e, psk_hex='<redacted>')
            if isinstance(e, dict) and e.get('psk_hex') else e
            for e in psk_map]
    return out


def _scp81_update_psk_map(body):
    """Replace the PSK table of the running TLS listener. The card presets are
    the source of truth; the PWA pushes edits without a listener restart."""
    global _SCP81_PSKS, _SCP81_PSK_LEGACY
    table, err = _parse_psk_map((body or {}).get('psk_map'))
    if err:
        return {'ok': False, 'error': err}
    if not table:
        return {'ok': False, 'error': 'no usable PSK entries (identity + key required)'}
    if not isinstance(_BIP_LISTENER, scp81.PskTlsServer):
        return {'ok': False, 'error': 'PSK TLS listener is not running'}
    _BIP_LISTENER.set_psk_map(table)
    _SCP81_PSKS = dict(table)
    _SCP81_PSK_LEGACY = None
    _BIP.log('tls-psk-map', identities=sorted(table))
    return {'ok': True, 'identities': sorted(table),
            'listener': _bip_listener_status()}


def _scp81_gen_install(body):
    """Generate the RAM APDU sequence for a .cap without touching the listener
    or the running script. The PWA uses it to turn an 'Install from .cap'
    script template into INSTALL [for load] / LOAD blocks / INSTALL [for
    install] APDUs; the .cap itself is never stored."""
    body = body or {}
    cap_hex = re.sub(r'\s', '', body.get('cap_hex') or '')
    if not cap_hex:
        return {'ok': False, 'error': 'No cap_hex provided'}
    try:
        loadfile_aid, module_aid, loadfile_data = _cap_parse(cap_hex)
        seq = _cap_apdu_sequence(
            loadfile_aid, module_aid, loadfile_data,
            sd_aid=re.sub(r'\s', '', body.get('sd_aid') or ''),
            privileges=re.sub(r'\s', '', body.get('privileges') or '') or '00',
            install_params=re.sub(r'\s', '', body.get('install_params') or ''),
            stk_params=re.sub(r'\s', '', body.get('stk_params') or ''),
            make_selectable=bool(body.get('make_selectable', True)),
            nv_quota=body.get('nv_quota'), volatile_quota=body.get('volatile_quota'))
    except ValueError as e:
        # the install-parameter composition rule (or a CAP parse refusal)
        return {'ok': False, 'error': str(e)}
    except Exception as e:
        return {'ok': False, 'error': 'cap parse failed: %s' % e}
    _BIP.log('gen-install', apdus=len(seq), load_file_aid=loadfile_aid,
             module_aid=module_aid)
    return {'ok': True, 'apdus': seq, 'load_file_aid': loadfile_aid,
            'module_aid': module_aid}


def _cap_info_body(body):
    """Validate a .cap archive and estimate its memory requirements.

    Read-only: runs the same structural parse as the install paths (so a
    corrupt/wrong file fails here first) plus the capmem analyzer.  Never
    touches the card, the SCP81 listener or the scripts; the estimate is
    informational - the install paths stay self-sufficient."""
    body = body or {}
    cap_hex = re.sub(r'\s', '', body.get('cap_hex') or '')
    if not cap_hex:
        return {'ok': False, 'error': 'No cap_hex provided'}
    try:
        loadfile_aid, module_aid, loadfile_data = _cap_parse(cap_hex)
    except Exception as e:
        return {'ok': False, 'error': 'cap parse failed: %s' % e}
    try:
        report, memory = capmem.analyze_bytes(bytes.fromhex(cap_hex))
        # the load file (all components) is our proxy for the GP "non-volatile
        # code" requirement, so the JSON can report the C6+C8-style total
        info = capmem.memory_json(report, memory, load_file_bytes=len(loadfile_data) // 2)
    except Exception as e:
        return {'ok': False, 'error': 'cap analysis failed: %s' % e}
    return {'ok': True, 'load_file_aid': loadfile_aid, 'module_aid': module_aid,
            'load_file_bytes': len(loadfile_data) // 2, 'memory': info}


def _bip_session_stop():
    """Stop the active BIP session, whichever control started it."""
    global _BIP_LISTENER, _BIP_MODE, _BIP_TARGET, _BIP_OWNER
    replaced = None
    if _BIP_OWNER:
        replaced = {'owner': _BIP_OWNER, 'mode': _BIP_MODE}
    if _BIP_LISTENER:
        _BIP_LISTENER.stop()
        _BIP_LISTENER = None
    _BIP_MODE = None
    _BIP_TARGET = None
    _BIP_OWNER = None
    _BIP.disable()
    return replaced


def _bip_status_body():
    return {'owner': _BIP_OWNER, 'bip': _BIP.status(),
            'listener': _bip_listener_status()}


def _bip_control(body):
    """Generic BIP terminal control (Simulator -> BIP pill).

    'sink' (default) accepts the card's channels on a local listener and only
    logs what arrives; 'redirect' forwards every channel to a fixed target;
    'passthru' dials the destination from the card's OPEN CHANNEL.  Starting
    replaces any running BIP session (an SCP81 listener included) - one BIP
    session at a time; the response reports what was replaced."""
    global _BIP_LISTENER, _BIP_MODE, _BIP_TARGET, _BIP_OWNER, _BIP_LINK_EVENTS
    body = body or {}
    action = body.get('action', 'start')
    if action == 'stop':
        replaced = _bip_session_stop()
        resp = _bip_status_body()
        resp.update({'ok': True, 'replaced': replaced})
        return resp
    mode = body.get('mode', 'sink')
    if mode not in ('passthru', 'redirect', 'sink'):
        return {'ok': False, 'error': 'unsupported mode: %s' % mode}
    host = (body.get('host') or '').strip() or '127.0.0.1'
    raw_port = body.get('port')
    if mode == 'redirect':
        if not body.get('host') or raw_port in (None, ''):
            return {'ok': False,
                    'error': 'redirect mode requires the target host and port'}
        try:
            port = int(raw_port)
        except (TypeError, ValueError):
            return {'ok': False, 'error': 'port is not a number'}
        if not 0 < port <= 0xFFFF:
            return {'ok': False, 'error': 'port must be 1-65535'}
    elif mode == 'sink':
        try:
            port = int(raw_port) if raw_port not in (None, '') else 0
        except (TypeError, ValueError):
            return {'ok': False, 'error': 'port is not a number'}
        if not 0 <= port <= 0xFFFF:
            return {'ok': False, 'error': 'port must be 0-65535 (0 = ephemeral)'}
    replaced = _bip_session_stop()
    _BIP_LINK_EVENTS = bool(body.get('link_events', True))
    _BIP.on_data = _bip_data_available
    if mode == 'passthru':
        _BIP_MODE = 'passthru'
        _BIP_TARGET = None
        _BIP.enable(mode='passthru')
    elif mode == 'redirect':
        _BIP_MODE = 'redirect'
        _BIP_TARGET = (host, port)
        _BIP.enable(host, port, mode='redirect')
    else:
        _BIP_LISTENER = httpota.TcpDumpServer(
            host, port,
            on_rx=lambda peer, data: _BIP.log('sink-rx', peer=peer,
                                              bytes=len(data),
                                              hex=data.hex().upper()[:2000]),
            on_log=lambda kind, **fields: _BIP.log(kind, **fields))
        _BIP_MODE = 'sink'
        _BIP_TARGET = (_BIP_LISTENER.host, _BIP_LISTENER.port)
        _BIP.enable(_BIP_LISTENER.host, _BIP_LISTENER.port, mode='redirect')
    _BIP_OWNER = 'bip'
    resp = _bip_status_body()
    resp.update({'ok': True, 'replaced': replaced})
    return resp


def _scp81_bip_control(body):
    global _BIP_LISTENER, _SCP81_PSKS, _SCP81_PSK_LEGACY
    global _BIP_MODE, _BIP_TARGET, _BIP_OWNER
    global _SCP81_SCRIPT_TEMPLATE, _SCP81_SCRIPT_CR_TAG, _SCP81_NEXT_URI
    global _BIP_LINK_EVENTS, _SCP81_TARGETED_APP
    global _SCP81_CHUNKED
    body = body or {}
    action = body.get('action', 'start')
    if action == 'stop':
        replaced = _bip_session_stop()
        resp = _bip_status_body()
        resp.update({'ok': True, 'replaced': replaced})
        return resp
    host = body.get('host') or '127.0.0.1'
    port = body.get('port')
    port = int(port) if port not in (None, '') else 8443
    mode = body.get('mode', 'dump')
    replaced = _bip_session_stop()
    # Channel status events (TS 102 223 7.5.11) apply to every mode: the
    # terminal reports BIP link changes it detects outside proactive commands.
    _BIP_LINK_EVENTS = bool(body.get('link_events', True))
    if mode == 'redirect':
        # No local listener: the card's BIP channels are redirected straight
        # to the configured target (e.g. a production HTTP OTA server), which
        # terminates TLS and runs the administration dialog. The address the
        # card requests is only logged.
        if not body.get('host') or body.get('port') in (None, ''):
            return {'ok': False,
                    'error': 'redirect mode requires the target host and port'}
        _BIP_MODE = 'redirect'
        _BIP_TARGET = (host, port)
        _BIP_OWNER = 'scp81'
        _BIP.on_data = _bip_data_available
        _BIP.enable(host, port, mode='redirect')
        return {'ok': True, 'replaced': replaced, 'bip': _BIP.status(),
                'listener': _bip_listener_status()}
    if mode == 'passthru':
        # No local listener and no pinned target: every BIP channel dials the
        # destination the card requests in OPEN CHANNEL (Other address +
        # Transport level port, TCP client only). Host and port are unused.
        _BIP_MODE = 'passthru'
        _BIP_TARGET = None
        _BIP_OWNER = 'scp81'
        _BIP.on_data = _bip_data_available
        _BIP.enable(mode='passthru')
        return {'ok': True, 'replaced': replaced, 'bip': _BIP.status(),
                'listener': _bip_listener_status()}
    if mode == 'tls':
        raw_map = body.get('psk_map')
        table, err = _parse_psk_map(raw_map)
        if err:
            return {'ok': False, 'error': err}
        if raw_map is not None and not table:
            return {'ok': False,
                    'error': 'psk_map has no usable entries (identity + key required)'}
        psk = None
        identity = None
        psk_hex = body.get('psk_hex') or ''
        if table:
            pass                        # table sent by the PWA from the presets
        elif psk_hex:
            # Legacy single-key form (API/tests): psk_hex [+ psk_identity].
            try:
                psk = bytes.fromhex(re.sub(r'\s', '', psk_hex))
            except ValueError:
                return {'ok': False, 'error': 'psk_hex is not valid hex'}
            if not psk:
                return {'ok': False, 'error': 'psk_hex is empty'}
            identity = body.get('psk_identity')
            if identity is not None:
                identity = identity.strip() or None    # empty clears the pin
        elif _SCP81_PSKS:
            table = dict(_SCP81_PSKS)   # reuse the table of the last start
        elif _SCP81_PSK_LEGACY:
            psk, identity = _SCP81_PSK_LEGACY   # reuse the last single key
        else:
            return {'ok': False, 'error': 'no PSK configured: send psk_map '
                                          '(card presets) or psk_hex'}
        script = body.get('script')
        if isinstance(script, str):
            if script in ('none', ''):
                _scp81_reset_script([], 'none')
            else:
                return {'ok': False, 'error': 'unknown script preset: %s; send an '
                                              'explicit APDU list or none' % script}
        elif isinstance(script, list):
            _scp81_reset_script(script, body.get('script_kind') or 'custom')
        # No 'script' key: keep the configured script and its run progress
        # (an explicit list starts a fresh run).
        template = body.get('script_template', 'indefinite')
        if template not in ('indefinite', 'definite'):
            return {'ok': False, 'error': 'script_template must be indefinite or definite'}
        _SCP81_SCRIPT_TEMPLATE = template
        _SCP81_SCRIPT_CR_TAG = bool(body.get('cr_tag', False))
        if 'next_uri' in body:
            _SCP81_NEXT_URI = body.get('next_uri') or ''
        _SCP81_TARGETED_APP = (body.get('targeted_app') or None)
        # Defaults reproduce the working reference session (decrypted from
        # samples/HTTP_OTA: RAM/HTTPOTA_test5.pcap): one keep-alive connection,
        # a chunked body whose script sits in one TLS record, no Connection
        # header, and an X-Admin-Next-URI with a query whose command id
        # increments. Overrides remain available; TLS is automatic (all
        # versions/ciphers the server can speak, negotiated per card).
        _SCP81_CHUNKED = bool(body.get('chunked', True))
        cs = body.get('chunk_size')
        chunk_size = int(cs) if cs not in (None, '') else 0
        _BIP_LISTENER = scp81.PskTlsServer(
            host, port, psk, identity=identity, psk_map=(table or None),
            responder=_scp81_script_responder,
            chunked=bool(body.get('chunked', True)),
            chunk_size=chunk_size,
            compact_headers=bool(body.get('compact_headers', False)),
            tls_version=str(body.get('tls_version') or 'auto'),
            cipher=(body.get('cipher') or None),
            keylog=(body.get('keylog') or None),
            conn_header=(body.get('conn_header') or 'none'),
            answer_delay=(body.get('answer_delay') or 0),
            on_log=lambda kind, **fields: _BIP.log(kind, **fields))
        _SCP81_PSKS = dict(_BIP_LISTENER.psk_map)
        _SCP81_PSK_LEGACY = None if table else (psk, identity)
        _BIP_MODE = 'tls'
        _BIP_TARGET = (_BIP_LISTENER.host, _BIP_LISTENER.port)
        _BIP_OWNER = 'scp81'
        _BIP.on_data = _bip_data_available
        _BIP.enable(host, _BIP_LISTENER.port, mode='redirect')
        return {'ok': True, 'replaced': replaced, 'bip': _BIP.status(),
                'listener': _bip_listener_status(),
                'script': list(_SCP81_SCRIPT_BASE),
                'script_kind': _SCP81_SCRIPT_KIND,
                'script_template': _SCP81_SCRIPT_TEMPLATE,
                'cr_tag': _SCP81_SCRIPT_CR_TAG, 'link_events': _BIP_LINK_EVENTS,
                'targeted_app': _SCP81_TARGETED_APP,
                'chunked': _SCP81_CHUNKED}
    if mode != 'dump':
        return {'ok': False, 'error': 'unsupported mode: %s' % mode}
    _BIP.on_data = _bip_data_available
    _BIP_LISTENER = httpota.TcpDumpServer(
        host, port,
        on_rx=lambda peer, data: _BIP.log('dump-rx', peer=peer, bytes=len(data), hex=data.hex().upper()[:2000]),
        on_log=lambda kind, **fields: _BIP.log(kind, **fields))
    _BIP_MODE = 'dump'
    _BIP_TARGET = (_BIP_LISTENER.host, _BIP_LISTENER.port)
    _BIP_OWNER = 'scp81'
    _BIP.enable(host, _BIP_LISTENER.port, mode='redirect')
    return {'ok': True, 'replaced': replaced, 'bip': _BIP.status(),
            'listener': _bip_listener_status()}


def _pli_data_hex(cmd_qual, dt=None):
    """Data object(s) for a PROVIDE LOCAL INFORMATION TERMINAL RESPONSE, as
    hex - or '' when there is nothing to send.

    The TR Config dictionary entry (if any) wins.  Otherwise the date/time
    qualifier is answered with the current date and time from the host clock
    (TS 102 223 6.4.15: "The terminal shall return the current date and time
    as set by the user"), coded as the Date-Time and Time zone object (8.39).
    `dt` is a test seam, the clock is read live otherwise."""
    val = str(_PLI_DATA.get(cmd_qual, '') or '').strip()
    if val:
        return val
    if cmd_qual == 0x03:
        return '2607' + _encode_scts(dt).hex().upper()
    return ''


def _build_tr(scc, cmd_num, cmd_type, dev_src, dev_dst, cmd_qual, dt=None):
    """Build the TERMINAL RESPONSE TLV payload for a fetched command.

    The object order follows TS 102 223 6.8.0: command details, device
    identities, Result, then the command-specific data objects (the echoed
    Duration for POLL INTERVAL, the PLI data for PROVIDE LOCAL INFORMATION).
    `dt` is a test seam for the PLI date/time default (see _pli_data_hex)."""
    base = bytes([0x81, 0x03, cmd_num, cmd_type, 0x00,
                  0x82, 0x02, dev_dst, dev_src,
                  0x03, 0x01, 0x00])
    if cmd_type == 0x03:
        base += bytes([0x84, 0x02, 0x01, _POLL_INTERVAL])
    elif cmd_type == 0x26 and cmd_qual is not None:
        pli_hex = _pli_data_hex(cmd_qual, dt)
        if pli_hex:
            base += bytes.fromhex(pli_hex)
    return base


_RESULT_NAMES_BASIC = {
    0x00: 'Command performed successfully',
    0x01: 'Command performed with partial comprehension',
    0x02: 'Command performed, with missing information',
    0x03: 'REFUSED BY THE ME',
    0x04: 'Command not understood by the ME',
    0x05: 'Command not permitted by the user',
    0x06: 'Command performed with modification',
    0x20: 'Proactive SIM session terminated by the user',
    0x21: 'Backward move in the proactive SIM session requested by the user',
    0x22: 'No response from user',
    0x23: 'Help information required by the user',
    0x24: 'Action in contradiction with the current timer state',
    0x25: 'Interaction with call control by NAA, temporary problem',
    0x26: 'Launch browser generic error',
}

_RESULT_NAMES_GENERAL = {
    0x10: 'Command performed with additional information',
    0x20: 'ME currently unable to process command',
    0x21: 'Network currently unable to process command',
    0x22: 'User did not accept the proactive command',
    0x23: 'User cleared down call before connection or network release',
    0x24: 'Action in contradiction with the current enforcement state',
    0x25: 'Action in contradiction with the current timer state',
    0x26: 'ME currently unable to process command',
    0x27: 'User did not accept the proactive command',
    0x28: 'User cleared down call before connection or network release',
    0x29: 'Action in contradiction with the current enforcement state',
    0x2A: 'Action in contradiction with the current timer state',
    0x30: 'Command performed but partial understanding',
    0x31: 'Command performed, with missing information',
    0x32: 'REFUSED BY THE ME',
    0x33: 'Command not understood by the ME',
    0x34: 'Command not permitted by the user',
    0x35: 'Command performed with modification',
}

def _tr_result_name(b, is_general):
    table = _RESULT_NAMES_GENERAL if is_general else _RESULT_NAMES_BASIC
    if b in table:
        return table[b]
    if 0x40 <= b <= 0x4F or 0x70 <= b <= 0x7F:
        return 'Command performed with modification'
    if 0x60 <= b <= 0x6F:
        return 'Command performed with limited understanding'
    return None


def _extract_tr_result(tr_tlv):
    """Return (tag, value) of the Result CTLV (tag 0x03 basic or 0x83 general)
    from a TERMINAL RESPONSE payload, or None if absent."""
    if not tr_tlv:
        return None
    off = 0
    while off < len(tr_tlv) - 1:
        tag, tlen = tr_tlv[off], tr_tlv[off + 1]
        if tag in (0x03, 0x83) and off + 2 + tlen <= len(tr_tlv):
            return (tag, tr_tlv[off + 2: off + 2 + tlen])
        off += 2 + tlen
    return None


def _record_tr(entry, tr_tlv, tr_sw=None):
    """Attach a sent TERMINAL RESPONSE to a log entry: payload hex, SW and
    server-side decode. tr_sw may be omitted (pySim auto-handler) and filled
    later by _LoggingApduTracer."""
    if entry is None:
        return
    entry['tr_hex'] = _tr_data_only(tr_tlv).hex()
    if tr_sw is not None:
        entry['tr_sw'] = tr_sw
    try:
        result = _extract_tr_result(tr_tlv)
        if result is not None:
            tag, val = result
            entry['tr_result'] = val.hex()
            name = _tr_result_name(val[0], tag == 0x83)
            if name:
                entry['tr_result_name'] = name
        entry['tr_decoded'] = _decode_tr(entry.get('type_hex'), entry.get('qualifier'), entry['tr_hex'])
    except Exception:
        entry['tr_decoded'] = []


def _is_pcsc_error(exc):
    """True for a PC/SC-level failure.

    pyscard sets ``hresult`` on its exceptions (card-level errors such as
    pySim's ``SwMatchError`` do not)."""
    return getattr(exc, 'hresult', -1) not in (-1, None)


# PC/SC hresults where rebuilding the transport cannot help: card/media states
# (the next connect() recovers) and a card that another process holds
# exclusively (a fresh context cannot free someone else's claim).
_PCSC_RECOVERABLE = (
    0x8010000B,   # SCARD_E_SHARING_VIOLATION (another process holds the card)
    0x8010000C,   # SCARD_E_NO_SMARTCARD
    0x80100066,   # SCARD_W_UNRESPONSIVE_CARD
    0x80100067,   # SCARD_W_UNPOWERED_CARD
    0x80100068,   # SCARD_W_RESET_CARD
    0x80100069,   # SCARD_W_REMOVED_CARD
)


def _is_transport_fatal(exc):
    """True when a PC/SC failure means the transport itself is unusable.

    Only service/context failures (pcscd restart, reader re-enumeration, dead
    handle) require a fresh transport.  Card-level states - most importantly
    ``SCARD_W_REMOVED_CARD`` on a normal card swap - and a sharing conflict
    with another process are recoverable on the existing link; rebuilding the
    transport there used to hand the new link the removed card's handle (live
    bug 2026-09-24: auto-equip failed with 0x80100069 until the server was
    restarted)."""
    hr = getattr(exc, 'hresult', -1)
    if hr in (-1, None):
        return False
    return hr not in _PCSC_RECOVERABLE


def _clear_app_card_state(app):
    """Unequip pySim's shell and drop its card/runtime-state references.

    The dead card must not stay referenced through ``app.card``/``app.rs``/
    ``app.lchan`` (handlers would keep transmitting over the removed card and
    the old PC/SC link and its exclusive handle would never be released), but
    the references cannot simply be nulled either: ``PysimApp.equip()``
    unregisters the previous profile's shell command sets from ``self.rs``, so
    with ``rs`` already None the next equip aborts with "CommandSet ... is
    already installed" and the file tree breaks.  Route through pySim's own
    unequip path (``equip(None, None)``) first, then clear the references."""
    if app is None:
        return
    if getattr(app, 'rs', None) is not None and callable(getattr(app, 'equip', None)):
        had_stdout = hasattr(app, 'stdout')
        old_stdout = getattr(app, 'stdout', None)
        try:
            app.stdout = StringIO()   # mute the 'pySim-shell not equipped!' line
            app.equip(None, None)
        except Exception as e:
            sys.stderr.write('UNEQUIP: pySim unequip failed: %s\n' % e)
        finally:
            if had_stdout:
                app.stdout = old_stdout
            else:
                try:
                    del app.stdout
                except Exception:
                    pass
    app.card = None
    app.rs = None
    app.lchan = None


def _handle_card_disconnect(stale=False):
    """Tear down the card session; ``stale`` marks the transport as dead.

    Called on card removal (no ``stale``) and on PC/SC errors
    (``stale=_is_transport_fatal(e)``): the next equip recreates the
    transport when it is really dead."""
    global _CARD_CONNECTED, _TRANSPORT_STALE
    if stale:
        _TRANSPORT_STALE = True
    _poll_disable()
    _cancel_menu_timeout()
    _timer_cancel()
    _CARD_CONNECTED = False
    if _server_ref:
        _clear_app_card_state(getattr(_server_ref, 'app', None))
        _server_ref.card = None
        _server_ref.scc = None
        _server_ref.stk_pending = None
        _server_ref.menu_active = False
        _server_ref.event_list = None
        _server_ref.sim_menu = None
        _server_ref.iccid = None
        _server_ref.net_state = None
        _server_ref.equipping = False
        _server_ref.card_session = getattr(_server_ref, 'card_session', 0) + 1
    _reset_proactive_log()


def _ensure_transport(server):
    """Recreate the PC/SC transport after a service failure.

    pyscard keeps the context handle it established when the reader was
    opened (``PCSCCardConnection.connect`` uses it), so the old connection
    cannot be revived once pcscd restarted - a fresh transport is the only
    way back.  ``server.transport_factory`` is installed by ``__main__``.
    Returns False when the reconnect failed (the caller should give up)."""
    global _TRANSPORT_STALE
    if not _TRANSPORT_STALE or server is None:
        return True
    factory = getattr(server, 'transport_factory', None)
    if factory is None:
        _TRANSPORT_STALE = False
        return True
    # Release the previous link before building the new one: pyscard keeps the
    # PC/SC context (and, while connected, an exclusive card handle) in the
    # connection object.  A second link created while the old one is still
    # connected inherits the stale card handle and fails with
    # SCARD_W_REMOVED_CARD on its first APDU.
    app = getattr(server, 'app', None)
    old = getattr(server, 'sl', None)
    _clear_app_card_state(app)
    server.sl = None
    if app is not None:
        app.sl = None
    if old is not None:
        try:
            old.disconnect()
        except Exception as e:
            sys.stderr.write('TRANSPORT: releasing the old link failed: %s\n' % e)
    try:
        sl = factory()
    except Exception as e:
        sys.stderr.write('TRANSPORT: PC/SC reconnect failed: %s\n' % e)
        # Keep the released link installed: a later equip can still reconnect
        # it (PcscSimLink.connect() re-establishes the connection).
        server.sl = old
        if app is not None:
            app.sl = old
        return False
    server.sl = sl
    server.card = None
    server.scc = None
    if app is not None:
        app.sl = sl
    _TRANSPORT_STALE = False
    sys.stderr.write('TRANSPORT: PC/SC reconnected\n')
    return True


def _apply_equipped_card(server):
    """Common post-equip state refresh + TERMINAL PROFILE, shared by the
    /api/command equip branch and the auto-equip worker."""
    global _CARD_CONNECTED, _POLL_DISABLED_BY_CARD
    server.stk_pending = None
    server.menu_active = False
    _cancel_menu_timeout()
    _timer_cancel()
    server.event_list = None
    _reset_proactive_log()
    server.card = server.app.card
    server.scc = server.app.card._scc
    server.scc.cat_cla = '80' if isinstance(server.card, UiccCardBase) else 'a0'
    _CARD_CONNECTED = True
    server.card_present = True
    server.card_session = getattr(server, 'card_session', 0) + 1
    server.iccid = None
    # Read the ICCID before the TERMINAL PROFILE starts a CAT session: the
    # PWA auto-selects the matching card preset (SCP80 views) from it.
    server.iccid = _read_iccid(server.app)
    if server.iccid:
        _tlog('equip: ICCID %s' % server.iccid)
        # Network state monitor: read the network-related EFs right after the
        # ICCID (still before the TERMINAL PROFILE opens a CAT session).  A
        # card without a readable ICCID is considered unusable - give up.
        _netstate_init(server)
    else:
        server.net_state = None
        _tlog('equip: ICCID not readable - network state skipped')
    # A new card session starts with polling allowed; a POLLING OFF from the
    # previous card does not survive the swap.
    _POLL_DISABLED_BY_CARD = False
    _poll_enable()
    sm, el = _send_terminal_profile(server.scc, server.terminal_profile)
    server.sim_menu = sm
    server.event_list = el
    _tlog('equip: terminal profile done')


def _esim_reinit(server):
    """Full card re-initialization after a profile switch.

    A profile switch is logically an equip: the active application (and the
    ICCID) changes, so every cached card view is flushed and re-read.  The
    card is physically reset first (after REFRESH it restarts on the newly
    active profile), then the standard equip path runs.
    """
    app = server.app
    if not _ensure_transport(server):
        return False
    server.equipping = True
    try:
        try:
            if server.scc:
                server.scc.reset_card()
        except Exception as e:
            sys.stderr.write('ESIM: card reset failed: %s\n' % e)
        old_stdout, old_stderr = app.stdout, sys.stderr
        app.stdout = StringIO()
        sys.stderr = app.stdout
        try:
            app.onecmd_plus_hooks('equip')
        finally:
            app.stdout = old_stdout
            sys.stderr = old_stderr
        if server.app.card is None:
            sys.stderr.write('ESIM: card gone during re-initialization\n')
            return False
        _apply_equipped_card(server)
        # The equip must leave the shell's command-set registration consistent
        # (pySim unregisters only the file selected at equip time).  A broken
        # registration only shows up on the next select made with the app, so
        # probe it here: a card-level select failure (no active profile) is
        # acceptable, a registration error is not.
        try:
            server.app.rs.lchan[0].select('MF', server.app)
        except CommandSetRegistrationError as e:
            sys.stderr.write('ESIM: shell command registration broken after '
                             're-initialization: %s\n' % e)
            return False
        except Exception as e:
            sys.stderr.write('ESIM: post-equip MF select failed: %s\n' % e)
        sys.stderr.write('ESIM: re-initialized after profile switch\n')
        return True
    except Exception as e:
        sys.stderr.write('ESIM: re-initialization failed: %s\n' % e)
        return False
    finally:
        server.equipping = False


def _esim_refresh_chain(server, sw91):
    """Answer the proactive command(s) that accompany a profile switch.

    With the refresh flag set the card sends REFRESH (SGP.22 §5.7.16 step 7);
    it is answered and the chain stops - the card reset that follows performs
    the switch.  No STATUS poll is sent: the card is mid-switch and refuses
    further commands (6985) until the reset."""
    seen = {'refresh': False}

    def on_fetch(raw, cmd_num, cmd_type, dev_src, dev_dst):
        if cmd_type == 0x01:
            seen['refresh'] = True
            return 'exit'
        return None

    _handle_proactive_chain(server.scc, sw91, on_fetch=on_fetch,
                            status_poll=False)
    return seen['refresh']


def _norm_iccid(value):
    """ICCID comparison form: hex digits without the trailing F pad."""
    s = re.sub(r'[^0-9a-fA-F]', '', str(value or '')).upper()
    return s[:-1] if s.endswith('F') else s


def _esim_verify_switch(app, action, iccid=None, isdp_aid=None):
    """Re-read the profile list and check the requested state took effect."""
    expected = 'enabled' if action == 'enable' else 'disabled'
    try:
        profs = esim.profiles(app).get('profiles') or []
    except Exception as e:
        sys.stderr.write('ESIM: state verification failed: %s\n' % e)
        return {'verified': None, 'state_after': None}
    want_iccid = _norm_iccid(iccid) if iccid else None
    want_aid = re.sub(r'[^0-9a-fA-F]', '', str(isdp_aid or '')).upper() or None
    state_after = None
    for p in profs:
        if (want_iccid and _norm_iccid(p.get('iccid')) == want_iccid) or \
           (want_aid and re.sub(r'[^0-9a-fA-F]', '',
                                str(p.get('isdp_aid') or '')).upper() == want_aid):
            state_after = p.get('state')
            break
    return {'verified': state_after == expected, 'state_after': state_after}


_AUTO_EQUIP = True
_AUTO_EQUIP_BUSY = False
_AUTO_EQUIP_ATTEMPTS = 3
_AUTO_EQUIP_RETRY_DELAY = 1.0    # seconds between attempts
_AUTO_EQUIP_REARM_DELAY = 5.0    # min seconds between watchdog re-arms
_AUTO_EQUIP_REARM_MAX = 60.0     # backoff ceiling after repeated failures
_AUTO_EQUIP_BACKOFF = _AUTO_EQUIP_REARM_DELAY
_AUTO_EQUIP_LAST = 0.0

def set_auto_equip(enabled):
    global _AUTO_EQUIP
    _AUTO_EQUIP = bool(enabled)

def _auto_equip_trigger():
    """Spawn a one-shot worker; never run equip in the pyscard monitor thread."""
    global _AUTO_EQUIP_BUSY
    if not _AUTO_EQUIP or _AUTO_EQUIP_BUSY:
        return False
    _AUTO_EQUIP_BUSY = True
    threading.Thread(target=_auto_equip_worker, name='auto-equip', daemon=True).start()
    return True

def _auto_equip_rearm(now=None):
    """Self-heal: re-arm auto-equip when a card is present but the session is
    down (failed attempt, missed insertion event).  Rate-limited so a genuinely
    broken card is not retried in a tight loop."""
    global _AUTO_EQUIP_LAST
    if not _AUTO_EQUIP or _AUTO_EQUIP_BUSY:
        return False
    server = _server_ref
    if server is None or _CARD_CONNECTED:
        return False
    if not getattr(server, 'card_present', False) or getattr(server, 'equipping', False):
        return False
    now = time.time() if now is None else now
    if now - _AUTO_EQUIP_LAST < _AUTO_EQUIP_BACKOFF:
        return False
    if not _auto_equip_trigger():
        return False     # busy/disabled: leave the window open for the next tick
    _AUTO_EQUIP_LAST = now
    return True

def _app_equip_complete(app):
    """True when pySim's shell really ended up equipped after an equip command.

    cmd2 swallows exceptions raised inside the equip command (it prints them,
    no traceback, and returns normally), and PysimApp.equip() assigns
    ``card``/``rs`` before it registers the command sets - so ``app.card``
    alone cannot tell a completed equip from a half-initialized shell (live
    2026-09-24: an abort at "CommandSet ... is already installed" passed the
    card check, the worker reported done and /api/tree stayed broken).  The
    profile's shell command-set *instances* must be installed; those are
    exactly what PysimApp.equip() registers.  States that cannot be verified
    (no profile command sets, foreign app objects) are trusted."""
    if app is None or getattr(app, 'card', None) is None or getattr(app, 'lchan', None) is None:
        return False
    rs = getattr(app, 'rs', None)
    profile = getattr(rs, 'profile', None)
    sets = list(getattr(profile, 'shell_cmdsets', []) or [])
    finder = getattr(app, 'find_commandsets', None)
    if not sets or not callable(finder):
        return True     # nothing to verify
    try:
        for cmd_set in sets:
            if cmd_set not in finder(type(cmd_set)):
                return False
        return True
    except Exception:
        return True


def _auto_equip_attempt(server):
    """One equip attempt under _CARD_LOCK; True when connected/no-op.

    cmd2 swallows exceptions raised inside the equip command, so success is
    only reported when the shell really ended up equipped
    (_app_equip_complete); a failed attempt unequips the half-initialized
    shell before the next retry."""
    with _CARD_LOCK:
        if _CARD_CONNECTED or not getattr(server, 'card_present', False):
            return True
        app = server.app
        if app is None or not getattr(server, 'terminal_profile', None):
            return True
        # A pcscd restart kills the PC/SC context: rebuild the transport
        # before equipping, otherwise the equip can never succeed.
        if not _ensure_transport(server):
            return False
        server.equipping = True
        try:
            out = StringIO()
            old_stdout, old_stderr = app.stdout, sys.stderr
            old_debug = getattr(app, 'debug', None)
            app.stdout = out
            sys.stderr = out
            if old_debug is not None:
                app.debug = True     # cmd2 prints a traceback for swallowed errors
            try:
                app.onecmd_plus_hooks('equip')
            finally:
                if old_debug is not None:
                    app.debug = old_debug
                app.stdout = old_stdout
                sys.stderr = old_stderr
            if not getattr(server, 'card_present', False) or server.app.card is None:
                sys.stderr.write('AUTO-EQUIP: card gone during initialization\n')
                return True
            if 'Traceback (most recent call last)' in out.getvalue():
                raise RuntimeError('equip reported an error (see the captured output)')
            if not _app_equip_complete(app):
                raise RuntimeError('equip finished with a half-initialized shell '
                                   '(command sets not registered)')
            _apply_equipped_card(server)
            sys.stderr.write('AUTO-EQUIP: done\n')
            return True
        except Exception as e:
            sys.stderr.write('AUTO-EQUIP failed: %s\n' % e)
            # A dead transport must be rebuilt before the next attempt;
            # card-level failures reconnect on the existing link.  Unequip the
            # half-initialized shell as well: a failed init can leave command
            # sets registered that the next equip would refuse to re-register.
            if _is_transport_fatal(e):
                _handle_card_disconnect(stale=True)
            _clear_app_card_state(app)
            return False
        finally:
            server.equipping = False

def _auto_equip_attempts(server, attempts=None, sleep_fn=None):
    """Bounded equip retries; returns True once the session is up."""
    attempts = _AUTO_EQUIP_ATTEMPTS if attempts is None else attempts
    sleep_fn = time.sleep if sleep_fn is None else sleep_fn
    if server is None:
        return False
    if _CARD_CONNECTED or not getattr(server, 'card_present', False) or getattr(server, 'app', None) is None:
        return True
    for attempt in range(1, attempts + 1):
        if attempt == 1:
            sys.stderr.write('AUTO-EQUIP: card inserted, initializing\n')
        else:
            sys.stderr.write('AUTO-EQUIP: retry %d/%d\n' % (attempt, attempts))
        if _auto_equip_attempt(server):
            return True
        if not _AUTO_EQUIP or _CARD_CONNECTED or not getattr(server, 'card_present', False):
            return False
        if attempt < attempts:
            sleep_fn(_AUTO_EQUIP_RETRY_DELAY)
    return False

def _auto_equip_worker():
    global _AUTO_EQUIP_BUSY, _AUTO_EQUIP_BACKOFF
    try:
        ok = _auto_equip_attempts(_server_ref)
        # A card that cannot be initialized at all must not be retried every
        # few seconds forever; the delay doubles up to the ceiling and is
        # reset by any successful (or no-op) attempt.
        _AUTO_EQUIP_BACKOFF = (_AUTO_EQUIP_REARM_DELAY if ok
                               else min(_AUTO_EQUIP_BACKOFF * 2, _AUTO_EQUIP_REARM_MAX))
    finally:
        _AUTO_EQUIP_BUSY = False


class _CardPresenceObserver(CardObserver):
    """Passive PC/SC presence watcher: pyscard's CardMonitor only polls
    SCardGetStatusChange (no connection, no APDUs), so it can never interleave
    with our APDU traffic. We only update flags and tear down card state."""

    def __init__(self, reader_name):
        self.reader_name = reader_name

    def update(self, observable, handlers):
        addedcards, removedcards = handlers
        try:
            trigger_auto = False
            for card in removedcards:
                if str(getattr(card, 'reader', '')) == self.reader_name:
                    sys.stderr.write('CARD-WATCH: card removed from %s\n' % self.reader_name)
                    with _CARD_LOCK:
                        if _server_ref:
                            _server_ref.card_present = False
                        _handle_card_disconnect()
            for card in addedcards:
                if str(getattr(card, 'reader', '')) == self.reader_name:
                    sys.stderr.write('CARD-WATCH: card inserted into %s\n' % self.reader_name)
                    with _CARD_LOCK:
                        if _server_ref:
                            _server_ref.card_present = True
                    trigger_auto = True
            if trigger_auto and _AUTO_EQUIP:
                _auto_equip_trigger()
        except Exception as e:
            sys.stderr.write('CARD-WATCH error: %s\n' % e)


_card_presence_observer = None
_card_watchdog = None


def _card_monitor_alive():
    """True while pyscard's presence-monitoring thread is running.

    pyscard 2.x runs the thread eagerly (``_START_ON_DEMAND_ = False``) and
    stops it itself on ``SCARD_E_NO_SERVICE``, so a server that outlives a
    pcscd restart would never see card insertions again without a watchdog."""
    try:
        from smartcard.CardMonitoring import CardMonitor
        rmthread = getattr(CardMonitor(), 'rmthread', None)
        thread = getattr(rmthread, 'instance', None)
        return bool(thread is not None and thread.is_alive())
    except Exception:
        return False


def _restart_card_monitor(reader_name):
    """Recreate pyscard's presence-monitoring thread.

    The fresh thread reports cards that are already present as *inserted* on
    its first pass, which triggers our auto-equip; the observers stay
    registered on the untouched Observable singleton."""
    from smartcard.CardMonitoring import CardMonitor, CardMonitoringThread
    monitor = CardMonitor().instance
    CardMonitoringThread.instance = None
    monitor.rmthread = CardMonitoringThread(monitor)


def _watchdog_tick(reader_name, alive_fn=None, restart_fn=None):
    """One presence-monitor watchdog pass; returns 'ok' or 'restarted'."""
    alive_fn = alive_fn or _card_monitor_alive
    restart_fn = restart_fn or _restart_card_monitor
    if alive_fn():
        return 'ok'
    sys.stderr.write('CARD-WATCH: PC/SC presence monitor stopped; restarting\n')
    restart_fn(reader_name)
    return 'restarted'


def _start_card_watchdog(reader_name, interval=5.0):
    """Keep the pyscard presence monitor alive across pcscd restarts."""
    if not reader_name:
        return None

    def run():
        while True:
            time.sleep(interval)
            try:
                _watchdog_tick(reader_name)
            except Exception as e:
                sys.stderr.write('CARD-WATCH: monitor restart failed: %s\n' % e)
            try:
                # Self-heal: a card may be present while the session is down
                # (failed auto-equip, missed insertion event).
                _auto_equip_rearm()
            except Exception as e:
                sys.stderr.write('CARD-WATCH: auto-equip re-arm failed: %s\n' % e)

    thread = threading.Thread(target=run, name='card-watchdog', daemon=True)
    thread.start()
    return thread


def start_card_monitor(reader_name):
    """Start the process-wide pyscard monitor (one daemon thread, no process)
    and register our reader's presence observer; a watchdog keeps the monitor
    (and with it auto-equip) alive when pcscd restarts."""
    global _card_presence_observer, _card_watchdog
    if not reader_name:
        return None
    if _card_presence_observer is None:
        _card_presence_observer = _CardPresenceObserver(reader_name)
        CardMonitor().addObserver(_card_presence_observer)
    if _card_watchdog is None or not _card_watchdog.is_alive():
        _card_watchdog = _start_card_watchdog(reader_name)
    return _card_presence_observer


def _init_proactive_session():
    global _PROACTIVE_SESSION_START
    _PROACTIVE_SESSION_START = time.time()


def _log_proactive(cmd_type, raw, qualifier=None, cmd_num=None):
    global _PROACTIVE_ENTRY_ID
    if _PROACTIVE_SESSION_START is None:
        return
    _PROACTIVE_ENTRY_ID += 1
    entry = {
        'id': _PROACTIVE_ENTRY_ID,
        'type_hex': '%02x' % cmd_type,
        'type_name': PROACTIVE_TYPE_NAMES.get(cmd_type, 'UNKNOWN'),
        'elapsed': round(time.time() - _PROACTIVE_SESSION_START, 1),
        'bytes': len(raw) if raw else 0,
        'raw': raw.hex() if raw else None,
    }
    if cmd_num is not None:
        entry['cmd_num'] = cmd_num
    if qualifier is not None:
        entry['qualifier'] = '%02x' % qualifier
    try:
        entry['cmd_decoded'] = _decode_cmd(cmd_type, raw, qualifier)
    except Exception:
        entry['cmd_decoded'] = []
    _PROACTIVE_LOG.append(entry)
    return entry


def _reset_proactive_log():
    global _PROACTIVE_LOG, _PROACTIVE_SESSION_START
    _PROACTIVE_LOG.clear()
    _PROACTIVE_SESSION_START = time.time()


def _send_status(scc):
    """STATUS (F2) with correct P3 per card type: SIM=0x23, UICC=0x00.
    P2=0C mirrors phone behavior - no FCP of the selected DF returned
    (plain F2 00 00 would echo the FCP, which is unnecessary overhead)."""
    p3 = '23' if scc.cat_cla == 'a0' else '00'
    return scc._tp.send_apdu('%sf2000c%s' % (scc.cat_cla, p3))


def _verify_adm(scc, app, adm_hex):
    """Verify the card's ADM PIN (TS 102 221 VERIFY) and report the result.

    ``adm_hex`` is the key from the matched card preset (4-16 hex digits);
    short keys are padded to the 8 CHV bytes with 'f', like pySim's
    verify_adm.  The result is structured so the UI can warn about the
    remaining attempts: every failed VERIFY consumes one, and a blocked ADM
    cannot be recovered from here (it needs the unblock key).
    """
    # Use the card's own logical channel, exactly like pySim-shell's
    # verify_adm.  server.scc can still be the startup placeholder (SIM CLA)
    # when no equip has happened yet, and a UICC then answers 6E00.
    rs = getattr(app, 'rs', None)
    lchan = rs.lchan[0] if rs is not None and getattr(rs, 'lchan', None) else None
    card_scc = (getattr(lchan, 'scc', None)
                or getattr(getattr(app, 'card', None), '_scc', None))
    if card_scc is not None:
        scc = card_scc
    chv = getattr(getattr(app, 'card', None), '_adm_chv_num', 0x0A)
    fc = rpad(str(adm_hex).lower(), 16)
    _data, sw = scc.send_apdu(scc.cla_byte + '2000' + ('%02X' % chv) + '08' + fc)
    sw = str(sw).upper()
    if sw == '9000':
        rs = getattr(app, 'rs', None)
        if rs is not None:
            rs.adm_verified = True
        return {'ok': True, 'sw': sw}
    if re.fullmatch(r'63C[0-9A-F]', sw):
        return {'ok': False, 'sw': sw, 'attempts_left': int(sw[3], 16)}
    if sw in ('6983', '9804'):
        return {'ok': False, 'sw': sw, 'blocked': True}
    return {'ok': False, 'sw': sw,
            'error': 'Security status not satisfied' if sw == '6982' else 'Error'}


# The single-ENVELOPE budget for event data: the inner data (event list +
# device identities + the event objects) is wrapped as D6 81 <len> + inner,
# and the APDU Lc must stay within one byte (255), so the inner data is
# capped at 252 bytes (244 bytes of event data + the 8-byte header).
# Chained (multi-envelope) delivery is not implemented - the slice/S-NSSAI
# data must fit one envelope (see the help).
_EVENT_INNER_MAX = 252
_EVENT_DATA_MAX = events.EVENT_DATA_MAX   # = 252 - 8; events.py owns the value


def _send_event_download(scc, event_type, event_data=None, drain=True, src=None,
                         log=True):
    """Send ENVELOPE(Event Download) for the given event type.
    Builds: CLA C2 0000 Lc  D6 [len] (99 01 [type] 82 02 <src> 81 [extra]).
    `src` is the source device identity (TS 102 223 8.7: '82' terminal,
    '83' network); the default is the terminal.  `log=False` suppresses the
    generic ENVELOPE lines - the test runner logs its own step-annotated line
    with the event name, the source and the response SW."""
    if event_data and len(event_data) > _EVENT_DATA_MAX:
        raise ValueError('event data does not fit one ENVELOPE (max %d bytes; '
                         'chained delivery is not implemented)' % _EVENT_DATA_MAX)
    src_byte = 0x82
    if src:
        try:
            src_byte = int(str(src), 16) & 0xFF
        except (TypeError, ValueError):
            src_byte = 0x82
    inner = bytearray()
    inner.extend([0x99, 0x01, event_type])
    inner.extend([0x82, 0x02, src_byte, 0x81])
    if event_data:
        inner.extend(event_data)
    if len(inner) < 0x80:
        d6_tlv = bytes([0xD6, len(inner)]) + bytes(inner)
    else:
        # BER long form for 128..255 bytes: the length field is "1 or 2" per
        # the TS 102 223 7.5.x ENVELOPE tables (a raw byte above 0x7F would be
        # read as a long-form indicator)
        d6_tlv = bytes([0xD6, 0x81, len(inner)]) + bytes(inner)
    env_hex = '%sc20000%02x%s' % (scc.cat_cla, len(d6_tlv), d6_tlv.hex())
    if log:
        sys.stderr.write('ENVELOPE(Event Download): type=0x%02x data=%s\n' % (event_type, event_data.hex() if event_data else '(none)'))
    data, sw = scc._tp.send_apdu(env_hex)
    if sw.startswith('61'):
        get_len = int(sw[2:], 16) if len(sw) == 4 else 0x100
        data, sw = scc._tp.send_apdu('00c00000%02x' % get_len)
    if log:
        sys.stderr.write('ENVELOPE SW: %s\n' % sw)
    if drain and sw.startswith('91'):
        _handle_proactive_chain(scc, sw)
        sw = '9000'
    return data, sw


_FLUSHING_CHANNEL_EVENTS = False
# True while a FETCH/TERMINAL RESPONSE chain is running: terminal-initiated
# ENVELOPEs must never interleave with it.
_PROACTIVE_BUSY = False


def _bip_flush_channel_events(scc):
    """Inform the UICC about BIP link changes detected outside its proactive
    commands (TS 102 223 7.5.11), if the card subscribed to Channel status.
    Called when the proactive session is idle, never between FETCH and TR."""
    global _FLUSHING_CHANNEL_EVENTS
    if _FLUSHING_CHANNEL_EVENTS:
        return
    if not _BIP_LINK_EVENTS:
        _BIP.take_pending_events()
        return
    ev_list = getattr(_server_ref, 'event_list', None) or []
    if 0x0A not in ev_list:
        return
    events = _BIP.take_pending_events()
    if not events:
        return
    _FLUSHING_CHANNEL_EVENTS = True
    try:
        for ev in events:
            _send_event_download(scc, 0x0A, bytes([
                httpota.TAG_CHANNEL_STATUS | 0x80, 0x02, ev['status'], ev['info']]))
    except Exception as e:
        sys.stderr.write('Channel status event error: %s\n' % e)
    finally:
        _FLUSHING_CHANNEL_EVENTS = False


def _ber_len_at(raw, off):
    """Return (length, value_offset) of a BER length field at raw[off]."""
    if off >= len(raw):
        return 0, off
    first = raw[off]
    if first < 0x80:
        return first, off + 1
    n = first & 0x7F
    if n < 1 or off + 1 + n > len(raw):
        return 0, off + 1
    return int.from_bytes(raw[off + 1:off + 1 + n], 'big'), off + 1 + n


def _skip_ber_len(raw, off):
    if off >= len(raw):
        return off
    if raw[off] < 0x80:
        return off + 1
    if raw[off] == 0x81:
        return off + 2
    return off + 3


def _ber_tlv_list(data):
    """[(tag, value)] of a BER-TLV sequence, stopping at a truncated or
    malformed TLV instead of walking into the value.

    The length MUST be read as BER: the big listings carry long-form lengths
    (`8B 81 96` = the 150-byte SMS TPDU) and a single-byte read walks into the
    value, eventually indexing past the end (live 2026-09-30: `IndexError:
    index out of range` in the FETCH path - a SEND SHORT MESSAGE whose TLV
    list was mis-walked)."""
    out = []
    off = 0
    while off + 1 < len(data):
        tag = data[off]
        tlen, voff = _ber_len_at(data, off + 1)
        if voff + tlen > len(data):
            break
        out.append((tag, data[voff:voff + tlen]))
        if voff + tlen <= off:      # no forward progress: stop
            break
        off = voff + tlen
    return out


def _proactive_body(raw):
    """The TLV region of a D0 proactive command (after its BER length)."""
    if not raw or raw[0] != 0xD0:
        return b''
    ln, off = _ber_len_at(raw, 1)
    end = off + ln if 0 < ln <= len(raw) - off else len(raw)
    return raw[off:end]


# ---- TIMER MANAGEMENT (TS 102 223 6.6.21, 6.8.13/14, 7.4) -----------------
# The terminal keeps up to 8 timers per card session. On expiry it must send
# ENVELOPE (TIMER EXPIRATION, tag D7) so the card can act (a common OTA retry
# mechanism); a reset or card removal deactivates all timers.

_TIMERS = {}
_TIMER_LOCK = threading.Lock()


def _timer_cancel(timer_id=None):
    """Cancel one timer, or all of them (reset / card removal)."""
    with _TIMER_LOCK:
        ids = list(_TIMERS) if timer_id is None else [timer_id]
        for tid in ids:
            entry = _TIMERS.pop(tid, None)
            if entry:
                entry['timer'].cancel()


def _timer_remaining(timer_id):
    with _TIMER_LOCK:
        entry = _TIMERS.get(timer_id)
        if not entry:
            return None
        return max(0, int(round(entry['deadline'] - time.time())))


def _timer_start(timer_id, seconds):
    """Start (or restart) a timer; returns False for an invalid identifier."""
    if not 1 <= timer_id <= 8:
        return False
    _timer_cancel(timer_id)
    timer = threading.Timer(seconds, _timer_fire, args=(timer_id, seconds))
    timer.daemon = True
    timer.start()
    with _TIMER_LOCK:
        _TIMERS[timer_id] = {'timer': timer, 'deadline': time.time() + seconds}
    return True


def _timer_fire(timer_id, elapsed):
    """Timer callback: the timer is consumed on expiry (7.4.1); a timer that
    was cancelled or restarted in the meantime must not report."""
    with _TIMER_LOCK:
        entry = _TIMERS.pop(timer_id, None)
    if entry is None:
        return
    _timer_expired(timer_id, elapsed)


def _timer_expired(timer_id, elapsed):
    """Pass an expired timer to the UICC with ENVELOPE (TIMER EXPIRATION)."""
    server = _server_ref
    if not _CARD_CONNECTED or not server:
        return
    # Never inject the ENVELOPE while a fetched command awaits its TERMINAL
    # RESPONSE (a paused STK menu); wait outside the card lock, then retry.
    for _ in range(10):
        if not getattr(server, 'stk_pending', None):
            break
        time.sleep(2)
    with _CARD_LOCK:
        if server is not _server_ref or not getattr(server, 'scc', None):
            return
        if getattr(server, 'stk_pending', None):
            _timer_start(timer_id, 5)
            return
        scc = server.scc
        inner = bytes([0x82, 0x02, 0x82, 0x81, 0xA4, 0x01, timer_id & 0xFF,
                       0xA5, 0x03]) + _hms_bcd(elapsed)
        tlv = bytes([0xD7, len(inner)]) + inner
        apdu = '%sc20000%02x%s' % (scc.cat_cla, len(tlv), tlv.hex())
        for _ in range(3):
            try:
                data, sw = scc._tp.send_apdu(apdu)
            except Exception as e:
                sys.stderr.write('TIMER-EXPIRATION send error: %s\n' % e)
                _handle_card_disconnect(stale=_is_transport_fatal(e))
                return
            sys.stderr.write('ENVELOPE(Timer Expiration): timer=%d elapsed=%ds -> %s\n'
                             % (timer_id, elapsed, sw))
            if sw == '9300':
                # UICC busy: the terminal shall retry until accepted (7.4.1).
                time.sleep(1)
                continue
            if sw.startswith('91'):
                _handle_proactive_chain(scc, sw)
            break


def _handle_timer_command(cmd_num, cmd_type, cmd_qual, raw, dev_src, dev_dst):
    """Terminal side of TIMER MANAGEMENT. Returns the TERMINAL RESPONSE payload."""
    tlvs = httpota.proactive_tlvs(raw)
    tid = _cmd_tlv(tlvs, 0x24)
    timer_id = tid[0] if tid else 1
    action = (cmd_qual or 0) & 0x03
    base = bytes([0x81, 0x03, cmd_num, cmd_type, (cmd_qual or 0) & 0xFF,
                  0x82, 0x02, dev_dst, dev_src])
    if action == 0x00:
        value = _cmd_tlv(tlvs, 0x25)
        if len(value) >= 3 and 1 <= timer_id <= 8:
            secs = (_bcd_swap(value[0]) * 3600 + _bcd_swap(value[1]) * 60
                    + _bcd_swap(value[2]))
            if secs > 0:
                _timer_start(timer_id, secs)
        return base + bytes([0x03, 0x01, 0x00])
    remaining = _timer_remaining(timer_id)
    if remaining is None:
        return base + bytes([0x03, 0x01, 0x24])
    if action == 0x01:
        _timer_cancel(timer_id)
    return (base + bytes([0xA4, 0x01, timer_id & 0xFF, 0xA5, 0x03])
            + _hms_bcd(remaining) + bytes([0x03, 0x01, 0x00]))


def _decode_stk_text(raw):
    try:
        return _STK_DECODE._decode(raw, {}, 'stk')
    except Exception:
        return raw.hex()


def _decode_dcs_text(raw):
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


def _parse_proactive_header(raw):
    cmd_num, cmd_type = 1, 0
    cmd_qual = None
    dev_src, dev_dst = 0x83, 0x81
    for tag, val in _ber_tlv_list(_proactive_body(raw)):
        # Cards use both the plain (01/02) and comprehension-required
        # (81/82) tag variants - TS 101 220 7.1.1 leaves the CR flag to the
        # application, and the reference cards switch between them.
        if tag in (0x01, 0x81) and len(val) >= 3:
            cmd_num, cmd_type, cmd_qual = val[0], val[1], val[2]
        elif tag in (0x02, 0x82) and len(val) >= 2:
            dev_src, dev_dst = val[0], val[1]
    return cmd_num, cmd_type, dev_src, dev_dst, cmd_qual


def _find_sms_tpdu(raw):
    """Extract the SMS TPDU (tag 0x8B) from a FETCH response.

    FETCH responses may use BER long-form lengths (`8B 81 97 ...` for the big
    listings), so the TLV length must be parsed, not read as one byte: a
    single-byte read silently truncates the TPDU and the PoR/data is lost."""
    if raw and raw[0] == 0xD0:
        off = _skip_ber_len(raw, 1)
        while off < len(raw) - 1:
            tag = raw[off]
            tlen, val_off = _ber_len_at(raw, off + 1)
            if tag in (0x8B, 0x0B) and tlen >= 1:
                return raw[val_off:val_off + tlen].hex()
            nxt = val_off + tlen
            if nxt <= off:          # no forward progress: stop
                break
            off = nxt
    return None


def _calc_ud_offset(tpdu):
    """Calculate the offset of TP-UD (User Data) within an SMS TPDU.
    Handles SMS-SUBMIT (MTI=01) and SMS-DELIVER (MTI=00)."""
    first_octet = tpdu[0]
    mti = first_octet & 0x03
    vpf = (first_octet >> 3) & 0x03
    if mti == 0x01:
        # SMS-SUBMIT: 1 + 1(MR) + 1(DA_len) + 1(DA_type) + ceil(DA_len/2) + 1(PID) + 1(DCS) [+7 if VPF]
        if len(tpdu) < 3:
            return None
        da_len_digits = tpdu[2]
        da_data_bytes = (da_len_digits + 1) // 2
        off = 1 + 1 + 1 + 1 + da_data_bytes + 1 + 1
        # TP-VP: none (0), enhanced (7), relative (1), absolute (7) - reading
        # the relative form as 7 bytes shifted the UD offset and made the
        # concatenation UDH unparseable.
        off += {0x00: 0, 0x01: 7, 0x02: 1, 0x03: 7}[vpf]
        if off >= len(tpdu):
            return None
        return off + 1  # skip UDL byte
    elif mti == 0x00:
        # SMS-DELIVER: 1 + 1(OA_len) + ceil(OA_len/2) + 1(PID) + 1(DCS) + 7(SCTS)
        if len(tpdu) < 2:
            return None
        oa_len_digits = tpdu[1]
        oa_data_bytes = (oa_len_digits + 1) // 2
        off = 1 + 1 + oa_data_bytes + 1 + 1 + 7
        if off >= len(tpdu):
            return None
        return off + 1  # skip UDL byte
    return None


def _parse_sms_concat(tpdu_bytes):
    """Parse an SMS TPDU for UDH concatenation info and payload.
    Returns (ref, total, num, payload_bytes) or (None, None, None, payload_bytes) if no concat."""
    if not tpdu_bytes or len(tpdu_bytes) < 2:
        return None, None, None, tpdu_bytes or b''

    first_octet = tpdu_bytes[0]
    udhi = bool(first_octet & 0x40)

    ud_offset = _calc_ud_offset(tpdu_bytes)
    if ud_offset is None or ud_offset >= len(tpdu_bytes):
        return None, None, None, tpdu_bytes

    if not udhi:
        # No UDH — entire UD is the payload
        return None, None, None, tpdu_bytes[ud_offset:]

    # TP-UDHI is set: UD starts with UDHL
    udhl = tpdu_bytes[ud_offset]
    udh_start = ud_offset + 1
    udh_end = udh_start + udhl
    if udh_end > len(tpdu_bytes):
        return None, None, None, tpdu_bytes[ud_offset:]

    # Walk UDH IEs looking for concatenation
    ref = None
    total = None
    num = None
    ie_off = udh_start
    while ie_off + 2 <= udh_end:
        iei = tpdu_bytes[ie_off]
        iedl = tpdu_bytes[ie_off + 1]
        if ie_off + 2 + iedl > udh_end:
            break
        if iei == 0x00 and iedl == 3:
            ref = tpdu_bytes[ie_off + 2]
            total = tpdu_bytes[ie_off + 3]
            num = tpdu_bytes[ie_off + 4]
        elif iei == 0x08 and iedl == 4:
            ref = (tpdu_bytes[ie_off + 2] << 8) | tpdu_bytes[ie_off + 3]
            total = tpdu_bytes[ie_off + 4]
            num = tpdu_bytes[ie_off + 5]
        ie_off += 2 + iedl

    payload = tpdu_bytes[ud_offset + 1 + udhl:]  # payload after UDH
    return ref, total, num, payload


def _parse_display_text(raw):
    for tag, val in _ber_tlv_list(_proactive_body(raw)):
        if tag in (0x8D, 0x0D) and val:
            return _decode_dcs_text(val)
    return None


# TS 102 223 8.24 + Table 9.4: the Items Next Action Indicator reuses the
# Type of Command coding, but only the values the table marks "Used for Next
# Action Indicator".  The ToC-only values are reserved there and shall be
# ignored, as are '00' and any value not listed (8.24).
NAI_TYPES = frozenset([
    0x10, 0x11, 0x12, 0x13, 0x15,                 # SET UP CALL / SEND SS / SEND USSD / SEND SM / LAUNCH BROWSER
    0x20, 0x21, 0x22, 0x23, 0x24, 0x25, 0x28,     # PLAY TONE .. SET UP IDLE MODE TEXT
    0x30, 0x31, 0x32, 0x33,                       # PERFORM CARD APDU .. GET READER STATUS
    0x40, 0x41, 0x42, 0x43, 0x44, 0x45, 0x46,     # BIP commands
    0x60, 0x61, 0x62,                             # MMS commands
    0x81,                                         # end of the proactive session
])


def _nai_name(value):
    """Items Next Action Indicator name (TS 102 223 Table 9.4); None = reserved."""
    if value in NAI_TYPES:
        return PROACTIVE_TYPE_NAMES.get(value)
    if 0xF0 <= value <= 0xFE:
        return 'Proprietary (0x%02X)' % value
    return None


def _attach_nai(items, nai):
    """Attach an Items Next Action Indicator list (TS 102 223 8.24) to items.

    One byte per item in list order; a short list leaves the tail without an
    indicator, extra bytes are ignored, reserved values are skipped (8.24)."""
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


def _item_line(item):
    """'1. Menu' plus ' -> SET UP MENU' when the item has a next action."""
    line = '%s. %s' % (item['id'], item['text'])
    if item.get('nai_name'):
        line += ' \u2192 ' + item['nai_name']
    return line


def _cmd_qualifier(tlvs):
    """Command Qualifier byte of the Command details TLV (TS 102 223 8.6)."""
    cd = _cmd_tlv(tlvs, 0x81)
    return cd[2] if len(cd) >= 3 else None


def _parse_get_inkey(raw):
    """GET INKEY (0x22) fields: prompt text and the qualifier's request flags
    (TS 102 223 6.6.2, 8.6, 8.15).  Returns None when the template is not a
    parseable GET INKEY."""
    tlvs = httpota.proactive_tlvs(raw)
    text = _cmd_tlv(tlvs, 0x8D) if tlvs else b''
    if not text:
        return None
    qual = _cmd_qualifier(tlvs)
    try:
        prompt = _decode_dcs_text(text)
    except Exception:
        return None
    return {
        'text': prompt,
        'digits_only': bool(qual is not None and not (qual & 0x01)),
        'ucs2': bool(qual is not None and (qual & 0x02)),
        'yes_no': bool(qual is not None and (qual & 0x04)),
        'immediate': bool(qual is not None and (qual & 0x08)),
        'help': bool(qual is not None and (qual & 0x80)),
    }


def _parse_get_input(raw):
    """GET INPUT (0x23) fields: prompt text, Response length (8.11), Default
    Text (8.23) and the qualifier's request flags (TS 102 223 6.6.3, 8.6).
    Returns None when the template is not a parseable GET INPUT."""
    tlvs = httpota.proactive_tlvs(raw)
    text = _cmd_tlv(tlvs, 0x8D) if tlvs else b''
    if not text:
        return None
    qual = _cmd_qualifier(tlvs)
    rl = _cmd_tlv(tlvs, 0x91)
    default = _cmd_tlv(tlvs, 0x9D)
    try:
        prompt = _decode_dcs_text(text)
        default_text = _decode_dcs_text(default) if default else None
    except Exception:
        return None
    return {
        'text': prompt,
        'min': rl[0] if len(rl) >= 1 else 0,
        'max': rl[1] if len(rl) >= 2 else 0xFF,
        'default': default_text,
        'digits_only': bool(qual is not None and not (qual & 0x01)),
        'ucs2': bool(qual is not None and (qual & 0x02)),
        'hidden': bool(qual is not None and (qual & 0x04)),
        'packed': bool(qual is not None and (qual & 0x08)),
        'help': bool(qual is not None and (qual & 0x80)),
    }


def _parse_select_item(raw):
    items = []
    nai = None
    for tag, val in _ber_tlv_list(_proactive_body(raw)):
        if tag in (0x85, 0x05) and val:
            try:
                _title = _decode_stk_text(val)
            except Exception:
                pass
        elif tag in (0x8F, 0x0F) and len(val) >= 2:
            items.append({'id': val[0], 'text': _decode_stk_text(val[1:])})
        elif tag in (0x18, 0x98) and val:
            nai = val
    return _attach_nai(items, nai)


def _parse_setup_menu_items(raw):
    items = []
    nai = None
    for tag, val in _ber_tlv_list(_proactive_body(raw)):
        if tag in (0x8F, 0x0F) and len(val) >= 2:
            items.append({'id': val[0], 'text': _decode_stk_text(val[1:])})
        elif tag in (0x18, 0x98) and val:
            nai = val
    return _attach_nai(items, nai)


def _handle_proactive_chain(scc, sw91, on_fetch=None, status_poll=True):
    """Run a FETCH/TERMINAL RESPONSE chain; marks the card as busy so that
    terminal-initiated ENVELOPEs (Data available, Channel status, timers) wait.
    ``status_poll=False`` skips the trailing STATUS (used by the profile
    switch, where the card is mid-switch and must not be queried further)."""
    global _PROACTIVE_BUSY
    _PROACTIVE_BUSY = True
    try:
        return _run_proactive_chain(scc, sw91, on_fetch, status_poll)
    finally:
        _PROACTIVE_BUSY = False


def _run_proactive_chain(scc, sw91, on_fetch=None, status_poll=True):
    sys.stderr.write('91XX chain: sw=%s\n' % sw91)
    sw = sw91
    paused = False
    while sw.startswith('91'):
        fetch_len = int(sw[2:], 16) if len(sw) == 4 else 0x100
        rv = scc._tp.send_apdu('%s120000%02x' % (scc.cat_cla, fetch_len))
        sys.stderr.write('FETCH(%s): %s -> %s\n' % (fetch_len, rv[0] if rv[0] else '(none)', rv[1]))
        fdata, sw = rv[0], rv[1]
        raw = bytes.fromhex(fdata) if fdata else None
        action = None
        cmd_num, cmd_type, dev_src, dev_dst, cmd_qual = 1, 0, 0x83, 0x81, None
        entry = None
        if raw:
            # Every step is guarded: a decoder bug must never break the
            # FETCH/TERMINAL RESPONSE conversation (live 2026-09-30: an
            # IndexError in the header parser turned into a 500 with the
            # card's command left unanswered).  The exception and the
            # offending bytes go to the log; the TR is still sent.
            try:
                cmd_num, cmd_type, dev_src, dev_dst, cmd_qual = _parse_proactive_header(raw)
            except Exception as e:
                sys.stderr.write('FETCH parse error: %s (raw=%s)\n' % (e, raw.hex()))
            try:
                entry = _log_proactive(cmd_type, raw, cmd_qual, cmd_num)
            except Exception as e:
                sys.stderr.write('FETCH log error: %s (raw=%s)\n' % (e, raw.hex()))
            try:
                if cmd_type == 0x25:
                    # Keep the cached menu current: text-based menu selection
                    # and the Phone tab must see the menu the card sends now
                    # (an applet install changes the items and their ids).
                    menu = _parse_setup_menu_command(raw)
                    if menu and _server_ref:
                        _server_ref.sim_menu = menu
            except Exception as e:
                sys.stderr.write('SET UP MENU cache error: %s (raw=%s)\n'
                                 % (e, raw.hex()))
        try:
            if raw and cmd_type in (0x03, 0x04):
                _handle_card_poll_command(cmd_type, raw)
            if on_fetch:
                action = on_fetch(raw, cmd_num, cmd_type, dev_src, dev_dst)
        except Exception as e:
            sys.stderr.write('FETCH handler error: %s (raw=%s)\n' % (
                e, raw.hex() if raw else '(none)'))
        paused = action == 'pause'
        if not paused:
            tr_tlv = None
            try:
                if raw and cmd_type in (0x40, 0x41, 0x42, 0x43, 0x44):
                    tr_tlv = _handle_bip_command(scc, cmd_num, cmd_type, cmd_qual, raw, dev_src, dev_dst)
                if cmd_type == 0x27:
                    tr_tlv = _handle_timer_command(cmd_num, cmd_type, cmd_qual, raw, dev_src, dev_dst)
            except Exception as e:
                sys.stderr.write('FETCH handler error: %s (raw=%s)\n' % (
                    e, raw.hex() if raw else '(none)'))
                tr_tlv = None
            if tr_tlv is None:
                tr_tlv = _build_tr(scc, cmd_num, cmd_type, dev_src, dev_dst, cmd_qual)
            tr_rv = scc._tp.send_apdu('%s140000%02x%s' % (scc.cat_cla, len(tr_tlv), tr_tlv.hex()))
            sys.stderr.write('TR: cmd=%02x type=%02x -> %s %s\n' % (cmd_num, cmd_type, tr_rv[1], ('(%d bytes)' % len(tr_tlv))))
            _record_tr(entry, tr_tlv, tr_rv[1])
            sw = tr_rv[1]
            if sw == '9000' and status_poll:
                sys.stderr.write('STATUS poll (chain ended)\n')
                st_data, st_sw = _send_status(scc)
                sys.stderr.write('STATUS -> %s\n' % st_sw)
                if st_sw.startswith('91'):
                    sw = st_sw
            if action == 'exit':
                _bip_flush_channel_events(scc)
                return sw
    if not paused:
        # Never inject an ENVELOPE while a fetched command awaits its
        # TERMINAL RESPONSE (the menu browser answers it later).
        _bip_flush_channel_events(scc)


def _parse_setup_menu_command(raw):
    """Parse a SET UP MENU proactive command: title (Alpha identifier
    0x85/0x05) and items (0x8F/0x0F) with their Items Next Action Indicator.

    Both TLV tag styles must be accepted (TS 101 220 7.1.1): the live Alfa
    card sends the title as '85' while this parser used to look for '05'
    only, so the menu arrived without a title and the PWA showed "No menu set
    by the card" (2026-09-29, card-specific because of the tag variant).
    Returns the menu dict or None when the template is not a SET UP MENU."""
    if not raw or raw[0] != 0xD0:
        return None
    tlvs = httpota.proactive_tlvs(raw)
    cd = _cmd_tlv(tlvs, 0x81)
    if len(cd) < 3 or cd[1] != 0x25:
        return None
    menu = {'command_number': cd[0], 'items': []}
    # an Item walk, not a TLV map: every 0x8F occurrence must be collected
    off = _skip_ber_len(raw, 1)
    nai = None
    while off < len(raw) - 1:
        tag, tlen = raw[off], raw[off + 1]
        val = raw[off + 2: off + 2 + tlen]
        off += 2 + tlen
        if tag in (0x85, 0x05) and tlen >= 1:
            try:
                menu['title'] = _STK_DECODE._decode(val, {}, 'stk_title')
            except Exception:
                menu['title'] = val.hex()
        elif tag in (0x8F, 0x0F) and tlen >= 2:
            try:
                txt = _STK_DECODE._decode(val[1:], {}, 'stk_item')
            except Exception:
                txt = val[1:].hex()
            menu['items'].append({'id': val[0], 'text': txt})
        elif tag in (0x18, 0x98) and tlen >= 1:
            nai = val
    _attach_nai(menu['items'], nai)
    return menu


def _send_terminal_profile(scc, tp_hex):
    tp_data, tp_sw = scc._tp.send_apdu('%s100000%02x%s' % (scc.cat_cla, len(tp_hex) // 2, tp_hex))
    sim_menu = None
    event_list = None
    if tp_sw.startswith('91'):
        sw = tp_sw
        while sw.startswith('91'):
            fetch_len = int(sw[2:], 16) if len(sw) == 4 else 0xff
            fdata, sw = scc._tp.send_apdu('%s120000%02x' % (scc.cat_cla, fetch_len))
            cmd_num, cmd_type = 1, 0
            cmd_qual = None
            dev_src, dev_dst = 0x83, 0x81
            if fdata:
                raw = bytes.fromhex(fdata)
                if raw[0] == 0xD0:
                    off = _skip_ber_len(raw, 1)
                    while off < len(raw) - 1:
                        tag, tlen = raw[off], raw[off + 1]
                        val = raw[off + 2: off + 2 + tlen]
                        off += 2 + tlen
                        if tag in (0x81, 0x01) and tlen >= 3:
                            cmd_num, cmd_type, cmd_qual = val[0], val[1], val[2]
                        elif tag in (0x82, 0x02) and tlen >= 2:
                            dev_src, dev_dst = val[0], val[1]
                        elif tag in (0x99, 0x19) and tlen >= 1:
                            event_list = [b for b in val]
                    if cmd_type == 0x25:
                        menu = _parse_setup_menu_command(raw)
                        if menu:
                            sim_menu = menu
            if fdata and cmd_type in (0x03, 0x04):
                _handle_card_poll_command(cmd_type, raw)
            if fdata and cmd_type:
                entry = _log_proactive(cmd_type, raw, cmd_qual, cmd_num)
            else:
                entry = None
            tr_tlv = _build_tr(scc, cmd_num, cmd_type, dev_src, dev_dst, cmd_qual)
            tr_rv = scc._tp.send_apdu('%s140000%02x%s' % (scc.cat_cla, len(tr_tlv), tr_tlv.hex()))
            sys.stderr.write('TR(tp): cmd=%02x type=%02x -> %s\n' % (cmd_num, cmd_type, tr_rv[1]))
            _record_tr(entry, tr_tlv, tr_rv[1])
            sw = tr_rv[1]
            if sw == '9000':
                sys.stderr.write('STATUS poll (tp chain ended)\n')
                st_data, st_sw = _send_status(scc)
                sys.stderr.write('STATUS -> %s\n' % st_sw)
                if st_sw.startswith('91'):
                    sw = st_sw
    return sim_menu, event_list


def _validate_tp_hex(hex_str):
    """Validate a TERMINAL PROFILE hex string. Returns (hex_or_None, error)."""
    h = re.sub(r'\s', '', str(hex_str or '')).upper()
    if not h:
        return None, 'profile is empty'
    if not re.fullmatch(r'[0-9A-F]+', h):
        return None, 'profile is not valid hex'
    if len(h) % 2:
        return None, 'profile must have an even number of hex digits'
    if len(h) // 2 > 255:
        return None, 'profile exceeds 255 bytes'
    return h, None


def _resend_terminal_profile(server, scc):
    """Re-send the current TERMINAL PROFILE and reset the STK session state.
    Shared by /api/rescue and the runtime profile update; the value lives in
    server.terminal_profile (CLI default, overridable at runtime)."""
    server.stk_pending = None
    server.menu_active = False
    _cancel_menu_timeout()
    server.event_list = None
    _reset_proactive_log()
    sm, el = _send_terminal_profile(scc, server.terminal_profile)
    server.sim_menu = sm
    server.event_list = el
    return {'ok': True, 'profile': server.terminal_profile,
            'menu': sm is not None, 'events': el}


def _make_menu_fetch_handler(server, resp):
    """on_fetch callback for the menu chain: pauses on user-interactive commands
    and stores the pending command so a TERMINAL RESPONSE can be sent later."""
    def _on_menu_fetch(raw, cmd_num, cmd_type, dev_src, dev_dst):
        if cmd_type == 0x21:
            text = _parse_display_text(raw) if raw else None
            if text:
                server.stk_pending = {'type': 'display_text',
                    'cmd_num': cmd_num, 'cmd_type': cmd_type,
                    'dev_src': dev_src, 'dev_dst': dev_dst, 'text': text}
                resp.update(type='display_text', text=text)
                return 'pause'
        elif cmd_type == 0x24:
            items = _parse_select_item(raw) if raw else []
            server.stk_pending = {'type': 'select_item',
                'cmd_num': cmd_num, 'cmd_type': cmd_type,
                'dev_src': dev_src, 'dev_dst': dev_dst, 'items': items}
            resp.update(type='select_item', items=items)
            return 'pause'
        elif cmd_type == 0x25:
            items = _parse_setup_menu_items(raw) if raw else []
            server.stk_pending = {'type': 'select_item',
                'cmd_num': cmd_num, 'cmd_type': cmd_type,
                'dev_src': dev_src, 'dev_dst': dev_dst, 'items': items}
            resp.update(type='select_item', items=items)
            return 'pause'
        elif cmd_type in (0x22, 0x23):
            # GET INKEY / GET INPUT: the user's answer is the TR's Text
            # string, so the panel must pause for it (v3.6.38)
            info = (_parse_get_inkey(raw) if cmd_type == 0x22
                    else _parse_get_input(raw)) if raw else None
            if info:
                kind = 'get_inkey' if cmd_type == 0x22 else 'get_input'
                pending = dict(info, type=kind, cmd_num=cmd_num,
                               cmd_type=cmd_type, dev_src=dev_src, dev_dst=dev_dst)
                dur = _find_duration(raw)
                pending['duration_unit'] = dur[0] if dur else None
                pending['shown_at'] = time.monotonic()
                server.stk_pending = pending
                resp.update(dict(info, type=kind))
                return 'pause'
    return _on_menu_fetch


# Hidden entry allows only the digits set (TS 102 223 6.4.3); digits-only
# requests use the same set for both input commands (6.4.2/6.4.3).
_DIGITS_CHARSET = re.compile(r'^[0-9*#+]*$')


def _validate_input_response(pd, text):
    """Validate the user's input against the pending GET INKEY / GET INPUT
    request (TS 102 223 6.4.2/6.4.3).  Returns (text, error)."""
    text = '' if text is None else str(text)
    if pd['type'] == 'get_inkey':
        if pd.get('yes_no'):
            if text not in ('01', '00'):
                return None, 'yes/no response must be 01 (positive) or 00 (negative)'
            return text, None
        if len(text) != 1:
            return None, 'GET INKEY needs exactly one character'
        if pd.get('digits_only') and not _DIGITS_CHARSET.match(text):
            return None, 'digits only (0-9, *, #, +)'
        return text, None
    lo = int(pd.get('min', 0) or 0)
    hi = int(pd.get('max', 0xFF) or 0)
    if len(text) < lo:
        return None, 'at least %d character(s) required' % lo
    if hi not in (0, 0xFF) and len(text) > hi:
        return None, 'at most %d character(s) allowed' % hi
    if (pd.get('digits_only') or pd.get('hidden')) and not _DIGITS_CHARSET.match(text):
        return None, 'digits only (0-9, *, #, +)'
    return text, None


def _pack_gsm7(septets):
    """Pack GSM 03.38 septets into 7-bit octets (TS 23.038 4, SMS packing).
    One implementation, shared with the scripted TR builder
    (`testscript.pack_gsm7`)."""
    return testscript.pack_gsm7(septets)


def _input_text_tlv(pd, text):
    """Text string TLV (TS 102 223 8.15) carrying the user's answer for a GET
    INKEY / GET INPUT TERMINAL RESPONSE (6.8.5): the response is coded in the
    SMS default alphabet unpacked (DCS '04'), as UCS2 ('08') when the request
    asked for it, or packed ('00') for a packed GET INPUT."""
    if text == '':
        return bytes([0x8D, 0x00])            # null text string (empty input)
    if pd.get('yes_no'):
        body = bytes([0x04, 0x01 if text == '01' else 0x00])
    elif pd.get('ucs2'):
        body = bytes([0x08]) + text.encode('utf-16-be')
    elif pd.get('packed'):
        body = bytes([0x00]) + _pack_gsm7(text.encode('gsm03.38'))
    else:
        body = bytes([0x04]) + text.encode('gsm03.38')
    return bytes([0x8D, len(body)]) + body


def _menu_send_response(server, result, item_id=None, text=None):
    """Send the pending command's TERMINAL RESPONSE and continue the chain.
    Shared by /api/menu-respond and the user-input timeout watchdog.  `text`
    is the user's answer for a pending GET INKEY / GET INPUT (v3.6.38).
    Returns (payload, http_status)."""
    if not server.stk_pending:
        return {'error': 'no pending command'}, 400
    scc = server.scc
    RESULT_MAP = {'ok': 0x00, 'cancel': 0x10, 'back': 0x11, 'timeout': 0x12,
                  'help': 0x13}
    gr = RESULT_MAP.get(result, 0x00)
    pd = server.stk_pending
    input_types = ('get_inkey', 'get_input')
    text_tlv = b''
    duration_tlv = b''
    if result == 'ok' and pd['type'] in input_types:
        text, err = _validate_input_response(pd, text)
        if err:
            return {'error': err}, 400
        try:
            text_tlv = _input_text_tlv(pd, text)
        except UnicodeEncodeError as e:
            return {'error': 'cannot encode the response text: %s' % e}, 400
        if pd.get('duration_unit') is not None and pd.get('shown_at') is not None:
            # TS 102 223 6.4.2/6.4.3: a variable-timeout request gets the
            # actual display/entry duration back, in the requested unit (8.8)
            elapsed = max(0.0, time.monotonic() - pd['shown_at'])
            value = int(round(elapsed * {0x00: 1 / 60.0, 0x01: 1.0,
                                         0x02: 10.0}.get(pd['duration_unit'], 1.0)))
            value = max(1, min(255, value))
            duration_tlv = bytes([0x04, 0x02, pd['duration_unit'], value])
    cd = bytes([0x81, 0x03, pd['cmd_num'], pd['cmd_type'], 0x00])
    di = bytes([0x82, 0x02, pd['dev_dst'], pd['dev_src']])
    # TS 102 223 6.8.0 object order: Result (C), Duration (D), Text string (E),
    # Item identifier (F).
    tr_data = cd + di
    tr_data += bytes([0x83, 0x02, gr, 0x00])
    tr_data += duration_tlv + text_tlv
    if isinstance(item_id, int) and result == 'ok' and pd['type'] == 'select_item':
        tr_data += bytes([0x90, 0x01, item_id])
    tr_hex = '%s140000%02x%s' % (scc.cat_cla, len(tr_data), tr_data.hex())
    tr_rv = scc._tp.send_apdu(tr_hex)
    sys.stderr.write('TR(menu): cmd=%02x type=%02x result=%02x -> %s\n' % (pd['cmd_num'], pd['cmd_type'], gr, tr_rv[1]))
    for entry in reversed(_PROACTIVE_LOG):
        if (entry.get('cmd_num') == pd['cmd_num']
                and entry.get('type_hex') == '%02x' % pd['cmd_type']
                and 'tr_hex' not in entry):
            _record_tr(entry, tr_data, tr_rv[1])
            break
    sw = tr_rv[1]
    resp = {'sw': sw}
    if result == 'cancel':
        server.stk_pending = None
        server.menu_active = False
    else:
        server.stk_pending = None
        if sw.startswith('91'):
            _handle_proactive_chain(scc, sw, _make_menu_fetch_handler(server, resp))
        else:
            server.menu_active = False
            resp['type'] = 'done'
    if server.stk_pending:
        _arm_menu_timeout()
    else:
        _cancel_menu_timeout()
    return resp, 200


def _finish_pending_menu(server, scc):
    """A new menu selection must never shadow a FETCHed command that awaits its
    TERMINAL RESPONSE: answer it with a cancel TR (0x10) first, then drain any
    follow-up proactive command so the card is ready for the new selection."""
    pd = server.stk_pending
    if not pd:
        return
    sys.stderr.write('MENU-SELECT: finishing pending cmd=%02x type=%02x with cancel TR\n'
                     % (pd['cmd_num'], pd['cmd_type']))
    resp, _ = _menu_send_response(server, 'cancel', None)
    sw = (resp or {}).get('sw', '')
    if sw.startswith('91'):
        _handle_proactive_chain(scc, sw)


# ===== Test scripts (scripted card dialogue) =====
# A script runs server-side in a worker thread and owns the card while it
# runs: other card endpoints answer 409 (_TEST_BLOCKED_PATHS) and background
# STATUS polling is suspended until the run finishes.  Steps and results live
# in _TEST_RUN (polled by the PWA via GET /api/test/status); the pure engine
# (validation, checks, TERMINAL RESPONSE building) is in testscript.py.

_TEST_RUNNING = False
_TEST_LOG_MAX = 600            # buffered run-log lines (the report's excerpt)
_TEST_MEMBER_LOG_MAX = 120     # lines kept per suite member
_TEST_RUN = {
    'running': False, 'stop': False, 'name': None, 'status': None,
    'session': None, 'index': 0, 'total': 0, 'steps': [],
    'preset': None, 'script_id': None,
    'scp80_counter': None, 'scp80_counters': {},
    'started': None, 'finished': None, 'error': None,
    # suite runs (v3.22.0): None for a single script; the members list and the
    # report summary live here, plus the run-log buffer/adm state
    'suite': None, 'log': [], 'log_prefix': '', 'adm': None,
}
_TEST_LOCK = threading.Lock()
_TEST_THREAD = None
_TEST_KIND_LABELS = {
    'envelope': 'ENVELOPE(Event Download)', 'event': 'ENVELOPE(Event Download)',
    'menu-select': 'ENVELOPE(Menu Selection)',
    'file-write': 'UPDATE FILE', 'file-read': 'READ FILE', 'apdu': 'APDU',
    'scp80': 'SCP80', 'status': 'STATUS', 'cleanup': 'CLEANUP',
    'proactive-drain': 'DRAIN',
}
# Card-touching endpoints refused while a script owns the card.
_TEST_BLOCKED_PATHS = frozenset([
    '/api/command', '/api/cardinfo', '/api/tree', '/api/select', '/api/read',
    '/api/write', '/api/apdu', '/api/verify-adm', '/api/send-ota',
    '/api/ram-install', '/api/ram-install-app', '/api/cap-compat',
    '/api/tar-probe', '/api/counter-probe',
    '/api/sp-verify', '/api/menu-select',
    '/api/menu-respond', '/api/event-send', '/api/net-sim',
    '/api/net-state-refresh', '/api/status-poll', '/api/rescue',
    '/api/terminal-profile', '/api/poll-toggle', '/api/esim/chip',
    '/api/esim/profiles', '/api/esim/notifications', '/api/esim/profile',
    '/api/scp81/bip', '/api/scp81/queue', '/api/scp81/psk-map',
    '/api/scp81/gen-install', '/api/scp81/log-clear',
    '/api/bip/control', '/api/bip/log-clear',
])
# GET endpoints that only read cached state (or the static file tree): served
# without _CARD_LOCK so they stay answerable while a long card operation runs
# (see do_GET).  Card-touching GETs are deliberately absent: /api/cardinfo
# (runs cardinfo on the card) and /api/esim/* (ES10 selection + STORE DATA).
_CARD_FREE_GET = frozenset([
    '/api/status', '/api/test/status', '/api/version',
    '/api/stk-status', '/api/poll-status', '/api/proactive-log',
    '/api/menu', '/api/events', '/api/terminal-profile',
    '/api/pli-qualifiers', '/api/pli-dict', '/api/commands',
    '/api/net-state', '/api/mcc-mnc', '/api/presets', '/api/test/scripts',
    '/api/test/suites',
    '/api/scp81/status', '/api/bip/status', '/api/scp81/script',
    '/api/scp81/log', '/api/bip/log',
])

# TAR values allocated by ETSI (TS 101 220 V18.3.0 Annex D, Table D.1): the
# TAR probe sends one harmless command to each and reports the card's answer.
# 'B00200' is not an application - it is the start of the RFU range, kept as a
# control (a card must not answer it).
TAR_PROBE_TARS = [
    ('000000', 'Issuer Security Domain (compact)'),
    ('B20100', 'Issuer Security Domain (expanded)'),
    ('B00000', 'UICC shared file system RFM (compact)'),
    ('B00001', 'ADF RFM (compact)'),
    ('B00010', 'SIM file system RFM (compact)'),
    ('B00120', 'UICC shared file system RFM (expanded)'),
    ('B00130', 'SIM file system RFM (expanded)'),
    ('B00140', 'ADF RFM (expanded)'),
    ('B20000', 'USAT interpreter'),
    ('B20200', 'Multiplexing application'),
    ('B20201', 'Controlling Authority Security Domain'),
    ('B20202', 'OMA BCAST audience measurement'),
    ('B20203', 'OMA DM LWM2M application'),
    ('B00200', 'RFU (control)'),
]
TAR_PROBE_DEFAULT_APDU = '00A40000023F00'   # SELECT MF: harmless, applet-visible

# The TARs the tool treats as remote-management targets: the preset's role
# entries (checked at call time - a card may use non-standard values) plus
# the ISD/RFM allocations of TS 101 220 V18.3.0 Annex D, Table D.1.  A command
# sent to any other TAR goes to a receiving application whose data format is
# application-specific (TS 102 226 4), so its response is reported raw -
# never decoded as an RM response with a fabricated status word.
RM_TAR_ALLOCATIONS = ('000000', 'B20100', 'B00000', 'B00001', 'B00010',
                      'B00120', 'B00130', 'B00140')


def _tar_is_rm(preset, tar):
    """True when `tar` is a remote-management TAR (a preset role entry or a
    standard ISD/RFM allocation), False for any other TAR; None when the TAR
    is empty (unknown).  Drives the response-form decision of `_decode_por`."""
    t = str(tar or '').strip().upper()
    if not t:
        return None
    for entry in (preset or {}).get('tars') or []:
        if entry.get('role') and str(entry.get('tar') or '').strip().upper() == t:
            return True
    return t in RM_TAR_ALLOCATIONS


def _tar_probe_verdict(step):
    """Classify one TAR probe step: the card's ENVELOPE verdict plus the PoR
    the registered application answered with."""
    pstatus = str(step.get('por_status') or '')
    if pstatus == 'por_ok':
        return 'registered' if (step.get('por_sw') or step.get('por_data')) else 'no_answer'
    if pstatus == 'no_por':
        return 'no_por'
    return 'refused'


_TEST_COMMAND_TYPES = {name.upper(): code for code, name in PROACTIVE_TYPE_NAMES.items()}


def _test_command_type(name):
    return _TEST_COMMAND_TYPES.get((name or '').upper())


def _test_state_snapshot():
    with _TEST_LOCK:
        return json.loads(json.dumps(_TEST_RUN))


def _test_suite_refs_error(script_store, suite):
    """The cross-store rule: every member script must exist in the script
    store (a suite holds references; the scripts are the single source of
    truth).  Returns an error string, or '' when the references resolve."""
    if not isinstance(suite, dict):
        return ''
    missing = []
    for e in (suite.get('scripts') or []):
        if not isinstance(e, dict):
            continue
        sid = str(e.get('script_id') or '').strip().lower()
        if sid and script_store.get(sid) is None:
            missing.append(sid)
    if missing:
        return 'unknown test script(s): %s' % ', '.join(sorted(set(missing))[:5])
    return ''


def _test_script_detach(suites, cur):
    """Remove a script from its suite's member list (before a delete or a
    move).  Best effort: the mutation endpoints report their own errors."""
    suite = suites.get(cur.get('suite_id')) if cur.get('suite_id') else None
    if suite is None:
        return
    entries = [e for e in suite['scripts'] if e['script_id'] != cur['id']]
    if entries == suite['scripts']:
        return
    try:
        suites.update(suite['id'], {'scripts': entries})
    except (test_suites.TestSuiteError, OSError) as e:
        sys.stderr.write('TESTSUITES: cannot detach %s: %s\n' % (cur['id'], e))


def _test_script_move(store, suites, cur, target_id):
    """Move a script to another suite: the source detaches, the target appends
    it as a member (keeping the on_fail policy it had).  Both stores are
    updated in the one request.  Returns ``(entry, error, status)``."""
    target = suites.get(target_id)
    if target is None:
        return None, 'unknown target suite', 400
    if target['id'] == (cur.get('suite_id') or ''):
        return cur, None, 200
    source = suites.get(cur.get('suite_id')) if cur.get('suite_id') else None
    on_fail = 'stop'
    if source is not None:
        entry = next((e for e in source['scripts'] if e['script_id'] == cur['id']), None)
        if entry is not None:
            on_fail = entry['on_fail']
        _test_script_detach(suites, cur)
    try:
        new = store.update(cur['id'], {'suite_id': target['id']})
    except (test_scripts.TestScriptError, OSError) as e:
        return None, 'cannot move the script: %s' % e, 500
    if new is None:
        return None, 'unknown test script id', 404
    try:
        entries = list(target['scripts']) + [
            {'script_id': cur['id'], 'role': 'member', 'on_fail': on_fail}]
        suites.update(target['id'], {'scripts': entries})
    except (test_suites.TestSuiteError, OSError) as e:
        return None, 'cannot attach to the target suite: %s' % e, 500
    return new, None, 200


def reconcile_scripts_to_suites(script_store, suite_store):
    """Make the ownership invariant hold again: every script belongs to
    exactly one suite and that suite lists it.

    - a script no suite lists (a v3.21.0 store, a suites import with
      ``mode: "replace"``, a hand edit or an interrupted move) is attached to
      the auto-created "Imported scripts" suite;
    - a script a suite lists but whose ``suite_id`` is empty or points
      elsewhere adopts the listing suite (the member list is authoritative).

    Idempotent; returns the number of changed scripts.  Called at startup
    (the v3.21.0 migration) and after a suites import."""
    if script_store is None or suite_store is None:
        return 0
    suites = suite_store.list()
    owner = {}
    for s in suites:
        for e in s['scripts']:
            owner.setdefault(e['script_id'], s['id'])
    orphans = []
    adopted = 0
    for script in script_store.list():
        sid = script.get('suite_id') or None
        if sid and owner.get(script['id']) == sid:
            continue
        listed_by = owner.get(script['id'])
        if listed_by:
            # the member list is authoritative: adopt the listing suite
            try:
                script_store.update(script['id'], {'suite_id': listed_by})
                adopted += 1
            except (test_scripts.TestScriptError, OSError) as e:
                sys.stderr.write('TESTSUITES: cannot adopt %s: %s\n'
                                 % (script['id'], e))
            continue
        orphans.append(script)
    if not orphans and not adopted:
        return 0
    suite = suite_store.find_by_name('Imported scripts')
    if suite is None:
        suite = suite_store.add({'name': 'Imported scripts'})
    entries = list(suite['scripts'])
    known = {e['script_id'] for e in entries}
    attached = 0
    for s in orphans:
        try:
            script_store.update(s['id'], {'suite_id': suite['id']})
        except (test_scripts.TestScriptError, OSError) as e:
            # The member list is authoritative: list the script anyway.  The
            # store keeps a script that no longer validates (a tightened rule
            # must not destroy it) and its suite_id write is refused by the
            # same validator - without the listing the script would be
            # invisible in the PWA and could never be fixed in the editor.
            sys.stderr.write('TESTSUITES: cannot set the suite id of %s '
                             '(listed in "%s" so the editor can fix it): %s\n'
                             % (s['id'], suite['name'], e))
        if s['id'] not in known:
            entries.append({'script_id': s['id'], 'role': 'member', 'on_fail': 'stop'})
            known.add(s['id'])
        attached += 1
    if attached:
        try:
            suite_store.update(suite['id'], {'scripts': entries})
        except (test_suites.TestSuiteError, OSError) as e:
            sys.stderr.write('TESTSUITES: cannot fill "%s": %s\n' % (suite['name'], e))
    if attached or adopted:
        sys.stderr.write('TESTSUITES: reconciled %d script(s) (attached to "%s": %d, '
                         'adopted by their suite: %d)\n'
                         % (attached + adopted, suite['name'], attached, adopted))
    return attached + adopted


def migrate_scripts_to_suites(script_store, suite_store):
    """The v3.21.0 -> v3.22.0 migration: attach the pre-suite scripts to the
    "Imported scripts" suite (the general reconciliation, called at startup)."""
    return reconcile_scripts_to_suites(script_store, suite_store)


def _test_request_blocked(path):
    if not _TEST_RUNNING:
        return False
    return path.split('?', 1)[0] in _TEST_BLOCKED_PATHS


def _increment_counter_hex(counter_hex):
    """SCP80 counter + 1, keeping the pattern's width (hex string)."""
    c = re.sub(r'\s', '', str(counter_hex or '')).upper()
    if not c or not re.fullmatch(r'[0-9A-F]+', c):
        return counter_hex
    width = len(c)
    return '%0*X' % (width, (int(c, 16) + 1) & ((1 << (4 * width)) - 1))


def _preset_keysets(preset):
    """The keysets of a preset dict - a v3.8.0 flat preset (kic/kid/kicKey/
    kidKey/counter) counts as a single keyset, so old data keeps working."""
    preset = preset or {}
    keysets = [ks for ks in (preset.get('keysets') or []) if isinstance(ks, dict)]
    if not keysets and (preset.get('kic') or preset.get('kid')):
        keysets = [{'kic': preset.get('kic'), 'kid': preset.get('kid'),
                    'kicKey': preset.get('kicKey'), 'kidKey': preset.get('kidKey'),
                    'cntr': preset.get('counter') or preset.get('cntr') or ''}]
    return keysets


def _test_preset_error(script, preset):
    """The SCP80 steps need at least one complete keyset in the card preset;
    a step may override the keyset (kvn), the TAR, SPI1 and SPI2 only - the
    KIc/KID keys and the counters stay preset-owned."""
    preset = preset or {}
    keysets = _preset_keysets(preset)
    if not keysets:
        return 'SCP80 preset is incomplete: no keyset is defined'
    if not [ks for ks in keysets if _keyset_complete(ks)]:
        return ('SCP80 preset is incomplete: a keyset needs KIc, KID, both keys '
                'and a counter')
    for i, step in enumerate(script['steps']):
        if step['type'] != 'action' or step['kind'] != 'scp80':
            continue
        p = step['params']
        if p.get('kvn') is not None:
            want = int(p['kvn'])
            ks = next((k for k in keysets if presets.keyset_kvn(k) == want), None)
            if ks is None:
                return 'step %d: keyset %d is not defined in the preset' % (i + 1, want)
            if not _keyset_complete(ks):
                return ('step %d: keyset %d is incomplete (KIc, KID, both '
                        'keys and a counter)' % (i + 1, want))
        tar = p.get('tar') or presets.role_tar(preset, 'isd')
        if not tar:
            return 'step %d: no TAR (neither in the step nor in the preset)' % (i + 1)
        if not (p.get('spi1') or presets.tar_msl(preset, tar)):
            return ('step %d: no SPI1 for TAR %s (neither in the step nor as '
                    'its MSL in the preset)' % (i + 1, tar))
    return None


def _keyset_complete(ks):
    """A usable keyset: both key bytes, both keys and a counter."""
    return bool(ks.get('kic') and ks.get('kid') and ks.get('kicKey')
                and ks.get('kidKey') and ks.get('cntr'))


def _test_check_result(label, ok, expected, actual, level, detail=None):
    res = {'label': label, 'ok': bool(ok), 'expected': str(expected),
           'actual': str(actual), 'level': level}
    if detail:
        res['detail'] = detail
    return res


def _test_entry(step, index):
    if step['type'] == 'expect':
        ctype = step['command'].get('type')
        kind = step['command'].get('name') or (('0x%02X' % ctype) if ctype is not None else 'ANY')
        label = 'EXPECT ' + kind
    else:
        kind = step['kind']
        label = step.get('label') or _TEST_KIND_LABELS.get(kind, kind)
    return {'index': index, 'type': step['type'], 'kind': kind, 'label': label,
            'status': 'running', 'checks': [], 'note': None, 'sent': None,
            'sw': None, 'data': None, 'ms': None, 'started': time.time()}


def _test_entry_update(entry, fields):
    """Mutate a step entry under _TEST_LOCK: the status endpoint serializes
    the whole run state with json.dumps, so entries must not change while it
    iterates them."""
    with _TEST_LOCK:
        entry.update(fields)


def _test_finish_entry(entry, result):
    fields = {'status': result.get('status', 'ok'),
              'checks': result.get('checks', []),
              'ms': int((time.time() - (entry.get('started') or time.time())) * 1000)}
    for key in ('sent', 'sw', 'data', 'note', 'por', 'sms', 'counter',
                'command', 'drained'):
        if result.get(key) is not None:
            fields[key] = result[key]
    _test_entry_update(entry, fields)


def _por_check_results(spec, por, level, prefix='PoR'):
    """The decoded-PoR assertion rows (status/sw/data), shared by the scp80
    action's `check.por` object and the expectation's `por` content check."""
    checks = []
    if por is None:
        checks.append(_test_check_result(
            prefix, False, 'a decoded PoR',
            'no PoR in this command (or no scp80 step before it)', level))
        return checks
    if 'status' in spec:
        actual = str(por.get('response_status') or '')
        want = spec['status']
        ok = (actual.lower() == want.lower()
              or (want.lower() == 'ok' and actual == 'por_ok'))
        checks.append(_test_check_result(prefix + ' status', ok, want,
                                         actual or '(none)', level))
    if 'sw' in spec:
        actual = str((por.get('decoded') or {}).get('last_status_word') or '')
        ok = testscript.match_value(spec['sw'], actual)
        detail = None
        if not ok and por.get('response_type') == 'raw':
            detail = ("the response is application data - an applet's own TAR "
                      "has no R-APDU SW; assert it with 'data'")
        checks.append(_test_check_result(prefix + ' SW', ok,
                                         spec['sw']['value'], actual or '(none)',
                                         level, detail=detail))
    if 'data' in spec:
        actual = str((por.get('decoded') or {}).get('last_response_data') or '')
        checks.append(_test_check_result(
            prefix + ' data', testscript.match_value(spec['data'], actual),
            spec['data']['value'], actual or '(none)', level))
    return checks


def _test_action_checks(step, sw, data, por, kind):
    check = step['check']
    level = step['on_fail']
    checks = []
    sw_ok = testscript.match_value(check['sw'], sw)
    checks.append(_test_check_result('SW', sw_ok, check['sw']['value'], sw or '(none)', level))
    if check.get('data'):
        if sw_ok:
            checks.append(_test_check_result('Data', testscript.match_value(check['data'], data),
                                             check['data']['value'], data or '(none)', level))
        else:
            checks.append(_test_check_result('Data', True, check['data']['value'],
                                             '(not checked - SW mismatch)', 'ok'))
    if kind == 'scp80':
        want = check.get('por')
        if isinstance(want, dict):
            # the decoded-PoR assertion (inline or submit transport)
            checks.extend(_por_check_results(want, por, level))
        elif want != 'any':
            if want == 'none':
                ok = por is None
                actual = 'none' if por is None else 'present'
            else:
                ok = bool(por) and por.get('response_status') == 'por_ok'
                actual = (por or {}).get('response_status') or 'none'
            checks.append(_test_check_result('PoR', ok, want, actual, level))
    return checks


def _test_log(msg):
    """One semantic run-log line, prefixed with the current step number: the
    verification trail (the plaintext C-APDU, the built secured packet, the
    PoR R-APDU, the FETCH/TR of an expectation, the menu selection's ENVELOPE).
    The raw transport view stays with `--apdu-trace`.  Lines are buffered for
    the run report (the suite report's per-member excerpt); the buffer is
    bounded - the stderr stream stays complete."""
    step = _TEST_RUN.get('index')
    n = (step + 1) if isinstance(step, int) else 0
    prefix = _TEST_RUN.get('log_prefix') or ''
    line = 'TEST-RUN %sstep %d: %s' % (prefix, n, msg)
    sys.stderr.write(line + '\n')
    try:
        with _TEST_LOCK:
            log = _TEST_RUN.setdefault('log', [])
            log.append(line)
            if len(log) > _TEST_LOG_MAX:
                del log[:len(log) - _TEST_LOG_MAX]
    except Exception:
        pass


def _test_run_status(scc, step):
    p = step['params']
    data, sw = '', ''
    used = 0
    for n in range(p['attempts']):
        data, sw = _send_status(scc)
        used = n + 1
        _test_log('STATUS %d/%d -> %s' % (used, p['attempts'], sw))
        if testscript.match_value(step['check']['sw'], sw):
            break
        if n + 1 < p['attempts'] and p['interval_ms']:
            time.sleep(p['interval_ms'] / 1000.0)
    return data or '', sw, ('STATUS x%d' % used if used > 1 else 'STATUS')


def _drain_require_match(req, consumed):
    """Whether one consumed command satisfies a proactive-drain action's `require`
    spec: the command type/name (ANY matches all) and an optional qualifier
    (exact/mask)."""
    if req.get('type') is not None and consumed.get('type_hex') != '%02X' % req['type']:
        return False
    q = req.get('qualifier')
    if q and not testscript.match_value(q, consumed.get('qualifier')):
        return False
    return True


def _drain_require_label(req):
    """The `require` spec as the report's expected string."""
    if req.get('type') is not None:
        name = req.get('name') or '0x%02X' % req['type']
    else:
        name = 'ANY'
    q = req.get('qualifier')
    return name + ((' q=%s' % q['value']) if q else '')


def _test_terminal_response(cmd_num, cmd_type, dev_dst, dev_src, cmd_qual, respond):
    """The runner's TERMINAL RESPONSE: the scripted answer plus the terminal's
    own command-specific object(s) - the same objects the terminal's answers
    carry (`_build_tr`), unless the script supplied `raw` TLVs, which win.
    A successful PROVIDE LOCAL INFORMATION answer takes the TR Config PLI
    dictionary entry for its qualifier (or the host clock for the date/time
    qualifier, TS 102 223 6.4.15); a POLL INTERVAL answer echoes the current
    interval (6.8.4).  Without this a scripted expectation silently stalled an
    applet that waited for its data (v3.22.2 review)."""
    respond = respond or {}
    answer = None
    if not respond.get('raw') and respond.get('result', 0) == 0x00:
        if cmd_type == 0x26 and cmd_qual is not None:
            answer = _pli_data_hex(cmd_qual)
        elif cmd_type == 0x03:
            answer = '840201%02X' % _POLL_INTERVAL
    return testscript.build_tr(cmd_num, cmd_type, dev_dst, dev_src, respond,
                               answer_tlvs=answer or None)


def _test_run_drain(server, step):
    """The proactive drain action: poll STATUS and consume every announced
    command - the TERMINAL RESPONSE is `first` for the first one, `respond`
    for the rest - until the card answers 9000 (idle), a non-91XX SW appears
    or the attempt budget is spent.  The final SW is the step's SW check; an
    optional `require` asserts what was drained (at least one match; an empty
    drain fails it)."""
    scc = server.scc
    p = step['params']
    consumed = []
    sw = ''
    polls = 0
    for n in range(p['attempts']):
        data, sw = _send_status(scc)
        polls = n + 1
        _test_log('STATUS %d/%d -> %s' % (polls, p['attempts'], sw))
        if not (sw or '').startswith('91'):
            break
        fetch_apdu = '%s120000%02x' % (scc.cat_cla, int(sw[2:], 16))
        fdata, fetch_sw = scc._tp.send_apdu(fetch_apdu)
        if not fdata:
            _test_log('FETCH=%s -> SW=%s (no data)' % (fetch_apdu, fetch_sw))
            sw = fetch_sw
            break
        raw = bytes.fromhex(fdata)
        _test_log('FETCH=%s -> SW=%s %s' % (fetch_apdu, fetch_sw, raw.hex().upper()))
        cmd_num, cmd_type, dev_src, dev_dst, cmd_qual = _parse_proactive_header(raw)
        name = PROACTIVE_TYPE_NAMES.get(cmd_type, 'UNKNOWN')
        _test_log('CMD 0x%02X %s qual=%s' % (
            cmd_type, name, ('%02X' % cmd_qual) if cmd_qual is not None else '-'))
        if cmd_type == 0x25:
            # keep the cached menu (text-based selection) current
            menu = _parse_setup_menu_command(raw)
            if menu:
                server.sim_menu = menu
        respond = p['first'] if (not consumed and p.get('first')) else p['respond']
        tr = _test_terminal_response(cmd_num, cmd_type, dev_dst, dev_src,
                                     cmd_qual, respond)
        tr_rv = scc._tp.send_apdu('%s140000%02x%s' % (scc.cat_cla, len(tr), tr.hex()))
        _test_log('TR=%s -> SW=%s' % (tr.hex().upper(), tr_rv[1]))
        consumed.append({'type_hex': '%02X' % cmd_type, 'type_name': name,
                         'qualifier': ('%02X' % cmd_qual) if cmd_qual is not None else None,
                         'tr_sw': tr_rv[1], 'raw': raw.hex().upper()})
        sw = tr_rv[1]
        if not (sw or '').startswith('91') and sw != '9000':
            # a TERMINAL RESPONSE the card rejected: stop and let the SW
            # check name it
            break
        if n + 1 < p['attempts'] and p['interval_ms']:
            time.sleep(p['interval_ms'] / 1000.0)
    checks = [_test_check_result('SW', testscript.match_value(step['check']['sw'], sw),
                                 step['check']['sw']['value'], sw or '(none)',
                                 step['on_fail'])]
    req = p.get('require')
    if req:
        matched = any(_drain_require_match(req, c) for c in consumed)
        if consumed:
            actual = ', '.join(
                '%s%s' % (c['type_name'],
                          (' q=%s' % c['qualifier']) if c['qualifier'] else '')
                for c in consumed)
        else:
            actual = '(nothing was pending)'
        checks.append(_test_check_result('Require', matched,
                                         _drain_require_label(req), actual,
                                         step['on_fail']))
    status = testscript.combine_levels([c['level'] if not c['ok'] else 'ok'
                                        for c in checks])
    result = {'status': status, 'sw': sw or '', 'data': '',
              'sent': 'STATUS x%d; TR x%d' % (polls, len(consumed)),
              'checks': checks, 'drained': consumed}
    pending = int(sw[2:], 16) if (sw or '').startswith('91') else None
    return result, pending


def _test_run_file(server, step):
    """UPDATE/READ a file by path (extends the network simulator's writes to
    arbitrary files); returns (data, sw, sent)."""
    app = server.app
    lchan = app.rs.lchan[0]
    p = step['params']
    cleanup = None
    try:
        _, cleanup = _select_path(lchan, p['path'], app)
        is_record = _get_file_type(lchan, lchan.selected_file) in ('linear_fixed', 'cyclic')
        use_record = p['mode'] == 'record' or (p['mode'] == 'auto' and is_record)
        record = p.get('record') or 1
        if step['kind'] == 'file-write':
            if use_record:
                _out, sw = lchan.update_record(record, p['data'])
                sent = 'UPDATE RECORD %d %s' % (record, p['path'])
            else:
                _out, sw = lchan.update_binary(p['data'])
                sent = 'UPDATE BINARY %s' % p['path']
            return _out or '', sw, sent
        if use_record:
            data, sw = lchan.read_record(record)
            return data or '', sw, 'READ RECORD %d %s' % (record, p['path'])
        data, sw = lchan.read_binary()
        return data or '', sw, 'READ BINARY %s' % p['path']
    finally:
        if cleanup:
            try:
                cleanup()
            except Exception:
                pass


def _test_run_scp80(server, step, ctx):
    scc = server.scc
    preset = ctx['preset']
    p = step['params']
    # The keyset: a step may name a keyset number (`kvn`), otherwise the
    # preset's first keyset is used (each keyset has its own keys and counter -
    # TS 102 225 Annex A.1).
    keysets = _preset_keysets(preset)
    keyset = None
    if p.get('kvn') is not None:
        want = int(p['kvn'])
        keyset = next((ks for ks in keysets if presets.keyset_kvn(ks) == want), None)
        if keyset is None:
            raise testscript.ScriptError('keyset %d is not defined in the '
                                         'selected card preset' % want)
    elif keysets:
        keyset = keysets[0]
    kic = p.get('kic') or (keyset or {}).get('kic') or ''
    kid = p.get('kid') or (keyset or {}).get('kid') or ''
    kic_key = p.get('kicKey') or (keyset or {}).get('kicKey') or ''
    kid_key = p.get('kidKey') or (keyset or {}).get('kidKey') or ''
    kvn, kvn_err = _kvn_of(kic, kid)
    if kvn_err:
        raise testscript.ScriptError(kvn_err)
    tar = p.get('tar') or presets.role_tar(preset, 'isd') or ''
    try:
        # a step may override the SPI1 deliberately (the test runner sends what
        # the operator asked for); the TAR's MSL is the default when absent
        spi1, _ = _spi1_for_tar(preset, tar, p.get('spi1'))
    except ValueError as e:
        raise testscript.ScriptError(str(e))
    spi2 = p.get('spi2') or '01'
    counters = ctx.setdefault('counters', {})
    if kvn and kvn not in counters:
        counters[kvn] = (keyset or {}).get('cntr') or '00000000'
    counter = counters.get(kvn) if kvn else (ctx.get('counter') or '00000000')
    counter = counter or '00000000'
    # The PoR of this exchange may arrive as a SEND SHORT MESSAGE (PoR-in-
    # submit); the expect step decodes it with the parameters the packet was
    # built from, so remember them.  `cmd_len` is the plaintext command
    # script's length (before any format wrapping) - the compact PoR's
    # command-count bound, None for a pre-built packet.
    cmd_len = len(p.get('apdu') or '') // 2 or None
    ctx['last_scp80'] = {'spi1': spi1, 'spi2': spi2, 'kic': kic, 'kid': kid,
                         'counter': counter, 'kicKey': kic_key, 'kidKey': kid_key,
                         'tar': tar, 'cmd_len': cmd_len,
                         'rm': _tar_is_rm(preset, tar)}
    fmt = p.get('format') or 'compact'
    apdu_hex = p.get('apdu') or ''
    if p.get('sp'):
        sp_hex = p['sp']
        source = 'sp'
        _test_log('SCP80 SP=%s' % sp_hex)
    else:
        source = 'apdu'
        if fmt != 'compact':
            # TS 102 226 5.2.1: wrap the C-APDU in the Command Scripting
            # template (the expanded remote-management format the RFM/GP
            # applications expect for multi-command payloads)
            apdu_hex = _ram_format_apdu(apdu_hex, fmt)
            _test_log('SCP80 C-APDU=%s WRAPPED=%s (%s)'
                      % (p['apdu'], apdu_hex, fmt))
        else:
            _test_log('SCP80 C-APDU=%s' % apdu_hex)
        sp_hex, _ = _build_secured_packet(spi1, spi2, kic, kid, tar, counter,
                                          apdu_hex, kic_key, kid_key)
    _test_log('SCP80 SECURED=%s (%d B) TAR=%s SPI1=%s SPI2=%s CNTR=%s'
              % (sp_hex, len(sp_hex) // 2, tar or '-', spi1, spi2, counter))
    result = _send_secured_packet(scc, sp_hex, server.sms_oa, sm_sc=server.sms_sc,
                                  handle_proactive=False)
    sw = result.get('sw') or ''
    data = result.get('response_data') or ''
    segments = result.get('segments') or 0
    _test_log('SCP80 -> SW=%s%s' % (sw or '(none)',
                                    ' (%d segment(s))' % segments if segments > 1 else ''))
    por = None
    if data:
        por = _decode_por(spi1, spi2, kic, kid, counter, kic_key, kid_key, data,
                          cmd_len=cmd_len, rm=_tar_is_rm(preset, tar))
    if por:
        dec = por.get('decoded') or {}
        extra = ''
        if por.get('bad_format'):
            extra = ' bad_format=%s (%s)' % (por['bad_format'],
                                             por.get('bad_format_name') or '')
        _test_log('PoR[inline] status=%s TAR=%s CNTR=%s%s raw=%s'
                  % (por.get('response_status'), por.get('tar'),
                     por.get('cntr'), extra, por.get('raw') or data))
        if por.get('response_type') == 'raw':
            _test_log('APP DATA=%s (no R-APDU SW)'
                      % (dec.get('last_response_data') or '(none)'))
        else:
            _test_log('R-APDU SW=%s data=%s'
                      % (dec.get('last_status_word') or '(none)',
                         dec.get('last_response_data') or '(none)'))
    elif data:
        _test_log('PoR[inline] undecodable raw=%s' % data)
    else:
        _test_log('PoR[inline] none')
    sent = 'SCP80 %s TAR=%s SPI1=%s SPI2=%s cntr=%s%s %s%s' % (
        source, tar or '-', spi1, spi2, counter,
        (' kvn=%d' % kvn) if kvn else '',
        ('SP=%s' % sp_hex) if source == 'sp'
        else ('C-APDU=%s' % apdu_hex),
        (' format=%s' % fmt) if (source == 'apdu' and fmt != 'compact') else '')
    if sw and _counter_tracked(spi1):
        # The card answered and the packet asks for a counter check, so the
        # SCP80 counter was consumed: advance the working value of this key
        # version and persist it into the server-side preset (v3.8.0 - the
        # server owns the counters, so the run survives a lost response and the
        # PWA's counter write-back dance is gone).
        if kvn:
            counters[kvn] = _increment_counter_hex(counter)
            ctx['counter'] = counters[kvn]
            _preset_counter_persist(server, preset.get('id'), counters[kvn],
                                    'test-run', kvn)
        else:
            ctx['counter'] = _increment_counter_hex(counter)
            _preset_counter_persist(server, preset.get('id'), ctx['counter'], 'test-run')
    return data, sw, sent, por, counter


def _menu_item_by_text(server, p):
    """Resolve a menu-select step's text against the cached SET UP MENU items
    (the item ids vary with the applet's install parameters, so a script
    matches the text the card actually shows; the match is case-insensitive
    unless the step sets `case_sensitive: true`).  Raises a ScriptError naming
    the available items when nothing - or more than one item - matches."""
    menu = getattr(server, 'sim_menu', None) or {}
    items = menu.get('items') or []
    spec = {'mode': p.get('mode', 'exact'), 'value': p['text'],
            'case_sensitive': p.get('case_sensitive', False)}
    matches = [it for it in items
               if testscript.match_text(spec, it.get('text') or '')]
    if not matches:
        listing = ', '.join('%s=%r' % (it.get('id'), it.get('text'))
                            for it in items)
        hint = ''
        if spec.get('case_sensitive') and items:
            # A strict-case miss is a common authoring slip with non-ASCII
            # menus: name the items a case-insensitive match would find.
            loose = [it for it in items if testscript.match_text(
                dict(spec, case_sensitive=False), it.get('text') or '')]
            if loose:
                hint = (' - a case-insensitive match exists (%s): set '
                        '"case_sensitive": false or fix the case'
                        % ', '.join('%s=%r' % (it.get('id'), it.get('text'))
                                    for it in loose))
        raise testscript.ScriptError(
            'menu-select: no menu item matches %r (%s)%s'
            % (p['text'], 'menu: ' + listing if listing
               else 'no menu cached - fetch the menu (a SET UP MENU '
                    'expectation) first', hint))
    if len(matches) > 1:
        raise testscript.ScriptError(
            'menu-select: %d items match %r (%s) - use the item id'
            % (len(matches), p['text'],
               ', '.join('%s=%r' % (it.get('id'), it.get('text'))
                         for it in matches)))
    return matches[0]['id']


def _test_run_action(server, step, ctx):
    scc = server.scc
    kind = step['kind']
    p = step['params']
    por = None
    counter = None
    if kind == 'envelope':
        data, sw = _send_event_download(scc, p['event'],
                                        bytes.fromhex(p['data']) if p['data'] else None,
                                        drain=False, src=p.get('src'), log=False)
        sent = 'ENVELOPE(Event Download) type=0x%02X' % p['event']
        _test_log('%s src=%s data=%s -> SW=%s%s'
                  % (sent, p.get('src') or '82', p['data'] or '(none)', sw or '(none)',
                     (' RESP=%s' % data) if data else ''))
    elif kind == 'event':
        # The semantic event download: the server builds the events it models
        # (run time, so the data-connection host clock is fresh); every other
        # event carries the PWA form's built hex (validated at load).
        if p.get('data') is not None:
            data_hex = p['data']
        else:
            data_hex = events.build(p['event'], p.get('fields') or {})
        data, sw = _send_event_download(scc, p['event'], bytes.fromhex(data_hex),
                                        drain=False, src=p.get('src'), log=False)
        sent = 'ENVELOPE(Event Download) type=0x%02X (%s) data=%s' % (
            p['event'], events.event_name(p['event']), data_hex)
        _test_log('%s src=%s -> SW=%s%s'
                  % (sent, p.get('src') or '82', sw or '(none)',
                     (' RESP=%s' % data) if data else ''))
    elif kind == 'menu-select':
        item_id = p.get('item_id')
        if item_id is None:
            item_id = _menu_item_by_text(server, p)
        tlv = bytes([0xD3, 0x07, 0x02, 0x02, 0x01, 0x81, 0x90, 0x01, item_id])
        sent = '%sc20000%02x%s' % (scc.cat_cla, len(tlv), tlv.hex())
        server.menu_active = True
        data, sw = scc._tp.send_apdu(sent)
        if not (sw or '').startswith('91'):
            server.menu_active = False
        _test_log('MENU-SELECT ENVELOPE=%s item=%d%s -> SW=%s%s'
                  % (sent, item_id, (' (%r)' % p['text']) if 'text' in p else '',
                     sw or '(none)', (' RESP=%s' % data) if data else ''))
    elif kind == 'apdu':
        sent = p['apdu']
        data, sw = scc._tp.send_apdu(sent)
        _test_log('APDU TX=%s -> SW=%s%s'
                  % (sent, sw or '(none)', (' RESP=%s' % data) if data else ''))
    elif kind == 'status':
        data, sw, sent = _test_run_status(scc, step)
    elif kind in ('file-write', 'file-read'):
        data, sw, sent = _test_run_file(server, step)
        _test_log('%s -> SW=%s%s' % (sent, sw or '(none)',
                                     (' RESP=%dB' % (len(data) // 2)) if data else ''))
    elif kind == 'scp80':
        data, sw, sent, por, counter = _test_run_scp80(server, step, ctx)
    elif kind == 'proactive-drain':
        return _test_run_drain(server, step)
    else:
        raise testscript.ScriptError('unknown action kind %r' % kind)
    checks = _test_action_checks(step, sw, data, por, kind)
    status = testscript.combine_levels([c['level'] if not c['ok'] else 'ok' for c in checks])
    result = {'status': status, 'sw': sw or '', 'data': data or '', 'sent': sent,
              'checks': checks, 'por': por}
    if counter:
        result['counter'] = counter
    pending = int(sw[2:], 16) if (sw or '').startswith('91') else None
    return result, pending


def _test_run_script_from_body(server, body):
    """The script of a run: the inline `script` body (the PWA always sends the
    edited copy) or the stored script named by `script_id`.  Returns
    ``(script_raw, script_id, error)``; error is None on success."""
    script_raw = body.get('script')
    script_id = str(body.get('script_id') or '').strip()
    if isinstance(script_raw, dict):
        return script_raw, script_id or None, None
    if not script_id:
        return None, None, None
    store = getattr(server, 'test_scripts', None)
    entry = store.get(script_id) if store else None
    if entry is None:
        return None, None, 'test script %s not found' % script_id
    return {'name': entry['name'], 'steps': entry['steps'],
            'require_adm': bool(entry.get('require_adm'))}, entry['id'], None


def _test_run_preset_from_body(server, body):
    """The preset of a run: the inline `preset` body (external API callers,
    docs/api.md) or the stored preset named by `preset_id` - the PWA sends the
    id, so the run always uses the store's authoritative keys/TARs/counters.
    Returns ``(preset, error)``; error is None on success."""
    preset = body.get('preset') or {}
    if preset:
        return preset, None
    pid = str(body.get('preset_id') or '').strip()
    if not pid:
        return {}, None
    preset = _preset_by_id(server, pid)
    if preset is None:
        return None, 'card preset %s not found' % pid
    return preset, None


def _parse_file_list(raw):
    """The File List (TS 102 223 8.18; tag 12/92 per TS 101 220) of a fetched
    proactive command, as a list of path hex strings; [] when absent.  The
    value is the number of files followed by concatenated 2-byte FIDs - every
    path starts with the MF ('3F'), which delimits the entries."""
    try:
        value = _cmd_tlv(httpota.proactive_tlvs(raw), 0x12)
    except Exception:
        return []
    if not value:
        return []
    data = value[1:]
    paths = []
    cur = bytearray()
    for i in range(0, len(data) - len(data) % 2, 2):
        fid = data[i:i + 2]
        if len(fid) < 2:
            break
        if fid[0] == 0x3F and cur:
            paths.append(bytes(cur).hex().upper())
            cur = bytearray()
        cur += fid
    if cur:
        paths.append(bytes(cur).hex().upper())
    return paths


def _test_expect_por(ctx, raw):
    """Decode the PoR carried by a fetched SEND SHORT MESSAGE command (the
    PoR-in-submit transport, SPI2 bit 0x20, TS 102 225 5.1.1), using the
    context of the scp80 step that triggered it.  None when the command is
    not a PoR or no scp80 step ran."""
    last = (ctx or {}).get('last_scp80')
    if not last:
        return None
    tpdu_hex = _find_sms_tpdu(raw)
    if not tpdu_hex:
        return None
    try:
        ref, total, num, payload = _parse_sms_concat(bytes.fromhex(tpdu_hex))
    except Exception:
        return None
    if total is not None and num is not None and total > 1:
        segs = ctx.setdefault('por_segments', [])
        segs.append((ref, total, num, payload.hex()))
        matching = sorted([s for s in segs if s[0] == ref], key=lambda s: s[2])
        if len(matching) < total:
            return None
        ud_hex = ''.join(s[3] for s in matching)
    else:
        ud_hex = payload.hex()
    if not ud_hex:
        return None
    por_hex = '027100' + ud_hex
    return _decode_por(last['spi1'], last['spi2'], last['kic'], last['kid'],
                       last['counter'], last['kicKey'], last['kidKey'], por_hex,
                       cmd_len=last.get('cmd_len'), rm=last.get('rm'))


def _test_run_expect(server, step, pending, ctx=None):
    scc = server.scc
    if not pending:
        # Nothing to fetch: the step fails at its own on_fail level.  The
        # tolerant "consume a pending command if there is one" form is an
        # expectation with on_fail "warning" (or the proactive-drain action,
        # which tolerates an empty card by design).  The UICC announces
        # pending commands in the response to a command (TS 102 221 7.4.2.1).
        want = step['command'].get('name')
        if not want and step['command'].get('type') is not None:
            want = '0x%02X' % step['command']['type']
        return {'status': step['on_fail'], 'checks': [_test_check_result(
                    'Command', False, want or 'a pending command', '(none)',
                    step['on_fail'],
                    detail='no proactive command pending - the previous step '
                           'did not end with SW 91XX (the card announces '
                           'pending commands in the response to a command, '
                           'TS 102 221 7.4.2.1); a status action with '
                           'attempts > 1 polls until one appears')]}, None
    fetch_apdu = '%s120000%02x' % (scc.cat_cla, pending)
    fdata, fetch_sw = scc._tp.send_apdu(fetch_apdu)
    if not fdata:
        _test_log('FETCH=%s -> SW=%s (no data)' % (fetch_apdu, fetch_sw))
        return {'status': step['on_fail'], 'checks': [_test_check_result(
                    'Fetch', False, 'the announced command',
                    'no data (SW %s)' % fetch_sw, step['on_fail'],
                    detail='the card announced a command (SW 91XX) but FETCH '
                           'returned no data')]}, None
    raw = bytes.fromhex(fdata)
    _test_log('FETCH=%s -> SW=%s %s' % (fetch_apdu, fetch_sw, raw.hex().upper()))
    cmd_num, cmd_type, dev_src, dev_dst, cmd_qual = _parse_proactive_header(raw)
    log_entry = _log_proactive(cmd_type, raw, cmd_qual, cmd_num)
    type_name = PROACTIVE_TYPE_NAMES.get(cmd_type, 'UNKNOWN')
    _test_log('CMD 0x%02X %s qual=%s' % (
        cmd_type, type_name, ('%02X' % cmd_qual) if cmd_qual is not None else '-'))
    sms_fields = _sms_tpdu_fields(raw) if cmd_type == 0x13 else None
    if sms_fields and sms_fields.get('mti') == 1:
        _test_log('SMS DA=%s PID=%s DCS=%s UDL=%s UD=%s'
                  % (sms_fields.get('da'), sms_fields.get('pid'),
                     sms_fields.get('dcs'), sms_fields.get('udl'),
                     sms_fields.get('ud')))
    # A SEND SHORT MESSAGE carries the PoR when the packet asked for it in
    # submit mode (SPI2 bit 0x20): decode it with the triggering scp80 step's
    # context so the checks can assert the PoR contents.
    por = _test_expect_por(ctx, raw) if cmd_type == 0x13 else None
    if cmd_type == 0x13:
        if por is not None:
            dec = por.get('decoded') or {}
            _test_log('PoR[sms-submit] status=%s TAR=%s CNTR=%s raw=%s'
                      % (por.get('response_status'), por.get('tar'),
                         por.get('cntr'), por.get('raw') or '(none)'))
            if por.get('response_type') == 'raw':
                _test_log('APP DATA=%s (no R-APDU SW)'
                          % (dec.get('last_response_data') or '(none)'))
            else:
                _test_log('R-APDU SW=%s data=%s'
                          % (dec.get('last_status_word') or '(none)',
                             dec.get('last_response_data') or '(none)'))
        else:
            _test_log('PoR[sms-submit] none (no decodable PDU or no scp80 '
                      'context)')
    if cmd_type == 0x25:
        # The card may re-send its menu (e.g. after an applet install); keep
        # the cache that text-based menu selection resolves against current.
        menu = _parse_setup_menu_command(raw)
        if menu:
            server.sim_menu = menu
    checks = []
    want_type = step['command'].get('type')
    type_ok = want_type is None or cmd_type == want_type
    if type_ok:
        checks.append(_test_check_result('Command', True, step['command'].get('name') or type_name,
                                         '%s (0x%02X)' % (type_name, cmd_type), 'ok'))
    else:
        checks.append(_test_check_result(
            'Command', False, step['command'].get('name') or '0x%02X' % want_type,
            '%s (0x%02X)' % (type_name, cmd_type), 'error'))
    if step.get('qualifier'):
        actual_q = '%02X' % cmd_qual if cmd_qual is not None else None
        checks.append(_test_check_result('Qualifier',
                                         testscript.match_value(step['qualifier'], actual_q),
                                         step['qualifier']['value'], actual_q or '(none)',
                                         step['on_fail']))
    for c in step['checks']:
        if c['kind'] == 'text':
            text = _parse_display_text(raw)
            ok = testscript.match_text(c, text)
            checks.append(_test_check_result('Text', ok, c['value'],
                                             text if text is not None else '(none)',
                                             c['on_fail']))
        elif c['kind'] == 'item':
            items = None
            if cmd_type == 0x24:
                items = _parse_select_item(raw)
            elif cmd_type == 0x25:
                items = _parse_setup_menu_items(raw)
            if items is None:
                checks.append(_test_check_result('Item', False,
                                                 'id=%s text=%r' % (c.get('id'), c.get('text')),
                                                 'command carries no items', c['on_fail']))
            else:
                ok, detail = testscript.match_item(items, c)
                checks.append(_test_check_result('Item', ok,
                                                 'id=%s text=%r' % (c.get('id'), c.get('text')),
                                                 detail, c['on_fail']))
        elif c['kind'] == 'raw':
            actual = raw.hex().upper()
            checks.append(_test_check_result('Raw',
                                             testscript.match_value({'mode': c['mode'], 'value': c['value']}, actual),
                                             c['value'], actual, c['on_fail']))
        elif c['kind'] == 'por':
            checks.extend(_por_check_results(c, por, c['on_fail']))
        elif c['kind'] == 'alpha':
            alpha = _cmd_tlv(httpota.proactive_tlvs(raw), 0x05)
            text = None
            if alpha:
                try:
                    text = _STK_DECODE._decode(alpha, {}, 'stk')
                except Exception:
                    text = None
            checks.append(_test_check_result(
                'Alpha', testscript.match_text(c, text),
                c['value'], text if text is not None else '(none)', c['on_fail']))
        elif c['kind'] == 'sms':
            if sms_fields is None or sms_fields.get('mti') != 1:
                checks.append(_test_check_result(
                    'SMS', False, 'a SUBMIT TPDU', 'none (not an SMS-SUBMIT)',
                    c['on_fail']))
            else:
                if 'da' in c:
                    checks.append(_test_check_result(
                        'SMS DA', sms_fields.get('da') == c['da'], c['da'],
                        sms_fields.get('da') or '(none)', c['on_fail']))
                for label, key in (('PID', 'pid'), ('DCS', 'dcs')):
                    if key in c:
                        actual = sms_fields.get(key) or ''
                        checks.append(_test_check_result(
                            'SMS ' + label, testscript.match_value(c[key], actual),
                            c[key]['value'], actual or '(none)', c['on_fail']))
                if 'udl' in c:
                    actual = sms_fields.get('udl')
                    checks.append(_test_check_result(
                        'SMS UDL', actual == c['udl'], str(c['udl']),
                        '(none)' if actual is None else str(actual), c['on_fail']))
                if 'ud' in c:
                    actual = sms_fields.get('ud') or ''
                    checks.append(_test_check_result(
                        'SMS UD', testscript.match_value(c['ud'], actual),
                        c['ud']['value'], actual or '(none)', c['on_fail']))
        elif c['kind'] == 'files':
            paths = _parse_file_list(raw)
            want = c['files']
            checks.append(_test_check_result(
                'Files', sorted(paths) == sorted(want), ', '.join(want),
                ', '.join(paths) or '(none)', c['on_fail']))
    tr = _test_terminal_response(cmd_num, cmd_type, dev_dst, dev_src, cmd_qual,
                                 step['respond'])
    tr_rv = scc._tp.send_apdu('%s140000%02x%s' % (scc.cat_cla, len(tr), tr.hex()))
    tr_sw = tr_rv[1]
    _test_log('TR=%s -> SW=%s' % (tr.hex().upper(), tr_sw))
    _record_tr(log_entry, tr, tr_sw)
    status = testscript.combine_levels([c['level'] if not c['ok'] else 'ok' for c in checks])
    result = {'status': status, 'sw': tr_sw, 'data': raw.hex().upper(),
              'sent': 'TR ' + tr.hex().upper(), 'checks': checks,
              'por': por, 'sms': sms_fields,
              'command': {'type_hex': '%02X' % cmd_type, 'type_name': type_name,
                          'qualifier': '%02X' % cmd_qual if cmd_qual is not None else None,
                          'raw': raw.hex().upper()}}
    pending_next = int(tr_sw[2:], 16) if tr_sw.startswith('91') else None
    return result, pending_next


def _test_drain_pending(scc, fetch_len, result=0x00, max_commands=3):
    """Fetch the commands the script did not expect and answer them, so the
    card is not left re-announcing one (TS 102 223 6.3).

    The answer defaults to 'ok' (0x00, command performed successfully): a
    cancel (0x10) tells the card the user terminated the proactive session,
    which makes assertive applications take an error path - a REFRESH
    answered cancel can queue a further command.  Answering one command can
    make the application emit another, so the drain keeps fetching while the
    TERMINAL RESPONSE answers 91XX (bounded by `max_commands`).  A script that
    wants to test the refusal path answers explicitly (an expectation's
    `respond`, or the `proactive-drain` action's `first`)."""
    answered = []
    try:
        while len(answered) < max_commands:
            # fetch_len 0 (a 9100 SW) means "length unknown": fetch the full
            # 255 bytes - `%02x` of 0x100 would emit an odd-length APDU
            fdata, sw = scc._tp.send_apdu('%s120000%02x' % (scc.cat_cla,
                                                            fetch_len or 0xff))
            if not fdata:
                break
            raw = bytes.fromhex(fdata)
            cmd_num, cmd_type, dev_src, dev_dst, cmd_qual = _parse_proactive_header(raw)
            tr = _test_terminal_response(cmd_num, cmd_type, dev_dst, dev_src,
                                         cmd_qual, {'result': result})
            rv = scc._tp.send_apdu('%s140000%02x%s' % (scc.cat_cla, len(tr), tr.hex()))
            name = PROACTIVE_TYPE_NAMES.get(cmd_type, '?')
            answered.append({'name': name, 'type': cmd_type,
                             'raw': raw.hex().upper(), 'tr': tr.hex().upper(),
                             'sw': rv[1]})
            sys.stderr.write('TEST-RUN drain: %s (0x%02X) TR=%s -> SW=%s\n'
                             % (name, cmd_type, tr.hex().upper(), rv[1]))
            if not (rv[1] or '').startswith('91'):
                break
            fetch_len = int(rv[1][2:], 16)
    except Exception as e:
        return {'note': 'drain failed: %s' % e}
    if not answered:
        return {'note': 'nothing to drain'}
    result_name = {0x00: 'ok', 0x10: 'cancel', 0x11: 'back',
                   0x12: 'timeout', 0x22: 'no response'}.get(result,
                                                             '0x%02X' % result)
    return {'sent': '; '.join('TR %s %s' % (result_name, a['tr']) for a in answered),
            'sw': answered[-1]['sw'], 'data': answered[0]['raw'],
            'note': '; '.join('pending %s (0x%02X) answered with %s'
                              % (a['name'], a['type'], result_name)
                              for a in answered)}


def _test_run_script(server, script, preset, shared=None):
    """One script's steps - the single-script run or one suite member.  The
    live steps land in `_TEST_RUN['steps']` (the snapshot the PWA renders),
    the semantic lines in `_TEST_RUN['log']` (the report's excerpt).  Returns
    ``(ctx, stopped)``: the per-script context (preset, per-keyset counters)
    and whether the stop request cut the script short.

    `shared` carries the counter state across a suite's members (the SCP80
    counter is per keyset and per card session: a second member must start
    from what the first consumed - the server persisted it, but the in-memory
    preset copy is the suite-start snapshot).  The dict is created once per
    suite and reused; a single script passes None."""
    shared = shared if shared is not None else {}
    scc = server.scc
    ctx = {'preset': dict(preset or {}),
           'counter': (shared.get('counter')
                       or str((preset or {}).get('counter') or (preset or {}).get('cntr') or '').upper()),
           'counters': shared.setdefault('counters', {}),
           'uses_scp80': any(s['type'] == 'action' and s['kind'] == 'scp80'
                             for s in script['steps'])}
    if ctx['uses_scp80']:
        with _TEST_LOCK:
            _TEST_RUN['scp80_counter'] = ctx['counter']
    pending = None
    stopped = False
    session = getattr(server, 'card_session', 0)
    for index, step in enumerate(script['steps']):
        with _TEST_LOCK:
            if _TEST_RUN['stop']:
                stopped = True
            _TEST_RUN['index'] = index
        if stopped:
            break
        entry = _test_entry(step, index)
        with _TEST_LOCK:
            _TEST_RUN['steps'].append(entry)
        if pending is not None and step['type'] != 'expect':
            _test_entry_update(entry, {
                'status': 'error',
                'note': 'unexpected proactive command pending (SW 91XX) - '
                        'add an expect step or a status action'})
            with _CARD_LOCK:
                drained = _test_drain_pending(scc, pending)
            fields = {k: v for k, v in drained.items() if k != 'note'}
            fields['ms'] = int((time.time() - entry['started']) * 1000)
            _test_entry_update(entry, fields)
            pending = None
            break
        if getattr(server, 'card_session', 0) != session:
            _test_entry_update(entry, {
                'status': 'error',
                'note': 'card session changed (card removed or re-equipped)',
                'ms': int((time.time() - entry['started']) * 1000)})
            break
        try:
            with _CARD_LOCK:
                if step['type'] == 'action':
                    result, pending = _test_run_action(server, step, ctx)
                else:
                    result, pending = _test_run_expect(server, step, pending, ctx)
            _test_finish_entry(entry, result)
            if entry['status'] == 'error':
                # Error terminates the script; Warning and OK continue.
                break
        except Exception as e:
            _test_entry_update(entry, {
                'status': 'error', 'note': str(e),
                'ms': int((time.time() - entry['started']) * 1000)})
            sys.stderr.write('TEST-RUN step %d failed: %s\n' % (index + 1, e))
            if pending is not None:
                with _CARD_LOCK:
                    drained = _test_drain_pending(scc, pending)
                _test_entry_update(entry, {k: v for k, v in drained.items() if k != 'note'})
                pending = None
            break
    if pending is not None:
        with _CARD_LOCK:
            drained = _test_drain_pending(scc, pending)
        with _TEST_LOCK:
            _TEST_RUN['steps'].append(dict(
                {'index': len(script['steps']), 'type': 'cleanup', 'kind': 'cleanup',
                 'label': _TEST_KIND_LABELS['cleanup'], 'status': 'error' if not stopped else 'warning',
                 'checks': [], 'started': time.time(), 'ms': 0, 'note': None,
                 'sent': None, 'sw': None, 'data': None}, **drained))
    if ctx['uses_scp80']:
        with _TEST_LOCK:
            _TEST_RUN['scp80_counter'] = ctx['counter']
            _TEST_RUN['scp80_counters'] = dict(ctx.get('counters') or {})
    # The no-keyset counter is a plain value in the ctx: carry it into the
    # shared state so the next suite member starts where this one ended.
    shared['counter'] = ctx.get('counter') or ''
    return ctx, stopped


def _test_run_execute(server, script, preset):
    """The single-script worker body: run the steps, finalise the run state."""
    global _TEST_RUNNING
    _ctx, stopped = _test_run_script(server, script, preset)
    with _TEST_LOCK:
        _TEST_RUN['running'] = False
        _TEST_RUN['finished'] = time.time()
        if _TEST_RUN['status'] is None:
            levels = [s.get('status') for s in _TEST_RUN['steps']]
            if stopped:
                _TEST_RUN['status'] = 'stopped'
            elif 'error' in levels:
                _TEST_RUN['status'] = 'error'
            elif 'warning' in levels:
                _TEST_RUN['status'] = 'warning'
            else:
                _TEST_RUN['status'] = 'ok'
    _TEST_RUNNING = False
    if _POLL_ENABLED and not _POLL_DISABLED_BY_CARD:
        _reset_poll_timer()


def _test_run_worker(server, script, preset):
    global _TEST_RUNNING
    try:
        _test_run_execute(server, script, preset)
    except Exception as e:
        traceback.print_exc()
        _TEST_RUNNING = False
        with _TEST_LOCK:
            _TEST_RUN['error'] = str(e)
            _TEST_RUN['running'] = False
            _TEST_RUN['finished'] = time.time()
            if not _TEST_RUN['status']:
                _TEST_RUN['status'] = 'error'
        if _POLL_ENABLED and not _POLL_DISABLED_BY_CARD:
            _reset_poll_timer()


# ---- test suites (v3.22.0) -------------------------------------------------

def _test_member_prefix(index, member):
    """The run-log prefix of a suite member: its position, role and name, so
    the shared stderr stream (and the report's excerpt) stays readable."""
    total = len((_TEST_RUN.get('suite') or {}).get('members') or []) or 1
    tag = member.get('name') or member.get('script_id') or 'script'
    role = member.get('role') or 'member'
    return '[%d/%d %s %s] ' % (index + 1, total, role, tag)


def _test_member_status(steps, stopped):
    """A suite member's verdict from its step levels (an error stops that
    script); a stop request marks the member stopped."""
    if stopped:
        return 'stopped'
    levels = [s.get('status') for s in steps]
    if 'error' in levels:
        return 'error'
    if 'warning' in levels:
        return 'warning'
    return 'ok'


def _preset_counters_snapshot(preset):
    """``{kvn: counter}`` of a preset's keysets - the suite report's SCP80
    counter before/after columns (the store persists what the card consumed)."""
    out = {}
    for ks in _preset_keysets(preset or {}):
        kvn = presets.keyset_kvn(ks)
        if kvn:
            out[str(kvn)] = str(ks.get('cntr') or '').upper()
    return out


def _adm_verified_session(server):
    """True when the ADM was verified for the current card session: the ADM
    badge's verify or a gated run set the latch; an equip/reset/eSIM switch
    bumps the session and invalidates it."""
    return getattr(server, 'adm_session', None) == getattr(server, 'card_session', 0)


def _ensure_adm(server, preset, needed):
    """The run's ADM prerequisite.  With `needed` the ADM must be verified for
    the current card session: the verified latch passes, otherwise the
    matched preset's ADM key is tried **once** - a failure is never retried
    automatically (the session is stamped and the operator verifies manually;
    the ADM badge's verify clears the stamp).  Returns ``(ok, info, error)``."""
    session = getattr(server, 'card_session', 0)
    info = {'required': bool(needed), 'verified': False}
    if not needed:
        return True, info, None
    if _adm_verified_session(server):
        info['verified'] = True
        return True, info, None
    if getattr(server, 'adm_failed_session', None) == session:
        info['failed'] = True
        return False, info, ('ADM is required and its verification failed '
                             'earlier in this card session - verify it '
                             'manually (the ADM badge) and retry')
    key = re.sub(r'\s', '', str((preset or {}).get('adm') or '')).upper()
    if not key:
        return False, info, ('ADM is required but the matching card preset '
                             'has no ADM key')
    if server.scc is None or server.app is None:
        return False, info, 'ADM is required but no card is equipped'
    try:
        with _CARD_LOCK:
            result = _verify_adm(server.scc, server.app, key)
    except Exception as e:
        return False, info, 'ADM verification failed: %s' % e
    if result.get('ok'):
        server.adm_session = session
        info['verified'] = True
        info['checked'] = True
        sys.stderr.write('TEST-RUN: ADM verified for this card session\n')
        return True, info, None
    server.adm_failed_session = session
    info['result'] = result
    if result.get('attempts_left') is not None:
        info['attempts_left'] = result['attempts_left']
    if result.get('blocked'):
        info['blocked'] = True
    extra = ''
    if result.get('attempts_left') is not None:
        extra = ' (%d attempt(s) left)' % result['attempts_left']
    elif result.get('blocked'):
        extra = ' (the ADM is blocked - the unblock key is needed)'
    return False, info, ('ADM verification failed (SW %s)%s - verify it '
                         'manually and retry' % (result.get('sw'), extra))


def _test_run_spawn(server, target, args, thread_name):
    """The shared run start: the run owns the card from here - suspend the
    background poll, answer a paused interactive command, spawn the worker."""
    global _TEST_THREAD, _TEST_RUNNING, _POLL_TIMER
    _TEST_RUNNING = True
    if _POLL_TIMER is not None:
        _POLL_TIMER.cancel()
        _POLL_TIMER = None
    with _CARD_LOCK:
        try:
            _finish_pending_menu(server, server.scc)
        except Exception as e:
            sys.stderr.write('TEST-RUN: finishing pending menu failed: %s\n' % e)
    try:
        _TEST_THREAD = threading.Thread(target=target, args=args,
                                        name=thread_name, daemon=True)
        _TEST_THREAD.start()
    except Exception:
        # A run that never starts must not leave the card blocked (the 409
        # guard keys off _TEST_RUNNING, and no worker would ever clear it).
        _TEST_THREAD = None
        _TEST_RUNNING = False
        with _TEST_LOCK:
            _TEST_RUN['running'] = False
            _TEST_RUN['status'] = 'error'
            _TEST_RUN['error'] = 'could not start the run worker'
            _TEST_RUN['finished'] = time.time()
        raise


def _test_run_start(server, script, preset, script_id=None, adm_info=None):
    """Start a single-script run."""
    with _TEST_LOCK:
        _TEST_RUN.update({
            'running': True, 'stop': False, 'name': script['name'], 'status': None,
            'session': getattr(server, 'card_session', 0), 'index': 0,
            'total': len(script['steps']), 'steps': [], 'started': time.time(),
            'finished': None, 'error': None, 'scp80_counter': None,
            'scp80_counters': {}, 'script_id': script_id,
            'preset': (preset or {}).get('name') or (preset or {}).get('iccid'),
            'suite': None, 'log': [], 'log_prefix': '',
            'adm': adm_info or {'required': False},
        })
    _test_run_spawn(server, _test_run_worker, (server, script, preset), 'test-script')


def _test_suite_execute(server, suite, members, preset, preset_id):
    """The suite worker body: setup -> members in order (per-member on_fail)
    -> teardown, one card session, no resume.  The teardown runs on every
    stop - its purpose is to leave the card ready for a new test - as long as
    the card is in the reader; a card reset stops the suite and the remaining
    members are skipped (the report flags it)."""
    global _TEST_RUNNING
    session_start = getattr(server, 'card_session', 0)
    state = _TEST_RUN['suite']
    stopped = False
    session_changed = False
    setup_i = next((i for i, (e, _s) in enumerate(members)
                    if e['role'] == 'setup'), None)
    teardown_i = next((i for i, (e, _s) in enumerate(members)
                       if e['role'] == 'teardown'), None)
    member_idx = [i for i, (e, _s) in enumerate(members) if e['role'] == 'member']
    # The counter state is per card session: one shared dict for the whole
    # suite, so a second SCP80 script starts from what the first consumed
    # (the store persists each packet's counter; the preset dict here is the
    # suite-start snapshot).
    shared = {}

    def run_member(i):
        entry, script = members[i]
        member = state['members'][i]
        with _TEST_LOCK:
            member['status'] = 'running'
            member['started'] = time.time()
            _TEST_RUN['steps'] = []
            _TEST_RUN['index'] = 0
            _TEST_RUN['total'] = len(script['steps'])
            _TEST_RUN['log_prefix'] = _test_member_prefix(i, member)
            log_from = len(_TEST_RUN['log'])
        ctx, member_stopped = _test_run_script(server, script, preset, shared=shared)
        with _TEST_LOCK:
            steps = json.loads(json.dumps(_TEST_RUN['steps']))
            log = list(_TEST_RUN['log'][log_from:])
            member.update({
                'status': _test_member_status(steps, member_stopped),
                'steps': steps,
                'log': log[-_TEST_MEMBER_LOG_MAX:],
                'log_truncated': len(log) > _TEST_MEMBER_LOG_MAX,
                'finished': time.time(),
                'counters_after': dict(ctx.get('counters') or {}),
            })
            status = member['status']
        return status, member_stopped

    def skip_pending(reason):
        with _TEST_LOCK:
            for member in state['members']:
                if member['status'] == 'pending':
                    member['status'] = 'skipped'
                    member['note'] = reason

    abort = False
    if setup_i is not None:
        status, _s = run_member(setup_i)
        if status not in ('ok', 'warning'):
            abort = True
            skip_pending('the setup script failed')
    for i in member_idx:
        if abort:
            break
        with _TEST_LOCK:
            stop_req = bool(_TEST_RUN['stop'])
        if stop_req:
            stopped = True
            skip_pending('stopped')
            break
        if getattr(server, 'card_session', 0) != session_start:
            session_changed = True
            skip_pending('card session changed (card removed or re-equipped)')
            break
        status, _s = run_member(i)
        if status not in ('ok', 'warning') and members[i][0]['on_fail'] == 'stop':
            abort = True
    if abort:
        skip_pending('a previous script failed (on_fail: stop)')
    else:
        skip_pending('stopped')
    if teardown_i is not None:
        if not getattr(server, 'card_present', False):
            with _TEST_LOCK:
                state['members'][teardown_i]['status'] = 'skipped'
                state['members'][teardown_i]['note'] = 'no card in the reader'
        else:
            # The teardown runs on every stop - its purpose is to leave the
            # card ready for a new test - so a pending stop request does not
            # cut it short (the run is finishing anyway).
            with _TEST_LOCK:
                stop_saved = _TEST_RUN['stop']
                _TEST_RUN['stop'] = False
            try:
                run_member(teardown_i)
            finally:
                with _TEST_LOCK:
                    _TEST_RUN['stop'] = stop_saved
    with _TEST_LOCK:
        session_end = getattr(server, 'card_session', 0)
        state['session_end'] = session_end
        state['session_changed'] = bool(session_changed or session_end != session_start)
        after = None
        store = getattr(server, 'card_presets', None)
        if preset_id and store is not None:
            after = _preset_counters_snapshot(store.get(preset_id))
        state['counters_after'] = after
        statuses = [m['status'] for m in state['members']]
        state['summary'] = {
            'ok': statuses.count('ok'),
            'warning': statuses.count('warning'),
            'error': statuses.count('error'),
            'skipped': statuses.count('skipped'),
            'stopped': bool(stopped),
            'session_changed': state['session_changed'],
            'wall_ms': int((time.time() - (_TEST_RUN['started'] or time.time())) * 1000),
            'counters_before': state.get('counters_before'),
            'counters_after': after,
            'setup': state['members'][setup_i]['status'] if setup_i is not None else None,
            'teardown': state['members'][teardown_i]['status'] if teardown_i is not None else None,
        }
        if 'error' in statuses:
            _TEST_RUN['status'] = 'error'
        elif 'warning' in statuses:
            _TEST_RUN['status'] = 'warning'
        elif stopped:
            _TEST_RUN['status'] = 'stopped'
        else:
            _TEST_RUN['status'] = 'ok'
        _TEST_RUN['running'] = False
        _TEST_RUN['finished'] = time.time()
        _TEST_RUN['log_prefix'] = ''      # the report is done: no stale prefix
    _TEST_RUNNING = False
    if _POLL_ENABLED and not _POLL_DISABLED_BY_CARD:
        _reset_poll_timer()


def _test_suite_worker(server, suite, members, preset, preset_id):
    global _TEST_RUNNING
    try:
        _test_suite_execute(server, suite, members, preset, preset_id)
    except Exception as e:
        traceback.print_exc()
        with _TEST_LOCK:
            _TEST_RUN['error'] = str(e)
            _TEST_RUN['running'] = False
            _TEST_RUN['finished'] = time.time()
            if not _TEST_RUN['status']:
                _TEST_RUN['status'] = 'error'
            state = _TEST_RUN.get('suite')
            if state is not None and state.get('summary') is None:
                # keep the report renderable: close the open members
                statuses = []
                for m in state['members']:
                    if m['status'] == 'running':
                        m['status'] = 'error'
                        m['note'] = m.get('note') or ('the run failed: %s' % e)
                    elif m['status'] == 'pending':
                        m['status'] = 'skipped'
                    statuses.append(m['status'])
                state['session_end'] = getattr(server, 'card_session', 0)
                state['summary'] = {
                    'ok': statuses.count('ok'), 'warning': statuses.count('warning'),
                    'error': statuses.count('error'), 'skipped': statuses.count('skipped'),
                    'stopped': False,
                    'session_changed': bool(state.get('session_changed')),
                    'wall_ms': int((time.time() - (_TEST_RUN['started'] or time.time())) * 1000),
                    'counters_before': state.get('counters_before'),
                    'counters_after': None,
                    'setup': None, 'teardown': None,
                }
    finally:
        _TEST_RUNNING = False
        if _POLL_ENABLED and not _POLL_DISABLED_BY_CARD:
            _reset_poll_timer()


def _test_suite_state(suite, members, preset, preset_id, session):
    """The `_TEST_RUN['suite']` report skeleton of a suite run: the member
    records (status pending) and the run-level metadata (preset, session,
    SCP80 counters before).  Shared by the starter and the tests."""
    return {
        'id': suite.get('id'),
        'name': suite.get('name') or 'test suite',
        'require_adm': bool(suite.get('require_adm')),
        'session_start': session, 'session_end': None,
        'session_changed': False, 'preset_id': preset_id,
        'preset': (preset or {}).get('name') or (preset or {}).get('iccid'),
        'counters_before': _preset_counters_snapshot(preset),
        'counters_after': None, 'summary': None,
        'members': [
            {'script_id': e['script_id'], 'name': (s or {}).get('name'),
             'role': e['role'], 'on_fail': e['on_fail'],
             'status': 'pending', 'steps': [], 'log': [],
             'log_truncated': False, 'note': None,
             'started': None, 'finished': None, 'counters_after': {}}
            for e, s in members],
    }


def _test_suite_start(server, suite, members, preset, preset_id, adm_info=None):
    """Start a suite run: `members` is the resolved, normalised
    ``[(entry, script)]`` list in run order."""
    with _TEST_LOCK:
        session = getattr(server, 'card_session', 0)
        _TEST_RUN.update({
            'running': True, 'stop': False,
            'name': suite.get('name') or 'test suite', 'status': None,
            'session': session, 'index': 0, 'total': 0, 'steps': [],
            'started': time.time(), 'finished': None, 'error': None,
            'scp80_counter': None, 'scp80_counters': {},
            'script_id': None,
            'preset': (preset or {}).get('name') or (preset or {}).get('iccid'),
            'suite': _test_suite_state(suite, members, preset, preset_id, session),
            'log': [], 'log_prefix': '', 'adm': adm_info or {'required': False},
        })
    _test_run_spawn(server, _test_suite_worker,
                    (server, suite, members, preset, preset_id), 'test-suite')


class PysimHandler(BaseHTTPRequestHandler):
    def _send_json(self, data, status=200):
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        try:
            self.wfile.write(json.dumps(data, ensure_ascii=False).encode())
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # The browser navigated away or reloaded while the response was in
            # flight (a cancelled poll): the request is done and the client is
            # gone - one line, no traceback (v3.6.35).
            sys.stderr.write('CLIENT GONE: %s %s\n' % (self.command, self.path))

    def _read_body(self):
        length = int(self.headers.get('Content-Length', 0))
        return json.loads(self.rfile.read(length))

    def _preset_store_or_503(self):
        """The card preset store, or None after answering 503 (a server built
        without one - e.g. a test harness - must not crash the endpoints)."""
        store = getattr(self.server, 'card_presets', None)
        if store is None:
            resp = {'error': 'preset store not initialized'}
            self._send_json(resp, 503)
            self._log_resp(resp)
            return None
        return store

    def _preset_store_error(self, e):
        """A store I/O failure (read-only home, disk full, ...) answers a JSON
        500 instead of escaping the handler as a dropped connection."""
        resp = {'error': 'preset store write failed: %s' % e}
        self._send_json(resp, 500)
        self._log_resp(resp)

    def _test_scripts_store_or_503(self):
        """The test script store, or None after answering 503 (a server built
        without one - e.g. a test harness - must not crash the endpoints)."""
        store = getattr(self.server, 'test_scripts', None)
        if store is None:
            resp = {'error': 'test script store not initialized'}
            self._send_json(resp, 503)
            self._log_resp(resp)
            return None
        return store

    def _test_scripts_store_error(self, e):
        """A store I/O failure (read-only home, disk full, ...) answers a JSON
        500 instead of escaping the handler as a dropped connection."""
        resp = {'error': 'test script store write failed: %s' % e}
        self._send_json(resp, 500)
        self._log_resp(resp)

    def _test_suites_store_or_503(self):
        """The test suite store, or None after answering 503."""
        store = getattr(self.server, 'test_suites', None)
        if store is None:
            resp = {'error': 'test suite store not initialized'}
            self._send_json(resp, 503)
            self._log_resp(resp)
            return None
        return store

    def _test_suites_store_error(self, e):
        resp = {'error': 'test suite store write failed: %s' % e}
        self._send_json(resp, 500)
        self._log_resp(resp)

    def _test_suite_run_request(self, suite_id, body):
        """Start a suite run: resolve the members and the preset, run the ADM
        prerequisite, then hand over to the suite runner (one card session,
        no resume)."""
        suites = self._test_suites_store_or_503()
        if suites is None:
            return
        scripts_store = self._test_scripts_store_or_503()
        if scripts_store is None:
            return
        suite = suites.get(suite_id)
        if suite is None:
            resp = {'error': 'unknown test suite id'}
            self._send_json(resp, 404)
            self._log_resp(resp)
            return
        if not suite['scripts']:
            resp = {'error': 'the suite has no scripts'}
            self._send_json(resp, 400)
            self._log_resp(resp)
            return
        members = []
        missing = []
        for entry in suite['scripts']:
            stored = scripts_store.get(entry['script_id'])
            if stored is None:
                missing.append(entry['script_id'])
                continue
            try:
                # the store validated at save; a hand edit (or a tightened
                # rule) is reported here, before anything is sent
                script = testscript.normalise_script(stored, _test_command_type)
            except testscript.ScriptError as e:
                resp = {'error': 'script %r does not validate: %s'
                                 % (stored.get('name') or stored['id'], e)}
                self._send_json(resp, 400)
                self._log_resp(resp)
                return
            script['require_adm'] = bool(stored.get('require_adm'))
            members.append((entry, script))
        if missing:
            resp = {'error': 'the suite references unknown test script(s): %s'
                             % ', '.join(missing[:3])}
            self._send_json(resp, 400)
            self._log_resp(resp)
            return
        preset, preset_err = _test_run_preset_from_body(self.server, body)
        if preset_err:
            resp = {'error': preset_err}
            self._send_json(resp, 400)
            self._log_resp(resp)
            return
        steps = [step for _entry, script in members for step in script['steps']]
        if any(s['type'] == 'action' and s['kind'] == 'scp80' for s in steps):
            err = _test_preset_error({'steps': steps}, preset)
            if err:
                resp = {'error': err}
                self._send_json(resp, 400)
                self._log_resp(resp)
                return
        needed_adm = bool(suite.get('require_adm')) or any(
            bool(script.get('require_adm')) for _entry, script in members)
        ok, adm_info, adm_err = _ensure_adm(self.server, preset, needed_adm)
        if not ok:
            resp = {'error': adm_err, 'adm': adm_info}
            self._send_json(resp, 400)
            self._log_resp(resp)
            return
        try:
            _test_suite_start(self.server, suite, members, preset,
                              str(body.get('preset_id') or '') or None,
                              adm_info=adm_info)
        except Exception as e:
            resp = {'error': 'could not start the suite run: %s' % e}
            self._send_json(resp, 500)
            self._log_resp(resp)
            return
        resp = _test_state_snapshot()
        self._send_json(resp)
        self._log_resp({'suite': suite.get('name'), 'members': len(members)})

    def _keyset_guard(self, body, kic, kid):
        """The keyset-number checks shared by the RAM operations: the KIc/KID
        numbers must agree (TS 102 225 A.2 - the card rejects a mismatch) and a
        named preset must define the number ("the keyset must exist").
        Returns ``(kvn, error)``: kvn is None when the packet carries no number
        ('00'/'00' = no security, legal per A.2), error an error string
        the caller answers with 400."""
        kvn, err = _kvn_of(kic, kid)
        if err:
            return None, err
        err = _preset_keyset_check(self.server, body.get('preset_id'), kic, kid)
        return (kvn or None), err

    def _log_req(self, body=None):
        if self.server.log_requests:
            if body is not None:
                sys.stderr.write("REQUEST %s %s: %s\n" % (self.command, self.path, json.dumps(body)))
            else:
                sys.stderr.write("REQUEST %s %s\n" % (self.command, self.path))

    def _log_resp(self, data):
        if self.server.log_requests:
            sys.stderr.write("RESPONSE %s: %s\n" % (self.path, json.dumps(data, ensure_ascii=False)))

    def do_OPTIONS(self):
        self._log_req()
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        # Chrome/Edge Private Network Access: a page served from a public (or
        # otherwise less-local) origin needs explicit permission to reach this
        # loopback server. Without this, the preflight fails and the browser
        # blocks the follow-up GET/POST ("Permission was denied ... loopback").
        self.send_header('Access-Control-Allow-Private-Network', 'true')
        self.end_headers()

    def _serve_static(self):
        web_dir = getattr(self.server, 'web_dir', None)
        if not web_dir:
            self.send_error(404)
            return
        rel = self.path.split('?', 1)[0].lstrip('/')
        if rel in ('', '/'):
            rel = 'index.html'
        if '..' in rel.split('/') or rel.startswith('/'):
            self.send_error(404)
            return
        fs_path = os.path.join(web_dir, rel)
        if not os.path.isfile(fs_path):
            self.send_error(404)
            return
        with open(fs_path, 'rb') as f:
            data = f.read()
        content_type = _STATIC_MIME.get(os.path.splitext(rel)[1].lower(), 'application/octet-stream')
        try:
            mtime = int(os.path.getmtime(fs_path))
        except OSError:
            mtime = 0
        last_modified = (time.strftime('%a, %d %b %Y %H:%M:%S GMT', time.gmtime(mtime))
                         if mtime else '')
        ims = self.headers.get('If-Modified-Since') if self.headers else None
        if last_modified and ims == last_modified:
            self.send_response(304)
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('Last-Modified', last_modified)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        # Every shell file revalidates - index.html/sw.js always did, but a
        # stale style.css silently drops newly added styles (v3.6.31).  The
        # revalidation is cheap thanks to Last-Modified/304.
        self.send_header('Cache-Control', 'no-cache')
        if last_modified:
            self.send_header('Last-Modified', last_modified)
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            sys.stderr.write('CLIENT GONE: GET %s\n' % rel)

    def do_GET(self):
        if _test_request_blocked(self.path):
            self._send_json({'error': 'test script running - card commands are '
                                      'blocked until it finishes'}, 409)
            return
        # Pure cached state (no card I/O) is served without the lock: the UI
        # must be able to report 'initializing' while a long equip (or a test
        # script step) holds the card lock.  The list is deliberately wide:
        # with a long RAM install the UI polls several of these every few
        # seconds, and if they queued behind the install they would eat the
        # browser's per-host connection budget - starving /api/status (the
        # modal's progress bar froze around 70% and jumped to 100% at the end,
        # live 2026-09-29).  Static PWA files are card-free too.  Card-touching
        # GETs (cardinfo, esim/*) stay locked below.
        path = self.path.split('?', 1)[0]
        if path in _CARD_FREE_GET or not path.startswith('/api/'):
            self._do_GET()
            return
        # Serialize all card access: the background STATUS poll runs in its own
        # thread and must never interleave with a FETCH/TERMINAL RESPONSE pair.
        with _CARD_LOCK:
            self._do_GET()

    def _do_GET(self):
        lang = _get_lang(self.headers)
        if self.path == '/api/version':
            self._log_req()
            self._send_json({'version': VERSION})
            self._log_resp({'version': VERSION})
        elif self.path == '/api/test/status':
            self._log_req()
            resp = _test_state_snapshot()
            self._send_json(resp)
            self._log_resp(resp)
        elif self.path.startswith('/api/mcc-mnc'):
            self._log_req()
            q = ''
            random_pick = False
            exclude = ''
            if '?' in self.path:
                for kv in self.path.split('?', 1)[1].split('&'):
                    if kv.startswith('q='):
                        q = unquote_plus(kv[2:])
                    elif kv.startswith('random='):
                        random_pick = kv.split('=', 1)[1] not in ('0', 'false', '')
                    elif kv.startswith('exclude='):
                        exclude = unquote_plus(kv[8:])
            path = getattr(self.server, 'mcc_mnc_path', None)
            data = _mcc_mnc_load(path)
            if data is None:
                resp = {'available': False}
            elif random_pick:
                resp = {'available': True, 'count': len(data),
                        'result': _mcc_mnc_random(data, exclude)}
            else:
                results = _mcc_mnc_search(data, q)
                resp = {'available': True, 'count': len(data), 'results': results}
            self._send_json(resp)
            self._log_resp({'available': resp['available'],
                            'results': len(resp.get('results', []))})
        elif self.path == '/api/net-state':
            self._log_req()
            state = getattr(self.server, 'net_state', None)
            if state is None:
                self._send_json({'available': False, 'state': None})
                self._log_resp({'available': False})
                return
            _netstate_compute(self.server)
            self._send_json({'available': True, 'state': state})
            self._log_resp({'available': True})
        elif self.path == '/api/status':
            self._log_req()
            app = self.server.app
            rs = app.rs if app else None
            lchan = rs.lchan[0] if rs else None
            cur_file = lchan.selected_file if lchan else None
            scc = app.card._scc if app and app.card else None
            card = app.card if app else None
            connected = bool(_CARD_CONNECTED and card is not None)
            if not connected:
                rs = None
                lchan = None
                cur_file = None
                scc = None
                card = None
            data = {
                'reader': str(self.server.sl) if self.server.sl else None,
                'connected': connected,
                'card_present': bool(getattr(self.server, 'card_present', False)),
                'card_session': int(getattr(self.server, 'card_session', 0)),
                'proactive_seq': _PROACTIVE_ENTRY_ID,
                'equipping': bool(getattr(self.server, 'equipping', False)),
                'ram_progress': _ram_progress_payload(),
                'auto_equip': bool(_AUTO_EQUIP),
                'card': card.name if card else None,
                'profile': str(rs.profile) if rs and rs.profile else None,
                'eid': (rs.identity.get('EID') if rs and rs.identity else None)
                       if connected else None,
                'euicc': bool(esim.is_euicc(app)) if connected else False,
                'iccid': getattr(self.server, 'iccid', None) if connected else None,
                'app_ready': app is not None,
                'adm_verified': rs.adm_verified if rs else False,
                'atr': rs.identity.get('ATR') if rs and rs.identity else None,
                'cla_byte': scc.cla_byte if scc else None,
                'sel_ctrl': scc.sel_ctrl if scc else None,
                'current_selection': {
                    'fid': cur_file.fid.upper() if cur_file and cur_file.fid else None,
                    'name': cur_file.name if cur_file else None,
                    'desc': cur_file.desc if cur_file else None,
                    'type': cur_file.__class__.__name__ if cur_file else None,
                    'path': str(lchan.get_cwd()) if lchan else None,
                    'file_type': _get_file_type(lchan, cur_file),
                    'file_size': _fcp_value(lchan, 'selected_file_size'),
                    'record_len': _fcp_value(lchan, 'selected_file_record_len'),
                    'num_of_rec': _fcp_value(lchan, 'selected_file_num_of_rec'),
                } if cur_file else None,
                'channels': [str(i) for i, ch in rs.lchan.items() if ch] if rs else [],
            }
            self._send_json(data)
            self._log_resp(data)
        elif self.path == '/api/esim/chip':
            app = self.server.app
            if not app or not self.server.scc:
                self._send_json({'error': _err('reader_not_init', lang)}, 503)
                self._log_resp({'error': _err('reader_not_init', lang)})
                return
            self._log_req()
            if not esim.is_euicc(app):
                resp = {'error': _err('not_an_euicc', lang)}
                self._send_json(resp, 400)
                self._log_resp(resp)
                return
            try:
                with _CARD_LOCK:
                    resp = esim.chip_info(app)
                self._send_json(resp)
                self._log_resp({'eid': resp.get('eid'), 'errors': resp.get('errors')})
            except Exception as e:
                resp = {'error': str(e)}
                sys.stderr.write('ESIM chip: %s\n' % e)
                self._send_json(resp, 500)
                self._log_resp(resp)
        elif self.path == '/api/esim/profiles':
            app = self.server.app
            if not app or not self.server.scc:
                self._send_json({'error': _err('reader_not_init', lang)}, 503)
                self._log_resp({'error': _err('reader_not_init', lang)})
                return
            self._log_req()
            if not esim.is_euicc(app):
                resp = {'error': _err('not_an_euicc', lang)}
                self._send_json(resp, 400)
                self._log_resp(resp)
                return
            try:
                with _CARD_LOCK:
                    resp = esim.profiles(app)
                self._send_json(resp)
                self._log_resp({'profiles': len(resp.get('profiles') or []),
                                'error': resp.get('error')})
            except Exception as e:
                resp = {'error': str(e)}
                sys.stderr.write('ESIM profiles: %s\n' % e)
                self._send_json(resp, 500)
                self._log_resp(resp)
        elif self.path == '/api/esim/notifications':
            app = self.server.app
            if not app or not self.server.scc:
                self._send_json({'error': _err('reader_not_init', lang)}, 503)
                self._log_resp({'error': _err('reader_not_init', lang)})
                return
            self._log_req()
            if not esim.is_euicc(app):
                resp = {'error': _err('not_an_euicc', lang)}
                self._send_json(resp, 400)
                self._log_resp(resp)
                return
            try:
                with _CARD_LOCK:
                    resp = esim.notifications(app)
                self._send_json(resp)
                self._log_resp({'notifications': len(resp.get('notifications') or []),
                                'error': resp.get('error')})
            except Exception as e:
                resp = {'error': str(e)}
                sys.stderr.write('ESIM notifications: %s\n' % e)
                self._send_json(resp, 500)
                self._log_resp(resp)
        elif self.path == '/api/commands':
            self._log_req()
            app = self.server.app
            if not app:
                self._send_json({'error': _err('app_not_init', lang)}, 503)
                self._log_resp({'error': _err('app_not_init', lang)})
                return
            cmds = sorted(
                attr[3:] for attr in dir(app)
                if attr.startswith('do_') and not attr.startswith('do__')
            )
            self._send_json(cmds)
            self._log_resp(cmds)
        elif self.path == '/api/cardinfo':
            self._log_req()
            app = self.server.app
            if not app:
                self._send_json({'error': _err('app_not_init', lang)}, 503)
                self._log_resp({'error': _err('app_not_init', lang)})
                return
            out = StringIO()
            old_stdout = app.stdout
            old_stderr = sys.stderr
            app.stdout = out
            sys.stderr = out
            try:
                app.onecmd_plus_hooks('cardinfo')
                output = _strip_ansi(out.getvalue())
            except Exception as e:
                output = str(e) + '\n' + traceback.format_exc()
            finally:
                app.stdout = old_stdout
                sys.stderr = old_stderr
            resp = {'output': output}
            self._send_json(resp)
            self._log_resp(resp)
        elif self.path == '/api/menu':
            menu = self.server.sim_menu or {'items': []}
            resp = {**menu, 'active': self.server.menu_active}
            self._send_json(resp)
            self._log_resp(resp)
        elif self.path == '/api/events':
            resp = self.server.event_list or []
            self._send_json(resp)
            self._log_resp(resp)
        elif self.path == '/api/terminal-profile':
            self._log_req()
            tp = getattr(self.server, 'terminal_profile', None) or ''
            resp = {'profile': tp or None, 'bytes': len(tp) // 2,
                    'cli_default': getattr(self.server, 'cli_terminal_profile', None)}
            self._send_json(resp)
            self._log_resp(resp)
        elif self.path == '/api/pli-qualifiers':
            qualifiers = [{'code': '%02X' % q, 'name': PLI_QUALIFIER_NAMES[q]} for q in PLI_QUALIFIER_NAMES]
            self._send_json(qualifiers)
            self._log_resp(qualifiers)
        elif self.path == '/api/pli-dict':
            resp = {('%02X' % q): v for q, v in _PLI_DATA.items()}
            self._send_json(resp)
            self._log_resp(resp)
        elif self.path == '/api/poll-status':
            resp = {'enabled': _POLL_ENABLED, 'interval': _POLL_INTERVAL,
                    'card_disabled': _POLL_DISABLED_BY_CARD}
            self._send_json(resp)
            self._log_resp(resp)
        elif self.path == '/api/proactive-log':
            log = list(reversed(_PROACTIVE_LOG[-50:]))
            self._send_json(log)
            self._log_resp(log)
        elif self.path == '/api/stk-status':
            pd = self.server.stk_pending
            resp = {'active': self.server.menu_active,
                    'pending': pd is not None,
                    'pending_type': pd['type'] if pd else None}
            if pd:
                # the panel renders a card-initiated pending command from this
                # (GET INKEY / GET INPUT prompt, SELECT ITEM items, ...)
                resp['pending_data'] = {k: v for k, v in pd.items()
                                        if k not in ('shown_at',)}
            self._send_json(resp)
            self._log_resp(resp)
        elif self.path in ('/api/scp81/status', '/api/bip/status'):
            self._log_req()
            resp = _bip_status_body()
            self._send_json(resp)
            self._log_resp(resp)
        elif (self.path == '/api/scp81/log' or self.path.startswith('/api/scp81/log?')
              or self.path == '/api/bip/log' or self.path.startswith('/api/bip/log?')):
            self._log_req()
            after = 0
            if '?' in self.path:
                for kv in self.path.split('?', 1)[1].split('&'):
                    if kv.startswith('after='):
                        try:
                            after = int(kv[6:])
                        except ValueError:
                            after = 0
            resp = {'seq': _BIP.seq, 'entries': _BIP.entries_after(after)[-200:]}
            self._send_json(resp)
            self._log_resp(resp)
        elif self.path == '/api/scp81/script':
            self._log_req()
            resp = _scp81_script_state()
            self._send_json(resp)
            self._log_resp(resp)
        elif self.path == '/api/test/scripts':
            self._log_req()
            store = self._test_scripts_store_or_503()
            if store is None:
                return
            resp = store.info()
            resp['scripts'] = store.list()
            self._send_json(resp)
            self._log_resp({'path': resp['path'], 'count': resp['count']})
        elif self.path == '/api/test/suites':
            self._log_req()
            store = self._test_suites_store_or_503()
            if store is None:
                return
            resp = store.info()
            resp['suites'] = store.list()
            self._send_json(resp)
            self._log_resp({'path': resp['path'], 'count': resp['count']})
        elif self.path == '/api/presets':
            self._log_req()
            store = self._preset_store_or_503()
            if store is None:
                return
            resp = store.info()
            resp['presets'] = store.list()
            self._send_json(resp)
            self._log_resp(resp)
        elif self.path.startswith('/api/'):
            self._send_json({'error': _err('not_found', lang)}, 404)
            self._log_resp({'error': _err('not_found', lang)})
        else:
            self._log_req()
            self._serve_static()

    def do_POST(self):
        if _test_request_blocked(self.path):
            self._send_json({'error': 'test script running - card commands are '
                                      'blocked until it finishes'}, 409)
            return
        # Serialize all card access: the background STATUS poll runs in its own
        # thread and must never interleave with a FETCH/TERMINAL RESPONSE pair.
        with _CARD_LOCK:
            _reset_poll_timer()
            self._do_POST()

    def _do_POST(self):
        lang = _get_lang(self.headers)
        if self.path == '/api/command':
            app = self.server.app
            if not app:
                self._send_json({'error': _err('app_not_init', lang)}, 503)
                self._log_resp({'error': _err('app_not_init', lang)})
                return
            body = self._read_body()
            self._log_req(body)
            cmd = body.get('cmd', '')
            is_equip = str(cmd).strip().startswith('equip')
            if is_equip:
                # A pcscd restart kills the PC/SC context; rebuild the
                # transport so a manual Equip recovers too.
                _ensure_transport(self.server)
            t0 = time.time()
            out = StringIO()
            old_stdout = app.stdout
            old_stderr = sys.stderr
            app.stdout = out
            sys.stderr = out
            try:
                stop = app.onecmd_plus_hooks(cmd)
                output = _strip_ansi(out.getvalue())
            except Exception as e:
                output = str(e) + '\n' + traceback.format_exc()
            finally:
                app.stdout = old_stdout
                sys.stderr = old_stderr
            elapsed = int((time.time() - t0) * 1000)
            status = 'OK' if not output or 'not a recognized command' not in output else 'ERROR'
            if is_equip:
                _tlog('equip: onecmd_plus_hooks %dms' % elapsed)
            if is_equip and self.server.app and self.server.terminal_profile and _app_equip_complete(self.server.app):
                _apply_equipped_card(self.server)
            elif is_equip:
                output += ('EQUIP: the shell did not register its command sets - '
                           'see the output above\n')
            sys.stderr.write("CMD: %s → %s (%dms)\n" % (cmd, status, elapsed))
            resp = {'output': output, 'stop': bool(stop)}
            self._send_json(resp)
            self._log_resp(resp)
        elif self.path == '/api/apdu':
            scc = self.server.scc
            if not scc:
                self._send_json({'error': _err('reader_not_init', lang)}, 503)
                self._log_resp({'error': _err('reader_not_init', lang)})
                return
            body = self._read_body()
            self._log_req(body)
            apdu_hex = body.get('apdu', '')
            t0 = time.time()
            try:
                data, sw = scc.send_apdu_checksw(apdu_hex)
                elapsed = int((time.time() - t0) * 1000)
                resp = {'response': data, 'sw': sw}
                sys.stderr.write("APDU: %s → SW: %s (%dms)\n" % (apdu_hex, sw, elapsed))
                self._send_json(resp)
                self._log_resp(resp)
            except Exception as e:
                elapsed = int((time.time() - t0) * 1000)
                err = {'error': str(e)}
                sys.stderr.write("APDU: %s → ERROR: %s (%dms)\n" % (apdu_hex, str(e), elapsed))
                self._send_json(err, 500)
                self._log_resp(err)
        elif self.path == '/api/verify-adm':
            scc = self.server.scc
            app = self.server.app
            if not scc or not app:
                self._send_json({'error': _err('reader_not_init', lang)}, 503)
                self._log_resp({'error': _err('reader_not_init', lang)})
                return
            body = self._read_body()
            self._log_req(_redact_psk_fields(body))
            adm = re.sub(r'\s', '', str(body.get('adm') or '')).upper()
            if not re.fullmatch(r'(?:[0-9A-F]{2}){2,8}', adm):
                resp = {'ok': False, 'error': 'adm must be 4-16 hex digits'}
                self._send_json(resp, 400)
                self._log_resp(resp)
                return
            try:
                with _CARD_LOCK:
                    resp = _verify_adm(scc, app, adm)
                if resp.get('ok'):
                    # the run prerequisites (scripts/suites) read this latch:
                    # the ADM is verified for the current card session
                    self.server.adm_session = getattr(self.server, 'card_session', 0)
                    self.server.adm_failed_session = None
                sys.stderr.write('VERIFY ADM → SW: %s\n' % resp.get('sw'))
                self._send_json(resp)
                self._log_resp(resp)
            except Exception as e:
                resp = {'ok': False, 'error': str(e)}
                sys.stderr.write('VERIFY ADM → ERROR: %s\n' % e)
                self._send_json(resp, 500)
                self._log_resp(resp)
        elif self.path == '/api/esim/profile':
            app = self.server.app
            if not app or not self.server.scc:
                self._send_json({'error': _err('reader_not_init', lang)}, 503)
                self._log_resp({'error': _err('reader_not_init', lang)})
                return
            body = self._read_body()
            self._log_req(body)
            if not esim.is_euicc(app):
                resp = {'error': _err('not_an_euicc', lang)}
                self._send_json(resp, 400)
                self._log_resp(resp)
                return
            action = str(body.get('action') or '')
            try:
                with _CARD_LOCK:
                    _finish_pending_menu(self.server, self.server.scc)
                    cursor = _PROACTIVE_ENTRY_ID
                    resp = esim.switch_profile(
                        app, self.server.scc._tp.send_apdu,
                        lambda sw: _esim_refresh_chain(self.server, sw),
                        action, iccid=body.get('iccid'),
                        isdp_aid=body.get('isdp_aid'),
                        refresh=body.get('refresh', True))
                    # A REFRESH during the command means the card wants the
                    # terminal to re-initialize; the switch itself completes on
                    # the TERMINAL RESPONSE or the reset (SGP.22 5.7.16 step 8).
                    resp['refresh_seen'] = resp.get('refresh_seen') or any(
                        e.get('type_hex') == '01' and e.get('id', 0) > cursor
                        for e in _PROACTIVE_LOG)
                    resp['reinitialized'] = False
                    resp['verified'] = None
                    resp['state_after'] = None
                    if resp['ok'] or resp['refresh_seen']:
                        resp['reinitialized'] = _esim_reinit(self.server)
                        if resp['reinitialized']:
                            resp.update(_esim_verify_switch(
                                app, action, iccid=body.get('iccid'),
                                isdp_aid=body.get('isdp_aid')))
                    resp['iccid'] = getattr(self.server, 'iccid', None)
                    resp['card_session'] = getattr(self.server, 'card_session', None)
                self._send_json(resp)
                self._log_resp({k: resp.get(k) for k in
                                ('ok', 'result', 'refresh_seen', 'reinitialized',
                                 'verified', 'state_after')})
            except esim.EsimError as e:
                resp = {'ok': False, 'error': e.code, 'message': str(e)}
                self._send_json(resp, 400)
                self._log_resp(resp)
            except Exception as e:
                resp = {'ok': False, 'error': str(e)}
                sys.stderr.write('ESIM profile switch: %s\n' % e)
                self._send_json(resp, 500)
                self._log_resp(resp)
        elif self.path == '/api/status-poll':
            scc = self.server.scc
            if not scc:
                self._send_json({'error': _err('reader_not_init', lang)}, 503)
                self._log_resp({'error': _err('reader_not_init', lang)})
                return
            sys.stderr.write('STATUS poll (manual)\n')
            try:
                st_data, st_sw = _send_status(scc)
                sys.stderr.write('STATUS -> %s\n' % st_sw)
                resp = {'sw': st_sw}
                if st_sw.startswith('91'):
                    _handle_proactive_chain(scc, st_sw)
                    resp['proactive'] = True
                self._send_json(resp)
                self._log_resp(resp)
            except Exception as e:
                sys.stderr.write('STATUS poll error: %s\n' % e)
                _handle_card_disconnect(stale=_is_transport_fatal(e))
                self._send_json({'sw': None, 'error': 'card disconnected'})
                self._log_resp({'sw': None, 'error': 'card disconnected'})
        elif self.path == '/api/net-sim':
            app = self.server.app
            rs = app.rs if app else None
            if not app or not rs:
                self._send_json({'error': _err('no_card_state', lang)}, 503)
                self._log_resp({'error': _err('no_card_state', lang)})
                return
            body = self._read_body()
            self._log_req(body)
            scenario = body.get('scenario')
            if not scenario:
                self._send_json({'error': 'scenario is required'}, 400)
                self._log_resp({'error': 'scenario is required'})
                return
            try:
                result = netsim.run_scenario(
                    sys.modules[__name__], app, scenario, body,
                    event_list=getattr(self.server, 'event_list', None) or [])
                _netstate_after_net_sim(self.server, scenario, result)
                result['net_state'] = getattr(self.server, 'net_state', None)
                self._send_json(result)
                self._log_resp({'scenario': scenario,
                                'success': result.get('success'),
                                'steps': len(result.get('steps', []))})
            except ValueError as e:
                self._send_json({'error': str(e)}, 400)
                self._log_resp({'error': str(e)})
            except Exception as e:
                sys.stderr.write('Net-sim error: %s\n' % e)
                _handle_card_disconnect(stale=_is_transport_fatal(e))
                self._send_json({'error': 'simulation failed: %s' % e}, 500)
                self._log_resp({'error': str(e)})
        elif self.path == '/api/net-state-refresh':
            if not self.server.app or not getattr(self.server, 'scc', None):
                self._send_json({'error': _err('no_card_state', lang)}, 503)
                self._log_resp({'error': _err('no_card_state', lang)})
                return
            body = self._read_body()
            self._log_req(body)
            keys = body.get('files') if isinstance(body, dict) else None
            state = getattr(self.server, 'net_state', None)
            if state is None:
                state = netstate.new_state()
                self.server.net_state = state
            try:
                files = _netstate_read(self.server.app, keys)
            except Exception as e:
                self._send_json({'error': str(e)}, 500)
                self._log_resp({'error': str(e)})
                return
            netstate.merge_read(state, files, source='refresh')
            netstate.set_read_time(state)
            _netstate_compute(self.server)
            resp = {'available': True, 'state': state}
            self._send_json(resp)
            self._log_resp({'available': True, 'files': len(files or {})})
        elif self.path == '/api/rescue':
            scc = self.server.scc
            if not scc:
                self._send_json({'error': _err('reader_not_init', lang)}, 503)
                self._log_resp({'error': _err('reader_not_init', lang)})
                return
            if not self.server.terminal_profile:
                self._send_json({'error': 'no terminal profile configured'}, 400)
                self._log_resp({'error': 'no terminal profile configured'})
                return
            _finish_pending_menu(self.server, scc)
            sys.stderr.write('RESCUE: re-sending TERMINAL PROFILE\n')
            resp = _resend_terminal_profile(self.server, scc)
            self._send_json(resp)
            self._log_resp(resp)
        elif self.path == '/api/terminal-profile':
            body = self._read_body()
            self._log_req(body)
            scc = self.server.scc
            if not scc:
                self._send_json({'error': _err('reader_not_init', lang)}, 503)
                self._log_resp({'error': _err('reader_not_init', lang)})
                return
            profile = body.get('profile')
            if profile is not None:
                h, perr = _validate_tp_hex(profile)
                if perr:
                    self._send_json({'error': perr}, 400)
                    self._log_resp({'error': perr})
                    return
                self.server.terminal_profile = h
            if not self.server.terminal_profile:
                self._send_json({'error': 'no terminal profile configured'}, 400)
                self._log_resp({'error': 'no terminal profile configured'})
                return
            # Never re-send the TP while a FETCHed command awaits its
            # TERMINAL RESPONSE: answer it with a cancel TR first (v3.6.35).
            _finish_pending_menu(self.server, scc)
            sys.stderr.write('TERMINAL-PROFILE: re-sending %s\n' % self.server.terminal_profile)
            try:
                resp = _resend_terminal_profile(self.server, scc)
            except Exception as e:
                resp = {'ok': False, 'error': str(e)}
                self._send_json(resp, 502)
                self._log_resp(resp)
                return
            self._send_json(resp)
            self._log_resp(resp)
        elif self.path == '/api/help':
            app = self.server.app
            if not app:
                self._send_json({'error': _err('app_not_init', lang)}, 503)
                self._log_resp({'error': _err('app_not_init', lang)})
                return
            body = self._read_body()
            self._log_req(body)
            cmd = body.get('cmd', '')
            out = StringIO()
            old_stdout = app.stdout
            old_stderr = sys.stderr
            app.stdout = out
            sys.stderr = out
            try:
                app.onecmd_plus_hooks('help ' + cmd)
                raw = out.getvalue()
            finally:
                app.stdout = old_stdout
                sys.stderr = old_stderr
            clean = _strip_ansi(raw)
            parsed = _parse_help_text(clean)
            self._send_json(parsed)
            self._log_resp(parsed)
        elif self.path == '/api/select':
            app = self.server.app
            if not app:
                self._send_json({'error': _err('app_not_init', lang)}, 503)
                self._log_resp({'error': _err('app_not_init', lang)})
                return
            body = self._read_body()
            self._log_req(body)
            path = body.get('path')
            fid = body.get('fid')
            name = fid if fid else body.get('name', '')
            parent_sel = body.get('parent_sel')
            parent_path = body.get('parent_path')
            allow_probe = bool(body.get('allow_probe'))
            rs = app.rs
            if not rs:
                self._send_json({'error': _err('no_card_state', lang)}, 503)
                self._log_resp({'error': _err('no_card_state', lang)})
                return
            lchan = rs.lchan[0]
            cleanup = None
            try:
                _collect_apdu_times()
                try:
                    if path:
                        cur, cleanup = _select_path(lchan, path, app)
                    else:
                        cur, cleanup = _select_with_parent(lchan, name, parent_sel, app, parent_path, allow_probe)
                finally:
                    apdu_times = _end_apdu_time_collection()
                cur = cur or lchan.selected_file
                data = {
                    'name': cur.name if cur else None,
                    'fid': cur.fid.upper() if cur and cur.fid else None,
                    'file_type': _get_file_type(lchan, cur),
                    'file_size': _fcp_value(lchan, 'selected_file_size'),
                    'record_len': _fcp_value(lchan, 'selected_file_record_len'),
                    'num_of_rec': _fcp_value(lchan, 'selected_file_num_of_rec'),
                    'fci_hex': (lchan.selected_file_fcp_hex or '').upper() if lchan and lchan.selected_file_fcp_hex else None,
                    'apdu_times': apdu_times,
                    'exists': True,
                }
                self._send_json(data)
                self._log_resp(data)
            except Exception as e:
                err = {'error': str(e), 'exists': False}
                self._send_json(err, 404)
                self._log_resp(err)
            finally:
                if cleanup:
                    cleanup()
        elif self.path == '/api/read':
            app = self.server.app
            if not app:
                self._send_json({'error': _err('app_not_init', lang)}, 503)
                self._log_resp({'error': _err('app_not_init', lang)})
                return
            body = self._read_body()
            self._log_req(body)
            path = body.get('path')
            fid = body.get('fid')
            name = fid if fid else body.get('name', '')
            parent_sel = body.get('parent_sel')
            parent_path = body.get('parent_path')
            allow_probe = bool(body.get('allow_probe'))
            mode = body.get('mode', 'raw')
            rs = app.rs
            if not rs:
                self._send_json({'error': _err('no_card_state', lang)}, 503)
                self._log_resp({'error': _err('no_card_state', lang)})
                return
            lchan = rs.lchan[0]
            cleanup = None
            try:
                _collect_apdu_times()
                try:
                    sel = fid if fid else name
                    if path:
                        _, cleanup = _select_path(lchan, path, app)
                    else:
                        _, cleanup = _select_with_parent(lchan, sel, parent_sel, app, parent_path, allow_probe)
                    ft = _get_file_type(lchan, lchan.selected_file)
                    is_record = ft in ('linear_fixed', 'cyclic')
                    if mode == 'decoded':
                        cmd = 'read_records_decoded' if is_record else 'read_binary_decoded'
                    else:
                        cmd = 'read_records' if is_record else 'read_binary'
                    out = StringIO()
                    old_stdout = app.stdout
                    old_stderr = sys.stderr
                    app.stdout = out
                    sys.stderr = out
                    try:
                        app.onecmd_plus_hooks(cmd)
                        output = _strip_ansi(out.getvalue())
                    finally:
                        app.stdout = old_stdout
                        sys.stderr = old_stderr
                finally:
                    apdu_times = _end_apdu_time_collection()
                sw_match = re.search(r'SW:\s*(\w+)', output)
                err_match = re.search(r'got (\w+)', output)
                if err_match:
                    sw = err_match.group(1)
                    descs = {'6982': 'Security status not satisfied', '6983': 'PIN blocked',
                             '6985': 'Conditions of use not satisfied', '6A88': 'Referenced data not found',
                             '6A82': 'File not found'}
                    resp = {'success': False, 'sw': sw, 'error': descs.get(sw, 'Error')}
                    self._send_json(resp)
                    self._log_resp(resp)
                    return
                sw = sw_match.group(1) if sw_match else '9000'
                clean = re.sub(r'^SW:\s*\w+\s*', '', output, flags=re.MULTILINE).strip()
                if mode == 'decoded':
                    try:
                        parsed = json.loads(clean)
                        resp = {'success': True, 'sw': sw, 'file_type': ft, 'decoded': parsed, 'apdu_times': apdu_times}
                    except json.JSONDecodeError:
                        resp = {'success': True, 'sw': sw, 'file_type': ft, 'data': clean, 'apdu_times': apdu_times}
                elif is_record:
                    records = []
                    for line in clean.split('\n'):
                        m = re.match(r'^(\d+)\s(.+)', line)
                        if m:
                            records.append({'num': int(m.group(1)), 'data': m.group(2)})
                    resp = {'success': True, 'sw': sw, 'file_type': ft, 'records': records, 'apdu_times': apdu_times}
                else:
                    resp = {'success': True, 'sw': sw, 'file_type': ft, 'data': clean, 'apdu_times': apdu_times}
                self._send_json(resp)
                self._log_resp(resp)
            except Exception as e:
                err = {'success': False, 'error': str(e)}
                self._send_json(err, 500)
                self._log_resp(err)
            finally:
                if cleanup:
                    cleanup()
        elif self.path == '/api/write':
            app = self.server.app
            if not app:
                self._send_json({'error': _err('app_not_init', lang)}, 503)
                self._log_resp({'error': _err('app_not_init', lang)})
                return
            body = self._read_body()
            self._log_req(body)
            name = body.get('name', '')
            data = body.get('data', '')
            fid = body.get('fid')
            record_nr = body.get('record_nr')
            parent_sel = body.get('parent_sel')
            parent_path = body.get('parent_path')
            allow_probe = bool(body.get('allow_probe'))
            path = body.get('path')
            rs = app.rs
            if not rs:
                self._send_json({'error': _err('no_card_state', lang)}, 503)
                self._log_resp({'error': _err('no_card_state', lang)})
                return
            lchan = rs.lchan[0]
            cleanup = None
            try:
                if path:
                    _, cleanup = _select_path(lchan, path, app)
                else:
                    sel = fid if fid else name
                    _, cleanup = _select_with_parent(lchan, sel, parent_sel, app, parent_path, allow_probe)
                ft = _get_file_type(lchan, lchan.selected_file)
                is_record = ft in ('linear_fixed', 'cyclic')
                if record_nr:
                    cmd = 'update_record %d %s' % (record_nr, data)
                elif is_record:
                    cmd = 'update_record 1 %s' % data
                else:
                    cmd = 'update_binary %s' % data
                out = StringIO()
                old_stdout = app.stdout
                old_stderr = sys.stderr
                app.stdout = out
                sys.stderr = out
                try:
                    app.onecmd_plus_hooks(cmd)
                    output = out.getvalue()
                finally:
                    app.stdout = old_stdout
                    sys.stderr = old_stderr
                sw_match = re.search(r'SW:\s*(\w+)', output)
                err_match = re.search(r'got (\w+)', output)
                if err_match:
                    sw = err_match.group(1)
                    descs = {'6982': 'Security status not satisfied', '6983': 'PIN blocked',
                             '6985': 'Conditions of use not satisfied', '6A88': 'Referenced data not found',
                             '6A82': 'File not found'}
                    resp = {'success': False, 'sw': sw, 'error': descs.get(sw, 'Error')}
                else:
                    sw = sw_match.group(1) if sw_match else '9000'
                    resp = {'success': True, 'sw': sw}
                self._send_json(resp)
                self._log_resp(resp)
            except Exception as e:
                err = {'success': False, 'error': str(e)}
                self._send_json(err, 500)
                self._log_resp(err)
            finally:
                if cleanup:
                    cleanup()
        elif self.path == '/api/tree':
            app = self.server.app
            if not app:
                self._send_json({'error': _err('app_not_init', lang)}, 503)
                self._log_resp({'error': _err('app_not_init', lang)})
                return
            body = self._read_body()
            self._log_req(body)
            fid = body.get('fid')
            name = fid if fid else body.get('name', '')
            fid = body.get('fid')
            parent_sel = body.get('parent_sel')
            parent_path = body.get('parent_path')
            allow_probe = bool(body.get('allow_probe'))
            rs = app.rs
            if not rs:
                self._send_json({'error': _err('no_card_state', lang)}, 503)
                self._log_resp({'error': _err('no_card_state', lang)})
                return
            lchan = rs.lchan[0]
            cleanup = None
            try:
                sel = fid if fid else name
                _, cleanup = _select_with_parent(lchan, sel, parent_sel, app, parent_path, allow_probe)
                cur = lchan.selected_file
                out = StringIO()
                old_stdout = app.stdout
                old_stderr = sys.stderr
                app.stdout = out
                sys.stderr = out
                try:
                    app.onecmd_plus_hooks('tree')
                    output = _strip_ansi(out.getvalue())
                finally:
                    app.stdout = old_stdout
                    sys.stderr = old_stderr
                children = _parse_tree_output(output)
                sels = lchan.selected_file.get_selectables() if lchan and lchan.selected_file else {}
                for child in children:
                    if child['isDir'] and child['name'] in sels:
                        f = sels[child['name']]
                        if hasattr(f, 'aid') and f.aid:
                            child['aid'] = f.aid.upper()
                resp = {
                    'exists': True,
                    'name': cur.name if cur else None,
                    'fid': cur.fid.upper() if cur and cur.fid else None,
                    'file_type': _get_file_type(lchan, cur),
                    'children': children,
                }
                self._send_json(resp)
                self._log_resp(resp)
            except Exception as e:
                sys.stderr.write('Handler error: %s\n' % e)
                if _is_pcsc_error(e) or 'Card' in str(e) or 'Transaction' in str(e) or 'Transmit' in str(e):
                    _handle_card_disconnect(stale=_is_transport_fatal(e))
                err = {'success': False, 'error': str(e), 'exists': False}
                self._send_json(err, 500)
                self._log_resp(err)
            finally:
                if cleanup:
                    cleanup()
        elif self.path == '/api/menu-select':
            scc = self.server.scc
            if not scc:
                self._send_json({'error': 'card reader not available'}, 503)
                return
            body = self._read_body()
            self._log_req(body)
            _finish_pending_menu(self.server, scc)
            item_id = body.get('item_id', 0)
            if not isinstance(item_id, int):
                item_id = int(item_id)
            self.server.menu_active = True
            # Build ENVELOPE(Menu Selection): D3 [len] DeviceIdentities + ItemIdentifier
            menu_tlv = bytes([0xD3, 0x07, 0x02, 0x02, 0x01, 0x81, 0x90, 0x01, item_id])
            env_hex = '%sc20000%02x%s' % (scc.cat_cla, len(menu_tlv), menu_tlv.hex())
            data, sw = scc._tp.send_apdu(env_hex)
            sys.stderr.write('MENU-SELECT: ENVELOPE %s item=%d -> %s\n'
                             % (env_hex, item_id, sw))
            resp = {'type': 'done', 'sw': sw}
            on_fetch = _make_menu_fetch_handler(self.server, resp)
            if sw.startswith('91'):
                _handle_proactive_chain(scc, sw, on_fetch)
            else:
                self.server.menu_active = False
                self.server.stk_pending = None
                if sw == '9000':
                    sys.stderr.write('STATUS poll (menu-select 9000)\n')
                    st_data, st_sw = _send_status(scc)
                    sys.stderr.write('STATUS -> %s\n' % st_sw)
                    if st_sw.startswith('91'):
                        _handle_proactive_chain(scc, st_sw, on_fetch)
            if self.server.stk_pending:
                _arm_menu_timeout()
            else:
                _cancel_menu_timeout()
            self._send_json(resp)
            self._log_resp(resp)
        elif self.path == '/api/menu-respond':
            body = self._read_body()
            self._log_req(body)
            resp, code = _menu_send_response(self.server, body.get('result', 'ok'),
                                             body.get('item_id'), body.get('text'))
            self._send_json(resp, code)
            self._log_resp(resp)
        elif self.path == '/api/event-send':
            scc = self.server.scc
            if not scc:
                self._send_json({'error': _err('reader_not_init', lang)}, 503)
                self._log_resp({'error': _err('reader_not_init', lang)})
                return
            body = self._read_body()
            self._log_req(body)
            event_type = body.get('event_type')
            event_data_hex = body.get('event_data')
            if event_type is None:
                self._send_json({'error': 'event_type is required'}, 400)
                return
            event_data = bytes.fromhex(event_data_hex) if event_data_hex else None
            event_src = body.get('event_src')
            try:
                data, sw = _send_event_download(scc, event_type, event_data, src=event_src)
                try:
                    ev_type = int(event_type)
                except (TypeError, ValueError):
                    ev_type = -1
                is_location = ev_type == netsim.EVENT_LOCATION_STATUS
                if is_location:
                    _netstate_after_event(self.server, event_type, event_data)
                resp = {'sw': sw}
                if data:
                    resp['data'] = data
                if ev_type == 0x1C:
                    negotiation = _decode_poll_negotiation(data)
                    if negotiation:
                        resp['negotiation'] = negotiation
                        _apply_poll_negotiation(negotiation)
                if getattr(self.server, 'net_state', None) is not None:
                    resp['net_state'] = self.server.net_state
                self._send_json(resp)
                self._log_resp(resp)
            except ValueError as e:
                # the single-envelope budget (no chained delivery)
                self._send_json({'error': str(e)}, 400)
                self._log_resp({'error': str(e)})
            except Exception as e:
                sys.stderr.write('Event send error: %s\n' % e)
                _handle_card_disconnect(stale=_is_transport_fatal(e))
                self._send_json({'sw': None, 'error': 'card disconnected'})
                self._log_resp({'sw': None, 'error': 'card disconnected'})
        elif self.path == '/api/pli-dict':
            body = self._read_body()
            self._log_req(body)
            if isinstance(body, dict):
                for k, v in body.items():
                    if isinstance(v, str):
                        try:
                            code = int(k, 16)
                            if code in _PLI_DATA:
                                bytes.fromhex('') if not v else bytes.fromhex(v)
                                _PLI_DATA[code] = v
                        except (ValueError, KeyError):
                            pass
            resp = {('%02X' % q): v for q, v in _PLI_DATA.items()}
            self._send_json(resp)
            self._log_resp(resp)
        elif self.path == '/api/poll-toggle':
            body = self._read_body()
            self._log_req(body)
            if isinstance(body, dict) and body.get('enabled'):
                _poll_enable()
            else:
                _poll_disable()
            resp = {'enabled': _POLL_ENABLED, 'interval': _POLL_INTERVAL,
                    'card_disabled': _POLL_DISABLED_BY_CARD}
            if _POLL_ENABLED and _POLL_DISABLED_BY_CARD:
                resp['warning'] = ('card disabled proactive polling with POLLING OFF '
                                   '(TS 102 223 6.4.14); it resumes after a POLL INTERVAL')
            self._send_json(resp)
            self._log_resp(resp)
        elif self.path == '/api/send-ota':
            app = self.server.app
            if not app:
                self._send_json({'error': _err('app_not_init', lang)}, 503)
                self._log_resp({'error': _err('app_not_init', lang)})
                return
            body = self._read_body()
            self._log_req(body)
            sp = body.get('sp', '')
            apdu = body.get('apdu', '')
            scc = self.server.scc
            if not scc:
                self._send_json({'error': _err('reader_not_init', lang)}, 503)
                self._log_resp({'error': _err('reader_not_init', lang)})
                return
            # The packet's keyset number (b8..b5 of KIc/KID, TS 102 225 A.2) must
            # be consistent - a mismatch is invalid input (the card rejects it
            # with "Unidentified security error") - and, when a preset is named,
            # defined in it.  Both are refused before anything is built (and
            # before the format probe consumes a counter).
            kvn, kvn_err = _kvn_of(body.get('kic', ''), body.get('kid', ''))
            if kvn_err:
                resp = {'error': kvn_err}
                self._send_json(resp, 400)
                self._log_resp(resp)
                return
            keyset_err = _preset_keyset_check(self.server, body.get('preset_id'),
                                              body.get('kic', ''), body.get('kid', ''))
            if keyset_err:
                resp = {'error': keyset_err}
                self._send_json(resp, 400)
                self._log_resp(resp)
                return
            # SPI1 for the packet: the caller's value wins (a hand send may
            # deliberately ask for more security than the TAR's minimum), with
            # a warning below the TAR's MSL; without one it is derived from the
            # MSL of the packet's TAR (TS 102 226 8.2.1.3.2.4 - the card checks
            # the MSL before the security processing).
            try:
                spi1, spi_warn = _spi1_for_tar(
                    _preset_by_id(self.server, body.get('preset_id')),
                    str(body.get('tar') or '').strip().upper(), body.get('spi1'))
            except ValueError as e:
                resp = {'error': str(e)}
                self._send_json(resp, 400)
                self._log_resp(resp)
                return
            spi2 = str(body.get('spi2') or '01').strip().upper()
            # Only a packet whose SPI1 asks the card to check the counter has a
            # counter to track (TS 102 225 5.1.1 b5b4): with b5b4 = 00 (the
            # keyless SPI1 00 case) the field is ignored and never updated, so
            # no counter is advanced or persisted.
            counter_tracked = _counter_tracked(spi1)
            include_cpi = body.get('includeCpi', True)
            # The response form follows the command's TAR (TS 102 226 4): only
            # a remote-management TAR answers with the RM structures; a hand
            # send may name any TAR (the pre-built `sp` path may name none -
            # unknown, decode with the structural fallback).
            rm = _tar_is_rm(_preset_by_id(self.server, body.get('preset_id')),
                            body.get('tar') or ('000000' if apdu else ''))
            ram_format = None
            try:
                if apdu:
                    # RAM operation: SCP80-wrap the raw GP command.  The packet
                    # may exceed one SMS (pySim refuses that), so use our own
                    # encoder and let _send_secured_packet segment it.
                    kic = body.get('kic', '25')
                    kid = body.get('kid', '25')
                    tar = body.get('tar', '000000')
                    kic_key = body.get('kicKey', '')
                    kid_key = body.get('kidKey', '')
                    # The counter: an explicit `cntr` wins; otherwise the
                    # preset store's value for the packet's keyset - the
                    # server owns the counters (v3.20.0).
                    cntr = _preset_counter_seed(self.server, body, kvn)
                    # RAM command format: only when the caller opted in with a
                    # ram_format value (the RAM views do; applet-directed or
                    # pre-built payloads must not be wrapped).  The probe(s)
                    # consume counters, so the command uses the advanced value.
                    if body.get('ram_format') is not None:
                        ram_format = _ram_normalize_format(body.get('ram_format'))
                        if ram_format == 'auto':
                            probe_state = {'steps': [], 'encode_error': None,
                                           'failure': {}, 'cntr': cntr,
                                           'preset_id': body.get('preset_id')}
                            sp_probe = {'spi1': spi1, 'spi2': spi2, 'kic': kic,
                                        'kid': kid, 'tar': tar, 'kic_key': kic_key,
                                        'kid_key': kid_key,
                                        'include_cpi': include_cpi}
                            ram_format = _ram_detect_format(self.server, scc, sp_probe,
                                                            probe_state)
                            cntr = probe_state['cntr']
                        apdu = _ram_format_apdu(apdu, ram_format)
                    sp_hex, _ = _build_secured_packet(spi1, spi2, kic, kid, tar, cntr, apdu, kic_key, kid_key)
                else:
                    # Regular SCP80: use pre-built secured packet.  The packet
                    # carries its own counter; `cntr` here is the caller's
                    # bookkeeping value for `final_cntr`/persistence (no
                    # preset seed - the packet may hold anything).
                    sp_hex = sp
                    cntr = str(body.get('cntr') or '').strip().upper()
                if cntr and not counter_tracked:
                    # the packet carries a counter (explicit or seeded) but
                    # the card will not track it: SPI1.b5b4 = 00 means
                    # "present, ignored, never updated" (TS 102 225 5.1.1)
                    sys.stderr.write('OTA SEND: counter not tracked (SPI1 %s: '
                                     'no counter check)\n' % spi1)
                spi2_val = int(spi2, 16)
                # Capture the PoR from either transport: inline in the
                # ENVELOPE response or as a proactive SEND SHORT MESSAGE (some
                # cards always submit it, whatever the SPI2 request bit says;
                # v3.6.24).
                submit_handler = None
                old_proactive = None
                if hasattr(scc, '_tp'):
                    submit_handler = PoRSubmitHandler()
                    old_proactive = scc._tp.proactive_handler
                    scc._tp.proactive_handler = submit_handler
                try:
                    sys.stderr.write('OTA SEND: SPI %s %s KIc %s KID %s TAR %s CNTR %s LEN %dB\n' % (
                        spi1, spi2, body.get('kic', ''),
                        body.get('kid', ''), body.get('tar', ''), cntr,
                        len(sp_hex) // 2))
                    sys.stderr.write('RAM C-APDU: %s\n' % apdu if apdu else sp)
                    sys.stderr.write('RAM SECURED-PACKET: %s\n' % sp_hex)
                    result = _send_secured_packet(
                        scc, sp_hex, oa_number=self.server.sms_oa,
                        sm_sc=self.server.sms_sc, include_cpi=include_cpi,
                        submit_handler=submit_handler)
                    if not result['success']:
                        resp = result
                        if ram_format is not None:
                            resp['ram_format'] = ram_format
                        # A warning ENVELOPE may still carry the PoR (the
                        # card's counter verdict): decode it so the caller
                        # sees 'cntr_low' instead of a bare failure.
                        failed_por_hex = result.get('response_data') or ''
                        if not failed_por_hex and submit_handler:
                            failed_por_hex = _sms_submit_por(submit_handler)
                        failed_por = _decode_por(spi1, spi2,
                                                 body.get('kic', ''), body.get('kid', ''),
                                                 cntr, body.get('kicKey', ''),
                                                 body.get('kidKey', ''), failed_por_hex,
                                                 cmd_len=len(body.get('apdu') or '') // 2 or None,
                                                 rm=rm)
                        if failed_por:
                            resp['por'] = failed_por
                            low = _cntr_low_fields(failed_por.get('response_status'))
                            if low:
                                resp.update(low)
                                sys.stderr.write('OTA CNTR-LOW: the packet counter is not '
                                                 'above the card\'s\n')
                            # only a packet the card accepted consumes the
                            # counter (a warning ENVELOPE can still carry a
                            # por_ok PoR); a rejected one must leave the store
                            # untouched (v3.9.x - it used to persist the
                            # packet's own counter here)
                            if (_counter_valid(cntr) and counter_tracked and
                                    failed_por.get('response_status') in
                                    ('por_ok', 'actual_response_sms_submit')):
                                resp['final_cntr'] = _ram_next_cntr(cntr, True)
                        sys.stderr.write('OTA SEND FAILED: %s\n' % result.get('error'))
                    else:
                        resp = {'success': True, 'sw': result['sw'],
                                'response_data': result['response_data'],
                                'bytes': result['bytes'], 'segments': result['segments']}
                        por_src = 'envelope'
                        por_hex = resp['response_data']
                        submit_hex = _sms_submit_por(submit_handler)
                        if submit_hex:
                            por_hex = submit_hex
                            por_src = 'sms-submit'
                        elif submit_handler and submit_handler.submit_tpdu_hex:
                            # no assembled UD: fall back to a raw RPI packet
                            tpdu_b = bytes.fromhex(submit_handler.submit_tpdu_hex)
                            idx = tpdu_b.find(b'\x02\x71\x00')
                            if idx >= 0:
                                por_hex = tpdu_b[idx:].hex()
                                por_src = 'sms-submit'
                        por = _decode_por(spi1, spi2, body.get('kic', ''),
                                          body.get('kid', ''), cntr, body.get('kicKey', ''),
                                          body.get('kidKey', ''), por_hex,
                                          cmd_len=len(body.get('apdu') or '') // 2 or None,
                                          rm=rm)
                        # Check for SPI2=0x21 (PoR required) but got 9000 with no PoR → card refuses PoR
                        por_required = bool(spi2_val & 0x01)
                        no_por_received = not por_hex and not (submit_handler and submit_handler.submit_tpdu_hex)
                        if por_required and result['sw'] == '9000' and no_por_received:
                            # the PoR was requested and the card did not answer -
                            # true for pre-built packets (the keyless form) too
                            resp['por_missing'] = True
                            sys.stderr.write('WARNING: Card refused to return PoR - ENVELOPE returned 9000 with no response data\n')
                        sys.stderr.write('RAM RESPONSE-PACKET: %s\n' % (por_hex if por_hex else 'empty'))
                        if por:
                            resp['por'] = por
                            low = _cntr_low_fields(por.get('response_status'))
                            if low:
                                # a 9000/91xx ENVELOPE can still carry a
                                # cntr_low PoR (the live card does)
                                resp.update(low)
                                sys.stderr.write('OTA CNTR-LOW: the packet counter is not '
                                                 'above the card\'s\n')
                            extra = ''
                            if por.get('bad_format'):
                                extra = ' (expanded: bad format %s - %s)' % (
                                    por['bad_format'], por.get('bad_format_name') or '')
                                sys.stderr.write('RAM R-APDU: (bad format %s)\n' % por['bad_format'])
                            elif por.get('decoded'):
                                extra = ' (compact: %s cmd, last SW %s)' % (por['decoded'].get('number_of_commands', '?'),
                                                                            por['decoded'].get('last_status_word', '?'))
                                sys.stderr.write('RAM R-APDU: %s\n' % por['decoded'].get('last_response_data', ''))
                            sys.stderr.write('OTA PoR[%s]: status=%s TAR=%s CNTR=%s PCNTR=%s RPL=%s RHL=%s%s\n' % (
                                por_src, por.get('response_status'), por.get('tar'), por.get('cntr'),
                                por.get('pcntr'), por.get('rpl'), por.get('rhl'), extra))
                        elif por_hex:
                            sys.stderr.write('OTA PoR[%s]: undecodable raw=%s\n' % (por_src, str(por_hex)))
                        else:
                            sys.stderr.write('OTA PoR[%s]: none\n' % por_src)
                        accepted = ((por is None) or por.get('response_status') in
                                    ('por_ok', 'actual_response_sms_submit'))
                        if ram_format is not None:
                            resp['ram_format'] = ram_format
                        # The counter to use next, reported for the RAM and the
                        # plain SCP80 path alike; the server persists it
                        # (v3.8.0).  A pre-built packet may come without a
                        # counter, and a packet whose SPI1 does not ask the card
                        # to check it has none to track - nothing is reported
                        # then (and int('') never happens).  A rejected verdict
                        # (cntr_low, PoR error) reports nothing either: the
                        # store must not move on a packet the card did not
                        # consume.
                        if _counter_valid(cntr) and counter_tracked and accepted:
                            resp['final_cntr'] = _ram_next_cntr(cntr, True)
                finally:
                    if submit_handler and hasattr(scc, '_tp'):
                        scc._tp.proactive_handler = old_proactive
                if cntr:
                    # echo the effective counter (a request without one got
                    # the preset's value - v3.20.0)
                    resp['cntr'] = cntr
                if counter_tracked:
                    _preset_counter_persist(self.server, body.get('preset_id'),
                                            resp.get('final_cntr'), 'send-ota', kvn)
                if spi_warn:
                    resp['warning'] = spi_warn
                self._send_json(resp)
                self._log_resp(resp)
            except Exception as e:
                sys.stderr.write('OTA send error: %s\n' % e)
                if _is_pcsc_error(e) or 'Card' in str(e) or 'Transaction' in str(e) or 'Transmit' in str(e):
                    _handle_card_disconnect(stale=_is_transport_fatal(e))
                err = {'success': False, 'error': str(e)}
                self._send_json(err, 500)
                self._log_resp(err)
        elif self.path == '/api/sp-verify':
            body = self._read_body()
            self._log_req(body)
            try:
                # The reference must be built from the same counter the JS
                # packet used: an explicit body value wins, otherwise the
                # preset's (v3.20.0 - the PWA omits an untouched counter).
                kvn_ref = _kvn_of(body.get('kic', ''), body.get('kid', ''))[0]
                cntr_ref = _preset_counter_seed(self.server, body, kvn_ref)
                ref, spi = _ota_reference(body.get('spi1', ''), body.get('spi2', ''), body.get('kic', ''),
                                          body.get('kid', ''), body.get('tar', ''), cntr_ref,
                                          body.get('apdu', ''), body.get('kicKey', ''), body.get('kidKey', ''))
                js_sp = (body.get('sp', '') or '').replace(' ', '').lower()
                ref_l = ref.lower()
                diffs = []
                if js_sp != ref_l:
                    n = min(len(js_sp), len(ref_l))
                    for i in range(0, n, 2):
                        if js_sp[i:i+2] != ref_l[i:i+2]:
                            diffs.append({'offset': i // 2, 'js': js_sp[i:i+2], 'ref': ref_l[i:i+2]})
                    if len(js_sp) != len(ref_l):
                        diffs.append({'offset': n // 2, 'js': js_sp[n:], 'ref': ref_l[n:]})
                resp = {'js_sp': js_sp, 'py_sp': ref_l, 'match': js_sp == ref_l, 'diffs': diffs[:50], 'spi': spi}
                self._send_json(resp)
                self._log_resp(resp)
            except Exception as e:
                err = {'success': False, 'error': str(e)}
                self._send_json(err, 500)
                self._log_resp(err)
        elif self.path == '/api/ram-install':
            body = self._read_body()
            self._log_req(body)
            scc = self.server.scc
            if not scc:
                self._send_json({'error': _err('reader_not_init', lang)}, 503)
                self._log_resp({'error': _err('reader_not_init', lang)})
                return
            try:
                cap_hex = body.get('cap_hex', '').replace(' ', '')
                if not cap_hex:
                    self._send_json({'error': 'No cap_hex provided'}, 400)
                    return

                # Parse .cap file
                loadfile_aid, module_aid, loadfile_data = _cap_parse(cap_hex)

                # SCP80 params: the SPI1 comes from the MSL of the packet's
                # TAR (the operation uses the preset's table); a caller-supplied
                # value is only the fallback for a TAR the preset does not carry.
                preset = _preset_by_id(self.server, body.get('preset_id'))
                tar = str(body.get('tar') or presets.role_tar(preset, 'isd')
                          or '000000').strip().upper()
                try:
                    spi1, _ = _spi1_for_tar(preset, tar, body.get('spi1'),
                                            prefer_msl=True)
                except ValueError as e:
                    err = {'success': False, 'error': str(e)}
                    self._send_json(err, 400)
                    self._log_resp(err)
                    return
                spi2 = body.get('spi2', '01')
                kic = body.get('kic', '25')
                kid = body.get('kid', '25')
                cntr = body.get('cntr', '00000000')
                kic_key = body.get('kicKey', '')
                kid_key = body.get('kidKey', '')
                sd_aid = body.get('sd_aid', '').replace(' ', '')
                install_params_hex = body.get('install_params', '').replace(' ', '')
                stk_params_hex = body.get('stk_params', '').replace(' ', '')
                make_selectable = body.get('make_selectable', True)
                privileges_hex = body.get('privileges', '').replace(' ', '') or '00'

                # The keyset number must be consistent and defined in the preset
                # (TS 102 225 A.2 / the "keyset must exist" rule) - checked
                # before the format probe consumes a counter.
                kvn, keyset_err = self._keyset_guard(body, kic, kid)
                if keyset_err:
                    err = {'success': False, 'error': keyset_err}
                    self._send_json(err, 400)
                    self._log_resp(err)
                    return

                # The counter: explicit `cntr` wins, else the preset's value
                # for this keyset (the server owns the counters, v3.20.0).
                cntr = _preset_counter_seed(self.server, body, kvn) or cntr

                # LOAD block size: the default 240-byte payload cannot be sent
                # over SCP80 (pySim refuses a secured packet above one SMS).
                # Fit it automatically, or honour an explicit override capped
                # to what still encodes into a single SMS.
                block_size_req = body.get('load_block_size')
                if block_size_req in (None, ''):
                    block_size_req = None
                else:
                    try:
                        block_size_req = int(block_size_req)
                    except (TypeError, ValueError):
                        block_size_req = -1
                    if not 1 <= block_size_req <= 240:
                        err = {'success': False,
                               'error': 'load_block_size must be 1..240'}
                        self._send_json(err, 400)
                        self._log_resp(err)
                        return
                # The GP LOAD payload limit is 240 bytes per APDU; SCP80
                # concatenates the secured packet over up to SCP80_MAX_SEGMENTS
                # SMs, so a block no longer has to fit into a single SMS.
                block_size = block_size_req or 240
                sys.stderr.write('RAM-INSTALL: LOAD block size %d bytes\n' % block_size)

                steps = []
                state = {'steps': steps, 'encode_error': None, 'failure': {},
                         'cntr': cntr, 'preset_id': body.get('preset_id'),
                         'kvn': kvn}
                sp_state = {'spi1': spi1, 'spi2': spi2, 'kic': kic, 'kid': kid,
                            'tar': tar, 'kic_key': kic_key, 'kid_key': kid_key,
                            'include_cpi': body.get('includeCpi', True)}

                # INSTALL [for load] -> LOAD blocks -> INSTALL [for install].
                # The install parameters are composed per the spec: an
                # EF-form STK part shares one System Specific Parameters EF
                # with the quotas (TS 102 226 8.2.1.3.2.1; v3.20.0) - an
                # invalid combination is refused before anything is sent.
                try:
                    seq = _cap_apdu_sequence(
                        loadfile_aid, module_aid, loadfile_data, sd_aid=sd_aid,
                        privileges=privileges_hex,
                        install_params=install_params_hex, stk_params=stk_params_hex,
                        make_selectable=make_selectable, block_size=block_size,
                        nv_quota=body.get('nv_quota'),
                        volatile_quota=body.get('volatile_quota'))
                except ValueError as e:
                    err = {'success': False, 'error': str(e)}
                    self._send_json(err, 400)
                    self._log_resp(err)
                    return

                # Live progress for the PWA's operation modal (the request holds
                # _CARD_LOCK until the chain is done; /api/status carries this)
                _ram_progress_begin('install-cap', len(seq) + 1)
                nv_before = None
                nv_after = None
                try:
                    # RAM command format: detected per operation (a read-only
                    # probe step) unless the caller pinned compact/expanded.
                    ram_format = _ram_normalize_format(body.get('ram_format'))
                    if ram_format == 'auto':
                        _ram_progress_step(0, 'FORMAT CHECK')
                        ram_format = _ram_detect_format(self.server, scc, sp_state, state)
                    sys.stderr.write('RAM-INSTALL: %d APDUs (INSTALL / %d x LOAD / INSTALL, %s) loadfile_aid=%s\n' % (
                        len(seq), len(seq) - 2, ram_format, loadfile_aid))
                    # NV footprint: free non-volatile memory before the chain
                    # (best effort - a card without FF21 skips it).
                    nv_before = _ram_read_ff21(self.server, scc, sp_state, state,
                                               ram_format, 'NV before')

                    for apdu_idx, gp_apdu in enumerate(seq):
                        if apdu_idx == 0:
                            step_name = 'INSTALL [for load]'
                        elif apdu_idx == len(seq) - 1:
                            step_name = 'INSTALL [for install]'
                        else:
                            step_name = 'LOAD (%d/%d)' % (apdu_idx, len(seq) - 2)
                        _ram_progress_step(apdu_idx + 1, step_name)
                        if not _ram_send_gp_apdu(self.server, scc, sp_state, state,
                                                 step_name,
                                                 _ram_format_apdu(gp_apdu, ram_format)):
                            # What the failed attempt consumed (a rejected load
                            # may keep or discard its partial file - the delta
                            # tells which).
                            nv_after = _ram_read_ff21(self.server, scc, sp_state, state,
                                                      ram_format, 'NV after')
                            resp = {'success': False, 'steps': steps, 'failed_step': len(steps),
                                    'error': (state['encode_error'] or state['failure'].get('error')
                                              or ('%s failed' % step_name)),
                                    'final_cntr': state['cntr'], 'ram_format': ram_format,
                                    'load_file_aid': loadfile_aid, 'module_aid': module_aid,
                                    'load_block_size': block_size,
                                    'load_block_size_requested': block_size_req,
                                    'load_block_size_auto': not block_size_req}
                            resp.update(_ram_nv_fields(nv_before, nv_after))
                            failed_rec = steps[-1] if steps else {}
                            resp.update(_cntr_low_fields(failed_rec.get('por_status')))
                            if resp.get('cntr_low'):
                                sys.stderr.write('RAM-INSTALL: low counter - the packet '
                                                 'counter is not above the card\'s\n')
                            self._send_json(resp)
                            self._log_resp(resp)
                            return
                    # NV footprint: what the successful chain consumed.
                    nv_after = _ram_read_ff21(self.server, scc, sp_state, state,
                                              ram_format, 'NV after')
                finally:
                    _ram_progress_end()

                resp = {'success': True, 'steps': steps, 'load_file_aid': loadfile_aid,
                        'module_aid': module_aid, 'final_cntr': state['cntr'],
                        'ram_format': ram_format, 'load_block_size': block_size,
                        'load_block_size_requested': block_size_req,
                        'load_block_size_auto': not block_size_req}
                resp.update(_ram_nv_fields(nv_before, nv_after))
                sys.stderr.write('RAM-INSTALL: Complete — loadfile_aid=%s module_aid=%s cntr=%s\n' % (
                    loadfile_aid, module_aid, state['cntr']))
                self._send_json(resp)
                self._log_resp(resp)
            except Exception as e:
                sys.stderr.write('RAM-INSTALL error: %s\n' % e)
                err = {'success': False, 'error': str(e)}
                self._send_json(err, 500)
                self._log_resp(err)
        elif self.path == '/api/ram-install-app':
            # INSTALL [for install] / [for make selectable] for an already
            # loaded package (iterate the final step without re-loading).
            body = self._read_body()
            self._log_req(body)
            scc = self.server.scc
            if not scc:
                self._send_json({'error': _err('reader_not_init', lang)}, 503)
                self._log_resp({'error': _err('reader_not_init', lang)})
                return
            try:
                mode = (body.get('mode') or 'install').strip()
                loadfile_aid = (body.get('loadfile_aid') or '').replace(' ', '').upper()
                module_aid = (body.get('module_aid') or '').replace(' ', '').upper()
                instance_aid = ((body.get('instance_aid') or '').replace(' ', '').upper()
                                or module_aid)
                privileges = (body.get('privileges') or '').replace(' ', '') or '00'
                install_params_hex = (body.get('install_params') or '').replace(' ', '')
                stk_params_hex = (body.get('stk_params') or '').replace(' ', '')
                make_selectable = bool(body.get('make_selectable', True))
                if mode == 'make_selectable':
                    if not instance_aid:
                        err = {'success': False, 'error': 'instance_aid required'}
                        self._send_json(err, 400)
                        self._log_resp(err)
                        return
                    apdu = _cap_make_selectable_apdu(instance_aid, privileges)
                    step_name = 'INSTALL [for make selectable]'
                else:
                    if not loadfile_aid or not module_aid:
                        err = {'success': False,
                               'error': 'loadfile_aid and module_aid required'}
                        self._send_json(err, 400)
                        self._log_resp(err)
                        return
                    try:
                        apdu = _cap_install_apdu(
                            loadfile_aid, module_aid, instance_aid=instance_aid,
                            privileges=privileges, install_params=install_params_hex,
                            stk_params=stk_params_hex, make_selectable=make_selectable,
                            nv_quota=body.get('nv_quota'),
                            volatile_quota=body.get('volatile_quota'))
                    except ValueError as e:
                        err = {'success': False, 'error': str(e)}
                        self._send_json(err, 400)
                        self._log_resp(err)
                        return
                    step_name = 'INSTALL [for install]'
                # SCP80 params: the SPI1 comes from the MSL of the packet's
                # TAR (the operation uses the preset's table); a caller-supplied
                # value is only the fallback for a TAR the preset does not carry.
                preset = _preset_by_id(self.server, body.get('preset_id'))
                tar = str(body.get('tar') or presets.role_tar(preset, 'isd')
                          or '000000').strip().upper()
                try:
                    spi1, _ = _spi1_for_tar(preset, tar, body.get('spi1'),
                                            prefer_msl=True)
                except ValueError as e:
                    err = {'success': False, 'error': str(e)}
                    self._send_json(err, 400)
                    self._log_resp(err)
                    return
                sp_state = {
                    'spi1': spi1, 'spi2': body.get('spi2', '01'),
                    'kic': body.get('kic', '25'), 'kid': body.get('kid', '25'),
                    'tar': tar, 'cntr': body.get('cntr', '00000000'),
                    'kic_key': body.get('kicKey', ''), 'kid_key': body.get('kidKey', ''),
                    'include_cpi': body.get('includeCpi', True)}
                # The keyset number must be consistent and defined in the preset
                # (TS 102 225 A.2 / the "keyset must exist" rule) - checked
                # before the format probe consumes a counter.
                kvn, keyset_err = self._keyset_guard(body, sp_state['kic'], sp_state['kid'])
                if keyset_err:
                    err = {'success': False, 'error': keyset_err}
                    self._send_json(err, 400)
                    self._log_resp(err)
                    return
                # The counter: explicit `cntr` wins, else the preset's value
                # for this keyset (the server owns the counters, v3.20.0).
                sp_state['cntr'] = _preset_counter_seed(self.server, body, kvn) or sp_state['cntr']
                state = {'steps': [], 'encode_error': None, 'failure': {},
                         'cntr': sp_state['cntr'], 'preset_id': body.get('preset_id'),
                         'kvn': kvn}
                # Live progress for the PWA's operation modal (see /api/ram-install)
                _ram_progress_begin('install-app', 2)
                try:
                    # RAM command format: detected per operation (a read-only
                    # probe step) unless the caller pinned compact/expanded.
                    ram_format = _ram_normalize_format(body.get('ram_format'))
                    if ram_format == 'auto':
                        _ram_progress_step(0, 'FORMAT CHECK')
                        ram_format = _ram_detect_format(self.server, scc, sp_state, state)
                    apdu = _ram_format_apdu(apdu, ram_format)
                    _ram_progress_step(1, step_name)
                    sys.stderr.write('RAM-INSTALL-APP: %s (%s, %s) cntr=%s\n' % (
                        apdu, step_name, ram_format, sp_state['cntr']))
                    ok = _ram_send_gp_apdu(self.server, scc, sp_state, state, step_name, apdu)
                finally:
                    _ram_progress_end()
                resp = {'success': bool(ok), 'steps': state['steps'],
                        'final_cntr': state['cntr'], 'ram_format': ram_format}
                if not ok:
                    resp['error'] = (state['encode_error'] or state['failure'].get('error')
                                     or ('%s failed' % step_name))
                    resp['failed_step'] = len(state['steps'])
                    failed_rec = state['steps'][-1] if state['steps'] else {}
                    resp.update(_cntr_low_fields(failed_rec.get('por_status')))
                self._send_json(resp)
                self._log_resp(resp)
            except Exception as e:
                sys.stderr.write('RAM-INSTALL-APP error: %s\n' % e)
                err = {'success': False, 'error': str(e)}
                self._send_json(err, 500)
                self._log_resp(err)
        elif self.path == '/api/cap-compat':
            # CAP compatibility test (v3.6.48): run the load only as far as the
            # block that completes the Import component - the JCRE verifies the
            # import list there, so the card's answer to that block says
            # whether it can satisfy the CAP's imports.  Nothing is committed
            # (no last-block flag, no INSTALL [for install]) and the probe
            # overrides/additions are applied to the load file first.
            body = self._read_body()
            self._log_req(body)
            scc = self.server.scc
            if not scc:
                self._send_json({'error': _err('reader_not_init', lang)}, 503)
                self._log_resp({'error': _err('reader_not_init', lang)})
                return
            try:
                cap_hex = body.get('cap_hex', '').replace(' ', '')
                if not cap_hex:
                    self._send_json({'error': 'No cap_hex provided'}, 400)
                    return
                loadfile_aid, module_aid, loadfile_data = _cap_parse(cap_hex)

                probe_applied = None
                probe_unmatched = []
                probe = body.get('probe_imports') or {}
                additions = body.get('probe_additions') or []
                if probe or additions:
                    original_aids = set(_cap_import_aids(loadfile_data))
                    try:
                        loadfile_data, probe_applied = _probe_import_versions(
                            loadfile_data, probe, additions)
                    except ValueError as e:
                        resp = {'error': 'import probe: %s' % e}
                        self._send_json(resp, 400)
                        self._log_resp(resp)
                        return
                    known = original_aids | {
                        str(a.get('aid', '')).replace(' ', '').upper()
                        for a in additions}
                    probe_unmatched = _probe_unmatched(probe, known)
                    sys.stderr.write('CAP-COMPAT: import probe applied: %s%s\n' % (
                        json.dumps(probe_applied),
                        (' (unmatched: %s)' % ', '.join(e['aid'] for e in probe_unmatched))
                        if probe_unmatched else ''))

                block_size_req = body.get('load_block_size')
                if block_size_req in (None, ''):
                    block_size_req = None
                else:
                    try:
                        block_size_req = int(block_size_req)
                    except (TypeError, ValueError):
                        block_size_req = -1
                    if not 1 <= block_size_req <= 240:
                        err = {'success': False, 'error': 'load_block_size must be 1..240'}
                        self._send_json(err, 400)
                        self._log_resp(err)
                        return
                block_size = block_size_req or 240
                boundary, total_blocks = _cap_compat_blocks(loadfile_data, block_size)
                sys.stderr.write('CAP-COMPAT: %s — the import list ends in LOAD %d/%d '
                                 '(block size %d)\n' % (loadfile_aid, boundary,
                                                        total_blocks, block_size))

                preset = _preset_by_id(self.server, body.get('preset_id'))
                tar = str(body.get('tar') or presets.role_tar(preset, 'isd')
                          or '000000').strip().upper()
                try:
                    spi1, _ = _spi1_for_tar(preset, tar, body.get('spi1'),
                                            prefer_msl=True)
                except ValueError as e:
                    err = {'success': False, 'error': str(e)}
                    self._send_json(err, 400)
                    self._log_resp(err)
                    return
                spi2 = body.get('spi2', '01')
                kic = body.get('kic', '25')
                kid = body.get('kid', '25')
                cntr = body.get('cntr', '00000000')
                kic_key = body.get('kicKey', '')
                kid_key = body.get('kidKey', '')
                steps = []
                kvn, keyset_err = self._keyset_guard(body, kic, kid)
                if keyset_err:
                    err = {'success': False, 'error': keyset_err}
                    self._send_json(err, 400)
                    self._log_resp(err)
                    return
                # The counter: explicit `cntr` wins, else the preset's value
                # for this keyset (the server owns the counters, v3.20.0).
                cntr = _preset_counter_seed(self.server, body, kvn) or cntr
                state = {'steps': steps, 'encode_error': None, 'failure': {},
                         'cntr': cntr, 'preset_id': body.get('preset_id'),
                         'kvn': kvn}
                sp_state = {'spi1': spi1, 'spi2': spi2, 'kic': kic, 'kid': kid,
                            'tar': tar, 'kic_key': kic_key, 'kid_key': kid_key,
                            'include_cpi': body.get('includeCpi', True)}

                seq = _cap_apdu_sequence(loadfile_aid, module_aid, loadfile_data,
                                         block_size=block_size)
                run = seq[:1 + boundary]     # INSTALL [for load] + boundary blocks
                _ram_progress_begin('cap-compat', len(run) + 1)
                try:
                    ram_format = _ram_normalize_format(body.get('ram_format'))
                    if ram_format == 'auto':
                        _ram_progress_step(0, 'FORMAT CHECK')
                        ram_format = _ram_detect_format(self.server, scc, sp_state, state)
                    ok = True
                    for apdu_idx, gp_apdu in enumerate(run):
                        step_name = ('INSTALL [for load]' if apdu_idx == 0
                                     else 'LOAD (%d/%d)' % (apdu_idx, total_blocks))
                        _ram_progress_step(apdu_idx + 1, step_name)
                        if not _ram_send_gp_apdu(self.server, scc, sp_state, state,
                                                 step_name,
                                                 _ram_format_apdu(gp_apdu, ram_format)):
                            ok = False
                            break
                finally:
                    _ram_progress_end()
                resp = {'success': ok, 'imports_ok': ok, 'steps': steps,
                        'boundary_block': boundary, 'total_blocks': total_blocks,
                        'load_file_aid': loadfile_aid, 'module_aid': module_aid,
                        'ram_format': ram_format, 'probe_imports': probe_applied,
                        'probe_unmatched': probe_unmatched,
                        'final_cntr': state['cntr'], 'load_block_size': block_size,
                        'load_block_size_requested': block_size_req}
                if not ok:
                    resp['error'] = (state['encode_error'] or state['failure'].get('error')
                                     or 'the card rejected the import list')
                    resp['failed_step'] = len(steps)
                    failed_rec = steps[-1] if steps else {}
                    resp.update(_cntr_low_fields(failed_rec.get('por_status')))
                sys.stderr.write('CAP-COMPAT: %s — imports %s (LOADS to %d/%d)\n' % (
                    loadfile_aid, 'OK' if ok else 'REJECTED', boundary, total_blocks))
                self._send_json(resp)
                self._log_resp(resp)
            except Exception as e:
                sys.stderr.write('CAP-COMPAT error: %s\n' % e)
                err = {'success': False, 'error': str(e)}
                self._send_json(err, 500)
                self._log_resp(err)
        elif self.path == '/api/scp81/bip':
            body = self._read_body()
            # Never log the pre-shared keys.
            self._log_req(_redact_psk_fields(body))
            try:
                resp = _scp81_bip_control(body)
            except Exception as e:
                resp = {'ok': False, 'error': str(e)}
            self._send_json(resp)
            self._log_resp(resp)
        elif self.path == '/api/bip/control':
            body = self._read_body()
            self._log_req(body)
            try:
                resp = _bip_control(body)
            except Exception as e:
                resp = {'ok': False, 'error': str(e)}
            self._send_json(resp)
            self._log_resp(resp)
        elif self.path == '/api/scp81/psk-map':
            body = self._read_body()
            self._log_req(_redact_psk_fields(body))
            try:
                resp = _scp81_update_psk_map(body)
            except Exception as e:
                resp = {'ok': False, 'error': str(e)}
            self._send_json(resp)
            self._log_resp(resp)
        elif self.path == '/api/scp81/queue':
            body = self._read_body()
            self._log_req(body)
            apdus = body.get('apdus') or ([body.get('apdu')] if body.get('apdu') else [])
            apdus = [a for a in apdus if a]
            if not apdus:
                resp = {'ok': False, 'error': 'no apdus given'}
            elif _BIP_LISTENER is None:
                resp = {'ok': False, 'error': 'SCP81 listener is not running'}
            else:
                queued = _scp81_queue_script(
                    apdus, kind=body.get('kind') or 'custom',
                    force=bool(body.get('force', False)))
                resp = dict(queued, ok=bool(queued.get('queued')))
                if queued.get('queued'):
                    resp['note'] = ('queued as the SCP81 command script; '
                                    'runs on the card next POST (push/trigger)')
            self._send_json(resp)
            self._log_resp(resp)
        elif self.path == '/api/scp81/gen-install':
            body = self._read_body()
            self._log_req(body)
            try:
                resp = _scp81_gen_install(body)
            except Exception as e:
                resp = {'ok': False, 'error': str(e)}
            self._send_json(resp)
            self._log_resp(resp)
        elif self.path == '/api/cap-info':
            body = self._read_body()
            self._log_req(body)
            try:
                resp = _cap_info_body(body)
            except Exception as e:
                resp = {'ok': False, 'error': str(e)}
            self._send_json(resp)
            self._log_resp(resp)
        elif self.path in ('/api/scp81/log-clear', '/api/bip/log-clear'):
            body = self._read_body()
            self._log_req(body)
            _BIP.clear_log()
            resp = {'ok': True, 'seq': _BIP.seq}
            self._send_json(resp)
            self._log_resp(resp)
        elif self.path == '/api/test/scripts':
            body = self._read_body()
            self._log_req(body)
            store = self._test_scripts_store_or_503()
            if store is None:
                return
            suites = self._test_suites_store_or_503()
            if suites is None:
                return
            script = body.get('script') if isinstance(body.get('script'), dict) else body
            suite_id = str((script or {}).get('suite_id') or '').strip().lower()
            suite = suites.get(suite_id) if suite_id else None
            if suite is None:
                resp = {'error': 'a test script belongs to a suite: pass the '
                                 'suite_id of an existing suite'}
                self._send_json(resp, 400)
                self._log_resp(resp)
                return
            try:
                entry = store.add(script)
            except test_scripts.TestScriptError as e:
                resp = {'error': str(e)}
                self._send_json(resp, 400)
                self._log_resp(resp)
                return
            except OSError as e:
                self._test_scripts_store_error(e)
                return
            # The suite owns the member list (order, role, on_fail): attach.
            try:
                entries = list(suite['scripts']) + [
                    {'script_id': entry['id'], 'role': 'member', 'on_fail': 'stop'}]
                suites.update(suite['id'], {'scripts': entries})
            except (test_suites.TestSuiteError, OSError) as e:
                sys.stderr.write('TESTSUITES: cannot attach %s to %s: %s\n'
                                 % (entry['id'], suite['id'], e))
                self._test_suites_store_error(e)
                return
            resp = {'ok': True, 'script': entry}
            self._send_json(resp)
            self._log_resp({'ok': True, 'id': entry['id'], 'name': entry['name']})
        elif self.path == '/api/test/scripts/update':
            body = self._read_body()
            self._log_req(body)
            store = self._test_scripts_store_or_503()
            if store is None:
                return
            suites = self._test_suites_store_or_503()
            if suites is None:
                return
            fields = body.get('script') if isinstance(body.get('script'), dict) else body
            sid = body.get('id')
            cur = store.get(sid)
            if cur is None:
                resp = {'error': 'unknown test script id'}
                self._send_json(resp, 404)
                self._log_resp(resp)
                return
            new_suite = fields.get('suite_id') if isinstance(fields, dict) else None
            if new_suite not in (None, '', cur.get('suite_id')):
                # an ownership change is the move operation: it must update
                # both suites in the same request
                entry, err, status = _test_script_move(store, suites, cur, new_suite)
                if entry is None:
                    resp = {'error': err}
                    self._send_json(resp, status)
                    self._log_resp(resp)
                    return
                resp = {'ok': True, 'script': entry, 'moved': True}
                self._send_json(resp)
                self._log_resp({'ok': True, 'id': entry['id'], 'name': entry['name'],
                                'moved': True})
                return
            fields = {k: v for k, v in (fields or {}).items() if k != 'suite_id'}
            try:
                entry = store.update(sid, fields)
            except test_scripts.TestScriptError as e:
                resp = {'error': str(e)}
                self._send_json(resp, 400)
                self._log_resp(resp)
                return
            except OSError as e:
                self._test_scripts_store_error(e)
                return
            if entry is None:
                resp = {'error': 'unknown test script id'}
                self._send_json(resp, 404)
                self._log_resp(resp)
                return
            resp = {'ok': True, 'script': entry}
            self._send_json(resp)
            self._log_resp({'ok': True, 'id': entry['id'], 'name': entry['name']})
        elif self.path == '/api/test/scripts/delete':
            body = self._read_body()
            self._log_req(body)
            store = self._test_scripts_store_or_503()
            if store is None:
                return
            suites = self._test_suites_store_or_503()
            if suites is None:
                return
            cur = store.get(body.get('id'))
            if cur is not None:
                # Detach from the owning suite first: the suite's member list
                # is the only reference, so a removal leaves no dangling id.
                _test_script_detach(suites, cur)
            try:
                removed = store.remove(body.get('id'))
            except OSError as e:
                self._test_scripts_store_error(e)
                return
            resp = {'ok': True, 'removed': removed}
            self._send_json(resp)
            self._log_resp(resp)
        elif self.path == '/api/test/scripts/move':
            body = self._read_body()
            self._log_req(body)
            store = self._test_scripts_store_or_503()
            if store is None:
                return
            suites = self._test_suites_store_or_503()
            if suites is None:
                return
            cur = store.get(body.get('id'))
            if cur is None:
                resp = {'error': 'unknown test script id'}
                self._send_json(resp, 404)
                self._log_resp(resp)
                return
            entry, err, status = _test_script_move(store, suites, cur,
                                                        body.get('suite_id'))
            if entry is None:
                resp = {'error': err}
                self._send_json(resp, status)
                self._log_resp(resp)
                return
            resp = {'ok': True, 'script': entry}
            self._send_json(resp)
            self._log_resp({'ok': True, 'id': entry['id'], 'name': entry['name'],
                            'suite_id': entry['suite_id']})
        elif self.path == '/api/test/scripts/copy':
            body = self._read_body()
            self._log_req(body)
            store = self._test_scripts_store_or_503()
            if store is None:
                return
            suites = self._test_suites_store_or_503()
            if suites is None:
                return
            cur = store.get(body.get('id'))
            if cur is None:
                resp = {'error': 'unknown test script id'}
                self._send_json(resp, 404)
                self._log_resp(resp)
                return
            target = suites.get(body.get('suite_id'))
            if target is None:
                resp = {'error': 'unknown target suite'}
                self._send_json(resp, 400)
                self._log_resp(resp)
                return
            name = str(body.get('name') or '').strip() or (cur['name'] + ' (copy)')
            try:
                entry = store.add({'name': name, 'suite_id': target['id'],
                                   'require_adm': cur.get('require_adm', False),
                                   'steps': copy.deepcopy(cur['steps'])})
            except test_scripts.TestScriptError as e:
                resp = {'error': str(e)}
                self._send_json(resp, 400)
                self._log_resp(resp)
                return
            except OSError as e:
                self._test_scripts_store_error(e)
                return
            try:
                entries = list(target['scripts']) + [
                    {'script_id': entry['id'], 'role': 'member', 'on_fail': 'stop'}]
                suites.update(target['id'], {'scripts': entries})
            except (test_suites.TestSuiteError, OSError) as e:
                sys.stderr.write('TESTSUITES: cannot attach copy %s: %s\n'
                                 % (entry['id'], e))
                self._test_suites_store_error(e)
                return
            resp = {'ok': True, 'script': entry}
            self._send_json(resp)
            self._log_resp({'ok': True, 'id': entry['id'], 'name': entry['name'],
                            'copied_from': cur['id']})
        elif self.path == '/api/test/suites':
            body = self._read_body()
            self._log_req(body)
            store = self._test_suites_store_or_503()
            if store is None:
                return
            scripts = self._test_scripts_store_or_503()
            if scripts is None:
                return
            suite = body.get('suite') if isinstance(body.get('suite'), dict) else body
            err = _test_suite_refs_error(scripts, suite)
            if err:
                resp = {'error': err}
                self._send_json(resp, 400)
                self._log_resp(resp)
                return
            try:
                entry = store.add(suite)
            except test_suites.TestSuiteError as e:
                resp = {'error': str(e)}
                self._send_json(resp, 400)
                self._log_resp(resp)
                return
            except OSError as e:
                self._test_suites_store_error(e)
                return
            resp = {'ok': True, 'suite': entry}
            self._send_json(resp)
            self._log_resp({'ok': True, 'id': entry['id'], 'name': entry['name']})
        elif self.path == '/api/test/suites/update':
            body = self._read_body()
            self._log_req(body)
            store = self._test_suites_store_or_503()
            if store is None:
                return
            scripts = self._test_scripts_store_or_503()
            if scripts is None:
                return
            fields = body.get('suite') if isinstance(body.get('suite'), dict) else body
            cur = store.get(body.get('id'))
            if cur is None:
                resp = {'error': 'unknown test suite id'}
                self._send_json(resp, 404)
                self._log_resp(resp)
                return
            if 'scripts' in (fields or {}):
                old_ids = [e['script_id'] for e in cur['scripts']]
                new = fields['scripts'] if isinstance(fields['scripts'], list) else []
                new_ids = [str((e or {}).get('script_id') or '').strip().lower()
                           for e in new if isinstance(e, dict)]
                if set(new_ids) != set(old_ids):
                    # The member set changes through the script endpoints
                    # (add/move/delete) - with one repair exception: a
                    # reference whose script is already gone (the store keeps
                    # such a suite servable) can be pruned here, since no
                    # script endpoint can reach it any more.
                    removed = set(old_ids) - set(new_ids)
                    added = set(new_ids) - set(old_ids)
                    if added or any(scripts.get(sid) is not None for sid in removed):
                        resp = {'error': 'the member set changes through the script '
                                         'endpoints (add/move/delete), not by '
                                         'editing the suite'}
                        self._send_json(resp, 400)
                        self._log_resp(resp)
                        return
            err = _test_suite_refs_error(scripts, fields)
            if err:
                resp = {'error': err}
                self._send_json(resp, 400)
                self._log_resp(resp)
                return
            fields = {k: v for k, v in (fields or {}).items()}
            try:
                entry = store.update(body.get('id'), fields)
            except test_suites.TestSuiteError as e:
                resp = {'error': str(e)}
                self._send_json(resp, 400)
                self._log_resp(resp)
                return
            except OSError as e:
                self._test_suites_store_error(e)
                return
            if entry is None:
                resp = {'error': 'unknown test suite id'}
                self._send_json(resp, 404)
                self._log_resp(resp)
                return
            resp = {'ok': True, 'suite': entry}
            self._send_json(resp)
            self._log_resp({'ok': True, 'id': entry['id'], 'name': entry['name']})
        elif self.path == '/api/test/suites/delete':
            body = self._read_body()
            self._log_req(body)
            store = self._test_suites_store_or_503()
            if store is None:
                return
            cur = store.get(body.get('id'))
            if cur is not None and cur['scripts']:
                resp = {'error': 'the suite still holds %d script(s): move or '
                                 'delete them first' % len(cur['scripts'])}
                self._send_json(resp, 400)
                self._log_resp(resp)
                return
            try:
                removed = store.remove(body.get('id'))
            except OSError as e:
                self._test_suites_store_error(e)
                return
            resp = {'ok': True, 'removed': removed}
            self._send_json(resp)
            self._log_resp(resp)
        elif self.path == '/api/test/suites/import':
            body = self._read_body()
            self._log_req(body)
            store = self._test_suites_store_or_503()
            if store is None:
                return
            scripts = self._test_scripts_store_or_503()
            if scripts is None:
                return
            suites_in = body.get('suites')
            if not isinstance(suites_in, list) or len(suites_in) > 500:
                resp = {'error': 'suites must be a list of at most 500 entries'}
                self._send_json(resp, 400)
                self._log_resp(resp)
                return
            scripts_in = body.get('scripts')
            if scripts_in is not None and (not isinstance(scripts_in, list)
                                           or len(scripts_in) > 1000):
                resp = {'error': 'scripts must be a list of at most 1000 entries'}
                self._send_json(resp, 400)
                self._log_resp(resp)
                return
            mode = body.get('mode') if body.get('mode') in ('merge', 'replace') else 'merge'
            # Bundle import: scripts first (merge always - a replace would wipe
            # every other suite's scripts), then the suites with references
            # rewritten through the script id map.
            script_result = {'added': 0, 'skipped': 0, 'errors': [], 'id_map': {}}
            if scripts_in:
                try:
                    script_result = scripts.import_scripts(scripts_in, 'merge')
                except OSError as e:
                    self._test_scripts_store_error(e)
                    return
            id_map = script_result.get('id_map') or {}
            mapped, ref_errors = [], []
            for i, suite in enumerate(suites_in):
                if not isinstance(suite, dict):
                    continue
                entry = copy.deepcopy(suite)
                for e in entry.get('scripts') or []:
                    if isinstance(e, dict):
                        sid = str(e.get('script_id') or '').strip().lower()
                        e['script_id'] = id_map.get(sid, sid)
                missing = [e['script_id'] for e in (entry.get('scripts') or [])
                           if isinstance(e, dict) and not scripts.get(e['script_id'])]
                if missing:
                    ref_errors.append('%s: unknown script(s) %s'
                                      % (entry.get('name') or ('#' + str(i + 1)),
                                         ', '.join(missing[:3])))
                    continue
                mapped.append(entry)
            try:
                resp = store.import_suites(mapped, mode)
            except OSError as e:
                self._test_suites_store_error(e)
                return
            # The ownership invariant: the imported scripts carry the *old*
            # suite id, and a `replace` wiped the suites that owned the rest -
            # the reconciliation re-points the listed scripts and adopts the
            # newly unreferenced ones into "Imported scripts".
            reconcile_scripts_to_suites(scripts, store)
            resp['scripts'] = {'added': script_result.get('added', 0),
                               'skipped': script_result.get('skipped', 0)}
            resp['skipped'] = resp.get('skipped', 0) + len(ref_errors)
            resp['errors'] = (resp.get('errors', []) + ref_errors)[:10]
            resp['ok'] = True
            self._send_json(resp)
            self._log_resp(resp)
        elif self.path == '/api/test/scripts/import':
            body = self._read_body()
            self._log_req(body)
            store = self._test_scripts_store_or_503()
            if store is None:
                return
            suites = self._test_suites_store_or_503()
            if suites is None:
                return
            items = body.get('scripts')
            if not isinstance(items, list) or len(items) > 1000:
                resp = {'error': 'scripts must be a list of at most 1000 entries'}
                self._send_json(resp, 400)
                self._log_resp(resp)
                return
            mode = body.get('mode') if body.get('mode') in ('merge', 'replace') else 'merge'
            if mode == 'replace':
                # A wipe would leave every suite's member list dangling: the
                # scripts are the references' targets.  Merge is the normal
                # path; delete the suites' scripts first if a replace is
                # really wanted.
                holders = [s for s in suites.list() if s['scripts']]
                if holders:
                    resp = {'error': 'replace would leave %d suite(s) with '
                                     'dangling script references - delete their '
                                     'scripts first, or import with merge'
                                     % len(holders)}
                    self._send_json(resp, 400)
                    self._log_resp(resp)
                    return
            target = suites.get(body.get('suite_id')) if body.get('suite_id') else None
            if body.get('suite_id') and target is None:
                resp = {'error': 'unknown target suite'}
                self._send_json(resp, 400)
                self._log_resp(resp)
                return
            try:
                resp = store.import_scripts(items, mode,
                                            suite_id=target['id'] if target else None)
            except OSError as e:
                self._test_scripts_store_error(e)
                return
            if target is not None and resp.get('added_ids'):
                try:
                    entries = list(target['scripts']) + [
                        {'script_id': sid, 'role': 'member', 'on_fail': 'stop'}
                        for sid in resp['added_ids']]
                    suites.update(target['id'], {'scripts': entries})
                except (test_suites.TestSuiteError, OSError) as e:
                    self._test_suites_store_error(e)
                    return
            resp.pop('added_ids', None)
            resp['ok'] = True
            self._send_json(resp)
            self._log_resp(resp)
        elif self.path == '/api/test/run':
            body = self._read_body()
            self._log_req(body)
            if _TEST_RUNNING:
                resp = {'error': 'a test script is already running'}
                self._send_json(resp, 409)
                self._log_resp(resp)
                return
            suite_id = str(body.get('suite_id') or '').strip().lower()
            if suite_id:
                self._test_suite_run_request(suite_id, body)
                return
            script_raw, script_id, script_err = _test_run_script_from_body(self.server, body)
            if script_err:
                resp = {'error': script_err}
                self._send_json(resp, 404)
                self._log_resp(resp)
                return
            try:
                script = testscript.normalise_script(script_raw, _test_command_type)
            except testscript.ScriptError as e:
                resp = {'error': str(e)}
                self._send_json(resp, 400)
                self._log_resp(resp)
                return
            preset, preset_err = _test_run_preset_from_body(self.server, body)
            if preset_err:
                resp = {'error': preset_err}
                self._send_json(resp, 400)
                self._log_resp(resp)
                return
            needs_scp80 = any(s['type'] == 'action' and s['kind'] == 'scp80'
                              for s in script['steps'])
            err = _test_preset_error(script, preset) if needs_scp80 else None
            if err:
                resp = {'error': err}
                self._send_json(resp, 400)
                self._log_resp(resp)
                return
            # The ADM prerequisite: a script may declare that it needs the ADM
            # PIN (protected files).  Verified for this session passes; a
            # failure is never retried automatically.
            needed_adm = bool(script_raw.get('require_adm'))
            ok, adm_info, adm_err = _ensure_adm(self.server, preset, needed_adm)
            if not ok:
                resp = {'error': adm_err, 'adm': adm_info}
                self._send_json(resp, 400)
                self._log_resp(resp)
                return
            try:
                _test_run_start(self.server, script, preset, script_id or None,
                                adm_info=adm_info)
            except Exception as e:
                resp = {'error': 'could not start the test run: %s' % e}
                self._send_json(resp, 500)
                self._log_resp(resp)
                return
            resp = _test_state_snapshot()
            self._send_json(resp)
            self._log_resp(resp)
        elif self.path == '/api/test/stop':
            self._log_req()
            with _TEST_LOCK:
                if _TEST_RUN['running']:
                    _TEST_RUN['stop'] = True
            resp = _test_state_snapshot()
            self._send_json(resp)
            self._log_resp(resp)
        elif self.path == '/api/test/clear':
            self._log_req()
            with _TEST_LOCK:
                busy = bool(_TEST_RUN['running'])
                if not busy:
                    _TEST_RUN.update({
                        'running': False, 'stop': False, 'name': None, 'status': None,
                        'session': None, 'index': 0, 'total': 0, 'steps': [],
                        'preset': None, 'scp80_counter': None,
                        'scp80_counters': {},
                        'started': None, 'finished': None, 'error': None,
                        'suite': None, 'log': [], 'log_prefix': '', 'adm': None,
                    })
            if busy:
                resp = {'error': 'test script is running'}
                self._send_json(resp, 409)
            else:
                resp = _test_state_snapshot()
                self._send_json(resp)
            self._log_resp(resp)
        elif self.path == '/api/tar-probe':
            app = self.server.app
            scc = self.server.scc
            if not app or not scc:
                self._send_json({'error': _err('reader_not_init', lang)}, 503)
                self._log_resp({'error': _err('reader_not_init', lang)})
                return
            body = self._read_body()
            self._log_req(body)
            kic = body.get('kic', '')
            kid = body.get('kid', '')
            kvn, keyset_err = self._keyset_guard(body, kic, kid)
            if keyset_err:
                err = {'success': False, 'error': keyset_err}
                self._send_json(err, 400)
                self._log_resp(err)
                return
            # The probe sends real secured packets: it needs a counter check
            # (every accepted packet advances and is persisted) and a PoR.  Each
            # TAR uses its own MSL when the preset carries it; the caller's SPI1
            # is the fallback for the arbitrary TARs the checklist probes.
            spi1_base = str(body.get('spi1') or '').strip().upper()
            spi2 = str(body.get('spi2') or '01').strip().upper()
            preset = _preset_by_id(self.server, body.get('preset_id'))
            apdu = (body.get('apdu') or TAR_PROBE_DEFAULT_APDU).replace(' ', '').upper()
            labels = {tar: label for tar, label in TAR_PROBE_TARS}
            wanted = body.get('tars')
            if isinstance(wanted, list) and wanted:
                entries = []
                for t in wanted:
                    tar = str(t).replace(' ', '').upper()
                    entries.append((tar, labels.get(tar, '')))
            else:
                entries = list(TAR_PROBE_TARS)
            # Resolve every TAR's SPI1 before the first packet is sent: a
            # missing MSL must refuse the whole probe, not leave it half-run.
            resolved = []
            for tar, label in entries:
                try:
                    tar_spi1, _ = _spi1_for_tar(preset, tar, spi1_base,
                                                prefer_msl=True)
                except ValueError as e:
                    err = {'success': False, 'error': str(e)}
                    self._send_json(err, 400)
                    self._log_resp(err)
                    return
                if not _counter_tracked(tar_spi1):
                    err = {'success': False,
                           'error': 'the TAR probe needs a counter check - SPI1 %s '
                                    'for TAR %s has none (b5b4 = 00)'
                                    % (tar_spi1, tar)}
                    self._send_json(err, 400)
                    self._log_resp(err)
                    return
                resolved.append((tar, label, tar_spi1))
            state = {'steps': [], 'encode_error': None, 'failure': {},
                     # the counter: explicit `cntr` wins, else the preset's
                     # value for this keyset (the server owns the counters,
                     # v3.20.0)
                     'cntr': _preset_counter_seed(self.server, body, kvn) or '00000000',
                     'preset_id': body.get('preset_id'), 'kvn': kvn}
            sp = {'spi1': spi1_base, 'spi2': spi2, 'kic': kic, 'kid': kid, 'tar': '000000',
                  'kic_key': body.get('kicKey', ''), 'kid_key': body.get('kidKey', ''),
                  'include_cpi': body.get('includeCpi', True)}
            sys.stderr.write('TAR-PROBE: %d TAR(s), APDU %s, SPI %s/%s, kvn=%s\n'
                             % (len(entries), apdu, spi1_base or '-', spi2, kvn))
            _ram_progress_begin('tar-probe', len(entries))
            try:
                for index, (tar, _label, tar_spi1) in enumerate(resolved):
                    _ram_progress_step(index, 'TAR ' + tar)
                    sp['tar'] = tar
                    sp['spi1'] = tar_spi1
                    _ram_send_gp_apdu(self.server, scc, sp, state, 'TAR ' + tar, apdu)
            finally:
                _ram_progress_end()
            results = []
            for (tar, label, tar_spi1), step in zip(resolved, state['steps']):
                results.append({
                    'tar': tar, 'label': label, 'spi1': tar_spi1,
                    'verdict': _tar_probe_verdict(step),
                    'sw': step.get('sw'), 'por_status': step.get('por_status'),
                    'por_sw': step.get('por_sw'), 'por_data': step.get('por_data'),
                    'bytes': step.get('bytes'), 'segments': step.get('segments'),
                })
            registered = sum(1 for r in results if r['verdict'] == 'registered')
            resp = {'success': True, 'results': results, 'steps': state['steps'],
                    'registered': registered, 'total': len(results),
                    'final_cntr': state['cntr'], 'kvn': kvn,
                    'spi1': spi1_base, 'spi2': spi2, 'apdu': apdu}
            self._send_json(resp)
            self._log_resp(resp)
        elif self.path == '/api/counter-probe':
            scc = self.server.scc
            if not scc:
                self._send_json({'error': _err('reader_not_init', lang)}, 503)
                self._log_resp({'error': _err('reader_not_init', lang)})
                return
            body = self._read_body()
            self._log_req(body)
            if not body.get('preset_id'):
                resp = {'success': False, 'error': 'preset_id is required'}
                self._send_json(resp, 400)
                self._log_resp(resp)
                return
            store = self._preset_store_or_503()
            if store is None:
                return
            preset = store.get(body.get('preset_id'))
            if not preset:
                resp = {'success': False, 'error': 'unknown preset'}
                self._send_json(resp, 400)
                self._log_resp(resp)
                return
            sys.stderr.write('COUNTER-PROBE: preset %s, start %s, ceiling %s\n'
                             % (preset.get('name'), body.get('cntr', ''),
                                body.get('ceiling', COUNTER_PROBE_CEILING)))
            try:
                resp = _counter_probe(self.server, scc, preset, body)
            except ValueError as e:
                resp = {'success': False, 'error': str(e)}
                self._send_json(resp, 400)
                self._log_resp(resp)
                return
            self._send_json(resp)
            self._log_resp(resp)
        elif self.path == '/api/presets':
            body = self._read_body()
            self._log_req(body)
            store = self._preset_store_or_503()
            if store is None:
                return
            try:
                preset = store.add(body)
            except presets.PresetError as e:
                resp = {'error': str(e)}
                self._send_json(resp, 400)
                self._log_resp(resp)
                return
            except OSError as e:
                self._preset_store_error(e)
                return
            resp = {'ok': True, 'preset': preset}
            self._send_json(resp)
            self._log_resp(resp)
        elif self.path == '/api/presets/update':
            body = self._read_body()
            self._log_req(body)
            store = self._preset_store_or_503()
            if store is None:
                return
            fields = body.get('fields')
            if not isinstance(fields, dict):
                fields = {k: v for k, v in body.items() if k not in ('id', 'fields')}
            try:
                preset = store.update(body.get('id'), fields)
            except presets.PresetError as e:
                resp = {'error': str(e)}
                self._send_json(resp, 400)
                self._log_resp(resp)
                return
            except OSError as e:
                self._preset_store_error(e)
                return
            if preset is None:
                resp = {'error': 'unknown preset id'}
                self._send_json(resp, 404)
                self._log_resp(resp)
                return
            resp = {'ok': True, 'preset': preset}
            self._send_json(resp)
            self._log_resp(resp)
        elif self.path == '/api/presets/delete':
            body = self._read_body()
            self._log_req(body)
            store = self._preset_store_or_503()
            if store is None:
                return
            try:
                removed = store.remove(body.get('id'))
            except OSError as e:
                self._preset_store_error(e)
                return
            resp = {'ok': True, 'removed': removed}
            self._send_json(resp)
            self._log_resp(resp)
        elif self.path == '/api/presets/import':
            body = self._read_body()
            self._log_req(body)
            store = self._preset_store_or_503()
            if store is None:
                return
            items = body.get('presets')
            if not isinstance(items, list) or len(items) > 1000:
                resp = {'error': 'presets must be a list of at most 1000 entries'}
                self._send_json(resp, 400)
                self._log_resp(resp)
                return
            mode = body.get('mode') if body.get('mode') in ('merge', 'replace') else 'merge'
            try:
                resp = store.import_presets(items, mode)
            except OSError as e:
                self._preset_store_error(e)
                return
            resp['ok'] = True
            self._send_json(resp)
            self._log_resp(resp)
        else:
            self._send_json({'error': _err('not_found', lang)}, 404)
            self._log_resp({'error': _err('not_found', lang)})

    def log_message(self, format, *args):
        sys.stderr.write('%s - - [%s] %s\n' % (self.client_address[0], self.log_date_time_string(), format % args))