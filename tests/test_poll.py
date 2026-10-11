#!/usr/bin/env python3
"""Tests for the background STATUS polling interval semantics."""

import sys
import types
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


class FakeChattyScc:
    """A card that re-queues the same PROVIDE LOCAL INFORMATION immediately
    after every accepted TERMINAL RESPONSE - the live card's loop (v3.25.1)."""

    cat_cla = '80'

    def __init__(self):
        self.sent = []
        self._tp = types.SimpleNamespace(send_apdu=self._send)

    def _send(self, hexstr):
        self.sent.append(hexstr)
        low = hexstr.lower()
        if low.startswith('8012'):                      # FETCH: the PLI is pending
            return ('D009810301260082028182', '9000')
        if low.startswith('8014'):                      # TR: accepted
            return ('', '9000')
        if low.startswith('80f2'):                      # STATUS: re-announced
            return ('', '910b')
        return ('', '9000')


class TestProactiveChainOwnership(unittest.TestCase):
    """The auto chain must yield: a bounded number of answers per poll cycle
    and no fetching at all while a test run owns the card (v3.25.1 - the live
    1100-command loop held _CARD_LOCK and blocked a run's start)."""

    def setUp(self):
        self.saved = (S._TEST_RUNNING, S._server_ref, S._CARD_CONNECTED,
                      S._BIP_LINK_EVENTS, S._FLUSHING_CHANNEL_EVENTS)
        S._TEST_RUNNING = False

    def tearDown(self):
        (S._TEST_RUNNING, S._server_ref, S._CARD_CONNECTED,
         S._BIP_LINK_EVENTS, S._FLUSHING_CHANNEL_EVENTS) = self.saved

    def _fetches(self, scc):
        return [a for a in scc.sent if a.lower().startswith('8012')]

    def test_the_poll_chain_yields_after_the_bound(self):
        scc = FakeChattyScc()
        S._handle_proactive_chain(scc, '910b', max_rounds=3)
        self.assertEqual(len(self._fetches(scc)), 3)

    def test_the_chain_stops_when_a_run_owns_the_card(self):
        scc = FakeChattyScc()
        S._TEST_RUNNING = True
        S._handle_proactive_chain(scc, '910b')
        self.assertEqual(scc.sent, [])

    def test_the_poll_never_fetches_during_a_run_and_keeps_ticking(self):
        scc = FakeChattyScc()
        S._TEST_RUNNING = True
        S._server_ref = types.SimpleNamespace(scc=scc)
        S._POLL_ENABLED = True
        S._POLL_DISABLED_BY_CARD = False
        try:
            with mock.patch.object(S, '_reset_poll_timer') as timer:
                S._do_status_poll()
                timer.assert_called_once()
        finally:
            S._POLL_ENABLED = False
        self.assertEqual(scc.sent, [])

    def test_the_timer_expiration_waits_for_a_run(self):
        S._TEST_RUNNING = True
        S._CARD_CONNECTED = True
        S._server_ref = types.SimpleNamespace()
        with mock.patch.object(S, '_timer_start') as start:
            S._timer_expired(1, 60)
            start.assert_called_once_with(1, 5)

    def test_sim_resume_poll_rearms_only_when_enabled(self):
        S._POLL_ENABLED = True
        S._POLL_DISABLED_BY_CARD = False
        with mock.patch.object(S, '_reset_poll_timer') as timer:
            S._sim_resume_poll()
            timer.assert_called_once()
        S._POLL_DISABLED_BY_CARD = True
        with mock.patch.object(S, '_reset_poll_timer') as timer:
            S._sim_resume_poll()
            timer.assert_not_called()

    def test_data_available_flows_during_a_run(self):
        # a BIP channel only exists because the card opened it: the event is
        # the card's own conversation continuing, so it flows during a run
        # (v3.25.3 - the card must not wait for the run to end)
        ch = types.SimpleNamespace(peer_closed=False, rx=b'ABCD', id=1)
        S._server_ref = types.SimpleNamespace(scc=object(), event_list=[0x09])
        S._CARD_CONNECTED = True
        S._TEST_RUNNING = True
        with mock.patch.object(S, '_send_event_download') as send:
            ok = S._bip_data_available(ch)
        self.assertTrue(ok)
        send.assert_called_once()
        self.assertEqual(send.call_args[0][1], 0x09)

    def test_link_events_are_taken_during_a_run(self):
        # the queued link events describe the card's own channels: the flush
        # no longer stops at the run guard (v3.25.3)
        S._TEST_RUNNING = True
        S._BIP_LINK_EVENTS = False
        with mock.patch.object(S._BIP, 'take_pending_events') as take:
            S._bip_flush_channel_events(None)
        take.assert_called_once()

    def test_every_run_end_resumes_the_poll(self):
        # the two worker bodies, their two exception paths and the spawn
        # failure all call the helper
        import inspect
        src = inspect.getsource(S)
        self.assertEqual(src.count('\n    _sim_resume_poll()'), 2)
        self.assertEqual(src.count('\n        _sim_resume_poll()'), 3)


if __name__ == '__main__':
    unittest.main()
