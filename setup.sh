#!/usr/bin/env bash
set -e

# pysim-simple-server setup script
# Creates a venv and installs pysim and its dependencies.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"

# Check prerequisites
command -v python3 >/dev/null 2>&1 || { echo "Error: Python 3 is required but not found. Install Python 3.9+ from https://python.org"; exit 1; }
command -v git >/dev/null 2>&1 || { echo "Error: Git is required but not found. Install Git from https://git-scm.com"; exit 1; }

PY_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
echo "Found Python $PY_VER"

# Create the venv.  A failed creation (missing venv/ensurepip) leaves a partial
# .venv behind: validate it and remove it so the next run starts over (v3.38.1).
if [ -d "$VENV_DIR" ] && { [ ! -f "$VENV_DIR/bin/activate" ] || [ ! -x "$VENV_DIR/bin/python" ] || [ ! -x "$VENV_DIR/bin/pip" ]; }; then
    echo "Removing an incomplete virtual environment (a previous setup did not finish)..."
    rm -rf "$VENV_DIR"
fi
if [ ! -d "$VENV_DIR" ]; then
    echo "Creating virtual environment..."
    if ! python3 -m venv "$VENV_DIR"; then
        rm -rf "$VENV_DIR"
        echo "Error: could not create the virtual environment."
        echo "  The Python venv/ensurepip component is missing or broken."
        echo "    Debian/Ubuntu: sudo apt install python3-venv"
        echo "    Fedora:        sudo dnf install python3-libs"
        echo "    Arch:          venv and ensurepip are part of python"
        echo "  Install it and run ./setup.sh again."
        exit 1
    fi
fi
source "$VENV_DIR/bin/activate"
PIP="$VENV_DIR/bin/pip"

# Upgrade pip
$PIP install --upgrade pip -q

# Install pysim from the Osmocom repository
echo ""
echo "=== Installing pysim ==="
$PIP install git+https://github.com/osmocom/pysim.git

# Install pysim-simple-server
echo ""
echo "=== Installing pysim-simple-server ==="
$PIP install -e "$SCRIPT_DIR"

# Check for PC/SC
echo ""
if command -v pcscd >/dev/null 2>&1; then
    echo "=== PC/SC daemon found ==="
    if ! pgrep -x pcscd >/dev/null; then
        echo "Starting pcscd..."
        sudo pcscd || echo "Warning: could not start pcscd. Start it manually: sudo systemctl start pcscd"
    fi
else
    echo "Note: PC/SC daemon not found. If you use a USB smart card reader,"
    echo "install pcsc-lite and ccid (plus the PC/SC headers that the"
    echo "pyscard build needs on Linux):"
    echo "  Debian/Ubuntu: sudo apt install pcscd pcsc-tools libpcsclite-dev"
    echo "  Arch Linux:    sudo pacman -S pcsc-lite ccid   (headers included)"
    echo "  Fedora:        sudo dnf install pcsc-lite pcsc-lite-ccid pcsc-lite-devel"
fi

# The SCP81 TLS-PSK listener uses ssl's PSK callbacks, added in Python 3.13.
PY_MINOR=$(echo "$PY_VER" | cut -d. -f2)
if [ "${PY_VER%.*}" = "3" ] && [ "$PY_MINOR" -lt 13 ]; then
    echo "Note: the SCP81 TLS-PSK listener needs Python 3.13+ (this is $PY_VER);"
    echo "      the rest of the app works normally."
    echo ""
fi

echo ""
echo "=== Setup complete ==="
echo "Run ./start.sh to start the server."
echo "Updating later: ./update.sh (reuses .venv - pysim is not re-installed)."