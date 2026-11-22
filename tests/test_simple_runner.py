# coding=utf-8
"""The test-script stores, CRUD and the single-script run (Phase 3c-3d-1).

No card and no pysim: every exchange is a scripted fake transport (the
startup sequence answers, then the queues the test arms for the runner's
APDUs) and the endpoints run on a real :class:`ThreadingHTTPServer` bound to
an ephemeral loopback port.  The runner is driven both through the HTTP
endpoints (the lifecycle, the store, the blocked paths) and directly
(``start(..., spawn=False)`` + ``execute``) for the step executors, so a
step's result is asserted without the worker thread's timing.

The card must be **equipped before the queues are armed**: the startup's
TERMINAL PROFILE drain polls STATUS itself, so an armed answer would be
consumed before the run (``run_steps`` takes a ``setup(card)`` callback that
runs after the equip).  Keys are synthetic (the repo rule).  The step
semantics are the legacy runner's; the drift tests in
:mod:`tests.test_simple_drift` compare the ported pure helpers against
``pysim_simple_server``.
"""

import json
import pathlib
import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from pysim_simple_server import presets as presets_store

from simple_server import testrun
from simple_server.server import BatchBusyError, ServerState, build_http_server

ATR = bytes.fromhex('3b9f95801fc78031e073fe211b67a3a0001505017204')
ICCID_DIGITS = '8970119000004600098'
ICCID_HEX = '980711090000640090F8'

# Synthetic keys (the repo rule: no card-specific material in tests).
KIC_KEY = '00112233445566778899AABBCCDDEEFF'
KID_KEY = 'FFEEDDCCBBAA99887766554433221100'

ROLE_TARS = [{'role': role, 'tar': tar, 'msl': '16', 'keyset': 1}
             for role, tar in (('isd', '000000'), ('uiccRfm', 'B00000'),
                               ('usimRfm', 'B00001'))]

# ---- APDU/TLV helpers -------------------------------------------------------

SELECT_MF = bytes.fromhex('00A40004023F0000')
STATUS = bytes.fromhex('80F2000C00')


def ber(tag, value):
    """A short-form BER/COMPREHENSION TLV (all fixture values are < 128 B)."""
    if len(value) >= 0x80:
        raise ValueError('the fixtures stay short-form')
    return bytes((tag, len(value))) + bytes(value)


def fcp(*objects):
    """An FCP template (tag 62) around the given data objects."""
    return ber(0x62, b''.join(objects))


# TS 102 221 §11.1.1.4.3 file descriptors (as tests/test_simple_files.py).
FD_TRANSPARENT = bytes.fromhex('4121')
FD_LINEAR = bytes.fromhex('4221000902')              # linear fixed, 9 x 2
TRANSPARENT_FCP = fcp(ber(0x80, b'\x00\x10'), ber(0x82, FD_TRANSPARENT),
                      ber(0x83, b'\x2f\xe2'))
LINEAR_FCP = fcp(ber(0x80, b'\x00\x12'), ber(0x82, FD_LINEAR),
                 ber(0x83, b'\x6f\x07'))


def proactive(command_details, *objects):
    """A ``D0`` proactive command template around the given CTLVs."""
    inner = command_details + ber(0x82, b'\x82\x81') + b''.join(objects)
    return ber(0xD0, inner)


def command_details(cmd_num, cmd_type, qualifier=0, tag=0x81):
    return ber(tag, bytes((cmd_num, cmd_type, qualifier)))


def submit_tpdu(payload, concat=None, serial=1):
    """An SMS-SUBMIT TPDU whose TP-UD carries ``payload``.

    The UD starts with the UDH: the (U)SIM Toolkit Security Header IE
    (``71 00``, TS 23.040 §9.2.3.24 - the submit form of the RPI) plus, with
    ``concat=(ref, total, num)``, the concatenation IE.  The TP-UDHI bit is
    set (the first octet ``41``) and no TP-VP is present.
    """
    ies = b''
    if concat is not None:
        # the 8-bit reference form (IEI 00, three octets, TS 23.040 9.2.3.24)
        ies += bytes([0x00, 0x03, concat[0] & 0xFF, concat[1] & 0xFF,
                      concat[2] & 0xFF])
    ies += bytes([0x71, 0x00])
    ud = bytes([len(ies)]) + ies + bytes(payload)
    return (bytes([0x41, serial & 0xFF, 0x01, 0x81, 0x21, 0x7F, 0x00,
                   len(ud)]) + ud)


def send_sm_command(tpdu, cmd_num=1):
    """A SEND SHORT MESSAGE proactive command carrying ``tpdu`` (tag 8B)."""
    return proactive(command_details(cmd_num, 0x13), ber(0x8B, tpdu))


# ---- the fake card ----------------------------------------------------------

class FakeTransport(object):
    """A scripted transport: ``handler(apdu)`` returns ``(data, sw)`` or None.

    An unhandled APDU answers ``6A82``, so the startup's optional reads fail
    cleanly.
    """

    def __init__(self, handler=None, reader_name='Fake reader', atr=ATR):
        self.handler = handler
        self.atr = atr
        self.reader_name = reader_name
        self.protocol = 1
        self.sent = []
        self.resets = 0
        self.connected = False
        self._con = None

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
        if self.handler is not None:
            answer = self.handler(apdu)
            if answer is not None:
                return answer
        return b'', b'\x6a\x82'


class Card(object):
    """A UICC fake: the startup answers plus per-prefix queues for the run.

    ``push(prefix, data, sw)`` queues one answer for every APDU starting with
    ``prefix`` (hex, upper case); the queue's **last** entry is sticky (a
    polling loop then behaves like a card that keeps saying the same thing)
    and a drained queue falls back to it.  ``status``/``fetch``/``tr`` are the
    CAT shorthand; ``envelope`` queues ENVELOPE answers and ``envelopes``
    records every ``80 C2`` APDU.
    """

    def __init__(self, iccid=ICCID_HEX):
        self.iccid = iccid
        self.sent = []
        self.queues = {}
        self.envelopes = []
        self.envelope_answers = []
        self.blocks = {}

    def push(self, prefix, data=b'', sw='9000'):
        prefix = prefix.upper()
        answer = (bytes(data), bytes.fromhex(sw))
        queue = self.queues.setdefault(prefix, [])
        queue.append(answer)
        return self

    def status(self, sw='9000', data=b''):
        return self.push('80F2000C', data, sw)

    def fetch(self, data, sw='9000'):
        return self.push('8012', data, sw)

    def tr(self, sw='9000'):
        return self.push('8014', b'', sw)

    def envelope(self, data=b'', sw='9000'):
        self.envelope_answers.append((bytes(data), bytes.fromhex(sw)))
        return self

    def send(self, prefix, data=b'', sw='9000'):
        """Alias of :meth:`push` for the file/APDU queues."""
        return self.push(prefix, data, sw)

    def __call__(self, apdu):
        up = apdu.hex().upper()
        self.sent.append(up)
        block = self.blocks.get(up)
        if block is not None:
            return block
        if up.startswith('80C2'):
            self.envelopes.append(apdu)
            if self.envelope_answers:
                return (self.envelope_answers.pop(0) if len(
                    self.envelope_answers) > 1
                    else self.envelope_answers[0])
            return b'', b'\x90\x00'
        for prefix in sorted(self.queues, key=len, reverse=True):
            if not up.startswith(prefix):
                continue
            queue = self.queues[prefix]
            if len(queue) > 1:
                return queue.pop(0)
            return queue[0] if queue else None
        # the startup: UICC detection (not the ISD-R), MF, EF.ICCID, TP
        if up.startswith('00A40404'):
            if 'A0000005591010' in up:
                return b'', b'\x6a\x82'      # no ISD-R: stays a plain UICC
            return b'', b'\x90\x00'
        if up == SELECT_MF.hex().upper():
            return b'', b'\x90\x00'
        if up.startswith('00A40004022FE2'):
            return TRANSPARENT_FCP, b'\x90\x00'
        if up.startswith('00B0'):
            return bytes.fromhex(self.iccid), b'\x90\x00'
        if up.startswith('8010'):
            return b'', b'\x90\x00'
        return None

    def sent_from(self, prefix):
        return [apdu for apdu in self.sent if apdu.startswith(prefix)]


# ---- the fixtures -----------------------------------------------------------

def preset_fields(**kwargs):
    """The request body of a complete synthetic preset (role TARs included)."""
    fields = {
        'name': 'Bench card',
        'iccid': ICCID_DIGITS,
        'keysets': [{'kic': '15', 'kid': '15', 'kicKey': KIC_KEY,
                     'kidKey': KID_KEY, 'cntr': '0000000010'}],
        'tars': [dict(entry) for entry in ROLE_TARS],
    }
    fields.update(kwargs)
    return fields


def temp_dir(testcase, prefix='simple-runner-'):
    root = tempfile.mkdtemp(prefix=prefix)
    testcase.addCleanup(shutil.rmtree, root, True)
    return pathlib.Path(root)


def make_state(testcase, card=None, **kwargs):
    """A cardless-then-equipped ServerState with the test seams set."""
    root = temp_dir(testcase)
    kwargs.setdefault('monitor', False)
    kwargs.setdefault('readers_fn', lambda: ['Reader A'])
    kwargs.setdefault('presets', presets_store.PresetStore(
        path=root / 'card_presets.json'))
    kwargs.setdefault('test_scripts_path', root / 'test_scripts.json')
    state = ServerState(**kwargs)
    testcase.addCleanup(state.close)
    if card is not None:
        state.open(transport=FakeTransport(handler=card), start_threads=False)
    return state


def normalise(steps, name='test'):
    from pysim_simple_server import testscript
    return testscript.normalise_script({'name': name, 'steps': steps},
                                       testrun.command_type)


def run_steps(testcase, steps, preset=None, setup=None):
    """Run a script synchronously; returns ``(state, runner, snapshot)``.

    The card is equipped first (the startup drain answers its own STATUS),
    then ``setup(card)`` arms the run's answers.
    """
    card = Card()
    state = make_state(testcase, card)
    if setup is not None:
        setup(card)
    script = normalise(steps)
    runner = state.test_runner
    runner.start(script, preset or {}, spawn=False)
    runner.execute(script, preset or {})
    return state, runner, runner.snapshot()


