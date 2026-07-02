"""Tests for :class:`GpiozeroMotorDriver` — the real Pi motor driver.

``gpiozero``/``lgpio`` are **not** installed on the laptop/CI that runs these
tests, so we exercise the driver against a *fake* ``gpiozero`` package injected
into ``sys.modules``. The fake records every device created and every duty/
direction written, letting us assert the phase/enable logic (clamping, invert,
direction pin, PWM duty, STBY) without any hardware.

Behaviors that genuinely need a real Pi (actual lgpio pin toggling, the Pi-5
RPi.GPIO silent-failure) are noted inline and verified only at the *contract*
level here (e.g. we check the driver refuses an RPi.GPIO factory, but real pin
electrical behavior can only be confirmed on hardware).
"""

from __future__ import annotations

import sys
import types

import pytest

from tests._import_isolation import assert_import_leaves_module_unloaded


# --------------------------------------------------------------------------- #
# 1. The module must import with NO gpiozero present (lazy imports).
# --------------------------------------------------------------------------- #
def test_module_imports_without_gpiozero():
    """Importing hardware.py must not require gpiozero/lgpio/RPi.

    The "no side-effect import" half is checked in a *fresh subprocess* so it is
    order-independent and holds on a Pi (where gpiozero/lgpio are installed) as
    well as on a laptop — see tests/_import_isolation.py. The structural check on
    the class runs in-process below.
    """
    assert_import_leaves_module_unloaded(
        "yalp.reactive.hardware", ("gpiozero", "lgpio")
    )

    import yalp.reactive.hardware as hw  # noqa: F401  (import must not raise)

    # The class is present and satisfies the MotorDriver protocol structurally.
    assert hasattr(hw, "GpiozeroMotorDriver")


# --------------------------------------------------------------------------- #
# Fake gpiozero package
# --------------------------------------------------------------------------- #
class _FakePWMOutputDevice:
    """Records PWM duty writes; mirrors gpiozero.PWMOutputDevice's API surface."""

    def __init__(self, pin, frequency=None):
        self.pin = pin
        self.frequency = frequency
        self.value = 0.0
        self.closed = False

    def close(self):
        self.closed = True


class _FakeDigitalOutputDevice:
    """Records on()/off() state; mirrors gpiozero.DigitalOutputDevice."""

    def __init__(self, pin):
        self.pin = pin
        self.value = 0  # 0 = low, 1 = high
        self.closed = False

    def on(self):
        self.value = 1

    def off(self):
        self.value = 0

    def close(self):
        self.closed = True


class _FakeLGPIOFactory:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _FakeRPiGPIOFactory:
    """Stand-in for gpiozero.pins.rpigpio.RPiGPIOFactory (the bad-on-Pi5 one)."""

    def close(self):
        pass


def _make_fake_gpiozero():
    """Build a minimal fake ``gpiozero`` package tree and return the root module."""
    gpiozero = types.ModuleType("gpiozero")

    class _Device:
        pin_factory = None

    gpiozero.Device = _Device
    gpiozero.PWMOutputDevice = _FakePWMOutputDevice
    gpiozero.DigitalOutputDevice = _FakeDigitalOutputDevice

    # gpiozero.pins subpackage
    pins = types.ModuleType("gpiozero.pins")
    lgpio_mod = types.ModuleType("gpiozero.pins.lgpio")
    lgpio_mod.LGPIOFactory = _FakeLGPIOFactory
    pins.lgpio = lgpio_mod

    gpiozero.pins = pins
    return gpiozero, pins, lgpio_mod


@pytest.fixture()
def fake_gpiozero(monkeypatch):
    """Inject a fake gpiozero package into sys.modules for the duration of a test."""
    gpiozero, pins, lgpio_mod = _make_fake_gpiozero()
    monkeypatch.setitem(sys.modules, "gpiozero", gpiozero)
    monkeypatch.setitem(sys.modules, "gpiozero.pins", pins)
    monkeypatch.setitem(sys.modules, "gpiozero.pins.lgpio", lgpio_mod)
    yield gpiozero


