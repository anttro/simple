#!/usr/bin/env python3
"""Tests for the reset-free fast initialization helpers."""

import sys
import types
import unittest
from pathlib import Path
from unittest import mock

PROJECTS = Path(__file__).resolve().parents[2]
PY_SIM = PROJECTS / 'pysim'
if str(PY_SIM) not in sys.path:
    sys.path.insert(0, str(PY_SIM))

from pySim.exceptions import SwMatchError
from pySim.ts_102_221 import CardProfileUICC

import pysim_simple_server.fastinit as fastinit
from pysim_simple_server.fastinit import (
    FastRuntimeState,
    do_reset_fast,
    pick_profile_no_reset,
)


class FakeScc:
    def __init__(self, mf_select_error=False):
        self.sel_ctrl = '0004'
        self.cla_byte = '00'
        self.resets = 0
        self.selected = []
        self.mf_select_error = mf_select_error

    def reset_card(self):
        self.resets += 1

    def select_file(self, fid):
        self.selected.append(fid)
        if fid == '3f00' and self.mf_select_error:
            # eUICC with the ISD-R ADF selected: MF is not selectable by FID
            raise SwMatchError('6a82', '9000')
        return ('', '9000')

    def select_adf(self, aid):
        raise SwMatchError('6a82', '9000')


class TestPickProfileNoReset(unittest.TestCase):
    def test_uicc_selected_without_any_reset(self):
        scc = FakeScc()
        profile = pick_profile_no_reset(scc)
        self.assertIsInstance(profile, CardProfileUICC)
        self.assertEqual(scc.resets, 0)
        self.assertIn('3f00', scc.selected)

    def test_reset_card_restored_after_pick(self):
        scc = FakeScc()
        pick_profile_no_reset(scc)
        scc.reset_card()
        self.assertEqual(scc.resets, 1)


class TestRestoreMfAfterProbe(unittest.TestCase):
    def test_normal_card_keeps_the_reset_free_path(self):
        scc = FakeScc()
        fastinit._restore_mf_after_probe(scc)
        self.assertEqual(scc.resets, 0)
        self.assertEqual(scc.selected[-1], '3f00')

    def test_adf_selected_card_falls_back_to_a_physical_reset(self):
        scc = FakeScc(mf_select_error=True)
        fastinit._restore_mf_after_probe(scc)
        self.assertEqual(scc.resets, 1)


class FakeLchan:
    def __init__(self):
        self.scc = types.SimpleNamespace(scp=object())
        self.selected_adf = 'SOMETHING'
        self.selected = []

    def select(self, path, cmd_app=None):
        self.selected.append(path)


class TestFastRuntimeStateSoftReset(unittest.TestCase):
    def make_rs(self):
        rs = FastRuntimeState.__new__(FastRuntimeState)
        rs.lchan = {0: FakeLchan(), 1: FakeLchan()}
        rs.adm_verified = True
        rs.card = types.SimpleNamespace(_scc=types.SimpleNamespace(get_atr=lambda: 'AABB'))
        rs.identity = {}
        return rs

    def test_soft_reset_selects_mf_without_physical_reset(self):
        rs = self.make_rs()
        atr = rs.soft_reset()
        self.assertEqual(atr, 'AABB')
        self.assertEqual(rs.identity['ATR'], 'AABB')
        self.assertEqual(rs.lchan[0].selected, ['MF'])
        self.assertIsNone(rs.lchan[0].selected_adf)
        self.assertFalse(rs.adm_verified)
        self.assertNotIn(1, rs.lchan)

    def test_reset_is_soft(self):
        rs = self.make_rs()
        rs.card = types.SimpleNamespace(_scc=types.SimpleNamespace(get_atr=lambda: 'EEFF'))
        self.assertEqual(rs.reset(), 'EEFF')
        self.assertEqual(rs.lchan[0].selected, ['MF'])


