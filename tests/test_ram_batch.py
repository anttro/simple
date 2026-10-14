#!/usr/bin/env python3
"""Batch CAP install tests (v3.28.0).

The batch worker applies one install plan to every card that gets equipped,
resolving the keys from each card's own preset (matched by ICCID).  A card is
attempted once per run - re-equipping it is reported, never re-installed - a
failure never stops the batch, and the stop flag finishes a running install
first.
"""

import json
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

PROJECTS = Path(__file__).resolve().parents[2]
PY_SIM = PROJECTS / 'pysim'
if str(PY_SIM) not in sys.path:
    sys.path.insert(0, str(PY_SIM))

from pysim_simple_server.__main__ import _build_http_server
from pysim_simple_server import presets
from pysim_simple_server import server as srv
from pysim_simple_server.server import PysimHandler, _CARD_LOCK


def _plan():
    return {'seq': ['80E60200', '80E80000', '80E60000'], 'loadfile_aid': 'F0414C46416101',
            'module_aid': 'F0414C4641610101', 'block_size': 240, 'block_size_req': None,
            'spi2': '01', 'include_cpi': True, 'ram_format': 'compact'}


class _FakeStore:
    def __init__(self, entries):
        self.entries = entries

    def find_by_iccid(self, iccid):
        want = presets.normalize_iccid(iccid)
        for p in self.entries:
            if presets.normalize_iccid(p.get('iccid')) == want:
                return p
        return None


def _preset(iccid, name='Card', keyset=1):
    byte = '%02X' % ((keyset << 4) | 0x05)
    ks = {'kic': byte, 'kid': byte, 'kicKey': '00' * 16, 'kidKey': '11' * 16,
          'cntr': '0000000001'}
    tar = {'role': 'isd', 'tar': '000000', 'msl': '16', 'desc': '', 'keyset': keyset}
    return {'id': 'p-' + iccid, 'name': name, 'iccid': iccid, 'keysets': [ks],
            'tars': [tar]}


def _fake_server(iccid, store, session=1, ready=None):
    """A server-shaped object carrying only the attributes the real server
    object has.  The connection state is the module global `_CARD_CONNECTED`
    (v3.28.0 audit: a fake `connected` attribute here hid a bug - the worker
    read it and never saw a card); `card_ready_session` is the marker
    _apply_equipped_card sets when the session is fully initialized (it
    defaults to the session, i.e. a finished equip)."""
    return SimpleNamespace(iccid=iccid, card_presets=store,
                           card_session=session,
                           card_ready_session=session if ready is None else ready,
                           scc=object())


class BatchStateTests(unittest.TestCase):
    def setUp(self):
        srv._RAM_BATCH = None

    def tearDown(self):
        srv._RAM_BATCH = None

    def test_the_report_counts_and_caps_the_attempts(self):
        st = srv._ram_batch_new_state(_plan())
        srv._ram_batch_record(st, {'iccid': '8970119000004600098', 'result': 'installed'})
        srv._ram_batch_record(st, {'iccid': '8970119000004600106', 'result': 'failed',
                                   'failed_step': 'LOAD (3/7)', 'por_sw': '6A80'})
        srv._ram_batch_record(st, {'iccid': '8970119000004600114', 'result': 'skipped',
                                   'reason': 'no preset matches this ICCID'})
        self.assertEqual(st['counts'], {'installed': 1, 'failed': 1, 'skipped': 1})
        self.assertEqual(st['attempts_total'], 3)
        # the notice names the last attempt's outcome
        self.assertIn('skipped: no preset matches this ICCID', st['notice'])
        srv._ram_batch_record(st, {'iccid': '8970119000004600122', 'result': 'failed',
                                   'failed_step': 'LOAD (3/7)', 'por_sw': '6A80'})
        self.assertIn('failed at LOAD (3/7) (6A80)', st['notice'])
        # the report keeps the last _RAM_BATCH_ATTEMPTS_MAX entries
        for _ in range(srv._RAM_BATCH_ATTEMPTS_MAX + 10):
            srv._ram_batch_record(st, {'result': 'skipped', 'reason': 'x'})
        self.assertEqual(len(st['attempts']), srv._RAM_BATCH_ATTEMPTS_MAX)
        self.assertEqual(st['attempts_total'], 4 + srv._RAM_BATCH_ATTEMPTS_MAX + 10)

    def test_the_failure_fields_name_the_failing_step(self):
        result = {'failed_step': 2, 'steps': [
            {'name': 'INSTALL [for load]', 'sw': '9000', 'por_status': 'por_ok'},
            {'name': 'LOAD (1/2)', 'sw': '9000', 'por_status': 'por_error',
             'por_sw': '6A80', 'por_error': 'remote SW 6A80'},
        ]}
        fields = srv._ram_batch_failure_fields(result)
        self.assertEqual(fields['failed_step'], 'LOAD (1/2)')
        self.assertEqual(fields['por_sw'], '6A80')
        self.assertEqual(fields['por_error'], 'remote SW 6A80')
        # a result without steps stays quiet
        self.assertEqual(srv._ram_batch_failure_fields({})['failed_step'], '')


