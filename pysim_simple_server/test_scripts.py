"""Server-side test script store: the card test scripts the runner executes.

Test scripts used to live in the browser's localStorage, although the runner
that executes them is server-side - the store makes the bench state one unit
(next to the card presets), lets every browser (or a plain API client) manage
the same set, and validates a script before it is saved, so what is stored is
what the runner accepts.

A stored script is the script JSON (``name`` + ``steps``, the same shape the
PWA exports) plus a stable uuid and timestamps::

    {"version": 1, "scripts": [
        {"id": "...", "name": "applet RFM update", "steps": [...],
         "created": 1690000000.0, "updated": 1690000000.0}]}

Validation runs on every mutation: the basic shape always, and - when the
server passes its ``validator`` - the full ``testscript.normalise_script``
pass (the same one the runner uses at start).  Import accepts the old
localStorage shape (an array of ``{name, steps}``); there is no automatic
migration - export/import is the path.

Layout (``--test-scripts`` overrides the file; ``Path.home()`` resolves the
same way on Linux, macOS and Windows)::

    ~/.pysim-simple-server/test_scripts.json

Writes are atomic (temp file + fsync + ``os.replace``) and every mutation
holds an ``RLock``.  Scripts carry a stable uuid - never array indices.
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
DEFAULT_FILENAME = 'test_scripts.json'
SCHEMA_VERSION = 1


def default_path():
    """The default store: ``~/.pysim-simple-server/test_scripts.json``."""
    return Path.home() / DEFAULT_DIR / DEFAULT_FILENAME


class TestScriptError(Exception):
    """A refused mutation (validation, unknown id, ...)."""


class TestScriptStore:
    """The test script store.  All public methods are thread-safe and return
    copies - callers can never mutate the store's own state."""

    def __init__(self, path=None, validator=None):
        self.path = Path(path) if path else default_path()
        self._validator = validator
        self._lock = threading.RLock()
        self._scripts = []
        self._load()

    # ------------------------------------------------------------------ disk
    def _load(self):
        try:
            raw = self.path.read_text(encoding='utf-8')
        except FileNotFoundError:
            self._scripts = []
            return
        except OSError as e:
            sys.stderr.write('TESTSCRIPTS: cannot read %s: %s\n' % (self.path, e))
            self._scripts = []
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
                sys.stderr.write('TESTSCRIPTS: %s is not valid JSON (%s) - kept as '
                                 '%s, starting empty\n' % (self.path, e, bad))
            except OSError:
                sys.stderr.write('TESTSCRIPTS: %s is not valid JSON (%s)\n'
                                 % (self.path, e))
            self._scripts = []
            return
        items = data.get('scripts') if isinstance(data, dict) else None
        if not isinstance(items, list):
            items = []
        loaded = []
        for entry in items:
            try:
                loaded.append(self._normalise(entry, keep_meta=True))
            except TestScriptError as e:
                sys.stderr.write('TESTSCRIPTS: dropping an invalid entry: %s\n' % e)
        self._scripts = loaded

    def _save(self):
        data = {'version': SCHEMA_VERSION, 'scripts': self._scripts}
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

    # ------------------------------------------------------------- shaping
    def _check(self, raw):
        """The script dict, validated: the basic shape always, the full engine
        pass when a validator was provided.  The *input* shape is kept - the
        engine's normalised output is not meant to be re-normalised (it fills
        defaults like an 'any' PoR check), and the runner normalises again at
        start, so storing what was submitted keeps the store re-validatable and
        hand-editable."""
        if not isinstance(raw, dict):
            raise TestScriptError('script must be an object')
        name = raw.get('name')
        if name is None:
            name = ''
        if not isinstance(name, str):
            raise TestScriptError('script name must be a string')
        steps = raw.get('steps')
        if not isinstance(steps, list) or not steps:
            raise TestScriptError('script must have at least one step')
        for i, step in enumerate(steps):
            if not isinstance(step, dict) or step.get('type') not in ('action', 'expect'):
                raise TestScriptError('step %d must be an action or an expectation'
                                      % (i + 1))
        script = {'name': name.strip(), 'steps': copy.deepcopy(steps)}
        if self._validator is not None:
            try:
                self._validator(copy.deepcopy(script))
            except Exception as e:
                # testscript.ScriptError is a ValueError: pass its message on
                raise TestScriptError(str(e))
        return script

    def _normalise(self, entry, keep_meta=False):
        """A stored entry: the validated script plus id and timestamps.  With
        ``keep_meta`` the stored id/timestamps are preserved (a load), else
        fresh ones are made (an add)."""
        if not isinstance(entry, dict):
            raise TestScriptError('script must be an object')
        script = self._check(entry)
        now = time.time()
        rid = str(entry.get('id') or '').strip().lower()
        if not re.fullmatch(r'[0-9a-f]{32}', rid):
            rid = uuid.uuid4().hex
        if keep_meta:
            created = entry.get('created')
            updated = entry.get('updated')
            created = created if isinstance(created, (int, float)) else now
            updated = updated if isinstance(updated, (int, float)) else created
        else:
            created = updated = now
        return {'id': rid, 'name': script['name'], 'steps': script['steps'],
                'created': created, 'updated': updated}

    def _find(self, sid):
        sid = str(sid or '').strip().lower()
        for s in self._scripts:
            if s['id'] == sid:
                return s
        return None

    # -------------------------------------------------------------- public
    def list(self):
        with self._lock:
            return [copy.deepcopy(s) for s in self._scripts]

    def get(self, sid):
        with self._lock:
            s = self._find(sid)
            return copy.deepcopy(s) if s else None

    def add(self, script):
        with self._lock:
            s = self._normalise(script or {})
            if self._find(s['id']):
                # a caller-supplied id that is already taken never wins: ids
                # must stay unique (the same guard the import uses)
                s['id'] = uuid.uuid4().hex
            self._scripts.append(s)
            self._save()
            sys.stderr.write('TESTSCRIPTS: added %s (%s)\n'
                             % (s['name'] or '(unnamed)', s['id']))
            return copy.deepcopy(s)

    def update(self, sid, fields):
        """Partial or full update: name/steps replaced, id and created kept."""
        with self._lock:
            cur = self._find(sid)
            if cur is None:
                return None
            merged = {'name': cur['name'], 'steps': copy.deepcopy(cur['steps'])}
            if isinstance(fields, dict):
                if 'name' in fields:
                    merged['name'] = fields['name']
                if 'steps' in fields:
                    merged['steps'] = fields['steps']
            script = self._check(merged)
            new = {'id': cur['id'], 'name': script['name'],
                   'steps': script['steps'], 'created': cur['created'],
                   'updated': time.time()}
            self._scripts[self._scripts.index(cur)] = new
            self._save()
            return copy.deepcopy(new)

    def remove(self, sid):
        with self._lock:
            s = self._find(sid)
            if s is None:
                return False
            self._scripts.remove(s)
            self._save()
            sys.stderr.write('TESTSCRIPTS: removed %s (%s)\n'
                             % (s['name'] or '(unnamed)', s['id']))
            return True

    def import_scripts(self, items, mode='merge'):
        """Import exported (or old localStorage) scripts: entries are validated
        one by one; an already-used id gets a fresh one, invalid entries are
        reported instead of failing the whole import.  ``replace`` wipes the
        store first."""
        added, skipped, errors = 0, 0, []
        with self._lock:
            if mode == 'replace':
                self._scripts = []
            for i, entry in enumerate(items or []):
                if not isinstance(entry, dict):
                    skipped += 1
                    continue
                try:
                    s = self._normalise(entry)
                    if self._find(s['id']):
                        s['id'] = uuid.uuid4().hex
                except TestScriptError as e:
                    skipped += 1
                    errors.append('%s: %s' % (entry.get('name') or ('#' + str(i + 1)), e))
                    continue
                self._scripts.append(s)
                added += 1
            if added or mode == 'replace':
                self._save()
            sys.stderr.write('TESTSCRIPTS: import %s: %d added, %d skipped\n'
                             % (mode, added, skipped))
        return {'added': added, 'skipped': skipped, 'errors': errors[:10]}

    def info(self):
        with self._lock:
            return {'path': str(self.path), 'count': len(self._scripts),
                    'version': SCHEMA_VERSION}
