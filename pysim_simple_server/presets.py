"""Server-side card presets: SCP80/SCP81 key material, counters and per-card
parameters, kept in one JSON file.

The presets used to live in the browser's localStorage and the PWA wrote the
SCP80 counter back after every operation.  The counter is card state (the card
rejects a packet whose counter is not above its own), so the server - which
performs every card operation and already computes the accepted counter - now
owns the store: an accepted counter is persisted in the same operation, and a
closed tab, a lost response or a second browser window can no longer lose an
increment.

A preset holds a **TAR table**: every Remote Management application the card
exposes is addressed by its TAR, and each carries its own **Minimum Security
Level** (MSL) - the "Minimum SPI1" the card checks before it processes a
secured packet (TS 102 226 6.1/8.2.1.3.2.4; a too-low SPI1 is answered with
response status 0A "Insufficient security level").  The three role entries
(`isd`, `uiccRfm`, `usimRfm`) are mandatory - the Cards form pre-settles their
values - and further TARs can be added with an optional description.  A packet
for a TAR derives its SPI1 from that TAR's MSL; the preset no longer carries a
card-wide SPI1/SPI2 (SPI2 is an operation property: whether and how a Proof of
Receipt is requested).

A preset holds **several keysets**; the b8..b5 nibble of KIc/KID numbers them
(TS 102 225 5.1.2/A.2).  Each keyset has its own KIc/KID keys and its own
counter - "a dedicated counter shall be associated to each key version"
(TS 102 225 Annex A.1, the spec's wording).  The two bytes of a keyset must
carry the same number (A.2: the card rejects a mismatch with "Unidentified
security error"); number '00' is reserved and not a keyset (it means "no
security" in a packet, which is a form-level choice, not a preset).

Layout (``--card-presets`` overrides the file; ``Path.home()`` resolves the
same way on Linux, macOS and Windows)::

    ~/.pysim-simple-server/card_presets.json
    ~/.pysim-simple-server/card_presets.json.audit.jsonl   (counter changes)

Writes are atomic (temp file + fsync + ``os.replace``) and every mutation
holds an ``RLock``, so request handlers and the operation threads can share one
store.  Presets carry a stable uuid - never array indices.  The JSON is
deliberately human-readable: hand-editing a counter or moving the file between
machines is part of the workbench workflow, and the Cards tab's export/import
uses the same shape (which is also how old localStorage presets move over -
there is no automatic migration).  v3.8.0's flat ``kic/kid/kicKey/kidKey/cntr``
shape is converted into a single keyset, and a v3.9.1 preset's card-wide
``spi1``/``tar``/``uiccTar``/``usimTar`` become the three role entries (the
spi1 as their MSL) - on load and on import alike.
"""

import copy
import json
import os
import re
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

DEFAULT_DIR = '.pysim-simple-server'
DEFAULT_FILENAME = 'card_presets.json'
SCHEMA_VERSION = 3

# The fields a preset carries: the localStorage shape plus the id.  The OTA key
# material lives in `keysets` - the b8..b5 nibble of KIc/KID numbers them,
# each with its own counter (TS 102 225 Annex A.1: "a dedicated counter shall
# be associated to each key version"); the TAR table lives in `tars` (see
# TAR_ROLES).
PRESET_FIELDS = ('name', 'iccid', 'adm', 'pskIdentity', 'pskKey')
# The v3.8.0 flat key fields: converted into a single keyset on load/import.
LEGACY_KEY_FIELDS = ('kic', 'kid', 'kicKey', 'kidKey', 'cntr')
KEYSET_FIELDS = ('kic', 'kid', 'kicKey', 'kidKey', 'cntr')
HEX_FIELDS = ('adm', 'pskKey')
KEYSET_HEX_FIELDS = ('kic', 'kid', 'kicKey', 'kidKey', 'cntr')
# The TAR table: the three mandatory role entries (their values are pre-settled
# in the Cards form) and free entries with an optional description.  Every
# entry carries the TAR and its MSL (Minimum SPI1, one hex byte).
TAR_ROLES = ('isd', 'uiccRfm', 'usimRfm')
TAR_ROLE_DEFAULTS = {
    'isd': {'tar': '000000', 'msl': '16'},
    'uiccRfm': {'tar': 'B00000', 'msl': '16'},
    'usimRfm': {'tar': 'B00001', 'msl': '16'},
}
COUNTER_BITS = 40      # the SCP80 counter is 5 bytes (TS 31.115)
COUNTER_WIDTH = 10     # ... shown as 10 hex digits
KVN_MAX = 0x0F         # keyset number '00' is reserved: no key set, no counter