# --------------------------------------------------------------------------- #
# 2. Construction wires up the pins / pin factory.
# --------------------------------------------------------------------------- #
def test_init_sets_lgpio_pin_factory_and_creates_devices(fake_gpiozero):
    from yalp.reactive.hardware import GpiozeroMotorDriver

    drv = GpiozeroMotorDriver(driver_kind="drv8833")

    # lgpio factory installed.
    from gpiozero.pins.lgpio import LGPIOFactory

    assert isinstance(fake_gpiozero.Device.pin_factory, LGPIOFactory)

    # PWM speed pins on the hardware-PWM lines, at the configured frequency.
    assert drv._left_pwm.pin == 12
    assert drv._right_pwm.pin == 13
    assert drv._left_pwm.frequency == 1000
    assert drv._right_pwm.frequency == 1000

    # Direction pins are plain GPIO.
    assert drv._left_dir.pin == 17
    assert drv._right_dir.pin == 22

    # DRV8833 ignores STBY.
    assert drv._stby is None


def test_init_satisfies_motordriver_protocol(fake_gpiozero):
    from yalp.reactive.hardware import GpiozeroMotorDriver, MotorDriver

    drv = GpiozeroMotorDriver()
    assert isinstance(drv, MotorDriver)


# --------------------------------------------------------------------------- #
# 3. set_motors: direction pin + PWM duty.
# --------------------------------------------------------------------------- #
def test_set_motors_forward(fake_gpiozero):
    from yalp.reactive.hardware import GpiozeroMotorDriver

    drv = GpiozeroMotorDriver()
    drv.set_motors(0.5, 0.75)

    assert drv._left_dir.value == 1  # forward
    assert drv._right_dir.value == 1
    assert drv._left_pwm.value == pytest.approx(0.5)
    assert drv._right_pwm.value == pytest.approx(0.75)


def test_set_motors_reverse(fake_gpiozero):
    from yalp.reactive.hardware import GpiozeroMotorDriver

    drv = GpiozeroMotorDriver()
    drv.set_motors(-0.4, -0.9)

    assert drv._left_dir.value == 0  # reverse
    assert drv._right_dir.value == 0
    assert drv._left_pwm.value == pytest.approx(0.4)
    assert drv._right_pwm.value == pytest.approx(0.9)


def test_set_motors_zero_is_forward_direction_with_zero_duty(fake_gpiozero):
    from yalp.reactive.hardware import GpiozeroMotorDriver

    drv = GpiozeroMotorDriver()
    drv.set_motors(0.0, 0.0)

    # throttle >= 0 → direction HIGH, duty 0.
    assert drv._left_dir.value == 1
    assert drv._right_dir.value == 1
    assert drv._left_pwm.value == 0.0
    assert drv._right_pwm.value == 0.0


# --------------------------------------------------------------------------- #
# 4. Clamping.
# --------------------------------------------------------------------------- #
def test_set_motors_clamps_out_of_range(fake_gpiozero):
    from yalp.reactive.hardware import GpiozeroMotorDriver

    drv = GpiozeroMotorDriver()
    drv.set_motors(5.0, -5.0)

    assert drv._left_dir.value == 1
    assert drv._left_pwm.value == pytest.approx(1.0)
    assert drv._right_dir.value == 0
    assert drv._right_pwm.value == pytest.approx(1.0)


# --------------------------------------------------------------------------- #
# 5. Per-channel invert.
# --------------------------------------------------------------------------- #
def test_set_motors_left_invert(fake_gpiozero):
    from yalp.reactive.hardware import GpiozeroMotorDriver

    drv = GpiozeroMotorDriver(left_invert=True, right_invert=False)
    drv.set_motors(0.6, 0.6)

    # Left inverted: a +0.6 command drives the dir pin LOW (reverse) at duty 0.6.
    assert drv._left_dir.value == 0
    assert drv._left_pwm.value == pytest.approx(0.6)
    # Right not inverted: forward.
    assert drv._right_dir.value == 1
    assert drv._right_pwm.value == pytest.approx(0.6)


def test_set_motors_right_invert_reverse_command(fake_gpiozero):
    from yalp.reactive.hardware import GpiozeroMotorDriver

    drv = GpiozeroMotorDriver(right_invert=True)
    drv.set_motors(0.0, -0.3)

    # Right inverted: a -0.3 command flips to forward (dir HIGH) at duty 0.3.
    assert drv._right_dir.value == 1
    assert drv._right_pwm.value == pytest.approx(0.3)


