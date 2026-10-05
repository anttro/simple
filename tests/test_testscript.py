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
import pysim_simple_server.events as events
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

    def test_proactive_action_normalises(self):
        script = T.normalise_script({'steps': [
            {'type': 'action', 'kind': 'proactive',
             'params': {'respond': {'result': 'ok'}, 'first': {'result': 'cancel'},
                        'attempts': 5, 'interval_ms': 0,
                        'require': {'command': 'REFRESH', 'qualifier': '00'}}},
        ]}, _resolver)
        p = script['steps'][0]['params']
        self.assertEqual(p['respond'], {'result': 0x00})
        self.assertEqual(p['first'], {'result': 0x10})
        self.assertEqual(p['attempts'], 5)
        self.assertEqual(p['interval_ms'], 0)
        self.assertEqual(p['require']['type'], 0x01)
        self.assertEqual(p['require']['name'], 'REFRESH')
        self.assertEqual(p['require']['qualifier'], {'mode': 'exact', 'value': '00'})
        # the defaults: ok / 3 attempts / 200 ms, no first, no require
        script = T.normalise_script({'steps': [
            {'type': 'action', 'kind': 'proactive'}]}, _resolver)
        p = script['steps'][0]['params']
        self.assertEqual(p['respond'], {'result': 0x00})
        self.assertNotIn('first', p)
        self.assertNotIn('require', p)
        self.assertEqual(p['attempts'], 3)
        self.assertEqual(p['interval_ms'], 200)
        # a bad require is refused
        for bad in ({'require': {'qualifier': '00'}},
                    {'require': 'REFRESH'},
                    {'require': {'command': 'NOT A COMMAND'}},
                    {'attempts': 0}):
            with self.assertRaises(T.ScriptError, msg=repr(bad)):
                T.normalise_script({'steps': [{'type': 'action', 'kind': 'proactive',
                                               'params': bad}]}, _resolver)

    def test_the_proactive_action_refuses_a_data_check(self):
        # The proactive result's data is the drained-command list, not a
        # single response: a `data` check would be silently ignored, so the
        # engine refuses it (assert what was drained with `require`).
        with self.assertRaises(T.ScriptError) as cm:
            T.normalise_script({'steps': [
                {'type': 'action', 'kind': 'proactive',
                 'check': {'data': 'AABB'}}]}, _resolver)
        self.assertIn('check.data is not used by the proactive action',
                      str(cm.exception))

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

    def test_event_action_accepts_the_form_built_data(self):
        # The events events.py does not model carry the PWA form's built hex;
        # a modelled event builds from `fields` (a stray data hex is ignored).
        script = T.normalise_script({'steps': [
            {'type': 'action', 'kind': 'event',
             'params': {'event': '0x00', 'data': '1c0100', 'src': '83'}}]}, _resolver)
        self.assertEqual(script['steps'][0]['params'],
                         {'event': 0x00, 'data': '1C0100', 'src': '83'})
        script = T.normalise_script({'steps': [
            {'type': 'action', 'kind': 'event',
             'params': {'event': '0x03', 'fields': {'status': 1}, 'data': 'AA'}}]}, _resolver)
        self.assertNotIn('data', script['steps'][0]['params'])
        self.assertEqual(script['steps'][0]['params']['fields'], {'status': 1})
        # missing, malformed and oversized data are refused before the run
        for bad in ({'event': '0x00'},
                    {'event': '0x00', 'data': 'ABC'},
                    {'event': '0x00', 'data': 'AA' * (events.EVENT_DATA_MAX + 1)}):
            with self.assertRaises(T.ScriptError):
                T.normalise_script({'steps': [{'type': 'action', 'kind': 'event',
                                               'params': bad}]}, _resolver)

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
        # a leading '+' (the TPDU keeps the international flag in its
        # type-of-number) and whitespace are dropped from `da`
        script = T.normalise_script({'steps': [
            {'type': 'expect', 'command': 'SEND SHORT MESSAGE',
             'checks': [{'kind': 'sms', 'da': '+79 332 505 884'}]},
        ]}, _resolver)
        self.assertEqual(script['steps'][0]['checks'][0]['da'], '79332505884')
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
        self.card_present = True
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
                            'started': time.time(), 'finished': None, 'error': None,
                            'suite': None, 'log': [], 'log_prefix': '', 'adm': None})
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
        self.assertEqual(run['steps'][0]['status'], 'error')
        check = run['steps'][0]['checks'][0]
        self.assertEqual(check['label'], 'Command')
        self.assertFalse(check['ok'])
        self.assertEqual(check['expected'], 'DISPLAY TEXT')
        self.assertEqual(check['actual'], '(none)')
        self.assertIn('no proactive command pending', check['detail'])

    def test_expectation_warning_without_pending_continues(self):
        # on_fail "warning" is the tolerant consume-if-pending form (v3.21.0):
        # the empty poll warns and the script continues
        scc = FakeScc()
        scc.push('80F2', '', '9000')
        run = self.run_script(FakeServer(scc), [
            {'type': 'expect', 'command': 'ANY', 'respond': {},
             'on_fail': 'warning'},
            {'type': 'action', 'kind': 'status', 'params': {}},
        ])
        self.assertEqual(run['status'], 'warning')
        self.assertEqual(len(run['steps']), 2)
        self.assertEqual(run['steps'][0]['status'], 'warning')
        self.assertIn('no proactive command pending',
                      run['steps'][0]['checks'][0]['detail'])

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
        # the drain FETCHed the command and answered with an ok TR (0x00,
        # v3.21.0: a cancel told the card the user aborted the session)
        self.assertTrue(any(a.startswith('8012') for a in scc.sent[1:]))
        self.assertTrue(any('83020000' in a for a in scc.sent))
        self.assertIn('TR ok', run['steps'][1]['sent'])

    def test_drain_answers_ok_and_follows_the_next_command(self):
        # answering one command can make the application emit another; the
        # drain keeps fetching while the TR answers 91XX (bounded, v3.21.0)
        scc = FakeScc()
        scc.push('80C2', '', '9103')                     # menu selection
        scc.push('8012', SELECT_ITEM_CMD, '9000')        # FETCH #1
        scc.push('8014', '', '9102')                     # TR -> another pending
        scc.push('8012', DISPLAY_TEXT_CMD, '9000')       # FETCH #2
        scc.push('8014', '', '9000')                     # TR -> idle
        run = self.run_script(FakeServer(scc), [
            {'type': 'action', 'kind': 'menu-select', 'params': {'item_id': 1},
             'check': {'sw': '91??'}},
            {'type': 'action', 'kind': 'apdu', 'params': {'apdu': '00A40000023F00'}},
        ])
        self.assertEqual(run['status'], 'error')   # the unexpected pending stops the script
        entry = run['steps'][1]
        self.assertEqual(entry['sent'].count('TR ok'), 2)
        self.assertEqual(entry['sw'], '9000')
        self.assertIn(SELECT_ITEM_CMD, entry['data'])

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


