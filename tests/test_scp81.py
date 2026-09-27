#!/usr/bin/env python3
"""Phase B tests: PSK TLS server, GP HTTP administration session, and the
terminal's Data available notification (TS 102 223 7.5.10).

Synthetic PSK only; no live card data.
"""

import socket
import ssl
import sys
import time
import types
import unittest
from pathlib import Path
from unittest import mock

PROJECTS = Path(__file__).resolve().parents[2]
PY_SIM = PROJECTS / 'pysim'
if str(PY_SIM) not in sys.path:
    sys.path.insert(0, str(PY_SIM))

from pysim_simple_server import scp81
import pysim_simple_server.server as server

PSK = bytes.fromhex('00112233445566778899aabbccddeeff')
IDENT = '89012345678901234567'

# The reference administration sequence (now a PWA-side 'Explore' template);
# tests use it as a generic multi-APDU script.
EXPLORE = ['80CAFF2100', '80F28002024F0000', '80CA008500',
           '80F24002024F0000', '80F22002024F0000', '80F21002024F0000']


def reset_script(script=None, kind='test'):
    server._scp81_reset_script(EXPLORE if script is None else script, kind)


class HttpParseTest(unittest.TestCase):
    def test_parse_request(self):
        raw = (b'POST /server/adminagent?cmd=1 HTTP/1.1\r\n'
               b'Host: 172.96.0.1\r\n'
               b'X-Admin-Protocol: globalplatform-remote-admin/1.0\r\n'
               b'X-Admin-From: 0123456789\r\n\r\n')
        method, target, headers = scp81.parse_http_request(raw)
        self.assertEqual(method, 'POST')
        self.assertEqual(target, '/server/adminagent?cmd=1')
        self.assertEqual(headers['host'], '172.96.0.1')
        self.assertEqual(headers['x-admin-from'], '0123456789')

    def test_parse_request_malformed(self):
        with self.assertRaises(ValueError):
            scp81.parse_http_request(b'GARBAGE\r\n\r\n')

    def test_decode_chunked(self):
        body = b'4\r\nABCD\r\n5\r\nEFGHI\r\n0\r\n\r\n'
        self.assertEqual(scp81.decode_chunked(body), b'ABCDEFGHI')

    def test_build_response_sets_content_length(self):
        out = scp81.build_http_response(200, 'OK',
                                        {'Content-Type': scp81.GP_CT_COMMAND}, b'\x80\x01\x00')
        self.assertTrue(out.startswith(b'HTTP/1.1 200 OK\r\n'))
        self.assertIn(b'Content-Length: 3\r\n\r\n\x80\x01\x00', out)

    def test_build_response_connection_header(self):
        out = scp81.build_http_response(200, 'OK', {}, b'\x01',
                                        connection='close')
        self.assertIn(b'Connection: close\r\n', out)
        out = scp81.build_http_response(200, 'OK', {}, b'\x01',
                                        compact=True, connection='keep-alive')
        self.assertIn(b'Connection:keep-alive\r\n', out)

    def test_build_response_chunked(self):
        out = scp81.build_http_response(200, 'OK', {},
                                        b'\x80' * 150, chunked=True)
        self.assertIn(b'Transfer-Encoding: chunked', out)
        self.assertNotIn(b'Content-Length', out)
        head, _, body = out.partition(b'\r\n\r\n')
        self.assertEqual(scp81.decode_chunked(body), b'\x80' * 150)
        # 100-byte chunks like the reference admin server
        self.assertTrue(body.startswith(b'64\r\n'))


