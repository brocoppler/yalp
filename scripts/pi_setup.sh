#!/usr/bin/env bash
# pi_setup.sh — Idempotent Raspberry Pi provisioning script for yalp.
#
# Run as the normal pi user (sudo access required for apt steps).
# Safe to re-run: each step checks whether it's already done.
#
# Usage:
#   bash scripts/pi_setup.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MIN_PYTHON_MINOR=11

echo "==> [1/6] Updating package lists and upgrading installed packages..."
sudo apt update
sudo apt full-upgrade -y

echo "==> [2/6] Installing system packages..."
sudo apt install -y \
    python3 \
    python3-venv \
    python3-pip \
    git \
    python3-lgpio

echo "==> [3/6] Checking Python version (>= 3.${MIN_PYTHON_MINOR} required)..."
PYTHON_BIN="$(command -v python3)"
PYTHON_MINOR="$("$PYTHON_BIN" -c 'import sys; print(sys.version_info.minor)')"
PYTHON_MAJOR="$("$PYTHON_BIN" -c 'import sys; print(sys.version_info.major)')"
if [ "$PYTHON_MAJOR" -lt 3 ] || { [ "$PYTHON_MAJOR" -eq 3 ] && [ "$PYTHON_MINOR" -lt "$MIN_PYTHON_MINOR" ]; }; then
    echo "ERROR: Python 3.${MIN_PYTHON_MINOR}+ is required, but found $("$PYTHON_BIN" --version)." >&2
    exit 1
fi
echo "    Python $("$PYTHON_BIN" --version) — OK"

echo "==> [4/6] Creating virtual environment (if absent)..."
VENV_DIR="${REPO_ROOT}/.venv"
if [ ! -d "${VENV_DIR}" ]; then
    "$PYTHON_BIN" -m venv "${VENV_DIR}"
    echo "    Created ${VENV_DIR}"
else
    echo "    ${VENV_DIR} already exists — skipping creation"
fi

echo "==> [5/6] Activating virtual environment..."
# shellcheck source=/dev/null
source "${VENV_DIR}/bin/activate"
echo "    Active Python: $(python --version)"

echo "==> [6/6] Installing yalp with [pi] and [dev] extras..."
pip install --upgrade pip --quiet
pip install -e "${REPO_ROOT}[pi]"
pip install -e "${REPO_ROOT}[dev]"

echo ""
echo "Pi setup complete"
