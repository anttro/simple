# coding=utf-8
"""Tests for :mod:`simple_server.server` (the pysim-free HTTP core).

The suite needs no card and no pysim: every exchange is driven through a
scripted fake transport (``send_apdu`` answers registered by APDU prefix), and
the HTTP endpoints are exercised on a real :class:`ThreadingHTTPServer` bound to
an ephemeral loopback port with ``urllib.request``.

Spec anchors: TS 102 221 §11.2 (the CAT APDUs), §13.2 (EF.ICCID), §14.6.2 (the
idle poll); TS 102 223 §6.4.6/§6.4.14 (POLL INTERVAL / POLLING OFF); TS 31.102
§4.4.2.1 (EF.PBR) and ISO 7816-4 Table 25 / TS 102 221 §11.1.1.4 (the FCP
``8B`` EF.ARR reference).
"""

import json
import os
import socket
import tempfile
import threading
import time
import types
import unittest
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from simple_server import cards
from simple_server import cat_session
from simple_server import server as server_module
from simple_server import transport
from simple_server.server import (ServerState, build_http_server, decode_iccid)

ATR = bytes.fromhex('3b9f95801fc78031e073fe211b67a3a0001505017204')

# '8970119000004600098' as EF.ICCID (TS 102 221 §13.2): nibble-swapped
# digits, the odd-nibble F pad last.
ICCID_DIGITS = '8970119000004600098'
ICCID_HEX = '980711090000640090f8'

SELECT_MF = bytes.fromhex('00A40004023F0000')
SELECT_ICCID = bytes.fromhex('00A40004022FE200')
SELECT_ARR_MF = bytes.fromhex('00A40004022F0600')
SELECT_DF_TELECOM = bytes.fromhex('00A40004027F1000')
SELECT_DF_PHONEBOOK = bytes.fromhex('00A40004025F3A00')
SELECT_PBR = bytes.fromhex('00A40004024F3000')
STATUS_POLL = bytes.fromhex('80F2000C00')

# A linear-fixed FCP: file descriptor 4A (shareable, working EF, structure
# 010 = linear fixed), record length 10, 5 records; FID 2F06 (TS 102 221
# Table 11.5 / §11.1.1.4.3).
ARR_FCP = bytes.fromhex('620B82054A00000A0583022F06')
ARR_RECORDS = ['AA000000000000000000', 'BB000000000000000000',
               'CC000000000000000000', 'DD000000000000000000',
               'EE000000000000000000']

# EF.PBR under DF.TELECOM/DF.PHONEBOOK (TS 31.102 §4.4.2.1): one 10-byte
# record, the 'A8' container with a type-1 EF.ADN ('C0', FID 4F3A, SFI 02).
PBR_FCP = bytes.fromhex('620B82054A00000A0183024F30')
PBR_RECORDS = ['A808C0024F3A0200FFFE']


class FakeTransport(object):
    """A scripted transport: ``handler(apdu)`` returns ``(data, sw)`` or None.

    An unhandled APDU answers ``6A82`` ("file or application not found"),
    which makes the best-effort reads fail cleanly.  No pyscard is involved.
    """

    def __init__(self, handler=None, reader_name='Fake reader', atr=ATR):
        self.handler = handler
        self.atr = atr
        self.reader_name = reader_name
        self.protocol = 1
        self.sent = []
        self.resets = 0
        self.connected = False

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


class CardScript(object):
    """The APDU answers of a UICC whose MF/ICCID (and optionally ARR/PBR) exist.

    ``arr``/``pbr`` register the record EF under its own selection (the read
    answer follows the last selected file), so the EF.ARR and EF.PBR caches
    are exercised independently.
    """

    def __init__(self, iccid=ICCID_HEX, arr=False, pbr=False):
        self.iccid = iccid
        self.arr = arr
        self.pbr = pbr
        self.selected = None

    def __call__(self, apdu):
        if apdu == SELECT_MF:
            self.selected = 'MF'
            return b'', b'\x90\x00'
        if apdu in (SELECT_DF_TELECOM, SELECT_DF_PHONEBOOK):
            self.selected = 'DF'
            return b'', b'\x90\x00'
        if self.pbr and apdu == SELECT_PBR:
            self.selected = 'PBR'
            return PBR_FCP, b'\x90\x00'
        if apdu[:2] == b'\x80\x10':          # TERMINAL PROFILE
            return b'', b'\x90\x00'
        if apdu == STATUS_POLL:
            return b'', b'\x90\x00'
        if apdu == SELECT_ICCID:
            self.selected = 'ICCID'
            return b'', b'\x90\x00'
        if apdu[:2] == b'\x00\xb0':          # READ BINARY (EF.ICCID)
            return bytes.fromhex(self.iccid), b'\x90\x00'
        if self.arr:
            if apdu == SELECT_ARR_MF:
                self.selected = 'ARR'
                return ARR_FCP, b'\x90\x00'
            if apdu[:2] == b'\x00\xb2' and self.selected == 'ARR':
                index = apdu[2] - 1
                value = ARR_RECORDS[min(index, len(ARR_RECORDS) - 1)]
                return bytes.fromhex(value), b'\x90\x00'
        if self.pbr and apdu[:2] == b'\x00\xb2' and self.selected == 'PBR':
            index = apdu[2] - 1
            value = PBR_RECORDS[min(index, len(PBR_RECORDS) - 1)]
            return bytes.fromhex(value), b'\x90\x00'
        return None


class FakePresetStore(object):
    """The ServerState seam for the card preset store (no home-directory I/O)."""

    path = '<fake>'

    def list(self):
        return []


def make_state(**kwargs):
    """A cardless ServerState with the test seams set."""
    kwargs.setdefault('monitor', False)
    kwargs.setdefault('presets', FakePresetStore())
    kwargs.setdefault('readers_fn', lambda: ['Reader A', 'Reader B'])
    kwargs.setdefault('poll_interval', 30)
    return ServerState(**kwargs)