def check_by_label(checks, label):
    for check in checks:
        if check['label'] == label:
            return check
    raise AssertionError('no %r check in %r' % (label, checks))


STATUS_9K = {'type': 'action', 'kind': 'status', 'params': {}}


def status_step(sw='9000', **kwargs):
    step = {'type': 'action', 'kind': 'status', 'params': {},
            'check': {'sw': sw}}
    step.update(kwargs)
    return step


# ---- the store CRUD ---------------------------------------------------------

class Harness(object):
    """A real ThreadingHTTPServer on an ephemeral loopback port."""

    def __init__(self, state):
        self.state = state
        self.server = build_http_server('127.0.0.1', 0, state)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={'poll_interval': 0.05},
            name='test-http-runner', daemon=True)
        self.thread.start()

    def request(self, method, path, body=None):
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body).encode('utf-8')
            headers['Content-Type'] = 'application/json'
        request = Request('http://127.0.0.1:%d%s' % (self.port, path),
                          data=data, method=method, headers=headers)
        try:
            with urlopen(request, timeout=10) as response:
                return response.status, response.read()
        except HTTPError as exc:
            try:
                return exc.code, exc.read()
            finally:
                exc.close()

    def json(self, method, path, body=None):
        status, raw = self.request(method, path, body)
        return status, json.loads(raw.decode('utf-8'))

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


SCRIPT = {'name': 'status poll', 'steps': [dict(STATUS_9K)]}


class ScriptStoreTests(unittest.TestCase):
    def setUp(self):
        self.state = make_state(self)
        self.harness = Harness(self.state)
        self.addCleanup(self.harness.close)

    def test_the_store_is_served_card_free(self):
        status, body = self.harness.json('GET', '/api/test/scripts')
        self.assertEqual(status, 200)
        self.assertEqual(body['count'], 0)
        self.assertEqual(body['scripts'], [])
        self.assertEqual(body['version'], 2)
        self.assertTrue(body['path'].endswith('test_scripts.json'))

    def test_create_update_delete(self):
        status, body = self.harness.json('POST', '/api/test/scripts', SCRIPT)
        self.assertEqual(status, 200)
        self.assertTrue(body['ok'])
        script = body['script']
        self.assertEqual(script['name'], 'status poll')
        self.assertIsNone(script['suite_id'])
        sid = script['id']
        status, body = self.harness.json('GET', '/api/test/scripts')
        self.assertEqual([entry['id'] for entry in body['scripts']], [sid])

        status, body = self.harness.json(
            'POST', '/api/test/scripts/update',
            {'id': sid, 'script': {'name': 'renamed',
                                   'steps': SCRIPT['steps']}})
        self.assertEqual(status, 200)
        self.assertEqual(body['script']['name'], 'renamed')

        status, body = self.harness.json(
            'POST', '/api/test/scripts/update',
            {'id': 'nope', 'script': SCRIPT})
        self.assertEqual(status, 404)

        status, body = self.harness.json('POST', '/api/test/scripts/delete',
                                         {'id': sid})
        self.assertEqual(status, 200)
        self.assertTrue(body['removed'])
        self.assertEqual(self.state.test_scripts.list(), [])

    def test_an_invalid_script_is_a_400(self):
        status, body = self.harness.json(
            'POST', '/api/test/scripts', {'name': 'empty', 'steps': []})
        self.assertEqual(status, 400)
        self.assertIn('at least one step', body['error'])
        # a step-level validation error comes from the runner's own engine
        status, body = self.harness.json(
            'POST', '/api/test/scripts',
            {'name': 'bad', 'steps': [{'type': 'action', 'kind': 'nope'}]})
        self.assertEqual(status, 400)
        self.assertIn('unknown action kind', body['error'])
        self.assertEqual(self.state.test_scripts.list(), [])

    def test_the_create_takes_the_wrapped_form(self):
        status, body = self.harness.json(
            'POST', '/api/test/scripts', {'script': SCRIPT})
        self.assertEqual(status, 200)
        self.assertEqual(body['script']['name'], 'status poll')

    def test_a_suite_id_is_kept_as_an_opaque_field(self):
        # the membership rules belong to the suite store (3c-3d-2)
        suite_id = 'a' * 32
        status, body = self.harness.json(
            'POST', '/api/test/scripts', dict(SCRIPT, suite_id=suite_id))
        self.assertEqual(status, 200)
        self.assertEqual(body['script']['suite_id'], suite_id)

    def test_an_update_does_not_move_the_script(self):
        _status, body = self.harness.json('POST', '/api/test/scripts',
                                          dict(SCRIPT, suite_id='b' * 32))
        sid = body['script']['id']
        status, body = self.harness.json(
            'POST', '/api/test/scripts/update',
            {'id': sid, 'script': dict(SCRIPT, suite_id='c' * 32)})
        self.assertEqual(status, 200)
        self.assertEqual(body['script']['suite_id'], 'b' * 32)

    def test_the_import_merges_and_reports_bad_entries(self):
        status, body = self.harness.json(
            'POST', '/api/test/scripts/import',
            {'scripts': [SCRIPT, {'name': 'empty', 'steps': []}],
             'mode': 'merge'})
        self.assertEqual(status, 200)
        self.assertTrue(body['ok'])
        self.assertEqual(body['added'], 1)
        self.assertEqual(body['skipped'], 1)
        self.assertTrue(body['errors'])
        self.assertNotIn('added_ids', body)
        self.assertEqual(len(self.state.test_scripts.list()), 1)

    def test_the_import_validates_the_list(self):
        status, body = self.harness.json(
            'POST', '/api/test/scripts/import', {'scripts': 'nope'})
        self.assertEqual(status, 400)
        self.assertIn('list', body['error'])


class KeptEntryTests(unittest.TestCase):
    def test_a_kept_entry_that_no_longer_validates_is_served(self):
        # the store must never destroy a stored script: it is served, fails at
        # run start with the validator's message and can be fixed
        root = temp_dir(self)
        path = root / 'test_scripts.json'
        path.write_text(json.dumps({'version': 2, 'scripts': [
            {'id': 'a' * 32, 'name': 'stale', 'suite_id': None,
             'require_adm': False,
             'steps': [{'type': 'action', 'kind': 'status',
                        'params': {'attempts': 'nope'}}]}]}))
        state = make_state(self, test_scripts_path=path)
        harness = Harness(state)
        self.addCleanup(harness.close)
        status, body = harness.json('GET', '/api/test/scripts')
        self.assertEqual(status, 200)
        self.assertEqual(len(body['scripts']), 1)
        status, body = harness.json('POST', '/api/test/run',
                                    {'script_id': 'a' * 32})
        self.assertEqual(status, 400)
        self.assertIn('attempts', body['error'])
        # ... and the entry is still stored
        self.assertEqual(len(state.test_scripts.list()), 1)


# ---- the run lifecycle over HTTP -------------------------------------------

RUN_STATUS_SCRIPT = {'name': 'status', 'steps': [dict(STATUS_9K)]}


class RunEndpointTests(unittest.TestCase):
    def _wait(self, state, harness, timeout=10):
        deadline = time.time() + timeout
        body = None
        while time.time() < deadline:
            _status, body = harness.json('GET', '/api/test/status')
            if not body['running']:
                return body
            time.sleep(0.02)
        return body

    def test_run_status_stop_clear(self):
        state = make_state(self, Card())
        state.transport.handler.status('9000')
        harness = Harness(state)
        self.addCleanup(harness.close)
        status, body = harness.json('POST', '/api/test/run',
                                    {'script': RUN_STATUS_SCRIPT})
        self.assertEqual(status, 200, body)
        self.assertTrue(body['running'])
        self.assertEqual(body['total'], 1)
        body = self._wait(state, harness)
        self.assertFalse(body['running'])
        self.assertEqual(body['status'], 'ok')
        self.assertEqual(body['steps'][0]['status'], 'ok')
        self.assertEqual(body['steps'][0]['sw'], '9000')
        status, body = harness.json('POST', '/api/test/clear', {})
        self.assertEqual(status, 200)
        self.assertIsNone(body['status'])

    def test_clear_is_refused_while_running(self):
        state = make_state(self, Card())
        state.test_running = True          # no worker: the flag alone
        state.test_runner._run['running'] = True
        harness = Harness(state)
        self.addCleanup(harness.close)
        status, body = harness.json('POST', '/api/test/clear', {})
        self.assertEqual(status, 409)
        self.assertIn('running', body['error'])

    def test_a_stored_script_runs_by_id(self):
        state = make_state(self, Card())
        state.transport.handler.status('9000')
        harness = Harness(state)
        self.addCleanup(harness.close)
        _status, body = harness.json('POST', '/api/test/scripts',
                                     RUN_STATUS_SCRIPT)
        sid = body['script']['id']
        status, body = harness.json('POST', '/api/test/run',
                                    {'script_id': sid})
        self.assertEqual(status, 200, body)
        self.assertEqual(body['script_id'], sid)
        final = self._wait(state, harness)
        self.assertEqual(final['status'], 'ok')

    def test_an_unknown_script_id_is_a_404(self):
        state = make_state(self, Card())
        harness = Harness(state)
        self.addCleanup(harness.close)
        status, body = harness.json('POST', '/api/test/run',
                                    {'script_id': 'nope'})
        self.assertEqual(status, 404)
        status, body = harness.json('POST', '/api/test/run', {})
        self.assertEqual(status, 400)
        self.assertIn('script', body['error'])

    def test_a_suite_request_is_refused_with_a_reason(self):
        state = make_state(self, Card())
        harness = Harness(state)
        self.addCleanup(harness.close)
        status, body = harness.json('POST', '/api/test/run',
                                    {'suite_id': 'a' * 32})
        self.assertEqual(status, 400)
        self.assertIn('3c-3d-2', body['error'])

    def test_the_iterations_are_validated(self):
        state = make_state(self, Card())
        harness = Harness(state)
        self.addCleanup(harness.close)
        for bad in (0, -1, 'x', 1000000001):
            status, body = harness.json(
                'POST', '/api/test/run',
                {'script': RUN_STATUS_SCRIPT, 'iterations': bad})
            self.assertEqual(status, 400, bad)
            self.assertIn('iterations', body['error'])

    def test_an_unknown_preset_id_is_a_400(self):
        state = make_state(self, Card())
        harness = Harness(state)
        self.addCleanup(harness.close)
        status, body = harness.json(
            'POST', '/api/test/run',
            {'script': RUN_STATUS_SCRIPT, 'preset_id': 'nope'})
        self.assertEqual(status, 400)
        self.assertIn('card preset', body['error'])

    def test_a_second_run_is_a_409(self):
        state = make_state(self, Card())
        state.test_running = True
        harness = Harness(state)
        self.addCleanup(harness.close)
        status, body = harness.json('POST', '/api/test/run',
                                    {'script': RUN_STATUS_SCRIPT})
        self.assertEqual(status, 409)


