"""Import card presets from a card vendor's **outfile** (v3.27.0).

An outfile is the vendor personalization tool's plain-text dump: a long
banner/configuration header (customer, batch, profile, ...) followed by the
actual data.  The data starts at a **column-name line** - whitespace-separated
tokens that include ``ICCID`` (the first token may carry a ``var_out:``-style
prefix) - and continues with one whitespace-separated row per card.  The
header and every column the presets do not use are ignored.

Recognised columns (case-insensitive): ``ICCID``, ``PIN1``/``PIN2``,
``PUK1``/``PUK2``, ``ADM`` (the key's hex value - 4-16 hex digits in **byte
pairs**, the rule ``/api/verify-adm`` applies; this is the form the preset store
uses, where pySim pads it with 'f' to the 8 CHV bytes), ``PSK`` (the SCP81 key, 16 bytes; an
optional ``PSKID``/``PSK_ID`` column names its identity, defaulting to the
card's ICCID) and ``KICn``/``KIDn`` - one keyset per numbered pair, up to 15.
Everything else (``IMSI*``, ``EKI``, ``KIKn`` - the key-management key, no
use yet) is skipped.

The **KIc/KID bytes are never in the file**: the keyset number is the column
number (``KIC2`` -> keyset 2) and the algorithm nibble follows the key size -
8 bytes DES (1), 16 bytes 3DES2 (5) or AES (2, the caller's choice), 24 bytes
3DES3 (9); any other size skips that keyset with a report line.  The KIc and
KID bytes carry the same number (TS 102 225 A.2).

The **TARs** never come from the file either: the caller passes the TAR table
(the three mandatory role entries with their spec defaults by default) and one
default keyset applied to every entry of every imported card; without one the
card's first keyset is used (a multi-keyset card would otherwise fail the
store's "a non-zero MSL needs a default keyset" rule).

``parse_outfile()`` is pure: it returns the preset dicts (the store's shape)
and a report.  Nothing is validated here - the preset store's normalisation
and validation own that, and an entry it refuses is reported by the import.
"""

import re

from pysim_simple_server import presets

# The algorithm nibble of KIc/KID by key size (TS 102 225 A.2; the project's
# _CRYPT_ALGO): 8 bytes = DES, 16 = 3DES2 or AES (the caller picks), 24 =
# 3DES3.  Anything else is refused with a report line.
_KEY_ALGO = {8: 0x01, 24: 0x09}
_KEY_ALGO_16 = {'3des2': 0x05, 'aes': 0x02}
# The 16-byte algorithm choices as the API accepts them (derived from the map
# above, so the endpoint's validation and the parser cannot drift apart).
ALGORITHM16_CHOICES = tuple(_KEY_ALGO_16)
KEY_SIZES = (8, 16, 24)
PSK_KEY_HEX_LEN = 32        # the store's PSK contract: 32 hex chars (16 bytes)
MAX_KEYSET = presets.KVN_MAX
SKIP_REPORT_MAX = 20        # the report names the first N skips, then counts


def _tokens(line):
    """The whitespace tokens of a line, with a leading ``word:`` prefix
    stripped from the first token (``var_out:IMSI ICCID ...`` -> IMSI, ICCID).
    A config line like ``ICCID:897...`` therefore yields a single value token
    and can never be mistaken for the column line."""
    parts = str(line or '').split()
    if parts and ':' in parts[0]:
        parts[0] = parts[0].split(':')[-1]
    return [p for p in parts if p]


# A column name: an identifier (IMSI, ICCID, KIC1, PSK_ID, ...).  The shape
# check keeps a configuration line that names a value next to a column
# (``ICCID 897...`` - the value is not an identifier) from being read as the
# column line.
_COLUMN_NAME_RE = re.compile(r'[A-Za-z][A-Za-z0-9_]*')

# The columns the presets use, beyond ICCID itself.  A candidate header must
# carry at least one of them: a line with ICCID and nothing usable cannot
# yield a card (every keyset needs a KICn/KIDn pair), so it is not the header.
_FIXED_COLUMNS = ('ADM', 'PIN1', 'PIN2', 'PUK1', 'PUK2', 'PSK', 'PSKID', 'PSK_ID')


def _is_known_column(name):
    """A column the import consumes: a fixed name or a numbered KIC/KID."""
    if name in _FIXED_COLUMNS:
        return True
    m = re.fullmatch(r'(?:KIC|KID)([0-9]{1,2})', name)
    return bool(m) and 1 <= int(m.group(1)) <= MAX_KEYSET


