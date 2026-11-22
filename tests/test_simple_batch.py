# coding=utf-8
"""Tests for the CAP compatibility test, the TAR probe and the batch install.

Phase 3c-3c-2b: ``POST /api/cap-compat`` (the import-list verdict, the LOAD
block boundary, the diagnostic probe), ``POST /api/tar-probe`` (one secured
packet per preset TAR, the four verdicts, per-row MSL/keyset and the counter
persistence) and ``POST /api/ram-batch/start|stop|clear`` +
``GET /api/ram-batch/status`` (the three kinds, the plan built once, the
worker's card-session poll, the per-card preset match and attempt-once rule,
the failure recording and the HTTP lifecycle guards).

No card and no pysim: the fake transport, the SCP80 response packets and the
synthetic ``.cap`` follow ``tests/test_simple_ram.py`` (same fixtures, kept
local so each suite stands alone).  The endpoints run on a real
:class:`ThreadingHTTPServer` bound to an ephemeral loopback port.  Keys are the
public synthetic material the other suites use.

Spec anchors: JC VM Specification v2.2.2 §4.5.2 (the compatibility rule - the
major versions equal, the CAP's minor not above the resident one - which is the
question), §6.2/Table 6-3 (the component load order), §6.6 (the Import
component's `package_info` entries: minor first, the structure defined under
§6.3's `package` item) and §6.4 (the Directory's positional size array: index =
tag - 1, value = the component's `size` item); GP Card Spec v2.3.1
§11.6.2/Table 11-56 (LOAD P1.b8 = the last block), §11.2/Tables 11-20-11-23
(DELETE); TS 102 225 §5.1.1 (the SPI
bits) and A.1 (one counter per keyset); TS 102 226 §8.2.1.3.2.4 (the TAR's MSL
as the Minimum SPI1) and §5.1.2 Table 5.1 (the compact response).
"""

import io
import json
import pathlib
import shutil
import struct
import tempfile
import threading
import time
import unittest
import zipfile
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from simple_server.shared import presets as presets_store

from simple_server import capram
from simple_server import ota
from simple_server import server as server_module
from simple_server.server import (BatchBusyError, RAM_BATCH_ATTEMPTS_MAX,
                                  ServerState, build_http_server)

ATR = bytes.fromhex('3b9f95801fc78031e073fe211b67a3a0001505017204')
ICCID_DIGITS = '8970119000004600098'
ICCID_HEX = '980711090000640090f8'

# Synthetic keys (the repo rule: no card-specific material in tests).
KIC_KEY = '00112233445566778899AABBCCDDEEFF'
KID_KEY = 'FFEEDDCCBBAA99887766554433221100'

ROLE_TARS = [{'role': role, 'tar': tar, 'msl': '16', 'keyset': 1}
             for role, tar in (('isd', '000000'), ('uiccRfm', 'B00000'),
                               ('usimRfm', 'B00001'))]

# ---- the fake card ---------------------------------------------------------

SELECT_MF = bytes.fromhex('00A40004023F0000')
SELECT_ICCID = bytes.fromhex('00A40004022FE200')
STATUS_POLL = bytes.fromhex('80F2000C00')


