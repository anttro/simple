# coding=utf-8
"""The test suites (Phase 3c-3d-2): the stores, the cross-store rules and the
suite execution.

No card and no pysim: every exchange is a scripted fake transport (the same
fixtures ``tests/test_simple_runner.py`` uses), and the endpoints run on a real
:class:`ThreadingHTTPServer` bound to an ephemeral loopback port.  The suite
runner is driven directly (``start_suite(..., spawn=False)`` +
``execute_suite``) where a step's result is asserted, and over HTTP where the
endpoint's rules (the busy guard, the preset/ADM refusals, the start reset)
matter.

The reference is the legacy server's suite implementation
(``pysim_simple_server/server.py``: ``_test_suite_execute_once``,
``_test_suite_state``, ``_test_suite_start``, ``_test_script_move``,
``reconcile_scripts_to_suites``, ...): the semantics are the v3.22.0 ones - the
ordered setup/members/teardown, the per-member ``on_fail``, one card session
without resets, the teardown always on a stop/failure, the shared SCP80
counter state and the report the PWA renders.  The drift assertions against
the legacy live in ``tests/test_simple_drift.py`` (the module allowed to import
both implementations).
"""

import time
import unittest
from unittest import mock

from simple_server.shared import test_scripts as test_scripts_store
from simple_server.shared import test_suites as test_suites_store

from simple_server import suites as suites_module
from simple_server import testrun

try:                                # `python -m unittest tests.<module>`
    from tests.test_simple_runner import (Card, Harness, STATUS_9K,
                                          create_suite, make_state, normalise,
                                          por_packet, preset_fields,
                                          status_step, temp_dir)
except ImportError:                 # `unittest discover -s tests`
    from test_simple_runner import (Card, Harness, STATUS_9K, create_suite,
                                    make_state, normalise, por_packet,
                                    preset_fields, status_step, temp_dir)

OK_STATUS = dict(STATUS_9K)


def failing_step(level='error'):
    """A STATUS step that expects 9000 (the card answers 6A82)."""
    return status_step('9000', on_fail=level)


def script_id(index):
    return '%032x' % (index + 1)


def ordered(members):
    """The store's member order: the setup first, the teardown last."""
    setups = [entry for entry in members if entry[0]['role'] == 'setup']
    middle = [entry for entry in members if entry[0]['role'] == 'member']
    teardowns = [entry for entry in members if entry[0]['role'] == 'teardown']
    return setups + middle + teardowns


def run_suite(testcase, scripts, preset=None, preset_id=None, setup=None,
              suite_fields=None, start_reset=False, iterations=1):
    """Run a suite synchronously; returns ``(state, runner, snapshot)``.

    ``scripts`` is a list of step lists (all ``member``) or
    ``{name, role, on_fail, steps}`` dicts; the members are put in the store's
    order (setup first, teardown last - the suite store normalises the list).
    The card is equipped first (the startup drain answers its own STATUS), then
    ``setup(card)`` arms the run.
    """
    card = Card()
    state = make_state(testcase, card)
    if setup is not None:
        setup(card)
    members = []
    for index, spec in enumerate(scripts):
        if isinstance(spec, dict):
            steps = spec.get('steps') or [dict(STATUS_9K)]
            entry = {'script_id': script_id(index),
                     'role': spec.get('role') or 'member',
                     'on_fail': spec.get('on_fail') or 'stop'}
            name = spec.get('name') or ('member %d' % (index + 1))
        else:
            steps = spec
            entry = {'script_id': script_id(index), 'role': 'member',
                     'on_fail': 'stop'}
            name = 'member %d' % (index + 1)
        members.append((entry, normalise(steps, name=name)))
    members = ordered(members)
    fields = {'id': 'a' * 32, 'name': 'bench suite', 'require_adm': False,
              'start_reset': start_reset}
    fields.update(suite_fields or {})
    runner = state.test_runner
    runner.start_suite(fields, members, preset or {}, preset_id,
                       {'required': False}, None, iterations, spawn=False)
    if iterations > 1:
        runner._load_suite_worker(fields, members, preset or {}, preset_id,
                                  iterations)
    else:
        runner.execute_suite(fields, members, preset or {}, preset_id)
    return state, runner, runner.snapshot()


def store_pair(testcase, scripts=(), suites=()):
    """A (script store, suite store) pair in a throw-away directory.

    ``scripts`` are step lists, ``suites`` suite dicts; the helper returns the
    two stores plus ``{'scripts': {index: entry}, 'suites': {name: entry}}``.
    """
    root = temp_dir(testcase, 'simple-suites-')
    script_store = test_scripts_store.TestScriptStore(
        path=root / 'test_scripts.json', validator=testrun.validate_script)
    suite_store = test_suites_store.TestSuiteStore(
        path=root / 'test_suites.json')
    added = {'scripts': {}, 'suites': {}}
    for index, steps in enumerate(scripts):
        added['scripts'][index] = script_store.add(
            {'name': 'script %d' % (index + 1), 'steps': steps})
    for suite in suites:
        added['suites'][suite.get('name')] = suite_store.add(suite)
    return script_store, suite_store, added


# ---- the suite store over HTTP ----------------------------------------------

