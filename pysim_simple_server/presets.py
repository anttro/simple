"""Server-side card presets: SCP80/SCP81 key material, counters and per-card
parameters, kept in one JSON file.

The presets used to live in the browser's localStorage and the PWA wrote the
SCP80 counter back after every operation.  The counter is card state (the card
rejects a packet whose counter is not above its own), so the server - which
performs every card operation and already computes the accepted counter - now
owns the store: an accepted counter is persisted in the same operation, and a
closed tab, a lost response or a second browser window can no longer lose an
increment.

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
there is no automatic migration).
"""

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
SCHEMA_VERSION = 1

# The fields a preset carries: the localStorage shape plus the id.  Defaults
# mirror the Cards form (spi1/spi2/counter and the per-target TARs).
PRESET_FIELDS = ('name', 'iccid', 'adm', 'kic', 'kid', 'spi1', 'spi2',
                 'tar', 'uiccTar', 'usimTar', 'cntr', 'kicKey', 'kidKey',
                 'pskIdentity', 'pskKey')
HEX_FIELDS = ('adm', 'kic', 'kid', 'spi1', 'spi2', 'tar', 'uiccTar', 'usimTar',
              'cntr', 'kicKey', 'kidKey', 'pskKey')
TAR_DEFAULTS = {'tar': '000000', 'uiccTar': 'B00000', 'usimTar': 'B00001'}
COUNTER_BITS = 40      # the SCP80 counter is 5 bytes (TS 31.115)
COUNTER_WIDTH = 10     # ... shown as 10 hex digits


def default_path():
    """The default store: ``~/.pysim-simple-server/card_presets.json``."""
    return Path.home() / DEFAULT_DIR / DEFAULT_FILENAME


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

    def _audit(self, pid, name, old, new, source):
        """One JSONL line per counter change: the forensic trail for "did an
        increment get lost?" questions."""
        try:
            line = json.dumps({'ts': time.strftime('%Y-%m-%dT%H:%M:%S'),
                               'id': pid, 'name': name, 'old': old, 'new': new,
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
        out['spi1'] = out['spi1'] or '16'
        out['spi2'] = out['spi2'] or '01'
        out['cntr'] = counter_hex(out['cntr'] or '1')
        for k, d in TAR_DEFAULTS.items():
            out[k] = out[k] or d
        pid = str(fields.get('id') or '').strip().lower()
        out['id'] = pid or uuid.uuid4().hex
        return out

    def _validate(self, p, skip_id=None):
        if not p['name']:
            raise PresetError('name is required')
        for key in ('kic', 'kid', 'kicKey', 'kidKey'):
            if not p[key]:
                raise PresetError('%s is required' % key)
        if bool(p['pskIdentity']) != bool(p['pskKey']):
            raise PresetError('PSK identity and PSK key must be set together')
        if p['pskKey'] and not re.fullmatch(r'[0-9A-F]{32}', p['pskKey']):
            raise PresetError('PSK key must be 32 hex characters')
        norm = normalize_iccid(p['iccid'])
        if norm:
            for other in self._presets:
                if other['id'] == skip_id:
                    continue
                if normalize_iccid(other['iccid']) == norm:
                    raise PresetError('card with this ICCID already exists: %s'
                                      % (other['name'] or other['id']))
        return p

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
            return [dict(p) for p in self._presets]

    def get(self, pid):
        with self._lock:
            p = self._find(pid)
            return dict(p) if p else None

    def find_by_iccid(self, iccid):
        norm = normalize_iccid(iccid)
        if not norm:
            return None
        with self._lock:
            for p in self._presets:
                if normalize_iccid(p['iccid']) == norm:
                    return dict(p)
        return None

    def add(self, fields):
        with self._lock:
            p = self._normalise(fields or {})
            self._validate(p)
            self._presets.append(p)
            self._save()
            sys.stderr.write('PRESETS: added %s (%s)\n' % (p['name'], p['id']))
            return dict(p)

    def update(self, pid, fields):
        """Partial or full update (the human edit path): the counter is written
        as given - the Cards tab is the place to resync it deliberately.  The
        counter an *operation* accepted goes through set_counter()."""
        with self._lock:
            cur = self._find(pid)
            if cur is None:
                return None
            merged = dict(cur)
            for k in PRESET_FIELDS:
                if isinstance(fields, dict) and k in fields:
                    merged[k] = fields[k]
            new = self._normalise(merged)
            new['id'] = cur['id']
            self._validate(new, skip_id=cur['id'])
            old_cntr = cur['cntr']
            self._presets[self._presets.index(cur)] = new
            self._save()
            if new['cntr'] != old_cntr:
                self._audit(new['id'], new['name'], old_cntr, new['cntr'], 'edit')
                sys.stderr.write('PRESETS: counter %s %s -> %s (edit)\n'
                                 % (new['name'], old_cntr, new['cntr']))
            return dict(new)

    def remove(self, pid):
        with self._lock:
            p = self._find(pid)
            if p is None:
                return False
            self._presets.remove(p)
            self._save()
            sys.stderr.write('PRESETS: removed %s (%s)\n' % (p['name'], p['id']))
            return True

    def set_counter(self, pid, cntr, source='operation'):
        """Persist the counter an operation accepted.  Monotonic: the counter
        only moves forward, so a stale caller can never regress it.  Returns
        the stored preset (or None when the id is unknown)."""
        with self._lock:
            p = self._find(pid)
            if p is None:
                return None
            new = counter_hex(cntr)
            if new == p['cntr']:
                return dict(p)
            if not counter_ahead(p['cntr'], new):
                sys.stderr.write('PRESETS: ignoring backwards counter %s -> %s (%s)\n'
                                 % (p['cntr'], new, source))
                return dict(p)
            old = p['cntr']
            p['cntr'] = new
            self._save()
            self._audit(p['id'], p['name'], old, new, source)
            sys.stderr.write('PRESETS: counter %s %s -> %s (%s)\n'
                             % (p['name'], old, new, source))
            return dict(p)

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