def open_state(handler=None, **kwargs):
    state = make_state(**kwargs)
    fake = FakeTransport(handler=handler or CardScript())
    state.open(transport=fake, start_threads=False)
    return state, fake


class HTTPHarness(object):
    """A real ThreadingHTTPServer on an ephemeral loopback port."""

    def __init__(self, state, web_dir=None):
        self.state = state
        self.server = build_http_server('127.0.0.1', 0, state,
                                        web_dir=web_dir)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={'poll_interval': 0.05},
            name='test-http', daemon=True)
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
            with urlopen(request, timeout=5) as response:
                return response.status, response.read(), dict(response.headers)
        except HTTPError as exc:
            try:
                return exc.code, exc.read(), dict(exc.headers)
            finally:
                exc.close()

    def json(self, method, path, body=None):
        status, raw, _headers = self.request(method, path, body)
        return status, json.loads(raw.decode('utf-8'))

    def raw_request(self, target):
        """Send a raw request line (urllib normalises ``..`` out of the path)."""
        with socket.create_connection(('127.0.0.1', self.port), timeout=5) \
                as sock:
            sock.sendall(('GET %s HTTP/1.0\r\nHost: test\r\n\r\n'
                          % target).encode('ascii'))
            chunks = []
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
        return b''.join(chunks)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class HTTPTestBase(unittest.TestCase):
    def setUp(self):
        self.harness = None

    def tearDown(self):
        if self.harness is not None:
            self.harness.close()
        for state in getattr(self, '_states', []):
            state.close()
        self._states = []

    def served(self, state, web_dir=None):
        self.harness = HTTPHarness(state, web_dir=web_dir)
        return self.harness

    def track(self, state):
        self._states = getattr(self, '_states', [])
        self._states.append(state)
        return state


class VersionStatusTests(HTTPTestBase):
    def test_version(self):
        harness = self.served(make_state())
        status, body = harness.json('GET', '/api/version')
        self.assertEqual(status, 200)
        self.assertEqual(body, {'version': server_module.VERSION})

    def test_cardless_status_shape(self):
        state = self.track(make_state(reader='Reader A'))
        status, body = self.served(state).json('GET', '/api/status')
        self.assertEqual(status, 200)
        self.assertEqual(body['version'], server_module.VERSION)
        self.assertTrue(body['app_ready'])
        self.assertFalse(body['connected'])
        self.assertFalse(body['card_present'])
        self.assertFalse(body['equipping'])
        self.assertEqual(body['card_session'], 0)
        self.assertEqual(body['card_ready_session'], 0)
        self.assertEqual(body['reader'], 'Reader A')
        self.assertEqual(body['readers'], ['Reader A', 'Reader B'])
        self.assertIsNone(body['atr'])
        self.assertIsNone(body['card'])
        self.assertIsNone(body['iccid'])
        self.assertIsNone(body['selection'])
        self.assertEqual(body['proactive_mode'], 'idle')
        self.assertEqual(body['poll'], {'enabled': True, 'suspended': False,
                                        'interval': 30})
        self.assertEqual(body['test_run'], {'running': False})
        self.assertIsNone(body['bip'])
        self.assertIsNone(body['scp80'])
        self.assertEqual(body['terminal_profile'],
                         server_module.DEFAULT_TERMINAL_PROFILE)

    def test_equipped_status_shape(self):
        state, fake = open_state()
        self.track(state)
        status, body = self.served(state).json('GET', '/api/status')
        self.assertEqual(status, 200)
        self.assertTrue(body['connected'])
        self.assertTrue(body['card_present'])
        self.assertEqual(body['card']['kind'], 'uicc')
        self.assertEqual(body['card']['name'], 'UICC')
        self.assertEqual(body['card']['cla'], '80')
        self.assertEqual(body['atr'], ATR.hex())
        self.assertEqual(body['iccid'], ICCID_DIGITS)
        self.assertIsNone(body['selection'])
        self.assertEqual(body['card_session'], 1)
        self.assertEqual(body['card_ready_session'], 1)
        self.assertEqual(body['proactive_mode'], 'polling')
        self.assertIsNotNone(body['bip'])
        self.assertFalse(body['bip']['bip']['enabled'])

    def test_unknown_api_path_is_a_json_404(self):
        status, body = self.served(make_state()).json('GET', '/api/nope')
        self.assertEqual(status, 404)
        self.assertEqual(body, {'error': 'Not found'})

    def test_options_preflight_allows_private_network_access(self):
        harness = self.served(make_state())
        status, _raw, headers = harness.request('OPTIONS', '/api/status')
        self.assertEqual(status, 204)
        self.assertEqual(headers.get('Access-Control-Allow-Private-Network'),
                         'true')
        self.assertEqual(headers.get('Access-Control-Allow-Origin'), '*')

    def test_status_is_card_free_while_the_lock_is_held(self):
        state = self.track(make_state())
        harness = self.served(state)
        with state.card_lock:
            status, body = harness.json('GET', '/api/status')
        self.assertEqual(status, 200)
        self.assertFalse(body['connected'])

    def test_readers_are_card_free_while_the_lock_is_held(self):
        state = self.track(make_state(reader='Reader A'))
        harness = self.served(state)
        with state.card_lock:
            status, body = harness.json('GET', '/api/readers')
        self.assertEqual(status, 200)
        self.assertEqual(body, {'readers': ['Reader A', 'Reader B'],
                                'active': 'Reader A'})

    def test_status_reads_the_bip_state_under_its_lock(self):
        # BipSession mutates owner/mode/target/listener under its own lock;
        # the status snapshot must take it, or the listener block can tear.
        class LockedBip(object):
            def __init__(self):
                self._state_lock = threading.RLock()
                self.locked_inside = None
                self.owner = None

            def status(self):
                self.locked_inside = self._state_lock._is_owned()
                return {'owner': None}

        state = self.track(make_state())
        bip = LockedBip()
        state.bip = bip
        body = state.status()
        self.assertTrue(bip.locked_inside)
        self.assertEqual(body['bip'], {'owner': None})

    def test_a_normal_endpoint_waits_for_the_card_lock(self):
        def factory(_reader):
            raise transport.ReaderError('no reader')
        state = self.track(make_state(transport_factory=factory))
        harness = self.served(state)
        result = {}

        def call():
            result['answer'] = harness.json('POST', '/api/equip', {})

        with state.card_lock:
            worker = threading.Thread(target=call, daemon=True)
            worker.start()
            time.sleep(0.3)
            self.assertTrue(worker.is_alive(),
                            'an /api/ POST must wait for the card lock')
        worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        status, body = result['answer']
        self.assertEqual(status, 503)
        self.assertIn('no reader', body['error'])