class BlockedPathTests(unittest.TestCase):
    def test_the_card_endpoints_answer_409_while_a_run_owns_the_card(self):
        state = make_state(self, Card())
        state.test_running = True          # the flag the runner sets
        harness = Harness(state)
        self.addCleanup(harness.close)
        for method, path in (('POST', '/api/read'),
                             ('POST', '/api/select'),
                             ('POST', '/api/send-ota'),
                             ('POST', '/api/apdu'),
                             ('GET', '/api/esim/chip')):
            status, body = harness.json(method, path, {})
            self.assertEqual(status, 409, path)
            self.assertIn('test script running', body['error'])
        # the run's own endpoints stay available
        status, _body = harness.json('GET', '/api/test/status')
        self.assertEqual(status, 200)
        status, _body = harness.json('GET', '/api/test/scripts')
        self.assertEqual(status, 200)
        status, _body = harness.json('POST', '/api/test/stop', {})
        self.assertEqual(status, 200)

    def test_stop_answers_while_the_card_lock_is_held(self):
        # the runner holds the card lock inside a step, so Stop must not queue
        # behind a blocked exchange (CARD_FREE_POST) - otherwise the button is
        # dead exactly when the operator needs it
        state = make_state(self, Card())
        state.test_running = True
        state.test_runner._run['running'] = True
        harness = Harness(state)
        self.addCleanup(harness.close)
        state.card_lock.acquire()
        try:
            started = time.time()
            status, body = harness.json('POST', '/api/test/stop', {})
            elapsed = time.time() - started
        finally:
            state.card_lock.release()
        self.assertEqual(status, 200, body)
        self.assertLess(elapsed, 2.0)
        self.assertTrue(state.test_runner.snapshot()['stop'])

    def test_the_guard_is_off_without_a_run(self):
        state = make_state(self, Card())
        harness = Harness(state)
        self.addCleanup(harness.close)
        status, body = harness.json('POST', '/api/apdu', {'apdu': ''})
        self.assertNotEqual(status, 409)
        self.assertFalse(testrun.request_blocked('/api/read', False))
        self.assertTrue(testrun.request_blocked('/api/read', True))
        # a query string does not defeat the match
        self.assertTrue(testrun.request_blocked('/api/read?x=1', True))

    def test_a_batch_is_refused_while_a_run_is_active(self):
        state = make_state(self, Card())
        state.test_running = True
        with self.assertRaises(BatchBusyError) as caught:
            state.ram_batch_start({'kind': 'install-cap', 'cap_hex': ''})
        self.assertIn('test run', str(caught.exception))

    def test_a_run_is_refused_while_a_batch_is_active(self):
        state = make_state(self, Card())
        state.ram_batch = {'active': True}
        with self.assertRaises(testrun.TestRunBusy):
            state.test_run({'script': RUN_STATUS_SCRIPT})


# ---- the step executors -----------------------------------------------------

class StatusActionTests(unittest.TestCase):
    def test_a_single_poll(self):
        _state, _runner, run = run_steps(
            self, [status_step()], setup=lambda card: card.status('9000'))
        self.assertEqual(run['status'], 'ok')
        self.assertEqual(run['steps'][0]['sw'], '9000')
        self.assertEqual(run['steps'][0]['sent'], 'STATUS')

    def test_the_attempts_poll_until_the_card_announces(self):
        _state, _runner, run = run_steps(
            self, [status_step('91??', params={'attempts': 3}),
                   {'type': 'expect', 'command': 'SELECT ITEM'}],
            setup=lambda card: (card.status('9000').status('9105')
                                .fetch(SELECT_ITEM)))
        self.assertEqual(run['status'], 'ok', run['steps'])
        self.assertEqual(run['steps'][0]['sw'], '9105')
        self.assertEqual(run['steps'][0]['sent'], 'STATUS x2')

    def test_a_failed_status_sw_honours_on_fail(self):
        _state, _runner, run = run_steps(
            self, [status_step(on_fail='warning'), status_step()],
            setup=lambda card: (card.status('6A82').status('9000')))
        self.assertEqual(run['status'], 'warning')
        self.assertEqual([step['status'] for step in run['steps']],
                         ['warning', 'ok'])


class ApduActionTests(unittest.TestCase):
    def test_a_raw_apdu_and_its_sw(self):
        _state, _runner, run = run_steps(
            self,
            [{'type': 'action', 'kind': 'apdu',
              'params': {'apdu': '00B0000004'},
              'check': {'sw': '9000', 'data': 'AABB'}}],
            setup=lambda card: card.send('00B00000', bytes.fromhex('AABB'),
                                         '9000'))
        self.assertEqual(run['status'], 'ok')
        self.assertEqual(run['steps'][0]['data'], 'AABB')
        self.assertEqual(run['steps'][0]['sw'], '9000')
        # the action sends the APDU verbatim - no auto-handler
        self.assertEqual(_state.transport.handler.sent[-1], '00B0000004')

    def test_a_mask_sw(self):
        _state, _runner, run = run_steps(
            self,
            [{'type': 'action', 'kind': 'apdu',
              'params': {'apdu': '80CA9F7F00'}, 'check': {'sw': '6A??'}}],
            setup=lambda card: card.send('80CA', b'', '6A81'))
        self.assertEqual(run['status'], 'ok')


class EnvelopeActionTests(unittest.TestCase):
    def test_the_event_envelope(self):
        state, _runner, run = run_steps(
            self,
            [{'type': 'action', 'kind': 'envelope',
              'params': {'event': 3, 'data': '9B0100'},
              'check': {'sw': '9000'}}],
            setup=lambda card: card.envelope(b'', '9000'))
        self.assertEqual(run['status'], 'ok')
        envelopes = state.transport.handler.envelopes
        self.assertTrue(envelopes)
        # D6 0A 99 01 03 82 02 82 81 9B 01 00
        self.assertIn('D60A9901038202' '82819B0100', envelopes[0].hex().upper())

    def test_a_network_sourced_event(self):
        state, _runner, run = run_steps(
            self,
            [{'type': 'action', 'kind': 'envelope',
              'params': {'event': 0x12, 'data': '', 'src': '83'},
              'check': {'sw': '9000'}}],
            setup=lambda card: card.envelope())
        self.assertEqual(run['status'], 'ok')
        self.assertIn('82028381',
                      state.transport.handler.envelopes[0].hex().upper())

    def test_the_semantic_event_builder(self):
        state, _runner, run = run_steps(
            self,
            [{'type': 'action', 'kind': 'event',
              'params': {'event': 'access_tech', 'fields': {'tech': 0x0A}},
              'check': {'sw': '9000'}}],
            setup=lambda card: card.envelope())
        self.assertEqual(run['status'], 'ok')
        self.assertIn('BF010A',
                      state.transport.handler.envelopes[0].hex().upper())


MENU = proactive(command_details(1, 0x25, 0x00, tag=0x81),
                 ber(0x85, b'test me'),
                 ber(0x8F, bytes((1,)) + b'One'),
                 ber(0x8F, bytes((2,)) + b'Coffe'))


class MenuSelectTests(unittest.TestCase):
    def _setup(self, card):
        card.status('9103').fetch(MENU).tr('9000')

    def test_select_by_id(self):
        state, _runner, run = run_steps(
            self,
            [status_step('91??'),
             {'type': 'expect', 'command': 'SET UP MENU'},
             {'type': 'action', 'kind': 'menu-select',
              'params': {'item_id': 2}, 'check': {'sw': '9000'}}],
            setup=self._setup)
        self.assertEqual(run['status'], 'ok', run['steps'])
        envelope = state.transport.handler.envelopes[-1].hex().upper()
        self.assertIn('D30702020181900102', envelope)

    def test_select_by_text_is_case_insensitive(self):
        state, _runner, run = run_steps(
            self,
            [status_step('91??'),
             {'type': 'expect', 'command': 'SET UP MENU'},
             {'type': 'action', 'kind': 'menu-select',
              'params': {'text': 'coffe'}, 'check': {'sw': '9000'}}],
            setup=self._setup)
        self.assertEqual(run['status'], 'ok', run['steps'])
        envelope = state.transport.handler.envelopes[-1].hex().upper()
        self.assertIn('D30702020181900102', envelope)

    def test_a_text_miss_names_the_menu(self):
        _state, _runner, run = run_steps(
            self,
            [status_step('91??'),
             {'type': 'expect', 'command': 'SET UP MENU'},
             {'type': 'action', 'kind': 'menu-select',
              'params': {'text': 'nope'}, 'check': {'sw': '9000'}}],
            setup=self._setup)
        self.assertEqual(run['status'], 'error')
        self.assertIn('no menu item matches', run['steps'][2]['note'])


