"""Tests for :class:`GpiozeroMotorDriver` — the real Pi motor driver.

``gpiozero``/``lgpio`` are **not** installed on the laptop/CI that runs these
tests, so we exercise the driver against a *fake* ``gpiozero`` package injected
into ``sys.modules``. The fake records every device created and every duty/
direction written, letting us assert pin state without any hardware.

**Assertions are written against the DRV8833's IN/IN truth table, not the
driver's own convention.** This is the lesson of the 2026-07-06 field bug: the
old suite asserted the code's phase/enable convention against the fake and so
happily passed while the real chip drove an "idle" channel at full reverse. With
PWM pin → xIN1 and DIR pin → xIN2, forward is xIN2 LOW + duty; reverse is xIN2
HIGH + duty ``1 - abs(throttle)`` (slow decay); zero is xIN2 LOW + duty 0 (coast).

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
# 3. set_motors: DRV8833 IN/IN truth table (PWM->xIN1, DIR->xIN2).
#    forward:  xIN2 LOW,  duty = throttle           (fast decay)
#    reverse:  xIN2 HIGH, duty = 1 - abs(throttle)  (slow decay)
#    zero:     xIN2 LOW,  duty = 0                   (true coast)
# --------------------------------------------------------------------------- #
def test_set_motors_forward(fake_gpiozero):
    from yalp.reactive.hardware import GpiozeroMotorDriver

    drv = GpiozeroMotorDriver(driver_kind="drv8833")
    drv.set_motors(0.6, 0.75)

    # Forward = xIN2 (dir pin) LOW, duty = throttle.
    assert drv._left_dir.value == 0
    assert drv._right_dir.value == 0
    assert drv._left_pwm.value == pytest.approx(0.6)
    assert drv._right_pwm.value == pytest.approx(0.75)


def test_set_motors_reverse(fake_gpiozero):
    from yalp.reactive.hardware import GpiozeroMotorDriver

    drv = GpiozeroMotorDriver(driver_kind="drv8833")
    drv.set_motors(-0.6, -0.9)

    # Reverse = xIN2 (dir pin) HIGH, slow-decay duty = 1 - abs(throttle).
    assert drv._left_dir.value == 1
    assert drv._right_dir.value == 1
    assert drv._left_pwm.value == pytest.approx(0.4)  # 1 - 0.6
    assert drv._right_pwm.value == pytest.approx(0.1)  # 1 - 0.9


def test_set_motors_zero_is_coast(fake_gpiozero):
    from yalp.reactive.hardware import GpiozeroMotorDriver

    drv = GpiozeroMotorDriver(driver_kind="drv8833")
    drv.set_motors(0.0, 0.0)

    # Zero throttle = true coast: xIN2 LOW + duty 0 (NOT dir HIGH + duty 0, which
    # on a DRV8833 is IN1=0/IN2=1 = full-speed reverse — the 2026-07-06 bug).
    assert drv._left_dir.value == 0
    assert drv._right_dir.value == 0
    assert drv._left_pwm.value == 0.0
    assert drv._right_pwm.value == 0.0


# --------------------------------------------------------------------------- #
# 4. Clamping.
# --------------------------------------------------------------------------- #
def test_set_motors_clamps_out_of_range(fake_gpiozero):
    from yalp.reactive.hardware import GpiozeroMotorDriver

    drv = GpiozeroMotorDriver(driver_kind="drv8833")
    drv.set_motors(5.0, -5.0)

    # Left clamps to +1.0 (full forward): xIN2 LOW, duty 1.0.
    assert drv._left_dir.value == 0
    assert drv._left_pwm.value == pytest.approx(1.0)
    # Right clamps to -1.0 (full reverse): xIN2 HIGH, slow-decay duty 1 - 1 = 0.0
    # (IN1=0/IN2=1 = full-speed reverse per the truth table).
    assert drv._right_dir.value == 1
    assert drv._right_pwm.value == pytest.approx(0.0)


# --------------------------------------------------------------------------- #
# 5. Per-channel invert (sign flips BEFORE the IN/IN mapping).
# --------------------------------------------------------------------------- #
def test_set_motors_left_invert(fake_gpiozero):
    from yalp.reactive.hardware import GpiozeroMotorDriver

    drv = GpiozeroMotorDriver(driver_kind="drv8833", left_invert=True, right_invert=False)
    drv.set_motors(0.6, 0.6)

    # Left inverted: +0.6 -> -0.6 -> reverse: xIN2 HIGH, duty 1 - 0.6 = 0.4.
    assert drv._left_dir.value == 1
    assert drv._left_pwm.value == pytest.approx(0.4)
    # Right not inverted: forward: xIN2 LOW, duty 0.6.
    assert drv._right_dir.value == 0
    assert drv._right_pwm.value == pytest.approx(0.6)


def test_set_motors_right_invert_reverse_command(fake_gpiozero):
    from yalp.reactive.hardware import GpiozeroMotorDriver

    drv = GpiozeroMotorDriver(driver_kind="drv8833", right_invert=True)
    drv.set_motors(0.0, -0.3)

    # Right inverted: -0.3 -> +0.3 -> forward: xIN2 LOW, duty 0.3.
    assert drv._right_dir.value == 0
    assert drv._right_pwm.value == pytest.approx(0.3)
    # Left commanded 0 stays coast: xIN2 LOW, duty 0.
    assert drv._left_dir.value == 0
    assert drv._left_pwm.value == 0.0


# --------------------------------------------------------------------------- #
# 5b. Regression tests named for the 2026-07-06 field bug (PH/EN vs IN/IN).
# --------------------------------------------------------------------------- #
def test_idle_channel_never_energized(fake_gpiozero):
    """An idle (zero) channel must coast, not run at full reverse.

    The old PH/EN code produced dir HIGH + duty 0 for a zero throttle, which on a
    DRV8833 is IN1=0/IN2=1 = full-speed reverse. Driving one wheel while the other
    is commanded 0 must leave the idle wheel at xIN2 LOW + duty 0.
    """
    from yalp.reactive.hardware import GpiozeroMotorDriver

    drv = GpiozeroMotorDriver(driver_kind="drv8833")
    drv.set_motors(0.0, 0.6)

    # Left (idle) channel: true coast, NOT full reverse.
    assert drv._left_dir.value == 0
    assert drv._left_pwm.value == 0.0
    # Right (driven) channel: forward.
    assert drv._right_dir.value == 0
    assert drv._right_pwm.value == pytest.approx(0.6)


def test_full_forward_is_full_duty(fake_gpiozero):
    """Commanded full forward is full duty, not BRAKE.

    The old code drove dir HIGH + duty 1.0 for a +1.0 command, which on a DRV8833
    is IN1=1/IN2=1 = brake. Correct IN/IN forward is xIN2 LOW + duty 1.0.
    """
    from yalp.reactive.hardware import GpiozeroMotorDriver

    drv = GpiozeroMotorDriver(driver_kind="drv8833")
    drv.set_motors(1.0, 1.0)

    assert drv._left_dir.value == 0
    assert drv._right_dir.value == 0
    assert drv._left_pwm.value == pytest.approx(1.0)
    assert drv._right_pwm.value == pytest.approx(1.0)


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
# 7. stop() coasts: zero BOTH PWM duties AND drop BOTH direction pins LOW.
# --------------------------------------------------------------------------- #
def test_stop_after_forward_is_coast(fake_gpiozero):
    """stop() after a forward command must land in true coast, not full reverse.

    On the DRV8833, zeroing only the PWM duty while the dir pin is latched HIGH is
    IN1=0/IN2=1 = full-speed reverse. stop() (the dead-man's switch path) must
    zero both duties AND drop both dir pins LOW.
    """
    from yalp.reactive.hardware import GpiozeroMotorDriver

    drv = GpiozeroMotorDriver(driver_kind="drv8833")
    drv.set_motors(0.7, 0.7)

    drv.stop()

    assert drv._left_dir.value == 0
    assert drv._right_dir.value == 0
    assert drv._left_pwm.value == 0.0
    assert drv._right_pwm.value == 0.0


def test_stop_after_reverse_is_coast(fake_gpiozero):
    """stop() after a reverse command (dir pins already HIGH) also coasts."""
    from yalp.reactive.hardware import GpiozeroMotorDriver

    drv = GpiozeroMotorDriver(driver_kind="drv8833")
    drv.set_motors(-0.8, -0.8)
    assert drv._left_dir.value == 1  # reverse latched the dir pins HIGH

    drv.stop()

    assert drv._left_dir.value == 0
    assert drv._right_dir.value == 0
    assert drv._left_pwm.value == 0.0
    assert drv._right_pwm.value == 0.0

    # Idempotent: a second stop() must not raise and stays in coast.
    drv.stop()
    assert drv._left_dir.value == 0
    assert drv._left_pwm.value == 0.0


def test_watchdog_stop_is_safe(fake_gpiozero):
    """A MotorWatchdog trip calls driver.stop(); that must leave the DRV8833 coasting.

    Focused integration: drive forward, then run the real safety-stop callback the
    watchdog invokes on a trip (``driver.stop``) and assert both channels are at
    dir LOW + duty 0 — the dead-man's switch must never command full reverse.
    """
    from yalp.reactive.hardware import GpiozeroMotorDriver
    from yalp.reactive.watchdog import MotorWatchdog

    drv = GpiozeroMotorDriver(driver_kind="drv8833")
    drv.set_motors(0.9, 0.9)

    # Build a watchdog around the real driver and invoke the exact stop path a
    # trip uses (``_trip`` calls ``motor_driver.stop()``), without depending on
    # wall-clock timing.
    wd = MotorWatchdog(drv, timeout_ms=10)
    wd._trip()  # what the watchdog thread runs when the heartbeat goes stale

    assert wd.trip_count == 1

    assert drv._left_dir.value == 0
    assert drv._right_dir.value == 0
    assert drv._left_pwm.value == 0.0
    assert drv._right_pwm.value == 0.0


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
