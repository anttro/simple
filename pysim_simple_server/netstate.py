# coding=utf-8
"""Network-state monitor for the SIMple lab.

Keeps a cached, per-card-session view of the network-related EFs: the files
the network simulator writes plus IMSI / EHPLMN / SPDI / HPLMNwAcT / FPLMN
(trace study: ``projects/UICC_NAA.md``).  It also tracks the *simulated*
service state (normal / limited / no service, ``Undefined`` until something
is simulated) and derives the current location + roaming view for the Phone
tab's "Network state" panel.

This module is pure: the server owns all card I/O (``_netstate_read`` in
``server.py``) and passes freshly read file entries in.  ``mcc_mnc_data`` is
the optional MCC/MNC operator list the server loads (``--mcc-mnc-list``).
"""

import re
import time

from pysim_simple_server import netsim

SERVICE_NORMAL = 'normal'
SERVICE_LIMITED = 'limited'
SERVICE_NONE = 'none'


def _def(key, name, fid):
    return {'key': key, 'name': name, 'fid': fid,
            'paths': list(netsim.FILE_PATHS.get(key) or [])}


# Display order of the monitor panel.
FILE_DEFS = [
    _def('imsi', 'EF.IMSI', '6F07'),
    _def('ehplmn', 'EF.EHPLMN', '6FD9'),
    _def('spdi', 'EF.SPDI', '6FCD'),
    _def('hplmnwact', 'EF.HPLMNwAcT', '6F62'),
    _def('loci', 'EF.LOCI', '6F7E'),
    _def('psloci', 'EF.PSLOCI', '6F73'),
    _def('epsloci', 'EF.EPSLOCI', '6FE3'),
    _def('epsnsc', 'EF.EPSNSC', '6FE4'),
    _def('cbmi', 'EF.CBMI', '6F45'),
    _def('cbmir', 'EF.CBMIR', '6F50'),
    _def('smsstatus', 'EF.SMSS', '6F43'),
    _def('fplmn', 'EF.FPLMN', '6F7B'),
]
MONITORED_KEYS = [d['key'] for d in FILE_DEFS]
FILE_BY_KEY = {d['key']: d for d in FILE_DEFS}


def _norm(value):
    return re.sub(r'[^0-9a-fA-F]', '', value or '').upper()


def new_state():
    """Empty monitor state (a new card session)."""
    return {'files': {},
            'service': {'state': None, 'source': None, 'time': None},
            'network': None, 'read_at': None}


def set_service(state, service, source=None):
    """Record the simulated service state ('normal'/'limited'/'none')."""
    if not state:
        return
    state['service'] = {'state': service, 'source': source, 'time': time.time()}


def set_read_time(state):
    if state:
        state['read_at'] = time.time()


def patch_file(state, key, path, data, source='write', record=None):
    """Patch one cached file from bytes we just wrote (no re-read needed)."""
    if not state or key not in FILE_BY_KEY:
        return
    d = FILE_BY_KEY[key]
    cur = (state.get('files') or {}).get(key) or {}
    if record is not None or cur.get('kind') == 'record':
        records = {r.get('num'): r.get('data', '')
                   for r in (cur.get('records') or [])}
        records[int(record or 1)] = _norm(data)
        body = {'kind': 'record',
                'records': [{'num': n, 'data': records[n]}
                            for n in sorted(records)]}
    else:
        body = {'kind': 'transparent', 'data': _norm(data)}
    entry = {'name': d['name'], 'fid': d['fid'],
             'path': path or cur.get('path') or (d['paths'][0] if d['paths'] else None),
             'present': True, 'source': source, 'updated': time.time()}
    entry.update(body)
    state.setdefault('files', {})[key] = entry


def apply_steps(state, steps):
    """Patch the cached files from a net-sim step log (writes we performed)."""
    if not state:
        return
    for s in steps or []:
        act = s.get('action')
        key = s.get('key')
        if act in ('update_binary', 'update_record') and key in FILE_BY_KEY:
            patch_file(state, key, s.get('path'), s.get('data') or '',
                       source='write',
                       record=s.get('record') if act == 'update_record' else None)


