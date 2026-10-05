"""Server-side test suite store: ordered groups of card test scripts.

The test scripts used to be a flat set; a suite groups them (a suite is the
root object - every script belongs to exactly one suite, ``suite_id`` in the
script store).  The suite keeps the **ordered member list** by reference
(``script_id``), so the scripts stay the single source of truth and moving or
editing one never touches the suite file beyond the reference list.  The
runner executes them in order: an optional ``setup`` script first, the
``member`` scripts, an optional ``teardown`` script last - the roles must be
in that order, so the store normalises the list (setup entries first,
teardown entries last, at most one of each) instead of rejecting a
mis-ordered edit.

A stored suite::

    {"version": 1, "suites": [
        {"id": "...", "name": "alfa regression", "require_adm": true,
         "scripts": [{"script_id": "...", "role": "setup", "on_fail": "stop"},
                     {"script_id": "...", "role": "member", "on_fail": "stop"},
                     {"script_id": "...", "role": "member", "on_fail": "continue"},
                     {"script_id": "...", "role": "teardown", "on_fail": "stop"}],
         "created": ..., "updated": ...}]}

``on_fail`` is the per-member policy: ``stop`` (default) ends the suite at
that member, ``continue`` runs the next one - "which failures cascade" is a
visible per-suite decision.  The store validates the shape on every mutation
(the cross-store rules - the suite exists, the scripts exist, a non-empty
suite cannot be deleted - live in the HTTP layer, which holds both stores);
a load keeps an entry that no longer validates (a tightened rule or a hand
edit must never destroy it), it is served and fails at run start.

Layout (``--test-suites`` overrides the file; ``Path.home()`` resolves the
same way on Linux, macOS and Windows)::

    ~/.pysim-simple-server/test_suites.json

Writes are atomic (temp file + fsync + ``os.replace``) and every mutation
holds an ``RLock``.  Suites carry a stable uuid - never array indices.
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
DEFAULT_FILENAME = 'test_suites.json'
SCHEMA_VERSION = 1

ROLES = ('setup', 'member', 'teardown')
ON_FAIL_LEVELS = ('stop', 'continue')
MAX_SCRIPTS = 200

_ID_RE = re.compile(r'[0-9a-f]{32}')


def default_path():
    """The default store: ``~/.pysim-simple-server/test_suites.json``."""
    return Path.home() / DEFAULT_DIR / DEFAULT_FILENAME


class TestSuiteError(Exception):
    """A refused mutation (validation, unknown id, ...)."""


class TestSuiteStore:
    """The test suite store.  All public methods are thread-safe and return
    copies - callers can never mutate the store's own state."""

    def __init__(self, path=None):
        self.path = Path(path) if path else default_path()
        self._lock = threading.RLock()
        self._suites = []
        self._load()

    # ------------------------------------------------------------------ disk
    def _load(self):
        try:
            raw = self.path.read_text(encoding='utf-8')
        except FileNotFoundError:
            self._suites = []
            return
        except OSError as e:
            sys.stderr.write('TESTSUITES: cannot read %s: %s\n' % (self.path, e))
            self._suites = []
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
                sys.stderr.write('TESTSUITES: %s is not valid JSON (%s) - kept as '
                                 '%s, starting empty\n' % (self.path, e, bad))
            except OSError:
                sys.stderr.write('TESTSUITES: %s is not valid JSON (%s)\n'
                                 % (self.path, e))
            self._suites = []
            return
        items = data.get('suites') if isinstance(data, dict) else None
        if not isinstance(items, list):
            items = []
        loaded = []
        for entry in items:
            try:
                s = self._normalise(entry, keep_meta=True)
            except TestSuiteError as e:
                sys.stderr.write('TESTSUITES: dropping an unreadable entry: %s\n' % e)
                continue
            # A shape-valid entry is kept even when a referenced script is
            # gone: served, the editor shows the missing reference, and the
            # HTTP layer refuses a save/run until it is fixed.
            loaded.append(s)
        self._suites = loaded

    def _save(self):
        data = {'version': SCHEMA_VERSION, 'suites': self._suites}
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
            dir_fd = os.open(str(self.path.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass

    # ------------------------------------------------------------- shaping
    @staticmethod
    def _script_entry(raw, index):
        """One member entry: the referenced script id, its role and the
        per-member failure policy."""
        if not isinstance(raw, dict):
            raise TestSuiteError('script entry %d must be an object' % (index + 1))
        sid = str(raw.get('script_id') or '').strip().lower()
        if not _ID_RE.fullmatch(sid):
            raise TestSuiteError('script entry %d: script_id must be a suite '
                                 'uuid' % (index + 1))
        role = str(raw.get('role') or 'member').strip().lower()
        if role not in ROLES:
            raise TestSuiteError('script entry %d: role must be one of %s'
                                 % (index + 1, ', '.join(ROLES)))
        on_fail = str(raw.get('on_fail') or 'stop').strip().lower()
        if on_fail not in ON_FAIL_LEVELS:
            raise TestSuiteError('script entry %d: on_fail must be one of %s'
                                 % (index + 1, ', '.join(ON_FAIL_LEVELS)))
        return {'script_id': sid, 'role': role, 'on_fail': on_fail}

    def _check(self, raw):
        """The suite dict, validated (shape only - the cross-store rules live
        in the HTTP layer): name, the roles-ordered script list."""
        if not isinstance(raw, dict):
            raise TestSuiteError('suite must be an object')
        name = raw.get('name')
        if name is None:
            name = ''
        if not isinstance(name, str):
            raise TestSuiteError('suite name must be a string')
        scripts_raw = raw.get('scripts')
        if scripts_raw is None:
            scripts_raw = []
        if not isinstance(scripts_raw, list):
            raise TestSuiteError('suite scripts must be a list')
        if len(scripts_raw) > MAX_SCRIPTS:
            raise TestSuiteError('a suite may hold at most %d scripts' % MAX_SCRIPTS)
        entries = [self._script_entry(e, i) for i, e in enumerate(scripts_raw)]
        seen = set()
        for e in entries:
            if e['script_id'] in seen:
                raise TestSuiteError('a script appears once per suite: %s'
                                     % e['script_id'])
            seen.add(e['script_id'])
        setups = [e for e in entries if e['role'] == 'setup']
        teardowns = [e for e in entries if e['role'] == 'teardown']
        if len(setups) > 1:
            raise TestSuiteError('a suite has at most one setup script')
        if len(teardowns) > 1:
            raise TestSuiteError('a suite has at most one teardown script')
        # The roles define the order: setup first, teardown last (members keep
        # their relative order in between).
        ordered = setups + [e for e in entries if e['role'] == 'member'] + teardowns
        return {'name': name.strip(), 'require_adm': bool(raw.get('require_adm')),
                'scripts': ordered}

    def _normalise(self, entry, keep_meta=False):
        """A stored entry: the validated suite plus id and timestamps.  With
        ``keep_meta`` the stored id/timestamps are preserved (a load), else
        fresh ones are made (an add)."""
        if not isinstance(entry, dict):
            raise TestSuiteError('suite must be an object')
        suite = self._check(entry)
        now = time.time()
        rid = str(entry.get('id') or '').strip().lower()
        if not _ID_RE.fullmatch(rid):
            rid = uuid.uuid4().hex
        if keep_meta:
            created = entry.get('created')
            updated = entry.get('updated')
            created = created if isinstance(created, (int, float)) else now
            updated = updated if isinstance(updated, (int, float)) else created
        else:
            created = updated = now
        return {'id': rid, 'name': suite['name'],
                'require_adm': suite['require_adm'],
                'scripts': suite['scripts'],
                'created': created, 'updated': updated}

    def _find(self, sid):
        sid = str(sid or '').strip().lower()
        for s in self._suites:
            if s['id'] == sid:
                return s
        return None

    # -------------------------------------------------------------- public
    def list(self):
        with self._lock:
            return [copy.deepcopy(s) for s in self._suites]

    def get(self, sid):
        with self._lock:
            s = self._find(sid)
            return copy.deepcopy(s) if s else None

    def find_by_name(self, name):
        want = str(name or '').strip()
        with self._lock:
            for s in self._suites:
                if s['name'] == want:
                    return copy.deepcopy(s)
        return None

    def add(self, suite):
        with self._lock:
            s = self._normalise(suite or {})
            if self._find(s['id']):
                # a caller-supplied id that is already taken never wins: ids
                # must stay unique (the same guard the import uses)
                s['id'] = uuid.uuid4().hex
            self._suites.append(s)
            self._save()
            sys.stderr.write('TESTSUITES: added %s (%s, %d scripts)\n'
                             % (s['name'] or '(unnamed)', s['id'], len(s['scripts'])))
            return copy.deepcopy(s)

    def update(self, sid, fields):
        """Partial or full update: name/require_adm/scripts replaced, id and
        created kept.  The order/role rules are re-applied (setup first,
        teardown last)."""
        with self._lock:
            cur = self._find(sid)
            if cur is None:
                return None
            merged = {'name': cur['name'], 'require_adm': cur['require_adm'],
                      'scripts': copy.deepcopy(cur['scripts'])}
            if isinstance(fields, dict):
                for key in ('name', 'require_adm', 'scripts'):
                    if key in fields:
                        merged[key] = fields[key]
            suite = self._check(merged)
            new = {'id': cur['id'], 'name': suite['name'],
                   'require_adm': suite['require_adm'],
                   'scripts': suite['scripts'],
                   'created': cur['created'], 'updated': time.time()}
            self._suites[self._suites.index(cur)] = new
            self._save()
            return copy.deepcopy(new)

    def remove(self, sid):
        """Remove a suite.  The HTTP layer refuses this while it still holds
        scripts (the caller checks first); the store itself deletes."""
        with self._lock:
            s = self._find(sid)
            if s is None:
                return False
            self._suites.remove(s)
            self._save()
            sys.stderr.write('TESTSUITES: removed %s (%s)\n'
                             % (s['name'] or '(unnamed)', s['id']))
            return True

    def import_suites(self, items, mode='merge'):
        """Import exported suites: entries are validated one by one; an
        already-used id gets a fresh one, invalid entries are reported instead
        of failing the whole import.  The caller remaps script references (a
        bundle import that had to regenerate a script's uuid rewrites the
        references before this call).  ``replace`` wipes the store first."""
        added, skipped, errors = 0, 0, []
        with self._lock:
            if mode == 'replace':
                self._suites = []
            for i, entry in enumerate(items or []):
                if not isinstance(entry, dict):
                    skipped += 1
                    continue
                try:
                    s = self._normalise(entry)
                    if self._find(s['id']):
                        s['id'] = uuid.uuid4().hex
                except TestSuiteError as e:
                    skipped += 1
                    errors.append('%s: %s' % (entry.get('name') or ('#' + str(i + 1)), e))
                    continue
                self._suites.append(s)
                added += 1
            if added or mode == 'replace':
                self._save()
            sys.stderr.write('TESTSUITES: import %s: %d added, %d skipped\n'
                             % (mode, added, skipped))
        return {'added': added, 'skipped': skipped, 'errors': errors[:10]}

    def info(self):
        with self._lock:
            return {'path': str(self.path), 'count': len(self._suites),
                    'version': SCHEMA_VERSION}