class HandlerErrorTests(HTTPTestBase):
    def test_a_non_object_json_body_is_a_400(self):
        state = self.track(make_state())
        harness = self.served(state)
        for body in ([], 'text', 7):
            status, answer = harness.json('POST', '/api/reader', body)
            self.assertEqual(status, 400, body)
            self.assertIn('JSON object', answer['error'])
        # An explicit JSON null is not the same as an absent body (which the
        # harness would not send at all).
        raw = Request('http://127.0.0.1:%d/api/reader' % harness.port,
                      data=b'null', method='POST',
                      headers={'Content-Type': 'application/json'})
        with self.assertRaises(HTTPError) as caught:
            urlopen(raw, timeout=5)
        self.assertEqual(caught.exception.code, 400)
        answer = json.loads(caught.exception.read().decode('utf-8'))
        caught.exception.close()
        self.assertIn('JSON object', answer['error'])

    def test_an_endpoint_value_error_is_its_own_400(self):
        state = self.track(make_state(reader='Reader A'))
        harness = self.served(state)
        status, answer = harness.json('POST', '/api/reader', {})
        self.assertEqual(status, 400)
        self.assertIn('reader name', answer['error'])
        self.assertNotIn('invalid JSON', answer['error'])

    def test_a_handler_error_is_a_500_and_the_server_stays_up(self):
        state = self.track(make_state())

        def boom():
            raise RuntimeError('boom')

        state.status = boom
        harness = self.served(state)
        status, body = harness.json('GET', '/api/status')
        self.assertEqual(status, 500)
        self.assertIn('boom', body['error'])
        # The same handler keeps serving the next request.
        status, body = harness.json('GET', '/api/version')
        self.assertEqual(status, 200)
        self.assertEqual(body['version'], server_module.VERSION)


class StaticServingTests(HTTPTestBase):
    def setUp(self):
        super(StaticServingTests, self).setUp()
        self.tmp = tempfile.TemporaryDirectory()
        with open(os.path.join(self.tmp.name, 'index.html'), 'w',
                  encoding='utf-8') as handle:
            handle.write('<html><body>SIMple</body></html>')
        with open(os.path.join(self.tmp.name, 'style.css'), 'w',
                  encoding='utf-8') as handle:
            handle.write('body { color: black; }')

    def tearDown(self):
        super(StaticServingTests, self).tearDown()
        self.tmp.cleanup()

    def test_index_is_served_for_root_and_name(self):
        state = make_state(web_dir=self.tmp.name)
        harness = self.served(state)
        for path in ('/', '/index.html'):
            status, raw, headers = harness.request('GET', path)
            self.assertEqual(status, 200)
            self.assertIn('SIMple', raw.decode('utf-8'))
            self.assertEqual(headers.get('Content-Type'),
                             'text/html; charset=utf-8')
            self.assertEqual(headers.get('Cache-Control'), 'no-cache')
            self.assertTrue(headers.get('Last-Modified'))

    def test_css_mime(self):
        harness = self.served(make_state(web_dir=self.tmp.name))
        status, _raw, headers = harness.request('GET', '/style.css')
        self.assertEqual(status, 200)
        self.assertEqual(headers.get('Content-Type'), 'text/css')

    def test_missing_file_is_404_and_not_rewritten_to_the_app(self):
        # The legacy server has no SPA fallback: an unknown path is a 404, not
        # the app shell (a silent fallback would hide a broken asset path).
        harness = self.served(make_state(web_dir=self.tmp.name))
        status, raw, _headers = harness.request('GET', '/missing.txt')
        self.assertEqual(status, 404)
        self.assertNotIn(b'SIMple', raw)

    def test_api_paths_never_reach_the_static_root(self):
        harness = self.served(make_state(web_dir=self.tmp.name))
        status, body = harness.json('GET', '/api/missing')
        self.assertEqual(status, 404)
        self.assertEqual(body, {'error': 'Not found'})

    def test_a_traversal_path_is_a_404(self):
        # urllib normalises ".." out of a URL; send the request line raw so the
        # handler really sees it.
        parent = os.path.dirname(self.tmp.name)
        handle, secret = tempfile.mkstemp(prefix='simple-secret-', dir=parent)
        try:
            os.write(handle, b'TOP SECRET')
            os.close(handle)
            harness = self.served(make_state(web_dir=self.tmp.name))
            raw = harness.raw_request('/../%s' % os.path.basename(secret))
            self.assertIn(b' 404 ', raw.split(b'\r\n', 1)[0])
            self.assertNotIn(b'TOP SECRET', raw)
        finally:
            try:
                os.unlink(secret)
            except OSError:
                pass

    def test_a_symlink_outside_the_root_is_not_served(self):
        outside = tempfile.TemporaryDirectory()
        try:
            secret = os.path.join(outside.name, 'secret.txt')
            with open(secret, 'w', encoding='utf-8') as handle:
                handle.write('TOP SECRET')
            link = os.path.join(self.tmp.name, 'leak.txt')
            try:
                os.symlink(secret, link)
            except (OSError, AttributeError, NotImplementedError):
                self.skipTest('symlinks are not available')
            harness = self.served(make_state(web_dir=self.tmp.name))
            status, raw, _headers = harness.request('GET', '/leak.txt')
            self.assertEqual(status, 404)
            self.assertNotIn(b'TOP SECRET', raw)
        finally:
            outside.cleanup()

    def test_static_files_bypass_the_card_lock(self):
        state = self.track(make_state(web_dir=self.tmp.name))
        harness = self.served(state)
        with state.card_lock:
            status, raw, _headers = harness.request('GET', '/index.html')
        self.assertEqual(status, 200)
        self.assertIn(b'SIMple', raw)

    def test_last_modified_revalidation(self):
        harness = self.served(make_state(web_dir=self.tmp.name))
        _status, _raw, headers = harness.request('GET', '/index.html')
        modified = headers['Last-Modified']
        request = Request('http://127.0.0.1:%d/index.html' % harness.port,
                          headers={'If-Modified-Since': modified})
        try:
            with urlopen(request, timeout=5) as response:
                self.assertEqual(response.status, 304)
        except HTTPError as exc:
            self.assertEqual(exc.code, 304)
            exc.close()


