"""The test-runner stuck-card watchdog (v3.37.0).

pyscard's SCardTransmit is blocking and has no timeout: a card that stops
answering (no SW, no driver error) leaves the runner inside a step holding
_CARD_LOCK, so Stop cannot take effect and a load test hangs in that iteration.
The watchdog notices the missing runner progress, fails the run, and
disconnects the PC/SC link - the pending transmit then raises and the normal
step-failure path finalises the run.  The transport is marked stale so the next
equip/operation rebuilds it.
"""

import copy
import threading
import time
import unittest
from unittest import mock

from pysim_simple_server import server as srv
from pysim_simple_server import testscript as T


def _warning_script():
    return T.normalise_script({'name': 'stuck', 'steps': [
        {'type': 'action', 'kind': 'status', 'params': {'attempts': 1},
         'check': {'sw': '9000'}, 'on_fail': 'warning'}]},
        srv._test_command_type)


class _RaisingScc:
    """send_apdu raises like a disconnected PC/SC handle after the abort."""
    cat_cla = '80'

    def __init__(self):
        self._tp = self

    def send_apdu(self, apdu):
        raise RuntimeError('SCARD_E_INVALID_HANDLE')


class _FakeServer:
    def __init__(self, scc=None, session=7):
        self.scc = scc
        self.card_session = session
        self.card_present = True
        self.sms_oa = '12345'
        self.sms_sc = '12345678912'
        self.stk_pending = None
        self.menu_active = False


class _FakeCon:
    def __init__(self, fail=False):
        self.calls = 0
        self.fail = fail
        self.done = threading.Event()

    def disconnect(self):
        self.calls += 1
        try:
            if self.fail:
                raise RuntimeError('dead handle')
        finally:
            self.done.set()


class _FakeScc:
    def __init__(self, con):
        self._tp = type('T', (), {'_con': con})()


class _WatchdogTestCase(unittest.TestCase):
    def setUp(self):
        self.saved_run = copy.deepcopy(srv._TEST_RUN)
        self.saved_ref = srv._server_ref
        self.saved_stale = srv._TRANSPORT_STALE
        self.saved_aborted = srv._TEST_WATCHDOG_ABORTED
        srv._TEST_WATCHDOG_ABORTED = None
        srv._TRANSPORT_STALE = False

    def tearDown(self):
        srv._TEST_RUN.clear()
        srv._TEST_RUN.update(self.saved_run)
        srv._server_ref = self.saved_ref
        srv._TRANSPORT_STALE = self.saved_stale
        srv._TEST_WATCHDOG_ABORTED = self.saved_aborted

    def _run(self, **fields):
        srv._TEST_RUN.update({
            'running': True, 'stop': False, 'status': None, 'error': None,
            'stuck': False, 'heartbeat': None, 'steps': [], 'index': 0,
        })
        srv._TEST_RUN.update(fields)

    @staticmethod
    def _step(started, index=0, status='running'):
        return {'index': index, 'status': status, 'started': started}


