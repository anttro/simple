# coding=utf-8
"""Tests for the pysim-free server's SCP81/BIP control endpoints (3c-3e-2b).

The SCP81 tab's listener/scripts and the Simulator's BIP pill ride one
server-owned :class:`simple_server.bip.BipSession` plus
:class:`simple_server.scp81.Scp81Control` (the script engine, the PSK table and
the ``.cap`` -> APDU generator), on top of the reusable, pysim-free
:class:`pysim_simple_server.scp81.PskTlsServer` (the TLS-PSK listener and the
GP HTTP administration dialog) and ``httpota.TcpDumpServer`` (the capture
listener).  The endpoints run through a real
:class:`ThreadingHTTPServer` on an ephemeral loopback port; the TLS test drives
a real PSK handshake with a loopback client, i.e. the same wire path a card's
BIP channel uses (the terminal's socket redirection is Phase 3c-2's, tested in
``tests/test_simple_bip.py``).

The legacy reference is ``pysim_simple_server/server.py``'s
``/api/scp81/bip``, ``/api/bip/control``, ``/api/scp81/status|script|log|
log-clear|queue|psk-map|gen-install`` and ``/api/bip/status|log|log-clear``
handlers (``_scp81_bip_control``, ``_bip_control``, ``_scp81_script_*``,
``_parse_psk_map``, ``_scp81_gen_install``); the parity of the ported shapes
lives in ``tests/test_simple_drift.py::Scp81DriftTests``.

Spec anchors: GPC v2.2 Amendment B §4.3 (TLS-PSK: the Table 4-2 suites, TLS
1.0-1.2, no resumption), §4.4 (the HTTP administration dialog), §4.4.2 (204 =
the end of the session), TS 102 226 §5.2.1 (the Command Scripting template),
§5.2.2/Table 5.12 (the Response Scripting template and the executed count),
§8.2.1.7.2 (GET DATA FF21) and GP Card Specification v2.3.1 §11.4.2
(GET STATUS, ``63 10`` = more occurrences).
"""

import json
import pathlib
import shutil
import socket
import ssl
import tempfile
import threading
import time
import unittest
import warnings
import zipfile
import io
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from simple_server.server import (
    CARD_FREE_GET, CARD_FREE_POST, ServerState, build_http_server)
from simple_server import testrun

PSK = bytes.fromhex('00112233445566778899aabbccddeeff')
PSK2 = bytes.fromhex('ffeeddccbbaa99887766554433221100')
IDENT = '89012345678901234567'
IDENT2 = '89012345678901234568'

#: The reference administration sequence (the PWA's "Explore ISD" template):
#: a generic multi-APDU script for the dialog tests.
EXPLORE = ['80CAFF2100', '80F28002024F0000', '80CA008500']


def cap_bytes():
    """A minimal ``.cap`` archive (Header + Import + Applet), the drift
    fixture's shape - enough for the parse and the APDU sequence."""
    aid = 'A0000003080000100001'
    module = 'A000000308000010000101'

    def component(tag, payload):
        return bytes([tag]) + len(payload).to_bytes(2, 'big') + payload
    header = component(
        0x01, (0xDECAFFED).to_bytes(4, 'big') + bytes([1, 2, 0x04, 1, 2])
        + bytes([len(bytes.fromhex(aid))]) + bytes.fromhex(aid))
    imports = component(0x04, bytes([1]) + bytes([2, 1, 7])
                        + bytes.fromhex('A0000000620101'))
    applet = component(0x03, bytes([1, len(bytes.fromhex(module))])
                       + bytes.fromhex(module) + (1).to_bytes(2, 'big'))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        for name, data in (('Header', header), ('Import', imports),
                           ('Applet', applet)):
            archive.writestr('pkg/%s.cap' % name, data)
    return buffer.getvalue()


#: A throw-away root for the test stores (never the user's real
#: ``~/.pysim-simple-server`` files); ``tearDownModule`` removes it.
_STORE_ROOT = pathlib.Path(tempfile.mkdtemp(prefix='simple-scp81-api-'))
_STORE_COUNTER = [0]


def tearDownModule():
    shutil.rmtree(_STORE_ROOT, ignore_errors=True)


def _store_path(name):
    _STORE_COUNTER[0] += 1
    return _STORE_ROOT / ('%s-%d.json' % (name, _STORE_COUNTER[0]))


class FakePresetStore(object):
    """The ServerState seam for the card preset store (no home-directory I/O)."""

    path = '<fake>'

    def list(self):
        return []

    def get(self, preset_id):
        return None


def make_state(**kwargs):
    kwargs.setdefault('monitor', False)
    kwargs.setdefault('presets', FakePresetStore())
    kwargs.setdefault('readers_fn', lambda: ['Reader A'])
    kwargs.setdefault('test_scripts_path', _store_path('test_scripts'))
    kwargs.setdefault('test_suites_path', _store_path('test_suites'))
    kwargs.setdefault('mcc_mnc_path', str(_STORE_ROOT / 'mcc-mnc.json'))
    return ServerState(**kwargs)


class HTTPHarness(object):
    """A real ThreadingHTTPServer on an ephemeral loopback port."""

    def __init__(self, state):
        self.state = state
        self.server = build_http_server('127.0.0.1', 0, state)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={'poll_interval': 0.05},
            name='test-scp81-api-http', daemon=True)
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

    def get(self, path):
        return self.json('GET', path)

    def post(self, path, body=None):
        return self.json('POST', path, body if body is not None else {})


class HarnessCase(unittest.TestCase):
    """One cardless state + HTTP harness per test.

    **No card is equipped on purpose**: the SCP81 listener, the BIP session
    and the script state are the *terminal* side - the PWA's SCP81/BIP views
    must work before a card is inserted (the typical "start the listener, then
    trigger the card" workflow).
    """

    def setUp(self):
        self.state = None
        self.http = None

    def tearDown(self):
        if self.http is not None:
            self.http.close()

    def serve(self):
        self.state = make_state()
        self.http = HTTPHarness(self.state)
        return self.state, self.http

    def start_tls(self, **body):
        request = {'action': 'start', 'mode': 'tls', 'host': '127.0.0.1',
                   'port': 0,
                   'psk_map': [{'identity': IDENT, 'psk_hex': PSK.hex()},
                               {'identity': IDENT2, 'psk_hex': PSK2.hex()}]}
        request.update(body)
        status, resp = self.http.post('/api/scp81/bip', request)
        self.assertEqual(status, 200, resp)
        self.assertTrue(resp['ok'], resp)
        return resp


# ---- the SCP81 listener ----------------------------------------------------


@unittest.skipUnless(hasattr(ssl.SSLContext, 'set_psk_server_callback'),
                     'TLS-PSK requires Python 3.13+')
