"""Tests for motor calibration (persisted physical model + miswire fixes).

These run entirely on a laptop / in CI with **no GPIO hardware**:

* :class:`~yalp.reactive.calibration.MotorCalibration` round-trips to/from JSON;
* the ``yalp calibrate`` ``--dry-run`` flow (FakeMotorDriver + scripted answers)
  writes a JSON file with the expected fields;
* :class:`~yalp.reactive.real_backend.RealReactiveBackend` constructed against a
  calibration file uses its ``max_speed_mps`` / ``turn_rate_dps`` in place of the
  hand-guessed defaults;
* :class:`~yalp.reactive.hardware.GpiozeroMotorDriver.set_motors` applies the
  per-wheel ``*_trim`` scaling and ``*_invert`` flips (exercised against a fake
  ``gpiozero`` injected into ``sys.modules``, exactly like ``test_motor_driver``).
"""

from __future__ import annotations

import json
import sys
import types

import pytest

from yalp.reactive.calibration import MotorCalibration, default_path, load_if_present


# --------------------------------------------------------------------------- #
# 1. Round-trip save/load of MotorCalibration JSON.
# --------------------------------------------------------------------------- #
def test_calibration_round_trip(tmp_path):
    path = tmp_path / "calibration.json"
    cal = MotorCalibration(
        left_invert=True,
        right_invert=False,
        left_trim=0.9,
        right_trim=1.1,
        max_speed_mps=0.42,
        turn_rate_dps=137.0,
    )
    written = cal.save(path)
    assert written == path
    assert path.exists()

    # The on-disk JSON has exactly the expected fields/values.
    on_disk = json.loads(path.read_text())
    assert on_disk == {
        "left_invert": True,
        "right_invert": False,
        "left_trim": 0.9,
        "right_trim": 1.1,
        "max_speed_mps": 0.42,
        "turn_rate_dps": 137.0,
    }

    loaded = MotorCalibration.load(path)
    assert loaded == cal


def test_defaults_are_sane():
    cal = MotorCalibration()
    assert cal.left_invert is False and cal.right_invert is False
    assert cal.left_trim == 1.0 and cal.right_trim == 1.0
    assert cal.max_speed_mps == 0.5
    assert cal.turn_rate_dps == 120.0


def test_from_dict_ignores_unknown_keys():
    cal = MotorCalibration.from_dict(
        {"max_speed_mps": 0.3, "turn_rate_dps": 90.0, "bogus": 123}
    )
    assert cal.max_speed_mps == 0.3
    assert cal.turn_rate_dps == 90.0


def test_default_path_env_override(tmp_path, monkeypatch):
    target = tmp_path / "robot-a" / "cal.json"
    monkeypatch.setenv("YALP_CALIBRATION_PATH", str(target))
    assert default_path() == target

    # save()/load() with path=None then use the env-overridden default path.
    MotorCalibration(max_speed_mps=0.25).save()
    assert target.exists()
    assert MotorCalibration.load().max_speed_mps == 0.25


def test_load_if_present_missing_returns_none(tmp_path):
    assert load_if_present(tmp_path / "nope.json") is None


