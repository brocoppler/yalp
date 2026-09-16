"""Tests for the visual heading hold, closed-loop turns, and trim learning.

The controller lives in :class:`ReactiveTickCore` and is exercised here through
:class:`RealReactiveBackend` with injected fakes (no GPIO, no real camera): a
scripted **yaw estimator double** decides what the camera "saw" each tick, so
the tests are deterministic and hardware-free. Sign conventions (pinned by the
2026-09-15 field session):

* heading / yaw: ``+`` = the robot turned LEFT (CCW), same as a rotate target;
* correction: ``+`` = steer RIGHT = add to the LEFT wheel, subtract from the RIGHT.
"""

from __future__ import annotations

import json

import numpy as np
import pytest

from yalp.camera import Camera
from yalp.contract.messages import GoalStatus, Intent, Mode
from yalp.reactive.calibration import MotorCalibration
from yalp.reactive.fake_backend import FakeReactiveBackend
from yalp.reactive.hardware import FakeMotorDriver, FakeRangeSensor
from yalp.reactive.real_backend import RealReactiveBackend
from yalp.reactive.visual_odometry import YawSample


# --------------------------------------------------------------------------- #
# Doubles
# --------------------------------------------------------------------------- #
class _IdCamera:
    """A started camera whose frames carry a fresh capture id every tick."""

    def __init__(self) -> None:
        self._frame = np.full((480, 640, 3), 128, dtype=np.uint8)
        self._id = 0
        self._started = False

    def start(self):
        self._started = True
        return self

    def stop(self) -> None:
        self._started = False

    def latest(self):
        return self._frame if self._started else None

    def latest_with_id(self):
        if not self._started:
            return None, None
        self._id += 1
        return self._frame, self._id


class _ScriptedYaw:
    """Yaw estimator double: returns the next scripted per-frame delta (deg)."""

    def __init__(self, deltas=None, response: float = 0.9) -> None:
        self.deltas = list(deltas or [])
        self.response = response
        self.updates = 0
        self.resets = 0

    def reset(self) -> None:
        self.resets += 1

    def update(self, frame):
        self.updates += 1
        d = self.deltas.pop(0) if self.deltas else 0.0
        if d is None:  # scripted "blind" frame
            return None
        return YawSample(delta_deg=float(d), dx_px=0.0, response=self.response)


def _backend(*, yaw=None, camera=None, cal_path=None, **kw):
    motor = FakeMotorDriver()
    sensor = FakeRangeSensor()
    cam = camera if camera is not None else _IdCamera()
    backend = RealReactiveBackend(
        motor_driver=motor,
        range_sensor=sensor,
        camera=cam,
        max_speed_mps=1.0,
        tick_hz=20.0,
        yaw_estimator=yaw,
        calibration_path=cal_path,
        **kw,
    )
    backend.start()
    return backend, motor


def _straight(target=2.0, speed=0.5, seq=1):
    return Intent(Mode.DRIVE_GOAL, {"kind": "straight", "target": target, "speed": speed}, seq=seq)


def _rotate(deg, speed=0.5, seq=1):
    return Intent(Mode.DRIVE_GOAL, {"kind": "rotate", "target": deg, "speed": speed}, seq=seq)


# --------------------------------------------------------------------------- #
# 1. Straight drive: the correction has the right SIGN and is bounded.
# --------------------------------------------------------------------------- #
def test_left_drift_speeds_left_wheel_and_slows_right():
    # Every frame says "yawed +1 deg (LEFT)"; heading integrates upward.
    yaw = _ScriptedYaw([1.0] * 50)
    b, motor = _backend(yaw=yaw, heading_hold=True, trim_learning=False)
    b.apply_intent(_straight())
    for _ in range(6):
        b.tick()
    left, right = motor.last
    assert left > 0.5 > right, (left, right)
    # Symmetric about the base duty (bias is 0.0).
    assert left - 0.5 == pytest.approx(0.5 - right)
    # Published, wire-compatible heading fields on the goal dict.
    goal = b.get_state().goal
    assert goal["heading_deg"] > 0
    assert goal["heading_live"] is True
    assert goal["closure"] == "timed"


