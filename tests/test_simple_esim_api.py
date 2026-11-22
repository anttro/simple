# coding=utf-8
"""Tests for the pysim-free server's eSIM/LPA endpoints (Phase 3c-3e-2a).

The endpoints ride :mod:`simple_server.esim` (the ES10 TLV layer of Phase 3a
and its card-level flow) on the ported :class:`ServerState`, so the tests
drive a scripted eUICC through a fake transport: SELECT/READ BINARY for the
init reads and the STORE DATA exchanges of the ES10 functions, plus the
FETCH/TERMINAL RESPONSE chain of a profile switch's REFRESH.  The HTTP
endpoints run through a real :class:`ThreadingHTTPServer` on an ephemeral
loopback port.  No card and no pysim are involved.

The legacy reference is ``pysim_simple_server/server.py``'s
``/api/esim/chip|profiles|notifications|profile`` handlers and
``pysim_simple_server/esim.py``'s two-phase switch (``build_switch_apdu`` -
``_esim_refresh_chain`` - ``_esim_reinit`` - ``_esim_verify_switch``); the
decoder parity lives in ``tests/test_simple_drift.py``.

Spec anchors: SGP.22 v2.6 §5.7.2 (the ES10 STORE DATA exchange), §5.7.8
(EUICCInfo1/2), §5.7.9 (ListNotification), §5.7.22 (GetRAT), §5.7.15
(GetProfilesInfo), §5.7.16/§5.7.17 (Enable/DisableProfile; the "result OK
before REFRESH" / REFRESH / reset are §3.2.1 steps 6-8 and §3.2.2 steps 5-7),
§5.7.20 (GetEuiccData/EID).
"""

import json
import pathlib
import shutil
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from simple_server import esim
from simple_server.server import ServerState, build_http_server

ATR = bytes.fromhex('3b9f95801fc78031e073fe211b67a3a0001505017204')

AID_USIM = 'A0000000871002'
AID_ISD_R = esim.AID_ISD_R
EID = '89049032000000000000000000000001'

ICCID_A = '89493010000000000011'
ICCID_B = '89493010000000000012'
AID_A = 'A0000005591010FFFFFFFF8900000100'
AID_B = 'A0000005591010FFFFFFFF8900000200'


# ---- fixture builders (explicit BER-TLVs, the legacy tests' shape) ---------

def tlv(tag, value_hex):
    """One BER-TLV: the tag is an int or a hex string, short/long length form."""
    tag_hex = ('%02X' % tag) if isinstance(tag, int) else str(tag).upper()
    assert len(value_hex) % 2 == 0
    length = len(value_hex) // 2
    if length < 0x80:
        encoded = '%02X' % length
    elif length < 0x100:
        encoded = '81%02X' % length
    else:
        encoded = '82%04X' % length
    return tag_hex + encoded + value_hex


def ascii_hex(text):
    return text.encode('utf-8').hex().upper()


def bcd(iccid):
    return esim.encode_bcd(iccid).hex().upper()


def info1_response():
    # SVN 1.2.3 and one verification SKI (04 inside A9)
    return tlv('BF20', tlv(0x82, '010203') + tlv(0xA9, tlv(0x04, '010203')))


def info2_response():
    # profile version, SVN, firmware, the UICC capability bit string, category
    return tlv('BF22',
               tlv(0x81, '020200') + tlv(0x82, '030400')
               + tlv(0x83, '010000')
               + tlv(0x85, '077F3E1F80')
               + tlv(0x8B, '02'))


def addresses_response():
    return tlv('BF3C', tlv(0x80, ascii_hex('smdp.example.org'))
               + tlv(0x81, ''))


def rat_response():
    # one rule: ppr1+ppr2 (bit string 04 60, no operators, no flags)
    return tlv('BF43', tlv(0xA0, tlv(0x30, tlv(0x80, '0460'))))


def eid_response(eid=EID):
    return tlv('BF3E', tlv(0x5A, eid))


