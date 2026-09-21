#!/usr/bin/env bash
set -e

# pysim-simple-server start script
# Starts the server, preferring the venv if it exists.
# Auto-detects PC/SC reader if available.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"

# Auto-detect reader
READER_ARGS=""
if command -v pcscd > /dev/null 2>&1; then
    # Give pcscd a moment if it's not running yet (USB enumeration delay)
    if ! pgrep -x pcscd > /dev/null 2>&1 && ! pgrep -x pcscd.bin > /dev/null 2>&1; then
        sleep 1
    fi
    if pgrep -x pcscd > /dev/null 2>&1 || pgrep -x pcscd.bin > /dev/null 2>&1; then
        READER_ARGS="-p 0"
    fi
fi

SERVER=""
if [ -f "$VENV_DIR/bin/pysim-simple-server" ]; then
    SERVER="$VENV_DIR/bin/pysim-simple-server"
elif command -v pysim-simple-server &> /dev/null; then
    SERVER="pysim-simple-server"
elif [ -f "$SCRIPT_DIR/pysim_simple_server/__main__.py" ]; then
    echo "Starting pysim-simple-server from source on http://127.0.0.1:8080"
    cd "$SCRIPT_DIR" && python3 -m pysim_simple_server --http-port 8080 $READER_ARGS "$@"
    exit $?
else
    echo "Error: pysim-simple-server not installed."
    echo "Run setup.sh first or install manually:"
    echo "  pip install pysim-simple-server"
    exit 1
fi

echo "Starting pysim-simple-server on http://127.0.0.1:8080"
echo "Press Ctrl+C to stop."
echo "Extra arguments are passed to the server (e.g. ./start.sh --gsmtap)."
$SERVER --http-port 8080 $READER_ARGS "$@"
