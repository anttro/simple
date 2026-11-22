# coding=utf-8
"""Tests for :mod:`simple_server.proactive` and :mod:`simple_server.tr`.

The command fixtures and their expected decodes are the ones the existing
server's suite pins (``tests/test_ota_helpers.py``, ``tests/test_testscript.py``,
``tests/test_tag_variants.py``), copied as literals so this suite stays free of
any pysim/pysim_simple_server import: the live Alfa SET UP MENU, the live
RemMobileID GET INPUT, the reference-trace OPEN CHANNEL/SEND DATA, the
live SEND SHORT MESSAGE with its long-form 150-octet TPDU, the SEND SHORT
MESSAGE TPDU family, the TERMINAL RESPONSE builders' byte pins and the
PROVIDE LOCAL INFORMATION answers.  No new captures were taken for the port -
every fixture is an existing live/reference capture, so a decoder change is
always measured against card data, not against a re-encode of itself.

Every fixture is decoded in both tag styles: bit 8 of a COMPREHENSION-TLV tag is
the comprehension-required flag (TS 101 220 §7.1.1) and real cards mix the
plain and CR-set variants.
"""

import ast
import json
import pathlib
import unittest

from simple_server import proactive as P
from simple_server import tr as R
from simple_server import tlv


def cmd_raw(cmd_type, qualifier, extras=b''):
    """A D0-wrapped proactive command (command details + device ids + extras)."""
    body = (bytes([0x81, 0x03, 0x01, cmd_type, qualifier])
            + bytes([0x82, 0x02, 0x83, 0x81]) + extras)
    return bytes([0xD0, len(body)]) + body


def flip_tags(raw):
    """Return the command with every top-level TLV tag's CR flag flipped."""
    raw = bytes(raw)
    out = bytearray(raw)
    off = tlv.parse_ber_len(raw, 1)[1] if raw and raw[0] == 0xD0 else 0
    while off + 1 < len(out):
        out[off] ^= 0x80
        length, value_off = tlv.parse_ber_len(bytes(out), off + 1)
        if length == 0 and bytes(out)[off + 1] == 0x80:
            break                                   # indefinite length: stop
        off = value_off + length
    return bytes(out)


# The live Alfa card's SET UP MENU (2026-09-29): CR-set title 0x85, items 0x8F.
LIVE_MENU = bytes.fromhex(
    'd03c810301250082028182850b416c6661204d6f62696c65'
    '8f16808112089db0c1c2c0beb9bab82f53657474696e6773'
    '8f0881810400d4e5f3f418020021')
# The live RemMobileID GET INPUT (2026-09-29): qualifier 04 = digits + hidden.
LIVE_GET_INPUT = bytes.fromhex(
    'd03c8103012304820281828d2d0804120432043504340438044204350020004d006f00'
    '620069006c0065002d00490044002000500049004e003191020410')
# The reference traces' OPEN CHANNEL (CR-set objects) and SEND DATA.
OPEN_CHANNEL = bytes.fromhex(
    'd02b8103014001820281828500b50103b9020200c70b076d656761666f6e2e7275'
    'bc03021f90be05217f000001')
SEND_DATA = bytes.fromhex('d00e8103014301820281213701013603aabbcc')
# A malformed SEND SHORT MESSAGE (a garbage TPDU) and the timer commands.
SEND_SM_GARBAGE = bytes.fromhex(
    'd0158103011300820283818b0b916106152670f900a35f020101')
TIMER_CR = bytes.fromhex('d011810301270082028182a40103a503417023')
TIMER_PLAIN = bytes.fromhex('d00c010301270102028182240103')

# The live card's SEND SHORT MESSAGE with a BER long-form TLV (v3.6.57): the
# template's own length is `D0 81 AB` and the SMS TPDU is `8B 81 96`, i.e. 150
# octets.  `tests/test_ota_helpers.py`'s `ProactiveLongTlvTests` capture, copied
# as literals - reading a length as one octet walked into the TPDU and indexed
# past the end (`IndexError: index out of range`, live 2026-09-30, a 500 in the
# FETCH path with the card's command left unanswered).
LONG_TPDU = bytes.fromhex(
    '410005812143f500f68c0700030302017100008d0a00000000000000bb0000029000e3284f10'
    'a1130001180001ffffffff89a10039009f7001018410a1130001180001ffffffff89a1003908'
    'e33a4f10a1130001180002fff7100e89040002009f7001018410a1130001180002fff7100e'
    '89040002088410a1130001180002fff7100e89494d4508e3174f07f0414c464160019f7001')
# the FETCH shape: command details + device identities + address object + the
# TPDU padded out to its declared 150 octets
LONG_RAW = (bytes.fromhex('d081ab8103011300820281838607919733824009f08b8196')
            + LONG_TPDU + bytes(range(len(LONG_TPDU), 150)))
LONG_RAW_TPDU = (LONG_TPDU + bytes(range(len(LONG_TPDU), 150))).hex().upper()
# the long-form DISPLAY TEXT: `8D 81 90` = the DCS octet + 143 octets of text
LONG_DISPLAY = (bytes.fromhex('d08198') + bytes([0x81, 0x03, 0x01, 0x21, 0x00])
                + bytes([0x8D, 0x81, 0x90]) + b'\x00' * 0x90)