class PskTlsServerTest(unittest.TestCase):
    def _client_ctx(self, identity=IDENT, psk=PSK):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.maximum_version = ssl.TLSVersion.TLSv1_2
        ctx.set_ciphers('PSK-AES128-CBC-SHA256:PSK-AES128-CBC-SHA')
        ctx.set_psk_client_callback(lambda hint: (identity.encode(), psk))
        return ctx

    def _connect(self, srv, ctx=None):
        sock = socket.create_connection(('127.0.0.1', srv.port), timeout=5)
        try:
            return (ctx or self._client_ctx()).wrap_socket(sock)
        except Exception:
            sock.close()
            raise

    def test_tls_version_auto_is_permissive(self):
        # TLS is automatic by default: accept TLS 1.0-1.2, OpenSSL picks the
        # highest the card offers.
        srv = scp81.PskTlsServer('127.0.0.1', 0, psk_map={IDENT: PSK})
        try:
            self.assertEqual(srv.tls_version, 'auto')
            self.assertEqual(srv.ctx.minimum_version, ssl.TLSVersion.TLSv1)
            self.assertEqual(srv.ctx.maximum_version, ssl.TLSVersion.TLSv1_2)
        finally:
            srv.stop()

    def test_tls_version_pin_remains_available(self):
        srv = scp81.PskTlsServer('127.0.0.1', 0, psk_map={IDENT: PSK},
                                 tls_version='1.0')
        try:
            self.assertEqual(srv.ctx.minimum_version, ssl.TLSVersion.TLSv1)
            self.assertEqual(srv.ctx.maximum_version, ssl.TLSVersion.TLSv1)
        finally:
            srv.stop()

    def test_handshake_and_204_session(self):
        logs = []
        srv = scp81.PskTlsServer('127.0.0.1', 0, PSK, identity=IDENT,
                                 on_log=lambda k, **f: logs.append((k, f)))
        try:
            tls = self._connect(srv)
            self.assertEqual(tls.version(), 'TLSv1.2')
            self.assertEqual(tls.cipher()[0], 'PSK-AES128-CBC-SHA256')
            tls.sendall(b'POST /server/adminagent?cmd=1 HTTP/1.1\r\n'
                        b'Host: 172.96.0.1\r\n'
                        b'X-Admin-Protocol: globalplatform-remote-admin/1.0\r\n'
                        b'X-Admin-From: 0123456789\r\n\r\n')
            reply = tls.recv(4096)
            self.assertTrue(reply.startswith(b'HTTP/1.1 204 No Content\r\n'))
            self.assertIn(b'X-Admin-Protocol: globalplatform-remote-admin/1.0', reply)
            tls.close()
            deadline = time.time() + 3
            while time.time() < deadline and srv.identity_seen is None:
                time.sleep(0.05)
            self.assertEqual(srv.identity_seen, IDENT)
            kinds = [k for k, _ in logs]
            self.assertIn('tls-handshake', kinds)
            self.assertIn('tls-request', kinds)
            self.assertIn('tls-response', kinds)
            req = [f for k, f in logs if k == 'tls-request'][0]
            self.assertEqual(req['uri'], '/server/adminagent?cmd=1')
            self.assertEqual(req['agent'], '0123456789')
        finally:
            srv.stop()

    def _recv(self, tls):
        try:
            return tls.recv(4096)
        except (ssl.SSLError, OSError):
            return b''

    def _read_http(self, tls):
        buf = b''
        while b'\r\n\r\n' not in buf:
            chunk = self._recv(tls)
            if not chunk:
                return buf
            buf += chunk
        head, _, rest = buf.partition(b'\r\n\r\n')
        if b'transfer-encoding: chunked' in head.lower():
            while b'0\r\n\r\n' not in rest:
                chunk = self._recv(tls)
                if not chunk:
                    break
                rest += chunk
            return head + b'\r\n\r\n' + rest
        length = 0
        for line in head.split(b'\r\n'):
            if line.lower().startswith(b'content-length:'):
                length = int(line.split(b':')[1])
        while len(rest) < length:
            chunk = self._recv(tls)
            if not chunk:
                break
            rest += chunk
        return head + b'\r\n\r\n' + rest

    def test_scripted_session_over_tls(self):
        old_bip = server._BIP
        server._BIP = mock.Mock()
        server._BIP.log = lambda *a, **k: None
        reset_script(['80CAFF2100'])
        server._SCP81_SCRIPT_NEXT = 0
        server._SCP81_SCRIPT_RESULTS = []
        srv = scp81.PskTlsServer('127.0.0.1', 0, PSK,
                                 responder=server._scp81_script_responder)
        try:
            tls = self._connect(srv)
            tls.sendall(b'POST /api/scp81 HTTP/1.1\r\nHost: 127.0.0.1\r\n'
                        b'X-Admin-Protocol: globalplatform-remote-admin/1.0\r\n'
                        b'X-Admin-From: simple\r\n\r\n')
            reply = self._read_http(tls)
            self.assertIn(b'HTTP/1.1 200 OK', reply)
            self.assertIn(b'X-Admin-Next-URI: /api/scp81?req=1', reply)
            self.assertIn(bytes.fromhex('ae80220580caff21000000'), reply)
            # Respond with the R-APDU (Response Scripting template)
            body = bytes.fromhex('af80' '800101' '2304' '93059000' '0000')
            tls.sendall(b'POST /api/scp81?step=1 HTTP/1.1\r\n'
                        b'X-Admin-Script-Status: ok\r\n'
                        b'Content-Length: %d\r\n\r\n' % len(body) + body)
            reply = self._read_http(tls)
            self.assertIn(b'HTTP/1.1 204 No Content', reply)
            self.assertEqual(server._SCP81_SCRIPT_RESULTS[0]['sw'], '9000')
            tls.close()
        finally:
            server._BIP = old_bip
            reset_script()
            server._SCP81_SCRIPT_NEXT = 0
            server._SCP81_SCRIPT_RESULTS = []
            srv.stop()

    def test_200_keeps_the_connection_for_the_next_post(self):
        # The card is the HTTP client and may reuse the connection for its
        # next POST (GP Am. B 4.3.1: connection management is the SD's job);
        # the server never closes between requests.
        def responder(method, target, headers, body):
            return 200, {'X-Admin-Protocol': scp81.GP_PROTOCOL}, b'\x80\x01\x00'

        srv = scp81.PskTlsServer('127.0.0.1', 0, PSK, responder=responder)
        try:
            tls = self._connect(srv)
            tls.sendall(b'POST /api/scp81?req=1 HTTP/1.1\r\n\r\n')
            reply = self._read_http(tls)
            self.assertTrue(reply.startswith(b'HTTP/1.1 200 OK'))
            # same TLS session, second request
            tls.sendall(b'POST /api/scp81?req=2 HTTP/1.1\r\n\r\n')
            reply = self._read_http(tls)
            self.assertTrue(reply.startswith(b'HTTP/1.1 200 OK'))
            tls.close()
        finally:
            srv.stop()

    def test_session_end_closes_with_close_notify(self):
        # Only the 204 ends the dialog; the server shuts the TLS session down
        # cleanly (close_notify while the response is still buffered) and
        # then closes the socket.
        def responder(method, target, headers, body):
            return 204, {'X-Admin-Protocol': scp81.GP_PROTOCOL}, b''

        srv = scp81.PskTlsServer('127.0.0.1', 0, PSK, responder=responder)
        try:
            tls = self._connect(srv)
            tls.sendall(b'POST /api/scp81 HTTP/1.1\r\n\r\n')
            reply = self._read_http(tls)
            self.assertIn(b'HTTP/1.1 204 No Content', reply)
            # answer the server's close_notify: a mutual clean shutdown means
            # unwrap() completes instead of timing out
            tls.settimeout(3.0)
            plain = tls.unwrap()
            plain.settimeout(3.0)
            self.assertEqual(plain.recv(64), b'')
            plain.close()
        finally:
            srv.stop()

    def test_wrong_identity_rejected(self):
        srv = scp81.PskTlsServer('127.0.0.1', 0, PSK, identity=IDENT)
        try:
            with self.assertRaises(ssl.SSLError):
                self._connect(srv, self._client_ctx(identity='unknown-id'))
        finally:
            srv.stop()

    def test_psk_map_selects_key_by_identity(self):
        psk2 = bytes.fromhex('ffeeddccbbaa99887766554433221100')
        logs = []
        srv = scp81.PskTlsServer('127.0.0.1', 0, psk_map={IDENT: PSK, 'id-2': psk2},
                                 on_log=lambda k, **f: logs.append((k, f)))
        try:
            tls = self._connect(srv, self._client_ctx())
            self.assertEqual(tls.version(), 'TLSv1.2')
            tls.close()
            deadline = time.time() + 3
            while time.time() < deadline and srv.identity_seen is None:
                time.sleep(0.05)
            self.assertEqual(srv.identity_seen, IDENT)
            self.assertIs(srv.identity_matched, True)
            # a second identity in the table uses its own key
            tls = self._connect(srv, self._client_ctx(identity='id-2', psk=psk2))
            tls.close()
            # an unlisted identity is rejected and logged
            with self.assertRaises(ssl.SSLError):
                self._connect(srv, self._client_ctx(identity='unknown-id'))
            self.assertEqual(srv.identity_seen, 'unknown-id')
            self.assertIs(srv.identity_matched, False)
            self.assertIn('tls-psk-unknown', [k for k, _ in logs])
            self.assertEqual(srv.psk_identities, [IDENT, 'id-2'])
            hs = [f for k, f in logs if k == 'tls-handshake'][0]
            self.assertTrue(hs['psk_match'])
        finally:
            srv.stop()

    def test_psk_map_listed_identity_with_wrong_key_fails(self):
        srv = scp81.PskTlsServer('127.0.0.1', 0, psk_map={IDENT: PSK})
        try:
            with self.assertRaises(ssl.SSLError):
                self._connect(srv, self._client_ctx(psk=bytes(16)))
            self.assertIs(srv.identity_matched, True)
        finally:
            srv.stop()

    def test_set_psk_map_swaps_keys(self):
        srv = scp81.PskTlsServer('127.0.0.1', 0, psk_map={IDENT: PSK})
        try:
            new_psk = bytes.fromhex('ffeeddccbbaa99887766554433221100')
            self.assertEqual(srv.set_psk_map({'id-9': new_psk}), ['id-9'])
            tls = self._connect(srv, self._client_ctx(identity='id-9', psk=new_psk))
            tls.close()
            with self.assertRaises(ssl.SSLError):
                self._connect(srv)
        finally:
            srv.stop()

    def test_wildcard_key_accepts_any_identity(self):
        srv = scp81.PskTlsServer('127.0.0.1', 0, PSK)
        try:
            tls = self._connect(srv, self._client_ctx(identity='whoever'))
            tls.close()
            deadline = time.time() + 3
            while time.time() < deadline and srv.identity_seen is None:
                time.sleep(0.05)
            self.assertEqual(srv.identity_seen, 'whoever')
            self.assertIs(srv.identity_matched, True)
            self.assertEqual(srv.psk_identities, [])
        finally:
            srv.stop()

    def test_norm_identity(self):
        self.assertIsNone(scp81._norm_identity(None))
        self.assertEqual(scp81._norm_identity(b'abc'), 'abc')
        self.assertEqual(scp81._norm_identity('abc'), 'abc')

    def test_server_hello_omits_encrypt_then_mac(self):
        # The live card offers encrypt_then_mac but aborts with
        # SSLV3_ALERT_UNEXPECTED_MESSAGE when the server echoes it.
        srv = scp81.PskTlsServer('127.0.0.1', 0, PSK)
        try:
            sock = socket.create_connection(('127.0.0.1', srv.port), timeout=5)
            hello = bytes.fromhex(
                '1603010038' '01000034' '0303' + '11' * 32 + '00'
                '0002' '00ae' '0100'
                '0009' '0001000101' '00160000')
            sock.sendall(hello)
            data = sock.recv(4096)
            sock.close()
            idx = data.find(b'\x00\xae')
            self.assertGreater(idx, 0, data.hex())
            ext_total = int.from_bytes(data[idx + 3:idx + 5], 'big')
            ext = data[idx + 5:idx + 5 + ext_total]
            seen = set()
            off = 0
            while off + 4 <= len(ext):
                etype = int.from_bytes(ext[off:off + 2], 'big')
                elen = int.from_bytes(ext[off + 2:off + 4], 'big')
                seen.add(etype)
                off += 4 + elen
            self.assertNotIn(0x0016, seen)   # encrypt_then_mac
            self.assertNotIn(0x0023, seen)   # session_ticket (no resumption)
        finally:
            srv.stop()

    def test_command_then_close(self):
        def responder(method, target, headers, body):
            if b'cmd=1' in target.encode():
                return (200, {'X-Admin-Protocol': scp81.GP_PROTOCOL,
                              'X-Admin-Next-URI': '/server/adminagent?cmd=2',
                              'Content-Type': scp81.GP_CT_COMMAND}, b'\x80\x01\x00')
            return 204, {'X-Admin-Protocol': scp81.GP_PROTOCOL}, b''

        srv = scp81.PskTlsServer('127.0.0.1', 0, PSK, responder=responder)
        try:
            tls = self._connect(srv)
            tls.sendall(b'POST /server/adminagent?cmd=1 HTTP/1.1\r\n\r\n')
            reply = self._read_http(tls)
            self.assertIn(b'X-Admin-Next-URI: /server/adminagent?cmd=2', reply)
            head, _, body = reply.partition(b'\r\n\r\n')
            self.assertIn(b'Content-Length: 3', head)
            self.assertEqual(body[:3], b'\x80\x01\x00')
            tls.sendall(b'POST /server/adminagent?cmd=2 HTTP/1.1\r\n'
                        b'X-Admin-Script-Status: ok\r\n'
                        b'Content-Length: 3\r\n\r\n\x80\x02\x00')
            reply = self._read_http(tls)
            self.assertTrue(reply.startswith(b'HTTP/1.1 204 No Content'))
            tls.close()
        finally:
            srv.stop()


