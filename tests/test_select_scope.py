#!/usr/bin/env python3
"""Unit tests for the parent-scoped select helpers in pysim_simple_server.server.

The helpers must resolve every model-known file strictly within the requested
parent (no pySim global selectables, no probe_file model injection) and must
detach any model-unknown file that had to be probed for a custom file.
"""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

PROJECTS = Path(__file__).resolve().parents[2]
PY_SIM = PROJECTS / 'pysim'
if str(PY_SIM) not in sys.path:
    sys.path.insert(0, str(PY_SIM))

from pysim_simple_server.server import (
    _app_by_sel,
    _fid4,
    _file_by_sel,
    _find_in_tree,
    _resolve_path_target,
    _select_path,
    _select_path_minimal,
    _select_with_parent,
)


class FakeFile:
    def __init__(self, fid=None, name=None, parent=None, aid=None):
        self.fid = fid
        self.name = name
        self.parent = parent
        self.aid = aid
        self.sfid = None
        self.children = {}

    def add_files(self, files):
        for f in files:
            f.parent = self
            self.children[f.fid] = f


class FakeMF(FakeFile):
    def __init__(self):
        super().__init__(fid='3f00', name='MF')
        self.applications = {}


class FakeLchan:
    """No .select() on purpose: any global-resolution call would fail loudly."""

    def __init__(self, mf):
        self.selected_file = mf
        self.selects = []
        self.probes = []

    def select_file(self, f, app=None):
        self.selected_file = f
        self.selects.append(f)

    def probe_file(self, fid, app=None):
        self.probes.append(fid)
        f = FakeFile(fid=fid, name='EF.' + fid.upper(), parent=self.selected_file)
        self.selected_file.add_files([f])
        self.selected_file = f


def build_model():
    mf = FakeMF()
    gsm = FakeFile('7f20', 'DF.GSM', mf)
    mf.children['7f20'] = gsm
    spn = FakeFile('6f46', 'EF.SPN', gsm)
    gsm.children['6f46'] = spn
    telecom = FakeFile('7f10', 'DF.TELECOM', mf)
    mf.children['7f10'] = telecom
    tel_ph = FakeFile('5f3a', 'DF.PHONEBOOK', telecom)
    telecom.children['5f3a'] = tel_ph
    usim = FakeFile(None, 'ADF.USIM', mf, aid='A0000000871002')
    mf.applications['a0000000871002'] = usim
    imsi = FakeFile('6f07', 'EF.IMSI', usim)
    usim.children['6f07'] = imsi
    usim_ph = FakeFile('5f3a', 'DF.PHONEBOOK', usim)
    usim.children['5f3a'] = usim_ph
    return mf, usim, usim_ph, telecom, tel_ph, gsm, spn


def setup():
    mf, usim, usim_ph, telecom, tel_ph, gsm, spn = build_model()
    app = SimpleNamespace(rs=SimpleNamespace(mf=mf))
    lchan = FakeLchan(mf)
    return app, lchan, mf, usim, usim_ph, gsm, spn


class FidHelpersTest(unittest.TestCase):
    def test_fid4(self):
        self.assertTrue(_fid4('6F07'))
        self.assertFalse(_fid4('EF.IMSI'))
        self.assertFalse(_fid4('6F0'))

    def test_file_by_sel_matches_fid_and_name(self):
        _, _, _, _, _, gsm, spn = setup()
        self.assertIs(_file_by_sel(gsm, '6f46'), spn)
        self.assertIs(_file_by_sel(gsm, 'EF.SPN'), spn)
        self.assertIsNone(_file_by_sel(gsm, '6f07'))

    def test_find_in_tree_reports_duplicates(self):
        app, _, mf, _, _, _, _ = setup()
        self.assertEqual(len(_find_in_tree(mf, '5f3a')), 2)
        self.assertEqual(len(_find_in_tree(mf, 'EF.IMSI')), 1)


