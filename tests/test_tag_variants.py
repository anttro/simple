"""Every proactive-command TLV parser must accept both tag styles.

Bit 8 of a COMPREHENSION-TLV tag is the comprehension-required flag
(TS 101 220 7.1.1) and real cards switch between the plain and the CR-set
variant per command - and sometimes per TLV within one command (the reference
traces show OPEN CHANNEL with `01 03` and SEND DATA with `81 03` from the
same card).  A single-style lookup silently drops data: the live Alfa card's
SET UP MENU title arrives as tag `85`, and looking for `05` only made the PWA
show "No menu set by the card" (2026-09-29).

This is the drift guard for the rule implemented by `server._cmd_tlv`: it
flips the flag on every top-level tag of real command fixtures and asserts
that each parser returns exactly the same result.  Add a fixture here for
every new proactive-command parser.
"""

import unittest

from pysim_simple_server.server import (
    _ber_len_at,
    _decode_cmd,
    _find_duration,
    _find_sms_tpdu,
    _parse_display_text,
    _parse_get_inkey,
    _parse_get_input,
    _parse_proactive_header,
    _parse_response_scripting,
    _parse_select_item,
    _parse_setup_menu_command,
    _parse_setup_menu_items,
    _scp81_decode_memory,
    _scp81_parse_response,
    _skip_ber_len,
)

# The live Alfa card's SET UP MENU (2026-09-29): CR-set title 0x85, items 0x8F.
LIVE_MENU = bytes.fromhex(
    'd03c810301250082028182850b416c6661204d6f62696c65'
    '8f16808112089db0c1c2c0beb9bab82f53657474696e6773'
    '8f0881810400d4e5f3f418020021')
# The live RemMobileID GET INPUT (2026-09-29): qualifier 04 = digits + hidden.
LIVE_GET_INPUT = bytes.fromhex(
    'd03c8103012304820281828d2d0804120432043504340438044204350020004d006f00'
    '620069006c0065002d00490044002000500049004e003191020410')
SEND_SM = bytes.fromhex('d0158103011300820283818b0b916106152670f900a35f020101')
OPEN_CHANNEL = bytes.fromhex(
    'd02b8103014001820281828500b50103b9020200c70b076d656761666f6e2e7275'
    'bc03021f90be05217f000001')
SEND_DATA = bytes.fromhex('d00e810301430182028182213701013603aabbcc')
TIMER_CR = bytes.fromhex('d011810301270082028182a40103a503417023')
TIMER_PLAIN = bytes.fromhex('d00c010301270102028182240103')


def flip_tags(raw):
    """Return the command with every top-level TLV tag's CR flag flipped."""
    raw = bytes(raw)
    out = bytearray(raw)
    off = _skip_ber_len(raw, 1) if raw and raw[0] == 0xD0 else 0
    while off < len(out) - 1:
        out[off] ^= 0x80
        tlen, voff = _ber_len_at(bytes(out), off + 1)
        if tlen < 0:
            break
        off = voff + tlen
    return bytes(out)


# (label, raw, parser) - the parser takes the raw command and returns a value
# that must be identical for the plain and the flipped variant.
FIXTURES = [
    ('proactive header', LIVE_MENU, _parse_proactive_header),
    ('SET UP MENU command', LIVE_MENU, _parse_setup_menu_command),
    ('SET UP MENU items', LIVE_MENU, _parse_setup_menu_items),
    ('SELECT ITEM', bytes.fromhex('d00d8103012400820281828f020141'), _parse_select_item),
    ('DISPLAY TEXT', bytes.fromhex('d00d8103012100820281828d020441'), _parse_display_text),
    ('GET INPUT (live)', LIVE_GET_INPUT, _parse_get_input),
    ('GET INKEY', bytes.fromhex('d00e8103012204820281828d03080041'), _parse_get_inkey),
    ('POLL INTERVAL duration', bytes.fromhex('d00d8103010300820283818402011e'), _find_duration),
    ('SEND SHORT MESSAGE TPDU', SEND_SM, _find_sms_tpdu),
    ('decode SET UP MENU', LIVE_MENU, lambda raw: _decode_cmd(0x25, raw, 0x00)),
    ('decode GET INPUT', LIVE_GET_INPUT, lambda raw: _decode_cmd(0x23, raw, 0x04)),
    ('decode SEND SHORT MESSAGE', SEND_SM, lambda raw: _decode_cmd(0x13, raw, 0x00)),
    ('decode OPEN CHANNEL', OPEN_CHANNEL, lambda raw: _decode_cmd(0x40, raw, 0x01)),
    ('decode SEND DATA', SEND_DATA, lambda raw: _decode_cmd(0x43, raw, 0x01)),
    ('decode TIMER (CR-set fixture)', TIMER_CR, lambda raw: _decode_cmd(0x27, raw, 0x00)),
    ('decode TIMER (plain fixture)', TIMER_PLAIN, lambda raw: _decode_cmd(0x27, raw, 0x00)),
]


class TagVariantMatrixTests(unittest.TestCase):
    def test_flipping_every_tag_keeps_the_parse_result(self):
        for label, raw, parser in FIXTURES:
            with self.subTest(label=label):
                plain = parser(raw)
                flipped = parser(flip_tags(raw))
                self.assertEqual(plain, flipped,
                                 '%s differs between the tag styles' % label)
                self.assertNotEqual(raw, flip_tags(raw), 'fixture has no TLVs?')

    def test_response_scripting_accepts_both_rapdu_tags(self):
        definite = bytes.fromhex('ab0780010123029000')
        self.assertEqual(_parse_response_scripting(definite), (1, '9000', ''))
        self.assertEqual(_parse_response_scripting(bytes.fromhex('ab07800101a3029000')),
                         (1, '9000', ''))
        indefinite = bytes.fromhex('af80800101230290000000')
        self.assertEqual(_parse_response_scripting(indefinite), (1, '9000', ''))
        self.assertEqual(_parse_response_scripting(bytes.fromhex('af80800101a30290000000')),
                         (1, '9000', ''))
        # the SCP81 R-APDU list parser reads the same templates
        self.assertEqual(_scp81_parse_response(definite), (1, [(b'', '9000')]))
        self.assertEqual(_scp81_parse_response(bytes.fromhex('ab07800101a3029000')),
                         (1, [(b'', '9000')]))

    def test_ff21_resource_tags_accept_both_styles(self):
        cr = _scp81_decode_memory(bytes.fromhex('ff210c810105820300ff000301 10'.replace(' ', '')))
        plain = _scp81_decode_memory(bytes.fromhex('ff210c010105020300ff000301 10'.replace(' ', '')))
        self.assertEqual(cr, {'applets': 5, 'free_nv': 0xFF00, 'free_volatile': 0x10})
        self.assertEqual(plain, cr)


if __name__ == '__main__':
    unittest.main()
