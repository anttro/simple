# coding=utf-8
"""Tests for :mod:`simple_server.cat_session` (the CAT session engine).

Every exchange is driven through a fake transport (scripted ``send_apdu``
answers), so the suite needs no card and no pysim.  The command fixtures are
existing live captures - the live Alfa SET UP MENU and DISPLAY TEXT, the live
RemMobileID GET INPUT, the live TIMER MANAGEMENT, the POLL INTERVAL and the
SEND SHORT MESSAGE form - and the TERMINAL RESPONSE / ENVELOPE bytes are
pinned as literals.  The byte parity with the legacy server
(``pysim_simple_server/server.py``) lives in ``tests/test_simple_drift.py``
(the one module allowed to import both implementations); this suite checks the
port against the *spec-derived* expectation and the fixtures.

Spec anchors used below: TS 102 221 §11.2.2 (ENVELOPE), §11.2.3 (FETCH),
§11.2.4 (TERMINAL RESPONSE); TS 102 223 §6.8.0/§6.8.1 (TR structure, the
echoed command details), §6.4.2/§6.4.3 (GET INKEY/GET INPUT), §6.4.6/§6.4.14
(POLL INTERVAL/POLLING OFF), §6.4.15 (PROVIDE LOCAL INFORMATION), §6.6.21/§7.4
(TIMER MANAGEMENT), §8.12 (result), §8.15 (text string, §6.8.5 codings),
§8.39 (date-time and time zone, TP-SCTS per TS 123 040 §9.2.3.11).
"""

import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from simple_server import cat_session
from simple_server import proactive
from simple_server import tr
from simple_server.cat_session import (CatSession, CatSessionError,
                                       PorSubmitCapture, input_text_tlv,
                                       validate_input_response)

# ---- fixtures -------------------------------------------------------------

# The live Alfa SET UP MENU (2026-09-29): CR-set title 0x85, items 0x8F.
LIVE_MENU = bytes.fromhex(
    'd03c810301250082028182850b416c6661204d6f62696c65'
    '8f16808112089db0c1c2c0beb9bab82f53657474696e6773'
    '8f0881810400d4e5f3f418020021')
# A DISPLAY TEXT with the SMS default alphabet text 'A' (8D 02 04 41).
DISPLAY_TEXT = bytes.fromhex('d00d8103012100820281828d020441')
# A SELECT ITEM with one item (8F 02 01 41 -> id 1, 'A').
SELECT_ITEM = bytes.fromhex('d00d8103012400820281828f020141')
# A GET INKEY, digits only (qualifier b1 = 0) with the UCS2 text 'A'.
GET_INKEY = bytes.fromhex('d00e8103012200820281828d03080041')
# The live RemMobileID GET INPUT (2026-09-29): qualifier 04 = digits + hidden.
LIVE_GET_INPUT = bytes.fromhex(
    'd03c8103012304820281828d2d0804120432043504340438044204350020004d006f00'
    '620069006c0065002d00490044002000500049004e003191020410')
# The live TIMER MANAGEMENT: start timer 3 with 14:07:32 (0x41 0x70 0x23).
TIMER_START = bytes.fromhex('d011810301270082028182a40103a503417023')
# A POLL INTERVAL requesting 30 s.
POLL_INTERVAL = bytes.fromhex('d00d8103010300820281838402011e')


def command(cmd_type, qualifier, extras=b'', cmd_num=1):
    """A D0-wrapped proactive command (CR-set command details, UICC -> terminal)."""
    body = (bytes([0x81, 0x03, cmd_num, cmd_type, qualifier])
            + bytes([0x82, 0x02, 0x83, 0x81]) + extras)
    return bytes([0xD0, len(body)]) + body


def send_short_message(tpdu):
    """A SEND SHORT MESSAGE carrying ``tpdu`` (tag 8B, TS 102 223 §8.13)."""
    return command(0x13, 0x00, bytes([0x8B, len(tpdu)]) + tpdu)


def sms_submit(segment, total, ref=0x01, payload=b'\xaa\xbb'):
    """An SMS-SUBMIT with a concatenation UDH (8-bit DCS, so UDL counts octets).

    ``payload`` is the part's user data.  The TP-UDI/UDHL layout is
    TS 23.040 §9.2.3.24 with the IEI 0x00 concatenation element.
    """
    da = bytes.fromhex('0581' '2143f5')          # 5 digits '12345'
    udh = bytes([0x00, 0x03, ref, total, segment])
    ud = bytes([len(udh)]) + udh + payload
    return bytes([0x41, 0x00]) + da + bytes([0x00, 0x04, len(ud)]) + ud


class FakeLink(object):
    """A scripted transport: ``(data, sw)`` answers, in order.

    ``repeat_last`` keeps answering the last queued pair (the card that
    re-queues a command); otherwise the default is an empty ``9000``.  Every
    sent APDU is recorded in ``sent`` as bytes.
    """

    def __init__(self, *answers, **kwargs):
        self.answers = list(answers)
        self.repeat_last = bool(kwargs.pop('repeat_last', False))
        if kwargs:
            raise TypeError('unexpected %s' % sorted(kwargs))
        self.sent = []

    def send_apdu(self, apdu):
        self.sent.append(bytes(apdu))
        if self.repeat_last and len(self.answers) == 1:
            return self.answers[0]
        if self.answers:
            return self.answers.pop(0)
        return (b'', b'\x90\x00')

    def hex(self, index=-1):
        return self.sent[index].hex()


def session(*answers, **kwargs):
    link = FakeLink(*answers, repeat_last=kwargs.pop('repeat_last', False))
    kwargs.setdefault('clock', lambda: 1000.0)
    return CatSession(link, **kwargs), link


class StatusTests(unittest.TestCase):
    def test_the_uicc_form_is_p2_0c_with_the_negotiated_length(self):
        # P2 0C keeps the DF's FCP out of the answer; a UICC takes Le 00 and
        # the transport negotiates 6Cxx (TS 102 221 §11.2.1).
        s, link = session()
        self.assertEqual(s.send_status(), (b'', b'\x90\x00'))
        self.assertEqual(link.sent, [bytes.fromhex('80f2000c00')])

    def test_the_sim_form_needs_the_fixed_response_length(self):
        # TS 151 011: a 2G SIM session answers P3 23 only.
        s, link = session()
        s.cat_cla = 'a0'
        s.send_status()
        self.assertEqual(link.sent, [bytes.fromhex('a0f2000c23')])

    def test_the_915xx_answer_is_returned_untouched(self):
        s, _link = session((b'', b'\x91\x0d'))
        self.assertEqual(s.send_status(), (b'', b'\x91\x0d'))


