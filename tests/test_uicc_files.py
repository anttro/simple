#!/usr/bin/env python3
"""Drift guard for the shipped standard file list (frontend/uicc_files.json).

The asset is generated from pySim's card profiles/application classes by
``pysim_simple_server.uicc_files``.  When pySim adds, renames or moves files,
this test fails and the asset has to be regenerated with::

    python -m pysim_simple_server.uicc_files --write frontend/uicc_files.json
"""

import json
import sys
import unittest
from pathlib import Path

# pySim checkout is a sibling of this repo; put it on sys.path so the builder
# (which imports pySim at module level) can be exercised against it.
PROJECTS = Path(__file__).resolve().parents[2]
PY_SIM = PROJECTS / 'pysim'
if str(PY_SIM) not in sys.path:
    sys.path.insert(0, str(PY_SIM))

REPO = Path(__file__).resolve().parents[1]
ASSET = REPO / 'frontend' / 'uicc_files.json'

from pysim_simple_server import uicc_files


def _load():
    with open(ASSET, 'r', encoding='utf-8') as fh:
        return json.load(fh)


class TestUiccFilesAsset(unittest.TestCase):
    def test_asset_matches_the_pysim_build(self):
        doc = _load()
        self.assertEqual(doc['files'], uicc_files.build(),
                         'frontend/uicc_files.json is out of date - regenerate it '
                         'with: python -m pysim_simple_server.uicc_files --write '
                         'frontend/uicc_files.json')

    def test_document_has_a_date_and_a_non_empty_list(self):
        doc = _load()
        self.assertRegex(doc['generated'], r'^\d{4}-\d{2}-\d{2}$')
        self.assertGreater(len(doc['files']), 300)

    def test_known_standard_files_and_paths(self):
        by_path = {f['path']: f for f in _load()['files']}
        self.assertEqual(by_path['MF/2FE2']['name'], 'EF.ICCID')
        self.assertEqual(by_path['MF/2FE2']['kind'], 'ef')
        self.assertEqual(by_path['MF/7F10/6F3A']['name'], 'EF.ADN')
        self.assertEqual(by_path['MF/7F10/6F3A']['sym'], 'MF/DF.TELECOM/EF.ADN')
        self.assertEqual(by_path['MF/7F20']['name'], 'DF.GSM')
        self.assertEqual(by_path['MF/7F20']['kind'], 'df')
        self.assertEqual(by_path['ADF.USIM/6F07']['name'], 'EF.IMSI')
        self.assertEqual(by_path['ADF.USIM']['kind'], 'adf')
        self.assertEqual(by_path['ADF.USIM']['aid'], 'A0000000871002')
        self.assertEqual(by_path['ADF.ISIM/6F02']['name'], 'EF.IMPI')

    def test_paths_are_unique_and_well_formed(self):
        files = _load()['files']
        paths = [f['path'] for f in files]
        self.assertEqual(len(paths), len(set(paths)))
        for f in files:
            root = f['root']
            self.assertTrue(f['path'].startswith(root), f['path'])
            self.assertIn(f['kind'], ('ef', 'df', 'adf'))
            if f['kind'] == 'adf':
                self.assertEqual(f['path'].upper(), f['name'].upper())
                self.assertTrue(f['aid'])
            else:
                self.assertRegex(f['fid'], r'^[0-9A-F]{4}$')
                self.assertTrue(f['name'].startswith(('EF.', 'DF.')))

    def test_optional_files_missing_from_the_pysim_model(self):
        """The service-gated files pySim does not model (the SUPPLEMENT)."""
        files = _load()['files']
        by_path = {f['path']: f for f in files}
        expected = {
            # TS 31.102 4.2.86 / 4.2.100 / 4.2.102 / 4.2.108 / 4.2.109
            'ADF.USIM/6FDC': 'EF.LRPLMNSI',
            'ADF.USIM/6FF0': 'EF.IAL',
            'ADF.USIM/6FF2': 'EF.IPD',
            'ADF.USIM/6FF9': 'EF.3GPPPSDATAOFF',
            'ADF.USIM/6FFB': 'EF.TVCONFIG',
            # 4.4.4 DF.MExE
            'ADF.USIM/5F3C': 'DF.MExE',
            'ADF.USIM/5F3C/4F40': 'EF.MExE-ST',
            'ADF.USIM/5F3C/4F41': 'EF.ORPK',
            'ADF.USIM/5F3C/4F42': 'EF.ARPK',
            'ADF.USIM/5F3C/4F43': 'EF.TPRPK',
            # 4.4.1 DF.SoLSA
            'ADF.USIM/5F70': 'DF.SoLSA',
            'ADF.USIM/5F70/4F30': 'EF.SAI',
            'ADF.USIM/5F70/4F31': 'EF.SLL',
            # 4.4.7 / 4.4.9 / 4.4.10
            'ADF.USIM/5F80': 'DF.BCAST',
            'ADF.USIM/5FA0': 'DF.ACDC',
            'ADF.USIM/5FA0/4F01': 'EF.ACDC_LIST',
            'ADF.USIM/5FB0': 'DF.TV',
            # 4.6.6 DF.A2X (DF.TELECOM)
            'MF/7F10/5F3F': 'DF.A2X',
            'MF/7F10/5F3F/4F01': 'EF.AST',
            'MF/7F10/5F3F/4F02': 'EF.A2X_CONFIG',
        }
        for path, name in expected.items():
            self.assertIn(path, by_path)
            self.assertEqual(by_path[path]['name'], name)
            self.assertEqual(by_path[path]['root'], path.split('/')[0])
            self.assertEqual(by_path[path]['kind'],
                             'df' if name.startswith('DF.') else 'ef')
        # The same FID under another DF stays a distinct entry (name, path).
        self.assertEqual(by_path['ADF.USIM/5F3A/4F30']['name'], 'EF.PBR')
        self.assertEqual(by_path['MF/7F10/5F3A/4F30']['name'], 'EF.PBR')
        # The ADF.USIM files are mirrored under the non-IMSI USIM root.
        for path, name in expected.items():
            if not path.startswith('ADF.USIM/'):
                continue
            mirror = 'ADF.USIM-NON-IMSI' + path[len('ADF.USIM'):]
            self.assertIn(mirror, by_path)
            self.assertEqual(by_path[mirror]['name'], name)
            self.assertEqual(by_path[mirror]['root'], 'ADF.USIM-NON-IMSI')
        # (name, root) resolves each SUPPLEMENT file to exactly one path.
        for path, name in expected.items():
            root = path.split('/')[0]
            self.assertEqual([f['path'] for f in files
                              if f['name'] == name and f['root'] == root], [path])


if __name__ == '__main__':
    unittest.main()
