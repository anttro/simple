#!/usr/bin/env python3
"""Tests for the background STATUS polling interval semantics."""

import sys
import unittest
from pathlib import Path
from unittest import mock

PROJECTS = Path(__file__).resolve().parents[2]
PY_SIM = PROJECTS / 'pysim'
if str(PY_SIM) not in sys.path:
    sys.path.insert(0, str(PY_SIM))

import pysim_simple_server.server as S


class TestPollInterval(unittest.TestCase):
    def setUp(self):
        self.saved = (S._POLL_ENABLED, S._POLL_INTERVAL, S._POLL_TIMER)

    def tearDown(self):
        S._poll_disable()
        S._POLL_ENABLED, S._POLL_INTERVAL, S._POLL_TIMER = self.saved

    def test_zero_interval_disables_polling(self):
        S._set_poll_interval(0)
        self.assertEqual(S._POLL_INTERVAL, 0)
        with mock.patch.object(S.threading, 'Timer') as timer:
            S._poll_enable()
            timer.assert_not_called()
        self.assertFalse(S._POLL_ENABLED)
        self.assertIsNone(S._POLL_TIMER)

    def test_negative_interval_clamped_to_zero(self):
        S._set_poll_interval(-5)
        self.assertEqual(S._POLL_INTERVAL, 0)

    def test_positive_interval_starts_timer(self):
        S._set_poll_interval(30)
        with mock.patch.object(S.threading, 'Timer') as timer:
            S._poll_enable()
            self.assertTrue(S._POLL_ENABLED)
            timer.assert_called_once_with(30, S._do_status_poll)

    def test_reset_timer_skipped_when_disabled(self):
        S._set_poll_interval(0)
        S._POLL_ENABLED = True
        with mock.patch.object(S.threading, 'Timer') as timer:
            S._reset_poll_timer()
            timer.assert_not_called()
        self.assertIsNone(S._POLL_TIMER)


POLL_CMD = 'D00D810301030082028182'          # Command Details + Device Identities
MIN_2_TLV = POLL_CMD + '04020002'            # Duration: 2 minutes (plain tag)
SEC_30_TLV = POLL_CMD + '8402011E'           # Duration: 30 seconds (CR tag)


class TestCardDrivenPolling(unittest.TestCase):
    """TS 102 223 6.4.6/6.4.14: adopt the card's POLL INTERVAL duration and
    honour POLLING OFF; TS 102 223 7.5.22/8.97: act on a negotiation answer."""

    def setUp(self):
        self.saved = (S._POLL_ENABLED, S._POLL_INTERVAL, S._POLL_TIMER,
                      S._POLL_DISABLED_BY_CARD)

    def tearDown(self):
        S._poll_disable()
        (S._POLL_ENABLED, S._POLL_INTERVAL, S._POLL_TIMER,
         S._POLL_DISABLED_BY_CARD) = self.saved

    def test_duration_units_convert_to_seconds(self):
        self.assertEqual(S._duration_seconds(0x01, 30), 30)
        self.assertEqual(S._duration_seconds(0x00, 2), 120)
        self.assertEqual(S._duration_seconds(0x02, 5), 1)      # tenths, min 1 s
        self.assertEqual(S._duration_seconds(0x00, 200), 255)  # clamped
        self.assertEqual(S._duration_text(0x00, 2), '2 min')
        self.assertEqual(S._duration_text(0x01, 30), '30 s')

    def test_find_duration_accepts_both_tag_styles(self):
        self.assertEqual(S._find_duration(bytes.fromhex(SEC_30_TLV)), (1, 30))
        self.assertEqual(S._find_duration(bytes.fromhex(MIN_2_TLV)), (0, 2))
        self.assertIsNone(S._find_duration(bytes.fromhex(POLL_CMD)))

    def test_poll_interval_command_is_adopted(self):
        S._set_poll_interval(30)
        S._handle_card_poll_command(0x03, bytes.fromhex(MIN_2_TLV))
        self.assertEqual(S._POLL_INTERVAL, 120)
        self.assertFalse(S._POLL_DISABLED_BY_CARD)

    def test_polling_off_suspends_until_a_new_interval(self):
        S._POLL_ENABLED = True
        S._set_poll_interval(30)
        S._handle_card_poll_command(0x04, b'')
        self.assertTrue(S._POLL_DISABLED_BY_CARD)
        with mock.patch.object(S.threading, 'Timer') as timer:
            S._reset_poll_timer()
            timer.assert_not_called()
        with mock.patch.object(S.threading, 'Timer') as timer:
            S._handle_card_poll_command(0x03, bytes.fromhex(SEC_30_TLV))
            timer.assert_called_once_with(30, S._do_status_poll)
        self.assertFalse(S._POLL_DISABLED_BY_CARD)

    def test_status_poll_does_not_run_when_card_disabled(self):
        S._POLL_ENABLED = True
        S._POLL_DISABLED_BY_CARD = True
        with mock.patch.object(S, '_send_status') as send, \
                mock.patch.object(S.threading, 'Timer') as timer:
            S._do_status_poll()
            send.assert_not_called()
            timer.assert_not_called()

    def test_negotiation_decode_and_modified_apply(self):
        neg = S._decode_poll_negotiation('1101020402011E')     # modified, 30 s
        self.assertEqual(neg['result_name'], 'modified')
        self.assertEqual(neg['seconds'], 30)
        self.assertEqual(S._decode_poll_negotiation('')['result_name'], 'accepted')
        self.assertEqual(S._decode_poll_negotiation('110100')['result_name'], 'accepted')
        self.assertEqual(S._decode_poll_negotiation('11010104020102')['result_name'], 'rejected')
        self.assertIsNone(S._decode_poll_negotiation('1102'))   # truncated result
        # a 'modified' answer becomes the new poll interval
        S._set_poll_interval(50)
        S._POLL_ENABLED = True
        with mock.patch.object(S.threading, 'Timer') as timer:
            S._apply_poll_negotiation(neg)
            timer.assert_called_once_with(30, S._do_status_poll)
        self.assertEqual(S._POLL_INTERVAL, 30)
        # a 'rejected' answer leaves the current interval alone
        S._apply_poll_negotiation(S._decode_poll_negotiation('11010104020102'))
        self.assertEqual(S._POLL_INTERVAL, 30)

    def test_poll_keeps_ticking_without_a_card(self):
        S._POLL_ENABLED = True
        S._POLL_DISABLED_BY_CARD = False
        S._set_poll_interval(30)
        saved_ref = S._server_ref
        S._server_ref = None
        try:
            with mock.patch.object(S.threading, 'Timer') as timer:
                S._do_status_poll()
                timer.assert_called_once_with(30, S._do_status_poll)
        finally:
            S._server_ref = saved_ref

    def test_polling_is_enabled_by_default(self):
        import subprocess
        code = 'import pysim_simple_server.server as s; print(s._POLL_ENABLED)'
        rv = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True,
                            cwd=str(PROJECTS / 'simple'))
        self.assertEqual(rv.stdout.strip(), 'True', rv.stderr)

    def test_poll_interval_decoded_in_the_proactive_log(self):
        fields = S._decode_cmd(0x03, bytes.fromhex(MIN_2_TLV), 0)
        self.assertEqual(fields[0]['value'], '2 min (120 s)')
        fields = S._decode_cmd(0x03, bytes.fromhex(SEC_30_TLV), 0)
        self.assertEqual(fields[0]['value'], '30 s')


if __name__ == '__main__':
    unittest.main()
