@echo off
REM Update this packaged SIMple install in place (see tools\update.py --help).
REM The .venv is reused - pysim and its prerequisites are not re-installed.
if exist "%~dp0.venv\Scripts\python.exe" (
    "%~dp0.venv\Scripts\python.exe" "%~dp0tools\update.py" %*
) else (
    python "%~dp0tools\update.py" %*
)
pause