class SuiteStoreTests(unittest.TestCase):
    def setUp(self):
        self.state = make_state(self)
        self.harness = Harness(self.state)
        self.addCleanup(self.harness.close)

    def test_the_store_is_served_card_free(self):
        status, body = self.harness.json('GET', '/api/test/suites')
        self.assertEqual(status, 200)
        self.assertEqual(body['count'], 0)
        self.assertEqual(body['suites'], [])
        self.assertEqual(body['version'], 2)
        self.assertTrue(body['path'].endswith('test_suites.json'))

    def test_create_update_delete(self):
        status, body = self.harness.json(
            'POST', '/api/test/suites',
            {'name': 'alfa regression', 'require_adm': True})
        self.assertEqual(status, 200)
        self.assertTrue(body['ok'])
        suite = body['suite']
        self.assertEqual(suite['name'], 'alfa regression')
        self.assertTrue(suite['require_adm'])
        self.assertFalse(suite['start_reset'])
        self.assertEqual(suite['scripts'], [])
        sid = suite['id']
        status, body = self.harness.json('GET', '/api/test/suites')
        self.assertEqual([entry['id'] for entry in body['suites']], [sid])

        status, body = self.harness.json(
            'POST', '/api/test/suites/update',
            {'id': sid, 'suite': {'name': 'renamed', 'start_reset': True}})
        self.assertEqual(status, 200)
        self.assertEqual(body['suite']['name'], 'renamed')
        self.assertTrue(body['suite']['start_reset'])
        # the untouched fields keep their values
        self.assertTrue(body['suite']['require_adm'])

        status, body = self.harness.json(
            'POST', '/api/test/suites/update',
            {'id': 'nope', 'suite': {'name': 'x'}})
        self.assertEqual(status, 404)

        status, body = self.harness.json('POST', '/api/test/suites/delete',
                                         {'id': sid})
        self.assertEqual(status, 200)
        self.assertTrue(body['removed'])
        self.assertEqual(self.state.test_suites.list(), [])

    def test_the_create_takes_the_wrapped_form(self):
        status, body = self.harness.json('POST', '/api/test/suites',
                                         {'suite': {'name': 'wrapped'}})
        self.assertEqual(status, 200)
        self.assertEqual(body['suite']['name'], 'wrapped')

    def test_an_invalid_suite_is_a_400(self):
        for payload in ({'name': 1},
                        {'name': 'x', 'scripts': 'nope'},
                        {'name': 'x', 'scripts': [{'script_id': 'nope'}]},
                        {'name': 'x', 'scripts': [{'script_id': 'a' * 32,
                                                   'role': 'nope'}]}):
            status, body = self.harness.json('POST', '/api/test/suites',
                                             payload)
            self.assertEqual(status, 400, payload)
            self.assertTrue(body['error'])
        self.assertEqual(self.state.test_suites.list(), [])

    def test_a_member_reference_must_resolve(self):
        status, body = self.harness.json(
            'POST', '/api/test/suites',
            {'name': 'dangling',
             'scripts': [{'script_id': 'a' * 32, 'role': 'member'}]})
        self.assertEqual(status, 400)
        self.assertIn('unknown test script', body['error'])
        self.assertEqual(self.state.test_suites.list(), [])

    def test_the_roles_are_normalised(self):
        # the store puts the setup first and the teardown last
        suite = create_suite(self.harness)
        first = self.harness.json('POST', '/api/test/scripts',
                                  dict({'name': 'first', 'steps': [OK_STATUS]},
                                       suite_id=suite['id']))[1]['script']
        second = self.harness.json('POST', '/api/test/scripts',
                                   dict({'name': 'second', 'steps': [OK_STATUS]},
                                        suite_id=suite['id']))[1]['script']
        status, body = self.harness.json(
            'POST', '/api/test/suites/update',
            {'id': suite['id'],
             'suite': {'scripts': [
                 {'script_id': first['id'], 'role': 'teardown'},
                 {'script_id': second['id'], 'role': 'setup'}]}})
        self.assertEqual(status, 200, body)
        self.assertEqual([(entry['script_id'], entry['role'])
                          for entry in body['suite']['scripts']],
                         [(second['id'], 'setup'), (first['id'], 'teardown')])

    def test_the_member_set_changes_through_the_scripts(self):
        suite = create_suite(self.harness)
        script = self.harness.json(
            'POST', '/api/test/scripts',
            dict({'name': 'one', 'steps': [OK_STATUS]},
                 suite_id=suite['id']))[1]['script']
        other_suite = create_suite(self.harness, 'other')
        other = self.harness.json(
            'POST', '/api/test/scripts',
            dict({'name': 'two', 'steps': [OK_STATUS]},
                 suite_id=other_suite['id']))[1]['script']
        # adding a live script by editing the suite is refused
        status, body = self.harness.json(
            'POST', '/api/test/suites/update',
            {'id': suite['id'],
             'suite': {'scripts': [{'script_id': script['id'], 'role': 'member'},
                                   {'script_id': other['id'],
                                    'role': 'member'}]}})
        self.assertEqual(status, 400)
        self.assertIn('script endpoints', body['error'])
        # ... and so is dropping the live one
        status, body = self.harness.json(
            'POST', '/api/test/suites/update',
            {'id': suite['id'], 'suite': {'scripts': []}})
        self.assertEqual(status, 400)
        self.assertEqual(len(self.state.test_suites.get(suite['id'])['scripts']),
                         1)

    def test_a_dangling_reference_can_be_pruned(self):
        # the store keeps a suite servable when its script vanished (a hand
        # edit): the editor prunes the row, and the update must allow exactly
        # that repair
        suite = create_suite(self.harness)
        script = self.harness.json(
            'POST', '/api/test/scripts',
            dict({'name': 'one', 'steps': [OK_STATUS]},
                 suite_id=suite['id']))[1]['script']
        # simulate a script that is gone (no endpoint can reach it any more)
        self.state.test_scripts._scripts = [
            entry for entry in self.state.test_scripts._scripts
            if entry['id'] != script['id']]
        status, body = self.harness.json(
            'POST', '/api/test/suites/update',
            {'id': suite['id'], 'suite': {'scripts': []}})
        self.assertEqual(status, 200, body)
        self.assertEqual(body['suite']['scripts'], [])

    def test_a_non_empty_suite_cannot_be_deleted(self):
        suite = create_suite(self.harness)
        self.harness.json('POST', '/api/test/scripts',
                          dict({'name': 'one', 'steps': [OK_STATUS]},
                               suite_id=suite['id']))
        status, body = self.harness.json('POST', '/api/test/suites/delete',
                                         {'id': suite['id']})
        self.assertEqual(status, 400)
        self.assertIn('still holds', body['error'])
        self.assertIsNotNone(self.state.test_suites.get(suite['id']))

    def test_the_import_remaps_and_skips(self):
        script = {'id': 'a' * 32, 'name': 'imported',
                  'steps': [dict(STATUS_9K)]}
        bundle = {
            'scripts': [script],
            'suites': [
                {'id': 'b' * 32, 'name': 'good',
                 'scripts': [{'script_id': 'a' * 32, 'role': 'member'}]},
                {'id': 'c' * 32, 'name': 'bad',
                 'scripts': [{'script_id': 'd' * 32, 'role': 'member'}]},
            ],
        }
        status, body = self.harness.json('POST', '/api/test/suites/import',
                                         bundle)
        self.assertEqual(status, 200, body)
        self.assertTrue(body['ok'])
        self.assertEqual(body['added'], 1)
        self.assertEqual(body['skipped'], 1)
        self.assertIn('unknown script', body['errors'][0])
        suites = self.state.test_suites.list()
        self.assertEqual([suite['name'] for suite in suites], ['good'])
        # the reference was rewritten to the stored script's (new) id
        stored = self.state.test_scripts.list()[0]
        self.assertEqual(suites[0]['scripts'][0]['script_id'], stored['id'])
        self.assertEqual(stored['suite_id'], suites[0]['id'])

    def test_the_import_validates_the_list(self):
        status, body = self.harness.json('POST', '/api/test/suites/import',
                                         {'suites': 'nope'})
        self.assertEqual(status, 400)
        self.assertIn('list', body['error'])

    def test_a_create_cannot_list_a_script_of_another_suite(self):
        # every script belongs to exactly one suite: a new suite listing a
        # live script another suite owns is refused (the member set changes
        # through move/copy/delete)
        owner = create_suite(self.harness, 'owner')
        script = self.harness.json(
            'POST', '/api/test/scripts',
            dict({'name': 'one', 'steps': [OK_STATUS]},
                 suite_id=owner['id']))[1]['script']
        status, body = self.harness.json(
            'POST', '/api/test/suites',
            {'name': 'second',
             'scripts': [{'script_id': script['id'], 'role': 'member'}]})
        self.assertEqual(status, 400, body)
        self.assertIn('another suite', body['error'])
        # nothing was created and the owner's listing is untouched
        self.assertEqual([suite['name']
                          for suite in self.state.test_suites.list()], ['owner'])
        self.assertEqual([entry['script_id']
                          for entry in self.state.test_suites.get(
                              owner['id'])['scripts']], [script['id']])

    def test_the_import_cannot_list_a_script_of_another_suite(self):
        owner = create_suite(self.harness, 'owner')
        script = self.harness.json(
            'POST', '/api/test/scripts',
            dict({'name': 'one', 'steps': [OK_STATUS]},
                 suite_id=owner['id']))[1]['script']
        status, body = self.harness.json(
            'POST', '/api/test/suites/import',
            {'suites': [{'name': 'imported',
                         'scripts': [{'script_id': script['id'],
                                      'role': 'member'}]}]})
        self.assertEqual(status, 400, body)
        self.assertIn('another suite', body['error'])
        self.assertEqual([suite['name']
                          for suite in self.state.test_suites.list()], ['owner'])

    def test_the_suites_import_refuses_a_bad_mode(self):
        status, body = self.harness.json('POST', '/api/test/suites/import',
                                         {'suites': [], 'mode': 'Replace'})
        self.assertEqual(status, 400)
        self.assertIn('mode', body['error'])

    def test_a_replace_with_nothing_importable_does_not_wipe(self):
        # a replace whose suites all fail the reference check must refuse
        # instead of wiping the store (the legacy silently replaced with [])
        suite = create_suite(self.harness, 'kept')
        status, body = self.harness.json(
            'POST', '/api/test/suites/import',
            {'mode': 'replace',
             'suites': [{'name': 'broken',
                         'scripts': [{'script_id': 'd' * 32,
                                      'role': 'member'}]}]})
        self.assertEqual(status, 400, body)
        self.assertIn('replace', body['error'])
        self.assertEqual([entry['name']
                          for entry in self.state.test_suites.list()], ['kept'])
        self.assertIsNotNone(self.state.test_suites.get(suite['id']))

    def test_the_scripts_import_refuses_replace_while_a_suite_lists_scripts(self):
        suite = create_suite(self.harness)
        self.harness.json('POST', '/api/test/scripts',
                          dict({'name': 'one', 'steps': [OK_STATUS]},
                               suite_id=suite['id']))
        status, body = self.harness.json(
            'POST', '/api/test/scripts/import',
            {'scripts': [{'name': 'x', 'steps': [OK_STATUS]}],
             'mode': 'replace'})
        self.assertEqual(status, 400)
        self.assertIn('dangling', body['error'])
        self.assertEqual(len(self.state.test_scripts.list()), 1)