class ListenerTests(HarnessCase):
    """``POST /api/scp81/bip``: the modes, the PSK table, the replacement."""

    def test_the_default_dump_listener(self):
        state, http = self.serve()
        status, resp = http.post('/api/scp81/bip',
                                 {'action': 'start', 'mode': 'dump',
                                  'host': '127.0.0.1', 'port': 0})
        self.assertEqual(status, 200)
        self.assertTrue(resp['ok'])
        self.assertEqual(resp['owner'], 'scp81')
        self.assertIsNone(resp['replaced'])
        self.assertEqual(resp['listener']['mode'], 'dump')
        self.assertGreater(resp['listener']['port'], 0)
        self.assertEqual(resp['listener']['connections'], 0)
        self.assertGreater(resp['bip']['seq'], 0)
        # the start response reports the control's view, but not the status's
        # `script` state (the TLS start echoes the legacy APDU list instead)
        self.assertEqual(resp['mode'], 'dump')
        self.assertEqual(resp['target'],
                         '127.0.0.1:%d' % resp['listener']['port'])
        self.assertNotIn('script', resp)
        # the status endpoints report the same session
        for path in ('/api/scp81/status', '/api/bip/status'):
            status, body = http.get(path)
            self.assertEqual(status, 200)
            self.assertEqual(body['owner'], 'scp81')
            self.assertEqual(body['listener']['mode'], 'dump')
            self.assertEqual(body['listener']['port'],
                             resp['listener']['port'])
            # the additive one-poll fields
            self.assertEqual(body['mode'], 'dump')
            self.assertEqual(body['target'],
                             '127.0.0.1:%d' % resp['listener']['port'])
            self.assertEqual(body['script'], http.get('/api/scp81/script')[1])

    def test_the_tls_listener_and_the_script(self):
        state, http = self.serve()
        resp = self.start_tls(script=list(EXPLORE), script_kind='Explore ISD')
        listener = resp['listener']
        self.assertEqual(listener['mode'], 'tls')
        self.assertGreater(listener['port'], 0)
        self.assertEqual(listener['psk_identities'], sorted([IDENT, IDENT2]))
        self.assertFalse(listener['psk_wildcard'])
        self.assertIsNone(listener['identity_seen'])
        self.assertEqual(listener['tls_version'], 'auto')
        self.assertTrue(listener['chunked'])
        self.assertEqual(listener['chunk_size'], 0)
        self.assertFalse(listener['compact_headers'])
        # the TLS start echoes the script framing (the legacy response)
        self.assertEqual(resp['script'], EXPLORE)
        self.assertEqual(resp['script_kind'], 'Explore ISD')
        self.assertEqual(resp['script_template'], 'indefinite')
        self.assertFalse(resp['cr_tag'])
        self.assertIsNone(resp['targeted_app'])
        self.assertTrue(resp['chunked'])
        self.assertTrue(resp['link_events'])
        # the status carries the control's mode/target/script view
        status = http.get('/api/scp81/status')[1]
        self.assertEqual(status['mode'], 'tls')
        self.assertEqual(status['target'],
                         '127.0.0.1:%d' % listener['port'])
        self.assertEqual(status['script']['script'], EXPLORE)
        self.assertEqual(status['script']['kind'], 'Explore ISD')

    def test_the_listener_options_are_reported(self):
        state, http = self.serve()
        resp = self.start_tls(chunked=False, chunk_size=64,
                              compact_headers=True, tls_version='1.2',
                              conn_header='keep-alive', script_template='definite',
                              cr_tag=True, targeted_app='//aid/A000000003/01',
                              next_uri='')
        listener = resp['listener']
        self.assertFalse(listener['chunked'])
        self.assertEqual(listener['chunk_size'], 64)
        self.assertTrue(listener['compact_headers'])
        self.assertEqual(listener['tls_version'], '1.2')
        self.assertTrue(resp['cr_tag'])
        self.assertEqual(resp['targeted_app'], '//aid/A000000003/01')
        self.assertEqual(resp['script_template'], 'definite')
        self.assertFalse(resp['chunked'])

    def test_the_legacy_single_key_form(self):
        state, http = self.serve()
        status, resp = http.post('/api/scp81/bip', {
            'action': 'start', 'mode': 'tls', 'host': '127.0.0.1', 'port': 0,
            'psk_hex': PSK.hex(), 'psk_identity': IDENT})
        self.assertEqual(status, 200, resp)
        self.assertEqual(resp['listener']['psk_identities'], [IDENT])
        self.assertFalse(resp['listener']['psk_wildcard'])
        # without an identity pin any identity is accepted (the wildcard form)
        status, resp = http.post('/api/scp81/bip', {
            'action': 'start', 'mode': 'tls', 'host': '127.0.0.1', 'port': 0,
            'psk_hex': PSK.hex()})
        self.assertEqual(status, 200, resp)
        self.assertTrue(resp['listener']['psk_wildcard'])
        self.assertEqual(resp['listener']['psk_identities'], [])

    def test_the_last_psk_table_is_reused(self):
        # The PWA pushes preset edits separately; a plain Stop/Start must not
        # require re-pasting the table (the legacy `_SCP81_PSKS`).
        state, http = self.serve()
        first = self.start_tls()
        http.post('/api/scp81/bip', {'action': 'stop'})
        status, resp = http.post('/api/scp81/bip', {
            'action': 'start', 'mode': 'tls', 'host': '127.0.0.1',
            'port': 0})
        self.assertEqual(status, 200, resp)
        self.assertEqual(resp['listener']['psk_identities'],
                         first['listener']['psk_identities'])

    def test_no_psk_configured_is_a_400(self):
        state, http = self.serve()
        status, resp = http.post('/api/scp81/bip',
                                 {'action': 'start', 'mode': 'tls',
                                  'port': 0})
        self.assertEqual(status, 400)
        self.assertFalse(resp['ok'])
        self.assertIn('no PSK configured', resp['error'])
        self.assertIsNone(http.get('/api/scp81/status')[1]['listener'])

    def test_redirect_pins_the_target(self):
        state, http = self.serve()
        status, resp = http.post('/api/scp81/bip', {
            'action': 'start', 'mode': 'redirect', 'host': '127.0.0.1',
            'port': 9})
        self.assertEqual(status, 200, resp)
        self.assertEqual(resp['owner'], 'scp81')
        self.assertEqual(resp['listener'],
                         {'mode': 'redirect', 'host': '127.0.0.1', 'port': 9,
                          'target': '127.0.0.1:9'})
        self.assertEqual(resp['bip']['target'], '127.0.0.1:9')
        self.assertTrue(resp['bip']['enabled'])
        # the delegation answers the control's documented shape too
        self.assertEqual(resp['mode'], 'redirect')
        self.assertEqual(resp['target'], '127.0.0.1:9')

    def test_passthru_has_neither_listener_nor_target(self):
        state, http = self.serve()
        status, resp = http.post('/api/scp81/bip',
                                 {'action': 'start', 'mode': 'passthru'})
        self.assertEqual(status, 200, resp)
        self.assertEqual(resp['listener'], {'mode': 'passthru'})
        self.assertIsNone(resp['bip']['target'])
        self.assertEqual(resp['mode'], 'passthru')
        self.assertIsNone(resp['target'])

    def test_a_start_replaces_the_previous_session(self):
        state, http = self.serve()
        http.post('/api/scp81/bip',
                  {'action': 'start', 'mode': 'dump', 'port': 0})
        resp = self.start_tls()
        self.assertEqual(resp['replaced'], {'owner': 'scp81', 'mode': 'dump'})
        self.assertEqual(resp['listener']['mode'], 'tls')

    def test_a_stop_reports_what_it_stopped(self):
        state, http = self.serve()
        self.start_tls()
        status, resp = http.post('/api/scp81/bip', {'action': 'stop'})
        self.assertEqual(status, 200, resp)
        self.assertTrue(resp['ok'])
        self.assertEqual(resp['replaced'], {'owner': 'scp81', 'mode': 'tls'})
        self.assertIsNone(resp['owner'])
        self.assertIsNone(resp['listener'])
        self.assertFalse(resp['bip']['enabled'])
        # stopping nothing is a success with nothing replaced
        status, resp = http.post('/api/scp81/bip', {'action': 'stop'})
        self.assertEqual(status, 200)
        self.assertIsNone(resp['replaced'])

    def test_the_validation_refusals_are_400s(self):
        state, http = self.serve()
        cases = [
            ({'mode': 'nope', 'port': 0}, 'unsupported mode'),
            ({'mode': 'redirect'}, 'requires the target host and port'),
            ({'mode': 'redirect', 'host': '127.0.0.1'}, 'requires the target'),
            ({'mode': 'dump', 'port': 'x'}, 'port is not a number'),
            ({'mode': 'dump', 'port': 70000}, 'port must be'),
            ({'mode': 'tls', 'psk_hex': PSK.hex(), 'port': -1},
             'port must be'),
            ({'mode': 'tls', 'psk_map': 'nope'}, 'psk_map must be'),
            ({'mode': 'tls', 'psk_map': ['nope']}, 'psk_map entries'),
            ({'mode': 'tls', 'psk_map': [{'identity': 'a',
                                         'psk_hex': 'zz'}]}, 'not valid hex'),
            ({'mode': 'tls', 'psk_map': []}, 'no usable entries'),
            ({'mode': 'tls', 'psk_hex': 'zz'}, 'not valid hex'),
            ({'mode': 'tls', 'psk_hex': PSK.hex(), 'tls_version': '1.3'},
             'tls_version must be'),
            ({'mode': 'tls', 'psk_hex': PSK.hex(), 'script_template': 'x'},
             'script_template must be'),
            ({'mode': 'tls', 'psk_hex': PSK.hex(), 'conn_header': 'x'},
             'conn_header must be'),
            ({'mode': 'tls', 'psk_hex': PSK.hex(), 'chunk_size': 'x'},
             'chunk_size is not a number'),
            ({'mode': 'tls', 'psk_hex': PSK.hex(), 'answer_delay': -1},
             'answer_delay must be'),
            ({'mode': 'tls', 'psk_hex': PSK.hex(), 'link_events': 'maybe'},
             'link_events must be a boolean'),
            ({'mode': 'tls', 'psk_hex': PSK.hex(), 'script': 'explore'},
             'unknown script preset'),
            ({'mode': 'tls', 'psk_hex': PSK.hex(), 'script': 42},
             'script must be'),
            ({'mode': 'tls', 'psk_hex': PSK.hex(), 'script': ['zz']},
             'not a hex APDU'),
            # the administration response headers must not be splittable
            ({'mode': 'tls', 'psk_hex': PSK.hex(), 'port': 0,
              'next_uri': '/x\r\nX-Evil: 1'}, 'control character'),
            ({'mode': 'tls', 'psk_hex': PSK.hex(), 'port': 0,
              'targeted_app': '//aid/A\nX-Evil: 1'}, 'control character'),
            ({'action': 'restart', 'port': 0}, 'unsupported action'),
        ]
        for body, message in cases:
            with self.subTest(body=body):
                request = {'action': 'start', 'host': '127.0.0.1'}
                request.update(body)
                status, resp = http.post('/api/scp81/bip', request)
                self.assertEqual(status, 400, resp)
                self.assertFalse(resp['ok'])
                self.assertIn(message, resp['error'])

    def test_a_refused_start_keeps_the_running_session(self):
        state, http = self.serve()
        first = self.start_tls()
        status, resp = http.post('/api/scp81/bip',
                                 {'action': 'start', 'mode': 'nope'})
        self.assertEqual(status, 400)
        body = http.get('/api/scp81/status')[1]
        self.assertEqual(body['owner'], 'scp81')
        self.assertEqual(body['listener']['port'],
                         first['listener']['port'])

    def test_a_restart_on_the_same_dump_port_replaces_the_listener(self):
        # The PWA's Start is the normal way to apply the Options, so a second
        # start on the port the listener already holds must rebind it (the
        # control stops the previous session before the bind).
        state, http = self.serve()
        first = http.post('/api/scp81/bip', {
            'action': 'start', 'mode': 'dump', 'host': '127.0.0.1',
            'port': 0})[1]
        port = first['listener']['port']
        status, second = http.post('/api/scp81/bip', {
            'action': 'start', 'mode': 'dump', 'host': '127.0.0.1',
            'port': port})
        self.assertEqual(status, 200, second)
        self.assertTrue(second['ok'])
        self.assertEqual(second['replaced'], {'owner': 'scp81', 'mode': 'dump'})
        self.assertEqual(second['listener']['port'], port)
        self.assertEqual(http.get('/api/scp81/status')[1]['listener']['port'],
                         port)
        # the restored port really accepts connections
        socket.create_connection(('127.0.0.1', port), timeout=2).close()

    def test_a_restart_on_the_same_tls_port_replaces_the_listener(self):
        state, http = self.serve()
        first = self.start_tls()
        port = first['listener']['port']
        second = self.start_tls(port=port)
        self.assertEqual(second['replaced'], {'owner': 'scp81', 'mode': 'tls'})
        self.assertEqual(second['listener']['port'], port)
        self.assertTrue(http.get('/api/scp81/status')[1]['listener']
                        ['psk_identities'])

    def test_a_failed_start_keeps_the_script_and_its_progress(self):
        # The script reset/rewrite happens only after the listener bound: a
        # refused Start must not silently swap the running script's progress.
        state, http = self.serve()
        self.start_tls(script=list(EXPLORE), script_kind='Explore ISD')
        state.scp81.script.responder('POST', '/api/scp81', {}, b'')
        before = http.get('/api/scp81/script')[1]
        self.assertEqual(before['next'], 1)
        blocker = socket.socket()
        blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        blocker.bind(('127.0.0.1', 0))
        blocker.listen(1)
        port = blocker.getsockname()[1]
        try:
            status, resp = http.post('/api/scp81/bip', {
                'action': 'start', 'mode': 'tls', 'host': '127.0.0.1',
                'port': port, 'psk_map': [{'identity': IDENT,
                                           'psk_hex': PSK.hex()}],
                'script': ['80CAFF2100'], 'script_template': 'definite',
                'cr_tag': True})
            self.assertEqual(status, 400, resp)
            self.assertFalse(resp['ok'])
        finally:
            blocker.close()
        after = http.get('/api/scp81/script')[1]
        self.assertEqual(after['script'], EXPLORE)
        self.assertEqual(after['kind'], 'Explore ISD')
        self.assertEqual(after['next'], 1)
        self.assertEqual(after['template'], 'indefinite')
        self.assertFalse(after['cr_tag'])

    def test_a_bind_failure_is_a_400(self):
        # A port already taken cannot be bound: the request is refused with
        # the control's own `{'ok': False, 'error': ...}` (the legacy's
        # answer).  The control stops the previous session before the bind
        # (see `test_a_restart_on_the_same_dump_port_replaces_the_listener`),
        # so the documented trade-off is that a foreign-port bind failure
        # leaves no session at all - but never a half-created one.
        #
        # The legacy `TcpDumpServer` (untouched: the legacy package is the
        # fallback until the cutover) lets the `OSError` escape its
        # constructor; the partially built object's socket is finalized by
        # the refcount as the traceback is cleared, which CPython reports as
        # a ResourceWarning.  The socket is closed either way - only the
        # report is noise, so it is filtered for this call.
        state, http = self.serve()
        blocker = socket.socket()
        blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        blocker.bind(('127.0.0.1', 0))
        blocker.listen(1)
        port = blocker.getsockname()[1]
        try:
            with warnings.catch_warnings():
                warnings.simplefilter('ignore', ResourceWarning)
                status, resp = http.post('/api/scp81/bip', {
                    'action': 'start', 'mode': 'dump', 'host': '127.0.0.1',
                    'port': port})
            self.assertEqual(status, 400, resp)
            self.assertFalse(resp['ok'])
            self.assertIn('error', resp)
            # the refused start did not half-create a session
            self.assertIsNone(http.get('/api/scp81/status')[1]['listener'])
        finally:
            blocker.close()

    def test_a_listener_construction_error_is_a_400(self):
        # PskTlsServer.__init__ raises RuntimeError below Python 3.13
        # (`_make_context`) and ssl.SSLError from set_ciphers: both must be
        # the control's `{ok: false}` (a 400), never the 500 catch-all.
        state, http = self.serve()
        for error in (RuntimeError('TLS-PSK requires Python 3.13+'),
                      ssl.SSLError('no cipher match')):
            with self.subTest(error=type(error).__name__):
                with mock.patch(
                        'simple_server.scp81.listener_module.PskTlsServer',
                        side_effect=error):
                    status, resp = http.post('/api/scp81/bip', {
                        'action': 'start', 'mode': 'tls',
                        'host': '127.0.0.1', 'port': 0,
                        'psk_hex': PSK.hex()})
                self.assertEqual(status, 400, resp)
                self.assertFalse(resp['ok'])
                self.assertIn(str(error), resp['error'])

    def test_the_chunked_flag_reads_a_literal_false(self):
        # `bool("false")` is truthy: the flag is validated, so an explicit
        # "false" really turns the framing off (the v3.22.0 rule).
        state, http = self.serve()
        resp = self.start_tls(chunked='false')
        self.assertFalse(resp['listener']['chunked'])

    def test_a_script_none_clears_the_script(self):
        state, http = self.serve()
        self.start_tls(script=list(EXPLORE), script_kind='Explore ISD')
        resp = self.start_tls(script='none')
        self.assertEqual(resp['script'], [])
        self.assertEqual(resp['script_kind'], 'none')
        self.assertTrue(http.get('/api/scp81/script')[1]['complete'])

    def test_the_script_progress_survives_a_restart_without_a_script(self):
        state, http = self.serve()
        self.start_tls(script=list(EXPLORE), script_kind='Explore ISD')
        state.scp81.script.responder('POST', '/api/scp81', {}, b'')
        resp = self.start_tls()
        self.assertEqual(resp['script'], EXPLORE)
        self.assertEqual(http.get('/api/scp81/script')[1]['next'], 1)


