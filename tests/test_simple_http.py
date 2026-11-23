# coding=utf-8
"""Tests for :mod:`simple_server.server` (the pysim-free HTTP core).

The suite needs no card and no pysim: every exchange is driven through a
scripted fake transport (``send_apdu`` answers registered by APDU prefix), and
the HTTP endpoints are exercised on a real :class:`ThreadingHTTPServer` bound to
an ephemeral loopback port with ``urllib.request``.  The preset store and the
outfile parser are the specification-only modules in :mod:`simple_server.shared`
(the 3.x copies were moved there verbatim; after the cut-over they are the only
implementation in this repository).

Spec anchors: TS 102 221 §11.2 (the CAT APDUs), §13.2 (EF.ICCID), §14.6.2 (the
idle poll); TS 102 223 §6.4.6/§6.4.14 (POLL INTERVAL / POLLING OFF); TS 31.102
§4.4.2.1 (EF.PBR); ISO 7816-4 Table 25 / TS 102 221 §11.1.1.4 (the FCP
``8B`` EF.ARR reference); TS 102 225 §5.1.1/§5.2 and TS 31.115 §4.2/§4.3
(SCP80: the SPI bits, the response packet, the concatenated SMS-PP download);
TS 102 226 §5.1.2/§5.2.2 (the compact / scripting response forms).
"""

import json
import os
import pathlib
import shutil
import socket
import tempfile
import threading
import time
import types
import unittest
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from simple_server.shared import presets as presets_store

from simple_server import cards
from simple_server import cat_session
from simple_server import ota
from simple_server import pbr
from simple_server import scp80
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


# ---- the file endpoints' fake filesystem (Phase 3c-3b) ---------------------
#
# One fake UICC with a small filesystem: the FIDs the standard file list knows
# (MF, EF.ICCID, EF.ARR, DF.TELECOM, DF.PHONEBOOK, EF.PBR) plus **EF.ADN 4F3A,
# which the standard list does not list under DF.PHONEBOOK** - it is the
# PBR-injected phonebook file and therefore the custom-file probe path a real
# card exercises.