class FileActionTests(unittest.TestCase):
    def test_read_a_transparent_file(self):
        _state, _runner, run = run_steps(
            self,
            [{'type': 'action', 'kind': 'file-read',
              'params': {'path': 'MF/2FE2'},
              'check': {'sw': '9000', 'data': ICCID_HEX}}],
            setup=lambda card: card.send('00B00000', bytes.fromhex(ICCID_HEX)))
        self.assertEqual(run['status'], 'ok', run['steps'])
        self.assertEqual(run['steps'][0]['data'], ICCID_HEX)
        self.assertEqual(run['steps'][0]['sent'], 'READ BINARY MF/2FE2')

    def test_read_a_record(self):
        state, _runner, run = run_steps(
            self,
            [{'type': 'action', 'kind': 'file-read',
              'params': {'path': 'MF/6F07', 'mode': 'record', 'record': 1},
              'check': {'sw': '9000'}}],
            setup=lambda card: (card.send('00A40004026F07', LINEAR_FCP),
                                card.send('00B20104',
                                          bytes.fromhex('0891010000000001'))))
        self.assertEqual(run['status'], 'ok', run['steps'])
        self.assertEqual(run['steps'][0]['sent'], 'READ RECORD 1 MF/6F07')
        self.assertEqual(state.transport.handler.sent_from('00B20104'),
                         ['00B2010409'])

    def test_write_a_record(self):
        _state, _runner, run = run_steps(
            self,
            [{'type': 'action', 'kind': 'file-write',
              'params': {'path': 'MF/6F07', 'mode': 'record', 'record': 1,
                         'data': '0891010000000001'},
              'check': {'sw': '9000'}}],
            setup=lambda card: (card.send('00A40004026F07', LINEAR_FCP),
                                card.send('00DC0104', b'', '9000')))
        self.assertEqual(run['status'], 'ok', run['steps'])
        self.assertEqual(run['steps'][0]['sent'], 'UPDATE RECORD 1 MF/6F07')

    def test_a_refused_read_reports_its_status_word(self):
        _state, _runner, run = run_steps(
            self,
            [{'type': 'action', 'kind': 'file-read',
              'params': {'path': 'MF/2FE2'}, 'check': {'sw': '6982'},
              'on_fail': 'warning'}],
            setup=lambda card: card.send('00B00000', b'', '6982'))
        self.assertEqual(run['status'], 'ok', run['steps'])
        self.assertEqual(run['steps'][0]['sw'], '6982')

    def test_a_missing_file_honours_on_fail(self):
        # the model knows the file, the card refuses the SELECT (6A82)
        _state, _runner, run = run_steps(
            self,
            [{'type': 'action', 'kind': 'file-read',
              'params': {'path': 'MF/2F06'}, 'check': {'sw': '9000'},
              'on_fail': 'warning'}])
        self.assertEqual(run['status'], 'warning', run['steps'])
        self.assertEqual(run['steps'][0]['checks'][0]['label'], 'Error')


# ---- the expectation --------------------------------------------------------

DISPLAY_TEXT = proactive(command_details(2, 0x21),
                         ber(0x8D, bytes((0x04,)) + b'hello'))
SELECT_ITEM = proactive(command_details(1, 0x24),
                        ber(0x8F, bytes((1,)) + b'test'))


class ExpectTests(unittest.TestCase):
    def test_the_text_check_and_the_scripted_tr(self):
        state, _runner, run = run_steps(
            self,
            [status_step('91??'),
             {'type': 'expect', 'command': 'DISPLAY TEXT',
              'checks': [{'kind': 'text', 'value': 'hello'}],
              'respond': {'result': 'ok'}}],
            setup=lambda card: (card.status('9103').fetch(DISPLAY_TEXT)
                                .tr('9000')))
        self.assertEqual(run['status'], 'ok', run['steps'])
        step = run['steps'][1]
        self.assertEqual(check_by_label(step['checks'], 'Text')['actual'],
                         'hello')
        self.assertEqual(step['command']['type_hex'], '21')
        tr = state.transport.handler.sent_from('8014')[-1]
        self.assertIn('8103022100', tr)
        self.assertIn('83020000', tr)

    def test_the_item_check(self):
        state, _runner, run = run_steps(
            self,
            [status_step('91??'),
             {'type': 'expect', 'command': 'SELECT ITEM',
              'checks': [{'kind': 'item', 'id': 1, 'text': 'test'}],
              'respond': {'result': 'ok', 'item_id': 1}}],
            setup=lambda card: (card.status('9103').fetch(SELECT_ITEM)
                                .tr('9000')))
        self.assertEqual(run['status'], 'ok', run['steps'])
        self.assertTrue(check_by_label(run['steps'][1]['checks'],
                                       'Item')['ok'])
        tr = state.transport.handler.sent_from('8014')[-1]
        self.assertIn('100101', tr)         # the Item identifier

    def test_a_wrong_command_honours_on_fail(self):
        _state, _runner, run = run_steps(
            self,
            [status_step('91??'),
             {'type': 'expect', 'command': 'DISPLAY TEXT',
              'on_fail': 'warning'}],
            setup=lambda card: (card.status('9103').fetch(SELECT_ITEM)
                                .tr('9000')))
        self.assertEqual(run['status'], 'warning', run['steps'])
        command = check_by_label(run['steps'][1]['checks'], 'Command')
        self.assertFalse(command['ok'])
        self.assertEqual(command['level'], 'warning')

    def test_nothing_pending_fails_at_on_fail(self):
        _state, _runner, run = run_steps(
            self,
            [status_step(), {'type': 'expect', 'command': 'DISPLAY TEXT',
                             'on_fail': 'warning'}],
            setup=lambda card: card.status('9000'))
        self.assertEqual(run['status'], 'warning', run['steps'])
        check = run['steps'][1]['checks'][0]
        self.assertIn('no proactive command pending', check['detail'])

    def test_the_raw_check_and_the_qualifier(self):
        _state, _runner, run = run_steps(
            self,
            [status_step('91??'),
             {'type': 'expect', 'command': 'DISPLAY TEXT',
              'qualifier': '00',
              'checks': [{'kind': 'raw', 'mode': 'mask',
                          'value': 'D0118103022100820282818D06'
                                   + '?' * 12}]}],
            setup=lambda card: (card.status('9103').fetch(DISPLAY_TEXT)
                                .tr('9000')))
        self.assertEqual(run['status'], 'ok', run['steps'])
        self.assertTrue(check_by_label(run['steps'][1]['checks'],
                                       'Qualifier')['ok'])

    def test_the_alpha_check(self):
        # the Alpha identifier object (05/85) is distinct from the 8D text
        command = proactive(command_details(2, 0x21),
                            ber(0x05, b'abc'),
                            ber(0x8D, bytes((0x04,)) + b'hello'))
        _state, _runner, run = run_steps(
            self,
            [status_step('91??'),
             {'type': 'expect', 'command': 'DISPLAY TEXT',
              'checks': [{'kind': 'alpha', 'value': 'abc'},
                         {'kind': 'text', 'value': 'hello'}]}],
            setup=lambda card: (card.status('9103').fetch(command)
                                .tr('9000')))
        self.assertEqual(run['status'], 'ok', run['steps'])
        self.assertEqual(check_by_label(run['steps'][1]['checks'],
                                        'Alpha')['actual'], 'abc')

    def test_the_files_check(self):
        refresh = proactive(command_details(1, 0x01),
                            ber(0x12, bytes((2,)) + bytes.fromhex('3F00')
                                + bytes.fromhex('7F20')))
        _state, _runner, run = run_steps(
            self,
            [status_step('91??'),
             {'type': 'expect', 'command': 'REFRESH',
              'checks': [{'kind': 'files', 'files': ['3F007F20']}]}],
            setup=lambda card: (card.status('9103').fetch(refresh)
                                .tr('9000')))
        self.assertEqual(run['status'], 'ok', run['steps'])
        self.assertTrue(check_by_label(run['steps'][1]['checks'],
                                       'Files')['ok'])

    def test_the_sms_check(self):
        # SEND SHORT MESSAGE carrying an SMS-SUBMIT to +1234567890
        tpdu = bytes.fromhex('01000A912143658709000002AB')
        command = proactive(
            command_details(1, 0x13),
            # the address object (06) then the 8B TPDU
            ber(0x06, bytes.fromhex('0A91') + bytes.fromhex('2143658709')),
            ber(0x8B, tpdu))
        _state, _runner, run = run_steps(
            self,
            [status_step('91??'),
             {'type': 'expect', 'command': 'SEND SHORT MESSAGE',
              'checks': [{'kind': 'sms', 'da': '+1234567890', 'dcs': '00',
                          'pid': '00'}]}],
            setup=lambda card: (card.status('9103').fetch(command)
                                .tr('9000')))
        self.assertEqual(run['status'], 'ok', run['steps'])
        self.assertEqual(check_by_label(run['steps'][1]['checks'],
                                        'SMS DA')['ok'], True)


class TerminalAnswerTests(unittest.TestCase):
    """The terminal's own objects in a scripted TERMINAL RESPONSE (v3.22.2).

    Without them a scripted PROVIDE LOCAL INFORMATION stalls an applet that
    waits for its data, and a POLL INTERVAL answer does not echo the interval
    (TS 102 223 §6.4.15/§6.8.4).
    """

    def setUp(self):
        self.state = make_state(self, Card())
        self.runner = self.state.test_runner
        self.session = self.state.cat

    def test_a_poll_interval_answer_echoes_the_84_duration(self):
        # the documented deviation: the runner's TR has shipped the CR-set
        # Duration (84) since v3.22.2 - §6.8.0 says the response objects
        # *should* use 04, and the card accepts 84 (the legacy test pins the
        # same bytes: assertRegex(..., r'840201[0-9A-F]{2}'))
        self.session.poll_interval = 30
        tr = self.runner._terminal_response(1, 0x03, 0x82, 0x81, 0x00,
                                           {'result': 0})
        self.assertRegex(tr.hex().upper(), r'8402011E$')
        self.assertNotIn(bytes.fromhex('0402011E'), tr)

    def test_a_pli_answer_carries_the_dictionary_entry(self):
        # qualifier 05 = Timing Advance: the dictionary default (TS 102 223
        # 8.x, tag 2E/AE)
        tr = self.runner._terminal_response(1, 0x26, 0x82, 0x81, 0x05,
                                           {'result': 0})
        self.assertTrue(tr.hex().upper().endswith('2E020000'), tr.hex())
        self.session.pli_data[0x00] = '130700F11000020002'
        tr = self.runner._terminal_response(1, 0x26, 0x82, 0x81, 0x00,
                                           {'result': 0})
        self.assertTrue(tr.hex().upper().endswith('130700F11000020002'))

    def test_the_date_time_qualifier_is_answered_from_the_host_clock(self):
        # an empty entry answers with the current date/time (6.4.15) coded as
        # the 8.39 object (TP-SCTS): base tag 26, length 07
        tr = self.runner._terminal_response(1, 0x26, 0x82, 0x81, 0x03,
                                           {'result': 0})
        self.assertRegex(tr.hex().upper(), r'2607[0-9A-F]{14}$')

    def test_a_raw_or_failed_answer_keeps_the_terminal_objects_out(self):
        # the script's raw TLVs win; a non-ok result carries no data objects
        self.session.poll_interval = 30
        raw = self.runner._terminal_response(1, 0x03, 0x82, 0x81, 0x00,
                                            {'result': 0, 'raw': 'AABB'})
        self.assertTrue(raw.hex().upper().endswith('AABB'))
        self.assertNotIn('840201', raw.hex().upper())
        failed = self.runner._terminal_response(1, 0x03, 0x82, 0x81, 0x00,
                                               {'result': 0x22})
        self.assertNotIn('840201', failed.hex().upper())