def default_path():
    """The default store: ``~/.pysim-simple-server/card_presets.json``."""
    return Path.home() / DEFAULT_DIR / DEFAULT_FILENAME


def keyset_kvn(keyset):
    """The keyset number: the high nibble of KIc (bits b8..b5 of KIc/KID,
    TS 102 225 5.1.2/A.2 - both bytes must agree, so KIc is enough).  None
    when the byte is unusable."""
    kic = str((keyset or {}).get('kic') or '').strip().upper()
    if not re.fullmatch(r'[0-9A-F]{2}', kic):
        return None
    return int(kic, 16) >> 4


def _swap_nibbles(s):
    """Nibble-swap a hex string; an odd trailing nibble becomes 'F' (the JS
    ``swapNibbles`` the PWA uses)."""
    out = []
    for i in range(0, len(s), 2):
        out.append(s[i + 1] if i + 1 < len(s) else 'F')
        out.append(s[i])
    return ''.join(out).upper()


def normalize_iccid(value):
    """ICCID -> plain digits, accepting every stored form (the PWA's
    ``cardsNormIccid``): digits, spaced digits, or the raw nibble-swapped EF
    hex - the swap applies only when the value carries A-F, because a raw
    value without hex letters is indistinguishable from digits.  Empty string
    means "no ICCID"."""
    s = re.sub(r'[\s-]', '', str(value or '')).upper()
    if not s:
        return ''
    if re.search(r'[A-F]', s):
        s = _swap_nibbles(re.sub(r'[^0-9A-F]', '', s))
        s = re.sub(r'F+$', '', s)
    return re.sub(r'^0+', '', re.sub(r'\D', '', s))


def counter_value(cntr):
    """The counter as an int, or None when it is not hex."""
    try:
        return int(re.sub(r'[^0-9A-Fa-f]', '', str(cntr or '')), 16)
    except ValueError:
        return None


def counter_hex(value):
    """The fixed-width 10-hex-digit counter form the card protocol uses."""
    v = counter_value(value)
    if v is None:
        return str(value or '').strip().upper()
    return '%0*X' % (COUNTER_WIDTH, v % (1 << COUNTER_BITS))


def counter_ahead(old, new):
    """True when ``new`` is a plausible forward step from ``old``: the counter
    only moves forward, and a value that wrapped around the 40-bit field
    (top of the range -> small value) counts as forward."""
    o, n = counter_value(old), counter_value(new)
    if o is None or n is None:
        return False
    if n > o:
        return True
    return (o - n) > (1 << (COUNTER_BITS - 1))


def tar_entry(preset, tar_hex):
    """The preset's TAR entry whose TAR matches `tar_hex` (case-insensitive),
    or None."""
    want = re.sub(r'\s', '', str(tar_hex or '')).upper()
    if not want:
        return None
    for t in (preset or {}).get('tars') or []:
        if str(t.get('tar') or '').upper() == want:
            return dict(t)
    return None


def role_tar(preset, role):
    """The TAR of a role entry ('isd' | 'uiccRfm' | 'usimRfm'), or ''."""
    for t in (preset or {}).get('tars') or []:
        if t.get('role') == role:
            return str(t.get('tar') or '')
    return ''


def tar_msl(preset, tar_hex):
    """The MSL (Minimum SPI1) of a TAR, or '' when the preset has no such
    entry or no MSL for it."""
    return str((tar_entry(preset, tar_hex) or {}).get('msl') or '')


class PresetError(Exception):
    """A refused mutation (validation, duplicate ICCID, ...)."""