class FakeTransport(object):
    """A scripted transport: ``handler(apdu)`` returns ``(data, sw)`` or None.

    An unhandled APDU answers ``6A82`` (file or application not found), which
    makes the best-effort startup reads fail cleanly.
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


class InitCard(object):
    """The APDU answers of the startup sequence (MF, ICCID, TP, STATUS)."""

    def __init__(self, iccid=ICCID_HEX):
        self.iccid = iccid

    def __call__(self, apdu):
        if apdu == SELECT_MF:
            return b'', b'\x90\x00'
        if apdu[:2] == b'\x80\x10':             # TERMINAL PROFILE
            return b'', b'\x90\x00'
        if apdu == STATUS_POLL:
            return b'', b'\x90\x00'
        if apdu == SELECT_ICCID:
            return b'', b'\x90\x00'
        if apdu[:2] == b'\x00\xb0':             # READ BINARY (EF.ICCID)
            return bytes.fromhex(self.iccid), b'\x90\x00'
        return None


class RamCard(InitCard):
    """A card whose ENVELOPEs answer a scripted response packet per index."""

    def __init__(self, answers=None, default=None, **kwargs):
        super().__init__(**kwargs)
        self.answers = dict(answers or {})
        self.default = answer() if default is None else default
        self.envelopes = []

    def __call__(self, apdu):
        if apdu[:2] == b'\x80\xc2':             # ENVELOPE (SMS-PP download)
            self.envelopes.append(apdu)
            index = len(self.envelopes)
            answer_value = self.answers.get(index, self.default)
            return answer_value if answer_value is not None else (b'', b'\x6a\x82')
        return super().__call__(apdu)

    def tpdu(self, index):
        """The ``8B`` SMS TPDU child of the ENVELOPE at 0-based ``index``."""
        apdu = self.envelopes[index]
        offset = apdu.find(b'\x8b') + 1
        length = apdu[offset]
        if length & 0x80:                       # BER long form
            count = length & 0x7f
            length = int.from_bytes(apdu[offset + 1:offset + 1 + count], 'big')
            offset += 1 + count
        else:
            offset += 1
        return apdu[offset:offset + length]


def por_packet(status=0x00, secured='', tar='000000', cntr='0000000005'):
    """An SCP80 response packet (TS 102 225 §5.2 Table 3) with the RPI UDH."""
    body = (bytes.fromhex(tar + cntr) + bytes([0x00, status])
            + bytes.fromhex(secured))
    return (bytes.fromhex('027100') + len(body).to_bytes(2, 'big')
            + bytes([10]) + body)


def answer(status=0x00, secured='019000'):
    """The ENVELOPE answer carrying a response packet (R-APDU SW 9000)."""
    return por_packet(status=status, secured=secured), b'\x90\x00'


def silent_answer():
    """A ``por_ok`` packet with no secured data: a registered, silent TAR."""
    return por_packet(status=0x00, secured=''), b'\x90\x00'


def no_por_answer():
    """An accepted ENVELOPE with no response packet at all."""
    return b'', b'\x90\x00'


# ---- the CAP fixture -------------------------------------------------------

PKG_AID = bytes.fromhex('A0000003080000100001')
MODULE_AID = bytes.fromhex('A000000308000010000101')
JC_FRAMEWORK = bytes.fromhex('A0000000620101')
UICC_TOOLKIT = bytes.fromhex('A0000000090005FFFFFFFF8912000000')


def component(tag, payload):
    """A CAP component file: tag + the payload length (not the 3-byte header)."""
    return bytes([tag]) + len(payload).to_bytes(2, 'big') + payload


def build_zip(parts):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        for name, data in parts:
            archive.writestr('pkg/%s.cap' % name, data)
    return buffer.getvalue()


def build_cap(method_size=8, header_flags=0x04):
    """A small synthetic ``.cap`` archive (ZIP bytes).

    The Import component (javacard.framework 1.2 and uicc.toolkit 1.11 - the
    platform/ETSI requirement) sits early in the load file; ``method_size``
    controls the Method component, i.e. how many LOAD blocks follow the
    Import boundary.
    """
    header = component(0x01, struct.pack('>I', 0xDECAFFED)
                       + bytes([1, 2, header_flags, 1, 2])
                       + bytes([len(PKG_AID)]) + PKG_AID)
    return build_zip([
        ('Header', header),
        ('Import', component(
            0x04, bytes([2])
            + bytes([2, 1, len(JC_FRAMEWORK)]) + JC_FRAMEWORK
            + bytes([11, 1, len(UICC_TOOLKIT)]) + UICC_TOOLKIT)),
        ('Applet', component(0x03, bytes([1, len(MODULE_AID)]) + MODULE_AID
                             + (1).to_bytes(2, 'big'))),
        ('Method', component(0x07, bytes([0]) + bytes([0x7A] * method_size))),
    ])


CAP = build_cap()
CAP_HEX = CAP.hex()
CAP_BIG = build_cap(method_size=700).hex()


# ---- the preset store and the HTTP harness ---------------------------------

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
    root = tempfile.mkdtemp(prefix='simple-batch-presets-')
    testcase.addCleanup(shutil.rmtree, root, True)
    return presets_store.PresetStore(path=pathlib.Path(root) /
                                     'card_presets.json')


def preset_with_store(testcase, **kwargs):
    store = temp_preset_store(testcase)
    return store, store.add(preset_fields(**kwargs))


#: A throw-away root for the test-script/-suite stores: a state must never
#: read or write the user's real ``~/.pysim-simple-server`` files.
_STORE_ROOT = pathlib.Path(tempfile.mkdtemp(prefix='simple-batch-stores-'))
_STORE_COUNTER = [0]


def _store_path(name):
    _STORE_COUNTER[0] += 1
    return _STORE_ROOT / ('%s-%d.json' % (name, _STORE_COUNTER[0]))


def make_state(**kwargs):
    """A cardless ServerState with the test seams set."""
    kwargs.setdefault('monitor', False)
    kwargs.setdefault('readers_fn', lambda: ['Reader A'])
    kwargs.setdefault('poll_interval', 30)
    kwargs.setdefault('test_scripts_path', _store_path('test_scripts'))
    kwargs.setdefault('test_suites_path', _store_path('test_suites'))
    return ServerState(**kwargs)


class BatchHarness(object):
    """A real ThreadingHTTPServer on an ephemeral loopback port."""

    def __init__(self, state):
        self.state = state
        self.server = build_http_server('127.0.0.1', 0, state)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={'poll_interval': 0.05},
            name='test-http-batch', daemon=True)
        self.thread.start()

    def request(self, method, path, body=None, timeout=5):
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body).encode('utf-8')
            headers['Content-Type'] = 'application/json'
        request = Request('http://127.0.0.1:%d%s' % (self.port, path),
                          data=data, method=method, headers=headers)
        try:
            with urlopen(request, timeout=timeout) as response:
                return response.status, response.read()
        except HTTPError as exc:
            try:
                return exc.code, exc.read()
            finally:
                exc.close()

    def json(self, method, path, body=None, timeout=5):
        status, raw = self.request(method, path, body, timeout)
        return status, json.loads(raw.decode('utf-8'))

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class BatchTestBase(unittest.TestCase):
    def setUp(self):
        self.harness = None
        self._states = []
        self.store, self.preset = preset_with_store(self)

    def tearDown(self):
        if self.harness is not None:
            self.harness.close()
        for state in self._states:
            state.close()
        self._states = []

    def served(self, state):
        # Replacing the harness must close the previous one (its shutdown()
        # joins a live server thread; a second call in one test would leak a
        # ThreadingHTTPServer, its thread and a socket).
        if self.harness is not None:
            self.harness.close()
        self.harness = BatchHarness(state)
        return self.harness

    def card_state(self, card, presets=None):
        state = make_state(presets=presets or self.store, reader='Reader A')
        state.open(transport=FakeTransport(handler=card), start_threads=False)
        self._states.append(state)
        return state

    def preset_id(self):
        return self.preset['id']


def batch_plan(kind='install-cap'):
    """A card-independent plan as the batch stores it (the .cap is mocked)."""
    return {'kind': kind,
            'seq': [{'name': 'INSTALL [for load]', 'apdu': '80E60200'},
                    {'name': 'LOAD (1/1)', 'apdu': '80E80000'},
                    {'name': 'INSTALL [for install]', 'apdu': '80E60000'}],
            'spi2': '01', 'include_cpi': True, 'ram_format': 'compact',
            'response': {'load_file_aid': PKG_AID.hex().upper(),
                         'module_aid': MODULE_AID.hex().upper()}}


# ═══════════════════════════════════════════════════════════════════
# /api/cap-compat
# ═══════════════════════════════════════════════════════════════════

class CapCompatTests(BatchTestBase):
    def compat_body(self, **kwargs):
        body = {'cap_hex': CAP_HEX, 'preset_id': self.preset_id(),
                'ram_format': 'compact'}
        body.update(kwargs)
        return body

    def test_the_basic_run_stops_at_the_import_boundary(self):
        card = RamCard()
        state = self.card_state(card)
        status, body = self.served(state).json('POST', '/api/cap-compat',
                                               self.compat_body())
        self.assertEqual(status, 200, body)
        self.assertTrue(body['success'])
        self.assertTrue(body['imports_ok'])
        self.assertEqual(body['boundary_block'], 1)
        self.assertEqual(body['total_blocks'], 1)
        self.assertEqual([step['name'] for step in body['steps']],
                         ['INSTALL [for load]', 'LOAD (1/1)'])
        for step in body['steps']:
            self.assertEqual(step['por_status'], 'por_ok')
            self.assertEqual(step['por_sw'], '9000')
        # no FINAL install: the request only asks about the import list
        self.assertNotIn('failed_step', body)
        self.assertEqual(body['probe_imports'], None)
        self.assertEqual(body['probe_unmatched'], [])
        self.assertEqual(body['load_file_aid'], PKG_AID.hex().upper())
        self.assertEqual(body['module_aid'], MODULE_AID.hex().upper())
        self.assertEqual(body['load_block_size'], 240)
        self.assertTrue(body['load_block_size_auto'])
        # each accepted packet consumed a counter, persisted per keyset
        self.assertEqual(body['final_cntr'], '0000000003')
        self.assertEqual(
            self.store.get(self.preset_id())['keysets'][0]['cntr'],
            '0000000003')
        # exactly two ENVELOPEs (no NV reads, no INSTALL [for install])
        self.assertEqual(len(card.envelopes), 2)

    def test_the_last_block_flag_is_cleared(self):
        """A CAP whose Import ends in the load file's last block would send a
        last-flagged LOAD (P1.b8) and complete the file.  The compatibility
        test clears the flag: the payload is identical, nothing is committed.
        """
        store, preset = preset_with_store(self, iccid='8970119000004600010')
        card = RamCard()
        state = make_state(presets=store, reader='Reader A')
        state.open(transport=FakeTransport(handler=card), start_threads=False)
        self._states.append(state)
        status, body = self.served(state).json('POST', '/api/cap-compat', {
            'cap_hex': CAP_HEX, 'preset_id': preset['id'],
            'ram_format': 'compact'})
        self.assertEqual(status, 200, body)
        self.assertTrue(body['imports_ok'])
        plan = capram.cap_compat_plan({
            'cap_hex': CAP_HEX, 'preset_id': preset['id'],
            'ram_format': 'compact'})
        # the plan equals the response's step list
        self.assertEqual([step['name'] for step in plan['seq']],
                         [step['name'] for step in body['steps']])
        # the LOAD block's APDU was de-flagged: 80 E8 P1 P2 ...
        load = plan['seq'][-1]['apdu']
        self.assertTrue(load.startswith('80E800'))
        self.assertEqual(int(load[4:6], 16) & 0x80, 0)

    def test_the_version_overrides_rewrite_the_import_list(self):
        store, preset = preset_with_store(self, iccid='8970119000004600010')
        card = RamCard()
        state = make_state(presets=store, reader='Reader A')
        state.open(transport=FakeTransport(handler=card), start_threads=False)
        self._states.append(state)
        status, body = self.served(state).json('POST', '/api/cap-compat', {
            'cap_hex': CAP_HEX, 'preset_id': preset['id'],
            'ram_format': 'compact', 'probe_imports': {'all': '0.0'}})
        self.assertEqual(status, 200, body)
        self.assertTrue(body['imports_ok'])
        applied = {entry['aid']: (entry['from'], entry['to'])
                   for entry in body['probe_imports']}
        self.assertEqual(applied[JC_FRAMEWORK.hex().upper()], ('1.2', '0.0'))
        self.assertEqual(applied[UICC_TOOLKIT.hex().upper()],
                         ('1.11', '0.0'))
        self.assertEqual(body['probe_unmatched'], [])

    def test_the_additions_are_appended_append_only(self):
        store, preset = preset_with_store(self, iccid='8970119000004600010')
        card = RamCard()
        state = make_state(presets=store, reader='Reader A')
        state.open(transport=FakeTransport(handler=card), start_threads=False)
        self._states.append(state)
        addition = {'aid': 'A0000000620201', 'version': '1.0'}
        status, body = self.served(state).json('POST', '/api/cap-compat', {
            'cap_hex': CAP_HEX, 'preset_id': preset['id'],
            'ram_format': 'compact', 'probe_additions': [addition]})
        self.assertEqual(status, 200, body)
        self.assertTrue(body['imports_ok'])
        self.assertEqual(body['probe_imports'], [
            {'aid': 'A0000000620201', 'from': None, 'to': '1.0'}])
        # a version override of an existing AID stays a rewrite, not an append
        status, body = self.served(state).json('POST', '/api/cap-compat', {
            'cap_hex': CAP_HEX, 'preset_id': preset['id'],
            'ram_format': 'compact',
            'probe_additions': [{'aid': JC_FRAMEWORK.hex(), 'version': '1.0'}]})
        self.assertEqual(status, 200, body)
        self.assertEqual(body['probe_imports'], [
            {'aid': JC_FRAMEWORK.hex().upper(), 'from': '1.2', 'to': '1.0'}])

    def test_the_unmatched_override_is_reported(self):
        card = RamCard()
        state = self.card_state(card)
        status, body = self.served(state).json('POST', '/api/cap-compat',
                                               self.compat_body(
                                                   probe_imports={
                                                       'all': '0.0',
                                                       'A00000009999': '1.0'}))
        self.assertEqual(status, 200, body)
        self.assertTrue(body['imports_ok'])
        self.assertEqual(body['probe_unmatched'],
                         [{'aid': 'A00000009999', 'version': '1.0'}])

    def test_a_probe_that_matches_nothing_is_a_400(self):
        card = RamCard()
        state = self.card_state(card)
        harness = self.served(state)
        status, body = harness.json('POST', '/api/cap-compat', self.compat_body(
            probe_imports={'A00000009999': '1.0'}))
        self.assertEqual(status, 400, body)
        self.assertIn('import probe: no import matched', body['error'])
        status, body = harness.json('POST', '/api/cap-compat', self.compat_body(
            probe_imports={'all': '9.x'}))
        self.assertEqual(status, 400, body)
        self.assertIn('import probe: invalid version', body['error'])
        self.assertEqual(card.envelopes, [])

    def test_a_missing_cap_is_a_400(self):
        card = RamCard()
        state = self.card_state(card)
        status, body = self.served(state).json(
            'POST', '/api/cap-compat', {'preset_id': self.preset_id()})
        self.assertEqual(status, 400)
        self.assertIn('No cap_hex provided', body['error'])
        self.assertEqual(card.envelopes, [])

    def test_the_install_for_load_refusal(self):
        # the first ENVELOPE is refused: the card rejects the load request
        card = RamCard(answers={1: None})
        state = self.card_state(card)
        status, body = self.served(state).json('POST', '/api/cap-compat',
                                               self.compat_body())
        self.assertEqual(status, 200, body)
        self.assertFalse(body['success'])
        self.assertFalse(body['imports_ok'])
        self.assertEqual(body['failed_step'], 1)
        self.assertEqual(body['steps'][0]['name'], 'INSTALL [for load]')
        self.assertEqual(body['steps'][0]['por_status'], 'envelope_error')
        self.assertEqual(body['error'], 'the card rejected the import list')
        self.assertEqual(len(card.envelopes), 1)

    def test_the_import_list_rejection(self):
        # the boundary LOAD answers a por_ok whose command failed 6A80: the
        # card refuses the import list exactly where the JCRE checks it
        card = RamCard(answers={2: answer(secured='016A80')})
        state = self.card_state(card)
        status, body = self.served(state).json('POST', '/api/cap-compat',
                                               self.compat_body())
        self.assertEqual(status, 200, body)
        self.assertFalse(body['imports_ok'])
        self.assertEqual(body['failed_step'], 2)
        self.assertEqual(body['steps'][-1]['name'], 'LOAD (1/1)')
        self.assertEqual(body['steps'][-1]['por_sw'], '6A80')
        self.assertIn('LOAD (1/1)', body['error'])
        self.assertIn('6A80', body['error'])

    def test_the_format_probe_failure(self):
        # every format probe is refused, then the compact INSTALL too: the
        # steps carry the FORMAT CHECK records the PWA's verdict keys on
        card = RamCard(answers={1: None, 2: None, 3: None, 4: None})
        state = self.card_state(card)
        status, body = self.served(state).json('POST', '/api/cap-compat',
                                               self.compat_body(
                                                   ram_format='auto'))
        self.assertEqual(status, 200, body)
        self.assertFalse(body['imports_ok'])
        self.assertEqual(body['ram_format'], 'compact')
        self.assertEqual([step['name'] for step in body['steps'][:3]], [
            'FORMAT CHECK (compact)', 'FORMAT CHECK (expanded)',
            'FORMAT CHECK (expanded-ae)'])
        for step in body['steps'][:3]:
            self.assertEqual(step['por_status'], 'envelope_error')
        self.assertEqual(body['steps'][-1]['name'], 'INSTALL [for load]')

    def test_the_block_boundary_and_the_block_size(self):
        # a larger Method: the Import ends in block 1 of 8 at 100-byte blocks
        card = RamCard()
        state = self.card_state(card)
        status, body = self.served(state).json('POST', '/api/cap-compat',
                                               self.compat_body(
                                                   cap_hex=CAP_BIG,
                                                   load_block_size=100))
        self.assertEqual(status, 200, body)
        self.assertTrue(body['imports_ok'])
        self.assertEqual(body['boundary_block'], 1)
        self.assertEqual(body['total_blocks'], 8)
        self.assertEqual([step['name'] for step in body['steps']],
                         ['INSTALL [for load]', 'LOAD (1/8)'])
        self.assertEqual(body['load_block_size'], 100)
        self.assertEqual(body['load_block_size_requested'], 100)
        self.assertFalse(body['load_block_size_auto'])
        self.assertEqual(len(card.envelopes), 2)

    def test_an_invalid_block_size_is_a_400(self):
        card = RamCard()
        state = self.card_state(card)
        harness = self.served(state)
        for value in (0, 241, 'x'):
            status, body = harness.json('POST', '/api/cap-compat',
                                        self.compat_body(
                                            load_block_size=value))
            self.assertEqual(status, 400, value)
            self.assertIn('load_block_size', body['error'])
        self.assertEqual(card.envelopes, [])

    def test_no_card_is_503(self):
        state = make_state(presets=self.store)
        self._states.append(state)
        status, body = self.served(state).json('POST', '/api/cap-compat',
                                               self.compat_body())
        self.assertEqual(status, 503)
        self.assertIn('no card session', body['error'])


class CapCompatPlanTests(unittest.TestCase):
    """The pure half of the compatibility test (``simple_server.capram``)."""

    def test_the_import_boundary(self):
        load = capram.cap_parse(CAP_HEX)[2]
        self.assertEqual(capram.cap_import_end(load), 56)
        self.assertEqual(capram.cap_import_aids(load),
                         [JC_FRAMEWORK.hex().upper(),
                          UICC_TOOLKIT.hex().upper()])
        self.assertEqual(capram.cap_compat_blocks(load, 240), (1, 1))
        self.assertEqual(capram.cap_compat_blocks(load, 10), (6, 9))

    def test_the_plan_truncates_the_chain(self):
        plan = capram.cap_compat_plan({'cap_hex': CAP_BIG,
                                       'load_block_size': 100})
        self.assertEqual(plan['kind'], 'cap-compat')
        self.assertEqual(len(plan['seq']), 2)
        self.assertEqual(plan['response']['boundary_block'], 1)
        self.assertEqual(plan['response']['total_blocks'], 8)
        # no NV reads (the compat test only asks the import question)
        self.assertNotIn('nv', plan)

    def test_the_version_pair_is_minor_major(self):
        self.assertEqual(capram.version_pair('1.11'), (11, 1))
        self.assertEqual(capram.version_pair('0.0'), (0, 0))
        for bad in ('1', '1.2.3', '255.256', 'x.y', None):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    capram.version_pair(bad)

    def test_the_unmatched_report(self):
        known = {'A0000000620101', 'A0000000090005FFFFFFFF8912000000'}
        self.assertEqual(capram.probe_unmatched(
            {'all': '0.0', 'A00000009999': '1.0'}, known),
            [{'aid': 'A00000009999', 'version': '1.0'}])
        self.assertEqual(capram.probe_unmatched({'a0000000620101': '0.0'},
                                                known), [])


# ═══════════════════════════════════════════════════════════════════
# /api/tar-probe
# ═══════════════════════════════════════════════════════════════════

class TarProbeTests(BatchTestBase):
    def probe_body(self, preset, **kwargs):
        body = {'preset_id': preset['id'], 'spi1': '16'}
        body.update(kwargs)
        return body

    def keyset(self, kic, kid, kic_key, kid_key, cntr):
        return {'kic': kic, 'kid': kid, 'kicKey': kic_key, 'kidKey': kid_key,
                'cntr': cntr}

    def multi_preset(self):
        """Two keysets, four TARs: the row's keyset is the TAR's default."""
        store, preset = preset_with_store(
            self, iccid='8970119000004600010',
            keysets=[self.keyset('15', '15', KIC_KEY, KID_KEY, '1'),
                     self.keyset('25', '25', KID_KEY, KIC_KEY,
                                 '0000000010')],
            tars=[{'role': 'isd', 'tar': '000000', 'msl': '16', 'keyset': 1},
                  {'role': 'uiccRfm', 'tar': 'B00000', 'msl': '16',
                   'keyset': 1},
                  {'role': 'usimRfm', 'tar': 'B00001', 'msl': '18',
                   'keyset': 1},
                  {'tar': 'B00200', 'msl': '16', 'keyset': 2,
                   'desc': 'Vendor'}])
        return store, preset

    def test_the_four_verdicts(self):
        store, preset = self.multi_preset()
        # envelope 1: a real answer; 2: no packet; 3: refused; 4: silent
        card = RamCard(answers={1: answer(), 2: no_por_answer(),
                                3: None, 4: silent_answer()})
        state = make_state(presets=store, reader='Reader A')
        state.open(transport=FakeTransport(handler=card), start_threads=False)
        self._states.append(state)
        status, body = self.served(state).json('POST', '/api/tar-probe',
                                               self.probe_body(preset))
        self.assertEqual(status, 200, body)
        self.assertTrue(body['success'])
        verdicts = [row['verdict'] for row in body['results']]
        self.assertEqual(verdicts, ['registered', 'no_por', 'refused',
                                    'no_answer'])
        self.assertEqual(body['registered'], 1)
        self.assertEqual(body['total'], 4)
        self.assertEqual([row['tar'] for row in body['results']],
                         ['000000', 'B00000', 'B00001', 'B00200'])
        self.assertEqual([row['label'] for row in body['results']],
                         ['ISD', 'UICC RFM', 'ADF RFM', 'Vendor'])
        self.assertEqual(body['apdu'], '00A40000023F00')

    def test_a_non_rm_tar_keeps_its_answer_raw(self):
        """The response form follows the packet's TAR (TS 102 226 §4).

        A free TAR is not a remote-management target, so the secured data is
        the receiving application's own: `01 00 0B 91` must be reported raw
        (no status word), never decoded as a compact response with SW `000B`.
        The legacy decoded per packet (`_ram_send_gp_apdu`'s
        `_tar_is_rm(..., sp['tar'])`), and the PWA's probe table has an
        "App SW" column that must stay empty here.
        """
        store, preset = self.multi_preset()
        card = RamCard(answers={1: answer(secured='01000b91')})
        state = make_state(presets=store, reader='Reader A')
        state.open(transport=FakeTransport(handler=card), start_threads=False)
        self._states.append(state)
        status, body = self.served(state).json(
            'POST', '/api/tar-probe',
            self.probe_body(preset, tars=['B00200']))
        self.assertEqual(status, 200, body)
        row = body['results'][0]
        self.assertEqual(row['tar'], 'B00200')
        self.assertEqual(row['por_status'], 'por_ok')
        self.assertFalse(row['por_sw'])            # nothing fabricated
        self.assertEqual(row['por_data'], '01000B91')
        self.assertEqual(body['steps'][0]['por_type'], 'raw')
        self.assertEqual(row['verdict'], 'registered')

    def test_a_bad_probe_command_or_spi2_is_a_400(self):
        """A malformed probe APDU must not become a `refused` verdict for
        every TAR (an operator typo would read as "no applications")."""
        card = RamCard()
        state = self.card_state(card)
        harness = self.served(state)
        for apdu in ('0', '0G04', '00' * 256):
            status, body = harness.json(
                'POST', '/api/tar-probe',
                self.probe_body(self.preset, apdu=apdu))
            self.assertEqual(status, 400, apdu[:10])
            self.assertIn('apdu', body['error'])
        for spi2 in ('1', 'GG', '0102'):
            status, body = harness.json(
                'POST', '/api/tar-probe',
                self.probe_body(self.preset, spi2=spi2))
            self.assertEqual(status, 400, spi2)
            self.assertIn('spi2', body['error'])
        self.assertEqual(card.envelopes, [])

    def test_the_per_row_msl_and_keyset(self):
        store, preset = self.multi_preset()
        card = RamCard()
        state = make_state(presets=store, reader='Reader A')
        state.open(transport=FakeTransport(handler=card), start_threads=False)
        self._states.append(state)
        status, body = self.served(state).json('POST', '/api/tar-probe',
                                               self.probe_body(preset))
        self.assertEqual(status, 200, body)
        # each row's SPI1 is its TAR's MSL, not the request's fallback
        self.assertEqual([row['spi1'] for row in body['results']],
                         ['16', '16', '18', '16'])
        self.assertEqual([row['kvn'] for row in body['results']], [1, 1, 1, 2])

    def test_the_counters_advance_and_persist_per_keyset(self):
        store, preset = self.multi_preset()
        card = RamCard()
        state = make_state(presets=store, reader='Reader A')
        state.open(transport=FakeTransport(handler=card), start_threads=False)
        self._states.append(state)
        status, body = self.served(state).json('POST', '/api/tar-probe',
                                               self.probe_body(preset))
        self.assertEqual(status, 200, body)
        stored = store.get(preset['id'])
        counters = {presets_store.keyset_kvn(ks): ks['cntr']
                    for ks in stored['keysets']}
        # all four packets were accepted: three on keyset 1 (1 -> 4), one on
        # keyset 2 (0x10 -> 0x11)
        self.assertEqual(counters, {1: '0000000004', 2: '0000000011'})
        self.assertEqual(body['final_cntr'], '0000000011')

    def test_only_the_checked_tars_are_sent(self):
        card = RamCard()
        state = self.card_state(card)
        status, body = self.served(state).json(
            'POST', '/api/tar-probe',
            self.probe_body(self.preset, tars=['000000', 'B00001']))
        self.assertEqual(status, 200, body)
        self.assertEqual([row['tar'] for row in body['results']],
                         ['000000', 'B00001'])
        self.assertEqual(len(card.envelopes), 2)

    def test_the_preset_tar_list_is_the_only_source(self):
        card = RamCard()
        state = self.card_state(card)
        harness = self.served(state)
        status, body = harness.json('POST', '/api/tar-probe', {
            'preset_id': self.preset_id(), 'spi1': '16',
            'tars': ['B0FFFF']})
        self.assertEqual(status, 400, body)
        self.assertIn('not in the preset', body['error'])
        # a preset is required - the probe checks its TAR list
        status, body = harness.json('POST', '/api/tar-probe', {'spi1': '16'})
        self.assertEqual(status, 400, body)
        self.assertIn('preset_id', body['error'])
        status, body = harness.json('POST', '/api/tar-probe',
                                    {'preset_id': 'nope', 'spi1': '16'})
        self.assertEqual(status, 400, body)
        self.assertIn('unknown preset', body['error'])
        self.assertEqual(card.envelopes, [])

    def raw_store(self, presets):
        """A store loaded from a hand-written file.

        The store's ``add`` refuses a TAR naming an undefined keyset or a
        security MSL without one, so those states only reach the server from a
        hand-edited file (which the store keeps verbatim on load).
        """
        store = temp_preset_store(self)
        store.path.write_text(
            json.dumps({'version': presets_store.SCHEMA_VERSION,
                        'presets': presets}), encoding='utf-8')
        return presets_store.PresetStore(path=store.path)

    def test_a_tar_without_a_counter_check_or_keyset_is_refused(self):
        card = RamCard()
        store = self.raw_store([
            # an MSL 00 TAR has no counter check: nothing would be tracked
            preset_fields(iccid='8970119000004600020',
                          tars=[{'role': 'isd', 'tar': '000000',
                                 'msl': '00'}]),
            # a security MSL without a default keyset refuses the whole probe
            preset_fields(
                iccid='8970119000004600021',
                keysets=[self.keyset('15', '15', KIC_KEY, KID_KEY, '1'),
                         self.keyset('25', '25', KID_KEY, KIC_KEY, '2')],
                tars=[{'role': 'isd', 'tar': '000000', 'msl': '16'}]),
        ])
        no_counter, no_keyset = store.list()
        state = self.card_state(card, presets=store)
        harness = self.served(state)
        status, body = harness.json('POST', '/api/tar-probe',
                                    {'preset_id': no_counter['id']})
        self.assertEqual(status, 400, body)
        self.assertIn('counter check', body['error'])
        status, body = harness.json('POST', '/api/tar-probe',
                                    {'preset_id': no_keyset['id']})
        self.assertEqual(status, 400, body)
        self.assertIn('needs a default keyset', body['error'])
        self.assertEqual(card.envelopes, [])

    def test_an_undefined_keyset_is_refused(self):
        store = self.raw_store([preset_fields(
            iccid='8970119000004600022',
            keysets=[self.keyset('15', '15', KIC_KEY, KID_KEY, '1'),
                     self.keyset('25', '25', KID_KEY, KIC_KEY, '2')],
            tars=[{'role': 'isd', 'tar': '000000', 'msl': '16',
                   'keyset': 5}])])
        preset = store.list()[0]
        card = RamCard()
        state = self.card_state(card, presets=store)
        status, body = self.served(state).json(
            'POST', '/api/tar-probe', {'preset_id': preset['id']})
        self.assertEqual(status, 400, body)
        self.assertIn('not defined in the preset', body['error'])
        self.assertEqual(card.envelopes, [])


# ═══════════════════════════════════════════════════════════════════
# The batch install: the state/record helpers
# ═══════════════════════════════════════════════════════════════════

class BatchStateTests(BatchTestBase):
    def batch_state(self, kind='install-cap'):
        state = make_state(presets=self.store)
        self._states.append(state)
        state.ram_batch = state._ram_batch_new_state(batch_plan(kind))
        return state

    def test_the_report_counts_and_caps_the_attempts(self):
        state = self.batch_state()
        state._ram_batch_record({'iccid': ICCID_DIGITS, 'result': 'installed'})
        state._ram_batch_record({'iccid': '8970119000004600106',
                                 'result': 'failed',
                                 'failed_step': 'LOAD (3/7)',
                                 'por_sw': '6A80'})
        state._ram_batch_record({'iccid': '8970119000004600114',
                                 'result': 'skipped',
                                 'reason': 'no preset matches this ICCID'})
        run = state.ram_batch
        self.assertEqual(run['counts'], {'installed': 1, 'already_absent': 0,
                                         'failed': 1, 'skipped': 1})
        self.assertEqual(run['attempts_total'], 3)
        # the notice names the last attempt's outcome
        self.assertIn('skipped: no preset matches this ICCID', run['notice'])
        state._ram_batch_record({'iccid': '8970119000004600122',
                                 'result': 'failed',
                                 'failed_step': 'LOAD (3/7)',
                                 'por_sw': '6A80'})
        self.assertIn('failed at LOAD (3/7) (6A80)', run['notice'])
        for _ in range(RAM_BATCH_ATTEMPTS_MAX + 10):
            state._ram_batch_record({'result': 'skipped', 'reason': 'x'})
        self.assertEqual(len(run['attempts']), RAM_BATCH_ATTEMPTS_MAX)
        self.assertEqual(run['attempts_total'],
                         4 + RAM_BATCH_ATTEMPTS_MAX + 10)

    def test_the_failure_fields_name_the_failing_step(self):
        result = {'failed_step': 2, 'steps': [
            {'name': 'INSTALL [for load]', 'sw': '9000',
             'por_status': 'por_ok'},
            {'name': 'LOAD (1/2)', 'sw': '9000', 'por_status': 'por_error',
             'por_sw': '6A80', 'por_error': 'remote SW 6A80'},
        ]}
        fields = ServerState._ram_batch_failure_fields(result)
        self.assertEqual(fields['failed_step'], 'LOAD (1/2)')
        self.assertEqual(fields['por_sw'], '6A80')
        self.assertEqual(fields['por_error'], 'remote SW 6A80')
        self.assertEqual(ServerState._ram_batch_failure_fields({})[
            'failed_step'], '')

    def test_the_steps_fields_carry_the_tail(self):
        result = {'steps': [{'name': 'INSTALL [for load]', 'sw': '9000'},
                            {'name': 'LOAD (1/1)', 'por_sw': '9000'},
                            {'name': 'INSTALL [for install]',
                             'por_sw': '6101'},
                            {'name': 'DELETE (application)', 'por_sw': '6A88',
                             'delete_not_found': True,
                             'por_error': 'remote SW 6A88'}]}
        fields = ServerState._ram_batch_steps_fields(result)
        self.assertEqual(fields['steps_total'], 4)
        self.assertEqual([x['name'] for x in fields['steps']],
                         ['LOAD (1/1)', 'INSTALL [for install]',
                          'DELETE (application)'])
        self.assertTrue(fields['steps'][-1]['not_found'])
        self.assertTrue(fields['steps'][-1]['ok'])

    def test_a_transport_refused_step_is_not_ok(self):
        """An `envelope_error` step has no `por_error` (the card never saw the
        command), so the panel must not mark it ok."""
        fields = ServerState._ram_batch_steps_fields(
            {'steps': [{'name': 'INSTALL [for load]', 'sw': '6200',
                        'por_status': 'envelope_error'}]})
        self.assertFalse(fields['steps'][0]['ok'])
        # a step with no PoR at all still proceeded (the legacy counts it ok)
        fields = ServerState._ram_batch_steps_fields(
            {'steps': [{'name': 'LOAD (1/1)', 'sw': '9000',
                        'por_status': 'no_por'}]})
        self.assertTrue(fields['steps'][0]['ok'])

    def test_a_skip_without_an_iccid_reads_sensibly(self):
        """A card with no readable ICCID has no identifier to print, so the
        notice must not read "Card the card skipped: ..."."""
        state = self.batch_state()
        state._ram_batch_record({'result': 'skipped',
                                 'reason': 'no ICCID readable'})
        self.assertEqual(state.ram_batch['notice'],
                         'The card was skipped: no ICCID readable.')
        state._ram_batch_record({'iccid': ICCID_DIGITS,
                                 'result': 'skipped',
                                 'reason': 'no preset matches this ICCID'})
        self.assertEqual(
            state.ram_batch['notice'],
            'Card %s was skipped: no preset matches this ICCID.'
            % ICCID_DIGITS)

    def test_the_notice_verb_follows_the_kind(self):
        for kind, verb in (('delete', 'deleted'),
                           ('reinstall-app', 'reinstalled'),
                           ('install-cap', 'installed')):
            state = self.batch_state(kind)
            state._ram_batch_record({'iccid': ICCID_DIGITS,
                                     'result': 'installed'})
            self.assertIn(verb, state.ram_batch['notice'], kind)


# ═══════════════════════════════════════════════════════════════════
# The batch install: the per-card decision
# ═══════════════════════════════════════════════════════════════════

class BatchCardTests(BatchTestBase):
    def batch_state(self, kind='install-cap', iccid=ICCID_DIGITS,
                    presets=None):
        state = make_state(presets=presets or self.store)
        self._states.append(state)
        state.ram_batch = state._ram_batch_new_state(batch_plan(kind))
        state.iccid = iccid
        return state

    def test_a_card_without_a_preset_is_skipped(self):
        state = self.batch_state(iccid='8970119000004600999')
        state._ram_batch_handle_card(state.ram_batch)
        attempt = state.ram_batch['attempts'][0]
        self.assertEqual(attempt['result'], 'skipped')
        self.assertIn('no preset', attempt['reason'])
        self.assertEqual(state.ram_batch['seen']['8970119000004600999'],
                         'no preset')

    def test_a_card_without_a_readable_iccid_is_skipped(self):
        state = self.batch_state(iccid=None)
        state._ram_batch_handle_card(state.ram_batch)
        self.assertIn('no ICCID', state.ram_batch['attempts'][0]['reason'])

    def test_the_preset_is_matched_by_iccid_and_the_install_runs(self):
        state = self.batch_state()
        calls = []

        def fake_run(body, plan):
            calls.append(body)
            return {'success': True, 'final_cntr': '0000000002',
                    'load_file_aid': PKG_AID.hex().upper(),
                    'module_aid': MODULE_AID.hex().upper()}

        with mock.patch.object(state, '_ram_plan_run', fake_run):
            state._ram_batch_handle_card(state.ram_batch)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['preset_id'], self.preset_id())
        self.assertEqual(calls[0]['tar'], '000000')       # the ISD role TAR
        attempt = state.ram_batch['attempts'][0]
        self.assertEqual(attempt['result'], 'installed')
        self.assertEqual(attempt['preset'], 'Bench card')
        self.assertEqual(state.ram_batch['counts']['installed'], 1)

    def test_a_failed_install_is_recorded_with_the_step_and_sw(self):
        state = self.batch_state()

        def fake_run(body, plan):
            return {'success': False, 'error': 'remote SW 6A80',
                    'failed_step': 2,
                    'steps': [{'name': 'INSTALL [for load]', 'sw': '9000'},
                              {'name': 'LOAD (1/1)', 'sw': '9000',
                               'por_sw': '6A80'}],
                    'final_cntr': '0000000002'}

        with mock.patch.object(state, '_ram_plan_run', fake_run):
            state._ram_batch_handle_card(state.ram_batch)
        attempt = state.ram_batch['attempts'][0]
        self.assertEqual(attempt['result'], 'failed')
        self.assertEqual(attempt['failed_step'], 'LOAD (1/1)')
        self.assertEqual(attempt['por_sw'], '6A80')
        self.assertEqual(state.ram_batch['counts']['failed'], 1)

    def test_a_request_error_is_a_skip_and_a_transport_error_a_failure(self):
        state = self.batch_state()

        def raise_value(body, plan):
            raise ValueError('TAR 000000 needs a default keyset (its MSL 16 '
                             'requires security)')

        with mock.patch.object(state, '_ram_plan_run', raise_value):
            state._ram_batch_handle_card(state.ram_batch)
        attempt = state.ram_batch['attempts'][0]
        self.assertEqual(attempt['result'], 'skipped')
        self.assertIn('needs a default keyset', attempt['reason'])
        self.assertEqual(state.ram_batch['seen'][ICCID_DIGITS], 'skipped')

        state = self.batch_state()

        def raise_os(body, plan):
            raise OSError('card removed')

        with mock.patch.object(state, '_ram_plan_run', raise_os):
            state._ram_batch_handle_card(state.ram_batch)
        attempt = state.ram_batch['attempts'][0]
        self.assertEqual(attempt['result'], 'failed')
        self.assertIn('card removed', attempt['error'])
        self.assertEqual(state.ram_batch['seen'][ICCID_DIGITS], 'failed')

    def test_a_card_swap_before_the_lock_skips_the_plan(self):
        """The ICCID/preset are matched before the card lock; a swap in between
        must not run another card's chain (the v3.28.0 audit class)."""
        state = self.batch_state()
        calls = []
        with mock.patch.object(
                state, '_ram_plan_run',
                lambda body, plan: calls.append(1) or {'success': True}):
            # the worker decided on session 1; a different card is equipped by
            # the time the chain takes the lock
            state.card_session = 2
            state._ram_batch_handle_card(state.ram_batch, 1)
        self.assertEqual(calls, [])
        attempt = state.ram_batch['attempts'][0]
        self.assertEqual(attempt['result'], 'skipped')
        self.assertIn('the card changed', attempt['reason'])

        # the ICCID changes between the preset match and the lock (the card is
        # swapped in that window)
        state = self.batch_state()
        calls = []
        real_find = self.store.find_by_iccid

        def swap_then_find(value):
            state.iccid = '8970119000004600777'
            return real_find(value)

        with mock.patch.object(self.store, 'find_by_iccid', swap_then_find):
            with mock.patch.object(
                    state, '_ram_plan_run',
                    lambda body, plan: calls.append(1) or {'success': True}):
                state._ram_batch_handle_card(state.ram_batch,
                                             state.card_session)
        self.assertEqual(calls, [])
        self.assertIn('the card changed',
                      state.ram_batch['attempts'][0]['reason'])

    def test_a_new_install_clears_the_previous_notice(self):
        state = self.batch_state()
        state._ram_batch_record({'iccid': '8970119000004600000',
                                 'result': 'installed'})
        self.assertIn('installed', state.ram_batch['notice'])
        seen = {}

        def fake_run(body, plan):
            seen['notice'] = state.ram_batch['notice']
            seen['current'] = state.ram_batch['current']
            return {'success': True}

        with mock.patch.object(state, '_ram_plan_run', fake_run):
            state._ram_batch_handle_card(state.ram_batch)
        self.assertEqual(seen['notice'], '')       # cleared while installing
        self.assertEqual(seen['current']['iccid'], ICCID_DIGITS)
        self.assertIn('installed', state.ram_batch['notice'])

    def test_a_card_already_processed_is_reported_not_reinstalled(self):
        state = self.batch_state()
        calls = []
        with mock.patch.object(
                state, '_ram_plan_run',
                lambda body, plan: calls.append(1) or {'success': True}):
            state._ram_batch_handle_card(state.ram_batch)
            state._ram_batch_handle_card(state.ram_batch)
        self.assertEqual(len(calls), 1)
        self.assertEqual(state.ram_batch['attempts'][1]['result'], 'skipped')
        self.assertIn('already processed in this batch (installed)',
                      state.ram_batch['attempts'][1]['reason'])
        self.assertEqual(state.ram_batch['attempts_total'], 2)

    def test_a_delete_that_finds_nothing_is_already_absent(self):
        state = self.batch_state(kind='delete')

        def fake_run(body, plan):
            return {'success': True, 'final_cntr': '0000000002',
                    'steps': [{'name': 'DELETE (application)', 'sw': '9000',
                               'por_status': 'por_ok', 'por_sw': '6A88',
                               'delete_not_found': True}]}

        with mock.patch.object(state, '_ram_plan_run', fake_run):
            state._ram_batch_handle_card(state.ram_batch)
        attempt = state.ram_batch['attempts'][0]
        self.assertEqual(attempt['result'], 'already_absent')
        self.assertTrue(attempt['not_found'])
        self.assertEqual(state.ram_batch['counts']['already_absent'], 1)
        self.assertEqual(state.ram_batch['counts']['installed'], 0)
        self.assertIn('already absent', state.ram_batch['notice'])
        self.assertEqual(state.ram_batch['seen'][ICCID_DIGITS],
                         'already_absent')

    def test_a_reinstall_with_a_tolerated_pre_delete_is_an_install(self):
        state = self.batch_state(kind='reinstall-app')

        def fake_run(body, plan):
            return {'success': True,
                    'steps': [{'name': 'DELETE (application)',
                               'por_sw': '6A88', 'delete_not_found': True},
                              {'name': 'INSTALL [for install]',
                               'por_sw': '9000'}]}

        with mock.patch.object(state, '_ram_plan_run', fake_run):
            state._ram_batch_handle_card(state.ram_batch)
        self.assertEqual(state.ram_batch['attempts'][0]['result'],
                         'installed')
        self.assertEqual(state.ram_batch['counts']['installed'], 1)
        self.assertEqual(state.ram_batch['counts']['already_absent'], 0)


