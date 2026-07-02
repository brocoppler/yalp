"""Unit tests for :class:`yalp.reactive.real_backend.RealReactiveBackend`.

These run entirely on a laptop / in CI with **no GPIO hardware**: the backend is
constructed with injected :class:`~yalp.reactive.hardware.FakeMotorDriver` and
:class:`~yalp.reactive.hardware.FakeRangeSensor` plus a synthetic camera, so we
can assert that the real backend honors the **same** preemption/safety tick
contract as :class:`~yalp.reactive.fake_backend.FakeReactiveBackend` (the
contract tests assert this ordering is faithful) AND that drive/follow steering
is translated into signed motor throttles clamped to ``speed_limit``.

Covered (mirrors the contract the fake is held to, software-spec.md §2.3):

  (i)   a DRIVE_GOAL straight intent commands a FORWARD throttle, then reports a
        "timed, unverified" completion, drops to IDLE, and zeroes the motors;
  (ii)  an obstacle -> the motors stop FIRST, SAFE_STOP/BLOCKED is latched, and
        the pending mailbox intent is NOT drained while blocked;
  (iii) SAFE_STOP is sticky: a fresh intent while blocked does not resume; once
        the obstacle clears the pending intent is adopted and it resumes;
  (iv)  an UNKNOWN (echo-timeout) reading biases to SAFE_STOP with reason
        ``echo_timeout`` (never decayed to "clear");
  (v)   a commanded speed above ``speed_limit`` is clamped before it reaches PWM.

Plus: import/construction stay hardware-free, rotate spins the wheels in place,
and FOLLOW steering maps to motor throttles (clean stop when lost).
"""

from __future__ import annotations

import sys

import numpy as np
import pytest

from yalp.camera import Camera
from yalp.contract.messages import GoalStatus, Intent, Mode
from yalp.reactive.hardware import FakeMotorDriver, FakeRangeSensor
from yalp.reactive.person_tracker import TrackResult
from yalp.reactive.real_backend import RealReactiveBackend


# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #
class _FakeTracker:
    """Minimal track-by-detection stub: returns a canned :class:`TrackResult`.

    Defaults to "target lost" (a clean stop). Pass a ``result`` to simulate a
    confident, visible box. Ignores the frame entirely so FOLLOW tests are
    deterministic and need no OpenCV / real camera.
    """

    def __init__(self, result: TrackResult | None = None) -> None:
        self._result = result

    def update(self, _frame) -> TrackResult:
        if self._result is not None:
            return self._result
        return TrackResult(False, None, 0.0, 0, False)


class _BrightCamera:
    """A synthetic camera that hands back one bright frame once started.

    Bright enough to clear the FOLLOW dark-floor so a visible target is steered
    (a black/None frame degrades to "lost"). No capture thread, fully
    deterministic.
    """

    width, height = 640, 480

    def __init__(self) -> None:
        self._frame = np.full((self.height, self.width, 3), 200, dtype=np.uint8)
        self._started = False
        self.stopped = False

    def start(self) -> "_BrightCamera":
        self._started = True
        return self

    def stop(self) -> None:
        self._started = False
        self.stopped = True

    def latest(self):
        return self._frame if self._started else None


def _make_backend(*, camera=None, tracker=None, **kwargs):
    """Build a RealReactiveBackend with injected fakes (no hardware touched)."""
    motor = FakeMotorDriver()
    sensor = FakeRangeSensor()  # default: 4.0 m clear, known
    cam = camera if camera is not None else Camera(source="synthetic")
    backend = RealReactiveBackend(
        motor_driver=motor,
        range_sensor=sensor,
        camera=cam,
        max_speed_mps=1.0,
        tick_hz=50.0,
        tracker=tracker,
        **kwargs,
    )
    return backend, motor, sensor


# --------------------------------------------------------------------------- #
# Import / construction hygiene
# --------------------------------------------------------------------------- #
def test_module_imports_without_gpio_libraries():
    import yalp.reactive.real_backend  # noqa: F401  (re-import is cheap)

    for mod in ("gpiozero", "lgpio", "RPi", "RPi.GPIO"):
        assert mod not in sys.modules, f"{mod} must not be imported at module load"


def test_injected_fakes_skip_the_hardware_imports():
    backend, motor, sensor = _make_backend()
    # Constructing with BOTH fakes injected must not import gpiozero/lgpio.
    for mod in ("gpiozero", "lgpio"):
        assert mod not in sys.modules
    assert backend._motor_driver is motor
    assert backend._range_sensor is sensor