class ParentScopedSelectTest(unittest.TestCase):
    def test_duplicate_fid_is_resolved_under_the_walked_parent(self):
        app, lchan, mf, usim, usim_ph, _, _ = setup()
        target, cleanup = _select_with_parent(lchan, '5f3a', None, app, parent_path=['MF', 'A0000000871002'])
        self.assertIs(target, usim_ph)
        self.assertIsNone(cleanup)
        self.assertEqual(lchan.selects, [mf, usim, usim_ph])
        self.assertEqual(lchan.probes, [])

    def test_path_with_fids_selects_exactly(self):
        app, lchan, mf, _, _, gsm, spn = setup()
        target, cleanup = _select_path(lchan, 'MF/7F20/6F46', app)
        self.assertIs(target, spn)
        self.assertIsNone(cleanup)
        self.assertEqual(lchan.selects, [mf, gsm, spn])

    def test_path_with_aid_root_selects_application(self):
        app, lchan, _, usim, _, _, _ = setup()
        target, _ = _select_path(lchan, 'A0000000871002/6F07', app)
        self.assertEqual(target.fid, '6f07')
        self.assertEqual(lchan.selected_file.parent, usim)

    def test_ambiguous_legacy_parent_selector_is_rejected(self):
        app, lchan, _, _, _, _, _ = setup()
        with self.assertRaisesRegex(RuntimeError, 'Ambiguous'):
            _select_with_parent(lchan, '6f07', '5f3a', app)

    def test_unknown_name_is_not_probed(self):
        app, lchan, _, _, _, gsm, _ = setup()
        with self.assertRaisesRegex(RuntimeError, 'File not found'):
            _select_with_parent(lchan, 'NOSUCH', None, app, parent_path=['MF', '7F20'])
        self.assertEqual(lchan.probes, [])

    def test_unknown_fid_without_allow_probe_does_not_touch_the_card(self):
        app, lchan, _, _, _, gsm, _ = setup()
        with self.assertRaisesRegex(RuntimeError, 'File not found'):
            _select_with_parent(lchan, '6f99', None, app, parent_path=['MF', '7F20'])
        self.assertEqual(lchan.probes, [])

    def test_allow_probe_detaches_the_temporary_file_and_restores_selection(self):
        app, lchan, mf, _, _, gsm, _ = setup()
        before = set(gsm.children)
        target, cleanup = _select_with_parent(lchan, '6f99', None, app, parent_path=['MF', '7F20'], allow_probe=True)
        self.assertEqual(lchan.probes, ['6f99'])
        self.assertEqual(target.fid, '6f99')
        self.assertIsNotNone(cleanup)
        self.assertIn('6f99', gsm.children)
        cleanup()
        self.assertEqual(set(gsm.children), before)
        self.assertIs(lchan.selected_file, mf)

    def test_custom_path_segments_are_probed_and_detached(self):
        app, lchan, mf, _, _, gsm, _ = setup()
        before = set(gsm.children)
        target, cleanup = _select_path(lchan, 'MF/7F20/A0B1/6F01', app)
        self.assertEqual(lchan.probes, ['a0b1', '6f01'])
        self.assertEqual(target.fid, '6f01')
        cleanup()
        self.assertEqual(set(gsm.children), before)
        self.assertIs(lchan.selected_file, mf)

    def test_model_known_selection_never_mutates_the_tree(self):
        app, lchan, _, _, _, gsm, spn = setup()
        before = {id(k): k for k in gsm.children}
        _select_with_parent(lchan, '6f46', None, app, parent_path=['MF', '7F20'])
        self.assertEqual({id(k): k for k in gsm.children}, before)
        self.assertEqual(lchan.probes, [])


class MinimalSelectTest(unittest.TestCase):
    def test_a_model_known_file_is_selected_directly(self):
        app, lchan, mf, _, _, gsm, spn = setup()
        target, cleanup = _select_path_minimal(lchan, 'MF/7F20/6F46', app)
        self.assertIs(target, spn)
        self.assertIsNone(cleanup)
        # no MF or DF re-select: pySim navigates the shortest route itself
        self.assertEqual(lchan.selects, [spn])

    def test_an_unknown_segment_falls_back_to_the_probe(self):
        app, lchan, mf, _, _, gsm, _ = setup()
        target, cleanup = _select_path_minimal(lchan, 'MF/7F20/6F99', app)
        self.assertEqual(lchan.probes, ['6f99'])
        self.assertIsNotNone(cleanup)
        cleanup()
        self.assertIs(lchan.selected_file, mf)

    def test_resolve_returns_the_model_object(self):
        app, _, _, usim, _, gsm, spn = setup()
        self.assertIs(_resolve_path_target(app.rs, 'MF/7F20/6F46'), spn)
        self.assertIs(_resolve_path_target(app.rs, 'A0000000871002/6F07'),
                      usim.children['6f07'])
        self.assertIsNone(_resolve_path_target(app.rs, 'MF/7F20/6F99'))
        self.assertIsNone(_resolve_path_target(app.rs, 'MF/7F20/NOSUCH'))
        self.assertIsNone(_resolve_path_target(None, 'MF/7F20/6F46'))
        self.assertIsNone(_resolve_path_target(app.rs, ''))


if __name__ == '__main__':
    unittest.main()