class ScriptResponderTest(unittest.TestCase):
    """RAM over HTTP command scripting (TS 102 226 5.2, GP 4.4.2)."""

    def setUp(self):
        reset_script()
        server._SCP81_SCRIPT_RESULTS = []

    def tearDown(self):
        reset_script()
        server._SCP81_SCRIPT_RESULTS = []

    def test_command_body_is_indefinite_scripting_template(self):
        # Reference admin server: AE 80 22 <len> <apdu> 00 00
        body = server._scp81_command_body('80CAFF2100')
        self.assertEqual(body.hex(), 'ae80220580caff210000' + '00')
        self.assertEqual(body[0:2], b'\xae\x80')
        self.assertEqual(body[2], 0x22)
        self.assertEqual(body[3], 5)

    def test_command_body_definite_scripting_template(self):
        # AA <len> 22 <len> <apdu>
        body = server._scp81_command_body('80CAFF2100', definite=True)
        self.assertEqual(body.hex(), 'aa072205' + '80caff2100')

    def test_command_body_cr_set_c_apdu_tag(self):
        # Some cards use the comprehension-required tag variant (A2)
        self.assertEqual(server._scp81_command_body('80CA004500', definite=True, cr_tag=True).hex(),
                         'aa07a20580ca004500')
        self.assertEqual(server._scp81_command_body('80CA004500', cr_tag=True).hex(),
                         'ae80a20580ca004500' + '0000')

    def test_parse_response_indefinite(self):
        # AF 80 (80 01 01) (23 04 93 05 90 00) 00 00
        body = bytes.fromhex('af80' '800101' '2304' '9305' '9000' '0000')
        count, rapdus = server._scp81_parse_response(body)
        self.assertEqual(count, 1)
        self.assertEqual(rapdus, [(bytes.fromhex('9305'), '9000')])

    def test_parse_response_definite(self):
        body = bytes.fromhex('ab08' '800101' '2303' '019000')
        count, rapdus = server._scp81_parse_response(body)
        self.assertEqual(count, 1)
        self.assertEqual(rapdus, [(bytes.fromhex('01'), '9000')])

    def test_decode_memory(self):
        rapdu = bytes.fromhex('ff210c' '810102' '8203' '00f0a0' '8302' '0800')
        self.assertEqual(server._scp81_decode_memory(rapdu),
                         {'applets': 2, 'free_nv': 0xf0a0, 'free_volatile': 0x0800})
        self.assertIsNone(server._scp81_decode_memory(bytes.fromhex('9000')))

    def test_responder_sends_script_then_204(self):
        # Use an explicit two-command script, independent of the presets.
        reset_script(['80CAFF2100', '80F22002024F0000'])
        logs = []
        old_bip = server._BIP
        server._BIP = mock.Mock()
        server._BIP.log = lambda kind, **f: logs.append((kind, f))
        try:
            # First POST: no script status -> first APDU + Next-URI
            status, headers, body = server._scp81_script_responder(
                'POST', '/api/scp81', {}, b'')
            self.assertEqual(status, 200)
            self.assertEqual(headers['X-Admin-Next-URI'], '/api/scp81?req=1')
            self.assertEqual(headers['Content-Type'],
                             'application/vnd.globalplatform.card-content-mgt;version=1.0')
            self.assertEqual(body.hex(), 'ae80220580caff21000000')
            # Response to it -> second APDU
            status, headers, body = server._scp81_script_responder(
                'POST', '/api/scp81?step=1', {'x-admin-script-status': 'ok'},
                bytes.fromhex('af80' '800101' '2304' '93059000' '0000'))
            self.assertEqual(status, 200)
            self.assertEqual(body.hex(), 'ae802208' + '80f22002024f0000' + '0000')
            # Last response -> session closed with 204
            status, headers, body = server._scp81_script_responder(
                'POST', '/api/scp81?step=2', {'x-admin-script-status': 'ok'},
                bytes.fromhex('af80' '800101' '2303' '5f9000' '0000'))
            self.assertEqual(status, 204)
            kinds = [k for k, _ in logs]
            self.assertIn('script-send', kinds)
            self.assertIn('script-rapdu', kinds)
            self.assertIn('script-done', kinds)
            self.assertEqual(len(server._SCP81_SCRIPT_RESULTS), 2)
        finally:
            server._BIP = old_bip

    def test_responder_reports_script_failure(self):
        old_bip = server._BIP
        server._BIP = mock.Mock()
        logs = []
        server._BIP.log = lambda kind, **f: logs.append((kind, f))
        try:
            server._scp81_script_responder('POST', '/x', {}, b'')
            status, headers, body = server._scp81_script_responder(
                'POST', '/x', {'x-admin-script-status': 'security-error'}, b'')
            self.assertEqual(status, 200)   # script continues with the next APDU
            self.assertIn(('script-status', {'index': 1, 'status': 'security-error'}), logs)
        finally:
            server._BIP = old_bip

    def test_resumed_dialog_resends_unreported_apdu(self):
        # The card never reported APDU 1 (the session died): a resumed dialog
        # resends it instead of skipping to the next one.
        reset_script(['80CAFF2100', '80F22002024F0000'])
        try:
            status, headers, body = server._scp81_script_responder(
                'POST', '/x', {}, b'')
            self.assertEqual(body.hex(), 'ae80220580caff21000000')
            status, headers, body = server._scp81_script_responder(
                'POST', '/x', {'x-admin-resume': 'true'}, b'')
            self.assertEqual(status, 200)
            self.assertEqual(body.hex(), 'ae80220580caff21000000')
            self.assertEqual(server._SCP81_SCRIPT_PENDING['pos'], 0)
            self.assertNotIn(0, server._SCP81_SCRIPT_DONE)
        finally:
            reset_script()

    def test_resumed_dialog_sends_leftover_tail(self):
        # APDU 1 was reported; the resumed dialog continues with APDU 2 only.
        reset_script(['80CAFF2100', '80F22002024F0000'])
        try:
            server._scp81_script_responder('POST', '/x', {}, b'')
            status, headers, body = server._scp81_script_responder(
                'POST', '/x', {'x-admin-script-status': 'ok'},
                bytes.fromhex('af80' '800101' '2304' '93059000' '0000'))
            self.assertIn(bytes.fromhex('80f22002024f0000'), body)
            self.assertEqual(server._SCP81_SCRIPT_DONE, {0})
            # The session dies before APDU 2 is reported; resume resends it.
            status, headers, body = server._scp81_script_responder(
                'POST', '/x', {'x-admin-resume': 'true'}, b'')
            self.assertIn(bytes.fromhex('80f22002024f0000'), body)
            self.assertNotIn(bytes.fromhex('80caff2100'), body)
        finally:
            reset_script()

    def test_fresh_dialog_restarts_completed_script(self):
        reset_script(['80CAFF2100'])
        try:
            server._scp81_script_responder('POST', '/x', {}, b'')
            status, headers, body = server._scp81_script_responder(
                'POST', '/x', {'x-admin-script-status': 'ok'},
                bytes.fromhex('af80' '800101' '2304' '93059000' '0000'))
            self.assertEqual(status, 204)
            # A stale resumed dialog stays closed ...
            status, headers, body = server._scp81_script_responder(
                'POST', '/x', {'x-admin-resume': 'true'}, b'')
            self.assertEqual(status, 204)
            # ... while a fresh dialog (new trigger) runs the script again.
            status, headers, body = server._scp81_script_responder(
                'POST', '/x', {}, b'')
            self.assertEqual(status, 200)
            self.assertIn(bytes.fromhex('80caff2100'), body)
        finally:
            reset_script()

    def test_script_status_error_consumes_the_apdu(self):
        # A reported failure still counts as processed: the run moves on and a
        # resumed dialog resends only the unreported tail.
        reset_script(['80CAFF2100', '80F22002024F0000'])
        try:
            server._scp81_script_responder('POST', '/x', {}, b'')
            server._scp81_script_responder(
                'POST', '/x', {'x-admin-script-status': 'security-error'}, b'')
            self.assertEqual(server._SCP81_SCRIPT_DONE, {0})
            self.assertEqual(server._SCP81_SCRIPT_PENDING['pos'], 1)
            server._scp81_script_responder(
                'POST', '/x', {'x-admin-resume': 'true'}, b'')
            self.assertEqual(server._SCP81_SCRIPT_PENDING['pos'], 1)
        finally:
            reset_script()


