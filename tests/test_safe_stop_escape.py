"""Tests for the SAFE_STOP *escape* rule (software-spec.md §2.3, 2026-09-16).

A KNOWN obstacle inside the threshold latches ``SAFE_STOP`` and halts forward
motion, but a fresh ROTATE goal or a NEGATIVE (reverse) straight goal may be
adopted and run while the obstacle is still in view — neither moves the nose
forward, and the spec names exactly these as the operator's post-collision
recovery. A forward intent stays refused; a blind sensor latches everything.
"""

from __future__ import annotations

import pytest

from yalp.camera import Camera
from yalp.contract.messages import GoalStatus, Intent, Mode
from yalp.reactive.drive_cli import preflight_decision
from yalp.reactive.fake_backend import FakeReactiveBackend
from yalp.reactive.hardware import FakeMotorDriver, FakeRangeSensor
from yalp.reactive.real_backend import RealReactiveBackend


def _real():
    motor = FakeMotorDriver()
    sensor = FakeRangeSensor()
    b = RealReactiveBackend(
        motor_driver=motor, range_sensor=sensor, camera=Camera(source="synthetic"),
        max_speed_mps=1.0, turn_rate_dps=360.0, tick_hz=20.0,
    )
    return b, motor, sensor


def _latched():
    b, motor, sensor = _real()
    sensor.set_distance(0.2, known=True)
    st = b.tick()
    assert st.mode == Mode.SAFE_STOP and st.blocked and st.goal["reason"] == "obstacle"
    return b, motor, sensor


def test_rotate_runs_while_obstacle_latched():
    b, motor, sensor = _latched()
    b.apply_intent(Intent(Mode.DRIVE_GOAL, {"kind": "rotate", "target": 90.0, "speed": 0.5}, seq=1))
    st = b.tick()
    assert st.mode == Mode.DRIVE_GOAL and st.goal_status == GoalStatus.RUNNING
    assert motor.last == (-0.5, 0.5)  # spinning left in place
    assert st.obstacle is True and st.distance_m == pytest.approx(0.2)  # still truthful


def test_reverse_runs_while_obstacle_latched():
    b, motor, _ = _latched()
    b.apply_intent(Intent(Mode.DRIVE_GOAL, {"kind": "straight", "target": -0.3, "speed": 0.5}, seq=1))
    st = b.tick()
    assert st.goal_status == GoalStatus.RUNNING
    assert motor.last[0] < 0 and motor.last[1] < 0


def test_forward_intent_stays_refused_and_mailbox_not_drained():
    b, motor, _ = _latched()
    b.apply_intent(Intent(Mode.DRIVE_GOAL, {"kind": "straight", "target": 0.5, "speed": 0.5}, seq=1))
    for _ in range(3):
        st = b.tick()
        assert st.mode == Mode.SAFE_STOP and st.blocked
        assert motor.last == (0.0, 0.0)
    assert b.mailbox.peek() is not None  # still pending: sticky, not consumed


def test_blind_sensor_refuses_escape():
    b, motor, sensor = _real()
    sensor.set_distance(0.2, known=False)
    b.tick()
    b.apply_intent(Intent(Mode.DRIVE_GOAL, {"kind": "rotate", "target": 90.0, "speed": 0.5}, seq=1))
    st = b.tick()
    assert st.mode == Mode.SAFE_STOP and motor.last == (0.0, 0.0)


def test_escape_completion_relatches_with_evidence():
    b, motor, sensor = _latched()
    # 90 deg at 360 deg/s * 0.5 = 0.5 s = 10 ticks (open-loop; synthetic camera).
    b.apply_intent(Intent(Mode.DRIVE_GOAL, {"kind": "rotate", "target": 90.0, "speed": 0.5}, seq=1))
    saw_completed = False
    st = None
    for _ in range(40):
        st = b.tick()
        if st.goal_status == GoalStatus.COMPLETED:
            saw_completed = True
        if saw_completed and st.blocked:
            break
    assert saw_completed
    assert st.mode == Mode.SAFE_STOP and st.goal["reason"] == "obstacle"
    assert st.goal["after"] == GoalStatus.COMPLETED
    assert st.goal["kind"] == "rotate" and st.goal["closure"] == "timed"
    assert motor.last == (0.0, 0.0)


def test_forward_drive_still_halted_by_reflex_mid_run():
    b, motor, sensor = _real()
    b.apply_intent(Intent(Mode.DRIVE_GOAL, {"kind": "straight", "target": 2.0, "speed": 0.5}, seq=1))
    b.tick()
    assert motor.last == (0.5, 0.5)
    sensor.set_distance(0.1, known=True)
    st = b.tick()
    assert st.mode == Mode.SAFE_STOP and motor.last == (0.0, 0.0)


def test_running_reverse_survives_obstacle_appearing_ahead():
    """Backing away from something that is (still) in front is exactly the point."""
    b, motor, sensor = _real()
    b.apply_intent(Intent(Mode.DRIVE_GOAL, {"kind": "straight", "target": -1.0, "speed": 0.5}, seq=1))
    b.tick()
    sensor.set_distance(0.15, known=True)
    st = b.tick()
    assert st.goal_status == GoalStatus.RUNNING and motor.last == (-0.5, -0.5)


def test_fake_backend_applies_same_rule():
    b = FakeReactiveBackend(camera=Camera(source="synthetic"), turn_rate_dps=360.0)
    b.set_sensor(distance_m=0.2, known=True)
    assert b.tick().blocked
    b.apply_intent(Intent(Mode.DRIVE_GOAL, {"kind": "rotate", "target": 45.0, "speed": 0.5}, seq=1))
    assert b.tick().goal_status == GoalStatus.RUNNING


# --------------------------------------------------------------------------- #
# drive CLI pre-flight
# --------------------------------------------------------------------------- #
class _State:
    def __init__(self, blocked=True, reason="obstacle", distance=0.2, known=True):
        self.blocked = blocked
        self.goal = {"reason": reason, "distance": distance}
        self.distance_m = distance
        self.distance_known = known


def test_preflight_refuses_forward_but_allows_escape():
    st = _State()
    assert preflight_decision(st, 0.30)[0] == "refuse"
    assert preflight_decision(st, 0.30, escape=False)[0] == "refuse"
    action, why = preflight_decision(st, 0.30, escape=True)
    assert action == "proceed" and "ESCAPE" in why


def test_preflight_escape_flag_is_irrelevant_when_clear():
    st = _State(blocked=False)
    assert preflight_decision(st, 0.30, escape=True)[0] == "proceed"