def test_right_drift_speeds_right_wheel():
    yaw = _ScriptedYaw([-1.0] * 50)
    b, motor = _backend(yaw=yaw, heading_hold=True, trim_learning=False)
    b.apply_intent(_straight())
    for _ in range(6):
        b.tick()
    left, right = motor.last
    assert right > 0.5 > left


def test_correction_is_proportional_to_heading_error():
    yaw = _ScriptedYaw([2.0] * 50, response=0.9)
    b, motor = _backend(yaw=yaw, heading_hold=True, trim_learning=False)
    b.heading_hold_gain = 0.01
    b.heading_hold_kd = 0.0
    b.apply_intent(_straight())
    b.tick()  # adopt (+ first sample: heading 2 deg)
    b.tick()  # heading 4 deg -> correction 0.04
    left, right = motor.last
    assert left - right == pytest.approx(2 * 0.01 * b._heading_deg, abs=1e-6)


def test_correction_is_clamped_to_heading_hold_max():
    yaw = _ScriptedYaw([30.0] * 50)  # absurd drift
    b, motor = _backend(yaw=yaw, heading_hold=True, trim_learning=False)
    b.heading_hold_max = 0.1
    b.apply_intent(_straight())
    for _ in range(5):
        b.tick()
    left, right = motor.last
    assert left == pytest.approx(0.6) and right == pytest.approx(0.4)


def test_wheel_commands_respect_speed_limit_after_correction():
    yaw = _ScriptedYaw([5.0] * 50)
    b, motor = _backend(yaw=yaw, heading_hold=True, trim_learning=False)
    b.apply_intent(Intent(Mode.DRIVE_GOAL, {"kind": "straight", "target": 2.0, "speed": 0.9}, seq=1, speed_limit=0.95))
    for _ in range(5):
        b.tick()
    left, right = motor.last
    assert left <= 0.95 + 1e-9


def test_reverse_drive_corrects_with_same_sign_rule():
    """Nose drifted LEFT while backing up: v_left must exceed v_right, i.e. the
    left wheel backs up SLOWER (less negative) — same ``left + c`` formula."""
    yaw = _ScriptedYaw([1.0] * 50)
    b, motor = _backend(yaw=yaw, heading_hold=True, trim_learning=False)
    b.apply_intent(_straight(target=-2.0))
    for _ in range(6):
        b.tick()
    left, right = motor.last
    assert left < 0 and right < 0
    assert left > right  # left less negative -> robot turns back right


# --------------------------------------------------------------------------- #
# 2. When the hold must NOT act.
# --------------------------------------------------------------------------- #
def test_disabled_hold_keeps_pure_open_loop_split():
    yaw = _ScriptedYaw([3.0] * 50)
    b, motor = _backend(yaw=yaw, heading_hold=False)
    b.apply_intent(_straight())
    for _ in range(6):
        b.tick()
    assert motor.last == (0.5, 0.5)


def test_synthetic_camera_disables_hold_by_default():
    b, motor = _backend(camera=Camera(source="synthetic"))
    assert b.heading_hold_enabled is False
    assert b.rotate_closed_loop is False
    b.apply_intent(_straight())
    for _ in range(6):
        b.tick()
    assert motor.last == (0.5, 0.5)


def test_blind_camera_drops_closed_loop_term_after_grace():
    # 3 good frames drifting left, then permanently blind (None samples).
    yaw = _ScriptedYaw([2.0, 2.0, 2.0] + [None] * 100)
    b, motor = _backend(yaw=yaw, heading_hold=True, trim_learning=False)
    b.heading_hold_blind_ticks = 4
    b.apply_intent(_straight())
    for _ in range(4):
        b.tick()
    corrected = motor.last
    assert corrected[0] > corrected[1]  # was correcting while live
    for _ in range(10):
        b.tick()
    assert motor.last == (0.5, 0.5)  # blind too long -> bias only (0) -> plain split
    assert b.get_state().goal["heading_live"] is False


