#!/usr/bin/env python3
"""Drift guard between ``simple_server`` and the legacy ``pysim_simple_server``.

The Phase 3b port copies spec tables out of the legacy server (the proactive
command, event and PROVIDE-LOCAL-INFORMATION names, the poll-negotiation
results, the Items Next Action Indicator types, the access-technology names and
the TS 102 223 §8.12.0/§8.12.2 TERMINAL RESPONSE result tables), and the two
implementations must decode a real capture identically.  A table fixed on one
side only would silently diverge; the legacy suites already guard this class of
bug *within* the legacy package (``tests/test_tr_results.py`` against the shared
``frontend/tests/tr_results.json``, ``tests/test_proactive_names.py``) and the
pysim-free suite guards the result tables against that same JSON
(``tests/test_simple_proactive.py``).  This module is the one place allowed to
import both implementations, so it does the cross-package comparison.

Add a table or a parser here when the port gains one.
"""

import pathlib
import contextlib
import io
import sys
import threading
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

PROJECTS = pathlib.Path(__file__).resolve().parents[2]
PY_SIM = PROJECTS / 'pysim'
if str(PY_SIM) not in sys.path:
    sys.path.insert(0, str(PY_SIM))

import pysim_simple_server.server as legacy  # noqa: E402
import pysim_simple_server.testscript as legacy_ts  # noqa: E402

from simple_server import bip as B  # noqa: E402
from simple_server import cat_session as C  # noqa: E402
from simple_server import proactive as P  # noqa: E402
from simple_server import tr as R  # noqa: E402


class SpecTableDriftTests(unittest.TestCase):
    """Every table the port duplicates must equal the legacy copy."""

    def test_proactive_type_names(self):
        self.assertEqual(P.PROACTIVE_TYPE_NAMES, legacy.PROACTIVE_TYPE_NAMES)

    def test_event_names(self):
        self.assertEqual(P.EVENT_NAMES, legacy.EVENT_NAMES)

    def test_pli_qualifier_names(self):
        self.assertEqual(P.PLI_QUALIFIER_NAMES, legacy.PLI_QUALIFIER_NAMES)

    def test_poll_negotiation_results(self):
        self.assertEqual(P.POLL_NEGOTIATION_RESULTS,
                         legacy.POLL_NEGOTIATION_RESULTS)

    def test_items_next_action_indicator_types(self):
        self.assertEqual(set(P.NAI_TYPES), set(legacy.NAI_TYPES))

    def test_access_technology_names(self):
        self.assertEqual(R.ACCESSTECH_NAMES, legacy.ACCESSTECH_NAMES)

    def test_sms_tpdu_type_names(self):
        self.assertEqual(P._SMS_MTI, legacy._SMS_MTI)

    def test_timer_management_actions(self):
        self.assertEqual(P._TIMER_ACTIONS, legacy._TIMER_ACTIONS)


class TerminalResponseDriftTests(unittest.TestCase):
    def test_result_tables(self):
        self.assertEqual(R.TR_RESULT_NAMES, legacy_ts.TR_RESULTS)
        self.assertEqual(R.TR_RESULT_ADD_INFO_NAMES,
                         legacy_ts.TR_RESULT_ADD_INFO)

    def test_result_names_for_every_coding(self):
        for code in range(0x100):
            self.assertEqual(R.tr_result_name(code),
                             legacy._tr_result_name(code), '0x%02X' % code)

    def test_additional_information_names(self):
        # 8.12.2 defines the coding for the terminal problem (20); the network
        # problem (21) only for 00; elsewhere there is no name
        for result in (0x00, 0x20, 0x21, 0x22):
            for info in range(0x100):
                self.assertEqual(
                    R.tr_result_add_info_name(result, info),
                    legacy._tr_result_add_info_name(result, info),
                    '%02X/%02X' % (result, info))

    def test_byte_level_helpers(self):
        self.assertEqual(R.bcd_swap(0x23), legacy._bcd_swap(0x23))
        self.assertEqual(R._dec_imei(bytes.fromhex('1A32547698103254')),
                         legacy._dec_imei('1A32547698103254'))
        self.assertEqual(R._dec_plmn(bytes.fromhex('521001')),
                         ('250', '11'))

    def test_tr_data_only(self):
        for tr in ('81030326008202818393055210011000030100',
                   '810301030082028281030100',
                   '8103010300020282810301008402011e',
                   ''):
            self.assertEqual(R.tr_data_only(bytes.fromhex(tr)),
                             legacy._tr_data_only(bytes.fromhex(tr)), tr)

    def test_extract_tr_result(self):
        for tr in ('8103010300820281838402011e030100',
                   '8103032600820281839305521001100083022001',
                   '810303260082028181',
                   ''):
            self.assertEqual(R.extract_tr_result(bytes.fromhex(tr)),
                             legacy._extract_tr_result(bytes.fromhex(tr)), tr)

    def test_decode_tr_parity_on_the_data_part(self):
        # The callers hand the legacy decoder the data part only; on that
        # input the two decodes are identical row for row.
        cases = [
            (0x26, 0x00, '93055210011000'),
            (0x26, 0x01, '94081A32547698103254'),
            (0x26, 0x03, '260762900312550021'),
            (0x26, 0x03, '260762105080709009'),
            (0x26, 0x04, '2D02656E'),
            (0x26, 0x05, '2E020000'),
            (0x26, 0x06, 'bf0103'),
            (0x26, 0x08, '62091332547698103204F8'),
            (0x26, 0x09, 'ad0101'),
            (0x26, 0x0A, '630104'),
            (0x26, 0x0E, '3F020308'),
            (0x26, 0x02, '3001ff'),
            (0x03, None, '8402011e'),
            (0x27, None, '2401032503417023'),
            (0x21, 0x00, '0d020441'),
        ]
        for cmd_type, qualifier, data in cases:
            label = '%02X/%s/%s' % (cmd_type,
                                    '--' if qualifier is None else '%02X' % qualifier,
                                    data)
            self.assertEqual(
                R.decode_tr(cmd_type, qualifier, bytes.fromhex(data)),
                legacy._decode_tr('%02x' % cmd_type,
                                  '' if qualifier is None else '%02x' % qualifier,
                                  data), label)