class ScriptStateTest(unittest.TestCase):
    """`GET /api/scp81/script` state: script progress vs listing pages."""

    def test_state_progress_pages_and_completion(self):
        reset_script(['80CAFF2100', '80F22002024F0000'])
        page = bytes.fromhex('E3114F08A0000000030000009F70010FC50100')
        cafe = b'\xAF\x80' + bytes([0x23, len(page) + 2]) + page + b'\xCA\xFE' + b'\x00\x00'
        ok = bytes.fromhex('af80' '800101' '2304' '93059000' '0000')
        try:
            st = server._scp81_script_state()
            self.assertEqual(st['total'], 2)
            self.assertEqual(st['next'], 0)
            self.assertEqual(st['done'], [])
            self.assertIsNone(st['pending'])
            self.assertEqual(st['pages'], 0)
            self.assertFalse(st['complete'])
            # first POST sends script APDU 1: pending is an object now
            server._scp81_script_responder('POST', '/x', {}, b'')
            st = server._scp81_script_state()
            self.assertEqual(st['pending'], {'index': 1, 'pos': 0, 'page': False,
                                             'apdu': '80CAFF2100'})
            self.assertFalse(st['complete'])
            # report it -> done[0], APDU 2 sent
            server._scp81_script_responder(
                'POST', '/x', {'x-admin-script-status': 'ok'}, ok)
            st = server._scp81_script_state()
            self.assertEqual(st['done'], [0])
            self.assertEqual(st['pending']['pos'], 1)
            self.assertFalse(st['pending']['page'])
            self.assertFalse(st['complete'])
            # APDU 2 truncates the listing: done[1], a page is sent
            server._scp81_script_responder(
                'POST', '/x', {'x-admin-script-status': 'ok'}, cafe)
            st = server._scp81_script_state()
            self.assertEqual(st['done'], [0, 1])
            self.assertEqual(st['pages'], 1)
            self.assertTrue(st['pending']['page'])
            self.assertIsNone(st['pending']['pos'])
            self.assertEqual(st['pending']['apdu'], '80F22003024F0000')
            self.assertFalse(st['complete'])
            # the page is reported: nothing left -> 204, state complete
            status, headers, body = server._scp81_script_responder(
                'POST', '/x', {'x-admin-script-status': 'ok'}, ok)
            self.assertEqual(status, 204)
            st = server._scp81_script_state()
            self.assertIsNone(st['pending'])
            self.assertEqual(st['pages_queued'], 0)
            self.assertTrue(st['complete'])
        finally:
            reset_script()


