"""Tests for the open-loop drive SPEED MODEL with a motor deadband.

Field tuning on the real robot (2026-07-17, hardwood, under load) showed the
linear ``v = max_speed_mps * duty`` model lies badly near the floor: duty 0.30
was BELOW the motor deadband and produced zero displacement, while duty 0.45
moved reliably. The model is now::

    v = max(0, gain * (duty - duty_deadband)),  gain = max_speed_mps / (1 - duty_deadband)

so ``max_speed_mps`` keeps its "speed at full throttle" meaning, a duty at/below
the deadband predicts NO motion, and ``duty_deadband == 0`` recovers the exact
old linear model (how a pre-deadband calibration file behaves).

These cover the math (:meth:`ReactiveTickCore._model_speed_mps` /
``_drive_duration``) and the honesty WARNINGS emitted on adoption
(:meth:`ReactiveTickCore._warn_if_below_deadband`). All headless (synthetic
camera, no GPIO).
"""

from __future__ import annotations

import logging
import math

import pytest

from yalp.contract.messages import Intent, Mode
from yalp.reactive.fake_backend import FakeReactiveBackend

_TICK_LOGGER = "yalp.reactive.tick_core"


def _backend(**kwargs) -> FakeReactiveBackend:
    kwargs.setdefault("camera_source", "synthetic")
    return FakeReactiveBackend(**kwargs)


# ===========================================================================
# 1. Speed-model math: zero at/below deadband, linear above, full-throttle pin.
# ===========================================================================
def test_model_speed_zero_at_and_below_deadband():
    b = _backend(max_speed_mps=0.5, duty_deadband=0.27)
    assert b._model_speed_mps(0.0) == 0.0
    assert b._model_speed_mps(0.20) == 0.0
    # AT the deadband is still exactly zero (max(0, gain*0)).
    assert b._model_speed_mps(0.27) == 0.0
    # The first duty ABOVE the deadband moves.
    assert b._model_speed_mps(0.28) > 0.0


def test_model_speed_linear_above_deadband_and_full_throttle():
    b = _backend(max_speed_mps=0.5, duty_deadband=0.27)
    # v(1.0) == max_speed_mps regardless of the deadband — the semantics of
    # max_speed_mps ("speed at full throttle") are preserved.
    assert b._model_speed_mps(1.0) == pytest.approx(0.5)
    # Linear in between: gain = max_speed / (1 - deadband).
    gain = 0.5 / (1 - 0.27)
    assert b._model_speed_mps(0.45) == pytest.approx(gain * (0.45 - 0.27))
    # Exactly halfway up the usable duty span -> half the full-throttle speed.
    mid = 0.27 + (1 - 0.27) / 2
    assert b._model_speed_mps(mid) == pytest.approx(0.25)


def test_model_back_compat_zero_deadband_reproduces_field_measurement():
    """duty_deadband == 0 is the pure old linear model; the live-robot file
    (max_speed_mps 0.29) then gives duty 0.45 -> ~0.13 m/s (measured reality)."""
    b = _backend(max_speed_mps=0.29, duty_deadband=0.0)
    v45 = b._model_speed_mps(0.45)
    assert v45 == pytest.approx(0.29 * 0.45)
    # Inside the measured 0.114..0.155 m/s sag band recorded at duty 0.45.
    assert 0.114 <= v45 <= 0.155
    # Pure linear: full-throttle speed is max_speed_mps and there is NO floor.
    assert b._model_speed_mps(1.0) == pytest.approx(0.29)
    assert b._model_speed_mps(0.10) == pytest.approx(0.029)


def test_model_speed_degenerate_deadband_never_moves():
    # A deadband that eats the whole usable range models no motion (no ZeroDiv).
    b = _backend(max_speed_mps=0.5, duty_deadband=0.9)
    assert b._model_speed_mps(1.0) == pytest.approx(0.5 / (1 - 0.9) * 0.1)
    # The fake clamps deadband to <= 0.9, so span stays positive; force-set to
    # verify the guard against a zero/negative span.
    b.duty_deadband = 1.0
    assert b._model_speed_mps(1.0) == 0.0


# ===========================================================================
# 2. _drive_duration: +inf below the deadband, finite above, rotation unchanged.
# ===========================================================================
def test_drive_duration_infinite_below_deadband():
    b = _backend(max_speed_mps=0.5, duty_deadband=0.27)
    dur = b._drive_duration({"kind": "straight", "target": 1.0, "speed": 0.20})
    # Below the deadband the timed goal cannot complete — honest open-loop read
    # on a stalled command (no encoders to prove displacement).
    assert math.isinf(dur)


