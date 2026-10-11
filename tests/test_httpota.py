#!/usr/bin/env python3
"""Unit tests for the HTTP OTA (SCP81) BIP terminal emulation (Phase A).

TR byte vectors come from the captured real-terminal traces in
samples/HTTP_OTA/traces (OPEN CHANNEL success/failure, SEND/RECEIVE/CLOSE).
No live card or live card data is used here.
"""

import socket
import sys
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

PROJECTS = Path(__file__).resolve().parents[2]
PY_SIM = PROJECTS / 'pysim'
if str(PY_SIM) not in sys.path:
    sys.path.insert(0, str(PY_SIM))

from pysim_simple_server import httpota
import pysim_simple_server.server as server


OPEN_LOCALHOST = bytes.fromhex(
    'd02b010301400102028182050035010339020200470b076d656761666f6e2e7275'
    '3c03021f903e05217f000001')

TR_OPEN_OK = '0103014001020282810301003802810035010339020200'
TR_OPEN_FAIL = '01030140010202828103023a0035010339020200'
# the same vector as sent by the handler: the command details echo the
# command's tag form (the test raws use 81/82, TS 102 223 6.8.1)
TR_OPEN_OK_ECHO = '8103014001020282810301003802810035010339020200'


class PeerServer(threading.Thread):
    """Tiny TCP peer: accepts one connection, greets, records what it receives."""

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
        self.conn.settimeout(2)
        deadline = time.time() + 3
        try:
            while time.time() < deadline:
                try:
                    data = self.conn.recv(4096)
                except socket.timeout:
                    break
                if not data:
                    break
                self.received += data
        except OSError:
            pass
        self.done.set()

    def stop(self):
        self.sock.close()


def open_cmd(host, port, buffer_size=512):
    ip = bytes(int(x) for x in host.split('.'))
    tlvs = (b'\x81\x03\x01\x40\x01'
            b'\x82\x02\x81\x82'
            b'\x35\x01\x03'
            b'\x39\x02' + buffer_size.to_bytes(2, 'big') +
            b'\x3c\x03\x02' + port.to_bytes(2, 'big') +
            b'\x3e\x05\x21' + ip)
    return b'\xd0' + bytes([len(tlvs)]) + tlvs


def channel_cmd(cmd_type, qualifier, data_tlvs=b''):
    tlvs = (bytes([0x81, 0x03, 0x01, cmd_type, qualifier]) +
            b'\x82\x02\x81\x21' + data_tlvs)
    return b'\xd0' + bytes([len(tlvs)]) + tlvs


class TlvTest(unittest.TestCase):
    def test_proactive_tlvs_open_channel(self):
        tlvs = httpota.proactive_tlvs(OPEN_LOCALHOST)
        self.assertEqual(tlvs[httpota.TAG_BEARER], b'\x03')
        self.assertEqual(tlvs[httpota.TAG_BUFFER_SIZE], b'\x02\x00')
        self.assertEqual(tlvs[httpota.TAG_NAA], b'\x07megafon.ru')
        self.assertEqual(httpota.parse_transport_level(tlvs[httpota.TAG_TRANSPORT_LEVEL]), (0x02, 8080))
        self.assertEqual(httpota.parse_other_address(tlvs[httpota.TAG_OTHER_ADDRESS]), '127.0.0.1')


class TrVectorTest(unittest.TestCase):
    def test_open_channel_success_vector(self):
        extra = bytes([0x38, 0x02, 0x81, 0x00]) + bytes([0x35, 0x01, 0x03]) + bytes([0x39, 0x02, 0x02, 0x00])
        tr = server._bip_tr(1, 0x40, 0x01, 0x81, 0x82, 0x00, None, extra)
        self.assertEqual(tr.hex(), TR_OPEN_OK)

    def test_open_channel_failure_vector(self):
        extra = bytes([0x35, 0x01, 0x03]) + bytes([0x39, 0x02, 0x02, 0x00])
        tr = server._bip_tr(1, 0x40, 0x01, 0x81, 0x82, 0x3A, 0x00, extra)
        self.assertEqual(tr.hex(), TR_OPEN_FAIL)

    def test_disabled_bip_fails_open_channel(self):
        old = server._BIP
        try:
            server._BIP = httpota.BipTerminal()
            tr = server._handle_bip_command(None, 1, 0x40, 0x01, OPEN_LOCALHOST, 0x81, 0x82)
            self.assertEqual(tr.hex(), TR_OPEN_FAIL)
        finally:
            server._BIP = old