def test_low_confidence_samples_do_not_move_the_heading():
    yaw = _ScriptedYaw([5.0] * 50, response=0.01)  # below the confidence floor
    b, motor = _backend(yaw=yaw, heading_hold=True, trim_learning=False)
    b.apply_intent(_straight())
    for _ in range(6):
        b.tick()
    assert b._heading_deg == 0.0
    assert motor.last == (0.5, 0.5)


def test_repeated_capture_id_is_not_a_new_sample():
    class _StaleCamera(_IdCamera):
        def latest_with_id(self):
            return (self._frame, 7) if self._started else (None, None)

    yaw = _ScriptedYaw([1.0] * 50)
    b, motor = _backend(yaw=yaw, camera=_StaleCamera(), heading_hold=True, trim_learning=False)
    b.apply_intent(_straight())
    for _ in range(6):
        b.tick()
    assert yaw.updates == 1  # one frame id ever -> one estimator update
    # That single sample landed on the adoption tick, i.e. it measured motion
    # BEFORE the goal existed, so adoption zeroed it: heading stays 0.
    assert b._heading_deg == pytest.approx(0.0)


def test_heading_resets_at_each_adoption():
    yaw = _ScriptedYaw([1.0] * 100)
    b, _ = _backend(yaw=yaw, heading_hold=True, trim_learning=False)
    b.apply_intent(_straight(seq=1))
    for _ in range(5):
        b.tick()
    assert b._heading_deg > 0
    b.apply_intent(_straight(seq=2))
    b.tick()  # preempt tick
    b.tick()  # adopt tick: this tick's sample lands first, then adoption zeroes it
    assert b._heading_deg == pytest.approx(0.0, abs=1e-9)
    b.tick()  # the first sample AFTER adoption counts
    assert b._heading_deg == pytest.approx(1.0, abs=1e-9)


# --------------------------------------------------------------------------- #
# 3. Closed-loop rotate goals.
# --------------------------------------------------------------------------- #
def test_rotate_completes_when_visual_yaw_reaches_target():
    yaw = _ScriptedYaw([10.0] * 100)
    b, motor = _backend(yaw=yaw, heading_hold=True, rotate_closed_loop=True, turn_rate_dps=1000.0)
    # Open-loop timer would say 90 deg at 1000 deg/s * 0.5 = 0.18 s = ~4 ticks;
    # bounded x3 = ~11 ticks. Visual: 10 deg/frame -> 9 ticks to reach 90.
    b.apply_intent(_rotate(90.0))
    state = None
    ticks = 0
    for ticks in range(1, 40):
        state = b.tick()
        if state.goal_status == GoalStatus.COMPLETED:
            break
    assert state.goal_status == GoalStatus.COMPLETED  # the pinned contract string
    assert state.goal["closure"] == "visual"
    assert abs(state.goal["heading_deg"]) >= 90.0
    # 9 samples after adoption (the adoption tick's own sample is pre-goal).
    assert ticks == 10
    assert motor.last == (0.0, 0.0)
    assert b.get_state().mode == Mode.IDLE


def test_rotate_right_is_negative_target_and_negative_yaw():
    yaw = _ScriptedYaw([-15.0] * 100)
    b, motor = _backend(yaw=yaw, rotate_closed_loop=True, turn_rate_dps=1000.0)
    b.apply_intent(_rotate(-45.0))
    state = None
    for _ in range(40):
        state = b.tick()
        if state.goal_status == GoalStatus.COMPLETED:
            break
    assert state.goal["closure"] == "visual"
    assert state.goal["heading_deg"] <= -45.0
    # Spin-in-place wheel sign for a RIGHT turn: left forward, right back.
    fwd = [c for c in motor.calls if c != (0.0, 0.0)]
    assert fwd and fwd[-1][0] > 0 > fwd[-1][1]


