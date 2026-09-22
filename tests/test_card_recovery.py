# coding=utf-8
"""Tests for the PC/SC failure recovery: presence-monitor watchdog and
transport recreation after a service failure (pcscd restart)."""

import unittest
from types import SimpleNamespace

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
