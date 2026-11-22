# coding=utf-8
"""Tests for the pysim-free server's CAT/Simulator endpoints (Phase 3c-3e-1).

The endpoints ride the ported :class:`simple_server.cat_session.CatSession`, so
these tests drive the whole conversation through a scripted fake card (a queue
of TERMINAL PROFILE / FETCH / TERMINAL RESPONSE / STATUS / ENVELOPE answers)
and the HTTP endpoints through a real :class:`ThreadingHTTPServer` on an
ephemeral loopback port.  No card and no pysim are involved.

The legacy reference is ``pysim_simple_server/server.py``'s handlers
(``/api/menu``, ``/api/menu-select|respond``, ``/api/stk-status``,
``/api/events|event-send``, ``/api/terminal-profile``, ``/api/rescue``,
``/api/status-poll``, ``/api/poll-status|toggle``, ``/api/proactive-log``,
``/api/pli-qualifiers|dict`` and ``/api/apdu``); the byte parity of the ported
helpers lives in ``tests/test_simple_drift.py``.

Spec anchors: TS 102 221 §11.2.1.2 (TERMINAL PROFILE), §11.2.2 (ENVELOPE),
§11.2.3/§11.2.4 (FETCH / TERMINAL RESPONSE), §14.6.2 (the idle poll);
TS 102 223 §6.3 (the 91XX status), §6.4.6/§6.4.14 (POLL INTERVAL / POLLING
OFF), §6.4.16 (SET UP EVENT LIST), §6.6.7 (the SET UP MENU alpha identifier,
**mandatory** - a card that omits it is tolerated as a guarded fallback),
§6.8.0/§6.8.1 (the TERMINAL RESPONSE structure and echoed command
details), §7.5 (event downloads), §7.5.22/§8.97 (Poll Interval Negotiation),
§8.7 (device identities), §8.12.0 (the result codings), §8.15/§6.8.5 (the Text
string codings).
"""

import json
import pathlib
import shutil
import socket
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from pysim_simple_server import events

from simple_server import cat_session
from simple_server import proactive
from simple_server.server import (ServerState, build_http_server,
                                  resolve_menu_item, validate_terminal_profile)
from simple_server.transport import ReaderError

ATR = bytes.fromhex('3b9f95801fc78031e073fe211b67a3a0001505017204')

# '8970119000004600098' as EF.ICCID (TS 102 221 §13.2): nibble-swapped
# digits, the odd-nibble F pad last.
ICCID_DIGITS = '8970119000004600098'
ICCID_HEX = '980711090000640090f8'

SELECT_MF = bytes.fromhex('00A40004023F0000')
SELECT_ICCID = bytes.fromhex('00A40004022FE200')
STATUS_POLL = bytes.fromhex('80F2000C00')


def command(cmd_type, qualifier=0x00, extras=b'', cmd_num=1):
    """A D0-wrapped proactive command (CR-set command details, UICC -> terminal)."""
    body = (bytes([0x81, 0x03, cmd_num, cmd_type, qualifier])
            + bytes([0x82, 0x02, 0x83, 0x81]) + extras)
    return bytes([0xD0, len(body)]) + body


def menu_command(title=b'Alfa Mobile', items=None, cmd_num=1):
    """A SET UP MENU with the CR-set title ``85`` and plain ``8F`` items."""
    if items is None:
        items = ((0x80, b'Settings'), (0x81, b'Coffe'))
    extras = b''
    if title is not None:
        title = title.encode('utf-8') if isinstance(title, str) else title
        extras += bytes([0x85, len(title)]) + title
    for item_id, text in items:
        extras += bytes([0x8F, len(text) + 1, item_id]) + text
    return command(0x25, 0x00, extras, cmd_num=cmd_num)


#: SET UP EVENT LIST subscribing to events 04 and 05 (tag 99).
EVENT_LIST_COMMAND = command(0x05, 0x00, bytes([0x99, 0x02, 0x04, 0x05]))

#: DISPLAY TEXT with the SMS default alphabet 'A' (8D 02 04 41).
DISPLAY_TEXT = bytes.fromhex('d00d8103012100820281828d020441')

#: SELECT ITEM with two items ('One' id 1, 'Sub' id 2).
SELECT_ITEM = command(0x24, 0x00,
                      bytes([0x8F, 0x04, 0x01]) + b'One'
                      + bytes([0x8F, 0x04, 0x02]) + b'Sub')

#: GET INPUT, digits + hidden (qualifier 04), 4..8 digits, prompt 'PIN'.
GET_INPUT = command(0x23, 0x04,
                    bytes([0x8D, 0x04, 0x04]) + b'PIN'
                    + bytes([0x91, 0x02, 0x04, 0x08]))


class FakePresetStore(object):
    """The ServerState seam for the card preset store (no home-directory I/O)."""

    path = '<fake>'

    def list(self):
        return []

    def get(self, preset_id):
        return None