def test_drive_duration_finite_above_deadband_matches_model():
    b = _backend(max_speed_mps=0.5, duty_deadband=0.27)
    dur = b._drive_duration({"kind": "straight", "target": 1.0, "speed": 0.45})
    assert dur == pytest.approx(1.0 / b._model_speed_mps(0.45))


def test_drive_duration_rotation_keeps_linear_estimate():
    # Rotation is NOT deadband-corrected (no field data for a turn model); it
    # keeps the linear turn_rate_dps estimate.
    b = _backend(turn_rate_dps=120.0, duty_deadband=0.27)
    dur = b._drive_duration({"kind": "rotate", "target": 90.0, "speed": 0.3})
    assert dur == pytest.approx(90.0 / (120.0 * 0.3))


def test_drive_duration_zero_target_completes_immediately():
    b = _backend(max_speed_mps=0.5, duty_deadband=0.27)
    # Even at a stalled duty, a zero-distance target has nothing to travel.
    assert b._drive_duration({"kind": "straight", "target": 0.0, "speed": 0.20}) == 0.0


# ===========================================================================
# 3. Below-deadband straight really never completes (end-to-end over ticks).
# ===========================================================================
def test_below_deadband_straight_never_completes_over_ticks():
    from yalp.contract.messages import GoalStatus

    b = _backend(max_speed_mps=0.5, duty_deadband=0.27, tick_hz=50.0)
    b.apply_intent(
        Intent(Mode.DRIVE_GOAL, {"kind": "straight", "target": 1.0, "speed": 0.20}, seq=1)
    )
    statuses = set()
    for _ in range(500):  # 500 ticks @ 50 Hz = 10 s simulated — never completes
        st = b.tick()
        statuses.add(st.goal_status)
    assert GoalStatus.RUNNING in statuses
    assert GoalStatus.COMPLETED not in statuses


# ===========================================================================
# 4. Honesty warnings on adoption.
# ===========================================================================
def test_warns_on_below_deadband_straight(caplog):
    b = _backend()  # default fake deadband = config.DRIVE_DUTY_DEADBAND (0.27)
    with caplog.at_level(logging.WARNING, logger=_TICK_LOGGER):
        b.apply_intent(
            Intent(Mode.DRIVE_GOAL, {"kind": "straight", "target": 1.0, "speed": 0.20}, seq=1)
        )
        b.tick()  # adoption happens on the first tick
    msg = " ".join(r.getMessage() for r in caplog.records)
    assert "at/below the motor deadband" in msg
    assert "will likely NOT move" in msg


def test_warns_on_stiction_dominated_rotation(caplog):
    b = _backend()
    with caplog.at_level(logging.WARNING, logger=_TICK_LOGGER):
        b.apply_intent(
            Intent(Mode.DRIVE_GOAL, {"kind": "rotate", "target": 45.0, "speed": 0.3}, seq=1)
        )
        b.tick()
    msg = " ".join(r.getMessage() for r in caplog.records).lower()
    assert "stiction" in msg
    assert "over-predict" in msg


def test_no_warning_for_healthy_duty(caplog):
    b = _backend()
    with caplog.at_level(logging.WARNING, logger=_TICK_LOGGER):
        # A full-throttle straight and a fast turn are both well clear of the floors.
        b.apply_intent(
            Intent(Mode.DRIVE_GOAL, {"kind": "straight", "target": 1.0, "speed": 1.0}, seq=1)
        )
        b.tick()
        b.apply_intent(
            Intent(Mode.DRIVE_GOAL, {"kind": "rotate", "target": 90.0, "speed": 0.8}, seq=2)
        )
        b.tick()
    msg = " ".join(r.getMessage() for r in caplog.records).lower()
    assert "deadband" not in msg
    assert "stiction" not in msg


def test_warning_respects_speed_limit_clamp(caplog):
    """A healthy commanded duty CLAMPED below the deadband by a speed limit still
    warns — the clamped duty is what actually reaches the pins."""
    b = _backend()
    # Clamp all motion to 0.2 (below the 0.27 deadband) via a control-only intent.
    b.apply_intent(Intent(mode=None, goal=None, seq=1, speed_limit=0.2))
    b.tick()
    with caplog.at_level(logging.WARNING, logger=_TICK_LOGGER):
        b.apply_intent(
            Intent(Mode.DRIVE_GOAL, {"kind": "straight", "target": 1.0, "speed": 1.0}, seq=2)
        )
        b.tick()
    msg = " ".join(r.getMessage() for r in caplog.records)
    assert "at/below the motor deadband" in msg