class TerminalProfileTests(unittest.TestCase):
    def test_the_profile_apdu_and_the_answer_chain(self):
        # TERMINAL PROFILE is CLA 10 00 00 Lc <profile> (TS 102 221 §11.2.1);
        # the 91xx answer raises the chain, whose FETCH uses the 91XX length
        # (the legacy form) and whose TERMINAL RESPONSE is the generic
        # basic-result one.
        s, link = session((b'', b'\x91\x3c'), (LIVE_MENU, b'\x90\x00'),
                          (b'', b'\x90\x00'), (b'', b'\x90\x00'))
        seen = []
        s.on_menu = seen.append
        menu, events = s.send_terminal_profile('deadbeef')
        self.assertEqual(link.sent[0], bytes.fromhex('8010000004deadbeef'))
        self.assertEqual(link.sent[1], bytes.fromhex('801200003c'))
        self.assertEqual(link.sent[2],
                         bytes.fromhex('801400000c') + bytes.fromhex(
                             '810301250002028281030100'))
        self.assertEqual(link.sent[3], bytes.fromhex('80f2000c00'))
        self.assertEqual(menu, proactive.parse_setup_menu_command(LIVE_MENU))
        self.assertIsNone(events)
        self.assertEqual(seen, [menu])
        self.assertEqual(s.sim_menu, menu)

    def test_a_bare_91_falls_back_to_the_ff_fetch_length(self):
        # a 91 answer without its length byte has no named length; the legacy
        # profile chain used 0xFF (a fixed 0xFF on a 91XX card, in contrast,
        # made a T=1 card answer 6Cxx and broke the chain).
        s, link = session((b'', b'\x91'), (DISPLAY_TEXT, b'\x90\x00'),
                          (b'', b'\x90\x00'), (b'', b'\x90\x00'))
        s.send_terminal_profile('00')
        self.assertEqual(link.sent[1], bytes.fromhex('80120000ff'))

    def test_the_poll_interval_is_adopted_in_the_profile_chain(self):
        # TS 102 223 §6.4.6/§6.8.4: the TR must state the interval the
        # terminal will use - the profile chain adopts it exactly like the
        # 91XX chain (the legacy _send_terminal_profile called
        # _handle_card_poll_command here too).
        poll60 = bytes.fromhex('d00d8103010300820281838402013c')
        s, link = session((b'', b'\x91\x0d'), (poll60, b'\x90\x00'),
                          (b'', b'\x90\x00'), (b'', b'\x90\x00'))
        s.send_terminal_profile('00')
        self.assertEqual(s.poll_interval, 60)
        self.assertFalse(s.poll_suspended)
        self.assertEqual(link.hex(2),
                         '8014000010' '8103010300' '02028381' '030100'
                         '0402013c')

    def test_polling_off_in_the_profile_chain_suspends(self):
        s, _link = session((b'', b'\x91\x09'), (command(0x04, 0x00), b'\x90\x00'),
                           (b'', b'\x90\x00'), (b'', b'\x90\x00'))
        s.send_terminal_profile('00')
        self.assertTrue(s.poll_suspended)

    def test_the_event_list_is_captured(self):
        events_cmd = bytes.fromhex('d00c810301050082028381990101')
        s, link = session((b'', b'\x91\x04'), (events_cmd, b'\x90\x00'),
                          (b'', b'\x90\x00'), (b'', b'\x90\x00'))
        _menu, events = s.send_terminal_profile(b'\xff')
        self.assertEqual(events, [0x01])
        self.assertEqual(s.event_list, [0x01])
        self.assertIsNone(s.sim_menu)
        self.assertEqual(link.hex(1), '8012000004')

    def test_the_chain_is_bounded(self):
        # A card that keeps re-queueing a command must not hold the caller
        # (the same bound handle_chain got in v3.25.1); the FETCH uses the
        # 91XX length, the legacy profile-chain form.
        s, link = session((b'', b'\x91\x0d'), (DISPLAY_TEXT, b'\x91\x0d'),
                          repeat_last=True)
        s.chain_rounds = 2
        s.send_terminal_profile('00')
        fetches = [a for a in link.sent if a[1] == 0x12]
        self.assertEqual(len(fetches), 2)
        self.assertEqual(fetches[0], bytes.fromhex('801200000d'))

    def test_a_bad_profile_is_refused(self):
        s, link = session()
        for value in ('', b'', 'zz', '00' * 256):
            with self.subTest(value=len(str(value))):
                with self.assertRaises(CatSessionError):
                    s.send_terminal_profile(value)
        self.assertEqual(link.sent, [])

    def test_a_resend_answers_a_pending_command_first(self):
        # A TERMINAL PROFILE re-send must never shadow a FETCHed command
        # (the "never leave a FETCH unanswered" invariant): the pending one is
        # answered with a cancel TR before the new profile goes out.
        s, link = session((b'', b'\x90\x00'))
        s.log_proactive(0x21, DISPLAY_TEXT, 0x00, 1)
        s.menu_fetch_handler({})(DISPLAY_TEXT, 1, 0x21, 0x81, 0x82)
        s.menu_active = True
        s.sim_menu = {'command_number': 1, 'items': []}
        self.assertIsNotNone(s.stk_pending)
        out = s.resend_terminal_profile('00')
        # the cancel TR is the first APDU, then the new profile
        self.assertEqual(link.sent[0],
                         bytes.fromhex('801400000d' '810301210002028281'
                                       '83021000'))
        self.assertEqual(link.sent[1], bytes.fromhex('801000000100'))
        # the history survives with a boundary marker (v3.23.7)
        self.assertEqual([e.get('id') for e in s.log], [1, 2])
        self.assertEqual(s.log[-1]['marker'], 'terminal-profile')
        self.assertIsNone(s.stk_pending)
        self.assertFalse(s.menu_active)
        self.assertEqual(out, {'menu': False, 'events': None})