class CatCard(object):
    """A scripted UICC with a CAT session behind it.

    The init reads (the ADF.USIM probe, SELECT MF, EF.ICCID, the EF.ARR/PBR
    caches) answer like a minimal UICC, so :meth:`ServerState.open` runs the
    real sequence; the CAT answers are queues the tests fill: ``fetches``
    (the raw proactive commands FETCH returns), ``trs`` (the TERMINAL
    RESPONSE status words), ``statuses`` (STATUS status words) and the
    ENVELOPE answer (``envelope_sw``/``envelope_data``).  ``tp_sw`` is the
    TERMINAL PROFILE's answer.
    """

    def __init__(self, fetches=None):
        self.atr = ATR
        self.reader_name = 'Fake CAT reader'
        self.protocol = 1
        self.sent = []
        self.fetches = list(fetches or [])
        self.trs = []
        self.statuses = []
        self.tp_sw = '9000'
        self.envelope_sw = '9000'
        self.envelope_data = b''
        self.iccid = ICCID_HEX
        self.resets = 0
        self.connected = False

    # -- the transport shape ------------------------------------------------
    def reset_card(self):
        self.resets += 1
        self.connected = True
        return 1

    def connect(self):
        self.connected = True
        return self

    def disconnect(self):
        self.connected = False

    def get_atr(self):
        return self.atr

    def send_apdu(self, apdu):
        apdu = bytes(apdu)
        self.sent.append(apdu)
        answer = self._answer(apdu)
        return answer if answer is not None else (b'', b'\x6A\x82')

    def sent_hex(self):
        return [apdu.hex().upper() for apdu in self.sent]

    def count(self, prefix):
        prefix = prefix.upper()
        return sum(1 for apdu in self.sent_hex() if apdu.startswith(prefix))

    # -- the card -----------------------------------------------------------
    def _answer(self, apdu):
        if apdu[:2] == b'\x80\x10':                     # TERMINAL PROFILE
            return b'', bytes.fromhex(self.tp_sw)
        if apdu[:2] == b'\x80\x12':                     # FETCH
            raw = self.fetches.pop(0) if self.fetches else b''
            return raw, b'\x90\x00'
        if apdu[:2] == b'\x80\x14':                     # TERMINAL RESPONSE
            sw = self.trs.pop(0) if self.trs else '9000'
            return b'', bytes.fromhex(sw)
        if apdu[:2] == b'\x80\xF2':                     # STATUS
            sw = self.statuses.pop(0) if self.statuses else '9000'
            return b'', bytes.fromhex(sw)
        if apdu[:2] == b'\x80\xC2':                     # ENVELOPE
            return self.envelope_data, bytes.fromhex(self.envelope_sw)
        if apdu[:2] == b'\x00\xA4':                     # SELECT
            if apdu[2] == 0x04:                         # by DF name (an AID)
                return b'', b'\x6A\x82'
            fid = apdu[5:7].hex().upper()
            if fid in ('3F00', '2FE2'):
                return b'', b'\x90\x00'
            return b'', b'\x6A\x82'
        if apdu[:2] == b'\x00\xB0':                     # READ BINARY
            return bytes.fromhex(self.iccid), b'\x90\x00'
        return None


#: A throw-away root for the test-script/-suite stores (never the user's real
#: ``~/.pysim-simple-server`` files).  ``tearDownModule`` removes it, so the
#: suite leaves no temp directory behind.
_STORE_ROOT = pathlib.Path(tempfile.mkdtemp(prefix='simple-simulator-'))
_STORE_COUNTER = [0]


def tearDownModule():
    """Remove the throw-away store root (no temp-dir leak per run)."""
    shutil.rmtree(_STORE_ROOT, ignore_errors=True)


def _store_path(name):
    _STORE_COUNTER[0] += 1
    return _STORE_ROOT / ('%s-%d.json' % (name, _STORE_COUNTER[0]))


def make_state(**kwargs):
    """A cardless ServerState with the test seams set."""
    kwargs.setdefault('monitor', False)
    kwargs.setdefault('presets', FakePresetStore())
    kwargs.setdefault('readers_fn', lambda: ['Reader A'])
    kwargs.setdefault('poll_interval', 30)
    kwargs.setdefault('test_scripts_path', _store_path('test_scripts'))
    kwargs.setdefault('test_suites_path', _store_path('test_suites'))
    return ServerState(**kwargs)


def open_state(card=None, **kwargs):
    """A state with the fake card equipped (``open`` ran the real init)."""
    card = card if card is not None else CatCard()
    state = make_state(**kwargs)
    state.open(transport=card, start_threads=False)
    return state, card


class HTTPHarness(object):
    """A real ThreadingHTTPServer on an ephemeral loopback port."""

    def __init__(self, state):
        self.state = state
        self.server = build_http_server('127.0.0.1', 0, state)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={'poll_interval': 0.05},
            name='test-simulator-http', daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.state.close()

    def request(self, method, path, body=None):
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body).encode('utf-8')
            headers['Content-Type'] = 'application/json'
        request = Request('http://127.0.0.1:%d%s' % (self.port, path),
                          data=data, method=method, headers=headers)
        try:
            with urlopen(request, timeout=5) as response:
                return response.status, response.read()
        except HTTPError as exc:
            try:
                return exc.code, exc.read()
            finally:
                exc.close()

    def json(self, method, path, body=None):
        status, raw = self.request(method, path, body)
        return status, json.loads(raw.decode('utf-8'))

    def get(self, path):
        return self.json('GET', path)

    def post(self, path, body=None):
        return self.json('POST', path, body if body is not None else {})


class HarnessCase(unittest.TestCase):
    """One state + HTTP harness per test (the fake card per test)."""

    def setUp(self):
        self.state = None
        self.card = None
        self.http = None

    def tearDown(self):
        if self.http is not None:
            self.http.close()

    def serve(self, card=None, **kwargs):
        self.state, self.card = open_state(card, **kwargs)
        self.http = HTTPHarness(self.state)
        return self.state, self.card


# ---- the helper ports ------------------------------------------------------


