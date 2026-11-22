#!/usr/bin/env python3
"""Tests for :mod:`simple_server.bip` (the BIP terminal side, Phase 3c-2).

The command handling runs against the real :class:`httpota.BipTerminal` with a
local TCP peer, so a channel really opens, carries bytes and closes; the
lifecycle and event tests use a scripted terminal / sink / CAT session so they
describe the *protocol* (which TR, which ENVELOPE, which lock) without a card.
The byte parity with the legacy server lives in ``tests/test_simple_drift.py``
(the one module allowed to import both implementations).

Spec anchors used below: TS 102 223 §6.4.27-§6.4.31 and §6.6.27-§6.6.31 (the
BIP commands and their TR objects), §6.8.0/§6.8.1 (TR object order and the
echoed command details), §7.5.10/§7.5.10.2 (Data available), §7.5.11 (Channel
status), §8.6 (the OPEN CHANNEL qualifier's background-mode bit), §8.7 (the
channel identifier in the device identities), §8.53/§8.54 (Channel data and
its length, ``FF`` above 255), §8.56 (the Channel status bytes).
"""

import socket
import threading
import time
import unittest

from pysim_simple_server import httpota

from simple_server import bip

# The live-tested legacy builder's vectors (the trace-confirmed shape): OPEN
# CHANNEL success and failure, with the command details echoing the test
# command's `81` tag (TS 102 223 §6.8.1).  They are *our* bytes, not the
# reference terminal's: that trace's OPEN CHANNEL TR uses the base tags
# (`02 81` device identities) where ours keep the legacy `82 81` form - a
# cutover decision, not a port bug (see the module docstring).
TR_OPEN_OK = '8103014001020282810301003802810035010339020200'
TR_OPEN_FAIL = '81030140010202828103023a0035010339020200'
#: The "Bearer Independent Protocol error" result object's value
#: (TS 102 223 §8.12.0), as the parsed TR carries it.
RESULT_BIP_ERROR = bytes.fromhex('3a00')


# ---- fixtures -------------------------------------------------------------

def open_cmd(host, port, buffer_size=512, qualifier=0x01, tag=0x81):
    """An OPEN CHANNEL (0x40) with the IPv4 destination and a TCP port."""
    ip = bytes(int(x) for x in host.split('.'))
    tlvs = (bytes([tag, 0x03, 0x01, 0x40, qualifier])
            + b'\x82\x02\x81\x82'
            + b'\x35\x01\x03'
            + b'\x39\x02' + buffer_size.to_bytes(2, 'big')
            + b'\x3c\x03\x02' + port.to_bytes(2, 'big')
            + b'\x3e\x05\x21' + ip)
    return b'\xd0' + bytes([len(tlvs)]) + tlvs


def channel_cmd(cmd_type, qualifier, data_tlvs=b'', tag=0x81):
    """A BIP command addressed to channel 1 (destination 0x21)."""
    tlvs = (bytes([tag, 0x03, 0x01, cmd_type, qualifier])
            + b'\x82\x02\x81\x21' + data_tlvs)
    return b'\xd0' + bytes([len(tlvs)]) + tlvs


class PeerServer(threading.Thread):
    """A tiny TCP peer: accepts one connection, greets, records its input."""

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
        self.done = threading.Event()

    def run(self):
        self.sock.settimeout(3)
        try:
            self.conn, _ = self.sock.accept()
        except OSError:
            return
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
        finally:
            try:
                self.conn.close()
            except OSError:
                pass
        self.done.set()

    def stop(self):
        try:
            self.sock.close()
        except OSError:
            pass


class RecordingLock(object):
    """A context manager that counts its entries (the injected card lock)."""

    def __init__(self, on_enter=None):
        self.entries = 0
        self.depth = 0
        self.on_enter = on_enter

    def __enter__(self):
        self.entries += 1
        self.depth += 1
        if self.on_enter:
            self.on_enter()
        return self

    def __exit__(self, *exc):
        self.depth -= 1
        return False


class FakeCatSession(object):
    """The CAT-session slice the BIP events use."""

    def __init__(self, event_list=None, busy=False, stk_pending=None,
                 on_send=None):
        self.event_list = event_list
        self.busy = busy
        self.stk_pending = stk_pending
        self.sent = []
        self.on_send = on_send

    def send_event_download(self, event_type, event_data=None, drain=True,
                            src=None):
        if self.on_send:
            self.on_send(event_type, event_data)
        self.sent.append((event_type, bytes(event_data)))
        return (b'', b'\x90\x00')


class FakeTerminal(object):
    """A scripted BipTerminal: the slice the handler and the lifecycle use."""

    def __init__(self, enabled=True, open_result=(1, None), send_ok=True,
                 receive=b'', available=0, capacity=0xFF, close_ok=True,
                 channels=None):
        self.enabled = enabled
        self.mode = 'redirect'
        self.target = None
        self.on_data = None
        self.channels = dict(channels or {})
        self.entries = []
        self.pending = []
        self.open_result = open_result
        self.send_ok = send_ok
        self.receive_data = receive
        self.available_bytes = available
        self.capacity = capacity
        self.close_ok = close_ok
        self.enabled_calls = []
        self.disable_calls = 0

    # the terminal API
    def log(self, kind, **fields):
        entry = dict(fields, kind=kind)
        self.entries.append(entry)
        return entry

    def _queue_link_status(self, channel_id, status=None, info=0x05):
        self.pending.append({
            'channel': channel_id,
            'status': channel_id & 0x07 if status is None else status,
            'info': info})

    def take_pending_events(self):
        events, self.pending = self.pending, []
        return events

    def enable(self, host=None, port=None, mode='redirect'):
        self.enabled = True
        self.mode = mode
        self.target = (host, int(port)) if host and port not in (None, '') \
            else None
        self.enabled_calls.append((host, port, mode))
        self.log('enabled', mode=mode,
                 target='%s:%d' % self.target if self.target else None)

    def disable(self):
        self.enabled = False
        self.disable_calls += 1
        self.log('disabled')

    def open(self, requested_host, requested_port, buffer_size, proto=None):
        self.opened = (requested_host, requested_port, buffer_size, proto)
        return self.open_result

    def send(self, channel_id, data):
        self.sent_data = (channel_id, bytes(data))
        return self.send_ok

    def send_capacity(self, channel_id):
        return self.capacity

    def receive(self, channel_id, maxlen):
        self.receive_args = (channel_id, maxlen)
        return self.receive_data

    def available(self, channel_id):
        return self.available_bytes

    def close(self, channel_id):
        self.closed = channel_id
        return self.close_ok

    def status(self):
        return {'enabled': self.enabled, 'mode': self.mode,
                'target': '%s:%d' % self.target if self.target else None,
                'channels': [], 'seq': 0}

    def kinds(self):
        return [entry['kind'] for entry in self.entries]