class BipTerminalTest(unittest.TestCase):
    def test_redirect_and_roundtrip(self):
        peer = PeerServer(greeting=b'SERVERHELLO')
        peer.start()
        bip = httpota.BipTerminal()
        bip.enable('127.0.0.1', peer.port)
        cid, err = bip.open('10.9.9.9', 1234, 512)
        self.assertIsNone(err)
        self.assertEqual(bip.channels[cid].requested, '10.9.9.9:1234')
        self.assertEqual(bip.channels[cid].target, ('127.0.0.1', peer.port))
        self.assertTrue(bip.send(cid, b'CLIENTHELLO'))
        data = b''
        for _ in range(20):
            data = bip.receive(cid, 100)
            if data:
                break
            time.sleep(0.05)
        self.assertEqual(data, b'SERVERHELLO')
        self.assertTrue(bip.close(cid))
        peer.done.wait(3)
        self.assertEqual(peer.received, b'CLIENTHELLO')
        kinds = [e['kind'] for e in bip.entries_after(0)]
        self.assertIn('open', kinds)
        self.assertIn('send', kinds)
        self.assertIn('receive', kinds)
        self.assertIn('close', kinds)
        peer.stop()

    def test_redirect_mode_roundtrip_via_bip_control(self):
        # SCP81 redirect: the control API enables BIP with the external
        # platform as the target and starts no local listener; the card's
        # channel talks straight to that platform.
        peer = PeerServer(greeting=b'PLATFORM')
        peer.start()
        try:
            resp = server._scp81_bip_control({'action': 'start', 'mode': 'redirect',
                                              'host': '127.0.0.1', 'port': peer.port})
            self.assertTrue(resp['ok'], resp)
            self.assertEqual(resp['listener']['mode'], 'redirect')
            self.assertEqual(server._BIP.target, ('127.0.0.1', peer.port))
            cid, err = server._BIP.open('10.9.9.9', 10174, 512)
            self.assertIsNone(err)
            self.assertTrue(server._BIP.send(cid, b'CARDHELLO'))
            data = b''
            for _ in range(20):
                data = server._BIP.receive(cid, 100)
                if data:
                    break
                time.sleep(0.05)
            self.assertEqual(data, b'PLATFORM')
        finally:
            server._scp81_bip_control({'action': 'stop'})
            peer.stop()

    def test_disabled_terminal_refuses_open(self):
        bip = httpota.BipTerminal()
        cid, err = bip.open('127.0.0.1', 1, 512)
        self.assertIsNone(cid)
        self.assertIn('disabled', err)

    def test_passthru_dials_the_requested_destination(self):
        # passthru has no pinned target: the socket goes to the destination
        # the card requested in OPEN CHANNEL (TCP client, remote).
        peer = PeerServer(greeting=b'PLATFORM')
        peer.start()
        try:
            bip = httpota.BipTerminal()
            bip.enable(mode='passthru')
            self.assertIsNone(bip.target)
            self.assertEqual(bip.status()['mode'], 'passthru')
            cid, err = bip.open('127.0.0.1', peer.port, 512, proto=0x02)
            self.assertIsNone(err)
            self.assertEqual(bip.channels[cid].target, ('127.0.0.1', peer.port))
            self.assertTrue(bip.send(cid, b'CARDHELLO'))
            data = b''
            for _ in range(20):
                data = bip.receive(cid, 100)
                if data:
                    break
                time.sleep(0.05)
            self.assertEqual(data, b'PLATFORM')
            self.assertTrue(bip.close(cid))
        finally:
            peer.stop()

    def test_passthru_rejects_incomplete_or_non_tcp_requests(self):
        # The specs define no default port (TS 102 223 8.59): anything but a
        # complete TCP-client remote request fails the channel (result 3A
        # upstream) with a visible log reason.
        srv = socket.socket()
        srv.bind(('127.0.0.1', 0))
        dead_port = srv.getsockname()[1]
        srv.close()
        bip = httpota.BipTerminal()
        bip.enable(mode='passthru')
        self.assertIsNone(bip.open('-', 0, 512, proto=0x02)[0])              # no address
        self.assertIsNone(bip.open('127.0.0.1', 0, 512, proto=0x02)[0])      # no port
        self.assertIsNone(bip.open('127.0.0.1', 1234, 512, proto=0x03)[0])   # TCP server mode
        self.assertIsNone(bip.open('127.0.0.1', dead_port, 512, proto=0x02)[0])  # refused
        reasons = [e.get('reason', '') for e in bip.entries_after(0)
                   if e['kind'] == 'open-fail']
        self.assertTrue(any('TCP client' in r for r in reasons), reasons)
        self.assertTrue(any('valid port' in r for r in reasons), reasons)
        self.assertTrue(any('address' in r for r in reasons), reasons)

    def test_peer_close_queues_channel_status_event(self):
        # TS 102 223 7.5.11: a link lost outside a proactive command must be
        # reported to the UICC (channel id, link not established, info 05).
        srv = socket.socket()
        srv.bind(('127.0.0.1', 0))
        srv.listen(1)
        try:
            bip = httpota.BipTerminal()
            bip.enable('127.0.0.1', srv.getsockname()[1])
            cid, err = bip.open('10.9.9.9', 1234, 512)
            self.assertIsNone(err)
            conn, _ = srv.accept()
            conn.close()
            events = []
            for _ in range(40):
                bip.receive(cid, 16)
                events = bip.take_pending_events()
                if events:
                    break
                time.sleep(0.05)
            self.assertEqual(events, [{'channel': cid, 'status': cid, 'info': 0x05}])
        finally:
            srv.close()

    def test_channel_status_queued_once_per_channel(self):
        bip = httpota.BipTerminal()
        bip._queue_link_status(3)
        bip._queue_link_status(3)
        self.assertEqual(bip.take_pending_events(),
                         [{'channel': 3, 'status': 3, 'info': 0x05}])
        self.assertEqual(bip.take_pending_events(), [])

    def test_proactive_close_does_not_queue_status(self):
        # A CLOSE CHANNEL proactive command is not an autonomous link change.
        peer = PeerServer()
        peer.start()
        try:
            bip = httpota.BipTerminal()
            bip.enable('127.0.0.1', peer.port)
            cid, err = bip.open('10.9.9.9', 1234, 512)
            self.assertIsNone(err)
            self.assertTrue(bip.close(cid))
            self.assertEqual(bip.take_pending_events(), [])
        finally:
            peer.stop()

    def test_dump_server_logs_received_bytes(self):
        received = []
        dump = httpota.TcpDumpServer('127.0.0.1', 0, on_rx=lambda peer, data: received.append(data))
        c = socket.create_connection(('127.0.0.1', dump.port), timeout=2)
        c.sendall(b'HELLOCARD')
        deadline = time.time() + 2
        while time.time() < deadline and not received:
            time.sleep(0.02)
        c.close()
        dump.stop()
        self.assertEqual(b''.join(received), b'HELLOCARD')


