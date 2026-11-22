# coding=utf-8
"""Tests for the pysim-free server's network simulation and monitor (3c-3e-2a).

Three things are exercised through a fake UICC and a real
:class:`ThreadingHTTPServer` on an ephemeral loopback port:

* ``POST /api/net-sim`` - the reused :mod:`pysim_simple_server.netsim`
  scenario runner driven through :mod:`simple_server.netsim`'s card adapter
  (path selection via the file model, the FCP-driven UPDATE/READ, the
  AUTHENTICATE command and the event download);
* ``GET /api/net-state`` / ``POST /api/net-state-refresh`` - the monitor of
  :mod:`simple_server.netmon` (the reused ``netstate`` computation);
* ``GET /api/mcc-mnc`` - the bundled operator list search / random pick.

The ported source is ``pysim_simple_server/server.py``'s ``_netstate_read``/
``_netstate_*`` plumbing and its four ``_mcc_mnc_*`` helpers; the scenario
bytes are the reused legacy builders (a drift guard asserts the adapter
re-exports them, not a copy).  No card and no pysim are involved.
"""

import json
import pathlib
import shutil
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from pysim_simple_server import netstate
from pysim_simple_server import netsim

from simple_server import esim
from simple_server import netmon
from simple_server import netsim as netsim_adapter
from simple_server.commands import FileNotFound, FileOps
from simple_server.files import FileModelError
from simple_server.netsim import NO_FCP_SW, _LchanShim
from simple_server.server import CARD_FREE_GET, ServerState, build_http_server

ATR = bytes.fromhex('3b9f95801fc78031e073fe211b67a3a0001505017204')
AID_USIM = 'A0000000871002'
ICCID = '89493010000000000011'
ICCID_HEX = esim.encode_bcd(ICCID).hex().upper()

PLMN_262_01 = netsim.plmn_bcd('262', '01')
PLMN_250_01 = netsim.plmn_bcd('250', '01')


def tlv(tag, value_hex):
    tag_hex = ('%02X' % tag) if isinstance(tag, int) else str(tag).upper()
    length = len(value_hex) // 2
    encoded = ('%02X' % length if length < 0x80
               else '81%02X' % length if length < 0x100
               else '82%04X' % length)
    return tag_hex + encoded + value_hex


def event_list_command(events=(0x03,)):
    """A SET UP EVENT LIST (type 0x05) subscribing to ``events``."""
    body = (bytes([0x81, 0x03, 0x01, 0x05, 0x00])
            + bytes([0x82, 0x02, 0x83, 0x81])
            + bytes([0x99, len(events)]) + bytes(events))
    return bytes([0xD0, len(body)]) + body


DEFAULT_FILES = {
    '2FE2': {'data': ICCID_HEX},
    '6F07': {'data': '0829051032547698'},               # EF.IMSI
    '6F62': {'data': PLMN_262_01 + '0C02' + 'FFFFFF'},  # EF.HPLMNwAcT
    '6FD9': {'data': 'FFFFFF'},                         # EF.EHPLMN
    '6F7E': {'data': 'FF' * 11},                        # EF.LOCI
    '6F73': {'data': 'FF' * 14},                        # EF.PSLOCI
    '6FE3': {'data': 'FF' * 18},                        # EF.EPSLOCI
    '6FE4': {'structure': 'linear_fixed', 'record_len': 54,
             'records': ['FF' * 54]},                   # EF.EPSNSC
    '6F7B': {'data': 'FF' * 12},                        # EF.FPLMN
    '6F45': {'data': 'FF' * 20},                        # EF.CBMI
    '6F50': {'data': 'FF' * 40},                        # EF.CBMIR
    '6F43': {'data': '0000'},                           # EF.SMSS
    '4F20': {'data': 'FF' * 9},                         # EF.Kc (ADF.USIM)
    '4F52': {'data': 'FF' * 9},                         # EF.KcGPRS
}

#: The DFs the model paths walk through (selected by FID, no FCP needed).
DF_FIDS = ('7F10', '7F20', '5F3B')


