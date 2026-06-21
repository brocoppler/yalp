"""Central configuration for yalp.

This module is the single source of truth for the canonical constants pinned by
the specs (model routing tiers, safety/timing thresholds, IPC endpoint) and for
loading the Anthropic API key from the environment.

Design rules (see docs/technical/software-spec.md §6 "Secrets & Config"):

  * Secrets live in the ENVIRONMENT, never in source. We read ``ANTHROPIC_API_KEY``
    from the environment (optionally hydrated from a git-ignored ``.env``); the
    key is never hardcoded, logged, or committed.
  * Config (model IDs, thresholds, the IPC endpoint) lives here in source.

This module deliberately imports only the standard library (plus an *optional*,
softly-guarded ``python-dotenv``) so it stays importable with no heavy
dependencies (anthropic / opencv / numpy) installed — see tests/test_smoke.py.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# --- .env hydration (optional) ----------------------------------------------
# If python-dotenv is installed and a .env file is present, load it so local dev
# "just works". Absence of the package (or the file) is fine — we fall back to
# the real process environment.
try:  # pragma: no cover - trivial import guard
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - dotenv is optional
    pass


# --- Canonical constants from the specs -------------------------------------
# Model routing tiers (software-spec.md §3). Env-overridable so the operator can
# pin different model IDs without editing source.
MODEL_FAST: str = os.environ.get("YALP_MODEL_FAST", "claude-haiku-4-5")
MODEL_MID: str = os.environ.get("YALP_MODEL_MID", "claude-sonnet-4-6")
MODEL_BIG: str = os.environ.get("YALP_MODEL_BIG", "claude-opus-4-8")

# Graceful-degrade light floor for follow / come-here (lux). Below this, vision
# tracking is unreliable and the system should degrade rather than drive blind.
GOOD_LIGHT_LUX: int = 200

# Person-detector throughput GO/NO-GO threshold (Hz). >= this sustained means
# track-by-detection is viable (Gate H / Gate L in roadmap.md). Below it, fall
# back to a blob/color tracker.
GATE_H_GO_HZ: int = 3

# Reactive-tick deadline (ms). The tick p99 must stay under this so the safety
# loop holds >= 30 Hz under full load (combined-load gate, software-spec.md §4).
TICK_BUDGET_MS: int = 33

# --- FOLLOW-mode tuning (software-spec.md §4) -------------------------------
# Track-by-detection cadence & geometry. The detector downscales the frame to
# FOLLOW_DETECT_WIDTH before inference (the main throughput lever on a no-NPU
# machine, §2.5), and re-seeds/validates the cheap tracker at least every
# FOLLOW_DETECT_INTERVAL_TICKS ticks.
FOLLOW_DETECT_WIDTH: int = 384
FOLLOW_DETECT_INTERVAL_TICKS: int = 5

# A held/tracked box below this confidence (0..1) is treated as not-visible.
FOLLOW_TRACK_MIN_SCORE: float = 0.20

# Steering: proportional turn toward the bbox center with a small deadband, and
# drive forward until the bbox height (distance proxy) reaches the stop fraction.
FOLLOW_TURN_DEADBAND: float = 0.12   # normalized -1..1 center error
FOLLOW_TURN_GAIN: float = 1.5
FOLLOW_FORWARD_GAIN: float = 2.0
FOLLOW_STOP_BBOX_HEIGHT: float = 0.60  # bbox height / frame height = "close enough"

# Graceful degradation: coast at most this many ticks without a detector
# re-confirmation before STOPPING and reporting "I lost you" (never drive blind
# on a stale box, §2.2/§4); and treat a frame dimmer than this mean brightness
# (0..255) as too dark to track (the lux-floor proxy for GOOD_LIGHT_LUX, §5).
FOLLOW_COAST_TICKS: int = 8
FOLLOW_DARK_BRIGHTNESS: float = 16.0

# Localhost IPC endpoint for the reactive <-> deliberative socket (JSON lines).
# TCP 127.0.0.1 in dev; a Unix-domain socket once co-located on the Pi.
IPC_HOST: str = "127.0.0.1"
IPC_PORT: int = 8765

# Reactive safety / timing (software-spec.md §2.3, §2.6; hardware.md §4).
# Distance (m) below which collision-stop fires this tick. A missed/timed-out
# HC-SR04 echo is treated as "unknown" and also stops (bias to STOP — never
# decay a missed echo into "clear").
SAFE_STOP_THRESHOLD_M: float = 0.30

# Default reactive tick rate (Hz). The spec target band is 10–30 Hz.
REACTIVE_TICK_HZ: float = 20.0

# Independent watchdog timeout (ms): the motor GPIO is zeroed if the reactive
# tick's heartbeat is older than this (software-spec.md §2.6).
WATCHDOG_TIMEOUT_MS: int = 100

# Hard per-session deliberative budget (software-spec.md §3). A WiFi retry-storm
# or a runaway model escalation must not silently run up cost: the agent tracks
# cumulative model calls and tokens for a session and, once either cap is hit,
# **stops issuing model calls** and falls back to IDLE/local behavior (exactly
# the §5 outage path) rather than retrying forever. This is a ceiling the
# operator sets here, separate from any per-call ``max_tokens``. Env-overridable.
BUDGET_MAX_CALLS: int = int(os.environ.get("YALP_BUDGET_MAX_CALLS", "40"))
BUDGET_MAX_TOKENS: int = int(os.environ.get("YALP_BUDGET_MAX_TOKENS", "200000"))


@dataclass(frozen=True)
class Config:
    """A typed snapshot of yalp's configuration.

    Defaults mirror the module-level constants above. Construct ``Config()`` for
    the canonical configuration, or pass overrides for tests / alternate setups.
    """

    model_fast: str = MODEL_FAST
    model_mid: str = MODEL_MID
    model_big: str = MODEL_BIG
    good_light_lux: int = GOOD_LIGHT_LUX
    gate_h_go_hz: int = GATE_H_GO_HZ
    tick_budget_ms: int = TICK_BUDGET_MS
    follow_detect_width: int = FOLLOW_DETECT_WIDTH
    follow_detect_interval_ticks: int = FOLLOW_DETECT_INTERVAL_TICKS
    follow_track_min_score: float = FOLLOW_TRACK_MIN_SCORE
    follow_turn_deadband: float = FOLLOW_TURN_DEADBAND
    follow_turn_gain: float = FOLLOW_TURN_GAIN
    follow_forward_gain: float = FOLLOW_FORWARD_GAIN
    follow_stop_bbox_height: float = FOLLOW_STOP_BBOX_HEIGHT
    follow_coast_ticks: int = FOLLOW_COAST_TICKS
    follow_dark_brightness: float = FOLLOW_DARK_BRIGHTNESS
    ipc_host: str = IPC_HOST
    ipc_port: int = IPC_PORT
    safe_stop_threshold_m: float = SAFE_STOP_THRESHOLD_M
    reactive_tick_hz: float = REACTIVE_TICK_HZ
    watchdog_timeout_ms: int = WATCHDOG_TIMEOUT_MS
    budget_max_calls: int = BUDGET_MAX_CALLS
    budget_max_tokens: int = BUDGET_MAX_TOKENS


def get_api_key() -> str | None:
    """Return the Anthropic API key from the environment, or None if unset/empty."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    return key or None