# --------------------------------------------------------------------------- #
# (i) DRIVE_GOAL straight: forward throttle -> timed completion -> IDLE + zeroed
# --------------------------------------------------------------------------- #
def test_drive_goal_straight_drives_forward_then_completes_idle():
    backend, motor, sensor = _make_backend()
    backend.apply_intent(
        Intent(Mode.DRIVE_GOAL, {"kind": "straight", "target": 0.5, "speed": 0.8}, seq=1)
    )

    # First tick adopts the intent and commands a FORWARD throttle on both wheels.
    st = backend.tick()
    assert st.mode == Mode.DRIVE_GOAL
    assert st.goal_status == GoalStatus.RUNNING
    assert motor.last == (0.8, 0.8)  # forward (positive), magnitude == speed

    # Advance to completion.
    state = st
    for _ in range(5000):
        state = backend.tick()
        if GoalStatus.COMPLETED in state.goal_status:
            break

    # Open-loop, timed, unverified completion — never a bare "completed".
    assert state.goal_status == "completed (timed, unverified)"
    assert backend.get_state().mode == Mode.IDLE
    # On completion the wheels are explicitly zeroed.
    assert motor.last == (0.0, 0.0)
    assert motor.stop_count >= 1


def test_drive_goal_rotate_spins_wheels_in_place():
    backend, motor, sensor = _make_backend()
    backend.apply_intent(
        Intent(Mode.DRIVE_GOAL, {"kind": "rotate", "target": 90.0, "speed": 0.5}, seq=1)
    )
    st = backend.tick()
    assert st.mode == Mode.DRIVE_GOAL
    left, right = motor.last
    # Rotate = spin in place: the wheels oppose each other.
    assert left == -right
    assert left != 0.0
    assert abs(left) == pytest.approx(0.5)


# --------------------------------------------------------------------------- #
# (ii) Obstacle -> motors stop FIRST, SAFE_STOP/BLOCKED, mailbox NOT drained
# --------------------------------------------------------------------------- #
def test_obstacle_stops_motors_and_does_not_drain_mailbox():
    backend, motor, sensor = _make_backend()
    # A pending intent that must NOT be adopted while blocked.
    backend.apply_intent(
        Intent(Mode.DRIVE_GOAL, {"kind": "straight", "target": 1.0, "speed": 1.0}, seq=7)
    )
    sensor.trigger_obstacle()  # known, close obstacle inside the threshold

    st = backend.tick()
    assert st.mode == Mode.SAFE_STOP
    assert st.goal_status == GoalStatus.BLOCKED
    assert st.blocked is True
    assert st.goal["reason"] == "obstacle"
    # Motors were stopped this tick (and never commanded forward).
    assert motor.stop_count >= 1
    assert motor.last == (0.0, 0.0)
    assert (1.0, 1.0) not in motor.calls  # the drive intent never reached the wheels

    # The pending intent is still queued — it was deliberately NOT drained.
    pending = backend.mailbox.peek()
    assert pending is not None
    assert pending.seq == 7


# --------------------------------------------------------------------------- #
# (iii) SAFE_STOP is sticky: fresh intent while blocked doesn't resume; clearing
#       the obstacle (with the pending intent) does.
# --------------------------------------------------------------------------- #
def test_safe_stop_is_sticky_until_cleared_then_resumes():
    backend, motor, sensor = _make_backend(tracker=_FakeTracker())
    sensor.trigger_obstacle()
    assert backend.tick().mode == Mode.SAFE_STOP

    # A fresh intent arriving WHILE blocked does not resume (stays latched).
    backend.apply_intent(Intent(Mode.FOLLOW, {"target": "nearest_person"}, seq=2))
    assert backend.tick().mode == Mode.SAFE_STOP

    # Clearing the obstacle lets the (still-pending) fresh intent be adopted.
    sensor.set_distance(4.0, known=True)
    st = backend.tick()
    assert st.mode == Mode.FOLLOW


def test_safe_stop_stays_latched_without_a_fresh_intent():
    backend, motor, sensor = _make_backend()
    backend.apply_intent(
        Intent(Mode.DRIVE_GOAL, {"kind": "straight", "target": 1.0, "speed": 1.0}, seq=1)
    )
    backend.tick()  # adopt + drive
    sensor.trigger_obstacle()
    assert backend.tick().mode == Mode.SAFE_STOP

    # Obstacle clears but NO fresh intent has arrived (the seq=1 drive was already
    # adopted/consumed): SAFE_STOP stays latched (sticky), motors stay stopped.
    sensor.set_distance(4.0, known=True)
    st = backend.tick()
    assert st.mode == Mode.SAFE_STOP
    assert motor.last == (0.0, 0.0)


# --------------------------------------------------------------------------- #
# (iv) Unknown (echo-timeout) reading -> SAFE_STOP with reason echo_timeout
# --------------------------------------------------------------------------- #
def test_echo_timeout_biases_to_safe_stop():
    backend, motor, sensor = _make_backend()
    # distance is nominally "clear" (4.0 m) but the echo is UNKNOWN -> STOP.
    sensor.timeout()
    st = backend.tick()
    assert st.mode == Mode.SAFE_STOP
    assert st.goal_status == GoalStatus.BLOCKED
    assert st.distance_known is False
    assert st.goal["reason"] == "echo_timeout"
    assert motor.stop_count >= 1
    assert motor.last == (0.0, 0.0)