class BatchCardTests(unittest.TestCase):
    """The per-card decision and one run (the install chain itself is mocked)."""

    def setUp(self):
        srv._RAM_BATCH = None

    def tearDown(self):
        srv._RAM_BATCH = None

    def test_a_card_without_a_preset_is_skipped(self):
        st = srv._ram_batch_new_state(_plan())
        srv._ram_batch_handle_card(st, _fake_server('8970119000004600098', _FakeStore([])))
        self.assertEqual(st['attempts'][0]['result'], 'skipped')
        self.assertIn('no preset', st['attempts'][0]['reason'])
        self.assertEqual(st['seen']['8970119000004600098'], 'no preset')

    def test_a_card_without_a_readable_iccid_is_skipped(self):
        st = srv._ram_batch_new_state(_plan())
        srv._ram_batch_handle_card(st, _fake_server(None, _FakeStore([])))
        self.assertIn('no ICCID', st['attempts'][0]['reason'])

    def test_the_preset_is_matched_by_iccid_and_the_install_runs(self):
        st = srv._ram_batch_new_state(_plan())
        preset = _preset('8970119000004600098', 'Alfa')
        calls = []

        def fake_run(server, body, plan, scc):
            calls.append(body)
            return {'success': True, 'final_cntr': '0000000002',
                    'load_file_aid': 'F0414C46416101', 'module_aid': 'F0414C4641610101'}

        with mock.patch.object(srv, '_ram_install_run', fake_run):
            srv._ram_batch_handle_card(st, _fake_server('8970119000004600098',
                                                        _FakeStore([preset])))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['preset_id'], 'p-8970119000004600098')
        self.assertEqual(calls[0]['tar'], '000000')       # the ISD role TAR
        rec = st['attempts'][0]
        self.assertEqual(rec['result'], 'installed')
        self.assertEqual(rec['preset'], 'Alfa')
        self.assertEqual(st['counts']['installed'], 1)

    def test_a_failed_install_is_recorded_with_the_step_and_sw(self):
        st = srv._ram_batch_new_state(_plan())
        preset = _preset('8970119000004600098')

        def fake_run(server, body, plan, scc):
            return {'success': False, 'error': 'remote SW 6A80', 'failed_step': 2,
                    'steps': [{'name': 'INSTALL [for load]', 'sw': '9000'},
                              {'name': 'LOAD (1/1)', 'sw': '9000', 'por_sw': '6A80'}],
                    'final_cntr': '0000000002'}

        with mock.patch.object(srv, '_ram_install_run', fake_run):
            srv._ram_batch_handle_card(st, _fake_server('8970119000004600098',
                                                        _FakeStore([preset])))
        rec = st['attempts'][0]
        self.assertEqual(rec['result'], 'failed')
        self.assertEqual(rec['failed_step'], 'LOAD (1/1)')
        self.assertEqual(rec['por_sw'], '6A80')
        self.assertEqual(st['counts']['failed'], 1)

    def test_a_raised_error_is_a_skip_and_the_card_stays_seen(self):
        st = srv._ram_batch_new_state(_plan())
        preset = _preset('8970119000004600098')

        def fake_run(*a, **k):
            raise ValueError('TAR 000000 needs a default keyset (its MSL 16 '
                             'requires security)')

        with mock.patch.object(srv, '_ram_install_run', fake_run):
            srv._ram_batch_handle_card(st, _fake_server('8970119000004600098',
                                                        _FakeStore([preset])))
        rec = st['attempts'][0]
        self.assertEqual(rec['result'], 'skipped')
        self.assertIn('needs a default keyset', rec['reason'])
        self.assertEqual(st['seen']['8970119000004600098'], 'skipped')

    def test_a_transport_error_mid_chain_is_a_failure(self):
        st = srv._ram_batch_new_state(_plan())
        preset = _preset('8970119000004600098')

        def fake_run(*a, **k):
            raise OSError('card removed')

        with mock.patch.object(srv, '_ram_install_run', fake_run):
            srv._ram_batch_handle_card(st, _fake_server('8970119000004600098',
                                                        _FakeStore([preset])))
        rec = st['attempts'][0]
        # the chain started and died: a failure, not a skip
        self.assertEqual(rec['result'], 'failed')
        self.assertIn('card removed', rec['error'])
        self.assertEqual(st['counts']['failed'], 1)
        self.assertEqual(st['seen']['8970119000004600098'], 'failed')

    def test_a_new_install_clears_the_previous_notice(self):
        """The notice reports the last *finished* card: starting the next
        card's install must clear it (v3.28.1)."""
        st = srv._ram_batch_new_state(_plan())
        srv._ram_batch_record(st, {'iccid': '8970119000004600000', 'result': 'installed'})
        self.assertIn('installed', st['notice'])
        preset = _preset('8970119000004600098', 'Alfa')
        seen = {}

        def fake_run(server, body, plan, scc):
            seen['notice'] = st['notice']      # what the panel would render
            seen['current'] = st['current']
            return {'success': True}

        with mock.patch.object(srv, '_ram_install_run', fake_run):
            srv._ram_batch_handle_card(st, _fake_server('8970119000004600098',
                                                        _FakeStore([preset])))
        self.assertEqual(seen['notice'], '')               # cleared while installing
        self.assertEqual(seen['current']['iccid'], '8970119000004600098')
        self.assertIn('installed', st['notice'])           # ...and set for the new outcome

    def test_a_card_already_processed_is_reported_not_reinstalled(self):
        st = srv._ram_batch_new_state(_plan())
        preset = _preset('8970119000004600098', 'Alfa')
        calls = []
        with mock.patch.object(srv, '_ram_install_run',
                               lambda *a, **k: calls.append(1) or {'success': True}):
            srv._ram_batch_handle_card(st, _fake_server('8970119000004600098',
                                                        _FakeStore([preset]), session=1))
            srv._ram_batch_handle_card(st, _fake_server('8970119000004600098',
                                                        _FakeStore([preset]), session=2))
        self.assertEqual(len(calls), 1)                   # the second card never installs
        self.assertEqual(st['attempts'][1]['result'], 'skipped')
        self.assertIn('already processed in this batch (installed)',
                      st['attempts'][1]['reason'])
        self.assertEqual(st['attempts_total'], 2)

    def test_the_worker_waits_for_a_card_and_installs(self):
        """The loop reacts to a new card session, records the attempt and ends
        on the stop flag."""
        st = srv._ram_batch_new_state(_plan())
        preset = _preset('8970119000004600098', 'Alfa')
        srv._RAM_BATCH = st
        srv._server_ref = _fake_server('8970119000004600098', _FakeStore([preset]),
                                       session=7)
        calls = []
        with mock.patch.object(srv, '_CARD_CONNECTED', True), \
                mock.patch.object(srv, '_ram_install_run',
                                  lambda *a, **k: calls.append(1) or {'success': True}):
            t = threading.Thread(target=srv._ram_batch_worker, args=(st,), daemon=True)
            t.start()
            for _ in range(60):
                if st['attempts']:
                    break
                time.sleep(0.05)
            self.assertTrue(srv._ram_batch_stop())
            t.join(timeout=3)
        srv._server_ref = None
        self.assertEqual(len(calls), 1)
        self.assertEqual(st['attempts'][0]['result'], 'installed')
        self.assertFalse(st['active'])
        self.assertEqual(st['phase'], 'stopped')
        # the summary /api/status carries
        self.assertEqual(srv._ram_batch_summary()['counts']['installed'], 1)
        self.assertFalse(srv._ram_batch_summary()['active'])