def profile_info_tlv(profile):
    body = tlv(0x5A, bcd(profile['iccid']))
    body += tlv(0x4F, profile.get('aid') or AID_A)
    state = profile.get('state')
    if state:
        body += tlv('9F70', '01' if state == 'enabled' else '00')
    if profile.get('nickname'):
        body += tlv(0x90, ascii_hex(profile['nickname']))
    if profile.get('provider'):
        body += tlv(0x91, ascii_hex(profile['provider']))
    if profile.get('name'):
        body += tlv(0x92, ascii_hex(profile['name']))
    icon_type = profile.get('icon_type')
    if icon_type:
        body += tlv(0x93, '01' if icon_type == 'png' else '00')
    if profile.get('icon'):
        body += tlv(0x94, profile['icon'])
    cls = profile.get('cls')
    if cls:
        body += tlv(0x95, {'test': '00', 'provisioning': '01',
                           'operational': '02'}[cls])
    if profile.get('owner'):
        body += tlv(0xB7, tlv(0x80, esim.encode_plmn(profile['owner']).hex()))
    return tlv(0xE3, body)


def profiles_response(profiles):
    return tlv('BF2D', tlv(0xA0,
                           ''.join(profile_info_tlv(p) for p in profiles)))


def profiles_error_response():
    return tlv('BF2D', tlv(0x81, '7F'))


def notifications_response(items):
    metas = ''
    for item in items:
        flags = {'install': 0x80, 'enable': 0x40, 'disable': 0x20,
                 'delete': 0x10}
        mask = 0
        for operation in item['operations']:
            mask |= flags[operation]
        body = tlv(0x80, '%02X' % item['seq'])
        body += tlv(0x81, '00%02X' % mask)
        body += tlv(0x0C, ascii_hex(item['address']))
        body += tlv(0x5A, bcd(item['iccid']))
        metas += tlv('BF2F', body)
    return tlv('BF28', tlv(0xA0, metas))


def switch_response(action, result='ok'):
    tag = 'BF31' if action == 'enable' else 'BF32'
    code = {'ok': '00', 'iccidOrAidNotFound': '01',
            'profileNotInDisabledState': '02', 'catBusy': '05',
            'undefinedError': '7F'}[result]
    return tlv(tag, tlv(0x80, code))


def refresh_command(cmd_num=1):
    """A minimal REFRESH (type 0x01) proactive command (D0 wrapped)."""
    body = (bytes([0x81, 0x03, cmd_num, 0x01, 0x00])
            + bytes([0x82, 0x02, 0x83, 0x81]))
    return bytes([0xD0, len(body)]) + body