class FakeSink(object):
    """A sink listener factory recording its arguments (no sockets)."""

    def __init__(self, host='127.0.0.1', port=4000, fail=None):
        self.host = host
        self.port = port
        self.fail = fail
        self.stopped = 0
        self.accepted = 0
        self.calls = []

    def __call__(self, host, port, on_rx=None, on_log=None):
        self.calls.append((host, port))
        if self.fail:
            raise self.fail
        self.on_rx = on_rx
        self.on_log = on_log
        return self

    def stop(self):
        self.stopped += 1


def parse_tr(tr):
    """The COMPREHENSION-TLV map of a TERMINAL RESPONSE payload."""
    out, off = {}, 0
    while off + 1 < len(tr):
        tag = tr[off]
        ln = tr[off + 1]
        if ln & 0x80:
            count = ln & 0x7F
            ln = int.from_bytes(tr[off + 2:off + 2 + count], 'big')
            off += count
        out[tag] = tr[off + 2:off + 2 + ln]
        off += 2 + ln
    return out


class FakeChannel(object):
    """What the ``on_data`` callback reads off a BipChannel."""

    def __init__(self, channel_id=1, data=b'', peer_closed=False):
        self.id = channel_id
        self.rx = bytearray(data)
        self.peer_closed = peer_closed


# ---- the command handler --------------------------------------------------

class ChannelIdTests(unittest.TestCase):
    def test_the_device_identities_code_the_channel(self):
        # TS 102 223 §8.7: destination 21-27 = channel identifier 1-7
        self.assertEqual([bip.bip_channel_id(0x21 + i) for i in range(7)],
                         [1, 2, 3, 4, 5, 6, 7])
        for value in (0x00, 0x20, 0x28, 0x81, 0x82, 0x83):
            self.assertIsNone(bip.bip_channel_id(value), '%02X' % value)


class TerminalResponseTests(unittest.TestCase):
    def test_the_reference_open_channel_vector(self):
        extra = (bytes.fromhex('350103') + bytes.fromhex('39020200'))
        tr = bip.bip_terminal_response(
            1, 0x40, 0x01, 0x81, 0x82, 0x00, None,
            bytes.fromhex('38028100') + extra, cd_tag=0x81)
        self.assertEqual(tr.hex(), TR_OPEN_OK)
        tr = bip.bip_terminal_response(
            1, 0x40, 0x01, 0x81, 0x82, 0x3A, 0x00, extra, cd_tag=0x81)
        self.assertEqual(tr.hex(), TR_OPEN_FAIL)

    def test_the_basic_and_general_result_forms(self):
        basic = bip.bip_terminal_response(1, 0x41, 0x00, 0x81, 0x21)
        self.assertEqual(basic.hex(), '010301410002028281' '030100')
        general = bip.bip_terminal_response(1, 0x40, 0x00, 0x81, 0x82,
                                            result=0x3A, info=0x00)
        self.assertEqual(general.hex(), '010301400002028281' '03023a00')

    def test_the_command_details_echo_the_tag_and_qualifier(self):
        # TS 102 223 §6.8.1: identical to the command's, CR flag included
        for tag in (0x01, 0x81):
            for qual in (0x00, 0x01, 0x0C, 0xFF):
                tr = bip.bip_terminal_response(7, 0x40, qual, 0x81, 0x82,
                                               cd_tag=tag)
                self.assertEqual(tr[:5], bytes([tag, 0x03, 0x07, 0x40, qual]))
        # a None qualifier is coded 00, a None tag falls back to the plain 01
        tr = bip.bip_terminal_response(1, 0x41, None, 0x81, 0x21)
        self.assertEqual(tr[:5], bytes.fromhex('0103014100'))

    def test_the_device_identities_are_the_legacy_terminal_to_uicc_form(self):
        # pinned: the live-tested legacy builder emitted 82 81 regardless of
        # the command's own device identities (see the module docstring)
        tr = bip.bip_terminal_response(1, 0x43, 0x01, 0x83, 0x27)
        self.assertEqual(tr[5:9], bytes.fromhex('02028281'))


class OpenChannelTests(unittest.TestCase):
    def setUp(self):
        self.peer = PeerServer()
        self.peer.start()
        self.terminal = httpota.BipTerminal()
        self.session = bip.BipSession(terminal=self.terminal)
        self.terminal.enable('127.0.0.1', self.peer.port)

    def tearDown(self):
        self.terminal.disable()
        self.peer.stop()

    def test_the_success_tr_and_the_opened_channel(self):
        tr = self.session.handle_command(
            1, 0x40, 0x01, open_cmd('127.0.0.1', self.peer.port), 0x81, 0x82)
        self.assertEqual(tr.hex(), TR_OPEN_OK)
        status = self.terminal.status()
        self.assertEqual(len(status['channels']), 1)
        channel = status['channels'][0]
        self.assertEqual(channel['id'], 1)
        self.assertEqual(channel['buffer_size'], 512)
        self.assertEqual(channel['target'], '127.0.0.1:%d' % self.peer.port)
        kinds = [e['kind'] for e in self.terminal.entries_after(0)]
        self.assertIn('open', kinds)

    def test_the_cr_set_object_tags_are_found(self):
        raw = bytes.fromhex('d0248103014003820281828500b50103b902058e'
                            'c70403475042bc03020582be05215bd50502')
        # redirected to the local peer; the requested address is informational
        tr = self.session.handle_command(1, 0x40, 0x03, raw, 0x81, 0x82)
        tlvs = parse_tr(tr)
        self.assertEqual(tlvs[0x03], b'\x00')
        self.assertEqual(tlvs[0x38], bytes.fromhex('8100'))

    def test_a_disabled_terminal_answers_3a_with_the_echo(self):
        self.terminal.disable()
        tr = self.session.handle_command(
            1, 0x40, 0x01, open_cmd('127.0.0.1', self.peer.port), 0x81, 0x82)
        self.assertEqual(tr.hex(), TR_OPEN_FAIL)
        kinds = [e['kind'] for e in self.terminal.entries_after(0)]
        self.assertIn('open-unavailable', kinds)
        self.assertEqual(self.terminal.status()['channels'], [])

    def test_a_truncated_destination_is_accepted_and_logged_relaxed(self):
        # the live card's `3e 05` with no value (same family as the reference
        # openchannel_not_understood_no_apn trace)
        raw = bytes.fromhex('d01c810301400c82028182850035010339020200'
                            '4701003c030227be3e05')
        tr = self.session.handle_command(1, 0x40, 0x0C, raw, 0x81, 0x82)
        tlvs = parse_tr(tr)
        self.assertEqual(tlvs[0x03], b'\x00')
        self.assertIn(0x38, tlvs)
        relaxed = [e for e in self.terminal.entries_after(0)
                   if e['kind'] == 'open-relaxed']
        self.assertEqual(len(relaxed), 1)
        self.assertEqual(relaxed[0]['note'],
                         'destination/transport not fully specified')
        self.assertTrue(relaxed[0]['address'] is None)

    def test_a_missing_transport_is_accepted(self):
        raw = bytes.fromhex('d00d81030140018202818239020200')
        tr = self.session.handle_command(1, 0x40, 0x01, raw, 0x81, 0x82)
        self.assertEqual(parse_tr(tr)[0x03], b'\x00')
        kinds = [e['kind'] for e in self.terminal.entries_after(0)]
        self.assertIn('open-relaxed', kinds)

    def test_a_refused_connection_is_a_3a(self):
        # passthru dials the card's own destination, so an unused port fails
        probe = socket.socket()
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
        probe.close()
        self.terminal.disable()
        self.terminal.enable(mode='passthru')
        tr = self.session.handle_command(
            1, 0x40, 0x01, open_cmd('127.0.0.1', port), 0x81, 0x82)
        tlvs = parse_tr(tr)
        self.assertEqual(tlvs[0x03], RESULT_BIP_ERROR)
        self.assertIn(0x39, tlvs)          # the buffer-size echo survives

    def test_the_background_mode_queues_the_link_established_event(self):
        # TS 102 223 §8.6 bit 3: background mode reports the link separately
        tr = self.session.handle_command(
            1, 0x40, 0x0C, open_cmd('127.0.0.1', self.peer.port,
                                    qualifier=0x0C), 0x81, 0x82)
        self.assertEqual(parse_tr(tr)[0x03], b'\x00')
        events = self.terminal.take_pending_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['info'], 0x00)
        self.assertEqual(events[0]['status'], 0x81)

    def test_an_immediate_open_queues_no_event(self):
        self.session.handle_command(
            1, 0x40, 0x01, open_cmd('127.0.0.1', self.peer.port), 0x81, 0x82)
        self.assertEqual(self.terminal.take_pending_events(), [])

    def test_an_unknown_command_type_falls_through(self):
        self.assertIsNone(self.session.handle_command(
            1, 0x26, 0x00, bytes.fromhex('d008810301260082028183'), 0x81,
            0x82))