def fcp(*objects):
    """An FCP template (tag 62) around the given data objects (hex strings)."""
    body = ''.join(objects)
    return bytes.fromhex('62%02X' % (len(body) // 2) + body)


FD_TRANSPARENT = '82020101'
FD_DF = '820138'


def fd_linear(record_len, records):
    """A linear-fixed file descriptor (TS 102 221 Table 11.5: 4A 00 len n)."""
    return '8205' + '4A00%04X%02X' % (record_len, records)


ADN_RECORDS = ['A0' * 28, 'B1' * 28]
# The EF.PBR record of the file-endpoint fixture: one type-1 container with
# EF.ADN (FID + SFI '02') and EF.PBC (FID + SFI '04') - the SFI-bearing form,
# so the injected tree children carry their ``sfi``.
PBR_STRUCTURE = ['A80AC0034F3A02C5034F0904']
IMSI_CONTENT = '082949709911111111'

FILE_MODEL = {
    '3F00': {'fcp': fcp(FD_DF)},
    '2FE2': {'fcp': fcp(FD_TRANSPARENT, '8002000A', '83022FE2'),
             'content': ICCID_HEX},
    '2F06': {'fcp': fcp(fd_linear(10, 5), '83022F06'),
             'records': ARR_RECORDS},
    '7F10': {'fcp': fcp(FD_DF)},
    '7F20': {'fcp': fcp(FD_DF)},
    '5F3A': {'fcp': fcp(FD_DF)},
    '4F30': {'fcp': fcp(fd_linear(10, 1), '80020A83024F30'),
             'records': PBR_STRUCTURE},
    '4F3A': {'fcp': fcp(fd_linear(28, 2), '800238', '83024F3A'),
             'records': ADN_RECORDS},
    '6F07': {'fcp': fcp(FD_TRANSPARENT, '80020009', '83026F07'),
             'content': IMSI_CONTENT},
}


class FileCard(object):
    """A UICC with a real filesystem behind it (``FILE_MODEL``).

    Selection is tracked by FID/AID (the fixture's FIDs are unique), so the
    parent chain is only asserted through the APDUs the server sends; UPDATEs
    mutate the served content, so a later read - or the EF.ARR cache refresh -
    sees the new bytes.  ``refuse`` maps an operation name
    (``read_binary``/``read_record``/``update_binary``/``update_record``/
    ``select``) to a status word the fake answers instead; ``refuse_records``
    maps a record number to a status word (one refused record mid-file).
    """

    def __init__(self, files=None, refuse=None, refuse_records=None):
        self.files = {fid: dict(entry) for fid, entry in FILE_MODEL.items()}
        for fid, entry in (files or {}).items():
            self.files[fid] = dict(entry)
        self.refuse = dict(refuse or {})
        # {record_number: sw}: a card that refuses one record mid-file (a
        # protected record, a stale record) while the FCP announces more.
        self.refuse_records = dict(refuse_records or {})
        self.selected = None
        self.sent = []
        self.writes = []
        self.reader_name = 'Fake reader'
        self.protocol = 1
        self.atr = ATR

    # -- the transport shape --------------------------------------------
    def reset_card(self):
        return 1

    def connect(self):
        return self

    def disconnect(self):
        pass

    def get_atr(self):
        return self.atr

    def send_apdu(self, apdu):
        apdu = bytes(apdu)
        self.sent.append(apdu)
        answer = self._answer(apdu)
        return answer if answer is not None else (b'', b'\x6a\x82')

    # -- the filesystem --------------------------------------------------
    def _answer(self, apdu):
        ins = apdu[1]
        if apdu[0] == 0x80 and ins in (0x10, 0xF2):     # TP / STATUS poll
            return b'', b'\x90\x00'
        if ins == 0xA4:
            return self._refused('select') or self._select(apdu)
        if self.selected is None or self.selected not in self.files:
            return b'', b'\x6a\x82'
        if ins == 0xB0:
            return self._refused('read_binary') or self._read_binary(apdu)
        if ins == 0xB2:
            refused = self._refused('read_record')
            if refused is None:
                sw = self.refuse_records.get(apdu[2])
                refused = (b'', bytes.fromhex(sw)) if sw else None
            return refused or self._read_record(apdu)
        if ins == 0xD6:
            return self._refused('update_binary') or self._update_binary(apdu)
        if ins == 0xDC:
            return self._refused('update_record') or self._update_record(apdu)
        return b'', b'\x6a\x81'

    def _refused(self, operation):
        sw = self.refuse.get(operation)
        return (b'', bytes.fromhex(sw)) if sw else None

    def _select(self, apdu):
        if apdu[2] == 0x04:                          # by DF name (AID)
            aid = apdu[5:5 + apdu[4]].hex().upper()
            if aid == cards.AID_USIM:
                self.selected = 'ADF.USIM'
                return self.files['3F00']['fcp'], b'\x90\x00'
            return b'', b'\x6a\x82'
        fid = apdu[5:7].hex().upper()
        entry = self.files.get(fid)
        if entry is None:
            return b'', b'\x6a\x82'
        self.selected = fid
        return entry['fcp'], b'\x90\x00'

    def _read_binary(self, apdu):
        offset = (apdu[2] << 8) | apdu[3]
        length = apdu[4] or 256
        content = bytes.fromhex(self.files[self.selected].get('content') or '')
        return content[offset:offset + length], b'\x90\x00'

    def _read_record(self, apdu):
        records = self.files[self.selected].get('records') or []
        number = apdu[2]
        if not 1 <= number <= len(records):
            return b'', b'\x6a\x83'
        return bytes.fromhex(records[number - 1]), b'\x90\x00'

    def _update_binary(self, apdu):
        offset = (apdu[2] << 8) | apdu[3]
        data = apdu[5:5 + apdu[4]].hex().upper()
        self.writes.append((self.selected, None, data))
        content = self.files[self.selected].get('content') or ''
        self.files[self.selected]['content'] = (
            content[:2 * offset] + data + content[2 * (offset + apdu[4]):])
        return b'', b'\x90\x00'

    def _update_record(self, apdu):
        number = apdu[2]
        data = apdu[5:5 + apdu[4]].hex().upper()
        self.writes.append((self.selected, number, data))
        records = list(self.files[self.selected].get('records') or [])
        if not 1 <= number <= len(records):
            return b'', b'\x6a\x83'
        records[number - 1] = data
        self.files[self.selected]['records'] = records
        return b'', b'\x90\x00'


def open_file_state(handler=None, **kwargs):
    """A cardless state with ``FileCard`` equipped (its caches populated)."""
    state = make_state(**kwargs)
    fake = handler if handler is not None else FileCard()
    state.open(transport=fake, start_threads=False)
    return state, fake


#: A throw-away root for the test-script/-suite stores: a state must never
#: read or write the user's real ``~/.pysim-simple-server`` files.
_STORE_ROOT = pathlib.Path(tempfile.mkdtemp(prefix='simple-http-stores-'))
_STORE_COUNTER = [0]


def _store_path(name):
    _STORE_COUNTER[0] += 1
    return _STORE_ROOT / ('%s-%d.json' % (name, _STORE_COUNTER[0]))


def make_state(**kwargs):
    """A cardless ServerState with the test seams set."""
    kwargs.setdefault('monitor', False)
    kwargs.setdefault('presets', FakePresetStore())
    kwargs.setdefault('readers_fn', lambda: ['Reader A', 'Reader B'])
    kwargs.setdefault('poll_interval', 30)
    kwargs.setdefault('test_scripts_path', _store_path('test_scripts'))
    kwargs.setdefault('test_suites_path', _store_path('test_suites'))
    return ServerState(**kwargs)


def open_state(handler=None, **kwargs):
    state = make_state(**kwargs)
    fake = FakeTransport(handler=handler or CardScript())
    state.open(transport=fake, start_threads=False)
    return state, fake


class HTTPHarness(object):
    """A real ThreadingHTTPServer on an ephemeral loopback port."""

    def __init__(self, state, web_dir=None, log_requests=False):
        self.state = state
        self.server = build_http_server('127.0.0.1', 0, state,
                                        web_dir=web_dir,
                                        log_requests=log_requests)
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

    def served(self, state, web_dir=None, log_requests=False):
        self.harness = HTTPHarness(state, web_dir=web_dir,
                                   log_requests=log_requests)
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
        self.assertFalse(body['adm_verified'])
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
        self.assertFalse(body['adm_verified'])
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
        # The guard keys on the persistent `bip_session.owner`, not the
        # per-card `bip` alias: a cardless listener (state.bip is None) must
        # refuse the switch too - it resets the card and kills the channels.
        for card in (False, True):
            with self.subTest(card=card):
                state = self.track(make_state(reader='Reader A'))
                state.bip_session.owner = 'scp81'
                state.bip = (types.SimpleNamespace(owner='scp81')
                             if card else None)
                status, body = self.served(state).json(
                    'POST', '/api/reader', {'name': 'Reader B'})
                self.assertEqual(status, 409)
                self.assertIn('BIP session', body['error'])
                self.assertEqual(state.reader_name, 'Reader A')

    def test_reader_switch_is_allowed_when_the_bip_session_is_idle(self):
        state = self.track(make_state(transport_factory=lambda name:
                                      FakeTransport(handler=CardScript(),
                                                    reader_name=name),
                                      reader='Reader A'))
        state.open(start_threads=False)
        state.bip_session.owner = None
        state.bip = None
        status, _body = self.served(state).json(
            'POST', '/api/reader', {'name': 'Reader B'})
        self.assertEqual(status, 200)

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


class FileTreeTests(HTTPTestBase):
    """``POST /api/tree``: the file model's children, not pySim's tree output."""

    def test_mf_lists_the_model_children_and_the_adf_roots(self):
        state, _fake = open_file_state()
        self.track(state)
        status, body = self.served(state).json(
            'POST', '/api/tree', {'name': 'MF'})
        self.assertEqual(status, 200)
        self.assertTrue(body['exists'])
        self.assertEqual(body['name'], 'MF')
        self.assertEqual(body['fid'], '3F00')
        self.assertEqual(body['file_type'], 'df')
        children = {child['name']: child for child in body['children']}
        self.assertEqual(children['EF.ICCID']['fid'], '2FE2')
        self.assertFalse(children['EF.ICCID']['isDir'])
        self.assertTrue(children['DF.TELECOM']['isDir'])
        # An ADF is a model root beside MF but an MF child in the tree, and it
        # carries its AID (the Profiler's walk roots an ADF by the AID).
        self.assertTrue(children['ADF.USIM']['isDir'])
        self.assertEqual(children['ADF.USIM']['aid'], cards.AID_USIM)
        for child in body['children']:
            self.assertIn('name', child)
            self.assertIn('fid', child)
            self.assertIn('isDir', child)

    def test_a_df_lists_its_children_with_the_injected_pbr_files(self):
        state, _fake = open_file_state()
        self.assertTrue(state.pbr, 'the equip read must cache EF.PBR')
        self.track(state)
        status, body = self.served(state).json('POST', '/api/tree', {
            'name': 'DF.PHONEBOOK', 'fid': '5F3A',
            'parent_sel': 'MF', 'parent_path': ['MF', '7F10']})
        self.assertEqual(status, 200, body)
        self.assertEqual((body['name'], body['fid']), ('DF.PHONEBOOK', '5F3A'))
        self.assertEqual(body['file_type'], 'df')
        children = {child['name']: child for child in body['children']}
        # The model's own children stay...
        self.assertIn('EF.PBR', children)
        self.assertFalse(children['EF.PBR'].get('pbr', False))
        # ...and the PBR-referenced phonebook files are appended.
        self.assertEqual(children['EF.ADN']['fid'], '4F3A')
        self.assertTrue(children['EF.ADN']['pbr'])
        self.assertEqual(children['EF.ADN']['sfi'], '02')
        self.assertFalse(children['EF.ADN']['isDir'])
        self.assertEqual(children['EF.PBC']['sfi'], '04')
        # A file the model already lists is never added twice.
        self.assertEqual([child['fid'] for child in body['children']].count(
            '4F30'), 1)

    def test_the_pbr_injection_matches_the_requests_own_df(self):
        # The cache holds the ADF copy only: the MF/DF.TELECOM phonebook DF
        # must not be answered by it, while the ADF's DF is.
        state, _fake = open_file_state()
        state.pbr = [{'fid': '4F30', 'path': 'ADF.USIM/5F3A/4F30',
                      'record_len': 10, 'num_records': 1,
                      'records': PBR_RECORDS}]
        self.track(state)
        harness = self.served(state)
        status, body = harness.json('POST', '/api/tree', {
            'name': 'DF.PHONEBOOK', 'fid': '5F3A', 'parent_path': ['MF', '7F10']})
        self.assertEqual(status, 200, body)
        self.assertNotIn('4F3A', [child['fid'] for child in body['children']])
        status, body = harness.json('POST', '/api/tree', {
            'name': 'DF.PHONEBOOK', 'fid': '5F3A', 'parent_path': ['MF', 'ADF.USIM']})
        self.assertEqual(status, 200, body)
        self.assertIn('4F3A', [child['fid'] for child in body['children']])

    def test_a_custom_file_is_probed_without_the_flag(self):
        # EF.ADN under DF.PHONEBOOK is not in the standard list; the request
        # carries no allow_probe, and the FID is still selected.
        state, fake = open_file_state()
        self.track(state)
        status, body = self.served(state).json('POST', '/api/tree', {
            'name': 'EF.ADN', 'fid': '4F3A',
            'parent_path': ['MF', '7F10', '5F3A']})
        self.assertEqual(status, 200, body)
        self.assertTrue(body['exists'])
        self.assertEqual((body['name'], body['fid']), ('4F3A', '4F3A'))
        self.assertEqual(body['file_type'], 'linear_fixed')
        self.assertEqual(body['children'], [])
        selects = [apdu.hex().upper() for apdu in fake.sent
                   if apdu[:2] == b'\x00\xa4']
        self.assertEqual(selects[-4:],
                         ['00A40004023F0000', '00A40004027F1000',
                          '00A40004025F3A00', '00A40004024F3A00'])

    def test_an_absent_custom_file_is_exists_false(self):
        state, _fake = open_file_state()
        self.track(state)
        status, body = self.served(state).json('POST', '/api/tree', {
            'name': 'EF.X', 'fid': 'A1B2', 'parent_path': ['MF']})
        self.assertEqual(status, 404)
        self.assertFalse(body['exists'])
        self.assertIn('A1B2', body['error'])

    def test_an_unknown_model_node_is_a_404(self):
        state, _fake = open_file_state()
        self.track(state)
        status, body = self.served(state).json(
            'POST', '/api/tree', {'name': 'EF.NOPE', 'parent_path': ['MF']})
        self.assertEqual(status, 404)
        self.assertFalse(body['exists'])
        self.assertIn('EF.NOPE', body['error'])

    def test_an_ambiguous_bare_fid_is_never_guessed(self):
        state, _fake = open_file_state()
        self.track(state)
        status, body = self.served(state).json(
            'POST', '/api/tree', {'fid': '5F3A'})
        self.assertEqual(status, 404)
        self.assertIn('ambiguous', body['error'])

    def test_a_cardless_server_is_a_503(self):
        state = self.track(make_state())
        status, body = self.served(state).json(
            'POST', '/api/tree', {'name': 'MF'})
        self.assertEqual(status, 503)
        self.assertFalse(body['exists'])
        self.assertIn('no card session', body['error'])
        self.assertFalse(body['connected'])


class FileSelectTests(HTTPTestBase):
    def test_the_metadata_and_the_published_selection(self):
        state, _fake = open_file_state()
        self.track(state)
        status, body = self.served(state).json('POST', '/api/select', {
            'name': 'EF.ICCID', 'fid': '2FE2', 'parent_path': ['MF']})
        self.assertEqual(status, 200, body)
        self.assertEqual(body['name'], 'EF.ICCID')
        self.assertEqual(body['fid'], '2FE2')
        self.assertEqual(body['file_type'], 'transparent')
        self.assertEqual(body['file_size'], 10)
        self.assertIsNone(body['record_len'])
        self.assertIsNone(body['num_of_rec'])
        self.assertTrue(body['fci_hex'].startswith('620C82020101'))
        self.assertTrue(body['exists'])
        self.assertTrue(body['apdu_times'])
        self.assertEqual({entry['type'] for entry in body['apdu_times']},
                         {'select'})
        self.assertEqual(state.selection, {
            'path': 'MF/2FE2', 'name': 'EF.ICCID', 'fid': '2FE2', 'kind': 'ef',
            'size': 10, 'record_len': None, 'num_records': None})
        self.assertEqual(state.status()['selection'], state.selection)

    def test_a_record_file_reports_the_record_metadata(self):
        state, _fake = open_file_state()
        self.track(state)
        status, body = self.served(state).json(
            'POST', '/api/select', {'path': 'MF/2F06'})
        self.assertEqual(status, 200, body)
        self.assertEqual(body['file_type'], 'linear_fixed')
        self.assertEqual(body['record_len'], 10)
        self.assertEqual(body['num_of_rec'], 5)
        self.assertEqual(state.selection['num_records'], 5)

    def test_an_adf_file_is_selected_by_its_aid(self):
        state, fake = open_file_state()
        self.track(state)
        status, body = self.served(state).json(
            'POST', '/api/select', {'path': 'ADF.USIM/6F07'})
        self.assertEqual(status, 200, body)
        self.assertEqual(body['name'], 'EF.IMSI')
        self.assertEqual(body['fid'], '6F07')
        self.assertEqual(body['file_size'], 9)
        self.assertEqual(state.selection['path'], 'ADF.USIM/6F07')
        self.assertIn('00A4040407A000000087100200',
                      [apdu.hex().upper() for apdu in fake.sent])

    def test_an_adf_addressed_by_aid_resolves(self):
        # The PWA's Profiler walk sends the ADF's AID in the parent path while
        # the file manager sends its name; both fold onto the same node.
        state, _fake = open_file_state()
        self.track(state)
        status, body = self.served(state).json('POST', '/api/select', {
            'name': 'EF.IMSI', 'fid': '6F07', 'parent_sel': 'MF',
            'parent_path': ['MF', cards.AID_USIM]})
        self.assertEqual(status, 200, body)
        self.assertEqual(body['name'], 'EF.IMSI')

    def test_a_custom_df_chain_is_walked_by_fid(self):
        # A PWA custom file may sit under a DF the standard list does not
        # describe; every segment is then a FID and the chain is selected from
        # MF without any model entry.
        state, fake = open_file_state(handler=FileCard(files={
            'A1B2': {'fcp': fcp(FD_DF)},
            'A1B3': {'fcp': fcp(FD_TRANSPARENT, '800204', '8302A1B3'),
                     'content': 'DEADBEEF'},
        }))
        self.track(state)
        status, body = self.served(state).json(
            'POST', '/api/select', {'path': 'MF/A1B2/A1B3'})
        self.assertEqual(status, 200, body)
        self.assertEqual((body['name'], body['fid']), ('A1B3', 'A1B3'))
        selects = [apdu.hex().upper() for apdu in fake.sent
                   if apdu[:2] == b'\x00\xa4']
        self.assertEqual(selects[-3:],
                         ['00A40004023F0000', '00A4000402A1B200',
                          '00A4000402A1B300'])

    def test_a_failed_select_is_a_404(self):
        state, _fake = open_file_state()
        self.track(state)
        status, body = self.served(state).json(
            'POST', '/api/select', {'path': 'MF/9F9F'})
        self.assertEqual(status, 404)
        self.assertFalse(body['exists'])
        self.assertIn('9F9F', body['error'])
        self.assertIsNone(state.selection)

    def test_a_sim_uses_the_ts_51_011_select_form(self):
        # A 2G SIM's file access is CLA A0 and the TS 51 011 §9.2.1 SELECT
        # (P1/P2 00, no Le, no FCP).
        state = self.track(make_state())
        fake = FileCard()
        state.transport = fake
        state.card = cards.CardType(kind='sim', cla='a0', aids=[], atr=ATR)
        state.connected = True
        status, body = self.served(state).json(
            'POST', '/api/select', {'path': 'MF/2FE2'})
        self.assertEqual(status, 200, body)
        self.assertEqual(body['file_type'], 'transparent')
        selects = [apdu.hex().upper() for apdu in fake.sent]
        self.assertEqual(selects, ['A0A40000023F00', 'A0A40000022FE2'])

    def test_a_probed_node_keeps_the_legacy_structure_name(self):
        # A probed custom node has no model kind, so the FCP decides.  A DF's
        # descriptor byte 38 has the RFU structure bits (TS 102 221 Table
        # 11.5: file type 111, b3-b1 000), which pySim's enum - and therefore
        # the legacy /api/select - names 'no_info_given'; the PWA's stored
        # profiles and snapshots pin that exact string with ===.
        state, _fake = open_file_state(handler=FileCard(files={
            'A1B2': {'fcp': fcp(FD_DF)}}))
        self.track(state)
        status, body = self.served(state).json(
            'POST', '/api/select', {'path': 'MF/A1B2'})
        self.assertEqual(status, 200, body)
        self.assertEqual(body['file_type'], 'no_info_given')
        self.assertEqual(body['fid'], 'A1B2')

    def test_the_adf_retry_only_drops_mf_before_an_adf(self):
        # A whole-chain ADF path (MF/ADF.USIM/6F07) is retried without its
        # leading MF because an ADF is a model root; an EF second segment is
        # not: MF/EF.IMPI says "an EF.IMPI directly under MF", which does not
        # exist, and the old unconditional retry silently selected ADF.ISIM's
        # copy instead of answering 404.
        state, fake = open_file_state()
        self.track(state)
        sent = len(fake.sent)
        status, body = self.served(state).json(
            'POST', '/api/select', {'path': 'MF/EF.IMPI'})
        self.assertEqual(status, 404, body)
        self.assertIn('EF.IMPI', body['error'])
        self.assertEqual(len(fake.sent), sent,
                         'an unresolved selector must spend no APDU')

    def test_a_whole_chain_adf_path_is_retried(self):
        # The other half of the same rule: the PWA's MF/ADF.USIM/... form
        # still resolves (an ADF is a root of its own).
        state, _fake = open_file_state()
        self.track(state)
        status, body = self.served(state).json(
            'POST', '/api/select', {'path': 'MF/ADF.USIM/6F07'})
        self.assertEqual(status, 200, body)
        self.assertEqual(body['name'], 'EF.IMSI')

    def test_a_card_without_an_fcp_cannot_be_read(self):
        # A 2G SIM's SELECT carries no FCP (TS 51 011 §9.2.1) and a UICC may
        # omit it too.  /api/read is FCP-driven, so it refuses instead of
        # guessing a length: this is a KNOWN unsupported case until the
        # decision recorded in docs/pysim-phaseout.md (an explicit `length`,
        # or READ BINARY Le=256 stopping on a short read) - the legacy server
        # failed here as well, and this test makes a future fix visible.
        state, _fake = open_file_state(handler=FileCard(files={
            '2FE2': {'fcp': b'', 'content': ICCID_HEX}}))
        self.track(state)
        harness = self.served(state)
        status, body = harness.json(
            'POST', '/api/select', {'path': 'MF/2FE2'})
        self.assertEqual(status, 200, body)
        self.assertEqual(body['file_type'], 'transparent')
        self.assertIsNone(body['file_size'])
        self.assertIsNone(body['fci_hex'])
        status, body = harness.json('POST', '/api/read', {'path': 'MF/2FE2'})
        self.assertEqual(status, 200, body)
        self.assertFalse(body['success'])
        self.assertIsNone(body['sw'])
        self.assertIn('no file size', body['error'])


class FileReadTests(HTTPTestBase):
    def test_a_transparent_read(self):
        state, _fake = open_file_state()
        self.track(state)
        status, body = self.served(state).json(
            'POST', '/api/read', {'path': 'MF/2FE2', 'mode': 'raw'})
        self.assertEqual(status, 200, body)
        self.assertTrue(body['success'])
        self.assertEqual(body['sw'], '9000')
        self.assertEqual(body['file_type'], 'transparent')
        self.assertEqual(body['data'], ICCID_HEX.upper())
        self.assertNotIn('records', body)
        types = [entry['type'] for entry in body['apdu_times']]
        self.assertEqual(types.count('read_binary'), 1)
        self.assertEqual(types, sorted(types, key=lambda t: t != 'select'))

    def test_a_record_read_reports_every_record(self):
        state, _fake = open_file_state()
        self.track(state)
        status, body = self.served(state).json(
            'POST', '/api/read', {'path': 'MF/2F06'})
        self.assertEqual(status, 200, body)
        self.assertTrue(body['success'])
        self.assertEqual(body['file_type'], 'linear_fixed')
        self.assertEqual(body['records'],
                         [{'num': index + 1, 'data': value}
                          for index, value in enumerate(ARR_RECORDS)])
        record_times = [entry['ms'] for entry in body['apdu_times']
                        if entry['type'] == 'read_record']
        self.assertEqual(len(record_times), len(ARR_RECORDS))

    def test_an_adf_read(self):
        state, _fake = open_file_state()
        self.track(state)
        status, body = self.served(state).json(
            'POST', '/api/read', {'path': 'ADF.USIM/6F07'})
        self.assertEqual(status, 200, body)
        self.assertEqual(body['data'], IMSI_CONTENT)

    def test_a_refused_read_is_success_false_with_the_sw(self):
        # The PWA prints the SW and offers its inline ADM action for '6982'
        # (it compares the lower case form).
        state, _fake = open_file_state(
            handler=FileCard(refuse={'read_binary': '6982'}))
        self.track(state)
        status, body = self.served(state).json(
            'POST', '/api/read', {'path': 'MF/2FE2'})
        self.assertEqual(status, 200, body)
        self.assertFalse(body['success'])
        self.assertEqual(body['sw'], '6982')
        self.assertEqual(body['error'], 'Security status not satisfied')
        self.assertEqual(body['file_type'], 'transparent')

    def test_a_refused_record_mid_file_is_not_a_success(self):
        # The FCP announces 5 records; the card refuses the 3rd (a protected
        # record).  The legacy ran pySim's read_records, whose SwMatchError
        # became {'success': false, ...}, and the PWA's `if (rd && rd.success)`
        # then skipped the content - a truncated list must never look
        # complete (it would land in a snapshot or profile verbatim).
        state, _fake = open_file_state(
            handler=FileCard(refuse_records={3: '6985'}))
        self.track(state)
        status, body = self.served(state).json(
            'POST', '/api/read', {'path': 'MF/2F06'})
        self.assertEqual(status, 200, body)
        self.assertFalse(body['success'])
        self.assertEqual(body['sw'], '6985')
        self.assertEqual(body['error'], 'Conditions of use not satisfied')
        self.assertEqual(body['file_type'], 'linear_fixed')
        self.assertEqual(body['records'],
                         [{'num': index + 1, 'data': value}
                          for index, value in enumerate(ARR_RECORDS[:2])])

    def test_an_fcp_without_a_record_count_reads_until_refused(self):
        # TS 102 221 §11.1.1.4.3: bytes 5-6 of the file descriptor are the
        # record length, byte 7 the record count.  A 4-byte descriptor
        # (8204 4A000010) stops at the record length, so the card promises no
        # count - the tolerant read-until-refused behaviour stays for exactly
        # this case (the legacy pySim read_records).
        state, _fake = open_file_state(handler=FileCard(files={
            '2F06': {'fcp': fcp('82044A000010', '83022F06'),
                     'records': ARR_RECORDS[:2]}}))
        self.track(state)
        status, body = self.served(state).json(
            'POST', '/api/read', {'path': 'MF/2F06'})
        self.assertEqual(status, 200, body)
        self.assertTrue(body['success'])
        self.assertEqual(body['sw'], '9000')
        self.assertEqual(body['records'],
                         [{'num': index + 1, 'data': value}
                          for index, value in enumerate(ARR_RECORDS[:2])])

    def test_two_requests_do_not_share_apdu_times(self):
        # The timing proxy is per request (the snapshot scan aggregates the
        # list); a shared instrument would append the second request's
        # exchanges to the first's list.
        state, _fake = open_file_state()
        self.track(state)
        harness = self.served(state)
        _, first = harness.json('POST', '/api/read', {'path': 'MF/2FE2'})
        _, second = harness.json('POST', '/api/read', {'path': 'MF/2F06'})
        self.assertEqual([entry['type'] for entry in first['apdu_times']],
                         ['select', 'select', 'read_binary'])
        self.assertEqual([entry['type'] for entry in second['apdu_times']],
                         ['select', 'select']
                         + ['read_record'] * len(ARR_RECORDS))

    def test_the_decoded_mode_is_refused_before_the_card(self):
        state, fake = open_file_state()
        self.track(state)
        sent = len(fake.sent)
        status, body = self.served(state).json(
            'POST', '/api/read', {'path': 'MF/2FE2', 'mode': 'decoded'})
        self.assertEqual(status, 400)
        self.assertIn('not supported', body['error'])
        self.assertIn('client-side', body['error'])
        self.assertEqual(len(fake.sent), sent,
                         'a refused request must not touch the card')

    def test_a_failed_read_select_is_a_404(self):
        state, _fake = open_file_state()
        self.track(state)
        status, body = self.served(state).json(
            'POST', '/api/read', {'path': 'MF/9F9F'})
        self.assertEqual(status, 404)
        self.assertFalse(body['success'])
        self.assertIn('9F9F', body['error'])

    def test_a_protocol_error_keeps_the_session(self):
        # A decoder/protocol failure must not tear a healthy card down (the
        # 3c-3a classification: only a transport error does).
        state, _fake = open_file_state()

        class Broken(FileCard):
            def _read_binary(self, apdu):
                raise IndexError('bad TLV')

        state.transport = Broken()
        state.auto_equip = lambda *args, **kwargs: self.fail(
            'a protocol error must not re-arm the auto-equip')
        self.track(state)
        status, body = self.served(state).json(
            'POST', '/api/read', {'path': 'MF/2FE2'})
        self.assertEqual(status, 500)
        self.assertIn('bad TLV', body['error'])
        self.assertTrue(state.connected)
        self.assertIsNotNone(state.cat)

    def test_a_transport_error_tears_the_link_down_and_rearms(self):
        state, _fake = open_file_state()
        calls = []
        state.auto_equip = lambda *args, **kwargs: calls.append('equip')

        class Unplugged(FileCard):
            def _read_binary(self, apdu):
                raise transport.ReaderError('PC/SC link is not connected')

        state.transport = Unplugged()
        self.track(state)
        status, body = self.served(state).json(
            'POST', '/api/read', {'path': 'MF/2FE2'})
        self.assertEqual(status, 503)
        self.assertIn('PC/SC link', body['error'])
        self.assertFalse(body['connected'])
        self.assertFalse(state.connected)
        self.assertTrue(state.card_present,
                        'the card is usually still inserted')
        self.assertEqual(calls, ['equip'])


class FileWriteTests(HTTPTestBase):
    def test_a_transparent_write(self):
        state, fake = open_file_state()
        self.track(state)
        arr_before = [dict(entry) for entry in state.arr]
        status, body = self.served(state).json('POST', '/api/write', {
            'path': 'MF/2FE2', 'data': '11' * 10})
        self.assertEqual(status, 200, body)
        self.assertEqual(body, {'success': True, 'sw': '9000'})
        self.assertIn('00D600000A' + '11' * 10,
                      [apdu.hex().upper() for apdu in fake.sent])
        # A non-ARR write leaves the ARR cache alone.
        self.assertEqual(state.arr, arr_before)

    def test_a_record_write_defaults_to_record_one(self):
        state, fake = open_file_state()
        self.track(state)
        status, body = self.served(state).json('POST', '/api/write', {
            'path': 'MF/2F06', 'data': 'DD' * 10})
        self.assertEqual(status, 200, body)
        self.assertTrue(body['success'])
        self.assertIn('00DC01040A' + 'DD' * 10,
                      [apdu.hex().upper() for apdu in fake.sent])

    def test_an_arr_write_refreshes_the_cache(self):
        state, _fake = open_file_state()
        self.track(state)
        self.assertEqual(state.arr[0]['records'][0], ARR_RECORDS[0])
        status, body = self.served(state).json('POST', '/api/write', {
            'path': 'MF/2F06', 'data': 'EE' * 10, 'record_nr': 3})
        self.assertEqual(status, 200, body)
        self.assertTrue(body['success'])
        self.assertEqual(state.arr[0]['records'][0], ARR_RECORDS[0])
        self.assertEqual(state.arr[0]['records'][2], 'EE' * 10,
                         'the EF.ARR session cache must be re-read')

    def test_a_record_write_with_an_explicit_number(self):
        state, fake = open_file_state()
        self.track(state)
        status, body = self.served(state).json('POST', '/api/write', {
            'path': 'MF/2F06', 'data': 'CC' * 10, 'record_nr': 2})
        self.assertEqual(status, 200, body)
        self.assertIn('00DC02040A' + 'CC' * 10,
                      [apdu.hex().upper() for apdu in fake.sent])

    def test_a_bad_payload_is_a_400_before_the_card(self):
        state, fake = open_file_state()
        self.track(state)
        sent = len(fake.sent)
        harness = self.served(state)
        for payload in ('ABC', '', 'ZZ'):
            status, body = harness.json('POST', '/api/write', {
                'path': 'MF/2FE2', 'data': payload})
            self.assertEqual(status, 400, payload)
            self.assertIn('hex', body['error'])
        self.assertEqual(len(fake.sent), sent,
                         'a refused request must not touch the card')

    def test_record_nr_on_a_transparent_file_is_a_400(self):
        state, fake = open_file_state()
        self.track(state)
        sent = len(fake.sent)
        status, body = self.served(state).json('POST', '/api/write', {
            'path': 'MF/2FE2', 'data': '11' * 10, 'record_nr': 1})
        self.assertEqual(status, 400)
        self.assertIn('record file', body['error'])
        # The file's shape is known only after the SELECT; what must never
        # happen is an UPDATE.
        self.assertGreaterEqual(len(fake.sent), sent)
        self.assertEqual(fake.writes, [])

    def test_a_refused_write_reports_the_sw_and_keeps_the_cache(self):
        state, _fake = open_file_state(
            handler=FileCard(refuse={'update_record': '6982'}))
        self.track(state)
        arr_before = [dict(entry) for entry in state.arr]
        status, body = self.served(state).json('POST', '/api/write', {
            'path': 'MF/2F06', 'data': 'DD' * 10})
        self.assertEqual(status, 200, body)
        self.assertFalse(body['success'])
        self.assertEqual(body['sw'], '6982')
        self.assertEqual(body['error'], 'Security status not satisfied')
        self.assertEqual(state.arr, arr_before)

    def test_a_cardless_server_is_a_503(self):
        state = self.track(make_state())
        status, body = self.served(state).json('POST', '/api/write', {
            'path': 'MF/2FE2', 'data': '11' * 10})
        self.assertEqual(status, 503)
        self.assertFalse(body['success'])
        self.assertIn('no card session', body['error'])


class FileCacheRouteTests(HTTPTestBase):
    """``GET /api/arr`` and ``GET /api/pbr``: the session caches, card-free."""

    def test_the_caches_are_served_with_the_session(self):
        state, _fake = open_file_state()
        self.track(state)
        harness = self.served(state)
        status, body = harness.json('GET', '/api/arr')
        self.assertEqual(status, 200)
        self.assertTrue(body['ok'])
        self.assertEqual(body['session'], state.card_session)
        self.assertEqual(body['arrs'][0]['path'], 'MF/2F06')
        status, body = harness.json('GET', '/api/pbr')
        self.assertEqual(status, 200)
        self.assertTrue(body['ok'])
        self.assertEqual(body['session'], state.card_session)
        self.assertEqual([entry['path'] for entry in body['pbrs']],
                         ['MF/7F10/5F3A/4F30', 'ADF.USIM/5F3A/4F30'])

    def test_they_answer_while_the_card_lock_is_held(self):
        state, _fake = open_file_state()
        self.track(state)
        harness = self.served(state)
        with state.card_lock:
            status, body = harness.json('GET', '/api/arr')
            self.assertEqual(status, 200)
            self.assertTrue(body['ok'])
            status, body = harness.json('GET', '/api/pbr')
            self.assertEqual(status, 200)
            self.assertTrue(body['ok'])

    def test_a_cardless_server_answers_empty(self):
        state = self.track(make_state())
        harness = self.served(state)
        for path, key in (('/api/arr', 'arrs'), ('/api/pbr', 'pbrs')):
            status, body = harness.json('GET', path)
            self.assertEqual(status, 200, path)
            self.assertFalse(body['ok'])
            self.assertEqual(body[key], [])

    def test_a_disconnect_clears_the_caches(self):
        state, _fake = open_file_state()
        self.track(state)
        harness = self.served(state)
        state.disconnect()
        status, body = harness.json('GET', '/api/arr')
        self.assertEqual(status, 200)
        self.assertFalse(body['ok'])
        self.assertEqual(body['arrs'], [])
        status, body = harness.json('GET', '/api/pbr')
        self.assertFalse(body['ok'])
        self.assertEqual(body['pbrs'], [])


PBR_VECTORS = json.loads(
    open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(
        __file__))), 'frontend', 'tests', 'pbr_vectors.json'),
        encoding='utf-8').read())