# ---- the status, the log and the script -----------------------------------


class StatusLogTests(HarnessCase):
    """The card-free GETs, the cursor and the log clear."""

    def test_the_initial_shapes(self):
        state, http = self.serve()
        status, body = http.get('/api/scp81/status')
        self.assertEqual(status, 200)
        self.assertEqual(body['owner'], None)
        self.assertIsNone(body['listener'])
        self.assertFalse(body['bip']['enabled'])
        self.assertEqual(body['bip']['channels'], [])
        self.assertIsNone(body['mode'])
        self.assertIsNone(body['target'])
        self.assertEqual(http.get('/api/bip/status')[1], body)
        status, script = http.get('/api/scp81/script')
        self.assertEqual(status, 200)
        self.assertEqual(script['script'], [])
        self.assertEqual(script['kind'], 'none')
        self.assertEqual(script['next'], 0)
        self.assertEqual(script['total'], 0)
        self.assertEqual(script['done'], [])
        self.assertIsNone(script['pending'])
        self.assertEqual(script['results'], [])
        self.assertTrue(script['complete'])
        self.assertEqual(script['template'], 'indefinite')
        self.assertFalse(script['cr_tag'])

    def test_the_log_and_its_cursor(self):
        state, http = self.serve()
        status, body = http.get('/api/scp81/log')
        self.assertEqual(status, 200)
        self.assertEqual(body, {'seq': 0, 'entries': []})
        self.start_tls()
        status, body = http.get('/api/scp81/log')
        self.assertGreater(body['seq'], 0)
        kinds = [entry['kind'] for entry in body['entries']]
        self.assertIn('tls-listener-start', kinds)
        self.assertIn('enabled', kinds)
        # every entry carries its sequence number and the terminal keeps the
        # ordering
        seqs = [entry['seq'] for entry in body['entries']]
        self.assertEqual(seqs, sorted(seqs))
        cursor = body['entries'][1]['seq']
        status, newer = http.get('/api/scp81/log?after=%d' % cursor)
        self.assertEqual(status, 200)
        self.assertEqual(newer['seq'], body['seq'])
        self.assertTrue(all(entry['seq'] > cursor
                            for entry in newer['entries']))
        http.post('/api/scp81/bip', {'action': 'stop'})
        status, newer = http.get('/api/scp81/log?after=%d' % cursor)
        self.assertTrue(any(entry['kind'] == 'tls-listener-stop'
                            for entry in newer['entries']))
        # the same stream under the BIP path, and a bad cursor replays it
        self.assertEqual(http.get('/api/bip/log?after=%d' % cursor)[1]['seq'],
                         newer['seq'])
        self.assertEqual(http.get('/api/scp81/log?after=xyz')[1]['seq'],
                         newer['seq'])
        self.assertEqual(http.get('/api/scp81/log?after=-5')[1]['seq'],
                         newer['seq'])

    def test_the_log_clear(self):
        state, http = self.serve()
        self.start_tls()
        before = http.get('/api/scp81/log')[1]
        self.assertTrue(before['entries'])
        status, resp = http.post('/api/scp81/log-clear', {})
        self.assertEqual(status, 200)
        self.assertTrue(resp['ok'])
        # the sequence number keeps counting (a cursor stays comparable)
        self.assertEqual(resp['seq'], before['seq'])
        after = http.get('/api/scp81/log')[1]
        self.assertEqual(after['entries'], [])
        self.assertEqual(after['seq'], before['seq'])
        # the BIP path clears the same stream
        http.post('/api/scp81/bip', {'action': 'stop'})
        self.assertTrue(http.get('/api/bip/log')[1]['entries'])
        self.assertTrue(http.post('/api/bip/log-clear', {})[1]['ok'])
        self.assertEqual(http.get('/api/bip/log')[1]['entries'], [])

    def test_the_log_is_capped_to_the_last_200_entries(self):
        state, http = self.serve()
        for index in range(260):
            state.bip_session.terminal.log('test-entry', n=index)
        body = http.get('/api/scp81/log')[1]
        self.assertEqual(len(body['entries']), 200)
        self.assertEqual(body['entries'][-1]['n'], 259)

    def test_the_cached_gets_answer_under_the_card_lock(self):
        # card-free GETs must not queue behind a long card operation (the 2 s
        # UI polls): the same rule as the other cached endpoints.
        state, http = self.serve()
        with state.card_lock:
            for path in ('/api/scp81/status', '/api/bip/status',
                         '/api/scp81/script', '/api/scp81/log',
                         '/api/bip/log'):
                with self.subTest(path=path):
                    self.assertEqual(http.get(path)[0], 200)

    def test_the_card_free_lists(self):
        for path in ('/api/scp81/status', '/api/bip/status',
                     '/api/scp81/script', '/api/scp81/log', '/api/bip/log'):
            self.assertIn(path, CARD_FREE_GET)
        for path in ('/api/scp81/bip', '/api/bip/control', '/api/scp81/queue',
                     '/api/scp81/psk-map', '/api/scp81/gen-install'):
            self.assertNotIn(path, CARD_FREE_POST)
            self.assertIn(path, testrun.TEST_BLOCKED_PATHS)
        # the two log clears only touch the terminal log, so they need no card
        # lock; a test run still blocks them (TEST_BLOCKED_PATHS is checked
        # before CARD_FREE_POST in the handler)
        for path in ('/api/scp81/log-clear', '/api/bip/log-clear'):
            self.assertIn(path, CARD_FREE_POST)
            self.assertIn(path, testrun.TEST_BLOCKED_PATHS)

    def test_the_log_clear_answers_under_the_card_lock(self):
        # The log is the terminal's, not the card's: a long card operation
        # must not make the (otherwise useless) Clear button wait.
        state, http = self.serve()
        with state.card_lock:
            for path in ('/api/scp81/log-clear', '/api/bip/log-clear'):
                with self.subTest(path=path):
                    self.assertEqual(http.post(path, {})[0], 200)

    def test_a_test_run_blocks_the_control_posts(self):
        state, http = self.serve()
        state.test_running = True
        try:
            for path in ('/api/scp81/bip', '/api/bip/control',
                         '/api/scp81/queue', '/api/scp81/psk-map',
                         '/api/scp81/gen-install', '/api/scp81/log-clear',
                         '/api/bip/log-clear'):
                with self.subTest(path=path):
                    status, body = http.post(path, {})
                    self.assertEqual(status, 409)
                    self.assertIn('test script running', body['error'])
            # the cached GETs stay available
            self.assertEqual(http.get('/api/scp81/status')[0], 200)
        finally:
            state.test_running = False


