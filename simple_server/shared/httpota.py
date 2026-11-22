"""HTTP OTA (SCP81 / GP RAM over HTTP) emulation.

Phase A: terminal-side BIP emulation (OPEN/SEND/RECEIVE/CLOSE CHANNEL) plus a
raw TCP capture listener. In the default redirect mode the card's BIP channel
is always redirected to the locally configured target (the future PSK TLS
platform) and the address the card requested is only logged; in passthru mode
the channel dials the destination the card requests in OPEN CHANNEL (TCP,
UICC in client mode, remote connection).

Reference behavior (TS 102 223 8.52-8.56, GP v2.2 Amendment B) is taken from
the captured real-terminal traces in samples/HTTP_OTA/traces:
  OPEN CHANNEL   TR: result, Channel status (38), Bearer description (35), Buffer size (39)
  SEND DATA      TR: result, Channel data length (37)
  RECEIVE DATA   TR: result, Channel data (36), Channel data length (37)
  CLOSE CHANNEL  TR: result
"""

import socket
import threading
import time

MAX_LOG = 1000


def ber_len_read(data, off):
    """Read a BER-TLV length at data[off]; returns (length, next_offset)."""
    if off >= len(data):
        return 0, off
    b = data[off]
    if b < 0x80:
        return b, off + 1
    n = b & 0x7F
    if n == 0 or off + 1 + n > len(data):
        return 0, len(data)
    return int.from_bytes(data[off + 1:off + 1 + n], 'big'), off + 1 + n


def proactive_tlvs(raw):
    """Top-level TLV map {tag: value} of a D0 proactive command."""
    out = {}
    if not raw or raw[0] != 0xD0:
        return out
    ln, off = ber_len_read(raw, 1)
    end = min(len(raw), off + ln)
    while off + 1 < end:
        tag = raw[off]
        tlen, off2 = ber_len_read(raw, off + 1)
        val = raw[off2:off2 + tlen]
        off = off2 + tlen
        out.setdefault(tag, val)
    return out


def parse_other_address(value):
    """Decode an 'Other address' TLV (21=IPv4, 57=IPv6, F0=FQDN)."""
    if not value:
        return None
    t = value[0]
    if t == 0x21 and len(value) >= 5:
        return '.'.join(str(b) for b in value[1:5])
    if t == 0x57 and len(value) >= 17:
        return ':'.join('%x' % int.from_bytes(value[i:i + 2], 'big') for i in range(1, 17, 2))
    if t == 0xF0:
        return value[1:].decode('ascii', 'replace')
    return None


def parse_transport_level(value):
    """Decode an UICC/terminal interface transport level TLV -> (proto, port)."""
    if not value or len(value) < 3:
        return None, None
    return value[0], int.from_bytes(value[1:3], 'big')


TAG_BEARER = 0x35
TAG_CHANNEL_DATA = 0x36
TAG_CHANNEL_DATA_LENGTH = 0x37
TAG_CHANNEL_STATUS = 0x38
TAG_BUFFER_SIZE = 0x39
TAG_TRANSPORT_LEVEL = 0x3C
TAG_OTHER_ADDRESS = 0x3E
TAG_NAA = 0x47


class BipChannel:
    def __init__(self, channel_id, sock, requested, target, buffer_size):
        self.id = channel_id
        self.sock = sock
        self.requested = requested
        self.target = target
        self.buffer_size = buffer_size or 512
        self.rx = bytearray()
        self.bytes_in = 0
        self.bytes_out = 0
        self.opened_at = time.time()
        self.peer_closed = False
        self.closed_reported = False
        self.notified_len = 0
        self.last_notify = 0.0

    def pump(self, timeout=0.05):
        """Move whatever the network has into the local buffer. Returns bytes moved."""
        if self.peer_closed:
            return 0
        moved = 0
        self.sock.settimeout(timeout)
        try:
            while True:
                chunk = self.sock.recv(self.buffer_size)
                if not chunk:
                    self.peer_closed = True
                    break
                self.rx.extend(chunk)
                self.bytes_in += len(chunk)
                moved += len(chunk)
                if len(chunk) < self.buffer_size:
                    break
        except (socket.timeout, BlockingIOError):
            pass
        except OSError:
            self.peer_closed = True
        return moved

    def send(self, data):
        self.sock.sendall(data)
        self.bytes_out += len(data)

    def take(self, maxlen):
        self.pump()
        n = min(maxlen, len(self.rx), self.buffer_size)
        out = bytes(self.rx[:n])
        del self.rx[:n]
        return out

    def available(self):
        self.pump()
        return len(self.rx)

    def send_capacity(self):
        free = self.buffer_size - len(self.rx)
        return 0xFF if free > 0xFF else max(0, free)

    def close(self):
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