class RamInstallPlanTests(unittest.TestCase):
    """The shared card-independent plan (v3.28.0): the block-size/hex checks
    and the transport knobs the single install and the batch both use."""

    def test_the_plan_validates_the_input(self):
        with mock.patch.object(srv, '_cap_parse', lambda h: ('A1', 'A2', b'\x01')), \
                mock.patch.object(srv, '_cap_apdu_sequence', lambda *a, **k: ['80E60200']):
            plan = srv._ram_install_plan({'cap_hex': 'AA', 'load_block_size': '120'})
            self.assertEqual(plan['block_size'], 120)
            self.assertEqual(plan['spi2'], '01')
            self.assertEqual(plan['ram_format'], 'auto')
            self.assertEqual(plan['seq'], ['80E60200'])
            with self.assertRaises(ValueError):
                srv._ram_install_plan({'cap_hex': 'AA', 'load_block_size': 300})
            with self.assertRaises(ValueError):
                srv._ram_install_plan({'cap_hex': 'AA', 'load_block_size': 'x'})
            with self.assertRaises(ValueError):
                srv._ram_install_plan({'cap_hex': ''})


class BatchWorkerSourceTests(unittest.TestCase):
    def test_the_worker_waits_for_a_finished_equip(self):
        """_CARD_CONNECTED flips True (and the session bumps) before the ICCID
        is read: acting mid-equip would consume the card with a bogus 'no
        ICCID' skip.  The worker must wait for the ready marker (v3.28.0
        audit)."""
        st = srv._ram_batch_new_state(_plan())
        preset = _preset('8970119000004600098', 'Alfa')
        srv._RAM_BATCH = st
        # the session is bumped but the equip has not finished yet
        srv._server_ref = _fake_server('8970119000004600098', _FakeStore([preset]),
                                       session=9, ready=8)
        calls = []
        with mock.patch.object(srv, '_CARD_CONNECTED', True), \
                mock.patch.object(srv, '_ram_install_run',
                                  lambda *a, **k: calls.append(1) or {'success': True}):
            t = threading.Thread(target=srv._ram_batch_worker, args=(st,), daemon=True)
            t.start()
            time.sleep(0.8)                     # a couple of polls mid-equip
            self.assertEqual(calls, [])
            self.assertEqual(st['attempts'], [])
            srv._server_ref.card_ready_session = 9   # the equip finished
            for _ in range(60):
                if st['attempts']:
                    break
                time.sleep(0.05)
            srv._ram_batch_stop()
            t.join(timeout=3)
        srv._server_ref = None
        self.assertEqual(len(calls), 1)
        self.assertEqual(st['attempts'][0]['result'], 'installed')

    def test_the_ready_marker_is_set_on_every_equip_path(self):
        """_apply_equipped_card marks the finished session, and the startup
        init (which never runs it) sets the same marker (v3.28.0 audit)."""
        import inspect
        src = inspect.getsource(srv._apply_equipped_card)
        self.assertIn('server.card_ready_session = server.card_session', src)
        main = (PROJECTS / 'simple' / 'pysim_simple_server' / '__main__.py').read_text(
            encoding='utf-8')
        self.assertIn('server.card_ready_session = server.card_session', main)

    def test_the_worker_reads_the_module_connection_state(self):
        """The server object has no `connected` attribute - the worker must
        read `_CARD_CONNECTED`, the global the rest of the server uses
        (v3.28.0 review)."""
        import inspect
        src = inspect.getsource(srv._ram_batch_worker)
        self.assertIn('_CARD_CONNECTED', src)
        self.assertNotIn("'connected'", src)