class EsimCard(object):
    """A scripted eUICC: the init reads, then the ES10 dispatch.

    ``profiles`` is a list of dicts (the fixture shape); the card renders the
    GetProfilesInfo answer from it and applies an Enable/Disable request by
    flipping the target's state (a card has one enabled profile).  The
    switch answer is ``switch_answer``: a result object for ``9000`` or a
    status word (``91XX`` = the REFRESH is pending, which the test queues in
    ``fetches``).
    """

    def __init__(self, euicc=True, profiles=None, notifications=None,
                 switch_sw='9000', switch_result='ok'):
        self.atr = ATR
        self.reader_name = 'Fake eUICC reader'
        self.protocol = 1
        self.euicc = euicc
        self.profiles = [dict(p) for p in (profiles or [])]
        self.notifications = list(notifications or [])
        self.switch_sw = switch_sw
        self.switch_result = switch_result
        self.fetches = []
        self.trs = []
        self.sent = []
        self.connected = False
        self.resets = 0
        self.iccid = esim.encode_bcd(ICCID_A)
        self.envelope_sw = '9000'

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
        return self._answer(apdu)

    def sent_hex(self):
        return [apdu.hex().upper() for apdu in self.sent]

    def store_data_tlvs(self):
        """The data object of every ES10 STORE DATA the card received."""
        out = []
        for apdu in self.sent_hex():
            if apdu.startswith('80E29100'):
                length = int(apdu[8:10], 16)
                out.append(apdu[10:10 + length * 2])
        return out

    # -- the card -----------------------------------------------------------

    def _answer(self, apdu):
        if apdu[:1] == b'\x00' and apdu[1:2] == b'\xA4':
            return self._select(apdu)
        if apdu[:2] == b'\x00\xB0':                     # READ BINARY
            return self._read_binary(apdu)
        if apdu[:2] == b'\x80\x10':                     # TERMINAL PROFILE
            return b'', b'\x90\x00'
        if apdu[:2] == b'\x80\xF2':                     # STATUS
            return b'', b'\x90\x00'
        if apdu[:2] == b'\x80\x12':                     # FETCH
            raw = self.fetches.pop(0) if self.fetches else b''
            return raw, b'\x90\x00'
        if apdu[:2] == b'\x80\x14':                     # TERMINAL RESPONSE
            sw = self.trs.pop(0) if self.trs else '9000'
            return b'', bytes.fromhex(sw)
        if apdu[:2] == b'\x80\xC2':                     # ENVELOPE
            return b'', bytes.fromhex(self.envelope_sw)
        if apdu[:2] == b'\x80\xE2':                     # STORE DATA
            return self._store_data(apdu)
        return b'', b'\x6A\x82'

    def _select(self, apdu):
        p1 = apdu[2]
        length = apdu[4]
        value = apdu[5:5 + length].hex().upper()
        if p1 == 0x04:                                  # by DF name (AID)
            if value == AID_USIM:
                return b'', b'\x90\x00'
            if value == AID_ISD_R and self.euicc:
                return b'', b'\x90\x00'
            return b'', b'\x6A\x82'
        if value in ('3F00', '2FE2'):
            return b'', b'\x90\x00'
        return b'', b'\x6A\x82'

    def _read_binary(self, apdu):
        length = apdu[4] or 256
        offset = int.from_bytes(apdu[2:4], 'big')
        return self.iccid[offset:offset + length], b'\x90\x00'

    def _store_data(self, apdu):
        p1 = apdu[2]
        length = apdu[4]
        if p1 != 0x91:
            return b'', b'\x6A\x86'
        data = apdu[5:5 + length].hex().upper()
        tag = data[:4]
        if tag == 'BF3E':
            return bytes.fromhex(eid_response()), b'\x90\x00'
        if tag == 'BF20':
            return bytes.fromhex(info1_response()), b'\x90\x00'
        if tag == 'BF22':
            return bytes.fromhex(info2_response()), b'\x90\x00'
        if tag == 'BF3C':
            return bytes.fromhex(addresses_response()), b'\x90\x00'
        if tag == 'BF43':
            return bytes.fromhex(rat_response()), b'\x90\x00'
        if tag == 'BF2D':
            return bytes.fromhex(profiles_response(self.profiles)), b'\x90\x00'
        if tag == 'BF28':
            return (bytes.fromhex(notifications_response(self.notifications)),
                    b'\x90\x00')
        if tag in ('BF31', 'BF32'):
            action = 'enable' if tag == 'BF31' else 'disable'
            if self.switch_sw != '9000':
                # 91XX = the spec's "result OK before REFRESH": the switch
                # completes on the TERMINAL RESPONSE / the following reset.
                if self.switch_sw.startswith('91'):
                    self._apply_switch(action, data)
                return b'', bytes.fromhex(self.switch_sw)
            if self.switch_result == 'ok':
                self._apply_switch(action, data)
            return (bytes.fromhex(switch_response(action,
                                                  self.switch_result)),
                    b'\x90\x00')
        return b'', b'\x6A\x88'

    def _apply_switch(self, action, data):
        """Apply Enable/DisableProfile: the target takes the wanted state."""
        from simple_server.tlv import ber_tlv_list
        inner = (dict(ber_tlv_list(bytes.fromhex(data)))
                 .get(0xBF31) or dict(ber_tlv_list(bytes.fromhex(data)))
                 .get(0xBF32))
        ident = dict(ber_tlv_list(inner)).get(0xA0) or b''
        children = dict(ber_tlv_list(ident))
        iccid = (esim.decode_bcd(children[0x5A])
                 if 0x5A in children and children[0x5A] else None)
        aid = children[0x4F].hex().upper() if 0x4F in children else None
        for profile in self.profiles:
            match = ((iccid and profile['iccid'] == iccid)
                     or (aid and (profile.get('aid') or '').upper() == aid))
            if not match:
                continue
            profile['state'] = 'enabled' if action == 'enable' else 'disabled'
            if action == 'enable':
                for other in self.profiles:
                    if other is not profile:
                        other['state'] = 'disabled'


