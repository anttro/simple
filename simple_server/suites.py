# coding=utf-8
"""The cross-store rules of the test suites: references, move/copy, reconcile.

A suite holds **references** to scripts (``script_id`` in its ordered member
list) while every script carries its ``suite_id`` - two stores, one invariant
(every script belongs to exactly one suite and that suite lists it).  The two
stores themselves (:mod:`simple_server.shared.test_suites` and
:mod:`simple_server.shared.test_scripts`) only validate their own shape; the
rules that need **both** live here:

* :func:`suite_refs_error` - every member reference resolves (the endpoint's
  pre-mutation check);
* :func:`suite_owners_error` - a suite may not list a live script that
  another existing suite owns (the create/import half of the invariant; the
  move is the one endpoint allowed to change the owner);
* :func:`script_detach` - a script delete/import removes the reference first,
  so no dangling ``script_id`` is ever left behind;
* :func:`script_move` - an ownership change updates both stores in the one
  request, repairing a duplicate listing or a stale ``suite_id`` instead of
  answering 500 (v3.22.3 review);
* :func:`reconcile_scripts_to_suites` - the invariant repair: prune duplicate
  listings to the explicit owner, let the member list win over a stale
  ``suite_id``, and adopt the unreferenced scripts into "Imported scripts"
  (the v3.21.0 -> v3.22.0 migration is the same pass).

Provenance: a behavioural port of the legacy module functions in
``pysim_simple_server/server.py`` (``_test_suite_refs_error``,
``_test_script_detach``, ``_test_script_move``,
``reconcile_scripts_to_suites``, ``migrate_scripts_to_suites``), pysim-free:
the two stores are the arguments, so the helpers stay pure test doubles'
targets.  The diagnostics go through :mod:`logging` instead of a raw
``sys.stderr.write`` (the same choice :mod:`simple_server.testrun` made).

What the port does **not** do: none of the helpers writes to a store it was
not given, and none keeps module state - the legacy's globals were the stores
on the ``server`` object.
"""

import logging

from simple_server.shared import test_suites as test_suites_store
from simple_server.shared import test_scripts as test_scripts_store

log = logging.getLogger(__name__)

__all__ = [
    'IMPORTED_SUITE_NAME', 'MoveError', 'suite_refs_error',
    'suite_owners_error', 'script_detach', 'script_move',
    'reconcile_scripts_to_suites', 'migrate_scripts_to_suites',
]

#: The auto-created suite the reconciliation adopts unreferenced scripts into
#: (the legacy's "Imported scripts": the v3.21.0 store and a
#: ``mode: "replace"`` suite import land there).
IMPORTED_SUITE_NAME = 'Imported scripts'


class MoveError(ValueError):
    """A refused cross-store move/copy: the message is the answer.

    ``status`` is the HTTP status the legacy answered (400 for a bad target,
    404 for an unknown script, 500 for a store failure).  A ``ValueError`` on
    purpose: the dispatch already answers that class ``400``, so a caller that
    does not inspect ``status`` still refuses the request.
    """

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = int(status or 400)


def suite_refs_error(script_store, suite):
    """The cross-store rule: every member script must exist in the script
    store (a suite holds references; the scripts are the single source of
    truth).  Returns an error string, or ``''`` when the references resolve."""
    if not isinstance(suite, dict):
        return ''
    missing = []
    for entry in (suite.get('scripts') or []):
        if not isinstance(entry, dict):
            continue
        sid = str(entry.get('script_id') or '').strip().lower()
        if sid and script_store.get(sid) is None:
            missing.append(sid)
    if missing:
        return 'unknown test script(s): %s' % ', '.join(sorted(set(missing))[:5])
    return ''


