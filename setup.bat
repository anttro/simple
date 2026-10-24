@echo off
REM pysim-simple-server setup script for Windows
REM Creates a venv and installs pysim and its dependencies.

setlocal enabledelayedexpansion

set VENV_DIR=%~dp0.venv

echo === pysim-simple-server setup ===
echo.

REM Check Python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo Error: Python is required but not found.
    echo Install Python 3.9+ from https://python.org
    pause
    exit /b 1
)

echo Found Python
python --version

REM Check Git
git --version >nul 2>&1
if %errorlevel% neq 0 (
    echo Error: Git is required but not found.
    echo Install Git from https://git-scm.com
    pause
    exit /b 1
)

echo Found Git
git --version
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
        echo Removing an incomplete virtual environment (a previous setup did not finish)...
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

REM Upgrade pip
python -m pip install --upgrade pip -q

REM Python 3.13 is strongly recommended: pyscard ships precompiled wheels up
REM to 3.13, the SCP81 TLS-PSK listener needs 3.13+, and on 3.14 pyscard and
REM some pysim dependency wheels are unavailable (source builds need MSVC).
echo Note: Python 3.13 is strongly recommended.
echo       pyscard has precompiled wheels up to 3.13 and the SCP81 TLS-PSK
echo       listener needs 3.13+.  On Python 3.14 pyscard and some pysim
echo       dependency wheels are unavailable; those build from source and
echo       require Microsoft C++ Build Tools ("Desktop development with C++").
echo.

REM Install pysim dependencies first, then pysim itself (without the SMPP
REM bridge - not needed by pysim-shell). Installing pysim with --no-deps after
REM its dependencies avoids pip's resolver warning about the intentionally
REM omitted smpp.twisted3 (SMPP bridge) dependency.
echo === Installing pysim dependencies ===
pip install -r "%~dp0requirements-pysim.txt"
if %errorlevel% neq 0 (
    echo Error: Failed to install pysim dependencies.
    pause
    exit /b 1
)
echo === Installing pysim ===
pip install --no-deps git+https://github.com/osmocom/pysim.git
if %errorlevel% neq 0 (
    echo Error: Failed to install pysim.
    pause
    exit /b 1
)
echo.

REM Install pysim-simple-server
echo === Installing pysim-simple-server ===
pip install -e "%~dp0."
if %errorlevel% neq 0 (
    echo Error: Failed to install pysim-simple-server.
    pause
    exit /b 1
)
echo.

echo === Setup complete ===
echo Run start.bat to start the server.
echo Updating later: update.bat ^(reuses .venv - pysim is not re-installed^).
pause