#: A throw-away root for the test stores (never the user's real
#: ``~/.pysim-simple-server`` files).  ``tearDownModule`` removes it.
_STORE_ROOT = pathlib.Path(tempfile.mkdtemp(prefix='simple-esim-api-'))
_STORE_COUNTER = [0]


def tearDownModule():
    shutil.rmtree(_STORE_ROOT, ignore_errors=True)


def _store_path(name):
    _STORE_COUNTER[0] += 1
    return _STORE_ROOT / ('%s-%d.json' % (name, _STORE_COUNTER[0]))


def _mcc_mnc_path():
    """An empty operator list (the eSIM tests never use the monitor)."""
    path = _STORE_ROOT / 'mcc-mnc.json'
    if not path.exists():
        path.write_text('[]', encoding='utf-8')
    return str(path)


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
    kwargs.setdefault('poll_interval', 30)
    kwargs.setdefault('test_scripts_path', _store_path('test_scripts'))
    kwargs.setdefault('test_suites_path', _store_path('test_suites'))
    kwargs.setdefault('mcc_mnc_path', _mcc_mnc_path())
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
            name='test-esim-api-http', daemon=True)
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
    """One state + HTTP harness per test (the fake eUICC per test)."""

    def setUp(self):
        self.state = None
        self.card = None
        self.http = None

    def tearDown(self):
        if self.http is not None:
            self.http.close()

    def serve(self, card=None, **kwargs):
        card = card if card is not None else EsimCard(
            profiles=[{'iccid': ICCID_A, 'aid': AID_A, 'state': 'enabled'}])
        self.state = make_state(**kwargs)
        self.state.open(transport=card, start_threads=False)
        self.card = card
        self.http = HTTPHarness(self.state)
        return self.state, card


# ---- the card-level ES10 functions ---------------------------------------


class SwitchRequestTests(unittest.TestCase):
    """The one-block request coding, pinned to the SGP.22 object bytes."""

    def test_enable_by_iccid(self):
        request = esim.switch_request('enable', iccid=ICCID_A)
        # BF31 11 A0 0C 5A 0A <BCD> 81 01 FF
        self.assertEqual(request.to_tlv().hex().upper(),
                         'BF3111A00C5A0A' + bcd(ICCID_A) + '8101FF')

    def test_disable_by_aid_without_refresh(self):
        request = esim.switch_request('disable', isdp_aid=AID_B, refresh=False)
        self.assertEqual(request.to_tlv().hex().upper(),
                         'BF3217A0124F10' + AID_B + '810100')

    def test_the_validation(self):
        with self.assertRaises(ValueError):
            esim.switch_request('delete', iccid=ICCID_A)
        with self.assertRaises(ValueError):
            esim.switch_request('enable')
        with self.assertRaises(ValueError):
            esim.switch_request('enable', iccid='89ab')
        with self.assertRaises(ValueError):
            esim.switch_request('enable', isdp_aid='ABC')

    def test_a_bad_response_is_an_error(self):
        with self.assertRaises(esim.EsimError):
            esim.parse_switch_response('enable', b'\xBF\x31\x05\x80\x01')


class SelectionTests(unittest.TestCase):
    """The ISD-R selection/restore on FileOps (a fake transport + ops)."""

    class Ops(object):
        def __init__(self, isdr=True, restore='ok'):
            self.isdr = isdr
            self.restore = restore
            self.calls = []

        def select_adf(self, aid):
            self.calls.append(('adf', aid))
            from simple_server.commands import FileNotFound
            if aid == AID_ISD_R and not self.isdr:
                raise FileNotFound(AID_ISD_R, '6a82')
            return None

        def select_mf(self):
            from simple_server.commands import UnexpectedStatusWord
            self.calls.append(('mf',))
            if self.restore != 'ok':
                raise UnexpectedStatusWord('00A40004023F0000', '6a82')

    def test_a_non_euicc_raises_the_request_error(self):
        ops = self.Ops(isdr=False)
        with self.assertRaises(esim.NotAnEuiccError) as caught:
            with esim.isdr_selected(ops):
                self.fail('the body must not run')
        self.assertIsInstance(caught.exception, ValueError)
        self.assertEqual(caught.exception.code, 'not_an_euicc')

    def test_mf_is_restored_after_the_operation(self):
        ops = self.Ops()
        with esim.isdr_selected(ops):
            ops.calls.append(('body',))
        self.assertEqual(ops.calls[-1], ('mf',))
        self.assertEqual(ops.calls[0], ('adf', AID_ISD_R))

    def test_a_refused_restore_prefers_adf_usim_then_mf(self):
        class Ops(SelectionTests.Ops):
            def select_mf(self):
                if self.restore == 'ok':
                    return super().select_mf()
                self.calls.append(('mf',))
                from simple_server.commands import UnexpectedStatusWord
                if not any(call == ('adf', AID_USIM) for call in self.calls):
                    raise UnexpectedStatusWord('00A40004023F0000', '6a82')
                self.restore = 'ok'
                return None

        ops = Ops(restore='refused')
        self.assertEqual(esim.restore_mf(ops), True)
        self.assertIn(('adf', AID_USIM), ops.calls)

    def test_a_restore_that_cannot_succeed_is_not_an_error(self):
        ops = self.Ops(restore='refused')
        self.assertFalse(esim.restore_mf(ops))