class PresetStore:
    """The card preset store.  All public methods are thread-safe and return
    copies - callers can never mutate the store's own state."""

    def __init__(self, path=None):
        self.path = Path(path) if path else default_path()
        self.audit_path = self.path.with_name(self.path.name + '.audit.jsonl')
        self._lock = threading.RLock()
        self._presets = []
        self._load()

    # ------------------------------------------------------------------ disk
    def _load(self):
        try:
            raw = self.path.read_text(encoding='utf-8')
        except FileNotFoundError:
            self._presets = []
            return
        except OSError as e:
            sys.stderr.write('PRESETS: cannot read %s: %s\n' % (self.path, e))
            self._presets = []
            return
        try:
            data = json.loads(raw)
        except ValueError as e:
            # Never silently overwrite an unreadable store: keep the file for
            # the user, start empty (the next save would destroy it).
            bad = self.path.with_name(self.path.name + '.bad-%s'
                                      % time.strftime('%Y%m%d-%H%M%S'))
            try:
                self.path.replace(bad)
                sys.stderr.write('PRESETS: %s is not valid JSON (%s) - kept as %s, '
                                 'starting empty\n' % (self.path, e, bad))
            except OSError:
                sys.stderr.write('PRESETS: %s is not valid JSON (%s)\n' % (self.path, e))
            self._presets = []
            return
        items = data.get('presets') if isinstance(data, dict) else None
        if not isinstance(items, list):
            items = []
        self._presets = [self._normalise(p) for p in items if isinstance(p, dict)]

    def _save(self):
        data = {'version': SCHEMA_VERSION, 'presets': self._presets}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_fd, tmp_name = tempfile.mkstemp(prefix=self.path.name + '.',
                                            dir=str(self.path.parent))
        try:
            with os.fdopen(tmp_fd, 'w', encoding='utf-8') as fh:
                json.dump(data, fh, indent=2, ensure_ascii=False)
                fh.write('\n')
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, self.path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        try:
            # Make the rename durable.  Best effort: opening a directory is not
            # portable (Windows) - a failure here never fails the mutation.
            dir_fd = os.open(str(self.path.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass

    def _audit(self, pid, name, old, new, source, kvn=None):
        """One JSONL line per counter change (the keyset number included - each
        keyset has its own counter): the forensic trail for "did an increment
        get lost?" questions."""
        try:
            line = json.dumps({'ts': time.strftime('%Y-%m-%dT%H:%M:%S'),
                               'id': pid, 'name': name, 'kvn': kvn,
                               'old': old, 'new': new,
                               'source': source}, ensure_ascii=False)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.audit_path, 'a', encoding='utf-8') as fh:
                fh.write(line + '\n')
        except OSError as e:
            sys.stderr.write('PRESETS: audit write failed: %s\n' % e)

    # ------------------------------------------------------------- shaping
    def _normalise(self, fields):
        """The stored shape: the known fields, cleaned; unknown keys dropped."""
        out = {}
        for k in PRESET_FIELDS:
            v = fields.get(k)
            v = '' if v is None else str(v).strip()
            if k in HEX_FIELDS:
                v = re.sub(r'\s', '', v).upper()
            out[k] = v
        out['tars'] = self._normalise_tars(fields)
        # Keysets: a v3.8.0 preset (flat kic/kid/kicKey/kidKey/cntr) converts
        # into a single keyset, so old files and exports keep working.
        raw = fields.get('keysets')
        if not isinstance(raw, list):
            raw = []
            if any(str(fields.get(k) or '').strip() for k in LEGACY_KEY_FIELDS):
                raw = [{k: fields.get(k) for k in LEGACY_KEY_FIELDS}]
        out['keysets'] = [self._normalise_keyset(ks) for ks in raw
                          if isinstance(ks, dict)]
        pid = str(fields.get('id') or '').strip().lower()
        out['id'] = pid or uuid.uuid4().hex
        return out

    def _normalise_tars(self, fields):
        """The preset's TAR table: the three mandatory role entries (values
        filled from the defaults when empty, so a partial API call and the
        pre-settled form both work) followed by the free entries in input
        order.  A v3.9.1 preset (tar/uiccTar/usimTar + the card-wide spi1)
        converts: the spi1 becomes the MSL of all three roles."""
        raw = fields.get('tars')
        entries = []
        if isinstance(raw, list):
            for t in raw:
                if not isinstance(t, dict):
                    continue
                role = str(t.get('role') or '').strip()
                entries.append({
                    'role': role if role in TAR_ROLES else '',
                    'tar': re.sub(r'\s', '', str(t.get('tar') or '')).upper(),
                    'msl': re.sub(r'\s', '', str(t.get('msl') or '')).upper(),
                    'desc': str(t.get('desc') or '').strip(),
                })
        else:
            msl = re.sub(r'\s', '', str(fields.get('spi1') or '')).upper()
            for role, src in (('isd', 'tar'), ('uiccRfm', 'uiccTar'),
                              ('usimRfm', 'usimTar')):
                entries.append({
                    'role': role,
                    'tar': re.sub(r'\s', '', str(fields.get(src) or '')).upper(),
                    'msl': msl,
                    'desc': '',
                })
        out = []
        for role in TAR_ROLES:
            cur = next((e for e in entries if e['role'] == role), None)
            d = TAR_ROLE_DEFAULTS[role]
            out.append({'role': role,
                        'tar': (cur or {}).get('tar') or d['tar'],
                        'msl': (cur or {}).get('msl') or d['msl'],
                        'desc': ''})
        for e in entries:
            if e['role']:
                continue
            if not (e['tar'] or e['msl'] or e['desc']):
                continue        # a blank editor row
            # free entries carry no `role`: the three roles are the fixed ones
            out.append({'tar': e['tar'], 'msl': e['msl'], 'desc': e['desc']})
        return out

    def _normalise_keyset(self, ks):
        """One keyset, cleaned.  The counter is kept as typed here and
        validated + padded in _validate_keyset() - a bad value must be
        refused, never silently masked."""
        out = {}
        for k in KEYSET_FIELDS:
            v = ks.get(k)
            v = '' if v is None else str(v).strip()
            if k in KEYSET_HEX_FIELDS:
                v = re.sub(r'\s', '', v).upper()
            out[k] = v
        out['cntr'] = out['cntr'] or '1'
        return out

    def _validate(self, p, skip_id=None):
        if not p['name']:
            raise PresetError('name is required')
        if bool(p['pskIdentity']) != bool(p['pskKey']):
            raise PresetError('PSK identity and PSK key must be set together')
        if p['pskKey'] and not re.fullmatch(r'[0-9A-F]{32}', p['pskKey']):
            raise PresetError('PSK key must be 32 hex characters')
        if not p['keysets']:
            raise PresetError('at least one keyset is required')
        seen = set()
        for ks in p['keysets']:
            self._validate_keyset(ks)
            kvn = keyset_kvn(ks)
            if kvn in seen:
                raise PresetError('duplicate keyset number %02X in this preset' % kvn)
            seen.add(kvn)
        seen_tars = set()
        for t in p['tars']:
            if not re.fullmatch(r'[0-9A-F]{6}', t['tar']):
                raise PresetError('TAR must be three hex bytes: %r' % t['tar'])
            if not t['msl']:
                raise PresetError('TAR %s needs its MSL (Minimum SPI1, one hex '
                                  'byte, TS 102 226 8.2.1.3.2.4)' % t['tar'])
            if not re.fullmatch(r'[0-9A-F]{2}', t['msl']):
                raise PresetError('MSL must be one hex byte: %r' % t['msl'])
            if t['tar'] in seen_tars:
                raise PresetError('duplicate TAR %s in this preset' % t['tar'])
            seen_tars.add(t['tar'])
        norm = normalize_iccid(p['iccid'])
        if norm:
            for other in self._presets:
                if other['id'] == skip_id:
                    continue
                if normalize_iccid(other['iccid']) == norm:
                    raise PresetError('card with this ICCID already exists: %s'
                                      % (other['name'] or other['id']))
        return p

    def _validate_keyset(self, ks):
        """One keyset: KIc/KID carry its number in b8..b5 and must agree
        (TS 102 225 A.2 - a mismatch is rejected by the card), the number is
        01-0F ('00' means "no security" and has no keys to define, so it is
        not a preset keyset), both keys are present and the counter is
        1-10 hex digits (the dedicated counter of that keyset, A.1)."""
        for key in ('kic', 'kid'):
            if not re.fullmatch(r'[0-9A-F]{2}', ks[key]):
                raise PresetError('%s must be one hex byte' % key)
        kic_v, kid_v = int(ks['kic'], 16) >> 4, int(ks['kid'], 16) >> 4
        if kic_v != kid_v:
            raise PresetError('KIc and KID must carry the same keyset number '
                              '(TS 102 225 A.2): %s / %s' % (ks['kic'], ks['kid']))
        if not 1 <= kic_v <= KVN_MAX:
            raise PresetError('keyset number must be 01-0F: %s' % ks['kic'])
        for key in ('kicKey', 'kidKey'):
            if not ks[key]:
                raise PresetError('%s is required' % key)
        if not re.fullmatch(r'[0-9A-F]{1,10}', ks['cntr']):
            raise PresetError('counter must be 1-10 hex digits')
        # validated: store the protocol's fixed-width form
        ks['cntr'] = counter_hex(ks['cntr'])
        return ks

    def _find(self, pid):
        want = str(pid or '').strip().lower()
        if not want:
            return None
        for p in self._presets:
            if p['id'] == want:
                return p
        return None

    # -------------------------------------------------------------- public
    def list(self):
        with self._lock:
            return [copy.deepcopy(p) for p in self._presets]

    def get(self, pid):
        with self._lock:
            p = self._find(pid)
            return copy.deepcopy(p) if p else None

    def find_by_iccid(self, iccid):
        norm = normalize_iccid(iccid)
        if not norm:
            return None
        with self._lock:
            for p in self._presets:
                if normalize_iccid(p['iccid']) == norm:
                    return copy.deepcopy(p)
        return None

    def add(self, fields):
        with self._lock:
            p = self._normalise(fields or {})
            if self._find(p['id']):
                # a caller-supplied id that is already taken never wins: ids
                # must stay unique (the same guard the import uses)
                p['id'] = uuid.uuid4().hex
            self._validate(p)
            self._presets.append(p)
            self._save()
            sys.stderr.write('PRESETS: added %s (%s)\n' % (p['name'], p['id']))
            return copy.deepcopy(p)

    def update(self, pid, fields):
        """Partial or full update (the human edit path): a counter in a keyset
        is written as given - the Cards tab is the place to resync it
        deliberately.  The counter an *operation* accepted goes through
        set_counter()."""
        with self._lock:
            cur = self._find(pid)
            if cur is None:
                return None
            merged = copy.deepcopy(cur)
            for k in PRESET_FIELDS:
                if isinstance(fields, dict) and k in fields:
                    merged[k] = fields[k]
            if isinstance(fields, dict) and 'keysets' in fields:
                merged['keysets'] = fields['keysets']
            if isinstance(fields, dict) and 'tars' in fields:
                if not isinstance(fields['tars'], list):
                    raise PresetError('tars must be a list of TAR entries')
                merged['tars'] = fields['tars']
            new = self._normalise(merged)
            new['id'] = cur['id']
            self._validate(new, skip_id=cur['id'])
            self._presets[self._presets.index(cur)] = new
            self._save()
            for old_ks, new_ks in zip(cur['keysets'], new['keysets']):
                if old_ks['cntr'] != new_ks['cntr']:
                    kvn = keyset_kvn(new_ks)
                    self._audit(new['id'], new['name'], old_ks['cntr'],
                                new_ks['cntr'], 'edit', kvn)
                    sys.stderr.write('PRESETS: counter %s kvn=%02X %s -> %s (edit)\n'
                                     % (new['name'], kvn, old_ks['cntr'], new_ks['cntr']))
            return copy.deepcopy(new)

    def remove(self, pid):
        with self._lock:
            p = self._find(pid)
            if p is None:
                return False
            self._presets.remove(p)
            self._save()
            sys.stderr.write('PRESETS: removed %s (%s)\n' % (p['name'], p['id']))
            return True

    def find_keyset(self, pid, kvn):
        """The keyset of a preset with the given number (the b8..b5 nibble of
        KIc/KID), or None when the preset or the number is unknown."""
        with self._lock:
            p = self._find(pid)
            if p is None:
                return None
            for ks in p['keysets']:
                if keyset_kvn(ks) == kvn:
                    return copy.deepcopy(ks)
        return None

    def set_counter(self, pid, cntr, source='operation', kvn=None):
        """Persist the counter an operation accepted, into the keyset it used
        (each keyset has its own dedicated counter, TS 102 225 A.1).
        Monotonic per keyset: the counter only moves
        forward, so a stale caller can never regress it.  `kvn=None` picks the
        preset's only keyset (a convenience for single-keyset callers).
        Returns the stored preset (or None when the id is unknown)."""
        with self._lock:
            p = self._find(pid)
            if p is None:
                return None
            ks = None
            if kvn is not None:
                for cand in p['keysets']:
                    if keyset_kvn(cand) == kvn:
                        ks = cand
                        break
                if ks is None:
                    sys.stderr.write('PRESETS: no keyset %s in %s (%s)\n'
                                     % (kvn, p['name'], source))
                    return copy.deepcopy(p)
            elif len(p['keysets']) == 1:
                ks = p['keysets'][0]
            else:
                sys.stderr.write('PRESETS: %s has several keysets - the '
                                 'counter needs one (%s)\n' % (p['name'], source))
                return copy.deepcopy(p)
            new = str(cntr or '').strip().upper()
            if not re.fullmatch(r'[0-9A-F]{1,10}', new):
                sys.stderr.write('PRESETS: ignoring malformed counter %r (%s)\n'
                                 % (cntr, source))
                return copy.deepcopy(p)
            new = counter_hex(new)
            if new == ks['cntr']:
                return copy.deepcopy(p)
            if not counter_ahead(ks['cntr'], new):
                sys.stderr.write('PRESETS: ignoring backwards counter kvn=%02X '
                                 '%s -> %s (%s)\n'
                                 % (keyset_kvn(ks), ks['cntr'], new, source))
                return copy.deepcopy(p)
            old = ks['cntr']
            ks['cntr'] = new
            self._save()
            self._audit(p['id'], p['name'], old, new, source, keyset_kvn(ks))
            sys.stderr.write('PRESETS: counter %s kvn=%02X %s -> %s (%s)\n'
                             % (p['name'], keyset_kvn(ks), old, new, source))
            return copy.deepcopy(p)

    def import_presets(self, items, mode='merge'):
        """Import exported (or old localStorage) presets: entries are validated
        one by one; a duplicate ICCID or an already-used id is skipped, invalid
        entries are reported instead of failing the whole import.  ``replace``
        wipes the store first."""
        added, skipped, errors = 0, 0, []
        with self._lock:
            if mode == 'replace':
                self._presets = []
            for i, entry in enumerate(items or []):
                if not isinstance(entry, dict):
                    skipped += 1
                    continue
                try:
                    p = self._normalise(entry)
                    if self._find(p['id']):
                        p['id'] = uuid.uuid4().hex
                    self._validate(p)
                except PresetError as e:
                    skipped += 1
                    errors.append('%s: %s' % (entry.get('name') or ('#' + str(i + 1)), e))
                    continue
                self._presets.append(p)
                added += 1
            if added or mode == 'replace':
                self._save()
            sys.stderr.write('PRESETS: import %s: %d added, %d skipped\n'
                             % (mode, added, skipped))
        return {'added': added, 'skipped': skipped, 'errors': errors[:10]}

    def info(self):
        with self._lock:
            return {'path': str(self.path), 'count': len(self._presets),
                    'version': SCHEMA_VERSION}
