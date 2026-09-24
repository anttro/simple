# coding=utf-8
"""Tests for the bundled MCC/MNC operator list and the simulator picker.

The list ships inside the package (``data/mcc-mnc-list.json``, MIT, from
pbakondy/mcc-mnc-list).  The Network simulation picker hides MVNO entries
(``bands`` marks them) because they do not operate their own network; the
Network state panel still resolves operator names from the full list.
"""

import os
import unittest

from pysim_simple_server import __main__, server


def _entry(mcc, mnc, brand, bands, **kw):
    e = {'mcc': mcc, 'mnc': mnc, 'brand': brand, 'operator': brand + ' op',
         'countryName': 'Testland', 'countryCode': 'TL', 'status': 'Operational',
         'bands': bands}
    e.update(kw)
    return e


class BundledMccMncListTests(unittest.TestCase):
    def test_default_path_is_bundled_and_parses(self):
        path = __main__._default_mcc_mnc_list()
        pkg_dir = os.path.dirname(os.path.abspath(server.__file__))
        self.assertTrue(os.path.abspath(path).startswith(pkg_dir + os.sep), path)
        self.assertTrue(os.path.isfile(path), path)
        data = server._mcc_mnc_load(path)
        self.assertIsInstance(data, list)
        self.assertGreater(len(data), 3000)
        for key in ('mcc', 'mnc', 'countryName', 'countryCode', 'brand',
                    'operator', 'status', 'bands'):
            self.assertIn(key, data[0])

    def test_bundled_list_has_mvno_entries_to_filter(self):
        data = server._mcc_mnc_load(__main__._default_mcc_mnc_list())
        mvno = [e for e in data if server._mcc_mnc_is_mvno(e)]
        self.assertGreater(len(mvno), 300)

    def test_mvno_marker_variants(self):
        for bands in ('MVNO', 'Satellite MVNO', 'FULL MVNO', 'MVNO / LTE 800',
                      '5G 3500 / MVNO', 'mvno'):
            self.assertTrue(server._mcc_mnc_is_mvno(_entry('262', '01', 'X', bands)),
                            bands)
        for bands in ('LTE 1800', 'GSM 900 / LTE 800', '', None):
            self.assertFalse(server._mcc_mnc_is_mvno(_entry('262', '01', 'X', bands)),
                             repr(bands))


class MccMncFilterTests(unittest.TestCase):
    DATA = [
        _entry('262', '01', 'Telekom', 'GSM 900 / LTE 800'),
        _entry('262', '77', 'MVNO brand', 'MVNO'),
        _entry('262', '78', 'Sat MVNO', 'Satellite MVNO'),
        _entry('262', '79', 'Mentions MVNO', 'LTE 1800',
               notes='Used by MVNO Kartu As'),
    ]

    def test_search_skips_mvno_entries(self):
        res = server._mcc_mnc_search(self.DATA, '262')
        self.assertEqual([(r['mcc'], r['mnc']) for r in res],
                         [('262', '01'), ('262', '79')])
        # the MVNO brands would match these queries without the filter, but the
        # notes-only MVNO mention (a real network) still matches 'mvno'
        self.assertEqual([(r['mcc'], r['mnc']) for r in server._mcc_mnc_search(self.DATA, 'mvno')],
                         [('262', '79')])
        self.assertEqual(server._mcc_mnc_search(self.DATA, 'sat mvno'), [])

    def test_search_real_list_hides_mvno_entry(self):
        data = server._mcc_mnc_load(__main__._default_mcc_mnc_list())
        mvno = next(e for e in data if server._mcc_mnc_is_mvno(e) and e.get('brand'))
        res = server._mcc_mnc_search(data, mvno['brand'], limit=10000)
        self.assertNotIn((mvno['mcc'], mvno['mnc']),
                         [(r['mcc'], r['mnc']) for r in res])

    def test_random_skips_mvno_entries(self):
        mixed = [_entry('262', '77', 'MVNO brand', 'MVNO'),
                 _entry('262', '78', 'Sat MVNO', 'Satellite MVNO')]
        self.assertIsNone(server._mcc_mnc_random(mixed))
        data = self.DATA
        for _ in range(100):
            r = server._mcc_mnc_random(data)
            self.assertIn((r['mcc'], r['mnc']), [('262', '01'), ('262', '79')])
        # exclusion still applies within the non-MVNO pool
        for _ in range(50):
            self.assertEqual(server._mcc_mnc_random(data, exclude='26201')['mnc'],
                             '79')

    def test_random_real_list_never_picks_mvno(self):
        data = server._mcc_mnc_load(__main__._default_mcc_mnc_list())
        # A few (mcc, mnc) pairs exist both as a real network and as an MVNO
        # entry (e.g. 234/18, 234/28), so a dict keyed by the pair can keep the
        # wrong entry: assert the picked pair has a non-MVNO entry behind it.
        real_pairs = {(e['mcc'], e['mnc']) for e in data
                      if not server._mcc_mnc_is_mvno(e)}
        for _ in range(100):
            r = server._mcc_mnc_random(data)
            self.assertIn((r['mcc'], r['mnc']), real_pairs)


if __name__ == '__main__':
    unittest.main()