class HelperTests(unittest.TestCase):
    """The module-level helpers the endpoints and the run engine share."""

    def test_the_terminal_profile_validator(self):
        self.assertEqual(validate_terminal_profile(' aa bb '), ('AABB', None))
        for bad, message in ((None, 'profile is empty'), ('', 'profile is empty'),
                             ('zz', 'profile is not valid hex'),
                             ('abc', 'profile must have an even number of hex digits'),
                             ('00' * 256, 'profile exceeds 255 bytes')):
            value, error = validate_terminal_profile(bad)
            self.assertIsNone(value)
            self.assertEqual(error, message)

    def test_the_menu_text_resolution(self):
        menu = {'items': [{'id': 1, 'text': 'One'},
                          {'id': 2, 'text': 'Coffe'}]}
        self.assertEqual(resolve_menu_item(menu, 'coffe'), 2)
        self.assertEqual(resolve_menu_item(menu, 'off', mode='contains'), 2)
        # the mode is validated, not passed through: 'Exact' is the mode name,
        # an unknown mode must not degrade to a contains match
        self.assertEqual(resolve_menu_item(menu, 'One', mode='Exact'), 1)
        with self.assertRaises(ValueError) as caught:
            resolve_menu_item(menu, 'offe', mode='fuzzy')
        self.assertIn('"exact" or "contains"', str(caught.exception))
        self.assertEqual(resolve_menu_item(menu, 'One', case_sensitive=True), 1)
        # the flag accepts its literal spellings and rejects the rest
        self.assertEqual(resolve_menu_item(menu, 'one',
                                           case_sensitive='false'), 1)
        self.assertEqual(resolve_menu_item(menu, 'One',
                                           case_sensitive='true'), 1)
        with self.assertRaises(ValueError) as caught:
            resolve_menu_item(menu, 'One', case_sensitive='maybe')
        self.assertIn('must be a boolean', str(caught.exception))
        with self.assertRaises(ValueError) as caught:
            resolve_menu_item(menu, 'coffe', case_sensitive=True)
        self.assertIn('no menu item matches', str(caught.exception))
        self.assertIn('case-insensitive match exists', str(caught.exception))
        with self.assertRaises(ValueError):
            resolve_menu_item(menu, 'o', mode='contains')
        with self.assertRaises(ValueError) as caught:
            resolve_menu_item({'items': []}, 'One')
        self.assertIn('no menu cached', str(caught.exception))
        self.assertIn('SET UP MENU', str(caught.exception))
        self.assertNotIn('expectation', str(caught.exception))


# ---- the STK menu browser --------------------------------------------------


class MenuInfoTests(HarnessCase):
    def test_the_init_captures_the_menu(self):
        card = CatCard(fetches=[menu_command(), EVENT_LIST_COMMAND])
        card.tp_sw = '9122'
        card.trs = ['9000', '9000']
        self.serve(card)
        status, body = self.http.get('/api/menu')
        self.assertEqual(status, 200)
        self.assertEqual(body['title'], 'Alfa Mobile')
        self.assertEqual([item['id'] for item in body['items']], [0x80, 0x81])
        self.assertEqual(body['items'][1]['text'], 'Coffe')
        self.assertFalse(body['active'])

    def test_a_title_less_menu_carries_no_title(self):
        card = CatCard(fetches=[menu_command(title=None)])
        card.tp_sw = '9122'
        card.trs = ['9000']
        self.serve(card)
        _status, body = self.http.get('/api/menu')
        self.assertNotIn('title', body)
        self.assertEqual(len(body['items']), 2)

    def test_no_menu_is_an_empty_item_list(self):
        self.serve()
        status, body = self.http.get('/api/menu')
        self.assertEqual(status, 200)
        self.assertEqual(body, {'items': [], 'active': False})

    def test_a_fetched_menu_refreshes_the_cache(self):
        self.serve()
        # a STATUS poll announces a new menu: the chain caches it
        self.card.statuses = ['9122']
        self.card.fetches = [menu_command(title='New menu')]
        self.card.trs = ['9000']
        _status, body = self.http.post('/api/status-poll')
        self.assertTrue(body['proactive'])
        _status, body = self.http.get('/api/menu')
        self.assertEqual(body['title'], 'New menu')

    def test_a_paused_menu_reports_active(self):
        # ``active`` follows the STK dialogue: a paused command is active
        card = CatCard(fetches=[menu_command()])
        card.tp_sw = '9122'
        card.trs = ['9000']
        state, card = self.serve(card)
        card.envelope_sw = '9122'
        card.fetches = [SELECT_ITEM]
        card.trs = ['9000']
        self.http.post('/api/menu-select', {'item_id': 0x80})
        self.assertTrue(state.cat.stk_pending)
        status, body = self.http.get('/api/menu')
        self.assertEqual(status, 200)
        self.assertTrue(body['active'])