class BipControlTest(unittest.TestCase):
    def tearDown(self):
        server._scp81_bip_control({'action': 'stop'})
        server._SCP81_PSKS = {}
        server._SCP81_PSK_LEGACY = None

    def test_tls_mode_requires_psk(self):
        server._SCP81_PSKS = {}
        server._SCP81_PSK_LEGACY = None
        resp = server._scp81_bip_control({'action': 'start', 'mode': 'tls'})
        self.assertFalse(resp['ok'])
        self.assertIn('psk_map', resp['error'])
        self.assertIn('psk_hex', resp['error'])

    def test_tls_mode_starts_and_reports_status(self):
        resp = server._scp81_bip_control({'action': 'start', 'mode': 'tls',
                                          'host': '127.0.0.1', 'port': 0,
                                          'psk_hex': '0011 2233', 'psk_identity': 'id-1'})
        self.assertTrue(resp['ok'], resp)
        listener = resp['listener']
        self.assertEqual(listener['mode'], 'tls')
        self.assertEqual(listener['psk_identities'], ['id-1'])
        self.assertFalse(listener['psk_wildcard'])
        self.assertIsNone(listener['identity_seen'])
        self.assertTrue(resp['bip']['enabled'])
        # the key never leaves the server
        self.assertNotIn('psk_hex', listener)

    def test_tls_defaults_and_framing_options(self):
        try:
            resp = server._scp81_bip_control({
                'action': 'start', 'mode': 'tls', 'host': '127.0.0.1', 'port': 0,
                'psk_hex': '00112233', 'psk_identity': 'id-1',
                'chunked': False, 'chunk_size': 100,
                'compact_headers': True, 'conn_header': 'close', 'next_uri': '',
                'script_template': 'definite', 'cr_tag': True,
                'targeted_app': '//aid/A000000151000000', 'link_events': False,
            })
            self.assertTrue(resp['ok'], resp)
            listener = resp['listener']
            # TLS is automatic (no version/cipher setting in the PWA)
            self.assertEqual(listener['tls_version'], 'auto')
            self.assertIn('version_seen', listener)
            self.assertIn('cipher_seen', listener)
            self.assertEqual((listener['chunked'], listener['chunk_size'],
                              listener['compact_headers']),
                             (False, 100, True))
            self.assertEqual(server._SCP81_SCRIPT_TEMPLATE, 'definite')
            self.assertTrue(server._SCP81_SCRIPT_CR_TAG)
            self.assertEqual(server._SCP81_TARGETED_APP, '//aid/A000000151000000')
            self.assertEqual(server._SCP81_NEXT_URI, '')
            self.assertFalse(server._BIP_LINK_EVENTS)
            self.assertEqual(resp['script_template'], 'definite')
            self.assertTrue(resp['cr_tag'])
            self.assertFalse(resp['link_events'])
            self.assertNotIn('apache_headers', resp)
        finally:
            server._SCP81_SCRIPT_TEMPLATE = 'indefinite'
            server._SCP81_SCRIPT_CR_TAG = False
            server._SCP81_TARGETED_APP = None
            server._SCP81_NEXT_URI = None
            server._BIP_LINK_EVENTS = True

    def test_link_events_apply_to_every_mode(self):
        # TS 102 223 7.5.11 events are a BIP-layer feature, not a TLS option.
        resp = server._scp81_bip_control({'action': 'start', 'mode': 'redirect',
                                          'host': '10.11.12.13', 'port': 10174,
                                          'link_events': False})
        self.assertTrue(resp['ok'], resp)
        self.assertFalse(server._BIP_LINK_EVENTS)
        resp = server._scp81_bip_control({'action': 'start', 'mode': 'passthru',
                                          'link_events': True})
        self.assertTrue(resp['ok'], resp)
        self.assertTrue(server._BIP_LINK_EVENTS)

    def test_tls_handshake_failure_is_logged(self):
        resp = server._scp81_bip_control({'action': 'start', 'mode': 'tls',
                                          'host': '127.0.0.1', 'port': 0,
                                          'psk_hex': '00112233', 'psk_identity': 'id-1'})
        self.assertTrue(resp['ok'], resp)
        port = resp['listener']['port']
        sock = socket.create_connection(('127.0.0.1', port), timeout=2)
        try:
            sock.sendall(b'this is not a tls hello')
            sock.settimeout(2)
            try:
                sock.recv(64)
            except OSError:
                pass
        finally:
            sock.close()
        kinds = []
        for _ in range(40):
            kinds = [e['kind'] for e in server._BIP.entries_after(0)]
            if 'tls-handshake-failed' in kinds:
                break
            time.sleep(0.05)
        self.assertIn('tls-handshake-failed', kinds)

    def test_psk_map_start_and_update(self):
        server._SCP81_PSKS = {}
        server._SCP81_PSK_LEGACY = None
        resp = server._scp81_bip_control({
            'action': 'start', 'mode': 'tls', 'host': '127.0.0.1', 'port': 0,
            'psk_map': [{'identity': 'id-1', 'psk_hex': '00112233'},
                        {'identity': 'id-2', 'psk_hex': '44556677'},
                        {'identity': '', 'psk_hex': '99'},        # skipped
                        {'identity': 'id-3', 'psk_hex': ''}]})   # skipped
        self.assertTrue(resp['ok'], resp)
        self.assertEqual(resp['listener']['psk_identities'], ['id-1', 'id-2'])
        # a preset edit pushes the new table without a listener restart
        upd = server._scp81_update_psk_map(
            {'psk_map': [{'identity': 'id-9', 'psk_hex': 'aabbccdd'}]})
        self.assertTrue(upd['ok'], upd)
        self.assertEqual(upd['identities'], ['id-9'])
        self.assertEqual(upd['listener']['psk_identities'], ['id-9'])

    def test_psk_map_requires_a_listener_for_update(self):
        server._scp81_bip_control({'action': 'stop'})
        resp = server._scp81_update_psk_map(
            {'psk_map': [{'identity': 'id-1', 'psk_hex': '00112233'}]})
        self.assertFalse(resp['ok'])
        self.assertIn('not running', resp['error'])

    def test_psk_map_empty_entries_rejected(self):
        server._SCP81_PSKS = {}
        server._SCP81_PSK_LEGACY = None
        resp = server._scp81_bip_control({
            'action': 'start', 'mode': 'tls', 'host': '127.0.0.1', 'port': 0,
            'psk_map': [{'identity': '', 'psk_hex': '00112233'}]})
        self.assertFalse(resp['ok'])
        self.assertIn('no usable entries', resp['error'])

    def test_psk_request_redaction(self):
        body = {'action': 'start',
                'psk_hex': '00112233445566778899aabbccddeeff',
                'psk_map': [{'identity': 'id-1', 'psk_hex': '00112233'},
                            {'identity': ''}]}
        red = server._redact_psk_fields(body)
        self.assertEqual(red['psk_hex'], '<redacted>')
        self.assertEqual(red['psk_map'][0]['psk_hex'], '<redacted>')
        self.assertEqual(red['psk_map'][0]['identity'], 'id-1')
        self.assertEqual(red['psk_map'][1], {'identity': ''})
        # the original body is untouched
        self.assertEqual(body['psk_map'][0]['psk_hex'], '00112233')

    def test_unknown_mode_rejected(self):
        resp = server._scp81_bip_control({'action': 'start', 'mode': 'nope'})
        self.assertFalse(resp['ok'])
        self.assertIn('unsupported mode', resp['error'])

    def test_redirect_mode_targets_the_external_server(self):
        resp = server._scp81_bip_control({'action': 'start', 'mode': 'redirect',
                                          'host': '10.11.12.13', 'port': 10174})
        self.assertTrue(resp['ok'], resp)
        self.assertIsNone(server._BIP_LISTENER)       # no local listener
        self.assertEqual(resp['listener']['mode'], 'redirect')
        self.assertEqual(resp['listener']['host'], '10.11.12.13')
        self.assertEqual(resp['listener']['port'], 10174)
        self.assertEqual(resp['listener']['target'], '10.11.12.13:10174')
        self.assertTrue(resp['bip']['enabled'])
        self.assertEqual(server._BIP.target, ('10.11.12.13', 10174))
        # the status endpoint sees the redirect mode while it runs ...
        self.assertEqual(server._bip_listener_status()['mode'], 'redirect')
        # ... and stopping clears it (no stale listener in the status)
        server._scp81_bip_control({'action': 'stop'})
        self.assertIsNone(server._bip_listener_status())
        self.assertFalse(server._BIP.enabled)

    def test_redirect_mode_requires_an_explicit_target(self):
        # No defaults for a remote platform: the target must be configured.
        resp = server._scp81_bip_control({'action': 'start', 'mode': 'redirect'})
        self.assertFalse(resp['ok'])
        self.assertIn('host and port', resp['error'])
        resp = server._scp81_bip_control({'action': 'start', 'mode': 'redirect',
                                          'host': '10.0.0.1'})
        self.assertFalse(resp['ok'])
        resp = server._scp81_bip_control({'action': 'start', 'mode': 'redirect',
                                          'port': 1234})
        self.assertFalse(resp['ok'])
        self.assertIsNone(server._BIP_LISTENER)
        self.assertFalse(server._BIP.enabled)

    def test_passthru_mode_needs_no_target(self):
        # Passthru: no listener and no pinned target - every BIP channel dials
        # the destination the card requests in OPEN CHANNEL.
        resp = server._scp81_bip_control({'action': 'start', 'mode': 'passthru'})
        self.assertTrue(resp['ok'], resp)
        self.assertIsNone(server._BIP_LISTENER)
        self.assertEqual(resp['listener'], {'mode': 'passthru'})
        self.assertEqual(server._BIP.mode, 'passthru')
        self.assertIsNone(server._BIP.target)
        self.assertTrue(resp['bip']['enabled'])
        self.assertEqual(resp['bip']['mode'], 'passthru')
        self.assertEqual(server._bip_listener_status(), {'mode': 'passthru'})
        server._scp81_bip_control({'action': 'stop'})
        self.assertIsNone(server._bip_listener_status())
        self.assertFalse(server._BIP.enabled)

    def test_start_accepts_explicit_script_list(self):
        server._SCP81_PSKS = {}
        server._SCP81_PSK_LEGACY = None
        resp = server._scp81_bip_control({
            'action': 'start', 'mode': 'tls', 'host': '127.0.0.1', 'port': 0,
            'psk_map': [{'identity': 'id-1', 'psk_hex': '00112233'}],
            'script': ['80CAFF2100'], 'script_kind': 'Explore'})
        self.assertTrue(resp['ok'], resp)
        self.assertEqual(resp['script'], ['80CAFF2100'])
        self.assertEqual(resp['script_kind'], 'Explore')
        self.assertEqual(server._SCP81_SCRIPT_BASE, ['80CAFF2100'])
        self.assertEqual(server._SCP81_SCRIPT_NEXT, 0)

    def test_start_rejects_named_script_presets(self):
        # Scripts live in the PWA now: the server only takes an explicit list.
        server._SCP81_PSKS = {}
        server._SCP81_PSK_LEGACY = None
        resp = server._scp81_bip_control({
            'action': 'start', 'mode': 'tls', 'host': '127.0.0.1', 'port': 0,
            'psk_hex': '00112233', 'script': 'explore'})
        self.assertFalse(resp['ok'])
        self.assertIn('unknown script preset', resp['error'])


