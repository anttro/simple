#!/usr/bin/env python3
"""Tests for the test-script engine (pure parts) and the server-side runner."""

import sys
import time
import unittest
from pathlib import Path
from unittest import mock

PROJECTS = Path(__file__).resolve().parents[2]
PY_SIM = PROJECTS / 'pysim'
if str(PY_SIM) not in sys.path:
    sys.path.insert(0, str(PY_SIM))

import pysim_simple_server.server as S
import pysim_simple_server.testscript as T


# ─── pure engine ─────────────────────────────────────────────────────────

def _resolver(name):
    return {n.upper(): c for c, n in S.PROACTIVE_TYPE_NAMES.items()}.get(name.upper())


class TestValidation(unittest.TestCase):
    def test_minimal_script_normalises(self):
        script = T.normalise_script({'name': 'demo', 'steps': [
            {'type': 'action', 'kind': 'menu-select', 'params': {'item_id': 128}},
            {'type': 'expect', 'command': 'SELECT ITEM',
             'checks': [{'kind': 'item', 'id': 1, 'text': 'test'}],
             'respond': {'result': 'ok', 'item_id': 1}},
        ]}, _resolver)
        self.assertEqual(script['name'], 'demo')
        self.assertEqual(script['steps'][0]['params']['item_id'], 128)
        self.assertEqual(script['steps'][0]['check']['sw'], {'mode': 'exact', 'value': '9000'})
        self.assertEqual(script['steps'][1]['command']['type'], 0x24)
        self.assertEqual(script['steps'][1]['respond'], {'result': 0x00, 'item_id': 1})

    def test_status_poll_defaults_to_a_91xx_mask(self):
        script = T.normalise_script({'steps': [
            {'type': 'action', 'kind': 'status', 'params': {'attempts': 5}},
        ]}, _resolver)
        self.assertEqual(script['steps'][0]['check']['sw'], {'mode': 'mask', 'value': '91??'})
        script = T.normalise_script({'steps': [
            {'type': 'action', 'kind': 'status', 'params': {}},
        ]}, _resolver)
        self.assertEqual(script['steps'][0]['check']['sw'], {'mode': 'exact', 'value': '9000'})

    def test_rejects_bad_scripts(self):
        bad = [
            'not a dict',
            {'steps': []},
            {'steps': [{'type': 'nope'}]},
            {'steps': [{'type': 'action', 'kind': 'nope'}]},
            {'steps': [{'type': 'action', 'kind': 'apdu', 'params': {}}]},
            {'steps': [{'type': 'action', 'kind': 'menu-select', 'params': {'item_id': 0}}]},
            {'steps': [{'type': 'action', 'kind': 'apdu', 'params': {'apdu': 'ABC'}}]},
            {'steps': [{'type': 'expect', 'command': 'NOT A COMMAND'}]},
            {'steps': [{'type': 'expect', 'command': 'SELECT ITEM', 'respond': {'result': 'bogus'}}]},
            {'steps': [{'type': 'action', 'kind': 'apdu', 'params': {'apdu': '00'},
                        'check': {'por': 'ok'}}]},
            {'steps': [{'type': 'action', 'kind': 'status', 'params': {'attempts': 0}}]},
            {'steps': [{'type': 'expect', 'command': 'SELECT ITEM',
                        'checks': [{'kind': 'item'}]}]},
        ]
        for raw in bad:
            with self.assertRaises(T.ScriptError, msg=repr(raw)):
                T.normalise_script(raw if isinstance(raw, dict) else raw, _resolver)

    def test_check_strings_and_masks(self):
        script = T.normalise_script({'steps': [
            {'type': 'action', 'kind': 'apdu', 'params': {'apdu': '00A4'},
             'check': {'sw': '91??', 'data': {'mode': 'mask', 'value': 'aa??'}}},
        ]}, _resolver)
        check = script['steps'][0]['check']
        self.assertEqual(check['sw'], {'mode': 'mask', 'value': '91??'})
        self.assertEqual(check['data'], {'mode': 'mask', 'value': 'AA??'})

    def test_integer_fields_accept_decimals_with_leading_zeros_and_hex(self):
        script = T.normalise_script({'steps': [
            {'type': 'action', 'kind': 'status',
             'params': {'attempts': '08', 'interval_ms': '0x10'}},
        ]}, _resolver)
        self.assertEqual(script['steps'][0]['params']['attempts'], 8)
        self.assertEqual(script['steps'][0]['params']['interval_ms'], 16)
        with self.assertRaises(T.ScriptError):
            T.normalise_script({'steps': [
                {'type': 'action', 'kind': 'status', 'params': {'attempts': 'x'}}]}, _resolver)

    def test_respond_text_and_raw(self):
        respond = T.normalise_respond({'result': 'ok', 'text': 'hello', 'dcs': '00',
                                       'raw': 'aa01bb'})
        self.assertEqual(respond['text'], 'hello')
        self.assertEqual(respond['raw'], 'AA01BB')
        tr = T.build_tr(4, 0x23, 0x81, 0x82, respond)
        self.assertEqual(tr.hex().upper(),
                         '8103042300' + '82028182' + 'AA01BB' + '8D0600' + '68656C6C6F'
                         + '83020000')