class MenuSelectTests(HarnessCase):
    def _menu_state(self):
        card = CatCard(fetches=[menu_command()])
        card.tp_sw = '9122'
        card.trs = ['9000']
        state, card = self.serve(card)
        return state, card

    def test_select_by_id(self):
        _state, card = self._menu_state()
        card.envelope_sw = '9122'
        card.fetches = [SELECT_ITEM]
        card.trs = ['9000']
        status, body = self.http.post('/api/menu-select', {'item_id': 0x80})
        self.assertEqual(status, 200)
        envelope = [apdu for apdu in card.sent_hex()
                    if apdu.startswith('80C2')][-1]
        self.assertIn('D30702020181900180', envelope)
        self.assertEqual(body['type'], 'select_item')
        self.assertEqual([item['id'] for item in body['items']], [1, 2])
        self.assertTrue(self.http.state.stk_status()['pending'])

    def test_select_by_text_is_case_insensitive(self):
        _state, card = self._menu_state()
        card.envelope_sw = '9000'
        status, body = self.http.post('/api/menu-select',
                                      {'item_name': 'coffe'})
        self.assertEqual(status, 200)
        envelope = [apdu for apdu in card.sent_hex()
                    if apdu.startswith('80C2')][-1]
        self.assertIn('D30702020181900181', envelope)
        self.assertEqual(body['type'], 'done')

    def test_text_contains_and_case_sensitive(self):
        _state, card = self._menu_state()
        card.envelope_sw = '9000'
        status, _body = self.http.post(
            '/api/menu-select',
            {'item_name': 'ettin', 'mode': 'contains'})
        self.assertEqual(status, 200)
        envelope = [apdu for apdu in card.sent_hex()
                    if apdu.startswith('80C2')][-1]
        self.assertIn('D30702020181900180', envelope)
        status, body = self.http.post(
            '/api/menu-select',
            {'item_name': 'settings', 'case_sensitive': True})
        self.assertEqual(status, 400)
        self.assertIn('no menu item matches', body['error'])

    def test_a_bad_item_consumes_nothing(self):
        state, card = self._menu_state()
        before = len(card.sent)
        status, body = self.http.post('/api/menu-select', {'item_id': 'x'})
        self.assertEqual(status, 400)
        self.assertEqual(len(card.sent), before)
        self.assertIsNone(state.cat.stk_pending)

    def test_an_unknown_mode_is_refused(self):
        # the mode must not reach match_text: anything but exact/contains used
        # to degrade to a contains match ('Exact' too)
        state, card = self._menu_state()
        before = len(card.sent)
        for mode in ('fuzzy', 'Exact!', 'regex', 0, False):
            with self.subTest(mode=mode):
                status, body = self.http.post(
                    '/api/menu-select', {'item_name': 'offe', 'mode': mode})
                self.assertEqual(status, 400, mode)
                self.assertIn('"exact" or "contains"', body['error'])
        self.assertEqual(len(card.sent), before)
        self.assertFalse(state.cat.menu_active)

    def test_the_case_sensitive_flag_is_validated(self):
        _state, card = self._menu_state()
        card.envelope_sw = '9000'
        # the literal spelling reads as a flag ...
        status, _body = self.http.post(
            '/api/menu-select',
            {'item_name': 'COFFE', 'case_sensitive': 'false'})
        self.assertEqual(status, 200)
        # ... anything else is a 400, never silently true
        status, body = self.http.post(
            '/api/menu-select',
            {'item_name': 'coffe', 'case_sensitive': 'maybe'})
        self.assertEqual(status, 400)
        self.assertIn('must be a boolean', body['error'])

    def test_a_new_selection_finishes_the_pending_command(self):
        state, card = self._menu_state()
        card.envelope_sw = '9122'
        card.fetches = [SELECT_ITEM]
        self.http.post('/api/menu-select', {'item_id': 0x80})
        self.assertTrue(state.cat.stk_pending)
        card.envelope_sw = '9000'
        self.http.post('/api/menu-select', {'item_id': 0x81})
        trs = [apdu for apdu in card.sent_hex() if apdu.startswith('8014')]
        # the cancel TR carries result 0x10 (TS 102 223 §8.12.0)
        self.assertIn('83021000', trs[-1])
        self.assertIsNone(state.cat.stk_pending)

    def test_no_card_session(self):
        state = make_state()
        self.http = HTTPHarness(state)
        status, body = self.http.post('/api/menu-select', {'item_id': 1})
        self.assertEqual(status, 503)
        self.assertIn('no card session', body['error'])


class MenuRespondTests(HarnessCase):
    def _paused(self, pending_command):
        card = CatCard()
        state, card = self.serve(card)
        state.cat.stk_pending = None
        state.cat.menu_active = True
        card.envelope_sw = '9122'
        card.fetches = [pending_command]
        self.http.post('/api/menu-select', {'item_id': 0x80})
        self.assertTrue(state.cat.stk_pending)
        return state, card

    def test_ok_with_the_item_id(self):
        state, card = self._paused(SELECT_ITEM)
        status, body = self.http.post('/api/menu-respond',
                                      {'result': 'ok', 'item_id': 2})
        self.assertEqual(status, 200)
        self.assertEqual(body, {'sw': '9000', 'type': 'done'})
        self.assertIsNone(state.cat.stk_pending)
        tr = [apdu for apdu in card.sent_hex() if apdu.startswith('8014')][-1]
        self.assertIn('83020000', tr)
        self.assertTrue(tr.endswith('100102'))

    def test_cancel(self):
        _state, card = self._paused(SELECT_ITEM)
        status, body = self.http.post('/api/menu-respond', {'result': 'cancel'})
        self.assertEqual(status, 200)
        self.assertEqual(body, {'sw': '9000'})
        tr = [apdu for apdu in card.sent_hex() if apdu.startswith('8014')][-1]
        self.assertIn('83021000', tr)

    def test_timeout(self):
        _state, card = self._paused(SELECT_ITEM)
        status, _body = self.http.post('/api/menu-respond', {'result': 'timeout'})
        self.assertEqual(status, 200)
        tr = [apdu for apdu in card.sent_hex() if apdu.startswith('8014')][-1]
        self.assertIn('83021200', tr)

    def test_the_text_answer_of_a_get_input(self):
        state, card = self._paused(GET_INPUT)
        pending = state.cat.stk_pending
        self.assertEqual(pending['type'], 'get_input')
        self.assertEqual((pending['min'], pending['max']), (4, 8))
        status, body = self.http.post('/api/menu-respond',
                                      {'result': 'ok', 'text': '1234'})
        self.assertEqual(status, 200)
        self.assertEqual(body['sw'], '9000')
        tr = [apdu for apdu in card.sent_hex() if apdu.startswith('8014')][-1]
        # TS 102 223 §6.8.5: the Text string (8-bit coding 04), object tag 0D
        self.assertIn('0D0504' + b'1234'.hex().upper(), tr)

    def test_an_invalid_text_is_refused_before_the_tr(self):
        state, _card = self._paused(GET_INPUT)
        status, body = self.http.post('/api/menu-respond',
                                      {'result': 'ok', 'text': '12a4'})
        self.assertEqual(status, 400)
        self.assertIn('digits', body['error'])
        self.assertIsNotNone(state.cat.stk_pending)

    def test_no_pending_command(self):
        self.serve()
        status, body = self.http.post('/api/menu-respond', {'result': 'ok'})
        self.assertEqual(status, 400)
        self.assertEqual(body, {'error': 'no pending command'})

    def test_an_out_of_range_item_id_is_refused(self):
        # menu_send_response masks the id to one octet (item_id & 0xFF), so an
        # unchecked 300 would report item 44 to the card
        state, card = self._paused(SELECT_ITEM)
        before = len(card.sent)
        for bad in (300, -1, '300'):
            with self.subTest(item_id=bad):
                status, body = self.http.post(
                    '/api/menu-respond', {'result': 'ok', 'item_id': bad})
                self.assertEqual(status, 400, bad)
                self.assertIn('item_id must be one octet', body['error'])
        self.assertEqual(len(card.sent), before)
        self.assertIsNotNone(state.cat.stk_pending)