class HeaderTests(unittest.TestCase):
    def test_plain_and_cr_set_tags(self):
        # The command-details tag follows the style the card used; the TR
        # echoes it (TS 102 223 §6.8.1).
        self.assertEqual(
            P.parse_proactive_header(bytes.fromhex('d00c010301270102028182240103')),
            (1, 0x27, 0x81, 0x82, 0x01, 0x01))
        self.assertEqual(P.parse_proactive_header(TIMER_CR),
                         (1, 0x27, 0x81, 0x82, 0x00, 0x81))

    def test_qualifier_and_device_identities(self):
        self.assertEqual(
            P.parse_proactive_header(bytes.fromhex('d00e8103014301820281213701013603aabbcc')),
            (1, 0x43, 0x81, 0x21, 0x01, 0x81))
        self.assertEqual(P.parse_proactive_header(OPEN_CHANNEL)[4], 0x01)

    def test_defaults_without_a_template(self):
        # No D0 wrapper: the documented defaults, never an exception.
        self.assertEqual(P.parse_proactive_header(b''), (1, 0, 0x83, 0x81, None, None))
        self.assertEqual(P.parse_proactive_header(b'\x00'), (1, 0, 0x83, 0x81, None, None))


class BerLongFormTests(unittest.TestCase):
    """BER long-form lengths in the command TLVs (v3.6.57).

    The whole point of the TLV walks is the live `D0 81 ab ... 8B 81 96`
    template: a single-octet length read walks into the 150-byte SMS TPDU.
    """

    def test_the_header_parses_the_long_form_template(self):
        self.assertEqual(P.parse_proactive_header(LONG_RAW),
                         (1, 0x13, 0x81, 0x83, 0, 0x81))

    def test_the_long_tlv_is_one_object_not_a_walk_into_its_value(self):
        tlvs = P._command_tlv_map(LONG_RAW)
        self.assertEqual(sorted(tlvs), [0x81, 0x82, 0x86, 0x8B])
        # every object carries its whole value: the TPDU is the 150 octets
        # the long-form length declares, not 0x81 = 129 read as a length
        self.assertEqual({tag: len(value) for tag, value in tlvs.items()},
                         {0x81: 3, 0x82: 2, 0x86: 7, 0x8B: 150})

    def test_the_150_octet_tpdu_decodes_whole(self):
        rows = P.decode_send_sm(LONG_RAW)
        self.assertEqual([r for r in rows if r['label'] == 'SMS TPDU'],
                         [{'label': 'SMS TPDU', 'value': LONG_RAW_TPDU}])
        self.assertEqual(len(LONG_RAW_TPDU), 150 * 2)
        # the log path decodes the same command
        self.assertEqual(P.decode_proactive_command(0x13, LONG_RAW, 0), rows)

    def test_the_long_form_display_text(self):
        text = P.parse_display_text(LONG_DISPLAY)
        self.assertIsNotNone(text)
        # the whole 0x90-octet value was read (144 octets minus the DCS byte)
        self.assertEqual(len(text), 143)
        self.assertEqual(P.decode_proactive_command(0x21, LONG_DISPLAY, 0),
                         [{'label': 'Text', 'value': text}])


class TemplateToleranceTests(unittest.TestCase):
    """The documented `_proactive_body` / TLV-walk tolerances, pinned.

    These are the Phase-3b deviations from the legacy byte scans: the region is
    everything after the `D0` length (a card's template length is sometimes off
    by a byte) and the objects are walked as TLVs, so a tag-like octet inside a
    value is a value octet.
    """

    BODY = (bytes([0x81, 0x03, 0x01, 0x25, 0x00])
            + bytes([0x82, 0x02, 0x83, 0x81])
            + bytes.fromhex('8F0A0180004D0065006E0075'))

    def test_a_wrong_template_length_does_not_clip_the_objects(self):
        for declared in (len(self.BODY) - 1, len(self.BODY) + 1):
            raw = bytes([0xD0, declared]) + self.BODY
            self.assertEqual(P.parse_setup_menu_command(raw)['items'],
                             [{'id': 1, 'text': 'Menu'}],
                             'declared length %d' % declared)

    def test_a_tag_like_byte_inside_a_value_is_not_a_tlv(self):
        # The legacy SET UP EVENT LIST decode byte-scanned the buffer
        # (`raw.find(b'\x99')`), so this 0x99 inside the alpha identifier's
        # value was read as the event-list tag - with the next octet as its
        # length.  The TLV walk sees the real object.
        extras = (bytes.fromhex('0504') + bytes([0x99, 0x01, 0x02, 0x03])
                  + bytes.fromhex('9902030B'))
        raw = cmd_raw(0x05, 0x00, extras)
        self.assertEqual(P.decode_proactive_command(0x05, raw, 0),
                         [{'label': 'Events', 'value':
                           'Location status, Access technology change '
                           '(single access technology)'}])