@unittest.skipUnless(hasattr(ssl.SSLContext, 'set_psk_server_callback'),
                     'TLS-PSK requires Python 3.13+')
class RequestLogTests(HarnessCase):
    """The request log must not carry the PSK material (the redaction rule)."""

    def test_the_psk_material_is_redacted_in_the_bip_request_log(self):
        # `_redact_psk_fields` is not ported: the shared `redact_body` masks
        # the TLS start's `psk_hex`/`psk_map` (docs/pysim-phaseout.md).
        state = make_state()
        server = build_http_server('127.0.0.1', 0, state, log_requests=True)
        port = server.server_address[1]
        thread = threading.Thread(
            target=server.serve_forever, kwargs={'poll_interval': 0.05},
            name='test-scp81-api-log', daemon=True)
        thread.start()
        try:
            body = {
                'action': 'start', 'mode': 'tls', 'host': '127.0.0.1',
                'port': 0,
                'psk_map': [{'identity': IDENT, 'psk_hex': PSK.hex()},
                            {'identity': IDENT2, 'psk_hex': PSK2.hex()}],
                'psk_hex': PSK.hex(),
            }
            request = Request(
                'http://127.0.0.1:%d/api/scp81/bip' % port,
                data=json.dumps(body).encode('utf-8'), method='POST',
                headers={'Content-Type': 'application/json'})
            with self.assertLogs('simple_server.server', level='INFO') as logs:
                with urlopen(request, timeout=10) as response:
                    self.assertEqual(response.status, 200)
            text = '\n'.join(logs.output)
            self.assertIn('POST /api/scp81/bip', text)
            # the identities stay for diagnostics, the keys never appear
            self.assertIn(IDENT, text)
            self.assertNotIn(PSK.hex(), text)
            self.assertNotIn(PSK2.hex(), text)
            self.assertIn('<redacted>', text)
        finally:
            server.shutdown()
            server.server_close()
            state.close()