# ---- the cross-store rules over HTTP ----------------------------------------

class CrossStoreTests(unittest.TestCase):
    def setUp(self):
        self.state = make_state(self)
        self.harness = Harness(self.state)
        self.addCleanup(self.harness.close)
        self.suite_a = create_suite(self.harness, 'A')
        self.suite_b = create_suite(self.harness, 'B')
        self.script = self.harness.json(
            'POST', '/api/test/scripts',
            dict({'name': 'mover', 'steps': [OK_STATUS]},
                 suite_id=self.suite_a['id']))[1]['script']

    def member_ids(self, suite_id):
        suite = self.state.test_suites.get(suite_id)
        return [entry['script_id'] for entry in suite['scripts']]

    def test_move(self):
        # give the entry a non-default policy, which the move keeps
        self.harness.json(
            'POST', '/api/test/suites/update',
            {'id': self.suite_a['id'],
             'suite': {'scripts': [{'script_id': self.script['id'],
                                    'role': 'member',
                                    'on_fail': 'continue'}]}})
        status, body = self.harness.json(
            'POST', '/api/test/scripts/move',
            {'id': self.script['id'], 'suite_id': self.suite_b['id']})
        self.assertEqual(status, 200, body)
        self.assertTrue(body['ok'])
        self.assertEqual(body['script']['suite_id'], self.suite_b['id'])
        self.assertEqual(self.member_ids(self.suite_a['id']), [])
        self.assertEqual(self.member_ids(self.suite_b['id']),
                         [self.script['id']])
        moved = self.state.test_suites.get(self.suite_b['id'])['scripts'][0]
        self.assertEqual(moved['on_fail'], 'continue')
        self.assertEqual(moved['role'], 'member')

    def test_a_move_is_a_noop_when_it_is_already_there(self):
        status, body = self.harness.json(
            'POST', '/api/test/scripts/move',
            {'id': self.script['id'], 'suite_id': self.suite_a['id']})
        self.assertEqual(status, 200, body)
        self.assertEqual(self.member_ids(self.suite_a['id']),
                         [self.script['id']])

    def test_the_update_moves_on_a_changed_suite_id(self):
        status, body = self.harness.json(
            'POST', '/api/test/scripts/update',
            {'id': self.script['id'],
             'script': {'name': 'mover', 'steps': [OK_STATUS],
                        'suite_id': self.suite_b['id']}})
        self.assertEqual(status, 200, body)
        self.assertTrue(body['moved'])
        self.assertEqual(self.member_ids(self.suite_b['id']),
                         [self.script['id']])

    def test_move_refusals(self):
        status, body = self.harness.json(
            'POST', '/api/test/scripts/move',
            {'id': 'nope', 'suite_id': self.suite_b['id']})
        self.assertEqual(status, 404)
        status, body = self.harness.json(
            'POST', '/api/test/scripts/move',
            {'id': self.script['id'], 'suite_id': 'c' * 32})
        self.assertEqual(status, 400)
        self.assertIn('unknown target suite', body['error'])
        # the refused move changed nothing
        self.assertEqual(self.member_ids(self.suite_a['id']),
                         [self.script['id']])

    def test_a_move_repairs_a_duplicate_listing(self):
        # both suites list the script (a hand edit / an interrupted move): the
        # move detaches from every lister and attaches to the target once
        entries = list(self.suite_b['scripts']) + [
            {'script_id': self.script['id'], 'role': 'member',
             'on_fail': 'stop'}]
        self.state.test_suites.update(self.suite_b['id'], {'scripts': entries})
        status, body = self.harness.json(
            'POST', '/api/test/scripts/move',
            {'id': self.script['id'], 'suite_id': self.suite_b['id']})
        self.assertEqual(status, 200, body)
        self.assertEqual(self.member_ids(self.suite_a['id']), [])
        self.assertEqual(self.member_ids(self.suite_b['id']),
                         [self.script['id']])

    def test_copy(self):
        status, body = self.harness.json(
            'POST', '/api/test/scripts/copy',
            {'id': self.script['id'], 'suite_id': self.suite_b['id']})
        self.assertEqual(status, 200, body)
        copy = body['script']
        self.assertNotEqual(copy['id'], self.script['id'])
        self.assertEqual(copy['name'], 'mover (copy)')
        self.assertEqual(copy['suite_id'], self.suite_b['id'])
        self.assertEqual(copy['steps'], self.script['steps'])
        self.assertEqual(self.member_ids(self.suite_b['id']), [copy['id']])
        # the source keeps its own membership
        self.assertEqual(self.member_ids(self.suite_a['id']),
                         [self.script['id']])
        status, body = self.harness.json(
            'POST', '/api/test/scripts/copy',
            {'id': 'nope', 'suite_id': self.suite_b['id']})
        self.assertEqual(status, 404)
        status, body = self.harness.json(
            'POST', '/api/test/scripts/copy',
            {'id': self.script['id'], 'suite_id': 'c' * 32})
        self.assertEqual(status, 400)

    def test_delete_detaches(self):
        status, body = self.harness.json(
            'POST', '/api/test/scripts/delete', {'id': self.script['id']})
        self.assertEqual(status, 200)
        self.assertTrue(body['removed'])
        self.assertEqual(self.member_ids(self.suite_a['id']), [])


