# coding=utf-8
"""Tests for the PC/SC failure recovery: presence-monitor watchdog and
transport recreation after a service failure (pcscd restart)."""

import unittest
from io import StringIO
from types import SimpleNamespace
from unittest import mock

from pysim_simple_server import server


class PcscErrorTests(unittest.TestCase):
    def test_pyscard_exception_counts_as_pcsc_error(self):
        class FakePcscError(Exception):
            def __init__(self):
                self.hresult = 0x8010001D   # SCARD_E_NO_SERVICE

        self.assertTrue(server._is_pcsc_error(FakePcscError()))

    def test_card_level_errors_do_not(self):
        self.assertFalse(server._is_pcsc_error(RuntimeError('SW match failed')))
        self.assertFalse(server._is_pcsc_error(Exception()))


class TransportFatalTests(unittest.TestCase):
    @staticmethod
    def _exc(hresult):
        exc = RuntimeError('pcsc')
        exc.hresult = hresult
        return exc

    def test_card_level_errors_do_not_rebuild_the_transport(self):
        # A normal card swap (SCARD_W_REMOVED_CARD) is recovered by a plain
        # reconnect on the existing link - it must not trigger a rebuild.
        for hr in (0x80100069, 0x8010000C, 0x80100068, 0x80100066, 0x80100067):
            self.assertFalse(server._is_transport_fatal(self._exc(hr)), hex(hr))

    def test_service_errors_rebuild_the_transport(self):
        for hr in (0x8010001D, 0x8010001E, 0x8010002E, 0x80100003):
            self.assertTrue(server._is_transport_fatal(self._exc(hr)), hex(hr))

    def test_non_pcsc_errors_are_not_transport_fatal(self):
        self.assertFalse(server._is_transport_fatal(RuntimeError('SW match failed')))


class WatchdogTests(unittest.TestCase):
    def test_alive_monitor_is_left_alone(self):
        calls = []
        rv = server._watchdog_tick('reader', alive_fn=lambda: True,
                                   restart_fn=lambda name: calls.append(name))
        self.assertEqual(rv, 'ok')
        self.assertEqual(calls, [])

    def test_stopped_monitor_is_restarted(self):
        calls = []
        rv = server._watchdog_tick('reader', alive_fn=lambda: False,
                                   restart_fn=lambda name: calls.append(name))
        self.assertEqual(rv, 'restarted')
        self.assertEqual(calls, ['reader'])


class EnsureTransportTests(unittest.TestCase):
    def setUp(self):
        server._TRANSPORT_STALE = False

    def tearDown(self):
        server._TRANSPORT_STALE = False

    @staticmethod
    def make_server(factory):
        app = SimpleNamespace(sl='old-sl')
        return SimpleNamespace(sl='old-sl', scc='old-scc', card='old-card',
                               app=app, transport_factory=factory)

    def test_fresh_transport_is_kept(self):
        calls = []
        srv = self.make_server(lambda: calls.append(1) or 'new-sl')
        self.assertTrue(server._ensure_transport(srv))
        self.assertEqual(calls, [])
        self.assertEqual(srv.sl, 'old-sl')

    def test_stale_transport_is_recreated(self):
        srv = self.make_server(lambda: 'new-sl')
        server._TRANSPORT_STALE = True
        self.assertTrue(server._ensure_transport(srv))
        self.assertEqual(srv.sl, 'new-sl')
        self.assertEqual(srv.app.sl, 'new-sl')
        self.assertIsNone(srv.scc)
        self.assertIsNone(srv.card)
        self.assertFalse(server._TRANSPORT_STALE)

    def test_failed_reconnect_keeps_the_transport_stale(self):
        def boom():
            raise RuntimeError('service not available')

        srv = self.make_server(boom)
        server._TRANSPORT_STALE = True
        self.assertFalse(server._ensure_transport(srv))
        self.assertTrue(server._TRANSPORT_STALE)
        self.assertEqual(srv.sl, 'old-sl')

    def test_old_link_is_released_before_the_rebuild(self):
        released = []

        class FakeLink:
            def disconnect(self):
                released.append(True)

        old = FakeLink()
        app = SimpleNamespace(sl=old, card='card', rs='rs', lchan='lchan')
        srv = SimpleNamespace(sl=old, scc='scc', card='card', app=app,
                              transport_factory=lambda: 'new-sl')
        server._TRANSPORT_STALE = True
        self.assertTrue(server._ensure_transport(srv))
        self.assertEqual(released, [True])
        self.assertEqual(srv.sl, 'new-sl')
        self.assertEqual(app.sl, 'new-sl')
        self.assertIsNone(app.card)
        self.assertIsNone(app.rs)
        self.assertIsNone(app.lchan)

    def test_no_factory_clears_the_flag(self):
        srv = SimpleNamespace(sl='old-sl', app=None, transport_factory=None)
        server._TRANSPORT_STALE = True
        self.assertTrue(server._ensure_transport(srv))
        self.assertFalse(server._TRANSPORT_STALE)

    def test_none_server_is_a_noop(self):
        server._TRANSPORT_STALE = True
        self.assertTrue(server._ensure_transport(None))
        self.assertTrue(server._TRANSPORT_STALE)


