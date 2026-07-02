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

import logging
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# --- .env hydration (optional) ----------------------------------------------
# If python-dotenv is installed and a .env file is present, load it so local dev
# "just works". Absence of the package (or the file) is fine — we fall back to
# the real process environment.
try:  # pragma: no cover - trivial import guard
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - dotenv is optional
    pass


# --- Robust env-override accessors ------------------------------------------
# CRITICAL: this module is imported at startup by EVERY yalp process, including
# the reactive safety loop. A malformed override (e.g. YALP_TICK_BUDGET_MS=fast)
# must NOT raise during ``import yalp.config`` and kill the process — a bad env
# var has to DEGRADE to the built-in default, not prevent the safety loop from
# starting. These helpers make every YALP_* parse fail-soft: absent -> default
# silently (unchanged behavior); present-but-malformed -> WARNING naming the
# variable, the bad value, and the default, then return the default.

# Recognized truthy/falsy spellings for boolean-ish overrides. Matches the
# historical truthy set exactly ("1"/"true"/"yes"); the falsy set is the
# symmetric complement so a clearly-intended "0"/"false"/"no" is silent while
# genuine garbage warns.
_ENV_TRUE_STRINGS: tuple[str, ...] = ("1", "true", "yes")
_ENV_FALSE_STRINGS: tuple[str, ...] = ("0", "false", "no")


def _env_str(name: str, default: str) -> str:
    """Return env var ``name`` (verbatim) or ``default`` if unset.

    Strings cannot be "malformed", so this never warns; it exists so every
    YALP_* override flows through one uniform accessor.
    """
    val = os.environ.get(name)
    return default if val is None else val


def _env_int(name: str, default: int) -> int:
    """Return env var ``name`` parsed as ``int``, or ``default``.

    Absent -> ``default`` silently. Present-but-unparseable -> WARNING + default.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        logger.warning(
            "Invalid value for %s=%r (expected an integer); using default %r.",
            name, raw, default,
        )
        return default


def _env_float(name: str, default: float) -> float:
    """Return env var ``name`` parsed as ``float``, or ``default``.

    Absent -> ``default`` silently. Present-but-unparseable -> WARNING + default.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        logger.warning(
            "Invalid value for %s=%r (expected a float); using default %r.",
            name, raw, default,
        )
        return default