class NetSimCard(object):
    """A scripted UICC with a small filesystem and the CAT answers.

    The file table is keyed by FID (the tests' paths use unique FIDs); a
    transparent file answers its FCP size, a linear-fixed one its records.
    ``subscribe`` delivers a SET UP EVENT LIST through the TERMINAL PROFILE's
    chain (so the scenarios' event step runs instead of being skipped).
    """

    def __init__(self, files=None, subscribe=(), iccid=ICCID_HEX):
        self.atr = ATR
        self.reader_name = 'Fake net-sim reader'
        self.protocol = 1
        self.connected = False
        self.sent = []
        self.files = {}
        for fid, spec in DEFAULT_FILES.items():
            self.files[fid] = dict(spec)
        for fid, spec in (files or {}).items():
            self.files[fid] = dict(spec)
        self.files['2FE2']['data'] = iccid
        self.selected = None
        self.fetches = []
        self.trs = []
        self.tp_sw = '9000'
        if subscribe:
            self.tp_sw = '9122'
            self.fetches = [event_list_command(subscribe)]
            self.trs = ['9000']
        self.resets = 0

    # -- the transport shape ------------------------------------------------

    def reset_card(self):
        self.resets += 1
        self.connected = True
        if self.tp_sw != '9000' and not self.fetches:
            self.fetches = [event_list_command((0x03,))]
            self.trs = ['9000']
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
        return self._answer(apdu)

    def sent_hex(self):
        return [apdu.hex().upper() for apdu in self.sent]

    def file_data(self, fid):
        spec = self.files[fid]
        return spec['data'] if spec.get('structure') != 'linear_fixed' \
            else spec['records'][0]

    # -- the card -----------------------------------------------------------

    def _select(self, apdu):
        p1 = apdu[2]
        length = apdu[4]
        value = apdu[5:5 + length].hex().upper()
        if p1 == 0x04:
            if value == AID_USIM:
                return b'', b'\x90\x00'
            return b'', b'\x6A\x82'
        fid = value
        if fid == '3F00' or fid in DF_FIDS:
            self.selected = fid
            return b'', b'\x90\x00'
        if fid not in self.files:
            return b'', b'\x6A\x82'
        self.selected = fid
        return self._fcp(fid), b'\x90\x00'

    def _fcp(self, fid):
        spec = self.files[fid]
        if spec.get('structure') == 'linear_fixed':
            records = spec.get('records') or ['']
            record_len = spec.get('record_len') or len(records[0]) // 2
            descriptor = '4221%04X%02X' % (record_len, len(records))
            size = record_len * len(records)
        else:
            descriptor = '4121'
            size = len(spec.get('data') or '') // 2
        body = (tlv(0x82, descriptor) + tlv(0x80, '%04X' % size)
                + tlv(0x83, fid))
        return bytes.fromhex(tlv(0x62, body))

    def _answer(self, apdu):
        if apdu[:2] == b'\x00\xA4':
            return self._select(apdu)
        if apdu[:2] == b'\x00\xB0':                     # READ BINARY
            spec = self.files.get(self.selected) or {}
            data = spec.get('data') or ''
            offset = int.from_bytes(apdu[2:4], 'big')
            length = apdu[4] or 256
            return bytes.fromhex(data)[offset:offset + length], b'\x90\x00'
        if apdu[:2] == b'\x00\xB2':                     # READ RECORD
            spec = self.files.get(self.selected) or {}
            records = spec.get('records') or []
            index = apdu[2]
            if not 1 <= index <= len(records):
                return b'', b'\x6A\x83'
            return bytes.fromhex(records[index - 1]), b'\x90\x00'
        if apdu[:2] == b'\x00\xD6':                     # UPDATE BINARY
            length = apdu[4]
            self.files[self.selected]['data'] = apdu[5:5 + length].hex().upper()
            return b'', b'\x90\x00'
        if apdu[:2] == b'\x00\xDC':                     # UPDATE RECORD
            index = apdu[2]
            length = apdu[4]
            records = self.files[self.selected].setdefault('records', [])
            while len(records) < index:
                records.append('')
            records[index - 1] = apdu[5:5 + length].hex().upper()
            return b'', b'\x90\x00'
        if apdu[:2] == b'\x00\x88':                     # AUTHENTICATE
            return bytes.fromhex('DB08' + '1122334455667788'), b'\x90\x00'
        if apdu[:2] == b'\x80\x10':                     # TERMINAL PROFILE
            return b'', bytes.fromhex(self.tp_sw)
        if apdu[:2] == b'\x80\x12':                     # FETCH
            raw = self.fetches.pop(0) if self.fetches else b''
            return raw, b'\x90\x00'
        if apdu[:2] == b'\x80\x14':                     # TERMINAL RESPONSE
            sw = self.trs.pop(0) if self.trs else '9000'
            return b'', bytes.fromhex(sw)
        if apdu[:2] == b'\x80\xF2':                     # STATUS
            return b'', b'\x90\x00'
        if apdu[:2] == b'\x80\xC2':                     # ENVELOPE
            return b'', b'\x90\x00'
        return b'', b'\x6A\x82'