class BearerEchoTests(unittest.TestCase):
    """The OPEN CHANNEL Bearer Description echo must stay inside one APDU.

    TS 102 223 §6.8.20: "When the terminal issues a successful or an
    unsuccessful TERMINAL RESPONSE for an OPEN CHANNEL command, the TERMINAL
    RESPONSE shall contain the Bearer description data object" - so an
    over-long description is cut, never dropped (an unanswered FETCH is worse:
    the card would wait for a TERMINAL RESPONSE that never comes).
    """

    @staticmethod
    def _open_with_bearer(bearer):
        tlvs = (bytes.fromhex('810301400182028182')
                + bytes([0x35]) + bytes([0x81, len(bearer)]) + bearer
                + bytes.fromhex('39020200'))
        # a D0 length of 128+ octets takes the BER long form (0x81 nn)
        return b'\xd0\x81' + bytes([len(tlvs)]) + tlvs

    def test_an_over_long_bearer_is_capped_to_fit_one_apdu(self):
        terminal = FakeTerminal(open_result=(1, None))
        session = bip.BipSession(terminal=terminal)
        bearer = bytes(i % 0x100 for i in range(236))
        tr = session.handle_command(1, 0x40, 0x01,
                                    self._open_with_bearer(bearer), 0x81, 0x82)
        self.assertEqual(len(tr), 255)
        tlvs = parse_tr(tr)
        self.assertEqual(tlvs[0x03], b'\x00')
        self.assertEqual(tlvs[0x35], bearer[:bip.BEARER_ECHO_MAX])
        self.assertEqual(len(tlvs[0x35]), bip.BEARER_ECHO_MAX)
        capped = [e for e in terminal.entries
                  if e['kind'] == 'open-bearer-capped']
        self.assertEqual(len(capped), 1)
        self.assertEqual(capped[0]['bytes'], len(bearer))
        self.assertEqual(capped[0]['echoed'], bip.BEARER_ECHO_MAX)

    def test_a_normal_bearer_is_echoed_unchanged(self):
        terminal = FakeTerminal(open_result=(1, None))
        session = bip.BipSession(terminal=terminal)
        tr = session.handle_command(1, 0x40, 0x01, open_cmd('127.0.0.1', 8080),
                                    0x81, 0x82)
        tlvs = parse_tr(tr)
        self.assertEqual(tlvs[0x35], b'\x03')       # the request's bearer
        self.assertEqual(tlvs[0x39], bytes.fromhex('0200'))
        self.assertNotIn('open-bearer-capped', terminal.kinds())