class DecodeCommandTests(unittest.TestCase):
    def test_poll_interval(self):
        self.assertEqual(
            P.decode_proactive_command(0x03, bytes.fromhex('d00d8103010300820283818402011e'), None),
            [{'label': 'Interval', 'value': '30 s'}])
        # minutes are converted and clamped to the 1..255 s TR Duration
        self.assertEqual(
            P.decode_proactive_command(0x03, bytes.fromhex('d00d81030103008202838184020005'), None),
            [{'label': 'Interval', 'value': '5 min (255 s)'}])
        # no Duration object: nothing to report
        self.assertEqual(P.decode_proactive_command(0x03, bytes.fromhex('d009810301030082028381'), None), [])

    def test_set_up_event_list(self):
        self.assertEqual(
            P.decode_proactive_command(0x05, bytes.fromhex('d00c810301050082028381990101'), None),
            [{'label': 'Events', 'value': 'Call connected'}])

    def test_set_up_menu_with_next_action_indicator(self):
        # TS 102 223 §8.24: one NAI byte per item, in item order (UCS2 text).
        extras = bytes.fromhex(
            '8F0A0180004D0065006E0075'      # 1: 'Menu'
            '8F0A028000430061006C006C'      # 2: 'Call'
            '18022510')                     # NAI: SET UP MENU, SET UP CALL
        raw = cmd_raw(0x25, 0x00, extras)
        self.assertEqual(P.decode_proactive_command(0x25, raw, 0x00),
                         [{'label': 'Items', 'value':
                           '1. Menu \u2192 SET UP MENU, 2. Call \u2192 SET UP CALL'}])
        # '26' (PROVIDE LOCAL INFORMATION) is Type-of-Command only, so it is
        # reserved for the NAI and shall be ignored (§8.24).
        reserved = cmd_raw(0x25, 0x00, bytes.fromhex('8F0A0180004D0065006E0075' '180126'))
        self.assertEqual(P.decode_proactive_command(0x25, reserved, 0x00),
                         [{'label': 'Items', 'value': '1. Menu'}])

    def test_select_item_with_a_short_next_action_list(self):
        extras = bytes.fromhex(
            '8F0A0180004D0065006E0075'
            '8F0A028000430061006C006C'
            '180121')
        raw = cmd_raw(0x24, 0x00, extras)
        items = P.parse_select_item(raw)
        self.assertEqual(items[0]['nai_name'], 'DISPLAY TEXT')
        self.assertNotIn('nai_name', items[1])
        self.assertEqual(P.decode_proactive_command(0x24, raw, 0x00),
                         [{'label': 'Items', 'value': '1. Menu \u2192 DISPLAY TEXT, 2. Call'}])

    def test_display_text(self):
        self.assertEqual(
            P.decode_proactive_command(0x21, bytes.fromhex('d00d8103012100820281828d020441'), 0x00),
            [{'label': 'Text', 'value': 'A'}])
        self.assertEqual(P.decode_proactive_command(0x21, bytes.fromhex('d009810301210082028182'), 0x00), [])

    def test_get_input_reads_text_response_length_and_flags(self):
        decoded = P.decode_proactive_command(0x23, LIVE_GET_INPUT, 0x04)
        self.assertEqual(decoded[0], {'label': 'Text', 'value': 'Введите Mobile-ID PIN1'})
        self.assertEqual(decoded[1], {'label': 'Length', 'value': '4..16'})
        self.assertEqual(decoded[2], {'label': 'Response', 'value': 'digits only, hidden'})
        info = P.parse_get_input(LIVE_GET_INPUT)
        self.assertEqual((info['min'], info['max']), (4, 16))
        self.assertIsNone(info['default'])
        # max 0xFF = no maximum (TS 102 223 §8.11); a help flag is reported too
        raw = bytes.fromhex('d0118103012381820281828d020041910200ff')
        decoded = P.decode_proactive_command(0x23, raw, 0x81)
        self.assertIn({'label': 'Length', 'value': '0..no max'}, decoded)
        self.assertEqual(decoded[-1], {'label': 'Response', 'value': 'SMS default alphabet, help'})
        # a template without a Text string is not a parseable input command
        self.assertIsNone(P.parse_get_input(bytes.fromhex('d0038103012300')))

    def test_get_inkey_reads_the_qualifier_flags(self):
        raw = bytes.fromhex('d0188103012284820281828d0d0804120435044004350434043a0430')
        info = P.parse_get_inkey(raw)
        self.assertTrue(info['digits_only'])
        self.assertTrue(info['yes_no'])
        self.assertTrue(info['help'])
        self.assertFalse(info['ucs2'] or info['immediate'])
        # yes/no wins the kind label, then the help flag
        self.assertEqual(P.decode_proactive_command(0x22, raw, 0x84)[-1],
                         {'label': 'Response', 'value': 'Yes/No, help'})
        self.assertIsNone(P.parse_get_inkey(bytes.fromhex('d0038103012200')))

    def test_provide_local_information_names_every_standard_qualifier(self):
        for qualifier, name in ((0x00, 'Location Information'), (0x05, 'Reserved for GSM'),
                                (0x07, 'ESN'), (0x0B, 'MEID'),
                                (0x1A, 'Supported Radio Access Technologies')):
            rows = P.decode_proactive_command(0x26, b'\xd0', qualifier)
            self.assertIn(name, rows[0]['value'], 'qualifier 0x%02X' % qualifier)
            self.assertIn('(0x%02X)' % qualifier, rows[0]['value'])
        self.assertEqual(P.decode_proactive_command(
            0x26, bytes.fromhex('d009810301260082028182'), None),
            [{'label': 'Data', 'value': 'd009810301260082028182'}])

    def test_timer_management_start(self):
        # TS 102 223 §6.6.21/§8.37/§8.38: start timer 3 for 14:07:32
        self.assertEqual(P.decode_proactive_command(0x27, TIMER_CR, 0x00), [
            {'label': 'Action', 'value': 'Start'},
            {'label': 'Timer', 'value': '3'},
            {'label': 'Value', 'value': '14:07:32'},
        ])
        # Cards may use the plain (non comprehension-required) tag variant.
        self.assertEqual(P.decode_proactive_command(0x27, TIMER_PLAIN, 0x01), [
            {'label': 'Action', 'value': 'Deactivate'},
            {'label': 'Timer', 'value': '3'},
        ])
        # A timer identifier outside 1..8 is named as invalid (§8.37).
        self.assertEqual(P.decode_proactive_command(
            0x27, bytes.fromhex('d010810301270082028182a4010fa503417023'), 0x00)[1],
            {'label': 'Timer', 'value': 'Invalid (0x0F)'})

    def test_open_channel_objects(self):
        self.assertEqual(P.decode_proactive_command(0x40, OPEN_CHANNEL, 0x01), [
            {'label': 'Bearer', 'value': '0x03'},
            {'label': 'Buffer size', 'value': '512'},
            {'label': 'APN', 'value': 'megafon.ru'},
            {'label': 'Destination', 'value': '127.0.0.1'},
            {'label': 'Transport', 'value': 'TCP client port 8080'},
        ])

    def test_send_data_channel_and_payload(self):
        # Real trace: SEND DATA carries the channel in the device identities
        # (source UICC 0x81, destination Channel 1 0x21).
        self.assertEqual(P.decode_proactive_command(0x43, SEND_DATA, 0x01), [
            {'label': 'Channel', 'value': '1'},
            {'label': 'Data bytes', 'value': '3'},
            {'label': 'Data', 'value': 'aabbcc'},
        ])

    def test_receive_data_requested_length(self):
        raw = bytes.fromhex('d00e810301420182028121370101')
        self.assertEqual(P.decode_proactive_command(0x42, raw, 0x01), [
            {'label': 'Channel', 'value': '1'},
            {'label': 'Requested bytes', 'value': '1'},
        ])

    def test_other_address_forms(self):
        # TS 102 223 §8.58: 21 IPv4, 57 IPv6, F0 FQDN.
        self.assertEqual(P.parse_other_address(bytes.fromhex('217F000001')),
                         '127.0.0.1')
        self.assertEqual(
            P.parse_other_address(bytes.fromhex('57')
                                  + bytes.fromhex('20010DB8000000000000000000000001')),
            '2001:db8:0:0:0:0:0:1')
        self.assertEqual(P.parse_other_address(bytes.fromhex('F0') + b'localhost'),
                         'localhost')
        # a truncated object and an unknown address type have no decode
        self.assertIsNone(P.parse_other_address(bytes.fromhex('5700010DB8')))
        self.assertIsNone(P.parse_other_address(bytes.fromhex('2107AA')))
        self.assertIsNone(P.parse_other_address(b''))

    def test_transport_level(self):
        # TS 102 223 §8.59: the transport protocol octet + a 2-octet port.
        self.assertEqual(P.parse_transport_level(bytes.fromhex('021F90')), (2, 8080))
        self.assertEqual(P.parse_transport_level(bytes.fromhex('02')), (None, None))
        self.assertEqual(P.parse_transport_level(b''), (None, None))

    def test_commands_without_a_decoder_fall_back_to_the_raw_data(self):
        raw = bytes.fromhex('d009810301040082028182')
        self.assertEqual(P.decode_proactive_command(0x04, raw, 0x00),
                         [{'label': 'Data', 'value': raw.hex()}])
        self.assertEqual(P.decode_proactive_command(0x01, raw, 0x00),
                         [{'label': 'Data', 'value': raw.hex()}])

    def test_empty_raw(self):
        self.assertEqual(P.decode_proactive_command(0x26, b'', None), [])
        self.assertEqual(P.decode_proactive_command(0x03, None, None), [])

    def test_a_bad_hex_string_never_raises_in_the_log_path(self):
        # the proactive log must render whatever the FETCH returned; hex that
        # does not parse is treated like an empty buffer, not an exception
        self.assertEqual(P.decode_proactive_command(0x26, 'zz', 0x00), [])
        self.assertEqual(P.decode_proactive_command(0x13, 'd0 0', 0x00), [])