def find_columns(text):
    """The column-name line of an outfile: the first line whose tokens all
    look like column names, include ``ICCID`` and carry at least one other
    column the import consumes (at least two tokens in total).  Returns
    ``(columns, line_index)`` with the column names uppercased, or
    ``(None, -1)``."""
    for i, line in enumerate(str(text or '').splitlines()):
        toks = _tokens(line)
        if len(toks) < 2 or not all(_COLUMN_NAME_RE.fullmatch(t) for t in toks):
            continue
        names = [t.upper() for t in toks]
        if 'ICCID' not in names:
            continue
        if not any(n != 'ICCID' and _is_known_column(n) for n in names):
            continue
        return names, i
    return None, -1


def _column_index(columns, name):
    try:
        return columns.index(name)
    except ValueError:
        return -1


def _keyset_columns(columns):
    """The numbered KIC/KID column pairs: ``{n: (kic_index, kid_index)}`` for
    the numbers both sides carry."""
    kic, kid = {}, {}
    for i, c in enumerate(columns):
        m = re.fullmatch(r'KIC([0-9]{1,2})', c)
        if m and 1 <= int(m.group(1)) <= MAX_KEYSET:
            kic[int(m.group(1))] = i
        m = re.fullmatch(r'KID([0-9]{1,2})', c)
        if m and 1 <= int(m.group(1)) <= MAX_KEYSET:
            kid[int(m.group(1))] = i
    return {n: (kic[n], kid[n]) for n in sorted(set(kic) & set(kid))}


def default_tars(tar_keyset=None):
    """The TAR table for the imported cards: the three mandatory role entries
    with their spec defaults and MSL 16, each with the default keyset (None
    leaves it to the store's single-keyset fill)."""
    out = []
    for role in presets.TAR_ROLES:
        d = presets.TAR_ROLE_DEFAULTS[role]
        out.append({'role': role, 'tar': d['tar'], 'msl': d['msl'],
                    'desc': '', 'keyset': tar_keyset})
    return out


def normalise_tars(tars, tar_keyset=None):
    """The caller's TAR table (or the role defaults) cleaned: the roles kept,
    the TAR/MSL uppercased, the global default keyset applied to every
    entry.  Raises ValueError on a malformed table (the caller answers 400)."""
    entries = tars if isinstance(tars, list) and tars else default_tars()
    out = []
    for t in entries:
        if not isinstance(t, dict):
            raise ValueError('invalid TAR entry')
        role = str(t.get('role') or '').strip()
        tar = re.sub(r'\s', '', str(t.get('tar') or '')).upper()
        msl = re.sub(r'\s', '', str(t.get('msl') or '')).upper()
        if not re.fullmatch(r'[0-9A-F]{6}', tar):
            raise ValueError('TAR must be three hex bytes: %r' % tar)
        if not re.fullmatch(r'[0-9A-F]{2}', msl):
            raise ValueError('MSL must be one hex byte: %r' % msl)
        ks_raw = t.get('keyset')
        if ks_raw in (None, ''):
            keyset = None
        else:
            keyset = presets.normalise_keyset_ref(ks_raw)
            if keyset is None:
                raise ValueError('keyset must be 1-%02X: %r'
                                 % (presets.KVN_MAX, ks_raw))
        out.append({'role': role if role in presets.TAR_ROLES else '',
                    'tar': tar, 'msl': msl,
                    'desc': str(t.get('desc') or '').strip(),
                    'keyset': keyset or (tar_keyset or None)})
    seen = set()
    for t in out:
        if t['tar'] in seen:
            raise ValueError('duplicate TAR %s' % t['tar'])
        seen.add(t['tar'])
    return out


def _key_algo(size_bytes, algorithm16):
    if size_bytes in _KEY_ALGO:
        return _KEY_ALGO[size_bytes]
    if size_bytes == 16:
        return _KEY_ALGO_16.get(str(algorithm16 or '3des2').strip().lower(), 0x05)
    return None


def _hex_value(tok):
    """A row token as an even-length uppercase hex string, or ''."""
    v = re.sub(r'[^0-9A-Fa-f]', '', str(tok or '')).upper()
    return v if v and len(v) % 2 == 0 else ''