class ReaderTests(HTTPTestBase):
    def test_readers_lists_names_and_active(self):
        state = self.track(make_state(reader='Reader A'))
        status, body = self.served(state).json('GET', '/api/readers')
        self.assertEqual(status, 200)
        self.assertEqual(body, {'readers': ['Reader A', 'Reader B'],
                                'active': 'Reader A'})

    def test_reader_switch_rebuilds_the_transport_and_bumps_the_session(self):
        built = {}

        def factory(name):
            built[name] = FakeTransport(handler=CardScript(), reader_name=name)
            return built[name]

        state = self.track(make_state(transport_factory=factory,
                                      reader='Reader A'))
        state.open(start_threads=False)
        self.assertEqual(state.card_session, 1)
        self.assertIs(state.transport, built['Reader A'])
        harness = self.served(state)
        status, body = harness.json('POST', '/api/reader', {'name': 'Reader B'})
        self.assertEqual(status, 200)
        self.assertEqual(body['reader'], 'Reader B')
        self.assertIs(state.transport, built['Reader B'])
        self.assertTrue(built['Reader B'].resets >= 1)
        self.assertEqual(state.card_session, 2)
        self.assertTrue(body['connected'])

    def test_reader_switch_refuses_an_unknown_name(self):
        state = self.track(make_state())
        status, body = self.served(state).json(
            'POST', '/api/reader', {'name': 'No such reader'})
        self.assertEqual(status, 400)
        self.assertIn('Reader A', body['error'])
        self.assertIsNone(state.reader_name)

    def test_reader_switch_requires_a_name(self):
        state = self.track(make_state())
        status, _body = self.served(state).json('POST', '/api/reader', {})
        self.assertEqual(status, 400)

    def test_reader_switch_is_refused_during_a_test_run(self):
        state = self.track(make_state(reader='Reader A'))
        state.test_running = True
        status, body = self.served(state).json(
            'POST', '/api/reader', {'name': 'Reader B'})
        self.assertEqual(status, 409)
        self.assertIn('test run', body['error'])
        self.assertEqual(state.reader_name, 'Reader A')

    def test_reader_switch_is_refused_while_a_bip_session_owns_the_card(self):
        state = self.track(make_state(reader='Reader A'))
        state.bip = types.SimpleNamespace(owner='scp81')
        status, body = self.served(state).json(
            'POST', '/api/reader', {'name': 'Reader B'})
        self.assertEqual(status, 409)
        self.assertIn('BIP session', body['error'])
        self.assertEqual(state.reader_name, 'Reader A')

    def test_reader_switch_to_a_cardless_reader_is_a_503_with_the_status(self):
        # ReaderError is a RuntimeError subclass: it used to be answered 409
        # with only {'error'}, hiding the bumped session from the PWA.
        def factory(name):
            if name == 'Reader A':
                return FakeTransport(handler=CardScript(), reader_name=name)
            raise transport.ReaderError('No card in the reader')

        state = self.track(make_state(transport_factory=factory,
                                      reader='Reader A'))
        state.open(start_threads=False)
        self.assertEqual(state.card_session, 1)
        harness = self.served(state)
        status, body = harness.json('POST', '/api/reader', {'name': 'Reader B'})
        self.assertEqual(status, 503)
        self.assertIn('No card', body['error'])
        self.assertEqual(body['reader'], 'Reader B')
        self.assertEqual(body['card_session'], state.card_session)
        self.assertFalse(body['connected'])
        self.assertEqual(state.reader_name, 'Reader B')