class TestMatchers(unittest.TestCase):
    def test_match_value(self):
        self.assertTrue(T.match_value({'mode': 'exact', 'value': '9000'}, '9000'))
        self.assertFalse(T.match_value({'mode': 'exact', 'value': '9000'}, '9001'))
        self.assertTrue(T.match_value({'mode': 'mask', 'value': '91??'}, '9102'))
        self.assertFalse(T.match_value({'mode': 'mask', 'value': '91??'}, '9002'))
        self.assertFalse(T.match_value({'mode': 'exact', 'value': '9000'}, None))
        self.assertFalse(T.match_value({'mode': 'mask', 'value': '91??'}, '91'))
        self.assertTrue(T.match_value(None, 'anything'))

    def test_match_text(self):
        self.assertTrue(T.match_text({'mode': 'contains', 'value': 'ell'}, 'hello'))
        self.assertFalse(T.match_text({'mode': 'contains', 'value': 'ELL'}, 'hello'))
        self.assertTrue(T.match_text({'mode': 'contains', 'value': 'ELL', 'case_sensitive': False}, 'hello'))
        self.assertTrue(T.match_text({'mode': 'exact', 'value': 'hello'}, 'hello'))
        self.assertFalse(T.match_text({'mode': 'exact', 'value': 'hell'}, 'hello'))
        self.assertFalse(T.match_text({'mode': 'contains', 'value': 'x'}, None))

    def test_match_item(self):
        items = [{'id': 1, 'text': 'test'}, {'id': 2, 'text': 'other'}]
        self.assertTrue(T.match_item(items, {'id': 1, 'text': None, 'mode': 'contains'})[0])
        self.assertTrue(T.match_item(items, {'id': None, 'text': 'test', 'mode': 'contains'})[0])
        self.assertFalse(T.match_item(items, {'id': 3, 'text': None, 'mode': 'contains'})[0])
        self.assertFalse(T.match_item([], {'id': 1, 'text': None, 'mode': 'contains'})[0])

    def test_combine_levels(self):
        self.assertEqual(T.combine_levels(['ok', 'ok']), 'ok')
        self.assertEqual(T.combine_levels(['ok', 'warning']), 'warning')
        self.assertEqual(T.combine_levels(['warning', 'error']), 'error')


class TestBuildTr(unittest.TestCase):
    def test_select_item_response_carries_the_identifier(self):
        tr = T.build_tr(3, 0x24, 0x82, 0x81, {'result': 0x00, 'item_id': 7})
        self.assertEqual(tr.hex().upper(), '81030324008202828190010783020000')

    def test_cancel_response_has_no_item_identifier(self):
        tr = T.build_tr(3, 0x24, 0x82, 0x81, {'result': 0x10, 'item_id': 7})
        self.assertEqual(tr.hex().upper(), '81030324008202828183021000')

    def test_display_text_result_only(self):
        tr = T.build_tr(2, 0x21, 0x82, 0x81, {'result': 0x00})
        self.assertEqual(tr.hex().upper(), '81030221008202828183020000')


# ─── runner with a fake card ─────────────────────────────────────────────

