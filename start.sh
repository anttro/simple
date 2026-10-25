#!/usr/bin/env bash
set -e

# pysim-simple-server start script
# Starts the server, preferring the venv if it exists.
# Auto-detects PC/SC reader if available.
# Every run is mirrored to simple_lastrun.log (overwritten on each start) so the
# latest server output can be read without copy-pasting the console.

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

# PC/SC pre-flight (v3.38.1): a user without access to pcscd must see the real
# cause - the server no longer falls back to /dev/ttyUSB0 silently.  An
# explicit serial reader (-d/--device) keeps the serial path.
SERIAL=0
for arg in "$@"; do
    case "$arg" in
        -d|--device|--device=*|-d*) SERIAL=1 ;;
    esac
done
READER_ARGS=""
if [ "$SERIAL" -eq 0 ]; then
    PYTHON_BIN="$VENV_DIR/bin/python"
    [ -x "$PYTHON_BIN" ] || PYTHON_BIN="$(command -v python3 || true)"
    if [ -n "$PYTHON_BIN" ]; then
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
            echo "  The server will not fall back to a serial reader silently."
            echo "  - pcscd uses PolicyKit (the default allows only active local sessions,"
            echo "    so SSH/headless is denied even with a 0666 socket).  Allow the user in"
            echo "    /etc/polkit-1/rules.d/99-pcscd-allow.rules: return polkit.Result.YES for"
            echo "    org.debian.pcsc-lite.access_pcsc and access_card, subject.user == \"$USER\";"
            echo "    then 'sudo systemctl restart polkit' (or log out/in);"
            echo "  - 'sudo systemctl start pcscd' if the service is not running;"
            echo "  - 'pcsc_scan' shows the same 'Access denied' error until the rule is active."
            exit 2
        }
        READER_ARGS="-p 0"
    fi
fi

SERVER=""
if [ -f "$VENV_DIR/bin/pysim-simple-server" ]; then
    SERVER="$VENV_DIR/bin/pysim-simple-server"
elif command -v pysim-simple-server &> /dev/null; then
    SERVER="pysim-simple-server"
elif [ -f "$SCRIPT_DIR/pysim_simple_server/__main__.py" ]; then
    echo "Starting pysim-simple-server from source on http://$HTTP_HOST:$HTTP_PORT"
    echo "Log: simple_lastrun.log (overwritten on each start)."
    cd "$SCRIPT_DIR" && python3 -m pysim_simple_server --http-port 8080 $READER_ARGS "$@"
    exit $?
else
    echo "Error: pysim-simple-server not installed."
    echo "Run setup.sh first or install manually:"
    echo "  pip install pysim-simple-server"
    exit 1
fi

echo "Starting pysim-simple-server on http://$HTTP_HOST:$HTTP_PORT"
echo "Log: simple_lastrun.log (overwritten on each start)."
echo "Press Ctrl+C to stop."
echo "Extra arguments are passed to the server (e.g. ./start.sh --gsmtap)."
$SERVER --http-port 8080 $READER_ARGS "$@"