class CommandDecodeDriftTests(unittest.TestCase):
    """The command decoder must match the legacy one on the real captures."""

    # the live Alfa SET UP MENU (2026-09-29)
    LIVE_MENU = bytes.fromhex(
        'd03c810301250082028182850b416c6661204d6f62696c65'
        '8f16808112089db0c1c2c0beb9bab82f53657474696e6773'
        '8f0881810400d4e5f3f418020021')
    # the live RemMobileID GET INPUT (2026-09-29)
    LIVE_GET_INPUT = bytes.fromhex(
        'd03c8103012304820281828d2d0804120432043504340438044204350020004d006f00'
        '620069006c0065002d00490044002000500049004e003191020410')
    # the reference traces' OPEN CHANNEL (CR-set objects) and SEND DATA
    OPEN_CHANNEL = bytes.fromhex(
        'd02b8103014001820281828500b50103b9020200c70b076d656761666f6e2e7275'
        'bc03021f90be05217f000001')
    SEND_DATA = bytes.fromhex('d00e8103014301820281213701013603aabbcc')
    # the live card's SEND SHORT MESSAGE TPDU shape
    SEND_SM = bytes.fromhex('d0158103011300820283818b0b916106152670f900a35f020101')
    TIMER = bytes.fromhex('d011810301270082028182a40103a503417023')

    FIXTURES = [
        (0x25, 0x00, LIVE_MENU),
        (0x23, 0x04, LIVE_GET_INPUT),
        (0x40, 0x01, OPEN_CHANNEL),
        (0x43, 0x01, SEND_DATA),
        (0x13, 0x00, SEND_SM),
        (0x27, 0x00, TIMER),
    ]

    def test_decode_proactive_command_parity(self):
        for cmd_type, qualifier, raw in self.FIXTURES:
            self.assertEqual(P.decode_proactive_command(cmd_type, raw, qualifier),
                             legacy._decode_cmd(cmd_type, raw, qualifier),
                             '%02X' % cmd_type)

    def test_header_parity(self):
        for _cmd_type, _qualifier, raw in self.FIXTURES:
            self.assertEqual(P.parse_proactive_header(raw),
                             legacy._parse_proactive_header(raw))

    def test_poll_negotiation_parity(self):
        for data in ('', '1101020402011E', '11010104020102', '110100'):
            self.assertEqual(P.decode_poll_negotiation(data),
                             legacy._decode_poll_negotiation(data), data)


class _LegacyTp(object):
    """The legacy ``scc._tp``: records the hex APDUs, answers ``9000``."""

    def __init__(self):
        self.sent = []

    def send_apdu(self, apdu):
        self.sent.append(apdu)
        return ('', '9000')


class _LegacyScc(object):
    """The minimum a legacy CAT helper needs: the CLA and a transport."""

    def __init__(self, cat_cla='80'):
        self.cat_cla = cat_cla
        self._tp = _LegacyTp()


class _Link(object):
    """The simple_server transport shape: bytes in, ``(bytes, bytes)`` out."""

    def __init__(self):
        self.sent = []

    def send_apdu(self, apdu):
        self.sent.append(bytes(apdu))
        return (b'', b'\x90\x00')