# ---- the BIP pill ----------------------------------------------------------


class BipControlTests(HarnessCase):
    """``POST /api/bip/control``: the generic BIP session."""

    def test_the_sink(self):
        state, http = self.serve()
        status, resp = http.post('/api/bip/control',
                                 {'action': 'start', 'mode': 'sink',
                                  'host': '127.0.0.1', 'port': 0})
        self.assertEqual(status, 200, resp)
        self.assertTrue(resp['ok'])
        self.assertEqual(resp['owner'], 'bip')
        self.assertEqual(resp['listener']['mode'], 'sink')
        self.assertGreater(resp['listener']['port'], 0)
        self.assertEqual(resp['listener']['connections'], 0)
        self.assertEqual(http.get('/api/bip/status')[1]['listener']['mode'],
                         'sink')

    def test_sink_is_the_default_mode(self):
        state, http = self.serve()
        status, resp = http.post('/api/bip/control', {'action': 'start'})
        self.assertEqual(status, 200, resp)
        self.assertEqual(resp['listener']['mode'], 'sink')

    def test_redirect_and_passthru(self):
        state, http = self.serve()
        status, resp = http.post('/api/bip/control', {
            'action': 'start', 'mode': 'redirect', 'host': '127.0.0.1',
            'port': 9})
        self.assertEqual(status, 200, resp)
        self.assertEqual(resp['listener']['target'], '127.0.0.1:9')
        self.assertEqual(resp['bip']['target'], '127.0.0.1:9')
        status, resp = http.post('/api/bip/control',
                                 {'action': 'start', 'mode': 'passthru'})
        self.assertEqual(status, 200, resp)
        self.assertEqual(resp['listener'], {'mode': 'passthru'})
        self.assertIsNone(resp['bip']['target'])

    def test_the_replacement_works_both_ways(self):
        state, http = self.serve()
        self.start_tls()
        status, resp = http.post('/api/bip/control',
                                 {'action': 'start', 'mode': 'sink',
                                  'port': 0})
        self.assertEqual(status, 200, resp)
        self.assertEqual(resp['replaced'], {'owner': 'scp81', 'mode': 'tls'})
        self.assertEqual(resp['owner'], 'bip')
        self.assertEqual(http.get('/api/scp81/status')[1]['owner'], 'bip')
        # and the SCP81 listener takes it back
        resp = self.start_tls()
        self.assertEqual(resp['replaced'], {'owner': 'bip', 'mode': 'sink'})
        self.assertEqual(resp['owner'], 'scp81')

    def test_a_stop_reports_and_clears(self):
        state, http = self.serve()
        http.post('/api/bip/control', {'action': 'start', 'mode': 'sink',
                                       'port': 0})
        status, resp = http.post('/api/bip/control', {'action': 'stop'})
        self.assertEqual(status, 200, resp)
        self.assertEqual(resp['replaced'], {'owner': 'bip', 'mode': 'sink'})
        self.assertIsNone(resp['owner'])
        self.assertIsNone(resp['listener'])
        self.assertEqual(http.post('/api/bip/control',
                                   {'action': 'stop'})[1]['replaced'], None)

    def test_the_validation_refusals_are_400s(self):
        state, http = self.serve()
        cases = [
            ({'mode': 'tls'}, 'unsupported mode'),
            ({'mode': 'redirect'}, 'requires the target host and port'),
            ({'mode': 'redirect', 'host': '127.0.0.1'}, 'requires the target'),
            ({'mode': 'sink', 'port': 'x'}, 'port is not a number'),
            ({'mode': 'sink', 'port': 70000}, 'port must be'),
        ]
        for body, message in cases:
            with self.subTest(body=body):
                request = {'action': 'start'}
                request.update(body)
                status, resp = http.post('/api/bip/control', request)
                self.assertEqual(status, 400, resp)
                self.assertFalse(resp['ok'])
                self.assertIn(message, resp['error'])

    def test_a_refused_start_keeps_the_session(self):
        state, http = self.serve()
        first = http.post('/api/bip/control',
                          {'action': 'start', 'mode': 'sink', 'port': 0})[1]
        status, _resp = http.post('/api/bip/control',
                                  {'action': 'start', 'mode': 'nope'})
        self.assertEqual(status, 400)
        self.assertEqual(http.get('/api/bip/status')[1]['listener']['port'],
                         first['listener']['port'])

    def test_link_events_reads_a_literal_false(self):
        state, http = self.serve()
        status, resp = http.post('/api/bip/control',
                                 {'action': 'start', 'mode': 'sink',
                                  'port': 0, 'link_events': 'false'})
        self.assertEqual(status, 200, resp)
        self.assertFalse(state.bip_session.link_events)
        # and a restart without the field restores the default
        http.post('/api/bip/control',
                  {'action': 'start', 'mode': 'sink', 'port': 0})
        self.assertTrue(state.bip_session.link_events)

    def test_an_unparsable_flag_is_a_400(self):
        state, http = self.serve()
        status, resp = http.post('/api/bip/control',
                                 {'action': 'start', 'mode': 'sink',
                                  'link_events': 'maybe'})
        self.assertEqual(status, 400)
        self.assertIn('link_events must be a boolean', resp['error'])