class ChainTests(unittest.TestCase):
    def test_a_fetched_display_text_is_logged_and_answered(self):
        s, link = session((DISPLAY_TEXT, b'\x90\x00'), (b'', b'\x90\x00'),
                          (b'', b'\x90\x00'))
        self.assertIsNone(s.handle_chain(b'\x91\x0d'))
        # FETCH uses the 91xx length; the TR is the generic basic-result one
        # (command details echoed, device identities swapped, result 03 01 00);
        # the 9000 answer polls STATUS once (TS 102 221 §14.6.2 idle polling).
        self.assertEqual(link.sent, [
            bytes.fromhex('801200000d'),
            bytes.fromhex('801400000c810301210002028281030100'),
            bytes.fromhex('80f2000c00'),
        ])
        self.assertEqual(len(s.log), 1)
        entry = s.log[0]
        self.assertEqual(entry['type_hex'], '21')
        self.assertEqual(entry['type_name'], 'DISPLAY TEXT')
        self.assertEqual(entry['raw'], DISPLAY_TEXT.hex())
        self.assertEqual(entry['cmd_decoded'], [{'label': 'Text', 'value': 'A'}])
        self.assertEqual(entry['tr_sw'], '9000')
        self.assertEqual(entry['tr_hex'], '')
        self.assertEqual(entry['tr_result'], '00')
        self.assertEqual(entry['tr_result_name'],
                         'Command performed successfully')
        self.assertEqual(entry['tr_decoded'], [])

    def test_the_response_objects_follow_the_command(self):
        # A POLL INTERVAL echoes the adopted Duration (TS 102 223 §6.8.4) and
        # a PROVIDE LOCAL INFORMATION the dictionary's data object (§6.4.15).
        s, link = session((POLL_INTERVAL, b'\x90\x00'), (b'', b'\x90\x00'),
                          (b'', b'\x90\x00'))
        s.handle_chain(b'\x91\x0d')
        self.assertEqual(link.hex(1),
                         '8014000010' '8103010300020283810301'
                         '000402011e')
        s2, link2 = session((command(0x26, 0x00), b'\x90\x00'),
                            (b'', b'\x90\x00'), (b'', b'\x90\x00'))
        s2.handle_chain(b'\x91\x0f')
        self.assertEqual(link2.hex(1),
                         '8014000015' '810301260002028183030100'
                         '130700f11000010001')

    def test_pause_sends_no_terminal_response(self):
        # The menu browser answers later: a pause must not FETCH again and must
        # not flush the BIP events either (the command awaits its TR).
        flushed = []
        s, link = session((DISPLAY_TEXT, b'\x90\x00'), bip_flush=lambda: flushed.append(1))
        response = {}
        self.assertIsNone(s.handle_chain(b'\x91\x0d',
                                         s.menu_fetch_handler(response)))
        self.assertEqual(link.sent, [bytes.fromhex('801200000d')])
        self.assertEqual(s.stk_pending['type'], 'display_text')
        self.assertEqual(s.stk_pending['text'], 'A')
        self.assertEqual(s.stk_pending['raw'], DISPLAY_TEXT.hex())
        self.assertEqual(response, {'type': 'display_text', 'text': 'A'})
        self.assertEqual(flushed, [])

    def test_exit_answers_then_returns_the_status_word(self):
        flushed = []
        s, link = session((DISPLAY_TEXT, b'\x90\x00'), (b'', b'\x90\x00'),
                          (b'', b'\x90\x00'), bip_flush=lambda: flushed.append(1))
        result = s.handle_chain(b'\x91\x0d', lambda *a: 'exit')
        self.assertEqual(result, b'\x90\x00')
        # the TR was still sent, then the exit flushed the channel events
        self.assertEqual(link.sent[1][1], 0x14)
        self.assertEqual(flushed, [1])

    def test_the_bip_handler_answers_the_command(self):
        open_channel = bytes.fromhex(
            'd02b8103014001820281828500b50103b9020200c70b076d656761666f6e2e7275'
            'bc03021f90be05217f000001')
        calls = []
        payload = bytes.fromhex('810301400102028182030100')

        def bip(cmd_num, cmd_type, cmd_qual, raw, dev_src, dev_dst, cd_tag):
            calls.append((cmd_num, cmd_type, cmd_qual, raw, dev_src, dev_dst,
                          cd_tag))
            return payload

        s, link = session((open_channel, b'\x90\x00'), (b'', b'\x90\x00'),
                          (b'', b'\x90\x00'), bip_handler=bip)
        s.handle_chain(b'\x91\x2b')
        self.assertEqual(calls, [(1, 0x40, 0x01, open_channel, 0x81, 0x82,
                                  0x81)])
        self.assertEqual(link.hex(1), '801400000c' + payload.hex())

    def test_the_bip_handler_none_falls_back_to_the_generic_tr(self):
        open_channel = bytes.fromhex('d00e8103014301820281213701013603aabbcc')
        s, link = session((open_channel, b'\x90\x00'), (b'', b'\x90\x00'),
                          (b'', b'\x90\x00'))
        s.handle_chain(b'\x91\x0e')
        self.assertEqual(link.hex(1),
                         '801400000c810301430102022181' + '030100')

    def test_a_raising_bip_handler_still_answers(self):
        open_channel = bytes.fromhex('d00e8103014301820281213701013603aabbcc')

        def boom(*args):
            raise RuntimeError('bip bug')

        s, link = session((open_channel, b'\x90\x00'), (b'', b'\x90\x00'),
                          (b'', b'\x90\x00'), bip_handler=boom)
        with self.assertLogs('simple_server.cat_session', 'WARNING') as logs:
            s.handle_chain(b'\x91\x0e')
        self.assertIn('bip bug', logs.output[0])
        self.assertEqual(link.sent[1][1], 0x14)

    def test_the_timer_command_is_answered_by_the_session(self):
        # TIMER MANAGEMENT is the session's own: a start answers the bare
        # success and arms timer 3 with the value 14:07:32.
        s, link = session((TIMER_START, b'\x90\x00'), (b'', b'\x90\x00'),
                          (b'', b'\x90\x00'))
        try:
            s.handle_chain(b'\x91\x11')
            self.assertEqual(link.hex(1),
                             '801400000c810301270002028281030100')
            self.assertIsNotNone(s._timer_remaining(3))
            self.assertGreaterEqual(s._timer_remaining(3), 50850)
        finally:
            s.cancel_timers()

    def test_the_round_bound_stops_a_requeueing_card(self):
        s, link = session((DISPLAY_TEXT, b'\x91\x0d'), repeat_last=True)
        s.chain_rounds = 3
        s.handle_chain(b'\x91\x0d')
        fetches = [a for a in link.sent if a[1] == 0x12]
        self.assertEqual(len(fetches), 3)

    def test_max_rounds_overrides_the_default(self):
        s, link = session((DISPLAY_TEXT, b'\x91\x0d'), repeat_last=True)
        s.handle_chain(b'\x91\x0d', max_rounds=1)
        self.assertEqual(len([a for a in link.sent if a[1] == 0x12]), 1)

    def test_a_test_run_owns_the_card(self):
        s, link = session((DISPLAY_TEXT, b'\x90\x00'),
                          test_running=lambda: True)
        self.assertIsNone(s.handle_chain(b'\x91\x0d'))
        self.assertEqual(link.sent, [])
        self.assertFalse(s.busy)

    def test_the_busy_flag_wraps_the_chain(self):
        seen = []
        s, _link = session((DISPLAY_TEXT, b'\x90\x00'), (b'', b'\x90\x00'),
                           (b'', b'\x90\x00'))
        s.handle_chain(b'\x91\x0d',
                       lambda *a: seen.append(s.busy) or None)
        self.assertEqual(seen, [True])
        self.assertFalse(s.busy)

    def test_a_decoder_bug_still_answers_the_fetch(self):
        # v3.6.57: an IndexError in the FETCH path used to be a 500 with the
        # card's command left unanswered.
        s, link = session((DISPLAY_TEXT, b'\x90\x00'), (b'', b'\x90\x00'),
                          (b'', b'\x90\x00'))
        with mock.patch.object(proactive, 'parse_proactive_header',
                               side_effect=IndexError('boom')):
            with self.assertLogs('simple_server.cat_session', 'WARNING') as logs:
                s.handle_chain(b'\x91\x0d')
        self.assertIn('FETCH parse error: boom', logs.output[0])
        self.assertEqual(link.sent[1],
                         bytes.fromhex('801400000c810301000002028183030100'))

    def test_a_non_915_start_does_nothing(self):
        s, link = session()
        self.assertIsNone(s.handle_chain('9000'))
        self.assertEqual(link.sent, [])

    def test_an_empty_handler_payload_falls_back_to_the_generic_tr(self):
        # A TERMINAL RESPONSE needs a payload; an empty one would be a bare
        # `14` header and leave the fetched command unanswered.
        open_channel = bytes.fromhex('d00e8103014301820281213701013603aabbcc')

        def empty(*args):
            return b''

        s, link = session((open_channel, b'\x90\x00'), (b'', b'\x90\x00'),
                          (b'', b'\x90\x00'), bip_handler=empty)
        s.handle_chain(b'\x91\x0e')
        self.assertEqual(link.hex(1),
                         '801400000c810301430102022181' + '030100')
        # the pure builder refuses an empty payload outright
        with self.assertRaises(CatSessionError):
            s._terminal_response_apdu(b'')