# ---- the device endpoints -------------------------------------------------


class ChipTests(HarnessCase):
    def test_the_eid_is_read_at_equip(self):
        state, _card = self.serve()
        self.assertEqual(state.eid, EID)
        _status, body = self.http.get('/api/status')
        self.assertTrue(body['euicc'])
        self.assertEqual(body['eid'], EID)

    def test_the_chip_view(self):
        state, card = self.serve()
        status, body = self.http.get('/api/esim/chip')
        self.assertEqual(status, 200, body)
        self.assertEqual(body['eid'], EID)
        self.assertEqual(body['errors'], {})
        self.assertEqual(body['info1']['svn'], '1.2.3')
        self.assertEqual(body['info1']['euicc_ci_pki_list_for_verification'],
                         ['010203'])
        self.assertEqual(body['info2']['profile_version'], '2.2.0')
        self.assertEqual(body['info2']['svn'], '3.4.0')
        self.assertIn('usimSupport', body['info2']['uicc_capability'])
        self.assertEqual(body['info2']['euicc_category'], 'mediumEuicc')
        self.assertEqual(body['addresses'],
                         {'default_dp_address': 'smdp.example.org',
                          'root_ds_address': ''})
        self.assertEqual(body['rat'], [{'ppr_ids': ['ppr1', 'ppr2'],
                                        'allowed_operators': [],
                                        'ppr_flags': []}])
        # every part is one STORE DATA; the ISD-R was selected before them
        self.assertTrue(all(data[:4] == 'BF3E' or data[:4] == 'BF20'
                            or data[:4] == 'BF22' or data[:4] == 'BF3C'
                            or data[:4] == 'BF43'
                            for data in card.store_data_tlvs()[1:]))

    def test_one_unsupported_part_does_not_fail_the_rest(self):
        class PartialCard(EsimCard):
            def _store_data(self, apdu):
                data = apdu[5:5 + apdu[4]].hex().upper()
                if data[:4] == 'BF43':
                    return b'', b'\x6A\x88'
                return super()._store_data(apdu)

        self.serve(PartialCard(profiles=[]))
        status, body = self.http.get('/api/esim/chip')
        self.assertEqual(status, 200)
        self.assertIsNone(body['rat'])
        self.assertIn('rat', body['errors'])
        self.assertEqual(body['info1']['svn'], '1.2.3')

    def test_a_non_euicc_is_a_400(self):
        card = EsimCard(euicc=False, profiles=[])
        self.serve(card)
        _status, body = self.http.get('/api/status')
        self.assertFalse(body['euicc'])
        self.assertIsNone(body['eid'])
        for path in ('/api/esim/chip', '/api/esim/profiles',
                     '/api/esim/notifications'):
            status, body = self.http.get(path)
            self.assertEqual(status, 400, path)
            self.assertIn('not an eUICC', body['error'])

    def test_no_card_is_a_503(self):
        self.state = make_state()
        self.http = HTTPHarness(self.state)
        status, body = self.http.get('/api/esim/chip')
        self.assertEqual(status, 503)
        self.assertIn('no card session', body['error'])


