#!/usr/bin/env python3
"""The release package (``tools/build_release.py`` → simple-<ver>.zip).

The zip is the READMEs' "download the release package" path.  It must carry
everything needed to run the app and none of the development-only files (the
tests, the frontend sources/build configuration, git metadata), and its PWA
files must cover what the service worker precaches - otherwise a newly added
asset would silently be missing from a release.
"""

import ast
import importlib.util
import re
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

try:                                    # Python 3.11+
    import tomllib
except ImportError:                     # pragma: no cover - older interpreters
    tomllib = None

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / 'tools' / 'build_release.py'
SERVER = REPO / 'simple_server' / 'server.py'


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
        for f in ('simple_server/__init__.py',
                  'simple_server/__main__.py',
                  'simple_server/server.py',
                  'simple_server/gsmtap.py',
                  'setup.sh', 'setup.bat', 'start.sh', 'start.bat',
                  'update.sh', 'update.bat', 'tools/update.py',
                  'tools/changelog_section.py', 'CHANGELOG.md',
                  'pyproject.toml', 'requirements.txt',
                  'README.md', 'README_RUS.md', 'LICENSE', 'THIRD-PARTY-NOTICES',
                  'docs/api.md'):
            self.assertIn(self.root + f, self.names)

    def test_the_legacy_pysim_tree_is_not_packaged(self):
        # The 4.0.0 package is the pysim-free tree; the legacy package and its
        # requirements belong to the frozen simple-pysim fork.
        for name in self.names:
            rel = name[len(self.root):]
            self.assertFalse(rel.startswith('pysim_simple_server/'), name)
            self.assertNotEqual(rel, 'requirements-pysim.txt', name)
        self.assertNotIn(self.root + 'requirements-pysim.txt', self.names)

    def test_the_shared_pysim_free_modules_ship(self):
        # Phase 3d-3b: the spec-only modules simple_server imports live in its
        # own subpackage, so the package no longer needs the legacy tree.
        self.assertIn(self.root + 'simple_server/shared/__init__.py', self.names)
        for module in ('presets', 'testscript', 'events', 'capmem',
                       'catrelease', 'netsim', 'netstate', 'outfile', 'scp81',
                       'httpota', 'test_scripts', 'test_suites'):
            self.assertIn(self.root + 'simple_server/shared/%s.py' % module,
                          self.names, module)

    def test_the_bundled_operator_list_ships(self):
        # netmon.default_mcc_mnc_path() reads these from the package
        # directory; pysim-free operation must not reach into the legacy tree.
        for f in ('simple_server/data/mcc-mnc-list.json',
                  'simple_server/data/mcc-mnc-list.LICENSE'):
            self.assertIn(self.root + f, self.names, f)

    def test_the_packaged_server_never_imports_the_legacy_tree(self):
        # The hard constraint of the split: what ships must run with no
        # pysim_simple_server (or pysim) importable.  Read with the AST, not a
        # grep, so an aliased/conditional import is caught too.
        forbidden = ('pysim_simple_server', 'pySim', 'osmocom', 'construct')
        offenders = []
        with zipfile.ZipFile(self.zip_path) as z:
            for name in self.names:
                rel = name[len(self.root):]
                if not (rel.startswith('simple_server/') and rel.endswith('.py')):
                    continue
                tree = ast.parse(z.read(name).decode('utf-8'), filename=rel)
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        names = [alias.name for alias in node.names]
                    elif isinstance(node, ast.ImportFrom):
                        names = [node.module or '']
                    else:
                        continue
                    for imported in names:
                        if imported.split('.')[0] in forbidden:
                            offenders.append('%s:%d: %s'
                                             % (rel, node.lineno, imported))
        self.assertEqual(offenders, [])

    @unittest.skipUnless(tomllib is not None, 'tomllib needs Python 3.11+')
    def test_every_console_script_module_ships_in_the_package(self):
        # setup.sh runs ``pip install --no-build-isolation -e .`` on the
        # packaged tree: a declared console script whose module is missing
        # installs an entry point that dies with ModuleNotFoundError (the
        # pre-3d-1 ``simple-server``).
        data = tomllib.loads((REPO / 'pyproject.toml').read_text(encoding='utf-8'))
        scripts = data['project'].get('scripts', {})
        self.assertIn('simple-server', scripts)
        # The legacy pySim entry point is not shipped (the fork keeps it).
        self.assertNotIn('pysim-simple-server', scripts)
        for name, target in scripts.items():
            with self.subTest(script=name):
                module = target.split(':', 1)[0]
                package = module.split('.', 1)[0]
                self.assertIn(
                    self.root + module.replace('.', '/') + '.py', self.names,
                    '%s points at the missing module %s' % (name, module))
                self.assertIn(self.root + package + '/__init__.py', self.names,
                              '%s: package %s has no __init__.py' % (name, package))

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