def parse_tr(tr):
    """Parse a BIP TERMINAL RESPONSE payload into {tag: value}."""
    out = {}
    off = 0
    while off + 1 < len(tr):
        tag, ln = tr[off], tr[off + 1]
        out[tag] = tr[off + 2:off + 2 + ln]
        off += 2 + ln
    return out


class BipCommandFlowTest(unittest.TestCase):
    def setUp(self):
        self.peer = PeerServer(greeting=b'SERVERHELLO')
        self.peer.start()
        self.old = server._BIP
        self.bip = httpota.BipTerminal()
        self.bip.enable('127.0.0.1', self.peer.port)
        server._BIP = self.bip

    def tearDown(self):
        server._BIP = self.old
        self.peer.stop()

    def test_open_send_receive_close_flow(self):
        tr = server._handle_bip_command(None, 1, 0x40, 0x01, open_cmd('127.0.0.1', self.peer.port), 0x81, 0x82)
        self.assertEqual(tr.hex(), TR_OPEN_OK_ECHO)

        tr = server._handle_bip_command(None, 1, 0x43, 0x01,
                                        channel_cmd(0x43, 0x01, bytes([0x36, 0x08]) + b'CLIENTHE'),
                                        0x81, 0x21)
        tlvs = parse_tr(tr)
        self.assertEqual(tlvs[0x81].hex(), '014301')
        self.assertEqual(tlvs[0x02].hex(), '8281')
        self.assertEqual(tlvs[0x03], b'\x00')
        self.assertEqual(tlvs[0x37], b'\xff')

        data = b''
        for _ in range(20):
            tr = server._handle_bip_command(None, 1, 0x42, 0x00, channel_cmd(0x42, 0x00, bytes([0x37, 0x01, 0x64])), 0x81, 0x21)
            tlvs = parse_tr(tr)
            if 0x36 in tlvs and tlvs[0x36]:
                data += tlvs[0x36]
                break
            time.sleep(0.05)
        self.assertEqual(data, b'SERVERHELLO')
        self.assertEqual(tlvs[0x37], b'\x00')

        tr = server._handle_bip_command(None, 1, 0x41, 0x00, channel_cmd(0x41, 0x00), 0x81, 0x21)
        self.assertEqual(tr.hex(), '810301410002028281030100')
        self.peer.done.wait(3)
        self.assertEqual(self.peer.received, b'CLIENTHE')

    def test_send_without_channel_fails(self):
        tr = server._handle_bip_command(None, 1, 0x43, 0x01,
                                        channel_cmd(0x43, 0x01, bytes([0x36, 0x01]) + b'X'),
                                        0x81, 0x21)
        tlvs = parse_tr(tr)
        self.assertEqual(tlvs[0x03].hex(), '3a00')

    def test_open_channel_cr_set_tlvs(self):
        # Live card 2026-09-15: the fallback OPEN CHANNEL uses the CR-set tag
        # variants (B5/B9/C7/BC/BE) - the handler must find them too.
        raw = bytes.fromhex('d0248103014003820281828500b50103b902058e'
                            'c70403475042bc03020582be05215bd50502')
        tr = server._handle_bip_command(None, 1, 0x40, 0x03, raw, 0x81, 0x82)
        tlvs = parse_tr(tr)
        self.assertEqual(tlvs[0x03], b'\x00')
        self.assertIn(0x38, tlvs)   # Channel status
        self.assertIn(0x39, tlvs)   # Buffer size echo

    def test_open_channel_plain_tlvs(self):
        # Same command with the plain tag variants (reference phone traces).
        raw = bytes.fromhex('d02401030140030202818205003501033902058e'
                            '4704034750423c030205823e05215bd50502')
        tr = server._handle_bip_command(None, 1, 0x40, 0x03, raw, 0x81, 0x82)
        tlvs = parse_tr(tr)
        self.assertEqual(tlvs[0x03], b'\x00')
        self.assertIn(0x38, tlvs)

    def test_open_channel_truncated_destination_accepted(self):
        # Live card 2026-09-15: '3e 05' with no value (empty buffer quirk,
        # same family as the reference openchannel_not_understood_no_apn
        # trace). The emulation is permissive and opens the configured target.
        raw = bytes.fromhex('d01c810301400c82028182850035010339020200'
                            '4701003c030227be3e05')
        tr = server._handle_bip_command(None, 1, 0x40, 0x0C, raw, 0x81, 0x82)
        tlvs = parse_tr(tr)
        self.assertEqual(tlvs[0x03], b'\x00')
        self.assertIn(0x38, tlvs)
        kinds = [(e['kind'], e.get('note')) for e in self.bip.entries_after(0)]
        self.assertIn(('open-relaxed', 'destination/transport not fully specified'), kinds)

    def test_open_channel_without_transport_accepted(self):
        # No transport level at all (bearer-level channel): still accepted.
        raw = bytes.fromhex('d00d81030140018202818239020200')
        tr = server._handle_bip_command(None, 1, 0x40, 0x01, raw, 0x81, 0x82)
        tlvs = parse_tr(tr)
        self.assertEqual(tlvs[0x03], b'\x00')
        self.assertIn(0x38, tlvs)
        kinds = [e['kind'] for e in self.bip.entries_after(0)]
        self.assertIn('open-relaxed', kinds)

    def test_background_open_queues_link_established(self):
        # Qualifier 0x04 (background mode): the terminal must report the
        # established link via ENVELOPE (Channel status) - 7.5.11.
        raw = bytes.fromhex('d01c810301400c82028182850035010339020200'
                            '4701003c030227be3e05')
        server._handle_bip_command(None, 1, 0x40, 0x0C, raw, 0x81, 0x82)
        events = self.bip.take_pending_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['info'], 0x00)
        self.assertTrue(events[0]['status'] & 0x80)

    def test_flush_channel_events_when_subscribed(self):
        sent = []

        class Tp:
            def send_apdu(self, apdu):
                sent.append(apdu)
                return '', '9000'

        scc = types.SimpleNamespace(cat_cla='80', _tp=Tp())
        self.bip._queue_link_status(2)
        ref = types.SimpleNamespace(event_list=[0x09, 0x0A])
        with mock.patch.object(server, '_server_ref', ref):
            server._bip_flush_channel_events(scc)
        # D6: event list (ch status), device ids, Channel status B8 02 02 05
        # (channel 2, link not established, info 05 = link dropped)
        self.assertEqual(sent, ['80c200000dd60b99010a82028281b8020205'])
        self.assertEqual(self.bip.take_pending_events(), [])

    def test_flush_skipped_without_subscription(self):
        sent = []

        class Tp:
            def send_apdu(self, apdu):
                sent.append(apdu)
                return '', '9000'

        scc = types.SimpleNamespace(cat_cla='80', _tp=Tp())
        self.bip._queue_link_status(1)
        ref = types.SimpleNamespace(event_list=[0x09])
        with mock.patch.object(server, '_server_ref', ref):
            server._bip_flush_channel_events(scc)
        self.assertEqual(sent, [])
        # Not subscribed: the event stays queued for a later card session.
        self.assertEqual(self.bip.take_pending_events(),
                         [{'channel': 1, 'status': 1, 'info': 0x05}])


