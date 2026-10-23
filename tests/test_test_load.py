"""The load-testing run mode (v3.37.0): a script or suite repeated N times.

Successful iterations are discarded (only the count grows); the first error
keeps that iteration's steps/log as the report; a manual stop or a card session
change ends the loop.  The workers are driven here with the runner mocked, so
the loop itself is what the tests pin.
"""

import copy
import time
import unittest
from unittest import mock

from pysim_simple_server import server as srv


class _FakeServer:
    def __init__(self, session=7):
        self.card_session = session


def _reset_run():
    srv._TEST_RUN.update({
        'running': True, 'stop': False, 'name': 'load', 'status': None,
        'session': 7, 'index': 0, 'total': 0, 'steps': [], 'preset': 'p',
        'script_id': 's1', 'scp80_counter': None, 'scp80_counters': {},
        'started': time.time(), 'finished': None, 'error': None,
        'suite': None, 'log': [], 'log_prefix': '', 'adm': {'required': False},
        'load': srv._test_load_state(1),
    })


class LoadIterationsTests(unittest.TestCase):
    def test_default_is_one(self):
        self.assertEqual(srv._test_load_iterations({}), 1)
        self.assertEqual(srv._test_load_iterations({'iterations': None}), 1)
        self.assertEqual(srv._test_load_iterations({'iterations': ''}), 1)

    def test_the_count_is_validated(self):
        self.assertEqual(srv._test_load_iterations({'iterations': '5'}), 5)
        self.assertEqual(srv._test_load_iterations({'iterations': 7}), 7)
        for bad in (0, -1, 1.5, 'abc', srv._TEST_LOAD_MAX + 1, [2]):
            with self.assertRaises(ValueError):
                srv._test_load_iterations({'iterations': bad})

    def test_the_state_flags_only_repeated_runs(self):
        one = srv._test_load_state(1)
        self.assertFalse(one['enabled'])
        self.assertIsNone(one['iterations'])
        many = srv._test_load_state(100)
        self.assertTrue(many['enabled'])
        self.assertEqual(many['iterations'], 100)
        self.assertEqual(many['done'], 0)


class ScriptLoadTests(unittest.TestCase):
    def setUp(self):
        _reset_run()
        self._saved = copy.deepcopy(srv._TEST_RUN)
        srv._TEST_RUNNING = True

    def tearDown(self):
        srv._TEST_RUN.clear()
        srv._TEST_RUN.update(self._saved)
        srv._TEST_RUNNING = False

    def _run(self, statuses, iterations, stop_after=None):
        """Drive the load worker with `statuses` per iteration; returns the
        shared dicts passed to the runner and the run counter."""
        calls = []
        state = {'i': 0}

        def fake_run(server, script, preset, shared=None):
            i = state['i']
            state['i'] += 1
            calls.append(shared)
            status = statuses[i] if i < len(statuses) else 'ok'
            srv._TEST_RUN['steps'].append({'index': 0, 'status': status,
                                           'label': 'step %d' % (i + 1)})
            if status == 'error':
                srv._TEST_RUN['steps'][-1]['note'] = 'boom'
            if stop_after is not None and i + 1 >= stop_after:
                srv._TEST_RUN['stop'] = True
            return {}, status == 'stopped'

        with mock.patch.object(srv, '_test_run_script', fake_run), \
                mock.patch.object(srv, '_sim_resume_poll'):
            srv._test_load_script_worker(_FakeServer(),
                                         {'name': 'S', 'steps': []}, None,
                                         's1', iterations)
        return calls

    def test_successful_iterations_are_discarded(self):
        calls = self._run(['ok'] * 5, 5)
        load = srv._TEST_RUN['load']
        self.assertEqual(load['done'], 5)
        self.assertEqual(load['iteration'], 5)
        self.assertIsNone(load['failed_iteration'])
        self.assertEqual(srv._TEST_RUN['status'], 'ok')
        self.assertEqual(srv._TEST_RUN['steps'], [])      # no tracked results
        self.assertEqual(srv._TEST_RUN['log'], [])
        self.assertFalse(srv._TEST_RUN['running'])
        self.assertIsNotNone(load['elapsed_ms'])
        self.assertIsNotNone(load['avg_ms'])
        # the counter state is shared by every iteration
        self.assertEqual(len(calls), 5)
        for shared in calls[1:]:
            self.assertIs(shared, calls[0])

    def test_an_error_stops_the_loop_and_keeps_that_iteration(self):
        self._run(['ok', 'ok', 'error', 'error', 'error'], 5)
        load = srv._TEST_RUN['load']
        self.assertEqual(load['done'], 2)
        self.assertEqual(load['iteration'], 3)
        self.assertEqual(load['failed_iteration'], 3)
        self.assertEqual(srv._TEST_RUN['status'], 'error')
        self.assertEqual(srv._TEST_RUN['error'], 'iteration 3 failed')
        self.assertEqual(len(srv._TEST_RUN['steps']), 1)
        self.assertEqual(srv._TEST_RUN['steps'][0]['note'], 'boom')
        self.assertFalse(srv._TEST_RUN['running'])

    def test_a_warning_does_not_stop_the_loop(self):
        self._run(['warning', 'ok', 'warning'], 3)
        load = srv._TEST_RUN['load']
        self.assertEqual(load['done'], 3)
        self.assertIsNone(load['failed_iteration'])
        self.assertEqual(srv._TEST_RUN['status'], 'warning')

    def test_a_manual_stop_ends_the_loop(self):
        # the fake sets the stop flag after the second iteration
        self._run(['ok', 'ok', 'ok'], 50, stop_after=2)
        load = srv._TEST_RUN['load']
        self.assertEqual(load['done'], 2)
        self.assertEqual(srv._TEST_RUN['status'], 'stopped')
        self.assertFalse(srv._TEST_RUN['running'])

    def test_a_card_session_change_between_iterations_is_an_error(self):
        calls = []
        state = {'i': 0}

        def fake_run(server, script, preset, shared=None):
            state['i'] += 1
            calls.append(1)
            srv._TEST_RUN['steps'].append({'index': 0, 'status': 'ok'})
            server.card_session = 99          # the card was swapped
            return {}, False

        with mock.patch.object(srv, '_test_run_script', fake_run), \
                mock.patch.object(srv, '_sim_resume_poll'):
            srv._test_load_script_worker(_FakeServer(), {'name': 'S', 'steps': []},
                                         None, 's1', 10)
        load = srv._TEST_RUN['load']
        self.assertEqual(len(calls), 1)
        self.assertEqual(load['failed_iteration'], 2)
        self.assertEqual(srv._TEST_RUN['status'], 'error')
        self.assertIn('card session changed', srv._TEST_RUN['error'])