# ---- the reconciliation -----------------------------------------------------

class ReconcileTests(unittest.TestCase):
    def test_an_orphan_joins_imported_scripts(self):
        script_store, suite_store, added = store_pair(
            self, scripts=[[dict(STATUS_9K)]])
        self.assertEqual(suites_module.reconcile_scripts_to_suites(
            script_store, suite_store), 1)
        suite = suite_store.find_by_name(suites_module.IMPORTED_SUITE_NAME)
        self.assertIsNotNone(suite)
        self.assertEqual([entry['script_id'] for entry in suite['scripts']],
                         [added['scripts'][0]['id']])
        self.assertEqual(script_store.get(added['scripts'][0]['id'])['suite_id'],
                         suite['id'])

    def test_a_listed_script_adopts_its_suite(self):
        script_store, suite_store, added = store_pair(
            self, scripts=[[dict(STATUS_9K)]],
            suites=[{'name': 'owner'}])
        suite = suite_store.find_by_name('owner')
        suite_store.update(suite['id'],
                           {'scripts': [{'script_id': added['scripts'][0]['id'],
                                         'role': 'member'}]})
        self.assertEqual(suites_module.reconcile_scripts_to_suites(
            script_store, suite_store), 1)
        self.assertEqual(script_store.get(added['scripts'][0]['id'])['suite_id'],
                         suite['id'])
        # "Imported scripts" was not created: nothing was an orphan
        self.assertIsNone(suite_store.find_by_name(
            suites_module.IMPORTED_SUITE_NAME))

    def test_duplicate_listings_are_pruned_to_the_explicit_owner(self):
        script_store, suite_store, added = store_pair(
            self, scripts=[[dict(STATUS_9K)]],
            suites=[{'name': 'first'}, {'name': 'second'}])
        first = suite_store.find_by_name('first')
        second = suite_store.find_by_name('second')
        entry = {'script_id': added['scripts'][0]['id'], 'role': 'member'}
        suite_store.update(first['id'], {'scripts': [entry]})
        suite_store.update(second['id'], {'scripts': [entry]})
        script_store.update(added['scripts'][0]['id'], {'suite_id': second['id']})
        self.assertEqual(suites_module.reconcile_scripts_to_suites(
            script_store, suite_store), 1)
        self.assertEqual(suite_store.get(first['id'])['scripts'], [])
        self.assertEqual([entry['script_id']
                          for entry in suite_store.get(second['id'])['scripts']],
                         [added['scripts'][0]['id']])

    def test_create_an_idempotent_repair(self):
        script_store, suite_store, _added = store_pair(
            self, scripts=[[dict(STATUS_9K)], [dict(STATUS_9K)]])
        self.assertEqual(suites_module.reconcile_scripts_to_suites(
            script_store, suite_store), 2)
        self.assertEqual(suites_module.reconcile_scripts_to_suites(
            script_store, suite_store), 0)

    def test_the_migration_is_the_reconcile(self):
        script_store, suite_store, _added = store_pair(
            self, scripts=[[dict(STATUS_9K)]])
        self.assertEqual(suites_module.migrate_scripts_to_suites(
            script_store, suite_store), 1)
        self.assertIsNotNone(suite_store.find_by_name(
            suites_module.IMPORTED_SUITE_NAME))
        self.assertEqual(suites_module.migrate_scripts_to_suites(
            script_store, suite_store), 0)

    def test_a_validator_refused_orphan_is_still_listed(self):
        # the store keeps a script that no longer validates (it must not be
        # destroyed), but its suite_id write is refused by the same validator:
        # the member list is authoritative, so the script is listed anyway and
        # stays visible (and fixable) in the editor
        script_store, suite_store, added = store_pair(self)
        script_store._scripts = [{
            'id': 'a' * 32, 'name': 'stale', 'suite_id': None,
            'require_adm': False,
            'steps': [{'type': 'action', 'kind': 'status',
                       'params': {'attempts': 'nope'}}]}]
        self.assertEqual(suites_module.reconcile_scripts_to_suites(
            script_store, suite_store), 1)
        suite = suite_store.find_by_name(suites_module.IMPORTED_SUITE_NAME)
        self.assertEqual([entry['script_id'] for entry in suite['scripts']],
                         ['a' * 32])
        self.assertIsNone(script_store.get('a' * 32)['suite_id'])

    def test_the_state_delegates(self):
        state = make_state(self)
        script = state.test_scripts.add({'name': 'orphan',
                                         'steps': [dict(STATUS_9K)]})
        self.assertEqual(state.migrate_scripts_to_suites(), 1)
        suite = state.test_suites.find_by_name(
            suites_module.IMPORTED_SUITE_NAME)
        self.assertEqual(suite['scripts'][0]['script_id'], script['id'])
        self.assertEqual(state.reconcile_scripts_to_suites(), 0)


# ---- the suite execution ----------------------------------------------------

SUITE_KEYS = {'id', 'name', 'require_adm', 'start_reset', 'reset', 'started',
              'session_start', 'session_end', 'session_changed', 'preset_id',
              'preset', 'counters_before', 'counters_after', 'summary',
              'members'}
MEMBER_KEYS = {'script_id', 'name', 'role', 'on_fail', 'status', 'steps',
               'log', 'log_truncated', 'note', 'started', 'finished',
               'counters_after'}
SUMMARY_KEYS = {'ok', 'warning', 'error', 'skipped', 'stopped',
                'session_changed', 'wall_ms', 'counters_before',
                'counters_after', 'setup', 'teardown'}