class DataFlowTests(unittest.TestCase):
    """SEND / RECEIVE / CLOSE / GET STATUS over a real loopback channel."""

    def setUp(self):
        self.peer = PeerServer(greeting=b'SERVERHELLO')
        self.peer.start()
        self.terminal = httpota.BipTerminal()
        self.session = bip.BipSession(terminal=self.terminal)
        self.terminal.enable('127.0.0.1', self.peer.port)
        self.session.handle_command(
            1, 0x40, 0x01, open_cmd('127.0.0.1', self.peer.port), 0x81, 0x82)

    def tearDown(self):
        self.terminal.disable()
        self.peer.stop()

    def test_send_data_returns_the_free_tx_space(self):
        tr = self.session.handle_command(
            1, 0x43, 0x01,
            channel_cmd(0x43, 0x01, bytes([0x36, 0x08]) + b'CLIENTHE'),
            0x81, 0x21)
        tlvs = parse_tr(tr)
        self.assertEqual(tlvs[0x03], b'\x00')
        # §6.4.30: the successful TR carries the free Tx-buffer space (8.54)
        self.assertEqual(tlvs[0x37], b'\xff')
        self.peer.done.wait(3)
        self.assertEqual(self.peer.received, b'CLIENTHE')

    def test_send_data_on_an_unknown_channel_is_a_3a(self):
        tr = self.session.handle_command(
            1, 0x43, 0x01,
            channel_cmd(0x43, 0x01, bytes([0x36, 0x01]) + b'X'),
            0x81, 0x27)
        self.assertEqual(parse_tr(tr)[0x03], RESULT_BIP_ERROR)

    def test_send_data_failure_is_a_3a(self):
        self.terminal.close(1)
        tr = self.session.handle_command(
            1, 0x43, 0x01,
            channel_cmd(0x43, 0x01, bytes([0x36, 0x01]) + b'X'),
            0x81, 0x21)
        self.assertEqual(parse_tr(tr)[0x03], RESULT_BIP_ERROR)

    def test_receive_data_returns_the_data_and_the_remaining_length(self):
        # accumulate: a loopback TCP release may deliver the greeting in more
        # than one pump, so one RECEIVE need not carry all of it
        chunks, tlvs = [], {}
        for _ in range(40):
            tr = self.session.handle_command(
                1, 0x42, 0x00,
                channel_cmd(0x42, 0x00, bytes([0x37, 0x01, 0x64])),
                0x81, 0x21)
            tlvs = parse_tr(tr)
            if tlvs.get(0x36):
                chunks.append(tlvs[0x36])
                if sum(len(c) for c in chunks) >= len(b'SERVERHELLO'):
                    break
            time.sleep(0.05)
        data = b''.join(chunks)
        self.assertEqual(data, b'SERVERHELLO')
        self.assertEqual(tlvs[0x03], b'\x00')
        self.assertEqual(tlvs[0x37], b'\x00')
        # only the requested length is taken (8.54 carries the request)
        self.assertEqual(self.terminal.channels[1].bytes_in,
                         len(b'SERVERHELLO'))

    def test_a_long_receive_uses_the_ber_long_form_length(self):
        # TS 101 220 §7.1.2 Table 7.6: >= 128 octets is `81 nn`; a raw octet
        # above 0x7F would be read as a long-form indicator
        greeting = bytes(i % 0x100 for i in range(200))
        peer = PeerServer(greeting=greeting)
        peer.start()
        terminal = httpota.BipTerminal()
        session = bip.BipSession(terminal=terminal)
        try:
            terminal.enable('127.0.0.1', peer.port)
            session.handle_command(1, 0x40, 0x01,
                                   open_cmd('127.0.0.1', peer.port),
                                   0x81, 0x82)
            chunks = []
            for _ in range(40):
                tr = session.handle_command(
                    1, 0x42, 0x00,
                    channel_cmd(0x42, 0x00, bytes([0x37, 0x01, 0xFF])),
                    0x81, 0x21)
                tlvs = parse_tr(tr)
                if tlvs.get(0x36):
                    chunk = tlvs[0x36]
                    # each chunk is coded the way its own length requires
                    if len(chunk) <= 0x7F:
                        self.assertIn(bytes([0x36, len(chunk)]) + chunk, tr)
                    else:
                        self.assertIn(bytes([0x36, 0x81, len(chunk)]) + chunk,
                                      tr)
                    chunks.append((chunk, tr))
                    if sum(len(c) for c, _t in chunks) >= len(greeting):
                        break
                time.sleep(0.05)
            self.assertEqual(b''.join(c for c, _t in chunks), greeting)
            # the bulk came in one long-form object (the point of the test)
            self.assertTrue(any(len(c) > 0x7F for c, _t in chunks),
                            [len(c) for c, _t in chunks])
        finally:
            terminal.disable()
            peer.stop()

    def test_a_receive_larger_than_one_apdu_is_capped(self):
        # TS 102 223 §6.4.29: "if the requested number of bytes is available
        # in the buffer but the whole requested data cannot be included in the
        # TERMINAL RESPONSE because of APDU size limits, the terminal shall
        # return the maximum number of bytes possible according to the length
        # of other TLVs" - and report success.  Without the cap the TR payload
        # (N + 18 octets) exceeds 255, cat_session refuses the APDU and the
        # fetched command is never answered.
        greeting = bytes(i % 0x100 for i in range(300))
        peer = PeerServer(greeting=greeting)
        peer.start()
        terminal = httpota.BipTerminal()
        session = bip.BipSession(terminal=terminal)
        try:
            terminal.enable('127.0.0.1', peer.port)
            session.handle_command(1, 0x40, 0x01,
                                   open_cmd('127.0.0.1', peer.port),
                                   0x81, 0x82)
            # wait until the whole greeting sits in the channel's Rx buffer
            deadline = time.time() + 3
            while (terminal.available(1) < len(greeting)
                   and time.time() < deadline):
                time.sleep(0.02)
            self.assertEqual(terminal.available(1), len(greeting))
            tr = session.handle_command(
                1, 0x42, 0x00,
                channel_cmd(0x42, 0x00, bytes([0x37, 0x01, 0xFF])),
                0x81, 0x21)
            # the full answer is exactly one APDU (255 octets of TR payload)
            self.assertEqual(len(tr), 255)
            self.assertIn(bytes.fromhex('3681ed'), tr)
            self.assertIn(bytes.fromhex('37013f'), tr)
            tlvs = parse_tr(tr)
            self.assertEqual(tlvs[0x03], b'\x00')
            self.assertEqual(tlvs[0x36], greeting[:bip.CHANNEL_DATA_MAX])
            self.assertEqual(len(tlvs[0x36]), 0xED)
            self.assertEqual(tlvs[0x37], b'\x3f')      # 300 - 237 remaining
        finally:
            terminal.disable()
            peer.stop()

    def test_receive_data_without_a_channel_is_a_3a(self):
        tr = self.session.handle_command(
            1, 0x42, 0x00, channel_cmd(0x42, 0x00, bytes([0x37, 0x01, 0x10])),
            0x81, 0x27)
        self.assertEqual(parse_tr(tr)[0x03], RESULT_BIP_ERROR)

    def test_an_empty_buffer_is_a_success_with_a_zero_length(self):
        # Ported legacy behaviour: an *empty* buffer is not an error (the card
        # polled before data arrived - §6.4.29 makes the terminal answer with
        # what is available, not wait); only a channel that is gone is a 3A.
        peer = PeerServer()                  # never greets
        peer.start()
        terminal = httpota.BipTerminal()
        session = bip.BipSession(terminal=terminal)
        try:
            terminal.enable('127.0.0.1', peer.port)
            session.handle_command(1, 0x40, 0x01,
                                   open_cmd('127.0.0.1', peer.port),
                                   0x81, 0x82)
            tr = session.handle_command(
                1, 0x42, 0x00,
                channel_cmd(0x42, 0x00, bytes([0x37, 0x01, 0x10])), 0x81, 0x21)
            tlvs = parse_tr(tr)
            self.assertEqual(tlvs[0x03], b'\x00')
            self.assertNotIn(0x36, tlvs)     # no Channel data object
            self.assertEqual(tlvs[0x37], b'\x00')
        finally:
            terminal.disable()
            peer.stop()

    def test_close_channel_and_an_unknown_channel(self):
        tr = self.session.handle_command(1, 0x41, 0x00,
                                         channel_cmd(0x41, 0x00), 0x81, 0x21)
        self.assertEqual(tr.hex(), '810301410002028281' '030100')
        self.assertEqual(self.terminal.status()['channels'], [])
        tr = self.session.handle_command(1, 0x41, 0x00,
                                         channel_cmd(0x41, 0x00), 0x81, 0x21)
        self.assertEqual(parse_tr(tr)[0x03], RESULT_BIP_ERROR)

    def test_get_channel_status_lists_every_channel(self):
        # a second channel, opened through the same handler
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)
        try:
            self.session.handle_command(
                1, 0x40, 0x01,
                open_cmd('127.0.0.1', listener.getsockname()[1]), 0x81, 0x82)
        finally:
            listener.close()
        tr = self.session.handle_command(1, 0x44, 0x00,
                                         channel_cmd(0x44, 0x00), 0x81, 0x21)
        self.assertEqual(tr.hex(), '810301440002028281030100'
                                   '3802810038028200')

    def test_the_command_details_tag_is_echoed(self):
        tr = self.session.handle_command(1, 0x43, 0x01,
                                         channel_cmd(0x43, 0x01, tag=0x01),
                                         0x81, 0x21)
        self.assertEqual(tr[:5], bytes.fromhex('0103014301'))


