#!/usr/bin/env bash
# Update this packaged SIMple install in place (see tools/update.py --help).
# The .venv is reused - pysim and its prerequisites are not re-installed.
set -e

# Windows guard: update.sh is the native Linux/macOS helper.  Under WSL the
# Linux guest cannot reach the Windows PC/SC readers, and a Git Bash / MSYS2 /
# MinGW / Cygwin shell has the wrong (bin/) venv layout - use update.bat.  A
# deliberate WSL/Cygwin flow can run tools/update.py without this script.
if grep -qi microsoft /proc/version 2>/dev/null || \
   uname -s 2>/dev/null | grep -qiE 'MINGW|MSYS|CYGWIN'; then
    echo "Error: update.sh is the Linux/macOS helper and cannot run on Windows"
    echo "(WSL / MSYS2 / MinGW / Cygwin detected)."
    echo "Use update.bat from a Windows command prompt (cmd.exe / PowerShell) instead."
    exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -x "$SCRIPT_DIR/.venv/bin/python" ]; then
    PY="$SCRIPT_DIR/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    PY=python3
else
    PY=python
fi
exec "$PY" "$SCRIPT_DIR/tools/update.py" "$@"