class SuiteRunTests(unittest.TestCase):
    def test_the_order_and_the_report(self):
        _state, runner, run = run_suite(
            self, [
                {'role': 'teardown', 'name': 'cleanup',
                 'steps': [dict(STATUS_9K)]},
                {'role': 'setup', 'name': 'prepare',
                 'steps': [dict(STATUS_9K)]},
                {'role': 'member', 'name': 'one', 'steps': [dict(STATUS_9K)]},
                {'role': 'member', 'name': 'two', 'steps': [dict(STATUS_9K)]},
            ],
            setup=lambda card: card.status('9000'))
        suite = run['suite']
        self.assertFalse(run['running'])
        self.assertEqual(run['status'], 'ok')
        self.assertEqual(run['kind'], 'suite')
        self.assertEqual(run['name'], 'bench suite')
        self.assertEqual(set(suite), SUITE_KEYS)
        self.assertEqual(set(suite['summary']), SUMMARY_KEYS)
        for member in suite['members']:
            self.assertEqual(set(member), MEMBER_KEYS)
        # the roles run in the stored order (setup first, teardown last)
        self.assertEqual([member['role'] for member in suite['members']],
                         ['setup', 'member', 'member', 'teardown'])
        self.assertEqual([member['status'] for member in suite['members']],
                         ['ok', 'ok', 'ok', 'ok'])
        started = [member['started'] for member in suite['members']]
        self.assertEqual(started, sorted(started))
        # the member log carries its own prefix
        self.assertTrue(any(line.startswith(
            'TEST-RUN [1/4 setup prepare] step 1: STATUS 1/1 -> 9000')
            for line in suite['members'][0]['log']),
            suite['members'][0]['log'])
        self.assertEqual(suite['members'][0]['steps'][0]['sw'], '9000')
        self.assertEqual(suite['summary']['ok'], 4)
        self.assertEqual(suite['summary']['setup'], 'ok')
        self.assertEqual(suite['summary']['teardown'], 'ok')
        self.assertFalse(suite['summary']['stopped'])
        self.assertEqual(suite['session_start'], suite['session_end'])
        self.assertFalse(suite['session_changed'])
        self.assertEqual(suite['members'][0]['name'], 'prepare')

    def test_a_member_log_survives_a_full_shared_buffer(self):
        # the shared run log is trimmed at TEST_LOG_MAX; a member starting
        # with a full buffer used to slice its own excerpt empty (log_from
        # above the post-trim length) - the member keeps a private buffer
        card = Card()
        state = make_state(self, card)
        card.status('9000')
        members = [
            ({'script_id': script_id(0), 'role': 'member', 'on_fail': 'stop'},
             normalise([dict(STATUS_9K)], name='one')),
        ]
        suite = {'id': 'a' * 32, 'name': 'suite', 'require_adm': False,
                 'start_reset': False}
        runner = state.test_runner
        runner.start_suite(suite, members, {}, None, {'required': False},
                           None, spawn=False)
        runner._run['log'] = ['TEST-RUN old %d' % index
                              for index in range(testrun.TEST_LOG_MAX)]
        runner.execute_suite(suite, members, {})
        member = runner.snapshot()['suite']['members'][0]
        self.assertEqual(len(member['log']), 1, member['log'])
        self.assertIn('STATUS 1/1 -> 9000', member['log'][0])
        self.assertFalse(member['log_truncated'])

    def test_a_member_on_fail_continue(self):
        _state, _runner, run = run_suite(
            self, [
                {'name': 'failing', 'on_fail': 'continue',
                 'steps': [failing_step()]},
                {'name': 'next', 'steps': [dict(STATUS_9K)]},
            ],
            setup=lambda card: card.status('6A82').status('9000'))
        suite = run['suite']
        self.assertEqual([member['status'] for member in suite['members']],
                         ['error', 'ok'])
        self.assertEqual(run['status'], 'error')
        self.assertEqual(suite['summary']['error'], 1)
        self.assertEqual(suite['summary']['ok'], 1)
        self.assertFalse(suite['summary']['stopped'])

    def test_a_member_on_fail_stop_skips_the_rest(self):
        _state, _runner, run = run_suite(
            self, [
                {'name': 'failing', 'steps': [failing_step()]},
                {'name': 'skipped', 'steps': [dict(STATUS_9K)]},
            ],
            setup=lambda card: card.status('6A82'))
        suite = run['suite']
        self.assertEqual([member['status'] for member in suite['members']],
                         ['error', 'skipped'])
        self.assertIn('previous script failed',
                      suite['members'][1]['note'])
        self.assertEqual(run['status'], 'error')

    def test_a_setup_failure_skips_the_members(self):
        _state, _runner, run = run_suite(
            self, [
                {'role': 'setup', 'name': 'prepare',
                 'steps': [failing_step()]},
                {'role': 'member', 'name': 'one', 'steps': [dict(STATUS_9K)]},
                {'role': 'teardown', 'name': 'cleanup',
                 'steps': [dict(STATUS_9K)]},
            ],
            setup=lambda card: (card.status('6A82').status('9000')))
        suite = run['suite']
        self.assertEqual([member['status'] for member in suite['members']],
                         ['error', 'skipped', 'ok'])
        self.assertIn('the setup script failed', suite['members'][1]['note'])
        # the teardown still runs
        self.assertEqual(suite['summary']['teardown'], 'ok')

    def test_the_teardown_runs_after_failure_and_stop(self):
        _state, _runner, run = run_suite(
            self, [
                {'name': 'failing', 'steps': [failing_step()]},
                {'role': 'teardown', 'name': 'cleanup',
                 'steps': [dict(STATUS_9K)]},
            ],
            setup=lambda card: card.status('6A82').status('9000'))
        suite = run['suite']
        self.assertEqual([member['status'] for member in suite['members']],
                         ['error', 'ok'])

    def test_the_teardown_is_skipped_without_a_card(self):
        card = Card()
        state = make_state(self, card)
        card.status('9000')
        members = [
            ({'script_id': script_id(0), 'role': 'member', 'on_fail': 'stop'},
             normalise([dict(STATUS_9K)], name='one')),
            ({'script_id': script_id(1), 'role': 'teardown',
              'on_fail': 'stop'},
             normalise([dict(STATUS_9K)], name='cleanup')),
        ]
        state.card_present = False
        suite = {'id': 'a' * 32, 'name': 'suite', 'require_adm': False,
                 'start_reset': False}
        runner = state.test_runner
        runner.start_suite(suite, members, {}, None, {'required': False},
                           None, spawn=False)
        runner.execute_suite(suite, members, {})
        run = runner.snapshot()
        self.assertEqual([member['status']
                          for member in run['suite']['members']],
                         ['ok', 'skipped'])
        self.assertIn('no card in the reader',
                      run['suite']['members'][1]['note'])

    def test_a_session_change_stops_the_suite(self):
        card = Card()
        state = make_state(self, card)
        card.status('9000').status('9000')
        members = [
            ({'script_id': script_id(0), 'role': 'member', 'on_fail': 'stop'},
             normalise([dict(STATUS_9K)], name='one')),
            ({'script_id': script_id(1), 'role': 'member', 'on_fail': 'stop'},
             normalise([dict(STATUS_9K)], name='two')),
        ]
        suite = {'id': 'a' * 32, 'name': 'suite', 'require_adm': False,
                 'start_reset': False}
        runner = state.test_runner
        runner.start_suite(suite, members, {}, None, {'required': False},
                           None, spawn=False)
        real = runner._run_status

        def wrapped(step):
            data, sw, sent = real(step)
            state.card_session += 1        # the card was re-equipped
            return data, sw, sent

        runner._run_status = wrapped
        runner.execute_suite(suite, members, {})
        run = runner.snapshot()
        suite_report = run['suite']
        self.assertEqual([member['status']
                          for member in suite_report['members']],
                         ['ok', 'skipped'])
        self.assertIn('card session changed',
                      suite_report['members'][1]['note'])
        self.assertTrue(suite_report['session_changed'])
        self.assertTrue(suite_report['summary']['session_changed'])

    def test_a_stop_request_marks_the_rest_stopped(self):
        card = Card()
        state = make_state(self, card)
        card.status('9000')
        members = [
            ({'script_id': script_id(0), 'role': 'member', 'on_fail': 'stop'},
             normalise([dict(STATUS_9K)], name='one')),
            ({'script_id': script_id(1), 'role': 'member', 'on_fail': 'stop'},
             normalise([dict(STATUS_9K)], name='two')),
        ]
        suite = {'id': 'a' * 32, 'name': 'suite', 'require_adm': False,
                 'start_reset': False}
        runner = state.test_runner
        runner.start_suite(suite, members, {}, None, {'required': False},
                           None, spawn=False)
        real = runner._run_status

        def wrapped(step):
            data, sw, sent = real(step)
            runner._run['stop'] = True
            return data, sw, sent

        runner._run_status = wrapped
        runner.execute_suite(suite, members, {})
        run = runner.snapshot()
        self.assertEqual(run['status'], 'stopped')
        self.assertEqual([member['status']
                          for member in run['suite']['members']],
                         ['ok', 'skipped'])
        self.assertIn('stopped', run['suite']['members'][1]['note'])
        self.assertTrue(run['suite']['summary']['stopped'])

    def test_the_members_share_the_scp80_counter(self):
        card = Card()
        state = make_state(self, card)
        card.envelope(por_packet(cntr='0000000010'), '9000')
        card.envelope(por_packet(cntr='0000000011'), '9000')
        preset = state.presets.add(preset_fields())
        members = [
            ({'script_id': script_id(0), 'role': 'member', 'on_fail': 'stop'},
             normalise([{'type': 'action', 'kind': 'scp80',
                         'params': {'apdu': '80E2900000'},
                         'check': {'sw': '9000', 'por': 'ok'}}], name='one')),
            ({'script_id': script_id(1), 'role': 'member', 'on_fail': 'stop'},
             normalise([{'type': 'action', 'kind': 'scp80',
                         'params': {'apdu': '80E2900000'},
                         'check': {'sw': '9000', 'por': 'ok'}}], name='two')),
        ]
        suite = {'id': 'a' * 32, 'name': 'suite', 'require_adm': False,
                 'start_reset': False}
        runner = state.test_runner
        runner.start_suite(suite, members, preset, preset['id'],
                           {'required': False}, None, spawn=False)
        runner.execute_suite(suite, members, preset, preset['id'])
        run = runner.snapshot()
        suite_report = run['suite']
        self.assertEqual(run['status'], 'ok', suite_report['members'])
        # the second packet continues from the first, not from the snapshot
        self.assertEqual(suite_report['members'][0]['steps'][0]['counter'],
                         '0000000010')
        self.assertEqual(suite_report['members'][1]['steps'][0]['counter'],
                         '0000000011')
        self.assertEqual(state.presets.get(preset['id'])['keysets'][0]['cntr'],
                         '0000000012')
        self.assertEqual(suite_report['counters_before'], {'1': '0000000010'})
        self.assertEqual(suite_report['counters_after'], {'1': '0000000012'})
        self.assertEqual(suite_report['summary']['counters_after'],
                         {'1': '0000000012'})

    def test_the_run_state_is_cleared_after_finish(self):
        state, runner, run = run_suite(
            self, [dict(STATUS_9K)], setup=lambda card: card.status('9000'))
        self.assertFalse(run['running'])
        self.assertFalse(state.test_running)
        self.assertEqual(run['log_prefix'], '')
        self.assertIsNotNone(run['finished'])

    def test_a_raised_worker_error_closes_the_report(self):
        card = Card()
        state = make_state(self, card)
        card.status('9000')
        members = [
            ({'script_id': script_id(0), 'role': 'member', 'on_fail': 'stop'},
             normalise([dict(STATUS_9K)], name='one')),
            ({'script_id': script_id(1), 'role': 'member', 'on_fail': 'stop'},
             normalise([dict(STATUS_9K)], name='two')),
        ]
        suite = {'id': 'a' * 32, 'name': 'suite', 'require_adm': False,
                 'start_reset': False}
        runner = state.test_runner
        runner.start_suite(suite, members, {}, None, {'required': False},
                           None, spawn=False)
        real = runner.run_script
        calls = []

        def flaky(script, preset, shared=None):
            calls.append(script['name'])
            if len(calls) == 2:
                raise RuntimeError('boom')
            return real(script, preset, shared=shared)

        with mock.patch.object(runner, 'run_script', side_effect=flaky):
            runner._suite_worker(suite, members, {}, None)
        run = runner.snapshot()
        self.assertFalse(run['running'])
        self.assertEqual(run['status'], 'error')
        self.assertIn('boom', run['error'])
        self.assertFalse(state.test_running)
        # the report is renderable: no member stays pending/running
        self.assertEqual([member['status']
                          for member in run['suite']['members']],
                         ['ok', 'error'])
        self.assertIn('boom', run['suite']['members'][1]['note'])
        self.assertIsNotNone(run['suite']['summary'])
        self.assertEqual(run['suite']['summary']['error'], 1)
        self.assertEqual(run['suite']['summary']['ok'], 1)


