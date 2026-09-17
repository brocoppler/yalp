"""Tests for the camera-measured drivetrain diagnostics (yalp hwtest --check wheels / straight)."""

from __future__ import annotations

import argparse
import csv

from yalp.reactive.drive_diagnostics import (
    DriverCoupledYaw,
    TickingFakeCamera,
    format_straight,
    format_wheel_matrix,
    measure_straight,
    measure_wheel_matrix,
    write_straight_csv,
)
from yalp.reactive.hardware import FakeMotorDriver, FakeRangeSensor


def _rig(**yaw_kw):
    driver = FakeMotorDriver()
    camera = TickingFakeCamera()
    est = DriverCoupledYaw(driver, **yaw_kw)
    sensor = FakeRangeSensor(distance_m=1.5, known=True)
    clock = {"t": 0.0}

    def monotonic():
        clock["t"] += 1 / 15
        return clock["t"]

    return driver, camera, est, sensor, (lambda s: None), monotonic


def test_wheel_matrix_signs_and_balance():
    driver, camera, est, sensor, sleep, mono = _rig()
    res = measure_wheel_matrix(driver, camera, est, sensor, duties=(0.5,), sleep=sleep, monotonic=mono, log=lambda s: None)
    assert [(p.wheel, p.direction) for p in res] == [("left", "rev"), ("right", "rev"), ("left", "fwd"), ("right", "fwd")]
    for p in res:
        assert p.sign_ok is True, p
    # Balanced wheels: same magnitude within a couple of percent.
    mags = [abs(p.yaw_rate_dps) for p in res]
    assert max(mags) - min(mags) < 0.05 * max(mags)
    assert "balanced" in format_wheel_matrix(res)
    assert driver.last == (0.0, 0.0)  # always ends stopped


def test_wheel_matrix_detects_weak_wheel_and_wrong_sign():
    driver, camera, est, sensor, sleep, mono = _rig(right_scale=0.4)
    res = measure_wheel_matrix(driver, camera, est, sensor, duties=(0.5,), sleep=sleep, monotonic=mono, log=lambda s: None)
    table = format_wheel_matrix(res)
    assert "RIGHT wheel weak" in table
    # A reversed wheel: flip the left scale negative -> wrong sign on both left pulses.
    driver, camera, est, sensor, sleep, mono = _rig(left_scale=-1.0)
    res = measure_wheel_matrix(driver, camera, est, sensor, duties=(0.5,), sleep=sleep, monotonic=mono, log=lambda s: None)
    assert [p.sign_ok for p in res if p.wheel == "left"] == [False, False]
    assert "WRONG SIGN" in format_wheel_matrix(res)


def test_wheel_matrix_flags_sign_flipping_gap_across_duties():
    """The 2026-09-15 decay-mode signature: left weak at one duty, right at the other."""
    driver = FakeMotorDriver()
    camera = TickingFakeCamera()
    sensor = FakeRangeSensor(distance_m=1.5, known=True)
    clock = {"t": 0.0}

    def mono():
        clock["t"] += 1 / 15
        return clock["t"]

    class _Flip(DriverCoupledYaw):
        def update(self, frame):
            last = self._driver.last or (0.0, 0.0)
            duty = max(abs(last[0]), abs(last[1]))
            # weak LEFT at 0.45, weak RIGHT at 0.60
            self.left_scale, self.right_scale = (0.5, 1.0) if duty < 0.5 else (1.0, 0.5)
            return super().update(frame)

    res = measure_wheel_matrix(driver, camera, _Flip(driver), sensor, duties=(0.45, 0.60), sleep=lambda s: None, monotonic=mono, log=lambda s: None)
    assert "FLIPS SIGN" in format_wheel_matrix(res)


def test_forward_pulse_is_guarded_by_sonar():
    driver, camera, est, sensor, sleep, mono = _rig()
    sensor.set_distance(0.05, known=True)
    res = measure_wheel_matrix(driver, camera, est, sensor, duties=(0.5,), sleep=sleep, monotonic=mono, log=lambda s: None)
    fwd = [p for p in res if p.direction == "fwd"]
    assert all("sonar guard" in p.stopped for p in fwd)
    rev = [p for p in res if p.direction == "rev"]
    assert all(p.stopped == "" for p in rev)  # reverse is never sonar-guarded


def test_straight_recorder_integrates_yaw_and_writes_csv(tmp_path):
    driver, camera, est, sensor, sleep, mono = _rig(right_scale=0.8)
    res = measure_straight(driver, camera, est, sensor, duty=0.45, seconds=2.0, sleep=sleep, monotonic=mono, log=lambda s: None)
    assert res.frames > 20
    assert res.yaw_deg < -10  # weak right wheel -> drifts RIGHT
    assert res.sonar_start_m == 1.5 and res.sonar_end_m == 1.5
    text = format_straight(res)
    assert "steady drift RIGHT" in text and "more than 10 deg" in text
    out = tmp_path / "s.csv"
    write_straight_csv(res, str(out))
    rows = list(csv.DictReader(out.open()))
    assert len(rows) == res.frames
    assert set(rows[0]) == {"t_s", "delta_deg", "response", "distance_m", "known", "cum_yaw_deg"}
    assert driver.last == (0.0, 0.0)


def test_straight_recorder_stops_on_sonar_guard_and_reverses_first():
    driver, camera, est, sensor, sleep, mono = _rig()
    calls = []
    orig = driver.set_motors

    def spy(l, r):
        calls.append((l, r))
        orig(l, r)

    driver.set_motors = spy
    sensor.set_distance(0.1, known=True)
    res = measure_straight(driver, camera, est, sensor, duty=0.45, seconds=3.0, reverse_first_s=1.0, sleep=sleep, monotonic=mono, log=lambda s: None)
    assert calls[0] == (-0.45, -0.45)  # backed up first
    assert "sonar guard" in res.stopped
    assert res.seconds < 1.0


def test_straight_recorder_without_sonar():
    driver, camera, est, _, sleep, mono = _rig()
    res = measure_straight(driver, camera, est, None, duty=0.5, seconds=1.0, sleep=sleep, monotonic=mono, log=lambda s: None)
    assert res.sonar_start_m is None and res.travel_m is None
    assert "straight within noise" in format_straight(res)


def test_hwtest_dry_run_wheels_and_straight(tmp_path, capsys):
    from yalp.reactive import hwtest_cli

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers()
    hwtest_cli.add_parser(sub)
    args = parser.parse_args(["hwtest", "--check", "wheels", "--dry-run", "--duty", "0.5"])
    assert hwtest_cli.run(args) == 0
    out = capsys.readouterr().out
    assert "yaw rate" in out and "expected signs" in out
    csv_path = tmp_path / "straight.csv"
    args = parser.parse_args(["hwtest", "--check", "straight", "--dry-run", "--seconds", "1", "--save", str(csv_path)])
    assert hwtest_cli.run(args) == 0
    assert csv_path.exists()
    out = capsys.readouterr().out
    assert "integrated yaw" in out


def test_all_never_includes_floor_checks():
    from yalp.reactive import hwtest_cli

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers()
    hwtest_cli.add_parser(sub)
    args = parser.parse_args(["hwtest", "--check", "all", "--dry-run"])
    assert hwtest_cli.run(args) == 0