class EnvelopeTests(unittest.TestCase):
    def test_the_sms_pp_download_apdu(self):
        # CLA C2 00 00 Lc D1 <len> 82 02 83 81 86 <len> <addr> 8B <len> <tpdu>
        # (TS 102 221 §11.2.2, TS 31.111 §7.1.1.2).
        s, link = session((b'', b'\x90\x00'))
        s.send_envelope(b'\x00\x01', handle_proactive=False)
        self.assertEqual(link.hex(0),
                         '80c2000013'
                         'd11182028381' '8607812143658719f2' '8b020001')

    def test_611xx_fetches_the_named_length(self):
        # ISO 7816-4: 61xx promises xx response bytes.
        por = bytes.fromhex('00112233445566778899AA')
        s, link = session((b'', b'\x61\x0b'), (por, b'\x90\x00'))
        data, sw = s.send_envelope(b'\x00', handle_proactive=False)
        self.assertEqual(link.hex(1), '00c000000b')
        self.assertEqual((data, sw), (por, b'\x90\x00'))

    def test_a_warning_with_pending_bytes_fetches_the_por(self):
        # The card quirk behind v3.25.7: 62xx/63xx without data may hold the
        # response packet; the GET RESPONSE uses Le 256 (coded 00).
        por = b'\x00' * 30
        s, link = session((b'', b'\x63\x10'), (por, b'\x90\x00'))
        data, sw = s.send_envelope(b'\x00', handle_proactive=False)
        self.assertEqual(link.hex(1), '00c0000000')
        self.assertEqual((data, sw), (por, b'\x90\x00'))

    def test_a_bare_61_uses_le_256_not_an_odd_apdu(self):
        s, link = session((b'', b'\x61'), (b'\xaa\xbb', b'\x90\x00'))
        data, sw = s.send_envelope(b'\x00', handle_proactive=False)
        self.assertEqual(link.hex(1), '00c0000000')
        self.assertEqual((data, sw), (b'\xaa\xbb', b'\x90\x00'))

    def test_a_warning_without_bytes_keeps_the_warning_and_polls(self):
        s, link = session((b'', b'\x62\x00'), (b'', b'\x6a\x86'),
                          (b'', b'\x90\x00'))
        data, sw = s.send_envelope(b'\x00', submit_handler=PorSubmitCapture())
        self.assertEqual(link.hex(1), '00c0000000')
        self.assertEqual((data, sw), (b'', b'\x62\x00'))
        # the late-PoR STATUS poll ran (the v3.6.58 path)
        self.assertEqual(link.sent[2], bytes.fromhex('80f2000c00'))

    def test_a_915xx_chain_keeps_the_delivered_por(self):
        # v3.28.6: 91xx is the CAT success state, not an error - the GET
        # RESPONSE may deliver the PoR in the same answer.
        por = bytes.fromhex('027100000e0a00000000000000140000016101')
        s, link = session((b'', b'\x61\x13'), (por, b'\x91\x30'),
                          (DISPLAY_TEXT, b'\x90\x00'), (b'', b'\x90\x00'),
                          (b'', b'\x90\x00'))
        data, sw = s.send_envelope(b'\x00')
        self.assertEqual(link.hex(1), '00c0000013')
        self.assertEqual((data, sw), (por, b'\x90\x00'))
        self.assertEqual(link.sent[2], bytes.fromhex('8012000030'))

    def test_poll_status_false_skips_the_late_por_poll(self):
        s, link = session((b'', b'\x90\x00'))
        s.send_envelope(b'\x00', submit_handler=PorSubmitCapture(),
                        poll_status=False)
        self.assertEqual(len(link.sent), 1)

    def test_the_send_short_message_capture_assembles_the_segments(self):
        first = sms_submit(1, 2)
        second = sms_submit(2, 2, payload=b'\xcc\xdd')
        capture = PorSubmitCapture()
        s, link = session(
            (b'', b'\x90\x00'),                      # the ENVELOPE itself
            (b'', b'\x91\x02'),                      # late-PoR STATUS
            (send_short_message(first), b'\x90\x00'),   # FETCH -> SEND SM 1
            (b'', b'\x90\x00'),                      # its TR
            (b'', b'\x91\x02'),                      # STATUS
            (send_short_message(second), b'\x90\x00'),
            (b'', b'\x90\x00'),
            (b'', b'\x90\x00'))
        data, sw = s.send_envelope(b'\x00', submit_handler=capture)
        self.assertEqual((data, sw), (b'', b'\x90\x00'))
        self.assertEqual(capture.sms_segments,
                         [(0x01, 2, 1, 'aabb'), (0x01, 2, 2, 'ccdd')])
        self.assertEqual(capture.submit_ud_hex, 'aabbccdd')
        # the legacy rule: the TPDU recorded is the segment that completed
        # the set (an immediate capture keeps its own)
        self.assertEqual(capture.submit_tpdu_hex, second.hex())
        # the DELIVER-style response packet form (the legacy _sms_submit_por)
        self.assertEqual(capture.submit_response_hex(), '027100aabbccdd')
        self.assertEqual(len([a for a in link.sent if a[1] == 0x14]), 2)

    def test_a_long_tpdu_uses_the_ber_long_form_length(self):
        # TS 101 220 §7.1.2 Table 7.6: 128..255 octets are ``81 nn``.  The
        # first segment of a concatenated SCP80/RAM send carries a ~145-octet
        # TPDU, so a raw length octet here garbles every such ENVELOPE.
        s, link = session((b'', b'\x90\x00'))
        s.send_envelope(b'\xaa' * 130, handle_proactive=False)
        data = link.sent[0]
        self.assertEqual(data[:4], bytes.fromhex('80c20000'))
        self.assertEqual(data[4], 0x95)                  # the D1 TLV + tag
        self.assertIn(bytes.fromhex('8b8182'), data)
        self.assertIn(bytes.fromhex('d18192'), data)

    def test_an_empty_tpdu_is_refused(self):
        s, link = session()
        with self.assertRaises(CatSessionError):
            s.send_envelope(b'')
        self.assertEqual(link.sent, [])

    def test_the_capture_prepends_the_rpi_udh(self):
        capture = PorSubmitCapture()
        self.assertEqual(capture.submit_response_hex(), '')
        capture.submit_ud_hex = 'aabb'
        self.assertEqual(capture.submit_response_hex(), '027100aabb')
        capture.submit_ud_hex = 'zz'
        self.assertEqual(capture.submit_response_hex(), '')


class EventDownloadTests(unittest.TestCase):
    def test_the_location_status_envelope(self):
        # D6 <len> 99 01 <type> 82 02 <src> 81 <objects> (TS 102 223 §7.5.3).
        s, link = session((b'', b'\x90\x00'))
        s.send_event_download(0x03, bytes.fromhex('9b0100'))
        self.assertEqual(link.hex(0),
                         '80c200000c' 'd60a99010382028281' '9b0100')

    def test_the_default_source_is_the_terminal(self):
        s, link = session((b'', b'\x90\x00'))
        s.send_event_download(0x03)
        self.assertEqual(link.hex(0), '80c2000009' 'd60799010382028281')

    def test_the_source_override(self):
        # 83 = network: the 3GPP-assigned events are network-sourced (§8.7).
        s, link = session((b'', b'\x90\x00'))
        s.send_event_download(0x03, bytes.fromhex('9b0100'), src='83')
        self.assertEqual(link.hex(0), '80c200000c' 'd60a99010382028381' '9b0100')

    def test_a_915xx_answer_is_drained(self):
        s, link = session((b'', b'\x91\x0d'), (DISPLAY_TEXT, b'\x90\x00'),
                          (b'', b'\x90\x00'), (b'', b'\x90\x00'))
        data, sw = s.send_event_download(0x03)
        self.assertEqual((data, sw), (b'', b'\x90\x00'))
        self.assertEqual(link.sent[1], bytes.fromhex('801200000d'))

    def test_drain_false_keeps_the_status_word(self):
        s, link = session((b'', b'\x91\x0d'))
        self.assertEqual(s.send_event_download(0x03, drain=False)[1],
                         b'\x91\x0d')
        self.assertEqual(len(link.sent), 1)

    def test_the_long_form_from_128_octets(self):
        # a raw length octet above 0x7F would read as a long-form indicator
        s, link = session((b'', b'\x90\x00'))
        s.send_event_download(0x03, b'\x00' * 200)
        self.assertEqual(link.sent[0][:7], bytes.fromhex('80c20000d2d681'))
        self.assertEqual(link.sent[0][7], 7 + 200)

    def test_the_one_envelope_budget_is_enforced(self):
        s, link = session()
        with self.assertRaises(CatSessionError):
            s.send_event_download(0x03, b'\x00' * (cat_session.EVENT_DATA_MAX + 1))
        self.assertEqual(link.sent, [])
        # exactly the budget still fits (a 255-byte APDU data field)
        s2, link2 = session((b'', b'\x90\x00'))
        s2.send_event_download(0x03, b'\x00' * cat_session.EVENT_DATA_MAX)
        self.assertEqual(link2.sent[0][4], 0xfe)
        self.assertEqual(len(link2.sent[0]), 5 + 254)