class SuiteEndpointRunTests(unittest.TestCase):
    """The ``/api/test/run {suite_id}`` path (the endpoint's own rules)."""

    def _state_with_suite(self, require_adm=False, steps=None,
                          suite_fields=None):
        state = make_state(self, Card())
        state.transport.handler.status('9000')
        suite = state.test_suites.add(dict({'name': 'stored suite',
                                            'require_adm': require_adm},
                                           **(suite_fields or {})))
        script = state.test_scripts.add(
            {'name': 'member', 'suite_id': suite['id'],
             'steps': steps or [dict(STATUS_9K)]})
        state.test_suites.update(
            suite['id'], {'scripts': [{'script_id': script['id'],
                                       'role': 'member', 'on_fail': 'stop'}]})
        return state, suite, script

    def wait(self, state, harness, timeout=10):
        deadline = time.time() + timeout
        body = None
        while time.time() < deadline:
            _status, body = harness.json('GET', '/api/test/status')
            if not body['running']:
                return body
            time.sleep(0.02)
        return body

    def test_a_stored_suite_runs(self):
        state, suite, _script = self._state_with_suite()
        harness = Harness(state)
        self.addCleanup(harness.close)
        status, body = harness.json('POST', '/api/test/run',
                                    {'suite_id': suite['id']})
        self.assertEqual(status, 200, body)
        self.assertTrue(body['running'])
        self.assertEqual(body['kind'], 'suite')
        final = self.wait(state, harness)
        self.assertEqual(final['status'], 'ok')
        self.assertEqual(final['suite']['id'], suite['id'])
        self.assertEqual([member['status']
                          for member in final['suite']['members']], ['ok'])

    def test_a_load_suite_runs_its_iterations(self):
        state, suite, _script = self._state_with_suite()
        harness = Harness(state)
        self.addCleanup(harness.close)
        status, body = harness.json('POST', '/api/test/run',
                                    {'suite_id': suite['id'],
                                     'iterations': 3})
        self.assertEqual(status, 200, body)
        self.assertTrue(body['load']['enabled'])
        self.assertTrue(body['load']['suite'])
        self.assertEqual(body['load']['iterations'], 3)
        final = self.wait(state, harness)
        self.assertEqual(final['status'], 'ok')
        self.assertEqual(final['load']['done'], 3)
        self.assertEqual(final['load']['iteration'], 3)
        self.assertIsNone(final['load']['failed_iteration'])
        # a successful load suite keeps no suite report
        self.assertIsNone(final['suite'])

    def test_an_empty_suite_is_refused(self):
        state = make_state(self, Card())
        suite = state.test_suites.add({'name': 'empty'})
        harness = Harness(state)
        self.addCleanup(harness.close)
        status, body = harness.json('POST', '/api/test/run',
                                    {'suite_id': suite['id']})
        self.assertEqual(status, 400)
        self.assertIn('no scripts', body['error'])
        self.assertFalse(state.test_running)

    def test_a_missing_member_refuses_the_run(self):
        state, suite, script = self._state_with_suite()
        state.test_scripts._scripts = []
        harness = Harness(state)
        self.addCleanup(harness.close)
        status, body = harness.json('POST', '/api/test/run',
                                    {'suite_id': suite['id']})
        self.assertEqual(status, 400)
        self.assertIn('unknown test script', body['error'])
        self.assertFalse(state.test_running)

    def test_an_invalid_member_refuses_the_run(self):
        state, suite, script = self._state_with_suite()
        # a hand edit that no longer validates (the store keeps it)
        state.test_scripts._scripts = [dict(script,
                                            steps=[{'type': 'action',
                                                    'kind': 'nope'}])]
        harness = Harness(state)
        self.addCleanup(harness.close)
        status, body = harness.json('POST', '/api/test/run',
                                    {'suite_id': suite['id']})
        self.assertEqual(status, 400)
        self.assertIn('does not validate', body['error'])

    def test_the_adm_gate_refuses_without_a_preset_key(self):
        state, suite, _script = self._state_with_suite(require_adm=True)
        preset = state.presets.add(preset_fields())        # no ADM key
        harness = Harness(state)
        self.addCleanup(harness.close)
        status, body = harness.json('POST', '/api/test/run',
                                    {'suite_id': suite['id'],
                                     'preset_id': preset['id']})
        self.assertEqual(status, 400)
        self.assertIn('no ADM key', body['error'])
        self.assertTrue(body['adm']['required'])
        self.assertFalse(state.test_running)

    def test_a_member_require_adm_counts(self):
        state, suite, script = self._state_with_suite()
        state.test_scripts.update(script['id'], {'require_adm': True})
        preset = state.presets.add(preset_fields())        # no ADM key
        harness = Harness(state)
        self.addCleanup(harness.close)
        status, body = harness.json('POST', '/api/test/run',
                                    {'suite_id': suite['id'],
                                     'preset_id': preset['id']})
        self.assertEqual(status, 400)
        self.assertIn('no ADM key', body['error'])

    def test_the_scp80_precondition_covers_every_member(self):
        state, suite, _script = self._state_with_suite(
            steps=[{'type': 'action', 'kind': 'scp80',
                    'params': {'apdu': '80E2900000'},
                    'check': {'sw': '9000'}}])
        harness = Harness(state)
        self.addCleanup(harness.close)
        # an inline preset (the store refuses an incomplete keyset at save)
        status, body = harness.json(
            'POST', '/api/test/run',
            {'suite_id': suite['id'],
             'preset': {'name': 'inline', 'keysets': []}})
        self.assertEqual(status, 400)
        self.assertIn('no keyset is defined', body['error'])
        self.assertFalse(state.test_running)