class FakeScc:
    """APDU queue keyed by prefix; unmatched commands answer 6D00."""
    cat_cla = '80'

    def __init__(self):
        self._tp = self
        self.queues = {}
        self.sent = []

    def push(self, prefix, data='', sw='9000'):
        self.queues.setdefault(prefix.upper(), []).append((data, sw))
        return self

    def send_apdu(self, apdu):
        up = apdu.upper()
        self.sent.append(up)
        for prefix, queue in self.queues.items():
            if up.startswith(prefix) and queue:
                return queue.pop(0)
        return '', '6D00'


class FakeServer:
    def __init__(self, scc):
        self.scc = scc
        self.app = None
        self.sms_oa = '12345'
        self.sms_sc = '12345678912'
        self.card_session = 1
        self.stk_pending = None
        self.menu_active = False


SELECT_ITEM_CMD = 'D0108103012400820281828F050174657374'       # item 1: 'test'
DISPLAY_TEXT_CMD = 'D0118103022100820281828D060468656C6C6F'    # 'hello'


class RunnerTestCase(unittest.TestCase):
    def setUp(self):
        self.saved = (S._TEST_RUNNING, dict(S._TEST_RUN), S._POLL_ENABLED)
        S._POLL_ENABLED = False
        S._TEST_RUNNING = False

    def tearDown(self):
        S._TEST_RUNNING, S._POLL_ENABLED = self.saved[0], self.saved[2]
        S._TEST_RUN.clear()
        S._TEST_RUN.update(self.saved[1])

    def run_script(self, server, steps, preset=None, stop=False):
        script = T.normalise_script({'name': 'test', 'steps': steps}, S._test_command_type)
        S._TEST_RUN.update({'running': True, 'stop': stop, 'name': 'test',
                            'status': None, 'steps': [], 'index': 0,
                            'total': len(script['steps']), 'scp80_counter': None,
                            'started': time.time(), 'finished': None, 'error': None})
        S._TEST_RUNNING = True
        try:
            S._test_run_execute(server, script, preset or {})
        finally:
            S._TEST_RUNNING = False
        return S._TEST_RUN