class ProfileListTests(HarnessCase):
    def test_the_profile_metadata(self):
        profiles = [
            {'iccid': ICCID_A, 'aid': AID_A, 'state': 'enabled',
             'nickname': 'Work', 'provider': 'Test Provider',
             'name': 'Profile One', 'cls': 'operational',
             'icon_type': 'png', 'icon': '89504E470D0A1A0A',
             'owner': '250-99'},
            {'iccid': ICCID_B, 'aid': AID_B, 'state': 'disabled',
             'cls': 'test'},
        ]
        self.serve(EsimCard(profiles=profiles))
        status, body = self.http.get('/api/esim/profiles')
        self.assertEqual(status, 200)
        self.assertIsNone(body['error'])
        self.assertEqual(body['profiles'], [
            {'iccid': ICCID_A, 'isdp_aid': AID_A, 'state': 'enabled',
             'nickname': 'Work', 'provider': 'Test Provider',
             'name': 'Profile One', 'class': 'operational',
             'icon_type': 'png', 'icon': '89504E470D0A1A0A',
             'icon_size': 8, 'owner': '250-99'},
            {'iccid': ICCID_B, 'isdp_aid': AID_B, 'state': 'disabled',
             'nickname': None, 'provider': None, 'name': None,
             'class': 'test', 'icon_type': None, 'icon': None,
             'icon_size': None, 'owner': None},
        ])

    def test_a_list_error_is_a_result(self):
        class ErrorCard(EsimCard):
            def _store_data(self, apdu):
                data = apdu[5:5 + apdu[4]].hex().upper()
                if data[:4] == 'BF2D':
                    return bytes.fromhex(profiles_error_response()), b'\x90\x00'
                return super()._store_data(apdu)

        self.serve(ErrorCard(profiles=[]))
        status, body = self.http.get('/api/esim/profiles')
        self.assertEqual(status, 200)
        self.assertEqual(body['profiles'], [])
        self.assertEqual(body['error'], 'undefined error')


class NotificationTests(HarnessCase):
    def test_the_notification_list(self):
        self.serve(EsimCard(
            profiles=[],
            notifications=[
                {'seq': 3, 'operations': ['enable'],
                 'address': 'smdp.example.org', 'iccid': ICCID_A},
                {'seq': 0, 'operations': ['install', 'delete'],
                 'address': 'mno-00.esimservices.com', 'iccid': ICCID_B},
            ]))
        status, body = self.http.get('/api/esim/notifications')
        self.assertEqual(status, 200)
        self.assertEqual(body, {'notifications': [
            {'seq_number': 3, 'operations': ['enable'],
             'address': 'smdp.example.org', 'iccid': ICCID_A},
            {'seq_number': 0, 'operations': ['install', 'delete'],
             'address': 'mno-00.esimservices.com', 'iccid': ICCID_B},
        ], 'error': None})


