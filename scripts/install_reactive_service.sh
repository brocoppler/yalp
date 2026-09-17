#!/usr/bin/env bash
# install_reactive_service.sh — Idempotent installer for the yalp-reactive
# systemd USER unit (scripts/yalp-reactive.service).
#
# Run as the robot user (izzy) on the Pi. No sudo needed for a user unit — the
# one privileged step, `loginctl enable-linger`, is printed, not run.
# Safe to re-run: the unit is re-copied only when it changed, the env file is
# never overwritten once it exists, and daemon-reload / enable are no-ops when
# already done.
#
# Usage:
#   bash scripts/install_reactive_service.sh
#
# Afterwards `yalp session start|stop|status|restart|logs` detects the unit and
# drives it through systemctl --user / journalctl --user.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_NAME="yalp-reactive.service"
UNIT_SRC="${REPO_ROOT}/scripts/${UNIT_NAME}"
UNIT_DIR="${HOME}/.config/systemd/user"
UNIT_DST="${UNIT_DIR}/${UNIT_NAME}"
ENV_DIR="${HOME}/.config/yalp"
ENV_FILE="${ENV_DIR}/reactive.env"
# The unit hardcodes %h/yalp (see its ExecStart / WorkingDirectory).
EXPECTED_ROOT="${HOME}/yalp"
ROBOT_USER="${USER:-$(id -un)}"

if ! command -v systemctl >/dev/null 2>&1; then
    echo "ERROR: systemctl not found — this installer is for a systemd host (the Pi), not a laptop." >&2
    exit 1
fi
if [ ! -f "${UNIT_SRC}" ]; then
    echo "ERROR: ${UNIT_SRC} not found." >&2
    exit 1
fi

echo "==> [1/5] Checking the checkout location the unit expects (${EXPECTED_ROOT})..."
if [ "${REPO_ROOT}" != "${EXPECTED_ROOT}" ]; then
    echo "    WARNING: this checkout is at ${REPO_ROOT}, but the unit's ExecStart and"
    echo "             WorkingDirectory point at %h/yalp (= ${EXPECTED_ROOT})."
    echo "             Either symlink it:  ln -s '${REPO_ROOT}' '${EXPECTED_ROOT}'"
    echo "             or edit ${UNIT_DST} after this install."
else
    echo "    OK"
fi
if [ ! -x "${EXPECTED_ROOT}/.venv/bin/python" ]; then
    echo "    WARNING: ${EXPECTED_ROOT}/.venv/bin/python not found — run scripts/pi_setup.sh first."
fi

echo "==> [2/5] Installing the user unit to ${UNIT_DST}..."
mkdir -p "${UNIT_DIR}"
if [ -f "${UNIT_DST}" ] && cmp -s "${UNIT_SRC}" "${UNIT_DST}"; then
    echo "    already up to date — skipping"
else
    install -m 0644 "${UNIT_SRC}" "${UNIT_DST}"
    echo "    installed"
fi

echo "==> [3/5] Seeding ${ENV_FILE} (never overwritten once present)..."
mkdir -p "${ENV_DIR}"
if [ -f "${ENV_FILE}" ]; then
    echo "    exists — leaving your settings alone"
else
    cat > "${ENV_FILE}" <<'EOF'
# ~/.config/yalp/reactive.env — environment for the yalp-reactive systemd user
# unit (EnvironmentFile=). One KEY=VALUE per line, no `export`, no shell
# expansion; lines starting with # are ignored. Every knob here has a default in
# src/yalp/config.py — uncomment a line to override it, then
# `yalp session restart` to apply.

# --- Ultrasonic (collision-stop) driver -------------------------------------
# gpiod = libgpiod v2, kernel-timestamped echo — the SAFE choice on the Pi 5.
# (gpiozero's software timing inflates range 2x/4x there; 'auto' prefers gpiod
# and falls back to gpiozero with a warning — force gpiod so a fallback is an
# error, not a silent unsafe run.)
YALP_ULTRASONIC_BACKEND=gpiod
#YALP_GPIOCHIP=

# --- Visual heading hold (camera-only yaw estimate during straight drives) ---
#YALP_HEADING_HOLD=1
#YALP_HEADING_HOLD_GAIN=0.01
#YALP_HEADING_HOLD_KD=0.004
#YALP_HEADING_HOLD_MAX=0.20
#YALP_HEADING_HOLD_MIN_RESPONSE=0.10
#YALP_HEADING_HOLD_BLIND_TICKS=10
#YALP_CAMERA_HFOV_DEG=55.0

# --- Closed-loop turns + trim learning ---------------------------------------
#YALP_ROTATE_CLOSED_LOOP=1
#YALP_ROTATE_TIMEOUT_FACTOR=3.0
#YALP_ROTATE_STOP_LEAD_S=0.06
#YALP_TRIM_LEARNING=1
#YALP_TRIM_LEARNING_RATE=0.3

# --- Motors ------------------------------------------------------------------
#YALP_MOTOR_DECAY=slow
#YALP_DRIVE_DUTY_DEADBAND=0.27
#YALP_MOTOR_LEFT_INVERT=0
#YALP_MOTOR_RIGHT_INVERT=0

# --- FOLLOW detector ---------------------------------------------------------
#YALP_FOLLOW_DETECTOR=person

# --- Telemetry (flight recorder under $XDG_STATE_HOME/yalp/telemetry) -------
#YALP_TELEMETRY=1
#YALP_TELEMETRY_SAMPLE_HZ=2.0
EOF
    echo "    created with commented defaults"
fi

echo "==> [4/5] Reloading the user manager and enabling ${UNIT_NAME}..."
systemctl --user daemon-reload
systemctl --user enable "${UNIT_NAME}"
echo "    enabled (starts with the user manager; see the linger note below)"

echo "==> [5/5] Checking linger (needed for the unit to run at boot without a login)..."
LINGER="$(loginctl show-user "${ROBOT_USER}" -p Linger --value 2>/dev/null || echo unknown)"
if [ "${LINGER}" = "yes" ]; then
    echo "    linger is enabled for ${ROBOT_USER} — the unit will start at boot"
else
    echo "    linger is '${LINGER}' for ${ROBOT_USER}: the unit starts at the first login"
    echo "    and is STOPPED at the last logout. To run at boot with nobody logged in, run ONCE:"
    echo "        sudo loginctl enable-linger ${ROBOT_USER}"
fi

echo ""
echo "Reactive service install complete"
echo "  start   : yalp session start          (or: systemctl --user start ${UNIT_NAME})"
echo "  stop    : yalp session stop           (or: systemctl --user stop ${UNIT_NAME}; SIGINT — motors zeroed first)"
echo "  status  : yalp session status         (or: systemctl --user status ${UNIT_NAME})"
echo "  logs    : yalp session logs -f        (or: journalctl --user -u yalp-reactive -f)"
echo "  knobs   : ${ENV_FILE}   then: yalp session restart"