class BipTerminal:
    """Terminal (device) side of BIP: channels to the configured target."""

    def __init__(self):
        self.enabled = False
        self.mode = 'redirect'
        self.target = None
        self.channels = {}
        self.next_id = 1
        self.entries = []
        self.seq = 0
        self.lock = threading.Lock()
        self.pending_events = []
        self.on_data = None
        self._monitor = None

    def log(self, kind, **fields):
        with self.lock:
            self.seq += 1
            entry = {'seq': self.seq, 't': time.time(), 'kind': kind}
            entry.update(fields)
            self.entries.append(entry)
            if len(self.entries) > MAX_LOG:
                del self.entries[:len(self.entries) - MAX_LOG]
            return entry

    def _monitor_loop(self):
        """Watch channels for incoming bytes and ask the card to fetch them.

        The card only learns about server data through the Data available
        event (TS 102 223 7.5.10), so the socket must be pumped even while
        the card is idle."""
        while True:
            time.sleep(0.25)
            with self.lock:
                channels = list(self.channels.values())
            for ch in channels:
                try:
                    ch.pump()
                except OSError:
                    ch.peer_closed = True
                if ch.peer_closed and not ch.closed_reported and not ch.rx:
                    # Report a dropped link (TS 102 223 7.5.11) only once the
                    # buffered server data has been fetched: signalling the
                    # drop while bytes are still waiting makes the card abort
                    # the fetch and end the session prematurely.
                    ch.closed_reported = True
                    self.log('peer-close', channel=ch.id)
                    self._queue_link_status(ch.id)
                if (self.on_data and ch.rx and not ch.peer_closed
                        and (len(ch.rx) > ch.notified_len
                             or time.time() - ch.last_notify > 2.0)):
                    # Re-notify while data stays unfetched: the live card
                    # sometimes needs the Data available event again to drain
                    # a partially received TLS record.
                    if self.on_data(ch):
                        ch.notified_len = len(ch.rx)
                        ch.last_notify = time.time()

    def _start_monitor(self):
        if self._monitor is None or not self._monitor.is_alive():
            self._monitor = threading.Thread(target=self._monitor_loop,
                                             name='bip-monitor', daemon=True)
            self._monitor.start()

    def enable(self, host=None, port=None, mode='redirect'):
        """Enable the BIP terminal.

        'redirect' (default) pins one target: every channel goes there whatever
        address the card requests. 'passthru' has no target at all: every
        channel dials the destination the card requested in OPEN CHANNEL."""
        self.mode = mode if mode in ('redirect', 'passthru') else 'redirect'
        self.target = (host, int(port)) if host and port not in (None, '') else None
        self.enabled = True
        self.log('enabled', mode=self.mode,
                 target='%s:%d' % self.target if self.target else None)
        self._start_monitor()

    def disable(self):
        self.enabled = False
        self.log('disabled')
        self.close_all(link_lost=True)
        self.target = None

    def close_all(self, link_lost=False):
        for ch in list(self.channels.values()):
            self._close_channel(ch, link_lost=link_lost)

    def _close_channel(self, ch, link_lost=False):
        ch.close()
        if self.channels.get(ch.id) is ch:
            del self.channels[ch.id]
        if link_lost:
            self._queue_link_status(ch.id)

    def _queue_link_status(self, channel_id, status=None, info=0x05):
        """Record a BIP link change that did not result from a proactive
        command (TS 102 223 7.5.11). The default is link not established +
        info 05 = link dropped; a successful background-mode OPEN CHANNEL
        reports link established instead. The server turns these into
        ENVELOPE (Channel status)."""
        with self.lock:
            if any(e['channel'] == channel_id for e in self.pending_events):
                return
            self.pending_events.append({
                'channel': channel_id,
                'status': channel_id & 0x07 if status is None else status,
                'info': info})

    def take_pending_events(self):
        with self.lock:
            events, self.pending_events = self.pending_events, []
            return events

    def _check_peer(self, ch):
        """Notify once per channel when the peer closed the connection, after
        any buffered data has been fetched (see _monitor_loop)."""
        if ch.peer_closed and not ch.closed_reported and not ch.rx:
            ch.closed_reported = True
            self.log('peer-close', channel=ch.id)
            self._queue_link_status(ch.id)

    def _alloc_id(self):
        for _ in range(7):
            cid = self.next_id
            self.next_id = 1 if cid >= 7 else cid + 1
            if cid not in self.channels:
                return cid
        return None

    def open(self, requested_host, requested_port, buffer_size, proto=None):
        """Open a channel.

        Redirect modes connect to the pinned target; passthru dials the
        destination the card sent in OPEN CHANNEL (Other address + Transport
        level port). Returns (channel_id, error)."""
        if not self.enabled:
            return None, 'bip disabled'
        requested = '%s:%s' % (requested_host, requested_port)
        if self.mode == 'passthru':
            # Use the card's request as-is: TCP, UICC in client mode, remote
            # connection (TS 102 223 6.4.27.2 / 8.59). The specs define no
            # default port, so an incomplete or non-TCP request fails.
            host = (requested_host or '').strip()
            try:
                port = int(requested_port)
            except (TypeError, ValueError):
                port = 0
            if proto != 0x02:
                reason = 'card did not request TCP client transport (passthru)'
            elif not host or host == '-':
                reason = 'card did not request a destination address (passthru)'
            elif not 0 < port <= 0xFFFF:
                reason = 'card did not request a valid port (passthru)'
            else:
                reason = None
            if reason:
                self.log('open-fail', requested=requested, reason=reason)
                return None, reason
            target = (host, port)
        else:
            if not self.target:
                return None, 'bip disabled'
            target = self.target
        cid = self._alloc_id()
        if cid is None:
            self.log('open-fail', requested=requested, reason='no free channel')
            return None, 'no free channel'
        try:
            sock = socket.create_connection(target, timeout=2.0)
        except OSError as e:
            self.log('open-fail', requested=requested, target='%s:%d' % target, reason=str(e))
            return None, str(e)
        ch = BipChannel(cid, sock, requested, target, buffer_size)
        self.channels[cid] = ch
        self.log('open', channel=cid, requested=requested, target='%s:%d' % target,
                 buffer_size=ch.buffer_size)
        return cid, None

    def send(self, channel_id, data):
        ch = self.channels.get(channel_id)
        if not ch:
            return False
        try:
            ch.send(data)
        except OSError as e:
            self.log('send-fail', channel=channel_id, error=str(e))
            self._close_channel(ch, link_lost=True)
            return False
        self.log('send', channel=channel_id, bytes=len(data), hex=data.hex().upper()[:2000])
        return True

    def receive(self, channel_id, maxlen):
        ch = self.channels.get(channel_id)
        if not ch:
            return None
        data = ch.take(maxlen)
        if data:
            self.log('receive', channel=channel_id, bytes=len(data), remaining=len(ch.rx),
                     hex=data.hex().upper()[:2000])
            # The TR announced the remainder via the channel-data-length TLV,
            # but the live card still waits for a fresh Data available event
            # before fetching it - re-arm the notification for what is left.
            ch.notified_len = 0
        self._check_peer(ch)
        return data

    def available(self, channel_id):
        ch = self.channels.get(channel_id)
        if not ch:
            return 0
        n = ch.available()
        self._check_peer(ch)
        return n

    def send_capacity(self, channel_id):
        ch = self.channels.get(channel_id)
        if not ch:
            return 0
        n = ch.send_capacity()
        self._check_peer(ch)
        return n

    def clear_log(self):
        with self.lock:
            self.entries = []

    def close(self, channel_id):
        ch = self.channels.get(channel_id)
        if not ch:
            return False
        self.log('close', channel=channel_id, bytes_in=ch.bytes_in, bytes_out=ch.bytes_out)
        self._close_channel(ch)
        return True

    def status(self):
        channels = []
        for ch in self.channels.values():
            channels.append({
                'id': ch.id,
                'requested': ch.requested,
                'target': '%s:%d' % ch.target,
                'buffer_size': ch.buffer_size,
                'bytes_in': ch.bytes_in,
                'bytes_out': ch.bytes_out,
                'pending': len(ch.rx),
                'peer_closed': ch.peer_closed,
            })
        return {
            'enabled': self.enabled,
            'mode': self.mode,
            'target': '%s:%d' % self.target if self.target else None,
            'channels': channels,
            'seq': self.seq,
        }

    def entries_after(self, after=0):
        with self.lock:
            return [e for e in self.entries if e['seq'] > after]