def parse_outfile(text, name='', algorithm16='3des2', tars=None, tar_keyset=None):
    """Parse an outfile into preset dicts (the store's shape).

    Returns ``{'presets': [...], 'report': {...}}``.  Every skipped row or key
    is named in the report (up to ``SKIP_REPORT_MAX`` lines, then counted) -
    nothing is silently dropped.  Raises ValueError on a malformed TAR table.
    """
    columns, line_index = find_columns(text)
    report = {'columns': columns or [], 'header_lines': max(line_index, 0),
              'rows': 0, 'cards': 0, 'keysets': [], 'tars': 0,
              'skipped': [], 'skipped_more': 0}
    if columns is None:
        report['error'] = ('no column-name line found - the data must start '
                           'with a whitespace-separated header containing ICCID')
        return {'presets': [], 'report': report}
    idx = {c: _column_index(columns, c)
           for c in ('ICCID', 'ADM', 'PIN1', 'PIN2', 'PUK1', 'PUK2',
                     'PSK', 'PSKID', 'PSK_ID')}
    if idx['PSKID'] < 0:
        idx['PSKID'] = idx['PSK_ID']
    pairs = _keyset_columns(columns)
    report['keysets'] = ['KIC%d/KID%d' % (n, n) for n in pairs]
    tars_base = normalise_tars(tars, tar_keyset)
    report['tars'] = len(tars_base)
    used_max = max([idx['ICCID']] + [i for pair in pairs.values() for i in pair])

    def skip(msg):
        if len(report['skipped']) < SKIP_REPORT_MAX:
            report['skipped'].append(msg)
        else:
            report['skipped_more'] += 1

    presets_out = []
    lines = str(text or '').splitlines()
    for row_no, line in enumerate(lines[line_index + 1:], start=line_index + 2):
        # The data rows split plainly: the ``word:`` prefix strip belongs to
        # the header detection and must not touch a value.
        toks = str(line).split()
        if not toks:
            continue
        if len(toks) <= used_max:
            skip('line %d: too few columns' % row_no)
            continue
        report['rows'] += 1
        iccid = presets.normalize_iccid(toks[idx['ICCID']]) if idx['ICCID'] >= 0 else ''
        if not iccid:
            skip('line %d: no ICCID' % row_no)
            continue
        keysets = []
        for n, (ki, di) in pairs.items():
            kic_key = _hex_value(toks[ki])
            kid_key = _hex_value(toks[di])
            if not kic_key or not kid_key:
                skip('line %d: keyset %d has no KIC/KID' % (row_no, n))
                continue
            if len(kic_key) != len(kid_key):
                skip('line %d: keyset %d KIC/KID sizes differ' % (row_no, n))
                continue
            algo = _key_algo(len(kic_key) // 2, algorithm16)
            if algo is None:
                skip('line %d: keyset %d has an unsupported key size (%d bytes)'
                     % (row_no, n, len(kic_key) // 2))
                continue
            byte = '%02X' % ((n << 4) | algo)
            keysets.append({'kic': byte, 'kid': byte,
                            'kicKey': kic_key, 'kidKey': kid_key})
        if not keysets:
            skip('line %d: no usable keyset' % row_no)
            continue
        # Every TAR gets the caller's global default keyset; without one the
        # card's first keyset is used (a multi-keyset card would otherwise
        # fail the store's "a non-zero MSL needs a default keyset" rule).
        # A per-entry keyset (chosen in the preview) wins; the caller's global
        # default - else the card's first keyset - fills the rest.
        card_keyset = tar_keyset or presets.keyset_kvn(keysets[0])
        card_tars = [dict(t, keyset=t.get('keyset') or card_keyset)
                     for t in tars_base]
        entry = {'name': str(name or '').strip() or iccid, 'iccid': iccid,
                 'keysets': keysets, 'tars': card_tars}
        for field, col in (('pin1', 'PIN1'), ('pin2', 'PIN2'),
                           ('puk1', 'PUK1'), ('puk2', 'PUK2')):
            i = idx[col]
            raw = str(toks[i]).strip() if 0 <= i < len(toks) else ''
            if not raw:
                continue        # the column is simply absent from this row
            v = re.sub(r'\D', '', raw)
            if re.fullmatch(r'[0-9]{4,8}', v):
                entry[field] = v
            else:
                skip('line %d: %s %r is not 4-8 digits - dropped'
                     % (row_no, col, raw))
        i = idx['ADM']
        if 0 <= i < len(toks) and toks[i]:
            # The ADM column carries the key's hex value itself (the length
            # varies) - the form the preset store and /api/verify-adm use.
            adm = re.sub(r'[^0-9A-Fa-f]', '', str(toks[i])).upper()
            # Byte pairs, exactly like /api/verify-adm: an odd length is a half
            # byte and the verifier would always refuse the stored value
            # (v3.28.3).
            if re.fullmatch(presets.ADM_PATTERN, adm):
                entry['adm'] = adm
            else:
                skip('line %d: ADM is not 4-16 hex digits in byte pairs - dropped'
                     % row_no)
        i = idx['PSK']
        if 0 <= i < len(toks) and toks[i]:
            psk = _hex_value(toks[i])
            if len(psk) == PSK_KEY_HEX_LEN:
                entry['pskKey'] = psk
                pi = idx['PSKID']
                ident = str(toks[pi]).strip() if 0 <= pi < len(toks) else ''
                entry['pskIdentity'] = ident or iccid
            else:
                skip('line %d: PSK is not 16 bytes (32 hex) - dropped' % row_no)
        presets_out.append(entry)
    report['cards'] = len(presets_out)
    return {'presets': presets_out, 'report': report}
