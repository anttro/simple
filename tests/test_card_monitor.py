#!/usr/bin/env python3
"""Tests for the passive PC/SC card-presence observer and auto-equip state."""

import sys
import types
import unittest
from pathlib import Path
from unittest import mock

PROJECTS = Path(__file__).resolve().parents[2]
PY_SIM = PROJECTS / 'pysim'
if str(PY_SIM) not in sys.path:
    sys.path.insert(0, str(PY_SIM))

import pysim_simple_server.server as S


class FakeCard:
    def __init__(self, reader):
        self.reader = reader


class TestCardPresenceObserver(unittest.TestCase):
    def setUp(self):
        self.observer = S._CardPresenceObserver('Test Reader 00 00')
        self.server = types.SimpleNamespace(card_present=True)
        self.saved_ref = S._server_ref
        S._server_ref = self.server
        self.disconnects = []
        self.patcher = mock.patch.object(
            S, '_handle_card_disconnect',
            side_effect=lambda: self.disconnects.append(True))
        self.patcher.start()
        self.trigger = mock.patch.object(S, '_auto_equip_trigger')
        self.trigger_mock = self.trigger.start()
        self.saved_auto = S._AUTO_EQUIP
        S._AUTO_EQUIP = True
        self.saved_stale = S._TRANSPORT_STALE
        S._TRANSPORT_STALE = False

    def tearDown(self):
        S._TRANSPORT_STALE = self.saved_stale
        S._AUTO_EQUIP = self.saved_auto
        self.trigger.stop()
        self.patcher.stop()
        S._server_ref = self.saved_ref

    def test_removal_of_our_reader_disconnects(self):
        self.observer.update(None, ([], [FakeCard('Test Reader 00 00')]))
        self.assertFalse(self.server.card_present)
        self.assertEqual(len(self.disconnects), 1)
        self.trigger_mock.assert_not_called()

    def test_removal_of_other_reader_ignored(self):
        self.observer.update(None, ([], [FakeCard('Other Reader 00 00')]))
        self.assertTrue(self.server.card_present)
        self.assertEqual(self.disconnects, [])
        self.trigger_mock.assert_not_called()

    def test_insertion_sets_card_present_and_triggers_auto_equip(self):
        self.server.card_present = False
        self.observer.update(None, ([FakeCard('Test Reader 00 00')], []))
        self.assertTrue(self.server.card_present)
        self.assertEqual(self.disconnects, [])
        self.trigger_mock.assert_called_once()

    def test_insertion_marks_the_transport_for_a_rebuild(self):
        # The first equip attempt must already use a fresh link: after a swap
        # the old one cannot connect on Windows, and a cardless-startup link
        # never connected (v3.32.4).
        S._TRANSPORT_STALE = False
        self.server.card_present = False
        self.observer.update(None, ([FakeCard('Test Reader 00 00')], []))
        self.assertTrue(S._TRANSPORT_STALE)
        self.trigger_mock.assert_called_once()

    def test_insertion_does_not_trigger_when_disabled(self):
        S._AUTO_EQUIP = False
        self.server.card_present = False
        self.observer.update(None, ([FakeCard('Test Reader 00 00')], []))
        self.assertTrue(self.server.card_present)
        self.trigger_mock.assert_not_called()

    def test_missing_reader_attribute_is_ignored(self):
        self.observer.update(None, ([], [types.SimpleNamespace()]))
        self.assertTrue(self.server.card_present)
        self.assertEqual(self.disconnects, [])
        self.trigger_mock.assert_not_called()


class TestAutoEquipTrigger(unittest.TestCase):
    def tearDown(self):
        S._AUTO_EQUIP = True
        S._AUTO_EQUIP_BUSY = False

    def test_disabled_does_not_spawn(self):
        S._AUTO_EQUIP = False
        with mock.patch.object(S.threading, 'Thread') as thread:
            S._auto_equip_trigger()
            thread.assert_not_called()

    def test_busy_does_not_spawn_twice(self):
        S._AUTO_EQUIP = True
        S._AUTO_EQUIP_BUSY = True
        with mock.patch.object(S.threading, 'Thread') as thread:
            S._auto_equip_trigger()
            thread.assert_not_called()

    def test_spawns_worker_once(self):
        S._AUTO_EQUIP = True
        S._AUTO_EQUIP_BUSY = False
        with mock.patch.object(S.threading, 'Thread') as thread:
            S._auto_equip_trigger()
            thread.assert_called_once_with(target=S._auto_equip_worker, name='auto-equip', daemon=True)


class TestCardSession(unittest.TestCase):
    def test_disconnect_bumps_session_and_clears_equipping(self):
        server = types.SimpleNamespace(
            card_session=5, card=object(), scc=object(), stk_pending=object(),
            menu_active=True, event_list=[1], sim_menu={}, equipping=True)
        saved = S._server_ref
        S._server_ref = server
        try:
            S._handle_card_disconnect()
        finally:
            S._server_ref = saved
        self.assertEqual(server.card_session, 6)
        self.assertFalse(server.equipping)
        self.assertIsNone(server.card)


class TestApplyEquippedCard(unittest.TestCase):
    def test_updates_state_and_bumps_session(self):
        scc = types.SimpleNamespace(cat_cla=None)
        card = types.SimpleNamespace(_scc=scc, name='Card')
        app = types.SimpleNamespace(card=card)
        server = types.SimpleNamespace(
            app=app, card=None, scc=None, stk_pending=object(), menu_active=True,
            event_list=[1], sim_menu={}, card_session=2, card_present=False,
            equipping=False, terminal_profile='7F')
        saved_ref, saved_conn = S._server_ref, S._CARD_CONNECTED
        S._server_ref = server
        S._CARD_CONNECTED = False
        try:
            with mock.patch.object(S, '_send_terminal_profile', return_value=('menu', ['ev'])):
                with mock.patch.object(S, '_poll_enable'):
                    S._apply_equipped_card(server)
            connected_after = S._CARD_CONNECTED
        finally:
            S._server_ref = saved_ref
            S._CARD_CONNECTED = saved_conn
        self.assertTrue(connected_after)
        self.assertIs(server.card, card)
        self.assertIs(server.scc, scc)
        self.assertEqual(server.card_session, 3)
        self.assertTrue(server.card_present)
        self.assertEqual(server.sim_menu, 'menu')
        self.assertEqual(server.event_list, ['ev'])


if __name__ == '__main__':
    unittest.main()
