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
    EnableProfileResp, EnableResult, EuiccConfiguredAddresses, EuiccInfo1,
    Iccid, IsdpAid, ListNotificationResp, NotificationAddress,
    NotificationMetadata, NotificationMetadataList, ProfileClass, ProfileInfo,
    ProfileInfoListResp, ProfileInfoSeq, ProfileMgmtOperation, ProfileNickname,
    ProfileOwner, ProfileOwnerPLMN, ProfileState, SeqNumber,
)
from pySim.utils import h2b

from pysim_simple_server import esim


class FakeLchan:
    def __init__(self):
        self.scc = SimpleNamespace(name='lchan-scc')
        self.selected = None

    def select_file(self, app):
        self.selected = app


def make_app(profile='Consumer eUICC (SGP.22)', isdr=True):
    lchan = FakeLchan()
    apps = {}
    if isdr:
        apps[AID_ISD_R.lower()] = SimpleNamespace(name='ISD-R')
    rs = SimpleNamespace(profile=profile,
                         mf=SimpleNamespace(applications=apps),
                         lchan=[lchan],
                         resets=0)

    def soft_reset():
        rs.resets += 1

    rs.soft_reset = soft_reset
    return SimpleNamespace(rs=rs), lchan


def profile_info(iccid, aid, state=None, nickname=None, cls=None, owner=None):
    children = [Iccid(decoded=iccid), IsdpAid(decoded=bytes.fromhex(aid))]
    if state:
        children.append(ProfileState(decoded=state))
    if nickname:
        children.append(ProfileNickname(decoded=nickname))
    if cls:
        children.append(ProfileClass(decoded=cls))
    if owner:
        children.append(ProfileOwner(children=[ProfileOwnerPLMN(decoded=owner)]))
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
                         owner='250-99'),
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
        self.assertEqual(out['profiles'][1]['state'], 'disabled')
        # ISD-R was selected and the previous selection restored
        self.assertEqual(lchan.selected.name, 'ISD-R')
        self.assertEqual(app.rs.resets, 1)
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

    def test_chip_info_collects_parts_and_errors(self):
        app, _ = make_app()
        self.patch_eid('89049032000000000000000000000001')
        info1 = EuiccInfo1()
        info1.from_tlv(h2b('BF20058203010203'))
        addresses = EuiccConfiguredAddresses()
        addresses.from_tlv(h2b('BF3C13800E736D64702E6578616D706C652E6F72678100'))
        self.patch([info1, RuntimeError('no info2'), addresses])
        out = esim.chip_info(app)
        self.assertEqual(out['eid'], '89049032000000000000000000000001')
        self.assertEqual(out['info1'], {'svn': '1.2.3'})
        self.assertIsNone(out['info2'])
        self.assertIn('info2', out['errors'])
        self.assertEqual(out['addresses'].get('default_dp_address'), 'smdp.example.o')

    def test_set_profile_state_enable_uses_iccid_and_refresh(self):
        app, _ = make_app()
        self.patch([EnableProfileResp(children=[EnableResult(decoded='ok')])])
        out = esim.set_profile_state(app, 'enable', iccid='8970119000004002667')
        self.assertTrue(out['ok'])
        self.assertEqual(out['result'], 'ok')
        tlv = self.calls[0].to_tlv().hex().upper()
        self.assertIn('5A0A980711090000042066F7', tlv)   # ProfileIdentifier/ICCID
        self.assertTrue(tlv.endswith('810101'))          # RefreshFlag = 1

    def test_set_profile_state_maps_cat_busy(self):
        app, _ = make_app()
        self.patch([DisableProfileResp(children=[DisableResult(decoded='catBusy')])])
        out = esim.set_profile_state(app, 'disable', iccid='8970119000004002667')
        self.assertFalse(out['ok'])
        self.assertEqual(out['result'], 'catBusy')
        self.assertIn('busy', out['message'])

    def test_set_profile_state_validation(self):
        app, _ = make_app()
        with self.assertRaises(esim.EsimError):
            esim.set_profile_state(app, 'delete', iccid='8970119000004002667')
        with self.assertRaises(esim.EsimError):
            esim.set_profile_state(app, 'enable')
        with self.assertRaises(esim.EsimError):
            esim.set_profile_state(app, 'enable', iccid='abc')

    def test_select_isdr_requires_an_euicc(self):
        app, _ = make_app(isdr=False)
        with self.assertRaises(esim.EsimError):
            esim.chip_info(app)


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