class WatchdogCheckTests(_WatchdogTestCase):
    def test_an_idle_runner_is_not_watched(self):
        now = time.time()
        self._run(running=False, heartbeat=now - 100,
                  steps=[self._step(now - 100)])
        with mock.patch.object(srv, '_abort_card_transport') as abort:
            self.assertIsNone(srv._test_watchdog_check(now))
        abort.assert_not_called()

    def test_a_step_within_the_timeout_is_ignored(self):
        now = time.time()
        started = now - srv._TEST_STEP_TIMEOUT + 5
        self._run(heartbeat=started, steps=[self._step(started)])
        with mock.patch.object(srv, '_abort_card_transport') as abort:
            self.assertIsNone(srv._test_watchdog_check(now))
        abort.assert_not_called()

    def test_finished_work_with_fresh_progress_is_ignored(self):
        now = time.time()
        self._run(heartbeat=now - 1,
                  steps=[self._step(now - 100, status='ok')])
        with mock.patch.object(srv, '_abort_card_transport') as abort:
            self.assertIsNone(srv._test_watchdog_check(now))
        abort.assert_not_called()

    def test_a_stale_heartbeat_between_steps_still_fires(self):
        # the bounded drains run with no step entry "running": the heartbeat
        # is the authority, so a hang there is caught too
        now = time.time()
        self._run(heartbeat=now - 100,
                  steps=[self._step(now - 100, index=4, status='ok')])
        with mock.patch.object(srv, '_abort_card_transport') as abort:
            msg = srv._test_watchdog_check(now)
        self.assertIn('step 5', msg)
        abort.assert_called_once()

    def test_a_stuck_step_fails_the_run_and_aborts_the_link(self):
        now = time.time()
        started = now - srv._TEST_STEP_TIMEOUT - 2
        self._run(heartbeat=started, steps=[self._step(started)])
        with mock.patch.object(srv, '_abort_card_transport') as abort:
            msg = srv._test_watchdog_check(now)
        self.assertIn('the card stopped responding', msg)
        self.assertIn('step 1', msg)
        self.assertIn('no answer for', msg)
        self.assertTrue(srv._TEST_RUN['stuck'])
        self.assertTrue(srv._TEST_RUN['stop'])
        self.assertEqual(srv._TEST_RUN['error'], msg)
        abort.assert_called_once()

    def test_the_abort_happens_once_per_heartbeat(self):
        now = time.time()
        started = now - 100
        self._run(heartbeat=started, steps=[self._step(started)])
        with mock.patch.object(srv, '_abort_card_transport') as abort:
            self.assertIsNotNone(srv._test_watchdog_check(now))
            self.assertIsNone(srv._test_watchdog_check(now + 1))
            self.assertIsNone(srv._test_watchdog_check(now + 2))
        abort.assert_called_once()

    def test_new_progress_rearms_the_watchdog(self):
        now = time.time()
        started = now - 100
        self._run(heartbeat=started, steps=[self._step(started)])
        with mock.patch.object(srv, '_abort_card_transport') as abort:
            self.assertIsNotNone(srv._test_watchdog_check(now))
            # the runner made progress (a new beat) and then hung again
            srv._TEST_RUN['steps'].append(self._step(started + 1, index=1))
            srv._TEST_RUN['heartbeat'] = started + 1
            self.assertIsNotNone(srv._test_watchdog_check(now))
        self.assertEqual(abort.call_count, 2)

    def test_a_heartbeat_keeps_a_long_poll_loop_alive(self):
        # a bounded STATUS/drain loop may legitimately run longer than one
        # exchange: its per-attempt heartbeat must keep the watchdog quiet
        now = time.time()
        self._run(heartbeat=now - 1, steps=[self._step(now - 120)])
        with mock.patch.object(srv, '_abort_card_transport') as abort:
            self.assertIsNone(srv._test_watchdog_check(now))
        abort.assert_not_called()


class TransportAbortTests(_WatchdogTestCase):
    def _wait_stale(self):
        for _ in range(200):
            if srv._TRANSPORT_STALE:
                return True
            time.sleep(0.01)
        return False

    def test_the_connection_is_disconnected_and_marked_stale(self):
        # the disconnect is attempted on a background thread (a synchronous
        # one would block behind the pending transmit on pcsc-lite)
        con = _FakeCon()
        srv._server_ref = _FakeServer(_FakeScc(con))
        self.assertTrue(srv._abort_card_transport())
        self.assertTrue(con.done.wait(2))
        self.assertEqual(con.calls, 1)
        self.assertTrue(self._wait_stale())

    def test_a_missing_connection_is_not_fatal(self):
        srv._server_ref = _FakeServer(None)
        self.assertFalse(srv._abort_card_transport())
        self.assertTrue(srv._TRANSPORT_STALE)

    def test_a_failing_disconnect_is_swallowed(self):
        con = _FakeCon(fail=True)
        srv._server_ref = _FakeServer(_FakeScc(con))
        self.assertTrue(srv._abort_card_transport())
        self.assertTrue(con.done.wait(2))
        self.assertEqual(con.calls, 1)
        self.assertTrue(self._wait_stale())


class HeartbeatTests(_WatchdogTestCase):
    def test_a_finished_step_beats(self):
        # between steps the heartbeat must stay fresh (the drains run with no
        # step entry "running")
        self._run(heartbeat=0)
        entry = {'index': 0, 'started': time.time() - 5}
        before = time.time()
        srv._test_finish_entry(entry, {'status': 'ok'})
        self.assertGreaterEqual(srv._TEST_RUN['heartbeat'], before)