PBR_ENTRY = {'fid': '4F30', 'path': 'MF/7F10/5F3A/4F30', 'record_len': 10,
             'num_records': 1, 'records': ['A80AC0034F3A02C5034F0904']}


class PbrParserTests(unittest.TestCase):
    """The pure EF.PBR port (:mod:`simple_server.pbr`), pinned to the PWA."""

    def test_the_shared_vectors(self):
        self.assertTrue(PBR_VECTORS['cases'])
        for case in PBR_VECTORS['cases']:
            parsed = pbr.parse_pbr(case['records'])
            expected = []
            for link in case['links']:
                if not link.get('kind'):
                    continue        # a raw-preserved unknown tag is not a file
                if any(entry['fid'] == link['fid'] for entry in expected):
                    continue        # the parser deduplicates by FID (first wins)
                expected.append({'fid': link['fid'], 'kind': link['kind'],
                                 'link': link['link'], 'sfi': link['sfi']})
            self.assertEqual(parsed, expected, case['name'])
            self.assertEqual([entry['fid'] for entry in parsed],
                             case['files'], case['name'])

    def test_the_containers_the_sfi_forms_and_the_padding(self):
        # type 1 (A8) and type 2 (A9) containers; 'C0 02' carries just the FID,
        # 'C6 03' the FID + SFI (TS 31.102 §4.4.2.1).
        parsed = pbr.parse_pbr(['A808C0024F3AC1024F09'
                                'A90AC6034F0B06C7034F0C07'])
        self.assertEqual([entry['link'] for entry in parsed], [1, 1, 2, 2])
        self.assertEqual([entry['sfi'] for entry in parsed],
                         [None, None, '06', '07'])
        self.assertEqual([entry['kind'] for entry in parsed],
                         ['EF.ADN', 'EF.IAP', 'EF.GRP', 'EF.AAS'])
        # FF padding is not a link; a truncated TLV keeps the earlier ones.
        self.assertEqual(pbr.parse_pbr(['A80AC0034F3A02C5034F0904' + 'FF' * 8]),
                         pbr.parse_pbr(['A80AC0034F3A02C5034F0904']))
        self.assertEqual(
            [entry['fid'] for entry in pbr.parse_pbr(['A80AC0034F3A02C5034F09'])],
            [])
        self.assertEqual(pbr.parse_pbr(['A80AC0034F3A02C5034F0904C5'])[1]['fid'],
                         '4F09')

    def test_the_path_normaliser(self):
        self.assertEqual(pbr.norm_path(['3f00', '7f10', '5f3a', '4f30']),
                         ['MF', '7F10', '5F3A', '4F30'])
        self.assertEqual(pbr.norm_path(['mf', '']), ['MF'])
        self.assertEqual(pbr.norm_path(None), [])
        # An ADF is its own root: the PWA's whole-chain form folds onto it,
        # and an AID address folds onto the ADF's name.
        aid = cards.AID_USIM
        self.assertEqual(pbr.norm_path(['MF', 'ADF.USIM', '5F3A']),
                         ['ADF.USIM', '5F3A'])
        self.assertEqual(pbr.norm_path(['MF', aid, '5F3A'], {aid: 'ADF.USIM'}),
                         ['ADF.USIM', '5F3A'])

    def test_the_injection_rules(self):
        cache = [PBR_ENTRY]
        children = [{'name': 'EF.PBR', 'fid': '4F30', 'isDir': False}]
        self.assertEqual(
            pbr.inject_children(cache, ['MF', '7F10', '5F3A'], children), 2)
        self.assertEqual(
            children[1], {'name': 'EF.ADN', 'fid': '4F3A', 'isDir': False,
                          'pbr': True, 'sfi': '02'})
        # A known FID is not added twice; an unrelated DF is untouched.
        self.assertEqual(pbr.inject_children(
            cache, ['MF', '7F10', '5F3A'],
            [{'name': 'EF.ADN', 'fid': '4F3A', 'isDir': False}]), 1)
        self.assertEqual(pbr.inject_children(cache, ['MF', '7F10', '5F3B'], []),
                         0)
        self.assertEqual(pbr.inject_children(None, ['MF', '7F10', '5F3A'], []),
                         0)
        # A full path is never answered by another DF's copy...
        adf_only = [dict(PBR_ENTRY, path='ADF.USIM/5F3A/4F30')]
        self.assertEqual(
            pbr.inject_children(adf_only, ['MF', '7F10', '5F3A'], []), 0)
        self.assertEqual(
            pbr.inject_children(adf_only, ['ADF.USIM', '5F3A'], []), 2)
        # ...while a bare FID is answered only when a single copy is cached.
        self.assertEqual(pbr.inject_children(adf_only, ['5F3A'], []), 2)
        both = [PBR_ENTRY, dict(PBR_ENTRY, path='ADF.USIM/5F3A/4F30')]
        self.assertEqual(pbr.inject_children(both, ['5F3A'], []), 0)


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