class EquipResetTests(HTTPTestBase):
    def test_equip_initializes_the_card(self):
        fake = FakeTransport(handler=CardScript())
        state = self.track(make_state(transport_factory=lambda _name: fake))
        status, body = self.served(state).json('POST', '/api/equip', {})
        self.assertEqual(status, 200)
        self.assertTrue(body['connected'])
        self.assertEqual(body['iccid'], ICCID_DIGITS)
        self.assertEqual(state.card_session, 1)
        self.assertEqual(state.card_ready_session, 1)
        self.assertTrue(any(apdu[:2] == b'\x80\x10' for apdu in fake.sent),
                        'the TERMINAL PROFILE must be sent')

    def test_equip_without_a_reader_is_a_503(self):
        def factory(_reader):
            raise transport.ReaderError('No PC/SC reader found')
        state = self.track(make_state(transport_factory=factory))
        status, body = self.served(state).json('POST', '/api/equip', {})
        self.assertEqual(status, 503)
        self.assertIn('PC/SC reader', body['error'])
        self.assertFalse(body['connected'])
        self.assertIsNone(body['card'])

    def test_reset_power_cycles_and_reinitializes(self):
        fake = FakeTransport(handler=CardScript())
        state = self.track(make_state(transport_factory=lambda _name: fake))
        state.open(start_threads=False)
        self.assertEqual(fake.resets, 1)
        status, body = self.served(state).json('POST', '/api/reset', {})
        self.assertEqual(status, 200)
        self.assertTrue(body['connected'])
        # One power cycle per request: init_card already resets, so the endpoint
        # must not reset again before equip (it used to reset twice).
        self.assertEqual(fake.resets, 2)
        self.assertEqual(state.card_session, 2)

    def test_equipping_is_visible_while_the_equip_holds_the_card(self):
        entered = threading.Event()
        gate = threading.Event()

        def factory(_name):
            entered.set()
            gate.wait(5)
            return FakeTransport(handler=CardScript())

        state = self.track(make_state(transport_factory=factory))
        harness = self.served(state)
        worker = threading.Thread(target=state.equip, daemon=True)
        worker.start()
        try:
            self.assertTrue(entered.wait(5), 'the equip never started')
            # /api/status is card-free: it answers while equip holds the lock
            # and reports the in-flight initialization.
            status, body = harness.json('GET', '/api/status')
            self.assertEqual(status, 200)
            self.assertTrue(body['equipping'])
            self.assertFalse(body['connected'])
        finally:
            gate.set()
            worker.join(timeout=5)
        self.assertFalse(state.equipping)

    def test_an_invalid_json_body_is_a_400(self):
        state = self.track(make_state())
        harness = self.served(state)
        request = Request('http://127.0.0.1:%d/api/reader' % harness.port,
                          data=b'{nope', method='POST',
                          headers={'Content-Type': 'application/json'})
        with self.assertRaises(HTTPError) as caught:
            urlopen(request, timeout=5)
        self.assertEqual(caught.exception.code, 400)
        body = json.loads(caught.exception.read().decode('utf-8'))
        caught.exception.close()
        self.assertIn('invalid JSON', body['error'])


class LifecycleTests(unittest.TestCase):
    def test_open_sends_the_profile_and_marks_the_session_ready(self):
        state = make_state()
        fake = FakeTransport(handler=CardScript())
        try:
            state.open(transport=fake, start_threads=False)
            self.assertTrue(state.connected)
            self.assertTrue(state.card_present)
            self.assertEqual(state.card.kind, 'uicc')
            self.assertEqual(state.card.cla, '80')
            self.assertEqual(state.atr, ATR.hex())
            self.assertEqual(state.iccid, ICCID_DIGITS)
            self.assertEqual(state.card_session, 1)
            self.assertEqual(state.card_ready_session, 1)
            self.assertIsNotNone(state.cat)
            self.assertIsNotNone(state.bip)
            self.assertTrue(any(apdu[:2] == b'\x80\x10' for apdu in fake.sent))
            # The startup drain makes one STATUS exchange (the profile chain
            # ended with 9000, so the drain's STATUS sees the card idle).
            self.assertTrue(any(apdu == STATUS_POLL for apdu in fake.sent))
        finally:
            state.close()

    def test_the_iccid_is_read_before_the_terminal_profile(self):
        # The legacy CAT-free window: EF.ICCID (and ARR/PBR) before the
        # TERMINAL PROFILE starts a CAT session.
        state, fake = open_state()
        try:
            reads = [index for index, apdu in enumerate(fake.sent)
                     if apdu[:2] == b'\x00\xb0']
            profiles = [index for index, apdu in enumerate(fake.sent)
                        if apdu[:2] == b'\x80\x10']
            self.assertTrue(reads, 'EF.ICCID was never read')
            self.assertTrue(profiles, 'the TERMINAL PROFILE was never sent')
            self.assertLess(max(reads), min(profiles))
            self.assertEqual(state.iccid, ICCID_DIGITS)
        finally:
            state.close()

    def test_disconnect_clears_the_session_and_its_caches(self):
        state, _fake = open_state()
        try:
            self.assertIsNotNone(state.iccid)
            state.disconnect()
            self.assertFalse(state.connected)
            self.assertFalse(state.card_present)
            self.assertIsNone(state.card)
            self.assertIsNone(state.cat)
            self.assertIsNone(state.bip)
            self.assertIsNone(state.iccid)
            self.assertIsNone(state.arr)
            self.assertIsNone(state.pbr)
            self.assertIsNone(state.selection)
            self.assertEqual(state.card_session, 2)
            self.assertEqual(state.card_ready_session, 1)
            self.assertEqual(state.proactive_mode(), 'idle')
        finally:
            state.close()

    def test_equip_after_disconnect_reinitializes(self):
        state, fake = open_state()
        try:
            state.disconnect()
            self.assertEqual(state.card_session, 2)
            state.equip()
            self.assertTrue(state.connected)
            self.assertEqual(state.card_session, 3)
            self.assertEqual(state.card_ready_session, 3)
            self.assertGreaterEqual(fake.resets, 2)
        finally:
            state.close()

    def test_arr_cache_reads_the_record_copies(self):
        state, _fake = open_state(handler=CardScript(arr=True))
        try:
            self.assertEqual(len(state.arr), 1)
            truth = state.arr[0]
            self.assertEqual(truth['fid'], '2F06')
            self.assertEqual(truth['path'], 'MF/2F06')
            self.assertEqual(truth['record_len'], 10)
            self.assertEqual(truth['num_records'], 5)
            self.assertEqual(truth['records'], ARR_RECORDS)
        finally:
            state.close()

    def test_a_failed_equip_leaves_no_session(self):
        def handler(apdu):
            return b'', b'\x6a\x82'          # nothing selectable
        state = make_state(transport_factory=lambda _name: FakeTransport(
            handler=handler))
        try:
            with self.assertRaises(Exception):
                state.equip()
            self.assertFalse(state.connected)
            self.assertIsNone(state.card)
            self.assertEqual(state.card_ready_session, 0)
            self.assertEqual(state.card_session, 1)
        finally:
            state.close()

    def test_a_failed_equip_marks_the_link_stale_and_the_retry_rebuilds(self):
        old = FakeTransport(handler=lambda apdu: (b'', b'\x6a\x82'))
        new = FakeTransport(handler=CardScript())
        state = make_state(reader='Reader A')
        state.transport = old
        try:
            with self.assertRaises(Exception):
                state.equip()
            self.assertTrue(state._transport_stale,
                            'a failed attempt must mark the link stale')
            self.assertFalse(state.connected)
            self.assertEqual(state.card_session, 1)
            state.transport_factory = lambda _name: new
            state.equip()
            self.assertTrue(state.connected)
            self.assertIs(state.transport, new)
            self.assertFalse(old.connected, 'the stale link must be released')
            self.assertFalse(state._transport_stale)
            self.assertEqual(state.card_session, 2)
        finally:
            state.close()

    def test_open_with_no_card_starts_the_threads_and_stays_cardless(self):
        installed = []
        removed = []

        def factory(_name):
            raise transport.ReaderError('No card in the reader')

        state = make_state(reader='Reader A', monitor=True,
                           transport_factory=factory,
                           presence_install=installed.append,
                           presence_remove=removed.append)
        try:
            with self.assertRaises(transport.ReaderError):
                state.open()
            self.assertFalse(state.connected)
            self.assertFalse(state.card_present)
            self.assertIsNone(state.card)
            self.assertEqual(state.card_session, 1)
            self.assertEqual(state.card_ready_session, 0)
            # The cardless start still owns the threads that equip the first
            # card later.
            self.assertIsNotNone(state._presence_observer)
            self.assertEqual(installed, [state._presence_observer])
            self.assertIsNotNone(state._poll_thread)
            self.assertTrue(state._poll_thread.is_alive())
        finally:
            state.close()
        self.assertEqual(removed, installed)

    def test_poll_interval_follows_the_session_default(self):
        state, _fake = open_state(poll_interval=5)
        try:
            self.assertEqual(state.poll_interval, 5)
            self.assertTrue(state.poll_enabled)
        finally:
            state.close()