class SendShortMessageTests(unittest.TestCase):
    TPDU = bytes.fromhex('010006912143F5000005E8329BFD06')

    def _decode(self, tpdu):
        return {row['label']: row['value'] for row in P.decode_send_sm(
            cmd_raw(0x13, 0, bytes([0x8B, len(tpdu)]) + tpdu))}

    def test_gsm7_submit(self):
        rows = P.decode_send_sm(cmd_raw(0x13, 0, bytes([0x8B, len(self.TPDU)]) + self.TPDU))
        self.assertEqual(rows[0], {'label': 'SMS TPDU', 'value': self.TPDU.hex().upper()})
        decoded = self._decode(self.TPDU)
        self.assertEqual(decoded['Type'], 'SMS-SUBMIT')
        self.assertEqual(decoded['TP-MR'], '0')
        self.assertEqual(decoded['TP-DA'], '12345')
        self.assertEqual(decoded['TP-PID'], '0x00')
        self.assertEqual(decoded['TP-DCS'], '0x00')
        self.assertEqual(decoded['TP-UDL'], '5')
        self.assertEqual(decoded['Text'], 'hello')

    def test_udh_concatenation_and_8bit_text(self):
        udh = bytes.fromhex('060804000102034142')
        tpdu = bytes.fromhex('4100' '06912143F5' '00' '04' '09') + udh
        decoded = self._decode(tpdu)
        self.assertEqual(decoded['Concat (16-bit ref)'], '1, part 2/3')
        self.assertEqual(decoded['Text'], 'AB')

    def test_ucs2_text(self):
        text = 'Тест'.encode('utf-16-be')
        tpdu = (bytes.fromhex('0100' '06912143F5' '00' '08' '%02X' % len(text)) + text)
        self.assertEqual(self._decode(tpdu)['Text'], 'Тест')

    def test_secured_packet_user_data(self):
        # PID 0x7F = SIM data download: the UD is a secured packet (TS 31.115).
        tpdu = bytes.fromhex('0100' '06912143F5' '7F' 'F6' '03' 'AABBCC')
        decoded = self._decode(tpdu)
        self.assertEqual(decoded['TP-PID'], '0x7F (SIM data download)')
        self.assertEqual(decoded['Secured packet (TS 31.115)'], '3 bytes: AABBCC')

    def test_malformed_tpdu_keeps_the_raw_row(self):
        # a TPDU too short to carry an SMS-SUBMIT header keeps its raw row
        # (plus the one field its first octet carries) and never raises into
        # the log
        self.assertEqual(
            P.decode_send_sm(cmd_raw(0x13, 0, bytes.fromhex('8B0101'))),
            [{'label': 'SMS TPDU', 'value': '01'},
             {'label': 'Type', 'value': 'SMS-SUBMIT'}])
        # the garbage-TPDU fixture keeps its raw row first, then the fields
        # the partial parse does read (a stricter parser - pySim's - refuses
        # the whole TPDU; the log keeps the raw row either way)
        rows = P.decode_send_sm(SEND_SM_GARBAGE)
        self.assertEqual(rows[0], {'label': 'SMS TPDU', 'value': '916106152670F900A35F02'})
        self.assertEqual([r['label'] for r in rows],
                         ['SMS TPDU', 'Type', 'TP-MR', 'TP-DA', 'TP-PID',
                          'TP-DCS', 'TP-VP', 'TP-UDL', 'Text'])

    def test_an_alphanumeric_address_keeps_uppercase_hex_digits(self):
        # A TON 5 (alphanumeric) address carries no BCD: the digits are the
        # nibbles printed as hex.  They stay uppercase here (the form
        # `simple_server.sms.decode_tp_address` documents) - type octet D1 =
        # TON 5 + ISDN, then A B 1 2 nibble-swapped.
        address = bytes.fromhex('D1BA21')
        raw = cmd_raw(0x13, 0, bytes([0x86, len(address)]) + address)
        self.assertEqual(P.decode_send_sm(raw),
                         [{'label': 'Address', 'value': 'AB12 (alphanumeric)'}])

    def test_alpha_identifier(self):
        rows = P.decode_send_sm(cmd_raw(
            0x13, 0, bytes.fromhex('0503414243') + bytes([0x8B, len(self.TPDU)]) + self.TPDU))
        self.assertEqual(rows[0], {'label': 'Alpha', 'value': 'ABC'})

    def test_a_command_without_a_tpdu(self):
        rows = P.decode_send_sm(cmd_raw(0x13, 0, bytes.fromhex('0503414243')))
        self.assertEqual(rows, [{'label': 'Alpha', 'value': 'ABC'}])


