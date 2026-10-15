#!/usr/bin/env python3
"""Tests for the Windows console-encoding guard (v3.30.3).

Under start.bat the server's stdout/stderr are pipes into PowerShell's
Tee-Object, so Python encodes with the ANSI code page instead of the wide
console API.  The banner's box-drawing rule (U+2500) and the APDU-trace arrows
(U+2192) are not encodable in cp1251 (Russian Windows), and one such character
raised UnicodeEncodeError and killed the process before it could listen.  The
guard sets errors="replace" on both streams and the banner picks the widest
rule character its stream can encode.
"""

import ast
import inspect
import io
import pathlib
import sys
import unittest

from pysim_simple_server import __main__ as srv_main


class ConsoleEncodingTests(unittest.TestCase):
    def setUp(self):
        self._stdout, self._stderr = sys.stdout, sys.stderr

    def tearDown(self):
        sys.stdout, sys.stderr = self._stdout, self._stderr

    @staticmethod
    def _pipe(encoding='cp1251'):
        """A strict text stream over a pipe-like buffer - what start.bat gives
        the server on Windows (a pipe, so Python uses the ANSI code page)."""
        raw = io.BytesIO()
        return io.TextIOWrapper(raw, encoding=encoding), raw

    def test_a_strict_cp1251_pipe_dies_on_the_banner(self):
        # the control: the pre-fix failure is real, so the tests below are
        # meaningful (a fix that forgets the rule would be caught here)
        out, _ = self._pipe()
        with self.assertRaises(UnicodeEncodeError):
            out.write('\u2500' * 70)

    def test_the_rule_falls_back_to_ascii_on_a_cp1251_pipe(self):
        out, _ = self._pipe()
        sys.stdout = out
        rule = srv_main._console_rule()
        self.assertEqual(rule, '-' * 70)
        self.assertTrue(rule.isascii())

    def test_the_rule_stays_pretty_on_a_utf8_stream(self):
        out, _ = self._pipe(encoding='utf-8')
        sys.stdout = out
        self.assertEqual(srv_main._console_rule(), '\u2500' * 70)

    def test_the_guard_keeps_the_banner_and_the_traces_alive(self):
        out, out_raw = self._pipe()
        err, err_raw = self._pipe()
        sys.stdout, sys.stderr = out, err
        srv_main._guard_console_streams()
        print(srv_main._console_rule())                 # the listening banner
        sys.stderr.write('CMD: EQUIP \u2192 ok\n')      # the APDU trace
        sys.stderr.write('CAP-COMPAT: x \u2014 y\n')    # the CAP em dash
        out.flush()
        err.flush()
        self.assertEqual(out_raw.getvalue().decode('cp1251').strip(), '-' * 70)
        # the arrow cannot be encoded - it prints as '?', never raising; the
        # em dash is in cp1251 and stays
        self.assertEqual(err_raw.getvalue().decode('cp1251').splitlines(),
                         ['CMD: EQUIP ? ok', 'CAP-COMPAT: x \u2014 y'])

    def test_the_guard_tolerates_streams_without_reconfigure(self):
        sys.stdout = io.StringIO()
        srv_main._guard_console_streams()               # must not raise
        print('still works')
        self.assertEqual(sys.stdout.getvalue(), 'still works\n')

    def test_main_guards_the_streams_and_uses_the_rule(self):
        src = inspect.getsource(srv_main.main)
        self.assertIn('_guard_console_streams()', src)
        self.assertEqual(src.count('print(_console_rule())'), 2)
        # the raw pretty character must not come back to the banner
        self.assertNotIn('\u2500', src)

    def test_no_console_write_carries_a_non_ascii_literal(self):
        """Every print()/stdout/stderr write in the package stays ASCII: a
        character the ANSI code page lacks would kill a piped start.bat run
        (the guard degrades it to '?', but the traces should stay readable)."""
        pkg = pathlib.Path(srv_main.__file__).resolve().parent
        offenders = []
        for path in sorted(pkg.glob('*.py')):
            tree = ast.parse(path.read_text(encoding='utf-8'))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                f = node.func
                is_print = isinstance(f, ast.Name) and f.id == 'print'
                is_write = (isinstance(f, ast.Attribute) and f.attr == 'write'
                            and isinstance(f.value, ast.Attribute)
                            and f.value.attr in ('stdout', 'stderr'))
                if not (is_print or is_write):
                    continue
                for a in ast.walk(node):
                    if isinstance(a, ast.Constant) and isinstance(a.value, str):
                        bad = ''.join(c for c in a.value if ord(c) > 127)
                        if bad:
                            offenders.append('%s:%d: %r'
                                             % (path.name, node.lineno, bad))
        self.assertEqual(offenders, [])


if __name__ == '__main__':
    unittest.main()