def merge_read(state, entries, source='read'):
    """Merge freshly read file entries (server I/O) into the state."""
    if not state or not entries:
        return
    for key, entry in entries.items():
        if not entry:
            continue
        entry = dict(entry)
        entry['source'] = source
        state.setdefault('files', {})[key] = entry


# ---- decoding helpers (mirror the client-side EF decoders) ----

def parse_imsi(data_hex):
    """EF.IMSI digits or None (TS 31.102 4.2.2)."""
    return netsim.parse_imsi(data_hex)


def plmn_from_hex(hex3):
    """3-byte PLMN (TS 24.008 BCD) -> {'mcc','mnc','plmn'} or None."""
    h = _norm(hex3)
    if len(h) != 6 or h == 'FFFFFF':
        return None
    b = [int(h[i:i + 2], 16) for i in (0, 2, 4)]
    d = lambda v: v & 0x0F
    e = lambda v: (v >> 4) & 0x0F
    if d(b[0]) > 9 or e(b[0]) > 9 or d(b[1]) > 9:
        return None
    if d(b[2]) > 9 or e(b[2]) > 9:
        return None
    mcc = '%d%d%d' % (d(b[0]), e(b[0]), d(b[1]))
    mnc = '%d%d' % (d(b[2]), e(b[2]))
    mnc3 = e(b[1])
    if mnc3 != 0x0F:
        if mnc3 > 9:
            return None
        mnc += '%d' % mnc3
    return {'mcc': mcc, 'mnc': mnc, 'plmn': mcc + mnc}


def _plmn_hex_list(data_hex, rec=3):
    """Valid 3-byte PLMN entries of a transparent list file ('FFFFFF' gaps
    are skipped, never treated as terminators)."""
    h = _norm(data_hex)
    step = rec * 2
    return [h[i:i + 6] for i in range(0, len(h) - 5, step)
            if plmn_from_hex(h[i:i + 6])]


def plmn_list(data_hex, rec=3):
    return [plmn_from_hex(h) for h in _plmn_hex_list(data_hex, rec)]


def _transparent(files, key):
    f = (files or {}).get(key) or {}
    if f.get('present') and f.get('kind') == 'transparent':
        return f
    return None


def _current_location(files):
    """Current PLMN + area, EPSLOCI (TAI) -> PSLOCI (RAI) -> LOCI (LAI).

    Dummies keep the PLMN in LOCI/PSLOCI while EPSLOCI is wiped, so the
    fallback order keeps the location known after service loss."""
    for key, area, off in (('epsloci', 'TAI', 12), ('psloci', 'RAI', 7),
                           ('loci', 'LAI', 4)):
        f = _transparent(files, key)
        if not f:
            continue
        h = _norm(f.get('data'))
        if len(h) < (off + 5) * 2:
            continue
        p = plmn_from_hex(h[off * 2:off * 2 + 6])
        if not p:
            continue
        return {'plmn': p['plmn'], 'mcc': p['mcc'], 'mnc': p['mnc'],
                'hex': h[off * 2:off * 2 + 6], 'area': area,
                'lac': h[off * 2 + 6:off * 2 + 10], 'source': f.get('source')}
    return None


def _home_sets(files):
    """(home PLMN hex, equivalent-home PLMN hex) sets from HPLMNwAcT, EHPLMN
    and the IMSI (fallback), as 3-byte uppercase hex."""
    home, eq = set(), set()
    f = _transparent(files, 'hplmnwact')
    if f:
        # First 5-byte record is the HPLMN (TS 31.102 4.2.5).
        head = _norm(f.get('data'))[0:6]
        if plmn_from_hex(head):
            home.add(head)
    f = _transparent(files, 'ehplmn')
    if f:
        eq.update(_plmn_hex_list(f.get('data')))
    if not home:
        imsi = parse_imsi((_transparent(files, 'imsi') or {}).get('data'))
        if imsi and len(imsi) >= 5 and imsi[:3].isdigit():
            try:
                home.add(netsim.plmn_bcd(imsi[0:3], imsi[3:5]))
                if len(imsi) >= 6 and imsi[5].isdigit():
                    home.add(netsim.plmn_bcd(imsi[0:3], imsi[3:6]))
            except ValueError:
                pass
    return home, eq