class FakeCardScc:
    def __init__(self):
        self.resets = 0

    def reset_card(self):
        self.resets += 1
        return 'ATR'

    def get_atr(self):
        return 'AABB'


class TestDoResetFast(unittest.TestCase):
    def test_explicit_reset_is_physical(self):
        scc = FakeCardScc()
        out = []
        app = types.SimpleNamespace(rs=None, card=types.SimpleNamespace(_scc=scc), poutput=out.append)
        do_reset_fast(app)
        self.assertEqual(scc.resets, 1)
        self.assertEqual(out, ['Card ATR: AABB'])

    def test_explicit_reset_uses_hard_reset_with_runtime_state(self):
        calls = []
        rs = types.SimpleNamespace(hard_reset=lambda cmd_app=None: calls.append(cmd_app) or 'CCDD')
        out = []
        app = types.SimpleNamespace(rs=rs, card=None, poutput=out.append)
        do_reset_fast(app)
        self.assertEqual(calls, [app])
        self.assertEqual(out, ['Card ATR: CCDD'])


if __name__ == '__main__':
    unittest.main()


class FlakyLchan:
    """Lchan whose first MF select fails, as if probing left the card in a
    context the software reset cannot clear."""

    def __init__(self):
        self.scc = types.SimpleNamespace(scp=object())
        self.selected_adf = 'SOMETHING'
        self.select_calls = 0
        self.selected = []

    def select(self, path, cmd_app=None):
        self.select_calls += 1
        if self.select_calls == 1:
            raise SwMatchError('6d00', '9000')
        self.selected.append(path)


class ResettableCard:
    def __init__(self):
        self.resets = 0
        self._scc = types.SimpleNamespace(get_atr=lambda: 'AABB')

    def reset(self):
        self.resets += 1
        return 'AABB'


class TestFastResetEscalation(unittest.TestCase):
    def make_rs(self):
        rs = FastRuntimeState.__new__(FastRuntimeState)
        rs.lchan = {0: FlakyLchan(), 1: types.SimpleNamespace(scc=types.SimpleNamespace(scp=None))}
        rs.adm_verified = True
        rs.card = ResettableCard()
        rs.identity = {}
        return rs

    def test_soft_reset_escalates_to_physical(self):
        rs = self.make_rs()
        atr = rs.reset()
        self.assertEqual(atr, 'AABB')
        self.assertEqual(rs.card.resets, 1)
        self.assertEqual(rs.lchan[0].select_calls, 2)
        self.assertEqual(rs.lchan[0].selected, ['MF'])
        self.assertFalse(rs.adm_verified)
        self.assertNotIn(1, rs.lchan)


class TestInitCardFastRetry(unittest.TestCase):
    def test_retries_once_after_physical_reset(self):
        calls = []

        def once(sl, skip, wait):
            calls.append(wait)
            if len(calls) == 1:
                raise SwMatchError('6d00', '9000')
            return ('rs', 'card')

        sl = types.SimpleNamespace(resets=0)

        def reset_card():
            sl.resets += 1

        sl.reset_card = reset_card
        with mock.patch.object(fastinit, '_init_card_once', side_effect=once):
            rs, card = fastinit.init_card_fast(sl, wait=True)
        self.assertEqual(calls, [True, False])
        self.assertEqual(sl.resets, 1)
        self.assertEqual((rs, card), ('rs', 'card'))


class TestDoEquipFastFailure(unittest.TestCase):
    def test_failed_equip_keeps_previous_state(self):
        calls = []
        app = types.SimpleNamespace(
            sl=object(),
            rs=types.SimpleNamespace(profile=types.SimpleNamespace(shell_cmdsets=[object()])),
            unregister_command_set=lambda cs: calls.append('unregister'),
            equip=lambda card, rs: calls.append('equip'),
        )
        with mock.patch.object(fastinit, 'init_card_fast', side_effect=SwMatchError('6d00', '9000')):
            with self.assertRaises(SwMatchError):
                fastinit.do_equip_fast(app)
        self.assertEqual(calls, [])
