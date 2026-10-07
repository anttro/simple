#!/usr/bin/env python3
"""TERMINAL RESPONSE results (TS 102 223 v18.3.0 8.12.0/8.12.2) against the
SHARED fixture `frontend/tests/tr_results.json`: the Node suite asserts the
same file against the PWA's own tables, this suite asserts it against
`testscript.py`, so neither implementation can drift alone.  Also pins the
corrected script aliases (the v3.23.5 review), the hex parsing of the
`dcs`/`additional_info` fields and the scripted TR's general result."""

import json
import pathlib
import sys
import unittest

PROJECTS = pathlib.Path(__file__).resolve().parents[2]
PY_SIM = PROJECTS / 'pysim'
if str(PY_SIM) not in sys.path:
    sys.path.insert(0, str(PY_SIM))

import pysim_simple_server.server as S
import pysim_simple_server.testscript as T

FIXTURE = (pathlib.Path(__file__).resolve().parents[1]
           / 'frontend' / 'tests' / 'tr_results.json')


class TrResults(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = json.loads(FIXTURE.read_text(encoding='utf-8'))

    def test_the_shared_result_table(self):
        want = {r['v']: r['name'] for r in self.data['results']}
        self.assertEqual(T.TR_RESULTS, want)
        # the proactive log's decode uses the same table (a spec name, not the
        # old GSM 11.14 mix)
        self.assertEqual(S._tr_result_name(0x03), want['03'])
        self.assertEqual(S._tr_result_name(0x20), want['20'])

    def test_the_shared_additional_info_table(self):
        want = {a['v']: a['name'] for a in self.data['additional_info']}
        self.assertEqual(T.TR_RESULT_ADD_INFO, want)

    def test_the_shared_aliases(self):
        want = {k: int(v, 16) for k, v in self.data['aliases'].items()}
        self.assertEqual(T.RESULT_NAMES, want)

    def test_the_corrected_aliases(self):
        # the v3.23.5 review: these four carried legacy (GSM 11.14) values
        self.assertEqual(T.RESULT_NAMES['refused'], 0x22)
        self.assertEqual(T.RESULT_NAMES['not_understood'], 0x32)
        self.assertEqual(T.RESULT_NAMES['modified'], 0x07)
        self.assertEqual(T.RESULT_NAMES['no_response'], 0x12)
        # the common ones were already right
        self.assertEqual(T.RESULT_NAMES['ok'], 0x00)
        self.assertEqual(T.RESULT_NAMES['cancel'], 0x10)
        self.assertEqual(T.RESULT_NAMES['timeout'], 0x12)

    def test_the_tr_decode_names(self):
        self.assertEqual(S._tr_result_name(0x22), 'User did not accept the proactive command')
        self.assertIsNone(S._tr_result_name(0x99))
        self.assertEqual(S._tr_result_add_info_name(0x20, 0x04), 'No service')
        self.assertEqual(S._tr_result_add_info_name(0x21, 0x00), 'No specific cause can be given')
        self.assertIsNone(S._tr_result_add_info_name(0x10, 0x04))

    def test_the_respond_normalises_the_additional_info(self):
        out = T.normalise_respond({'result': 'refused', 'additional_info': '0A'})
        self.assertEqual(out['result'], 0x22)
        self.assertEqual(out['additional_info'], 0x0A)
        out = T.normalise_respond({'result': '00', 'additional_info': 4})
        self.assertEqual(out['additional_info'], 4)
        out = T.normalise_respond({'result': 'ok'})
        self.assertNotIn('additional_info', out)
        with self.assertRaises(T.ScriptError):
            T.normalise_respond({'result': 'ok', 'additional_info': 'zz'})

    def test_the_dcs_field_parses_as_hex(self):
        # '0A' used to be refused and '10' read as decimal 10 (= 0x0A)
        self.assertEqual(T.normalise_respond({'result': 'ok', 'text': 'A', 'dcs': '0A'})['dcs'], 0x0A)
        self.assertEqual(T.normalise_respond({'result': 'ok', 'text': 'A', 'dcs': '10'})['dcs'], 0x10)

    def test_the_scripted_tr_carries_the_additional_info(self):
        tr = T.build_tr(1, 0x21, 0x81, 0x82, {'result': 0x20, 'additional_info': 0x04})
        self.assertIn(bytes([0x83, 0x02, 0x20, 0x04]), tr)
        tr = T.build_tr(1, 0x21, 0x81, 0x82, {'result': 0x00})
        self.assertIn(bytes([0x83, 0x02, 0x00, 0x00]), tr)


if __name__ == '__main__':
    unittest.main()