# ---- the lifecycle --------------------------------------------------------

class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.terminal = FakeTerminal()
        self.sink = FakeSink(port=4123)
        self.session = bip.BipSession(terminal=self.terminal, sink=self.sink)

    def test_the_default_status_is_empty(self):
        self.assertEqual(self.session.status(),
                         {'owner': None, 'bip': self.terminal.status(),
                          'listener': None})

    def test_sink_start_binds_the_sink_and_enables_the_redirect(self):
        resp = self.session.start({'mode': 'sink', 'port': 0})
        self.assertTrue(resp['ok'])
        self.assertIsNone(resp['replaced'])
        self.assertEqual(resp['owner'], 'bip')
        self.assertEqual(resp['listener']['mode'], 'sink')
        self.assertEqual(resp['listener']['host'], '127.0.0.1')
        self.assertEqual(resp['listener']['port'], 4123)
        self.assertEqual(resp['listener']['connections'], 0)
        self.assertEqual(self.sink.calls, [('127.0.0.1', 0)])
        # the terminal redirects every channel to the sink
        self.assertEqual(self.terminal.enabled_calls,
                         [('127.0.0.1', 4123, 'redirect')])
        self.assertEqual(self.terminal.on_data.__self__, self.session)
        self.assertEqual(self.terminal.on_data.__func__,
                         self.session.data_available.__func__)

    def test_sink_is_the_default_mode(self):
        resp = self.session.start({})
        self.assertTrue(resp['ok'])
        self.assertEqual(self.session.mode, 'sink')
        self.assertEqual(self.sink.calls, [('127.0.0.1', 0)])

    def test_the_sink_callbacks_log_through_the_terminal(self):
        # the legacy wiring: the dump listener's rx and lifecycle lines are
        # BIP log entries (the PWA's HTTP OTA log renders them)
        self.session.start({'mode': 'sink', 'port': 0})
        self.sink.on_rx('10.0.0.9:1234', b'\x01\x02')
        self.sink.on_log('listener-start', host='127.0.0.1', port=4000)
        rx = [e for e in self.terminal.entries if e['kind'] == 'sink-rx']
        self.assertEqual(rx[0]['peer'], '10.0.0.9:1234')
        self.assertEqual(rx[0]['bytes'], 2)
        self.assertEqual(rx[0]['hex'], '0102')
        self.assertIn('listener-start', self.terminal.kinds())

    def test_redirect_pins_the_target(self):
        resp = self.session.start({'mode': 'redirect', 'host': '10.0.0.5',
                                   'port': 9000})
        self.assertTrue(resp['ok'])
        self.assertEqual(resp['listener'], {'mode': 'redirect',
                                            'host': '10.0.0.5', 'port': 9000,
                                            'target': '10.0.0.5:9000'})
        self.assertIsNone(self.session.listener)
        self.assertEqual(self.terminal.enabled_calls,
                         [('10.0.0.5', 9000, 'redirect')])

    def test_passthru_has_neither_listener_nor_target(self):
        resp = self.session.start({'mode': 'passthru', 'host': 'ignored',
                                   'port': 1})
        self.assertTrue(resp['ok'])
        self.assertEqual(resp['listener'], {'mode': 'passthru'})
        self.assertEqual(self.terminal.enabled_calls,
                         [(None, None, 'passthru')])
        self.assertIsNone(self.session.target)

    def test_the_validation_errors(self):
        cases = [
            ({'mode': 'stream'}, 'unsupported mode: stream'),
            ({'mode': 'redirect'}, 'redirect mode requires the target host '
                                   'and port'),
            ({'mode': 'redirect', 'host': 'h'}, 'redirect mode requires the '
                                                'target host and port'),
            ({'mode': 'redirect', 'host': 'h', 'port': 'x'},
             'port is not a number'),
            ({'mode': 'redirect', 'host': 'h', 'port': 0},
             'port must be 1-65535'),
            ({'mode': 'redirect', 'host': 'h', 'port': 70000},
             'port must be 1-65535'),
            ({'mode': 'sink', 'port': 'x'}, 'port is not a number'),
            ({'mode': 'sink', 'port': 70000},
             'port must be 0-65535 (0 = ephemeral)'),
        ]
        for body, error in cases:
            with self.subTest(body=body):
                self.assertEqual(self.session.start(body),
                                 {'ok': False, 'error': error})
        self.assertEqual(self.sink.calls, [])
        self.assertEqual(self.terminal.enabled_calls, [])

    def test_a_validation_error_keeps_the_running_session(self):
        self.session.start({'mode': 'redirect', 'host': '10.0.0.5',
                            'port': 9000})
        disables = self.terminal.disable_calls
        enables = len(self.terminal.enabled_calls)
        self.assertEqual(self.session.start({'mode': 'nope'}),
                         {'ok': False, 'error': 'unsupported mode: nope'})
        self.assertEqual(self.session.mode, 'redirect')
        self.assertEqual(self.session.owner, 'bip')
        self.assertEqual(self.terminal.disable_calls, disables)
        self.assertEqual(len(self.terminal.enabled_calls), enables)

    def test_a_start_replaces_the_previous_session(self):
        self.session.start({'mode': 'sink', 'port': 0})
        resp = self.session.start({'mode': 'redirect', 'host': '10.0.0.5',
                                   'port': 9000})
        self.assertEqual(resp['replaced'], {'owner': 'bip', 'mode': 'sink'})
        self.assertEqual(self.sink.stopped, 1)
        # each start runs the legacy _bip_session_stop (which always disables)
        self.assertEqual(self.terminal.disable_calls, 2)
        self.assertIsNone(self.session.listener)

    def test_stop_reports_what_it_replaced(self):
        self.session.start({'mode': 'redirect', 'host': '10.0.0.5',
                            'port': 9000})
        resp = self.session.start({'action': 'stop'})
        self.assertTrue(resp['ok'])
        self.assertEqual(resp['replaced'],
                         {'owner': 'bip', 'mode': 'redirect'})
        self.assertIsNone(resp['owner'])
        self.assertIsNone(resp['listener'])
        # stopping an idle session is a no-op
        self.assertIsNone(self.session.stop())

    def test_the_owner_can_be_named_by_the_caller(self):
        resp = self.session.start({'mode': 'passthru'}, owner='scp81')
        self.assertEqual(resp['owner'], 'scp81')
        self.assertEqual(self.session.stop(), {'owner': 'scp81',
                                               'mode': 'passthru'})

    def test_link_events_comes_from_the_control(self):
        self.assertTrue(self.session.link_events)
        self.session.start({'mode': 'passthru', 'link_events': False})
        self.assertFalse(self.session.link_events)
        self.session.start({'mode': 'passthru'})
        self.assertTrue(self.session.link_events)

    def test_a_bind_failure_leaves_no_half_started_session(self):
        failing = FakeSink(fail=OSError('address already in use'))
        session = bip.BipSession(terminal=self.terminal, sink=failing)
        resp = session.start({'mode': 'sink', 'port': 4123})
        self.assertEqual(resp, {'ok': False,
                                'error': 'address already in use'})
        self.assertIsNone(session.owner)
        self.assertIsNone(session.mode)
        self.assertIsNone(session.listener)
        self.assertFalse(self.terminal.enabled)

    def test_a_listener_that_cannot_be_wired_up_is_stopped(self):
        # the sink bound but terminal.enable failed: the listener object must
        # be released (its thread holds the port), not only forgotten
        class UnwirableTerminal(FakeTerminal):
            def enable(self, host=None, port=None, mode='redirect'):
                super().enable(host, port, mode)
                raise OSError('enable failed')

        sink = FakeSink(port=4123)
        terminal = UnwirableTerminal()
        session = bip.BipSession(terminal=terminal, sink=sink)
        resp = session.start({'mode': 'sink', 'port': 4123})
        self.assertEqual(resp, {'ok': False, 'error': 'enable failed'})
        self.assertEqual(sink.stopped, 1)
        self.assertIsNone(session.listener)
        self.assertIsNone(session.owner)
        self.assertFalse(terminal.enabled)