class NoFcpCard(NetSimCard):
    """A card whose SELECT returns no FCP (the TS 51 011 §9.2.1 answer).

    A UICC on the ISO form that omits the FCP template is the same case for
    the reader: an FCP-driven read has no size to work from.  ``reads``
    records ``(the selected FID, the READ INS)`` so a test can assert that no
    guessed READ was sent.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.reads = []

    def _select(self, apdu):
        p1 = apdu[2]
        length = apdu[4]
        value = apdu[5:5 + length].hex().upper()
        if p1 != 0x04 and value in self.files:
            self.selected = value
            return b'', b'\x90\x00'
        return super()._select(apdu)

    def _answer(self, apdu):
        if apdu[:2] in (b'\x00\xB0', b'\x00\xB2'):
            self.reads.append((self.selected, apdu[:2].hex().upper()))
        return super()._answer(apdu)


#: A throw-away root for the test stores (never the user's real
#: ``~/.pysim-simple-server`` files).  ``tearDownModule`` removes it.
_STORE_ROOT = pathlib.Path(tempfile.mkdtemp(prefix='simple-netsim-api-'))
_STORE_COUNTER = [0]


def tearDownModule():
    shutil.rmtree(_STORE_ROOT, ignore_errors=True)


def _store_path(name):
    _STORE_COUNTER[0] += 1
    return _STORE_ROOT / ('%s-%d.json' % (name, _STORE_COUNTER[0]))


class FakePresetStore(object):
    path = '<fake>'

    def list(self):
        return []

    def get(self, preset_id):
        return None


def make_state(**kwargs):
    kwargs.setdefault('monitor', False)
    kwargs.setdefault('presets', FakePresetStore())
    kwargs.setdefault('readers_fn', lambda: ['Reader A'])
    kwargs.setdefault('poll_interval', 30)
    kwargs.setdefault('test_scripts_path', _store_path('test_scripts'))
    kwargs.setdefault('test_suites_path', _store_path('test_suites'))
    return ServerState(**kwargs)


class HTTPHarness(object):
    def __init__(self, state):
        self.state = state
        self.server = build_http_server('127.0.0.1', 0, state)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={'poll_interval': 0.05},
            name='test-netsim-api-http', daemon=True)
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
    def setUp(self):
        self.state = None
        self.card = None
        self.http = None

    def tearDown(self):
        if self.http is not None:
            self.http.close()

    def serve(self, card=None, **kwargs):
        card = card if card is not None else NetSimCard()
        self.state = make_state(**kwargs)
        self.state.open(transport=card, start_threads=False)
        self.card = card
        self.http = HTTPHarness(self.state)
        return self.state, card


# ---- the adapter and the reused tables -----------------------------------


class ReuseTests(unittest.TestCase):
    """The scenario bytes are the legacy module's, not a copy."""

    def test_the_shared_tables_are_the_legacy_objects(self):
        self.assertIs(netsim_adapter.EVENT_LOCATION_STATUS,
                      netsim.EVENT_LOCATION_STATUS)
        self.assertIs(netsim_adapter.SCENARIO_SERVICE,
                      netsim.SCENARIO_SERVICE)
        self.assertEqual(netsim_adapter.SCENARIO_SERVICE['attach_eps'],
                         'normal')
        # every scenario the legacy runner offers is reachable
        self.assertEqual(set(netsim.NetSimRunner.SCENARIOS),
                         {'cold_boot', 'attach_eps', 'attach_2g',
                          'service_lost', 'limited_service', 'roaming_denied',
                          'churn', 'sms_received', 'cb_reconfig',
                          'authenticate'})

    def test_the_kc_path_aliases(self):
        # the legacy candidate paths predate the current tree (TS 31.102
        # 4.4.3.1/4.4.3.2, TS 51 011 10.3.3/10.3.32): EF.Kc/KcGPRS sit under
        # ADF.USIM/DF.GSM-ACCESS and the 2G SIM ones are 6F20/6F52
        self.assertEqual(netsim_adapter.PATH_ALIASES['ADF.USIM/4F20'],
                         'ADF.USIM/5F3B/4F20')
        self.assertEqual(netsim_adapter.PATH_ALIASES['DF.GSM/6F08'],
                         'DF.GSM/6F20')

    def test_the_monitor_state_helpers_are_the_reused_module(self):
        state = netstate.new_state()
        netstate.set_service(state, netstate.SERVICE_NORMAL, 'test')
        self.assertEqual(state['service']['state'], 'normal')
        self.assertEqual(netmon.compute(state)['service']['state'], 'normal')