# ---- the script ------------------------------------------------------------


class ScriptTests(HarnessCase):
    """``/api/scp81/script|queue|psk-map|gen-install``."""

    def test_the_queue_needs_a_listener(self):
        state, http = self.serve()
        status, resp = http.post('/api/scp81/queue',
                                 {'apdus': list(EXPLORE)})
        self.assertEqual(status, 400)
        self.assertFalse(resp['ok'])
        self.assertIn('listener is not running', resp['error'])

    def test_the_queue_replaces_the_script(self):
        state, http = self.serve()
        self.start_tls(script=list(EXPLORE))
        status, resp = http.post('/api/scp81/queue',
                                 {'apdus': list(EXPLORE[:1]),
                                  'kind': 'one'})
        self.assertEqual(status, 200, resp)
        self.assertTrue(resp['ok'])
        self.assertTrue(resp['queued'])
        self.assertEqual(resp['apdus'], 1)
        self.assertIn('note', resp)
        script = http.get('/api/scp81/script')[1]
        self.assertEqual(script['script'], EXPLORE[:1])
        self.assertEqual(script['kind'], 'one')
        self.assertEqual(script['next'], 0)

    def test_the_documented_script_alias(self):
        state, http = self.serve()
        self.start_tls()
        status, resp = http.post('/api/scp81/queue',
                                 {'script': list(EXPLORE), 'kind': 'alias'})
        self.assertEqual(status, 200, resp)
        self.assertEqual(http.get('/api/scp81/script')[1]['script'], EXPLORE)

    def test_the_single_apdu_still_works_beside_an_empty_list(self):
        # the legacy `apdus or [apdu]` form: a caller that posts the request
        # shape with empty `apdus` keeps the single-`apdu` shorthand
        state, http = self.serve()
        self.start_tls()
        status, resp = http.post('/api/scp81/queue',
                                 {'apdus': [], 'apdu': '80CAFF2100',
                                  'kind': 'single'})
        self.assertEqual(status, 200, resp)
        self.assertTrue(resp['queued'])
        script = http.get('/api/scp81/script')[1]
        self.assertEqual(script['script'], ['80CAFF2100'])
        self.assertEqual(script['kind'], 'single')

    def test_the_queue_refuses_mid_run_without_force(self):
        state, http = self.serve()
        self.start_tls(script=list(EXPLORE))
        # the listener served the first APDU: the run is in progress
        state.scp81.script.responder('POST', '/api/scp81', {}, b'')
        self.assertEqual(http.get('/api/scp81/script')[1]['next'], 1)
        status, resp = http.post('/api/scp81/queue',
                                 {'apdus': ['80CAFF2100']})
        self.assertEqual(status, 400)
        self.assertFalse(resp['ok'])
        self.assertIn('script in progress', resp['reason'])
        # force restarts with the new list
        status, resp = http.post('/api/scp81/queue',
                                 {'apdus': ['80CAFF2100'], 'force': True})
        self.assertEqual(status, 200, resp)
        self.assertTrue(resp['ok'])
        script = http.get('/api/scp81/script')[1]
        self.assertEqual(script['script'], ['80CAFF2100'])
        self.assertEqual(script['next'], 0)
        self.assertIsNone(script['pending'])

    def test_a_malformed_apdu_is_a_400(self):
        state, http = self.serve()
        self.start_tls()
        for apdus in (['zz'], ['80CAFF'], [42], 'not-a-list'):
            with self.subTest(apdus=apdus):
                status, resp = http.post('/api/scp81/queue',
                                         {'apdus': apdus})
                self.assertEqual(status, 400, resp)
                self.assertFalse(resp['ok'])

    def test_the_psk_map_update(self):
        state, http = self.serve()
        self.start_tls()
        status, resp = http.post('/api/scp81/psk-map', {
            'psk_map': {'id-new': 'ff' * 16}})
        self.assertEqual(status, 200, resp)
        self.assertTrue(resp['ok'])
        self.assertEqual(resp['identities'], ['id-new'])
        self.assertEqual(resp['listener']['psk_identities'], ['id-new'])
        self.assertEqual(http.get('/api/scp81/status')[1]['listener']
                         ['psk_identities'], ['id-new'])
        # the table survives a Stop/Start without keys
        http.post('/api/scp81/bip', {'action': 'stop'})
        status, resp = http.post('/api/scp81/bip',
                                 {'action': 'start', 'mode': 'tls',
                                  'port': 0})
        self.assertEqual(status, 200, resp)
        self.assertEqual(resp['listener']['psk_identities'], ['id-new'])
        # a map/entry problem is a 400
        for body, message in (({'psk_map': 'x'}, 'psk_map must be'),
                              ({'psk_map': []}, 'no usable PSK entries'),
                              ({'psk_map': [{'identity': 'a',
                                             'psk_hex': 'zz'}]},
                               'not valid hex')):
            with self.subTest(body=body):
                status, resp = http.post('/api/scp81/psk-map', body)
                self.assertEqual(status, 400, resp)
                self.assertIn(message, resp['error'])

    def test_the_psk_map_needs_the_tls_listener(self):
        state, http = self.serve()
        http.post('/api/scp81/bip',
                  {'action': 'start', 'mode': 'dump', 'port': 0})
        status, resp = http.post('/api/scp81/psk-map', {
            'psk_map': {'id': PSK.hex()}})
        self.assertEqual(status, 400, resp)
        self.assertIn('PSK TLS listener is not running', resp['error'])

    def test_gen_install(self):
        state, http = self.serve()
        cap_hex = cap_bytes().hex()
        status, resp = http.post('/api/scp81/gen-install',
                                 {'cap_hex': cap_hex})
        self.assertEqual(status, 200, resp)
        self.assertTrue(resp['ok'], resp)
        self.assertEqual(resp['load_file_aid'], 'A0000003080000100001')
        self.assertEqual(resp['module_aid'], 'A000000308000010000101')
        self.assertGreaterEqual(len(resp['apdus']), 3)
        self.assertTrue(resp['apdus'][0].startswith('80E60200'))
        self.assertTrue(any(apdu.startswith('80E8') for apdu in resp['apdus']))
        self.assertTrue(resp['apdus'][-1].startswith('80E60C00'))
        # `make_selectable: false` selects P1 0x04 (GP Table 11-43)
        resp = http.post('/api/scp81/gen-install',
                         {'cap_hex': cap_hex, 'make_selectable': False})[1]
        self.assertTrue(resp['apdus'][-1].startswith('80E60400'))
        # the literal string is read as FALSE (a JSON boolean the PWA sends;
        # `bool("false")` would select make-selectable)
        resp = http.post('/api/scp81/gen-install',
                         {'cap_hex': cap_hex,
                          'make_selectable': 'false'})[1]
        self.assertTrue(resp['apdus'][-1].startswith('80E60400'))
        # a custom SD AID travels in INSTALL [for load]
        resp = http.post('/api/scp81/gen-install', {
            'cap_hex': cap_hex, 'sd_aid': 'A000000003000000'})[1]
        self.assertIn('08A000000003000000', resp['apdus'][0])

    def test_gen_install_refusals_keep_the_legacy_200(self):
        state, http = self.serve()
        for body, message in (
                ({}, 'No cap_hex provided'),
                ({'cap_hex': 'zz'}, 'not valid hex'),
                ({'cap_hex': '00'}, 'not a ZIP archive'),
                ({'cap_hex': cap_bytes().hex(), 'stk_params': 'CA00'},
                 'belongs inside the System Specific Parameters')):
            with self.subTest(body=body):
                status, resp = http.post('/api/scp81/gen-install', body)
                self.assertEqual(status, 200, resp)
                self.assertFalse(resp['ok'])
                self.assertIn(message, resp['error'])

    def test_gen_install_does_not_touch_the_listener(self):
        state, http = self.serve()
        self.start_tls(script=list(EXPLORE), script_kind='Explore ISD')
        before = http.get('/api/scp81/status')[1]
        http.post('/api/scp81/gen-install', {'cap_hex': cap_bytes().hex()})
        after = http.get('/api/scp81/status')[1]
        self.assertEqual(before['listener']['port'], after['listener']['port'])
        self.assertEqual(http.get('/api/scp81/script')[1]['script'], EXPLORE)
        self.assertTrue(any(entry['kind'] == 'gen-install'
                            for entry in http.get('/api/scp81/log')[1]
                            ['entries']))


