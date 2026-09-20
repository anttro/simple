import json
import random
import sys
import os
import time
import threading
import traceback
import re
import codecs
from urllib.parse import unquote_plus
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from io import StringIO
from pySim.transport import ApduTracer, ProactiveHandler
from pySim.cards import UiccCardBase
from pysim_simple_server import httpota
from pysim_simple_server import netsim
from pysim_simple_server import netstate
from pysim_simple_server import scp81
from smartcard.CardMonitoring import CardMonitor, CardObserver

import gsm0338  # registers 'gsm03.38' codec
from construct import GreedyBytes
from osmocom.construct import GsmOrUcs2Adapter
from osmocom.tlv import BER_TLV_IE
from osmocom.utils import rpad


VERSION = '2.7.9'

MAX_ENVELOPE_SEGMENTS = 5  # max SMS segments for outgoing C-APDU in ENVELOPE


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
    },
    'ru': {
        'app_not_init': 'Сервер не инициализирован',
        'no_card_state': 'Состояние карты недоступно',
        'reader_not_init': 'Считыватель не инициализирован',
        'not_found': 'Не найдено',
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


def _cap_apdu_sequence(loadfile_aid, module_aid, loadfile_data, sd_aid='',
                       privileges='00', install_params='', stk_params='',
                       make_selectable=True, block_size=240, instance_aid=None):
    """RAM (GP) APDU sequence for a parsed .cap: INSTALL [for load], LOAD
    blocks (240-byte payloads, block counter in P2, last block P1=0x80),
    INSTALL [for install]. Shared by the SCP80 delivery path and the SCP81
    command-script path; keep byte-compatible with /api/ram-install."""
    sd = sd_aid or 'A000000003000000'
    ifl_data = _lv(loadfile_aid) + _lv(sd) + '00' + '00' + '00'
    apdus = ['80E60200%02X%s00' % (len(ifl_data) // 2, ifl_data)]
    loadfile_tlv = 'C4' + _ber_len(len(loadfile_data) // 2) + loadfile_data
    # Split the TLV into consecutive 240-byte blocks (char offsets, 2 per
    # byte). The earlier form indexed with the block number ('i * 2'), which
    # produced overlapping 1-byte-shifted copies - the card failed mid-load
    # with SW 6400 (live 2026-09-16).
    blocks = [loadfile_tlv[off:off + block_size * 2]
              for off in range(0, len(loadfile_tlv), block_size * 2)]
    for i, block in enumerate(blocks):
        p1 = 0x80 if i == len(blocks) - 1 else 0x00
        apdus.append('80E8%02X%02X%02X%s00' % (p1, i % 256, len(block) // 2, block))
    instance = instance_aid or module_aid
    params = install_params if install_params else 'C900'
    if stk_params:
        params += stk_params
    p1_install = 0x0C if make_selectable else 0x04
    ifi_data = (_lv(loadfile_aid) + _lv(module_aid) + _lv(instance) +
                _lv(privileges or '00') + _lv(params) + '00')
    apdus.append('80E6%02X00%02X%s00' % (p1_install, len(ifi_data) // 2, ifi_data))
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


def _build_sms_tpdu(chunk_hex, chunk_total=1, chunk_num=1, oa_number='12345', include_cpi=True):
    chunk = bytes.fromhex(chunk_hex)
    # TS 23.040 UDH: first octet is UDHL, then the information elements.
    # TS 31.115 4.2/4.3: the OTA CPI is UDH IEIa='70' with IEIDLa='00'.
    udh = b''
    if chunk_total > 1:
        udh = bytes([0x00, 0x03, 0x01, chunk_total, chunk_num])
        if chunk_num == 1 and include_cpi:
            udh += bytes([0x70, 0x00])
    elif include_cpi:
        udh = bytes([0x70, 0x00])
    tp_ud = (bytes([len(udh)]) + udh + chunk) if udh else chunk
    first_byte = 0x44 if udh and chunk_total > 1 else (0x40 if udh else 0x04)
    tpdu = bytes([first_byte]) + _encode_sms_oa(oa_number) + bytes([0x7F, 0xF6]) + _encode_scts() + bytes([len(tp_ud)]) + tp_ud
    return tpdu.hex()


def _send_envelope(tpdu_hex, scc, sm_sc='12345678912', submit_handler=None):
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
    elif sw.startswith('91'):
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
                            submit_handler.submit_tpdu_hex = assembled.hex()
                            sys.stderr.write('SMS concat: assembled %d segments (ref=%s)\n' % (total, ref))
                    else:
                        submit_handler.submit_tpdu_hex = tpdu_hex
        _handle_proactive_chain(scc, sw, _capture_sms_tpdu)
        data, sw = '', '9000'
    if sw == '9000' and submit_handler and not submit_handler.submit_tpdu_hex:
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
    algo_crypt = _CRYPT_ALGO.get(kic_b & 0x0F)
    algo_auth = _AUTH_ALGO.get(kid_b & 0x0F)
    if algo_crypt is None:
        raise ValueError('Unsupported KIc algorithm nibble %02X' % (kic_b & 0x0F))
    if algo_auth is None:
        raise ValueError('Unsupported KID algorithm nibble %02X' % (kid_b & 0x0F))
    return OtaKeyset(algo_crypt=algo_crypt, kic_idx=kic_b >> 4, kic=h2b(kic_key_hex),
                     algo_auth=algo_auth, kid_idx=kid_b >> 4, kid=h2b(kid_key_hex),
                     cntr=int(cntr_hex, 16) if cntr_hex else 0)


def _ota_reference(spi1, spi2, kic, kid, tar_hex, cntr_hex, apdu_hex, kic_key_hex, kid_key_hex):
    from pySim.ota import OtaDialectSms
    from osmocom.utils import h2b, b2h
    otak = _ota_keyset(spi1, spi2, kic, kid, cntr_hex, kic_key_hex, kid_key_hex)
    spi = _spi_from_bytes(int(spi1, 16), int(spi2, 16))
    out = OtaDialectSms().encode_cmd(otak, h2b(tar_hex), spi, h2b(apdu_hex))
    if not spi['ciphering'] and spi['rc_cc_ds'] != 'no_rc_cc_ds':
        # pySim drops the CPL octets from its unciphered output; re-add them
        # (they are included in the RC/CC/DS calculation) per TS 31.115 4.2.
        # CPL counts octets from the CHL octet to the last octet of the
        # Secured Data (incl. padding); pySim's unciphered output is exactly
        # that range, so the CPL value equals its length.
        cpl = len(out)
        out = cpl.to_bytes(2, 'big') + out
    return b2h(out), spi


def _max_load_block_size(spi1, spi2, kic, kid, tar_hex, cntr_hex,
                         kic_key_hex, kid_key_hex, requested=240):
    """Largest LOAD block payload that still fits one SMS (TS 31.115).

    pySim's SMS dialect refuses to encode a secured packet above 140 octets,
    so a LOAD APDU of the default 240-byte block cannot be sent over SCP80.
    Trial-encode a synthetic LOAD APDU for decreasing payload sizes (the
    cipher padding makes a closed-form bound unreliable) and return the
    largest one that encodes; 0 = not even a 1-byte block fits."""
    cap = max(1, min(int(requested or 240), 240))
    for n in range(cap, 0, -1):
        apdu = '80E80000%02X%s00' % (n, '00' * n)
        try:
            out_hex, _ = _ota_reference(spi1, spi2, kic, kid, tar_hex, cntr_hex,
                                        apdu, kic_key_hex, kid_key_hex)
        except ValueError:
            continue
        if len(out_hex) // 2 <= 140:
            return n
    return 0


def _decode_por(spi1, spi2, kic, kid, cntr_hex, kic_key_hex, kid_key_hex, response_hex):
    from pySim.ota import OtaDialectSms, CompactRemoteResp
    from osmocom.utils import h2b, b2h
    if not response_hex:
        return None
    otak = _ota_keyset(spi1, spi2, kic, kid, cntr_hex, kic_key_hex, kid_key_hex)
    spi = _spi_from_bytes(int(spi1, 16), int(spi2, 16))
    try:
        data = h2b(response_hex)
        if not data or data[0] != 0x02:
            return None
        res, dec = OtaDialectSms().decode_resp(otak, spi, data)
    except Exception:
        # any malformed/garbage PoR (non-hex, bad UDL, truncated fields,
        # bad CC) is not a POR; never let decoding crash the request handler.
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
    
    # Try ExpandedRemoteResponse first (TS 102 226 §5.2.2)
    if res.response_status == 'por_ok' and len(res['secured_data']):
        expanded_response_data = ''
        try:
            from construct import Struct, Int8ub, Bytes, GreedyBytes, Optional, Array, this
            ExpandedRemoteResponse = Struct(
                'response_count'/Int8ub,
                'responses'/Array(this.response_count, Struct(
                    'command_number'/Int8ub,
                    'status_word'/Bytes(2),
                    'response_data'/GreedyBytes,
                    'error_details'/Optional(Struct(
                        'error_code'/Int8ub,
                        'error_info'/GreedyBytes
                    )),
                    'chaining_context'/Optional(Struct(
                        'script_id'/Bytes(4),
                        'is_first'/Int8ub,
                        'is_last'/Int8ub,
                    ))
                ))
            )
            expanded = ExpandedRemoteResponse.parse(res['secured_data'])
            out['response_type'] = 'expanded'
            out['response_count'] = expanded.response_count
            out['responses'] = []
            for resp in expanded.responses:
                response_data = {
                    'command_number': resp.command_number,
                    'status_word': resp.status_word.hex().upper(),
                    'response_data': b2h(resp.response_data).upper() if resp.response_data else '',
                }
                if resp.error_details:
                    response_data['error_code'] = resp.error_details.error_code
                    response_data['error_info'] = b2h(resp.error_details.error_info).upper()
                if resp.chaining_context:
                    response_data['script_id'] = resp.chaining_context.script_id.hex().upper()
                    response_data['is_first'] = resp.chaining_context.is_first == 0x01
                    response_data['is_last'] = resp.chaining_context.is_last == 0x01
                out['responses'].append(response_data)
            if expanded.response_count > 0 and expanded.responses[0].response_data:
                expanded_response_data = b2h(expanded.responses[0].response_data).upper()
        except Exception:
            pass
        if dec is not None:
            out['response_type'] = 'compact'
            # Use compact parser's last_response_data; expanded parser gives wrong results for compact format
            out['decoded'] = {
                'number_of_commands': dec.number_of_commands,
                'last_status_word': str(dec.last_status_word),
                'last_response_data': str(dec.last_response_data),
            }
        else:
            out['response_type'] = 'none'
    elif dec is not None:
        out['response_type'] = 'compact'
        out['decoded'] = {
            'number_of_commands': dec.number_of_commands,
            'last_status_word': str(dec.last_status_word),
            'last_response_data': str(dec.last_response_data),
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
            pli_hex = _PLI_DATA.get(cmd_qual, '')
            if pli_hex:
                try:
                    ti_list.insert(2, _RawBerTlv(pli_hex))
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

PROACTIVE_TYPE_NAMES = {
    0x03: 'POLL INTERVAL', 0x05: 'SET UP EVENT LIST',
    0x13: 'SEND SHORT MESSAGE', 0x20: 'PLAY TONE',
    0x21: 'DISPLAY TEXT', 0x22: 'GET INKEY', 0x23: 'GET INPUT',
    0x24: 'SELECT ITEM', 0x25: 'SET UP MENU',
    0x26: 'PROVIDE LOCAL INFORMATION', 0x27: 'TIMER MANAGEMENT',
    0x15: 'LAUNCH BROWSER', 0x70: 'ACTIVATE',
    0x40: 'OPEN CHANNEL', 0x41: 'CLOSE CHANNEL',
    0x42: 'RECEIVE DATA', 0x43: 'SEND DATA', 0x44: 'GET CHANNEL STATUS',
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
_SCP81_LISTENER = None
# Active listener mode: 'dump' | 'tls' | 'redirect' | 'passthru'.
# 'redirect' pins one target and has no listener object - the BIP channels
# connect straight to the configured external platform (TLS terminated
# there). 'passthru' has neither listener nor target: each channel dials the
# destination the card requests in OPEN CHANNEL. Mode/target are tracked here
# for the status API.
_SCP81_MODE = None
_SCP81_TARGET = None
# PSK table of the TLS listener: identity -> key (memory only, never logged or
# persisted; the PWA sends it from the card presets at listener start).
# _SCP81_PSK_LEGACY keeps a single-key start (psk_hex [+ psk_identity]) so an
# API restart without psk_map/psk_hex can reuse it.
_SCP81_PSKS = {}
_SCP81_PSK_LEGACY = None

_POLL_ENABLED = False
_POLL_INTERVAL = 30
_POLL_TIMER = None
_CARD_LOCK = threading.RLock()
_CARD_CONNECTED = False

def _set_poll_interval(seconds):
    global _POLL_INTERVAL
    _POLL_INTERVAL = max(0, min(255, int(seconds)))

def _reset_poll_timer():
    global _POLL_TIMER
    if _POLL_TIMER is not None:
        _POLL_TIMER.cancel()
        _POLL_TIMER = None
    if _POLL_ENABLED and _POLL_INTERVAL > 0:
        _POLL_TIMER = threading.Timer(_POLL_INTERVAL, _do_status_poll)
        _POLL_TIMER.daemon = True
        _POLL_TIMER.start()

def _do_status_poll():
    global _POLL_TIMER
    _POLL_TIMER = None
    if not _POLL_ENABLED:
        return
    with _CARD_LOCK:
        try:
            scc = getattr(_server_ref, 'scc', None) if _server_ref else None
            if not scc:
                return
            st_data, st_sw = _send_status(scc)
            sys.stderr.write('AUTO-STATUS -> %s\n' % st_sw)
            if st_sw.startswith('91'):
                _handle_proactive_chain(scc, st_sw)
        except Exception as e:
            sys.stderr.write('AUTO-STATUS error: %s\n' % e)
            _handle_card_disconnect()
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


EVENT_NAMES = {
    0x00: 'MT call', 0x01: 'Call connected', 0x02: 'Call disconnected',
    0x03: 'Location status', 0x04: 'User activity', 0x05: 'Idle screen available',
    0x06: 'Card reader status', 0x07: 'Language selection',
    0x08: 'Browser termination', 0x09: 'Data available',
    0x0A: 'Channel status', 0x0B: 'Access Technology Change',
    0x0C: 'Display parameters changed', 0x0D: 'Local connection',
    0x0E: 'Network Search Mode Change', 0x0F: 'Browsing status',
    0x10: 'Frames Information Change', 0x11: 'I-WLAN Access Status',
    0x12: 'Network Rejection', 0x13: 'HCI Connectivity',
    0x14: 'Change of UICC Access', 0x15: 'CSG Cell Change',
    0x16: 'Contactless state request', 0x17: 'Profile Container',
    0x18: 'LTE D2D Discovery Monitoring', 0x19: 'LTE D2D Communication Monitoring',
    0x1A: 'LTE D2D Announcement Response', 0x1B: 'LTE D2D Revocation',
    0x1C: 'LTE D2D Application Port', 0x1D: 'LTE D2D Security Recovery',
    0x1E: 'Off-net Emergency Call', 0x1F: 'ECall Over IMS',
    0x20: 'EARFCN Update', 0x21: 'SCEF Channel Status',
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
    """Fetch a command TLV, tolerating both the plain tag and its
    comprehension-required variant (e.g. 0x24 and 0xA4, TS 101 220 7.1.1)."""
    return tlvs.get(tag) or tlvs.get(tag | 0x80) or b''


def _tlv_map(data):
    """Top-level COMPREHENSION-TLV map {tag: value} of a payload without a
    D0 wrapper (e.g. the command-specific TLVs of a TERMINAL RESPONSE)."""
    out = {}
    off = 0
    while off + 1 < len(data):
        tag, tlen = data[off], data[off + 1]
        out.setdefault(tag, data[off + 2: off + 2 + tlen])
        off += 2 + tlen
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
        idx = raw.find(b'\x84\x02\x01')
        if idx >= 0 and idx + 3 < len(raw):
            return [{'label': 'Interval', 'value': '%d s' % raw[idx + 3]}]
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
    if cmd_type == 0x24:
        items = _parse_select_item(raw)
        if items:
            return [{'label': 'Items', 'value': ', '.join('%s. %s' % (it['id'], it['text']) for it in items)}]
        return []
    if cmd_type == 0x25:
        items = _parse_setup_menu_items(raw)
        if items:
            return [{'label': 'Items', 'value': ', '.join('%s. %s' % (it['id'], it['text']) for it in items)}]
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
                yr = 2000 + int(v[0:2]) if int(v[0:2]) < 70 else 1900 + int(v[0:2])
                mo, dy = int(v[2:4]), int(v[4:6])
                hh, mm, ss = int(v[6:8]), int(v[8:10]), int(v[10:12])
                tz = int(v[12:14], 16)
                tz_sign = '-' if tz & 0x80 else '+'
                tz_q = (tz & 0x3F) or 0
                return [{'label': 'Date', 'value': '%04d-%02d-%02d' % (yr, mo, dy)},
                        {'label': 'Time', 'value': '%02d:%02d:%02d' % (hh, mm, ss)},
                        {'label': 'TZ offset', 'value': '%s%02d:%02d' % (tz_sign, tz_q // 4, (tz_q % 4) * 15)}]
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


def _scp81_listener_status():
    if not _SCP81_LISTENER:
        if _SCP81_MODE == 'redirect' and _SCP81_TARGET:
            return {'mode': 'redirect', 'host': _SCP81_TARGET[0],
                    'port': _SCP81_TARGET[1],
                    'target': '%s:%d' % _SCP81_TARGET}
        if _SCP81_MODE == 'passthru':
            # No listener and no pinned target: every channel dials the
            # destination the card requests (per-channel targets in the
            # BIP status).
            return {'mode': 'passthru'}
        return None
    if isinstance(_SCP81_LISTENER, scp81.PskTlsServer):
        return {'mode': 'tls', 'host': _SCP81_LISTENER.host, 'port': _SCP81_LISTENER.port,
                'psk_identities': _SCP81_LISTENER.psk_identities,
                'psk_wildcard': _SCP81_LISTENER.wildcard_psk is not None,
                'identity_seen': _SCP81_LISTENER.identity_seen,
                'identity_matched': _SCP81_LISTENER.identity_matched,
                'version_seen': _SCP81_LISTENER.version_seen,
                'cipher_seen': _SCP81_LISTENER.cipher_seen,
                'chunked': _SCP81_LISTENER.chunked,
                'chunk_size': _SCP81_LISTENER.chunk_size,
                'compact_headers': _SCP81_LISTENER.compact_headers,
                'tls_version': _SCP81_LISTENER.tls_version,
                'cipher': _SCP81_LISTENER.cipher}
    return {'mode': 'dump', 'host': _SCP81_LISTENER.host, 'port': _SCP81_LISTENER.port}


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
_SCP81_LINK_EVENTS = True


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
        elif tag == 0x23 and len(val) >= 2:
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
        if tag == 0x81:
            out['applets'] = int.from_bytes(val, 'big')
        elif tag == 0x82:
            out['free_nv'] = int.from_bytes(val, 'big')
        elif tag == 0x83:
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
    if not isinstance(_SCP81_LISTENER, scp81.PskTlsServer):
        return {'ok': False, 'error': 'PSK TLS listener is not running'}
    _SCP81_LISTENER.set_psk_map(table)
    _SCP81_PSKS = dict(table)
    _SCP81_PSK_LEGACY = None
    _BIP.log('tls-psk-map', identities=sorted(table))
    return {'ok': True, 'identities': sorted(table),
            'listener': _scp81_listener_status()}


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
            make_selectable=bool(body.get('make_selectable', True)))
    except Exception as e:
        return {'ok': False, 'error': 'cap parse failed: %s' % e}
    _BIP.log('gen-install', apdus=len(seq), load_file_aid=loadfile_aid,
             module_aid=module_aid)
    return {'ok': True, 'apdus': seq, 'load_file_aid': loadfile_aid,
            'module_aid': module_aid}


def _scp81_bip_control(body):
    global _SCP81_LISTENER, _SCP81_PSKS, _SCP81_PSK_LEGACY
    global _SCP81_MODE, _SCP81_TARGET
    global _SCP81_SCRIPT_TEMPLATE, _SCP81_SCRIPT_CR_TAG, _SCP81_NEXT_URI
    global _SCP81_LINK_EVENTS, _SCP81_TARGETED_APP
    global _SCP81_CHUNKED
    body = body or {}
    action = body.get('action', 'start')
    if action == 'stop':
        if _SCP81_LISTENER:
            _SCP81_LISTENER.stop()
            _SCP81_LISTENER = None
        _SCP81_MODE = None
        _SCP81_TARGET = None
        _BIP.disable()
        return {'ok': True, 'bip': _BIP.status(), 'listener': None}
    host = body.get('host') or '127.0.0.1'
    port = body.get('port')
    port = int(port) if port not in (None, '') else 8443
    mode = body.get('mode', 'dump')
    if _SCP81_LISTENER:
        _SCP81_LISTENER.stop()
        _SCP81_LISTENER = None
    _BIP.disable()
    # Channel status events (TS 102 223 7.5.11) apply to every mode: the
    # terminal reports BIP link changes it detects outside proactive commands.
    _SCP81_LINK_EVENTS = bool(body.get('link_events', True))
    if mode == 'redirect':
        # No local listener: the card's BIP channels are redirected straight
        # to the configured target (e.g. a production HTTP OTA server), which
        # terminates TLS and runs the administration dialog. The address the
        # card requests is only logged.
        if not body.get('host') or body.get('port') in (None, ''):
            return {'ok': False,
                    'error': 'redirect mode requires the target host and port'}
        _SCP81_MODE = 'redirect'
        _SCP81_TARGET = (host, port)
        _BIP.on_data = _bip_data_available
        _BIP.enable(host, port, mode='redirect')
        return {'ok': True, 'bip': _BIP.status(),
                'listener': _scp81_listener_status()}
    if mode == 'passthru':
        # No local listener and no pinned target: every BIP channel dials the
        # destination the card requests in OPEN CHANNEL (Other address +
        # Transport level port, TCP client only). Host and port are unused.
        _SCP81_MODE = 'passthru'
        _SCP81_TARGET = None
        _BIP.on_data = _bip_data_available
        _BIP.enable(mode='passthru')
        return {'ok': True, 'bip': _BIP.status(),
                'listener': _scp81_listener_status()}
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
        _SCP81_LISTENER = scp81.PskTlsServer(
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
        _SCP81_PSKS = dict(_SCP81_LISTENER.psk_map)
        _SCP81_PSK_LEGACY = None if table else (psk, identity)
        _SCP81_MODE = 'tls'
        _SCP81_TARGET = (_SCP81_LISTENER.host, _SCP81_LISTENER.port)
        _BIP.on_data = _bip_data_available
        _BIP.enable(host, _SCP81_LISTENER.port, mode='redirect')
        return {'ok': True, 'bip': _BIP.status(), 'listener': _scp81_listener_status(),
                'script': list(_SCP81_SCRIPT_BASE),
                'script_kind': _SCP81_SCRIPT_KIND,
                'script_template': _SCP81_SCRIPT_TEMPLATE,
                'cr_tag': _SCP81_SCRIPT_CR_TAG, 'link_events': _SCP81_LINK_EVENTS,
                'targeted_app': _SCP81_TARGETED_APP,
                'chunked': _SCP81_CHUNKED}
    if mode != 'dump':
        return {'ok': False, 'error': 'unsupported mode: %s' % mode}
    _BIP.on_data = _bip_data_available
    _SCP81_LISTENER = httpota.TcpDumpServer(
        host, port,
        on_rx=lambda peer, data: _BIP.log('dump-rx', peer=peer, bytes=len(data), hex=data.hex().upper()[:2000]),
        on_log=lambda kind, **fields: _BIP.log(kind, **fields))
    _SCP81_MODE = 'dump'
    _SCP81_TARGET = (_SCP81_LISTENER.host, _SCP81_LISTENER.port)
    _BIP.enable(host, _SCP81_LISTENER.port, mode='redirect')
    return {'ok': True, 'bip': _BIP.status(), 'listener': _scp81_listener_status()}


def _build_tr(scc, cmd_num, cmd_type, dev_src, dev_dst, cmd_qual):
    """Build the TERMINAL RESPONSE TLV payload for a fetched command
    (includes PLI dictionary data for PROVIDE LOCAL INFORMATION)."""
    if cmd_type == 0x03:
        return bytes([0x81, 0x03, cmd_num, cmd_type, 0x00,
                      0x82, 0x02, dev_dst, dev_src,
                      0x84, 0x02, 0x01, _POLL_INTERVAL,
                      0x03, 0x01, 0x00])
    base = bytes([0x81, 0x03, cmd_num, cmd_type, 0x00,
                  0x82, 0x02, dev_dst, dev_src])
    if cmd_type == 0x26 and cmd_qual is not None:
        pli_hex = _PLI_DATA.get(cmd_qual, '')
        if pli_hex:
            base += bytes.fromhex(pli_hex)
    return base + bytes([0x03, 0x01, 0x00])


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


def _handle_card_disconnect():
    global _CARD_CONNECTED
    _poll_disable()
    _cancel_menu_timeout()
    _timer_cancel()
    _CARD_CONNECTED = False
    if _server_ref:
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


def _apply_equipped_card(server):
    """Common post-equip state refresh + TERMINAL PROFILE, shared by the
    /api/command equip branch and the auto-equip worker."""
    global _CARD_CONNECTED
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
    _poll_enable()
    sm, el = _send_terminal_profile(server.scc, server.terminal_profile)
    server.sim_menu = sm
    server.event_list = el
    _tlog('equip: terminal profile done')


_AUTO_EQUIP = True
_AUTO_EQUIP_BUSY = False

def set_auto_equip(enabled):
    global _AUTO_EQUIP
    _AUTO_EQUIP = bool(enabled)

def _auto_equip_trigger():
    """Spawn a one-shot worker; never run equip in the pyscard monitor thread."""
    global _AUTO_EQUIP_BUSY
    if not _AUTO_EQUIP or _AUTO_EQUIP_BUSY:
        return
    _AUTO_EQUIP_BUSY = True
    threading.Thread(target=_auto_equip_worker, name='auto-equip', daemon=True).start()

def _auto_equip_worker():
    global _AUTO_EQUIP_BUSY
    try:
        with _CARD_LOCK:
            server = _server_ref
            if not server or _CARD_CONNECTED or not getattr(server, 'card_present', False):
                return
            app = server.app
            if app is None or not getattr(server, 'terminal_profile', None):
                return
            server.equipping = True
            try:
                sys.stderr.write('AUTO-EQUIP: card inserted, initializing\n')
                old_stdout, old_stderr = app.stdout, sys.stderr
                app.stdout = StringIO()
                sys.stderr = app.stdout
                try:
                    app.onecmd_plus_hooks('equip')
                finally:
                    app.stdout = old_stdout
                    sys.stderr = old_stderr
                if not getattr(server, 'card_present', False) or server.app.card is None:
                    sys.stderr.write('AUTO-EQUIP: card gone during initialization\n')
                    return
                _apply_equipped_card(server)
                sys.stderr.write('AUTO-EQUIP: done\n')
            except Exception as e:
                sys.stderr.write('AUTO-EQUIP failed: %s\n' % e)
            finally:
                server.equipping = False
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

def start_card_monitor(reader_name):
    """Start the process-wide pyscard monitor (one daemon thread, no process)
    and register our reader's presence observer."""
    global _card_presence_observer
    if not reader_name:
        return None
    if _card_presence_observer is None:
        _card_presence_observer = _CardPresenceObserver(reader_name)
        CardMonitor().addObserver(_card_presence_observer)
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


def _send_event_download(scc, event_type, event_data=None):
    """Send ENVELOPE(Event Download) for the given event type.
    Builds: CLA C2 0000 Lc  D6 [len] (99 01 [type] 82 02 82 81 [extra])"""
    inner = bytearray()
    inner.extend([0x99, 0x01, event_type])
    inner.extend([0x82, 0x02, 0x82, 0x81])
    if event_data:
        inner.extend(event_data)
    d6_tlv = bytes([0xD6, len(inner)]) + bytes(inner)
    env_hex = '%sc20000%02x%s' % (scc.cat_cla, len(d6_tlv), d6_tlv.hex())
    sys.stderr.write('ENVELOPE(Event Download): type=0x%02x data=%s\n' % (event_type, event_data.hex() if event_data else '(none)'))
    data, sw = scc._tp.send_apdu(env_hex)
    sys.stderr.write('ENVELOPE SW: %s\n' % sw)
    if sw.startswith('91'):
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
    if not _SCP81_LINK_EVENTS:
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


def _skip_ber_len(raw, off):
    if off >= len(raw):
        return off
    if raw[off] < 0x80:
        return off + 1
    if raw[off] == 0x81:
        return off + 2
    return off + 3


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
                _handle_card_disconnect()
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
    if raw[0] == 0xD0:
        off = _skip_ber_len(raw, 1)
        while off < len(raw) - 1:
            tag, tlen = raw[off], raw[off + 1]
            val = raw[off + 2: off + 2 + tlen]; off += 2 + tlen
            # Cards use both the plain (01/02) and comprehension-required
            # (81/82) tag variants - TS 101 220 7.1.1 leaves the CR flag to
            # the application, and the reference cards switch between them.
            if tag in (0x01, 0x81) and tlen >= 3:
                cmd_num, cmd_type, cmd_qual = val[0], val[1], val[2]
            elif tag in (0x02, 0x82) and tlen >= 2:
                dev_src, dev_dst = val[0], val[1]
    return cmd_num, cmd_type, dev_src, dev_dst, cmd_qual


def _find_sms_tpdu(raw):
    if raw[0] == 0xD0:
        off = _skip_ber_len(raw, 1)
        while off < len(raw) - 1:
            tag, tlen = raw[off], raw[off + 1]
            val = raw[off + 2: off + 2 + tlen]; off += 2 + tlen
            if tag == 0x8B and tlen >= 1:
                return val.hex()
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
        if vpf in (0x01, 0x02):  # relative or absolute
            off += 7
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
    if raw[0] == 0xD0:
        off = _skip_ber_len(raw, 1)
        while off < len(raw) - 1:
            tag, tlen = raw[off], raw[off + 1]
            val = raw[off + 2: off + 2 + tlen]; off += 2 + tlen
            if tag == 0x8D and tlen >= 1:
                return _decode_dcs_text(val)
    return None


def _parse_select_item(raw):
    items = []
    if raw[0] == 0xD0:
        off = _skip_ber_len(raw, 1)
        while off < len(raw) - 1:
            tag, tlen = raw[off], raw[off + 1]
            val = raw[off + 2: off + 2 + tlen]; off += 2 + tlen
            if tag == 0x05 and tlen >= 1:
                try:
                    _title = _decode_stk_text(val)
                except Exception:
                    pass
            elif tag in (0x8F, 0x0F) and tlen >= 2:
                items.append({'id': val[0], 'text': _decode_stk_text(val[1:])})
    return items


def _parse_setup_menu_items(raw):
    items = []
    if not raw or raw[0] != 0xD0:
        return items
    off = _skip_ber_len(raw, 1)
    while off < len(raw) - 1:
        tag, tlen = raw[off], raw[off + 1]
        val = raw[off + 2: off + 2 + tlen]; off += 2 + tlen
        if tag == 0x8F and tlen >= 2:
            items.append({'id': val[0], 'text': _decode_stk_text(val[1:])})
    return items


def _handle_proactive_chain(scc, sw91, on_fetch=None):
    """Run a FETCH/TERMINAL RESPONSE chain; marks the card as busy so that
    terminal-initiated ENVELOPEs (Data available, Channel status, timers) wait."""
    global _PROACTIVE_BUSY
    _PROACTIVE_BUSY = True
    try:
        return _run_proactive_chain(scc, sw91, on_fetch)
    finally:
        _PROACTIVE_BUSY = False


def _run_proactive_chain(scc, sw91, on_fetch=None):
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
        if raw:
            cmd_num, cmd_type, dev_src, dev_dst, cmd_qual = _parse_proactive_header(raw)
            entry = _log_proactive(cmd_type, raw, cmd_qual, cmd_num)
        else:
            cmd_num, cmd_type, dev_src, dev_dst, cmd_qual = 1, 0, 0x83, 0x81, None
            entry = None
        if on_fetch:
            action = on_fetch(raw, cmd_num, cmd_type, dev_src, dev_dst)
        paused = action == 'pause'
        if not paused:
            tr_tlv = None
            if raw and cmd_type in (0x40, 0x41, 0x42, 0x43, 0x44):
                tr_tlv = _handle_bip_command(scc, cmd_num, cmd_type, cmd_qual, raw, dev_src, dev_dst)
            if cmd_type == 0x27:
                tr_tlv = _handle_timer_command(cmd_num, cmd_type, cmd_qual, raw, dev_src, dev_dst)
            if tr_tlv is None:
                tr_tlv = _build_tr(scc, cmd_num, cmd_type, dev_src, dev_dst, cmd_qual)
            tr_rv = scc._tp.send_apdu('%s140000%02x%s' % (scc.cat_cla, len(tr_tlv), tr_tlv.hex()))
            sys.stderr.write('TR: cmd=%02x type=%02x -> %s %s\n' % (cmd_num, cmd_type, tr_rv[1], ('(%d bytes)' % len(tr_tlv))))
            _record_tr(entry, tr_tlv, tr_rv[1])
            sw = tr_rv[1]
            if sw == '9000':
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


def _send_terminal_profile(scc, tp_hex):
    tp_data, tp_sw = scc._tp.send_apdu('%s100000%02x%s' % (scc.cat_cla, len(tp_hex) // 2, tp_hex))
    sim_menu = None
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
                    menu = None
                    items = []
                    while off < len(raw) - 1:
                        tag, tlen = raw[off], raw[off + 1]
                        val = raw[off + 2: off + 2 + tlen]
                        off += 2 + tlen
                        if tag == 0x81 and tlen >= 3:
                            cmd_num, cmd_type, cmd_qual = val[0], val[1], val[2]
                            if cmd_type == 0x25:
                                menu = {'command_number': cmd_num, 'items': items}
                        elif tag == 0x82 and tlen >= 2:
                            dev_src, dev_dst = val[0], val[1]
                        elif tag == 0x05 and tlen >= 1 and menu is not None:
                            try:
                                menu['title'] = _STK_DECODE._decode(val, {}, 'stk_title')
                            except Exception:
                                menu['title'] = val.hex()
                        elif tag == 0x8F and tlen >= 2:
                            try:
                                txt = _STK_DECODE._decode(val[1:], {}, 'stk_item')
                            except Exception:
                                txt = val[1:].hex()
                            items.append({'id': val[0], 'text': txt})
                        elif tag in (0x99, 0x19) and tlen >= 1:
                            event_list = [b for b in val]
                    if menu:
                        sim_menu = menu
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
    return _on_menu_fetch


def _menu_send_response(server, result, item_id=None):
    """Send the pending command's TERMINAL RESPONSE and continue the chain.
    Shared by /api/menu-respond and the user-input timeout watchdog. Returns
    (payload, http_status)."""
    if not server.stk_pending:
        return {'error': 'no pending command'}, 400
    scc = server.scc
    RESULT_MAP = {'ok': 0x00, 'cancel': 0x10, 'back': 0x11, 'timeout': 0x12}
    gr = RESULT_MAP.get(result, 0x00)
    pd = server.stk_pending
    cd = bytes([0x81, 0x03, pd['cmd_num'], pd['cmd_type'], 0x00])
    di = bytes([0x82, 0x02, pd['dev_dst'], pd['dev_src']])
    tr_data = cd + di
    if isinstance(item_id, int) and result == 'ok' and pd['type'] == 'select_item':
        tr_data += bytes([0x90, 0x01, item_id])
    tr_data += bytes([0x83, 0x02, gr, 0x00])
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


class PysimHandler(BaseHTTPRequestHandler):
    def _send_json(self, data, status=200):
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode())

    def _read_body(self):
        length = int(self.headers.get('Content-Length', 0))
        return json.loads(self.rfile.read(length))

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
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        if rel in ('index.html', 'sw.js'):
            self.send_header('Cache-Control', 'no-cache')
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        # /api/status is pure cached state (no card I/O); keeping it out of the
        # lock lets the UI report 'initializing' while a long equip holds the
        # card lock. Result-shaping masks everything card-derived when the
        # session is not connected.
        if self.path == '/api/status':
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
                'auto_equip': bool(_AUTO_EQUIP),
                'card': card.name if card else None,
                'profile': str(rs.profile) if rs and rs.profile else None,
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
                    'file_size': lchan.selected_file_size() if lchan else None,
                    'record_len': lchan.selected_file_record_len() if lchan else None,
                    'num_of_rec': lchan.selected_file_num_of_rec() if lchan else None,
                } if cur_file else None,
                'channels': [str(i) for i, ch in rs.lchan.items() if ch] if rs else [],
            }
            self._send_json(data)
            self._log_resp(data)
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
            resp = {'enabled': _POLL_ENABLED, 'interval': _POLL_INTERVAL}
            self._send_json(resp)
            self._log_resp(resp)
        elif self.path == '/api/proactive-log':
            log = list(reversed(_PROACTIVE_LOG[-50:]))
            self._send_json(log)
            self._log_resp(log)
        elif self.path == '/api/stk-status':
            resp = {'active': self.server.menu_active,
                    'pending': self.server.stk_pending is not None,
                    'pending_type': self.server.stk_pending['type'] if self.server.stk_pending else None}
            self._send_json(resp)
            self._log_resp(resp)
        elif self.path == '/api/scp81/status':
            self._log_req()
            resp = {'bip': _BIP.status(), 'listener': _scp81_listener_status()}
            self._send_json(resp)
            self._log_resp(resp)
        elif self.path == '/api/scp81/log' or self.path.startswith('/api/scp81/log?'):
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
        elif self.path.startswith('/api/'):
            self._send_json({'error': _err('not_found', lang)}, 404)
            self._log_resp({'error': _err('not_found', lang)})
        else:
            self._log_req()
            self._serve_static()

    def do_POST(self):
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
            is_equip = str(cmd).strip().startswith('equip')
            if is_equip:
                _tlog('equip: onecmd_plus_hooks %dms' % elapsed)
            if is_equip and self.server.app and self.server.app.card and self.server.terminal_profile:
                _apply_equipped_card(self.server)
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
                sys.stderr.write('VERIFY ADM → SW: %s\n' % resp.get('sw'))
                self._send_json(resp)
                self._log_resp(resp)
            except Exception as e:
                resp = {'ok': False, 'error': str(e)}
                sys.stderr.write('VERIFY ADM → ERROR: %s\n' % e)
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
                _handle_card_disconnect()
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
                _handle_card_disconnect()
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
                    'file_size': lchan.selected_file_size() if lchan else None,
                    'record_len': lchan.selected_file_record_len() if lchan else None,
                    'num_of_rec': lchan.selected_file_num_of_rec() if lchan else None,
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
                if 'Card' in str(e) or 'Transaction' in str(e) or 'Transmit' in str(e):
                    _handle_card_disconnect()
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
            resp, code = _menu_send_response(self.server, body.get('result', 'ok'), body.get('item_id'))
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
            try:
                data, sw = _send_event_download(scc, event_type, event_data)
                try:
                    is_location = int(event_type) == netsim.EVENT_LOCATION_STATUS
                except (TypeError, ValueError):
                    is_location = False
                if is_location:
                    _netstate_after_event(self.server, event_type, event_data)
                resp = {'sw': sw}
                if data:
                    resp['data'] = data
                if getattr(self.server, 'net_state', None) is not None:
                    resp['net_state'] = self.server.net_state
                self._send_json(resp)
                self._log_resp(resp)
            except Exception as e:
                sys.stderr.write('Event send error: %s\n' % e)
                _handle_card_disconnect()
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
            resp = {'enabled': _POLL_ENABLED, 'interval': _POLL_INTERVAL}
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
            include_cpi = body.get('includeCpi', True)
            try:
                if apdu:
                    # RAM operation: SCP80-wrap the raw GP command
                    spi1 = body.get('spi1', '16')
                    spi2 = body.get('spi2', '01')
                    kic = body.get('kic', '25')
                    kid = body.get('kid', '25')
                    tar = body.get('tar', '000000')
                    cntr = body.get('cntr', '')
                    kic_key = body.get('kicKey', '')
                    kid_key = body.get('kidKey', '')
                    sp_hex, _ = _ota_reference(spi1, spi2, kic, kid, tar, cntr, apdu, kic_key, kid_key)
                    sp_bytes = bytes.fromhex(sp_hex)
                else:
                    # Regular SCP80: use pre-built secured packet
                    sp_hex = sp
                    sp_bytes = bytes.fromhex(sp_hex)
                spi2_val = int(body.get('spi2', '00'), 16)
                por_in_submit = bool(spi2_val & 0x20)
                submit_handler = None
                old_proactive = None
                if por_in_submit and hasattr(scc, '_tp'):
                    submit_handler = PoRSubmitHandler()
                    old_proactive = scc._tp.proactive_handler
                    scc._tp.proactive_handler = submit_handler
                try:
                    max_chunk = 130
                    chunks = [sp_bytes[i:i+max_chunk] for i in range(0, len(sp_bytes), max_chunk)]
                    total = len(chunks)
                    sys.stderr.write('OTA SEND: SPI %s %s KIc %s KID %s TAR %s CNTR %s LEN %dB CHUNKS %d\n' % (
                        body.get('spi1', ''), body.get('spi2', ''), body.get('kic', ''),
                        body.get('kid', ''), body.get('tar', ''), body.get('cntr', ''),
                        len(sp_bytes), total))
                    if total > MAX_ENVELOPE_SEGMENTS:
                        resp = {'success': False, 'error': 'Secured packet too large: %d segments (max %d)' % (total, MAX_ENVELOPE_SEGMENTS)}
                        sys.stderr.write('OTA SEND FAILED: %d segments exceeds max %d\n' % (total, MAX_ENVELOPE_SEGMENTS))
                    else:
                        sys.stderr.write('RAM C-APDU: %s\n' % apdu if apdu else sp)
                        sys.stderr.write('RAM SECURED-PACKET: %s\n' % sp_hex)
                        last_data = None
                        last_sw = None
                        for i, chunk in enumerate(chunks):
                            tpdu = _build_sms_tpdu(chunk.hex(), total, i + 1, oa_number=self.server.sms_oa,
                                                   include_cpi=include_cpi)
                            data, sw = _send_envelope(tpdu, scc, sm_sc=self.server.sms_sc, submit_handler=submit_handler)
                            last_data = data
                            last_sw = sw
                            if sw != '9000' and not sw.startswith('91'):
                                resp = {'success': False, 'sw': sw, 'error': 'ENVELOPE failed at chunk %d' % (i + 1)}
                                sys.stderr.write('OTA SEND FAILED: chunk %d SW %s\n' % (i + 1, sw))
                                break
                        else:
                            resp = {'success': True, 'sw': last_sw, 'response_data': last_data if last_data else None}
                            por_src = 'envelope'
                            por_hex = resp['response_data']
                            if submit_handler and submit_handler.submit_tpdu_hex:
                                tpdu_b = bytes.fromhex(submit_handler.submit_tpdu_hex)
                                idx = tpdu_b.find(b'\x02\x71\x00')
                                if idx >= 0:
                                    por_hex = tpdu_b[idx:].hex()
                                    por_src = 'sms-submit'
                            por = _decode_por(body.get('spi1', ''), body.get('spi2', ''), body.get('kic', ''),
                                              body.get('kid', ''), body.get('cntr', ''), body.get('kicKey', ''),
                                              body.get('kidKey', ''), por_hex)
                            # Check for SPI2=0x21 (PoR required) but got 9000 with no PoR → card refuses PoR
                            is_ram = bool(apdu)
                            por_required = bool(spi2_val & 0x01)
                            no_por_received = not por_hex and not (submit_handler and submit_handler.submit_tpdu_hex)
                            if is_ram and por_required and last_sw == '9000' and no_por_received:
                                sys.stderr.write('WARNING: Card refused to return PoR - ENVELOPE returned 9000 with no response data\n')
                            sys.stderr.write('RAM RESPONSE-PACKET: %s\n' % (por_hex if por_hex else 'empty'))
                            if por:
                                resp['por'] = por
                                extra = ''
                                if por.get('decoded'):
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
                finally:
                    if submit_handler and hasattr(scc, '_tp'):
                        scc._tp.proactive_handler = old_proactive
                self._send_json(resp)
                self._log_resp(resp)
            except Exception as e:
                sys.stderr.write('OTA send error: %s\n' % e)
                if 'Card' in str(e) or 'Transaction' in str(e) or 'Transmit' in str(e):
                    _handle_card_disconnect()
                err = {'success': False, 'error': str(e)}
                self._send_json(err, 500)
                self._log_resp(err)
        elif self.path == '/api/sp-verify':
            body = self._read_body()
            self._log_req(body)
            try:
                ref, spi = _ota_reference(body.get('spi1', ''), body.get('spi2', ''), body.get('kic', ''),
                                          body.get('kid', ''), body.get('tar', ''), body.get('cntr', ''),
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

                # SCP80 params
                spi1 = body.get('spi1', '16')
                spi2 = body.get('spi2', '01')
                kic = body.get('kic', '25')
                kid = body.get('kid', '25')
                tar = body.get('tar', '000000')
                cntr = body.get('cntr', '00000000')
                kic_key = body.get('kicKey', '')
                kid_key = body.get('kidKey', '')
                sd_aid = body.get('sd_aid', '').replace(' ', '')
                install_params_hex = body.get('install_params', '').replace(' ', '')
                stk_params_hex = body.get('stk_params', '').replace(' ', '')
                make_selectable = body.get('make_selectable', True)
                privileges_hex = body.get('privileges', '').replace(' ', '') or '00'

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
                max_block = _max_load_block_size(spi1, spi2, kic, kid, tar, cntr,
                                                 kic_key, kid_key,
                                                 requested=block_size_req or 240)
                if max_block < 1:
                    err = {'success': False,
                           'error': 'no LOAD block fits a single SMS with these '
                                    'SCP80 parameters'}
                    self._send_json(err, 500)
                    self._log_resp(err)
                    return
                block_size = min(block_size_req, max_block) if block_size_req else max_block
                block_clamped = block_size_req is not None and block_size != block_size_req
                sys.stderr.write('RAM-INSTALL: LOAD block size %d bytes%s\n' % (
                    block_size,
                    (' (requested %d, clamped to fit one SMS)' % block_size_req)
                    if block_clamped else ''))

                steps = []
                encode_error = None
                include_cpi = body.get('includeCpi', True)
                spi2_val = int(spi2, 16)
                por_in_submit = bool(spi2_val & 0x20)

                def _send_gp_apdu(apdu_hex, step_name):
                    nonlocal cntr, encode_error
                    try:
                        sp_hex, _ = _ota_reference(spi1, spi2, kic, kid, tar, cntr, apdu_hex, kic_key, kid_key)
                    except ValueError as e:
                        encode_error = str(e)
                        steps.append({'name': step_name, 'por_status': 'encode_error',
                                      'sw': encode_error})
                        sys.stderr.write('RAM-INSTALL: %s encode failed: %s\n' % (step_name, e))
                        return False
                    sp_bytes = bytes.fromhex(sp_hex)
                    max_chunk = 130
                    chunks = [sp_bytes[i:i + max_chunk] for i in range(0, len(sp_bytes), max_chunk)]
                    submit_handler = None
                    old_proactive = None
                    if por_in_submit and hasattr(scc, '_tp'):
                        submit_handler = PoRSubmitHandler()
                        old_proactive = scc._tp.proactive_handler
                        scc._tp.proactive_handler = submit_handler
                    try:
                        last_data = None
                        last_sw = None
                        for i, chunk in enumerate(chunks):
                            tpdu = _build_sms_tpdu(chunk.hex(), len(chunks), i + 1,
                                                   oa_number=self.server.sms_oa, include_cpi=include_cpi)
                            data, sw = _send_envelope(tpdu, scc, sm_sc=self.server.sms_sc,
                                                      submit_handler=submit_handler)
                            last_data = data
                            last_sw = sw
                            if sw != '9000' and not sw.startswith('91'):
                                steps.append({'name': step_name, 'por_status': 'envelope_error', 'sw': sw})
                                sys.stderr.write('RAM-INSTALL: %s ENVELOPE failed SW %s\n' % (step_name, sw))
                                return False
                        # Decode PoR
                        por_src = 'envelope'
                        por_hex = last_data
                        if submit_handler and submit_handler.submit_tpdu_hex:
                            tpdu_b = bytes.fromhex(submit_handler.submit_tpdu_hex)
                            idx = tpdu_b.find(b'\x02\x71\x00')
                            if idx >= 0:
                                por_hex = tpdu_b[idx:].hex()
                                por_src = 'sms-submit'
                        por = _decode_por(spi1, spi2, kic, kid, cntr, kic_key, kid_key, por_hex)
                        por_status = 'unknown'
                        if por and por.get('decoded'):
                            ps = por['decoded'].get('response_status', '')
                            por_status = 'por_ok' if ps == '9100' else 'por_error_%s' % ps
                            sys.stderr.write('RAM-INSTALL: %s PoR[%s] status=%s\n' % (step_name, por_src, ps))
                        elif last_sw == '9000' and not por_hex:
                            por_status = 'no_por'
                        steps.append({'name': step_name, 'por_status': por_status, 'sw': last_sw})
                        # Increment counter
                        cntr = '%010X' % ((int(cntr, 16) + 1) % (2 ** 32))
                        return True
                    finally:
                        if submit_handler and hasattr(scc, '_tp'):
                            scc._tp.proactive_handler = old_proactive

                # INSTALL [for load] -> LOAD blocks -> INSTALL [for install]
                seq = _cap_apdu_sequence(
                    loadfile_aid, module_aid, loadfile_data, sd_aid=sd_aid,
                    privileges=privileges_hex,
                    install_params=install_params_hex, stk_params=stk_params_hex,
                    make_selectable=make_selectable, block_size=block_size)
                sys.stderr.write('RAM-INSTALL: %d APDUs (INSTALL / %d x LOAD / INSTALL) loadfile_aid=%s\n' % (
                    len(seq), len(seq) - 2, loadfile_aid))
                for apdu_idx, gp_apdu in enumerate(seq):
                    if apdu_idx == 0:
                        step_name = 'INSTALL [for load]'
                    elif apdu_idx == len(seq) - 1:
                        step_name = 'INSTALL [for install]'
                    else:
                        step_name = 'LOAD (%d/%d)' % (apdu_idx, len(seq) - 2)
                    if not _send_gp_apdu(gp_apdu, step_name):
                        resp = {'success': False, 'steps': steps, 'failed_step': len(steps),
                                'error': encode_error or ('%s failed' % step_name),
                                'load_file_aid': loadfile_aid, 'module_aid': module_aid,
                                'load_block_size': block_size,
                                'load_block_size_requested': block_size_req,
                                'load_block_size_clamped': block_clamped}
                        self._send_json(resp)
                        self._log_resp(resp)
                        return

                resp = {'success': True, 'steps': steps, 'load_file_aid': loadfile_aid,
                        'module_aid': module_aid, 'final_cntr': cntr,
                        'load_block_size': block_size,
                        'load_block_size_requested': block_size_req,
                        'load_block_size_clamped': block_clamped}
                sys.stderr.write('RAM-INSTALL: Complete — loadfile_aid=%s module_aid=%s cntr=%s\n' % (
                    loadfile_aid, module_aid, cntr))
                self._send_json(resp)
                self._log_resp(resp)
            except Exception as e:
                sys.stderr.write('RAM-INSTALL error: %s\n' % e)
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
            elif _SCP81_LISTENER is None:
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
        elif self.path == '/api/scp81/log-clear':
            body = self._read_body()
            self._log_req(body)
            _BIP.clear_log()
            resp = {'ok': True, 'seq': _BIP.seq}
            self._send_json(resp)
            self._log_resp(resp)
        else:
            self._send_json({'error': _err('not_found', lang)}, 404)
            self._log_resp({'error': _err('not_found', lang)})

    def log_message(self, format, *args):
        sys.stderr.write('%s - - [%s] %s\n' % (self.client_address[0], self.log_date_time_string(), format % args))