#!/usr/bin/env bash
# Update this packaged SIMple install in place (see tools/update.py --help).
# The .venv is reused - pysim and its prerequisites are not re-installed.
set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -x "$SCRIPT_DIR/.venv/bin/python" ]; then
    PY="$SCRIPT_DIR/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    PY=python3
else
    PY=python
fi
exec "$PY" "$SCRIPT_DIR/tools/update.py" "$@"
