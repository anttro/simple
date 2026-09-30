@echo off
REM pysim-simple-server start script for Windows
REM Starts the server, preferring the venv if it exists.
REM PC/SC is built into Windows, so defaults to reader 0.
REM Every run is mirrored to simple_lastrun.log (overwritten on each start) with
REM PowerShell Tee-Object, so the latest server output can be read without
REM copy-pasting the console.  The log contains card keys (test cards) and is
REM gitignored.  (Windows PowerShell 5.1 writes it as UTF-16; PowerShell 7+ as
REM UTF-8.)

set VENV_DIR=%~dp0.venv
set LOG=%~dp0simple_lastrun.log
set PYTHONUNBUFFERED=1

if exist "%VENV_DIR%\Scripts\pysim-simple-server.exe" (
    echo Starting pysim-simple-server from venv on http://127.0.0.1:8080
    echo Log: simple_lastrun.log (overwritten on each start^). Press Ctrl+C to stop.
    "%VENV_DIR%\Scripts\pysim-simple-server.exe" --http-port 8080 -p 0 %* 2>&1 | powershell -NoProfile -Command "$input | Tee-Object -FilePath '%LOG%'"
    goto :eof
)

if exist "%~dp0pysim_simple_server\__main__.py" (
    echo Starting pysim-simple-server from source on http://127.0.0.1:8080
    echo Log: simple_lastrun.log (overwritten on each start^). Press Ctrl+C to stop.
    python -m pysim_simple_server --http-port 8080 -p 0 %* 2>&1 | powershell -NoProfile -Command "$input | Tee-Object -FilePath '%LOG%'"
    goto :eof
)

where pysim-simple-server >nul 2>&1
if %errorlevel% equ 0 (
    echo Starting pysim-simple-server on http://127.0.0.1:8080
    echo Log: simple_lastrun.log (overwritten on each start^). Press Ctrl+C to stop.
    pysim-simple-server --http-port 8080 -p 0 %* 2>&1 | powershell -NoProfile -Command "$input | Tee-Object -FilePath '%LOG%'"
    goto :eof
)

echo Error: pysim-simple-server not installed.
echo Run setup.bat first or install manually:
echo   pip install pysim-simple-server
pause