class _ScriptedTp(object):
    """A legacy ``scc._tp`` with queued hex ``(data, sw)`` answers."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.sent = []

    def send_apdu(self, apdu):
        self.sent.append(apdu)
        if self.answers:
            return self.answers.pop(0)
        return ('', '9000')


class _ScriptedScc(object):
    """The minimum a legacy CAT helper needs, with scripted answers."""

    def __init__(self, *answers, cat_cla='80'):
        self.cat_cla = cat_cla
        self._tp = _ScriptedTp(*answers)


class _ScriptedLink(object):
    """The simple_server transport shape with queued ``(bytes, bytes)`` answers."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.sent = []

    def send_apdu(self, apdu):
        self.sent.append(bytes(apdu))
        if self.answers:
            data, sw = self.answers.pop(0)
            return data, sw
        return (b'', b'\x90\x00')


class CatSessionParityTests(unittest.TestCase):
    """The CAT session's APDU builders must match the legacy server's bytes.

    ``simple_server.cat_session`` is the port of the legacy CAT helpers; a
    builder that drifts changes what the card receives, so every constructor is
    compared byte for byte against the live-tested legacy implementation.  This
    module is the one place allowed to import both.
    """

    def setUp(self):
        self.legacy = _LegacyScc()
        self.link = _Link()
        self.session = C.CatSession(self.link)

    def test_the_tables(self):
        self.assertEqual(C.PLI_DEFAULTS, legacy.PLI_DEFAULTS)
        self.assertEqual(C.EVENT_DATA_MAX, legacy._EVENT_DATA_MAX)
        self.assertEqual(C.EVENT_INNER_MAX, legacy._EVENT_INNER_MAX)
        self.assertEqual(C.CHAIN_ROUNDS, legacy._POLL_CHAIN_MAX)
        self.assertEqual(C.TIMER_COUNT, 8)
        self.assertEqual(C.MENU_RESULTS,
                         {'ok': 0x00, 'cancel': 0x10, 'back': 0x11,
                          'timeout': 0x12, 'help': 0x13})

    def test_send_status(self):
        for cla in ('80', 'a0'):
            with self.subTest(cla=cla):
                self.link.sent[:] = []
                self.legacy._tp.sent[:] = []
                self.legacy.cat_cla = cla
                self.session.cat_cla = cla
                self.session.send_status()
                legacy._send_status(self.legacy)
                self.assertEqual(self.link.sent[0].hex(), self.legacy._tp.sent[0])

    def test_the_chain_terminal_response(self):
        cases = [
            (1, 0x21, 0x81, 0x82, 0x00, 0x81),
            (2, 0x40, 0x81, 0x82, 0x01, 0x81),
            (3, 0x27, 0x81, 0x82, 0x02, 0x01),
            (1, 0x26, 0x83, 0x82, 0x00, 0x81),
            (1, 0x26, 0x83, 0x82, 0x01, None),
            (1, 0x26, 0x83, 0x82, 0x06, 0x01),
            (1, 0x03, 0x83, 0x82, None, 0x84),
            (1, 0x00, 0x83, 0x81, None, None),
        ]
        for cmd_num, cmd_type, dev_src, dev_dst, qual, tag in cases:
            label = '%02X/%s/%s' % (cmd_type, qual, tag)
            self.assertEqual(
                self.session.build_chain_tr(cmd_num, cmd_type, dev_src, dev_dst,
                                            qual, tag),
                legacy._build_tr(self.legacy, cmd_num, cmd_type, dev_src,
                                 dev_dst, qual, cd_tag=tag), label)
        # the date/time answer is the host clock on both sides (frozen here):
        # TS 102 223 §6.4.15 / §8.39, the TP-SCTS coding
        stamp = datetime(2024, 5, 4, 3, 2, 1,
                         tzinfo=timezone(timedelta(hours=2)))
        with mock.patch.object(self.session, '_now', return_value=stamp):
            self.assertEqual(
                self.session.build_chain_tr(1, 0x26, 0x83, 0x82, 0x03, 0x01),
                legacy._build_tr(self.legacy, 1, 0x26, 0x83, 0x82, 0x03,
                                 cd_tag=0x01, dt=stamp))

    def test_pli_data_hex(self):
        for qual in sorted(C.PLI_DEFAULTS):
            self.assertEqual(self.session.pli_data_hex(qual),
                             legacy._pli_data_hex(qual), '%02X' % qual)
        self.assertEqual(self.session.pli_data_hex(0x10), legacy._pli_data_hex(0x10))

    def test_the_sms_pp_download_envelope(self):
        for tpdu in ('00', '0001', '02710000000f'):
            with self.subTest(tpdu=tpdu):
                self.link.sent[:] = []
                self.legacy._tp.sent[:] = []
                self.session.send_envelope(bytes.fromhex(tpdu),
                                           handle_proactive=False)
                legacy._send_envelope(tpdu, self.legacy,
                                      handle_proactive=False)
                self.assertEqual(self.link.sent[0].hex(),
                                 self.legacy._tp.sent[0])

    def test_a_long_sms_tpdu_uses_the_ber_long_form_length(self):
        # TS 101 220 §7.1.2 Table 7.6: a value of 128..255 octets is coded
        # ``81 nn`` - the first concatenated SCP80/RAM segment's TPDU is ~145
        # octets, so a raw length octet made every such ENVELOPE garbage.
        for length, body_len in ((130, 0x92), (150, 0xA6)):
            with self.subTest(length=length):
                tpdu = bytes(i % 0x100 for i in range(length))
                self.link.sent[:] = []
                self.legacy._tp.sent[:] = []
                self.session.send_envelope(tpdu, handle_proactive=False)
                legacy._send_envelope(tpdu.hex(), self.legacy,
                                      handle_proactive=False)
                self.assertEqual(self.link.sent[0].hex(),
                                 self.legacy._tp.sent[0])
                # the 8B object's length is ``81 <len>`` on both sides
                self.assertIn(bytes([0x8B, 0x81, length & 0xFF]),
                              self.link.sent[0])
                self.assertIn(bytes([0xD1, 0x81, body_len]),
                              self.link.sent[0])

    def test_the_terminal_profile_chain_bytes(self):
        # The profile chain is compared byte for byte: the APDU sequence and
        # the FETCH length (the 91XX byte, 0xFF for a bare 91) - the two bugs
        # a structural comparison would miss (M1/M2 of the review).
        poll60 = 'd00d8103010300820281838402013c'
        live_menu = CommandDecodeDriftTests.LIVE_MENU.hex()
        cases = [
            ('menu', [('', '913c'), (live_menu, '9000'), ('', '9000')]),
            ('bare 91', [('', '91'), (live_menu, '9000'), ('', '9000')]),
            ('poll', [('', '913c'), (poll60, '9000'), ('', '9000')]),
        ]
        for label, answers in cases:
            with self.subTest(case=label):
                legacy_scc = _ScriptedScc(*answers)
                link = _ScriptedLink(*[(bytes.fromhex(data), bytes.fromhex(sw))
                                       for data, sw in answers])
                session = C.CatSession(link)
                with mock.patch.object(legacy, '_POLL_ENABLED', False), \
                        mock.patch.object(legacy, '_POLL_INTERVAL', 30), \
                        mock.patch.object(legacy, '_POLL_DISABLED_BY_CARD',
                                          False), \
                        contextlib.redirect_stderr(io.StringIO()):
                    legacy._send_terminal_profile(legacy_scc, 'deadbeef')
                    session.send_terminal_profile('deadbeef')
                self.assertEqual([a.hex() for a in link.sent],
                                 legacy_scc._tp.sent, label)

    def test_the_chain_fetch_and_terminal_response_bytes(self):
        display_text = 'd00d8103012100820281828d020441'
        answers = [(display_text, '9000'), ('', '9000')]
        legacy_scc = _ScriptedScc(*answers)
        link = _ScriptedLink(*[(bytes.fromhex(data), bytes.fromhex(sw))
                               for data, sw in answers])
        session = C.CatSession(link)
        with mock.patch.object(legacy, '_TEST_RUNNING', False), \
                mock.patch.object(legacy, '_PROACTIVE_BUSY', False), \
                mock.patch.object(legacy, '_server_ref', None), \
                contextlib.redirect_stderr(io.StringIO()):
            legacy._run_proactive_chain(legacy_scc, '910d')
            session.handle_chain(b'\x91\x0d')
        self.assertEqual([a.hex() for a in link.sent], legacy_scc._tp.sent)
        self.assertEqual(link.sent[0], bytes.fromhex('801200000d'))

    def test_the_event_download_envelope(self):
        cases = [(0x03, '9b0100', None), (0x03, None, None),
                 (0x0A, '80010000', None), (0x03, '9b0100', '83'),
                 (0x03, '00' * 200, None)]
        for event_type, data, src in cases:
            with self.subTest(event=event_type, data=len(data or ''), src=src):
                self.link.sent[:] = []
                self.legacy._tp.sent[:] = []
                self.session.send_event_download(
                    event_type, bytes.fromhex(data) if data else None,
                    src=src)
                legacy._send_event_download(
                    self.legacy, event_type,
                    bytes.fromhex(data) if data else None, log=False, src=src)
                self.assertEqual(self.link.sent[0].hex(),
                                 self.legacy._tp.sent[0])

    def test_the_timer_command(self):
        raw = bytes.fromhex('d011810301270082028182a40103a503417023')
        # a start (the legacy arms a real timer; both are stubbed here)
        with mock.patch.object(legacy, '_timer_start') as l_start, \
                mock.patch.object(self.session, '_timer_start') as s_start:
            self.assertEqual(
                self.session.handle_timer_command(1, 0x27, 0x00, raw, 0x81,
                                                  0x82, cd_tag=0x81),
                legacy._handle_timer_command(1, 0x27, 0x00, raw, 0x81, 0x82,
                                             cd_tag=0x81))
        self.assertEqual(s_start.call_args, l_start.call_args)
        # a get / deactivate with a running timer and without one
        for remaining in (90, None):
            for qual in (0x01, 0x02):
                with mock.patch.object(legacy, '_timer_remaining',
                                       return_value=remaining), \
                        mock.patch.object(self.session, '_timer_remaining',
                                          return_value=remaining):
                    self.assertEqual(
                        self.session.handle_timer_command(
                            1, 0x27, qual, raw, 0x81, 0x82, cd_tag=0x81),
                        legacy._handle_timer_command(
                            1, 0x27, qual, raw, 0x81, 0x82, cd_tag=0x81),
                        '%s/%s' % (qual, remaining))

    def test_the_timer_expiration_envelope(self):
        # the legacy helper sends the envelope itself; both build the same APDU
        self.link.sent[:] = []
        self.legacy._tp.sent[:] = []
        self.session._timer_expired(1, 3723)
        server = mock.Mock(stk_pending=None, scc=self.legacy)
        with mock.patch.object(legacy, '_CARD_CONNECTED', True), \
                mock.patch.object(legacy, '_TEST_RUNNING', False), \
                mock.patch.object(legacy, '_server_ref', server), \
                mock.patch.object(legacy, '_CARD_LOCK', mock.MagicMock()), \
                contextlib.redirect_stderr(io.StringIO()):
            legacy._timer_expired(1, 3723)
        self.assertEqual(self.link.sent[0].hex(), self.legacy._tp.sent[0])
        self.assertEqual(self.link.sent[0].hex(),
                         '80c200000ed70c82028281a40101a503102030')

    def test_the_input_text_tlv(self):
        cases = [
            ({'type': 'get_input'}, ''),
            ({'type': 'get_inkey', 'yes_no': True}, '01'),
            ({'type': 'get_inkey', 'yes_no': True}, '00'),
            ({'type': 'get_inkey', 'digits_only': True}, '7'),
            ({'type': 'get_input', 'ucs2': True}, 'ab'),
            ({'type': 'get_input', 'packed': True}, 'hello'),
            ({'type': 'get_input'}, '1234'),
        ]
        for pending, text in cases:
            self.assertEqual(C.input_text_tlv(pending, text),
                             legacy._input_text_tlv(pending, text),
                             repr(text))

    def test_the_input_validation(self):
        cases = [
            ({'type': 'get_inkey', 'yes_no': True}, '01'),
            ({'type': 'get_inkey', 'yes_no': True}, '02'),
            ({'type': 'get_inkey'}, ''),
            ({'type': 'get_inkey'}, '12'),
            ({'type': 'get_inkey', 'digits_only': True}, 'x'),
            ({'type': 'get_input', 'min': 2, 'max': 4}, 'a'),
            ({'type': 'get_input', 'min': 2, 'max': 4}, 'abc'),
            ({'type': 'get_input', 'min': 2, 'max': 4}, 'abcde'),
            ({'type': 'get_input', 'hidden': True}, '12a'),
            ({'type': 'get_input'}, ''),
        ]
        for pending, text in cases:
            self.assertEqual(C.validate_input_response(pending, text),
                             legacy._validate_input_response(pending, text),
                             '%r %r' % (pending, text))