class NetSimAdapterTests(HarnessCase):
    def test_the_kc_write_uses_the_model_paths(self):
        # attach_2g writes EF.Kc through the aliased path: the adapter must
        # reach ADF.USIM/5F3B/4F20 (the legacy path only) and succeed
        _state, card = self.serve()
        status, body = self.http.post('/api/net-sim',
                                      {'scenario': 'attach_2g',
                                       'mcc': '262', 'mnc': '01',
                                       'lac': '6CD7', 'rac': '01'})
        self.assertEqual(status, 200, body)
        self.assertTrue(body['success'], body)
        steps = body['steps']
        kc = [step for step in steps if step.get('file') == 'kc']
        self.assertTrue(kc, steps)
        self.assertEqual(kc[0].get('sw'), '9000')
        # the legacy path was redirected to the model's DF.GSM-ACCESS entry
        self.assertEqual(kc[0].get('path'), 'ADF.USIM/4F20')
        self.assertTrue(any(apdu.startswith('00A40004025F3B')
                            for apdu in card.sent_hex()))

    def test_a_card_without_an_fcp_is_a_failed_read_not_an_error(self):
        # A 2G SIM (or a UICC omitting the FCP) gives the reader no size: the
        # FCP-driven read cannot run.  The adapter reports a failed read
        # (NO_FCP_SW) instead of raising, so the scenario still completes with
        # the runner's own fallbacks - and no READ is guessed.
        _state, card = self.serve(NoFcpCard())
        status, body = self.http.post('/api/net-sim', {
            'scenario': 'roaming_denied', 'send_event': False,
            'mcc': '262', 'mnc': '01'})
        self.assertEqual(status, 200, body)
        self.assertTrue(body['success'], body)
        self.assertIsNone(body['error'])
        fplmn = [step for step in body['steps'] if step.get('file') == 'fplmn']
        self.assertTrue(fplmn, body['steps'])
        self.assertEqual(fplmn[0].get('sw'), '9000')
        # the EF.FPLMN read was skipped, not sent with a guessed length
        self.assertNotIn(('6F7B', '00B0'), card.reads)


class FcpLessReadTests(unittest.TestCase):
    """The shim's reads on a selected file whose FCP carries no size.

    A real :class:`FileOps` over a fake transport answering the TS 51 011
    SELECT form (``P2 = 00``, no Le, no FCP) - :meth:`FileOps.read_file` has no
    length and raises, which used to abort the whole scenario.
    """

    class Transport(object):
        def __init__(self):
            self.sent = []

        def send_apdu(self, apdu):
            self.sent.append(bytes(apdu))
            return b'', b'\x90\x00'

    class Model(object):
        def path_chain(self, path):
            return ['3F00', '6F07']

    def setUp(self):
        self.transport = self.Transport()
        self.ops = FileOps(self.transport, cla='a0', sel_fcp=False)
        self.lchan = _LchanShim(self.ops, self.Model())
        self.lchan.select('DF.GSM/6F07')

    def _read_ins(self):
        return [apdu[1] for apdu in self.transport.sent
                if apdu[1] in (0xB0, 0xB2)]

    def test_a_transparent_read_is_no_fcp_sw(self):
        self.assertEqual(self.lchan.read_binary(), ('', NO_FCP_SW))
        # the only APDUs were the SELECTs: no READ BINARY with a guessed length
        self.assertEqual(self._read_ins(), [])
        self.assertEqual(self.lchan.selected_file_size(), None)

    def test_a_record_read_is_no_fcp_sw(self):
        self.assertEqual(self.lchan.read_record(1), ('', NO_FCP_SW))
        self.assertEqual(self._read_ins(), [])


# ---- the scenarios --------------------------------------------------------


