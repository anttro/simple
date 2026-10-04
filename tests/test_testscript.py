#!/usr/bin/env python3
"""Tests for the test-script engine (pure parts) and the server-side runner."""

import contextlib
import io
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


def preset_tars(isd='000000', msl='16'):
    """The three mandatory role entries of a card preset's TAR table."""
    return [{'role': 'isd', 'tar': isd, 'msl': msl, 'desc': ''},
            {'role': 'uiccRfm', 'tar': 'B00000', 'msl': '16', 'desc': ''},
            {'role': 'usimRfm', 'tar': 'B00001', 'msl': '16', 'desc': ''}]


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

    def test_respond_text_codings_and_raw(self):
        # TS 102 223 8.15 recommended codings: '00' GSM default alphabet
        # 7 bits packed, '04' GSM default alphabet 8 bits, '08' UCS2.
        respond = T.normalise_respond({'result': 'ok', 'text': 'hello', 'dcs': '00',
                                       'raw': 'aa01bb'})
        self.assertEqual(respond['text'], 'hello')
        self.assertEqual(respond['raw'], 'AA01BB')
        # 6.8.0 object order: command details, device identities, Result,
        # then the command-specific objects (here Text), then the raw extras.
        tr = T.build_tr(4, 0x23, 0x81, 0x82, respond)
        self.assertEqual(tr.hex().upper(),
                         '8103042300' + '82028182' + '83020000'
                         + '8D0600' + 'E8329BFD06' + 'AA01BB')
        # 8 bits: one octet per character
        tr = T.build_tr(4, 0x23, 0x81, 0x82, {'result': 0x00, 'text': 'hello', 'dcs': 0x04})
        self.assertEqual(tr.hex().upper(),
                         '8103042300' + '82028182' + '83020000' + '8D0604' + '68656C6C6F')
        # UCS2 (UTF-16-BE): 'A' U+0041, 'Ж' U+0416
        tr = T.build_tr(4, 0x23, 0x81, 0x82,
                        {'result': 0x00, 'text': 'A\u0416', 'dcs': 0x08})
        self.assertEqual(tr.hex().upper(),
                         '8103042300' + '82028182' + '83020000' + '8D0508' + '00410416')
        # an empty answer is the null text string (Length 00, no DCS)
        tr = T.build_tr(4, 0x23, 0x81, 0x82, {'result': 0x00, 'text': ''})
        self.assertEqual(tr.hex().upper(),
                         '8103042300' + '82028182' + '83020000' + '8D00')

    def test_menu_select_params_accept_id_or_text(self):
        script = T.normalise_script({'steps': [
            {'type': 'action', 'kind': 'menu-select', 'params': {'item_id': 3}},
            {'type': 'action', 'kind': 'menu-select',
             'params': {'text': 'My menu', 'mode': 'contains'}},
            {'type': 'action', 'kind': 'menu-select',
             'params': {'text': 'Exact', 'mode': 'exact', 'case_sensitive': True}},
        ]}, _resolver)
        self.assertEqual(script['steps'][0]['params'], {'item_id': 3})
        # the text match is case-insensitive by default (menu labels are UI
        # text); an explicit true is kept for an exact-case match
        self.assertEqual(script['steps'][1]['params'],
                         {'text': 'My menu', 'mode': 'contains', 'case_sensitive': False})
        self.assertEqual(script['steps'][2]['params'],
                         {'text': 'Exact', 'mode': 'exact', 'case_sensitive': True})
        with self.assertRaises(T.ScriptError):
            T.normalise_script({'steps': [{'type': 'action', 'kind': 'menu-select',
                                           'params': {}}]}, _resolver)
        with self.assertRaises(T.ScriptError):
            T.normalise_script({'steps': [{'type': 'action', 'kind': 'menu-select',
                                           'params': {'item_id': 1, 'text': 'x'}}]}, _resolver)
        with self.assertRaises(T.ScriptError):
            T.normalise_script({'steps': [{'type': 'action', 'kind': 'menu-select',
                                           'params': {'text': 'x', 'mode': 'nope'}}]}, _resolver)

    def test_event_action_normalises_the_semantic_fields(self):
        script = T.normalise_script({'steps': [
            {'type': 'action', 'kind': 'event',
             'params': {'event': 'location status',
                        'fields': {'status': 1, 'mcc': '250'}}},
            {'type': 'action', 'kind': 'event',
             'params': {'event': '0x1D', 'src': '83',
                        'fields': {'status': '0', 'type': '0', 'ti': '00',
                                   'loc_status': '0'}}},
        ]}, _resolver)
        p = script['steps'][0]['params']
        self.assertEqual(p['event'], 0x03)
        self.assertEqual(p['fields'], {'status': 1, 'mcc': '250'})
        self.assertNotIn('src', p)
        p = script['steps'][1]['params']
        self.assertEqual(p['event'], 0x1D)
        self.assertEqual(p['src'], '83')
        # an unknown name, a bad field and a bad source are refused before the run
        with self.assertRaises(T.ScriptError):
            T.normalise_script({'steps': [{'type': 'action', 'kind': 'event',
                                           'params': {'event': 'nope'}}]}, _resolver)
        with self.assertRaises(T.ScriptError):
            T.normalise_script({'steps': [{'type': 'action', 'kind': 'event',
                'params': {'event': '0x03', 'fields': {'status': 9}}}]}, _resolver)
        with self.assertRaises(T.ScriptError):
            T.normalise_script({'steps': [{'type': 'action', 'kind': 'event',
                'params': {'event': 3, 'src': 'zz'}}]}, _resolver)
        # the raw envelope action carries the same source override
        script = T.normalise_script({'steps': [
            {'type': 'action', 'kind': 'envelope',
             'params': {'event': 0x00, 'data': '', 'src': '83'}}]}, _resolver)
        self.assertEqual(script['steps'][0]['params'],
                         {'event': 0x00, 'data': '', 'src': '83'})
        with self.assertRaises(T.ScriptError):
            T.normalise_script({'steps': [{'type': 'action', 'kind': 'envelope',
                'params': {'event': 3, 'src': 'zz'}}]}, _resolver)

    def test_scp80_keyset_number_and_new_content_checks(self):
        # kvn (1..15) selects the preset's keyset; `por`/`files` are expect
        # content checks (the PoR contents / the REFRESH file list).
        script = T.normalise_script({'steps': [
            {'type': 'action', 'kind': 'scp80',
             'params': {'apdu': '80E2900000', 'kvn': 2}},
            {'type': 'expect', 'command': 'SEND SHORT MESSAGE',
             'checks': [{'kind': 'por', 'status': 'por_ok', 'sw': '6A8?',
                         'data': {'mode': 'mask', 'value': 'AA??'}}]},
            {'type': 'expect', 'command': 'REFRESH',
             'checks': [{'kind': 'files', 'value': '3F007F106F3A, 3F002FE2'}]},
        ]}, _resolver)
        self.assertEqual(script['steps'][0]['params']['kvn'], 2)
        por = script['steps'][1]['checks'][0]
        self.assertEqual(por['status'], 'por_ok')
        self.assertEqual(por['sw'], {'mode': 'mask', 'value': '6A8?'})
        self.assertEqual(por['data'], {'mode': 'mask', 'value': 'AA??'})
        self.assertEqual(script['steps'][2]['checks'][0]['files'],
                         ['3F007F106F3A', '3F002FE2'])
        # a keyset number outside 1..15, an empty por check and a bad path are
        # refused before anything runs
        with self.assertRaises(T.ScriptError):
            T.normalise_script({'steps': [
                {'type': 'action', 'kind': 'scp80',
                 'params': {'apdu': '80E2', 'kvn': 0}}]}, _resolver)
        with self.assertRaises(T.ScriptError):
            T.normalise_script({'steps': [
                {'type': 'expect', 'command': 'ANY',
                 'checks': [{'kind': 'por'}]}]}, _resolver)
        with self.assertRaises(T.ScriptError):
            T.normalise_script({'steps': [
                {'type': 'expect', 'command': 'ANY',
                 'checks': [{'kind': 'files', 'value': ['zz']}]}]}, _resolver)

    def test_scp80_format_param_and_por_object(self):
        # v3.19.0: the scp80 step may wrap the C-APDU in the expanded Command
        # Scripting template (TS 102 226 5.2.1) and assert the decoded PoR
        # inline (`check.por` as an object).
        script = T.normalise_script({'steps': [
            {'type': 'action', 'kind': 'scp80',
             'params': {'apdu': '80E2900000', 'format': 'expanded'},
             'check': {'por': {'status': 'por_ok', 'sw': '6A8?',
                               'data': {'mode': 'mask', 'value': 'AA??'}}}},
        ]}, _resolver)
        params = script['steps'][0]['params']
        self.assertEqual(params['format'], 'expanded')
        por = script['steps'][0]['check']['por']
        self.assertEqual(por['status'], 'por_ok')
        self.assertEqual(por['sw'], {'mode': 'mask', 'value': '6A8?'})
        self.assertEqual(por['data'], {'mode': 'mask', 'value': 'AA??'})
        # both codings are accepted; an unknown one is refused
        for fmt in ('compact', 'expanded-ae'):
            script = T.normalise_script({'steps': [
                {'type': 'action', 'kind': 'scp80',
                 'params': {'apdu': '80E2', 'format': fmt}}]}, _resolver)
            self.assertEqual(script['steps'][0]['params']['format'], fmt)
        with self.assertRaises(T.ScriptError):
            T.normalise_script({'steps': [
                {'type': 'action', 'kind': 'scp80',
                 'params': {'apdu': '80E2', 'format': 'auto'}}]}, _resolver)
        # an empty PoR object has nothing to assert; the object is scp80-only
        with self.assertRaises(T.ScriptError):
            T.normalise_script({'steps': [
                {'type': 'action', 'kind': 'scp80', 'params': {'apdu': '80E2'},
                 'check': {'por': {}}}]}, _resolver)
        with self.assertRaises(T.ScriptError):
            T.normalise_script({'steps': [
                {'type': 'action', 'kind': 'status', 'params': {'attempts': 1},
                 'check': {'por': {'status': 'por_ok'}}}]}, _resolver)

    def test_alpha_and_sms_content_checks(self):
        # `alpha` reads the Alpha identifier ('05', Annex A coded); `sms`
        # asserts the SEND SHORT MESSAGE TPDU fields (submit only).
        script = T.normalise_script({'steps': [
            {'type': 'expect', 'command': 'DISPLAY TEXT',
             'checks': [{'kind': 'alpha', 'mode': 'contains', 'value': 'Alfa',
                         'case_sensitive': False}]},
            {'type': 'expect', 'command': 'SEND SHORT MESSAGE',
             'checks': [{'kind': 'sms', 'da': '12 345', 'pid': '7F', 'dcs': '00',
                         'udl': 5, 'ud': '0271??AABB'}]},
        ]}, _resolver)
        alpha = script['steps'][0]['checks'][0]
        self.assertEqual(alpha['kind'], 'alpha')
        self.assertEqual(alpha['mode'], 'contains')
        self.assertFalse(alpha['case_sensitive'])
        sms = script['steps'][1]['checks'][0]
        self.assertEqual(sms['da'], '12345')
        self.assertEqual(sms['pid'], {'mode': 'exact', 'value': '7F'})
        self.assertEqual(sms['dcs'], {'mode': 'exact', 'value': '00'})
        self.assertEqual(sms['udl'], 5)
        self.assertEqual(sms['ud'], {'mode': 'mask', 'value': '0271??AABB'})
        for check in ({'kind': 'alpha'},                      # no value
                      {'kind': 'alpha', 'mode': 'nope', 'value': 'x'},
                      {'kind': 'sms'},                        # no fields
                      {'kind': 'sms', 'da': '12X'},
                      {'kind': 'sms', 'udl': 256}):
            with self.assertRaises(T.ScriptError):
                T.normalise_script({'steps': [
                    {'type': 'expect', 'command': 'ANY', 'checks': [check]}]}, _resolver)


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
        # TS 102 223 6.8.0: the item identifier (F) follows the Result (C)
        tr = T.build_tr(3, 0x24, 0x82, 0x81, {'result': 0x00, 'item_id': 7})
        self.assertEqual(tr.hex().upper(),
                         '81030324008202828183020000900107')

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

    def test_failed_thread_start_unblocks_the_card(self):
        script = T.normalise_script({'steps': [
            {'type': 'action', 'kind': 'status', 'params': {}}]}, S._test_command_type)
        server = FakeServer(FakeScc())
        with mock.patch.object(S.threading, 'Thread', side_effect=RuntimeError('no threads')):
            with self.assertRaises(RuntimeError):
                S._test_run_start(server, script, {})
        self.assertFalse(S._TEST_RUNNING)
        self.assertFalse(S._TEST_RUN['running'])
        self.assertEqual(S._TEST_RUN['status'], 'error')

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

    # SET UP MENU (title 'Menu', item 1 = 'One') as the card sends it.
    SETUP_MENU_CMD = 'D01581030125008202828185044D656E758F04014F6E65'

    def test_expect_updates_the_cached_menu_and_text_selection_resolves(self):
        # The item ids vary with the applet's install parameters: the menu the
        # card sends must refresh the cache, and a text selection resolves
        # against it (the live id is not in the script).
        scc = FakeScc()
        scc.push('80F2', '', '9102')
        scc.push('8012', self.SETUP_MENU_CMD, '9000')
        scc.push('8014', '', '9000')
        scc.push('80C2', '', '9000')
        server = FakeServer(scc)
        run = self.run_script(server, [
            {'type': 'action', 'kind': 'status', 'params': {'attempts': 2},
             'check': {'sw': {'mode': 'mask', 'value': '91??'}}},
            {'type': 'expect', 'command': 'SET UP MENU', 'respond': {'result': 'ok'}},
            {'type': 'action', 'kind': 'menu-select', 'params': {'text': 'One'}},
        ])
        self.assertEqual(run['status'], 'ok', run['steps'])
        self.assertEqual(server.sim_menu['items'],
                         [{'id': 1, 'text': 'One'}])
        # the resolved Menu Selection TLV carries the item id the card gave
        self.assertIn('D30702020181900101', ''.join(scc.sent))

    def test_menu_text_selection_reports_the_available_items(self):
        scc = FakeScc()
        server = FakeServer(scc)
        server.sim_menu = {'title': 'Menu', 'items': [{'id': 3, 'text': 'Alpha'}]}
        run = self.run_script(server, [
            {'type': 'action', 'kind': 'menu-select', 'params': {'text': 'Beta'}},
        ])
        self.assertEqual(run['status'], 'error')
        note = run['steps'][0].get('note') or ''
        self.assertIn("no menu item matches 'Beta'", note)
        self.assertIn("3='Alpha'", note)
        self.assertEqual(scc.sent, [])

    # The live card's menu (captured 2026-10-04 20:58): Annex A Variant 2
    # Cyrillic items under the card's ids 128..130 - the compressed
    # `81 <num_chars> 08 <chars>` form (the menu-select text must resolve
    # against it; the card's ids stay out of the script).
    LIVE_MENU = {'title': 'GPB MOBILE', 'items': [
        {'id': 128, 'text': 'Выбор сети'}, {'id': 129, 'text': 'Язык'},
        {'id': 130, 'text': 'Кофе'}]}

    def test_menu_text_selection_matches_variant2_cyrillic(self):
        # a lowercase exact match resolves by default (case-insensitive)
        scc = FakeScc().push('80C2', '', '9000')
        server = FakeServer(scc)
        server.sim_menu = dict(self.LIVE_MENU)
        run = self.run_script(server, [
            {'type': 'action', 'kind': 'menu-select',
             'params': {'text': 'кофе'}},
        ])
        self.assertEqual(run['status'], 'ok', run['steps'])
        self.assertIn('D30702020181900182', ''.join(scc.sent))
        # a lowercase contains match resolves too (item 128)
        scc = FakeScc().push('80C2', '', '9000')
        server = FakeServer(scc)
        server.sim_menu = dict(self.LIVE_MENU)
        run = self.run_script(server, [
            {'type': 'action', 'kind': 'menu-select',
             'params': {'text': 'ыбор', 'mode': 'contains'}},
        ])
        self.assertEqual(run['status'], 'ok', run['steps'])
        self.assertIn('D30702020181900180', ''.join(scc.sent))
        # an explicit case-sensitive spec misses and names the loose match
        server = FakeServer(FakeScc())
        server.sim_menu = dict(self.LIVE_MENU)
        run = self.run_script(server, [
            {'type': 'action', 'kind': 'menu-select',
             'params': {'text': 'кофе', 'case_sensitive': True}},
        ])
        self.assertEqual(run['status'], 'error')
        note = run['steps'][0].get('note') or ''
        self.assertIn("no menu item matches 'кофе'", note)
        self.assertIn('case-insensitive match exists', note)
        self.assertIn("130='Кофе'", note)

    def test_menu_text_selection_refuses_an_ambiguous_match(self):
        scc = FakeScc()
        server = FakeServer(scc)
        server.sim_menu = {'items': [{'id': 1, 'text': 'Alpha one'},
                                     {'id': 2, 'text': 'Alpha two'}]}
        run = self.run_script(server, [
            {'type': 'action', 'kind': 'menu-select',
             'params': {'text': 'Alpha', 'mode': 'contains'}},
        ])
        self.assertEqual(run['status'], 'error')
        note = run['steps'][0].get('note') or ''
        self.assertIn('2 items match', note)
        self.assertIn('use the item id', note)

    def test_event_action_sends_the_built_object(self):
        scc = FakeScc()
        scc.push('80C2', '', '9000')
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            run = self.run_script(FakeServer(scc), [
                {'type': 'action', 'kind': 'event',
                 'params': {'event': 'location status',
                            'fields': {'status': '0', 'mcc': '250', 'mnc': '01',
                                       'lac': '00FF', 'cell': '0001'}}},
            ])
        self.assertEqual(run['status'], 'ok', run['steps'])
        apdu = scc.sent[0].upper()
        # event list + device identities (terminal default) + the built object
        self.assertIn('99010382028281' + '9B0100930752F01000FF0001', apdu)
        # the runner logs one step-annotated ENVELOPE line, not the generic
        # sender pair as well (its line carries the name, src and SW)
        log = buf.getvalue()
        self.assertIn('TEST-RUN step 1: ENVELOPE(Event Download) type=0x03 '
                      '(location_status) data=9B0100930752F01000FF0001 src=82 -> SW=9000',
                      log)
        self.assertNotIn('ENVELOPE(Event Download): type=', log)
        self.assertNotIn('ENVELOPE SW:', log)
        # the src override names the network as the source
        scc2 = FakeScc()
        scc2.push('80C2', '', '9000')
        buf2 = io.StringIO()
        with contextlib.redirect_stderr(buf2):
            run = self.run_script(FakeServer(scc2), [
                {'type': 'action', 'kind': 'event',
                 'params': {'event': '0x0B', 'fields': {'tech': '8'}, 'src': '83'}},
            ])
        self.assertEqual(run['status'], 'ok', run['steps'])
        apdu = scc2.sent[0].upper()
        self.assertIn('99010B82028381' + 'BF0108', apdu)
        self.assertIn('src=83 -> SW=9000', buf2.getvalue())
        # the raw envelope action takes the same source override
        scc3 = FakeScc()
        scc3.push('80C2', '', '9000')
        run = self.run_script(FakeServer(scc3), [
            {'type': 'action', 'kind': 'envelope',
             'params': {'event': 0x00, 'data': '', 'src': '83'}},
        ])
        self.assertEqual(run['status'], 'ok', run['steps'])
        self.assertIn('99010082028381', scc3.sent[0].upper())

    def test_scp80_uses_the_preset_and_advances_the_counter(self):
        scc = FakeScc()
        server = FakeServer(scc)
        # the ISD entry carries a distinctive TAR: the assertion below must
        # fail if the runner ever falls back to a built-in default
        preset = {'kic': '15', 'kid': '15', 'kicKey': 'AA' * 16, 'kidKey': 'BB' * 16,
                  'counter': '0000000A', 'tars': preset_tars(isd='B00000')}
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
                  'counter': '00000001', 'tars': preset_tars()}
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

    def test_scp80_step_selects_the_keyset_by_kvn(self):
        # the step's keyset number picks the keyset (v3.15.0: the normaliser
        # dropped `kvn`, so scripts silently used the preset's first keyset)
        scc = FakeScc()
        preset = {'keysets': [
                      {'kic': '15', 'kid': '15', 'kicKey': 'AA' * 16,
                       'kidKey': 'BB' * 16, 'cntr': '00000001'},
                      {'kic': '25', 'kid': '25', 'kicKey': 'CC' * 16,
                       'kidKey': 'DD' * 16, 'cntr': '00000020'}],
                  'tars': preset_tars()}
        with mock.patch.object(S, '_build_secured_packet', return_value=('AA' * 10, {})) as build, \
                mock.patch.object(S, '_send_secured_packet',
                                  return_value={'success': True, 'sw': '9000',
                                                'response_data': None}):
            run = self.run_script(FakeServer(scc), [
                {'type': 'action', 'kind': 'scp80',
                 'params': {'apdu': '80E2900000', 'kvn': 2}},
            ], preset)
        self.assertEqual(run['status'], 'ok', run['steps'])
        args = build.call_args[0]
        self.assertEqual(args[2], '25')        # the second keyset's KIc/KID
        self.assertEqual(args[3], '25')
        self.assertEqual(args[5], '00000020')  # and its counter
        self.assertEqual(args[7], 'CC' * 16)
        self.assertEqual(args[8], 'DD' * 16)
        self.assertEqual(run['scp80_counters'], {2: '00000021'})
        self.assertEqual(run['scp80_counter'], '00000021')

    def test_preset_completeness_validates_the_step_keyset(self):
        script = T.normalise_script({'steps': [
            {'type': 'action', 'kind': 'scp80',
             'params': {'apdu': '80E2900000', 'kvn': 3}}]}, S._test_command_type)
        preset = {'keysets': [{'kic': '15', 'kid': '15', 'kicKey': 'AA', 'kidKey': 'BB',
                               'cntr': '00000000'}],
                  'tars': preset_tars()}
        self.assertIn('keyset 3', S._test_preset_error(script, preset) or '')
        good = T.normalise_script({'steps': [
            {'type': 'action', 'kind': 'scp80',
             'params': {'apdu': '80E2900000', 'kvn': 1}}]}, S._test_command_type)
        self.assertIsNone(S._test_preset_error(good, preset))

    def test_run_preset_resolves_the_stored_id(self):
        class Store:
            def get(self, pid):
                return {'id': pid, 'name': 'Card 1'} if pid == 'p1' else None

        import types
        server = types.SimpleNamespace(card_presets=Store())
        preset, err = S._test_run_preset_from_body(server, {'preset_id': 'p1'})
        self.assertEqual(preset['name'], 'Card 1')
        self.assertIsNone(err)
        preset, err = S._test_run_preset_from_body(server, {'preset_id': 'nope'})
        self.assertIsNone(preset)
        self.assertIn('not found', err)
        inline = {'name': 'inline'}
        preset, err = S._test_run_preset_from_body(server, {'preset': inline})
        self.assertIs(preset, inline)
        self.assertIsNone(err)

    def test_run_script_resolves_the_stored_id(self):
        import types
        entry = {'id': 'a' * 32, 'name': 'stored',
                 'steps': [{'type': 'action', 'kind': 'status',
                            'params': {'attempts': 1}}]}

        class Store:
            def get(self, sid):
                return entry if sid == entry['id'] else None

        server = types.SimpleNamespace(test_scripts=Store())
        # the inline script wins (the PWA sends the edited copy) - the id is
        # carried for the report
        raw, sid, err = S._test_run_script_from_body(
            server, {'script': {'name': 'inline', 'steps': [{}]},
                     'script_id': entry['id']})
        self.assertEqual(raw['name'], 'inline')
        self.assertEqual(sid, entry['id'])
        self.assertIsNone(err)
        # script_id alone resolves from the store
        raw, sid, err = S._test_run_script_from_body(server, {'script_id': entry['id']})
        self.assertEqual(raw, {'name': 'stored', 'steps': entry['steps']})
        self.assertEqual(sid, entry['id'])
        self.assertIsNone(err)
        raw, sid, err = S._test_run_script_from_body(server, {'script_id': 'nope'})
        self.assertIsNone(raw)
        self.assertIn('not found', err)
        # neither form: the normaliser reports the missing script
        raw, sid, err = S._test_run_script_from_body(server, {})
        self.assertIsNone(raw)
        self.assertIsNone(sid)
        self.assertIsNone(err)

    def test_preset_completeness_is_validated(self):
        script = T.normalise_script({'steps': [
            {'type': 'action', 'kind': 'scp80', 'params': {'apdu': '80E2900000'}},
        ]}, S._test_command_type)
        self.assertIn('incomplete', S._test_preset_error(script, {}) or '')
        full = {'kic': '15', 'kid': '15', 'kicKey': 'AA', 'kidKey': 'BB',
                'counter': '00000000', 'tars': preset_tars()}
        self.assertIsNone(S._test_preset_error(script, full))
        no_tar = dict(full, tars=[])
        self.assertIn('TAR', S._test_preset_error(script, no_tar) or '')

    # A SEND SHORT MESSAGE announcing the PoR-in-submit (SPI2 bit 0x20): the
    # TPDU carries a submit PDUs with UD = 02 71 00 AA BB (the deliver-style
    # PoR header); the runner must decode it with the scp80 step's context.
    SEND_SM_POR_CMD = 'D0188103011300820283810B0D01000181127F0005027100AABB'
    # A REFRESH (qualifier 00 = NAA init + full FCN) with the file list
    # 12 07 01 3F007F106F3A (one file: MF/7F10/6F3A).
    REFRESH_FCN_CMD = 'D0128103010100820283811207013F007F106F3A'

    def test_the_run_log_carries_the_card_exchange_details(self):
        # the semantic lines a verification needs: the plaintext C-APDU, the
        # built secured packet, the FETCH/CMD/TR pair of an expectation and
        # the menu selection's ENVELOPE with the resolved item
        scc = FakeScc()
        scc.push('80F2', '', '9102')
        scc.push('8012', self.SETUP_MENU_CMD, '9000')
        scc.push('8014', '', '9000')
        scc.push('80C2', '', '9000')
        preset = {'kic': '15', 'kid': '15', 'kicKey': 'AA' * 16, 'kidKey': 'BB' * 16,
                  'counter': '0000000A', 'tars': preset_tars()}
        buf = io.StringIO()
        with mock.patch.object(S, '_build_secured_packet', return_value=('CC' * 10, {})), \
                mock.patch.object(S, '_send_secured_packet',
                                  return_value={'success': True, 'sw': '9000',
                                                'response_data': None}):
            with contextlib.redirect_stderr(buf):
                run = self.run_script(FakeServer(scc), [
                    {'type': 'action', 'kind': 'scp80',
                     'params': {'apdu': '80E2900000'}},
                    {'type': 'action', 'kind': 'status', 'params': {'attempts': 2},
                     'check': {'sw': {'mode': 'mask', 'value': '91??'}}},
                    {'type': 'expect', 'command': 'SET UP MENU',
                     'respond': {'result': 'ok'}},
                    {'type': 'action', 'kind': 'menu-select',
                     'params': {'text': 'One'}},
                ], preset)
        self.assertEqual(run['status'], 'ok', run['steps'])
        log = buf.getvalue()
        self.assertIn('TEST-RUN step 1: SCP80 C-APDU=80E2900000', log)
        self.assertIn('TEST-RUN step 1: SCP80 SECURED=' + 'CC' * 10, log)
        self.assertIn('TEST-RUN step 1: SCP80 -> SW=9000', log)
        self.assertIn('TEST-RUN step 1: PoR[inline] none', log)
        self.assertIn('TEST-RUN step 2: STATUS 1/2 -> 9102', log)
        self.assertIn('TEST-RUN step 3: FETCH=', log)
        self.assertIn('TEST-RUN step 3: CMD 0x25 SET UP MENU', log)
        self.assertIn('TEST-RUN step 3: TR=', log)
        self.assertIn('TEST-RUN step 4: MENU-SELECT ENVELOPE=', log)
        self.assertIn("item=1 ('One')", log)
        # the report's label carries the plaintext APDU too (log/report parity)
        self.assertIn('C-APDU=80E2900000', run['steps'][0]['sent'])

    def test_the_run_log_carries_the_inline_por(self):
        # the decoded PoR and its R-APDU are logged with the raw packet
        scc = FakeScc()
        preset = {'kic': '15', 'kid': '15', 'kicKey': 'AA' * 16, 'kidKey': 'BB' * 16,
                  'counter': '0000000A', 'tars': preset_tars()}
        decoded = {'response_status': 'por_ok', 'tar': '000000', 'cntr': '0000000A',
                   'raw': '0271000021AABB',
                   'decoded': {'last_status_word': '9000', 'last_response_data': 'AABB'}}
        buf = io.StringIO()
        with mock.patch.object(S, '_build_secured_packet', return_value=('AA' * 10, {})), \
                mock.patch.object(S, '_send_secured_packet',
                                  return_value={'success': True, 'sw': '9000',
                                                'response_data': '0271000021AABB'}), \
                mock.patch.object(S, '_decode_por', return_value=decoded):
            with contextlib.redirect_stderr(buf):
                run = self.run_script(FakeServer(scc), [
                    {'type': 'action', 'kind': 'scp80',
                     'params': {'apdu': '80E2900000'}},
                ], preset)
        self.assertEqual(run['status'], 'ok', run['steps'])
        log = buf.getvalue()
        self.assertIn('TEST-RUN step 1: PoR[inline] status=por_ok', log)
        self.assertIn('raw=0271000021AABB', log)
        self.assertIn('TEST-RUN step 1: R-APDU SW=9000 data=AABB', log)

    def test_expect_decodes_the_submit_por(self):
        scc = FakeScc()
        preset = {'kic': '15', 'kid': '15', 'kicKey': 'AA' * 16, 'kidKey': 'BB' * 16,
                  'counter': '0000000A', 'tars': preset_tars()}
        decoded = {'response_status': 'por_ok', 'tar': '000000',
                   'decoded': {'last_status_word': '9000', 'last_response_data': 'AABB'}}
        buf = io.StringIO()
        with mock.patch.object(S, '_build_secured_packet', return_value=('AA' * 10, {})), \
                mock.patch.object(S, '_send_secured_packet',
                                  return_value={'success': True, 'sw': '9102',
                                                'response_data': None}), \
                mock.patch.object(S, '_decode_por', return_value=decoded) as dec:
            scc.push('8012', self.SEND_SM_POR_CMD, '9000')
            scc.push('8014', '', '9000')
            with contextlib.redirect_stderr(buf):
                run = self.run_script(FakeServer(scc), [
                    {'type': 'action', 'kind': 'scp80', 'params': {'apdu': '80E2900000'},
                     'check': {'sw': {'mode': 'mask', 'value': '91??'}, 'por': 'none'}},
                    {'type': 'expect', 'command': 'SEND SHORT MESSAGE',
                     'checks': [{'kind': 'por', 'status': 'por_ok', 'sw': '9000'}],
                     'respond': {'result': 'ok'}},
                ], preset)
        self.assertEqual(run['status'], 'ok', run['steps'])
        log = buf.getvalue()
        self.assertIn('TEST-RUN step 2: PoR[sms-submit] status=por_ok', log)
        self.assertIn('TEST-RUN step 2: R-APDU SW=9000 data=AABB', log)
        self.assertEqual(run['steps'][1]['por']['response_status'], 'por_ok')
        labels = {c['label']: c for c in run['steps'][1]['checks']}
        self.assertTrue(labels['PoR status']['ok'], labels)
        self.assertTrue(labels['PoR SW']['ok'], labels)
        args = dec.call_args[0]
        self.assertEqual(args[4], '0000000A')       # the command's counter
        self.assertEqual(args[5], 'AA' * 16)        # the keyset's keys
        self.assertEqual(args[6], 'BB' * 16)
        self.assertEqual(args[-1], '027100027100aabb')

    def test_expect_por_check_without_a_prior_scp80_fails(self):
        scc = FakeScc()
        scc.push('80F2', '', '9102')
        scc.push('8012', self.SEND_SM_POR_CMD, '9000')
        scc.push('8014', '', '9000')
        run = self.run_script(FakeServer(scc), [
            {'type': 'action', 'kind': 'status', 'params': {'attempts': 2},
             'check': {'sw': {'mode': 'mask', 'value': '91??'}}},
            {'type': 'expect', 'command': 'SEND SHORT MESSAGE',
             'checks': [{'kind': 'por', 'status': 'por_ok'}],
             'respond': {'result': 'ok'}},
        ])
        self.assertIsNone(run['steps'][1].get('por'))
        por_check = [c for c in run['steps'][1]['checks'] if c['label'] == 'PoR'][0]
        self.assertFalse(por_check['ok'])
        self.assertIn('no PoR', por_check['actual'])

    def test_expect_checks_the_refresh_file_list(self):
        scc = FakeScc()
        scc.push('80F2', '', '9102')
        scc.push('8012', self.REFRESH_FCN_CMD, '9000')
        scc.push('8014', '', '9000')
        run = self.run_script(FakeServer(scc), [
            {'type': 'action', 'kind': 'status', 'params': {'attempts': 2},
             'check': {'sw': {'mode': 'mask', 'value': '91??'}}},
            {'type': 'expect', 'command': 'REFRESH', 'qualifier': '00',
             'checks': [{'kind': 'files', 'value': '3F007F106F3A'}],
             'respond': {'result': 'ok'}},
        ])
        self.assertEqual(run['status'], 'ok', run['steps'])
        labels = {c['label']: c for c in run['steps'][1]['checks']}
        self.assertTrue(labels['Command']['ok'], labels)
        self.assertTrue(labels['Qualifier']['ok'], labels)
        self.assertTrue(labels['Files']['ok'], labels)
        self.assertEqual(labels['Files']['actual'], '3F007F106F3A')

    # a DISPLAY TEXT carrying the Alpha identifier ('05' = 'Alfa') plus the
    # text string ('8D' = 'hello') - the alpha check must not read the text
    DISPLAY_TEXT_ALPHA_CMD = 'D0178103012100820281820504416C66618D060468656C6C6F'
    # a SEND SHORT MESSAGE with an SMS-SUBMIT: DA 12345, PID 7F, DCS 00,
    # UDL 5, UD 02 71 00 AA BB
    SEND_SM_SUBMIT_CMD = 'D01A8103011300820283810B0F010005812143F57F0005027100AABB'

    def test_expect_alpha_and_sms_checks(self):
        # the Alpha identifier ('05', TS 102 223 8.2) and the SEND SHORT
        # MESSAGE TPDU fields (an applet's own SMS, not the OTA PoR)
        scc = FakeScc()
        scc.push('80F2', '', '9102')
        scc.push('8012', self.DISPLAY_TEXT_ALPHA_CMD, '9000')
        scc.push('8014', '', '9000')
        scc.push('80F2', '', '9102')
        scc.push('8012', self.SEND_SM_SUBMIT_CMD, '9000')
        scc.push('8014', '', '9000')
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            run = self.run_script(FakeServer(scc), [
                {'type': 'action', 'kind': 'status', 'params': {'attempts': 2},
                 'check': {'sw': {'mode': 'mask', 'value': '91??'}}},
                {'type': 'expect', 'command': 'DISPLAY TEXT',
                 'checks': [{'kind': 'alpha', 'mode': 'exact', 'value': 'Alfa'}],
                 'respond': {'result': 'ok'}},
                {'type': 'action', 'kind': 'status', 'params': {'attempts': 2},
                 'check': {'sw': {'mode': 'mask', 'value': '91??'}}},
                {'type': 'expect', 'command': 'SEND SHORT MESSAGE',
                 'checks': [{'kind': 'sms', 'da': '12345', 'pid': '7F', 'dcs': '00',
                             'udl': 5, 'ud': '027100AABB'}],
                 'respond': {'result': 'ok'}},
            ])
        self.assertEqual(run['status'], 'ok', run['steps'])
        labels = {c['label']: c for c in run['steps'][1]['checks']}
        self.assertTrue(labels['Alpha']['ok'], labels)
        self.assertEqual(labels['Alpha']['actual'], 'Alfa')
        labels = {c['label']: c for c in run['steps'][3]['checks']}
        for label in ('SMS DA', 'SMS PID', 'SMS DCS', 'SMS UDL', 'SMS UD'):
            self.assertTrue(labels[label]['ok'], labels)
        self.assertEqual(run['steps'][3]['sms']['da'], '12345')
        log = buf.getvalue()
        self.assertIn('TEST-RUN step 4: SMS DA=12345 PID=7F DCS=00 UDL=5 UD=027100AABB', log)

    def test_expect_sms_check_mismatch_terminates(self):
        scc = FakeScc()
        scc.push('80F2', '', '9102')
        scc.push('8012', self.SEND_SM_SUBMIT_CMD, '9000')
        scc.push('8014', '', '9000')
        run = self.run_script(FakeServer(scc), [
            {'type': 'action', 'kind': 'status', 'params': {'attempts': 2},
             'check': {'sw': {'mode': 'mask', 'value': '91??'}}},
            {'type': 'expect', 'command': 'SEND SHORT MESSAGE',
             'checks': [{'kind': 'sms', 'dcs': 'AA'}], 'respond': {'result': 'ok'}},
        ])
        self.assertNotEqual(run['status'], 'ok')
        labels = {c['label']: c for c in run['steps'][1]['checks']}
        self.assertFalse(labels['SMS DCS']['ok'])
        self.assertEqual(labels['SMS DCS']['expected'], 'AA')
        self.assertEqual(labels['SMS DCS']['actual'], '00')

    # The live applet's SEND SHORT MESSAGE (captured 2026-10-04 20:58): the
    # Alpha identifier is Annex A Variant 2 Cyrillic ("Отправка...") and the
    # TPDU is a real SMS-SUBMIT to +79332505884 with 7-bit user data.
    APP_SEND_SM_CMD = ('D02F810301130082028183050E810B089EC2BFC0B0B2BAB02E2E2E'
                       '0B1401000B919733525088F40000084B1C12579C9D83')

    def test_expect_alpha_and_sms_checks_on_the_live_applet_sms(self):
        scc = FakeScc()
        scc.push('80F2', '', '9102')
        scc.push('8012', self.APP_SEND_SM_CMD, '9000')
        scc.push('8014', '', '9000')
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            run = self.run_script(FakeServer(scc), [
                {'type': 'action', 'kind': 'status', 'params': {'attempts': 2},
                 'check': {'sw': {'mode': 'mask', 'value': '91??'}}},
                {'type': 'expect', 'command': 'SEND SHORT MESSAGE',
                 'checks': [{'kind': 'alpha', 'mode': 'exact', 'value': 'Отправка...'},
                            {'kind': 'sms', 'da': '79332505884', 'pid': '00',
                             'dcs': '00', 'udl': 8, 'ud': '4B1C12579C9D83'}],
                 'respond': {'result': 'ok'}},
            ])
        self.assertEqual(run['status'], 'ok', run['steps'])
        labels = {c['label']: c for c in run['steps'][1]['checks']}
        self.assertTrue(labels['Alpha']['ok'], labels)
        self.assertEqual(labels['Alpha']['actual'], 'Отправка...')
        for label in ('SMS DA', 'SMS PID', 'SMS DCS', 'SMS UDL', 'SMS UD'):
            self.assertTrue(labels[label]['ok'], labels)
        self.assertEqual(run['steps'][1]['sms']['da'], '79332505884')
        log = buf.getvalue()
        self.assertIn('TEST-RUN step 2: SMS DA=79332505884 PID=00 DCS=00 UDL=8 '
                      'UD=4B1C12579C9D83', log)

    def test_scp80_format_wraps_the_apdu(self):
        # `format` wraps the C-APDU in the expanded Command Scripting template
        # (TS 102 226 5.2.1): `expanded` = AA/<len>/22/..., `expanded-ae` =
        # AE 80 ... 00 00.  A compact step keeps the APDU verbatim.
        for fmt, want in (('', '80E2900000'),
                          ('expanded', 'AA07220580E2900000'),
                          ('expanded-ae', 'AE80220580E29000000000')):
            scc = FakeScc()
            preset = {'kic': '15', 'kid': '15', 'kicKey': 'AA' * 16,
                      'kidKey': 'BB' * 16, 'counter': '00000001',
                      'tars': preset_tars()}
            params = {'apdu': '80E2900000'}
            if fmt:
                params['format'] = fmt
            buf = io.StringIO()
            with mock.patch.object(S, '_build_secured_packet',
                                   return_value=('AA' * 10, {})) as build, \
                    mock.patch.object(S, '_send_secured_packet',
                                      return_value={'success': True, 'sw': '9000',
                                                    'response_data': None}):
                with contextlib.redirect_stderr(buf):
                    run = self.run_script(FakeServer(scc), [
                        {'type': 'action', 'kind': 'scp80', 'params': params},
                    ], preset)
            self.assertEqual(run['status'], 'ok', run['steps'])
            self.assertEqual(build.call_args[0][6], want)
            log = buf.getvalue()
            if fmt:
                self.assertIn('TEST-RUN step 1: SCP80 C-APDU=80E2900000 '
                              'WRAPPED=%s (%s)' % (want, fmt), log)
                self.assertIn('C-APDU=%s format=%s' % (want, fmt),
                              run['steps'][0]['sent'])
            else:
                self.assertIn('TEST-RUN step 1: SCP80 C-APDU=80E2900000', log)
                self.assertNotIn('WRAPPED', log)
                self.assertNotIn('format=', run['steps'][0]['sent'])

    def test_scp80_inline_por_object_check(self):
        # `check.por` as an object asserts the decoded PoR (status/SW/data) of
        # this very exchange - inline here, the SEND SHORT MESSAGE transport
        # works the same
        decoded = {'response_status': 'por_ok', 'tar': '000000',
                   'cntr': '0000000A', 'raw': '0271000021AABB',
                   'decoded': {'last_status_word': '9000',
                               'last_response_data': 'AABB'}}
        preset = {'kic': '15', 'kid': '15', 'kicKey': 'AA' * 16,
                  'kidKey': 'BB' * 16, 'counter': '0000000A',
                  'tars': preset_tars()}
        with mock.patch.object(S, '_build_secured_packet', return_value=('AA' * 10, {})), \
                mock.patch.object(S, '_send_secured_packet',
                                  return_value={'success': True, 'sw': '9000',
                                                'response_data': '0271000021AABB'}), \
                mock.patch.object(S, '_decode_por', return_value=decoded):
            run = self.run_script(FakeServer(FakeScc()), [
                {'type': 'action', 'kind': 'scp80', 'params': {'apdu': '80E2900000'},
                 'check': {'por': {'status': 'por_ok', 'sw': '9000',
                                   'data': 'AABB'}}},
            ], preset)
        self.assertEqual(run['status'], 'ok', run['steps'])
        labels = {c['label']: c for c in run['steps'][0]['checks']}
        self.assertTrue(labels['PoR status']['ok'], labels)
        self.assertTrue(labels['PoR SW']['ok'], labels)
        self.assertTrue(labels['PoR data']['ok'], labels)
        # a mismatch fails the step; without a decoded PoR the check names it
        with mock.patch.object(S, '_build_secured_packet', return_value=('AA' * 10, {})), \
                mock.patch.object(S, '_send_secured_packet',
                                  return_value={'success': True, 'sw': '9000',
                                                'response_data': '0271000021AABB'}), \
                mock.patch.object(S, '_decode_por', return_value=decoded):
            run = self.run_script(FakeServer(FakeScc()), [
                {'type': 'action', 'kind': 'scp80', 'params': {'apdu': '80E2900000'},
                 'check': {'por': {'status': 'por_error'}}},
            ], preset)
        self.assertNotEqual(run['status'], 'ok')
        labels = {c['label']: c for c in run['steps'][0]['checks']}
        self.assertFalse(labels['PoR status']['ok'])
        self.assertEqual(labels['PoR status']['actual'], 'por_ok')
        with mock.patch.object(S, '_build_secured_packet', return_value=('AA' * 10, {})), \
                mock.patch.object(S, '_send_secured_packet',
                                  return_value={'success': True, 'sw': '9000',
                                                'response_data': None}):
            run = self.run_script(FakeServer(FakeScc()), [
                {'type': 'action', 'kind': 'scp80', 'params': {'apdu': '80E2900000'},
                 'check': {'por': {'status': 'por_ok'}}},
            ], preset)
        labels = {c['label']: c for c in run['steps'][0]['checks']}
        self.assertFalse(labels['PoR']['ok'])
        self.assertIn('no PoR', labels['PoR']['actual'])

    def test_parse_file_list_splits_concatenated_paths(self):
        raw = bytes.fromhex('D016810301010082028381120B023F002FE23F007F106F3A')
        self.assertEqual(S._parse_file_list(raw), ['3F002FE2', '3F007F106F3A'])
        self.assertEqual(S._parse_file_list(bytes.fromhex('D009810301010082028381')), [])

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
