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

# Create venv
if [ ! -d "$VENV_DIR" ]; then
    echo "Creating virtual environment..."
    python3 -m venv "$VENV_DIR"
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

echo ""
echo "=== Setup complete ==="
echo "Run ./start.sh to start the server."