class SwapChannel(object):
    """A channel the detach seam can close (the ``BipChannel`` slice)."""

    def __init__(self, channel_id):
        self.id = channel_id
        self.closed_reported = False
        self.closed = False

    def close(self):
        self.closed = True


class SwapTerminal(FakeTerminal):
    """A FakeTerminal that records the teardown order (the detach seam)."""

    def __init__(self):
        super().__init__()
        self.calls = []
        self.closed_channels = []

    def close_all(self, link_lost=False):
        self.calls.append(('close_all', link_lost))
        for channel_id, channel in list(self.channels.items()):
            channel.close()
            self.closed_channels.append(channel_id)
            del self.channels[channel_id]

    def take_pending_events(self):
        self.calls.append(('take_pending_events',))
        return super().take_pending_events()


class CardSwapTests(unittest.TestCase):
    """``attach_card``/``detach_card``: the terminal side survives a swap."""

    def setUp(self):
        self.terminal = SwapTerminal()
        self.sink = FakeSink(port=4123)
        self.session = bip.BipSession(terminal=self.terminal, sink=self.sink)

    def test_detach_closes_the_channels_then_drains_the_events(self):
        channel = SwapChannel(1)
        self.terminal.channels[1] = channel
        self.terminal._queue_link_status(1)       # a stale queued event
        self.session.start({'mode': 'sink', 'port': 0})
        self.session.attach_card(FakeCatSession([0x0A]))
        self.session.detach_card()
        self.assertIsNone(self.session.cat_session)
        # the channels closed, marked so the monitor cannot re-queue for them
        self.assertTrue(channel.closed)
        self.assertTrue(channel.closed_reported)
        self.assertEqual(self.terminal.closed_channels, [1])
        # the drain runs after the close, and clears the stale event
        self.assertEqual(self.terminal.calls,
                         [('close_all', False), ('take_pending_events',)])
        self.assertEqual(self.terminal.pending, [])
        # the terminal side (listener/mode/owner/link_events) stays
        self.assertIs(self.session.listener, self.sink)
        self.assertEqual(self.session.mode, 'sink')
        self.assertEqual(self.session.owner, 'bip')
        self.assertTrue(self.session.link_events)

    def test_attach_card_binds_a_new_session_and_ignores_a_repeat(self):
        first = FakeCatSession([])
        second = FakeCatSession([])
        self.session.attach_card(first)
        self.assertIs(self.session.cat_session, first)
        self.session.attach_card(first)           # the same session: a no-op
        self.assertIs(self.session.cat_session, first)
        self.session.attach_card(second)
        self.assertIs(self.session.cat_session, second)
        # a detach clears it, so the next attach is meaningful again
        self.session.detach_card()
        self.assertIsNone(self.session.cat_session)
        self.session.attach_card(first)
        self.assertIs(self.session.cat_session, first)

    def test_stop_without_the_card_lock_still_cleans_up(self):
        self.session.start({'mode': 'sink', 'port': 0})
        lock = RecordingLock()
        self.session.card_lock = lock
        replaced = self.session.stop(lock=False)
        self.assertEqual(lock.entries, 0)
        self.assertEqual(replaced, {'owner': 'bip', 'mode': 'sink'})
        self.assertIsNone(self.session.listener)
        self.assertIsNone(self.session.mode)
        self.assertIsNone(self.session.owner)
        self.assertFalse(self.terminal.enabled)
        self.assertEqual(self.sink.stopped, 1)


class LockingTests(unittest.TestCase):
    """``card_lock``: taken by the state changes, inherited from the session."""

    def test_the_lock_defaults_to_the_cat_sessions(self):
        lock = RecordingLock()
        cat = FakeCatSession([0x09])
        cat.card_lock = lock
        session = bip.BipSession(cat, terminal=FakeTerminal())
        self.assertIs(session.card_lock, lock)
        self.assertTrue(session.data_available(FakeChannel(1, b'x')))
        self.assertEqual(lock.entries, 1)

    def test_the_lock_comes_from_the_real_cat_session(self):
        from simple_server import cat_session

        class Link(object):
            def send_apdu(self, apdu):
                return (b'', b'\x90\x00')

        cat = cat_session.CatSession(Link())
        session = bip.BipSession(cat, terminal=FakeTerminal())
        self.assertIs(session.card_lock, cat.card_lock)

    def test_an_explicit_lock_wins_over_the_session(self):
        lock = RecordingLock()
        cat = FakeCatSession([0x09])
        cat.card_lock = threading.RLock()
        session = bip.BipSession(cat, card_lock=lock,
                                 terminal=FakeTerminal())
        self.assertIs(session.card_lock, lock)

    def test_the_default_lock_is_reentrant(self):
        # data_available holds it while its chain ends in flush_channel_events,
        # which takes it again on the same thread (the documented requirement)
        session = bip.BipSession(terminal=FakeTerminal())
        with session.card_lock:
            with session.card_lock:
                pass

    def test_start_and_stop_take_the_card_lock_and_release_it(self):
        lock = RecordingLock()
        session = bip.BipSession(card_lock=lock, terminal=FakeTerminal(),
                                 sink=FakeSink(port=4123))
        session.start({'mode': 'passthru'})
        self.assertGreaterEqual(lock.entries, 2)    # start + the nested stop
        self.assertEqual(lock.depth, 0)
        before = lock.entries
        session.stop()
        self.assertGreater(lock.entries, before)
        self.assertEqual(lock.depth, 0)