class SetUpMenuCommandTests(unittest.TestCase):
    def test_live_card_title_and_items(self):
        menu = P.parse_setup_menu_command(LIVE_MENU)
        self.assertEqual(menu['command_number'], 1)
        self.assertEqual(menu['title'], 'Alfa Mobile')
        self.assertEqual(menu['items'][0], {'id': 0x80, 'text': 'Настройки/Settings'})
        self.assertEqual(menu['items'][1],
                         {'id': 0x81, 'text': 'Test', 'nai': 0x21, 'nai_name': 'DISPLAY TEXT'})

    def test_plain_tag_variants_are_accepted(self):
        # the same title as the plain tag 0x05 and an empty plain item object
        raw = bytes.fromhex('d012810301250082028182850b416c6661204d6f62696c650f00')
        self.assertEqual(P.parse_setup_menu_command(raw),
                         {'command_number': 1, 'title': 'Alfa Mobile', 'items': []})

    def test_a_command_that_is_not_a_menu_has_no_menu(self):
        self.assertIsNone(P.parse_setup_menu_command(bytes.fromhex('d009810301260182028182')))
        self.assertIsNone(P.parse_setup_menu_command(b''))


class PollNegotiationTests(unittest.TestCase):
    def test_no_response_data_means_accepted(self):
        self.assertEqual(P.decode_poll_negotiation(''), {
            'result': 0, 'result_name': 'accepted', 'unit': None,
            'interval': None, 'seconds': None})
        self.assertEqual(P.decode_poll_negotiation(b''),
                         P.decode_poll_negotiation(''))

    def test_modified_or_rejected_with_a_duration(self):
        self.assertEqual(P.decode_poll_negotiation('1101020402011E'), {
            'result': 2, 'result_name': 'modified', 'unit': 1,
            'interval': 30, 'seconds': 30})
        self.assertEqual(P.decode_poll_negotiation('11010104020102')['result_name'], 'rejected')
        self.assertEqual(P.decode_poll_negotiation('110100')['result_name'], 'accepted')

    def test_malformed_data(self):
        self.assertIsNone(P.decode_poll_negotiation('1102'))      # truncated result
        self.assertIsNone(P.decode_poll_negotiation('zz'))        # not hex


class TagVariantTests(unittest.TestCase):
    """Every parser must accept both tag styles (TS 101 220 §7.1.1)."""

    FIXTURES = [
        ('SET UP MENU items', LIVE_MENU, P.parse_setup_menu_items),
        ('SET UP MENU command', LIVE_MENU, P.parse_setup_menu_command),
        ('SELECT ITEM', bytes.fromhex('d00d8103012400820281828f020141'), P.parse_select_item),
        ('DISPLAY TEXT', bytes.fromhex('d00d8103012100820281828d020441'), P.parse_display_text),
        ('GET INPUT (live)', LIVE_GET_INPUT, P.parse_get_input),
        ('GET INKEY', bytes.fromhex('d00e8103012204820281828d03080041'), P.parse_get_inkey),
        ('POLL INTERVAL duration', bytes.fromhex('d00d8103010300820283818402011e'), P.find_duration),
        ('SEND SHORT MESSAGE', SEND_SM_GARBAGE, P.decode_send_sm),
        ('decode SET UP MENU', LIVE_MENU, lambda raw: P.decode_proactive_command(0x25, raw, 0x00)),
        ('decode GET INPUT', LIVE_GET_INPUT, lambda raw: P.decode_proactive_command(0x23, raw, 0x04)),
        ('decode OPEN CHANNEL', OPEN_CHANNEL, lambda raw: P.decode_proactive_command(0x40, raw, 0x01)),
        ('decode SEND DATA', SEND_DATA, lambda raw: P.decode_proactive_command(0x43, raw, 0x01)),
        ('decode TIMER', TIMER_CR, lambda raw: P.decode_proactive_command(0x27, raw, 0x00)),
    ]

    def test_flipping_every_tag_keeps_the_parse_result(self):
        for label, raw, parser in self.FIXTURES:
            with self.subTest(label=label):
                self.assertEqual(parser(raw), parser(flip_tags(raw)))
                self.assertNotEqual(raw, flip_tags(raw), 'fixture has no TLVs?')

    def test_the_header_parser_follows_the_tag_style(self):
        plain = P.parse_proactive_header(TIMER_PLAIN)
        flipped = P.parse_proactive_header(flip_tags(TIMER_PLAIN))
        self.assertEqual(plain[:5], flipped[:5])
        self.assertEqual((plain[5], flipped[5]), (0x01, 0x81))