class DataAvailableTest(unittest.TestCase):
    def _channel(self, cid=1, rx=b'\x16\x03\x03'):
        ch = types.SimpleNamespace(id=cid, rx=bytearray(rx), peer_closed=False)
        return ch

    def _state(self, event_list=(0x09,)):
        sent = []

        class Tp:
            def send_apdu(self, apdu):
                sent.append(apdu)
                return '', '9000'

        scc = types.SimpleNamespace(cat_cla='80', _tp=Tp())
        ref = types.SimpleNamespace(scc=scc, event_list=list(event_list),
                                    stk_pending=None)
        return ref, sent

    def test_data_available_event(self):
        ref, sent = self._state()
        with mock.patch.object(server, '_server_ref', ref):
            with mock.patch.object(server, '_CARD_CONNECTED', True):
                ok = server._bip_data_available(self._channel())
        self.assertTrue(ok)
        # ENVELOPE(Event Download - Data available): channel 1 established,
        # 3 bytes waiting (B8 status + B7 length)
        self.assertEqual(sent, ['80c2000010d60e99010982028281b8028100b70103'])

    def test_skipped_without_subscription(self):
        ref, sent = self._state(event_list=[0x03, 0x0A])
        with mock.patch.object(server, '_server_ref', ref):
            with mock.patch.object(server, '_CARD_CONNECTED', True):
                ok = server._bip_data_available(self._channel())
        self.assertFalse(ok)
        self.assertEqual(sent, [])

    def test_skipped_while_menu_pending(self):
        ref, sent = self._state()
        ref.stk_pending = {'type': 'select_item'}
        with mock.patch.object(server, '_server_ref', ref):
            with mock.patch.object(server, '_CARD_CONNECTED', True):
                ok = server._bip_data_available(self._channel())
        self.assertFalse(ok)
        self.assertEqual(sent, [])

    def test_monitor_notifies_once_per_arrival(self):
        peer = socket.socket()
        peer.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        peer.bind(('127.0.0.1', 0))
        peer.listen(1)
        bip = server.httpota.BipTerminal()
        seen = []
        bip.on_data = lambda ch: (seen.append((ch.id, len(ch.rx)))) or True
        try:
            bip.enable('127.0.0.1', peer.getsockname()[1])
            cid, err = bip.open('10.0.0.1', 1, 512)
            self.assertIsNone(err)
            conn, _ = peer.accept()
            conn.sendall(b'HELLO')
            deadline = time.time() + 3
            while time.time() < deadline and not seen:
                time.sleep(0.05)
            time.sleep(0.6)  # several monitor ticks
            self.assertEqual(seen, [(cid, 5)])
        finally:
            bip.disable()
            peer.close()

    def test_remaining_bytes_are_re_announced(self):
        # The live card waits for a fresh Data available event for the bytes
        # left after a partial RECEIVE DATA (announced in the TR length TLV).
        peer = socket.socket()
        peer.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        peer.bind(('127.0.0.1', 0))
        peer.listen(1)
        bip = server.httpota.BipTerminal()
        seen = []
        bip.on_data = lambda ch: (seen.append(len(ch.rx))) or True
        try:
            bip.enable('127.0.0.1', peer.getsockname()[1])
            cid, err = bip.open('10.0.0.1', 1, 512)
            self.assertIsNone(err)
            conn, _ = peer.accept()
            conn.sendall(b'0123456789')
            deadline = time.time() + 3
            while time.time() < deadline and not seen:
                time.sleep(0.05)
            self.assertEqual(seen, [10])
            self.assertEqual(bip.receive(cid, 4), b'0123')
            deadline = time.time() + 3
            while time.time() < deadline and len(seen) < 2:
                time.sleep(0.05)
            self.assertEqual(seen[1], 6)
        finally:
            bip.disable()
            peer.close()


if __name__ == '__main__':
    unittest.main()

class KeylogTest(unittest.TestCase):
    def test_keylog_filename_set(self):
        import tempfile, os
        fd, path = tempfile.mkstemp(prefix='scp81keys')
        os.close(fd)
        srv = scp81.PskTlsServer('127.0.0.1', 0, PSK, keylog=path)
        try:
            self.assertEqual(srv.ctx.keylog_filename, path)
        finally:
            srv.stop()
            os.unlink(path)