class SinkListenerTests(unittest.TestCase):
    """The real ``TcpDumpServer`` sink: ephemeral port, accepts and logs."""

    def test_the_sink_accepts_and_logs_the_bytes(self):
        terminal = httpota.BipTerminal()
        session = bip.BipSession(terminal=terminal)
        try:
            resp = session.start({'mode': 'sink', 'port': 0})
            self.assertTrue(resp['ok'])
            port = resp['listener']['port']
            self.assertTrue(0 < port <= 0xFFFF)
            self.assertEqual(resp['listener']['mode'], 'sink')
            conn = socket.create_connection(('127.0.0.1', port), timeout=2)
            try:
                conn.sendall(b'hello sink')
            finally:
                conn.close()
            kinds, entry = [], None
            for _ in range(40):
                entries = terminal.entries_after(0)
                kinds = [e['kind'] for e in entries]
                found = [e for e in entries if e['kind'] == 'sink-rx']
                if found:
                    entry = found[0]
                    break
                time.sleep(0.05)
            self.assertIn('listener-start', kinds)
            self.assertIn('conn', kinds)
            self.assertIsNotNone(entry, kinds)
            self.assertEqual(entry['bytes'], len(b'hello sink'))
            self.assertEqual(entry['hex'], b'hello sink'.hex().upper())
        finally:
            session.stop()
            terminal.disable()
        self.assertIsNone(session.listener)


# ---- the terminal-initiated events ---------------------------------------

class DataAvailableTests(unittest.TestCase):
    def session(self, **kwargs):
        self.lock = RecordingLock(kwargs.pop('on_lock_enter', None))
        self.cat = FakeCatSession(kwargs.pop('event_list', [0x09, 0x0A]),
                                  busy=kwargs.pop('busy', False),
                                  stk_pending=kwargs.pop('stk_pending', None),
                                  on_send=kwargs.pop('on_send', None))
        terminal = kwargs.pop('terminal', None) or FakeTerminal()
        return bip.BipSession(self.cat, card_lock=self.lock,
                              terminal=terminal, **kwargs), terminal

    def test_the_subscribed_card_gets_the_data_available_event(self):
        session, terminal = self.session()
        ch = FakeChannel(3, b'12345')
        self.assertTrue(session.data_available(ch))
        # TS 102 223 §7.5.10.2: Channel status + Channel data length (8.54)
        self.assertEqual(self.cat.sent,
                         [(0x09, bytes.fromhex('b8028300' 'b70105'))])
        self.assertEqual(self.lock.entries, 1)
        self.assertIn('data-available', terminal.kinds())

    def test_the_length_is_capped_at_ff(self):
        session, _terminal = self.session()
        self.assertTrue(session.data_available(FakeChannel(1, b'x' * 400)))
        self.assertEqual(self.cat.sent[0][1][-1], 0xFF)

    def test_an_unsubscribed_card_gets_nothing(self):
        session, _terminal = self.session(event_list=[0x0A])
        self.assertFalse(session.data_available(FakeChannel(1, b'x')))
        self.assertEqual(self.cat.sent, [])
        self.assertEqual(self.lock.entries, 0)

    def test_an_empty_buffer_or_a_closed_peer_gets_nothing(self):
        session, _terminal = self.session()
        self.assertFalse(session.data_available(FakeChannel(1, b'')))
        self.assertFalse(session.data_available(FakeChannel(1, b'x',
                                                            peer_closed=True)))
        self.assertEqual(self.cat.sent, [])
        self.assertEqual(self.lock.entries, 0)

    def test_a_busy_or_pending_card_gets_nothing(self):
        for kwargs in ({'busy': True},
                       {'stk_pending': {'type': 'select_item'}}):
            with self.subTest(**kwargs):
                cat = FakeCatSession([0x09], **kwargs)
                lock = RecordingLock()
                session = bip.BipSession(cat, card_lock=lock,
                                         terminal=FakeTerminal())
                self.assertFalse(session.data_available(FakeChannel(1, b'x')))
                self.assertEqual(cat.sent, [])
                self.assertEqual(lock.entries, 0)

    def test_the_state_is_rechecked_under_the_lock(self):
        # the fast path sees an idle card; the card turns busy before the lock
        def go_busy():
            self.cat.busy = True

        session, _terminal = self.session(on_lock_enter=go_busy)
        self.assertFalse(session.data_available(FakeChannel(1, b'x')))
        self.assertEqual(self.cat.sent, [])
        self.assertEqual(self.lock.entries, 1)

    def test_a_send_failure_is_reported_and_returns_false(self):
        def boom(*_args):
            raise RuntimeError('card gone')

        session, terminal = self.session(on_send=boom)
        with self.assertLogs('simple_server.bip', 'WARNING'):
            self.assertFalse(session.data_available(FakeChannel(2, b'x')))
        skipped = [e for e in terminal.entries
                   if e['kind'] == 'data-available-skip']
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0]['reason'], 'card gone')
        self.assertEqual(self.lock.entries, 1)

    def test_the_card_connected_hook_gates_the_event(self):
        connected = {'value': False}
        session, _terminal = self.session(
            card_connected=lambda: connected['value'])
        self.assertFalse(session.data_available(FakeChannel(1, b'x')))
        connected['value'] = True
        self.assertTrue(session.data_available(FakeChannel(1, b'x')))
        self.assertEqual(len(self.cat.sent), 1)
        # a failing hook never raises into the monitor thread
        session.card_connected = lambda: 1 / 0
        with self.assertLogs('simple_server.bip', 'WARNING'):
            self.assertFalse(session.data_available(FakeChannel(1, b'x')))

    def test_without_a_cat_session_nothing_is_sent(self):
        lock = RecordingLock()
        session = bip.BipSession(None, card_lock=lock, terminal=FakeTerminal())
        self.assertFalse(session.data_available(FakeChannel(1, b'x')))
        self.assertEqual(lock.entries, 0)