class TcpDumpServer:
    """Plain TCP listener that logs whatever it receives (ClientHello capture).

    Used as the BIP redirect target until the PSK TLS platform is brought up.
    """

    def __init__(self, host, port, on_rx=None, on_log=None):
        self.on_rx = on_rx
        self.on_log = on_log
        self.stopped = False
        self.conns = []
        self.accepted = 0
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((host, int(port)))
        self.sock.listen(4)
        self.host, self.port = self.sock.getsockname()[:2]
        self.thread = threading.Thread(target=self._accept_loop, daemon=True)
        self.thread.start()
        if self.on_log:
            self.on_log('listener-start', host=self.host, port=self.port)

    def _accept_loop(self):
        while not self.stopped:
            try:
                self.sock.settimeout(0.2)
                conn, addr = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            self.conns.append(conn)
            self.accepted += 1
            if self.on_log:
                self.on_log('conn', peer='%s:%d' % addr[:2])
            threading.Thread(target=self._conn_loop, args=(conn, addr), daemon=True).start()

    def _conn_loop(self, conn, addr):
        total = 0
        try:
            while not self.stopped:
                conn.settimeout(0.2)
                try:
                    data = conn.recv(4096)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not data:
                    break
                total += len(data)
                if self.on_rx:
                    self.on_rx('%s:%d' % addr[:2], data)
        finally:
            if self.on_log:
                self.on_log('conn-close', peer='%s:%d' % addr[:2], bytes=total)
            try:
                conn.close()
            except OSError:
                pass

    def stop(self):
        self.stopped = True
        if self.on_log:
            self.on_log('listener-stop', host=self.host, port=self.port)
        try:
            self.sock.close()
        except OSError:
            pass
        for conn in self.conns:
            try:
                conn.close()
            except OSError:
                pass
        self.conns = []
        # Wait for the accept loop to leave accept(): a thread blocked in
        # accept() keeps the listening socket alive for up to its poll
        # timeout, so the port must be released before stop() returns
        # (restarting a listener on the same port must not race).
        try:
            self.thread.join(timeout=2)
        except RuntimeError:
            pass
