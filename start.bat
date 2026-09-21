@echo off
REM pysim-simple-server start script for Windows
REM Starts the server, preferring the venv if it exists.
REM PC/SC is built into Windows, so defaults to reader 0.

set VENV_DIR=%~dp0.venv

if exist "%VENV_DIR%\Scripts\pysim-simple-server.exe" (
    echo Starting pysim-simple-server from venv on http://127.0.0.1:8080
    echo Press Ctrl+C to stop.
    "%VENV_DIR%\Scripts\pysim-simple-server.exe" --http-port 8080 -p 0 %*
    goto :eof
)

if exist "%~dp0pysim_simple_server\__main__.py" (
    echo Starting pysim-simple-server from source on http://127.0.0.1:8080
    echo Press Ctrl+C to stop.
    python -m pysim_simple_server --http-port 8080 -p 0 %*
    goto :eof
)

where pysim-simple-server >nul 2>&1
if %errorlevel% equ 0 (
    echo Starting pysim-simple-server on http://127.0.0.1:8080
    echo Press Ctrl+C to stop.
    pysim-simple-server --http-port 8080 -p 0 %*
    goto :eof
)

echo Error: pysim-simple-server not installed.
echo Run setup.bat first or install manually:
echo   pip install pysim-simple-server
pause