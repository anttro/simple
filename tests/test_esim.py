# coding=utf-8
"""Tests for the local eSIM/LPA operations (ES10a/b/c) in esim.py.

The ES10 transport (``CardApplicationISDR.store_data_tlv``/``get_eid``) is
monkeypatched with canned responses, so the mapping to the JSON API and the
ISD-R selection/restore logic are tested without hardware.
"""

import inspect
import unittest
from types import SimpleNamespace

from pySim.euicc import (
    AID_ISD_R, CardApplicationISDR, DisableProfileResp, DisableResult,
    EnableProfileResp, EnableResult, Icon, IconType,
    Iccid, IsdpAid, ListNotificationResp, NotificationAddress,
    NotificationMetadata, NotificationMetadataList, ProfileClass, ProfileInfo,
    ProfileInfoListResp, ProfileInfoSeq, ProfileMgmtOperation, ProfileNickname,
    ProfileOwner, ProfileOwnerPLMN, ProfileState, SeqNumber,
)
from pySim.utils import h2b

from pysim_simple_server import esim


def _tlv(tag, value):
    """Encode one BER-TLV with a single-byte length (test fixtures only)."""
    tag_hex = '%02X' % tag if tag <= 0xFF else '%04X' % tag
    return '%s%02X%s' % (tag_hex, len(value) // 2, value)


class FakeLchan:
    def __init__(self):
        self.scc = SimpleNamespace(name='lchan-scc')
        self.selected = None
        self.cmd_app = None

    def select_file(self, app, cmd_app=None):
        self.selected = app
        self.cmd_app = cmd_app


def make_app(profile='Consumer eUICC (SGP.22)', isdr=True):
    lchan = FakeLchan()
    apps = {}
    if isdr:
        apps[AID_ISD_R.lower()] = SimpleNamespace(name='ISD-R')
    rs = SimpleNamespace(profile=profile,
                         mf=SimpleNamespace(applications=apps),
                         lchan=[lchan],
                         resets=0,
                         soft_reset_cmd_app=None)

    def soft_reset(cmd_app=None):
        rs.resets += 1
        rs.soft_reset_cmd_app = cmd_app

    rs.soft_reset = soft_reset
    app = SimpleNamespace(rs=rs)
    return app, lchan


def profile_info(iccid, aid, state=None, nickname=None, cls=None, owner=None,
                 icon=None):
    children = [Iccid(decoded=iccid), IsdpAid(decoded=bytes.fromhex(aid))]
    if state:
        children.append(ProfileState(decoded=state))
    if nickname:
        children.append(ProfileNickname(decoded=nickname))
    if cls:
        children.append(ProfileClass(decoded=cls))
    if owner:
        children.append(ProfileOwner(children=[ProfileOwnerPLMN(decoded=owner)]))
    if icon:
        icon_type, icon_data = icon
        children.append(IconType(decoded=icon_type))
        children.append(Icon(decoded=bytes.fromhex(icon_data)))
    return ProfileInfo(children=children)


class EsimTests(unittest.TestCase):
    def setUp(self):
        self._orig_store = CardApplicationISDR.store_data_tlv
        self._orig_eid = CardApplicationISDR.get_eid
        self.calls = []

    def tearDown(self):
        CardApplicationISDR.store_data_tlv = self._orig_store
        CardApplicationISDR.get_eid = self._orig_eid

    def patch(self, responses):
        """Queue canned responses for store_data_tlv (Exception entries raise)."""
        def fake(scc, cmd_do, resp_cls, exp_sw='9000'):
            self.calls.append(cmd_do)
            r = responses.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        CardApplicationISDR.store_data_tlv = staticmethod(fake)

    def patch_eid(self, eid):
        CardApplicationISDR.get_eid = staticmethod(lambda scc: eid)

    def test_is_euicc(self):
        app, _ = make_app()
        self.assertTrue(esim.is_euicc(app))
        other, _ = make_app(profile='UICC')
        self.assertFalse(esim.is_euicc(other))

    def test_profiles_maps_metadata(self):
        app, lchan = make_app()
        resp = ProfileInfoListResp(children=[ProfileInfoSeq(children=[
            profile_info('8970119000004002667', 'A0000005591010FFFFFFFF8900000100',
                         state='enabled', nickname='Work', cls='operational',
                         owner='250-99', icon=('png', '89504E470D0A1A0A')),
            profile_info('8970119000004002668', 'A0000005591010FFFFFFFF8900000200',
                         state='disabled', cls='test'),
        ])])
        self.patch([resp])
        out = esim.profiles(app)
        self.assertIsNone(out['error'])
        self.assertEqual(len(out['profiles']), 2)
        first = out['profiles'][0]
        self.assertEqual(first['iccid'], '8970119000004002667')
        self.assertEqual(first['isdp_aid'], 'A0000005591010FFFFFFFF8900000100')
        self.assertEqual(first['state'], 'enabled')
        self.assertEqual(first['nickname'], 'Work')
        self.assertEqual(first['class'], 'operational')
        self.assertEqual(first['owner'], '250-99')
        self.assertEqual(first['icon_type'], 'png')
        self.assertEqual(first['icon'], '89504E470D0A1A0A')
        self.assertEqual(first['icon_size'], 8)
        self.assertEqual(out['profiles'][1]['state'], 'disabled')
        self.assertIsNone(out['profiles'][1]['icon'])
        self.assertIsNone(out['profiles'][1]['icon_size'])
        # ISD-R was selected (with the shell app, so pySim's command-set
        # bookkeeping follows) and the previous selection restored the same way
        self.assertEqual(lchan.selected.name, 'ISD-R')
        self.assertIs(lchan.cmd_app, app)
        self.assertEqual(app.rs.resets, 1)
        self.assertIs(app.rs.soft_reset_cmd_app, app)
        # the request asks for every ProfileInfo tag
        self.assertIn('9F70', self.calls[0].to_tlv().hex().upper())

    def test_profiles_error_result(self):
        app, _ = make_app()
        resp = ProfileInfoListResp()
        resp.from_tlv(h2b('BF2D0381017F'))
        self.patch([resp])
        out = esim.profiles(app)
        self.assertEqual(out['profiles'], [])
        self.assertEqual(out['error'], 'undefined error')

    def test_notifications_maps_operations(self):
        app, _ = make_app()
        meta = NotificationMetadata(children=[
            SeqNumber(decoded=3),
            ProfileMgmtOperation(decoded={'install': False, 'enable': True,
                                          'disable': False, 'delete': False}),
            NotificationAddress(decoded='smdp.example.org'),
            Iccid(decoded='8970119000004002667'),
        ])
        self.patch([ListNotificationResp(
            children=[NotificationMetadataList(children=[meta])])])
        out = esim.notifications(app)
        self.assertIsNone(out['error'])
        self.assertEqual(out['notifications'], [{
            'seq_number': 3, 'operations': ['enable'],
            'address': 'smdp.example.org', 'iccid': '8970119000004002667',
        }])

    def test_notifications_parses_operations_from_the_card_tlv(self):
        # The card's ProfileMgmtOperation TLV (padding octet + flags octet,
        # SGP.22 5.7.9) nests the flags under 'pmo' when pySim parses it.
        app, _ = make_app()
        address = '6D6E6F2D30302E6573696D73657276696365732E636F6D'  # mno-00.esimservices.com
        meta = _tlv(0xBF2F, _tlv(0x80, '00') + _tlv(0x81, '0140')
                    + _tlv(0x0C, address) + _tlv(0x5A, '980711090000042066F7'))
        resp = ListNotificationResp()
        resp.from_tlv(bytes.fromhex(_tlv(0xBF28, _tlv(0xA0, meta))))
        self.patch([resp])
        out = esim.notifications(app)
        self.assertIsNone(out['error'])
        self.assertEqual(out['notifications'], [{
            'seq_number': 0, 'operations': ['enable'],
            'address': 'mno-00.esimservices.com',
            'iccid': '8970119000004002667',
        }])

    def test_profile_operations_handles_both_shapes(self):
        self.assertEqual(esim._profile_operations(
            {'pmo': {'install': False, 'enable': True, 'disable': False,
                     'delete': True}}), ['enable', 'delete'])
        self.assertEqual(esim._profile_operations(
            {'install': True, 'enable': False, 'disable': False, 'delete': False}),
            ['install'])
        self.assertEqual(esim._profile_operations(None), [])
        self.assertEqual(esim._profile_operations('pmo'), [])

    def test_chip_info_collects_parts_and_errors(self):
        app, _ = make_app()
        self.patch_eid('89049032000000000000000000000001')
        info1 = _tlv(0xBF20, _tlv(0x82, '010203'))
        addresses = _tlv(0xBF3C, _tlv(0x80, '736D64702E6578616D706C652E6F7267')
                         + _tlv(0x81, ''))
        rat = _tlv(0xBF43, _tlv(0xA0, _tlv(0x30, _tlv(0x80, '0460'))))
        self.patch([info1, RuntimeError('no info2'), addresses, rat])
        out = esim.chip_info(app)
        self.assertEqual(out['eid'], '89049032000000000000000000000001')
        self.assertEqual(out['info1'], {
            'svn': '1.2.3', 'euicc_ci_pki_list_for_verification': [],
            'euicc_ci_pki_list_for_signing': []})
        self.assertIsNone(out['info2'])
        self.assertIn('info2', out['errors'])
        self.assertEqual(out['addresses'], {'default_dp_address': 'smdp.example.org',
                                            'root_ds_address': ''})
        self.assertEqual(out['rat'], [{'ppr_ids': ['ppr1', 'ppr2'],
                                       'allowed_operators': [], 'ppr_flags': []}])

    def test_build_switch_apdu_encodes_iccid_and_refresh(self):
        apdu = esim.build_switch_apdu('enable', iccid='8970119000004002667')
        self.assertTrue(apdu.startswith('80E29100'))
        self.assertTrue(apdu.endswith('00'))
        self.assertIn('5A0A980711090000042066F7', apdu)   # ProfileIdentifier/ICCID
        self.assertIn('810101', apdu)                      # RefreshFlag = 1
        apdu = esim.build_switch_apdu(
            'disable', isdp_aid='A0000005591010FFFFFFFF8900000100', refresh=False)
        self.assertIn('4F10A0000005591010FFFFFFFF8900000100', apdu)
        self.assertIn('810100', apdu)                      # RefreshFlag = 0

    def test_build_switch_apdu_validation(self):
        with self.assertRaises(esim.EsimError):
            esim.build_switch_apdu('delete', iccid='8970119000004002667')
        with self.assertRaises(esim.EsimError):
            esim.build_switch_apdu('enable')
        with self.assertRaises(esim.EsimError):
            esim.build_switch_apdu('enable', iccid='abc')

    def test_parse_switch_response_maps_result_codes(self):
        resp = DisableProfileResp(children=[DisableResult(decoded='catBusy')])
        out = esim.parse_switch_response('disable', resp.to_tlv().hex())
        self.assertFalse(out['ok'])
        self.assertEqual(out['result'], 'catBusy')
        self.assertIn('busy', out['message'])
        out = esim.parse_switch_response('disable', '')
        self.assertFalse(out['ok'])
        self.assertEqual(out['result'], 'undefinedError')

    def test_switch_profile_parses_ok_response(self):
        app, lchan = make_app()
        sent = []
        resp = EnableProfileResp(children=[EnableResult(decoded='ok')])
        out = esim.switch_profile(
            app,
            lambda apdu: sent.append(apdu) or (resp.to_tlv().hex(), '9000'),
            lambda sw: self.fail('no chain expected'),
            'enable', iccid='8970119000004002667')
        self.assertTrue(out['ok'])
        self.assertEqual(out['result'], 'ok')
        self.assertFalse(out['refresh_seen'])
        self.assertEqual(len(sent), 1)
        # the ISD-R was selected for the command (with the shell app) and the
        # selection restored the same way
        self.assertEqual(lchan.selected.name, 'ISD-R')
        self.assertIs(lchan.cmd_app, app)
        self.assertEqual(app.rs.resets, 1)
        self.assertIs(app.rs.soft_reset_cmd_app, app)

    def test_switch_profile_treats_91xx_as_ok_and_runs_the_chain(self):
        app, lchan = make_app()
        chain = []
        out = esim.switch_profile(
            app,
            lambda apdu: ('', '9111'),
            lambda sw: chain.append(sw) or True,
            'disable', iccid='8970119000004002667')
        self.assertTrue(out['ok'])
        self.assertEqual(out['result'], 'ok')
        self.assertTrue(out['refresh_seen'])
        self.assertEqual(chain, ['9111'])
        self.assertEqual(lchan.selected.name, 'ISD-R')
        self.assertIs(lchan.cmd_app, app)
        self.assertEqual(app.rs.resets, 1)
        self.assertIs(app.rs.soft_reset_cmd_app, app)

    def test_switch_profile_chain_failure_keeps_the_accepted_switch(self):
        app, _ = make_app()

        def boom(sw):
            raise RuntimeError('fetch failed')
        out = esim.switch_profile(app, lambda apdu: ('', '910f'), boom,
                                  'disable', iccid='8970119000004002667')
        self.assertTrue(out['ok'])
        self.assertFalse(out['refresh_seen'])

    def test_switch_profile_reports_error_sw(self):
        app, _ = make_app()
        out = esim.switch_profile(app, lambda apdu: ('', '6985'),
                                  lambda sw: self.fail('no chain expected'),
                                  'disable', iccid='8970119000004002667')
        self.assertFalse(out['ok'])
        self.assertEqual(out['sw'], '6985')
        self.assertEqual(out['result'], 'undefinedError')

    def test_select_isdr_requires_an_euicc(self):
        app, _ = make_app(isdr=False)
        with self.assertRaises(esim.EsimError):
            esim.chip_info(app)


SKI = '81370F5125D0B1D408D4C3B232E6D25E795BEBFB'


class EsimInfoDecodeTests(unittest.TestCase):
    """EUICCInfo1/2 and RAT decoders against a real consumer eUICC's values."""

    def test_bit_string_decodes_unused_bits_msb_first(self):
        self.assertEqual(
            esim._decode_bit_string(bytes.fromhex('077F3E1F80'),
                                    esim.UICC_CAPABILITY_BITS),
            ['usimSupport', 'isimSupport', 'csimSupport', 'akaMilenage',
             'akaCave', 'akaTuak128', 'akaTuak256', 'gbaAuthenUsim',
             'gbaAuthenISim', 'mbmsAuthenUsim', 'eapClient', 'javacard',
             'berTlvFileSupport', 'dfLinkSupport', 'catTp', 'getIdentity',
             'profile-a-x25519', 'profile-b-p256'])
        self.assertEqual(
            esim._decode_bit_string(bytes.fromhex('0490'),
                                    esim.RSP_CAPABILITY_BITS),
            ['additionalProfile', 'testProfileSupport'])
        self.assertEqual(
            esim._decode_bit_string(bytes.fromhex('0640'), esim.PPR_ID_BITS),
            ['ppr1'])
        self.assertEqual(esim._decode_bit_string(b'', esim.PPR_ID_BITS), [])

    def test_info2_decodes_every_field(self):
        ski = _tlv(0x04, SKI)
        raw = _tlv(0xBF22, ''.join((
            _tlv(0x81, '020301'), _tlv(0x82, '020202'), _tlv(0x83, '040200'),
            _tlv(0x84, '81010082040006B32C83022646'),
            _tlv(0x85, '077F3E1F80'), _tlv(0x86, '090200'),
            _tlv(0x87, '020300'), _tlv(0x88, '0490'),
            _tlv(0xA9, ski), _tlv(0xAA, ski), _tlv(0x8B, '00'),
            _tlv(0x99, '0640'), _tlv(0x04, '010000'),
            _tlv(0x0C, '45442D5A492D55502D30383236'),
        )))
        out = esim._decode_info2(raw)
        self.assertEqual(out['profile_version'], '2.3.1')
        self.assertEqual(out['svn'], '2.2.2')
        self.assertEqual(out['euicc_firmware_ver'], '4.2.0')
        self.assertEqual(out['ext_card_resource'], {
            'installed_application': 0, 'free_non_volatile_memory': 439084,
            'free_volatile_memory': 9798})
        self.assertEqual(out['uicc_capability'][:3],
                         ['usimSupport', 'isimSupport', 'csimSupport'])
        self.assertEqual(out['ts102241_version'], '9.2.0')
        self.assertEqual(out['globalplatform_version'], '2.3.0')
        self.assertEqual(out['rsp_capability'],
                         ['additionalProfile', 'testProfileSupport'])
        self.assertEqual(out['euicc_ci_pki_list_for_verification'], [SKI])
        self.assertEqual(out['euicc_ci_pki_list_for_signing'], [SKI])
        self.assertEqual(out['euicc_category'], 'other')
        self.assertEqual(out['forbidden_profile_policy_rules'], ['ppr1'])
        self.assertEqual(out['pp_version'], '1.0.0')
        self.assertEqual(out['ss_acreditation_number'], 'ED-ZI-UP-0826')
        self.assertNotIn('raw_tlvs', out)

    def test_info2_accepts_both_category_tags_and_keeps_unknown_tlvs(self):
        out = esim._decode_info2(_tlv(0xBF22, _tlv(0xAB, '02') + _tlv(0xE0, 'AABB')))
        self.assertEqual(out['euicc_category'], 'mediumEuicc')
        self.assertEqual(out['raw_tlvs'], {'E0': 'AABB'})

    def test_info2_decodes_certification_data_object(self):
        out = esim._decode_info2(_tlv(0xBF22, _tlv(0xAC, _tlv(0x80, '504C')
                                      + _tlv(0x81, '68747470733A2F2F642E6578616D706C65'))))
        self.assertEqual(out['certification_data_object'],
                         {'platform_label': 'PL',
                          'discovery_base_url': 'https://d.example'})

    def test_info1_decodes_svn_and_ski_lists(self):
        out = esim._decode_info1(_tlv(0xBF20, _tlv(0x82, '020202')
                                      + _tlv(0xA9, _tlv(0x04, SKI))))
        self.assertEqual(out, {'svn': '2.2.2',
                               'euicc_ci_pki_list_for_verification': [SKI],
                               'euicc_ci_pki_list_for_signing': []})

    def test_rat_decodes_rules(self):
        raw = _tlv(0xBF43, _tlv(0xA0, _tlv(0x30,
            _tlv(0x80, '0460')
            + _tlv(0xA1, _tlv(0x30, _tlv(0x80, 'EEEEEE')))
            + _tlv(0x82, '0180'))))
        self.assertEqual(esim._decode_rat(raw), [{
            'ppr_ids': ['ppr1', 'ppr2'],
            'allowed_operators': [{'plmn': 'EEEEEE', 'gid1': None, 'gid2': None}],
            'ppr_flags': ['consentRequired']}])

    def test_addresses_decode(self):
        out = esim._decode_addresses(_tlv(0xBF3C, _tlv(
            0x81, '74657374726F6F74736D64732E67736D612E636F6D')))
        self.assertIsNone(out['default_dp_address'])
        self.assertEqual(out['root_ds_address'], 'testrootsmds.gsma.com')


class SelectionMetadataTests(unittest.TestCase):
    """Status/select FCP metadata must never crash when the card has no usable
    FCP (an ADF selected, or a failed select while the active profile is
    disabled)."""

    class FakeLchan:
        def __init__(self, fcp):
            self.selected_file_fcp = fcp

        def selected_file_size(self):
            return self.selected_file_fcp.get('file_size')

        def selected_file_record_len(self):
            return self.selected_file_fcp['file_descriptor'].get('record_len')

        def selected_file_num_of_rec(self):
            return self.selected_file_fcp['file_descriptor'].get('num_of_rec')

        def selected_file_structure(self):
            return self.selected_file_fcp['file_descriptor']['file_descriptor_byte']['structure']

        def selected_file_type(self):
            return self.selected_file_fcp['file_descriptor']['file_descriptor_byte'].get('file_type', 'ef')

    def test_missing_fcp_yields_none(self):
        from pysim_simple_server import server
        lchan = self.FakeLchan(None)
        self.assertIsNone(server._fcp_value(lchan, 'selected_file_size'))
        self.assertIsNone(server._fcp_value(lchan, 'selected_file_record_len'))
        self.assertIsNone(server._fcp_value(lchan, 'selected_file_num_of_rec'))
        # an EF without FCP falls back to 'transparent' (the historical default)
        self.assertEqual(server._get_file_type(lchan, SimpleNamespace(name='EF.ICCID')),
                         'transparent')

    def test_adf_fcp_without_file_descriptor_yields_none(self):
        from pysim_simple_server import server
        lchan = self.FakeLchan({'file_size': None})
        self.assertIsNone(server._fcp_value(lchan, 'selected_file_record_len'))
        self.assertIsNone(server._fcp_value(lchan, 'selected_file_num_of_rec'))
        self.assertIsNone(server._get_file_type(lchan, SimpleNamespace(name='EF.ICCID')))

    def test_usable_fcp_reports_values(self):
        from pysim_simple_server import server
        fcp = {'file_size': 10,
               'file_descriptor': {'record_len': 5, 'num_of_rec': 2,
                                   'file_descriptor_byte': {'structure': 'linear_fixed',
                                                            'file_type': 'ef'}}}
        lchan = self.FakeLchan(fcp)
        self.assertEqual(server._fcp_value(lchan, 'selected_file_size'), 10)
        self.assertEqual(server._fcp_value(lchan, 'selected_file_record_len'), 5)
        self.assertEqual(server._get_file_type(lchan, SimpleNamespace(name='EF.ADN')),
                         'linear_fixed')

    def test_norm_iccid_drops_the_f_pad(self):
        from pysim_simple_server import server
        self.assertEqual(server._norm_iccid('8970119000004002667'),
                         '8970119000004002667')
        self.assertEqual(server._norm_iccid('98 90 71 11 90 00 00 40 02 66 7F'),
                         '989071119000004002667')


class EsimRoutingTests(unittest.TestCase):
    def test_esim_routes_are_in_the_right_http_handlers(self):
        from pysim_simple_server import server
        get_src = inspect.getsource(server.PysimHandler._do_GET)
        post_src = inspect.getsource(server.PysimHandler._do_POST)
        for route in ('/api/esim/chip', '/api/esim/profiles',
                      '/api/esim/notifications'):
            self.assertIn("self.path == '%s'" % route, get_src, route)
            self.assertNotIn("self.path == '%s'" % route, post_src, route)
        self.assertIn("self.path == '/api/esim/profile'", post_src)
        self.assertNotIn("self.path == '/api/esim/profile'", get_src)


if __name__ == '__main__':
    unittest.main()