class NetSimTests(HarnessCase):
    def test_the_attach_scenario(self):
        _state, card = self.serve()
        status, body = self.http.post('/api/net-sim', {
            'scenario': 'attach_eps', 'mcc': '262', 'mnc': '01',
            'lac': '6CD7', 'cell_id': '1234',
            'tmsi': '11223344', 'ptmsi': '55667788',
            'ptmsi_sig': 'AABBCC', 'guti': '0102030405060708090A0B0C',
            'send_event': False})
        self.assertEqual(status, 200, body)
        self.assertTrue(body['success'], body)
        self.assertEqual(body['scenario'], 'attach_eps')
        actions = [step['action'] for step in body['steps']]
        self.assertIn('update_record', actions)          # EF.EPSNSC
        self.assertEqual(actions.count('update_binary') >= 3, True)
        # the EPS location carries the GUTI and the requested TAI
        epsloci = card.file_data('6FE3')
        self.assertTrue(epsloci.startswith('0102030405060708090A0B0C'))
        self.assertIn(PLMN_262_01, epsloci)
        self.assertIn('6CD7', epsloci)
        monitor = body['net_state']
        self.assertEqual(monitor['service']['state'], 'normal')
        self.assertEqual(monitor['service']['source'], 'net-sim:attach_eps')
        location = monitor['network']['location']
        self.assertEqual(location['plmn'], '26201')
        self.assertEqual(location['mcc'], '262')
        self.assertEqual(location['mnc'], '01')
        self.assertEqual(location['area'], 'TAI')
        self.assertEqual(location['roaming'], 'home')
        self.assertEqual(location['country'], 'Germany')
        self.assertEqual(location['operator'], 'Telekom')
        self.assertEqual(monitor['network']['home']['plmn'], '26201')

    def test_a_guest_network_is_not_roaming_home(self):
        _state, _card = self.serve()
        status, body = self.http.post('/api/net-sim', {
            'scenario': 'attach_eps', 'mcc': '250', 'mnc': '01',
            'lac': '0001', 'send_event': False})
        self.assertEqual(status, 200, body)
        self.assertTrue(body['success'], body)
        location = body['net_state']['network']['location']
        self.assertEqual(location['plmn'], '25001')
        self.assertEqual(location['roaming'], 'guest')
        self.assertEqual(location['country'], 'Russian Federation')

    def test_the_service_loss_scenario_sends_the_event(self):
        card = NetSimCard(subscribe=(0x03,))
        _state, card = self.serve(card)
        status, body = self.http.post('/api/net-sim', {
            'scenario': 'service_lost', 'send_event': True,
            'invalidate_epsnsc': True, 'dummy_locations': True,
            'write_kc': True})
        self.assertEqual(status, 200, body)
        self.assertTrue(body['success'], body)
        events = [step for step in body['steps']
                  if step.get('action') == 'event']
        self.assertTrue(events, body['steps'])
        self.assertEqual(events[0]['data'], '9B0102')     # no service
        self.assertEqual(body['net_state']['service']['state'], 'none')
        # the event really went out as an ENVELOPE
        self.assertTrue(any(apdu.startswith('80C2')
                            for apdu in card.sent_hex()))

    def test_an_unsubscribed_event_is_a_skip_step(self):
        self.serve()                                     # no event list
        status, body = self.http.post('/api/net-sim',
                                      {'scenario': 'service_lost'})
        self.assertEqual(status, 200, body)
        self.assertTrue(body['success'], body)
        self.assertTrue(any(step.get('action') == 'skip'
                            and step.get('file') == 'location_status'
                            for step in body['steps']))
        self.assertEqual(body['net_state']['service']['state'], 'none')

    def test_the_authenticate_scenario(self):
        self.serve()
        status, body = self.http.post('/api/net-sim', {
            'scenario': 'authenticate',
            'rand': '00' * 16, 'autn': '11' * 16})
        self.assertEqual(status, 200, body)
        self.assertTrue(body['success'], body)
        step = [s for s in body['steps'] if s.get('action') == 'authenticate']
        self.assertTrue(step)
        self.assertEqual(step[0]['parsed']['type'], 'success')
        self.assertEqual(step[0]['sw'], '9000')

    def test_the_churn_scenario_writes_the_security_context(self):
        _state, card = self.serve()
        status, body = self.http.post('/api/net-sim', {
            'scenario': 'churn', 'churn_count': 2, 'churn_delay_ms': 0,
            'kasme': 'AA' * 32})
        self.assertEqual(status, 200, body)
        self.assertTrue(body['success'], body)
        writes = [s for s in body['steps']
                  if s.get('action') == 'update_record']
        self.assertEqual(len(writes), 4)
        self.assertTrue(all(s['sw'] == '9000' for s in writes))

    def test_the_cb_reconfiguration(self):
        _state, card = self.serve()
        status, body = self.http.post('/api/net-sim',
                                      {'scenario': 'cb_reconfig',
                                       'cbmi_ids': [0x111F, 0x1112]})
        self.assertEqual(status, 200, body)
        self.assertTrue(body['success'], body)
        self.assertTrue(card.file_data('6F45').startswith('111F'))
        self.assertTrue(card.file_data('6F50').startswith('111F111F'))

    def test_a_bad_request(self):
        self.serve()
        status, body = self.http.post('/api/net-sim', {})
        self.assertEqual(status, 400)
        self.assertEqual(body['error'], 'scenario is required')
        status, body = self.http.post('/api/net-sim', {'scenario': 'nope'})
        self.assertEqual(status, 400)
        self.assertIn('unknown scenario', body['error'])

    def test_no_card_is_a_503(self):
        self.state = make_state()
        self.http = HTTPHarness(self.state)
        status, body = self.http.post('/api/net-sim',
                                      {'scenario': 'attach_eps'})
        self.assertEqual(status, 503)
        self.assertIn('no card session', body['error'])


