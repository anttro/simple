# coding=utf-8
"""Tests for the ADM verification helper used by ``POST /api/verify-adm``.

The helper builds the TS 102 221 VERIFY APDU itself (instead of pySim's
``verify_chv``) so the raw SW can be reported back: the UI warns about the
remaining attempts after a 63Cx and stops trying once the ADM is blocked.
"""

import unittest
from types import SimpleNamespace

from pysim_simple_server import server


class FakeScc:
    """Records the VERIFY APDU and returns a canned SW."""

    cla_byte = '00'

    def __init__(self, sw='9000'):
        self.sw = sw
        self.apdus = []

    def send_apdu(self, pdu):
        self.apdus.append(pdu)
        return '', self.sw


def _app(chv=0x0A):
    return SimpleNamespace(card=SimpleNamespace(_adm_chv_num=chv),
                           rs=SimpleNamespace(adm_verified=False))


class AdmVerifyTests(unittest.TestCase):
    def test_success_sets_adm_verified(self):
        app = _app()
        scc = FakeScc('9000')
        res = server._verify_adm(scc, app, '0011')
        self.assertEqual(res, {'ok': True, 'sw': '9000'})
        self.assertTrue(app.rs.adm_verified)
        # short keys are padded to the 8 CHV bytes with 'f' (pySim behaviour)
        self.assertEqual(scc.apdus, ['0020000A08' + '0011' + 'f' * 12])

    def test_full_length_key_is_not_padded(self):
        scc = FakeScc('9000')
        server._verify_adm(scc, _app(), 'DEADBEEFDEADBEEF')
        self.assertEqual(scc.apdus, ['0020000A08' + 'deadbeefdeadbeef'])

    def test_chv_number_comes_from_the_card_model(self):
        scc = FakeScc('9000')
        server._verify_adm(scc, _app(chv=0x0B), '0011')
        self.assertIn('0020000B', scc.apdus[0])

    def test_63cx_reports_attempts_left(self):
        app = _app()
        res = server._verify_adm(FakeScc('63C2'), app, '0011')
        self.assertEqual(res, {'ok': False, 'sw': '63C2', 'attempts_left': 2})
        self.assertFalse(app.rs.adm_verified)

    def test_last_attempt_reports_zero(self):
        res = server._verify_adm(FakeScc('63C0'), _app(), '0011')
        self.assertEqual(res, {'ok': False, 'sw': '63C0', 'attempts_left': 0})

    def test_blocked_sw_marks_blocked(self):
        for sw in ('6983', '9804'):
            res = server._verify_adm(FakeScc(sw), _app(), '0011')
            self.assertEqual(res, {'ok': False, 'sw': sw, 'blocked': True})

    def test_other_sw_is_a_plain_error(self):
        self.assertEqual(server._verify_adm(FakeScc('6982'), _app(), '0011'),
                         {'ok': False, 'sw': '6982', 'error': 'Security status not satisfied'})
        self.assertEqual(server._verify_adm(FakeScc('6A88'), _app(), '0011'),
                         {'ok': False, 'sw': '6A88', 'error': 'Error'})

    def test_redaction_masks_adm(self):
        out = server._redact_psk_fields({'adm': '0011', 'psk_hex': 'AA', 'other': 'x'})
        self.assertEqual(out, {'adm': '<redacted>', 'psk_hex': '<redacted>', 'other': 'x'})
        self.assertEqual(server._redact_psk_fields({'adm': ''}), {'adm': ''})

    def test_verify_adm_uses_the_cards_own_channel(self):
        # server.scc can be the startup placeholder left at the SIM CLA ('a0')
        # while the card is a UICC: the VERIFY must go out through the card's
        # own channel (CLA 00), exactly like pySim-shell's verify_adm.
        stale = FakeScc('6E00')
        stale.cla_byte = 'a0'
        card_scc = FakeScc('9000')
        card_scc.cla_byte = '00'
        app = SimpleNamespace(
            card=SimpleNamespace(_adm_chv_num=0x0A, _scc=card_scc),
            rs=SimpleNamespace(adm_verified=False,
                               lchan=[SimpleNamespace(scc=card_scc)]))
        res = server._verify_adm(stale, app, '0011')
        self.assertEqual(res, {'ok': True, 'sw': '9000'})
        self.assertEqual(card_scc.apdus, ['0020000A08' + '0011' + 'f' * 12])
        self.assertEqual(stale.apdus, [])


if __name__ == '__main__':
    unittest.main()