class CachePathTests(unittest.TestCase):
    """The legacy FID labels the PWA resolves against its own file paths."""

    def test_arr_labels_are_the_legacy_fid_paths(self):
        self.assertEqual(
            [label for label, _chain in server_module.ARR_READ_PATHS],
            ['MF/2F06', 'MF/7F10/6F06', 'MF/7F20/6F06', 'ADF.USIM/6F06'])
        self.assertEqual(dict(server_module.ARR_READ_PATHS)['MF/7F10/6F06'],
                         ('3F00', '7F10', '6F06'))

    def test_pbr_labels_are_the_legacy_fid_paths(self):
        self.assertEqual(
            [label for label, _chain in server_module.PBR_READ_PATHS],
            ['MF/7F10/5F3A/4F30', 'ADF.USIM/5F3A/4F30',
             'ADF.USIM-NON-IMSI/5F3A/4F30'])
        chains = dict(server_module.PBR_READ_PATHS)
        self.assertEqual(chains['MF/7F10/5F3A/4F30'],
                         ('3F00', '7F10', '5F3A', '4F30'))
        self.assertEqual(chains['ADF.USIM/5F3A/4F30'],
                         (cards.AID_USIM, '5F3A', '4F30'))
        self.assertEqual(chains['ADF.USIM-NON-IMSI/5F3A/4F30'],
                         (cards.AID_USIM_NON_IMSI, '5F3A', '4F30'))

    def test_the_pbr_cache_carries_the_fid_label(self):
        state, _fake = open_state(handler=CardScript(pbr=True))
        try:
            self.assertEqual(state.arr, None)
            self.assertEqual(state.pbr,
                             [{'fid': '4F30',
                               'path': 'MF/7F10/5F3A/4F30',
                               'record_len': 10,
                               'num_records': 1,
                               'records': PBR_RECORDS}])
        finally:
            state.close()


class FakeCat(object):
    """The CAT session seam of the poll tests (``error`` simulates a failure)."""

    def __init__(self, sw=b'\x90\x00', interval=30, chain_sw=None,
                 error=None):
        self.sw = sw
        self.poll_interval = interval
        self.poll_suspended = False
        self.chain_sw = chain_sw
        self.error = error
        self.status_calls = 0
        self.chain_calls = []
        self.stk_pending = None
        self.busy = False

    def send_status(self):
        self.status_calls += 1
        if self.error is not None:
            raise self.error
        return b'', self.sw

    def handle_chain(self, sw, max_rounds=None):
        self.chain_calls.append((bytes(sw), max_rounds))
        return self.chain_sw