class StkStatusTests(HarnessCase):
    def test_the_pending_command_state(self):
        card = CatCard()
        state, card = self.serve(card)
        _status, idle = self.http.get('/api/stk-status')
        self.assertEqual(idle, {'active': False, 'pending': False,
                                'pending_type': None})
        card.envelope_sw = '9122'
        card.fetches = [GET_INPUT]
        self.http.post('/api/menu-select', {'item_id': 0x80})
        status, body = self.http.get('/api/stk-status')
        self.assertEqual(status, 200)
        self.assertTrue(body['active'])
        self.assertTrue(body['pending'])
        self.assertEqual(body['pending_type'], 'get_input')
        self.assertNotIn('type', body)          # the legacy shape only
        self.assertEqual(body['pending_data']['text'], 'PIN')
        self.assertNotIn('shown_at', body['pending_data'])
        self.assertIsNotNone(state.cat.stk_pending['shown_at'])

    def test_the_pending_data_is_json_safe(self):
        card = CatCard()
        state, card = self.serve(card)
        card.envelope_sw = '9122'
        card.fetches = [GET_INPUT]
        self.http.post('/api/menu-select', {'item_id': 0x80})
        _status, body = self.http.get('/api/stk-status')
        json.dumps(body)                                # no bytes anywhere
        self.assertIsInstance(body['pending_data']['raw'], str)


# ---- the event downloads ---------------------------------------------------


class EventsTests(HarnessCase):
    def test_the_subscribed_events(self):
        card = CatCard(fetches=[EVENT_LIST_COMMAND])
        card.tp_sw = '9122'
        card.trs = ['9000']
        self.serve(card)
        status, body = self.http.get('/api/events')
        self.assertEqual(status, 200)
        self.assertEqual(body, [0x04, 0x05])

    def test_no_event_list(self):
        self.serve()
        _status, body = self.http.get('/api/events')
        self.assertEqual(body, [])

    def test_the_raw_event_data_and_the_source(self):
        _state, card = self.serve()
        status, body = self.http.post('/api/event-send',
                                      {'event_type': 0x03,
                                       'event_data': '9B0100',
                                       'event_src': '83'})
        self.assertEqual(status, 200)
        # 3c-3e-2a: the monitor is patched from the event and attached, so
        # the response is no longer just the status word.
        self.assertEqual(body['sw'], '9000')
        self.assertEqual(body['net_state']['service']['state'], 'normal')
        self.assertEqual(body['net_state']['service']['source'],
                         'event:location-status')
        envelope = [apdu for apdu in card.sent_hex()
                    if apdu.startswith('80C2')][-1]
        # D6 (99 01 03 82 02 83 81) + 9B0100
        self.assertIn('D60A990103820283819B0100', envelope)

    def test_the_semantic_events(self):
        fields = {'status': 0}
        expected = events.build(0x03, fields)
        _state, card = self.serve()
        status, body = self.http.post('/api/event-send',
                                      {'event_type': 3, 'fields': fields})
        self.assertEqual(status, 200)
        envelope = [apdu for apdu in card.sent_hex()
                    if apdu.startswith('80C2')][-1]
        self.assertIn(expected.upper(), envelope)

    def test_the_four_semantic_builders(self):
        cases = [(0x03, {'status': 0}),
                 (0x0B, {'access_tech': 0}),
                 (0x12, {'registration': 0, 'cause': 0}),
                 (0x1D, {'status': 0})]
        _state, card = self.serve()
        for event, fields in cases:
            with self.subTest(event=event):
                expected = events.build(event, fields)
                status, body = self.http.post(
                    '/api/event-send', {'event_type': event,
                                        'fields': fields})
                self.assertEqual(status, 200, body)
                envelope = [apdu for apdu in card.sent_hex()
                            if apdu.startswith('80C2')][-1]
                self.assertIn(expected.upper(), envelope)

    def test_an_event_name_resolves(self):
        _state, card = self.serve()
        status, body = self.http.post(
            '/api/event-send',
            {'event_type': 'location_status', 'fields': {'status': 0}})
        self.assertEqual(status, 200, body)

    def test_a_decimal_string_keeps_the_legacy_reading(self):
        # the documented API form is a JSON number; a digit string is decimal
        # like the legacy ``int(event_type)`` ('12' = 12, not 0x12)
        _state, card = self.serve()
        self.http.post('/api/event-send',
                       {'event_type': '12', 'event_data': ''})
        envelope = [apdu for apdu in card.sent_hex()
                    if apdu.startswith('80C2')][-1]
        self.assertIn('99010C', envelope)           # 0x0C, not 0x12

    def test_an_unknown_event_name_and_bad_hex(self):
        self.serve()
        status, body = self.http.post('/api/event-send', {})
        self.assertEqual(status, 400)
        self.assertEqual(body['error'], 'event_type is required')
        status, body = self.http.post('/api/event-send',
                                      {'event_type': 'nope'})
        self.assertEqual(status, 400)
        self.assertIn('not an event byte', body['error'])
        status, body = self.http.post('/api/event-send',
                                      {'event_type': 3,
                                       'event_data': 'xyz'})
        self.assertEqual(status, 400)

    def test_fields_for_an_unmodelled_event(self):
        self.serve()
        status, body = self.http.post('/api/event-send',
                                      {'event_type': 0x04, 'fields': {}})
        self.assertEqual(status, 400)
        self.assertIn('no semantic builder', body['error'])

    def test_an_oversized_event_data_is_refused(self):
        self.serve()
        status, body = self.http.post(
            '/api/event-send',
            {'event_type': 3, 'event_data': '00' * (events.EVENT_DATA_MAX + 1)})
        self.assertEqual(status, 400)
        self.assertIn('one ENVELOPE', body['error'])

    def test_the_poll_negotiation_answer_is_applied(self):
        state, card = self.serve()
        state.poll_interval = 10
        card.envelope_data = bytes.fromhex('1101020402011E')
        status, body = self.http.post(
            '/api/event-send',
            {'event_type': 0x1C, 'event_data': '0402011E'})
        self.assertEqual(status, 200)
        self.assertEqual(body['negotiation']['result'], 2)
        self.assertEqual(body['negotiation']['result_name'], 'modified')
        self.assertEqual(body['negotiation']['seconds'], 30)
        self.assertEqual(state.poll_interval, 30)
        _status, poll = self.http.get('/api/poll-status')
        self.assertEqual(poll['interval'], 30)