class TestRunnerProactive(RunnerTestCase):
    """The proactive cleanup action (v3.21.0): consume whatever the card
    announces, answer it and confirm the card is idle."""

    def test_empty_drain_passes(self):
        # the point of the action: an empty first poll is fine
        scc = FakeScc()
        scc.push('80F2', '', '9000')
        run = self.run_script(FakeServer(scc), [
            {'type': 'action', 'kind': 'proactive', 'params': {}},
        ])
        self.assertEqual(run['status'], 'ok', run['steps'])
        entry = run['steps'][0]
        self.assertEqual(entry['status'], 'ok')
        self.assertEqual(entry['sent'], 'STATUS x1; TR x0')
        self.assertEqual(entry['drained'], [])
        self.assertEqual(entry['checks'][0]['label'], 'SW')
        self.assertTrue(entry['checks'][0]['ok'])

    def test_drains_a_pending_command_and_confirms_idle(self):
        scc = FakeScc()
        scc.push('80F2', '', '9102')          # STATUS: DISPLAY TEXT pending
        scc.push('8012', DISPLAY_TEXT_CMD, '9000')
        scc.push('8014', '', '9000')          # TR ok
        scc.push('80F2', '', '9000')          # confirming STATUS: idle
        run = self.run_script(FakeServer(scc), [
            {'type': 'action', 'kind': 'proactive', 'params': {'attempts': 3, 'interval_ms': 0}},
        ])
        self.assertEqual(run['status'], 'ok', run['steps'])
        entry = run['steps'][0]
        self.assertEqual(entry['sent'], 'STATUS x2; TR x1')
        self.assertEqual(len(entry['drained']), 1)
        self.assertEqual(entry['drained'][0]['type_name'], 'DISPLAY TEXT')
        self.assertEqual(entry['drained'][0]['qualifier'], '00')
        self.assertTrue(any('83020000' in a for a in scc.sent))

    def test_require_matches_a_drained_command(self):
        scc = FakeScc()
        scc.push('80F2', '', '9102')
        scc.push('8012', DISPLAY_TEXT_CMD, '9000')
        scc.push('8014', '', '9000')
        scc.push('80F2', '', '9000')
        run = self.run_script(FakeServer(scc), [
            {'type': 'action', 'kind': 'proactive',
             'params': {'attempts': 3, 'interval_ms': 0, 'require': {'command': 'DISPLAY TEXT'}}}, 
        ])
        self.assertEqual(run['status'], 'ok', run['steps'])
        req = [c for c in run['steps'][0]['checks'] if c['label'] == 'Require'][0]
        self.assertTrue(req['ok'])

    def test_require_fails_on_a_mismatch(self):
        scc = FakeScc()
        scc.push('80F2', '', '9102')
        scc.push('8012', DISPLAY_TEXT_CMD, '9000')
        scc.push('8014', '', '9000')
        scc.push('80F2', '', '9000')
        run = self.run_script(FakeServer(scc), [
            {'type': 'action', 'kind': 'proactive',
             'params': {'attempts': 3, 'interval_ms': 0,
                        'require': {'command': 'REFRESH', 'qualifier': '00'}}}, 
        ])
        self.assertEqual(run['status'], 'error')
        req = [c for c in run['steps'][0]['checks'] if c['label'] == 'Require'][0]
        self.assertFalse(req['ok'])
        self.assertIn('DISPLAY TEXT', req['actual'])

    def test_require_fails_on_an_empty_drain(self):
        scc = FakeScc()
        scc.push('80F2', '', '9000')
        run = self.run_script(FakeServer(scc), [
            {'type': 'action', 'kind': 'proactive',
             'params': {'require': {'command': 'REFRESH'}}},
        ])
        self.assertEqual(run['status'], 'error')
        req = [c for c in run['steps'][0]['checks'] if c['label'] == 'Require'][0]
        self.assertEqual(req['actual'], '(nothing was pending)')

    def test_first_uses_a_different_result(self):
        scc = FakeScc()
        scc.push('80F2', '', '9102')          # STATUS: DISPLAY TEXT pending
        scc.push('8012', DISPLAY_TEXT_CMD, '9000')
        scc.push('8014', '', '9102')          # TR cancel -> another pending
        scc.push('80F2', '', '9102')          # STATUS: SELECT ITEM pending
        scc.push('8012', SELECT_ITEM_CMD, '9000')
        scc.push('8014', '', '9000')          # TR ok
        scc.push('80F2', '', '9000')          # confirming STATUS
        run = self.run_script(FakeServer(scc), [
            {'type': 'action', 'kind': 'proactive',
             'params': {'attempts': 4, 'interval_ms': 0, 'respond': {'result': 'ok'},
                        'first': {'result': 'cancel'}}},
        ])
        self.assertEqual(run['status'], 'ok', run['steps'])
        self.assertEqual(len(run['steps'][0]['drained']), 2)
        self.assertTrue(any('83021000' in a for a in scc.sent))   # the first TR
        self.assertTrue(any('83020000' in a for a in scc.sent))   # the second

    def test_bound_leaves_the_pending_for_the_next_step(self):
        scc = FakeScc()
        scc.push('80F2', '', '9102')
        scc.push('8012', DISPLAY_TEXT_CMD, '9000')
        scc.push('8014', '', '9102')          # another command pending
        run = self.run_script(FakeServer(scc), [
            {'type': 'action', 'kind': 'proactive', 'params': {'attempts': 1}},
        ])
        # the bound stopped with 91XX: the SW check fails and the pending is
        # handed to the next step
        self.assertEqual(run['status'], 'error')
        self.assertEqual(run['steps'][0]['sw'], '9102')
        self.assertFalse(run['steps'][0]['checks'][0]['ok'])


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
        self.assertIn('TEST-RUN step 1: ENVELOPE(Event Download) type=0x03 '                      '(location_status) data=9B0100930752F01000FF0001 src=82 -> SW=9000',
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

    def test_event_action_sends_the_form_built_data(self):
        # The events events.py does not model (e.g. MT call) run with the
        # PWA form's built hex and its source (network).
        scc = FakeScc()
        scc.push('80C2', '', '9000')
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            run = self.run_script(FakeServer(scc), [
                {'type': 'action', 'kind': 'event',
                 'params': {'event': '0x00', 'data': '1C0100', 'src': '83'}},
            ])
        self.assertEqual(run['status'], 'ok', run['steps'])
        self.assertIn('99010082028381' + '1C0100', scc.sent[0].upper())
        self.assertIn('type=0x00 (0x00) data=1C0100 src=83 -> SW=9000',
                      buf.getvalue())

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
        self.assertEqual(raw, {'name': 'stored', 'steps': entry['steps'],
                               'require_adm': False})
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
                            {'kind': 'sms', 'da': '+79 332 505 884', 'pid': '00',
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

    # the live alfa-dsa applet's response packet (2026-10-04): the secured
    # data `80 01 10` is the applet's own - the compact structure's command
    # count (0x80) cannot fit the 1-byte command script
    APP_POR_RAW = '027100000e0aaf4d0100020000190000800110'

    def test_scp80_por_object_check_on_an_applet_response(self):
        # the live alfa-dsa applet's response (2026-10-04): the secured data
        # `80 01 10` is the applet's own, so the compact parse must not claim
        # 128 commands - the runner exposes it as the PoR data
        preset = {'kic': '15', 'kid': '15', 'kicKey': 'AA' * 16,
                  'kidKey': 'BB' * 16, 'counter': '00000001',
                  'tars': preset_tars()}
        buf = io.StringIO()
        with mock.patch.object(S, '_build_secured_packet', return_value=('AA' * 10, {})), \
                mock.patch.object(S, '_send_secured_packet',
                                  return_value={'success': True, 'sw': '9000',
                                                'response_data': self.APP_POR_RAW}):
            with contextlib.redirect_stderr(buf):
                run = self.run_script(FakeServer(FakeScc()), [
                    {'type': 'action', 'kind': 'scp80', 'params': {'apdu': '01'},
                     'check': {'por': {'status': 'por_ok', 'data': '800110'}}},
                ], preset)
        self.assertEqual(run['status'], 'ok', run['steps'])
        labels = {c['label']: c for c in run['steps'][0]['checks']}
        self.assertTrue(labels['PoR status']['ok'], labels)
        self.assertTrue(labels['PoR data']['ok'], labels)
        self.assertEqual(run['steps'][0]['por']['response_type'], 'raw')
        # the log names application data instead of inventing an R-APDU line
        self.assertIn('APP DATA=800110 (no R-APDU SW)', buf.getvalue())
        self.assertNotIn('R-APDU SW=', buf.getvalue())

    def test_scp80_por_sw_check_on_applet_data_reports_the_hint(self):
        # a `por.sw` expectation cannot be satisfied by an applet's own TAR -
        # the report explains why on the check row (the live confusion)
        preset = {'kic': '15', 'kid': '15', 'kicKey': 'AA' * 16,
                  'kidKey': 'BB' * 16, 'counter': '00000001',
                  'tars': preset_tars()}
        with mock.patch.object(S, '_build_secured_packet', return_value=('AA' * 10, {})), \
                mock.patch.object(S, '_send_secured_packet',
                                  return_value={'success': True, 'sw': '9000',
                                                'response_data': self.APP_POR_RAW}):
            run = self.run_script(FakeServer(FakeScc()), [
                {'type': 'action', 'kind': 'scp80', 'params': {'apdu': '01'},
                 'check': {'por': {'sw': '9000'}}},
            ], preset)
        self.assertNotEqual(run['status'], 'ok')
        sw_check = [c for c in run['steps'][0]['checks'] if c['label'] == 'PoR SW'][0]
        self.assertFalse(sw_check['ok'])
        self.assertIn('application data', sw_check.get('detail') or '')
        self.assertIn("assert it with 'data'", sw_check.get('detail') or '')

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


OK_STEP = {'type': 'action', 'kind': 'apdu', 'params': {'apdu': '00A40000023F00'},
           'check': {'sw': '9000'}}
BAD_STEP = {'type': 'action', 'kind': 'apdu', 'params': {'apdu': '00B0000000'},
            'check': {'sw': '9000'}}   # no queue -> 6D00


class SuiteRunnerTestCase(RunnerTestCase):
    """Suite runner tests (v3.22.0): setup -> members (per-member on_fail) ->
    teardown, one card session, no resume."""

    def members(self, specs):
        """specs: [(role, ok, on_fail)] -> the resolved member list."""
        out = []
        for i, spec in enumerate(specs):
            role, ok = spec[0], spec[1]
            on_fail = spec[2] if len(spec) > 2 else 'stop'
            step = dict(OK_STEP if ok else BAD_STEP)
            out.append(({'script_id': '%032x' % (i + 1), 'role': role,
                         'on_fail': on_fail},
                        T.normalise_script({'name': 'script %d' % (i + 1),
                                            'steps': [step]}, S._test_command_type)))
        return out

    def suite_run(self, server, members, suite=None, preset=None, preset_id=None):
        suite = suite or {'name': 'suite'}
        S._TEST_RUN.clear()
        S._TEST_RUN.update({'running': True, 'stop': False, 'name': 'suite',
                            'status': None, 'session': server.card_session,
                            'index': 0, 'total': 0, 'steps': [],
                            'started': time.time(), 'finished': None, 'error': None,
                            'scp80_counter': None, 'scp80_counters': {},
                            'script_id': None, 'preset': None,
                            'suite': S._test_suite_state(suite, members,
                                                         preset or {}, preset_id,
                                                         server.card_session),
                            'log': [], 'log_prefix': '', 'adm': None})
        S._TEST_RUNNING = True
        try:
            S._test_suite_execute(server, suite, members, preset or {}, preset_id)
        finally:
            S._TEST_RUNNING = False
        return S._TEST_RUN

    def push_ok(self, scc, n):
        for _ in range(n):
            scc.push('00A4', '', '9000')


class TestSuiteRunner(SuiteRunnerTestCase):
    def test_setup_members_teardown_run_in_order(self):
        scc = FakeScc()
        self.push_ok(scc, 4)
        members = self.members([('setup', True), ('member', True),
                                ('member', True), ('teardown', True)])
        run = self.suite_run(FakeServer(scc), members)
        self.assertEqual(run['status'], 'ok')
        state = run['suite']
        self.assertEqual([m['role'] for m in state['members']],
                         ['setup', 'member', 'member', 'teardown'])
        self.assertEqual([m['status'] for m in state['members']],
                         ['ok', 'ok', 'ok', 'ok'])
        self.assertEqual(state['summary']['setup'], 'ok')
        self.assertEqual(state['summary']['teardown'], 'ok')
        self.assertEqual(state['summary']['ok'], 4)
        self.assertEqual(state['summary']['error'], 0)
        self.assertFalse(state['summary']['stopped'])
        self.assertEqual(state['summary']['counters_before'], {})
        # the member steps are copied into the report (the live view is reset
        # between members)
        self.assertEqual(len(state['members'][1]['steps']), 1)
        self.assertEqual(state['members'][1]['steps'][0]['status'], 'ok')
        # the run log carries the member prefix and is sliced per member
        joined = '\n'.join(state['members'][1]['log'])
        self.assertIn('[2/4 member script 2]', joined)

    def test_on_fail_stop_skips_the_rest_and_still_runs_teardown(self):
        scc = FakeScc()
        self.push_ok(scc, 1)                       # setup ok
        self.push_ok(scc, 1)                       # first member ok
        # the failing member gets no queue entry -> 6D00 (error)
        self.push_ok(scc, 1)                       # teardown ok
        members = self.members([('setup', True), ('member', True),
                                ('member', False, 'stop'), ('member', True),
                                ('teardown', True)])
        run = self.suite_run(FakeServer(scc), members)
        self.assertEqual(run['status'], 'error')
        statuses = [m['status'] for m in run['suite']['members']]
        self.assertEqual(statuses, ['ok', 'ok', 'error', 'skipped', 'ok'])
        self.assertEqual(run['suite']['members'][3]['note'],
                         'a previous script failed (on_fail: stop)')
        # no member ran after the error: the queue holds exactly the teardown's
        self.assertEqual(sum(1 for a in scc.sent if a.startswith('00A4')), 3)

    def test_on_fail_continue_runs_the_next_member(self):
        scc = FakeScc()
        self.push_ok(scc, 1)                       # member 1 ok
        # member 2 fails (no queue entry)
        self.push_ok(scc, 1)                       # member 3 ok
        members = self.members([('member', True), ('member', False, 'continue'),
                                ('member', True)])
        run = self.suite_run(FakeServer(scc), members)
        self.assertEqual(run['status'], 'error')   # the failure is reported
        self.assertEqual([m['status'] for m in run['suite']['members']],
                         ['ok', 'error', 'ok'])

    def test_setup_failure_skips_members_but_runs_teardown(self):
        scc = FakeScc()
        # setup fails (no queue), members never run, teardown ok
        self.push_ok(scc, 1)
        members = self.members([('setup', False), ('member', True),
                                ('teardown', True)])
        run = self.suite_run(FakeServer(scc), members)
        self.assertEqual([m['status'] for m in run['suite']['members']],
                         ['error', 'skipped', 'ok'])
        self.assertEqual(run['suite']['members'][1]['note'],
                         'the setup script failed')

    def test_stop_skips_the_members_and_runs_the_teardown(self):
        scc = FakeScc()
        self.push_ok(scc, 1)                       # setup ok (runs first)
        self.push_ok(scc, 1)                       # teardown ok
        members = self.members([('setup', True), ('member', True),
                                ('teardown', True)])
        server = FakeServer(scc)
        real = S._test_run_script
        calls = []

        def wrapped(srv, script, preset, shared=None):
            ctx, stopped = real(srv, script, preset, shared=shared)
            calls.append(script['name'])
            if srv is server and len(calls) == 1:
                S._TEST_RUN['stop'] = True         # the operator stops here
            return ctx, stopped

        with mock.patch.object(S, '_test_run_script', wrapped):
            run = self.suite_run(server, members)
        statuses = [m['status'] for m in run['suite']['members']]
        self.assertEqual(statuses, ['ok', 'skipped', 'ok'])
        self.assertTrue(run['suite']['summary']['stopped'])
        self.assertEqual(run['status'], 'stopped')

    def test_a_card_reset_stops_the_suite_and_flags_it(self):
        scc = FakeScc()
        self.push_ok(scc, 1)                       # member 1 ok
        self.push_ok(scc, 1)                       # teardown ok (new session)
        members = self.members([('member', True), ('member', True),
                                ('teardown', True)])
        server = FakeServer(scc)
        real = S._test_run_script

        def wrapped(srv, script, preset, shared=None):
            ctx, stopped = real(srv, script, preset, shared=shared)
            if srv is server and len(scc.sent) <= 1:
                srv.card_session += 1              # the card reset mid-suite
            return ctx, stopped

        with mock.patch.object(S, '_test_run_script', wrapped):
            run = self.suite_run(server, members)
        state = run['suite']
        self.assertEqual([m['status'] for m in state['members']],
                         ['ok', 'skipped', 'ok'])
        self.assertEqual(state['members'][1]['note'],
                         'card session changed (card removed or re-equipped)')
        self.assertTrue(state['session_changed'])
        self.assertEqual(state['session_start'], 1)
        self.assertEqual(state['session_end'], 2)
        self.assertTrue(state['summary']['session_changed'])

    def test_teardown_is_skipped_when_the_card_is_gone(self):
        scc = FakeScc()
        self.push_ok(scc, 1)
        members = self.members([('member', True), ('teardown', True)])
        server = FakeServer(scc)
        server.card_present = False
        run = self.suite_run(server, members)
        self.assertEqual([m['status'] for m in run['suite']['members']],
                         ['ok', 'skipped'])
        self.assertEqual(run['suite']['members'][1]['note'],
                         'no card in the reader')

    def test_scp80_members_share_the_counter_state(self):
        # The counter is per keyset and per card session: the second SCP80
        # script must send what the first consumed + 1, not the suite-start
        # snapshot again (the store persists each packet's counter, but the
        # in-memory preset copy does not move).
        built = []
        server = FakeServer(FakeScc())
        step = {'type': 'action', 'kind': 'scp80',
                'params': {'apdu': '80E2900000', 'tar': 'B00000'}}
        script = T.normalise_script({'name': 'scp80', 'steps': [step]},
                                    S._test_command_type)
        preset = {'id': 'p1',
                  'keysets': [{'kic': '15', 'kid': '15', 'kicKey': '00' * 16,
                               'kidKey': '00' * 16, 'cntr': '0000000010'}],
                  'tars': [{'role': 'isd', 'tar': 'B00000', 'msl': '16'}]}
        members = [({'script_id': 'a' * 32, 'role': 'member', 'on_fail': 'stop'}, script),
                   ({'script_id': 'b' * 32, 'role': 'member', 'on_fail': 'stop'}, script)]
        with mock.patch.object(S, '_build_secured_packet',
                               side_effect=lambda *a, **k: (
                                   built.append(a[5]) or ('00' * 20, {}))), \
             mock.patch.object(S, '_send_secured_packet', return_value={
                 'success': True, 'sw': '9000', 'response_data': '',
                 'bytes': 10, 'segments': 1}), \
             mock.patch.object(S, '_decode_por', return_value=None), \
             mock.patch.object(S, '_preset_counter_persist', return_value=None):
            run = self.suite_run(server, members, preset=preset)
        self.assertEqual(built, ['0000000010', '0000000011'])
        self.assertEqual([m['status'] for m in run['suite']['members']], ['ok', 'ok'])
        self.assertEqual(run['scp80_counters'], {1: '0000000012'})


class TestAdmGate(unittest.TestCase):
    """The run prerequisite: a script/suite flagged `require_adm` runs only
    with the ADM verified for the current card session - the preset's key is
    tried once, never retried automatically."""

    def setUp(self):
        self.server = FakeServer(FakeScc())
        self.server.app = object()

    def test_not_needed_passes(self):
        ok, info, err = S._ensure_adm(self.server, {}, False)
        self.assertTrue(ok)
        self.assertIsNone(err)
        self.assertFalse(info['required'])

    def test_verified_latch_passes(self):
        self.server.adm_session = 1
        ok, info, err = S._ensure_adm(self.server, {}, True)
        self.assertTrue(ok)
        self.assertTrue(info['verified'])

    def test_verifies_once_with_the_preset_key(self):
        with mock.patch.object(S, '_verify_adm', return_value={'ok': True,
                                                               'sw': '9000'}) as v:
            ok, info, err = S._ensure_adm(self.server, {'adm': 'AABBCCDD'}, True)
        self.assertTrue(ok, err)
        self.assertTrue(info['verified'])
        self.assertTrue(info['checked'])
        self.assertEqual(self.server.adm_session, self.server.card_session)
        v.assert_called_once()
        # the second gate in the same session does not verify again
        with mock.patch.object(S, '_verify_adm', return_value={'ok': True}) as v2:
            ok, _info, _err = S._ensure_adm(self.server, {'adm': 'AABBCCDD'}, True)
        self.assertTrue(ok)
        v2.assert_not_called()

    def test_a_failure_is_not_retried_automatically(self):
        with mock.patch.object(S, '_verify_adm', return_value={
                'ok': False, 'sw': '63C2', 'attempts_left': 2}) as v:
            ok, info, err = S._ensure_adm(self.server, {'adm': 'AABBCCDD'}, True)
        self.assertFalse(ok)
        self.assertIn('63C2', err)
        self.assertIn('2 attempt(s) left', err)
        self.assertEqual(info['attempts_left'], 2)
        self.assertEqual(self.server.adm_failed_session, self.server.card_session)
        # the next gate refuses without touching the card again
        with mock.patch.object(S, '_verify_adm') as v2:
            ok, _info, err = S._ensure_adm(self.server, {'adm': 'AABBCCDD'}, True)
        self.assertFalse(ok)
        self.assertIn('verify it manually', err)
        v2.assert_not_called()

    def test_no_key_in_the_preset_refuses(self):
        ok, _info, err = S._ensure_adm(self.server, {}, True)
        self.assertFalse(ok)
        self.assertIn('no ADM key', err)

    def test_a_new_session_clears_the_failure_stamp(self):
        self.server.adm_failed_session = 1
        self.server.card_session = 2
        with mock.patch.object(S, '_verify_adm', return_value={'ok': True}):
            ok, _info, _err = S._ensure_adm(self.server, {'adm': 'AABBCCDD'}, True)
        self.assertTrue(ok)


if __name__ == '__main__':
    unittest.main()
