#!/usr/bin/env bash
# pi_setup.sh — Idempotent Raspberry Pi provisioning script for yalp.
#
# Run as the normal pi user (sudo access required for apt steps).
# Safe to re-run: each step checks whether it's already done.
#
# Usage:
#   bash scripts/pi_setup.sh
#
# Note on GPIO libraries: on Raspberry Pi OS the lgpio C-extension is best
# installed from apt (python3-lgpio) rather than built from PyPI (a source
# build needs swig + headers). We therefore create the venv with
# --system-site-packages so the venv can see the apt-installed lgpio, and pip
# treats the "lgpio" dependency as already satisfied instead of compiling it.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MIN_PYTHON_MINOR=11

echo "==> [1/8] Updating package lists and upgrading installed packages..."
sudo apt update
sudo apt full-upgrade -y

echo "==> [2/8] Installing system packages..."
# python3-lgpio: the GPIO backend (used via --system-site-packages, NOT pip).
# swig + python3-dev: fallback so a source build of lgpio could still succeed
# if the apt package is ever unavailable.
# libgl1 + libglib2.0-0: OpenCV (opencv-python) runtime libs. Raspberry Pi OS
# Lite is headless and omits libGL, so `import cv2` fails with
# "libGL.so.1: cannot open shared object file" without these.
sudo apt install -y \
    python3 \
    python3-venv \
    python3-pip \
    python3-dev \
    git \
    swig \
    python3-lgpio \
    libgl1 \
    libglib2.0-0

echo "==> [3/8] Checking Python version (>= 3.${MIN_PYTHON_MINOR} required)..."
PYTHON_BIN="$(command -v python3)"
PYTHON_MINOR="$("$PYTHON_BIN" -c 'import sys; print(sys.version_info.minor)')"
PYTHON_MAJOR="$("$PYTHON_BIN" -c 'import sys; print(sys.version_info.major)')"
if [ "$PYTHON_MAJOR" -lt 3 ] || { [ "$PYTHON_MAJOR" -eq 3 ] && [ "$PYTHON_MINOR" -lt "$MIN_PYTHON_MINOR" ]; }; then
    echo "ERROR: Python 3.${MIN_PYTHON_MINOR}+ is required, but found $("$PYTHON_BIN" --version)." >&2
    exit 1
fi
echo "    Python $("$PYTHON_BIN" --version) — OK"

echo "==> [4/8] Creating virtual environment with system site-packages (if absent)..."
VENV_DIR="${REPO_ROOT}/.venv"
PYVENV_CFG="${VENV_DIR}/pyvenv.cfg"
if [ ! -d "${VENV_DIR}" ]; then
    "$PYTHON_BIN" -m venv --system-site-packages "${VENV_DIR}"
    echo "    Created ${VENV_DIR} (with --system-site-packages)"
else
    echo "    ${VENV_DIR} already exists — skipping creation"
fi

echo "==> [5/8] Ensuring the venv can see system site-packages (for apt lgpio)..."
# Repair an older/sealed venv in place rather than forcing a recreate.
if [ -f "${PYVENV_CFG}" ]; then
    if grep -q '^include-system-site-packages = false' "${PYVENV_CFG}"; then
        sed -i 's/^include-system-site-packages = false/include-system-site-packages = true/' "${PYVENV_CFG}"
        echo "    Flipped include-system-site-packages → true"
    elif grep -q '^include-system-site-packages = true' "${PYVENV_CFG}"; then
        echo "    include-system-site-packages already true — OK"
    else
        echo "include-system-site-packages = true" >> "${PYVENV_CFG}"
        echo "    Added include-system-site-packages = true"
    fi
fi

echo "==> [6/8] Activating venv and installing yalp with [pi] and [dev] extras..."
# shellcheck source=/dev/null
source "${VENV_DIR}/bin/activate"
echo "    Active Python: $(python --version)"
pip install --upgrade pip --quiet
pip install -e "${REPO_ROOT}[pi]"
pip install -e "${REPO_ROOT}[dev]"

echo "==> [7/8] Swapping OpenCV for the HEADLESS build (Qt-abort fix)..."
# pyproject pins full `opencv-python`, whose bundled Qt ships ONLY the `xcb`
# plugin. On a headless machine (the robot) `cv2.namedWindow()` hits
# `qFatal -> abort()` — a NATIVE C++ abort (SIGABRT) that Python's
# `except Exception` cannot catch, so it kills the whole process (it aborted the
# test suite and would hard-crash `yalp follow --preview`). `opencv-python-headless`
# has no GUI/Qt, so window calls raise a CATCHABLE exception instead and the
# headless fallback works. (`QT_QPA_PLATFORM=offscreen` does NOT help — the
# bundled Qt has no offscreen plugin.) The editable install above (re)pulls the
# full wheel as a core dependency, so we swap it here every run. See
# docs/technical/pi-validation-2026-07.md §9 issue #1.
# Idempotent: skip cleanly when headless is already the only OpenCV installed.
if pip show opencv-python-headless >/dev/null 2>&1 && ! pip show opencv-python >/dev/null 2>&1; then
    echo "    opencv-python-headless already installed (full build absent) — skipping"
else
    pip uninstall -y opencv-python >/dev/null 2>&1 || true
    pip install opencv-python-headless
    echo "    Installed opencv-python-headless (removed full opencv-python if present)"
fi

echo "==> [8/8] Verifying the GPIO stack imports..."
python - <<'PYEOF'
import sys
try:
    import lgpio  # from apt python3-lgpio, visible via --system-site-packages
    import gpiozero
except Exception as exc:  # pragma: no cover - environment guard
    print(f"ERROR: GPIO stack not importable: {exc}", file=sys.stderr)
    print("Confirm 'python3-lgpio' is installed (apt) and the venv has "
          "include-system-site-packages = true.", file=sys.stderr)
    sys.exit(1)
print(f"    lgpio + gpiozero import OK (lgpio at {lgpio.__file__})")
PYEOF

echo ""
echo "Pi setup complete"