class PliTests(unittest.TestCase):
    def test_the_defaults(self):
        s, _link = session()
        self.assertEqual(s.pli_data_hex(0x00), '130700F11000010001')
        self.assertEqual(s.pli_data_hex(0x01), '14084A09512430325781')
        self.assertEqual(s.pli_data_hex(0x06), '3F0108')
        # a qualifier the ETSI specs leave to 3GPP has no answer
        self.assertEqual(s.pli_data_hex(0x10), '')

    def test_an_override_wins_and_clearing_restores_the_default(self):
        s, _link = session()
        s.set_pli_data(0x00, '1307' '00F110' '0002' '0001')
        self.assertEqual(s.pli_data_hex(0x00), '130700F11000020001')
        s.set_pli_data(0x00, None)
        self.assertEqual(s.pli_data_hex(0x00), '130700F11000010001')
        # bytes are accepted too, and a non-hex value is refused
        s.set_pli_data(0x0A, b'\x63\x01\x04')
        self.assertEqual(s.pli_data_hex(0x0A), '630104')
        with self.assertRaises(CatSessionError):
            s.set_pli_data(0x0A, 'zz')

    def test_the_date_time_is_answered_from_the_clock(self):
        # TS 102 223 §6.4.15: the current date/time as the 8.39 object, coded
        # like the SMS timestamp (TS 123 040 TP-SCTS): 26 07 + 7 octets.
        s, _link = session()
        dt = datetime(2024, 5, 4, 3, 2, 1,
                      tzinfo=timezone(timedelta(hours=2)))
        self.assertEqual(s.pli_data_hex(0x03, dt=dt), '260742504030201080')
        # without a dictionary entry the live clock answers, never an empty TR
        self.assertRegex(s.pli_data_hex(0x03), r'^2607[0-9A-F]{14}$')

    def test_a_dictionary_override_wins_over_the_clock(self):
        s, _link = session()
        s.set_pli_data(0x03, '260742504030201080')
        self.assertEqual(s.pli_data_hex(0x03), '260742504030201080')


class LogTests(unittest.TestCase):
    def test_the_entry_shape(self):
        s, _link = session()
        entry = s.log_proactive(0x25, LIVE_MENU, 0x00, 7)
        self.assertEqual(entry['id'], 1)
        self.assertEqual(entry['type_hex'], '25')
        self.assertEqual(entry['type_name'], 'SET UP MENU')
        self.assertEqual(entry['elapsed'], 0.0)
        self.assertEqual(entry['bytes'], len(LIVE_MENU))
        self.assertEqual(entry['raw'], LIVE_MENU.hex())
        self.assertEqual(entry['cmd_num'], 7)
        self.assertEqual(entry['qualifier'], '00')
        self.assertEqual(entry['cmd_decoded'][0]['label'], 'Items')

    def test_record_tr(self):
        s, _link = session()
        entry = s.log_proactive(0x27, TIMER_START, 0x02, 1)
        payload = bytes.fromhex('8103012702020282812401032503'
                                '417023030100')
        s.record_tr(entry, payload, b'\x90\x00')
        self.assertEqual(entry['tr_hex'], '2401032503417023')
        self.assertEqual(entry['tr_sw'], '9000')
        self.assertEqual(entry['tr_result'], '00')
        self.assertEqual(entry['tr_result_name'],
                         'Command performed successfully')
        self.assertEqual(entry['tr_decoded'],
                         [{'label': 'Timer', 'value': '3'},
                          {'label': 'Remaining', 'value': '14:07:32'}])

    def test_a_general_result_is_decoded(self):
        s, _link = session()
        entry = s.log_proactive(0x21, DISPLAY_TEXT, 0x00, 1)
        s.record_tr(entry, bytes.fromhex('81030121000202818283021200'),
                    b'\x90\x00')
        self.assertEqual(entry['tr_result'], '1200')
        self.assertEqual(entry['tr_result_name'], 'No response from user')

    def test_record_tr_tolerates_a_missing_entry(self):
        s, _link = session()
        s.record_tr(None, b'', b'\x90\x00')       # no exception

    def test_mark_session_keeps_the_history(self):
        s, _link = session()
        s.log_proactive(0x21, DISPLAY_TEXT, 0x00, 1)
        s.mark_session('terminal-profile')
        self.assertEqual(len(s.log), 2)
        self.assertEqual(s.log[1]['marker'], 'terminal-profile')
        self.assertEqual(s.log[1]['elapsed'], 0.0)
        # the elapsed clock restarts at the marker
        s.log_proactive(0x21, DISPLAY_TEXT, 0x00, 1)
        self.assertEqual(s.log[2]['elapsed'], 0.0)

    def test_entries_are_newest_first(self):
        s, _link = session()
        for number in (1, 2, 3):
            s.log_proactive(0x21, DISPLAY_TEXT, 0x00, number)
        self.assertEqual([e['cmd_num'] for e in s.entries()], [3, 2, 1])
        self.assertEqual([e['cmd_num'] for e in s.entries(limit=2)], [3, 2])
        self.assertEqual(len(s.entries(limit=0)), 3)

    def test_reset_clears_the_session_state_but_keeps_the_pli_dictionary(self):
        s, _link = session((b'', b'\x90\x00'), poll_interval=45)
        s.log_proactive(0x21, DISPLAY_TEXT, 0x00, 1)
        s.sim_menu = {'command_number': 1, 'items': []}
        s.event_list = [0x03]
        s.stk_pending = {'type': 'display_text'}
        s.menu_active = True
        s.poll_interval = 60
        s.poll_suspended = True
        s.set_pli_data(0x04, '2D02656E')
        s._timer_start(3, 100)
        self.assertIsNotNone(s._timer_remaining(3))
        s.reset()
        self.assertEqual(s.log, [])
        self.assertIsNone(s.sim_menu)
        self.assertIsNone(s.event_list)
        self.assertIsNone(s.stk_pending)
        self.assertFalse(s.menu_active)
        self.assertIsNone(s._timer_remaining(3))
        self.assertEqual(s.pli_data_hex(0x04), '2D02656E')
        # a new card has not spoken about polling: the terminal default is back
        self.assertFalse(s.poll_suspended)
        self.assertEqual(s.poll_interval, 45)