# ---- the preset store and the plain SCP80 endpoints (Phase 3c-3c-1) --------
#
# The preset fixtures are synthetic (the repo rule: no card-specific keys).
# The 16-byte keys are a 3DES2 keyset (KIc/KID nibble 5, TS 102 225 A.2), which
# is what the role TARs' default MSL 0x16 (counter check + ciphering + CC)
# needs.

KIC_KEY = '00112233445566778899AABBCCDDEEFF'
KID_KEY = 'FFEEDDCCBBAA99887766554433221100'

ROLE_TARS = [{'role': role, 'tar': tar, 'msl': '16', 'keyset': 1}
             for role, tar in (('isd', '000000'), ('uiccRfm', 'B00000'),
                               ('usimRfm', 'B00001'))]

# A small vendor outfile (the ALFA layout: a var_out: column line, the ICCID as
# digits + the odd-nibble F pad, one KICn/KIDn pair per keyset).
OUTFILE_TEXT = '\n'.join([
    'var_out:IMSI ICCID KIC1 KID1 KIC2 KID2',
    '250197700000000 ' + ICCID_DIGITS + 'F ' + KIC_KEY + ' ' + KID_KEY +
    ' ' + KIC_KEY + ' ' + KID_KEY,
])


def preset_fields(**kwargs):
    """The request body of a complete synthetic preset (role TARs included)."""
    fields = {
        'name': 'Bench card',
        'iccid': ICCID_DIGITS,
        'keysets': [{'kic': '15', 'kid': '15', 'kicKey': KIC_KEY,
                     'kidKey': KID_KEY, 'cntr': '1'}],
        'tars': [dict(entry) for entry in ROLE_TARS],
    }
    fields.update(kwargs)
    return fields


