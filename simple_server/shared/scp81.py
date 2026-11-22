"""Phase B: PSK TLS server and HTTP administration session for SCP81.

Implements the Remote Administration Server side of GP RAM over HTTP
(GPC v2.2 Amendment B):

  - TLS 1.2 with the PSK cipher suites of clause 4.3.2. The handshake and
    record layer are handled by the stdlib ``ssl`` module through OpenSSL's
    PSK callbacks (identity -> PSK), so no TLS code lives here.
  - The HTTP dialog of clause 4.4: parse the Security Domain's POST
    (``X-Admin-*`` headers, optional body with the previous response string)
    and answer with 200 + a command string, or 204 No Content to close the
    administration session.

The card talks TLS *through* the BIP channel: this server listens on the
local redirect target and the BIP terminal proxies the card's SEND/RECEIVE
DATA records to it.
"""

import socket
import ssl
import sys
import threading
import time

MAX_HEAD = 32 * 1024
MAX_BODY = 1 * 1024 * 1024

# TLS_PSK_* suites from GPC v2.2 Amendment B Table 4-2 / RFC 4279/4785/5487.
PSK_CIPHERS = ':'.join([
    'PSK-AES128-CBC-SHA256',   # TLS_PSK_WITH_AES_128_CBC_SHA256 (0x00AE)
    'PSK-AES128-CBC-SHA',      # TLS_PSK_WITH_AES_128_CBC_SHA    (0x008C)
    'PSK-AES256-CBC-SHA',      # TLS_PSK_WITH_AES_256_CBC_SHA    (0x008D)
    'PSK-3DES-EDE-CBC-SHA',    # TLS_PSK_WITH_3DES_EDE_CBC_SHA   (0x008B)
    'PSK-NULL-SHA256',         # TLS_PSK_WITH_NULL_SHA256        (0x00B0)
    'PSK-NULL-SHA',            # TLS_PSK_WITH_NULL_SHA           (0x002C)
])

GP_PROTOCOL = 'globalplatform-remote-admin/1.0'
GP_CT_COMMAND = 'application/vnd.globalplatform.card-content-mgt;version=1.0'
GP_CT_RESPONSE = 'application/vnd.globalplatform.card-content-mgt-response;version=1.0'

# OpenSSL SSL_OP_NO_ENCRYPT_THEN_MAC (not exposed by the ssl module). The live
# card offers the encrypt_then_mac extension but aborts the session with
# SSLV3_ALERT_UNEXPECTED_MESSAGE as soon as the server echoes it, so keep the
# extension out of the ServerHello (verified live 2026-09-15).
OP_NO_ENCRYPT_THEN_MAC = 0x00080000

TLS_VERSIONS = {
    '1.0': ssl.TLSVersion.TLSv1,
    '1.1': ssl.TLSVersion.TLSv1_1,
    '1.2': ssl.TLSVersion.TLSv1_2,
}


def _norm_identity(identity):
    """Normalize a PSK identity to the str OpenSSL reports (CPython hands it
    to the PSK callback as a str; bytes are decoded byte-exact)."""
    if identity is None:
        return None
    if isinstance(identity, (bytes, bytearray)):
        return bytes(identity).decode('latin-1')
    return str(identity)


def parse_http_request(data):
    """Parse an HTTP/1.1 request head (bytes up to CRLFCRLF) into
    (method, target, headers dict with lower-case names)."""
    head = data.split(b'\r\n\r\n', 1)[0]
    lines = head.split(b'\r\n')
    parts = lines[0].split(b' ')
    if len(parts) < 3:
        raise ValueError('malformed request line')
    method, target = parts[0].decode('latin-1'), parts[1].decode('latin-1')
    headers = {}
    for line in lines[1:]:
        name, _, value = line.partition(b':')
        headers[name.strip().decode('latin-1').lower()] = value.strip().decode('latin-1')
    return method, target, headers


def decode_chunked(body):
    """Decode a chunked transfer body (RFC 2616 3.6.1)."""
    out = bytearray()
    while body:
        line, _, rest = body.partition(b'\r\n')
        try:
            size = int(line.split(b';')[0], 16)
        except ValueError:
            raise ValueError('bad chunk size %r' % line[:16])
        if size == 0:
            break
        out.extend(rest[:size])
        body = rest[size + 2:]
    return bytes(out)