def test_load_if_present_corrupt_returns_none(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{ not valid json")
    assert load_if_present(bad) is None


# --------------------------------------------------------------------------- #
# 2. Dry-run calibrate (FakeMotorDriver + scripted answers) writes a file.
# --------------------------------------------------------------------------- #
def test_dry_run_calibrate_writes_file(tmp_path, capsys):
    from yalp.reactive import calibration_cli

    out = tmp_path / "calibration.json"
    args = types.SimpleNamespace(dry_run=True, out=str(out))
    rc = calibration_cli.run(args)
    assert rc == 0
    assert out.exists()

    data = json.loads(out.read_text())
    # All expected fields are present...
    assert set(data) == {
        "left_invert",
        "right_invert",
        "left_trim",
        "right_trim",
        "max_speed_mps",
        "turn_rate_dps",
    }
    # ...with the deterministic values derived from the scripted answers
    # ("y" -> no invert; 1.0 m over 2.0 s -> 0.5 m/s; 360° over 2.0 s -> 180 °/s).
    assert data["left_invert"] is False
    assert data["right_invert"] is False
    assert data["max_speed_mps"] == pytest.approx(0.5)
    assert data["turn_rate_dps"] == pytest.approx(180.0)


def test_calibrate_inverts_on_no_answers():
    """A 'no' to 'both forward?' + per-wheel answers sets the right inverts."""
    from yalp.reactive.calibration_cli import calibrate
    from yalp.reactive.hardware import FakeMotorDriver

    # both forward? -> n; left forward? -> n (invert left); right forward? -> y.
    answers = iter(["n", "n", "y", "1.0", "360"])
    cal = calibrate(
        FakeMotorDriver(),
        lambda _prompt: next(answers),
        sleep=lambda _s: None,
    )
    assert cal.left_invert is True
    assert cal.right_invert is False


def test_calibrate_drives_then_stops():
    """The wizard nudges/drives/spins and always leaves the driver stopped."""
    from yalp.reactive.calibration_cli import calibrate
    from yalp.reactive.hardware import FakeMotorDriver

    driver = FakeMotorDriver()
    answers = iter(["y", "2.0", "90"])
    cal = calibrate(driver, lambda _p: next(answers), sleep=lambda _s: None)

    # A forward nudge, a straight, and an in-place spin were all commanded.
    assert (0.4, 0.4) in driver.calls          # nudge forward
    assert (1.0, 1.0) in driver.calls          # straight at full throttle
    assert (-1.0, 1.0) in driver.calls         # spin in place (wheels oppose)
    # Last command is a stop (0, 0).
    assert driver.last == (0.0, 0.0)
    # Derived from 2.0 m / 2.0 s and 90° / 2.0 s.
    assert cal.max_speed_mps == pytest.approx(1.0)
    assert cal.turn_rate_dps == pytest.approx(45.0)


# --------------------------------------------------------------------------- #
# 3. RealReactiveBackend honors a calibration file's motion model.
# --------------------------------------------------------------------------- #
def test_real_backend_uses_calibration_file(tmp_path):
    from yalp.camera import Camera
    from yalp.reactive.hardware import FakeMotorDriver, FakeRangeSensor
    from yalp.reactive.real_backend import RealReactiveBackend

    path = tmp_path / "cal.json"
    MotorCalibration(max_speed_mps=0.33, turn_rate_dps=271.0).save(path)

    backend = RealReactiveBackend(
        motor_driver=FakeMotorDriver(),
        range_sensor=FakeRangeSensor(),
        camera=Camera(source="synthetic"),
        calibration_path=path,
    )
    assert backend.max_speed_mps == pytest.approx(0.33)
    assert backend.turn_rate_dps == pytest.approx(271.0)


def test_real_backend_falls_back_to_defaults_without_file(tmp_path):
    from yalp.camera import Camera
    from yalp.reactive.hardware import FakeMotorDriver, FakeRangeSensor
    from yalp.reactive.real_backend import RealReactiveBackend

    backend = RealReactiveBackend(
        motor_driver=FakeMotorDriver(),
        range_sensor=FakeRangeSensor(),
        camera=Camera(source="synthetic"),
        calibration_path=tmp_path / "absent.json",
    )
    assert backend.max_speed_mps == pytest.approx(0.5)
    assert backend.turn_rate_dps == pytest.approx(120.0)


def test_real_backend_explicit_args_override_calibration(tmp_path):
    from yalp.camera import Camera
    from yalp.reactive.hardware import FakeMotorDriver, FakeRangeSensor
    from yalp.reactive.real_backend import RealReactiveBackend

    path = tmp_path / "cal.json"
    MotorCalibration(max_speed_mps=0.33, turn_rate_dps=271.0).save(path)

    backend = RealReactiveBackend(
        motor_driver=FakeMotorDriver(),
        range_sensor=FakeRangeSensor(),
        camera=Camera(source="synthetic"),
        calibration_path=path,
        max_speed_mps=0.9,  # explicit wins over the file
    )
    assert backend.max_speed_mps == pytest.approx(0.9)
    assert backend.turn_rate_dps == pytest.approx(271.0)  # still from the file


# --------------------------------------------------------------------------- #
# 4. GpiozeroMotorDriver.set_motors applies trim + invert.
#    (Exercised against a fake gpiozero injected into sys.modules.)
# --------------------------------------------------------------------------- #
class _FakePWMOutputDevice:
    def __init__(self, pin, frequency=None):
        self.pin = pin
        self.frequency = frequency
        self.value = 0.0
        self.closed = False

    def close(self):
        self.closed = True


class _FakeDigitalOutputDevice:
    def __init__(self, pin):
        self.pin = pin
        self.value = 0
        self.closed = False

    def on(self):
        self.value = 1

    def off(self):
        self.value = 0

    def close(self):
        self.closed = True


class _FakeLGPIOFactory:
    def close(self):
        pass


def _make_fake_gpiozero():
    gpiozero = types.ModuleType("gpiozero")

    class _Device:
        pin_factory = None

    gpiozero.Device = _Device
    gpiozero.PWMOutputDevice = _FakePWMOutputDevice
    gpiozero.DigitalOutputDevice = _FakeDigitalOutputDevice

    pins = types.ModuleType("gpiozero.pins")
    lgpio_mod = types.ModuleType("gpiozero.pins.lgpio")
    lgpio_mod.LGPIOFactory = _FakeLGPIOFactory
    pins.lgpio = lgpio_mod
    gpiozero.pins = pins
    return gpiozero, pins, lgpio_mod


@pytest.fixture
def fake_gpiozero(monkeypatch):
    gpiozero, pins, lgpio_mod = _make_fake_gpiozero()
    monkeypatch.setitem(sys.modules, "gpiozero", gpiozero)
    monkeypatch.setitem(sys.modules, "gpiozero.pins", pins)
    monkeypatch.setitem(sys.modules, "gpiozero.pins.lgpio", lgpio_mod)
    return gpiozero


def test_set_motors_applies_trim(fake_gpiozero):
    from yalp.reactive.hardware import GpiozeroMotorDriver

    drv = GpiozeroMotorDriver(driver_kind="drv8833", left_trim=0.5, right_trim=0.8)
    drv.set_motors(0.8, 1.0)
    # DRV8833 IN/IN forward = dir (xIN2) LOW, duty = trimmed throttle.
    # left: 0.8 * 0.5 = 0.4 duty, forward; right: 1.0 * 0.8 = 0.8 duty, forward.
    assert drv._left_dir.value == 0
    assert drv._left_pwm.value == pytest.approx(0.4)
    assert drv._right_dir.value == 0
    assert drv._right_pwm.value == pytest.approx(0.8)


def test_set_motors_trim_default_is_noop(fake_gpiozero):
    from yalp.reactive.hardware import GpiozeroMotorDriver

    drv = GpiozeroMotorDriver()  # trims default to 1.0
    drv.set_motors(0.6, 0.3)
    assert drv._left_pwm.value == pytest.approx(0.6)
    assert drv._right_pwm.value == pytest.approx(0.3)


def test_set_motors_invert_flips_direction(fake_gpiozero):
    from yalp.reactive.hardware import GpiozeroMotorDriver

    drv = GpiozeroMotorDriver(driver_kind="drv8833", left_invert=True, right_invert=False)
    drv.set_motors(0.6, 0.6)
    # Left inverted: +0.6 -> -0.6 -> DRV8833 reverse: dir (xIN2) HIGH, slow-decay
    # duty = 1 - 0.6 = 0.4.
    assert drv._left_dir.value == 1
    assert drv._left_pwm.value == pytest.approx(0.4)
    # Right unchanged: forward -> dir LOW, duty 0.6.
    assert drv._right_dir.value == 0
    assert drv._right_pwm.value == pytest.approx(0.6)


def test_set_motors_trim_and_invert_together(fake_gpiozero):
    from yalp.reactive.hardware import GpiozeroMotorDriver

    drv = GpiozeroMotorDriver(driver_kind="drv8833", left_invert=True, left_trim=0.5)
    drv.set_motors(1.0, 0.0)
    # Left: 1.0 * 0.5 = 0.5, inverted -> -0.5 -> DRV8833 reverse: dir (xIN2) HIGH,
    # slow-decay duty = 1 - 0.5 = 0.5.
    assert drv._left_dir.value == 1
    assert drv._left_pwm.value == pytest.approx(0.5)