class LoadRegressionTests(unittest.TestCase):
    """The review findings: a raised error, warning aggregation, the average
    and the per-iteration clearing."""

    def setUp(self):
        _reset_run()
        self._saved = copy.deepcopy(srv._TEST_RUN)
        srv._TEST_RUNNING = True

    def tearDown(self):
        srv._TEST_RUN.clear()
        srv._TEST_RUN.update(self._saved)
        srv._TEST_RUNNING = False

    def test_a_raised_runner_error_finalises_the_run(self):
        # the PWA would poll a dead run forever and Clear would stay refused
        def boom(*a, **k):
            raise OSError('card vanished')
        with mock.patch.object(srv, '_test_run_script', boom), \
                mock.patch.object(srv, '_sim_resume_poll'):
            srv._test_load_script_worker(_FakeServer(), {'name': 'S', 'steps': []},
                                         None, 's1', 5)
        self.assertFalse(srv._TEST_RUN['running'])
        self.assertEqual(srv._TEST_RUN['status'], 'error')
        self.assertIn('card vanished', srv._TEST_RUN['error'])
        self.assertEqual(srv._TEST_RUN['load']['failed_iteration'], 1)
        self.assertIsNotNone(srv._TEST_RUN['load']['finished'])

    def test_a_warning_stays_in_the_final_status(self):
        statuses = iter(['warning', 'ok', 'ok'])
        with mock.patch.object(srv, '_test_run_script',
                               lambda *a, **k: (srv._TEST_RUN['steps'].append(
                                   {'status': next(statuses)}), ({}, False))[1]), \
                mock.patch.object(srv, '_sim_resume_poll'):
            srv._test_load_script_worker(_FakeServer(), {'name': 'S', 'steps': []},
                                         None, 's1', 3)
        self.assertEqual(srv._TEST_RUN['status'], 'warning')
        self.assertEqual(srv._TEST_RUN['load']['done'], 3)

    def test_the_average_covers_the_failing_iteration(self):
        statuses = iter(['ok', 'ok', 'error'])
        def fake(*a, **k):
            st = next(statuses)
            srv._TEST_RUN['steps'].append({'status': st})
            return {}, False
        with mock.patch.object(srv, '_test_run_script', fake), \
                mock.patch.object(srv, '_sim_resume_poll'):
            srv._test_load_script_worker(_FakeServer(), {'name': 'S', 'steps': []},
                                         None, 's1', 10)
        load = srv._TEST_RUN['load']
        self.assertEqual(load['done'], 2)
        self.assertIsNotNone(load['avg_ms'])
        self.assertLessEqual(load['avg_ms'], load['elapsed_ms'])

    def test_the_steps_are_cleared_before_each_iteration(self):
        seen = []
        def fake(*a, **k):
            seen.append(len(srv._TEST_RUN['steps']))
            srv._TEST_RUN['steps'].append({'status': 'ok'})
            return {}, False
        with mock.patch.object(srv, '_test_run_script', fake), \
                mock.patch.object(srv, '_sim_resume_poll'):
            srv._test_load_script_worker(_FakeServer(), {'name': 'S', 'steps': []},
                                         None, 's1', 4)
        self.assertEqual(seen, [0, 0, 0, 0], 'every iteration starts empty')


