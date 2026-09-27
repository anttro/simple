#!/usr/bin/env python3
"""Unit tests for the generic BIP terminal control (Simulator -> BIP pill).

The three modes share the SCP81 BIP terminal but are independent of HTTP OTA:
'sink' accepts and only logs (never answers), 'redirect' forwards to a fixed
target and 'passthru' dials the destination from the card's OPEN CHANNEL.
Only one BIP session runs at a time - starting either control replaces the
other.
"""

import socket
import sys
import threading
import time
import unittest
from pathlib import Path

PROJECTS = Path(__file__).resolve().parents[2]
PY_SIM = PROJECTS / 'pysim'
if str(PY_SIM) not in sys.path:
    sys.path.insert(0, str(PY_SIM))

import pysim_simple_server.server as server


class PeerServer(threading.Thread):
    """Tiny TCP peer: accepts one connection, greets, records what it gets."""

    def __init__(self, greeting=b''):
        super().__init__(daemon=True)
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(('127.0.0.1', 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.greeting = greeting
        self.received = b''
        self.conn = None
        self.ready = threading.Event()
        self.done = threading.Event()

    def run(self):
        self.sock.settimeout(3)
        try:
            self.conn, _ = self.sock.accept()
        except OSError:
            return
        self.ready.set()
        if self.greeting:
            self.conn.sendall(self.greeting)
        self.conn.settimeout(3)
        try:
            while True:
                data = self.conn.recv(4096)
                if not data:
                    break
                self.received += data
        except OSError:
            pass
        self.done.set()

    def stop(self):
        for s in (self.conn, self.sock):
            try:
                if s:
                    s.close()
            except OSError:
                pass


def drain(channel_id, tries=20):
    """Receive from a BIP channel with a short retry loop."""
    for _ in range(tries):
        data = server._BIP.receive(channel_id, 100)
        if data:
            return data
        time.sleep(0.05)
    return b''


def kinds():
    return [e['kind'] for e in server._BIP.entries_after(0)]


class BipControlTest(unittest.TestCase):
    def tearDown(self):
        server._bip_control({'action': 'stop'})

    def test_sink_is_the_default_and_accepts_ephemeral_ports(self):
        resp = server._bip_control({})
        self.assertTrue(resp['ok'], resp)
        self.assertEqual(resp['owner'], 'bip')
        self.assertEqual(resp['bip']['mode'], 'redirect')      # BIP-level mode
        self.assertEqual(resp['listener']['mode'], 'sink')
        self.assertEqual(resp['listener']['host'], '127.0.0.1')
        self.assertGreater(resp['listener']['port'], 0)        # 0 -> ephemeral
        self.assertIsNone(resp['replaced'])

    def test_sink_accepts_and_logs_but_never_answers(self):
        resp = server._bip_control({'mode': 'sink', 'port': 0})
        port = resp['listener']['port']
        cid, err = server._BIP.open('10.9.9.9', 1234, 512)
        self.assertIsNone(err)
        self.assertEqual(server._BIP.channels[cid].target, ('127.0.0.1', port))
        self.assertTrue(server._BIP.send(cid, b'CARDHELLO'))
        # the sink receives and logs the bytes ...
        for _ in range(40):
            if 'sink-rx' in kinds():
                break
            time.sleep(0.05)
        rx = [e for e in server._BIP.entries_after(0) if e['kind'] == 'sink-rx']
        self.assertTrue(rx, kinds())
        self.assertEqual(rx[0]['bytes'], len(b'CARDHELLO'))
        self.assertEqual(rx[0]['hex'], b'CARDHELLO'.hex().upper())
        # ... and sends nothing back
        self.assertEqual(server._BIP.available(cid), 0)
        self.assertEqual(drain(cid), b'')
        # the listener reports the accepted connection
        st = server._bip_status_body()
        self.assertEqual(st['listener']['mode'], 'sink')
        self.assertEqual(st['listener']['connections'], 1)

    def test_redirect_forwards_to_the_fixed_target(self):
        peer = PeerServer(greeting=b'PLATFORM')
        peer.start()
        try:
            resp = server._bip_control({'mode': 'redirect',
                                        'host': '127.0.0.1', 'port': peer.port})
            self.assertTrue(resp['ok'], resp)
            self.assertEqual(resp['listener']['mode'], 'redirect')
            self.assertEqual(resp['bip']['target'], '127.0.0.1:%d' % peer.port)
            cid, err = server._BIP.open('10.9.9.9', 10174, 512)
            self.assertIsNone(err)
            self.assertTrue(server._BIP.send(cid, b'CARDHELLO'))
            self.assertEqual(drain(cid), b'PLATFORM')
            server._BIP.close(cid)
            peer.done.wait(3)
            self.assertEqual(peer.received, b'CARDHELLO')
        finally:
            peer.stop()

    def test_passthru_dials_the_destination_from_open_channel(self):
        peer = PeerServer(greeting=b'PLATFORM')
        peer.start()
        try:
            resp = server._bip_control({'mode': 'passthru'})
            self.assertTrue(resp['ok'], resp)
            self.assertEqual(resp['listener'], {'mode': 'passthru'})
            cid, err = server._BIP.open('127.0.0.1', peer.port, 512, proto=0x02)
            self.assertIsNone(err)
            self.assertEqual(server._BIP.channels[cid].target,
                             ('127.0.0.1', peer.port))
            self.assertTrue(server._BIP.send(cid, b'CARDHELLO'))
            self.assertEqual(drain(cid), b'PLATFORM')
        finally:
            peer.stop()

    def test_starting_bip_replaces_the_scp81_listener(self):
        peer = PeerServer()
        peer.start()
        try:
            resp = server._scp81_bip_control({'action': 'start', 'mode': 'redirect',
                                              'host': '127.0.0.1', 'port': peer.port})
            self.assertTrue(resp['ok'], resp)
            self.assertEqual(server._BIP_OWNER, 'scp81')
            # the BIP pill takes the terminal over: the SCP81 session is
            # reported as replaced and no longer owns the terminal
            resp = server._bip_control({'mode': 'sink', 'port': 0})
            self.assertTrue(resp['ok'], resp)
            self.assertEqual(resp['replaced'], {'owner': 'scp81', 'mode': 'redirect'})
            self.assertEqual(server._BIP_OWNER, 'bip')
            self.assertEqual(server._BIP_MODE, 'sink')
        finally:
            peer.stop()

    def test_scp81_start_replaces_the_bip_session(self):
        resp = server._bip_control({'mode': 'sink', 'port': 0})
        self.assertTrue(resp['ok'], resp)
        sink_port = resp['listener']['port']
        peer = PeerServer()
        peer.start()
        try:
            resp = server._scp81_bip_control({'action': 'start', 'mode': 'redirect',
                                              'host': '127.0.0.1', 'port': peer.port})
            self.assertTrue(resp['ok'], resp)
            self.assertEqual(resp['replaced'], {'owner': 'bip', 'mode': 'sink'})
            self.assertEqual(server._BIP_OWNER, 'scp81')
            self.assertEqual(server._BIP_MODE, 'redirect')
        finally:
            peer.stop()
        # the old sink listener is gone: its port accepts nothing anymore
        probe = socket.socket()
        probe.settimeout(0.3)
        self.addCleanup(probe.close)
        with self.assertRaises(OSError):
            probe.connect(('127.0.0.1', sink_port))

    def test_status_reports_the_owner(self):
        st = server._bip_status_body()
        self.assertIsNone(st['owner'])
        self.assertFalse(st['bip']['enabled'])
        server._bip_control({'mode': 'passthru'})
        st = server._bip_status_body()
        self.assertEqual(st['owner'], 'bip')
        self.assertTrue(st['bip']['enabled'])
        self.assertEqual(st['listener'], {'mode': 'passthru'})

    def test_stop_reports_what_was_running(self):
        server._bip_control({'mode': 'passthru'})
        resp = server._bip_control({'action': 'stop'})
        self.assertTrue(resp['ok'], resp)
        self.assertEqual(resp['replaced'], {'owner': 'bip', 'mode': 'passthru'})
        self.assertIsNone(resp['owner'])
        self.assertFalse(resp['bip']['enabled'])
        # stopping an idle terminal is fine too
        resp = server._bip_control({'action': 'stop'})
        self.assertTrue(resp['ok'], resp)
        self.assertIsNone(resp['replaced'])

    def test_invalid_requests_do_not_start_a_session(self):
        for body in ({'mode': 'udp'},
                     {'mode': 'redirect', 'port': 1234},            # no host
                     {'mode': 'redirect', 'host': '127.0.0.1'},     # no port
                     {'mode': 'redirect', 'host': '127.0.0.1', 'port': 'x'},
                     {'mode': 'redirect', 'host': '127.0.0.1', 'port': 70000},
                     {'mode': 'sink', 'port': 'x'},
                     {'mode': 'sink', 'port': 70000}):
            resp = server._bip_control(body)
            self.assertFalse(resp['ok'], resp)
            self.assertTrue(resp['error'], resp)
            self.assertFalse(server._BIP.enabled, body)
            self.assertIsNone(server._BIP_OWNER, body)
        # a valid start still works afterwards
        self.assertTrue(server._bip_control({'mode': 'sink', 'port': 0})['ok'])


if __name__ == '__main__':
    unittest.main()