class DisconnectTests(unittest.TestCase):
    def setUp(self):
        server._TRANSPORT_STALE = False

    def tearDown(self):
        server._TRANSPORT_STALE = False

    def test_stale_disconnect_marks_the_transport(self):
        server._handle_card_disconnect(stale=True)
        self.assertTrue(server._TRANSPORT_STALE)

    def test_plain_disconnect_leaves_the_transport_usable(self):
        server._handle_card_disconnect()
        self.assertFalse(server._TRANSPORT_STALE)

    def test_disconnect_unequips_the_shell_first(self):
        # PysimApp.equip(None, None) unregisters the old profile's command
        # sets; without it the next equip fails with "CommandSet ... is
        # already installed" and the file tree breaks.
        calls = []
        app = SimpleNamespace(rs='rs', card='card', lchan='lchan',
                              stdout=StringIO(),
                              equip=lambda c, r: calls.append((c, r)))
        srv = SimpleNamespace(app=app, card='card', scc='scc', stk_pending=None,
                              menu_active=False, event_list=None, sim_menu=None,
                              iccid=None, net_state=None, equipping=False, card_session=1)
        saved = server._server_ref
        server._server_ref = srv
        try:
            server._handle_card_disconnect()
        finally:
            server._server_ref = saved
        self.assertEqual(calls, [(None, None)])
        self.assertIsNone(app.card)
        self.assertIsNone(app.rs)
        self.assertIsNone(app.lchan)

    def test_disconnect_clears_the_app_card_state(self):
        # The dead card must not stay reachable through app.card/app.rs:
        # handlers using app.rs kept transmitting over the removed card and
        # the old PC/SC link stayed connected (the v3.5.1 auto-equip bug).
        app = SimpleNamespace(card='card', rs='rs', lchan='lchan')
        srv = SimpleNamespace(app=app, card='card', scc='scc', stk_pending='x',
                              menu_active=True, event_list='e', sim_menu='m',
                              iccid='123', net_state='n', equipping=True, card_session=5)
        saved = server._server_ref
        server._server_ref = srv
        try:
            server._handle_card_disconnect()
        finally:
            server._server_ref = saved
        self.assertIsNone(app.card)
        self.assertIsNone(app.rs)
        self.assertIsNone(app.lchan)
        self.assertIsNone(srv.card)
        self.assertIsNone(srv.scc)