# ---- the TERMINAL PROFILE --------------------------------------------------


class TerminalProfileTests(HarnessCase):
    def test_get_reports_the_profile_and_the_startup_default(self):
        profile = 'FFFFFFFF7F9F00DFFF03021FE2000000C3FB000704117800710100000038428003'
        self.serve(terminal_profile=profile)
        status, body = self.http.get('/api/terminal-profile')
        self.assertEqual(status, 200)
        self.assertEqual(body['profile'], profile)
        self.assertEqual(body['bytes'], 33)
        self.assertEqual(body['cli_default'], profile)

    def test_post_sets_and_resends(self):
        state, card = self.serve()
        card.fetches = []
        status, body = self.http.post('/api/terminal-profile',
                                      {'profile': 'aa bb'})
        self.assertEqual(status, 200)
        self.assertEqual(body['ok'], True)
        self.assertEqual(body['profile'], 'AABB')
        self.assertEqual(state.terminal_profile, 'AABB')
        tp = [apdu for apdu in card.sent_hex() if apdu.startswith('8010')][-1]
        self.assertTrue(tp.startswith('8010000002AABB'))

    def test_an_empty_body_resends_the_current_profile(self):
        self.serve(terminal_profile='DEADBEEF')
        status, body = self.http.post('/api/terminal-profile', {})
        self.assertEqual(status, 200)
        self.assertEqual(body['profile'], 'DEADBEEF')

    def test_the_resend_keeps_the_log_and_adds_a_marker(self):
        state, card = self.serve()
        card.statuses = ['9122']
        card.fetches = [menu_command()]
        card.trs = ['9000']
        self.http.post('/api/status-poll')
        entries = state.cat.entries(50)
        self.assertTrue(entries)
        status, _body = self.http.post('/api/terminal-profile', {})
        self.assertEqual(status, 200)
        markers = [entry for entry in state.cat.entries(50)
                   if entry.get('marker')]
        self.assertEqual(len(markers), 1)
        self.assertEqual(markers[0]['marker'], 'terminal-profile')
        # the fetched command stays in the history
        self.assertEqual(state.cat.entries(50)[-1]['type_hex'], '25')

    def test_an_invalid_profile(self):
        state, _card = self.serve()
        for bad, message in (('zz', 'not valid hex'),
                             ('abc', 'even number of hex digits'),
                             ('00' * 256, 'exceeds 255 bytes')):
            with self.subTest(profile=bad[:6]):
                status, body = self.http.post('/api/terminal-profile',
                                              {'profile': bad})
                self.assertEqual(status, 400)
                self.assertIn(message, body['error'])
        self.assertEqual(state.terminal_profile,
                         'FFFFFFFF7F9F00DFFF03021FE2000000C3FB000704117800710100000038428003')

    def test_rescue(self):
        state, card = self.serve(terminal_profile='DEADBEEF')
        card.tp_sw = '9122'
        card.fetches = [menu_command()]
        card.trs = ['9000']
        status, body = self.http.post('/api/rescue')
        self.assertEqual(status, 200)
        self.assertTrue(body['ok'])
        self.assertTrue(body['menu'])
        self.assertEqual(state.cat.sim_menu['title'], 'Alfa Mobile')

    def test_no_card_session(self):
        state = make_state()
        self.http = HTTPHarness(state)
        status, _body = self.http.post('/api/terminal-profile', {})
        self.assertEqual(status, 503)
        status, _body = self.http.post('/api/rescue')
        self.assertEqual(status, 503)


# ---- the polling -----------------------------------------------------------


class PollTests(HarnessCase):
    def test_the_poll_state(self):
        state, _card = self.serve(poll_interval=30)
        status, body = self.http.get('/api/poll-status')
        self.assertEqual(status, 200)
        self.assertEqual(body, {'enabled': True, 'interval': 30,
                                'card_disabled': False})

    def test_the_toggle(self):
        self.serve()
        status, body = self.http.post('/api/poll-toggle', {'enabled': False})
        self.assertEqual(status, 200)
        self.assertFalse(body['enabled'])
        status, body = self.http.post('/api/poll-toggle', {'enabled': True})
        self.assertTrue(body['enabled'])

    def test_the_toggle_refuses_to_enable_without_an_interval(self):
        state, _card = self.serve(poll_interval=0)
        status, body = self.http.post('/api/poll-toggle', {'enabled': True})
        self.assertEqual(status, 200)
        self.assertFalse(body['enabled'])
        self.assertFalse(state.poll_enabled)

    def test_the_card_polling_off_is_reported(self):
        state, _card = self.serve()
        state.cat.poll_suspended = True
        _status, body = self.http.get('/api/poll-status')
        self.assertTrue(body['card_disabled'])
        _status, body = self.http.post('/api/poll-toggle', {'enabled': True})
        self.assertIn('POLLING OFF', body['warning'])
        self.assertIn('6.4.14', body['warning'])

    def test_the_manual_status_poll_with_a_chain(self):
        _state, card = self.serve()
        card.statuses = [b'\x91\x0d'.hex()]
        card.fetches = [DISPLAY_TEXT]
        card.trs = ['9000']
        status, body = self.http.post('/api/status-poll')
        self.assertEqual(status, 200)
        self.assertTrue(body['proactive'])
        # the legacy response reports the STATUS answer itself, not the chain's
        self.assertEqual(body['sw'], '910D')
        self.assertEqual(card.count('8012'), 1)
        self.assertEqual(card.count('8014'), 1)

    def test_the_manual_status_poll_idle(self):
        _state, card = self.serve()
        status, body = self.http.post('/api/status-poll')
        self.assertEqual(status, 200)
        self.assertEqual(body, {'sw': '9000'})

    def test_the_manual_poll_is_bounded(self):
        state, card = self.serve()
        # a card that re-queues a command: every STATUS says 91xx again
        card.statuses = ['910d'] * 50
        card.fetches = [DISPLAY_TEXT] * 50
        card.trs = ['91ff'] * 50
        status, body = self.http.post('/api/status-poll')
        self.assertEqual(status, 200)
        self.assertLessEqual(card.count('8012'), 10)      # POLL_CHAIN_MAX
        self.assertTrue(state.connected)

    def test_no_card_session(self):
        state = make_state()
        self.http = HTTPHarness(state)
        status, _body = self.http.post('/api/status-poll')
        self.assertEqual(status, 503)