class BuildTrTests(unittest.TestCase):
    # The byte pins below are the ones the existing suite holds for
    # `testscript.build_tr` (general Result form) and `server._build_tr`
    # (basic Result form) - TS 102 223 §6.8.0/§6.8.1.

    def test_text_codings_and_raw_extras(self):
        tr = R.build_tr(4, 0x23, 0x82, 0x81, 0x00, 0x81,
                        {'result': 0, 'text': 'hello', 'dcs': 0, 'raw': 'AA01BB'})
        self.assertEqual(tr.hex().upper(),
                         '8103042300' + '02028182' + '83020000'
                         + '0D0600' + 'E8329BFD06' + 'AA01BB')
        self.assertEqual(
            R.build_tr(4, 0x23, 0x82, 0x81, 0x00, 0x81,
                       {'result': 0, 'text': 'hello', 'dcs': 4}).hex().upper(),
            '8103042300' + '02028182' + '83020000' + '0D0604' + '68656C6C6F')
        # UCS2 (UTF-16-BE): 'A' U+0041, 'Ж' U+0416
        self.assertEqual(
            R.build_tr(4, 0x23, 0x82, 0x81, 0x00, 0x81,
                       {'result': 0, 'text': 'A\u0416', 'dcs': 8}).hex().upper(),
            '8103042300' + '02028182' + '83020000' + '0D0508' + '00410416')
        # an empty answer is the null text string (Length 00, no DCS)
        self.assertEqual(
            R.build_tr(4, 0x23, 0x82, 0x81, 0x00, 0x81,
                       {'result': 0, 'text': ''}).hex().upper(),
            '8103042300' + '02028182' + '83020000' + '0D00')

    def test_a_long_text_uses_the_ber_long_form_length(self):
        # TS 101 220 §7.1.2 Table 7.6: a Text string of 128 octets or more
        # takes `81 nn` (the legacy emitted a raw octet, unreadable by a BER
        # parser).
        text = 'A' * 130
        tr = R.build_tr(1, 0x21, 0x81, 0x82, None, None,
                        {'result': 0, 'text': text, 'dcs': 4})
        self.assertTrue(tr.hex().upper().endswith(
            '0D' + '81' + '83' + '04' + '41' * 130))

    def test_item_identifier_follows_a_successful_result_only(self):
        self.assertEqual(
            R.build_tr(3, 0x24, 0x81, 0x82, None, None,
                       {'result': 0x00, 'item_id': 7}).hex().upper(),
            '81030324000202828183020000100107')
        # a cancel (0x10) carries no item identifier
        self.assertEqual(
            R.build_tr(3, 0x24, 0x81, 0x82, None, None,
                       {'result': 0x10, 'item_id': 7}).hex().upper(),
            '81030324000202828183021000')

    def test_display_text_result_only(self):
        self.assertEqual(
            R.build_tr(2, 0x21, 0x81, 0x82, None, None, {'result': 0}).hex().upper(),
            '81030221000202828183020000')

    def test_the_terminal_answer_objects_follow_the_result(self):
        tr = R.build_tr(1, 0x26, 0x81, 0x82, 0x00, None, {'result': 0},
                        answer_tlvs='2606AABBCCDDEEFF')
        self.assertEqual(tr.hex().upper(),
                         '810301260002028281830200002606AABBCCDDEEFF')
        # with a text object the answer object keeps its place
        self.assertEqual(
            R.build_tr(1, 0x23, 0x81, 0x82, 0x00, None,
                       {'result': 0, 'text': 'hi', 'dcs': 4},
                       answer_tlvs='8402011E').hex().upper(),
            '810301230002028281830200008402011E0D03046869')
        # and the caller's raw TLVs stay last
        self.assertEqual(
            R.build_tr(1, 0x26, 0x81, 0x82, 0x00, None,
                       {'result': 0, 'raw': '9B0100'},
                       answer_tlvs='2607AA').hex().upper(),
            '810301260002028281830200002607AA9B0100')

    def test_answer_objects_accept_bytes_and_tag_value_pairs(self):
        # `2606AABBCCDDEEFF` = tag 0x26, 6 content octets
        expected = R.build_tr(1, 0x26, 0x81, 0x82, 0x00, None, {'result': 0},
                              answer_tlvs='2606AABBCCDDEEFF')
        self.assertEqual(
            R.build_tr(1, 0x26, 0x81, 0x82, 0x00, None, {'result': 0},
                       answer_tlvs=bytes.fromhex('2606AABBCCDDEEFF')), expected)
        self.assertEqual(
            R.build_tr(1, 0x26, 0x81, 0x82, 0x00, None, {'result': 0},
                       answer_tlvs=[(0x26, bytes.fromhex('AABBCCDDEEFF'))]), expected)
        self.assertEqual(
            R.build_tr(1, 0x26, 0x81, 0x82, 0x00, None, {'result': 0},
                       answer_tlvs=['2606AABBCCDDEEFF']), expected)
        # a `(tag, int)` pair is one content octet
        self.assertEqual(
            R.build_tr(1, 0x20, 0x81, 0x82, 0x00, None, {'result': 0},
                       answer_tlvs=[(0x26, 5)]).hex().upper(),
            '81030120000202828183020000260105')

    def test_general_result_carries_the_additional_information(self):
        self.assertEqual(
            R.build_tr(1, 0x26, 0x81, 0x82, 0x00, None,
                       {'result': 0x20, 'additional_info': 1}).hex().upper(),
            '81030126000202828183022001')

    def test_basic_result_form(self):
        # The terminal's own chain answers use the 1-octet Result (03 01 RR);
        # the PLI data / Duration then follow via answer_tlvs (6.8.0 order).
        self.assertEqual(
            R.build_tr(1, 0x26, 0x81, 0x82, 0x00, None, {'result': 0},
                       general_result=False).hex().upper(),
            '810301260002028281030100')
        self.assertEqual(
            R.build_tr(1, 0x03, 0x81, 0x82, None, None, {'result': 0},
                       answer_tlvs=[(0x04, b'\x01\x1E')],
                       general_result=False).hex().upper(),
            '8103010300020282810301000402011E')
        # additional information needs the general form
        with self.assertRaises(ValueError):
            R.build_tr(1, 0x26, 0x81, 0x82, 0x00, None,
                       {'result': 0, 'additional_info': 1}, general_result=False)

    def test_the_command_details_echo_tag_and_qualifier(self):
        # TS 102 223 §6.8.1: identical to the command's object, CR flag and
        # qualifier included; without a tag the previous CR-set default stays.
        self.assertTrue(R.build_tr(1, 0x26, 0x81, 0x82, 0x06, 0x01, {})
                        .hex().startswith('0103012606'))
        self.assertTrue(R.build_tr(1, 0x26, 0x81, 0x82, None, None, {})
                        .hex().startswith('8103012600'))

    def test_the_device_identities_are_the_commands_swapped(self):
        # `dev_src`/`dev_dst` are the *fetched command's* device identities: a
        # command from the UICC (0x81) to the terminal (0x82).  The TR answers
        # it (TS 102 223 §6.8.0: "Direction: terminal to UICC"), so its object
        # carries the swapped pair - and §8.7 orders the object source first,
        # destination second.  Real TRs are `02 02 82 81`.
        self.assertTrue(R.build_tr(1, 0x21, 0x81, 0x82, None, None, {})
                        .hex().startswith('810301210002028281'))


