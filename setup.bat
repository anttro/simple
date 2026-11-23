@echo off
REM SIMple setup script for Windows
REM Creates a venv and installs the server and its runtime dependencies.
REM The legacy pySim-based 3.x line lives in the simple-pysim fork.

setlocal enabledelayedexpansion

set VENV_DIR=%~dp0.venv

echo === SIMple setup ===
echo.

REM Check Python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo Error: Python is required but not found.
    echo Install Python 3 from https://python.org ^(3.13 strongly recommended^)
    pause
    exit /b 1
)

echo Found Python
python --version
echo.

REM Create the venv.  A failed creation leaves a partial .venv behind: remove
REM it so the next run starts over (v3.38.1).
set "VENV_OK="
if exist "%VENV_DIR%" (
    set "VENV_OK=1"
    if not exist "%VENV_DIR%\Scripts\activate.bat" set "VENV_OK="
    if not exist "%VENV_DIR%\Scripts\python.exe" set "VENV_OK="
    if not exist "%VENV_DIR%\Scripts\pip.exe" set "VENV_OK="
    if not defined VENV_OK (
        echo Removing an incomplete virtual environment ^(a previous setup did not finish^)...
        rmdir /s /q "%VENV_DIR%"
    )
)
if not exist "%VENV_DIR%" (
    echo Creating virtual environment...
    python -m venv "%VENV_DIR%"
    if !errorlevel! neq 0 (
        if exist "%VENV_DIR%" rmdir /s /q "%VENV_DIR%"
        echo Error: could not create the virtual environment.
        echo   Make sure the Python installation includes venv and pip.
        echo   Run the Python installer, choose Modify, enable pip/venv, then run setup.bat again.
        pause
        exit /b 1
    )
)
if not exist "%VENV_DIR%\Scripts\activate.bat" (
    echo Error: the virtual environment has no Scripts\activate.bat.
    pause
    exit /b 1
)
call "%VENV_DIR%\Scripts\activate.bat"

REM Upgrade pip and the build backend.  The editable install below runs with
REM --no-build-isolation (the package's build-system requirements), so
REM setuptools/wheel must be in the venv.
python -m pip install --upgrade pip setuptools wheel -q

REM Python 3.13 is strongly recommended: pyscard ships precompiled wheels up
REM to 3.13 and the SCP81 TLS-PSK listener needs 3.13+.  On 3.14 the pyscard
REM wheel is unavailable and it builds from source (needs MSVC).
echo Note: Python 3.13 is strongly recommended.
echo       pyscard has precompiled wheels up to 3.13 and the SCP81 TLS-PSK
echo       listener needs 3.13+.  On Python 3.14 the pyscard wheel is
echo       unavailable; it builds from source and requires Microsoft C++
echo       Build Tools ("Desktop development with C++").
echo.

REM Install the runtime dependencies (pyscard, pycryptodomex, gsm0338).
echo === Installing dependencies ===
pip install -r "%~dp0requirements.txt"
if %errorlevel% neq 0 (
    echo Error: Failed to install the dependencies.
    pause
    exit /b 1
)
echo.

REM Install SIMple (editable, so the bundled frontend/ is served by default).
REM setuptools emits an informational "Editable installation." warning for
REM every editable build; its same-name-folder advice cannot apply here (the
REM import package is simple_server), so that one message is appended to any
REM existing PYTHONWARNINGS (a comma-separated list) and the original value is
REM restored afterwards.
echo === Installing SIMple ===
set "OLD_WARNINGS=%PYTHONWARNINGS%"
if defined PYTHONWARNINGS (
    set "PYTHONWARNINGS=%PYTHONWARNINGS%,ignore:Editable installation"
) else (
    set "PYTHONWARNINGS=ignore:Editable installation"
)
pip install --no-build-isolation -e "%~dp0."
REM restore the original value (an empty original unsets, i.e. clears it)
set "PYTHONWARNINGS=%OLD_WARNINGS%"
set "OLD_WARNINGS="
if %errorlevel% neq 0 (
    echo Error: Failed to install SIMple.
    pause
    exit /b 1
)
echo.

echo === Setup complete ===
echo Run start.bat to start the server.
echo Updating later: update.bat ^(reuses .venv - the dependencies are not re-installed^).
pause