def temp_preset_store(testcase):
    """A real :class:`PresetStore` in a throw-away directory."""
    root = tempfile.mkdtemp(prefix='simple-presets-')
    testcase.addCleanup(shutil.rmtree, root, True)
    return presets_store.PresetStore(path=pathlib.Path(root) /
                                     'card_presets.json')


def preset_with_store(testcase, **kwargs):
    store = temp_preset_store(testcase)
    return store, store.add(preset_fields(**kwargs))


class StoreSeamTests(unittest.TestCase):
    """The constructor seams the 3d entry point supplies (no HTTP here)."""

    def test_presets_path_builds_the_store_and_the_sms_addresses_are_kept(self):
        root = tempfile.mkdtemp(prefix='simple-presets-')
        self.addCleanup(shutil.rmtree, root, True)
        path = pathlib.Path(root) / 'card_presets.json'
        state = make_state(presets=None, presets_path=path)
        self.addCleanup(state.close)
        self.assertEqual(str(state.presets.path), str(path))
        self.assertFalse(path.exists())
        self.assertEqual((state.sms_oa, state.sms_sc),
                         ('12345', '12345678912'))
        custom = make_state(presets=None, presets_path=path,
                            sms_oa='99999', sms_sc='11121314')
        self.addCleanup(custom.close)
        self.assertEqual((custom.sms_oa, custom.sms_sc), ('99999', '11121314'))


def por_packet(tar='000000', cntr='0000000005', status=0x00, secured=''):
    """An SCP80 response packet (TS 102 225 §5.2 Table 3) with the RPI UDH.

    ``UDHL RPI | RPL RHL TAR CNTR PCNTR STS secured data``; RHL 10 means no
    RC/CC (the requests below use SPI2 ``01``: a PoR, no response signature).
    """
    body = (bytes.fromhex(tar + cntr) + bytes([0x00, status])
            + bytes.fromhex(secured))
    return (bytes.fromhex('027100') + len(body).to_bytes(2, 'big')
            + bytes([10]) + body)


def envelope_tpdu(apdu):
    """The ``8B`` SMS TPDU child of an ENVELOPE (SMS-PP download) APDU."""
    index = apdu.find(b'\x8b')
    offset = index + 1
    length = apdu[offset]
    if length & 0x80:                       # BER long form (a >127-byte TPDU)
        count = length & 0x7f
        length = int.from_bytes(apdu[offset + 1:offset + 1 + count], 'big')
        offset += 1 + count
    else:
        offset += 1
    return apdu[offset:offset + length]


class OtaCard(CardScript):
    """A UICC whose ENVELOPEs answer queued ``(data, sw)`` pairs.

    An answer may be a callable ``(apdu, count) -> (data, sw)`` (the counter
    probe's ladder needs the sent counter).  Every ENVELOPE is recorded.
    """

    def __init__(self, envelope_answers=None, **kwargs):
        super().__init__(**kwargs)
        self.envelope_answers = list(envelope_answers or [])
        self.envelopes = []

    def __call__(self, apdu):
        if apdu[:2] == b'\x80\xc2':
            self.envelopes.append(apdu)
            if self.envelope_answers:
                answer = self.envelope_answers.pop(0)
                return answer(apdu, len(self.envelopes)) \
                    if callable(answer) else answer
            return b'', b'\x6a\x82'
        return super().__call__(apdu)

    def open_state(self, testcase, store, **state_kwargs):
        """``ServerState`` with this card equipped and the store injected."""
        state = make_state(presets=store, reader='Reader A', **state_kwargs)
        state.open(transport=FakeTransport(handler=self), start_threads=False)
        testcase.track(state)
        return state


