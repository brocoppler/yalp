"""Tests for the optional closed-loop sensors in the tick core (2026-09-16).

* IMU (MPU-6050) is the PRIMARY heading source when it answers; the camera
  estimator is the fallback; both feed the same heading hold / closed-loop turn.
* Wheel encoders close the DISTANCE loop on straight goals (closure "odometry"),
  with the open-loop timer as an upper bound.
* The INA219 pack monitor is sampled slowly into RobotState.sensors and warns
  once when the pack is low. Everything hermetic (fakes; no I2C / GPIO).
"""

from __future__ import annotations

import logging

import pytest

from yalp.camera import Camera
from yalp.contract.messages import GoalStatus, Intent, Mode
from yalp.reactive.encoders import FakeWheelEncoders
from yalp.reactive.fake_backend import FakeReactiveBackend
from yalp.reactive.hardware import FakeMotorDriver, FakeRangeSensor
from yalp.reactive.imu import FakeImu
from yalp.reactive.power_monitor import FakePowerMonitor
from yalp.reactive.real_backend import RealReactiveBackend


class _Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t

    def tick(self, dt=0.05):
        self.t += dt
        return self.t


def _real(**kw):
    motor = FakeMotorDriver()
    sensor = FakeRangeSensor()
    b = RealReactiveBackend(
        motor_driver=motor, range_sensor=sensor, camera=Camera(source="synthetic"),
        max_speed_mps=1.0, turn_rate_dps=360.0, tick_hz=20.0, probe_sensors=False, **kw,
    )
    b.start()
    return b, motor, sensor


def _straight(target, speed=0.5, seq=1):
    return Intent(Mode.DRIVE_GOAL, {"kind": "straight", "target": target, "speed": speed}, seq=seq)


# --------------------------------------------------------------------------- #
# 1. IMU heading
# --------------------------------------------------------------------------- #
def test_imu_is_primary_heading_source_even_with_synthetic_camera():
    clk = _Clock()
    imu = FakeImu(yaw_rates=20.0, monotonic=clk)  # steady +20 deg/s (LEFT)
    b, motor, _ = _real(imu=imu, heading_hold=True, trim_learning=False)
    b.apply_intent(_straight(5.0))
    for _ in range(5):
        clk.tick(0.05)
        st = b.tick()
    assert st.goal["heading_source"] == "imu"
    assert st.goal["heading_deg"] == pytest.approx(20.0 * 0.05 * 4, abs=0.5)  # first sample after adoption sets the reference
    assert st.sensors["heading_source"] == "imu"
    left, right = motor.last
    assert left > 0.5 > right  # drifted LEFT -> steer RIGHT


def test_imu_dropout_falls_back_to_blind_then_camera():
    clk = _Clock()
    imu = FakeImu(yaw_rates=10.0, monotonic=clk)
    b, motor, _ = _real(imu=imu, heading_hold=True, trim_learning=False)
    b.heading_hold_blind_ticks = 3
    b.apply_intent(_straight(5.0))
    for _ in range(4):
        clk.tick(0.05)
        b.tick()
    assert b.heading_source == "imu"
    imu.set_ok(False)  # bus fault: samples come back ok=False
    for _ in range(6):
        clk.tick(0.05)
        st = b.tick()
    # No camera estimator on a synthetic camera -> blind -> plain split, bias only.
    assert st.goal["heading_live"] is False
    assert motor.last == (0.5, 0.5)


def test_imu_gyro_calibrated_on_start():
    imu = FakeImu(yaw_rates=0.0)
    b = RealReactiveBackend(
        motor_driver=FakeMotorDriver(), range_sensor=FakeRangeSensor(), camera=Camera(source="synthetic"),
        imu=imu, probe_sensors=False,
    )
    assert imu.stats().get("calibrations", 0) == 0
    b.start()
    b.start()  # idempotent
    assert imu.stats().get("calibrations", 0) == 1


def test_closed_loop_rotate_on_imu():
    clk = _Clock()
    imu = FakeImu(yaw_rates=100.0, monotonic=clk)  # 100 deg/s LEFT
    b, motor, _ = _real(imu=imu, rotate_closed_loop=True)
    b.rotate_stop_lead_s = 0.0
    b.apply_intent(Intent(Mode.DRIVE_GOAL, {"kind": "rotate", "target": 90.0, "speed": 0.5}, seq=1))
    st = None
    n = 0
    for n in range(1, 100):
        clk.tick(0.05)
        st = b.tick()
        if st.goal_status == GoalStatus.COMPLETED:
            break
    assert st.goal["closure"] == "visual" and st.goal["heading_source"] == "imu"
    assert 17 <= n <= 20  # 90 deg at 5 deg/tick, first post-adoption sample is the reference
    assert motor.last == (0.0, 0.0)