# --------------------------------------------------------------------------- #
# 6. STBY handling (TB6612FNG vs DRV8833).
# --------------------------------------------------------------------------- #
def test_tb6612fng_drives_stby_high(fake_gpiozero):
    from yalp.reactive.hardware import GpiozeroMotorDriver

    drv = GpiozeroMotorDriver(driver_kind="tb6612fng", stby_pin=24)
    assert drv._stby is not None
    assert drv._stby.pin == 24
    assert drv._stby.value == 1  # enabled on init


def test_tb6612fng_without_stby_pin(fake_gpiozero):
    from yalp.reactive.hardware import GpiozeroMotorDriver

    drv = GpiozeroMotorDriver(driver_kind="tb6612fng", stby_pin=None)
    assert drv._stby is None


def test_drv8833_ignores_stby_pin(fake_gpiozero):
    from yalp.reactive.hardware import GpiozeroMotorDriver

    drv = GpiozeroMotorDriver(driver_kind="drv8833", stby_pin=24)
    assert drv._stby is None


# --------------------------------------------------------------------------- #
# 7. stop() coasts: zero PWM, leave direction pins alone.
# --------------------------------------------------------------------------- #
def test_stop_zeros_pwm_only(fake_gpiozero):
    from yalp.reactive.hardware import GpiozeroMotorDriver

    drv = GpiozeroMotorDriver()
    drv.set_motors(-0.8, 0.8)
    left_dir_before = drv._left_dir.value
    right_dir_before = drv._right_dir.value

    drv.stop()

    assert drv._left_pwm.value == 0.0
    assert drv._right_pwm.value == 0.0
    # Direction pins unchanged (coast, not reverse).
    assert drv._left_dir.value == left_dir_before
    assert drv._right_dir.value == right_dir_before


# --------------------------------------------------------------------------- #
# 8. close() zeros PWM, closes devices, releases factory; idempotent.
# --------------------------------------------------------------------------- #
def test_close_zeros_and_closes_all_devices(fake_gpiozero):
    from yalp.reactive.hardware import GpiozeroMotorDriver

    drv = GpiozeroMotorDriver(driver_kind="tb6612fng", stby_pin=24)
    drv.set_motors(0.5, 0.5)

    factory = fake_gpiozero.Device.pin_factory
    drv.close()

    assert drv._left_pwm.value == 0.0
    assert drv._right_pwm.value == 0.0
    for dev in (drv._left_pwm, drv._right_pwm, drv._left_dir, drv._right_dir, drv._stby):
        assert dev.closed is True
    # Pin factory best-effort released.
    assert factory.closed is True
    assert fake_gpiozero.Device.pin_factory is None

    # Idempotent: a second close() must not raise.
    drv.close()


# --------------------------------------------------------------------------- #
# 9. Pin-factory safety: refuse to run on RPi.GPIO (silently fails on Pi 5).
# --------------------------------------------------------------------------- #
def test_refuses_rpigpio_pin_factory(fake_gpiozero):
    from yalp.reactive.hardware import GpiozeroMotorDriver

    # Simulate gpiozero already having defaulted to the RPi.GPIO factory.
    fake_gpiozero.Device.pin_factory = _FakeRPiGPIOFactory()

    with pytest.raises(RuntimeError, match="RPi.GPIO"):
        GpiozeroMotorDriver()


# --------------------------------------------------------------------------- #
# 10. Pin-factory safety: fail loudly when lgpio cannot be loaded.
# --------------------------------------------------------------------------- #
def test_fails_loudly_when_lgpio_unavailable(monkeypatch):
    # Inject gpiozero but WITHOUT a usable gpiozero.pins.lgpio module.
    gpiozero, pins, _ = _make_fake_gpiozero()
    monkeypatch.setitem(sys.modules, "gpiozero", gpiozero)
    monkeypatch.setitem(sys.modules, "gpiozero.pins", pins)
    # Make importing gpiozero.pins.lgpio fail.
    monkeypatch.setitem(sys.modules, "gpiozero.pins.lgpio", None)

    from yalp.reactive.hardware import GpiozeroMotorDriver

    with pytest.raises(RuntimeError, match="lgpio"):
        GpiozeroMotorDriver()
