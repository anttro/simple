"""Test suite store tests (server-side suites, v3.22.0).

A suite groups the test scripts: the root object, holding the ordered member
list by reference (setup first, members, teardown last - the store
normalises the order), the per-member on_fail policy and the suite-level
require_adm flag.  The cross-store rules live in the HTTP layer; this file
covers the store's own shape, ordering, persistence and import behaviour.
"""

import json
import pathlib
import tempfile
import unittest

from pysim_simple_server import test_suites

SID_A = 'a' * 32
SID_B = 'b' * 32
SID_C = 'c' * 32


def make_store(tmp):
    return test_suites.TestSuiteStore(pathlib.Path(tmp) / 'test_suites.json')


def suite(name='demo', scripts=None, **kw):
    s = {'name': name, 'scripts': scripts if scripts is not None else []}
    s.update(kw)
    return s


class StoreCrudTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = make_store(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_add_list_get_and_remove(self):
        s = self.store.add(suite())
        self.assertEqual(len(s['id']), 32)
        self.assertEqual(s['name'], 'demo')
        self.assertEqual(s['require_adm'], False)
        self.assertEqual(self.store.list()[0]['id'], s['id'])
        self.assertTrue(self.store.remove(s['id']))
        self.assertEqual(self.store.list(), [])
        self.assertFalse(self.store.remove(s['id']))
        self.assertIsNone(self.store.get('nope'))

    def test_callers_never_mutate_the_store(self):
        s = self.store.add(suite(scripts=[
            {'script_id': SID_A, 'role': 'member', 'on_fail': 'stop'}]))
        s['scripts'][0]['on_fail'] = 'continue'
        s['name'] = 'hacked'
        self.assertEqual(self.store.get(s['id'])['name'], 'demo')
        self.assertEqual(self.store.get(s['id'])['scripts'][0]['on_fail'], 'stop')

    def test_roles_are_ordered_setup_first_teardown_last(self):
        # a caller may send any order; the store normalises it and marks the
        # roles - setup first, teardown last, members keep their order
        s = self.store.add(suite(scripts=[
            {'script_id': SID_C, 'role': 'teardown', 'on_fail': 'stop'},
            {'script_id': SID_A, 'role': 'member', 'on_fail': 'continue'},
            {'script_id': SID_B, 'role': 'member', 'on_fail': 'stop'},
            {'script_id': SID_A.replace('a', 'd'), 'role': 'setup', 'on_fail': 'stop'},
        ]))
        ids = [e['script_id'] for e in s['scripts']]
        self.assertEqual(ids[0], 'd' * 32)
        self.assertEqual(ids[-1], SID_C)
        self.assertEqual(ids[1:3], [SID_A, SID_B])
        roles = [e['role'] for e in s['scripts']]
        self.assertEqual(roles, ['setup', 'member', 'member', 'teardown'])
        self.assertEqual(s['scripts'][1]['on_fail'], 'continue')

    def test_shape_is_validated(self):
        with self.assertRaises(test_suites.TestSuiteError):
            self.store.add('nope')
        with self.assertRaises(test_suites.TestSuiteError):
            self.store.add(suite(scripts='nope'))
        with self.assertRaises(test_suites.TestSuiteError):
            self.store.add(suite(scripts=[{'script_id': 'short'}]))
        with self.assertRaises(test_suites.TestSuiteError):
            self.store.add(suite(scripts=[{'script_id': SID_A, 'role': 'boss'}]))
        with self.assertRaises(test_suites.TestSuiteError):
            self.store.add(suite(scripts=[{'script_id': SID_A, 'on_fail': 'maybe'}]))
        # one script appears once per suite
        with self.assertRaises(test_suites.TestSuiteError):
            self.store.add(suite(scripts=[
                {'script_id': SID_A}, {'script_id': SID_A}]))
        # at most one setup / teardown
        with self.assertRaises(test_suites.TestSuiteError):
            self.store.add(suite(scripts=[
                {'script_id': SID_A, 'role': 'setup'},
                {'script_id': SID_B, 'role': 'setup'}]))
        with self.assertRaises(test_suites.TestSuiteError):
            self.store.add(suite(scripts=[
                {'script_id': SID_A, 'role': 'teardown'},
                {'script_id': SID_B, 'role': 'teardown'}]))

    def test_update_keeps_id_and_created(self):
        s = self.store.add(suite(name='one'))
        updated = self.store.update(s['id'], {
            'name': 'two', 'require_adm': True,
            'scripts': [{'script_id': SID_A, 'role': 'member', 'on_fail': 'stop'}]})
        self.assertEqual(updated['id'], s['id'])
        self.assertEqual(updated['created'], s['created'])
        self.assertEqual(updated['name'], 'two')
        self.assertTrue(updated['require_adm'])
        self.assertEqual(len(updated['scripts']), 1)
        self.assertIsNone(self.store.update('nope', {'name': 'x'}))

    def test_find_by_name(self):
        self.store.add(suite(name='alpha'))
        self.assertEqual(self.store.find_by_name('alpha')['name'], 'alpha')
        self.assertIsNone(self.store.find_by_name('beta'))


class StorePersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.tmp.name) / 'test_suites.json'
        self.store = test_suites.TestSuiteStore(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_round_trip_keeps_ids_and_timestamps(self):
        s = self.store.add(suite(require_adm=True, scripts=[
            {'script_id': SID_A, 'role': 'member', 'on_fail': 'stop'}]))
        again = test_suites.TestSuiteStore(self.path)
        got = again.get(s['id'])
        self.assertEqual(got['name'], s['name'])
        self.assertEqual(got['created'], s['created'])
        self.assertTrue(got['require_adm'])
        self.assertEqual(got['scripts'], s['scripts'])

    def test_the_file_carries_the_schema_version(self):
        self.store.add(suite())
        data = json.loads(self.path.read_text())
        self.assertEqual(data['version'], test_suites.SCHEMA_VERSION)

    def test_an_entry_with_a_dangling_reference_is_kept(self):
        # a referenced script removed by hand (or an import that lost it): the
        # suite keeps loading - it is served (the editor shows the missing
        # reference) and the HTTP layer refuses a save/run until it is fixed
        self.store.add(suite(scripts=[{'script_id': SID_A, 'role': 'member',
                                       'on_fail': 'stop'}]))
        again = test_suites.TestSuiteStore(self.path)
        self.assertEqual(len(again.list()), 1)
        self.assertEqual(again.list()[0]['scripts'][0]['script_id'], SID_A)

    def test_a_shape_invalid_entry_is_dropped_with_a_log(self):
        # an unreadable entry (a malformed script id) is not servable: unlike
        # the engine-validated scripts, the store's own shape check drops it
        self.store.add(suite())
        raw = json.loads(self.path.read_text())
        raw['suites'][0]['scripts'] = [{'script_id': 'short', 'role': 'member',
                                        'on_fail': 'stop'}]
        self.path.write_text(json.dumps(raw))
        again = test_suites.TestSuiteStore(self.path)
        self.assertEqual(again.list(), [])


class StoreImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = make_store(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_import_merge_and_replace(self):
        self.store.add(suite(name='old'))
        result = self.store.import_suites([suite(name='new', scripts=[
            {'script_id': SID_A, 'role': 'member', 'on_fail': 'stop'}])])
        self.assertEqual(result['added'], 1)
        self.assertEqual(result['skipped'], 0)
        self.assertEqual([s['name'] for s in self.store.list()], ['old', 'new'])
        result = self.store.import_suites([suite(name='third')], 'replace')
        self.assertEqual(result['added'], 1)
        self.assertEqual([s['name'] for s in self.store.list()], ['third'])

    def test_import_keeps_a_given_id_and_reassigns_a_duplicate(self):
        s = self.store.add(suite(name='one'))
        entry = dict(s, name='copy')
        result = self.store.import_suites([entry])
        self.assertEqual(result['added'], 1)
        ids = sorted(x['id'] for x in self.store.list())
        self.assertEqual(len(set(ids)), 2)
        self.assertIn(s['id'], ids)

    def test_import_reports_invalid_entries(self):
        result = self.store.import_suites([suite(name='bad', scripts=[
            {'script_id': 'nope'}]), suite(name='ok')])
        self.assertEqual(result['added'], 1)
        self.assertEqual(result['skipped'], 1)
        self.assertTrue(result['errors'])


class MigrationTests(unittest.TestCase):
    """v3.21.0 -> v3.22.0: scripts without a suite are attached to an
    auto-created "Imported scripts" suite, once (idempotent)."""

    def test_orphans_are_attached_to_the_imported_suite(self):
        from pysim_simple_server import server as srv
        from pysim_simple_server import test_scripts
        with tempfile.TemporaryDirectory() as tmp:
            scripts = test_scripts.TestScriptStore(pathlib.Path(tmp) / 's.json')
            suites = test_suites.TestSuiteStore(pathlib.Path(tmp) / 'u.json')
            step = {'type': 'action', 'kind': 'status', 'params': {'attempts': 1}}
            a = scripts.add({'name': 'a', 'steps': [step]})
            b = scripts.add({'name': 'b', 'steps': [step]})
            self.assertEqual(srv.migrate_scripts_to_suites(scripts, suites), 2)
            suite = suites.find_by_name('Imported scripts')
            self.assertIsNotNone(suite)
            self.assertEqual([e['script_id'] for e in suite['scripts']],
                             [a['id'], b['id']])
            self.assertEqual([e['role'] for e in suite['scripts']],
                             ['member', 'member'])
            self.assertEqual(scripts.get(a['id'])['suite_id'], suite['id'])
            # idempotent: nothing left to attach
            self.assertEqual(srv.migrate_scripts_to_suites(scripts, suites), 0)

    def test_an_invalid_script_is_still_listed_by_the_reconciliation(self):
        # The script store keeps an entry that no longer validates (a
        # tightened rule must not destroy it).  Its suite_id write is refused
        # by the same validator, but the member list is authoritative: the
        # script must be listed, or it would be invisible in the PWA and
        # could never be fixed in the editor.
        from pysim_simple_server import server as srv
        from pysim_simple_server import test_scripts
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / 's.json'
            step = {'type': 'action', 'kind': 'status', 'params': {'attempts': 1}}
            scripts = test_scripts.TestScriptStore(path)      # permissive
            script = scripts.add({'name': 'stale', 'steps': [step]})

            def validator(raw):
                raise ValueError('the rule tightened')

            strict = test_scripts.TestScriptStore(path, validator=validator)
            suites = test_suites.TestSuiteStore(pathlib.Path(tmp) / 'u.json')
            self.assertEqual(srv.reconcile_scripts_to_suites(strict, suites), 1)
            imported = suites.find_by_name('Imported scripts')
            self.assertEqual([e['script_id'] for e in imported['scripts']],
                             [script['id']])
            # the suite_id write could not go through - the listing is what
            # keeps the script visible and fixable
            self.assertIsNone(strict.get(script['id'])['suite_id'])

    def test_the_reconciliation_adopts_listed_and_missing_suite_scripts(self):
        # The general invariant repair: a script whose suite_id points at a
        # missing suite (a suites import with mode replace) joins "Imported
        # scripts"; a script a suite lists but whose suite_id is empty or
        # wrong adopts the listing suite (the member list is authoritative).
        from pysim_simple_server import server as srv
        from pysim_simple_server import test_scripts
        with tempfile.TemporaryDirectory() as tmp:
            scripts = test_scripts.TestScriptStore(pathlib.Path(tmp) / 's.json')
            suites = test_suites.TestSuiteStore(pathlib.Path(tmp) / 'u.json')
            step = {'type': 'action', 'kind': 'status', 'params': {'attempts': 1}}
            suite_a = suites.add({'name': 'A'})
            gone = 'f' * 32
            lost = scripts.add({'name': 'lost', 'suite_id': gone, 'steps': [step]})
            listed = scripts.add({'name': 'listed', 'steps': [step]})
            suites.update(suite_a['id'], {'scripts': [
                {'script_id': listed['id'], 'role': 'setup', 'on_fail': 'stop'}]})
            self.assertEqual(srv.reconcile_scripts_to_suites(scripts, suites), 2)
            # the listing suite wins (its role is kept)
            self.assertEqual(scripts.get(listed['id'])['suite_id'], suite_a['id'])
            # the lost one is adopted by "Imported scripts"
            imported = suites.find_by_name('Imported scripts')
            self.assertEqual(scripts.get(lost['id'])['suite_id'], imported['id'])
            self.assertEqual([e['script_id'] for e in imported['scripts']], [lost['id']])
            # idempotent
            self.assertEqual(srv.reconcile_scripts_to_suites(scripts, suites), 0)


if __name__ == '__main__':
    unittest.main()
