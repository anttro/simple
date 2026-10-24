"""The direct read of an already-selected EF (v3.38.1).

pySim's ``scc.read_binary``/``scc.read_record`` select the file first; the
equip/init readers have already made a parent-scoped selection and hold its
FCP, so that second SELECT is pure overhead (one per file, one per record).
These tests pin the direct READ BINARY/READ RECORD form, the chunking and the
fallback when the FCP lacks the size/record length.
"""

import pathlib
import unittest

from pysim_simple_server import server as S


class FakeScc:
    cla_byte = '00'
    max_cmd_len = 255

    def __init__(self, data='AABB'):
        self.sent = []
        self.data = data

    def send_apdu_checksw(self, pdu):
        self.sent.append(pdu)
        return self.data, '9000'


class FakeLchan:
    def __init__(self, size=2, rec_len=2, data='AABB'):
        self.scc = FakeScc(data)
        self.size = size
        self.rec_len = rec_len

    def selected_file_size(self):
        return self.size

    def selected_file_record_len(self):
        return self.rec_len

    def read_binary(self, *args):
        raise AssertionError('pySim read_binary (re-select) used')

    def read_record(self, *args):
        raise AssertionError('pySim read_record (re-select) used')


class ReadSelectedTests(unittest.TestCase):
    def test_a_transparent_read_is_direct(self):
        lchan = FakeLchan(size=2)
        self.assertEqual(S._read_selected_ef(lchan, 'transparent'),
                         ('AABB', '9000'))
        self.assertEqual(lchan.scc.sent, ['00b0000002'])

    def test_a_transparent_file_is_chunked_at_max_cmd_len(self):
        lchan = FakeLchan(size=300)
        S._read_selected_ef(lchan, 'transparent')
        # 255 bytes at offset 0, then 45 bytes at offset 0x00ff
        self.assertEqual(lchan.scc.sent, ['00b00000ff', '00b000ff2d'])

    def test_an_empty_file_reads_nothing(self):
        lchan = FakeLchan(size=0)
        self.assertEqual(S._read_selected_ef(lchan, 'transparent'),
                         ('', '9000'))
        self.assertEqual(lchan.scc.sent, [])

    def test_a_record_read_is_direct(self):
        lchan = FakeLchan(rec_len=6)
        S._read_selected_ef(lchan, 'record', 3)
        self.assertEqual(lchan.scc.sent, ['00b2030406'])

    def test_a_missing_fcp_falls_back_to_the_pysim_helpers(self):
        lchan = FakeLchan(size=None, rec_len=None)
        lchan.read_binary = lambda *a: ('CC', '9000')
        lchan.read_record = lambda n: ('DD', '9000')
        self.assertEqual(S._read_selected_ef(lchan, 'transparent'),
                         ('CC', '9000'))
        self.assertEqual(S._read_selected_ef(lchan, 'record', 1),
                         ('DD', '9000'))


class StartupDrainPin(unittest.TestCase):
    """The startup drain must not poll STATUS again after the chain already
    returned an idle SW (v3.38.1)."""

    def test_the_drain_uses_the_chain_result(self):
        src = (pathlib.Path(S.__file__).parent / '__main__.py').read_text()
        self.assertIn('chain_sw = _handle_proactive_chain(scc, st_sw)', src)
        self.assertIn("if chain_sw and not chain_sw.startswith('91'):", src)


if __name__ == '__main__':
    unittest.main()