def build_http_response(status, reason, headers, body=b'', chunked=False,
                        compact=False, connection=None):
    """Build an HTTP response. With chunked=True the body is framed as 100-byte
    chunks (like the reference admin server); with compact=True header names
    and values are separated by ':' without whitespace, which keeps the whole
    response inside one card-sized TLS record (<= 256 bytes ciphertext).
    connection ('close'/'keep-alive') declares the connection fate: without
    it an HTTP/1.1 client assumes the connection persists and tries to reuse
    it for the next POST instead of dialing a new one (live card 2026-09-15)."""
    lines = ['HTTP/1.1 %d %s' % (status, reason)]
    sep = ':' if compact else ': '
    for name, value in headers.items():
        lines.append('%s%s%s' % (name, sep, value))
    if connection:
        lines.append('Connection%s%s' % (sep, connection))
    has_te = 'transfer-encoding' in [k.lower() for k in headers]
    if body and (chunked or has_te):
        if not has_te:
            lines.append('Transfer-Encoding: chunked')
    elif body and 'content-length' not in [k.lower() for k in headers]:
        lines.append('Content-Length%s%d' % (sep, len(body)))
    head = ('\r\n'.join(lines) + '\r\n\r\n').encode('latin-1')
    if not body:
        return head
    if not chunked:
        return head + body
    out = bytearray(head)
    for i in range(0, len(body), 100):
        piece = body[i:i + 100]
        out += ('%X\r\n' % len(piece)).encode('latin-1') + piece + b'\r\n'
    out += b'0\r\n\r\n'
    return bytes(out)