class StuckStatusTests(_WatchdogTestCase):
    def test_a_stuck_member_is_an_error_not_a_stop(self):
        self.assertEqual(
            srv._test_member_status([{'status': 'warning'}], True, stuck=True),
            'error')
        self.assertEqual(
            srv._test_member_status([{'status': 'warning'}], True),
            'stopped')

    def test_a_stuck_run_finalises_as_error(self):
        script = _warning_script()
        self._run(stuck=True, heartbeat=time.time(), stop=True,
                  total=1, name='stuck')
        srv._test_run_execute(_FakeServer(_RaisingScc()), script, {})
        self.assertEqual(srv._TEST_RUN['status'], 'error')
        self.assertFalse(srv._TEST_RUN['running'])

    def test_the_control_warning_run_without_the_stuck_flag(self):
        # the same raising warning step is only a warning when the watchdog
        # did not mark the run: the stuck flag is what changes the verdict
        script = _warning_script()
        self._run(stuck=False, heartbeat=time.time(), total=1, name='stuck')
        srv._test_run_execute(_FakeServer(_RaisingScc()), script, {})
        self.assertEqual(srv._TEST_RUN['status'], 'warning')

    def test_a_blocked_runner_step_ends_the_run_as_error(self):
        # end to end: a step blocked inside send_apdu is flagged by the
        # watchdog; when the call returns (the driver's timeout / the abort)
        # the ordinary failure path finalises the run as an error even though
        # the step was a warning one
        entered = threading.Event()
        release = threading.Event()

        class _BlockingScc:
            cat_cla = '80'

            def __init__(self):
                self._tp = self

            def send_apdu(self, apdu):
                entered.set()
                release.wait(5)
                raise RuntimeError('SCARD_E_NOT_TRANSACTED')

        script = _warning_script()
        self._run(total=1, name='stuck')
        saved_timeout = srv._TEST_STEP_TIMEOUT
        srv._TEST_STEP_TIMEOUT = 0.2
        try:
            runner = threading.Thread(
                target=srv._test_run_execute,
                args=(_FakeServer(_BlockingScc()), script, {}))
            runner.start()
            self.assertTrue(entered.wait(2))
            time.sleep(0.3)
            with mock.patch.object(srv, '_abort_card_transport') as abort:
                msg = srv._test_watchdog_check()
            self.assertIn('stopped responding', msg)
            abort.assert_called_once()
            release.set()
            runner.join(5)
        finally:
            srv._TEST_STEP_TIMEOUT = saved_timeout
        self.assertFalse(runner.is_alive())
        self.assertTrue(srv._TEST_RUN['stuck'])
        self.assertEqual(srv._TEST_RUN['status'], 'error')
        self.assertFalse(srv._TEST_RUN['running'])

    def test_a_stuck_load_iteration_is_a_failure(self):
        def fake_run(server, script, preset, shared=None):
            srv._TEST_RUN['steps'].append(
                {'index': 0, 'status': 'warning', 'label': 's'})
            srv._TEST_RUN['stuck'] = True
            srv._TEST_RUN['error'] = ('the card stopped responding '
                                      '(step 1, no answer for 31 s)')
            return {}, True

        self._run(load=srv._test_load_state(3), name='load')
        srv._TEST_RUN['load'].update({'iteration': 1})
        with mock.patch.object(srv, '_test_run_script', fake_run), \
                mock.patch.object(srv, '_sim_resume_poll'):
            srv._test_load_script_worker(_FakeServer(), {'name': 'S',
                                                         'steps': []}, None,
                                         's1', 3)
        load = srv._TEST_RUN['load']
        self.assertEqual(srv._TEST_RUN['status'], 'error')
        self.assertEqual(load['failed_iteration'], 1)
        self.assertEqual(load['done'], 0)
        self.assertIn('stopped responding', srv._TEST_RUN['error'])
        self.assertFalse(srv._TEST_RUN['running'])


if __name__ == '__main__':
    unittest.main()