class MenuTests(unittest.TestCase):
    def test_the_display_text_pause(self):
        s, _link = session()
        response = {}
        handler = s.menu_fetch_handler(response)
        self.assertEqual(handler(DISPLAY_TEXT, 1, 0x21, 0x81, 0x82), 'pause')
        pending = s.stk_pending
        self.assertEqual(pending['type'], 'display_text')
        self.assertEqual(pending['cmd_qual'], 0x00)
        self.assertEqual(pending['cd_tag'], 0x81)
        self.assertEqual((pending['dev_src'], pending['dev_dst']), (0x81, 0x82))
        self.assertEqual(response, {'type': 'display_text', 'text': 'A'})

    def test_the_select_item_and_menu_pause(self):
        s, _link = session()
        response = {}
        self.assertEqual(s.menu_fetch_handler(response)(
            SELECT_ITEM, 1, 0x24, 0x81, 0x82), 'pause')
        self.assertEqual(s.stk_pending['items'], [{'id': 1, 'text': 'A'}])
        self.assertEqual(response['type'], 'select_item')
        response = {}
        self.assertEqual(s.menu_fetch_handler(response)(
            LIVE_MENU, 1, 0x25, 0x81, 0x82), 'pause')
        self.assertEqual(len(s.stk_pending['items']), 2)
        self.assertEqual(response['items'], s.stk_pending['items'])

    def test_the_get_inkey_and_get_input_pause(self):
        s, _link = session()
        response = {}
        self.assertEqual(s.menu_fetch_handler(response)(
            GET_INKEY, 1, 0x22, 0x81, 0x82), 'pause')
        self.assertTrue(s.stk_pending['digits_only'])
        self.assertFalse(s.stk_pending['yes_no'])
        self.assertEqual(response['text'], 'A')
        response = {}
        self.assertEqual(s.menu_fetch_handler(response)(
            LIVE_GET_INPUT, 1, 0x23, 0x81, 0x82), 'pause')
        self.assertEqual((s.stk_pending['min'], s.stk_pending['max']), (4, 16))
        self.assertTrue(s.stk_pending['hidden'])
        self.assertIsNotNone(s.stk_pending['shown_at'])

    def test_a_non_interactive_command_is_not_paused(self):
        s, _link = session()
        self.assertIsNone(s.menu_fetch_handler({})(
            POLL_INTERVAL, 1, 0x03, 0x81, 0x82))
        self.assertIsNone(s.stk_pending)

    def test_a_select_item_answer_carries_the_item(self):
        s, link = session((SELECT_ITEM, b'\x90\x00'), (b'', b'\x90\x00'))
        s.menu_active = True
        s.handle_chain(b'\x91\x0d', s.menu_fetch_handler({}))
        response = s.menu_send_response('ok', item_id=1)
        # §6.8.0 order: Result (general form), then the Item identifier
        self.assertEqual(link.hex(1),
                         '8014000010' '81030124000202828183020000' '100101')
        self.assertEqual(response, {'sw': '9000', 'type': 'done'})
        self.assertIsNone(s.stk_pending)
        self.assertFalse(s.menu_active)
        # the TR is recorded on the pending command's log entry
        self.assertEqual(s.log[0]['tr_hex'], '100101')
        self.assertEqual(s.log[0]['tr_result'], '0000')

    def test_a_cancel_answer(self):
        s, link = session((b'', b'\x90\x00'))
        s.menu_fetch_handler({})(DISPLAY_TEXT, 1, 0x21, 0x81, 0x82)
        s.menu_active = True
        response = s.menu_send_response('cancel')
        self.assertEqual(link.hex(0),
                         '801400000d' '810301210002028281' '83021000')
        self.assertEqual(response, {'sw': '9000'})
        self.assertIsNone(s.stk_pending)
        self.assertFalse(s.menu_active)

    def test_no_pending_command_is_refused(self):
        s, _link = session()
        with self.assertRaises(CatSessionError):
            s.menu_send_response('ok')

    def test_a_get_inkey_text_answer(self):
        # §6.8.5: the answer is a Text string in the requested coding (here
        # the SMS default alphabet as 8-bit characters, DCS 04).
        s, link = session((b'', b'\x90\x00'))
        s.menu_fetch_handler({})(GET_INKEY, 1, 0x22, 0x81, 0x82)
        s.menu_send_response('ok', text='7')
        self.assertEqual(link.hex(0),
                         '8014000011' '81030122000202828183020000' '0d020437')

    def test_the_input_validation(self):
        s, link = session((b'', b'\x90\x00'))
        s.menu_fetch_handler({})(GET_INKEY, 1, 0x22, 0x81, 0x82)
        for text in ('', '12', 'x'):
            with self.subTest(text=text):
                with self.assertRaises(CatSessionError):
                    s.menu_send_response('ok', text=text)
        self.assertEqual(link.sent, [])

    def test_the_get_input_bounds(self):
        s, link = session((b'', b'\x90\x00'))
        s.menu_fetch_handler({})(LIVE_GET_INPUT, 1, 0x23, 0x81, 0x82)
        with self.assertRaises(CatSessionError):
            s.menu_send_response('ok', text='123')      # below the minimum
        with self.assertRaises(CatSessionError):
            s.menu_send_response('ok', text='1' * 17)    # above the maximum
        with self.assertRaises(CatSessionError):
            s.menu_send_response('ok', text='12ab')      # digits only (hidden)
        s.menu_send_response('ok', text='1234')
        self.assertEqual(link.hex(0),
                         '8014000014' '81030123040202828183020000'
                         '0d0504' '31323334')

    def test_the_variable_timeout_duration_is_echoed(self):
        # TS 102 223 §6.4.2/§6.4.3 + §6.8.4: a variable-timeout request gets
        # the actual entry duration back in its own unit (the legacy pin:
        # 3 s -> 04 02 01 03).
        s, link = session((b'', b'\x90\x00'))
        response = {}
        s.menu_fetch_handler(response)(LIVE_GET_INPUT, 1, 0x23, 0x81, 0x82)
        s.stk_pending['duration_unit'] = 0x01
        s.stk_pending['shown_at'] = 997.0
        with mock.patch.object(cat_session.time, 'monotonic',
                               return_value=1000.0):
            s.menu_send_response('ok', text='1234')
        self.assertIn('04020103', link.hex(0))

    def test_the_help_result_is_result_13_without_text(self):
        # the legacy help TR: result 0x13, no Text string
        s, link = session((b'', b'\x90\x00'))
        s.menu_fetch_handler({})(LIVE_GET_INPUT, 1, 0x23, 0x81, 0x82)
        s.menu_send_response('help')
        self.assertIn('83021300', link.hex(0))

    def test_the_item_identifier_is_kept_for_a_successful_select_item_only(self):
        # KEPT DEVIATION (pinned): TS 102 223 §6.8.6 (p. 104) also requires
        # the Item identifier for "Help information required by the user"; the
        # legacy never sent it (and there is no PWA help path yet), so the
        # port keeps the legacy bytes.  Implementing it is cutover work.
        s, link = session((b'', b'\x90\x00'))
        s.menu_fetch_handler({})(SELECT_ITEM, 1, 0x24, 0x81, 0x82)
        s.menu_send_response('help', item_id=1)
        self.assertIn('83021300', link.hex(0))
        self.assertNotIn('100101', link.hex(0))

    def test_the_pending_dict_is_json_safe(self):
        # /api/stk-status serves pending_data as-is (shown_at stripped): every
        # value must serialise - the raw command is hex text, not bytes.
        s, _link = session()
        response = {}
        for raw, cmd_type in ((DISPLAY_TEXT, 0x21), (SELECT_ITEM, 0x24),
                              (LIVE_MENU, 0x25), (GET_INKEY, 0x22),
                              (LIVE_GET_INPUT, 0x23)):
            s.menu_fetch_handler(response)(raw, 1, cmd_type, 0x81, 0x82)
            ready = {k: v for k, v in s.stk_pending.items()
                     if k != 'shown_at'}
            json.dumps(ready)               # raises TypeError on bytes
            self.assertIsInstance(s.stk_pending['raw'], str)

    def test_the_timeout_result(self):
        s, link = session((b'', b'\x90\x00'))
        s.menu_fetch_handler({})(DISPLAY_TEXT, 1, 0x21, 0x81, 0x82)
        s.menu_timeout_fire()
        self.assertEqual(link.hex(0),
                         '801400000d' '810301210002028281' '83021200')
        self.assertIsNone(s.stk_pending)

    def test_the_answer_continues_the_chain(self):
        # The card's answer to the TR may raise the next command; the chain
        # runs again with the menu handler, so the PWA sees it.
        s, link = session((b'', b'\x91\x0d'), (LIVE_MENU, b'\x90\x00'))
        s.menu_fetch_handler({})(DISPLAY_TEXT, 1, 0x21, 0x81, 0x82)
        response = s.menu_send_response('ok')
        self.assertEqual(link.sent[1], bytes.fromhex('801200000d'))
        self.assertEqual(s.stk_pending['cmd_type'], 0x25)
        self.assertEqual(response['type'], 'select_item')
        self.assertEqual(len(response['items']), 2)

    def test_the_watchdog_is_armed_and_cancelled(self):
        s, _link = session()
        s.menu_timeout = 30
        s.arm_menu_timeout()
        timer = s._menu_timer
        self.assertIsNotNone(timer)
        self.assertTrue(timer.is_alive() or timer.finished.is_set())
        s.cancel_menu_timeout()
        self.assertIsNone(s._menu_timer)
        # 0 disables the watchdog
        s.menu_timeout = 0
        s.arm_menu_timeout()
        self.assertIsNone(s._menu_timer)

    def test_the_timeout_fire_runs_under_the_card_lock(self):
        class RecordingLock(object):
            def __init__(self):
                self.entries = 0

            def __enter__(self):
                self.entries += 1

            def __exit__(self, *exc):
                return False

        lock = RecordingLock()
        s, link = session((b'', b'\x90\x00'), card_lock=lock)
        s.menu_fetch_handler({})(DISPLAY_TEXT, 1, 0x21, 0x81, 0x82)
        s.menu_timeout_fire()
        self.assertEqual(lock.entries, 1)
        self.assertIn('83021200', link.hex(0))
        self.assertIsNone(s.stk_pending)

    def test_finish_pending_menu_answers_cancel_then_drains(self):
        s, link = session((b'', b'\x91\x0d'), (DISPLAY_TEXT, b'\x90\x00'),
                          (b'', b'\x90\x00'), (b'', b'\x90\x00'))
        s.menu_fetch_handler({})(SELECT_ITEM, 1, 0x24, 0x81, 0x82)
        s.finish_pending_menu()
        self.assertEqual(link.hex(0),
                         '801400000d' '810301240002028281' '83021000')
        # the TR's 91xx answer was drained with the generic chain
        self.assertEqual(link.sent[1], bytes.fromhex('801200000d'))
        self.assertEqual(link.sent.count(bytes.fromhex('80f2000c00')), 1)
        self.assertIsNone(s.stk_pending)

    def test_finish_pending_menu_is_a_no_op_without_a_pending_command(self):
        s, link = session()
        s.finish_pending_menu()
        self.assertEqual(link.sent, [])


