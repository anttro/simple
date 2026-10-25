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

REM The address the opening line names (v3.38.2).  The server parses the same
REM flags; this mirrors its last-one-wins rule for the echo.  %* is not
REM affected by SHIFT, so the server still receives every argument.
set "HTTP_HOST=127.0.0.1"
set "HTTP_PORT=8080"
:parse_http_args
if "%~1"=="" goto http_args_done
if /i "%~1"=="--http-host" (
    set "HTTP_HOST=%~2"
    shift
    shift
    goto parse_http_args
)
if /i "%~1"=="--http-port" (
    set "HTTP_PORT=%~2"
    shift
    shift
    goto parse_http_args
)
set "ARG=%~1"
if /i "%ARG:~0,12%"=="--http-host=" set "HTTP_HOST=%ARG:~12%"
if /i "%ARG:~0,12%"=="--http-port=" set "HTTP_PORT=%ARG:~12%"
shift
goto parse_http_args
:http_args_done

if exist "%VENV_DIR%\Scripts\pysim-simple-server.exe" (
    echo Starting pysim-simple-server from venv on http://%HTTP_HOST%:%HTTP_PORT%
    echo Log: simple_lastrun.log (overwritten on each start^). Press Ctrl+C to stop.
    "%VENV_DIR%\Scripts\pysim-simple-server.exe" --http-port 8080 -p 0 %* 2>&1 | powershell -NoProfile -Command "$input | Tee-Object -FilePath '%LOG%'"
    goto :eof
)

if exist "%~dp0pysim_simple_server\__main__.py" (
    echo Starting pysim-simple-server from source on http://%HTTP_HOST%:%HTTP_PORT%
    echo Log: simple_lastrun.log (overwritten on each start^). Press Ctrl+C to stop.
    python -m pysim_simple_server --http-port 8080 -p 0 %* 2>&1 | powershell -NoProfile -Command "$input | Tee-Object -FilePath '%LOG%'"
    goto :eof
)

where pysim-simple-server >nul 2>&1
if %errorlevel% equ 0 (
    echo Starting pysim-simple-server on http://%HTTP_HOST%:%HTTP_PORT%
    echo Log: simple_lastrun.log (overwritten on each start^). Press Ctrl+C to stop.
    pysim-simple-server --http-port 8080 -p 0 %* 2>&1 | powershell -NoProfile -Command "$input | Tee-Object -FilePath '%LOG%'"
    goto :eof
)

echo Error: pysim-simple-server not installed.
echo Run setup.bat first or install manually:
echo   pip install pysim-simple-server
pause