class PollTests(unittest.TestCase):
    def test_poll_sends_a_status_and_no_chain_on_9000(self):
        state = make_state()
        cat = FakeCat(sw=b'\x90\x00')
        state.cat = cat
        state.connected = True
        try:
            state.poll_once()
            self.assertEqual(cat.status_calls, 1)
            self.assertEqual(cat.chain_calls, [])
        finally:
            state.close()

    def test_poll_runs_the_chain_on_91xx(self):
        state = make_state()
        cat = FakeCat(sw=b'\x91\x12')
        state.cat = cat
        state.connected = True
        try:
            state.poll_once()
            self.assertEqual(cat.status_calls, 1)
            self.assertEqual(cat.chain_calls,
                             [(b'\x91\x12', server_module.POLL_CHAIN_MAX)])
        finally:
            state.close()

    def test_poll_adopts_the_card_interval(self):
        state = make_state()
        cat = FakeCat(interval=5)
        state.cat = cat
        state.connected = True
        try:
            state.poll_once()
            self.assertEqual(state.poll_interval, 5)
        finally:
            state.close()

    def test_poll_skips_while_a_test_run_owns_the_card(self):
        state = make_state()
        cat = FakeCat(sw=b'\x91\x12')
        state.cat = cat
        state.connected = True
        state.test_running = True
        try:
            state.poll_once()
            self.assertEqual(cat.status_calls, 0)
            self.assertEqual(cat.chain_calls, [])
        finally:
            state.close()

    def test_poll_skips_when_the_card_suspended_it(self):
        state = make_state()
        cat = FakeCat(sw=b'\x91\x12')
        state.cat = cat
        state.connected = True
        state.poll_suspended = True
        try:
            state.poll_once()
            self.assertEqual(cat.status_calls, 0)
        finally:
            state.close()

    def test_poll_skips_without_a_session(self):
        state = make_state()
        state.cat = FakeCat()
        state.connected = False
        try:
            state.poll_once()
            self.assertEqual(state.cat.status_calls, 0)
        finally:
            state.close()

    def test_a_transport_error_keeps_the_card_present_and_rearms(self):
        state = make_state()
        cat = FakeCat(error=transport.ReaderError('PC/SC link is not connected'))
        state.cat = cat
        state.connected = True
        state.card_present = True
        calls = []
        state.auto_equip = lambda *args, **kwargs: calls.append('equip')
        try:
            with self.assertLogs('simple_server.server',
                                 level='WARNING') as logs:
                state.poll_once()
            self.assertTrue(any('transport error' in line
                                for line in logs.output))
            self.assertFalse(state.connected)
            self.assertIsNone(state.cat)
            self.assertTrue(state.card_present,
                            'a transport error must not hide the card')
            self.assertEqual(calls, ['equip'])
        finally:
            state.close()

    def test_a_protocol_error_keeps_the_session(self):
        state = make_state()
        cat = FakeCat(error=cat_session.CatSessionError('bad TLV'))
        state.cat = cat
        state.connected = True
        calls = []
        state.auto_equip = lambda *args, **kwargs: calls.append('equip')
        try:
            with self.assertLogs('simple_server.server',
                                 level='WARNING') as logs:
                state.poll_once()
            self.assertTrue(any('session kept' in line for line in logs.output))
            self.assertTrue(state.connected)
            self.assertIs(state.cat, cat)
            self.assertEqual(calls, [])
        finally:
            state.close()

    def test_the_dead_sessions_poll_state_is_not_written_back(self):
        # disconnect() clears poll_suspended; without the `self.cat is cat`
        # guard the finally block would restore the dead session's value.
        state = make_state()
        cat = FakeCat(error=transport.ReaderError('gone'))
        cat.poll_suspended = True
        state.cat = cat
        state.connected = True
        state.card_present = True
        state.auto_equip = lambda *args, **kwargs: None
        try:
            state.poll_once()
            self.assertFalse(state.poll_suspended)
        finally:
            state.close()

    def test_poll_thread_starts_and_stops(self):
        state = make_state(poll_interval=30)
        try:
            thread = state.start_poll()
            self.assertTrue(thread.is_alive())
            self.assertIs(state.start_poll(), thread)
            state.stop_poll()
            self.assertFalse(thread.is_alive())
        finally:
            state.close()


class ProactiveModeTests(unittest.TestCase):
    def test_a_test_run_wins(self):
        state = make_state()
        cat = FakeCat()
        cat.stk_pending = {'cmd': 1}
        cat.busy = True
        state.cat = cat
        state.connected = True
        state.test_running = True
        try:
            self.assertEqual(state.proactive_mode(), 'test')
        finally:
            state.close()

    def test_a_pending_command_is_paused(self):
        state = make_state()
        cat = FakeCat()
        cat.stk_pending = {'cmd': 1}
        state.cat = cat
        state.connected = True
        try:
            self.assertEqual(state.proactive_mode(), 'paused')
        finally:
            state.close()

    def test_a_running_chain(self):
        state = make_state()
        cat = FakeCat()
        cat.busy = True
        state.cat = cat
        state.connected = True
        try:
            self.assertEqual(state.proactive_mode(), 'chain')
        finally:
            state.close()

    def test_a_bip_session(self):
        state = make_state()
        state.connected = True
        state.bip = types.SimpleNamespace(owner='bip')
        try:
            self.assertEqual(state.proactive_mode(), 'bip')
        finally:
            state.close()

    def test_polling_and_idle(self):
        state = make_state()
        self.assertEqual(state.proactive_mode(), 'idle')
        state.connected = True
        try:
            self.assertEqual(state.proactive_mode(), 'polling')
            state.poll_suspended = True
            self.assertEqual(state.proactive_mode(), 'idle')
        finally:
            state.close()


