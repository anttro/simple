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
import sys
import unittest

PROJECTS = pathlib.Path(__file__).resolve().parents[2]
PY_SIM = PROJECTS / 'pysim'
if str(PY_SIM) not in sys.path:
    sys.path.insert(0, str(PY_SIM))

import pysim_simple_server.server as legacy  # noqa: E402
import pysim_simple_server.testscript as legacy_ts  # noqa: E402

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


if __name__ == '__main__':
    unittest.main()