class StartResetTests(unittest.TestCase):
    def _state(self, iccid_after='8970119000004600098'):
        state = make_state(self, Card())
        state.transport.handler.status('9000')
        suite = state.test_suites.add({'name': 'reset suite',
                                       'start_reset': True})
        script = state.test_scripts.add(
            {'name': 'member', 'suite_id': suite['id'],
             'steps': [dict(STATUS_9K)]})
        state.test_suites.update(
            suite['id'], {'scripts': [{'script_id': script['id'],
                                       'role': 'member', 'on_fail': 'stop'}]})
        calls = []

        def fake_reset():
            calls.append('reset')
            state.card_session += 1
            state.adm_session = None
            state.iccid = iccid_after
            return state.status()

        return state, suite, calls, fake_reset

    def test_the_reset_runs_before_the_adm_gate_and_is_reported(self):
        from simple_server.server import ServerState
        state, suite, calls, fake_reset = self._state()
        harness = Harness(state)
        self.addCleanup(harness.close)
        real_ensure_adm = state.test_runner.ensure_adm

        def ensure_adm(preset, needed):
            calls.append('adm')
            return real_ensure_adm(preset, needed)

        with mock.patch.object(ServerState, 'reset', autospec=True,
                               side_effect=lambda self: fake_reset()), \
                mock.patch.object(state.test_runner, 'ensure_adm',
                                  side_effect=ensure_adm):
            status, body = harness.json('POST', '/api/test/run',
                                        {'suite_id': suite['id']})
        self.assertEqual(status, 200, body)
        self.assertEqual(calls[:2], ['reset', 'adm'])
        self.assertTrue(body['suite']['start_reset'])
        self.assertEqual(body['suite']['reset']['iccid'],
                         '8970119000004600098')
        self.assertTrue(body['suite']['reset']['ok'])
        self.assertIsNotNone(body['suite']['reset']['ms'])
        # the run's session is the post-reset one
        self.assertEqual(body['suite']['session_start'], state.card_session)
        deadline = time.time() + 10
        while time.time() < deadline and state.test_running:
            time.sleep(0.02)
        self.assertFalse(state.test_running)

    def test_the_adm_latch_is_reverified_after_the_reset(self):
        # the start reset changes the card session, so a verification made
        # before the run (the ADM badge) no longer covers it: the gate must
        # verify the key again for the post-reset session
        state, suite, _calls, fake_reset = self._state()
        state.test_suites.update(suite['id'], {'require_adm': True})
        preset = state.presets.add(preset_fields(adm='0123456789ABCDEF'))
        state.adm_session = state.card_session          # the badge's latch
        verified = []

        def fake_verify(body):
            verified.append(body['adm'])
            state.adm_session = state.card_session
            return {'ok': True, 'sw': '9000', 'attempts_left': 3}

        harness = Harness(state)
        self.addCleanup(harness.close)
        with mock.patch.object(type(state), 'reset', autospec=True,
                               side_effect=lambda self: fake_reset()), \
                mock.patch.object(type(state), 'verify_adm', autospec=True,
                                  side_effect=lambda self, body:
                                  fake_verify(body)):
            status, body = harness.json('POST', '/api/test/run',
                                        {'suite_id': suite['id'],
                                         'preset_id': preset['id']})
        self.assertEqual(status, 200, body)
        self.assertEqual(verified, ['0123456789ABCDEF'])
        self.assertTrue(body['adm']['verified'])
        self.assertTrue(body['adm']['checked'])
        deadline = time.time() + 10
        while time.time() < deadline and state.test_running:
            time.sleep(0.02)
        self.assertFalse(state.test_running)

    def test_a_failed_scp80_preset_does_not_reset(self):
        # a run that cannot start must not touch the card (the v3.23.2 fix):
        # the SCP80 preset check runs before the start reset
        state = make_state(self, Card())
        state.transport.handler.status('9000')
        suite = state.test_suites.add({'name': 'reset suite',
                                       'start_reset': True})
        script = state.test_scripts.add(
            {'name': 'member', 'suite_id': suite['id'],
             'steps': [{'type': 'action', 'kind': 'scp80',
                        'params': {'apdu': '80E2900000'},
                        'check': {'sw': '9000'}}]})
        state.test_suites.update(
            suite['id'], {'scripts': [{'script_id': script['id'],
                                       'role': 'member', 'on_fail': 'stop'}]})
        harness = Harness(state)
        self.addCleanup(harness.close)
        with mock.patch.object(type(state), 'reset', autospec=True) as reset:
            status, body = harness.json(
                'POST', '/api/test/run',
                {'suite_id': suite['id'],
                 'preset': {'name': 'inline', 'keysets': []}})
        self.assertEqual(status, 400, body)
        self.assertIn('no keyset is defined', body['error'])
        reset.assert_not_called()
        self.assertFalse(state.test_running)

    def test_a_preset_card_mismatch_refuses_the_run(self):
        state, suite, calls, fake_reset = self._state(
            iccid_after='8970119000004600000')
        preset = state.presets.add(preset_fields())    # the other ICCID
        harness = Harness(state)
        self.addCleanup(harness.close)
        with mock.patch.object(type(state), 'reset', autospec=True,
                               side_effect=lambda self: fake_reset()):
            status, body = harness.json('POST', '/api/test/run',
                                        {'suite_id': suite['id'],
                                         'preset_id': preset['id']})
        self.assertEqual(status, 400)
        self.assertIn('after the reset', body['error'])
        self.assertFalse(state.test_running)

    def test_a_failed_reset_refuses_the_run(self):
        state, suite, _calls, _fake = self._state()
        harness = Harness(state)
        self.addCleanup(harness.close)

        def boom(self):
            raise RuntimeError('no card')

        with mock.patch.object(type(state), 'reset', autospec=True,
                               side_effect=boom):
            status, body = harness.json('POST', '/api/test/run',
                                        {'suite_id': suite['id']})
        self.assertEqual(status, 500)
        self.assertIn('re-initialization failed', body['error'])
        self.assertFalse(state.test_running)

    def test_the_reset_needs_a_card(self):
        state, suite, _calls, _fake = self._state()
        state.disconnect()               # no card session any more
        harness = Harness(state)
        self.addCleanup(harness.close)
        status, body = harness.json('POST', '/api/test/run',
                                    {'suite_id': suite['id']})
        self.assertEqual(status, 400)
        self.assertIn('no card is equipped', body['error'])