def por_packet(status=0x00, secured='', tar='000000', cntr='0000000010'):
    """An SCP80 response packet (TS 102 225 §5.2 Table 3) with the RPI UDH."""
    body = (bytes.fromhex(tar + cntr) + bytes([0x00, status])
            + bytes.fromhex(secured))
    return (bytes.fromhex('027100') + len(body).to_bytes(2, 'big')
            + bytes([10]) + body)


class Scp80Tests(unittest.TestCase):
    def _run(self, card_arm, steps):
        state = make_state(self, Card())
        state.transport.handler  # the card is equipped; arm it now
        card_arm(state.transport.handler)
        preset = state.presets.add(preset_fields())
        script = normalise(steps)
        runner = state.test_runner
        runner.start(script, preset, spawn=False)
        runner.execute(script, preset)
        return state, preset, runner.snapshot()

    def test_a_packet_and_its_por(self):
        state, preset, run = self._run(
            lambda card: card.envelope(por_packet(secured='019000'), '9000'),
            [{'type': 'action', 'kind': 'scp80',
              'params': {'apdu': '80E2900000'},
              'check': {'sw': '9000', 'por': 'ok'}}])
        self.assertEqual(run['status'], 'ok', run['steps'])
        self.assertEqual(run['steps'][0]['por']['response_status'], 'por_ok')
        self.assertEqual(run['steps'][0]['counter'], '0000000010')
        # the counter the card consumed is persisted into the store
        stored = state.presets.get(preset['id'])
        self.assertEqual(stored['keysets'][0]['cntr'], '0000000011')
        self.assertEqual(run['scp80_counter'], '0000000011')

    def test_a_por_object_assertion(self):
        _state, _preset, run = self._run(
            lambda card: card.envelope(por_packet(secured='016A88'), '9000'),
            [{'type': 'action', 'kind': 'scp80',
              'params': {'apdu': '80E2900000'},
              'check': {'sw': '9000', 'por': {'status': 'ok', 'sw': '6A88'}}}])
        self.assertEqual(run['status'], 'ok', run['steps'])
        self.assertEqual(
            check_by_label(run['steps'][0]['checks'], 'PoR SW')['actual'],
            '6a88')

    def test_a_refused_keyset_number_is_a_step_error(self):
        _state, _preset, run = self._run(
            lambda card: None,
            [{'type': 'action', 'kind': 'scp80',
              'params': {'apdu': '80E2900000', 'kvn': 2},
              'check': {'sw': '9000'}, 'on_fail': 'warning'}])
        self.assertEqual(run['status'], 'warning')
        self.assertIn('keyset 2', run['steps'][0]['note'])

    def test_a_rejected_packet_leaves_the_counter_untouched(self):
        # a 6200 ENVELOPE carrying a cntr_low PoR: the card refused the packet
        # and did not consume the counter, so the store must not move (the
        # three-way rule's clear-rejection branch; the legacy test runner
        # advanced on any status word)
        state, preset, run = self._run(
            lambda card: card.envelope(
                por_packet(status=0x02, secured='019000'), '6200'),
            [{'type': 'action', 'kind': 'scp80',
              'params': {'apdu': '80E2900000'},
              'check': {'sw': '62??', 'por': 'any'}}])
        self.assertEqual(run['status'], 'ok', run['steps'])
        self.assertEqual(run['steps'][0]['por']['response_status'], 'cntr_low')
        stored = state.presets.get(preset['id'])
        self.assertEqual(stored['keysets'][0]['cntr'], '0000000010')
        self.assertEqual(run['scp80_counters'], {'1': '0000000010'})

    def test_a_9000_envelope_without_a_por_advances_the_counter(self):
        # SPI2 00 asks for no PoR: a clean ENVELOPE is a clear acceptance
        state, preset, run = self._run(
            lambda card: card.envelope(b'', '9000'),
            [{'type': 'action', 'kind': 'scp80',
              'params': {'apdu': '80E2900000', 'spi2': '00'},
              'check': {'sw': '9000', 'por': 'none'}}])
        self.assertEqual(run['status'], 'ok', run['steps'])
        self.assertEqual(state.presets.get(preset['id'])
                         ['keysets'][0]['cntr'], '0000000011')

    def test_an_unclear_refusal_still_advances_the_counter(self):
        # no PoR and a 6A82 ENVELOPE: unclear, not a proof of rejection - the
        # three-way rule advances (a skipped value is harmless, a card left
        # expecting a higher counter answers cntr_low forever)
        state, preset, run = self._run(
            lambda card: card.envelope(b'', '6A82'),
            [{'type': 'action', 'kind': 'scp80',
              'params': {'apdu': '80E2900000'},
              'check': {'sw': '6A82', 'por': 'any'}}])
        self.assertNotIn('por', run['steps'][0])
        self.assertEqual(state.presets.get(preset['id'])
                         ['keysets'][0]['cntr'], '0000000011')

    def test_a_transport_error_still_advances_the_counter(self):
        # the packet may have reached the card before the link died: the
        # outcome is unclear, so the counter advances; the step still fails
        state = make_state(self, Card())
        card = state.transport.handler
        card.envelope(b'', '9000')

        def handler(apdu):
            if bytes(apdu).hex().upper().startswith('80C2'):
                raise OSError('link lost')
            return card(apdu)

        state.transport.handler = handler
        preset = state.presets.add(preset_fields())
        script = normalise([{'type': 'action', 'kind': 'scp80',
                             'params': {'apdu': '80E2900000'}}])
        runner = state.test_runner
        runner.start(script, preset, spawn=False)
        runner.execute(script, preset)
        run = runner.snapshot()
        self.assertEqual(run['status'], 'error')
        self.assertIn('link lost', run['steps'][0]['note'])
        self.assertEqual(state.presets.get(preset['id'])
                         ['keysets'][0]['cntr'], '0000000011')

    def test_a_pending_command_survives_the_packet(self):
        # handle_proactive=False: a 91XX answer stays for the next expectation
        state, _preset, run = self._run(
            lambda card: (card.envelope(b'', '9110')
                          .fetch(SELECT_ITEM, '9000').tr('9000')),
            [{'type': 'action', 'kind': 'scp80',
              'params': {'apdu': '80E2900000'}, 'check': {'sw': '91??'}},
             {'type': 'expect', 'command': 'SELECT ITEM'}])
        self.assertEqual(run['status'], 'ok', run['steps'])
        self.assertEqual(len(state.transport.handler.sent_from('8012')), 1)

    def test_a_keyless_step_is_refused_by_the_precondition(self):
        script = normalise([{'type': 'action', 'kind': 'scp80',
                             'params': {'apdu': '80E2900000'}}])
        error = testrun.preset_error(script, {'keysets': [], 'tars': []})
        self.assertIn('no keyset', error)


class ExpectPorTests(unittest.TestCase):
    """The PoR carried by a FETCHed SEND SHORT MESSAGE (the submit transport).

    ``_expect_por`` decodes the response packet of the preceding scp80 step
    from the applet's own SMS-SUBMIT (TS 102 225 §5.1.1 PoR-in-submit, bit
    ``0x20``); a concatenated submit is assembled across expectations.
    """

    def setUp(self):
        self.state = make_state(self, Card())
        self.runner = self.state.test_runner
        self.ctx = {'last_scp80': {'spi1': '16', 'spi2': '01',
                                   'kic': '15', 'kid': '15',
                                   'counter': '0000000010',
                                   'kicKey': KIC_KEY, 'kidKey': KID_KEY,
                                   'tar': '000000', 'cmd_len': 5, 'rm': True}}

    def _por_body(self, secured='019000'):
        """The response packet *after* the RPI IE UDH (the submit's UD)."""
        return bytes.fromhex(por_packet(secured=secured).hex()[6:])

    def test_the_submit_por_is_decoded_with_the_scp80_context(self):
        por = self.runner._expect_por(
            self.ctx, send_sm_command(submit_tpdu(self._por_body())))
        self.assertEqual(por['response_status'], 'por_ok')
        self.assertEqual(por['tar'], '000000')
        self.assertEqual(por['cntr'], '0000000010')
        self.assertEqual(por['decoded']['last_status_word'], '9000')

    def test_a_concatenated_submit_assembles_before_decoding(self):
        body = self._por_body()
        first, second = body[:7], body[7:]
        por = self.runner._expect_por(
            self.ctx, send_sm_command(
                submit_tpdu(first, concat=(1, 2, 1), serial=1), cmd_num=1))
        # one segment is not a PoR yet
        self.assertIsNone(por)
        self.assertEqual(len(self.ctx['por_segments']), 1)
        por = self.runner._expect_por(
            self.ctx, send_sm_command(
                submit_tpdu(second, concat=(1, 2, 2), serial=2), cmd_num=2))
        self.assertIsNotNone(por)
        self.assertEqual(por['response_status'], 'por_ok')
        self.assertEqual(por['decoded']['last_status_word'], '9000')

    def test_a_second_concatenation_reference_is_not_mixed_in(self):
        body = self._por_body()
        self.runner._expect_por(
            self.ctx, send_sm_command(
                submit_tpdu(body[:7], concat=(1, 2, 1)), cmd_num=1))
        # a segment of another concatenation does not complete ref 1
        por = self.runner._expect_por(
            self.ctx, send_sm_command(
                submit_tpdu(body[7:], concat=(2, 2, 2)), cmd_num=2))
        self.assertIsNone(por)

    def test_no_scp80_context_or_no_tpdu_is_no_por(self):
        raw = send_sm_command(submit_tpdu(self._por_body()))
        self.assertIsNone(self.runner._expect_por({}, raw))
        # a command without an SMS TPDU (a SELECT ITEM) carries no PoR
        self.assertIsNone(self.runner._expect_por(
            self.ctx, proactive(command_details(1, 0x24))))