class _FakeBipTerminal(object):
    """The BipTerminal slice both BIP handlers use, scripted per case."""

    def __init__(self, enabled=True, open_result=(1, None), send_ok=True,
                 receive=b'', available=0, capacity=0xFF, close_ok=True,
                 channels=None):
        self.enabled = enabled
        self.mode = 'redirect'
        self.target = None
        self.channels = dict(channels or {})
        self.entries = []
        self.pending = []
        self.open_result = open_result
        self.send_ok = send_ok
        self.receive_data = receive
        self.available_bytes = available
        self.capacity = capacity
        self.close_ok = close_ok

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


def _bip_command(cmd_type, qualifier, data_tlvs=b'', dev_dst=0x21, tag=0x81,
                 cmd_num=1):
    """A D0-wrapped BIP command (the ``channel_cmd`` of the bip suite)."""
    tlvs = (bytes([tag, 0x03, cmd_num, cmd_type, qualifier])
            + bytes([0x82, 0x02, 0x81, dev_dst]) + data_tlvs)
    return bytes([0xD0, len(tlvs)]) + tlvs


def _bip_open(host, port, buffer_size=512, qualifier=0x01, tag=0x81):
    """An OPEN CHANNEL with an IPv4 destination and a TCP port."""
    ip = bytes(int(x) for x in host.split('.'))
    tlvs = (bytes([tag, 0x03, 0x01, 0x40, qualifier])
            + b'\x82\x02\x81\x82'
            + b'\x35\x01\x03'
            + b'\x39\x02' + buffer_size.to_bytes(2, 'big')
            + b'\x3c\x03\x02' + port.to_bytes(2, 'big')
            + b'\x3e\x05\x21' + ip)
    return bytes([0xD0, len(tlvs)]) + tlvs


