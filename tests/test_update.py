#!/usr/bin/env python3
"""Updating a packaged install in place (tools/update.py).

The update replaces the application files, keeps the .venv (the runtime
dependencies - the slow part of a reinstall) and the run log, preserves the
scripts' exec bits, and refuses a package whose version does not match or that
carries unsafe paths.  A **major-version jump** is refused as well: the 4.0.0
cutover replaced the backend (the frozen 3.x tool is the ``simple-pysim``
fork), so a 3.x install must never be half-migrated.
``tests/test_release_package.py`` pins that the update scripts are in the
package.
"""

import importlib.util
import os
import re
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
UPDATE = REPO / 'tools' / 'update.py'
CHANGELOG_SECTION = REPO / 'tools' / 'changelog_section.py'


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _package(path, version, entries=None):
    """A minimal 4.x release package: simple-<version>/... with the real updater."""
    root = 'simple-%s/' % version
    with zipfile.ZipFile(path, 'w') as z:
        def add(name, data, mode=0o644):
            info = zipfile.ZipInfo(root + name)
            info.external_attr = mode << 16
            z.writestr(info, data)
        add('simple_server/server.py', "VERSION = '%s'\n" % version)
        add('README.md', '# readme %s\n' % version)
        add('update.sh', '#!/bin/sh\n', 0o755)
        add('tools/update.py', UPDATE.read_text(encoding='utf-8'))
        for name, data in (entries or {}).items():
            add(name, data)
    return path


def _app(tmp, version='1.0.0', legacy=False):
    """A simulated packaged install: app files, a .venv and the run log.

    ``legacy`` builds a 3.x layout (``pysim_simple_server``), the shape the
    major-version guard exists for.
    """
    app = Path(tmp) / 'app'
    (app / 'tools').mkdir(parents=True)
    (app / 'tools' / 'update.py').write_text(UPDATE.read_text(encoding='utf-8'))
    package = 'pysim_simple_server' if legacy else 'simple_server'
    (app / package).mkdir()
    (app / package / 'server.py').write_text("VERSION = '%s'\n" % version)
    (app / 'README.md').write_text('# old\n')
    (app / '.venv').mkdir()
    (app / '.venv' / 'MARKER').write_text('venv')
    (app / 'simple_lastrun.log').write_text('log')
    return app


class ReinstallWarningFilterTests(unittest.TestCase):
    """The re-install filters setuptools' unconditional editable warning."""

    def test_the_reinstall_passes_the_warning_filter(self):
        src = UPDATE.read_text(encoding='utf-8')
        # setuptools emits "Editable installation." for every editable build;
        # the filter is appended to an inherited PYTHONWARNINGS instead of
        # replacing it, and the child gets a copy of the environment (its
        # same-name-folder advice cannot apply - the package is simple_server).
        self.assertIn("os.environ.get('PYTHONWARNINGS')", src)
        self.assertIn(",ignore:Editable installation'", src)
        self.assertIn('dict(os.environ, PYTHONWARNINGS=', src)
        self.assertIn('env=env', src)


class InPlaceUpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.app = _app(self.tmp.name)
        self.zip = _package(self.dir / 'simple-1.1.0.zip', '1.1.0',
                            {'frontend/new-asset.txt': 'new'})

    def tearDown(self):
        self.tmp.cleanup()

    def run_update(self, *args):
        return subprocess.run(
            [sys.executable, str(self.app / 'tools' / 'update.py')] + list(args),
            capture_output=True, text=True)

    def test_the_files_are_replaced_and_the_venv_and_log_kept(self):
        r = self.run_update('--zip', str(self.zip), '--no-pip')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("VERSION = '1.1.0'",
                      (self.app / 'simple_server' / 'server.py').read_text())
        self.assertEqual((self.app / 'README.md').read_text(), '# readme 1.1.0\n')
        self.assertEqual((self.app / 'frontend' / 'new-asset.txt').read_text(), 'new')
        # the dependencies live in .venv - they must survive untouched, as does
        # the run log
        self.assertEqual((self.app / '.venv' / 'MARKER').read_text(), 'venv')
        self.assertEqual((self.app / 'simple_lastrun.log').read_text(), 'log')
        self.assertIn('restart the server', r.stdout)

    @unittest.skipIf(os.name == 'nt', 'POSIX permission bits')
    def test_the_scripts_keep_their_exec_bits(self):
        self.run_update('--zip', str(self.zip), '--no-pip')
        # zipfile drops the mode on extraction; the updater restores it
        self.assertTrue(os.access(self.app / 'update.sh', os.X_OK))

    def test_the_same_version_is_not_reapplied_unless_forced(self):
        self.run_update('--zip', str(self.zip), '--no-pip')
        r = self.run_update('--zip', str(self.zip), '--no-pip')
        self.assertEqual(r.returncode, 0)
        self.assertIn('already up to date', r.stdout)
        (self.app / 'README.md').write_text('# edited\n')
        r = self.run_update('--zip', str(self.zip), '--no-pip', '--force')
        self.assertEqual(r.returncode, 0)
        self.assertEqual((self.app / 'README.md').read_text(), '# readme 1.1.0\n')

    def test_check_reports_both_versions_without_touching_anything(self):
        r = self.run_update('--check', '--version', '1.1.0')
        self.assertEqual(r.returncode, 0)
        self.assertIn('current: 1.0.0', r.stdout)
        self.assertIn('latest:  1.1.0', r.stdout)
        self.assertEqual((self.app / 'README.md').read_text(), '# old\n')

    def test_a_version_mismatch_is_refused(self):
        bad = self.dir / 'simple-1.9.9.zip'
        _package(bad, '1.8.8')          # the file name says 1.9.9
        r = self.run_update('--zip', str(bad), '--no-pip', '--version', '1.9.9')
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('does not match', r.stderr)
        self.assertEqual((self.app / 'simple_server' / 'server.py').read_text(),
                         "VERSION = '1.0.0'\n")

    def test_an_unsafe_path_is_refused(self):
        bad = self.dir / 'simple-1.1.0-evil.zip'
        with zipfile.ZipFile(bad, 'w') as z:
            z.writestr('simple-1.1.0/../../evil.txt', 'x')
            z.writestr('simple-1.1.0/simple_server/server.py', "VERSION = '1.1.0'\n")
        r = self.run_update('--zip', str(bad), '--no-pip')
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('unsafe path', r.stderr)
        self.assertFalse((self.dir / 'evil.txt').exists())


class MajorVersionGuardTests(unittest.TestCase):
    """A 3.x (pySim) install must never be half-updated to the 4.x tree."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.app = _app(self.tmp.name, version='3.40.2', legacy=True)
        self.zip = _package(self.dir / 'simple-4.0.0.zip', '4.0.0')

    def tearDown(self):
        self.tmp.cleanup()

    def run_update(self, *args):
        return subprocess.run(
            [sys.executable, str(self.app / 'tools' / 'update.py')] + list(args),
            capture_output=True, text=True)

    def test_check_refuses_and_names_the_legacy_fork(self):
        r = self.run_update('--check', '--version', '4.0.0')
        self.assertEqual(r.returncode, 2)
        self.assertIn('current: 3.40.2', r.stdout)
        self.assertIn('latest:  4.0.0', r.stdout)
        self.assertIn('refusing a major-version update', r.stderr)
        self.assertIn('simple-pysim', r.stderr)
        self.assertIn('3.x', r.stderr)

    def test_the_update_is_refused_before_anything_is_copied(self):
        r = self.run_update('--zip', str(self.zip), '--no-pip')
        self.assertEqual(r.returncode, 2)
        self.assertIn('refusing a major-version update', r.stderr)
        self.assertIn('simple-pysim', r.stderr)
        # the legacy tree is untouched and the new package was not merged in
        self.assertEqual((self.app / 'pysim_simple_server' / 'server.py').read_text(),
                         "VERSION = '3.40.2'\n")
        self.assertFalse((self.app / 'simple_server').exists())
        self.assertEqual((self.app / 'README.md').read_text(), '# old\n')

    def test_force_cannot_bypass_the_guard(self):
        r = self.run_update('--zip', str(self.zip), '--no-pip', '--force')
        self.assertEqual(r.returncode, 2)
        self.assertFalse((self.app / 'simple_server').exists())


class ChangelogSectionTests(unittest.TestCase):
    def setUp(self):
        self.mod = _load(CHANGELOG_SECTION, 'changelog_section')

    def test_the_current_version_has_a_section(self):
        # the server version is the single source of truth
        text = (REPO / 'simple_server' / 'server.py').read_text(encoding='utf-8')
        ver = re.search(r"^VERSION = '([^']+)'$", text, re.M).group(1)
        body = self.mod.section(ver)
        self.assertIsNotNone(body, 'CHANGELOG.md has no [%s] section' % ver)
        self.assertIn('### ', body)

    def test_an_unknown_version_has_no_section(self):
        self.assertIsNone(self.mod.section('0.0.0'))

    def test_the_helper_exits_non_zero_for_an_unknown_version(self):
        r = subprocess.run([sys.executable, str(CHANGELOG_SECTION), '0.0.0'],
                           capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)


if __name__ == '__main__':
    unittest.main()
