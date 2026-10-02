"""Tests for the CAT item introduction-release table (catrelease) and its PWA mirror."""

import pathlib
import re
import unittest

from pysim_simple_server import catrelease

HTML = pathlib.Path(__file__).resolve().parents[1].joinpath('frontend', 'index.html')


def _pwa_block():
    html = HTML.read_text(encoding='utf-8')
    m = re.search(r"const CAT_RELEASE = \{(.*?)\n\};", html, re.S)
    assert m is not None, 'CAT_RELEASE not found in index.html'
    return m.group(1), html


class TestCatReleaseTable(unittest.TestCase):

    def test_pwa_mirror_matches_the_module(self):
        # the PWA's release guard uses the same table (drift guard, like
        # ETSI_TOOLKIT_RELEASE in test_cap_memory)
        block, html = _pwa_block()
        ev = re.search(r"events:\s*\{(.*?)\}", block, re.S)
        self.assertIsNotNone(ev, 'events section not found')
        self.assertEqual(
            {int(k, 16): int(v)
             for k, v in re.findall(r"(0x[0-9A-Fa-f]{2}):\s*(\d+)", ev.group(1))},
            catrelease.CAT_RELEASE['events'])
        for section in ('commands', 'objects', 'pli'):
            m = re.search(r"%s:\s*\{(.*?)\}" % section, block, re.S)
            self.assertIsNotNone(m, section)
            self.assertEqual(
                {k: int(v) for k, v in re.findall(r"'([^']+)':\s*(\d+)", m.group(1))},
                catrelease.CAT_RELEASE[section], section)
        d = re.search(r"const CAT_RELEASE_DEFAULT = (\d+);", html)
        self.assertIsNotNone(d, 'CAT_RELEASE_DEFAULT not found in index.html')
        self.assertEqual(int(d.group(1)), catrelease.DEFAULT_RELEASE)

    def test_events_cover_the_pwa_event_list(self):
        # every event the UI names through 0x1F except the Void 0x1A carries
        # an introduction release; the reserved 0x20+ codes do not
        _, html = _pwa_block()
        names = re.search(r"const EVENT_NAMES = \{(.*?)\};", html, re.S)
        self.assertIsNotNone(names, 'EVENT_NAMES not found in index.html')
        codes = {int(k, 16) for k in re.findall(r"(0x[0-9A-Fa-f]{2}):", names.group(1))}
        self.assertEqual(set(catrelease.CAT_RELEASE['events']),
                         {c for c in codes if c <= 0x1F} - {0x1A})

    def test_release_label_collapses_below_rel6(self):
        self.assertEqual(catrelease.release_label(4), 'pre-Rel-6')
        self.assertEqual(catrelease.release_label(5), 'pre-Rel-6')
        self.assertEqual(catrelease.release_label(6), 'Rel-6')
        self.assertEqual(catrelease.release_label(18), 'Rel-18')
        # unknown never gets a label - it must not be filtered
        self.assertEqual(catrelease.release_label(None), '')
        self.assertEqual(catrelease.release_label(0), '')

    def test_item_release_lookup(self):
        self.assertEqual(catrelease.item_release('events', 0x1D), 14)
        self.assertEqual(catrelease.item_release('commands', 'LSI COMMAND'), 17)
        self.assertIsNone(catrelease.item_release('events', 0x1A))
        self.assertIsNone(catrelease.item_release('nope', 'x'))

    def test_preset_release_resolves_the_default(self):
        self.assertEqual(catrelease.DEFAULT_RELEASE, 12)
        self.assertEqual(catrelease.preset_release({'release': '14'}), 14)
        self.assertEqual(catrelease.preset_release({'release': 4}), 4)
        self.assertEqual(catrelease.preset_release({'release': '18'}), 18)
        # an unset or invalid release must never filter anything away
        for p in ({}, {'release': ''}, {'release': '0'}, {'release': '3'},
                  {'release': '99'}, {'release': 'x'}, None):
            self.assertEqual(catrelease.preset_release(p),
                             catrelease.DEFAULT_RELEASE, p)

    def test_table_sanity(self):
        for section, table in catrelease.CAT_RELEASE.items():
            self.assertTrue(table, section)
            for key, rel in table.items():
                self.assertIsInstance(rel, int, (section, key))
                self.assertTrue(4 <= rel <= 18, (section, key, rel))
        self.assertTrue(all(0 <= c <= 0x1F for c in catrelease.CAT_RELEASE['events']))


if __name__ == '__main__':
    unittest.main()