class BipParityTests(unittest.TestCase):
    """The BIP handler must match the legacy one byte for byte.

    ``simple_server.bip`` is the port of the legacy BIP helpers; the TR bytes
    are what a live card receives and the local terminal state (the queued
    Channel status event, the open arguments) is what the following exchanges
    see, so both are compared.  The scripted terminal satisfies the slice both
    implementations call, and the legacy module global is patched for each
    case (the simple side takes the terminal by injection).
    """

    def test_the_terminal_response_builder(self):
        cases = [
            (1, 0x40, 0x01, 0x81, 0x82, 0x00, None, b'', 0x81),
            (1, 0x40, 0x01, 0x81, 0x82, 0x3A, 0x00, b'\x35\x01\x03', 0x81),
            (2, 0x43, None, 0x81, 0x21, 0x00, None, b'\x37\x01\xff', None),
            (3, 0x42, 0x00, 0x83, 0x21, 0x00, None, b'', 0x01),
            (1, 0x41, 0x00, 0x81, 0x21, 0x3A, 0x00, b'', 0x81),
            (1, 0x44, 0x00, 0x81, 0x82, 0x00, None, b'\x38\x02\x81\x00', 0x01),
        ]
        for (num, ctype, qual, src, dst, result, info, extra,
             tag) in cases:
            label = '%02X/%s/%s/%s' % (ctype, qual, result, tag)
            self.assertEqual(
                B.bip_terminal_response(num, ctype, qual, src, dst, result,
                                        info, extra, cd_tag=tag),
                legacy._bip_tr(num, ctype, qual, src, dst, result, info,
                               extra, cd_tag=tag), label)

    def test_the_command_handler(self):
        truncated = bytes.fromhex('d01c810301400c82028182850035010339'
                                  '020200' '4701003c030227be3e05')
        plain = bytes.fromhex('d00d01030140010202818239020200')
        cr_set = bytes.fromhex('d0248103014003820281828500b50103b90205'
                               '8ec70403475042bc03020582be05215bd50502')
        cases = [
            # (label, fake kwargs, cmd_type, qualifier, raw, dev_dst)
            ('open', dict(open_result=(1, None)),
             0x40, 0x01, _bip_open('127.0.0.1', 8080), 0x82),
            ('open disabled', dict(enabled=False),
             0x40, 0x01, _bip_open('127.0.0.1', 8080), 0x82),
            ('open refused', dict(open_result=(None, 'refused')),
             0x40, 0x01, _bip_open('127.0.0.1', 8080), 0x82),
            ('open background', dict(open_result=(3, None)),
             0x40, 0x0C, _bip_open('127.0.0.1', 8080, qualifier=0x0C), 0x82),
            ('open truncated', dict(open_result=(1, None)),
             0x40, 0x0C, truncated, 0x82),
            ('open no transport', dict(open_result=(1, None)),
             0x40, 0x01, plain, 0x82),
            ('open small buffer', dict(open_result=(1, None)), 0x40, 0x01,
             _bip_open('127.0.0.1', 8080, buffer_size=1), 0x82),
            ('open no buffer object',
             dict(open_result=(1, None)), 0x40, 0x01,
             bytes.fromhex('d00c810301400182028182350103'), 0x82),
            ('open cr-set', dict(open_result=(1, None)), 0x40, 0x03, cr_set,
             0x82),
            ('send', dict(capacity=0xFF), 0x43, 0x01,
             _bip_command(0x43, 0x01, b'\x36\x03abc'), 0x21),
            ('send low', dict(capacity=0x10), 0x43, 0x01,
             _bip_command(0x43, 0x01, b'\x36\x03abc'), 0x21),
            ('send failed', dict(send_ok=False), 0x43, 0x01,
             _bip_command(0x43, 0x01, b'\x36\x01x'), 0x21),
            ('send no channel', dict(), 0x43, 0x01,
             _bip_command(0x43, 0x01, b'\x36\x01x'), 0x27),
            ('receive', dict(receive=b'abc', available=5), 0x42, 0x00,
             _bip_command(0x42, 0x00, b'\x37\x01\x64'), 0x21),
            ('receive long', dict(receive=b'x' * 200, available=300), 0x42,
             0x00, _bip_command(0x42, 0x00, b'\x37\x01\xff'), 0x21),
            ('receive empty', dict(receive=b'', available=0), 0x42, 0x00,
             _bip_command(0x42, 0x00, b'\x37\x01\x10'), 0x21),
            ('receive nowhere', dict(receive=None), 0x42, 0x00,
             _bip_command(0x42, 0x00, b'\x37\x01\x10'), 0x21),
            ('receive no channel', dict(receive=b'abc'), 0x42, 0x00,
             _bip_command(0x42, 0x00, b'\x37\x01\x10'), 0x27),
            ('close', dict(), 0x41, 0x00, _bip_command(0x41, 0x00), 0x21),
            ('close failed', dict(close_ok=False), 0x41, 0x00,
             _bip_command(0x41, 0x00), 0x21),
            ('close no channel', dict(), 0x41, 0x00,
             _bip_command(0x41, 0x00), 0x27),
            ('status', dict(channels={3: None, 1: None}), 0x44, 0x00,
             _bip_command(0x44, 0x00), 0x21),
            ('status empty', dict(), 0x44, 0x00, _bip_command(0x44, 0x00),
             0x21),
        ]
        for label, kwargs, cmd_type, qualifier, raw, dev_dst in cases:
            with self.subTest(case=label):
                ours = _FakeBipTerminal(**kwargs)
                theirs = _FakeBipTerminal(**kwargs)
                session = B.BipSession(terminal=ours)
                with mock.patch.object(legacy, '_BIP', theirs):
                    self.assertEqual(
                        session.handle_command(1, cmd_type, qualifier, raw,
                                               0x81, dev_dst),
                        legacy._handle_bip_command(None, 1, cmd_type,
                                                   qualifier, raw, 0x81,
                                                   dev_dst), label)
                self.assertEqual(ours.pending, theirs.pending, label)
                self.assertEqual([e['kind'] for e in ours.entries],
                                 [e['kind'] for e in theirs.entries], label)
                # the local terminal state the following exchanges see
                for attr in ('opened', 'sent_data', 'closed', 'receive_args'):
                    ours_value = getattr(ours, attr, None)
                    theirs_value = getattr(theirs, attr, None)
                    if attr == 'receive_args' and theirs_value:
                        # the documented 3c-2 fix: the port caps the requested
                        # length so the answer fits one TERMINAL RESPONSE (the
                        # legacy asked for the full 255 and could not answer)
                        channel_id, maxlen = theirs_value
                        theirs_value = (channel_id,
                                        min(maxlen, B.CHANNEL_DATA_MAX))
                    self.assertEqual(ours_value, theirs_value,
                                     '%s: %s' % (label, attr))

    def test_an_unknown_command_falls_through_on_both_sides(self):
        raw = bytes.fromhex('d008810301260082028183')
        session = B.BipSession(terminal=_FakeBipTerminal())
        with mock.patch.object(legacy, '_BIP', _FakeBipTerminal()):
            self.assertIsNone(session.handle_command(1, 0x26, 0x00, raw,
                                                     0x81, 0x82))
            self.assertIsNone(legacy._handle_bip_command(None, 1, 0x26, 0x00,
                                                         raw, 0x81, 0x82))

    def test_the_channel_identifier_rule(self):
        for value in range(0x100):
            self.assertEqual(B.bip_channel_id(value),
                             legacy._bip_channel_id(value), '%02X' % value)