def _env_bool(name: str, default: bool) -> bool:
    """Return env var ``name`` parsed as ``bool``, or ``default``.

    Truthy: 1/true/yes; falsy: 0/false/no (case-insensitive, surrounding
    whitespace ignored). Absent or empty -> ``default`` silently. Any other
    non-empty value -> WARNING + default.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    val = raw.strip().lower()
    if val == "":
        return default
    if val in _ENV_TRUE_STRINGS:
        return True
    if val in _ENV_FALSE_STRINGS:
        return False
    logger.warning(
        "Invalid value for %s=%r (expected a boolean: 1/true/yes or "
        "0/false/no); using default %r.",
        name, raw, default,
    )
    return default


# --- Canonical constants from the specs -------------------------------------
# Model routing tiers (software-spec.md §3). Env-overridable so the operator can
# pin different model IDs without editing source.
MODEL_FAST: str = _env_str("YALP_MODEL_FAST", "claude-haiku-4-5")
MODEL_MID: str = _env_str("YALP_MODEL_MID", "claude-sonnet-4-6")
MODEL_BIG: str = _env_str("YALP_MODEL_BIG", "claude-opus-4-8")

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

# Which detector `yalp follow` uses by default. The laptop/desk default is "face"
# (OpenCV's bundled Haar cascade) because at desk range a webcam frames only the
# user's HEAD + UPPER TORSO, which the full-body detectors cannot see. "hog" is
# OpenCV's built-in standing-body detector; "person" is the ORIENTATION-AGNOSTIC
# MobileNet-SSD body detector (front/back/side at room range — what the ROBOT's
# follow defaults to, so it keeps tracking when the user walks AWAY); "auto"
# prefers the person detector at range and falls back to face for close-ups.
# Env-overridable for quick A/B. NOTE: the eventual robot defaults to "person";
# "face" is the desk-only convenience default here.
FOLLOW_DETECTOR_DEFAULT: str = _env_str("YALP_FOLLOW_DETECTOR", "face")

# --- Orientation-agnostic DNN person detector (Gate H candidate) -------------
# A REAL person detector that fires from ANY angle (front/back/side) at room
# range, run via OpenCV's BUILT-IN cv2.dnn module (NO new pip dependency). The
# default is MobileNet-SSD (Caffe), which detects the Pascal-VOC "person" class
# (index 15) from a 300x300 input. The model files are NOT bundled (binaries are
# gitignored); they are downloaded once on first use and cached under
# FOLLOW_MODEL_CACHE_DIR. All values are env-overridable so the operator can pin
# a mirror or pre-seed the cache. See person_tracker.DnnPersonDetector.
FOLLOW_MODEL_CACHE_DIR: str = _env_str(
    "YALP_MODEL_CACHE_DIR", os.path.expanduser("~/.cache/yalp/models")
)
# Pascal-VOC class index for "person" in the MobileNet-SSD output.
FOLLOW_DNN_PERSON_CLASS_ID: int = _env_int("YALP_DNN_PERSON_CLASS_ID", 15)
# Square network input edge (MobileNet-SSD expects 300x300).
FOLLOW_DNN_INPUT_SIZE: int = _env_int("YALP_DNN_INPUT_SIZE", 300)
# Minimum detection confidence (0..1) to accept a person box.
FOLLOW_DNN_CONFIDENCE: float = _env_float("YALP_DNN_CONFIDENCE", 0.5)
# Cached model filenames (what to drop into FOLLOW_MODEL_CACHE_DIR by hand if the
# automatic download is blocked / offline).
FOLLOW_DNN_PROTOTXT_NAME: str = _env_str(
    "YALP_DNN_PROTOTXT_NAME", "MobileNetSSD_deploy.prototxt"
)
FOLLOW_DNN_CAFFEMODEL_NAME: str = _env_str(
    "YALP_DNN_CAFFEMODEL_NAME", "MobileNetSSD_deploy.caffemodel"
)
# Stable, reputable download sources for the MobileNet-SSD deploy files (prototxt
# + caffemodel from the same repo, so the layer names match). Env-overridable so
# the operator can point at an internal mirror.
FOLLOW_DNN_PROTOTXT_URL: str = _env_str(
    "YALP_DNN_PROTOTXT_URL",
    "https://raw.githubusercontent.com/djmv/MobilNet_SSD_opencv/master/"
    "MobileNetSSD_deploy.prototxt",
)
FOLLOW_DNN_CAFFEMODEL_URL: str = _env_str(
    "YALP_DNN_CAFFEMODEL_URL",
    "https://github.com/djmv/MobilNet_SSD_opencv/raw/master/"
    "MobileNetSSD_deploy.caffemodel",
)

# The face cascade is cheap, so re-detect far more often than HOG (every couple
# ticks) — that keeps the cheap tracker from coasting on a dead box for long.
FOLLOW_FACE_DETECT_INTERVAL_TICKS: int = 2

# Expand a detected face box DOWNWARD by this fraction of its height to
# approximate head+shoulders, giving a steadier distance proxy than the face
# alone. Width is widened modestly for the shoulders too.
FOLLOW_FACE_EXPAND_DOWN: float = 1.4

# Sanity rejection of implausible detections (kills one-off edge false positives
# like the observed x=-0.83 latch). A candidate box is dropped when its area is
# below FOLLOW_MIN_BOX_AREA_FRAC of the frame; when it is a FRESH box (no current
# track for continuity) whose center sits within FOLLOW_EDGE_MARGIN_FRAC of a
# left/right frame edge; or when it jumps more than FOLLOW_MAX_JUMP_FRAC of the
# frame width away from the box we are already tracking.
FOLLOW_MIN_BOX_AREA_FRAC: float = 0.004
FOLLOW_EDGE_MARGIN_FRAC: float = 0.08
FOLLOW_MAX_JUMP_FRAC: float = 0.6

# Camera warm-up: ignore (and don't print) the first few FOLLOW ticks while the
# webcam auto-exposes, so the "too dark / lost" noise during warm-up is quieted.
FOLLOW_WARMUP_TICKS: int = 6

# Live read-out cadence: print a heartbeat summarizing the current action at most
# this often (seconds). Acquire/lose transitions print immediately; otherwise we
# stay quiet instead of spamming an identical line every tick.
FOLLOW_HEARTBEAT_S: float = 1.5

# A held/tracked box below this confidence (0..1) is treated as not-visible.
FOLLOW_TRACK_MIN_SCORE: float = 0.20

# Steering: proportional turn toward the bbox center with a small deadband, and
# drive forward until the bbox height (distance proxy) reaches the stop fraction.
FOLLOW_TURN_DEADBAND: float = 0.12   # normalized -1..1 center error
FOLLOW_TURN_GAIN: float = 1.5
FOLLOW_FORWARD_GAIN: float = 2.0
FOLLOW_STOP_BBOX_HEIGHT: float = 0.60  # bbox height / frame height = "close enough"

# Graceful degradation: treat a frame dimmer than this mean brightness (0..255)
# as too dark to track (the lux-floor proxy for GOOD_LIGHT_LUX, §5) -> STOP and
# report, because vision tracking is unreliable in the dark.
FOLLOW_DARK_BRIGHTNESS: float = 16.0

# HYSTERESIS / lost-grace window (the real fix for the acquired/lost FLICKER).
# Track-by-detection runs the slow detector only every N ticks and the cheap
# tracker COASTS the box in between; the detector also fires intermittently, so a
# brief, NORMAL gap between detector hits must NOT read as "lost". As long as a
# valid (detected-or-coasted) box exists whose last SUCCESSFUL detection is
# younger than this grace, the target stays TRACKING (target_visible True) — the
# coasted box that steers / draws the green preview is NOT "stale". Only after the
# grace elapses with no fresh detection (and the tracker can no longer hold a box)
# does FOLLOW transition LOST -> stop -> "searching: no target". Kept SHORT (~1s)
# so a real departure still settles promptly — this is hysteresis, not latching a
# dead box forever. FOLLOW_LOST_GRACE_TICKS (derived from REACTIVE_TICK_HZ below)
# is the tick-domain window used by the tracker and the controller.
FOLLOW_LOST_GRACE_S: float = 0.9
# Back-compat alias: the old name some callers/tests pass through. The grace
# window supersedes the old "coast a few ticks then drop" policy.
FOLLOW_COAST_TICKS: int = 3

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

# --- FOLLOW hysteresis: the lost-grace window (the acquired/lost flicker fix) --
# The grace window is authoritatively expressed in SECONDS (FOLLOW_LOST_GRACE_S,
# above). A backend converts it to a TICK count from its OWN actual tick rate at
# construction time (see ``lost_grace_ticks`` below), so the intended ~0.9 s window
# is honored whether the entry point runs at 15, 20, or 50 Hz — instead of being
# frozen at import time against the 20 Hz default (which silently stretched/shrank
# the real-world window at other rates). Standing in front of the camera must read
# as a STABLE "tracking" state, not an acquired/lost flicker: track-by-detection
# only fires the DETECTOR intermittently (every few ticks) and the cheap tracker
# COASTS the box in between, so a brief, NORMAL detection gap must NOT read as
# "lost". A coasted box younger than this window stays "tracking" (target_visible
# True) — it is NOT "stale" — and only past it does FOLLOW genuinely let go
# (-> "searching: no target"); the grace does NOT latch a dead box forever. It is
# the SINGLE window used by both the tracker (its coast budget) and the steering
# controller (its "stale" gate), so the published STATE always matches the drawn
# green box. Kept deliberately >= the detector cadence
# (FOLLOW_*_DETECT_INTERVAL_TICKS) so a normal gap between detector hits never
# reads as lost (the flip-flop bug).
def lost_grace_ticks(tick_hz: float = REACTIVE_TICK_HZ) -> int:
    """Convert the seconds-domain lost-grace window to ticks at ``tick_hz``.

    Backends call this at construction with their ACTUAL tick rate so the ~0.9 s
    window holds across 15/20/50 Hz entry points (never re-derives it from the
    20 Hz default). At 20 Hz this returns 18, preserving today's semantics.
    """
    return max(1, int(round(FOLLOW_LOST_GRACE_S * float(tick_hz))))


# Back-compat: the tick-domain window derived from the DEFAULT reactive tick rate
# (~18 ticks at 20 Hz). Retained as a derived value because ``FollowController`` /
# ``PersonTracker`` import it as a default; a backend overrides it per its own
# tick_hz via ``lost_grace_ticks`` at construction.
FOLLOW_LOST_GRACE_TICKS: int = lost_grace_ticks(REACTIVE_TICK_HZ)

# Independent watchdog timeout (ms): the motor GPIO is zeroed if the reactive
# tick's heartbeat is older than this (software-spec.md §2.6).
WATCHDOG_TIMEOUT_MS: int = 100

# Hard per-session deliberative budget (software-spec.md §3). A WiFi retry-storm
# or a runaway model escalation must not silently run up cost: the agent tracks
# cumulative model calls and tokens for a session and, once either cap is hit,
# **stops issuing model calls** and falls back to IDLE/local behavior (exactly
# the §5 outage path) rather than retrying forever. This is a ceiling the
# operator sets here, separate from any per-call ``max_tokens``. Env-overridable.
BUDGET_MAX_CALLS: int = _env_int("YALP_BUDGET_MAX_CALLS", 40)
BUDGET_MAX_TOKENS: int = _env_int("YALP_BUDGET_MAX_TOKENS", 200000)

# --- Voice input / Speech-to-Text (STT) configuration -----------------------
# Audio source: 'microphone' for live capture, 'file' for a WAV path, or
# 'synthetic' for tests / offline pipelines that inject audio without hardware.
VOICE_SOURCE: str = _env_str("YALP_VOICE_SOURCE", "microphone")  # 'microphone' | 'file' | 'synthetic'
# PCM audio parameters used when capturing from a microphone or decoding a WAV.
# Sample rate (Hz); 16000 is recommended for Whisper.
VOICE_SAMPLE_RATE: int = _env_int("YALP_VOICE_SAMPLE_RATE", 16000)
# Number of audio channels (1 = mono, 2 = stereo); Whisper expects mono.
VOICE_CHANNELS: int = _env_int("YALP_VOICE_CHANNELS", 1)
# How many seconds of audio to capture per utterance (microphone mode only).
VOICE_RECORD_SECONDS: float = _env_float("YALP_VOICE_RECORD_SECONDS", 5.0)
# Path to the WAV file used when VOICE_SOURCE='file'. Empty string means unset.
VOICE_AUDIO_FILE: str = _env_str("YALP_VOICE_AUDIO_FILE", "")  # WAV path when VOICE_SOURCE='file'
# Speech-to-text backend: 'faster-whisper' for local on-device inference, or
# 'fake' for deterministic unit tests that return canned text.
STT_BACKEND: str = _env_str("YALP_STT_BACKEND", "faster-whisper")  # 'faster-whisper' | 'fake'
# Whisper model size when STT_BACKEND='faster-whisper'. 'tiny' and 'base' run
# comfortably on laptop-class CPUs (trade speed vs accuracy).
STT_MODEL: str = _env_str("YALP_STT_MODEL", "tiny")  # tiny|base for faster-whisper


# --- Reactive hardware GPIO pin map (BCM numbering; hardware.md) ---
# All pin constants use BCM (Broadcom) numbering as used by gpiozero / lgpio.
# gpiozero and lgpio are NEVER imported here; they live in the 'pi' optional extra
# and must be imported lazily inside hardware-specific modules only.
#
# Motor driver: TB6612FNG or DRV8833 dual H-bridge (see MOTOR_DRIVER_KIND below).
#   AIN1/BIN1 = PWM speed inputs (hardware PWM channels PWM0/PWM1 on Pi 5)
#   AIN2/BIN2 = direction inputs (plain GPIO, HIGH = forward per wiring)
#   STBY      = TB6612FNG standby active-LOW; DRV8833 ties nSLEEP HIGH so unused.
# Ultrasonic: HC-SR04 (5 V tolerant level-shifter in series with ECHO line).

MOTOR_LEFT_PWM_PIN: int = _env_int("YALP_MOTOR_LEFT_PWM_PIN", 12)   # hardware PWM0, left speed / AIN1
MOTOR_LEFT_DIR_PIN: int = _env_int("YALP_MOTOR_LEFT_DIR_PIN", 17)   # plain GPIO, left dir / AIN2
MOTOR_RIGHT_PWM_PIN: int = _env_int("YALP_MOTOR_RIGHT_PWM_PIN", 13) # hardware PWM1, right speed / BIN1
MOTOR_RIGHT_DIR_PIN: int = _env_int("YALP_MOTOR_RIGHT_DIR_PIN", 22) # plain GPIO, right dir / BIN2
# TB6612FNG STBY pin (active-LOW). DRV8833 ties nSLEEP high permanently, so for
# that driver this pin is unused (None). The hardware layer checks MOTOR_DRIVER_KIND
# before driving this pin. The literal "none"/"" spellings select None; any other
# value is parsed as an int pin number (fail-soft to the default 24 via _env_int).
_stby_raw: str = _env_str("YALP_MOTOR_STBY_PIN", "24")
MOTOR_STBY_PIN: int | None = (
    None
    if _stby_raw.strip().lower() in ("none", "")
    else _env_int("YALP_MOTOR_STBY_PIN", 24)
)

ULTRASONIC_TRIG_PIN: int = _env_int("YALP_ULTRASONIC_TRIG_PIN", 5)
ULTRASONIC_ECHO_PIN: int = _env_int("YALP_ULTRASONIC_ECHO_PIN", 6)

# Motor driver kind: 'drv8833' (default, nSLEEP tied HIGH) or 'tb6612fng' (STBY used).
MOTOR_DRIVER_KIND: str = _env_str("YALP_MOTOR_DRIVER", "drv8833")

# PWM carrier frequency for the motor speed inputs (Hz). 1 kHz is a good default
# for most DC gear-motors; go higher (e.g. 20 kHz) to eliminate audible whine.
MOTOR_PWM_FREQUENCY_HZ: int = _env_int("YALP_MOTOR_PWM_HZ", 1000)

# HC-SR04 ultrasonic sensor timing constants.
ULTRASONIC_MAX_POLL_HZ: float = 15.0      # max safe polling rate; HC-SR04 needs ≥60 ms between pings
ULTRASONIC_ECHO_TIMEOUT_S: float = 0.06   # treat echo as missed if not received within 60 ms
ULTRASONIC_MAX_DISTANCE_M: float = 4.0    # discard readings beyond the sensor's reliable range
SPEED_OF_SOUND_MPS: float = 343.0         # m/s at ~20 °C; distance = (echo_time * v) / 2

# Per-channel direction invert flags. Set True if a motor is wired in reverse
# (turns the "wrong" way for a given PWM/dir signal). Env values are truthy strings
# ("1", "true", "yes") — anything else is False.
MOTOR_LEFT_INVERT: bool = _env_bool("YALP_MOTOR_LEFT_INVERT", False)
MOTOR_RIGHT_INVERT: bool = _env_bool("YALP_MOTOR_RIGHT_INVERT", False)


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
    follow_detector_default: str = FOLLOW_DETECTOR_DEFAULT
    follow_model_cache_dir: str = FOLLOW_MODEL_CACHE_DIR
    follow_dnn_person_class_id: int = FOLLOW_DNN_PERSON_CLASS_ID
    follow_dnn_input_size: int = FOLLOW_DNN_INPUT_SIZE
    follow_dnn_confidence: float = FOLLOW_DNN_CONFIDENCE
    follow_face_detect_interval_ticks: int = FOLLOW_FACE_DETECT_INTERVAL_TICKS
    follow_face_expand_down: float = FOLLOW_FACE_EXPAND_DOWN
    follow_min_box_area_frac: float = FOLLOW_MIN_BOX_AREA_FRAC
    follow_edge_margin_frac: float = FOLLOW_EDGE_MARGIN_FRAC
    follow_max_jump_frac: float = FOLLOW_MAX_JUMP_FRAC
    follow_warmup_ticks: int = FOLLOW_WARMUP_TICKS
    follow_heartbeat_s: float = FOLLOW_HEARTBEAT_S
    follow_track_min_score: float = FOLLOW_TRACK_MIN_SCORE
    follow_turn_deadband: float = FOLLOW_TURN_DEADBAND
    follow_turn_gain: float = FOLLOW_TURN_GAIN
    follow_forward_gain: float = FOLLOW_FORWARD_GAIN
    follow_stop_bbox_height: float = FOLLOW_STOP_BBOX_HEIGHT
    follow_coast_ticks: int = FOLLOW_COAST_TICKS
    follow_lost_grace_s: float = FOLLOW_LOST_GRACE_S
    follow_lost_grace_ticks: int = FOLLOW_LOST_GRACE_TICKS
    follow_dark_brightness: float = FOLLOW_DARK_BRIGHTNESS
    ipc_host: str = IPC_HOST
    ipc_port: int = IPC_PORT
    safe_stop_threshold_m: float = SAFE_STOP_THRESHOLD_M
    reactive_tick_hz: float = REACTIVE_TICK_HZ
    watchdog_timeout_ms: int = WATCHDOG_TIMEOUT_MS
    budget_max_calls: int = BUDGET_MAX_CALLS
    budget_max_tokens: int = BUDGET_MAX_TOKENS
    voice_source: str = VOICE_SOURCE
    voice_sample_rate: int = VOICE_SAMPLE_RATE
    voice_channels: int = VOICE_CHANNELS
    voice_record_seconds: float = VOICE_RECORD_SECONDS
    voice_audio_file: str = VOICE_AUDIO_FILE
    stt_backend: str = STT_BACKEND
    stt_model: str = STT_MODEL
    # --- Reactive hardware GPIO pin map ---
    motor_left_pwm_pin: int = MOTOR_LEFT_PWM_PIN
    motor_left_dir_pin: int = MOTOR_LEFT_DIR_PIN
    motor_right_pwm_pin: int = MOTOR_RIGHT_PWM_PIN
    motor_right_dir_pin: int = MOTOR_RIGHT_DIR_PIN
    motor_stby_pin: int | None = MOTOR_STBY_PIN
    ultrasonic_trig_pin: int = ULTRASONIC_TRIG_PIN
    ultrasonic_echo_pin: int = ULTRASONIC_ECHO_PIN
    motor_driver_kind: str = MOTOR_DRIVER_KIND
    motor_pwm_frequency_hz: int = MOTOR_PWM_FREQUENCY_HZ
    ultrasonic_max_poll_hz: float = ULTRASONIC_MAX_POLL_HZ
    ultrasonic_echo_timeout_s: float = ULTRASONIC_ECHO_TIMEOUT_S
    ultrasonic_max_distance_m: float = ULTRASONIC_MAX_DISTANCE_M
    speed_of_sound_mps: float = SPEED_OF_SOUND_MPS
    motor_left_invert: bool = MOTOR_LEFT_INVERT
    motor_right_invert: bool = MOTOR_RIGHT_INVERT


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
    "FOLLOW_DETECTOR_DEFAULT",
    "FOLLOW_MODEL_CACHE_DIR",
    "FOLLOW_DNN_PERSON_CLASS_ID",
    "FOLLOW_DNN_INPUT_SIZE",
    "FOLLOW_DNN_CONFIDENCE",
    "FOLLOW_DNN_PROTOTXT_NAME",
    "FOLLOW_DNN_CAFFEMODEL_NAME",
    "FOLLOW_DNN_PROTOTXT_URL",
    "FOLLOW_DNN_CAFFEMODEL_URL",
    "FOLLOW_FACE_DETECT_INTERVAL_TICKS",
    "FOLLOW_FACE_EXPAND_DOWN",
    "FOLLOW_MIN_BOX_AREA_FRAC",
    "FOLLOW_EDGE_MARGIN_FRAC",
    "FOLLOW_MAX_JUMP_FRAC",
    "FOLLOW_WARMUP_TICKS",
    "FOLLOW_HEARTBEAT_S",
    "FOLLOW_TRACK_MIN_SCORE",
    "FOLLOW_TURN_DEADBAND",
    "FOLLOW_TURN_GAIN",
    "FOLLOW_FORWARD_GAIN",
    "FOLLOW_STOP_BBOX_HEIGHT",
    "FOLLOW_COAST_TICKS",
    "FOLLOW_LOST_GRACE_S",
    "FOLLOW_LOST_GRACE_TICKS",
    "lost_grace_ticks",
    "FOLLOW_DARK_BRIGHTNESS",
    "IPC_HOST",
    "IPC_PORT",
    "SAFE_STOP_THRESHOLD_M",
    "REACTIVE_TICK_HZ",
    "WATCHDOG_TIMEOUT_MS",
    "BUDGET_MAX_CALLS",
    "BUDGET_MAX_TOKENS",
    "VOICE_SOURCE",
    "VOICE_SAMPLE_RATE",
    "VOICE_CHANNELS",
    "VOICE_RECORD_SECONDS",
    "VOICE_AUDIO_FILE",
    "STT_BACKEND",
    "STT_MODEL",
    "MOTOR_LEFT_PWM_PIN",
    "MOTOR_LEFT_DIR_PIN",
    "MOTOR_RIGHT_PWM_PIN",
    "MOTOR_RIGHT_DIR_PIN",
    "MOTOR_STBY_PIN",
    "ULTRASONIC_TRIG_PIN",
    "ULTRASONIC_ECHO_PIN",
    "MOTOR_DRIVER_KIND",
    "MOTOR_PWM_FREQUENCY_HZ",
    "ULTRASONIC_MAX_POLL_HZ",
    "ULTRASONIC_ECHO_TIMEOUT_S",
    "ULTRASONIC_MAX_DISTANCE_M",
    "SPEED_OF_SOUND_MPS",
    "MOTOR_LEFT_INVERT",
    "MOTOR_RIGHT_INVERT",
    "Config",
    "get_api_key",
    "require_api_key",
]