class ProfileSwitchTests(HarnessCase):
    def profiles(self):
        return [{'iccid': ICCID_A, 'aid': AID_A, 'state': 'enabled'},
                {'iccid': ICCID_B, 'aid': AID_B, 'state': 'disabled'}]

    def test_the_refresh_chain_and_the_reinit(self):
        card = EsimCard(profiles=self.profiles(), switch_sw='9130')
        card.fetches = [refresh_command()]
        card.trs = ['9000']
        state, card = self.serve(card)
        session_before = state.card_session
        sent_before = len(card.store_data_tlvs())
        status, body = self.http.post('/api/esim/profile',
                                      {'action': 'enable',
                                       'iccid': ICCID_B, 'refresh': True})
        self.assertEqual(status, 200, body)
        self.assertTrue(body['ok'])
        self.assertEqual(body['result'], 'ok')
        self.assertTrue(body['refresh_seen'])
        self.assertTrue(body['reinitialized'])
        self.assertTrue(body['verified'])
        self.assertEqual(body['state_after'], 'enabled')
        self.assertEqual(body['iccid'], ICCID_A)    # the card's EF.ICCID
        self.assertEqual(body['card_session'], state.card_session)
        self.assertEqual(state.card_session, session_before + 1)
        # the request: one EnableProfileRequest, never retried
        requests = [data for data in card.store_data_tlvs()[sent_before:]
                    if data[:4] in ('BF31', 'BF32')]
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0], 'BF3111A00C5A0A' + bcd(ICCID_B)
                         + '8101FF')
        # the REFRESH was fetched and answered, then the card was reset
        self.assertGreaterEqual(card.resets, 2)
        self.assertEqual(card.fetches, [])
        # the state was re-read after the switch
        self.assertEqual(card.profiles[1]['state'], 'enabled')
        self.assertEqual(card.profiles[0]['state'], 'disabled')

    def test_a_switch_answer_without_refresh(self):
        card = EsimCard(profiles=self.profiles(), switch_sw='9000')
        self.serve(card)
        status, body = self.http.post('/api/esim/profile',
                                      {'action': 'disable',
                                       'isdp_aid': AID_A})
        self.assertEqual(status, 200, body)
        self.assertTrue(body['ok'])
        self.assertFalse(body['refresh_seen'])
        self.assertTrue(body['reinitialized'])
        self.assertTrue(body['verified'])
        self.assertEqual(body['state_after'], 'disabled')
        # the AID form rode in the request
        requests = [data for data in card.store_data_tlvs()
                    if data[:4] in ('BF31', 'BF32')]
        self.assertEqual(requests,
                         ['BF3217A0124F10' + AID_A + '8101FF'])

    def test_the_refresh_chain_sends_no_status(self):
        # The card is mid-switch after the REFRESH: a STATUS poll there gets
        # ``6985``.  The chain answers the REFRESH and stops - the reset does
        # the rest.
        class TimelineCard(EsimCard):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.timeline = []

            def send_apdu(self, apdu):
                answer = super().send_apdu(apdu)
                self.timeline.append(bytes(apdu).hex().upper())
                return answer

            def reset_card(self):
                self.timeline.append('<reset>')
                return super().reset_card()

        card = TimelineCard(profiles=self.profiles(), switch_sw='9130')
        card.fetches = [refresh_command()]
        card.trs = ['9000']
        self.serve(card)
        status, body = self.http.post('/api/esim/profile',
                                      {'action': 'enable',
                                       'iccid': ICCID_B, 'refresh': True})
        self.assertEqual(status, 200, body)
        self.assertTrue(body['refresh_seen'])
        start = next(index for index, item in enumerate(card.timeline)
                     if item.startswith('80E29100')
                     and item[10:14] in ('BF31', 'BF32'))
        end = card.timeline.index('<reset>', start)
        window = card.timeline[start:end]
        # the REFRESH was FETCHed and answered inside that window
        self.assertTrue(any(item.startswith('8012') for item in window),
                        window)
        self.assertTrue(any(item.startswith('8014') for item in window),
                        window)
        self.assertFalse([item for item in window if item.startswith('80F2')],
                         window)

    def test_a_refresh_outside_the_chain_still_reinitializes(self):
        # The legacy safety net over the proactive log: the switch answered
        # 9000 (no chain), but a REFRESH was answered anyway - the card wants
        # the terminal to re-initialize.

        class LoggingCard(EsimCard):
            on_switch = None

            def _store_data(self, apdu):
                data = apdu[5:5 + apdu[4]].hex().upper()
                if data[:4] in ('BF31', 'BF32') and self.on_switch:
                    self.on_switch()
                return super()._store_data(apdu)

        card = LoggingCard(profiles=self.profiles(), switch_sw='9000')
        state, card = self.serve(card)
        session_before = state.card_session
        card.on_switch = lambda: state.cat.log_proactive(
            0x01, refresh_command(), 0x00, 1)
        status, body = self.http.post('/api/esim/profile',
                                      {'action': 'enable',
                                       'iccid': ICCID_B})
        self.assertEqual(status, 200, body)
        self.assertTrue(body['ok'])
        self.assertTrue(body['refresh_seen'])
        self.assertTrue(body['reinitialized'])
        self.assertEqual(state.card_session, session_before + 1)
        self.assertTrue(body['verified'])

    def test_a_failed_verification_keeps_the_switch_result(self):
        # The switch happened; the ISD-R then refuses the verification's
        # selection.  That is "not verified", never a 400 that discards a
        # completed, destructive switch.

        class VanishingIsdrCard(EsimCard):
            fail_isdr = False

            def reset_card(self):
                answer = super().reset_card()
                if self.resets >= 2:        # after the switch's re-init reset
                    self.fail_isdr = True
                return answer

            def _select(self, apdu):
                if (self.fail_isdr and apdu[2] == 0x04
                        and apdu[5:5 + apdu[4]].hex().upper() == AID_ISD_R):
                    return b'', b'\x6A\x82'
                return super()._select(apdu)

        card = VanishingIsdrCard(profiles=self.profiles(), switch_sw='9000')
        state, card = self.serve(card)
        session_before = state.card_session
        status, body = self.http.post('/api/esim/profile',
                                      {'action': 'disable',
                                       'iccid': ICCID_A})
        self.assertEqual(status, 200, body)
        self.assertTrue(body['ok'])
        self.assertEqual(body['result'], 'ok')
        self.assertTrue(body['reinitialized'])
        self.assertEqual(state.card_session, session_before + 1)
        self.assertIsNone(body['verified'])
        self.assertIsNone(body['state_after'])
        # the switch itself took effect on the card
        self.assertEqual(card.profiles[0]['state'], 'disabled')

    def test_the_refresh_flag_is_read_as_a_boolean(self):
        # bool("false") is truthy: the literal spellings are honoured, a bad
        # value is a request error.
        card = EsimCard(profiles=self.profiles(), switch_sw='9000')
        self.serve(card)
        status, body = self.http.post('/api/esim/profile',
                                      {'action': 'disable',
                                       'iccid': ICCID_A, 'refresh': 'false'})
        self.assertEqual(status, 200, body)
        requests = [data for data in card.store_data_tlvs()
                    if data[:4] == 'BF32']
        self.assertEqual(requests, ['BF3211A00C5A0A' + bcd(ICCID_A)
                                    + '810100'])
        status, body = self.http.post('/api/esim/profile',
                                      {'action': 'enable',
                                       'iccid': ICCID_A, 'refresh': 'maybe'})
        self.assertEqual(status, 400, body)
        self.assertIn('refresh must be a boolean', body['error'])

    def test_a_refused_switch_is_a_result(self):
        card = EsimCard(profiles=self.profiles(), switch_sw='6985')
        state, card = self.serve(card)
        session_before = state.card_session
        status, body = self.http.post('/api/esim/profile',
                                      {'action': 'enable',
                                       'iccid': ICCID_B})
        self.assertEqual(status, 200, body)
        self.assertFalse(body['ok'])
        self.assertEqual(body['result'], 'undefinedError')
        self.assertEqual(body['sw'], '6985')
        self.assertFalse(body['refresh_seen'])
        self.assertFalse(body['reinitialized'])
        self.assertIsNone(body['verified'])
        self.assertEqual(state.card_session, session_before)
        self.assertEqual(card.profiles[1]['state'], 'disabled')

    def test_a_card_busy_result_takes_effect_as_a_failure(self):
        card = EsimCard(profiles=self.profiles(), switch_sw='9000',
                        switch_result='catBusy')
        self.serve(card)
        status, body = self.http.post('/api/esim/profile',
                                      {'action': 'disable',
                                       'iccid': ICCID_A})
        self.assertEqual(status, 200)
        self.assertFalse(body['ok'])
        self.assertEqual(body['result'], 'catBusy')
        self.assertIn('busy', body['message'])
        self.assertFalse(body['reinitialized'])

    def test_the_request_is_validated_before_the_card(self):
        state, card = self.serve(EsimCard(profiles=self.profiles()))
        sent_before = len(card.sent)
        for body, message in (({'action': 'enable'},
                               'names no profile'),
                              ({'action': 'delete', 'iccid': ICCID_A},
                               'action must be'),
                              ({'action': 'enable', 'iccid': 'xyz'},
                               'not a digit string')):
            status, response = self.http.post('/api/esim/profile', body)
            self.assertEqual(status, 400, body)
            self.assertIn(message, response['error'])
        self.assertEqual(len(card.sent), sent_before)
        self.assertFalse(state.equipping)

    def test_a_non_euicc_switch_is_a_400(self):
        self.serve(EsimCard(euicc=False, profiles=[]))
        status, body = self.http.post('/api/esim/profile',
                                      {'action': 'enable',
                                       'iccid': ICCID_A})
        self.assertEqual(status, 400)
        self.assertIn('not an eUICC', body['error'])


if __name__ == '__main__':
    unittest.main()