def test_rotate_falls_back_to_bounded_timer_when_blind():
    yaw = _ScriptedYaw([None] * 200)  # camera never yields a sample
    b, _ = _backend(yaw=yaw, rotate_closed_loop=True, turn_rate_dps=100.0)
    b.rotate_timeout_factor = 2.0
    b.apply_intent(_rotate(90.0))  # timed: 90/(100*0.5)=1.8 s -> x2 = 3.6 s = 72 ticks
    state = None
    ticks = 0
    for ticks in range(1, 200):
        state = b.tick()
        if state.goal_status == GoalStatus.COMPLETED:
            break
    assert state.goal["closure"] == "timed"
    assert 70 <= ticks <= 74


def test_rotate_open_loop_when_closed_loop_disabled():
    yaw = _ScriptedYaw([10.0] * 100)
    b, _ = _backend(yaw=yaw, rotate_closed_loop=False, turn_rate_dps=100.0)
    b.apply_intent(_rotate(90.0))  # timed 1.8 s = 36 ticks, NOT x3 and NOT visual
    state = None
    ticks = 0
    for ticks in range(1, 100):
        state = b.tick()
        if state.goal_status == GoalStatus.COMPLETED:
            break
    assert state.goal["closure"] == "timed"
    assert 35 <= ticks <= 38


def test_rotate_progress_reflects_visual_fraction():
    yaw = _ScriptedYaw([9.0] * 100)
    b, _ = _backend(yaw=yaw, rotate_closed_loop=True, turn_rate_dps=1000.0)
    b.apply_intent(_rotate(90.0))
    b.tick()  # adopt (its sample is pre-goal)
    b.tick()
    b.tick()
    b.tick()  # 27 deg of 90 -> progress >= 0.3 even though the timer says less
    assert b.get_state().goal["progress"] >= 0.3


# --------------------------------------------------------------------------- #
# 4. Trim learning: a completed straight drive folds its mean correction into a
#    persisted feed-forward bias, which the next backend picks up.
# --------------------------------------------------------------------------- #
def test_learned_bias_is_persisted_and_reloaded(tmp_path):
    path = tmp_path / "cal.json"
    MotorCalibration(max_speed_mps=1.0).save(path)
    yaw = _ScriptedYaw([1.0] * 500)  # steady left drift the whole drive
    b, motor = _backend(yaw=yaw, cal_path=path, heading_hold=True, trim_learning=True)
    b.trim_learning_rate = 1.0
    b.apply_intent(_straight(target=1.0, speed=1.0))  # 1 m at 1 m/s = 20 ticks
    state = None
    for _ in range(60):
        state = b.tick()
        if state.goal_status == GoalStatus.COMPLETED:
            break
    assert state.goal_status == GoalStatus.COMPLETED
    assert "learned_bias" in state.goal and state.goal["learned_bias"] > 0
    on_disk = json.loads(path.read_text())
    assert on_disk["straight_bias_fwd"] == pytest.approx(state.goal["learned_bias"], abs=1e-3)
    assert on_disk["straight_bias_rev"] == 0.0
    assert on_disk["max_speed_mps"] == 1.0  # the rest of the file is untouched

    # A fresh backend applies the bias from tick one, even while still blind.
    yaw2 = _ScriptedYaw([None] * 100)
    b2, motor2 = _backend(yaw=yaw2, cal_path=path, heading_hold=True)
    b2.apply_intent(_straight())
    for _ in range(3):
        b2.tick()
    left, right = motor2.last
    assert left > 0.5 > right
    assert left - 0.5 == pytest.approx(on_disk["straight_bias_fwd"], abs=1e-6)


def test_learning_is_skipped_for_short_or_blind_drives(tmp_path):
    path = tmp_path / "cal.json"
    MotorCalibration(max_speed_mps=1.0).save(path)
    yaw = _ScriptedYaw([None] * 500)  # never a live estimate
    b, _ = _backend(yaw=yaw, cal_path=path, heading_hold=True, trim_learning=True)
    b.apply_intent(_straight(target=1.0, speed=1.0))
    for _ in range(60):
        if b.tick().goal_status == GoalStatus.COMPLETED:
            break
    assert json.loads(path.read_text())["straight_bias_fwd"] == 0.0


