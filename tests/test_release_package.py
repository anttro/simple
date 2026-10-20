#!/usr/bin/env python3
"""The release package (``tools/build_release.py`` → simple-<ver>.zip).

The zip is the READMEs' "download the release package" path.  It must carry
everything needed to run the app and none of the development-only files (the
tests, the frontend sources/build configuration, git metadata), and its PWA
files must cover what the service worker precaches - otherwise a newly added
asset would silently be missing from a release.
"""

import importlib.util
import re
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / 'tools' / 'build_release.py'
SERVER = REPO / 'pysim_simple_server' / 'server.py'


def _load_builder():
    spec = importlib.util.spec_from_file_location('build_release', SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _version():
    m = re.search(r"^VERSION = '([^']+)'$", SERVER.read_text(encoding='utf-8'), re.M)
    return m.group(1)


def _sw_precache():
    """The PWA files sw.js precaches (the shell a release must be able to serve)."""
    text = (REPO / 'frontend' / 'sw.js').read_text(encoding='utf-8')
    m = re.search(r'const URLS = \[(.*?)\];', text, re.S)
    return re.findall(r"'([^']+)'", m.group(1))


class ReleasePackageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.builder = _load_builder()
        cls.version = _version()
        cls.tmp = tempfile.TemporaryDirectory()
        cls.zip_path = cls.builder.build(cls.tmp.name)
        with zipfile.ZipFile(cls.zip_path) as z:
            cls.names = z.namelist()
        cls.root = 'simple-%s/' % cls.version

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_the_package_is_named_after_the_server_version(self):
        self.assertEqual(self.zip_path.name, 'simple-%s.zip' % self.version)

    def test_a_single_top_level_directory(self):
        self.assertTrue(all(n.startswith(self.root) for n in self.names), 'stray top-level entries')
        self.assertIn(self.root + 'pyproject.toml', self.names)

    def test_the_runtime_files_are_present(self):
        for f in ('pysim_simple_server/server.py',
                  'pysim_simple_server/__main__.py',
                  'pysim_simple_server/data/mcc-mnc-list.json',
                  'pysim_simple_server/data/mcc-mnc-list.LICENSE',
                  'setup.sh', 'setup.bat', 'start.sh', 'start.bat',
                  'update.sh', 'update.bat', 'tools/update.py',
                  'tools/changelog_section.py', 'CHANGELOG.md',
                  'pyproject.toml', 'requirements-pysim.txt',
                  'README.md', 'README_RUS.md', 'LICENSE', 'THIRD-PARTY-NOTICES',
                  'docs/api.md'):
            self.assertIn(self.root + f, self.names)

    def test_every_frontend_runtime_file_is_present(self):
        for f in self.builder.FRONTEND_FILES:
            self.assertIn(self.root + 'frontend/' + f, self.names, f)

    def test_the_service_worker_precache_is_covered(self):
        for f in _sw_precache():
            self.assertIn(self.root + 'frontend/' + f, self.names,
                          'sw.js precaches %s but the package lacks it' % f)

    def test_no_development_files(self):
        for n in self.names:
            rel = n[len(self.root):]
            self.assertFalse(rel.startswith('tests/'), n)
            self.assertFalse(rel.startswith('frontend/src/'), n)
            self.assertFalse(rel.startswith('frontend/tests/'), n)
            for dev in ('frontend/package.json', 'frontend/package-lock.json',
                        'frontend/tailwind.config.js', 'frontend/postcss.config.js'):
                self.assertNotEqual(rel, dev, n)
            self.assertNotIn('node_modules/', rel, n)
            self.assertNotIn('.git/', rel, n)
            self.assertNotIn('__pycache__', rel, n)
            self.assertFalse(rel.endswith('.pyc'), n)

    def test_no_bytecode_or_temp_files(self):
        basenames = [n.rsplit('/', 1)[-1] for n in self.names]
        self.assertNotIn('.DS_Store', basenames)
        self.assertFalse(any(b.endswith('~') for b in basenames))

    def test_expect_version_guard(self):
        # CI passes the tag without "v"; a mismatch must fail the build before
        # anything is published.
        r = subprocess.run([sys.executable, str(SCRIPT), '--expect-version', '0.0.0',
                            '--out', self.tmp.name], capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('version mismatch', r.stderr)


if __name__ == '__main__':
    unittest.main()