class ConnHeaderTest(unittest.TestCase):
    def test_conn_header_none_omits_connection(self):
        import types
        seen = {}
        def responder(method, target, headers, body):
            return 204, {}, b''
        srv = scp81.PskTlsServer('127.0.0.1', 0, PSK, responder=responder,
                                 conn_header='none')
        try:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            ctx.set_ciphers('PSK')
            ctx.set_psk_client_callback(lambda hint: (b'id', PSK))
            raw = socket.create_connection(('127.0.0.1', srv.port), timeout=5)
            tls = ctx.wrap_socket(raw, server_hostname='x')
            tls.sendall(b'POST /api/scp81 HTTP/1.1\r\n\r\n')
            data = tls.recv(4096)
            self.assertNotIn(b'Connection:', data)
            tls.close()
        finally:
            srv.stop()


class TargetedAppTest(unittest.TestCase):
    def test_targeted_app_header(self):
        reset_script(['80CAFF2100'])
        server._SCP81_SCRIPT_NEXT = 0
        server._SCP81_TARGETED_APP = '//aid/A000000151000000'
        try:
            status, headers, body = server._scp81_script_responder(
                'POST', '/api/scp81', {}, b'')
            self.assertEqual(status, 200)
            self.assertEqual(headers['X-Admin-Targeted-Application'],
                             '//aid/A000000151000000')
        finally:
            server._SCP81_TARGETED_APP = None
            reset_script()
            server._SCP81_SCRIPT_NEXT = 0

    def test_response_headers_are_minimal(self):
        # No Date/Server/X-Powered-By mimicry (dropped 2.2.14 - the reference
        # server's extra headers earned nothing); only the dialog headers.
        reset_script(['80CAFF2100'])
        server._SCP81_SCRIPT_NEXT = 0
        server._SCP81_CHUNKED = False
        try:
            status, headers, body = server._scp81_script_responder(
                'POST', '/api/scp81', {}, b'')
            self.assertEqual(list(headers),
                             ['X-Admin-Protocol', 'X-Admin-Next-URI', 'Content-Type'])
            self.assertTrue(headers['X-Admin-Next-URI'].startswith('/api/scp81?req='))
            out = scp81.build_http_response(status, 'OK', headers, body)
            self.assertIn(b'Content-Length', out)
            for gone in (b'Date:', b'Server:', b'X-Powered-By'):
                self.assertNotIn(gone, out)
        finally:
            server._SCP81_CHUNKED = False
            reset_script()
            server._SCP81_SCRIPT_NEXT = 0

    def test_session_end_204_has_only_the_admin_header(self):
        reset_script([])
        server._SCP81_SCRIPT_NEXT = 0
        try:
            status, headers, body = server._scp81_script_responder(
                'POST', '/api/scp81', {}, b'')
            self.assertEqual(status, 204)
            self.assertEqual(list(headers), ['X-Admin-Protocol'])
            self.assertEqual(body, b'')
        finally:
            reset_script()
            server._SCP81_SCRIPT_NEXT = 0

    def test_chunked_response_has_no_content_length(self):
        # A chunked response must not carry Content-Length (invalid HTTP - and
        # the card rejects it); the Transfer-Encoding header is emitted by the
        # HTTP builder, exactly once.
        reset_script(['80CAFF2100'])
        server._SCP81_SCRIPT_NEXT = 0
        server._SCP81_CHUNKED = True
        try:
            status, headers, body = server._scp81_script_responder(
                'POST', '/api/scp81', {}, b'')
            self.assertNotIn('Content-Length', headers)
            out = scp81.build_http_response(status, 'OK', headers, body,
                                            chunked=True, connection=None)
            self.assertNotIn(b'Content-Length', out)
            self.assertEqual(out.count(b'Transfer-Encoding: chunked'), 1)
        finally:
            server._SCP81_CHUNKED = False
            reset_script()
            server._SCP81_SCRIPT_NEXT = 0

    def test_continuation_sets_p2_next_bit(self):
        # P2.b1: 0 = first/all, 1 = next batch of the SAME search criteria
        self.assertEqual(server._scp81_continuation('80F24002024F0000'),
                         '80F24003024F0000')
        self.assertEqual(server._scp81_continuation('80F21002024F0000'),
                         '80F21003024F0000')
        self.assertIsNone(server._scp81_continuation('80CAFF2100'))

    def test_cafe_page_auto_continuation(self):
        reset_script(['80F24002024F0000'])
        server._SCP81_SCRIPT_NEXT = 1
        server._SCP81_SCRIPT_PENDING = {'index': 1, 'pos': 0, 'page': False,
                                        'apdu': '80F24002024F0000'}
        try:
            page = bytes.fromhex('E3114F08A0000000030000009F70010FC50100')
            tlv = bytes([0x23, len(page) + 2]) + page + b'\xCA\xFE'
            body = b'\xAF\x80' + tlv + b'\x00\x00'
            status, headers, out = server._scp81_script_responder(
                'POST', '/api/scp81?req=1', {'x-admin-script-status': 'ok'}, body)
            # The continuation was queued and sent as the next command.
            self.assertEqual(server._SCP81_SCRIPT_BASE, ['80F24002024F0000'])
            self.assertEqual(server._SCP81_SCRIPT_PENDING['apdu'],
                             '80F24003024F0000')
            self.assertEqual(status, 200)
            self.assertIn(bytes.fromhex('80F24003024F0000'), out)
        finally:
            reset_script()

    def test_standard_more_data_sw_also_pages(self):
        # GP Table 11-38: SW '63 10' = more data available, continue with
        # GET STATUS [next occurrence] - same handling as the card's 'CA FE'.
        reset_script(['80F2 4002 024F 0000'.replace(' ', '')])
        server._SCP81_SCRIPT_NEXT = 1
        server._SCP81_SCRIPT_PENDING = {'index': 1, 'pos': 0, 'page': False,
                                        'apdu': '80F24002024F0000'}
        try:
            page = bytes.fromhex('E3114F08A0000000030000009F70010FC50100')
            tlv = bytes([0x23, len(page) + 2]) + page + b'\x63\x10'
            body = b'\xAF\x80' + tlv + b'\x00\x00'
            server._scp81_script_responder(
                'POST', '/api/scp81?req=1', {'x-admin-script-status': 'ok'}, body)
            self.assertEqual(server._SCP81_SCRIPT_PENDING['apdu'],
                             '80F24003024F0000')
        finally:
            reset_script()

    def test_repeated_pages_keep_paging(self):
        # The continuation is stateful (P2=03): the same APDU legitimately
        # repeats until the card answers 9000; the page counter caps it.
        reset_script(['80F24002024F0000'])
        server._SCP81_SCRIPT_NEXT = 1
        server._SCP81_SCRIPT_PENDING = {'index': 1, 'pos': 0, 'page': False,
                                        'apdu': '80F24002024F0000'}
        try:
            page = bytes.fromhex('E3114F08A0000000030000009F70010FC50100')
            tlv = bytes([0x23, len(page) + 2]) + page + b'\xCA\xFE'
            body = b'\xAF\x80' + tlv + b'\x00\x00'
            for _ in range(3):
                server._scp81_script_responder(
                    'POST', '/api/scp81?req=2', {'x-admin-script-status': 'ok'}, body)
            self.assertEqual(server._SCP81_PAGES, 3)
        finally:
            reset_script()

    def test_resumed_dialog_keeps_pages_fresh_dialog_resets(self):
        # The continuation pages belong to one run: a resumed dialog keeps
        # them, a fresh dialog starts the script over.
        reset_script(['80F24002024F0000'])
        server._SCP81_SCRIPT_NEXT = 1
        server._SCP81_SCRIPT_PENDING = {'index': 1, 'pos': 0, 'page': False,
                                        'apdu': '80F24002024F0000'}
        server._SCP81_SCRIPT_PAGE_QUEUE = ['80F24003024F0000']
        try:
            # Resumed dialog: the queued page is sent as the next command.
            server._scp81_script_responder(
                'POST', '/api/scp81', {'x-admin-resume': 'true'}, b'')
            self.assertEqual(server._SCP81_SCRIPT_PENDING['apdu'],
                             '80F24003024F0000')
            self.assertEqual(server._SCP81_SCRIPT_PENDING['page'], True)
            # Fresh dialog: the run restarts from the first APDU.
            status, headers, out = server._scp81_script_responder(
                'POST', '/api/scp81', {}, b'')
            self.assertIn(bytes.fromhex('80F24002024F0000'), out)
            self.assertEqual(server._SCP81_SCRIPT_PENDING['pos'], 0)
            self.assertEqual(server._SCP81_SCRIPT_PAGE_QUEUE, [])
        finally:
            reset_script()

    def test_exact_wire_bodies_from_reference_log(self):
        # De-chunked bodies captured in adminserver.log (2019-09-05).
        count, rapdus = server._scp81_parse_response(bytes.fromhex(
            'af802319e3154f08a0000001510000009f70010fc5039afe80ea0090000000'))
        self.assertEqual(len(rapdus), 1)
        self.assertEqual(rapdus[0][1], '9000')
        self.assertTrue(rapdus[0][0].startswith(b'\xe3\x15'))
        count, rapdus = server._scp81_parse_response(
            bytes.fromhex('af8023026a880000'))
        self.assertEqual(rapdus[0][1], '6A88')
        # A status-only POST (no body, e.g. unknown-application) parses empty.
        self.assertEqual(server._scp81_parse_response(b''), (0, []))