class PskTlsServer:
    """PSK TLS listener speaking the GP remote administration HTTP dialog."""

    def __init__(self, host, port, psk=None, identity=None, on_log=None,
                 responder=None, timeout=10.0, chunked=False, chunk_size=0,
                 compact_headers=False, tls_version='auto',
                 cipher=None, keylog=None,
                 conn_header=None, half_close=False, answer_delay=0.0,
                 psk_map=None):
        # PSK lookup table: identity -> key. With an explicit psk_map a
        # handshake is accepted only for a listed identity; the legacy
        # single-key form (psk + optional identity pin, pin None = accept any
        # identity) remains for scripts and tests.
        self.wildcard_psk = None
        self.psk_map = {}
        if psk_map is not None:
            self.psk_map = {_norm_identity(k): bytes(v)
                            for k, v in dict(psk_map).items() if v}
        elif psk is not None:
            pin = _norm_identity(identity)
            if pin is None:
                self.wildcard_psk = psk
            else:
                self.psk_map = {pin: bytes(psk)}
        self.psk = psk
        self.identity = _norm_identity(identity)
        self.on_log = on_log
        self.responder = responder or self._default_responder
        self.timeout = timeout
        self.chunked = chunked
        # chunk_size 0 = one record for the whole response
        self.chunk_size = int(chunk_size)
        self.compact_headers = compact_headers
        # TLS is permissive by default: 'auto' accepts TLS 1.0-1.2 and lets
        # OpenSSL pick the highest the card offers. The '1.0'/'1.1'/'1.2'
        # pins are debugging aids for a card that offers 1.2 but mishandles
        # it; no setting is needed for normal use.
        self.tls_version = (tls_version if tls_version == 'auto'
                            or tls_version in TLS_VERSIONS else 'auto')
        # Pin one cipher suite (e.g. PSK-AES128-CBC-SHA) if the card's SD only
        # maps a specific suite to a usable SCP81 security level.
        self.cipher = cipher or None
        # Debug aid: write the TLS traffic secrets to this file
        # (SSLKEYLOGFILE format), so captures of the PSK dialog can be
        # decrypted (tshark etc). Contains key material - use a temp path.
        self.keylog = keylog or None
        # Connection header value: None/'none' = omit the header (implicit
        # HTTP/1.1 keep-alive); 'keep-alive' adds it explicitly. The server
        # never closes mid-session - only the 204 ends the dialog.
        self.conn_header = conn_header or None
        # TLS half-close after a script body. NOTE (live 2026-09-16):
        # CPython's SSLSocket.unwrap() poisons the session when the peer does
        # not answer with its own close_notify in time, so this cannot be
        # implemented with the stdlib ssl module; the flag is kept for the
        # option surface and for cards that answer promptly (the exception
        # path leaves the session unusable, so it is off by default).
        self.half_close = half_close
        # Wait before answering a request (cards may need their BIP SEND DATA
        # conversation to settle before they accept the response; 0 = answer
        # immediately).
        self.answer_delay = float(answer_delay or 0)
        self.identity_seen = None
        self.identity_matched = None
        self.version_seen = None
        self.cipher_seen = None
        self.stopped = False
        self.conns = []
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # A quick Stop -> Start can race the previous listener's close (the
        # port stays busy for a moment); retry before giving up.
        last_error = None
        for _ in range(10):
            try:
                self.sock.bind((host, int(port)))
                last_error = None
                break
            except OSError as e:
                last_error = e
                time.sleep(0.3)
        if last_error is not None:
            self.sock.close()
            raise last_error
        self.sock.listen(4)
        self.host, self.port = self.sock.getsockname()[:2]
        self.ctx = self._make_context()
        if self.keylog:
            try:
                self.ctx.keylog_filename = self.keylog
            except (AttributeError, OSError):
                self.keylog = None
        self.thread = threading.Thread(target=self._accept_loop, daemon=True)
        self.thread.start()
        self.log('tls-listener-start', host=self.host, port=self.port)

    def log(self, kind, **fields):
        if self.on_log:
            try:
                self.on_log(kind, **fields)
            except Exception:
                pass

    def _make_context(self):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        if self.tls_version == 'auto':
            # Accept everything the cards speak; OpenSSL negotiates the
            # highest common version.
            ctx.minimum_version = ssl.TLSVersion.TLSv1
            ctx.maximum_version = ssl.TLSVersion.TLSv1_2
        else:
            ver = TLS_VERSIONS[self.tls_version]
            ctx.minimum_version = ver
            ctx.maximum_version = ver
        ciphers = self.cipher or PSK_CIPHERS
        if self.tls_version in ('auto', '1.0', '1.1'):
            # OpenSSL 3.x disables the legacy protocol versions by default.
            ciphers += ':@SECLEVEL=0'
        ctx.set_ciphers(ciphers)
        # Prefer our (AES-first) order over the card's NULL-suite-first list.
        ctx.options |= ssl.OP_CIPHER_SERVER_PREFERENCE
        ctx.options |= OP_NO_ENCRYPT_THEN_MAC
        # No TLS session resumption: the live card aborts with
        # SSLV3_ALERT_UNEXPECTED_MESSAGE on the post-handshake
        # NewSessionTicket record (verified live 2026-09-15).
        ctx.options |= ssl.OP_NO_TICKET
        if not hasattr(ctx, 'set_psk_server_callback'):
            # ssl's TLS-PSK callbacks exist only since Python 3.13.
            raise RuntimeError(
                'TLS-PSK requires Python 3.13+ (ssl.SSLContext.set_psk_server_callback '
                'is missing in %s)' % sys.version.split()[0])
        ctx.set_psk_server_callback(self._psk_cb)
        return ctx

    def _psk_cb(self, identity):
        """OpenSSL asks for the key of the identity the client sent.

        The identity is looked up in the configured table (identity -> key);
        without a match the handshake fails on the Finished MAC check with a
        dummy key, and the attempt is logged as 'tls-psk-unknown'."""
        ident = _norm_identity(identity)
        self.identity_seen = ident
        key = self.psk_map.get(ident) if ident is not None else None
        if key is None:
            # Legacy single-key mode: no identity pin accepts any identity.
            key = self.wildcard_psk
        self.identity_matched = key is not None
        if key is None:
            self.log('tls-psk-unknown', identity=ident)
            return b'\x00' * 16
        return key

    @property
    def psk_identities(self):
        """Identities the listener looks up (keys are never exposed)."""
        return sorted(self.psk_map)

    def set_psk_map(self, psk_map):
        """Replace the identity -> key table of a running listener."""
        self.psk_map = {_norm_identity(k): bytes(v)
                        for k, v in dict(psk_map).items() if v}
        self.wildcard_psk = None
        return self.psk_identities

    @staticmethod
    def _default_responder(method, target, headers, body):
        """No script configured: close the administration session (4.4.2)."""
        return 204, {'X-Admin-Protocol': GP_PROTOCOL}, b''

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
            peer = '%s:%d' % addr[:2]
            threading.Thread(target=self._conn_loop, args=(conn, peer),
                             daemon=True).start()

    def _read_request(self, tls):
        buf = b''
        while b'\r\n\r\n' not in buf:
            chunk = tls.recv(4096)
            if not chunk:
                return None
            buf += chunk
            if len(buf) > MAX_HEAD:
                raise ValueError('request head too large')
        head, _, rest = buf.partition(b'\r\n\r\n')
        method, target, headers = parse_http_request(head + b'\r\n\r\n')
        body = rest
        if 'content-length' in headers:
            want = int(headers['content-length'])
            while len(body) < want:
                chunk = tls.recv(4096)
                if not chunk:
                    break
                body += chunk
            body = body[:want]
        elif headers.get('transfer-encoding', '').lower() == 'chunked':
            while not body.endswith(b'0\r\n\r\n'):
                chunk = tls.recv(4096)
                if not chunk:
                    break
                body += chunk
            body = decode_chunked(body)
        return method, target, headers, body

    def _conn_loop(self, conn, peer):
        tls = None
        handshake_done = False
        try:
            tls = self.ctx.wrap_socket(conn, server_side=True)
            handshake_done = True
            self.version_seen = tls.version()
            self.cipher_seen = (tls.cipher() or (None,))[0]
            self.log('tls-handshake', peer=peer, cipher=self.cipher_seen,
                     version=self.version_seen, identity=self.identity_seen,
                     psk_match=self.identity_matched)
            while not self.stopped:
                req = self._read_request(tls)
                if req is None:
                    break
                method, target, headers, body = req
                if self.answer_delay > 0:
                    time.sleep(self.answer_delay)
                self.log('tls-request', peer=peer, method=method, uri=target,
                         headers=headers,
                         agent=headers.get('x-admin-from'),
                         protocol=headers.get('x-admin-protocol'),
                         script_status=headers.get('x-admin-script-status'),
                         resume=headers.get('x-admin-resume'),
                         content_type=headers.get('content-type'),
                         bytes=len(body), body_hex=body.hex().upper()[:2000] or None)
                status, resp_headers, resp_body = self.responder(
                    method, target, headers, body)
                reason = {200: 'OK', 204: 'No Content'}.get(status, 'Status')
                conn_hdr = None if self.conn_header in (None, 'none') else self.conn_header
                response = build_http_response(
                    status, reason, resp_headers, resp_body,
                    chunked=self.chunked, compact=self.compact_headers,
                    connection=conn_hdr)
                # The card's HTTP client reads its response record-by-record:
                # the whole response must arrive in ONE TLS record (chunk_size
                # 0), otherwise a split head stalls it and a head-only record
                # followed by the body draws an unexpected_message alert. When
                # a chunk_size is given, the head goes in one record and the
                # body in pieces of that size.
                if self.chunk_size <= 0:
                    tls.sendall(response)
                else:
                    head, sep, rest = response.partition(b'\r\n\r\n')
                    tls.sendall(head + sep if sep else head)
                    for off in range(0, len(rest), self.chunk_size):
                        tls.sendall(rest[off:off + self.chunk_size])
                self.log('tls-response', peer=peer, status=status,
                         bytes=len(resp_body), chunked=self.chunked,
                         response_hex=response.hex().upper()[:600],
                         body_hex=resp_body.hex().upper()[:2000] or None)
                # Only the end of the dialog closes the connection: 204 (or
                # an empty body) ends the session; every other response
                # leaves the TLS connection open for the card's next POST.
                # Reusing it - or dialing a fresh one - is the card's call
                # (GP Am. B 4.3.1: the SD manages connection establishment).
                if status == 204 or not resp_body:
                    # Clean TLS shutdown with the response still in the BIP
                    # buffer: the card fetches the 204 and the close_notify
                    # together, then the FIN. A bare close here makes the
                    # card abort the session with a fatal alert.
                    plain = None
                    try:
                        tls.settimeout(2.0)
                        plain = tls.unwrap()
                        tls = None
                    except Exception:
                        plain = None
                    if plain is not None:
                        try:
                            plain.close()
                        except OSError:
                            pass
                    break
        except ssl.SSLError as e:
            if handshake_done:
                self.log('tls-error', peer=peer, error=str(e))
            else:
                # No shared cipher / unsupported protocol version / card
                # alert: keep the handshake reason distinguishable from
                # post-handshake record errors.
                self.log('tls-handshake-failed', peer=peer, error=str(e))
        except (OSError, ValueError) as e:
            self.log('tls-error', peer=peer, error=str(e))
        finally:
            if tls is not None:
                try:
                    tls.close()
                except OSError:
                    pass
            else:
                try:
                    conn.close()
                except OSError:
                    pass
            self.log('tls-close', peer=peer)
            if conn in self.conns:
                self.conns.remove(conn)

    def stop(self):
        self.stopped = True
        self.log('tls-listener-stop', host=self.host, port=self.port)
        try:
            self.sock.close()
        except OSError:
            pass
        for conn in list(self.conns):
            try:
                conn.close()
            except OSError:
                pass
        self.conns = []