# ---- the real TLS-PSK administration dialog --------------------------------


def _client_context(identity=IDENT, psk=PSK):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.maximum_version = ssl.TLSVersion.TLSv1_2
    context.set_ciphers('PSK-AES128-CBC-SHA256:PSK-AES128-CBC-SHA')
    context.set_psk_client_callback(lambda hint: (identity.encode(), psk))
    return context


@unittest.skipUnless(hasattr(ssl.SSLContext, 'set_psk_server_callback'),
                     'TLS-PSK requires Python 3.13+')
class TlsDialogTests(HarnessCase):
    """A real PSK handshake and the GP HTTP administration dialog."""

    def _connect(self, port, identity=IDENT, psk=PSK):
        sock = socket.create_connection(('127.0.0.1', port), timeout=5)
        try:
            return _client_context(identity, psk).wrap_socket(sock)
        except Exception:
            sock.close()
            raise

    @staticmethod
    def _recv(tls):
        try:
            return tls.recv(4096)
        except (ssl.SSLError, OSError):
            return b''

    def _read_http(self, tls):
        """One complete HTTP response (chunked or Content-Length framed)."""
        buffer = b''
        while b'\r\n\r\n' not in buffer:
            chunk = self._recv(tls)
            if not chunk:
                return buffer
            buffer += chunk
        head, _, rest = buffer.partition(b'\r\n\r\n')
        if b'transfer-encoding: chunked' in head.lower():
            while b'0\r\n\r\n' not in rest:
                chunk = self._recv(tls)
                if not chunk:
                    break
                rest += chunk
            return head + b'\r\n\r\n' + rest
        length = 0
        for line in head.split(b'\r\n'):
            if line.lower().startswith(b'content-length:'):
                length = int(line.split(b':')[1])
        while len(rest) < length:
            chunk = self._recv(tls)
            if not chunk:
                break
            rest += chunk
        return head + b'\r\n\r\n' + rest

    def test_the_administration_dialog(self):
        state, http = self.serve()
        resp = self.start_tls(script=['80CAFF2100'])
        port = resp['listener']['port']
        tls = self._connect(port)
        try:
            self.assertEqual(tls.version(), 'TLSv1.2')
            self.assertEqual(tls.cipher()[0], 'PSK-AES128-CBC-SHA256')
            tls.sendall(b'POST /api/scp81 HTTP/1.1\r\nHost: 127.0.0.1\r\n'
                        b'X-Admin-Protocol: globalplatform-remote-admin/1.0\r\n'
                        b'X-Admin-From: simple\r\n\r\n')
            reply = self._read_http(tls)
            self.assertIn(b'HTTP/1.1 200 OK', reply)
            self.assertIn(b'X-Admin-Next-URI: /api/scp81?req=1', reply)
            # the C-APDU rides in the indefinite Command Scripting template
            self.assertIn(bytes.fromhex('ae80220580caff21000000'), reply)
            # the card reports the R-APDU in its next POST
            body = bytes.fromhex('af80' '800101' '2304' '93059000' '0000')
            tls.sendall(b'POST /api/scp81?step=1 HTTP/1.1\r\n'
                        b'X-Admin-Script-Status: ok\r\n'
                        b'Content-Length: %d\r\n\r\n' % len(body) + body)
            reply = self._read_http(tls)
            self.assertIn(b'HTTP/1.1 204 No Content', reply)
        finally:
            tls.close()
        # the handshake and the result are visible through the API
        deadline = time.time() + 3
        status = http.get('/api/scp81/status')[1]['listener']
        while time.time() < deadline and status['identity_seen'] is None:
            time.sleep(0.05)
            status = http.get('/api/scp81/status')[1]['listener']
        self.assertEqual(status['identity_seen'], IDENT)
        self.assertTrue(status['identity_matched'])
        self.assertEqual(status['version_seen'], 'TLSv1.2')
        self.assertEqual(status['cipher_seen'], 'PSK-AES128-CBC-SHA256')
        script = http.get('/api/scp81/script')[1]
        self.assertEqual(script['results'][0]['sw'], '9000')
        self.assertEqual(script['results'][0]['apdu'], '80CAFF2100')
        self.assertEqual(script['done'], [0])
        self.assertTrue(script['complete'])
        kinds = [entry['kind'] for entry in http.get('/api/scp81/log')[1]
                 ['entries']]
        for kind in ('tls-listener-start', 'tls-handshake', 'tls-request',
                     'script-send', 'script-rapdu', 'tls-response',
                     'script-done'):
            self.assertIn(kind, kinds)

    def test_an_unknown_identity_is_refused_and_logged(self):
        state, http = self.serve()
        resp = self.start_tls()
        port = resp['listener']['port']
        with self.assertRaises(ssl.SSLError):
            self._connect(port, identity='unknown-identity')
        deadline = time.time() + 3
        while time.time() < deadline:
            entries = http.get('/api/scp81/log')[1]['entries']
            if any(entry['kind'] == 'tls-psk-unknown' for entry in entries):
                break
            time.sleep(0.05)
        self.assertTrue(any(entry['kind'] == 'tls-psk-unknown'
                            for entry in entries))
        status = http.get('/api/scp81/status')[1]['listener']
        self.assertEqual(status['identity_seen'], 'unknown-identity')
        self.assertFalse(status['identity_matched'])

    def test_the_psk_map_update_reaches_the_handshake(self):
        state, http = self.serve()
        resp = self.start_tls()
        port = resp['listener']['port']
        http.post('/api/scp81/psk-map', {'psk_map': {'id-new': PSK2.hex()}})
        tls = self._connect(port, identity='id-new', psk=PSK2)
        tls.close()
        deadline = time.time() + 3
        while time.time() < deadline:
            status = http.get('/api/scp81/status')[1]['listener']
            if status['identity_seen'] == 'id-new':
                break
            time.sleep(0.05)
        self.assertEqual(status['identity_seen'], 'id-new')
        self.assertTrue(status['identity_matched'])

    def test_a_completed_script_restarts_on_a_fresh_dialog(self):
        state, http = self.serve()
        resp = self.start_tls(script=['80CAFF2100'])
        port = resp['listener']['port']

        def run_dialog():
            """One administration dialog; returns its first reply."""
            tls = self._connect(port)
            try:
                tls.sendall(b'POST /api/scp81 HTTP/1.1\r\n\r\n')
                reply = self._read_http(tls)
                body = bytes.fromhex('af80' '800101' '2304' '93059000'
                                     '0000')
                tls.sendall(b'POST /api/scp81 HTTP/1.1\r\n'
                            b'X-Admin-Script-Status: ok\r\n'
                            b'Content-Length: %d\r\n\r\n' % len(body) + body)
                self.assertIn(b'HTTP/1.1 204 No Content',
                              self._read_http(tls))
                return reply
            finally:
                tls.close()

        self.assertIn(b'HTTP/1.1 200 OK', run_dialog())
        self.assertTrue(http.get('/api/scp81/script')[1]['complete'])
        # a new trigger (a fresh POST without X-Admin-Resume) runs it again
        self.assertIn(b'HTTP/1.1 200 OK', run_dialog())
        script = http.get('/api/scp81/script')[1]
        self.assertEqual(script['done'], [0])
        self.assertEqual(len(script['results']), 1)