def suite_owners_error(script_store, suite_store, suite, suite_id=None,
                       ignore=()):
    """The one-owner rule: a listed script must not already belong elsewhere.

    Every script belongs to exactly one suite and that suite lists it, so a
    suite may only list a script no other existing suite owns.  Returns an
    error string, or ``''`` when the references are free.

    ``suite_id`` is the suite being created/updated (``None`` for a new
    suite); a script whose ``suite_id`` names that suite is its own member.
    ``ignore`` holds the script ids the caller just created in the same
    request - a bundle import's fresh scripts carry the bundle's old
    ``suite_id`` values and its member list is authoritative (the
    reconciliation adopts them afterwards).  A script whose ``suite_id``
    names a suite that no longer exists is not a conflict either: the member
    list wins (the same repair rule as
    :func:`reconcile_scripts_to_suites`).
    """
    if not isinstance(suite, dict):
        return ''
    own = str(suite_id or '').strip().lower()
    ignored = {str(value).strip().lower() for value in (ignore or ())}
    owned = []
    seen = set()
    for entry in (suite.get('scripts') or []):
        if not isinstance(entry, dict):
            continue
        sid = str(entry.get('script_id') or '').strip().lower()
        if not sid or sid in seen or sid in ignored:
            continue
        seen.add(sid)
        script = script_store.get(sid)
        if script is None:
            continue        # suite_refs_error reports the dangling reference
        owner = str(script.get('suite_id') or '').strip().lower()
        if not owner or owner == own:
            continue
        if suite_store.get(owner) is None:
            continue        # a stale id: the member list wins (reconcile)
        owned.append(sid)
    if owned:
        return ('test script(s) already belong to another suite: %s'
                % ', '.join(sorted(owned)[:5]))
    return ''


def scripts_by_id(script_store):
    """``{id: script}`` of a store (the reconciliation's working copy)."""
    return {script['id']: script for script in script_store.list()}


def script_detach(suite_store, cur):
    """Remove a script from its owning suite's member list (before a delete).

    Best effort: the calling endpoint reports its own errors.  ``cur`` is the
    script entry (its ``suite_id`` names the suite).
    """
    suite_id = (cur or {}).get('suite_id')
    suite = suite_store.get(suite_id) if suite_id else None
    if suite is None:
        return
    entries = [entry for entry in suite['scripts']
               if entry['script_id'] != cur['id']]
    if entries == suite['scripts']:
        return
    try:
        suite_store.update(suite['id'], {'scripts': entries})
    except (test_suites_store.TestSuiteError, OSError) as exc:
        log.warning('TESTSUITES: cannot detach %s: %s', cur.get('id'), exc)


def script_move(script_store, suite_store, cur, target_id):
    """Move a script to another suite; returns ``(entry, error, status)``.

    The target appends it as a ``member`` keeping the ``on_fail`` policy it
    had, and **every** suite that lists it detaches.  The script's own
    ``suite_id`` names the owner when that suite lists it, otherwise the first
    lister does - so a duplicate listing or a stale ``suite_id`` (a hand edit,
    an interrupted move) is repaired by the move instead of leaving two
    owners.  The script is updated **first**: its validation refuses before any
    listing changes, so a refused move never leaves the script unlisted.
    """
    target = suite_store.get(target_id)
    if target is None:
        return None, 'unknown target suite', 400
    listings = [suite for suite in suite_store.list()
                if any(entry['script_id'] == cur['id']
                       for entry in suite['scripts'])]
    if (target['id'] == (cur.get('suite_id') or '')
            and [suite['id'] for suite in listings] == [target['id']]):
        # the exact no-op: the script already belongs to the target and only
        # the target lists it
        return cur, None, 200
    owner = next((suite for suite in listings
                  if suite['id'] == (cur.get('suite_id') or '')), None)
    if owner is None and listings:
        owner = listings[0]
    on_fail = 'stop'
    if owner is not None:
        entry = next((entry for entry in owner['scripts']
                      if entry['script_id'] == cur['id']), None)
        if entry is not None:
            on_fail = entry['on_fail']
    try:
        new = script_store.update(cur['id'], {'suite_id': target['id']})
    except (test_scripts_store.TestScriptError, OSError) as exc:
        return None, 'cannot move the script: %s' % exc, 500
    if new is None:
        return None, 'unknown test script id', 404
    for suite in listings:
        entries = [entry for entry in suite['scripts']
                   if entry['script_id'] != cur['id']]
        if entries == suite['scripts']:
            continue
        try:
            suite_store.update(suite['id'], {'scripts': entries})
        except (test_suites_store.TestSuiteError, OSError) as exc:
            log.warning('TESTSUITES: cannot detach %s from "%s": %s',
                        cur['id'], suite['name'], exc)
    try:
        fresh = suite_store.get(target['id']) or target
        entries = [entry for entry in fresh['scripts']
                   if entry['script_id'] != cur['id']]
        entries.append({'script_id': cur['id'], 'role': 'member',
                        'on_fail': on_fail})
        suite_store.update(target['id'], {'scripts': entries})
    except (test_suites_store.TestSuiteError, OSError) as exc:
        return None, 'cannot attach to the target suite: %s' % exc, 500
    return new, None, 200


