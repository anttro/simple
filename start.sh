#!/usr/bin/env bash
set -e

# SIMple start script: launches `simple-server`, preferring the venv if it
# exists.  Every run is mirrored to simple_lastrun.log (overwritten on each
# start) so the latest server output can be read without copy-pasting the
# console.

# Windows guard: start.sh is the native Linux/macOS helper.  Under WSL the
# Linux guest cannot reach the Windows PC/SC readers, and a Git Bash / MSYS2 /
# MinGW / Cygwin shell has the wrong (bin/) venv layout - use start.bat, which
# drives the Windows Python and the built-in PC/SC stack.  A deliberate
# WSL/Cygwin flow can run the server without this script.
if grep -qi microsoft /proc/version 2>/dev/null || \
   uname -s 2>/dev/null | grep -qiE 'MINGW|MSYS|CYGWIN'; then
    echo "Error: start.sh is the Linux/macOS helper and cannot run on Windows"
    echo "(WSL / MSYS2 / MinGW / Cygwin detected)."
    echo "Use start.bat from a Windows command prompt (cmd.exe / PowerShell) instead."
    exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"

# The address the opening line names (v3.38.2).  The server parses the same
# flags; start.sh passes its own --http-port 8080 before "$@", so a later user
# value wins (argparse keeps the last one) - mirror that here.
HTTP_HOST=127.0.0.1
HTTP_PORT=8080
PREV_ARG=""
for arg in "$@"; do
    case "$PREV_ARG" in
        --http-host) HTTP_HOST="$arg" ;;
        --http-port) HTTP_PORT="$arg" ;;
    esac
    case "$arg" in
        --http-host=*) HTTP_HOST="${arg#*=}" ;;
        --http-port=*) HTTP_PORT="${arg#*=}" ;;
    esac
    PREV_ARG="$arg"
done

# Console + log file.  The log contains card keys and request payloads (test
# cards) - it is gitignored and must not be committed.
LOG_FILE="$SCRIPT_DIR/simple_lastrun.log"
exec > >(tee "$LOG_FILE") 2>&1
export PYTHONUNBUFFERED=1   # keep the log live: a piped stdout is block-buffered

PYTHON_BIN="$VENV_DIR/bin/python"
[ -x "$PYTHON_BIN" ] || PYTHON_BIN="$(command -v python3 || true)"
if [ -z "$PYTHON_BIN" ]; then
    echo "Error: Python 3 was not found (and there is no .venv)."
    echo "Run setup.sh first."
    exit 1
fi

# PC/SC access pre-flight (v3.38.1): a user without access to pcscd must see
# the real cause - the server is PC/SC-only and does not fall back to a serial
# reader silently.  The probe and the guidance are the same as the entry
# point's (a reader named with -r/--reader is checked at startup).
PCSC_ERR="$("$PYTHON_BIN" - <<'PY' 2>&1
import time
from smartcard.System import readers
last = None
for _ in range(3):
    try:
        readers()
        raise SystemExit(0)
    except SystemExit:
        raise
    except Exception as e:
        last = e
        hr = getattr(e, 'hresult', None)
        if hr == 0x80100027 or 'access denied' in str(e).lower():
            break          # permissions do not fix themselves
        time.sleep(1)      # pcscd may still be starting (socket activation)
hr = getattr(last, 'hresult', -1)
msg = str(last) or 'PC/SC error'
print(msg + ((' (hresult=0x%08X)' % hr) if hr not in (None, -1) else ''))
raise SystemExit(1)
PY
)" || {
    echo "Error: PC/SC is not accessible: ${PCSC_ERR:-unknown error}"
    echo "  The server does not fall back to a serial reader silently."
    echo "  - pcscd uses PolicyKit (the default allows only active local sessions,"
    echo "    so SSH/headless is denied even with a 0666 socket).  Allow the user in"
    echo "    /etc/polkit-1/rules.d/99-pcscd-allow.rules: return polkit.Result.YES for"
    echo "    org.debian.pcsc-lite.access_pcsc and access_card, subject.user == \"$USER\";"
    echo "    then 'sudo systemctl restart polkit' (or log out/in);"
    echo "  - 'sudo systemctl start pcscd' if the service is not running;"
    echo "  - 'pcsc_scan' shows the same 'Access denied' error until the rule is active."
    exit 2
}

SERVER=""
if [ -x "$VENV_DIR/bin/simple-server" ]; then
    SERVER="$VENV_DIR/bin/simple-server"
elif command -v simple-server &> /dev/null; then
    SERVER="simple-server"
elif [ -f "$SCRIPT_DIR/simple_server/__main__.py" ]; then
    echo "Starting simple-server from source on http://$HTTP_HOST:$HTTP_PORT"
    echo "Log: simple_lastrun.log (overwritten on each start)."
    cd "$SCRIPT_DIR" && "$PYTHON_BIN" -m simple_server --http-port 8080 "$@"
    exit $?
else
    echo "Error: simple-server is not installed."
    echo "Run setup.sh first or install manually:"
    echo "  pip install -e ."
    exit 1
fi

echo "Starting simple-server on http://$HTTP_HOST:$HTTP_PORT"
echo "Log: simple_lastrun.log (overwritten on each start)."
echo "Press Ctrl+C to stop."
echo "Extra arguments are passed to the server (e.g. ./start.sh --gsmtap)."
$SERVER --http-port 8080 "$@"