class InputCodingTests(unittest.TestCase):
    def test_the_text_codings(self):
        # §6.8.5: DCS 04 (8-bit), 08 (UCS2), 00 (packed) and the null string.
        self.assertEqual(input_text_tlv({}, ''), bytes.fromhex('0d00'))
        self.assertEqual(input_text_tlv({'yes_no': True}, '01'),
                         bytes.fromhex('0d020401'))
        self.assertEqual(input_text_tlv({'digits_only': True}, '7'),
                         bytes.fromhex('0d020437'))
        self.assertEqual(input_text_tlv({'ucs2': True}, 'ab'),
                         bytes.fromhex('0d050800610062'))
        self.assertEqual(input_text_tlv({'packed': True}, 'hello'),
                         bytes.fromhex('0d0600e8329bfd06'))

    def test_the_validation_rules(self):
        self.assertEqual(validate_input_response(
            {'type': 'get_inkey', 'yes_no': True}, '01'), ('01', None))
        self.assertIsNotNone(validate_input_response(
            {'type': 'get_inkey', 'yes_no': True}, '00 ')[1])
        self.assertEqual(validate_input_response(
            {'type': 'get_inkey'}, '*'), ('*', None))
        self.assertIsNotNone(validate_input_response(
            {'type': 'get_inkey'}, '12')[1])
        self.assertEqual(validate_input_response(
            {'type': 'get_input', 'min': 2, 'max': 4}, 'abc'), ('abc', None))
        self.assertIsNotNone(validate_input_response(
            {'type': 'get_input', 'min': 2, 'max': 4}, 'a')[1])
        self.assertIsNotNone(validate_input_response(
            {'type': 'get_input', 'hidden': True}, 'a1')[1])


class TimerTests(unittest.TestCase):
    def test_a_start_arms_the_timer_and_answers(self):
        s, _link = session()
        try:
            payload = s.handle_timer_command(
                1, 0x27, 0x00, TIMER_START, 0x81, 0x82, cd_tag=0x81)
            self.assertEqual(payload.hex(), '810301270002028281030100')
            remaining = s._timer_remaining(3)
            self.assertIsNotNone(remaining)
            self.assertGreaterEqual(remaining, 50850)
        finally:
            s.cancel_timers()
        self.assertIsNone(s._timer_remaining(3))

    def test_a_get_and_a_deactivate_answer_the_remaining_value(self):
        s, _link = session()
        # 90 s = 00:01:30 as semi-octet BCD (TS 123 040)
        with mock.patch.object(s, '_timer_remaining', return_value=90):
            self.assertEqual(
                s.handle_timer_command(1, 0x27, 0x02, TIMER_START, 0x81, 0x82,
                                       cd_tag=0x81).hex(),
                '810301270202028281' '2401032503' '001003' '030100')
        with mock.patch.object(s, '_timer_remaining', return_value=None):
            # "action in contradiction with the current timer state" (§8.12.0)
            self.assertEqual(
                s.handle_timer_command(1, 0x27, 0x01, TIMER_START, 0x81, 0x82,
                                       cd_tag=0x81).hex(),
                '810301270102028281030124')

    def test_a_deactivate_cancels_a_running_timer(self):
        s, _link = session()
        s._timer_start(3, 100)
        self.assertIsNotNone(s._timer_remaining(3))
        payload = s.handle_timer_command(1, 0x27, 0x01, TIMER_START, 0x81, 0x82,
                                        cd_tag=0x81)
        self.assertTrue(payload.hex().endswith('030100'))
        self.assertIsNone(s._timer_remaining(3))

    def test_the_command_details_tag_is_echoed(self):
        s, _link = session()
        plain = bytes.fromhex('d00c010301270102028182240103')
        payload = s.handle_timer_command(1, 0x27, 0x01, plain, 0x81, 0x82)
        self.assertTrue(payload.hex().startswith('0103012701'))

    def test_the_expiry_envelope(self):
        # D7 <len> 82 02 82 81 A4 01 <id> A5 03 <h:m:s> (TS 102 223 §7.4.1).
        s, link = session((b'', b'\x90\x00'))
        s._timer_expired(5, 3723)                 # 1 h 2 min 3 s
        self.assertEqual(link.hex(0),
                         '80c200000e' 'd70c82028281a40105a503102030')

    def test_a_9300_answer_is_retried(self):
        s, link = session((b'', b'\x93\x00'), (b'', b'\x90\x00'))
        with mock.patch.object(cat_session.time, 'sleep'):
            s._timer_expired(1, 60)
        self.assertEqual(len(link.sent), 2)
        self.assertEqual(link.sent[0], link.sent[1])

    def test_the_expiry_chains_a_915xx_answer(self):
        s, link = session((b'', b'\x91\x0d'), (DISPLAY_TEXT, b'\x90\x00'),
                          (b'', b'\x90\x00'), (b'', b'\x90\x00'))
        s._timer_expired(1, 60)
        self.assertEqual(link.sent[1], bytes.fromhex('801200000d'))

    def test_a_test_run_defers_the_expiry(self):
        s, _link = session(test_running=lambda: True)
        with mock.patch.object(s, '_timer_start') as start:
            s._timer_expired(1, 60)
        start.assert_called_once_with(1, 5)

    def test_a_pending_command_defers_the_expiry(self):
        s, link = session()
        s.stk_pending = {'type': 'display_text'}
        with mock.patch.object(cat_session.time, 'sleep'), \
                mock.patch.object(s, '_timer_start') as start:
            s._timer_expired(1, 60)
        start.assert_called_once_with(1, 5)
        self.assertEqual(link.sent, [])

    def test_the_timer_identifier_is_bounded_to_the_table(self):
        # §6.6.21: up to 8 timers per session.
        s, _link = session()
        self.assertFalse(s._timer_start(0, 10))
        self.assertFalse(s._timer_start(9, 10))
        self.assertTrue(s._timer_start(8, 10))
        s.cancel_timers()

    def test_timer_fire_consumes_the_timer_and_a_cancelled_one_is_silent(self):
        s, link = session((b'', b'\x90\x00'))
        s._timer_start(3, 100)
        s._timer_fire(3, 60)                 # the fired timer is consumed
        self.assertIsNone(s._timer_remaining(3))
        self.assertEqual(link.hex(0),
                         '80c200000e' 'd70c82028281a40103a503001000')
        # a cancelled timer must not report when its callback still runs
        s._timer_start(4, 100)
        s._timer_cancel(4)
        link.sent[:] = []
        s._timer_fire(4, 60)
        self.assertEqual(link.sent, [])

    def test_the_timer_objects_stay_before_the_result(self):
        # KEPT DEVIATION (pinned): TS 102 223 §6.8.0 (p. 102) puts the Result
        # third (command details, device identities, Result, then the timer
        # objects).  The legacy emitted the timer objects before the Result
        # and the live cards accepted it, so the port keeps those bytes.
        # TODO(Phase 3c cutover): fix the order per §6.8.0, then re-run the
        # bench cards (the live timer capture) before trusting the new form.
        s, _link = session()
        with mock.patch.object(s, '_timer_remaining', return_value=90):
            payload = s.handle_timer_command(1, 0x27, 0x02, TIMER_START,
                                             0x81, 0x82, cd_tag=0x81)
        # timer id + remaining value first, the Result last
        self.assertEqual(
            payload.hex(),
            '810301270202028281' + '2401032503' + '001003' + '030100')
        self.assertLess(payload.hex().index('240103'),
                        payload.hex().index('030100'))

    def test_the_expiry_runs_under_the_card_lock(self):
        # M4: the timer thread's ENVELOPE must take the injected lock (the
        # legacy _CARD_LOCK), so it can never interleave a FETCH/TR pair.
        class RecordingLock(object):
            def __init__(self):
                self.entries = 0

            def __enter__(self):
                self.entries += 1

            def __exit__(self, *exc):
                return False

        lock = RecordingLock()
        s, link = session((b'', b'\x90\x00'), card_lock=lock)
        s._timer_expired(1, 60)
        self.assertEqual(lock.entries, 1)
        self.assertEqual(len(link.sent), 1)

    def test_the_hms_coding(self):
        self.assertEqual(cat_session.hms_bcd(3723), bytes.fromhex('102030'))
        self.assertEqual(cat_session.hms_bcd(0), bytes.fromhex('000000'))
        self.assertEqual(cat_session.hms_bcd(14 * 3600 + 7 * 60 + 32),
                         bytes.fromhex('417023'))