# ---- the monitor ----------------------------------------------------------


class NetStateTests(HarnessCase):
    def test_the_equip_read_populates_the_monitor(self):
        state, _card = self.serve()
        self.assertIsNotNone(state.netstate)
        status, body = self.http.get('/api/net-state')
        self.assertEqual(status, 200)
        self.assertTrue(body['available'])
        files = body['state']['files']
        self.assertTrue(files['imsi']['present'])
        self.assertEqual(files['imsi']['kind'], 'transparent')
        self.assertEqual(files['imsi']['data'], '0829051032547698')
        self.assertEqual(files['imsi']['source'], 'init')
        self.assertTrue(files['epsnsc']['present'])
        self.assertEqual(files['epsnsc']['kind'], 'record')
        self.assertEqual(len(files['epsnsc']['records']), 1)
        self.assertEqual(files['epsnsc']['records'][0]['num'], 1)
        # a file the card does not have is reported absent, not an error
        self.assertFalse(files['spdi']['present'])
        self.assertIsNone(files['spdi']['kind'])
        self.assertIsNone(body['state']['network']['location'])
        self.assertEqual(body['state']['network']['home']['plmn'], '26201')

    def test_the_refresh_rereads_the_filtered_files(self):
        _state, card = self.serve()
        card.files['6F07']['data'] = '0829851032547698'
        status, body = self.http.post('/api/net-state-refresh',
                                      {'files': ['imsi']})
        self.assertEqual(status, 200, body)
        files = body['state']['files']
        self.assertEqual(files['imsi']['data'], '0829851032547698')
        self.assertEqual(files['imsi']['source'], 'refresh')
        # the other cached entries are untouched
        self.assertTrue(files['epsnsc']['present'])

    def test_a_refresh_without_a_filter_reads_everything(self):
        _state, _card = self.serve()
        status, body = self.http.post('/api/net-state-refresh', {})
        self.assertEqual(status, 200, body)
        self.assertEqual(len(body['state']['files']), len(netstate.FILE_DEFS))

    def test_a_bad_files_filter(self):
        self.serve()
        status, body = self.http.post('/api/net-state-refresh',
                                      {'files': 'imsi'})
        self.assertEqual(status, 400)
        self.assertIn('list of file keys', body['error'])

    def test_no_card_is_a_503_and_no_state_is_unavailable(self):
        self.state = make_state()
        self.http = HTTPHarness(self.state)
        status, body = self.http.get('/api/net-state')
        self.assertEqual(status, 200)
        self.assertEqual(body, {'available': False, 'state': None})
        status, body = self.http.post('/api/net-state-refresh', {})
        self.assertEqual(status, 503)

    def test_the_cached_reads_bypass_the_card_lock(self):
        # the monitor and the operator list are cached GETs: the Phone tab
        # polls them while a long card operation holds the lock
        self.assertIn('/api/net-state', CARD_FREE_GET)
        self.assertIn('/api/mcc-mnc', CARD_FREE_GET)
        self.assertNotIn('/api/esim/chip', CARD_FREE_GET)

    def test_the_event_patches_the_monitor(self):
        self.serve()
        status, body = self.http.post('/api/event-send',
                                      {'event_type': 3,
                                       'event_data': '9B0101'})
        self.assertEqual(status, 200, body)
        self.assertEqual(body['net_state']['service']['state'], 'limited')
        self.assertEqual(body['net_state']['service']['source'],
                         'event:location-status')
        status, body = self.http.post('/api/event-send',
                                      {'event_type': 3,
                                       'event_data': '9B0102'})
        self.assertEqual(status, 200, body)
        self.assertEqual(body['net_state']['service']['state'], 'none')
        # a non-location event leaves the simulated service alone
        status, body = self.http.post('/api/event-send',
                                      {'event_type': 0x0B,
                                       'event_data': '01'})
        self.assertEqual(status, 200, body)
        self.assertEqual(body['net_state']['service']['state'], 'none')

    def test_a_non_location_event_issues_no_read(self):
        # The legacy gate: only a Location status event patches the monitor.
        # Any other event must not re-read EF.IMSI (three APDUs) nor flip the
        # entry's source - the monitor describes the card's own state.
        state, card = self.serve()
        self.assertEqual(state.netstate['files']['imsi']['source'], 'init')
        before = len(card.sent)
        status, body = self.http.post('/api/event-send',
                                      {'event_type': 0x0B,
                                       'event_data': '01'})
        self.assertEqual(status, 200, body)
        new = card.sent_hex()[before:]
        self.assertFalse([apdu for apdu in new
                          if apdu.startswith('00A40004')
                          and apdu[10:14] == '6F07'], new)
        self.assertFalse([apdu for apdu in new if apdu.startswith('00B0')], new)
        self.assertEqual(state.netstate['files']['imsi']['source'], 'init')
        self.assertEqual(body['net_state']['files']['imsi']['source'], 'init')
        # the Location status event does read it (the gate is one-sided only)
        status, body = self.http.post('/api/event-send',
                                      {'event_type': 3,
                                       'event_data': '9B0100'})
        self.assertEqual(status, 200, body)
        self.assertEqual(state.netstate['files']['imsi']['source'], 'read')