class FlushChannelEventsTests(unittest.TestCase):
    def session(self, **kwargs):
        self.lock = RecordingLock()
        self.cat = FakeCatSession(kwargs.pop('event_list', [0x09, 0x0A]),
                                  on_send=kwargs.pop('on_send', None))
        self.terminal = kwargs.pop('terminal', None) or FakeTerminal()
        return bip.BipSession(self.cat, card_lock=self.lock,
                              terminal=self.terminal, **kwargs)

    def test_the_queued_events_are_sent_and_drained(self):
        session = self.session()
        self.terminal._queue_link_status(1, status=0x81, info=0x00)
        self.terminal._queue_link_status(2)          # dropped, no further info
        session.flush_channel_events()
        self.assertEqual(self.cat.sent, [
            (0x0A, bytes.fromhex('b8028100')),
            (0x0A, bytes.fromhex('b8020205'))])
        self.assertEqual(self.lock.entries, 1)
        # the queue is drained: a second flush sends nothing
        session.flush_channel_events()
        self.assertEqual(len(self.cat.sent), 2)
        self.assertEqual(self.lock.entries, 2)

    def test_an_unsubscribed_card_keeps_the_events_queued(self):
        # the legacy queue semantics: without the Channel status subscription
        # the events stay pending for a later SET UP EVENT LIST
        session = self.session(event_list=[0x09])
        self.terminal._queue_link_status(1)
        session.flush_channel_events()
        self.assertEqual(self.cat.sent, [])
        self.assertEqual(len(self.terminal.pending), 1)
        self.assertEqual(self.lock.entries, 0)

    def test_link_events_off_drains_without_sending(self):
        session = self.session(link_events=False)
        self.terminal._queue_link_status(1)
        session.flush_channel_events()
        self.assertEqual(self.cat.sent, [])
        self.assertEqual(self.terminal.pending, [])
        self.assertEqual(self.lock.entries, 0)

    def test_nothing_queued_is_a_no_op(self):
        session = self.session()
        session.flush_channel_events()
        self.assertEqual(self.cat.sent, [])
        self.assertEqual(self.lock.entries, 1)

    def test_the_reentrancy_guard_stops_a_nested_flush(self):
        calls = []

        def nested(_event_type, _data):
            calls.append(len(calls))
            session.flush_channel_events()

        session = self.session(on_send=nested)
        self.terminal._queue_link_status(1, status=0x81, info=0x00)
        session.flush_channel_events()
        self.assertEqual(len(calls), 1)          # the nested call returned

    def test_a_failing_send_drops_the_batch_and_releases_the_guard(self):
        state = {'fail': True}

        def send(_event_type, _data):
            if state['fail']:
                raise RuntimeError('card gone')

        session = self.session(on_send=send)
        self.terminal._queue_link_status(1)
        self.terminal._queue_link_status(2)
        with self.assertLogs('simple_server.bip', 'WARNING'):
            session.flush_channel_events()       # never raises
        self.assertEqual(self.terminal.pending, [])
        state['fail'] = False
        self.terminal._queue_link_status(3)
        session.flush_channel_events()           # the guard was released
        self.assertEqual(len(self.cat.sent), 1)
        self.assertEqual(self.cat.sent[0][1], bytes.fromhex('b8020305'))

    def test_without_a_cat_session_the_queue_is_kept(self):
        lock = RecordingLock()
        session = bip.BipSession(None, card_lock=lock,
                                 terminal=FakeTerminal())
        session.terminal._queue_link_status(1)
        session.flush_channel_events()
        self.assertEqual(session.terminal.pending, [{'channel': 1,
                                                     'status': 1,
                                                     'info': 5}])
        self.assertEqual(lock.entries, 0)


class ChainWiringTests(unittest.TestCase):
    """The documented injection: ``bip_handler``/``bip_flush`` on the chain.

    The real :class:`simple_server.cat_session.CatSession` fetches a BIP
    command, answers it with this module's TERMINAL RESPONSE and flushes the
    queued Channel status event when the chain ends - the wiring the server
    (3c-3) will install.
    """

    class Link(object):
        def __init__(self, *answers):
            self.answers = list(answers)
            self.sent = []

        def send_apdu(self, apdu):
            self.sent.append(bytes(apdu))
            if self.answers:
                return self.answers.pop(0)
            return (b'', b'\x90\x00')

    def test_the_chain_answers_a_bip_command_and_flushes_the_events(self):
        from simple_server import cat_session

        terminal = FakeTerminal(open_result=(1, None))
        session = bip.BipSession(terminal=terminal)
        raw = open_cmd('127.0.0.1', 8080)
        link = self.Link((raw, b'\x90\x00'), (b'', b'\x90\x00'),
                         (b'', b'\x90\x00'))
        cat = cat_session.CatSession(link,
                                     bip_handler=session.handle_command,
                                     bip_flush=session.flush_channel_events)
        session.cat_session = cat
        cat.event_list = [0x0A]
        terminal._queue_link_status(1, status=0x81, info=0x00)
        cat.handle_chain(b'\x91' + bytes([len(raw)]))
        # the FETCH, the handler's TR (its command details echoing 81) and the
        # trailing STATUS poll
        self.assertEqual(link.sent[0],
                         bytes.fromhex('80120000') + bytes([len(raw)]))
        self.assertEqual(link.sent[1],
                         bytes.fromhex('80140000') +
                         bytes([len(bytes.fromhex(TR_OPEN_OK))]) +
                         bytes.fromhex(TR_OPEN_OK))
        self.assertEqual(link.sent[2], bytes.fromhex('80f2000c00'))
        # the flush ran at the end: ENVELOPE (Channel status) with the queued
        # event (TS 102 223 §7.5.11.2)
        self.assertIn(bytes.fromhex('d60b99010a82028281b8028100'),
                      link.sent[3])
        self.assertEqual(link.sent[3][:5], bytes.fromhex('80c200000d'))

    def test_an_oversized_receive_is_still_answered_in_one_apdu(self):
        # The critical invariant: a FETCHed command always gets a TERMINAL
        # RESPONSE.  ``_terminal_response_apdu`` refuses more than 255 payload
        # octets, and the RECEIVE answer of 238 bytes would need 256 - a
        # raise at the TR send, outside the chain's handler guard (the caller
        # saw a 500 and the card waited for an answer that never came).
        from simple_server import cat_session

        terminal = FakeTerminal(receive=b'x' * 238, available=100)
        session = bip.BipSession(terminal=terminal)
        raw = channel_cmd(0x42, 0x00, bytes([0x37, 0x01, 0xFF]))
        link = self.Link((raw, b'\x90\x00'), (b'', b'\x90\x00'),
                         (b'', b'\x90\x00'))
        cat = cat_session.CatSession(link,
                                     bip_handler=session.handle_command,
                                     bip_flush=session.flush_channel_events)
        session.cat_session = cat
        cat.handle_chain(b'\x91' + bytes([len(raw)]))
        tr_apdu = link.sent[1]
        self.assertEqual(tr_apdu[:5], bytes.fromhex('80140000ff'))
        self.assertEqual(len(tr_apdu), 5 + 0xFF)     # one case-3 APDU
        self.assertIn(bytes.fromhex('3681ed'), tr_apdu)
        self.assertIn(bytes.fromhex('370164'), tr_apdu)   # 100 left


if __name__ == '__main__':
    unittest.main()