class QueueScriptTest(unittest.TestCase):
    def test_queue_replaces_and_resets(self):
        reset_script(['80CAFF2100'])
        server._SCP81_SCRIPT_NEXT = 1
        server._SCP81_SCRIPT_RESULTS = [{'index': 1, 'sw': '9000',
                                         'apdu': '80CAFF2100', 'rapdu': ''}]
        try:
            r = server._scp81_queue_script(['80E6020013' + '00' * 20,
                                            '80E88000' + '00' * 4],
                                           kind='ram-install')
            self.assertTrue(r['queued'])
            self.assertEqual(server._SCP81_SCRIPT_KIND, 'ram-install')
            self.assertEqual(server._SCP81_SCRIPT_NEXT, 0)
            self.assertEqual(server._SCP81_SCRIPT_RESULTS, [])
            self.assertEqual(len(server._SCP81_SCRIPT_BASE), 2)
        finally:
            reset_script()

    def test_queue_refuses_while_running(self):
        reset_script(['80CAFF2100', '80F28002024F0000'])
        server._SCP81_SCRIPT_NEXT = 1
        server._SCP81_SCRIPT_PENDING = {'index': 1, 'pos': 0, 'page': False,
                                        'apdu': '80CAFF2100'}
        try:
            r = server._scp81_queue_script(['80E60200'], kind='ram-install')
            self.assertFalse(r['queued'])
            self.assertEqual(r['next'], 1)
            self.assertTrue(r['of'])
            r = server._scp81_queue_script(['80E60200'], kind='ram-install', force=True)
            self.assertTrue(r['queued'])
            self.assertEqual(server._SCP81_SCRIPT_BASE, ['80E60200'])
        finally:
            reset_script()

    def test_queue_allowed_before_first_send(self):
        reset_script(['80CAFF2100'])
        try:
            r = server._scp81_queue_script(['80E60200'], kind='install')
            self.assertTrue(r['queued'])
            self.assertEqual(server._SCP81_SCRIPT_BASE, ['80E60200'])
        finally:
            reset_script()


class ScriptBodyLengthTest(unittest.TestCase):
    def test_long_c_apdu_uses_ber_long_form(self):
        from pysim_simple_server.server import _scp81_command_body
        apdu = '80E80000F0' + 'AB' * 239 + '00'      # exactly 245-byte LOAD
        body = _scp81_command_body(apdu)
        # AE 80 22 81 F5 <245 bytes> 00 00
        self.assertEqual(body[:5].hex().upper(), 'AE802281F5')
        self.assertEqual(body[-2:].hex().upper(), '0000')
        self.assertEqual(len(body), 5 + 245 + 2)

    def test_short_apdu_stays_short_form(self):
        from pysim_simple_server.server import _scp81_command_body
        self.assertEqual(_scp81_command_body('80CAFF2100').hex().upper(),
                         'AE80220580CAFF21000000')

    def test_definite_variant_ber_lengths(self):
        from pysim_simple_server.server import _scp81_command_body
        apdu = 'AB' * 130
        body = _scp81_command_body(apdu, definite=True)
        # AA 81 85 22 81 82 <130 bytes>  (outer 1+2+130 = 133 = 0x85)
        self.assertEqual(body[:6].hex().upper(), 'AA8185228182')


class VerbatimScriptTest(unittest.TestCase):
    def test_expanded_templates_sent_verbatim(self):
        reset_script(['AA0B2208 80CAFF2100'.replace(' ', '')])
        server._SCP81_SCRIPT_NEXT = 0
        server._SCP81_SCRIPT_RESULTS = []
        server._SCP81_SCRIPT_INSERTED = []
        server._SCP81_PAGES = 0
        try:
            status, headers, body = server._scp81_script_responder(
                'POST', '/api/scp81', {}, b'')
            self.assertEqual(status, 200)
            # Sent as-is (no AE80/22 wrapper added)
            self.assertEqual(body.hex().upper(), 'AA0B220880CAFF2100')
        finally:
            reset_script()
            server._SCP81_SCRIPT_NEXT = 0

    def test_plain_apdu_still_wrapped(self):
        reset_script(['80CAFF2100'])
        server._SCP81_SCRIPT_NEXT = 0
        try:
            status, headers, body = server._scp81_script_responder(
                'POST', '/api/scp81', {}, b'')
            self.assertEqual(body.hex().upper(), 'AE80220580CAFF21000000')
        finally:
            reset_script()
            server._SCP81_SCRIPT_NEXT = 0


class ResponseTlvLengthTest(unittest.TestCase):
    def test_long_form_r_apdu_length(self):
        # A page bigger than 127 bytes: `AF 80 23 81 FC <252 bytes> 00 00`
        page = bytes.fromhex('E3284F08D276000005AAFFCAFE00019F700101CC08A000000003000000' * 9)[:250]
        rapdu = page + b'\x63\x10'
        body = b'\xAF\x80\x23' + bytes([0x81, len(rapdu)]) + rapdu + b'\x00\x00'
        count, rapdus = server._scp81_parse_response(body)
        self.assertEqual(len(rapdus), 1)
        data, sw = rapdus[0]
        self.assertEqual(sw, '6310')
        self.assertEqual(len(data), 250)

    def test_short_form_still_works(self):
        page = bytes.fromhex('E3114F08A0000000030000009F70010FC50100')
        body = b'\xAF\x80\x23' + bytes([len(page) + 2]) + page + b'\xCA\xFE' + b'\x00\x00'
        count, rapdus = server._scp81_parse_response(body)
        self.assertEqual(rapdus, [(page, 'CAFE')])
