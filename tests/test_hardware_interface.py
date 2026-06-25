"""Tests for the narrow GPIO interfaces and their laptop fakes.

These run on a laptop / CI with **no** hardware libraries installed: the
``hardware`` module must import cleanly with no ``gpiozero``/``lgpio``/``RPi``
present, and the fakes must satisfy the runtime-checkable protocols.
"""

from __future__ import annotations

import sys

from yalp.reactive.hardware import (
    FakeMotorDriver,
    FakeRangeSensor,
    MotorDriver,
    RangeSensor,
)


def test_imports_with_no_hardware_libs_present():
    """The module must import without any GPIO library loaded."""
    for mod in ("gpiozero", "lgpio", "RPi", "RPi.GPIO", "pigpio"):
        assert mod not in sys.modules, f"{mod} should not be imported"


def test_fakes_satisfy_protocols():
    assert isinstance(FakeMotorDriver(), MotorDriver)
    assert isinstance(FakeRangeSensor(), RangeSensor)


def test_motor_driver_records_calls():
    m = FakeMotorDriver()
    assert m.last is None
    assert m.calls == []

    m.set_motors(0.5, -0.25)
    assert m.last == (0.5, -0.25)
    assert m.calls == [(0.5, -0.25)]

    m.set_motors(-1.0, 1.0)
    assert m.last == (-1.0, 1.0)
    assert m.calls == [(0.5, -0.25), (-1.0, 1.0)]


def test_motor_driver_stop_records_zero():
    m = FakeMotorDriver()
    m.set_motors(0.8, 0.8)
    m.stop()
    assert m.last == (0.0, 0.0)
    assert m.calls[-1] == (0.0, 0.0)
    assert m.stop_count == 1


def test_motor_driver_close():
    m = FakeMotorDriver()
    assert m.closed is False
    m.close()
    assert m.closed is True


def test_range_sensor_default_clear():
    s = FakeRangeSensor()
    dist, known = s.read_distance()
    assert known is True
    assert dist == 4.0
    assert s.read_count == 1


def test_range_sensor_set_distance():
    s = FakeRangeSensor()
    s.set_distance(1.5)
    assert s.read_distance() == (1.5, True)
    s.set_distance(0.3, known=False)
    assert s.read_distance() == (0.3, False)


def test_range_sensor_trigger_obstacle():
    s = FakeRangeSensor()
    s.trigger_obstacle()
    dist, known = s.read_distance()
    assert known is True
    assert dist == 0.10


def test_range_sensor_timeout_means_unknown():
    s = FakeRangeSensor(distance_m=2.0)
    s.timeout()
    _dist, known = s.read_distance()
    assert known is False


def test_range_sensor_close():
    s = FakeRangeSensor()
    assert s.closed is False
    s.close()
    assert s.closed is True
