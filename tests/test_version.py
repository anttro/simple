#!/usr/bin/env python3
"""The release version moves together: server, pyproject and the PWA.

A functional change bumps ``simple_server/server.py``'s ``VERSION``,
``pyproject.toml``'s ``version`` and the PWA's ``SIMPLE_VERSION`` together,
and an ``index.html`` change bumps the service worker's ``CACHE`` (
AGENTS.md).  The frontend suite pins the same three values from the Node side
(``frontend/tests/version.test.js``).
"""

import re
import unittest
from pathlib import Path

try:                                    # Python 3.11+
    import tomllib
except ImportError:                     # pragma: no cover - older interpreters
    tomllib = None

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / 'simple_server' / 'server.py'
PYPROJECT = ROOT / 'pyproject.toml'
INDEX = ROOT / 'frontend' / 'index.html'
SW = ROOT / 'frontend' / 'sw.js'


def server_version():
    text = SERVER.read_text(encoding='utf-8')
    m = re.search(r"^VERSION = '([^']+)'$", text, re.M)
    if not m:
        raise AssertionError('VERSION not found in simple_server/server.py')
    return m.group(1)


class VersionAlignmentTests(unittest.TestCase):
    def test_the_server_pyproject_and_pwa_agree(self):
        if tomllib is None:
            self.skipTest('tomllib needs Python 3.11+')
        version = server_version()
        data = tomllib.loads(PYPROJECT.read_text(encoding='utf-8'))
        self.assertEqual(data['project']['version'], version)
        m = re.search(r"const SIMPLE_VERSION = '([^']+)';",
                      INDEX.read_text(encoding='utf-8'))
        self.assertIsNotNone(m, 'SIMPLE_VERSION not found in frontend/index.html')
        self.assertEqual(m.group(1), version)

    def test_it_is_the_pysim_free_major_release(self):
        self.assertEqual(server_version().split('.', 1)[0], '4')

    def test_the_distribution_is_named_simple_with_one_console_script(self):
        if tomllib is None:
            self.skipTest('tomllib needs Python 3.11+')
        data = tomllib.loads(PYPROJECT.read_text(encoding='utf-8'))
        self.assertEqual(data['project']['name'], 'simple')
        self.assertEqual(data['project']['scripts'],
                         {'simple-server': 'simple_server.__main__:main'})

    def test_the_service_worker_cache_is_a_bumped_version(self):
        m = re.search(r"const CACHE = 'simple-v(\d+)';", SW.read_text(encoding='utf-8'))
        self.assertIsNotNone(m, 'the sw.js CACHE name is not the versioned form')
        self.assertGreaterEqual(int(m.group(1)), 499)


if __name__ == '__main__':
    unittest.main()