# ---- the lifecycle ---------------------------------------------------------


class LifecycleTests(HarnessCase):
    """The one-session rule across a card change and the shutdown."""

    def test_close_stops_the_listener(self):
        state = make_state()
        http = HTTPHarness(state)
        try:
            status, resp = http.post('/api/scp81/bip', {
                'action': 'start', 'mode': 'dump', 'host': '127.0.0.1',
                'port': 0})
            port = resp['listener']['port']
            socket.create_connection(('127.0.0.1', port), timeout=2).close()
        finally:
            http.close()
        with self.assertRaises(OSError):
            socket.create_connection(('127.0.0.1', port), timeout=2)

    def test_the_listener_survives_a_card_session_teardown(self):
        # The listener is the terminal side, not per-card state: an equip /
        # removal closes the old card's channels but keeps the session, so a
        # card swap does not need a listener restart.
        state, http = self.serve()
        resp = self.start_tls()
        port = resp['listener']['port']
        session = _FakeCatSession()
        state.bip_session.attach_card(session)
        state.bip = state.bip_session
        self.assertIs(state.bip_session.cat_session, session)
        state._teardown_session(bump=True)
        self.assertIsNone(state.bip)
        self.assertIsNone(state.bip_session.cat_session)
        body = http.get('/api/scp81/status')[1]
        self.assertEqual(body['owner'], 'scp81')
        self.assertEqual(body['listener']['port'], port)
        # the listener still answers (its socket was never closed)
        socket.create_connection(('127.0.0.1', port), timeout=2).close()

    def test_the_script_and_psk_state_survive_a_teardown(self):
        state, http = self.serve()
        self.start_tls(script=list(EXPLORE), script_kind='Explore ISD')
        state._teardown_session(bump=True)
        script = http.get('/api/scp81/script')[1]
        self.assertEqual(script['script'], EXPLORE)
        self.assertEqual(script['kind'], 'Explore ISD')
        self.assertEqual(http.get('/api/scp81/status')[1]['listener']
                         ['psk_identities'], sorted([IDENT, IDENT2]))

    def test_a_cardless_start_is_normal(self):
        state, http = self.serve()
        self.assertFalse(state.connected)
        self.assertIsNone(state.cat)
        self.assertIsNone(state.bip)
        # the control works cardless; the CAT hooks stay inert
        resp = self.start_tls(script=list(EXPLORE))
        self.assertEqual(resp['owner'], 'scp81')
        self.assertEqual(http.get('/api/scp81/script')[1]['script'], EXPLORE)
        self.assertFalse(state.bip_session._card_connected())


class _FakeCatSession(object):
    """The CAT-session slice the BIP events read (no card exchange)."""

    event_list = []
    busy = False
    stk_pending = None

    def send_event_download(self, event_type, data, drain=True):
        return '9000'


if __name__ == '__main__':
    unittest.main()