class _EventCatSession(object):
    """The CAT slice the BIP events use (the legacy server object's mirror)."""

    def __init__(self, event_list=None, busy=False, stk_pending=None):
        self.event_list = event_list
        self.busy = busy
        self.stk_pending = stk_pending
        self.sent = []

    def send_event_download(self, event_type, event_data=None, drain=True,
                            src=None):
        self.sent.append((event_type, bytes(event_data)))
        return (b'', b'\x90\x00')


class _FakeChannel(object):
    """What the ``on_data`` callback reads off a ``BipChannel``."""

    def __init__(self, channel_id=1, data=b'', peer_closed=False):
        self.id = channel_id
        self.rx = bytearray(data)
        self.peer_closed = peer_closed


class BipSessionParityTests(unittest.TestCase):
    """``_bip_listener_status`` and the two events against the legacy copies.

    ``_FakeBipTerminal``/``_FakeChannel``/``_EventCatSession`` give both sides
    the same inputs; the legacy module globals are patched per case (the port
    takes the terminal/session by injection).
    """

    def test_the_listener_status(self):
        listener = mock.Mock(host='127.0.0.1', port=4123, accepted=2)
        cases = [
            # (session state, legacy globals)
            ({'listener': listener, 'mode': 'sink',
              'target': ('127.0.0.1', 4123)},
             {'_BIP_LISTENER': listener, '_BIP_MODE': 'sink',
              '_BIP_TARGET': ('127.0.0.1', 4123)}),
            ({'listener': None, 'mode': 'redirect', 'target': ('10.0.0.5', 9000)},
             {'_BIP_LISTENER': None, '_BIP_MODE': 'redirect',
              '_BIP_TARGET': ('10.0.0.5', 9000)}),
            ({'listener': None, 'mode': 'passthru', 'target': None},
             {'_BIP_LISTENER': None, '_BIP_MODE': 'passthru',
              '_BIP_TARGET': None}),
            ({'listener': None, 'mode': None, 'target': None},
             {'_BIP_LISTENER': None, '_BIP_MODE': None, '_BIP_TARGET': None}),
        ]
        for state, globals_ in cases:
            label = repr(state['mode'])
            session = B.BipSession(terminal=_FakeBipTerminal())
            session.listener = state['listener']
            session.mode = state['mode']
            session.target = state['target']
            patches = [mock.patch.object(legacy, name, value)
                       for name, value in globals_.items()]
            for patch in patches:
                patch.start()
            try:
                theirs = legacy._bip_listener_status()
            finally:
                for patch in reversed(patches):
                    patch.stop()
            self.assertEqual(session._listener_status(), theirs, label)

    def test_the_data_available_event(self):
        cases = [
            # (label, event list, channel, busy)
            ('data', [0x09, 0x0A], _FakeChannel(3, b'12345'), False),
            ('long', [0x09], _FakeChannel(1, b'x' * 400), False),
            ('empty', [0x09], _FakeChannel(1, b''), False),
            ('closed peer', [0x09], _FakeChannel(1, b'x', peer_closed=True),
             False),
            ('unsubscribed', [0x0A], _FakeChannel(1, b'x'), False),
            ('busy card', [0x09], _FakeChannel(1, b'x'), True),
        ]
        for label, events, channel, busy in cases:
            with self.subTest(case=label):
                cat = _EventCatSession(events, busy=busy)
                ours_terminal = _FakeBipTerminal()
                session = B.BipSession(cat, terminal=ours_terminal)
                theirs_terminal = _FakeBipTerminal()
                server = mock.Mock(event_list=events, scc=object(),
                                   stk_pending=None)
                send = mock.Mock(return_value=('', '9000'))
                with mock.patch.object(legacy, '_BIP', theirs_terminal), \
                        mock.patch.object(legacy, '_server_ref', server), \
                        mock.patch.object(legacy, '_CARD_CONNECTED', True), \
                        mock.patch.object(legacy, '_PROACTIVE_BUSY', busy), \
                        mock.patch.object(legacy, '_CARD_LOCK',
                                          threading.RLock()), \
                        mock.patch.object(legacy, '_send_event_download',
                                          send):
                    theirs = legacy._bip_data_available(channel)
                    ours = session.data_available(channel)
                self.assertEqual(ours, theirs, label)
                self.assertEqual(
                    [(c.args[1], c.args[2]) for c in send.call_args_list],
                    cat.sent, label)
                self.assertEqual([e['kind'] for e in ours_terminal.entries],
                                 [e['kind'] for e in theirs_terminal.entries],
                                 label)

    def test_the_channel_status_events(self):
        cases = [
            # (label, link_events, event list, queued statuses)
            ('two events', True, [0x09, 0x0A], [(0x81, 0x00), (None, 0x05)]),
            ('link events off', False, [0x0A], [(0x81, 0x00)]),
            ('unsubscribed', True, [0x09], [(0x81, 0x00)]),
            ('nothing queued', True, [0x0A], []),
        ]
        for label, link_events, events, queued in cases:
            with self.subTest(case=label):
                cat = _EventCatSession(events)
                ours_terminal = _FakeBipTerminal()
                session = B.BipSession(cat, link_events=link_events,
                                       terminal=ours_terminal)
                theirs_terminal = _FakeBipTerminal()
                for channel_id, (status, info) in enumerate(queued, start=1):
                    session.terminal._queue_link_status(channel_id,
                                                        status=status,
                                                        info=info)
                    theirs_terminal._queue_link_status(channel_id,
                                                       status=status,
                                                       info=info)
                server = mock.Mock(event_list=events, scc=object())
                send = mock.Mock(return_value=('', '9000'))
                with mock.patch.object(legacy, '_BIP', theirs_terminal), \
                        mock.patch.object(legacy, '_server_ref', server), \
                        mock.patch.object(legacy, '_CARD_LOCK',
                                          threading.RLock()), \
                        mock.patch.object(legacy, '_BIP_LINK_EVENTS',
                                          link_events), \
                        mock.patch.object(legacy, '_FLUSHING_CHANNEL_EVENTS',
                                          False), \
                        mock.patch.object(legacy, '_send_event_download',
                                          send):
                    legacy._bip_flush_channel_events(server.scc)
                    session.flush_channel_events()
                self.assertEqual(
                    [(c.args[1], c.args[2]) for c in send.call_args_list],
                    cat.sent, label)
                self.assertEqual(ours_terminal.pending, theirs_terminal.pending,
                                 label)


if __name__ == '__main__':
    unittest.main()