class AutoEquipTests(unittest.TestCase):
    def setUp(self):
        self.saved = (server._server_ref, server._CARD_CONNECTED, server._TRANSPORT_STALE,
                      server._AUTO_EQUIP, server._AUTO_EQUIP_BUSY, server._AUTO_EQUIP_LAST,
                      server._AUTO_EQUIP_BACKOFF)
        server._server_ref = None
        server._CARD_CONNECTED = False
        server._TRANSPORT_STALE = False
        server._AUTO_EQUIP = True
        server._AUTO_EQUIP_BUSY = False
        server._AUTO_EQUIP_LAST = 0.0
        server._AUTO_EQUIP_BACKOFF = server._AUTO_EQUIP_REARM_DELAY

    def tearDown(self):
        (server._server_ref, server._CARD_CONNECTED, server._TRANSPORT_STALE,
         server._AUTO_EQUIP, server._AUTO_EQUIP_BUSY, server._AUTO_EQUIP_LAST,
         server._AUTO_EQUIP_BACKOFF) = self.saved

    @staticmethod
    def make_server(onecmd):
        app = SimpleNamespace(stdout=StringIO(), card=None)
        app.onecmd_plus_hooks = onecmd
        return SimpleNamespace(app=app, card=None, scc=None, card_present=True,
                               equipping=False, terminal_profile='tp', card_session=1)

    def test_retries_after_a_transient_card_level_failure(self):
        state = {'calls': 0}
        srv = self.make_server(None)

        def onecmd(cmd):
            state['calls'] += 1
            if state['calls'] == 1:
                raise RuntimeError('Failed to transmit with protocol T0. Card was removed.')
            srv.app.card = SimpleNamespace(_scc='scc')

        srv.app.onecmd_plus_hooks = onecmd
        applied = []
        with mock.patch.object(server, '_ensure_transport', lambda s: True), \
             mock.patch.object(server, '_apply_equipped_card', lambda s: applied.append(s)):
            self.assertTrue(server._auto_equip_attempts(srv, 3, lambda s: None))
        self.assertEqual(state['calls'], 2)
        self.assertEqual(applied, [srv])
        self.assertFalse(server._TRANSPORT_STALE)

    def test_card_level_failure_keeps_the_transport(self):
        srv = self.make_server(None)

        def onecmd(cmd):
            exc = RuntimeError('Card was removed.')
            exc.hresult = 0x80100069
            raise exc

        srv.app.onecmd_plus_hooks = onecmd
        with mock.patch.object(server, '_ensure_transport', lambda s: True):
            self.assertFalse(server._auto_equip_attempt(srv))
        self.assertFalse(server._TRANSPORT_STALE)

    def test_transport_fatal_failure_marks_the_transport(self):
        srv = self.make_server(None)

        def onecmd(cmd):
            exc = RuntimeError('service not available')
            exc.hresult = 0x8010001D
            raise exc

        srv.app.onecmd_plus_hooks = onecmd
        with mock.patch.object(server, '_ensure_transport', lambda s: True):
            self.assertFalse(server._auto_equip_attempt(srv))
        self.assertTrue(server._TRANSPORT_STALE)

    def test_rearm_triggers_with_cooldown(self):
        calls = []
        server._server_ref = SimpleNamespace(card_present=True, equipping=False)
        with mock.patch.object(server, '_auto_equip_trigger',
                               lambda: calls.append(1) or True):
            self.assertTrue(server._auto_equip_rearm(now=100.0))
            self.assertFalse(server._auto_equip_rearm(now=100.5))
            self.assertTrue(server._auto_equip_rearm(now=106.0))
        self.assertEqual(len(calls), 2)

    def test_failed_worker_backs_off_the_rearm(self):
        state = {'calls': 0}
        srv = self.make_server(None)

        def onecmd(cmd):
            state['calls'] += 1
            raise RuntimeError('card cannot be initialized')

        srv.app.onecmd_plus_hooks = onecmd
        server._server_ref = srv
        with mock.patch.object(server, '_ensure_transport', lambda s: True), \
             mock.patch.object(server, '_apply_equipped_card', lambda s: None), \
             mock.patch.object(server, '_AUTO_EQUIP_ATTEMPTS', 1):
            server._auto_equip_worker()
        self.assertEqual(state['calls'], 1)
        self.assertGreater(server._AUTO_EQUIP_BACKOFF, server._AUTO_EQUIP_REARM_DELAY)

    def test_rearm_needs_a_present_card_and_no_session(self):
        calls = []
        server._server_ref = SimpleNamespace(card_present=True, equipping=False)
        with mock.patch.object(server, '_auto_equip_trigger',
                               lambda: calls.append(1) or True):
            server._CARD_CONNECTED = True
            self.assertFalse(server._auto_equip_rearm(now=100.0))
            server._CARD_CONNECTED = False
            server._server_ref.card_present = False
            self.assertFalse(server._auto_equip_rearm(now=100.0))
            server._server_ref.card_present = True
            server._server_ref.equipping = True
            self.assertFalse(server._auto_equip_rearm(now=100.0))
        self.assertEqual(calls, [])
