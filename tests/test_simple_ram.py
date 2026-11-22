# coding=utf-8
"""Tests for the CAP analysis and the single-card RAM installs (3c-3c-2a).

``POST /api/cap-info`` (the capmem analysis), ``POST /api/ram-install`` (the
INSTALL/LOAD chain) and ``POST /api/ram-install-app`` (INSTALL [for install] /
[for make selectable]) plus the pure builders in
:mod:`simple_server.capram` and the shared step logic.

No card and no pysim: every exchange is a scripted fake transport (the CAP
fixture is a synthetic ZIP, the response packets are built by the test) and
the endpoints run on a real :class:`ThreadingHTTPServer` bound to an ephemeral
loopback port.  Keys are the public synthetic material the other suites use.

Spec anchors: GP Card Spec v2.3.1 §11.5.2.3.1/Table 11-42 (INSTALL [for load]),
§11.6.2/Tables 11-56/11-57 (LOAD), §11.5.2.3.2/Table 11-43 (INSTALL [for
install]), §11.5.2.3.3/Table 11-44 (make selectable), §11.2/Tables 11-20-11-23
(DELETE), §11.5.2.3.7/Tables 11-48/11-49 (the C6/C7/C8 quotas and the mandatory
C9); TS 102 226 §8.2.1.3.2.1 (the one System Specific Parameters EF),
§8.2.1.7.2 (GET DATA FF21), §5.1.2 Table 5.1 (the compact response);
TS 102 225 §5.1.1 (the SPI bits) and §5.2 Table 3 (the response packet).
The CAP component order is the JC VM Specification v2.2.2 §6.2/Table 6-3 load
order (not a GP rule), and the builders' INSTALL/LOAD/INSTALL APDUs are pinned
to the TCA reference trace
``samples/tca_ram_traces/test_mycard_keyset2_applet_full_load.log``.
"""

import io
import json
import pathlib
import shutil
import struct
import tempfile
import threading
import unittest
import zipfile
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from pysim_simple_server import capmem
from pysim_simple_server import presets as presets_store

from simple_server import capram
from simple_server import ota
from simple_server.server import ServerState, build_http_server

ATR = bytes.fromhex('3b9f95801fc78031e073fe211b67a3a0001505017204')
ICCID_DIGITS = '8970119000004600098'
ICCID_HEX = '980711090000640090f8'

# Synthetic keys (the repo rule: no card-specific material in tests).
KIC_KEY = '00112233445566778899AABBCCDDEEFF'
KID_KEY = 'FFEEDDCCBBAA99887766554433221100'

ROLE_TARS = [{'role': role, 'tar': tar, 'msl': '16', 'keyset': 1}
             for role, tar in (('isd', '000000'), ('uiccRfm', 'B00000'),
                               ('usimRfm', 'B00001'))]
# An MSL-00 TAR table: `spi1_for_tar(prefer_msl=True)` then picks the spec's
# no-security SPI1 `00`, so the SCP80 command packets travel unciphered and the
# test can read the C-APDU back out of the ENVELOPE.
NO_SECURITY_TARS = [{'role': role, 'tar': tar, 'msl': '00'}
                    for role, tar in (('isd', '000000'), ('uiccRfm', 'B00000'),
                                      ('usimRfm', 'B00001'))]

# ---- the startup / ENVELOPE fake card -------------------------------------

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
    """A card whose ENVELOPEs answer a scripted response packet per index.

    ``answers`` maps the 1-based ENVELOPE number to a ``(data, sw)`` answer
    (``None`` refuses the ENVELOPE with ``6A82``); every other ENVELOPE gets
    ``default`` - a successful response packet with no secured data, so each
    step is ``por_ok`` unless the test says otherwise.
    """

    def __init__(self, answers=None, default=None, **kwargs):
        super().__init__(**kwargs)
        self.answers = dict(answers or {})
        self.default = answer() if default is None else default
        self.envelopes = []

    def __call__(self, apdu):
        if apdu[:2] == b'\x80\xc2':             # ENVELOPE (SMS-PP download)
            self.envelopes.append(apdu)
            index = len(self.envelopes)
            answer = self.answers.get(index, self.default)
            return answer if answer is not None else (b'', b'\x6a\x82')
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
    """An SCP80 response packet (TS 102 225 §5.2 Table 3) with the RPI UDH.

    ``UDHL RPI | RPL RHL TAR CNTR PCNTR STS secured data``; RHL 10 = no RC/CC
    (the RAM sends use SPI2 ``01``: a PoR, no response signature).
    """
    body = (bytes.fromhex(tar + cntr) + bytes([0x00, status])
            + bytes.fromhex(secured))
    return (bytes.fromhex('027100') + len(body).to_bytes(2, 'big')
            + bytes([10]) + body)


def answer(status=0x00, secured='019000'):
    """The ENVELOPE answer carrying a response packet (R-APDU SW 9000).

    The secured data is the compact remote response for one executed command
    (TS 102 226 §5.1.2 Table 5.1: count 1, the command's SW 9000), so every
    step reports its remote status word.
    """
    return por_packet(status=status, secured=secured), b'\x90\x00'


