#!/usr/bin/env python3
"""Tests for the cardless-startup handling in __main__.

A reader without a card is a normal state, not an initialization failure:
pySim raises NoCardError after its wait, which must be reported with a single
line and no traceback, and the pySim-internal 'Waiting for card...' /
'pySim-shell not equipped!' lines must not reach the console. Any other
failure keeps the stock-pysim fallback and its traceback.
"""

import io
import sys
import unittest
from pathlib import Path
from unittest import mock

PROJECTS = Path(__file__).resolve().parents[2]
PY_SIM = PROJECTS / 'pysim'
if str(PY_SIM) not in sys.path:
    sys.path.insert(0, str(PY_SIM))

from pySim.exceptions import NoCardError, SwMatchError

from pysim_simple_server import __main__ as srv_main
from pysim_simple_server.server import _LineFilter

CARDLESS_LINE = 'INIT: no card in the reader — server ready; insert a card or press Equip'


class LineFilterTest(unittest.TestCase):
    def test_drops_matching_lines_and_keeps_the_rest(self):
        out = io.StringIO()
        f = _LineFilter(out, ('Waiting for card...', 'pySim-shell not equipped!'))
        f.write('Waiting for card...\n')
        f.write('pySim-shell not equipped!\n')
        f.write('INIT: keep me\n')
        f.flush()
        self.assertEqual(out.getvalue(), 'INIT: keep me\n')

    def test_line_completed_by_the_next_write_is_still_dropped(self):
        out = io.StringIO()
        f = _LineFilter(out, ('Waiting for card...',))
        f.write('Waiting for card...')
        self.assertEqual(out.getvalue(), '')
        f.write('\n')
        f.write('ok\n')
        self.assertEqual(out.getvalue(), 'ok\n')

    def test_partial_line_is_flushed_when_it_does_not_match(self):
        out = io.StringIO()
        f = _LineFilter(out, ('Waiting for card...',))
        f.write('partial')
        f.flush()
        self.assertEqual(out.getvalue(), 'partial')

    def test_stream_attributes_are_proxied(self):
        out = io.StringIO()
        f = _LineFilter(out, ('x',))
        self.assertEqual(f.encoding, out.encoding)
        self.assertIs(f.isatty(), out.isatty())


class StartupNoCardTest(unittest.TestCase):
    """Exercise the init branch of main() without a reader or HTTP server."""

    def _run(self, init_side_effect, argv=()):
        fake_app = mock.Mock()
        fake_mod = mock.Mock()
        fake_mod.option_parser = _parser()
        fake_mod.PysimApp.return_value = fake_app
        fake_mod.init_card = mock.Mock(side_effect=init_side_effect)
        err = io.StringIO()
        out = io.StringIO()
        with mock.patch.object(srv_main, 'load_pysim_app', return_value=fake_mod), \
             mock.patch.object(srv_main, 'fastinit') as fake_fastinit, \
             mock.patch.object(srv_main, 'start_card_monitor'), \
             mock.patch.object(srv_main, '_read_iccid', return_value=None), \
             mock.patch.object(srv_main, '_send_terminal_profile', return_value=(None, None)), \
             mock.patch.object(srv_main, '_send_status', return_value=('', '9000')), \
             mock.patch.object(srv_main, '_build_http_server') as fake_http, \
             mock.patch.object(srv_main, 'CardHandler'), \
             mock.patch('sys.stderr', err), mock.patch('sys.stdout', out):
            fake_fastinit.init_card_fast.side_effect = init_side_effect
            fake_fastinit.install = mock.Mock()
            fake_http.return_value.serve_forever.side_effect = KeyboardInterrupt
            try:
                with mock.patch('sys.argv', ['pysim-simple-server']):
                    srv_main.main()
            except (KeyboardInterrupt, SystemExit):
                pass
        return err.getvalue(), out.getvalue(), fake_fastinit, fake_mod

    def test_no_card_reports_one_line_without_traceback(self):
        err, out, fake_fastinit, fake_mod = self._run(NoCardError())
        self.assertIn(CARDLESS_LINE, err)
        self.assertNotIn('Traceback', err)
        self.assertNotIn('falling back to pysim init', err)
        # no second (stock) init attempt after the cardless fast init
        fake_mod.init_card.assert_not_called()

    def test_real_failure_still_falls_back_with_traceback(self):
        def boom(*a, **k):
            raise SwMatchError('6a82', '9000')

        err, out, fake_fastinit, fake_mod = self._run(boom)
        self.assertIn('falling back to pysim init', err)
        self.assertIn('Traceback', err)
        self.assertIn('SwMatchError', err)
        self.assertTrue(fake_mod.init_card.called)

    def test_cardless_app_construction_mutes_pysim_internals(self):
        # PysimApp is constructed without a card: the filter must be active
        # around the call, so lines written through sys.stdout are dropped.
        seen = {}

        def make_app(**kwargs):
            seen['kwargs'] = kwargs
            sys.stdout.write('Waiting for card...\n')
            sys.stdout.write('pySim-shell not equipped!\n')
            sys.stdout.write('INIT: something else\n')
            return mock.Mock()

        fake_mod = mock.Mock()
        fake_mod.option_parser = _parser()
        fake_mod.PysimApp.side_effect = make_app
        fake_mod.init_card = mock.Mock()
        err, out = io.StringIO(), io.StringIO()
        with mock.patch.object(srv_main, 'load_pysim_app', return_value=fake_mod), \
             mock.patch.object(srv_main, 'fastinit') as fake_fastinit, \
             mock.patch.object(srv_main, 'start_card_monitor'), \
             mock.patch.object(srv_main, '_build_http_server') as fake_http, \
             mock.patch.object(srv_main, 'CardHandler'), \
             mock.patch('sys.stderr', err), mock.patch('sys.stdout', out):
            fake_fastinit.init_card_fast.side_effect = NoCardError()
            fake_fastinit.install = mock.Mock()
            fake_http.return_value.serve_forever.side_effect = KeyboardInterrupt
            try:
                with mock.patch('sys.argv', ['pysim-simple-server']):
                    srv_main.main()
            except (KeyboardInterrupt, SystemExit):
                pass
        self.assertIsNone(seen['kwargs']['card'])
        self.assertIsNone(seen['kwargs']['rs'])
        self.assertNotIn('Waiting for card...', out.getvalue())
        self.assertNotIn('pySim-shell not equipped!', out.getvalue())
        self.assertIn('INIT: something else', out.getvalue())


def _parser():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument('-p', '--pcsc-dev', type=int, default=0)
    p.add_argument('--pcsc-regex', default=None)
    p.add_argument('--apdu-trace', action='store_true', default=False)
    p.add_argument('--verbose', action='store_true', default=False)
    return p


if __name__ == '__main__':
    unittest.main()