class SendOtaCounterTests(unittest.TestCase):
    """The plain send follows the same three-way counter rule (3c-3d-1).

    It lives here because the rule was decided with the runner's scp80 step.
    The exception path used to leave the store untouched although the packet
    may have reached the card before the link died - and a card left expecting
    a higher counter answers ``cntr_low`` to every later packet.
    """

    def test_a_transport_error_persists_the_unclear_counter(self):
        state = make_state(self, Card())
        preset = state.presets.add(preset_fields())
        state._send_secured_packet = mock.Mock(side_effect=OSError('link lost'))
        with self.assertRaises(OSError):
            state.send_ota({'apdu': '80E2900000', 'preset_id': preset['id']})
        self.assertEqual(state.presets.get(preset['id'])
                         ['keysets'][0]['cntr'], '0000000011')

    def test_a_decoded_rejection_does_not_move_the_counter(self):
        state = make_state(self, Card())
        preset = state.presets.add(preset_fields())
        state._send_secured_packet = mock.Mock(return_value={
            'success': False, 'sw': '6200', 'bytes': 20, 'segments': 1,
            'response_data': por_packet(status=0x02, secured='019000').hex(),
            'error': 'ENVELOPE failed at segment 1'})
        resp = state.send_ota({'apdu': '80E2900000',
                               'preset_id': preset['id']})
        self.assertFalse(resp['success'])
        self.assertEqual(resp['por']['response_status'], 'cntr_low')
        self.assertTrue(resp['cntr_low'])
        self.assertNotIn('final_cntr', resp)
        self.assertEqual(state.presets.get(preset['id'])
                         ['keysets'][0]['cntr'], '0000000010')


# ---- the proactive drain ----------------------------------------------------

class DrainTests(unittest.TestCase):
    def test_the_drain_consumes_the_commands(self):
        _state, _runner, run = run_steps(
            self,
            [{'type': 'action', 'kind': 'proactive-drain',
              'params': {'attempts': 3,
                         'require': {'command': 'SELECT ITEM'}},
              'check': {'sw': '9000'}}],
            setup=lambda card: (card.status('9103').fetch(SELECT_ITEM, '9103')
                                .tr('9000').status('9000')))
        self.assertEqual(run['status'], 'ok', run['steps'])
        self.assertEqual(len(run['steps'][0]['drained']), 1)
        self.assertTrue(check_by_label(run['steps'][0]['checks'],
                                       'Require')['ok'])

    def test_an_empty_drain_with_a_require_fails(self):
        _state, _runner, run = run_steps(
            self,
            [{'type': 'action', 'kind': 'proactive-drain',
              'params': {'attempts': 1, 'require': {'command': 'SELECT ITEM'}},
              'check': {'sw': '9000'}, 'on_fail': 'warning'}],
            setup=lambda card: card.status('9000'))
        self.assertEqual(run['status'], 'warning', run['steps'])
        self.assertIn('nothing was pending',
                      check_by_label(run['steps'][0]['checks'],
                                     'Require')['actual'])

    def test_the_drain_is_exempt_from_the_pending_guard(self):
        # a status action leaves a command pending, the drain consumes it
        _state, _runner, run = run_steps(
            self,
            [status_step('91??'),
             {'type': 'action', 'kind': 'proactive-drain',
              'params': {'attempts': 2}, 'check': {'sw': '9000'}}],
            setup=lambda card: (card.status('9103').fetch(SELECT_ITEM, '9103')
                                .tr('9000').status('9000')))
        self.assertEqual(run['status'], 'ok', run['steps'])


# ---- the loop semantics -----------------------------------------------------

class LoopTests(unittest.TestCase):
    def test_a_warning_continues_and_an_error_stops(self):
        _state, _runner, run = run_steps(
            self,
            [status_step(), status_step(on_fail='warning'), status_step()],
            setup=lambda card: (card.status('9000').status('6A82')
                                .status('9000')))
        self.assertEqual(run['status'], 'warning')
        self.assertEqual([step['status'] for step in run['steps']],
                         ['ok', 'warning', 'ok'])

        _state, _runner, run = run_steps(
            self, [status_step(), status_step()],
            setup=lambda card: (card.status('6A82').status('9000')))
        self.assertEqual(run['status'], 'error')
        self.assertEqual(len(run['steps']), 1)

    def test_a_raised_error_honours_on_fail(self):
        _state, _runner, run = run_steps(
            self,
            [{'type': 'action', 'kind': 'menu-select',
              'params': {'text': 'missing'}, 'check': {'sw': '9000'},
              'on_fail': 'warning'},
             status_step()],
            setup=lambda card: card.status('9000'))
        self.assertEqual(run['status'], 'warning')
        self.assertEqual(run['steps'][0]['checks'][0]['label'], 'Error')
        self.assertEqual(len(run['steps']), 2)

    def test_an_unexpected_pending_command_stops_the_run(self):
        state, _runner, run = run_steps(
            self, [status_step('91??'), status_step()],
            setup=lambda card: (card.status('9103').fetch(SELECT_ITEM, '9000')
                                .tr('9000').status('9000')))
        self.assertEqual(run['status'], 'error')
        self.assertIn('unexpected proactive command pending',
                      run['steps'][1]['note'])
        self.assertEqual(len(state.transport.handler.sent_from('8012')), 1)

    def test_a_pending_command_at_the_end_is_drained(self):
        _state, _runner, run = run_steps(
            self, [status_step('9103')],
            setup=lambda card: (card.status('9103').fetch(SELECT_ITEM, '9000')
                                .tr('9000')))
        # a command still pending at the end is an error (the legacy rule:
        # the run drained it, but the script left the card mid-session)
        self.assertEqual(run['status'], 'error')
        cleanup = run['steps'][-1]
        self.assertEqual(cleanup['kind'], 'cleanup')
        self.assertEqual(cleanup['status'], 'error')
        self.assertIn('pending SELECT ITEM', cleanup['note'])

    def test_the_log_buffer_and_the_step_index(self):
        _state, _runner, run = run_steps(
            self, [status_step(), status_step()],
            setup=lambda card: card.status('9000'))
        self.assertEqual(run['index'], 1)
        self.assertTrue(any('step 1: STATUS 1/1' in line
                            for line in run['log']), run['log'])
        self.assertTrue(any('step 2: STATUS 1/1' in line
                            for line in run['log']), run['log'])

    def test_the_log_buffer_is_bounded(self):
        state = make_state(self, Card())
        runner = state.test_runner
        runner.start(normalise([dict(STATUS_9K)]), {}, spawn=False)
        for index in range(testrun.TEST_LOG_MAX + 10):
            runner._log('line %d' % index)
        with runner._lock:
            self.assertEqual(len(runner._run['log']), testrun.TEST_LOG_MAX)

    def test_a_stop_request_ends_the_loop(self):
        # the stop flag was set *before* the first step: nothing runs
        state = make_state(self, Card())
        runner = state.test_runner
        script = normalise([status_step(), status_step()])
        runner.start(script, {}, spawn=False)
        runner._run['stop'] = True
        runner.execute(script, {})
        run = runner.snapshot()
        self.assertEqual(run['status'], 'stopped')
        self.assertEqual(run['steps'], [])

    def test_a_card_session_change_stops_the_run(self):
        state = make_state(self, Card())
        state.transport.handler.status('9000')
        script = normalise([status_step(), status_step()])
        runner = state.test_runner
        runner.start(script, {}, spawn=False)
        real = runner._run_status
        calls = []

        def wrapped(step):
            calls.append(step)
            state.card_session += 1          # a swap during the run
            return real(step)

        runner._run_status = wrapped
        runner.execute(script, {})
        run = runner.snapshot()
        self.assertEqual(run['status'], 'error')
        self.assertIn('card session changed', run['steps'][1]['note'])
        self.assertEqual(len(calls), 1)


# ---- the ADM gate -----------------------------------------------------------

class AdmGateTests(unittest.TestCase):
    def setUp(self):
        self.state = make_state(self, Card())
        self.runner = self.state.test_runner

    def test_not_needed_passes(self):
        ok, info, error = self.runner.ensure_adm({}, False)
        self.assertTrue(ok)
        self.assertIsNone(error)
        self.assertFalse(info['required'])

    def test_the_latch_passes(self):
        self.state.adm_session = self.state.card_session
        ok, info, error = self.runner.ensure_adm({}, True)
        self.assertTrue(ok)
        self.assertTrue(info['verified'])

    def test_the_preset_key_is_verified_once(self):
        calls = []

        def verify(body):
            calls.append(body)
            return {'ok': True, 'sw': '9000'}

        self.state.verify_adm = verify
        ok, info, error = self.runner.ensure_adm({'adm': 'AABBCCDD'}, True)
        self.assertTrue(ok, error)
        self.assertTrue(info['checked'])
        self.assertEqual(calls, [{'adm': 'AABBCCDD'}])
        self.assertEqual(self.state.adm_session, self.state.card_session)
        # the same session does not verify again
        calls[:] = []
        ok, _info, _error = self.runner.ensure_adm({'adm': 'AABBCCDD'}, True)
        self.assertTrue(ok)
        self.assertEqual(calls, [])

    def test_a_failure_is_not_retried(self):
        self.state.verify_adm = lambda body: {'ok': False, 'sw': '63C2',
                                              'attempts_left': 2}
        ok, info, error = self.runner.ensure_adm({'adm': 'AABBCCDD'}, True)
        self.assertFalse(ok)
        self.assertIn('63C2', error)
        self.assertIn('2 attempt(s) left', error)
        self.assertEqual(info['attempts_left'], 2)
        self.assertEqual(self.state.adm_failed_session,
                         self.state.card_session)
        calls = []
        self.state.verify_adm = lambda body: calls.append(body) or {'ok': True}
        ok, _info, error = self.runner.ensure_adm({'adm': 'AABBCCDD'}, True)
        self.assertFalse(ok)
        self.assertIn('verify it manually', error)
        self.assertEqual(calls, [])

    def test_no_key_refuses(self):
        ok, _info, error = self.runner.ensure_adm({}, True)
        self.assertFalse(ok)
        self.assertIn('no ADM key', error)

    def test_a_new_session_clears_the_failure(self):
        self.state.adm_failed_session = self.state.card_session
        self.state.card_session += 1
        calls = []
        self.state.verify_adm = lambda body: calls.append(body) or {'ok': True}
        ok, _info, _error = self.runner.ensure_adm({'adm': 'AABBCCDD'}, True)
        self.assertTrue(ok)
        self.assertEqual(len(calls), 1)

    def test_the_endpoint_refuses_a_failed_gate(self):
        state = make_state(self, Card())
        state.verify_adm = lambda body: {'ok': False, 'sw': '63C1',
                                         'attempts_left': 1}
        preset = state.presets.add(preset_fields(adm='AABBCCDD'))
        harness = Harness(state)
        self.addCleanup(harness.close)
        status, body = harness.json(
            'POST', '/api/test/run',
            {'script': dict(RUN_STATUS_SCRIPT, require_adm=True),
             'preset_id': preset['id']})
        self.assertEqual(status, 400)
        self.assertIn('63C1', body['error'])
        self.assertEqual(body['adm']['attempts_left'], 1)
        self.assertFalse(state.test_running)

    def test_the_endpoint_passes_a_verified_gate(self):
        state = make_state(self, Card())
        state.verify_adm = lambda body: {'ok': True, 'sw': '9000'}
        preset = state.presets.add(preset_fields(adm='AABBCCDD'))
        harness = Harness(state)
        self.addCleanup(harness.close)
        status, body = harness.json(
            'POST', '/api/test/run',
            {'script': dict(RUN_STATUS_SCRIPT, require_adm=True),
             'preset_id': preset['id']})
        self.assertEqual(status, 200, body)
        self.assertTrue(body['adm']['verified'])
        deadline = time.time() + 5
        while time.time() < deadline and state.test_running:
            time.sleep(0.02)
        self.assertFalse(state.test_running)

    def test_a_stored_script_carries_its_require_adm(self):
        state = make_state(self, Card())
        state.transport.handler.status('9000')
        preset = state.presets.add(preset_fields())      # no ADM key
        harness = Harness(state)
        self.addCleanup(harness.close)
        status, body = harness.json(
            'POST', '/api/test/scripts',
            dict(RUN_STATUS_SCRIPT, require_adm=True))
        self.assertEqual(status, 200, body)
        sid = body['script']['id']
        status, body = harness.json('POST', '/api/test/run',
                                    {'script_id': sid,
                                     'preset_id': preset['id']})
        self.assertEqual(status, 400)
        self.assertIn('no ADM key', body['error'])
        self.assertFalse(state.test_running)