class TestRunnerDialogue(RunnerTestCase):
    def test_stk_menu_browsing_script(self):
        # 1) menu selection -> SELECT ITEM announced; 2) expect it, answer with
        # item 1; the TR leaves DISPLAY TEXT pending; 3) expect it, answer 00.
        scc = FakeScc()
        scc.push('80C2', '', '9103')                     # ENVELOPE(Menu Selection)
        scc.push('8012', SELECT_ITEM_CMD, '9000')        # FETCH
        scc.push('8014', '', '9105')                     # TR -> DISPLAY TEXT pending
        scc.push('8012', DISPLAY_TEXT_CMD, '9000')       # FETCH
        scc.push('8014', '', '9000')                     # TR -> session over
        server = FakeServer(scc)
        run = self.run_script(server, [
            {'type': 'action', 'kind': 'menu-select', 'params': {'item_id': 128},
             'check': {'sw': '91??'}},
            {'type': 'expect', 'command': 'SELECT ITEM',
             'checks': [{'kind': 'item', 'id': 1, 'text': 'test'}],
             'respond': {'result': 'ok', 'item_id': 1}},
            {'type': 'expect', 'command': 'DISPLAY TEXT',
             'checks': [{'kind': 'text', 'value': 'hello'}],
             'respond': {'result': 'ok'}},
        ])
        self.assertEqual(run['status'], 'ok', run['steps'])
        self.assertEqual([s['status'] for s in run['steps']], ['ok', 'ok', 'ok'])
        # the SELECT ITEM TR carries the item identifier
        self.assertIn('900101', run['steps'][1]['sent'])
        # the DISPLAY TEXT check decoded the text
        text_check = [c for c in run['steps'][2]['checks'] if c['label'] == 'Text'][0]
        self.assertEqual(text_check['actual'], 'hello')

    def test_expectation_without_pending_command_is_an_error(self):
        server = FakeServer(FakeScc())
        run = self.run_script(server, [
            {'type': 'expect', 'command': 'DISPLAY TEXT',
             'checks': [{'kind': 'text', 'value': 'hello'}], 'respond': {}},
        ])
        self.assertEqual(run['status'], 'error')
        self.assertIn('no proactive command pending', run['steps'][0]['note'])

    def test_text_mismatch_fails_the_expectation(self):
        scc = FakeScc()
        scc.push('80C2', '', '9105')
        scc.push('8012', DISPLAY_TEXT_CMD, '9000')
        scc.push('8014', '', '9000')
        run = self.run_script(FakeServer(scc), [
            {'type': 'action', 'kind': 'menu-select', 'params': {'item_id': 1},
             'check': {'sw': '91??'}},
            {'type': 'expect', 'command': 'DISPLAY TEXT',
             'checks': [{'kind': 'text', 'value': 'goodbye'}], 'respond': {}},
        ])
        self.assertEqual(run['status'], 'error')
        self.assertEqual(run['steps'][1]['status'], 'error')
        text_check = [c for c in run['steps'][1]['checks'] if c['label'] == 'Text'][0]
        self.assertFalse(text_check['ok'])
        self.assertEqual(text_check['actual'], 'hello')

    def test_unexpected_pending_command_terminates_and_is_drained(self):
        scc = FakeScc()
        scc.push('80C2', '', '9103')       # announces a command
        scc.push('8012', SELECT_ITEM_CMD, '9000')
        scc.push('8014', '', '9000')
        run = self.run_script(FakeServer(scc), [
            {'type': 'action', 'kind': 'menu-select', 'params': {'item_id': 1},
             'check': {'sw': '91??'}},
            {'type': 'action', 'kind': 'menu-select', 'params': {'item_id': 2}},
        ])
        self.assertEqual(run['status'], 'error')
        self.assertIn('unexpected proactive command pending', run['steps'][1]['note'])
        # the drain FETCHed the command and answered with a cancel TR (0x10)
        self.assertTrue(any(a.startswith('8012') for a in scc.sent[1:]))
        self.assertTrue(any('83021000' in a for a in scc.sent))

    def test_sw_mismatch_error_stops_the_script_warning_continues(self):
        scc = FakeScc()
        scc.push('00A4', '6A82', '6A82')
        run = self.run_script(FakeServer(scc), [
            {'type': 'action', 'kind': 'apdu', 'params': {'apdu': '00A4040008A00000015100'},
             'check': {'sw': '9000'}, 'on_fail': 'error'},
            {'type': 'action', 'kind': 'apdu', 'params': {'apdu': '00A4040008A00000015100'}},
        ])
        self.assertEqual(run['status'], 'error')
        self.assertEqual(len(run['steps']), 1)     # second step never ran

        scc = FakeScc()
        scc.push('00A4', 'AA', '9000')
        scc.push('00A4', '', '6982')
        run = self.run_script(FakeServer(scc), [
            {'type': 'action', 'kind': 'apdu', 'params': {'apdu': '00A4040008A00000015100'},
             'check': {'sw': '6982'}, 'on_fail': 'warning'},
            {'type': 'action', 'kind': 'apdu', 'params': {'apdu': '00A4040008A00000015100'},
             'check': {'sw': '6982'}},
        ])
        self.assertEqual(len(run['steps']), 2)     # warning continued
        self.assertEqual(run['status'], 'warning')

    def test_status_poll_waits_for_91xx(self):
        scc = FakeScc()
        scc.push('80F2', '', '9000')
        scc.push('80F2', '', '9000')
        scc.push('80F2', '', '9103')
        scc.push('8012', SELECT_ITEM_CMD, '9000')
        scc.push('8014', '', '9000')
        run = self.run_script(FakeServer(scc), [
            {'type': 'action', 'kind': 'status', 'params': {'attempts': 3, 'interval_ms': 0}},
            {'type': 'expect', 'command': 'SELECT ITEM', 'respond': {'result': 'cancel'}},
        ])
        self.assertEqual(run['status'], 'ok', run['steps'])
        self.assertEqual(sum(1 for a in scc.sent if a.startswith('80F2')), 3)
        self.assertEqual(run['steps'][0]['sent'], 'STATUS x3')

    def test_stop_before_the_first_step(self):
        run = self.run_script(FakeServer(FakeScc()), [
            {'type': 'action', 'kind': 'status', 'params': {}},
        ], stop=True)
        self.assertEqual(run['status'], 'stopped')
        self.assertEqual(run['steps'], [])