class DetachedSessionTests(unittest.TestCase):
    def test_the_default_hooks_are_no_ops(self):
        s, link = session((DISPLAY_TEXT, b'\x90\x00'), (b'', b'\x90\x00'),
                          (b'', b'\x90\x00'))
        s.handle_chain(b'\x91\x0d')          # no bip_flush / on_menu / test hook
        self.assertEqual(len(link.sent), 3)

    def test_a_failing_hook_never_breaks_the_chain(self):
        def boom(*_args):
            raise RuntimeError('hook')

        s, link = session((LIVE_MENU, b'\x90\x00'), (b'', b'\x90\x00'),
                          (b'', b'\x90\x00'), bip_flush=boom, on_menu=boom)
        with self.assertLogs('simple_server.cat_session', 'WARNING') as logs:
            s.handle_chain(b'\x91\x3c')
        self.assertEqual(len(logs.output), 2)
        self.assertEqual(len(link.sent), 3)
        self.assertEqual(s.sim_menu['title'], 'Alfa Mobile')

    def test_a_bad_cla_is_refused(self):
        for value in ('zz', '1234', None):
            with self.subTest(value=value):
                with self.assertRaises(CatSessionError):
                    CatSession(FakeLink(), value)

    def test_the_cla_accepts_ints_and_case(self):
        self.assertEqual(CatSession(FakeLink(), 0xA0).cat_cla, 'a0')
        self.assertEqual(CatSession(FakeLink(), 'A0').cat_cla, 'a0')
        self.assertEqual(CatSession(FakeLink(), '80').cat_cla, '80')
        # assignment is normalised too, so `'A0'` can never slip through
        s = CatSession(FakeLink())
        s.cat_cla = 'A0'
        self.assertEqual(s.cat_cla, 'a0')
        with self.assertRaises(CatSessionError):
            s.cat_cla = 'zz'

    def test_a_bad_chain_rounds_is_a_session_error(self):
        for value in ('many', None, -1):
            with self.subTest(value=value):
                with self.assertRaises(CatSessionError):
                    CatSession(FakeLink(), chain_rounds=value)


class SpecWiringTests(unittest.TestCase):
    """The chain TR is ``tr.build_tr`` in the terminal form - pinned."""

    def test_build_chain_tr_equals_the_pure_builder(self):
        s, _link = session()
        for (cmd_num, cmd_type, dev_src, dev_dst, qual, tag) in [
                (1, 0x21, 0x81, 0x82, 0x00, 0x81),
                (2, 0x40, 0x81, 0x82, 0x01, 0x81),
                (3, 0x27, 0x81, 0x82, 0x02, 0x01),
                (1, 0x00, 0x83, 0x81, None, None)]:
            answer = b''
            if cmd_type == 0x03:
                answer = bytes([0x04, 0x02, 0x01, s.poll_interval])
            elif cmd_type == 0x26 and qual is not None:
                answer = bytes.fromhex(s.pli_data_hex(qual))
            self.assertEqual(
                s.build_chain_tr(cmd_num, cmd_type, dev_src, dev_dst, qual, tag),
                tr.build_tr(cmd_num, cmd_type, dev_src, dev_dst, qual, tag, {},
                            answer_tlvs=answer, general_result=False))

    def test_the_poll_interval_is_adopted_and_echoed(self):
        s, link = session((POLL_INTERVAL, b'\x90\x00'), (b'', b'\x90\x00'),
                          (b'', b'\x90\x00'))
        s.handle_chain(b'\x91\x0d')
        self.assertEqual(s.poll_interval, 30)
        self.assertFalse(s.poll_suspended)
        self.assertTrue(link.hex(1).endswith('0402011e'))

    def test_polling_off_suspends_and_a_poll_interval_resumes(self):
        s, link = session((command(0x04, 0x00), b'\x90\x00'),
                          (b'', b'\x90\x00'), (b'', b'\x90\x00'),
                          (POLL_INTERVAL, b'\x90\x00'), (b'', b'\x90\x00'),
                          (b'', b'\x90\x00'))
        s.handle_chain(b'\x91\x0d')
        self.assertTrue(s.poll_suspended)
        s.handle_chain(b'\x91\x0d')
        self.assertFalse(s.poll_suspended)
        self.assertEqual(s.poll_interval, 30)


if __name__ == '__main__':
    unittest.main()