# ---- the load test ----------------------------------------------------------

class LoadTests(unittest.TestCase):
    def _state(self):
        state = make_state(self, Card())
        state.transport.handler.status('9000')
        return state

    def test_successful_iterations_are_discarded(self):
        state = self._state()
        script = normalise([dict(STATUS_9K)])
        runner = state.test_runner
        runner.start(script, {}, iterations=4, spawn=False)
        runner._load_worker(script, {}, 4)
        run = runner.snapshot()
        self.assertEqual(run['status'], 'ok')
        self.assertEqual(run['load']['done'], 4)
        self.assertEqual(run['load']['iteration'], 4)
        self.assertIsNone(run['load']['failed_iteration'])
        self.assertEqual(run['steps'], [])
        self.assertEqual(run['log'], [])
        self.assertIsNotNone(run['load']['elapsed_ms'])
        self.assertFalse(run['running'])

    def test_an_error_keeps_that_iteration(self):
        state = make_state(self, Card())
        state.transport.handler.status('9000').status('6A82')
        script = normalise([dict(STATUS_9K)])
        runner = state.test_runner
        runner.start(script, {}, iterations=5, spawn=False)
        runner._load_worker(script, {}, 5)
        run = runner.snapshot()
        self.assertEqual(run['status'], 'error')
        self.assertEqual(run['load']['done'], 1)
        self.assertEqual(run['load']['failed_iteration'], 2)
        self.assertEqual(run['error'], 'iteration 2 failed')
        self.assertEqual(len(run['steps']), 1)
        self.assertEqual(run['steps'][0]['sw'], '6A82')

    def test_a_manual_stop_ends_after_the_iteration(self):
        state = self._state()
        script = normalise([dict(STATUS_9K)])
        runner = state.test_runner
        runner.start(script, {}, iterations=50, spawn=False)
        real = runner._run_status
        calls = []

        def wrapped(step):
            calls.append(step)
            if len(calls) == 2:
                runner._run['stop'] = True
            return real(step)

        runner._run_status = wrapped
        runner._load_worker(script, {}, 50)
        run = runner.snapshot()
        self.assertEqual(run['status'], 'stopped')
        self.assertEqual(run['load']['done'], 2)
        self.assertEqual(len(calls), 2)

    def test_a_session_change_between_iterations_is_an_error(self):
        state = self._state()
        script = normalise([dict(STATUS_9K)])
        runner = state.test_runner
        runner.start(script, {}, iterations=10, spawn=False)
        real = runner._run_status
        calls = []

        def wrapped(step):
            calls.append(step)
            state.card_session += 1
            return real(step)

        runner._run_status = wrapped
        runner._load_worker(script, {}, 10)
        run = runner.snapshot()
        self.assertEqual(run['status'], 'error')
        self.assertEqual(run['load']['failed_iteration'], 2)
        self.assertIn('card session changed', run['error'])
        self.assertEqual(len(calls), 1)

    def test_the_scp80_counter_is_shared(self):
        state = make_state(self, Card())
        card = state.transport.handler
        card.envelope(por_packet(secured='019000'))
        preset = state.presets.add(preset_fields())
        script = normalise([{'type': 'action', 'kind': 'scp80',
                             'params': {'apdu': '80E2900000'}}])
        runner = state.test_runner
        runner.start(script, preset, iterations=2, spawn=False)
        runner._load_worker(script, preset, 2)
        run = runner.snapshot()
        self.assertEqual(run['status'], 'ok', run['steps'])
        self.assertEqual(run['scp80_counters'], {'1': '0000000012'})
        stored = state.presets.get(preset['id'])
        self.assertEqual(stored['keysets'][0]['cntr'], '0000000012')

    def test_a_raised_error_finalises_the_run(self):
        state = self._state()
        script = normalise([dict(STATUS_9K)])
        runner = state.test_runner
        runner.start(script, {}, iterations=5, spawn=False)

        def boom(*args, **kwargs):
            raise OSError('card vanished')

        runner.run_script = boom
        runner._load_worker(script, {}, 5)
        run = runner.snapshot()
        self.assertFalse(run['running'])
        self.assertEqual(run['status'], 'error')
        self.assertIn('card vanished', run['error'])
        self.assertEqual(run['load']['failed_iteration'], 1)

    def test_the_average_covers_the_failing_iteration(self):
        state = make_state(self, Card())
        state.transport.handler.status('9000').status('6A82')
        script = normalise([dict(STATUS_9K)])
        runner = state.test_runner
        runner.start(script, {}, iterations=10, spawn=False)
        runner._load_worker(script, {}, 10)
        load = runner.snapshot()['load']
        self.assertEqual(load['done'], 1)
        self.assertIsNotNone(load['avg_ms'])
        self.assertLessEqual(load['avg_ms'], load['elapsed_ms'])

    def test_the_start_line_names_the_iterations(self):
        # the legacy logs `TEST-LOAD: script %r, %d iterations` before the
        # worker starts - the bench's "did the load test really run N times?"
        state = self._state()
        script = normalise([dict(STATUS_9K)])
        runner = state.test_runner
        with self.assertLogs('simple_server.testrun', level='INFO') as logs:
            runner.start(script, {}, iterations=3, spawn=False)
        self.assertTrue(
            any('TEST-LOAD: script' in line and '3 iterations' in line
                for line in logs.output), logs.output)

    def test_the_endpoint_runs_a_load_test(self):
        state = make_state(self, Card())
        state.transport.handler.status('9000')
        harness = Harness(state)
        self.addCleanup(harness.close)
        status, body = harness.json(
            'POST', '/api/test/run',
            {'script': RUN_STATUS_SCRIPT, 'iterations': 3})
        self.assertEqual(status, 200, body)
        self.assertTrue(body['load']['enabled'])
        self.assertEqual(body['load']['iterations'], 3)
        deadline = time.time() + 10
        while time.time() < deadline:
            _status, body = harness.json('GET', '/api/test/status')
            if not body['running']:
                break
            time.sleep(0.02)
        self.assertEqual(body['load']['done'], 3)
        self.assertEqual(body['status'], 'ok')


# ---- the watchdog -----------------------------------------------------------