def test_learning_disabled_leaves_file_alone(tmp_path):
    path = tmp_path / "cal.json"
    MotorCalibration(max_speed_mps=1.0).save(path)
    before = path.read_text()
    yaw = _ScriptedYaw([1.0] * 500)
    b, _ = _backend(yaw=yaw, cal_path=path, heading_hold=True, trim_learning=False)
    b.apply_intent(_straight(target=1.0, speed=1.0))
    for _ in range(60):
        if b.tick().goal_status == GoalStatus.COMPLETED:
            break
    assert path.read_text() == before


def test_no_calibration_file_is_never_invented(tmp_path):
    path = tmp_path / "missing.json"
    yaw = _ScriptedYaw([1.0] * 500)
    b, _ = _backend(yaw=yaw, cal_path=path, heading_hold=True, trim_learning=True)
    b.apply_intent(_straight(target=1.0, speed=1.0))
    for _ in range(60):
        if b.tick().goal_status == GoalStatus.COMPLETED:
            break
    assert not path.exists()


def test_reverse_bias_is_learned_separately(tmp_path):
    path = tmp_path / "cal.json"
    MotorCalibration(max_speed_mps=1.0).save(path)
    yaw = _ScriptedYaw([-1.0] * 500)  # drifts RIGHT while reversing
    b, _ = _backend(yaw=yaw, cal_path=path, heading_hold=True, trim_learning=True)
    b.trim_learning_rate = 1.0
    b.apply_intent(_straight(target=-1.0, speed=1.0))
    for _ in range(60):
        if b.tick().goal_status == GoalStatus.COMPLETED:
            break
    on_disk = json.loads(path.read_text())
    assert on_disk["straight_bias_rev"] < 0
    assert on_disk["straight_bias_fwd"] == 0.0


# --------------------------------------------------------------------------- #
# 5. The fake backend can opt in (shared core), and stays off by default.
# --------------------------------------------------------------------------- #
def test_fake_backend_opt_in_uses_the_same_controller():
    yaw = _ScriptedYaw([1.0] * 50)
    b = FakeReactiveBackend(camera=_IdCamera(), heading_hold=True, yaw_estimator=yaw)
    b.start()
    b.apply_intent(_straight())
    for _ in range(6):
        b.tick()
    assert b._drive_throttles(b.get_state().goal)[0] > 0.5


def test_fake_backend_default_is_open_loop():
    b = FakeReactiveBackend(camera=Camera(source="synthetic"))
    assert b.heading_hold_enabled is False and b._yaw is None


def test_learning_also_runs_when_the_reflex_ends_the_drive(tmp_path):
    """On a small floor most drives end in SAFE_STOP, not COMPLETED — the
    steering they needed must still be learned."""
    path = tmp_path / "cal.json"
    MotorCalibration(max_speed_mps=1.0).save(path)
    yaw = _ScriptedYaw([1.0] * 500)
    b, motor = _backend(yaw=yaw, cal_path=path, heading_hold=True, trim_learning=True)
    b.trim_learning_rate = 1.0
    sensor = b._range_sensor
    b.apply_intent(_straight(target=5.0, speed=1.0))  # would take 100 ticks
    for _ in range(30):
        b.tick()
    sensor.set_distance(0.1, known=True)  # obstacle -> reflex
    state = b.tick()
    assert state.mode == Mode.SAFE_STOP and state.blocked
    assert json.loads(path.read_text())["straight_bias_fwd"] > 0
    assert motor.last == (0.0, 0.0)


def test_learning_also_runs_on_preemption(tmp_path):
    path = tmp_path / "cal.json"
    MotorCalibration(max_speed_mps=1.0).save(path)
    yaw = _ScriptedYaw([1.0] * 500)
    b, _ = _backend(yaw=yaw, cal_path=path, heading_hold=True, trim_learning=True)
    b.trim_learning_rate = 1.0
    b.apply_intent(_straight(target=5.0, speed=1.0, seq=1))
    for _ in range(30):
        b.tick()
    b.apply_intent(Intent(Mode.IDLE, None, seq=2))  # preempts the running drive
    state = b.tick()
    assert state.goal_status == GoalStatus.PREEMPTED
    assert json.loads(path.read_text())["straight_bias_fwd"] > 0