# ---- the proactive log -----------------------------------------------------


class ProactiveLogTests(HarnessCase):
    def test_the_log_shape_and_order(self):
        _state, card = self.serve()
        card.statuses = ['9122']
        card.fetches = [menu_command()]
        card.trs = ['9000']
        self.http.post('/api/status-poll')
        status, body = self.http.get('/api/proactive-log')
        self.assertEqual(status, 200)
        self.assertEqual(len(body), 1)
        entry = body[0]
        self.assertEqual(entry['type_hex'], '25')
        self.assertEqual(entry['type_name'], 'SET UP MENU')
        self.assertEqual(entry['bytes'], len(menu_command()))
        self.assertEqual(entry['tr_sw'], '9000')
        self.assertIsInstance(entry['elapsed'], float)
        self.assertIsInstance(entry['cmd_decoded'], list)
        json.dumps(body)

    def test_the_newest_entry_comes_first(self):
        _state, card = self.serve()
        card.statuses = ['910d', '910d']
        card.fetches = [DISPLAY_TEXT, SELECT_ITEM]
        card.trs = ['9000', '9000']
        self.http.post('/api/status-poll')
        self.http.post('/api/status-poll')
        _status, body = self.http.get('/api/proactive-log')
        self.assertEqual([entry['type_hex'] for entry in body], ['24', '21'])

    def test_a_cardless_log_is_empty(self):
        state = make_state()
        self.http = HTTPHarness(state)
        status, body = self.http.get('/api/proactive-log')
        self.assertEqual(status, 200)
        self.assertEqual(body, [])


# ---- the PLI dictionary ----------------------------------------------------


class PliTests(HarnessCase):
    def test_the_qualifier_list(self):
        self.serve()
        status, body = self.http.get('/api/pli-qualifiers')
        self.assertEqual(status, 200)
        self.assertEqual(len(body), len(proactive.PLI_QUALIFIER_NAMES))
        codes = [row['code'] for row in body]
        self.assertEqual(codes, ['%02X' % code
                                 for code in proactive.PLI_QUALIFIER_NAMES])
        self.assertIn({'code': '03', 'name': 'Date, time and time zone'}, body)

    def test_the_defaults(self):
        self.serve()
        status, body = self.http.get('/api/pli-dict')
        self.assertEqual(status, 200)
        self.assertEqual(body['00'], cat_session.PLI_DEFAULTS[0x00])
        self.assertEqual(body['03'], '')            # the host clock answers it
        self.assertEqual(body['0A'], cat_session.PLI_DEFAULTS[0x0A])

    def test_an_override_and_its_clear(self):
        self.serve()
        status, body = self.http.post('/api/pli-dict',
                                      {'00': '130500F1100000'})
        self.assertEqual(status, 200)
        self.assertEqual(body['00'], '130500F1100000')
        self.assertNotIn('invalid', body)
        _status, body = self.http.get('/api/pli-dict')
        self.assertEqual(body['00'], '130500F1100000')
        # clearing restores the default (v3.23.1)
        _status, body = self.http.post('/api/pli-dict', {'00': ''})
        self.assertEqual(body['00'], cat_session.PLI_DEFAULTS[0x00])

    def test_the_invalid_and_unknown_lists(self):
        self.serve()
        status, body = self.http.post(
            '/api/pli-dict',
            {'0G': '00', '18': '0102', '0A': 'xyz', '01': 7,
             '03': ''})
        self.assertEqual(status, 200)
        self.assertEqual(sorted(body['invalid']), ['01', '0A', '0G'])
        self.assertEqual(body['unknown'], ['18'])

    def test_a_valid_save_names_nothing(self):
        self.serve()
        _status, body = self.http.post('/api/pli-dict', {'0A': '630104'})
        self.assertNotIn('invalid', body)
        self.assertNotIn('unknown', body)

    def test_the_overrides_are_card_free_and_survive_an_equip(self):
        state, card = self.serve()
        self.http.post('/api/pli-dict', {'0A': '630104'})
        self.assertIs(state.cat.pli_data, state.pli_data)
        # an equip builds a new CatSession on the same override dict
        state.equip()
        self.assertIs(state.cat.pli_data, state.pli_data)
        _status, body = self.http.get('/api/pli-dict')
        self.assertEqual(body['0A'], '630104')

    def test_the_cardless_dictionary(self):
        state = make_state()
        self.http = HTTPHarness(state)
        status, body = self.http.get('/api/pli-dict')
        self.assertEqual(status, 200)
        self.assertEqual(body['0A'], cat_session.PLI_DEFAULTS[0x0A])
        status, body = self.http.post('/api/pli-dict', {'0A': '630104'})
        self.assertEqual(status, 200)
        self.assertEqual(body['0A'], '630104')

    def test_the_dictionary_reaches_the_terminal_response(self):
        # the terminal's own PLI answer uses the override (the session shares
        # the state's dict, so the endpoint set it)
        state, card = self.serve()
        self.http.post('/api/pli-dict', {'00': '130500F1100000'})
        card.statuses = ['9122']
        card.fetches = [command(0x26, 0x00)]
        card.trs = ['9000']
        self.http.post('/api/status-poll')
        tr = [apdu for apdu in card.sent_hex() if apdu.startswith('8014')][-1]
        self.assertIn('130500F1100000', tr)
        self.assertEqual(state.cat.pli_data[0x00], '130500F1100000')