class PresenceTests(unittest.TestCase):
    def test_presence_observer_is_installed_through_the_seam(self):
        installed = []
        state = make_state(reader='Reader A', monitor=True,
                           presence_install=installed.append)
        try:
            observer = state.start_monitor()
            self.assertIsNotNone(observer)
            self.assertEqual(installed, [observer])
            self.assertIs(state.start_monitor(), observer)
        finally:
            state.close()

    def test_stop_monitor_uses_the_removal_seam(self):
        installed = []
        removed = []
        state = make_state(reader='Reader A', monitor=True,
                           presence_install=installed.append,
                           presence_remove=removed.append)
        try:
            observer = state.start_monitor()
            state.stop_monitor()
            self.assertEqual(removed, [observer])
            self.assertIsNone(state._presence_observer)
            state.stop_monitor()        # idempotent
            self.assertEqual(removed, [observer])
        finally:
            state.close()
        self.assertEqual(removed, installed)

    def test_stop_monitor_never_constructs_the_real_card_monitor(self):
        # A seam-installed observer is not pyscard's: constructing CardMonitor
        # just to remove it would start (and leak) its polling thread.
        state = make_state(reader='Reader A', monitor=True,
                           presence_install=lambda observer: None)
        fake_module = types.ModuleType('smartcard.CardMonitoring')

        class Boom(object):
            def __init__(self):
                raise AssertionError('the real CardMonitor was constructed')

        fake_module.CardMonitor = Boom
        try:
            state.start_monitor()
            with mock.patch.dict('sys.modules',
                                 {'smartcard.CardMonitoring': fake_module}):
                state.stop_monitor()
            self.assertIsNone(state._presence_observer)
        finally:
            state.close()

    def test_close_stops_a_running_auto_equip_worker(self):
        state = make_state(reader='Reader A')
        state.card_present = True
        started = threading.Event()

        def equip():
            started.set()
            raise transport.ReaderError('not ready')

        state.equip = equip
        try:
            thread = state.auto_equip(attempts=5, retry_delay=30.0)
            self.assertIsNotNone(thread)
            self.assertTrue(started.wait(5), 'the worker never ran')
            state.close()
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive(), 'close must stop the worker')
            self.assertTrue(state._closed.is_set())
            self.assertIsNone(state.auto_equip())
        finally:
            state.connected = False

    def test_removal_clears_the_session(self):
        state, fake = open_state(reader='Reader A')
        try:
            self.assertTrue(state.connected)
            removed = types.SimpleNamespace(reader='Reader A')
            state.handle_presence([], [removed])
            self.assertFalse(state.connected)
            self.assertFalse(state.card_present)
            self.assertIsNone(state.card)
            self.assertIsNone(state.transport)
            self.assertEqual(state.card_session, 2)
            self.assertTrue(fake.connected is False)
        finally:
            state.close()

    def test_insertion_triggers_auto_equip(self):
        state = make_state(reader='Reader A')
        calls = []
        state.auto_equip = lambda *args, **kwargs: calls.append('equip')
        try:
            inserted = types.SimpleNamespace(reader='Reader A')
            self.assertTrue(state.handle_presence([inserted], []))
            self.assertTrue(state.card_present)
            self.assertEqual(calls, ['equip'])
            self.assertTrue(state._transport_stale)
        finally:
            state.close()

    def test_monitor_registration_of_an_equipped_card_keeps_the_link(self):
        # pyscard's addObserver reports the cards present at registration as
        # inserted; an already-equipped session must not be marked stale (the
        # next equip would needlessly rebuild a healthy link).
        state, _fake = open_state(reader='Reader A')
        state.monitor_enabled = True

        def install(observer):
            observer.update(None, ([types.SimpleNamespace(reader='Reader A')],
                                   []))

        state.presence_install = install
        try:
            self.assertIsNotNone(state.start_monitor())
            self.assertTrue(state.card_present)
            self.assertTrue(state.connected)
            self.assertFalse(state._transport_stale)
        finally:
            state.close()

    def test_other_readers_are_ignored(self):
        state = make_state(reader='Reader A')
        calls = []
        state.auto_equip = lambda *args, **kwargs: calls.append('equip')
        try:
            other = types.SimpleNamespace(reader='Reader B')
            self.assertFalse(state.handle_presence([other], [other]))
            self.assertFalse(state.card_present)
            self.assertEqual(calls, [])
        finally:
            state.close()

    def test_auto_equip_worker_retries_a_failed_attempt(self):
        state = make_state(reader='Reader A')
        state.card_present = True
        # The first attempt runs on a link that cannot initialize the card.
        bad = FakeTransport(handler=lambda apdu: (b'', b'\x6a\x82'))
        state.transport = bad
        calls = []

        def factory(_name):
            calls.append(1)
            return FakeTransport(handler=CardScript(), reader_name='Reader A')

        state.transport_factory = factory
        try:
            with self.assertLogs('simple_server.server', level='WARNING') as logs:
                thread = state.auto_equip(attempts=3, retry_delay=0.0)
                self.assertIsNotNone(thread)
                thread.join(timeout=5)
            self.assertTrue(any('auto-equip failed' in line
                                for line in logs.output))
            # The failed attempt marked the link stale: the retry built a
            # fresh link instead of reusing the unusable one.
            self.assertEqual(len(calls), 1)
            self.assertFalse(bad.connected)
            self.assertTrue(state.connected)
            self.assertFalse(state._transport_stale)
            self.assertFalse(state._equip_busy)
        finally:
            state.connected = False
            state.close()

    def test_auto_equip_is_not_started_twice(self):
        state = make_state(reader='Reader A')
        state.card_present = True
        gate = threading.Event()

        def equip():
            gate.wait(2)

        state.equip = equip
        try:
            first = state.auto_equip()
            self.assertIsNotNone(first)
            self.assertIsNone(state.auto_equip())
            gate.set()
            first.join(timeout=5)
        finally:
            state.close()


class DecodeIccidTests(unittest.TestCase):
    def test_digits_and_padding(self):
        self.assertEqual(decode_iccid(ICCID_HEX), ICCID_DIGITS)
        self.assertEqual(decode_iccid(bytes.fromhex(ICCID_HEX)), ICCID_DIGITS)
        self.assertEqual(decode_iccid('98 07 11 09 00 00 64 00 90 F8'),
                         ICCID_DIGITS)

    def test_implausible_values(self):
        self.assertIsNone(decode_iccid(''))
        self.assertIsNone(decode_iccid(None))
        self.assertIsNone(decode_iccid('zzzz'))
        self.assertIsNone(decode_iccid('abcd'))      # swaps to non-digits
        self.assertIsNone(decode_iccid('ffff'))      # only the pad
        self.assertIsNone(decode_iccid('98071109000064009F8'))  # odd length

    def test_even_length_digit_string_is_kept_whole(self):
        self.assertEqual(decode_iccid('21436587092143658709'),
                         '12345678901234567890')


if __name__ == '__main__':
    unittest.main()