def require_api_key() -> str:
    """Return the Anthropic API key, raising a friendly error if it is missing.

    Use this on the deliberative path right before constructing the LLM client,
    so a missing key fails loudly with a pointer to setup instructions rather
    than deep inside the SDK.
    """
    key = get_api_key()
    if not key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. Copy .env.example to .env and add your "
            "key, or export ANTHROPIC_API_KEY in your shell. See SETUP.md."
        )
    return key


__all__ = [
    "MODEL_FAST",
    "MODEL_MID",
    "MODEL_BIG",
    "GOOD_LIGHT_LUX",
    "GATE_H_GO_HZ",
    "TICK_BUDGET_MS",
    "FOLLOW_DETECT_WIDTH",
    "FOLLOW_DETECT_INTERVAL_TICKS",
    "FOLLOW_TRACK_MIN_SCORE",
    "FOLLOW_TURN_DEADBAND",
    "FOLLOW_TURN_GAIN",
    "FOLLOW_FORWARD_GAIN",
    "FOLLOW_STOP_BBOX_HEIGHT",
    "FOLLOW_COAST_TICKS",
    "FOLLOW_DARK_BRIGHTNESS",
    "IPC_HOST",
    "IPC_PORT",
    "SAFE_STOP_THRESHOLD_M",
    "REACTIVE_TICK_HZ",
    "WATCHDOG_TIMEOUT_MS",
    "BUDGET_MAX_CALLS",
    "BUDGET_MAX_TOKENS",
    "Config",
    "get_api_key",
    "require_api_key",
]