# ---- the raw APDU ----------------------------------------------------------


class RawApduTests(HarnessCase):
    def test_a_plain_apdu(self):
        _state, card = self.serve()
        status, body = self.http.post('/api/apdu', {'apdu': '00b000000a'})
        self.assertEqual(status, 200)
        self.assertEqual(body, {'response': ICCID_HEX.upper(), 'sw': '9000'})
        self.assertEqual(card.sent_hex()[-1], '00B000000A')

    def test_a_non_9000_sw_is_reported_not_raised(self):
        # the legacy send_apdu_checksw raised on a non-9000 answer (the
        # endpoint answered 500); a raw-APDU workbench must see any SW
        state, _card = self.serve()
        status, body = self.http.post('/api/apdu',
                                      {'apdu': '00A4000402FFFF'})
        self.assertEqual(status, 200)
        self.assertEqual(body, {'response': '', 'sw': '6A82'})
        self.assertTrue(state.connected)

    def test_a_91xx_answer_runs_the_chain(self):
        _state, card = self.serve()
        card.fetches = [DISPLAY_TEXT]
        card.trs = ['9000']

        original = card._answer

        def answer(apdu):
            if apdu == bytes.fromhex('00A40004023F00'):
                return b'', b'\x91\x0d'
            return original(apdu)

        card._answer = answer
        status, body = self.http.post('/api/apdu', {'apdu': '00A40004023F00'})
        self.assertEqual(status, 200)
        self.assertEqual(body['sw'], '9000')
        self.assertEqual(card.count('8012'), 1)
        self.assertEqual(card.count('8014'), 1)

    def test_a_bip_session_keeps_the_91xx(self):
        state, card = self.serve()
        state.bip.owner = 'bip'

        original = card._answer

        def answer(apdu):
            if apdu == bytes.fromhex('00A40004023F00'):
                return b'', b'\x91\x0d'
            return original(apdu)

        card._answer = answer
        status, body = self.http.post('/api/apdu', {'apdu': '00A40004023F00'})
        self.assertEqual(status, 200)
        self.assertEqual(body['sw'], '910D')
        self.assertEqual(card.count('8012'), 0)

    def test_the_apdu_is_blocked_during_a_test_run(self):
        state, _card = self.serve()
        state.test_running = True
        status, body = self.http.post('/api/apdu', {'apdu': '00B000000A'})
        self.assertEqual(status, 409)
        self.assertIn('test script running', body['error'])
        status, _body = self.http.post('/api/menu-select', {'item_id': 1})
        self.assertEqual(status, 409)

    def test_an_invalid_apdu(self):
        _state, card = self.serve()
        before = len(card.sent)
        for bad in ('', 'zz', '001', '0000'):
            with self.subTest(apdu=bad):
                status, body = self.http.post('/api/apdu', {'apdu': bad})
                self.assertEqual(status, 400, bad)
        self.assertEqual(len(card.sent), before)

    def test_no_card_session(self):
        state = make_state()
        self.http = HTTPHarness(state)
        status, body = self.http.post('/api/apdu', {'apdu': '00B000000A'})
        self.assertEqual(status, 503)
        self.assertIn('no card session', body['error'])


class TransportFailureTests(HarnessCase):
    """A transport error is a 503 with the status body, not a lost session."""

    def test_a_reader_error_answers_503_with_the_status(self):
        # the legacy /api/status-poll answered 200 {'sw': null, 'error':
        # 'card disconnected'}; the 3c-3a classification tears the stale link
        # down and answers 503 with status() (the PWA resets from connected)
        state, card = self.serve(auto_equip=False)

        def answer(apdu):
            if apdu[:2] == b'\x80\xF2':                 # STATUS
                raise ReaderError('reader gone')
            return card._answer(apdu)

        card._answer = answer
        status, body = self.http.post('/api/status-poll')
        self.assertEqual(status, 503)
        self.assertFalse(body['connected'])
        self.assertEqual(body['sw'], None)
        self.assertEqual(body['error'], 'reader gone')
        self.assertIsNone(state.transport)


# ---- the card-free GETs ----------------------------------------------------


class CardFreeGetTests(HarnessCase):
    """The eight cached-state GETs answer while another request holds the lock."""

    def test_the_cached_gets_answer_under_the_card_lock(self):
        self.serve()
        paths = ['/api/menu', '/api/stk-status', '/api/events',
                 '/api/poll-status', '/api/proactive-log',
                 '/api/terminal-profile', '/api/pli-qualifiers',
                 '/api/pli-dict']
        with self.state.card_lock:
            for path in paths:
                status, _body = self.http.get(path)
                self.assertEqual(status, 200, path)


class SocketHelpersTests(unittest.TestCase):
    """A direct raw-socket request keeps the JSON contract (no urllib magic)."""

    def test_a_get_on_an_unknown_api_path_is_a_json_404(self):
        state = make_state()
        harness = HTTPHarness(state)
        try:
            with socket.create_connection(('127.0.0.1', harness.port),
                                          timeout=5) as sock:
                sock.sendall(b'GET /api/nope HTTP/1.0\r\nHost: t\r\n\r\n')
                raw = b''
                while True:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    raw += chunk
            self.assertIn(b'404', raw.split(b'\r\n', 1)[0])
            self.assertIn(b'"error": "Not found"', raw)
        finally:
            harness.close()


if __name__ == '__main__':
    unittest.main()