class DecodeTrTests(unittest.TestCase):
    def test_pli_location(self):
        self.assertEqual(R.decode_tr(0x26, 0x00, bytes.fromhex('93055210011000')), [
            {'label': 'MCC', 'value': '250'},
            {'label': 'MNC', 'value': '11'},
            {'label': 'LAC/TAC', 'value': '1000'},
        ])

    def test_pli_imei_and_imeisv(self):
        # The Mobile Identity form (digit 1 + odd/even + type 010/011 first),
        # then the remaining digits nibble-swapped BCD (TS 24.008 §10.5.1.4).
        self.assertEqual(R.decode_tr(0x26, 0x01, bytes.fromhex('94081A32547698103254')),
                         [{'label': 'IMEI', 'value': '123456789012345'}])
        self.assertEqual(R.decode_tr(0x26, 0x08, bytes.fromhex('62091332547698103204F8')),
                         [{'label': 'IMEISV', 'value': '1234567890123408'}])

    def test_pli_date_time_and_time_zone(self):
        # The 8.39 object in the TP-SCTS coding (TS 123 040 §9.2.3.11):
        # swapped semi-octet BCD digits, the time zone sign bit 0x08, FF unknown.
        self.assertEqual(R.decode_tr(0x26, 0x03, bytes.fromhex('260762900312550021')), [
            {'label': 'Date', 'value': '2026-09-30'},
            {'label': 'Time', 'value': '21:55:00'},
            {'label': 'TZ offset', 'value': '+03:00'},
        ])
        self.assertEqual(R.decode_tr(0x26, 0x03, bytes.fromhex('260762105080709009'))[2],
                         {'label': 'TZ offset', 'value': '-02:30'})
        self.assertEqual(R.decode_tr(0x26, 0x03, bytes.fromhex('2607629003125500FF'))[2],
                         {'label': 'TZ offset', 'value': 'unknown'})

    def test_pli_scalar_answers(self):
        self.assertEqual(R.decode_tr(0x26, 0x04, bytes.fromhex('2D02656E')),
                         [{'label': 'Language', 'value': 'en'}])
        self.assertEqual(R.decode_tr(0x26, 0x05, bytes.fromhex('2E020000')), [
            {'label': 'ME Status', 'value': '0'},
            {'label': 'Timing Advance', 'value': '0'},
        ])
        self.assertEqual(R.decode_tr(0x26, 0x06, bytes.fromhex('bf0103')),
                         [{'label': 'Access Technology', 'value': 'UTRAN (3)'}])
        self.assertEqual(R.decode_tr(0x26, 0x09, bytes.fromhex('ad0101')),
                         [{'label': 'Search Mode', 'value': 'Manual'}])
        self.assertEqual(R.decode_tr(0x26, 0x0A, bytes.fromhex('630104')),
                         [{'label': 'Charge state (%)', 'value': '4'}])
        self.assertEqual(R.decode_tr(0x26, 0x0E, bytes.fromhex('3F020308')),
                         [{'label': 'Access Technologies', 'value': 'UTRAN, E-UTRAN'}])

    def test_pli_unknown_or_short_answers_keep_the_raw_data(self):
        self.assertEqual(R.decode_tr(0x26, 0x02, bytes.fromhex('3001ff')),
                         [{'label': 'Data', 'value': '3001ff'}])

    def test_poll_interval(self):
        self.assertEqual(R.decode_tr(0x03, None, bytes.fromhex('8402011e')),
                         [{'label': 'Interval', 'value': '30 s'}])

    def test_timer_management(self):
        expected = [
            {'label': 'Timer', 'value': '3'},
            {'label': 'Remaining', 'value': '14:07:32'},
        ]
        self.assertEqual(R.decode_tr(0x27, None, bytes.fromhex('2401032503417023')), expected)
        # the complete TR payload is stripped before the decode
        self.assertEqual(R.decode_tr(
            0x27, None, bytes.fromhex('8103032700820281832401032503417023')), expected)

    def test_a_full_payload_is_stripped_like_the_data_part(self):
        full = bytes.fromhex('81030326008202818393055210011000030100')
        data = bytes.fromhex('93055210011000')
        self.assertEqual(R.decode_tr(0x26, 0x00, full), R.decode_tr(0x26, 0x00, data))
        # a data part too short for one object has no rows at all
        self.assertEqual(R.decode_tr(0x26, 0x00, b'\x93'), [])

    def test_a_command_without_a_specific_decoder(self):
        self.assertEqual(R.decode_tr(0x21, 0x00, bytes.fromhex('0d020441')),
                         [{'label': 'Data', 'value': '0d020441'}])

    def test_empty(self):
        self.assertEqual(R.decode_tr(0x26, 0x00, b''), [])