# --------------------------------------------------------------------------- #
# 2. Encoder odometry
# --------------------------------------------------------------------------- #
def test_straight_goal_completes_on_odometry():
    clk = _Clock()
    enc = FakeWheelEncoders(monotonic=clk)
    enc.set_speeds(0.20, 0.20)  # 0.2 m/s -> 0.5 m in 2.5 s = 50 ticks
    b, motor, _ = _real(encoders=enc, heading_hold=False)
    assert b.odometry_closed_loop is True
    # Timer alone would say 0.5 m / (1.0 m/s * 0.5) = 1.0 s = 20 ticks; x3 bound = 60.
    b.apply_intent(_straight(0.5))
    st = None
    n = 0
    for n in range(1, 200):
        clk.tick(0.05)
        enc.step(0.05)
        st = b.tick()
        if st.goal_status == GoalStatus.COMPLETED:
            break
    assert st.goal["closure"] == "odometry"
    assert st.goal["odometry_m"] == pytest.approx(0.5, abs=0.03)
    assert 45 <= n <= 55
    assert st.sensors["odometry_m"] > 0.4 and st.sensors["left_mps"] == pytest.approx(0.2, abs=0.05)
    assert motor.last == (0.0, 0.0)


def test_odometry_timer_bound_when_wheels_do_not_move():
    clk = _Clock()
    enc = FakeWheelEncoders(monotonic=clk)  # never advances: stalled wheels
    b, _, _ = _real(encoders=enc)
    b.apply_intent(_straight(0.5))
    st = None
    n = 0
    bound_ticks = None
    for n in range(1, 400):
        clk.tick(0.05)
        st = b.tick()
        if bound_ticks is None:
            # The adopted duration is the timed estimate x ODOMETRY_TIMEOUT_FACTOR (3).
            bound_ticks = round(b._goal_duration_s / 0.05)
        if st.goal_status == GoalStatus.COMPLETED:
            break
    assert st.goal["closure"] == "timed"
    assert bound_ticks == pytest.approx(3 * round(b._drive_duration({"kind": "straight", "target": 0.5, "speed": 0.5}) / 0.05), abs=2)
    assert abs(n - bound_ticks) <= 2


def test_odometry_disabled_by_config_flag(monkeypatch):
    from yalp import config as cfg

    monkeypatch.setattr(cfg, "ODOMETRY_CLOSED_LOOP", False)
    b, _, _ = _real(encoders=FakeWheelEncoders())
    assert b.odometry_closed_loop is False


def test_reverse_goal_completes_on_odometry_magnitude():
    clk = _Clock()
    enc = FakeWheelEncoders(monotonic=clk)
    enc.set_speeds(-0.2, -0.2)
    b, motor, _ = _real(encoders=enc)
    b.apply_intent(_straight(-0.4))
    st = None
    for _ in range(200):
        clk.tick(0.05)
        enc.step(0.05)
        st = b.tick()
        if st.goal_status == GoalStatus.COMPLETED:
            break
    assert st.goal["closure"] == "odometry"


# --------------------------------------------------------------------------- #
# 3. Pack monitor
# --------------------------------------------------------------------------- #
def test_pack_monitor_published_and_low_warning_once(caplog):
    mon = FakePowerMonitor(voltage_v=5.45, current_a=0.4)
    b, _, _ = _real(power_monitor=mon)
    st = b.tick()
    assert st.sensors["pack_voltage_v"] == pytest.approx(5.45)
    assert st.sensors["pack_state"] in ("full", "ok")
    mon.set_voltage(4.2)
    b._power_next_ts = 0.0  # force a resample
    with caplog.at_level(logging.WARNING, logger="yalp.reactive.tick_core"):
        st = b.tick()
        b._power_next_ts = 0.0
        b.tick()
    assert st.sensors["pack_state"] in ("low", "flat")
    assert sum("pack voltage" in r.getMessage() for r in caplog.records) == 1


def test_no_sensors_publishes_none():
    b, _, _ = _real()
    st = b.tick()
    assert st.sensors is None


# --------------------------------------------------------------------------- #
# 4. Probing is skipped for tests and tolerant on the CLI path
# --------------------------------------------------------------------------- #
def test_auto_probe_without_hardware_is_silent(monkeypatch):
    b = RealReactiveBackend(
        motor_driver=FakeMotorDriver(), range_sensor=FakeRangeSensor(), camera=Camera(source="synthetic"),
        probe_sensors=True,  # "auto" for all three; nothing is fitted on this laptop
    )
    assert b._imu is None and b._encoders is None and b._power is None


def test_probe_off_flag_skips(monkeypatch):
    from yalp import config as cfg

    monkeypatch.setattr(cfg, "IMU_ENABLED", "0")
    monkeypatch.setattr(cfg, "ENCODERS_ENABLED", "off")
    monkeypatch.setattr(cfg, "POWER_MONITOR_ENABLED", "0")
    b = RealReactiveBackend(
        motor_driver=FakeMotorDriver(), range_sensor=FakeRangeSensor(), camera=Camera(source="synthetic"),
        probe_sensors=True,
    )
    assert b._imu is None and b._encoders is None and b._power is None


def test_fake_backend_can_simulate_sensors():
    clk = _Clock()
    enc = FakeWheelEncoders(monotonic=clk)
    enc.set_speeds(0.5, 0.5)
    b = FakeReactiveBackend(camera=Camera(source="synthetic"), encoders=enc, odometry_closed_loop=True, max_speed_mps=1.0)
    b.apply_intent(_straight(0.25, speed=1.0))
    st = None
    for _ in range(100):
        clk.tick(0.05)
        enc.step(0.05)
        st = b.tick()
        if st.goal_status == GoalStatus.COMPLETED:
            break
    assert st.goal["closure"] == "odometry"