# ---- the suite load test ----------------------------------------------------

class LoadSuiteTests(unittest.TestCase):
    def _members(self, count=1):
        return [
            ({'script_id': script_id(index), 'role': 'member',
              'on_fail': 'stop'},
             normalise([dict(STATUS_9K)], name='member %d' % (index + 1)))
            for index in range(count)
        ]

    def _start(self, state, members, members_in_suite=0, iterations=3):
        suite = {'id': 'a' * 32, 'name': 'load suite', 'require_adm': False,
                 'start_reset': False}
        runner = state.test_runner
        runner.start_suite(suite, members, {}, None, {'required': False},
                           None, iterations, spawn=False)
        return suite, runner

    def test_successful_iterations_are_discarded(self):
        state = make_state(self, Card())
        state.transport.handler.status('9000')
        members = self._members(2)
        suite, runner = self._start(state, members, iterations=3)
        runner._load_suite_worker(suite, members, {}, None, 3)
        run = runner.snapshot()
        self.assertEqual(run['status'], 'ok')
        self.assertEqual(run['kind'], 'suite')
        self.assertTrue(run['load']['enabled'])
        self.assertTrue(run['load']['suite'])
        self.assertEqual(run['load']['done'], 3)
        self.assertEqual(run['load']['iteration'], 3)
        self.assertIsNone(run['load']['failed_iteration'])
        self.assertIsNone(run['suite'])
        self.assertEqual(run['steps'], [])
        self.assertIsNotNone(run['load']['elapsed_ms'])
        self.assertFalse(run['running'])

    def test_an_error_keeps_that_iteration(self):
        state = make_state(self, Card())
        state.transport.handler.status('9000').status('6A82')
        members = self._members()
        suite, runner = self._start(state, members, iterations=5)
        runner._load_suite_worker(suite, members, {}, None, 5)
        run = runner.snapshot()
        self.assertEqual(run['status'], 'error')
        self.assertEqual(run['load']['done'], 1)
        self.assertEqual(run['load']['failed_iteration'], 2)
        self.assertEqual(run['error'], 'iteration 2 failed')
        self.assertIsNotNone(run['suite'])
        self.assertEqual(run['suite']['members'][0]['status'], 'error')

    def test_a_stop_ends_after_the_iteration(self):
        state = make_state(self, Card())
        state.transport.handler.status('9000')
        members = self._members()
        suite, runner = self._start(state, members, iterations=50)
        real = runner._run_status
        calls = []

        def wrapped(step):
            calls.append(step)
            if len(calls) == 2:
                runner._run['stop'] = True
            return real(step)

        runner._run_status = wrapped
        runner._load_suite_worker(suite, members, {}, None, 50)
        run = runner.snapshot()
        self.assertEqual(run['status'], 'stopped')
        self.assertEqual(run['load']['done'], 2)
        self.assertEqual(len(calls), 2)

    def test_a_stop_during_the_teardown_ends_the_load(self):
        # the teardown runs with the stop flag cleared (a pending stop must
        # not cut it short), but a Stop pressed *inside* it is a real request:
        # it must end the load after this iteration
        state = make_state(self, Card())
        state.transport.handler.status('9000')
        members = [
            ({'script_id': script_id(0), 'role': 'member', 'on_fail': 'stop'},
             normalise([dict(STATUS_9K)], name='one')),
            ({'script_id': script_id(1), 'role': 'teardown',
              'on_fail': 'stop'},
             normalise([dict(STATUS_9K)], name='cleanup')),
        ]
        suite, runner = self._start(state, members, iterations=5)
        real = runner._run_status
        calls = []

        def wrapped(step):
            result = real(step)
            calls.append(step)
            if len(calls) == 2:            # the teardown's STATUS
                runner._run['stop'] = True
            return result

        runner._run_status = wrapped
        runner._load_suite_worker(suite, members, {}, None, 5)
        run = runner.snapshot()
        self.assertEqual(len(calls), 2)    # the second iteration never started
        self.assertEqual(run['load']['done'], 1)
        self.assertEqual(run['status'], 'stopped')

    def test_a_session_change_is_an_error(self):
        state = make_state(self, Card())
        state.transport.handler.status('9000')
        members = self._members()
        suite, runner = self._start(state, members, iterations=10)
        real = runner._run_status
        calls = []

        def wrapped(step):
            calls.append(step)
            state.card_session += 1
            return real(step)

        runner._run_status = wrapped
        runner._load_suite_worker(suite, members, {}, None, 10)
        run = runner.snapshot()
        self.assertEqual(run['status'], 'error')
        # the iteration that ran is discarded; the *next* one refuses to start
        # on the changed session (the legacy's rule)
        self.assertEqual(run['load']['failed_iteration'], 2)
        self.assertIn('session changed', run['error'])
        self.assertEqual(len(calls), 1)

    def test_a_warning_keeps_the_worst_verdict(self):
        state = make_state(self, Card())
        state.transport.handler.status('6A82')
        members = [({'script_id': script_id(0), 'role': 'member',
                     'on_fail': 'stop'},
                    normalise([failing_step('warning')], name='warn'))]
        suite, runner = self._start(state, members, iterations=2)
        runner._load_suite_worker(suite, members, {}, None, 2)
        run = runner.snapshot()
        self.assertEqual(run['status'], 'warning')
        self.assertEqual(run['load']['done'], 2)
        self.assertIsNone(run['suite'])


if __name__ == '__main__':
    unittest.main()