class BatchLifecycleTests(unittest.TestCase):
    def setUp(self):
        srv._RAM_BATCH = None

    def tearDown(self):
        srv._RAM_BATCH = None

    def test_start_refuses_a_second_batch_and_a_test_run(self):
        with mock.patch.object(srv, '_ram_install_plan', lambda body: _plan()), \
                mock.patch.object(srv, '_ram_batch_worker', lambda state: None):
            st = srv._ram_batch_start({'cap_hex': 'AA'})
            self.assertTrue(st['active'])
            with self.assertRaises(RuntimeError):
                srv._ram_batch_start({'cap_hex': 'AA'})
            srv._ram_batch_stop()
        # a test run blocks the start
        srv._RAM_BATCH = None
        with mock.patch.object(srv, '_ram_install_plan', lambda body: _plan()), \
                mock.patch.object(srv, '_TEST_RUNNING', True):
            with self.assertRaises(RuntimeError):
                srv._ram_batch_start({'cap_hex': 'AA'})

    def test_the_status_payload_shape(self):
        idle = srv._ram_batch_status()
        self.assertFalse(idle['active'])
        self.assertEqual(idle['phase'], 'idle')
        self.assertEqual(idle['attempts'], [])
        self.assertIsNone(srv._ram_batch_summary())
        with mock.patch.object(srv, '_ram_install_plan', lambda body: _plan()), \
                mock.patch.object(srv, '_ram_batch_worker', lambda state: None):
            srv._ram_batch_start({'cap_hex': 'AA'})
        srv._ram_batch_record(srv._RAM_BATCH, {'iccid': '8970119000004600098',
                                               'result': 'installed'})
        st = srv._ram_batch_status()
        self.assertTrue(st['active'])
        self.assertEqual(st['phase'], 'waiting_card')
        self.assertEqual(len(st['attempts']), 1)
        self.assertEqual(st['counts']['installed'], 1)
        self.assertEqual(srv._ram_batch_summary()['counts']['installed'], 1)
        # a stop request flips the phase; clear is refused while active
        self.assertTrue(srv._ram_batch_stop())
        self.assertEqual(srv._ram_batch_status()['phase'], 'stopping')
        self.assertFalse(srv._ram_batch_clear())
        srv._RAM_BATCH['active'] = False
        self.assertTrue(srv._ram_batch_clear())
        self.assertIsNone(srv._RAM_BATCH)