def memory_answer(free_nv, applets=1):
    """An answer whose remote command is a GET DATA FF21 (TS 102 226 8.2.1.7.2)."""
    content = ('8101%02X' % applets
               + '8202%04X' % free_nv)
    rapdu = 'FF21%02X%s' % (len(content) // 2, content)
    return answer(secured='019000' + rapdu)


def not_found_answer():
    """A ``por_ok`` answer whose remote command refused with 6A88."""
    return answer(secured='016A88')


# ---- the CAP fixture -------------------------------------------------------

PKG_AID = bytes.fromhex('A0000003080000100001')
MODULE_AID = bytes.fromhex('A000000308000010000101')
JC_FRAMEWORK = bytes.fromhex('A0000000620101')
UICC_TOOLKIT = bytes.fromhex('A0000000090005FFFFFFFF8912000000')


def component(tag, payload):
    """A CAP component file: tag + the payload length (not the 3-byte header)."""
    return bytes([tag]) + len(payload).to_bytes(2, 'big') + payload


def build_zip(parts):
    """A ``.cap`` ZIP from ``[(component_name, bytes)]`` entries."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        for name, data in parts:
            archive.writestr('pkg/%s.cap' % name, data)
    return buffer.getvalue()


def build_cap(header_flags=0x04, header_aid=PKG_AID, applet_aid=MODULE_AID,
              with_applet=True, with_import=True):
    """A small synthetic ``.cap`` archive (ZIP bytes).

    The Header (package AID, flags), an Import component (javacard.framework
    1.2 and uicc.toolkit 1.11 - the platform/ETSI requirement), the Applet
    component (one applet) and a Method component (the load-file size).
    """
    header = component(0x01, struct.pack('>I', 0xDECAFFED)
                       + bytes([1, 2, header_flags, 1, 2])
                       + bytes([len(header_aid)]) + header_aid)
    parts = [('Header', header)]
    if with_import:
        parts.append(('Import', component(
            0x04, bytes([2])
            + bytes([2, 1, len(JC_FRAMEWORK)]) + JC_FRAMEWORK
            + bytes([11, 1, len(UICC_TOOLKIT)]) + UICC_TOOLKIT)))
    if with_applet:
        parts.append(('Applet', component(
            0x03, bytes([1, len(applet_aid)]) + applet_aid
            + (1).to_bytes(2, 'big'))))
    parts.append(('Method', component(0x07, bytes([0]) + bytes([0x7A] * 8))))
    return build_zip(parts)


CAP_PARTS = {
    'Header': component(0x01, struct.pack('>I', 0xDECAFFED)
                        + bytes([1, 2, 0x04, 1, 2])
                        + bytes([len(PKG_AID)]) + PKG_AID),
    'Import': component(0x04, bytes([2])
                        + bytes([2, 1, len(JC_FRAMEWORK)]) + JC_FRAMEWORK
                        + bytes([11, 1, len(UICC_TOOLKIT)]) + UICC_TOOLKIT),
    'Applet': component(0x03, bytes([1, len(MODULE_AID)]) + MODULE_AID
                        + (1).to_bytes(2, 'big')),
    'Method': component(0x07, bytes([0]) + bytes([0x7A] * 8)),
}
CAP = build_cap()
CAP_HEX = CAP.hex()
# The load file in the JC VM component load order (JC VM spec §6.2/Table 6-3;
# all components; the strings below pin the APDUs built from it).
EXPECTED_LOAD = ''.join(CAP_PARTS[name].hex() for name in
                        ('Header', 'Import', 'Applet', 'Method')).upper()

# ---- the TCA reference trace ----------------------------------------------
# Independent ground truth: the clear (unwrapped) C-APDUs of
# `samples/tca_ram_traces/test_mycard_keyset2_applet_full_load.log` (the file
# itself stays out of the repo).  The trace loads with 64-octet blocks, so its
# first LOAD is this builder's `block_size=64` block; the whole 1049-byte load
# file follows the same split (17 blocks, last P1=0x80/P2=0x10).
TRACE_LOADFILE_AID = 'AA1902BC225001'
TRACE_MODULE_AID = 'AA1902BC22500101'
TRACE_INSTALL_FOR_LOAD = '80E602000C07AA1902BC22500100000000'
TRACE_FIRST_LOAD = (
    '80E8000040C4820419010011DECAFFED010204000107AA1902BC22500102001F'
    '0011001F000C0028009600180258004000540000015400080004002A03010004'
    '0028030001')
# The trace's caller-composed install parameters: `C9 00 EF 00 EA 10 ...` (an
# empty System Specific Parameters EF plus the toolkit part).
TRACE_INSTALL_PARAMS = 'C900EF00EA10800A0000100100000000000081008200'
TRACE_INSTALL_FOR_INSTALL = (
    '80E60C003607AA1902BC22500108AA1902BC2250010108AA1902BC2250010103'
    '00000016C900EF00EA10800A000010010000000000008100820000')


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
    """A real :class:`PresetStore` in a throw-away directory."""
    root = tempfile.mkdtemp(prefix='simple-ram-presets-')
    testcase.addCleanup(shutil.rmtree, root, True)
    return presets_store.PresetStore(path=pathlib.Path(root) /
                                     'card_presets.json')


def preset_with_store(testcase, **kwargs):
    store = temp_preset_store(testcase)
    return store, store.add(preset_fields(**kwargs))


#: A throw-away root for the test-script/-suite stores: a state must never
#: read or write the user's real ``~/.pysim-simple-server`` files.
_STORE_ROOT = pathlib.Path(tempfile.mkdtemp(prefix='simple-ram-stores-'))
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


class RamHarness(object):
    """A real ThreadingHTTPServer on an ephemeral loopback port."""

    def __init__(self, state):
        self.state = state
        self.server = build_http_server('127.0.0.1', 0, state)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={'poll_interval': 0.05},
            name='test-http-ram', daemon=True)
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


class RamTestBase(unittest.TestCase):
    def setUp(self):
        self.harness = None
        self.store, self.preset = preset_with_store(self)

    def tearDown(self):
        if self.harness is not None:
            self.harness.close()
        for state in getattr(self, '_states', []):
            state.close()
        self._states = []

    def served(self, state):
        self.harness = RamHarness(state)
        return self.harness

    def card_state(self, card):
        state = make_state(presets=self.store, reader='Reader A')
        state.open(transport=FakeTransport(handler=card), start_threads=False)
        self._states = getattr(self, '_states', [])
        self._states.append(state)
        return state

    def preset_id(self):
        return self.preset['id']


# ═══════════════════════════════════════════════════════════════════
# /api/cap-info
# ═══════════════════════════════════════════════════════════════════

class CapInfoTests(RamTestBase):
    def test_the_analysis_fields(self):
        state = make_state(presets=self.store)
        self._states = [state]
        status, body = self.served(state).json('POST', '/api/cap-info',
                                               {'cap_hex': CAP_HEX})
        self.assertEqual(status, 200)
        self.assertTrue(body['ok'])
        self.assertEqual(body['load_file_aid'], PKG_AID.hex().upper())
        self.assertEqual(body['module_aid'], MODULE_AID.hex().upper())
        self.assertEqual(body['load_file_bytes'], 86)
        memory = body['memory']
        # the package identity and the applet list
        self.assertEqual(memory['package_aid'], PKG_AID.hex().upper())
        self.assertEqual(memory['applet_count'], 1)
        self.assertEqual(memory['applets'], [MODULE_AID.hex().upper()])
        # the required libraries, each with its import version and the
        # distinct constant-pool reference count (JC VM spec 6.6/6.7)
        imports = {entry['aid']: entry for entry in memory['imports']}
        self.assertEqual(set(imports), {JC_FRAMEWORK.hex().upper(),
                                        UICC_TOOLKIT.hex().upper()})
        self.assertEqual((imports[JC_FRAMEWORK.hex().upper()]['major'],
                          imports[JC_FRAMEWORK.hex().upper()]['minor']), (1, 2))
        self.assertEqual(
            (imports[UICC_TOOLKIT.hex().upper()]['major'],
             imports[UICC_TOOLKIT.hex().upper()]['minor']), (1, 11))
        self.assertEqual(imports[JC_FRAMEWORK.hex().upper()]['refs'], 0)
        # the header package flags (JC VM spec Table 6-4)
        self.assertEqual(memory['flags'], {'raw': 4, 'int': False,
                                           'export': False, 'applet': True})
        self.assertTrue(memory['needs_int'] is False)
        # the component sizes in load-file order, summing to the load file
        self.assertEqual(memory['components'], [
            {'name': 'Header', 'size': 23},
            {'name': 'Import', 'size': 33},
            {'name': 'Applet', 'size': 18},
            {'name': 'Method', 'size': 12}])
        self.assertEqual(sum(c['size'] for c in memory['components']),
                         body['load_file_bytes'])
        # the platform requirement and the ETSI release
        self.assertEqual(memory['requires_framework'], '1.2')
        self.assertEqual(memory['requires_java_card'], '2.2.1')
        self.assertEqual(memory['requires_etsi_basis'], 'uicc.toolkit 1.11')
        self.assertEqual(memory['requires_etsi_release'], 'REL-12')
        # the memory model and the suggested quota values
        self.assertEqual(memory['code'], {'method_component': 9,
                                          'load_file': 86})
        self.assertEqual(memory['nvram']['total'], 6)
        self.assertEqual(memory['nvram']['requirement'], 92)
        self.assertEqual(memory['suggested']['c6'], 86)
        self.assertEqual(memory['suggested']['c8'], 6)
        self.assertEqual(memory['suggested']['c7'], 256)

    def test_the_cap_field_is_an_alias(self):
        state = make_state(presets=self.store)
        self._states = [state]
        status, body = self.served(state).json('POST', '/api/cap-info',
                                               {'cap': CAP_HEX})
        self.assertEqual(status, 200)
        self.assertTrue(body['ok'])

    def test_an_invalid_cap_is_a_400(self):
        state = make_state(presets=self.store)
        self._states = [state]
        harness = self.served(state)
        for body in ({}, {'cap_hex': ''}, {'cap_hex': 'zz'},
                     {'cap_hex': '00112233'}):
            status, answer = harness.json('POST', '/api/cap-info', body)
            self.assertEqual(status, 400, body)
            self.assertFalse(answer['ok'])
            self.assertTrue(answer['error'])

    def test_a_cap_without_an_applet_component_is_a_400(self):
        cap = build_cap(with_applet=False).hex()
        state = make_state(presets=self.store)
        self._states = [state]
        status, body = self.served(state).json('POST', '/api/cap-info',
                                               {'cap_hex': cap})
        self.assertEqual(status, 400)
        self.assertIn('missing Applet component', body['error'])

    def test_a_truncated_header_is_a_400(self):
        payload = struct.pack('>I', 0xDECAFFED) + bytes([1, 2, 0x04, 1, 2])
        truncated = component(0x01, payload)
        cap = build_zip([
            ('Header', truncated),
            ('Applet', component(0x03, bytes([1, len(MODULE_AID)]) + MODULE_AID
                                 + (1).to_bytes(2, 'big')))])
        state = make_state(presets=self.store)
        self._states = [state]
        status, body = self.served(state).json(
            'POST', '/api/cap-info', {'cap_hex': cap.hex()})
        self.assertEqual(status, 400)
        self.assertIn('Header is truncated', body['error'])

    def test_the_analysis_is_card_free(self):
        # CARD_FREE_POST: the endpoint answers while another operation holds
        # the card lock (a RAM install's live progress polls during it)
        state = make_state(presets=self.store)
        self._states = [state]
        harness = self.served(state)
        with state.card_lock:
            status, body = harness.json('POST', '/api/cap-info',
                                        {'cap_hex': CAP_HEX})
        self.assertEqual(status, 200)
        self.assertTrue(body['ok'])


# ═══════════════════════════════════════════════════════════════════
# /api/ram-install
# ═══════════════════════════════════════════════════════════════════

class RamInstallTests(RamTestBase):
    def install_body(self, **kwargs):
        body = {'cap_hex': CAP_HEX, 'preset_id': self.preset_id(),
                'spi2': '01', 'ram_format': 'compact'}
        body.update(kwargs)
        return body

    def test_the_step_sequence_and_the_response_shape(self):
        card = RamCard()
        state = self.card_state(card)
        status, body = self.served(state).json('POST', '/api/ram-install',
                                               self.install_body())
        self.assertEqual(status, 200)
        self.assertTrue(body['success'])
        self.assertEqual(body['load_file_aid'], PKG_AID.hex().upper())
        self.assertEqual(body['module_aid'], MODULE_AID.hex().upper())
        self.assertEqual(body['load_block_size'], 240)
        self.assertIsNone(body['load_block_size_requested'])
        self.assertTrue(body['load_block_size_auto'])
        self.assertEqual(body['ram_format'], 'compact')
        self.assertEqual([step['name'] for step in body['steps']], [
            'INSTALL [for load]', 'LOAD (1/1)', 'INSTALL [for install]'])
        for step in body['steps']:
            self.assertEqual(step['por_status'], 'por_ok')
            self.assertEqual(step['por_sw'], '9000')
        # the INSTALL/LOAD APDUs, pinned against cap_apdu_sequence
        self.assertEqual(capram.cap_apdu_sequence(
            PKG_AID.hex().upper(), MODULE_AID.hex().upper(), EXPECTED_LOAD), [
            '80E602000F0aA000000308000010000100000000',
            '80E8800058C456010014DECAFFED01020401020AA0000003080000100001'
            '04001E02020107A00000006201010B0110A0000000090005FFFFFFFF8912'
            '00000003000F010BA0000003080000100001010001070009007A7A7A7A7A'
            '7A7A7A',
            '80E60C00290aA00000030800001000010bA000000308000010000101'
            '0bA000000308000010000101010002C90000'])
        # NV before + 3 steps + NV after: every accepted packet consumed a
        # counter, and the store holds the next value
        self.assertEqual(body['final_cntr'], '0000000006')
        self.assertEqual(
            self.store.get(self.preset_id())['keysets'][0]['cntr'],
            '0000000006')
        self.assertEqual(len(card.envelopes), 5)
        # the card has no FF21 answer here, so no footprint fields
        self.assertNotIn('nv_before', body)

    def test_the_gp_apdus_reach_the_card_verbatim(self):
        # an MSL-00 TAR: no ciphering, so the C-APDU is readable in the TPDU
        store, preset = preset_with_store(self, tars=NO_SECURITY_TARS)
        card = RamCard()
        state = make_state(presets=store, reader='Reader A')
        state.open(transport=FakeTransport(handler=card), start_threads=False)
        self._states = [state]
        status, body = self.served(state).json('POST', '/api/ram-install', {
            'cap_hex': CAP_HEX, 'preset_id': preset['id'],
            'spi2': '01', 'ram_format': 'compact'})
        self.assertEqual(status, 200)
        self.assertTrue(body['success'])
        # no counter is tracked with SPI1 00 (TS 102 225 5.1.1 b5b4): the
        # reported counter never advances and the store is untouched
        self.assertEqual(body['final_cntr'], '0000000001')
        self.assertEqual(
            store.get(preset['id'])['keysets'][0]['cntr'], '0000000001')
        loadfile = PKG_AID.hex().upper()
        module = MODULE_AID.hex().upper()
        expected = ['80E602000F0a%s00000000' % loadfile,
                    '80E8800058C456010014DECAFFED01020401020AA000000308000'
                    '010000104001E02020107A00000006201010B0110A00000000900'
                    '05FFFFFFFF891200000003000F010BA00000030800001000010100'
                    '01070009007A7A7A7A7A7A7A7A',
                    '80E60C00290a%s0b%s0b%s010002C90000'
                    % (loadfile, module, module)]
        # 0-based: NV before, INSTALL [for load], LOAD (1/1), INSTALL
        self.assertIn(expected[0].upper(), card.tpdu(1).hex().upper())
        self.assertIn(expected[1].upper(), card.tpdu(2).hex().upper())
        self.assertIn(expected[2].upper(), card.tpdu(3).hex().upper())
        # the NV probes are the read-only GET DATA FF21
        self.assertIn('80CAFF2100', card.tpdu(0).hex().upper())

    def test_the_load_blocks_split_the_cap(self):
        card = RamCard()
        state = self.card_state(card)
        status, body = self.served(state).json(
            'POST', '/api/ram-install',
            self.install_body(load_block_size=40))
        self.assertEqual(status, 200)
        self.assertTrue(body['success'])
        self.assertEqual(body['load_block_size'], 40)
        self.assertEqual(body['load_block_size_requested'], 40)
        self.assertFalse(body['load_block_size_auto'])
        self.assertEqual([step['name'] for step in body['steps']], [
            'INSTALL [for load]', 'LOAD (1/3)', 'LOAD (2/3)', 'LOAD (3/3)',
            'INSTALL [for install]'])
        self.assertEqual(capram.cap_apdu_sequence(
            PKG_AID.hex().upper(), MODULE_AID.hex().upper(),
            EXPECTED_LOAD, block_size=40)[1:4], [
            '80E8000028C456010014DECAFFED01020401020AA0000003080000100001'
            '04001E02020107A00000006201010B',
            '80E80001280110A0000000090005FFFFFFFF891200000003000F010BA00000'
            '0308000010000101000107000900',
            '80E88002087A7A7A7A7A7A7A7A'])

    def test_the_delete_package_variant(self):
        # the cascade DELETE is the first step and a 'not found' answer (the
        # card has no such package) lets the chain proceed
        card = RamCard(answers={2: not_found_answer()})
        state = self.card_state(card)
        status, body = self.served(state).json(
            'POST', '/api/ram-install', self.install_body(delete_package=True))
        self.assertEqual(status, 200)
        self.assertTrue(body['success'])
        self.assertEqual([step['name'] for step in body['steps']], [
            'DELETE (package + applications)', 'INSTALL [for load]',
            'LOAD (1/1)', 'INSTALL [for install]'])
        self.assertTrue(body['steps'][0]['delete_not_found'])
        self.assertEqual(body['steps'][0]['por_sw'], '6A88')
        self.assertEqual(capram.ram_delete_apdu(
            PKG_AID.hex().upper(), cascade=True),
            '80E400800C4F0AA0000003080000100001')

    def test_a_hard_delete_failure_stops_the_chain(self):
        # anything but 'not found' on a delete step is a real failure
        card = RamCard(answers={2: answer(secured='016982')})
        state = self.card_state(card)
        status, body = self.served(state).json(
            'POST', '/api/ram-install', self.install_body(delete_package=True))
        self.assertEqual(status, 200)
        self.assertFalse(body['success'])
        self.assertEqual(body['failed_step'], 1)
        self.assertNotIn('delete_not_found', body['steps'][0])
        self.assertIn('DELETE (package + applications)', body['error'])

    def test_a_failed_step_stops_the_chain(self):
        # the LOAD answers a cntr_low verdict (SPI2 status 0x02)
        card = RamCard(answers={3: answer(status=0x02)})
        state = self.card_state(card)
        status, body = self.served(state).json('POST', '/api/ram-install',
                                               self.install_body())
        self.assertEqual(status, 200)
        self.assertFalse(body['success'])
        self.assertEqual(body['failed_step'], 2)
        self.assertEqual(body['steps'][-1]['name'], 'LOAD (1/1)')
        self.assertEqual(body['steps'][-1]['por_status'], 'cntr_low')
        self.assertEqual(body['steps'][-1]['por_error'], 'PoR cntr_low')
        self.assertIn('LOAD (1/1)', body['error'])
        self.assertTrue(body['cntr_low'])
        # NV before + INSTALL [for load] + the (silent) NV-after read were
        # accepted; the rejected LOAD itself was not
        self.assertEqual(body['final_cntr'], '0000000004')
        self.assertEqual(
            self.store.get(self.preset_id())['keysets'][0]['cntr'],
            '0000000004')
        # the chain stopped at the LOAD: the NV-after read plus the
        # INSTALL [for install] never went out (4 envelopes)
        self.assertEqual(len(card.envelopes), 4)

    def test_a_remote_status_word_failure_is_named(self):
        card = RamCard(answers={2: answer(secured='016A80')})
        state = self.card_state(card)
        status, body = self.served(state).json('POST', '/api/ram-install',
                                               self.install_body())
        self.assertEqual(status, 200)
        self.assertFalse(body['success'])
        self.assertEqual(body['steps'][-1]['por_sw'], '6A80')
        self.assertEqual(body['steps'][-1]['por_error'], 'remote SW 6A80')

    def test_the_nv_footprint(self):
        # ENVELOPE 1 = NV before, ENVELOPE 5 = NV after (NV, 3 steps, NV)
        card = RamCard(answers={1: memory_answer(1000), 5: memory_answer(400)})
        state = self.card_state(card)
        status, body = self.served(state).json('POST', '/api/ram-install',
                                               self.install_body())
        self.assertEqual(status, 200)
        self.assertTrue(body['success'])
        self.assertEqual((body['nv_before'], body['nv_after'],
                          body['nv_delta']), (1000, 400, 600))

    def test_a_refused_ff21_read_never_fails_the_operation(self):
        # ENVELOPE 1 is the NV-before read: the card may refuse the remote
        # command (6A88) or the ENVELOPE itself.  Neither may fail the chain,
        # and with no successful FF21 no footprint fields are reported.
        for refused in (answer(secured='016A88'), None):
            card = RamCard(answers={1: refused})
            state = self.card_state(card)
            self.served(state)
            status, body = self.harness.json('POST', '/api/ram-install',
                                             self.install_body())
            self.assertEqual(status, 200)
            self.assertTrue(body['success'], refused)
            self.assertNotIn('nv_before', body)
            # the RAM steps still went out (the silent read is not a step)
            self.assertEqual([step['name'] for step in body['steps']], [
                'INSTALL [for load]', 'LOAD (1/1)',
                'INSTALL [for install]'])
            self.harness.close()
            self.harness = None

    def test_the_format_probe_falls_back_to_the_expanded_form(self):
        # an MSL-00 TAR: no ciphering, so the chain's C-APDUs are readable in
        # the TPDUs (the format detection itself is the same)
        store, preset = preset_with_store(self, tars=NO_SECURITY_TARS)
        card = RamCard(answers={1: None})       # the compact probe is refused
        state = make_state(presets=store, reader='Reader A')
        state.open(transport=FakeTransport(handler=card), start_threads=False)
        self._states = [state]
        status, body = self.served(state).json('POST', '/api/ram-install', {
            'cap_hex': CAP_HEX, 'preset_id': preset['id'],
            'spi2': '01', 'ram_format': 'auto'})
        self.assertEqual(status, 200)
        self.assertTrue(body['success'])
        self.assertEqual(body['ram_format'], 'expanded')
        self.assertEqual([step['name'] for step in body['steps'][:2]], [
            'FORMAT CHECK (compact)', 'FORMAT CHECK (expanded)'])
        self.assertEqual(body['steps'][0]['por_status'], 'envelope_error')
        # the detected format is used for the chain, not only the probe: the
        # INSTALL [for load] (ENVELOPE 4, after the two probes and the silent
        # NV-before read) is the `AA <len> 22 <len> <apdu>` template
        install = capram.cap_apdu_sequence(
            PKG_AID.hex().upper(), MODULE_AID.hex().upper(),
            EXPECTED_LOAD)[0]
        wrapped = ota.wrap_expanded_apdu(install, 'definite')
        self.assertTrue(wrapped.startswith('AA'))
        self.assertIn(wrapped.upper(), card.tpdu(3).hex().upper())

    def test_a_card_that_refuses_every_format_stays_compact(self):
        card = RamCard(answers={1: None, 2: None, 3: None})
        state = self.card_state(card)
        status, body = self.served(state).json('POST', '/api/ram-install',
                                               self.install_body(
                                                   ram_format='auto'))
        self.assertEqual(status, 200)
        self.assertEqual(body['ram_format'], 'compact')
        self.assertEqual([step['name'] for step in body['steps'][:3]], [
            'FORMAT CHECK (compact)', 'FORMAT CHECK (expanded)',
            'FORMAT CHECK (expanded-ae)'])

    def test_the_cap_parameter_composition_applies_to_the_final_apdu(self):
        card = RamCard()
        state = self.card_state(card)
        body = self.install_body(volatile_quota=200, nv_quota=1000)
        status, answer = self.served(state).json('POST', '/api/ram-install',
                                                 body)
        self.assertEqual(status, 200)
        self.assertTrue(answer['success'])
        # the final INSTALL [for install] carries one System Specific
        # Parameters EF with both quotas (C9 00 EF {...}, TS 102 226
        # 8.2.1.3.2.1)
        final = capram.install_plan(body)['seq'][-1]['apdu']
        self.assertIn('C900EF08C70200C8C80203E8', final)

    def test_the_one_ef_refusal_is_a_400_and_sends_nothing(self):
        card = RamCard()
        state = self.card_state(card)
        harness = self.served(state)
        for body in ({'install_params': 'C900', 'stk_params': 'CA0C01'},
                     {'install_params': 'C900EF00', 'stk_params': 'EF02CA00'},
                     # a malformed caller field: a truncated/indefinite length
                     # that used to swallow a following EF, and one with no EF
                     # at all (the strict walk must consume every octet)
                     {'install_params': 'C980EF02CA00',
                      'stk_params': 'EF02CA00'},
                     {'install_params': 'EF80', 'stk_params': 'EF02CA00'},
                     {'install_params': 'C980'},
                     {'install_params': 'C900', 'nv_quota': 100},
                     {'stk_params': 'EF08CA00'}):
            status, answer = harness.json('POST', '/api/ram-install',
                                          self.install_body(**body))
            self.assertEqual(status, 400, body)
            self.assertFalse(answer['success'])
        self.assertEqual(card.envelopes, [])

    def test_an_invalid_block_size_is_a_400(self):
        card = RamCard()
        state = self.card_state(card)
        harness = self.served(state)
        for value in (0, 241, 'x'):
            status, body = harness.json('POST', '/api/ram-install',
                                        self.install_body(load_block_size=value))
            self.assertEqual(status, 400, value)
            self.assertIn('load_block_size', body['error'])
        self.assertEqual(card.envelopes, [])

    def test_a_missing_cap_is_a_400(self):
        card = RamCard()
        state = self.card_state(card)
        status, body = self.served(state).json('POST', '/api/ram-install',
                                               {'preset_id': self.preset_id()})
        self.assertEqual(status, 400)
        self.assertIn('No cap_hex provided', body['error'])

    def test_no_card_is_503(self):
        state = make_state(presets=self.store)
        self._states = [state]
        status, body = self.served(state).json('POST', '/api/ram-install',
                                               self.install_body())
        self.assertEqual(status, 503)
        self.assertIn('no card session', body['error'])

    def test_the_progress_is_published_and_cleared(self):
        card = RamCard()
        state = self.card_state(card)
        harness = self.served(state)
        status, body = harness.json('POST', '/api/ram-install',
                                    self.install_body())
        self.assertTrue(body['success'])
        # the chain finished: the live progress is no longer active, and the
        # step counter kept the last position for the modal
        status, answer = harness.json('GET', '/api/status')
        self.assertFalse(answer['ram_progress']['active'])
        self.assertEqual(answer['ram_progress']['kind'], 'install-cap')
        self.assertEqual(answer['ram_progress']['total'], 4)
        self.assertEqual(answer['ram_progress']['step'], 3)

    def test_a_keyset_the_preset_does_not_define_is_a_400(self):
        card = RamCard()
        state = self.card_state(card)
        status, body = self.served(state).json('POST', '/api/ram-install',
                                               self.install_body(kic='25',
                                                                 kid='25'))
        self.assertEqual(status, 400)
        self.assertIn('not defined in preset', body['error'])
        self.assertEqual(card.envelopes, [])


# ═══════════════════════════════════════════════════════════════════
# /api/ram-install-app
# ═══════════════════════════════════════════════════════════════════

class RamInstallAppTests(RamTestBase):
    def app_body(self, **kwargs):
        body = {'preset_id': self.preset_id(), 'spi2': '01',
                'ram_format': 'compact',
                'loadfile_aid': PKG_AID.hex(), 'module_aid': MODULE_AID.hex()}
        body.update(kwargs)
        return body

    def test_the_install_mode_deletes_the_instance_first(self):
        card = RamCard()
        state = self.card_state(card)
        status, body = self.served(state).json('POST', '/api/ram-install-app',
                                               self.app_body())
        self.assertEqual(status, 200)
        self.assertTrue(body['success'])
        self.assertEqual([step['name'] for step in body['steps']], [
            'DELETE (application)', 'INSTALL [for install]'])
        self.assertEqual(body['load_file_aid'], PKG_AID.hex().upper())
        self.assertEqual(body['module_aid'], MODULE_AID.hex().upper())
        self.assertEqual(body['instance_aid'], MODULE_AID.hex().upper())
        # no LOAD and no NV read: DELETE + INSTALL [for install]
        self.assertEqual(len(card.envelopes), 2)
        self.assertEqual(body['final_cntr'], '0000000003')
        self.assertEqual(capram.reinstall_plan({
            'loadfile_aid': PKG_AID.hex(), 'module_aid': MODULE_AID.hex()})[
                'seq'][1]['apdu'],
            '80E60C00290aA00000030800001000010bA000000308000010000101'
            '0bA000000308000010000101010002C90000')

    def test_the_instance_delete_tolerates_not_found(self):
        # a fresh card has no instance: the DELETE answers 6A88 and the
        # install proceeds
        card = RamCard(answers={1: not_found_answer()})
        state = self.card_state(card)
        status, body = self.served(state).json('POST', '/api/ram-install-app',
                                               self.app_body())
        self.assertEqual(status, 200)
        self.assertTrue(body['success'])
        self.assertTrue(body['steps'][0]['delete_not_found'])

    def test_the_make_selectable_mode(self):
        card = RamCard()
        state = self.card_state(card)
        status, body = self.served(state).json(
            'POST', '/api/ram-install-app',
            self.app_body(mode='make_selectable', privileges='00',
                          instance_aid=MODULE_AID.hex()))
        self.assertEqual(status, 200)
        self.assertTrue(body['success'])
        self.assertEqual([step['name'] for step in body['steps']], [
            'INSTALL [for make selectable]'])
        self.assertEqual(capram.cap_make_selectable_apdu(
            MODULE_AID.hex().upper(), '00'),
            '80E608001200000bA00000030800001000010101000000')

    def test_a_missing_instance_aid_is_a_400(self):
        card = RamCard()
        state = self.card_state(card)
        status, body = self.served(state).json(
            'POST', '/api/ram-install-app',
            {'mode': 'make_selectable', 'preset_id': self.preset_id()})
        self.assertEqual(status, 400)
        self.assertIn('instance_aid', body['error'])

    def test_missing_aids_are_a_400(self):
        card = RamCard()
        state = self.card_state(card)
        harness = self.served(state)
        for body in ({}, {'loadfile_aid': PKG_AID.hex()},
                     {'module_aid': MODULE_AID.hex()}):
            status, answer = harness.json('POST', '/api/ram-install-app',
                                          dict(body,
                                               preset_id=self.preset_id()))
            self.assertEqual(status, 400, body)
            self.assertIn('required', answer['error'])
        self.assertEqual(card.envelopes, [])

    def test_an_unknown_mode_is_a_400(self):
        card = RamCard()
        state = self.card_state(card)
        status, body = self.served(state).json(
            'POST', '/api/ram-install-app', self.app_body(mode='delete'))
        self.assertEqual(status, 400)
        self.assertIn("mode must be 'install' or 'make_selectable'",
                      body['error'])

    def test_a_caller_composed_ef_with_a_bare_ca_is_a_400(self):
        card = RamCard()
        state = self.card_state(card)
        status, body = self.served(state).json(
            'POST', '/api/ram-install-app',
            self.app_body(stk_params='CA0C01'))
        self.assertEqual(status, 400)
        self.assertIn('belongs inside the System Specific Parameters EF',
                      body['error'])
        self.assertEqual(card.envelopes, [])


# ═══════════════════════════════════════════════════════════════════
# The pure builders (simple_server.capram)
# ═══════════════════════════════════════════════════════════════════

class InstallParameterTests(unittest.TestCase):
    """The one-System-Specific-Parameters rule (v3.20.0)."""

    def test_quotas_alone(self):
        self.assertEqual(capram.compose_install_params('', '', nv_quota=0,
                                                       volatile_quota=200),
                         'C900EF08C70200C8C8020000')
        # the 4-byte form above 32767 (GP Table 11-49)
        self.assertEqual(capram.compose_install_params('', '', nv_quota=0x10000),
                         'C900EF06C80400010000')
        # C9 is mandatory even with no parameters at all
        self.assertEqual(capram.compose_install_params('', ''), 'C900')

    def test_a_ca_form_stk_part_shares_the_ef_with_the_quotas(self):
        composed = capram.compose_install_params('', 'EF02CA00', nv_quota=400)
        self.assertEqual(composed, 'C900EF06C8020190CA00')

    def test_an_ea_form_stk_part_is_a_sibling(self):
        # with no quotas and no EF there is no EF object at all: the EA part
        # follows C9 directly (the reference TCA loader's C9 00 EF 00 EA ...)
        self.assertEqual(capram.compose_install_params('', 'EA02CA00'),
                         'C900EA02CA00')
        self.assertEqual(capram.compose_install_params('', 'EA02CA00',
                                                       nv_quota=400),
                         'C900EF04C8020190EA02CA00')

    def test_the_caller_composed_parameters_are_kept(self):
        self.assertEqual(capram.compose_install_params('c900', ''), 'C900')
        self.assertEqual(capram.compose_install_params('C900EF00', 'EA00'),
                         'C900EF00EA00')

    def test_the_refusals(self):
        cases = [
            # an EF-form STK part next to a caller EF would add a second EF
            ('C900EF00', 'EF02CA00', 'second System Specific Parameters'),
            # a bare CA belongs inside the EF
            ('', 'CA0C0102', 'belongs inside the System Specific'),
            # quotas are composed only into an empty install_params
            ('C900', '', 'memory quotas are composed only'),
            # malformed EF forms
            ('', 'EF', 'malformed EF TLV'),
            ('', 'EF80', 'indefinite length'),
            ('', 'EF81', 'truncated length'),
            ('', 'EF03CA00', 'length does not'),
        ]
        for raw, stk, message in cases:
            with self.subTest(raw=raw, stk=stk):
                kwargs = {'nv_quota': 100} if 'quotas' in message else {}
                with self.assertRaises(ValueError) as caught:
                    capram.compose_install_params(raw, stk, **kwargs)
                self.assertIn(message, str(caught.exception))

    def test_a_malformed_caller_install_params_is_refused(self):
        # the tolerant walk swallowed a truncated/indefinite length, so a
        # caller field like `C9 80 EF ...` could hide a second EF; the strict
        # walk must consume every octet (a 400, never a card surprise)
        for raw in ('EF80', 'C980', 'C9', 'C90080', 'C900EF'):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError) as caught:
                    capram.compose_install_params(raw, '')
                self.assertIn('malformed', str(caught.exception))

    def test_a_bad_quota_is_refused(self):
        for value in ('x', 1.5, -1, 0x100000000):
            with self.assertRaises(ValueError):
                capram.compose_install_params('', '', nv_quota=value)
        # an integral float is accepted (a JSON client may send 200.0)
        self.assertEqual(capram.compose_install_params('', '', nv_quota=200.0),
                         'C900EF04C80200C8')


class ApduBuilderTests(unittest.TestCase):
    def test_the_install_for_install_apdu(self):
        self.assertEqual(
            capram.cap_install_apdu('0102030405', '010203040506'),
            '80E60C001A0501020304050601020304050606010203040506010002C90000')
        # P1 0x04 = no make-selectable
        self.assertEqual(
            capram.cap_install_apdu('0102030405', '010203040506',
                                    make_selectable=False)[:8], '80E60400')
        # the instance AID overrides the module AID
        # lv(instance AID) is '08' + the AID; the module AID stays '06 ...'
        self.assertIn('080102030405060708',
                      capram.cap_install_apdu('0102030405', '010203040506',
                                              instance_aid='0102030405060708'))

    def test_the_make_selectable_apdu(self):
        self.assertEqual(capram.cap_make_selectable_apdu('0102030405'),
                         '80E608000C000005010203040501000000')

    def test_the_delete_apdu(self):
        self.assertEqual(capram.ram_delete_apdu('0102030405'),
                         '80E40000074F050102030405')
        self.assertEqual(capram.ram_delete_apdu('0102030405', cascade=True),
                         '80E40080074F050102030405')
        # the AID is cleaned and upper-cased; empty means no APDU
        self.assertEqual(capram.ram_delete_apdu('01 02:03'),
                         '80E40000054F03010203')
        self.assertEqual(capram.ram_delete_apdu(''), '')

    def test_the_security_domain_aid_is_conditional(self):
        default = capram.cap_apdu_sequence('0102030405', '010203040506', 'AA')
        custom = capram.cap_apdu_sequence('0102030405', '010203040506', 'AA',
                                          sd_aid='0909090909')
        # no Security Domain AID: the card defaults to the ISD (P2 '00')
        self.assertEqual(default[0], '80E602000A05010203040500000000')
        self.assertEqual(custom[0],
                         '80E602000F050102030405050909090909000000')

    def test_the_privileges_must_be_one_or_three_bytes(self):
        # GP Table 11-43/11-44: the privileges LV is '01' or '03'; a 2-byte
        # value is an invalid LV the card rejects (a plan-time 400 instead)
        for value in ('0000', '00000000', 'zz'):
            with self.subTest(privileges=value):
                with self.assertRaises(ValueError):
                    capram.cap_install_apdu('0102030405', '010203040506',
                                            privileges=value)
                with self.assertRaises(ValueError):
                    capram.cap_make_selectable_apdu('0102030405', value)
        # 1 and 3 octets (the reference trace's 000000) are accepted
        self.assertIn('03000000',
                      capram.cap_install_apdu('0102030405', '010203040506',
                                              privileges='000000'))

    def test_a_ber_length_beyond_three_octets_is_refused(self):
        # the BER long form has at most three length octets here; the old
        # helper silently emitted an invalid field for >= 0x10000
        self.assertEqual(capram.ber_len(0x10000), '83010000')
        self.assertEqual(capram.ber_len(0xFFFFFF), '83ffffff')
        with self.assertRaises(ValueError):
            capram.ber_len(0x1000000)

    def test_the_install_for_load_keeps_the_gp_chain_order(self):
        sequence = capram.cap_apdu_sequence('0102030405', '010203040506',
                                            'AB' * 600)
        self.assertTrue(sequence[0].startswith('80E602'))
        self.assertTrue(all(apdu.startswith('80E8') for apdu in sequence[1:-1]))
        self.assertTrue(sequence[-1].startswith('80E60C'))
        # the last block carries P1 0x80, the others 0x00, and P2 counts
        self.assertEqual([apdu[4:6] for apdu in sequence[1:-1]],
                         ['00', '00', '80'])
        self.assertEqual([apdu[6:8] for apdu in sequence[1:-1]],
                         ['00', '01', '02'])


class ReferenceTraceTests(unittest.TestCase):
    """The builders reproduce the TCA loader's trace APDUs byte for byte.

    Independent ground truth: the reference trace sends these exact clear
    C-APDUs (the file is not in the repo; the strings are its bytes).  A
    builder change that still matches our own legacy encoder but drifts from
    the reference tool fails here.
    """

    def setUp(self):
        # The trace's load file is a 1049-byte C4 block; only its first 60
        # bytes are needed to pin the first 64-octet LOAD block, so the rest
        # is padded (the C4 length then matches the trace's 0x419).
        prefix = TRACE_FIRST_LOAD[18:]          # after 80E8000040 C4820419
        self.load_file = prefix + '00' * (1049 - len(prefix) // 2)

    def test_the_install_for_load_matches_the_trace(self):
        self.assertEqual(
            capram.cap_apdu_sequence(TRACE_LOADFILE_AID, TRACE_MODULE_AID,
                                     self.load_file, block_size=64)[0],
            TRACE_INSTALL_FOR_LOAD)

    def test_the_first_load_block_matches_the_trace(self):
        sequence = capram.cap_apdu_sequence(TRACE_LOADFILE_AID,
                                            TRACE_MODULE_AID,
                                            self.load_file, block_size=64)
        self.assertEqual(sequence[1], TRACE_FIRST_LOAD)
        # 1049 bytes + the C4 length octets at 64 bytes a block: 17 blocks,
        # the last one flagged P1=0x80 with P2=0x10, as in the trace
        self.assertEqual(len(sequence) - 2, 17)
        self.assertTrue(sequence[-2].startswith('80E88010'))

    def test_the_install_for_install_matches_the_trace(self):
        self.assertEqual(
            capram.cap_install_apdu(
                TRACE_LOADFILE_AID, TRACE_MODULE_AID,
                instance_aid=TRACE_MODULE_AID, privileges='000000',
                install_params=TRACE_INSTALL_PARAMS),
            TRACE_INSTALL_FOR_INSTALL)

    def test_the_composer_omits_the_trace_s_empty_ef(self):
        # The trace's caller-composed parameters carry an empty `EF 00`; when
        # the same EA part is composed from the primitive the empty EF is
        # correctly omitted (the packet stays valid, see the phase doc).
        trace_toolkit = TRACE_INSTALL_PARAMS[len('C900EF00'):]
        composed = capram.compose_install_params('', trace_toolkit)
        self.assertEqual(composed, 'C900' + trace_toolkit)
        apdu = capram.cap_install_apdu(TRACE_LOADFILE_AID, TRACE_MODULE_AID,
                                       instance_aid=TRACE_MODULE_AID,
                                       privileges='000000',
                                       install_params=composed)
        self.assertNotIn('EF00EA', apdu)
        self.assertIn(composed.upper(), apdu)


class StepRecordTests(unittest.TestCase):
    def test_a_successful_step(self):
        por = {'response_status': 'por_ok', 'response_type': 'compact',
               'decoded': {'last_status_word': '9000',
                           'last_response_data': ''}}
        step, error = capram.step_result('LOAD (1/1)', '9000', por, 'abc',
                                         120, 1)
        self.assertIsNone(error)
        self.assertEqual(step['name'], 'LOAD (1/1)')
        self.assertEqual(step['por_status'], 'por_ok')
        self.assertEqual(step['por_sw'], '9000')
        self.assertEqual((step['bytes'], step['segments']), (120, 1))

    def test_a_remote_failure(self):
        por = {'response_status': 'por_ok',
               'decoded': {'last_status_word': '6A88'}}
        step, error = capram.step_result('DELETE (application)', '9000', por,
                                         'abc', 10, 1)
        self.assertEqual(error, 'remote SW 6A88')
        self.assertEqual(step['por_error'], 'remote SW 6A88')

    def test_a_warning_status_word_counts_as_success(self):
        for sw in ('61FF', '6283', '63C2', 'CAFE'):
            por = {'response_status': 'por_ok',
                   'decoded': {'last_status_word': sw}}
            self.assertIsNone(capram.step_result('x', '9000', por, 'a', 1, 1)[1],
                              sw)

    def test_a_low_counter(self):
        por = {'response_status': 'cntr_low', 'decoded': {}}
        step, error = capram.step_result('LOAD (1/1)', '6200', por, 'abc', 1, 1)
        self.assertEqual(error, 'PoR cntr_low')
        self.assertEqual(step['por_status'], 'cntr_low')

    def test_a_bad_format(self):
        por = {'response_status': 'por_ok', 'bad_format': '02',
               'bad_format_name': 'wrong length', 'decoded': {}}
        _step, error = capram.step_result('x', '9000', por, 'abc', 1, 1)
        self.assertEqual(error, 'bad format 02 (wrong length)')

    def test_no_por(self):
        step, error = capram.step_result('x', '9000', None, '', 1, 1)
        self.assertIsNone(error)
        self.assertEqual(step['por_status'], 'no_por')
        step, error = capram.step_result('x', '6A82', None, '', 1, 1)
        self.assertEqual(error, 'PoR undecodable')

    def test_the_expanded_status_word_list(self):
        por = {'response_status': 'por_ok',
               'responses': [{'status_word': '9000'},
                             {'status_word': '6A86'}]}
        self.assertEqual(capram.por_remote_sw(por), '6A86')


class NvFootprintTests(unittest.TestCase):
    def test_the_ff21_decode(self):
        rapdu = bytes.fromhex('FF210B810101820203E8830201F4')
        self.assertEqual(capram.decode_memory(rapdu),
                         {'applets': 1, 'free_nv': 1000,
                          'free_volatile': 500})
        self.assertIsNone(capram.decode_memory(b'\x90\x00'))
        self.assertIsNone(capram.decode_memory(b''))
        self.assertEqual(capram.free_nv(rapdu.hex()), 1000)
        self.assertIsNone(capram.free_nv('zz'))
        self.assertIsNone(capram.free_nv(''))

    def test_the_delta_fields(self):
        self.assertEqual(capram.nv_fields(None, None), {})
        self.assertEqual(capram.nv_fields(1000, 400),
                         {'nv_before': 1000, 'nv_after': 400, 'nv_delta': 600})
        self.assertEqual(capram.nv_fields(400, 1000)['nv_delta'], -600)
        self.assertEqual(capram.nv_fields(1000, None),
                         {'nv_before': 1000, 'nv_after': None, 'nv_delta': None})


class CapParseTests(unittest.TestCase):
    def test_the_component_load_order_is_the_jc_vm_order(self):
        # JC VM Specification v2.2.2 §6.2/Table 6-3 (not a GP rule); the TCA
        # reference trace follows it.  A reorder corrupts every load file.
        self.assertEqual(capram.CAP_LOAD_ORDER, (
            'Header', 'Directory', 'Import', 'Applet', 'Class', 'Method',
            'StaticField', 'Export', 'ConstantPool', 'RefLocation',
            'Descriptor'))

    def test_the_identity_and_the_load_file(self):
        loadfile, module, load = capram.cap_parse(CAP_HEX)
        self.assertEqual(loadfile, PKG_AID.hex().upper())
        self.assertEqual(module, MODULE_AID.hex().upper())
        self.assertEqual(load.upper(), EXPECTED_LOAD.upper())
        self.assertEqual(len(load) // 2, 86)

    def test_a_cap_without_the_import_component(self):
        loadfile, module, load = capram.cap_parse(
            build_cap(with_import=False).hex())
        self.assertEqual(loadfile, PKG_AID.hex().upper())
        self.assertEqual(module, MODULE_AID.hex().upper())
        self.assertNotIn(CAP_PARTS['Import'].hex().upper(), load)
        self.assertEqual(
            load.upper(),
            ''.join(CAP_PARTS[name].hex() for name in
                    ('Header', 'Applet', 'Method')).upper())

    def test_the_refusals(self):
        cases = [('zz', 'not valid hex'), ('', 'empty')]
        for value, message in cases:
            with self.assertRaises(ValueError) as caught:
                capram.cap_parse(value)
            self.assertIn(message, str(caught.exception))
        with self.assertRaises(ValueError) as caught:
            capram.cap_parse('00112233')
        self.assertIn('not a ZIP archive', str(caught.exception))
        with self.assertRaises(ValueError) as caught:
            capram.cap_parse(build_cap(with_applet=False).hex())
        self.assertIn('missing Applet component', str(caught.exception))


class CapInfoBodyTests(unittest.TestCase):
    def test_nothing_uploaded(self):
        with self.assertRaises(ValueError) as caught:
            capram.cap_info_body({})
        self.assertIn('No cap_hex provided', str(caught.exception))

    def test_the_platform_and_etsi_tables_are_capmem_s(self):
        body = capram.cap_info_body({'cap_hex': CAP_HEX})
        self.assertEqual(body['memory']['requires_etsi_release'],
                         capmem.ETSI_TOOLKIT_RELEASE['1.11'])
        self.assertEqual(body['memory']['requires_java_card'],
                         capmem.JC_FRAMEWORK_JDK['1.2'])
