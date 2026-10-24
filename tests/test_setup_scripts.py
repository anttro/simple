"""The setup scripts recover from an incomplete .venv (v3.38.1).

A failed venv creation (the Debian/Ubuntu python3-venv package missing) used to
leave the .venv directory behind; the next run saw it, skipped creation and
failed to activate a venv without activate/pip.
"""

import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class SetupShTests(unittest.TestCase):
    def setUp(self):
        self.src = (ROOT / 'setup.sh').read_text()

    def test_an_incomplete_venv_is_removed_before_creation(self):
        self.assertIn('Removing an incomplete virtual environment', self.src)
        self.assertIn('rm -rf "$VENV_DIR"', self.src)
        self.assertIn('bin/activate', self.src)
        self.assertIn('bin/python', self.src)
        self.assertIn('bin/pip', self.src)

    def test_a_failed_creation_removes_the_partial_dir_and_guides(self):
        self.assertIn('if ! python3 -m venv "$VENV_DIR"; then', self.src)
        self.assertIn('python3-venv', self.src)
        fails = self.src[self.src.index('if ! python3 -m venv'):]
        self.assertLess(fails.index('rm -rf "$VENV_DIR"'),
                        fails.index('could not create the virtual environment'))

    def test_the_script_is_valid_bash(self):
        bash = shutil.which('bash')
        if not bash:
            self.skipTest('bash not available')
        res = subprocess.run([bash, '-n', str(ROOT / 'setup.sh')],
                             capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)


class SetupBatTests(unittest.TestCase):
    def setUp(self):
        self.src = (ROOT / 'setup.bat').read_text()

    def test_an_incomplete_venv_is_removed_before_creation(self):
        self.assertIn('Removing an incomplete virtual environment', self.src)
        self.assertIn('rmdir /s /q "%VENV_DIR%"', self.src)
        for marker in ('Scripts\\activate.bat', 'Scripts\\python.exe',
                       'Scripts\\pip.exe'):
            self.assertIn(marker, self.src)

    def test_a_failed_creation_removes_the_partial_dir_and_guides(self):
        self.assertIn('if !errorlevel! neq 0', self.src)
        self.assertIn('python -m venv "%VENV_DIR%"', self.src)
        fails = self.src[self.src.index('if !errorlevel! neq 0'):]
        self.assertLess(fails.index('rmdir /s /q "%VENV_DIR%"'),
                        fails.index('could not create the virtual environment'))


if __name__ == '__main__':
    unittest.main()