class PresetEndpointTests(HTTPTestBase):
    """The server-side preset store over HTTP (card-free)."""

    def setUp(self):
        super().setUp()
        self.store = temp_preset_store(self)

    def test_list_is_card_free_and_carries_the_path(self):
        state = self.track(make_state(presets=self.store))
        status, body = self.served(state).json('GET', '/api/presets')
        self.assertEqual(status, 200)
        self.assertEqual(body['presets'], [])
        self.assertEqual(body['path'], str(self.store.path))
        self.assertEqual(body['count'], 0)

    def test_presets_stay_answerable_while_the_card_lock_is_held(self):
        state = self.track(make_state(presets=self.store))
        harness = self.served(state)
        with state.card_lock:
            status, body = harness.json('GET', '/api/presets')
        self.assertEqual(status, 200)
        self.assertEqual(body['presets'], [])

    def test_create_update_delete_round_trip(self):
        state = self.track(make_state(presets=self.store))
        harness = self.served(state)
        status, body = harness.json('POST', '/api/presets', preset_fields())
        self.assertEqual(status, 200)
        self.assertTrue(body['ok'])
        preset = body['preset']
        self.assertTrue(preset['id'])
        self.assertEqual(preset['keysets'][0]['cntr'], '0000000001')
        status, listing = harness.json('GET', '/api/presets')
        self.assertEqual(len(listing['presets']), 1)
        status, updated = harness.json(
            'POST', '/api/presets/update',
            {'id': preset['id'], 'fields': {'name': 'Renamed'}})
        self.assertEqual(status, 200)
        self.assertEqual(updated['preset']['name'], 'Renamed')
        # a partial edit keeps the keysets and TARs
        self.assertEqual(updated['preset']['keysets'][0]['kic'], '15')
        self.assertEqual(len(updated['preset']['tars']), 3)
        status, removed = harness.json('POST', '/api/presets/delete',
                                       {'id': preset['id']})
        self.assertEqual(status, 200)
        self.assertTrue(removed['removed'])
        status, listing = harness.json('GET', '/api/presets')
        self.assertEqual(listing['presets'], [])

    def test_update_of_an_unknown_id_is_404(self):
        state = self.track(make_state(presets=self.store))
        status, body = self.served(state).json(
            'POST', '/api/presets/update', {'id': 'nope', 'fields': {}})
        self.assertEqual(status, 404)
        self.assertEqual(body['error'], 'unknown preset id')

    def test_duplicate_iccid_is_refused(self):
        state = self.track(make_state(presets=self.store))
        harness = self.served(state)
        status, _body = harness.json('POST', '/api/presets', preset_fields())
        self.assertEqual(status, 200)
        status, body = harness.json(
            'POST', '/api/presets',
            preset_fields(name='Second', iccid=ICCID_DIGITS))
        self.assertEqual(status, 400)
        self.assertIn('already exists', body['error'])
        # the spaced/raw forms normalise to the same card
        status, body = harness.json(
            'POST', '/api/presets',
            preset_fields(name='Third', iccid=ICCID_HEX))
        self.assertEqual(status, 400)
        self.assertIn('already exists', body['error'])

    def test_validation_errors_answer_400(self):
        state = self.track(make_state(presets=self.store))
        harness = self.served(state)
        cases = [
            ({'name': 'No keysets'}, 'at least one keyset'),
            (preset_fields(name=''), 'name is required'),
            (preset_fields(keysets=[{'kic': '15', 'kid': '25', 'kicKey': KIC_KEY,
                                     'kidKey': KID_KEY, 'cntr': '1'}]),
             'same keyset number'),
            (preset_fields(tars=[{'role': 'isd', 'tar': 'XYZ', 'msl': '16',
                                  'keyset': 1}]), 'TAR must be three hex bytes'),
        ]
        for fields, message in cases:
            with self.subTest(message=message):
                status, body = harness.json('POST', '/api/presets', fields)
                self.assertEqual(status, 400)
                self.assertIn(message, body['error'])

    def test_import_merge_and_replace(self):
        state = self.track(make_state(presets=self.store))
        harness = self.served(state)
        status, body = harness.json(
            'POST', '/api/presets/import',
            {'presets': [preset_fields()], 'mode': 'merge'})
        self.assertEqual(status, 200)
        self.assertEqual(body['added'], 1)
        # the same card again is a skipped duplicate, never fatal
        status, body = harness.json(
            'POST', '/api/presets/import',
            {'presets': [preset_fields()], 'mode': 'merge'})
        self.assertEqual(status, 200)
        self.assertEqual(body['added'], 0)
        self.assertEqual(body['skipped'], 1)
        # replace wipes the store first
        status, body = harness.json(
            'POST', '/api/presets/import',
            {'presets': [preset_fields(name='Replacement')], 'mode': 'replace'})
        self.assertEqual(status, 200)
        listing = state.presets.list()
        self.assertEqual([p['name'] for p in listing], ['Replacement'])

    def test_import_of_a_non_list_is_400(self):
        state = self.track(make_state(presets=self.store))
        status, body = self.served(state).json(
            'POST', '/api/presets/import', {'presets': 'nope'})
        self.assertEqual(status, 400)
        self.assertIn('at most 1000', body['error'])

    def test_outfile_preview_then_import(self):
        state = self.track(make_state(presets=self.store))
        harness = self.served(state)
        status, body = harness.json('POST', '/api/presets/import-outfile',
                                    {'text': OUTFILE_TEXT})
        self.assertEqual(status, 200)
        self.assertTrue(body['preview'])
        self.assertEqual(len(body['presets']), 1)
        self.assertEqual(body['presets'][0]['iccid'], ICCID_DIGITS)
        self.assertEqual(body['report']['cards'], 1)
        # the preview stored nothing
        self.assertEqual(state.presets.list(), [])
        status, body = harness.json(
            'POST', '/api/presets/import-outfile',
            {'text': OUTFILE_TEXT, 'mode': 'merge'})
        self.assertEqual(status, 200)
        self.assertFalse(body['preview'])
        self.assertEqual(body['added'], 1)
        stored = state.presets.list()
        self.assertEqual(stored[0]['iccid'], ICCID_DIGITS)
        # the inferred KIc/KID bytes carry the column number and the 3DES2
        # nibble (a 16-byte key, TS 102 225 A.2)
        self.assertEqual([ks['kic'] for ks in stored[0]['keysets']],
                         ['15', '25'])
        self.assertEqual(stored[0]['keysets'][0]['cntr'], '0000000001')

    def test_outfile_validation(self):
        state = self.track(make_state(presets=self.store))
        harness = self.served(state)
        cases = [
            ({}, 'text is required'),
            ({'text': OUTFILE_TEXT, 'algorithm16': 'des'},
             'algorithm16 must be one of'),
            ({'text': OUTFILE_TEXT, 'mode': 'upsert'},
             "mode must be 'merge', 'replace' or omitted"),
            ({'text': OUTFILE_TEXT, 'tar_keyset': 16},
             'tar_keyset must be 1-0F'),
            ({'text': OUTFILE_TEXT, 'tars': 'nope'},
             'tars must be a list'),
        ]
        for fields, message in cases:
            with self.subTest(message=message):
                status, body = harness.json('POST',
                                            '/api/presets/import-outfile',
                                            fields)
                self.assertEqual(status, 400)
                self.assertIn(message, body['error'])

    def test_an_invalid_outfile_is_never_partially_imported(self):
        state = self.track(make_state(presets=self.store))
        harness = self.served(state)
        status, body = harness.json('POST', '/api/presets/import-outfile',
                                    {'text': 'just a banner\n'})
        self.assertEqual(status, 200)
        self.assertTrue(body['preview'])
        self.assertEqual(body['presets'], [])
        self.assertIn('column-name line', body['report']['error'])
        status, body = harness.json('POST', '/api/presets/import-outfile',
                                    {'text': 'just a banner\n', 'mode': 'merge'})
        self.assertEqual(status, 400)
        self.assertIn('column-name line', body['error'])
        self.assertEqual(state.presets.list(), [])

    def test_the_request_log_redacts_the_keyset_keys(self):
        state = self.track(make_state(presets=self.store))
        harness = self.served(state, log_requests=True)
        with self.assertLogs('simple_server.server', level='INFO') as logs:
            status, _body = harness.json('POST', '/api/presets',
                                         preset_fields())
        self.assertEqual(status, 200)
        text = '\n'.join(logs.output)
        # the top-level `keysets` list carries the KIC/KID keys of a create
        self.assertIn('<redacted>', text)
        self.assertNotIn(KIC_KEY, text)
        self.assertNotIn(KID_KEY, text)

    def test_a_store_io_failure_is_a_json_500(self):
        preset = self.store.add(preset_fields())
        state = self.track(make_state(presets=self.store))
        harness = self.served(state)
        cases = [
            ('/api/presets',
             preset_fields(name='New', iccid='8970119000004600099')),
            ('/api/presets/update',
             {'id': preset['id'], 'fields': {'name': 'Renamed'}}),
            ('/api/presets/delete', {'id': preset['id']}),
            ('/api/presets/import',
             {'presets': [preset_fields(name='Imported',
                                        iccid='8970119000004600097')],
              'mode': 'merge'}),
        ]
        for path, body in cases:
            with mock.patch.object(self.store, '_save',
                                   side_effect=OSError('disk full')):
                status, answer = harness.json('POST', path, body)
            self.assertEqual(status, 500, path)
            self.assertIn('preset store write failed', answer['error'])
            self.assertFalse(answer['success'], path)
            self.assertFalse(answer['ok'], path)


class VerifyAdmTests(HTTPTestBase):
    """``POST /api/verify-adm``: the VERIFY and its structured result."""

    class AdmCard(CardScript):
        def __init__(self, sw):
            super().__init__()
            self.sw = sw
            self.verifies = []

        def __call__(self, apdu):
            if apdu[:2] == b'\x00\x20':
                self.verifies.append(apdu)
                return b'', bytes.fromhex(self.sw)
            return super().__call__(apdu)

    def adm_state(self, sw):
        card = self.AdmCard(sw)
        state = make_state(presets=temp_preset_store(self), reader='Reader A')
        state.open(transport=FakeTransport(handler=card), start_threads=False)
        self.track(state)
        return state, card

    def test_ok(self):
        state, card = self.adm_state('9000')
        harness = self.served(state)
        status, body = harness.json(
            'POST', '/api/verify-adm', {'adm': 'AABBCC'})
        self.assertEqual(status, 200)
        self.assertEqual(body, {'ok': True, 'sw': '9000'})
        # VERIFY: the card CLA, CHV 0x0A, 8 bytes, right-padded with 'f'
        self.assertEqual(card.verifies[0].hex().upper(),
                         '0020000A08AABBCCFFFFFFFFFF')
        # the ADM latch the 3d runner will read
        self.assertEqual(state.adm_session, state.card_session)
        # ... and the status badge the PWA's header reads
        status, status_body = harness.json('GET', '/api/status')
        self.assertEqual(status, 200)
        self.assertTrue(status_body['adm_verified'])

    def test_an_equip_clears_the_adm_verified_flag(self):
        state, _card = self.adm_state('9000')
        harness = self.served(state)
        status, _body = harness.json('POST', '/api/verify-adm',
                                     {'adm': 'AABBCC'})
        self.assertEqual(status, 200)
        status, status_body = harness.json('GET', '/api/status')
        self.assertTrue(status_body['adm_verified'])
        # A re-initialization is a new card session: the latch is per session
        # (the legacy's rs.adm_verified lived on the per-equip shell state).
        state.equip()
        self.assertEqual(state.card_session, 2)
        status, status_body = harness.json('GET', '/api/status')
        self.assertEqual(status, 200)
        self.assertTrue(status_body['connected'])
        self.assertFalse(status_body['adm_verified'])

    def test_chv_accepts_a_hex_string_or_an_integer_byte(self):
        state, card = self.adm_state('9000')
        harness = self.served(state)
        status, _body = harness.json('POST', '/api/verify-adm',
                                     {'adm': 'AABBCC', 'chv': '0A'})
        self.assertEqual(status, 200)
        status, _body = harness.json('POST', '/api/verify-adm',
                                     {'adm': 'AABBCC', 'chv': 10})
        self.assertEqual(status, 200)
        # 10 is the byte value 0x0A, not the hex-encoded string '10' (0x10)
        self.assertEqual([apdu[3] for apdu in card.verifies], [0x0A, 0x0A])
        status, body = harness.json('POST', '/api/verify-adm',
                                    {'adm': 'AABBCC', 'chv': 256})
        self.assertEqual(status, 400)
        self.assertIn('chv must be one hex byte', body['error'])


    def test_wrong_key_reports_the_attempts_left(self):
        state, _card = self.adm_state('63C2')
        status, body = self.served(state).json(
            'POST', '/api/verify-adm', {'adm': 'AABBCC'})
        self.assertEqual(status, 200)
        self.assertFalse(body['ok'])
        self.assertEqual(body['attempts_left'], 2)
        self.assertEqual(body['sw'], '63C2')

    def test_blocked(self):
        state, _card = self.adm_state('6983')
        status, body = self.served(state).json(
            'POST', '/api/verify-adm', {'adm': 'AABBCC'})
        self.assertEqual(status, 200)
        self.assertEqual(body, {'ok': False, 'sw': '6983', 'blocked': True})

    def test_the_matched_preset_supplies_the_key(self):
        store, preset = preset_with_store(self, adm='AABBCC')
        card = self.AdmCard('9000')
        state = make_state(presets=store, reader='Reader A')
        state.open(transport=FakeTransport(handler=card), start_threads=False)
        self.track(state)
        status, body = self.served(state).json(
            'POST', '/api/verify-adm', {'preset_id': preset['id']})
        self.assertEqual(status, 200)
        self.assertTrue(body['ok'])
        self.assertIn(bytes.fromhex('AABBCCFFFFFFFFFF'), card.verifies[0])

    def test_a_preset_without_a_key_and_a_malformed_key_are_400(self):
        store, preset = preset_with_store(self)
        card = self.AdmCard('9000')
        state = make_state(presets=store, reader='Reader A')
        state.open(transport=FakeTransport(handler=card), start_threads=False)
        self.track(state)
        harness = self.served(state)
        status, body = harness.json('POST', '/api/verify-adm',
                                    {'preset_id': preset['id']})
        self.assertEqual(status, 400)
        self.assertIn('no ADM key', body['error'])
        status, body = harness.json('POST', '/api/verify-adm',
                                    {'preset_id': 'nope'})
        self.assertEqual(status, 400)
        self.assertIn('unknown preset id', body['error'])
        status, body = harness.json('POST', '/api/verify-adm',
                                    {'adm': 'XYZ'})
        self.assertEqual(status, 400)
        self.assertIn('adm must be 4-16 hex digits', body['error'])
        self.assertEqual(card.verifies, [])

    def test_the_request_log_redacts_the_key(self):
        state, _card = self.adm_state('9000')
        harness = self.served(state, log_requests=True)
        with self.assertLogs('simple_server.server', level='INFO') as logs:
            status, _body = harness.json('POST', '/api/verify-adm',
                                         {'adm': 'AABBCCDD'})
        self.assertEqual(status, 200)
        text = '\n'.join(logs.output)
        self.assertIn('/api/verify-adm', text)
        self.assertIn('<redacted>', text)
        self.assertNotIn('AABBCCDD', text)

    def test_no_card_is_503(self):
        state = self.track(make_state(presets=temp_preset_store(self)))
        status, body = self.served(state).json(
            'POST', '/api/verify-adm', {'adm': 'AABBCC'})
        self.assertEqual(status, 503)
        self.assertIn('no card session', body['error'])