def reconcile_scripts_to_suites(script_store, suite_store):
    """Make the ownership invariant hold again; returns the changed count.

    - a script **listed by several suites** (a hand edit or an interrupted
      move) stays with its explicit owner - the suite its ``suite_id`` names
      when that suite lists it, else the first lister - and the other
      listings are pruned;
    - a script a suite lists but whose ``suite_id`` is empty or points
      elsewhere adopts the listing suite (the member list is authoritative);
    - a script no suite lists (a v3.21.0 store, a suites import with
      ``mode: "replace"``, a hand edit) joins the auto-created
      :data:`IMPORTED_SUITE_NAME` suite.  Its ``suite_id`` write may be
      refused by the store's validator (a stored script that no longer
      validates is kept, not dropped) - the member list is authoritative, so
      it is listed anyway and stays visible in the editor.

    Idempotent.  The entry point calls it once at startup (the v3.21.0
    migration, :func:`migrate_scripts_to_suites`) and a suites import runs it
    so ``mode: "replace"`` adopts the wiped suites' scripts.
    """
    if script_store is None or suite_store is None:
        return 0
    suites = suite_store.list()
    listings = {}
    for suite in suites:
        for entry in suite['scripts']:
            listings.setdefault(entry['script_id'], []).append(suite['id'])
    by_id = scripts_by_id(script_store)
    pruned = 0
    for script_id, owners in listings.items():
        if len(owners) < 2:
            continue
        explicit = (by_id.get(script_id) or {}).get('suite_id') or None
        keep = explicit if explicit in owners else owners[0]
        for suite in suites:
            if suite['id'] == keep or suite['id'] not in owners:
                continue
            entries = [entry for entry in suite['scripts']
                       if entry['script_id'] != script_id]
            try:
                suite_store.update(suite['id'], {'scripts': entries})
                suite['scripts'] = entries
                pruned += 1
                log.warning('TESTSUITES: pruned %s from "%s" (owner "%s")',
                            script_id, suite['name'], keep)
            except (test_suites_store.TestSuiteError, OSError) as exc:
                log.warning('TESTSUITES: cannot prune %s from "%s": %s',
                            script_id, suite['name'], exc)
    # The owner map after the pruning (the member list is authoritative).
    owner = {}
    for suite in suites:
        for entry in suite['scripts']:
            owner.setdefault(entry['script_id'], suite['id'])
    orphans = []
    adopted = 0
    for script in script_store.list():
        suite_id = script.get('suite_id') or None
        if suite_id and owner.get(script['id']) == suite_id:
            continue
        listed_by = owner.get(script['id'])
        if listed_by:
            try:
                script_store.update(script['id'], {'suite_id': listed_by})
                adopted += 1
            except (test_scripts_store.TestScriptError, OSError) as exc:
                log.warning('TESTSUITES: cannot adopt %s: %s', script['id'], exc)
            continue
        orphans.append(script)
    if not orphans and not adopted and not pruned:
        return 0
    attached = 0
    if orphans:
        suite = suite_store.find_by_name(IMPORTED_SUITE_NAME)
        if suite is None:
            suite = suite_store.add({'name': IMPORTED_SUITE_NAME})
        entries = list(suite['scripts'])
        known = {entry['script_id'] for entry in entries}
        for script in orphans:
            try:
                script_store.update(script['id'], {'suite_id': suite['id']})
            except (test_scripts_store.TestScriptError, OSError) as exc:
                # The member list is authoritative: list the script anyway.
                log.warning('TESTSUITES: cannot set the suite id of %s (listed '
                            'in "%s" so the editor can fix it): %s',
                            script['id'], suite['name'], exc)
            if script['id'] not in known:
                entries.append({'script_id': script['id'], 'role': 'member',
                                'on_fail': 'stop'})
                known.add(script['id'])
            attached += 1
        if attached:
            try:
                suite_store.update(suite['id'], {'scripts': entries})
            except (test_suites_store.TestSuiteError, OSError) as exc:
                log.warning('TESTSUITES: cannot fill "%s": %s', suite['name'], exc)
    if attached or adopted or pruned:
        log.info('TESTSUITES: reconciled %d script(s) (attached to "%s": %d, '
                 'adopted by their suite: %d, pruned duplicate listings: %d)',
                 attached + adopted + pruned, IMPORTED_SUITE_NAME, attached,
                 adopted, pruned)
    return attached + adopted + pruned


def migrate_scripts_to_suites(script_store, suite_store):
    """The v3.21.0 -> v3.22.0 migration: attach the pre-suite scripts to
    "Imported scripts" (the general reconciliation, called at startup)."""
    return reconcile_scripts_to_suites(script_store, suite_store)