class TestRunnerActions(RunnerTestCase):
    def test_script_menu_select_mirrors_menu_active(self):
        # 9000 -> the menu dialogue is over
        server = FakeServer(FakeScc().push('80C2', '', '9000'))
        self.run_script(server, [{'type': 'action', 'kind': 'menu-select', 'params': {'item_id': 1}}])
        self.assertFalse(server.menu_active)
        # 91XX -> a menu dialogue is pending (drained at the end)
        scc = FakeScc()
        scc.push('80C2', '', '9103')
        scc.push('8012', SELECT_ITEM_CMD, '9000')
        scc.push('8014', '', '9000')
        server = FakeServer(scc)
        self.run_script(server, [{'type': 'action', 'kind': 'menu-select',
                                  'params': {'item_id': 1}, 'check': {'sw': '91??'}}])
        self.assertTrue(server.menu_active)

    def test_scp80_uses_the_preset_and_advances_the_counter(self):
        scc = FakeScc()
        server = FakeServer(scc)
        preset = {'kic': '15', 'kid': '15', 'kicKey': 'AA' * 16, 'kidKey': 'BB' * 16,
                  'counter': '0000000A', 'tar': 'B00000', 'spi1': '16', 'spi2': '01'}
        with mock.patch.object(S, '_build_secured_packet',
                               return_value=('AA' * 10, {})) as build, \
                mock.patch.object(S, '_send_secured_packet',
                                  return_value={'success': True, 'sw': '9102',
                                                'response_data': None}) as send:
            scc.push('8012', DISPLAY_TEXT_CMD, '9000')
            scc.push('8014', '', '9000')
            run = self.run_script(server, [
                {'type': 'action', 'kind': 'scp80', 'params': {'apdu': '80E2900000'},
                 'check': {'sw': {'mode': 'mask', 'value': '91??'}, 'por': 'none'}},
                {'type': 'expect', 'command': 'DISPLAY TEXT',
                 'checks': [{'kind': 'text', 'value': 'hello'}],
                 'respond': {'result': 'ok'}},
            ], preset)
        self.assertEqual(run['status'], 'ok', run['steps'])
        self.assertEqual(run['steps'][0]['counter'], '0000000A')
        self.assertEqual(run['scp80_counter'], '0000000B')
        # the counter is the preset's, TAR comes from the preset when not overridden
        self.assertEqual(build.call_args[0][4], 'B00000')
        self.assertEqual(build.call_args[0][5], '0000000A')
        self.assertFalse(send.call_args.kwargs.get('handle_proactive', True))

    def test_scp80_source_selects_between_apdu_and_packet(self):
        script = T.normalise_script({'steps': [
            {'type': 'action', 'kind': 'scp80',
             'params': {'source': 'apdu', 'apdu': '80E2900000', 'sp': 'AABB'}},
        ]}, _resolver)
        params = script['steps'][0]['params']
        self.assertIn('apdu', params)
        self.assertNotIn('sp', params)
        script = T.normalise_script({'steps': [
            {'type': 'action', 'kind': 'scp80', 'params': {'source': 'sp', 'sp': 'AABB', 'apdu': '80E2'}},
        ]}, _resolver)
        self.assertIn('sp', script['steps'][0]['params'])
        self.assertNotIn('apdu', script['steps'][0]['params'])
        with self.assertRaises(T.ScriptError):
            T.normalise_script({'steps': [
                {'type': 'action', 'kind': 'scp80', 'params': {'source': 'sp', 'apdu': '80E2'}}]}, _resolver)
        with self.assertRaises(T.ScriptError):
            T.normalise_script({'steps': [
                {'type': 'action', 'kind': 'scp80', 'params': {'source': 'nope', 'apdu': '80E2'}}]}, _resolver)

    def test_scp80_step_overrides_tar_and_spi_only(self):
        scc = FakeScc()
        preset = {'kic': '15', 'kid': '15', 'kicKey': 'AA' * 16, 'kidKey': 'BB' * 16,
                  'counter': '00000001', 'tar': 'B00000', 'spi1': '16', 'spi2': '01'}
        with mock.patch.object(S, '_build_secured_packet', return_value=('AA' * 10, {})) as build, \
                mock.patch.object(S, '_send_secured_packet',
                                  return_value={'success': True, 'sw': '9000',
                                                'response_data': None}):
            self.run_script(FakeServer(scc), [
                {'type': 'action', 'kind': 'scp80',
                 'params': {'apdu': '80E2900000', 'tar': 'B00001', 'spi1': '17', 'spi2': '01'}},
            ], preset)
        args = build.call_args[0]
        self.assertEqual(args[0], '17')       # spi1 override
        self.assertEqual(args[4], 'B00001')   # tar override
        self.assertEqual(args[2], '15')       # kic stays preset-owned

    def test_preset_completeness_is_validated(self):
        script = T.normalise_script({'steps': [
            {'type': 'action', 'kind': 'scp80', 'params': {'apdu': '80E2900000'}},
        ]}, S._test_command_type)
        self.assertIn('incomplete', S._test_preset_error(script, {}) or '')
        full = {'kic': '15', 'kid': '15', 'kicKey': 'AA', 'kidKey': 'BB',
                'counter': '00000000', 'tar': 'B00000', 'spi1': '16', 'spi2': '01'}
        self.assertIsNone(S._test_preset_error(script, full))
        no_tar = dict(full, tar='')
        self.assertIn('TAR', S._test_preset_error(script, no_tar) or '')

    def test_file_write_and_read_actions(self):
        class FakeLchan:
            selected_file = None

            def __init__(self):
                self.written = None
                self.content = 'AA55'
            def update_binary(self, data):
                self.written = data
                return '', '9000'
            def read_binary(self):
                return self.content, '9000'

        lchan = FakeLchan()
        server = FakeServer(FakeScc())
        with mock.patch.object(S, '_select_path', return_value=(None, None)), \
                mock.patch.object(S, '_get_file_type', return_value='transparent'):
            server.app = type('App', (), {'rs': type('Rs', (), {
                'lchan': [lchan]})()})()
            run = self.run_script(server, [
                {'type': 'action', 'kind': 'file-write',
                 'params': {'path': 'ADF.USIM/EF.TEST', 'data': 'AA55'}},
                {'type': 'action', 'kind': 'file-read',
                 'params': {'path': 'ADF.USIM/EF.TEST'},
                 'check': {'sw': '9000', 'data': {'mode': 'mask', 'value': 'AA??'}}},
            ])
        self.assertEqual(run['status'], 'ok', run['steps'])
        self.assertEqual(lchan.written, 'AA55')
        self.assertEqual(run['steps'][1]['data'], 'AA55')

    def test_run_guard_blocks_card_endpoints_only(self):
        S._TEST_RUNNING = True
        try:
            for path in ('/api/apdu', '/api/read', '/api/send-ota', '/api/menu-select',
                         '/api/esim/profiles', '/api/scp81/bip'):
                self.assertTrue(S._test_request_blocked(path), path)
            for path in ('/api/test/status', '/api/test/stop', '/api/status',
                         '/api/version', '/api/proactive-log', '/index.html'):
                self.assertFalse(S._test_request_blocked(path), path)
            self.assertFalse(S._test_request_blocked('/api/apdu?x=1') is False)
        finally:
            S._TEST_RUNNING = False

    def test_state_snapshot_is_a_copy(self):
        S._TEST_RUN['steps'] = [{'index': 0, 'status': 'ok'}]
        snap = S._test_state_snapshot()
        snap['steps'][0]['status'] = 'mutated'
        self.assertEqual(S._TEST_RUN['steps'][0]['status'], 'ok')

    def test_increment_counter_hex_keeps_width(self):
        self.assertEqual(S._increment_counter_hex('000000FF'), '00000100')
        self.assertEqual(S._increment_counter_hex('FF'), '00')
        self.assertEqual(S._increment_counter_hex(''), '')


if __name__ == '__main__':
    unittest.main()
