# coding=utf-8
"""Tests for the proactive command type names (TS 102 223 §9.4).

The live card issues REFRESH (0x01) right after a Location status event; a
missing entry makes the proactive log show the generic 'Cmd 0x01' fallback.
"""

import unittest

from pysim_simple_server.server import PROACTIVE_TYPE_NAMES


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


if __name__ == '__main__':
    unittest.main()