# --------------------------------------------------------------------------- #
# (v) Commanded speed clamps to speed_limit before reaching PWM
# --------------------------------------------------------------------------- #
def test_commanded_speed_clamps_to_speed_limit():
    backend, motor, sensor = _make_backend()
    backend._state.speed_limit = 0.3  # tighten the safety speed cap
    backend.apply_intent(
        Intent(Mode.DRIVE_GOAL, {"kind": "straight", "target": 2.0, "speed": 1.0}, seq=1)
    )
    st = backend.tick()
    assert st.mode == Mode.DRIVE_GOAL
    left, right = motor.last
    assert left == pytest.approx(0.3)
    assert right == pytest.approx(0.3)
    assert abs(left) <= backend._state.speed_limit + 1e-9
    assert abs(right) <= backend._state.speed_limit + 1e-9


def test_speed_limit_intent_writes_state_then_clamps_motor_commands():
    """END-TO-END: a control-only speed-limit intent -> RobotState.speed_limit ->
    clamped wheel PWM. This is the honesty fix: 'go slow' actually slows the wheels.

    A control-only Intent (mode=None, speed_limit=0.4) is adopted WITHOUT changing
    the mode, writing RobotState.speed_limit. A SUBSEQUENT drive at speed=1.0 is
    then clamped to 0.4 at the pins (not driven full speed).
    """
    backend, motor, sensor = _make_backend()
    assert backend.get_state().speed_limit == 1.0  # wire default before any limit

    # 1. Control-only intent sets the limit (no motion, mode unchanged from IDLE).
    backend.apply_intent(Intent(mode=None, seq=1, speed_limit=0.4))
    st = backend.tick()
    assert st.mode == Mode.IDLE  # a control-only intent does NOT change the mode
    assert st.speed_limit == 0.4  # ...but the reactive layer recorded the clamp

    # 2. A subsequent full-speed drive is clamped to the recorded limit at the pins.
    backend.apply_intent(
        Intent(Mode.DRIVE_GOAL, {"kind": "straight", "target": 2.0, "speed": 1.0}, seq=2)
    )
    st = backend.tick()
    assert st.mode == Mode.DRIVE_GOAL
    left, right = motor.last
    assert left == pytest.approx(0.4)
    assert right == pytest.approx(0.4)


def test_speed_limit_intent_clamps_into_sane_band():
    """A requested limit outside [0.1, 1.0] is clamped by the reactive core."""
    backend, motor, sensor = _make_backend()
    backend.apply_intent(Intent(mode=None, seq=1, speed_limit=5.0))  # absurd
    assert backend.tick().speed_limit == 1.0  # clamped to the max
    backend.apply_intent(Intent(mode=None, seq=2, speed_limit=0.0))  # too slow
    assert backend.tick().speed_limit == 0.1  # clamped to the min


def test_follow_throttles_also_clamp_to_speed_limit():
    # A confident, off-center, very-far box would otherwise command > the cap.
    result = TrackResult(True, (600, 50, 200, 400), 0.95, 0, True)
    cam = _BrightCamera()
    backend, motor, sensor = _make_backend(camera=cam, tracker=_FakeTracker(result))
    backend._state.speed_limit = 0.4
    backend.start()
    backend.apply_intent(Intent(Mode.FOLLOW, {"target": "nearest_person"}, seq=1))
    st = backend.tick()
    assert st.mode == Mode.FOLLOW
    left, right = motor.last
    assert abs(left) <= 0.4 + 1e-9
    assert abs(right) <= 0.4 + 1e-9


# --------------------------------------------------------------------------- #
# FOLLOW steering -> motor throttles
# --------------------------------------------------------------------------- #
def test_follow_lost_target_issues_clean_stop():
    backend, motor, sensor = _make_backend(tracker=_FakeTracker())  # always lost
    backend.apply_intent(Intent(Mode.FOLLOW, {"target": "nearest_person"}, seq=1))
    st = backend.tick()
    assert st.mode == Mode.FOLLOW
    assert st.target_visible is False
    # Lost -> turn=forward=0 -> a clean stop commanded via set_motors.
    assert motor.last == (0.0, 0.0)


def test_follow_visible_target_drives_forward():
    # A confident, centered, far box -> forward, (roughly) no turn.
    result = TrackResult(True, (270, 100, 100, 100), 0.95, 0, True)
    cam = _BrightCamera()
    backend, motor, sensor = _make_backend(camera=cam, tracker=_FakeTracker(result))
    backend.start()
    backend.apply_intent(Intent(Mode.FOLLOW, {"target": "nearest_person"}, seq=1))
    st = backend.tick()
    assert st.mode == Mode.FOLLOW
    assert st.target_visible is True
    left, right = motor.last
    assert left > 0 and right > 0  # driving forward toward the person
    backend.close()


# --------------------------------------------------------------------------- #
# Teardown
# --------------------------------------------------------------------------- #
def test_close_zeroes_motors_and_releases_hardware():
    cam = _BrightCamera()
    backend, motor, sensor = _make_backend(camera=cam)
    backend.start()
    backend.close()
    assert motor.stop_count >= 1
    assert motor.closed is True
    assert sensor.closed is True
    assert cam.stopped is True
    # Idempotent.
    backend.close()
