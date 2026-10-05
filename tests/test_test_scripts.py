"""Test script store tests (server-side scripts).

The store replaces the localStorage scripts: scripts carry a stable uuid
(never array indices), every mutation validates against the same engine the
runner uses (so a stored script cannot fail at run start for a validation
reason), the old localStorage export imports as-is, and a corrupt store is
kept aside instead of being silently overwritten.
"""

import json
import pathlib
import tempfile
import unittest

from pysim_simple_server import test_scripts


def make_store(tmp, validator=None):
    return test_scripts.TestScriptStore(pathlib.Path(tmp) / 'test_scripts.json',
                                        validator=validator)


def script(name='demo', steps=None):
    return {'name': name,
            'steps': steps if steps is not None else [
                {'type': 'action', 'kind': 'status', 'params': {'attempts': 1}}]}


class StoreCrudTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = make_store(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_add_list_get_and_remove(self):
        s = self.store.add(script('one'))
        self.assertEqual(len(s['id']), 32)
        self.assertEqual(s['name'], 'one')
        self.assertEqual(self.store.list()[0]['id'], s['id'])
        got = self.store.get(s['id'])
        self.assertEqual(got['steps'], s['steps'])
        self.assertTrue(self.store.remove(s['id']))
        self.assertEqual(self.store.list(), [])
        self.assertFalse(self.store.remove(s['id']))
        self.assertIsNone(self.store.get('nope'))

    def test_callers_never_mutate_the_store(self):
        s = self.store.add(script('one'))
        s['steps'].append({'type': 'action', 'kind': 'status', 'params': {}})
        s['name'] = 'hacked'
        self.assertEqual(self.store.get(s['id'])['name'], 'one')
        self.assertEqual(len(self.store.get(s['id'])['steps']), 1)

    def test_update_replaces_name_and_steps_keeps_id_and_created(self):
        s = self.store.add(script('one'))
        renamed = self.store.update(s['id'], {'name': 'two'})
        self.assertEqual(renamed['id'], s['id'])
        self.assertEqual(renamed['name'], 'two')
        self.assertEqual(renamed['steps'], s['steps'])
        self.assertEqual(renamed['created'], s['created'])
        # a steps-only update keeps the name
        steps = s['steps'] + [{'type': 'expect', 'command': 'REFRESH',
                               'checks': [], 'respond': {'result': 0}}]
        updated = self.store.update(s['id'], {'steps': steps})
        self.assertEqual(updated['name'], 'two')
        self.assertEqual(len(updated['steps']), 2)
        self.assertIsNone(self.store.update('nope', {'name': 'x'}))

    def test_basic_shape_is_validated(self):
        with self.assertRaises(test_scripts.TestScriptError):
            self.store.add({'name': 'x', 'steps': []})
        with self.assertRaises(test_scripts.TestScriptError):
            self.store.add({'name': 'x', 'steps': [{'kind': 'status'}]})
        with self.assertRaises(test_scripts.TestScriptError):
            self.store.add({'name': 5, 'steps': [{'type': 'action'}]})
        # a rename that keeps the steps stays valid
        s = self.store.add(script())
        self.store.update(s['id'], {'name': 'renamed'})
        # ... but an invalid step list is refused
        with self.assertRaises(test_scripts.TestScriptError):
            self.store.update(s['id'], {'steps': [{}]})

    def test_validator_runs_and_an_invalid_script_is_refused(self):
        calls = []

        def validator(raw):
            calls.append(raw['name'])
            if raw['name'] == 'bad':
                raise ValueError('validator says no')
            self.assertEqual(raw['steps'][0]['type'], 'action')
            return raw

        store = make_store(self.tmp.name, validator=validator)
        s = store.add(script('good'))
        self.assertEqual(s['name'], 'good')     # the submitted shape is kept
        self.assertEqual(calls, ['good'])
        with self.assertRaises(test_scripts.TestScriptError) as ctx:
            store.add(script('bad'))
        self.assertIn('validator says no', str(ctx.exception))
        # a stored script re-validates on update (the normaliser's own output
        # is not re-normalisable, so the submitted shape is what is kept)
        renamed = store.update(s['id'], {'name': 'renamed'})
        self.assertEqual(renamed['name'], 'renamed')
        self.assertEqual(calls, ['good', 'bad', 'renamed'])


class StorePersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_an_entry_that_fails_the_validator_is_kept_and_survives_a_save(self):
        # a tightened engine rule (or a hand edit) must not silently destroy a
        # stored script on load - it stays, fails at run/update time, and the
        # next save keeps it on disk
        store = make_store(self.tmp.name, validator=lambda raw: raw)
        store.add(script('one'))

        def strict(raw):
            if raw['name'] == 'one':
                raise ValueError('rule tightened in a later version')
            return raw

        reopened = make_store(self.tmp.name, validator=strict)
        self.assertEqual([s['name'] for s in reopened.list()], ['one'])
        reopened.add(script('two'))
        again = make_store(self.tmp.name, validator=strict)
        self.assertEqual(sorted(s['name'] for s in again.list()), ['one', 'two'])
        # the kept entry is refused by the mutation paths while it stays invalid
        # (here the validator rejects the name 'one'), and renaming it - the
        # editor's fix - succeeds
        one = next(s for s in reopened.list() if s['name'] == 'one')
        with self.assertRaises(test_scripts.TestScriptError):
            reopened.update(one['id'], {'name': 'one'})
        fixed = reopened.update(one['id'], {'name': 'renamed'})
        self.assertEqual(fixed['name'], 'renamed')

    def test_round_trip_keeps_ids_and_timestamps(self):
        store = make_store(self.tmp.name)
        s = store.add(script('one'))
        reopened = make_store(self.tmp.name)
        again = reopened.get(s['id'])
        self.assertEqual(again['name'], 'one')
        self.assertEqual(again['steps'], s['steps'])
        self.assertEqual(again['created'], s['created'])
        self.assertEqual(again['updated'], s['updated'])

    def test_the_file_carries_the_schema_version(self):
        store = make_store(self.tmp.name)
        store.add(script('one'))
        data = json.loads(store.path.read_text(encoding='utf-8'))
        self.assertEqual(data['version'], test_scripts.SCHEMA_VERSION)
        self.assertEqual(len(data['scripts']), 1)

    def test_a_corrupt_store_is_kept_aside(self):
        path = pathlib.Path(self.tmp.name) / 'test_scripts.json'
        path.write_text('{not json', encoding='utf-8')
        store = test_scripts.TestScriptStore(path)
        self.assertEqual(store.list(), [])
        bad = sorted(pathlib.Path(self.tmp.name).glob('test_scripts.json.bad-*'))
        self.assertEqual(len(bad), 1)
        self.assertEqual(bad[0].read_text(encoding='utf-8'), '{not json')


class StoreImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = make_store(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_import_merge_skips_invalid_and_reports_them(self):
        resp = self.store.import_scripts([
            script('one'),
            {'name': 'broken', 'steps': []},
            'not a dict',
            script('two'),
        ])
        self.assertEqual(resp['added'], 2)
        self.assertEqual(resp['skipped'], 2)
        self.assertTrue(resp['errors'])
        self.assertEqual([s['name'] for s in self.store.list()], ['one', 'two'])

    def test_import_replace_wipes_first(self):
        self.store.add(script('old'))
        resp = self.store.import_scripts([script('new')], mode='replace')
        self.assertEqual(resp['added'], 1)
        self.assertEqual([s['name'] for s in self.store.list()], ['new'])

    def test_import_keeps_a_given_id_and_reassigns_a_duplicate(self):
        first = self.store.add(script('one'))
        entry = dict(script('copy'))
        entry['id'] = first['id']
        resp = self.store.import_scripts([entry])
        self.assertEqual(resp['added'], 1)
        ids = [s['id'] for s in self.store.list()]
        self.assertEqual(len(set(ids)), 2)

    def test_a_localstorage_export_imports_as_is(self):
        # the old PWA export: an array of {name, steps}
        resp = self.store.import_scripts([
            {'name': 'menu', 'steps': [{'type': 'action', 'kind': 'menu-select',
                                        'params': {'item_id': 1},
                                        'check': {'sw': {'mode': 'exact', 'value': '9000'}}}]},
        ])
        self.assertEqual(resp['added'], 1)
        self.assertEqual(self.store.list()[0]['name'], 'menu')


class StoreInfoTests(unittest.TestCase):
    def test_info_reports_path_and_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = make_store(tmp)
            store.add(script('one'))
            info = store.info()
            self.assertEqual(info['path'], str(store.path))
            self.assertEqual(info['count'], 1)
            self.assertEqual(info['version'], test_scripts.SCHEMA_VERSION)


class SuiteAttachmentTests(unittest.TestCase):
    """v3.22.0: a script belongs to a suite (suite_id), declares require_adm,
    and the import reports the ids a bundle import needs."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.tmp.name) / 'test_scripts.json'
        self.store = test_scripts.TestScriptStore(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_suite_id_and_require_adm_round_trip(self):
        sid = 'a' * 32
        s = self.store.add(dict(script('one'), suite_id=sid, require_adm=True))
        self.assertEqual(s['suite_id'], sid)
        self.assertTrue(s['require_adm'])
        got = test_scripts.TestScriptStore(self.path).get(s['id'])
        self.assertEqual(got['suite_id'], sid)
        self.assertTrue(got['require_adm'])
        with self.assertRaises(test_scripts.TestScriptError):
            self.store.add(dict(script('two'), suite_id='short'))

    def test_orphans_and_list_by_suite(self):
        sid = 'a' * 32
        s1 = self.store.add(dict(script('one'), suite_id=sid))
        s2 = self.store.add(script('two'))
        self.assertEqual([x['id'] for x in self.store.list_by_suite(sid)], [s1['id']])
        self.assertEqual([x['id'] for x in self.store.orphans()], [s2['id']])
        # a move is an update
        moved = self.store.update(s2['id'], {'suite_id': sid})
        self.assertEqual(moved['suite_id'], sid)
        self.assertEqual(self.store.orphans(), [])
        self.assertEqual(len(self.store.list_by_suite(sid)), 2)

    def test_import_reports_the_id_map_and_attaches_to_a_suite(self):
        sid = 'a' * 32
        s = self.store.add(script('one'))
        result = self.store.import_scripts([self.store.get(s['id'])])
        self.assertEqual(result['added'], 1)
        self.assertEqual(result['id_map'][s['id']], result['added_ids'][0])
        self.assertNotEqual(result['added_ids'][0], s['id'])
        # import into a named suite: every added script belongs to it
        result = self.store.import_scripts(
            [script('two'), script('three')], suite_id=sid)
        self.assertEqual(result['added'], 2)
        for sid_added in result['added_ids']:
            self.assertEqual(self.store.get(sid_added)['suite_id'], sid)

    def test_a_v1_store_loads_with_no_suite(self):
        self.store.add(script('one'))
        data = json.loads(self.path.read_text())
        data['version'] = 1
        for e in data['scripts']:
            e.pop('suite_id', None)
            e.pop('require_adm', None)
        self.path.write_text(json.dumps(data))
        again = test_scripts.TestScriptStore(self.path)
        got = again.list()[0]
        self.assertIsNone(got['suite_id'])
        self.assertFalse(got['require_adm'])
        self.assertEqual(len(again.orphans()), 1)