if __name__ == '__main__':
    unittest.main()

    def test_peer_close_reported_after_buffer_drained(self):
        # A dropped link must not be signalled while server data still waits
        # to be fetched: the card would abort the fetch mid-record. Drain
        # first, then report.
        srv = socket.socket()
        srv.bind(('127.0.0.1', 0))
        srv.listen(1)
        try:
            bip = httpota.BipTerminal()
            bip.enable('127.0.0.1', srv.getsockname()[1])
            cid, err = bip.open('10.9.9.9', 1234, 512)
            self.assertIsNone(err)
            conn, _ = srv.accept()
            conn.sendall(b'response-bytes')
            conn.close()
            ch = bip.channels[cid]
            for _ in range(40):
                ch.pump()
                if ch.rx and ch.peer_closed:
                    break
                time.sleep(0.05)
            self.assertTrue(ch.rx)
            self.assertTrue(ch.peer_closed)
            # Partial fetch: the link-dropped event must still be withheld.
            bip.receive(cid, 5)
            self.assertEqual(bip.take_pending_events(), [])
            # Remaining bytes fetched: the event is reported now.
            bip.receive(cid, 64)
            self.assertEqual(bip.take_pending_events(),
                             [{'channel': cid, 'status': cid, 'info': 0x05}])
            bip.close(cid)
        finally:
            srv.close()

    def test_receive_data_tlv_long_form_length(self):
        # A >127-byte channel data TLV must use the BER long form (0x81 len),
        # as the reference terminal traces do (`36 81 ed` for 237 bytes).
        import types
        server = __import__('pysim_simple_server.server', fromlist=['x'])
        big = bytes(range(256)) * 1  # 256 bytes; take a slice below
        ch = types.SimpleNamespace(rx=bytearray(b'\xAA' * 237))
        class FakeBip:
            def __init__(self): self.channels = {1: ch}
            def receive(self, cid, n): 
                data = bytes(ch.rx[:min(n, len(ch.rx))]); del ch.rx[:len(data)]; return data
            def available(self, cid): return len(ch.rx)
            def log(self, *a, **k): pass
        old = server._BIP
        server._BIP = FakeBip()
        try:
            raw = bytes.fromhex('d00c8103014200820281213701ed')
            tr = server._handle_bip_command(None, 1, 0x42, 0, raw, None, 0x21)
            self.assertIn(b'\x36\x81\xed' + b'\xAA' * 237, tr)
        finally:
            server._BIP = old