class NetStateReadTests(unittest.TestCase):
    """The read helper's candidate order and absent-file handling."""

    class Model(object):
        def path_chain(self, path):
            return [path]

    class Ops(object):
        def __init__(self, table):
            self.table = table
            self.selected = None

        def select_path(self, chain):
            path = chain[0]
            if path not in self.table:
                raise FileNotFound('path', '6a82')
            self.selected = path
            structure = ('linear_fixed'
                         if isinstance(self.table[path], list) else
                         'transparent')
            from types import SimpleNamespace
            return SimpleNamespace(structure=structure,
                                   num_of_rec=len(self.table[path])
                                   if structure == 'linear_fixed' else None,
                                   record_len=2 if structure ==
                                   'linear_fixed' else None,
                                   file_size=len(self.table[path][0]) // 2
                                   if structure == 'linear_fixed' else
                                   len(self.table[path]) // 2)

        def read_file(self, fcp, rec=None):
            value = self.table[self.selected]
            if rec is not None:
                value = value[rec - 1]
            return bytes.fromhex(value), '9000'

    def test_the_first_readable_candidate_wins(self):
        ops = self.Ops({'ADF.USIM/6FE4': ['AABB', 'CCDD'],
                        'DF.GSM/6F07': '0829AA'})
        out = netmon.read_files(ops, self.Model(), ['epsnsc', 'imsi'])
        self.assertTrue(out['epsnsc']['present'])
        self.assertEqual(out['epsnsc']['path'], 'ADF.USIM/6FE4')
        self.assertEqual(out['epsnsc']['records'],
                         [{'num': 1, 'data': 'AABB'},
                          {'num': 2, 'data': 'CCDD'}])
        self.assertTrue(out['imsi']['present'])
        self.assertEqual(out['imsi']['path'], 'DF.GSM/6F07')

    def test_a_file_without_a_readable_candidate_is_absent(self):
        out = netmon.read_files(self.Ops({}), self.Model(), ['imsi'])
        self.assertFalse(out['imsi']['present'])
        self.assertIsNone(out['imsi']['kind'])
        self.assertEqual(out['imsi']['fid'], '6F07')

    def test_a_model_unknown_candidate_falls_through(self):
        # The read is driven by the file model (the documented difference from
        # the legacy runtime channel): a candidate path the model does not
        # know raises during resolution and is skipped like an absent file.
        class Model(object):
            def path_chain(self, path):
                if path.startswith('ADF.USIM'):
                    raise FileModelError('unknown path %r' % path)
                return [path]

        ops = self.Ops({'DF.GSM/6F07': '0829AA'})
        out = netmon.read_files(ops, Model(), ['imsi'])
        self.assertTrue(out['imsi']['present'])
        self.assertEqual(out['imsi']['path'], 'DF.GSM/6F07')
        # every candidate unknown -> absent, not an error
        out = netmon.read_files(self.Ops({}), Model(), ['ehplmn'])
        self.assertFalse(out['ehplmn']['present'])


# ---- the operator list ----------------------------------------------------


class MccMncSearchTests(unittest.TestCase):
    DATA = [
        {'countryName': 'Germany', 'countryCode': 'de', 'mcc': '262',
         'mnc': '01', 'brand': 'Telekom', 'operator': 'Telekom Deutschland',
         'bands': 'GSM 900', 'status': 'Operational'},
        {'countryName': 'Germany', 'countryCode': 'de', 'mcc': '262',
         'mnc': '02', 'brand': 'Vodafone', 'operator': 'Vodafone GmbH',
         'bands': 'LTE', 'status': 'Operational'},
        {'countryName': 'Germany', 'countryCode': 'de', 'mcc': '262',
         'mnc': '99', 'brand': 'MVNO One', 'operator': 'Reseller',
         'bands': 'MVNO', 'status': 'Operational'},
    ]

    def test_search_hides_the_mvnos(self):
        results = netmon.search(self.DATA, 'Telekom')
        self.assertEqual([row['mnc'] for row in results], ['01'])
        self.assertEqual(results[0]['countryName'], 'Germany')
        # an MVNO-only brand is not findable either
        self.assertEqual(netmon.search(self.DATA, 'MVNO'), [])
        self.assertEqual(netmon.search(self.DATA, ''), [])
        # the projection drops the bands field (the API's row shape)
        self.assertNotIn('bands', results[0])

    def test_the_random_pick_skips_the_excluded_plmn_and_mvnos(self):
        picked = netmon.random_entry(self.DATA, '26201')
        self.assertIsNotNone(picked)
        self.assertNotEqual(picked['mcc'] + picked['mnc'], '26201')
        self.assertNotEqual(picked['mnc'], '99')
        self.assertIsNone(netmon.random_entry([], None))


class MccMncEndpointTests(HarnessCase):
    def test_the_search_endpoint(self):
        self.serve()
        status, body = self.http.get('/api/mcc-mnc?q=Telekom')
        self.assertEqual(status, 200)
        self.assertTrue(body['available'])
        self.assertGreater(body['count'], 0)
        self.assertIn('26201',
                      [row['mcc'] + row['mnc'] for row in body['results']])
        self.assertNotIn('bands', body['results'][0])

    def test_an_empty_query_has_no_results(self):
        self.serve()
        status, body = self.http.get('/api/mcc-mnc?q=')
        self.assertEqual(status, 200)
        self.assertEqual(body['results'], [])

    def test_the_random_pick(self):
        self.serve()
        status, body = self.http.get('/api/mcc-mnc?random=1&exclude=26201')
        self.assertEqual(status, 200)
        self.assertTrue(body['available'])
        picked = body['result']
        self.assertEqual(len(picked['mcc']), 3)
        self.assertIn(len(picked['mnc']), (2, 3))
        self.assertNotEqual(picked['mcc'] + picked['mnc'], '26201')
        status, body = self.http.get('/api/mcc-mnc?random=0')
        self.assertEqual(body['results'], [])

    def test_a_missing_list_is_unavailable(self):
        self.state = make_state()
        self.http = HTTPHarness(self.state)
        self.state.mcc_mnc_path = str(_STORE_ROOT / 'absent.json')
        netmon._reset_cache()
        try:
            with self.assertLogs('simple_server.netmon', 'WARNING'):
                status, body = self.http.get('/api/mcc-mnc?q=Telekom')
        finally:
            netmon._reset_cache()
        self.assertEqual(status, 200)
        self.assertEqual(body, {'available': False})


class MccMncAssetTests(unittest.TestCase):
    def test_the_default_list_is_the_bundled_asset(self):
        path = netmon.default_mcc_mnc_path()
        self.assertTrue(path.endswith('mcc-mnc-list.json'))
        data = netmon.load_mcc_mnc(path)
        self.assertIsInstance(data, list)
        self.assertGreater(len(data), 1000)
        # the bundled list carries MVNO entries, which the picker hides
        self.assertTrue(any(netmon.is_mvno(entry) for entry in data))
        self.assertFalse(any(netmon.is_mvno(entry)
                             for entry in netmon.search(data, 'Telekom', 50)))


if __name__ == '__main__':
    unittest.main()