class SuiteLoadTests(unittest.TestCase):
    def setUp(self):
        _reset_run()
        self._saved = copy.deepcopy(srv._TEST_RUN)
        srv._TEST_RUNNING = True
        self.suite = {'id': 'suite1', 'name': 'S', 'require_adm': False,
                      'start_reset': False, 'scripts': []}
        self.members = [({'script_id': 'a', 'role': 'member', 'on_fail': 'stop'},
                         {'name': 'A', 'steps': []})]

    def tearDown(self):
        srv._TEST_RUN.clear()
        srv._TEST_RUN.update(self._saved)
        srv._TEST_RUNNING = False

    def _run(self, statuses, iterations):
        calls = []
        counter = {'i': 0}

        def fake_once(server, suite, members, preset, preset_id, state, shared=None):
            i = counter['i']
            counter['i'] += 1
            calls.append(shared)
            state['members'][0]['status'] = 'ok'
            state['summary'] = {'ok': 1, 'warning': 0, 'error': 0, 'skipped': 0,
                                'stopped': False, 'wall_ms': 1}
            return statuses[i] if i < len(statuses) else 'ok', False, False

        with mock.patch.object(srv, '_test_suite_execute_once', fake_once), \
                mock.patch.object(srv, '_sim_resume_poll'):
            srv._test_load_suite_worker(_FakeServer(), self.suite, self.members,
                                        None, None, iterations)
        return calls

    def test_successful_iterations_are_discarded(self):
        calls = self._run(['ok'] * 4, 4)
        load = srv._TEST_RUN['load']
        self.assertEqual(load['done'], 4)
        self.assertIsNone(srv._TEST_RUN['suite'])          # no tracked reports
        self.assertEqual(srv._TEST_RUN['steps'], [])
        self.assertEqual(srv._TEST_RUN['status'], 'ok')
        self.assertEqual(len(calls), 4)
        self.assertIs(calls[0], calls[-1])                 # one counter state

    def test_an_error_keeps_the_failing_iteration_report(self):
        self._run(['ok', 'error', 'error'], 3)
        load = srv._TEST_RUN['load']
        self.assertEqual(load['done'], 1)
        self.assertEqual(load['failed_iteration'], 2)
        self.assertEqual(srv._TEST_RUN['status'], 'error')
        self.assertIsNotNone(srv._TEST_RUN['suite'])
        self.assertEqual(srv._TEST_RUN['suite']['name'], 'S')

    def test_a_session_change_between_suite_iterations_is_an_error(self):
        def fake_once(server, suite, members, preset, preset_id, state, shared=None):
            return 'ok', False, True
        with mock.patch.object(srv, '_test_suite_execute_once', fake_once), \
                mock.patch.object(srv, '_sim_resume_poll'):
            srv._test_load_suite_worker(_FakeServer(), self.suite, self.members,
                                        None, None, 5)
        self.assertEqual(srv._TEST_RUN['load']['failed_iteration'], 1)
        self.assertEqual(srv._TEST_RUN['status'], 'error')
        self.assertIn('card session changed', srv._TEST_RUN['error'])
        self.assertIsNotNone(srv._TEST_RUN['suite'])

    def test_a_stopped_member_ends_the_suite_loop(self):
        def fake_once(server, suite, members, preset, preset_id, state, shared=None):
            return 'ok', True, False
        with mock.patch.object(srv, '_test_suite_execute_once', fake_once), \
                mock.patch.object(srv, '_sim_resume_poll'):
            srv._test_load_suite_worker(_FakeServer(), self.suite, self.members,
                                        None, None, 5)
        self.assertEqual(srv._TEST_RUN['status'], 'stopped')
        self.assertEqual(srv._TEST_RUN['load']['done'], 0)

    def test_a_stopped_iteration_ends_the_loop(self):
        self._run(['stopped'], 5)
        self.assertEqual(srv._TEST_RUN['status'], 'stopped')
        self.assertEqual(srv._TEST_RUN['load']['done'], 0)


if __name__ == '__main__':
    unittest.main()