class SendOtaTests(HTTPTestBase):
    """``POST /api/send-ota``: the packet build, delivery, PoR and counter."""

    def setUp(self):
        super().setUp()
        self.store, self.preset = preset_with_store(self)

    def ota_state(self, answers):
        card = OtaCard(answers)
        state = card.open_state(self, self.store)
        return state, card

    def preset_id(self):
        return self.preset['id']

    def test_the_prebuilt_packet_path(self):
        state, card = self.ota_state([(por_packet(tar='B00000'), b'\x90\x00')])
        status, body = self.served(state).json('POST', '/api/send-ota', {
            'sp': '00151015000050B0000000000000000100',
            'preset_id': self.preset_id(), 'kic': '15', 'kid': '15',
            'tar': 'B00000', 'spi1': '16', 'spi2': '01',
            'cntr': '0000000005'})
        self.assertEqual(status, 200)
        self.assertTrue(body['success'])
        self.assertEqual(body['sw'], '9000')
        self.assertEqual(body['bytes'], 17)
        self.assertEqual(body['segments'], 1)
        self.assertEqual(body['cntr'], '0000000005')
        self.assertEqual(body['final_cntr'], '0000000006')
        self.assertEqual(body['por']['response_status'], 'por_ok')
        self.assertEqual(body['por']['tar'], 'B00000')
        self.assertEqual(body['por']['response_type'], 'none')
        # the accepted counter is persisted for that keyset
        self.assertEqual(self.store.get(self.preset_id())['keysets'][0]['cntr'],
                         '0000000006')
        # the pre-built packet was sent verbatim
        self.assertTrue(envelope_tpdu(card.envelopes[0]).endswith(
            bytes.fromhex('00151015000050B0000000000000000100')))

    def test_the_apdu_path_is_pinned_to_the_packet_builder(self):
        state, card = self.ota_state([(por_packet(), b'\x90\x00')])
        status, body = self.served(state).json('POST', '/api/send-ota', {
            'apdu': '00A40000023F00', 'preset_id': self.preset_id(),
            'spi2': '01'})
        self.assertEqual(status, 200)
        self.assertTrue(body['success'])
        # SPI1 defaults to the TAR's MSL (0x16), the keyset and counter come
        # from the preset's ISD entry
        expected, _spi = scp80.build_secured_packet(
            '16', '01', '15', '15', '000000', '0000000001', '00A40000023F00',
            KIC_KEY, KID_KEY)
        self.assertEqual(body['bytes'], len(expected))
        self.assertEqual(body['segments'], 1)
        self.assertEqual(body['cntr'], '0000000001')
        self.assertEqual(body['final_cntr'], '0000000002')
        self.assertIn(expected, envelope_tpdu(card.envelopes[0]))
        self.assertEqual(self.store.get(self.preset_id())['keysets'][0]['cntr'],
                         '0000000002')

    def test_the_counter_is_seeded_from_the_store(self):
        state, card = self.ota_state([(por_packet(), b'\x90\x00'),
                                      (por_packet(), b'\x90\x00')])
        harness = self.served(state)
        first = harness.json('POST', '/api/send-ota', {
            'apdu': '00A40000023F00', 'preset_id': self.preset_id(),
            'spi2': '01'})[1]
        self.assertEqual(first['cntr'], '0000000001')
        second = harness.json('POST', '/api/send-ota', {
            'apdu': '00A40000023F00', 'preset_id': self.preset_id(),
            'spi2': '01'})[1]
        self.assertEqual(second['cntr'], '0000000002')
        self.assertEqual(second['final_cntr'], '0000000003')
        # the second packet literally carries the seeded counter (SPI1 0x16
        # ciphers it, so compare against the built packet)
        seeded, _spi = scp80.build_secured_packet(
            '16', '01', '15', '15', '000000', '0000000002', '00A40000023F00',
            KIC_KEY, KID_KEY)
        self.assertIn(seeded, envelope_tpdu(card.envelopes[1]))

    def test_a_large_apdu_is_a_concatenated_download(self):
        state, card = self.ota_state([(por_packet(), b'\x90\x00'),
                                      (por_packet(), b'\x90\x00')])
        status, body = self.served(state).json('POST', '/api/send-ota', {
            'apdu': 'AA' * 200, 'preset_id': self.preset_id(),
            'spi2': '01'})
        self.assertEqual(status, 200)
        self.assertTrue(body['success'])
        self.assertEqual(body['segments'], 2)
        self.assertEqual(len(card.envelopes), 2)
        first = envelope_tpdu(card.envelopes[0])
        second = envelope_tpdu(card.envelopes[1])
        # TS 31.115 §4.3: the concatenation IE is in every SM (ref 01, total
        # 02) and the OTA CPI IE only in the first
        self.assertIn(bytes.fromhex('0003010201'), first)
        self.assertIn(bytes.fromhex('0003010202'), second)
        self.assertIn(bytes.fromhex('7000'), first)
        # the two parts are the splitter's own cut
        packet, _spi = scp80.build_secured_packet(
            '16', '01', '15', '15', '000000', '0000000001', 'AA' * 200,
            KIC_KEY, KID_KEY)
        parts = scp80.split_secured_packet(packet)
        self.assertIn(parts[0], first)
        self.assertIn(parts[1], second)

    def test_a_low_counter_verdict_is_reported(self):
        state, _card = self.ota_state(
            [(por_packet(status=0x02), b'\x62\x00')])
        status, body = self.served(state).json('POST', '/api/send-ota', {
            'sp': '00151015000050B0000000000000000100',
            'preset_id': self.preset_id(), 'kic': '15', 'kid': '15',
            'tar': 'B00000', 'spi1': '16', 'spi2': '01',
            'cntr': '0000000001'})
        self.assertEqual(status, 200)
        self.assertFalse(body['success'])
        self.assertTrue(body['cntr_low'])
        self.assertEqual(body['por']['response_status'], 'cntr_low')
        self.assertNotIn('final_cntr', body)
        # a rejected packet never moves the store counter
        self.assertEqual(self.store.get(self.preset_id())['keysets'][0]['cntr'],
                         '0000000001')

    def test_the_compact_por_form(self):
        state, _card = self.ota_state(
            [(por_packet(secured='019000'), b'\x90\x00')])
        body = self.served(state).json('POST', '/api/send-ota', {
            'apdu': '00A40000023F00', 'preset_id': self.preset_id(),
            'spi2': '01'})[1]
        self.assertEqual(body['por']['response_type'], 'compact')
        self.assertEqual(body['por']['decoded']['last_status_word'], '9000')
        self.assertEqual(body['por']['decoded']['number_of_commands'], 1)

    def test_the_scripting_por_form(self):
        state, _card = self.ota_state(
            [(por_packet(secured='AB0780010123029000'), b'\x90\x00')])
        body = self.served(state).json('POST', '/api/send-ota', {
            'apdu': '00A40000023F00', 'preset_id': self.preset_id(),
            'spi2': '01'})[1]
        self.assertEqual(body['por']['response_type'], 'scripting')
        self.assertEqual(body['por']['decoded']['last_status_word'], '9000')

    def test_a_non_rm_tar_reports_raw_data(self):
        # a TAR outside the preset is the receiving application's own target:
        # its response is never decoded as a remote-management response
        state, _card = self.ota_state(
            [(por_packet(tar='F0F0F0', secured='800110'), b'\x90\x00')])
        body = self.served(state).json('POST', '/api/send-ota', {
            'sp': '00151015000050F0F0F00000000000000100',
            'preset_id': self.preset_id(), 'kic': '15', 'kid': '15',
            'tar': 'F0F0F0', 'spi1': '16', 'spi2': '01',
            'cntr': '0000000005'})[1]
        self.assertEqual(body['por']['response_type'], 'raw')
        self.assertEqual(body['por']['decoded']['last_status_word'], '')
        self.assertEqual(body['por']['decoded']['last_response_data'],
                         '800110')

    def test_the_compact_command_count_guard(self):
        # a command count above the plaintext command length proves the data
        # is not a compact RM response (TS 102 226 §5.1.2 Table 5.1)
        state, _card = self.ota_state(
            [(por_packet(secured='800110'), b'\x90\x00')])
        body = self.served(state).json('POST', '/api/send-ota', {
            'apdu': '00A40000023F00', 'preset_id': self.preset_id(),
            'spi2': '01'})[1]
        self.assertEqual(body['por']['response_type'], 'raw')

    def test_a_requested_por_that_never_arrived_is_flagged(self):
        state, _card = self.ota_state([(b'', b'\x90\x00')])
        body = self.served(state).json('POST', '/api/send-ota', {
            'apdu': '00A40000023F00', 'preset_id': self.preset_id(),
            'spi2': '01'})[1]
        self.assertTrue(body['success'])
        self.assertTrue(body['por_missing'])
        self.assertTrue(body['final_cntr'])

    def test_missing_preset_keyset_errors(self):
        state, _card = self.ota_state([(por_packet(), b'\x90\x00')])
        harness = self.served(state)
        # a TAR the preset does not carry (an explicit SPI1 keeps the MSL
        # derivation from refusing first, so the TAR-table rule is reached)
        status, body = harness.json('POST', '/api/send-ota', {
            'apdu': '00A40000023F00', 'preset_id': self.preset_id(),
            'tar': 'F0F0F0', 'spi1': '16', 'spi2': '01'})
        self.assertEqual(status, 400)
        self.assertIn('not in the preset', body['error'])
        # an unknown preset leaves the packet without a keyset
        status, body = harness.json('POST', '/api/send-ota', {
            'apdu': '00A40000023F00', 'preset_id': 'nope',
            'tar': '000000', 'spi1': '16', 'spi2': '01'})
        self.assertEqual(status, 400)
        self.assertIn('needs KIc/KID', body['error'])
        # a KIc/KID number mismatch (TS 102 225 A.2)
        status, body = harness.json('POST', '/api/send-ota', {
            'sp': '00', 'preset_id': self.preset_id(), 'kic': '15',
            'kid': '25', 'tar': 'B00000', 'spi1': '16', 'spi2': '01'})
        self.assertEqual(status, 400)
        self.assertIn('same keyset number', body['error'])
        # a keyset number the preset does not define
        status, body = harness.json('POST', '/api/send-ota', {
            'sp': '00', 'preset_id': self.preset_id(), 'kic': '25',
            'kid': '25', 'tar': 'B00000', 'spi1': '16', 'spi2': '01'})
        self.assertEqual(status, 400)
        self.assertIn('not defined in preset', body['error'])
        self.assertEqual(len(_card.envelopes), 0)

    def test_an_unknown_tar_without_spi1_is_refused(self):
        state, _card = self.ota_state([(por_packet(), b'\x90\x00')])
        status, body = self.served(state).json('POST', '/api/send-ota', {
            'sp': '00', 'kic': '15', 'kid': '15', 'tar': 'F0F0F0',
            'spi2': '01'})
        self.assertEqual(status, 400)
        self.assertIn('no MSL for TAR F0F0F0', body['error'])

    def test_the_ram_format_rewrites_an_expanded_packet(self):
        state, card = self.ota_state([(por_packet(), b'\x90\x00')])
        body = self.served(state).json('POST', '/api/send-ota', {
            'apdu': '80F24000024F0000', 'preset_id': self.preset_id(),
            'spi2': '01', 'ram_format': 'expanded'})[1]
        self.assertTrue(body['success'])
        self.assertEqual(body['ram_format'], 'expanded')
        expected, _spi = scp80.build_secured_packet(
            '16', '01', '15', '15', '000000', '0000000001',
            ota.wrap_expanded_apdu('80F24000024F0000', 'definite'),
            KIC_KEY, KID_KEY)
        self.assertIn(expected, envelope_tpdu(card.envelopes[0]))

    def test_auto_format_reports_the_compact_form_it_sent(self):
        state, _card = self.ota_state([(por_packet(), b'\x90\x00')])
        body = self.served(state).json('POST', '/api/send-ota', {
            'apdu': '00A40000023F00', 'preset_id': self.preset_id(),
            'spi2': '01', 'ram_format': 'auto'})[1]
        self.assertEqual(body['ram_format'], 'compact')

    def test_no_card_is_503(self):
        state = self.track(make_state(presets=self.store))
        status, body = self.served(state).json('POST', '/api/send-ota', {
            'apdu': '00A40000023F00', 'preset_id': self.preset_id()})
        self.assertEqual(status, 503)
        self.assertIn('no card session', body['error'])

    def test_a_protocol_error_keeps_the_session(self):
        # a decoder/protocol failure must not lose a healthy card (3c-3a)
        state, _card = self.ota_state([])
        session = state.card_session

        def boom(sp_hex, **kwargs):
            raise IndexError('bad TLV walk')

        state._send_secured_packet = boom
        status, body = self.served(state).json('POST', '/api/send-ota', {
            'apdu': '00A40000023F00', 'preset_id': self.preset_id(),
            'spi2': '01'})
        self.assertEqual(status, 500)
        self.assertIn('bad TLV walk', body['error'])
        self.assertTrue(state.connected)
        self.assertEqual(state.card_session, session)

    def test_a_transport_error_tears_the_link_down(self):
        state, _card = self.ota_state([])
        session = state.card_session
        state.auto_equip = lambda *args, **kwargs: None

        def boom(sp_hex, **kwargs):
            raise transport.ReaderError('dead link')

        state._send_secured_packet = boom
        status, body = self.served(state).json('POST', '/api/send-ota', {
            'apdu': '00A40000023F00', 'preset_id': self.preset_id(),
            'spi2': '01'})
        self.assertEqual(status, 503)
        self.assertIn('dead link', body['error'])
        self.assertFalse(state.connected)
        self.assertIsNone(state.transport)
        self.assertTrue(state.card_present)      # the card is usually inserted
        self.assertEqual(state.card_session, session + 1)