class BatchHttpTests(unittest.TestCase):
    """The endpoints: start needs no card, status is card-free (the panel polls
    it during an install), stop answers while the card lock is held."""

    def setUp(self):
        srv._RAM_BATCH = None
        self.server = _build_http_server('127.0.0.1', 0, PysimHandler)
        self.server.log_requests = False
        self.server.app = None
        self.server.sl = None
        self.server.scc = None
        self.server.card_presets = None
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        srv._RAM_BATCH = None

    def _post(self, path, body, timeout=5):
        req = urllib.request.Request(
            'http://127.0.0.1:%d%s' % (self.port, path),
            data=json.dumps(body).encode(),
            headers={'Content-Type': 'application/json'}, method='POST')
        try:
            with urllib.request.urlopen(req, timeout=timeout) as res:
                return res.status, json.loads(res.read() or b'{}')
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b'{}')

    def _get(self, path, timeout=5):
        try:
            with urllib.request.urlopen(
                    'http://127.0.0.1:%d%s' % (self.port, path), timeout=timeout) as res:
                return res.status, json.loads(res.read() or b'{}')
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b'{}')

    def test_the_batch_endpoints(self):
        with mock.patch.object(srv, '_ram_install_plan', lambda body: _plan()), \
                mock.patch.object(srv, '_ram_batch_worker', lambda state: None):
            status, resp = self._post('/api/ram-batch/start', {'cap_hex': 'AA'})
            self.assertEqual(status, 200, resp)
            self.assertTrue(resp['active'])
            self.assertEqual(resp['phase'], 'waiting_card')
            # a second start is refused
            status, resp = self._post('/api/ram-batch/start', {'cap_hex': 'AA'})
            self.assertEqual(status, 409, resp)
            # the status is card-free
            status, resp = self._get('/api/ram-batch/status')
            self.assertEqual(status, 200, resp)
            self.assertTrue(resp['active'])
            # clear is refused while active
            status, resp = self._post('/api/ram-batch/clear', {})
            self.assertEqual(status, 409, resp)
            # the stop answers and flips the phase
            status, resp = self._post('/api/ram-batch/stop', {})
            self.assertEqual(status, 200, resp)
            self.assertEqual(resp['phase'], 'stopping')
            # a repeated stop is idempotent (the panel disables the button)
            status, resp = self._post('/api/ram-batch/stop', {})
            self.assertEqual(status, 200, resp)
        srv._RAM_BATCH['active'] = False
        status, resp = self._post('/api/ram-batch/clear', {})
        self.assertEqual(status, 200, resp)
        # with no run left, a stop is a 409
        status, resp = self._post('/api/ram-batch/stop', {})
        self.assertEqual(status, 409, resp)
        status, resp = self._get('/api/ram-batch/status')
        self.assertEqual(status, 200, resp)
        self.assertFalse(resp['active'])
        self.assertEqual(resp['phase'], 'idle')

    def test_a_bad_start_is_a_400_and_a_test_run_blocks_it(self):
        def boom(body):
            raise ValueError('No cap_hex provided')

        with mock.patch.object(srv, '_ram_install_plan', boom):
            status, resp = self._post('/api/ram-batch/start', {})
            self.assertEqual(status, 400, resp)
            self.assertIn('cap_hex', resp['error'])
        # a malformed .cap (a zip parse error) is a 400 too, never a dropped
        # connection
        status, resp = self._post('/api/ram-batch/start', {'cap_hex': '00'})
        self.assertEqual(status, 400, resp)
        self.assertIn('zip', resp['error'].lower())
        with mock.patch.object(srv, '_ram_install_plan', lambda body: _plan()), \
                mock.patch.object(srv, '_TEST_RUNNING', True):
            status, resp = self._post('/api/ram-batch/start', {'cap_hex': 'AA'})
            self.assertEqual(status, 409, resp)

    def test_a_test_run_is_refused_while_a_batch_runs(self):
        with mock.patch.object(srv, '_ram_install_plan', lambda body: _plan()), \
                mock.patch.object(srv, '_ram_batch_worker', lambda state: None):
            status, resp = self._post('/api/ram-batch/start', {'cap_hex': 'AA'})
            self.assertEqual(status, 200, resp)
        status, resp = self._post('/api/test/run', {})
        self.assertEqual(status, 409, resp)
        self.assertIn('batch install', resp['error'])

    def test_stop_answers_while_the_card_lock_is_held(self):
        """The panel's Stop must be effective during an install (the worker
        holds _CARD_LOCK for the whole chain)."""
        with mock.patch.object(srv, '_ram_install_plan', lambda body: _plan()), \
                mock.patch.object(srv, '_ram_batch_worker', lambda state: None):
            status, resp = self._post('/api/ram-batch/start', {'cap_hex': 'AA'})
            self.assertEqual(status, 200, resp)
        with _CARD_LOCK:
            status, resp = self._post('/api/ram-batch/stop', {}, timeout=2)
            self.assertEqual(status, 200, resp)
            self.assertEqual(resp['phase'], 'stopping')
            status, resp = self._get('/api/ram-batch/status', timeout=2)
            self.assertEqual(status, 200, resp)


if __name__ == '__main__':
    unittest.main()