# ═══════════════════════════════════════════════════════════════════
# The batch install: the worker
# ═══════════════════════════════════════════════════════════════════

class BatchWorkerTests(BatchTestBase):
    def wait_for(self, predicate, timeout=4.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return False

    def test_the_worker_waits_for_a_finished_equip_then_installs(self):
        """A new card session is acted on only once the equip finished: the
        ready marker must match, else the worker would consume the session
        with a bogus "no ICCID" skip (the v3.28.0 audit)."""
        state = make_state(presets=self.store)
        self._states.append(state)
        run = state._ram_batch_new_state(batch_plan())
        state.ram_batch = run
        calls = []

        def fake_run(body, plan):
            calls.append(body)
            return {'success': True, 'final_cntr': '0000000002'}

        with mock.patch.object(state, '_ram_plan_run', fake_run):
            thread = threading.Thread(target=state._ram_batch_worker,
                                      args=(run,), daemon=True)
            thread.start()
            # connected, session bumped - but the equip has not finished
            state.connected = True
            state.iccid = ICCID_DIGITS
            state.card_session = 9
            state.card_ready_session = 8
            time.sleep(0.8)
            self.assertEqual(calls, [])
            self.assertEqual(run['attempts'], [])
            state.card_ready_session = 9
            self.assertTrue(self.wait_for(lambda: bool(run['attempts'])))
            self.assertTrue(state.ram_batch_stop())
            thread.join(timeout=3)
        self.assertEqual(len(calls), 1)
        self.assertEqual(run['attempts'][0]['result'], 'installed')
        self.assertFalse(run['active'])
        self.assertEqual(run['phase'], 'stopped')
        # the summary /api/status carries
        self.assertEqual(state.ram_batch_summary()['counts']['installed'], 1)
        self.assertFalse(state.ram_batch_summary()['active'])

    def test_the_worker_acts_on_the_next_card_session(self):
        state = make_state(presets=self.store)
        self._states.append(state)
        run = state._ram_batch_new_state(batch_plan())
        state.ram_batch = run
        state.connected = True
        state.iccid = ICCID_DIGITS
        state.card_session = 1
        state.card_ready_session = 1
        with mock.patch.object(state, '_ram_plan_run',
                               lambda body, plan: {'success': True}):
            thread = threading.Thread(target=state._ram_batch_worker,
                                      args=(run,), daemon=True)
            thread.start()
            self.assertTrue(self.wait_for(lambda: bool(run['attempts'])))
            # the same session is never attempted twice
            time.sleep(0.7)
            self.assertEqual(len(run['attempts']), 1)
            # a second card (a new session) is a new attempt
            state.iccid = '8970119000004600999'
            state.card_session = 2
            state.card_ready_session = 2
            self.assertTrue(self.wait_for(lambda: len(run['attempts']) == 2))
            state.ram_batch_stop()
            thread.join(timeout=3)
        self.assertEqual(run['attempts'][1]['result'], 'skipped')

    def test_a_delete_batch_sends_one_apdu_on_the_equipped_card(self):
        """The delete plan over the fake transport: one ENVELOPE, one attempt
        (no cap, no LOAD chain)."""
        card = RamCard(default=answer())
        state = self.card_state(card)
        state.ram_batch_start({'kind': 'delete',
                               'aid': PKG_AID.hex(),
                               'target': 'package',
                               'ram_format': 'compact'})
        try:
            self.assertTrue(self.wait_for(
                lambda: bool(state.ram_batch_status()['attempts'])))
        finally:
            state.ram_batch_stop()
            state._ram_batch_thread.join(timeout=5)
        attempt = state.ram_batch_status()['attempts'][0]
        self.assertEqual(attempt['result'], 'installed')
        self.assertEqual(attempt['iccid'], ICCID_DIGITS)
        self.assertEqual([step['name'] for step in attempt['steps']],
                         ['DELETE (package + applications)'])
        self.assertTrue(attempt['steps'][0]['ok'])
        self.assertEqual(len(card.envelopes), 1)

    def test_close_stops_the_batch_worker(self):
        state = make_state(presets=self.store)
        self._states.append(state)
        run = state._ram_batch_new_state(batch_plan())
        state.ram_batch = run
        state.connected = False                    # no card: the worker waits
        thread = threading.Thread(target=state._ram_batch_worker,
                                  args=(run,), daemon=True)
        state._ram_batch_thread = thread
        thread.start()
        time.sleep(0.2)
        self.assertTrue(state.ram_batch_active())
        state.close()
        self.assertFalse(thread.is_alive())
        self.assertFalse(run['active'])
        self.assertEqual(run['phase'], 'stopped')

    def test_close_does_not_wait_for_a_held_card_lock(self):
        """A batch install holds the card lock for its chain; shutdown must
        skip the teardown instead of blocking on it."""
        state = make_state(presets=self.store)
        self._states.append(state)
        held = threading.Event()
        release = threading.Event()

        def hold():
            with state.card_lock:
                held.set()
                release.wait(5)

        holder = threading.Thread(target=hold, daemon=True)
        holder.start()
        self.assertTrue(held.wait(2))
        with mock.patch.object(server_module, 'CLOSE_LOCK_TIMEOUT', 0.05):
            with mock.patch.object(state, '_teardown_session') as teardown:
                started = time.time()
                try:
                    state.close()
                finally:
                    release.set()
                    holder.join(timeout=5)
                self.assertLess(time.time() - started, 1.0)
                self.assertFalse(teardown.called)

    def test_a_disconnected_card_is_not_acted_on(self):
        state = make_state(presets=self.store)
        self._states.append(state)
        run = state._ram_batch_new_state(batch_plan())
        state.ram_batch = run
        state.connected = False
        state.iccid = ICCID_DIGITS
        state.card_session = 2
        state.card_ready_session = 2
        with mock.patch.object(state, '_ram_plan_run',
                               lambda body, plan: {'success': True}):
            thread = threading.Thread(target=state._ram_batch_worker,
                                      args=(run,), daemon=True)
            thread.start()
            time.sleep(0.8)
            self.assertEqual(run['attempts'], [])
            state.ram_batch_stop()
            thread.join(timeout=3)
        self.assertFalse(run['active'])


# ═══════════════════════════════════════════════════════════════════
# The batch install: the lifecycle and the status payload
# ═══════════════════════════════════════════════════════════════════

class BatchLifecycleTests(BatchTestBase):
    def test_the_three_kinds(self):
        for kind, body in (
                ('delete', {'kind': 'delete', 'aid': 'A0000003080000100001',
                            'target': 'package'}),
                ('reinstall-app', {'kind': 'reinstall-app',
                                   'loadfile_aid': PKG_AID.hex(),
                                   'module_aid': MODULE_AID.hex()}),
                ('install-cap', {'cap_hex': CAP_HEX})):
            state = make_state(presets=self.store)
            self._states.append(state)
            with mock.patch.object(state, '_ram_batch_worker',
                                   lambda run: None):
                state.ram_batch_start(body)
            self.assertEqual(state.ram_batch['plan']['kind'], kind)
            self.assertEqual(state.ram_batch_status()['kind'], kind)
            self.assertEqual(state.ram_batch_summary()['kind'], kind)
            # the plan is built once: the delete/reinstall chains are ready
            self.assertTrue(state.ram_batch['plan']['seq'])

    def test_the_delete_plan_is_the_gp_delete(self):
        state = make_state(presets=self.store)
        self._states.append(state)
        with mock.patch.object(state, '_ram_batch_worker', lambda run: None):
            state.ram_batch_start({'kind': 'delete',
                                   'aid': 'A0000003080000100001',
                                   'target': 'package'})
        plan = state.ram_batch['plan']
        self.assertEqual(plan['seq'][0]['apdu'],
                         capram.ram_delete_apdu('A0000003080000100001',
                                                cascade=True))
        self.assertTrue(plan['seq'][0]['delete'])

    def test_an_unknown_kind_and_a_bad_plan_are_refused(self):
        state = make_state(presets=self.store)
        self._states.append(state)
        with self.assertRaises(ValueError):
            state.ram_batch_start({'kind': 'wipe'})
        with self.assertRaises(ValueError):
            state.ram_batch_start({'kind': 'delete', 'aid': 'F041'})
        with self.assertRaises(ValueError):
            state.ram_batch_start({'kind': 'install-cap'})
        self.assertIsNone(state.ram_batch)

    def test_start_refuses_a_second_batch_and_a_test_run(self):
        state = make_state(presets=self.store)
        self._states.append(state)
        with mock.patch.object(state, '_ram_batch_worker', lambda run: None):
            state.ram_batch_start({'kind': 'delete',
                                   'aid': 'A0000003080000100001'})
            with self.assertRaises(BatchBusyError):
                state.ram_batch_start({'kind': 'delete',
                                       'aid': 'A0000003080000100001'})
        state.ram_batch['active'] = False
        state.ram_batch = None
        state.test_running = True
        with self.assertRaises(BatchBusyError):
            state.ram_batch_start({'kind': 'delete',
                                   'aid': 'A0000003080000100001'})

    def test_the_status_payload_shape(self):
        state = make_state(presets=self.store)
        self._states.append(state)
        idle = state.ram_batch_status()
        self.assertFalse(idle['active'])
        self.assertEqual(idle['phase'], 'idle')
        self.assertEqual(idle['attempts'], [])
        self.assertIsNone(state.ram_batch_summary())
        with mock.patch.object(state, '_ram_batch_worker', lambda run: None):
            state.ram_batch_start({'kind': 'delete',
                                   'aid': 'A0000003080000100001'})
        state._ram_batch_record({'iccid': ICCID_DIGITS,
                                 'result': 'installed'})
        status = state.ram_batch_status()
        self.assertTrue(status['active'])
        self.assertEqual(status['phase'], 'waiting_card')
        self.assertEqual(len(status['attempts']), 1)
        self.assertEqual(status['counts']['installed'], 1)
        summary = state.ram_batch_summary()
        self.assertEqual(summary['counts']['installed'], 1)
        self.assertNotIn('attempts', summary)
        # a stop request flips the phase; clear is refused while active
        self.assertTrue(state.ram_batch_stop())
        self.assertEqual(state.ram_batch_status()['phase'], 'stopping')
        self.assertFalse(state.ram_batch_clear())
        state.ram_batch['active'] = False
        self.assertTrue(state.ram_batch_clear())
        self.assertIsNone(state.ram_batch)
        self.assertFalse(state.ram_batch_stop())


# ═══════════════════════════════════════════════════════════════════
# The batch install: the HTTP endpoints
# ═══════════════════════════════════════════════════════════════════

class BatchHttpTests(BatchTestBase):
    def wait_for(self, predicate, timeout=4.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return False

    def cardless_state(self):
        state = make_state(presets=self.store)
        self._states.append(state)
        return state

    def test_the_batch_endpoints(self):
        state = self.cardless_state()
        harness = self.served(state)
        status, body = harness.json('POST', '/api/ram-batch/start',
                                    {'kind': 'delete',
                                     'aid': 'A0000003080000100001'})
        self.assertEqual(status, 200, body)
        self.assertTrue(body['active'])
        self.assertTrue(body['ok'])
        self.assertEqual(body['kind'], 'delete')
        # a second start is refused
        status, body = harness.json('POST', '/api/ram-batch/start',
                                    {'kind': 'delete',
                                     'aid': 'A0000003080000100001'})
        self.assertEqual(status, 409, body)
        # the status is card-free
        status, body = harness.json('GET', '/api/ram-batch/status')
        self.assertEqual(status, 200, body)
        self.assertTrue(body['active'])
        # /api/status carries the summary
        status, body = harness.json('GET', '/api/status')
        self.assertEqual(body['ram_batch']['kind'], 'delete')
        # clear is refused while active
        status, body = harness.json('POST', '/api/ram-batch/clear', {})
        self.assertEqual(status, 409, body)
        # the stop answers and flips the phase
        status, body = harness.json('POST', '/api/ram-batch/stop', {})
        self.assertEqual(status, 200, body)
        self.assertEqual(body['phase'], 'stopping')
        self.assertTrue(self.wait_for(
            lambda: not state.ram_batch_status()['active']))
        status, body = harness.json('POST', '/api/ram-batch/clear', {})
        self.assertEqual(status, 200, body)
        # with no run left, a stop is a 409
        status, body = harness.json('POST', '/api/ram-batch/stop', {})
        self.assertEqual(status, 409, body)
        status, body = harness.json('GET', '/api/ram-batch/status')
        self.assertEqual(status, 200, body)
        self.assertFalse(body['active'])
        self.assertEqual(body['phase'], 'idle')
        status, body = harness.json('GET', '/api/status')
        self.assertIsNone(body['ram_batch'])

    def test_a_bad_start_is_a_400(self):
        state = self.cardless_state()
        harness = self.served(state)
        status, body = harness.json('POST', '/api/ram-batch/start', {})
        self.assertEqual(status, 400, body)
        self.assertIn('cap_hex', body['error'])
        status, body = harness.json('POST', '/api/ram-batch/start',
                                    {'cap_hex': '00'})
        self.assertEqual(status, 400, body)
        self.assertIn('zip', body['error'].lower())
        status, body = harness.json('POST', '/api/ram-batch/start',
                                    {'kind': 'wipe'})
        self.assertEqual(status, 400, body)
        self.assertIsNone(state.ram_batch)

    def test_a_test_run_blocks_the_start(self):
        state = self.cardless_state()
        state.test_running = True
        status, body = self.served(state).json(
            'POST', '/api/ram-batch/start',
            {'kind': 'delete', 'aid': 'A0000003080000100001'})
        self.assertEqual(status, 409, body)
        self.assertIn('test run', body['error'])

    def test_stop_and_status_answer_while_the_card_lock_is_held(self):
        """The panel's Stop must be effective during an install (the worker
        holds the card lock for the whole chain)."""
        state = self.cardless_state()
        harness = self.served(state)
        with mock.patch.object(state, '_ram_batch_worker', lambda run: None):
            status, body = harness.json(
                'POST', '/api/ram-batch/start',
                {'kind': 'delete', 'aid': 'A0000003080000100001'})
        self.assertEqual(status, 200, body)
        with state.card_lock:
            status, body = harness.json('POST', '/api/ram-batch/stop', {},
                                        timeout=2)
            self.assertEqual(status, 200, body)
            self.assertEqual(body['phase'], 'stopping')
            status, body = harness.json('GET', '/api/ram-batch/status',
                                        timeout=2)
            self.assertEqual(status, 200, body)
            # the CAP analysis stays card-free too (the existing behaviour)
            status, body = harness.json('POST', '/api/cap-info',
                                        {'cap_hex': CAP_HEX}, timeout=2)
        self.assertEqual(status, 200, body)
        self.assertTrue(body['ok'])


if __name__ == '__main__':
    unittest.main()