class WatchdogTests(unittest.TestCase):
    def setUp(self):
        self.state = make_state(self, Card())
        self.state.transport.handler.status('9000')
        self.runner = self.state.test_runner
        self.script = normalise([dict(STATUS_9K)])

    def test_an_idle_runner_is_not_watched(self):
        self.assertIsNone(self.runner.watchdog_check())
        self.assertFalse(self.runner.running)

    def test_a_fresh_heartbeat_is_ignored(self):
        self.runner.start(self.script, {}, spawn=False)
        now = time.time()
        self.runner._run['heartbeat'] = now
        with mock.patch.object(self.state, 'abort_card_transport') as abort:
            self.assertIsNone(self.runner.watchdog_check(now + 1))
        abort.assert_not_called()

    def test_a_stale_heartbeat_fails_the_run(self):
        self.runner.start(self.script, {}, spawn=False)
        now = time.time()
        self.runner._run['heartbeat'] = now - 100
        self.runner._run['steps'].append(
            {'index': 4, 'status': 'running', 'started': now - 100})
        with mock.patch.object(self.state,
                               'abort_card_transport') as abort:
            message = self.runner.watchdog_check(now)
        self.assertIn('step 5', message)
        self.assertIn('stopped responding', message)
        self.assertTrue(self.runner._run['stuck'])
        self.assertTrue(self.runner._run['stop'])
        self.assertEqual(self.runner._run['error'], message)
        abort.assert_called_once()

    def test_the_abort_happens_once_per_heartbeat(self):
        self.runner.start(self.script, {}, spawn=False)
        now = time.time()
        self.runner._run['heartbeat'] = now - 100
        with mock.patch.object(self.state,
                               'abort_card_transport') as abort:
            self.assertIsNotNone(self.runner.watchdog_check(now))
            self.assertIsNone(self.runner.watchdog_check(now + 1))
        abort.assert_called_once()
        # new progress re-arms it
        self.runner._run['heartbeat'] = now
        with mock.patch.object(self.state,
                               'abort_card_transport') as abort:
            self.assertIsNotNone(self.runner.watchdog_check(now + 100))
        abort.assert_called_once()

    def test_a_stuck_run_finalises_as_error(self):
        self.runner.start(self.script, {}, spawn=False)
        self.runner._run['stuck'] = True
        self.runner._run['stop'] = True
        self.runner.execute(self.script, {})
        run = self.runner.snapshot()
        self.assertEqual(run['status'], 'error')
        self.assertFalse(run['running'])

    def test_the_control_stop_without_the_stuck_flag(self):
        self.runner.start(self.script, {}, spawn=False)
        self.runner._run['stop'] = True
        self.runner.execute(self.script, {})
        self.assertEqual(self.runner.snapshot()['status'], 'stopped')

    def test_a_blocked_step_ends_the_run_as_error(self):
        entered = threading.Event()
        release = threading.Event()

        class BlockingCard(Card):
            armed = False

            def __call__(card_self, apdu):
                if not card_self.armed:
                    return Card.__call__(card_self, apdu)
                if bytes(apdu).hex().upper().startswith('80F2000C') \
                        and not entered.is_set():
                    entered.set()
                    release.wait(5)
                return Card.__call__(card_self, apdu)

        state = make_state(self, BlockingCard())
        state.transport.handler.armed = True
        runner = state.test_runner
        runner.step_timeout = 0.2
        script = normalise([status_step(on_fail='warning')])
        runner.start(script, {}, spawn=False)
        worker = threading.Thread(target=runner.execute, args=(script, {}),
                                  daemon=True)
        worker.start()
        self.assertTrue(entered.wait(2))
        time.sleep(0.3)
        with mock.patch.object(state, 'abort_card_transport') as abort:
            message = runner.watchdog_check()
        self.assertIn('stopped responding', message)
        abort.assert_called_once()
        release.set()
        worker.join(5)
        self.assertFalse(worker.is_alive())
        run = runner.snapshot()
        self.assertTrue(run['stuck'])
        self.assertEqual(run['status'], 'error')
        self.assertFalse(run['running'])

    def test_the_abort_disconnects_on_a_background_thread(self):
        # a synchronous disconnect would block behind the stuck transmit
        released = threading.Event()

        class Connection(object):
            def __init__(self):
                self.calls = 0
                self.done = threading.Event()

            def disconnect(self):
                self.calls += 1
                released.wait(2)
                self.done.set()

        connection = Connection()
        state = self.state
        old = state.transport
        state.transport = type('Link', (), {'_con': connection})()
        self.addCleanup(setattr, state, 'transport', old)
        started = time.time()
        self.assertTrue(state.abort_card_transport())
        # the call does not wait for the disconnect (pcsc-lite serializes it
        # behind the stuck transmit)
        self.assertLess(time.time() - started, 1.0)
        self.assertFalse(connection.done.is_set())
        released.set()
        self.assertTrue(connection.done.wait(2))
        self.assertEqual(connection.calls, 1)
        self.assertTrue(self._wait_stale(state))

    @staticmethod
    def _wait_stale(state, timeout=2.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if state._transport_stale:
                return True
            time.sleep(0.01)
        return False

    def test_a_missing_connection_is_not_fatal(self):
        state = self.state
        old = state.transport
        state.transport = None
        self.addCleanup(setattr, state, 'transport', old)
        self.assertFalse(state.abort_card_transport())
        self.assertTrue(state._transport_stale)

    def test_a_failing_disconnect_is_swallowed(self):
        class Connection(object):
            def __init__(self):
                self.done = threading.Event()

            def disconnect(self):
                try:
                    raise RuntimeError('dead handle')
                finally:
                    self.done.set()

        connection = Connection()
        state = self.state
        old = state.transport
        state.transport = type('Link', (), {'_con': connection})()
        self.addCleanup(setattr, state, 'transport', old)
        self.assertTrue(state.abort_card_transport())
        self.assertTrue(connection.done.wait(2))
        # the flag is set by the worker thread's own `finally` *after* the
        # disconnect returns, so it must be awaited, not read immediately
        self.assertTrue(self._wait_stale(state))


# ---- the pure helpers -------------------------------------------------------

class HelperTests(unittest.TestCase):
    def test_the_kind_labels(self):
        self.assertEqual(testrun.TEST_KIND_LABELS['status'], 'STATUS')
        self.assertEqual(testrun.TEST_KIND_LABELS['proactive-drain'], 'DRAIN')
        self.assertIn('/api/tree', testrun.TEST_BLOCKED_PATHS)
        # the run's own endpoints are never blocked
        self.assertNotIn('/api/test/status', testrun.TEST_BLOCKED_PATHS)
        self.assertNotIn('/api/test/run', testrun.TEST_BLOCKED_PATHS)
        self.assertNotIn('/api/test/stop', testrun.TEST_BLOCKED_PATHS)

    def test_the_step_label(self):
        kind, label = testrun.step_label(
            {'type': 'action', 'kind': 'status', 'params': {}})
        self.assertEqual((kind, label), ('status', 'STATUS'))
        kind, label = testrun.step_label(
            {'type': 'expect', 'command': {'type': 0x24, 'name': None}})
        self.assertEqual((kind, label), ('0x24', 'EXPECT 0x24'))
        kind, label = testrun.step_label(
            {'type': 'expect', 'command': {'type': None, 'name': 'ANY'}})
        self.assertEqual(label, 'EXPECT ANY')

    def test_the_load_iterations(self):
        self.assertEqual(testrun.load_iterations({}), 1)
        self.assertEqual(testrun.load_iterations({'iterations': ''}), 1)
        self.assertEqual(testrun.load_iterations({'iterations': '5'}), 5)
        for bad in (0, -1, 1.5, 'abc', testrun.TEST_LOAD_MAX + 1, [2]):
            with self.assertRaises(ValueError):
                testrun.load_iterations({'iterations': bad})

    def test_the_load_state_flags_repeated_runs(self):
        one = testrun.load_state(1)
        self.assertFalse(one['enabled'])
        self.assertIsNone(one['iterations'])
        many = testrun.load_state(100)
        self.assertTrue(many['enabled'])
        self.assertEqual(many['iterations'], 100)

    def test_the_json_safe_state(self):
        state = make_state(self, Card())
        snapshot = state.test_runner.snapshot()
        self.assertIsInstance(json.loads(json.dumps(snapshot)), dict)
        self.assertFalse(snapshot['running'])

    def test_the_command_names_resolve(self):
        self.assertEqual(testrun.command_type('SELECT ITEM'), 0x24)
        self.assertIsNone(testrun.command_type('NOPE'))

    def test_the_file_list_parser(self):
        raw = proactive(command_details(1, 0x01),
                        ber(0x12, bytes((2,)) + bytes.fromhex('3F00')
                            + bytes.fromhex('7F20')))
        self.assertEqual(testrun.parse_file_list(raw), ['3F007F20'])
        self.assertEqual(testrun.parse_file_list(b''), [])

    def test_the_sms_fields(self):
        tpdu = bytes.fromhex('01000A912143658709000002AB')
        raw = proactive(
            command_details(1, 0x13),
            ber(0x06, bytes.fromhex('0A91') + bytes.fromhex('2143658709')),
            ber(0x8B, tpdu))
        fields = testrun.sms_tpdu_fields(raw)
        self.assertEqual(fields['mti'], 1)
        self.assertEqual(fields['da'], '1234567890')
        self.assertEqual((fields['pid'], fields['dcs']), ('00', '00'))
        self.assertEqual(fields['ud'], 'AB')

    def test_the_action_checks(self):
        step = {'check': {'sw': {'mode': 'exact', 'value': '9000'},
                          'data': {'mode': 'exact', 'value': 'AA'},
                          'por': 'any'},
                'on_fail': 'error'}
        checks = testrun.action_checks(step, '6A82', '', None, 'status')
        self.assertEqual(checks[0]['label'], 'SW')
        self.assertFalse(checks[0]['ok'])
        self.assertEqual(checks[1]['actual'], '(not checked - SW mismatch)')
        self.assertTrue(checks[1]['ok'])

    def test_the_counter_rule_is_three_way(self):
        # clear acceptance advances
        self.assertTrue(testrun.counter_consumed(
            {'response_status': 'por_ok'}, '9000'))
        self.assertTrue(testrun.counter_consumed(
            {'response_status': 'actual_response_sms_submit'}, '9000'))
        self.assertTrue(testrun.counter_consumed(None, '9000'))
        self.assertTrue(testrun.counter_consumed(None, '9110'))
        # a clear rejection does not
        for status in ('cntr_low', 'rc_cc_ds_failed',
                       'undefined_security_error', 'cntr_blocked'):
            self.assertFalse(testrun.counter_consumed(
                {'response_status': status}, '9000'), status)
        self.assertTrue(testrun.packet_rejected(
            {'response_status': 'cntr_low'}))
        self.assertFalse(testrun.packet_rejected(
            {'response_status': 'more_time_needed'}))
        self.assertFalse(testrun.packet_rejected(None))
        # an unclear outcome advances (no/undecodable PoR, an ambiguous SW)
        self.assertTrue(testrun.counter_consumed(None, '6A82'))
        self.assertTrue(testrun.counter_consumed(None, '6F00'))
        self.assertTrue(testrun.counter_consumed(None, '6200'))
        # no exchange at all (a pre-send failure) consumes nothing
        self.assertFalse(testrun.counter_consumed(None, ''))
        self.assertFalse(testrun.counter_consumed(None, None))

    def test_the_por_checks(self):
        por = {'response_status': 'por_ok',
               'decoded': {'last_status_word': '9000',
                           'last_response_data': '01'}}
        checks = testrun.por_check_results(
            {'status': 'ok', 'sw': {'mode': 'exact', 'value': '9000'},
             'data': {'mode': 'mask', 'value': '0?'}}, por, 'error')
        self.assertTrue(all(check['ok'] for check in checks))
        missing = testrun.por_check_results({'status': 'ok'}, None, 'error')
        self.assertFalse(missing[0]['ok'])
        self.assertIn('no PoR', missing[0]['actual'])


if __name__ == '__main__':
    unittest.main()