class TrHelpersTests(unittest.TestCase):
    def test_tr_data_only_strips_the_boilerplate(self):
        tr = bytes.fromhex('81030326008202818393055210011000030100')
        self.assertEqual(R.tr_data_only(tr).hex(), '93055210011000')
        # a response with no command-specific object has no data part
        self.assertEqual(R.tr_data_only(bytes.fromhex('810301030002028281030100')), b'')

    def test_tr_data_only_handles_a_long_form_text(self):
        # TS 101 220 Table 7.6: a 145-octet Text string (0D) uses '81 91'.
        long_text = bytes([0x0D, 0x81, 0x91, 0x04]) + b'A' * 144
        tr = bytes.fromhex('810301210002028281') + long_text + bytes.fromhex('030100')
        self.assertEqual(R.tr_data_only(tr), long_text)
        # the Result walk skips the long object and still finds the Result
        self.assertEqual(R.extract_tr_result(tr), (0x03, b'\x00'))

    def test_extract_tr_result(self):
        # the basic (03) and the general (83) Result objects are both accepted
        self.assertEqual(
            R.extract_tr_result(bytes.fromhex('8103010300820281838402011e030100')),
            (0x03, b'\x00'))
        self.assertEqual(
            R.extract_tr_result(bytes.fromhex('8103032600820281839305521001100083022001')),
            (0x83, b'\x20\x01'))
        self.assertIsNone(R.extract_tr_result(bytes.fromhex('810303260082028181')))
        self.assertIsNone(R.extract_tr_result(b''))

    def test_result_names(self):
        self.assertEqual(R.tr_result_name(0x00), 'Command performed successfully')
        self.assertEqual(R.tr_result_name(0x07), 'Command performed with modification')
        self.assertIsNone(R.tr_result_name(0x40))       # reserved: no name
        self.assertEqual(R.tr_result_add_info_name(0x20, 0x01), 'Screen is busy')
        self.assertEqual(R.tr_result_add_info_name(0x20, 0x05), 'Access control class bar')
        self.assertEqual(R.tr_result_add_info_name(0x21, 0x00),
                         'No specific cause can be given')
        self.assertIsNone(R.tr_result_add_info_name(0x22, 0x01))

    def test_the_plmn_decoder_refuses_a_short_value(self):
        # no fabricated MCC/MNC for a value that is not a PLMN (TS 24.008
        # §10.5.1.3 wants 3 octets; the decode paths guard the length)
        self.assertEqual(R._dec_plmn(bytes.fromhex('521001')), ('250', '11'))
        with self.assertRaises(ValueError):
            R._dec_plmn(bytes.fromhex('5210'))
        with self.assertRaises(ValueError):
            R._dec_plmn(b'')


class TrResultTableDriftTests(unittest.TestCase):
    """The §8.12.0 result and §8.12.2 additional-information tables against the
    shared fixture ``frontend/tests/tr_results.json``.

    The Node suite asserts the same file against the PWA's tables and
    ``tests/test_tr_results.py`` against ``testscript.py``; this is the port's
    side, read as JSON so the module stays free of a pysim import.
    """

    @classmethod
    def setUpClass(cls):
        path = (pathlib.Path(__file__).resolve().parents[1]
                / 'frontend' / 'tests' / 'tr_results.json')
        cls.data = json.loads(path.read_text(encoding='utf-8'))

    def test_the_result_table(self):
        self.assertEqual(R.TR_RESULT_NAMES,
                         {r['v']: r['name'] for r in self.data['results']})

    def test_the_additional_information_table(self):
        self.assertEqual(R.TR_RESULT_ADD_INFO_NAMES,
                         {a['v']: a['name'] for a in self.data['additional_info']})


class PackageImportHygieneTests(unittest.TestCase):
    """The hard phase-out constraint: no ``pySim``/``osmocom``/``construct``
    import anywhere in ``simple_server/``.

    Read with the AST, not a grep - a conditional or aliased import is still a
    dependency.  This lives in the pysim-free suite so the guard can never be
    skipped with the legacy side.
    """

    FORBIDDEN = ('pySim', 'osmocom', 'construct')

    def test_no_forbidden_import(self):
        package = pathlib.Path(P.__file__).resolve().parent
        offenders = []
        for path in sorted(package.glob('*.py')):
            tree = ast.parse(path.read_text(encoding='utf-8'))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or '']
                else:
                    continue
                for name in names:
                    if name.split('.')[0] in self.FORBIDDEN:
                        offenders.append('%s:%d: %s'
                                         % (path.name, node.lineno, name))
        self.assertEqual(offenders, [])


if __name__ == '__main__':
    unittest.main()
