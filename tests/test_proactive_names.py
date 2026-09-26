# coding=utf-8
"""Tests for the proactive command type names (TS 102 223 §9.4).

The live card issues REFRESH (0x01) right after a Location status event; a
missing entry makes the proactive log show the generic 'Cmd 0x01' fallback.
"""

import unittest

from pysim_simple_server.server import EVENT_NAMES, PROACTIVE_TYPE_NAMES


class ProactiveTypeNamesTests(unittest.TestCase):
    def test_refresh_is_named(self):
        self.assertEqual(PROACTIVE_TYPE_NAMES[0x01], 'REFRESH')

    def test_common_types_match_the_spec(self):
        for value, name in [
            (0x02, 'MORE TIME'), (0x03, 'POLL INTERVAL'), (0x04, 'POLLING OFF'),
            (0x05, 'SET UP EVENT LIST'), (0x10, 'SET UP CALL'),
            (0x11, 'SEND SS'), (0x12, 'SEND USSD'),
            (0x13, 'SEND SHORT MESSAGE'), (0x14, 'SEND DTMF'),
            (0x15, 'LAUNCH BROWSER'), (0x16, 'GEOGRAPHICAL LOCATION REQUEST'),
            (0x21, 'DISPLAY TEXT'), (0x24, 'SELECT ITEM'), (0x25, 'SET UP MENU'),
            (0x26, 'PROVIDE LOCAL INFORMATION'), (0x27, 'TIMER MANAGEMENT'),
            (0x28, 'SET UP IDLE MODE TEXT'), (0x40, 'OPEN CHANNEL'),
            (0x44, 'GET CHANNEL STATUS'), (0x70, 'ACTIVATE'),
        ]:
            self.assertEqual(PROACTIVE_TYPE_NAMES[value], name, hex(value))

    def test_names_are_nonempty_strings(self):
        for value, name in PROACTIVE_TYPE_NAMES.items():
            self.assertIsInstance(value, int)
            self.assertTrue(isinstance(name, str) and name, hex(value))


class EventNamesTests(unittest.TestCase):
    """Event list names follow TS 102 223 v18.3.0 8.25; the values the CAT
    spec leaves "Reserved for 3GPP" carry the TS 31.111 event name."""

    def test_reassigned_values_match_the_pinned_spec(self):
        for value, name in [
            (0x0B, 'Access technology change (single)'),
            (0x14, 'Access technology change (multiple)'),
            (0x19, 'Profile container'),
            (0x1A, 'Void'),
            (0x1B, 'Secured profile container'),
            (0x1C, 'Poll interval negotiation'),
            (0x20, 'Reserved for 3GPP (future usage)'),
        ]:
            self.assertEqual(EVENT_NAMES[value], name, hex(value))

    def test_3gpp_reserved_events_carry_their_3gpp_name(self):
        for value, fragment, clause in [
            (0x11, '(I-)WLAN access status', 'TS 31.111 7.5.1'),
            (0x12, 'Network rejection', 'TS 31.111 7.5.2'),
            (0x15, 'CSG cell selection', 'TS 31.111 7.5.3'),
            (0x17, 'IMS registration', 'TS 31.111 7.5.21'),
            (0x18, 'Incoming IMS data', 'TS 31.111 7.5.20'),
            (0x1D, 'Data connection status change', 'TS 31.111 7.5.25'),
            (0x1E, 'CAG cell selection', 'TS 31.111 7.5.26'),
            (0x1F, 'Slices status change', 'TS 31.111 7.5.27'),
        ]:
            name = EVENT_NAMES[value]
            self.assertIn('Reserved for 3GPP', name, hex(value))
            self.assertIn(fragment, name, hex(value))
            self.assertIn(clause, name, hex(value))

    def test_names_are_nonempty_strings(self):
        for value, name in EVENT_NAMES.items():
            self.assertIsInstance(value, int)
            self.assertTrue(isinstance(name, str) and name, hex(value))


if __name__ == '__main__':
    unittest.main()
