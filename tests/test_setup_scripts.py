"""The setup/start scripts (pysim-free since 4.0.0) and the venv recovery.

A failed venv creation (the Debian/Ubuntu python3-venv package missing) used to
leave the .venv directory behind; the next run saw it, skipped creation and
failed to activate a venv without activate/pip (v3.38.1).  The 4.0.0 scripts
install the pysim-free runtime dependencies (``requirements.txt``) and launch
the ``simple-server`` console script - no pysim, no serial reader flags.
"""

import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class RequirementsTests(unittest.TestCase):
    def test_the_pysim_free_dependency_floor(self):
        lines = [ln.strip() for ln in
                 (ROOT / 'requirements.txt').read_text().splitlines()]
        deps = [ln for ln in lines if ln and not ln.startswith('#')]
        self.assertEqual(deps, ['pyscard', 'pycryptodomex', 'gsm0338'])
        self.assertNotIn('requirements-pysim.txt', deps)


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

    def test_windows_guard_points_to_setup_bat(self):
        self.assertIn('microsoft /proc/version', self.src)
        self.assertIn('MINGW|MSYS|CYGWIN', self.src)
        self.assertIn('Use setup.bat from a Windows command prompt', self.src)

    def test_the_pysim_free_dependencies_are_installed(self):
        self.assertIn('install -r "$SCRIPT_DIR/requirements.txt"', self.src)
        self.assertIn('install --no-build-isolation -e "$SCRIPT_DIR"', self.src)
        # no pysim checkout, no legacy requirement file, no git dependency
        self.assertNotIn('git+https://github.com/osmocom/pysim.git', self.src)
        self.assertNotIn('requirements-pysim.txt', self.src)
        self.assertNotIn('Installing pysim', self.src)
        self.assertNotIn('command -v git', self.src)

    def test_the_scp81_python_313_note_survives(self):
        self.assertIn('the SCP81 TLS-PSK listener needs Python 3.13+', self.src)
        self.assertIn('libpcsclite-dev', self.src)


class SetupBatTests(unittest.TestCase):
    def setUp(self):
        self.src = (ROOT / 'setup.bat').read_text()

    def test_an_incomplete_venv_is_removed_before_creation(self):
        self.assertIn('Removing an incomplete virtual environment', self.src)
        self.assertIn('rmdir /s /q "%VENV_DIR%"', self.src)
        for marker in ('Scripts\\activate.bat', 'Scripts\\python.exe',
                       'Scripts\\pip.exe'):
            self.assertIn(marker, self.src)

    def test_echo_inside_a_block_escapes_its_parentheses(self):
        # The removal notice sits inside `if not defined VENV_OK (...)`; an
        # unescaped ')' there ends the cmd.exe block early and breaks the whole
        # script (live: "Непредвиденное появление: ...." on Windows).  Both
        # parentheses must be ^-escaped.
        self.assertIn('Removing an incomplete virtual environment ^(a previous '
                      'setup did not finish^)', self.src)
        self.assertNotIn('Removing an incomplete virtual environment (a previous '
                         'setup did not finish)', self.src)

    def test_a_failed_creation_removes_the_partial_dir_and_guides(self):
        self.assertIn('if !errorlevel! neq 0', self.src)
        self.assertIn('python -m venv "%VENV_DIR%"', self.src)
        fails = self.src[self.src.index('if !errorlevel! neq 0'):]
        self.assertLess(fails.index('rmdir /s /q "%VENV_DIR%"'),
                        fails.index('could not create the virtual environment'))

    def test_the_pysim_free_dependencies_are_installed(self):
        self.assertIn('pip install -r "%~dp0requirements.txt"', self.src)
        self.assertIn('pip install --no-build-isolation -e "%~dp0."', self.src)
        self.assertNotIn('git+https://github.com/osmocom/pysim.git', self.src)
        self.assertNotIn('requirements-pysim.txt', self.src)
        self.assertNotIn('Installing pysim', self.src)


class StartShTests(unittest.TestCase):
    def setUp(self):
        self.src = (ROOT / 'start.sh').read_text()

    def test_the_script_is_valid_bash(self):
        bash = shutil.which('bash')
        if not bash:
            self.skipTest('bash not available')
        res = subprocess.run([bash, '-n', str(ROOT / 'start.sh')],
                             capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)

    def test_it_launches_the_pysim_free_entry_point(self):
        self.assertIn('$VENV_DIR/bin/simple-server', self.src)
        self.assertIn('command -v simple-server', self.src)
        self.assertIn('simple_server/__main__.py', self.src)
        self.assertIn('-m simple_server --http-port 8080 "$@"', self.src)
        self.assertNotIn('pysim_simple_server', self.src)
        self.assertNotIn('pysim-simple-server', self.src)

    def test_the_removed_serial_and_slot_flags_are_gone(self):
        # 4.0.0 is PC/SC-only: -d/--device and the -p slot form are gone;
        # a reader is named with -r/--reader (passed through).
        self.assertNotIn('-p 0', self.src)
        self.assertNotIn('--device', self.src)
        self.assertNotIn('SERIAL', self.src)

    def test_it_keeps_the_http_port_and_the_tee(self):
        self.assertIn('--http-port 8080 "$@"', self.src)
        self.assertIn('tee "$LOG_FILE"', self.src)
        self.assertIn('simple_lastrun.log', self.src)


class StartBatTests(unittest.TestCase):
    def setUp(self):
        self.src = (ROOT / 'start.bat').read_text()

    def test_it_launches_the_pysim_free_entry_point(self):
        self.assertIn(r'%VENV_DIR%\Scripts\simple-server.exe', self.src)
        self.assertIn('python -m simple_server', self.src)
        self.assertNotIn('pysim_simple_server', self.src)
        self.assertNotIn('pysim-simple-server', self.src)

    def test_the_removed_slot_flag_is_gone(self):
        self.assertNotIn('-p 0', self.src)

    def test_it_keeps_the_http_port_and_the_tee(self):
        self.assertIn('--http-port 8080', self.src)
        self.assertIn('Tee-Object', self.src)


if __name__ == '__main__':
    unittest.main()