def home_plmn(files):
    """The simulator's "home network" PLMN: the first EF.HPLMNwAcT record
    (the HPLMN per TS 31.102 4.2.5), falling back to the IMSI (the MNC is
    taken as 2 digits — the IMSI does not encode its length)."""
    f = _transparent(files, 'hplmnwact')
    if f:
        p = plmn_from_hex(_norm(f.get('data'))[0:6])
        if p:
            return {'mcc': p['mcc'], 'mnc': p['mnc'], 'plmn': p['plmn'],
                    'source': 'hplmnwact'}
    imsi = parse_imsi((_transparent(files, 'imsi') or {}).get('data'))
    if imsi and len(imsi) >= 5 and imsi[:5].isdigit():
        return {'mcc': imsi[:3], 'mnc': imsi[3:5], 'plmn': imsi[:5],
                'source': 'imsi'}
    return None


def _rejected(files):
    """A permanent 'PLMN not allowed' rejection fingerprint: status 010 in a
    location file (UICC_NAA.md C3a) or a non-empty EF.FPLMN."""
    off = {'loci': 10, 'psloci': 13, 'epsloci': 17}
    for key, idx in off.items():
        f = _transparent(files, key)
        if not f:
            continue
        h = _norm(f.get('data'))
        if len(h) >= (idx + 1) * 2 and h[idx * 2:idx * 2 + 2] == '02':
            return True
    f = _transparent(files, 'fplmn')
    if f and _plmn_hex_list(f.get('data')):
        return True
    return False


def _operator(mcc_mnc_data, mcc, mnc):
    """Exact MCC/MNC lookup in the optional operator list; a country-only
    fallback (first entry of that MCC) keeps the location line useful."""
    if not mcc_mnc_data:
        return None
    try:
        mcc_i, mnc_i = int(mcc), int(mnc)
    except (TypeError, ValueError):
        return None
    fallback = None
    for e in mcc_mnc_data:
        try:
            if int(str(e.get('mcc') or '').strip() or -1) != mcc_i:
                continue
        except ValueError:
            continue
        if fallback is None:
            fallback = e
        try:
            if int(str(e.get('mnc') or '').strip() or -1) == mnc_i:
                return {'country': e.get('countryName'),
                        'operator': e.get('brand') or e.get('operator')}
        except ValueError:
            continue
    if fallback:
        return {'country': fallback.get('countryName'), 'operator': None}
    return None


def compute_network(state, mcc_mnc_data=None):
    """Derive the monitor header data from the cached state."""
    state = state or {}
    files = state.get('files') or {}
    loc = _current_location(files)
    home, eq = _home_sets(files)
    roaming = None
    if loc and home:
        if loc['hex'] in home:
            roaming = 'home'
        elif loc['hex'] in eq:
            roaming = 'equivalent'
        else:
            roaming = 'guest'
    location = None
    if loc:
        op = _operator(mcc_mnc_data, loc['mcc'], loc['mnc'])
        location = {'plmn': loc['plmn'], 'mcc': loc['mcc'], 'mnc': loc['mnc'],
                    'area': loc['area'], 'lac': loc['lac'],
                    'country': (op or {}).get('country'),
                    'operator': (op or {}).get('operator'),
                    'roaming': roaming, 'rejected': _rejected(files),
                    'source': loc.get('source')}
    network = {'service': state.get('service') or
               {'state': None, 'source': None, 'time': None},
               'location': location,
               'home': home_plmn(files)}
    state['network'] = network
    return network