class CounterProbeTests(HTTPTestBase):
    """``POST /api/counter-probe``: the bounded counter-sync ladder."""

    def setUp(self):
        super().setUp()
        self.store, self.preset = preset_with_store(self)

    def probe_state(self, answers):
        card = OtaCard(answers)
        return card.open_state(self, self.store), card

    def stored_counter(self):
        return self.store.get(self.preset['id'])['keysets'][0]['cntr']

    def test_the_ladder_stops_at_the_first_accepted(self):
        # start 1: the ladder is 2, 4, 8 - low, low, accepted
        state, _card = self.probe_state([
            (por_packet(status=0x02), b'\x90\x00'),
            (por_packet(status=0x02), b'\x90\x00'),
            (por_packet(cntr='0000000008'), b'\x90\x00')])
        status, body = self.served(state).json('POST', '/api/counter-probe',
                                                {'preset_id':
                                                 self.preset['id']})
        self.assertEqual(status, 200)
        self.assertTrue(body['success'])
        self.assertEqual(body['stopped'], 'accepted')
        self.assertEqual([a['cntr'] for a in body['attempts']],
                         ['0000000002', '0000000004', '0000000008'])
        self.assertEqual(body['stored_cntr'], '0000000009')
        self.assertEqual(self.stored_counter(), '0000000009')
        # SPI2 b1 (PoR required) is forced on the RAM listing default 21
        self.assertEqual(body['spi2'], '21')
        self.assertEqual(body['spi1'], '16')

    def test_the_ladder_only_advances_on_a_low_counter(self):
        state, _card = self.probe_state([
            (por_packet(status=0x01), b'\x90\x00')])
        status, body = self.served(state).json('POST', '/api/counter-probe',
                                                {'preset_id':
                                                 self.preset['id']})
        self.assertEqual(status, 200)
        self.assertFalse(body['success'])
        self.assertEqual(len(body['attempts']), 1)
        self.assertEqual(body['stopped'], 'error')
        self.assertNotIn('counter_saved', body)
        self.assertEqual(self.stored_counter(), '0000000001')

    def test_a_post_low_counter_security_error_still_saves(self):
        state, _card = self.probe_state([
            (por_packet(status=0x02), b'\x90\x00'),
            (por_packet(status=0x01), b'\x90\x00')])
        status, body = self.served(state).json('POST', '/api/counter-probe',
                                                {'preset_id':
                                                 self.preset['id']})
        self.assertEqual(status, 200)
        self.assertFalse(body['success'])
        self.assertTrue(body['counter_saved'])
        self.assertEqual(body['verdict'], 'rc_cc_ds_failed')
        self.assertEqual(body['stored_cntr'], '0000000005')
        self.assertIn('security settings', body['error'])
        self.assertEqual(self.stored_counter(), '0000000005')

    def test_no_por_is_an_error(self):
        state, _card = self.probe_state([(b'', b'\x90\x00')])
        status, body = self.served(state).json('POST', '/api/counter-probe',
                                                {'preset_id':
                                                 self.preset['id']})
        self.assertEqual(status, 200)
        self.assertFalse(body['success'])
        self.assertIn('no PoR', body['error'])
        self.assertEqual(self.stored_counter(), '0000000001')

    def test_the_attempt_budget_bounds_the_ladder(self):
        state, _card = self.probe_state([
            (por_packet(status=0x02), b'\x90\x00'),
            (por_packet(status=0x02), b'\x90\x00')])
        status, body = self.served(state).json('POST', '/api/counter-probe',
                                                {'preset_id':
                                                 self.preset['id'],
                                                 'max_attempts': 2})
        self.assertEqual(status, 200)
        self.assertFalse(body['success'])
        self.assertEqual(body['stopped'], 'attempts')
        self.assertEqual(len(body['attempts']), 2)
        self.assertIn('in 2 attempts', body['error'])

    def test_the_ceiling_bounds_the_ladder(self):
        # the first candidate (2) is above the ceiling 1
        state, _card = self.probe_state([
            (por_packet(status=0x02), b'\x90\x00')])
        status, body = self.served(state).json('POST', '/api/counter-probe',
                                                {'preset_id':
                                                 self.preset['id'],
                                                 'ceiling': '1'})
        self.assertEqual(status, 200)
        self.assertFalse(body['success'])
        self.assertEqual(body['attempts'], [])
        self.assertEqual(body['stopped'], 'ceiling')
        self.assertIn('ceiling', body['error'])

    def test_parameter_errors_are_400(self):
        state, card = self.probe_state([])
        harness = self.served(state)
        status, body = harness.json('POST', '/api/counter-probe', {})
        self.assertEqual(status, 400)
        self.assertIn('preset_id is required', body['error'])
        status, body = harness.json('POST', '/api/counter-probe',
                                    {'preset_id': 'nope'})
        self.assertEqual(status, 400)
        self.assertIn('unknown preset', body['error'])
        status, body = harness.json('POST', '/api/counter-probe',
                                    {'preset_id': self.preset['id'],
                                     'kvn': 5})
        self.assertEqual(status, 400)
        self.assertIn('not defined in the preset', body['error'])
        status, body = harness.json('POST', '/api/counter-probe',
                                    {'preset_id': self.preset['id'],
                                     'ceiling': '0'})
        self.assertEqual(status, 400)
        self.assertIn('ceiling is below the current counter', body['error'])
        self.assertEqual(card.envelopes, [])


if __name__ == '__main__':
    unittest.main()
