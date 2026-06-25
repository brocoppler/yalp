"""Tests for :class:`GpiozeroUltrasonicSensor` — the safety-critical range read.

``gpiozero``/``lgpio`` are **not** installed on the laptop/CI that runs these
tests, so the suite is split three ways:

1. The module must import with no ``gpiozero`` present (lazy imports).
2. The *pure* logic — the echo-seconds→metres conversion and the poll-rate
   limiter — is factored out of the hardware path so it can be unit-tested with
   no ``gpiozero`` at all. This is where the load-bearing semantics live:
   timeout → ``known=False`` (never decayed to "clear"), and the 60 ms rate cap.
3. End-to-end ``read_distance`` is exercised against a *fake* ``gpiozero``
   package (a fake :class:`DistanceSensor`) injected into ``sys.modules``.

Real electrical behavior (genuine TRIG pulse / ECHO timing on a Pi 5) can only
be confirmed on hardware; here we verify the contract.
"""

from __future__ import annotations

import sys
import types

import pytest


# --------------------------------------------------------------------------- #
# 1. The module must import with NO gpiozero present (lazy imports).
# --------------------------------------------------------------------------- #
def test_module_imports_without_gpiozero():
    assert "gpiozero" not in sys.modules
    import yalp.reactive.hardware as hw  # noqa: F401  (import must not raise)

    assert hasattr(hw, "GpiozeroUltrasonicSensor")
    # Merely importing must not drag in any hardware library.
    assert "gpiozero" not in sys.modules
    assert "lgpio" not in sys.modules


def test_class_satisfies_rangesensor_protocol_structurally():
    # We can't instantiate without gpiozero, but the class must expose the
    # RangeSensor surface (read_distance + close).
    from yalp.reactive.hardware import GpiozeroUltrasonicSensor

    assert callable(getattr(GpiozeroUltrasonicSensor, "read_distance", None))
    assert callable(getattr(GpiozeroUltrasonicSensor, "close", None))


# --------------------------------------------------------------------------- #
# 2a. Pure conversion: echo seconds -> one-way metres (no gpiozero needed).
# --------------------------------------------------------------------------- #
def test_echo_seconds_to_distance_one_millisecond():
    from yalp.reactive.hardware import GpiozeroUltrasonicSensor

    # 1 ms round trip at 343 m/s -> 0.001 * 343 / 2 ≈ 0.1715 m.
    d = GpiozeroUltrasonicSensor._echo_seconds_to_distance(0.001, speed_of_sound=343.0)
    assert d == pytest.approx(0.1715, abs=1e-4)
    assert d == pytest.approx(0.17, abs=0.01)


def test_echo_seconds_to_distance_is_half_round_trip():
    from yalp.reactive.hardware import GpiozeroUltrasonicSensor

    # Doubling echo time doubles distance; it's exactly (t * v) / 2.
    f = GpiozeroUltrasonicSensor._echo_seconds_to_distance
    assert f(0.0, speed_of_sound=343.0) == 0.0
    assert f(0.02, speed_of_sound=343.0) == pytest.approx(0.02 * 343.0 / 2.0)
    assert f(0.04, speed_of_sound=343.0) == pytest.approx(2 * f(0.02, speed_of_sound=343.0))


# --------------------------------------------------------------------------- #
# 2b. Pure rate-limiter: opens at most once per min_interval (no gpiozero).
# --------------------------------------------------------------------------- #
def test_rate_limiter_caps_to_interval():
    from yalp.reactive.hardware import GpiozeroUltrasonicSensor

    lim = GpiozeroUltrasonicSensor._RateLimiter(0.06)  # 60 ms (15 Hz)

    assert lim.allow(100.0) is True       # first call always passes
    assert lim.allow(100.02) is False     # +20 ms — too soon
    assert lim.allow(100.05) is False     # +50 ms — still too soon
    assert lim.allow(100.06) is True      # +60 ms — re-opens exactly at interval
    assert lim.allow(100.07) is False     # and closes again immediately after


# --------------------------------------------------------------------------- #
# Fake gpiozero package with a controllable DistanceSensor.
# --------------------------------------------------------------------------- #
class _FakeDistanceSensor:
    """Mirrors gpiozero.DistanceSensor's API surface we rely on.

    ``.distance`` is a fraction 0..1 of ``max_distance`` (1.0 == no echo in
    range == timeout). Tests set ``fraction`` to drive readings, and
    ``read_count`` records how many times ``.distance`` was actually sampled so
    we can prove the rate cap avoids re-pulsing.
    """

    def __init__(self, echo=None, trigger=None, max_distance=1.0, queue_len=1):
        self.echo = echo
        self.trigger = trigger
        self.max_distance = max_distance
        self.queue_len = queue_len
        self.fraction = 1.0  # default: full scale == no echo == timeout
        self.read_count = 0
        self.closed = False

    @property
    def distance(self):
        self.read_count += 1
        return self.fraction

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
    gpiozero.DistanceSensor = _FakeDistanceSensor

    pins = types.ModuleType("gpiozero.pins")
    lgpio_mod = types.ModuleType("gpiozero.pins.lgpio")
    lgpio_mod.LGPIOFactory = _FakeLGPIOFactory
    pins.lgpio = lgpio_mod
    gpiozero.pins = pins
    return gpiozero, pins, lgpio_mod


@pytest.fixture()
def fake_gpiozero(monkeypatch):
    gpiozero, pins, lgpio_mod = _make_fake_gpiozero()
    monkeypatch.setitem(sys.modules, "gpiozero", gpiozero)
    monkeypatch.setitem(sys.modules, "gpiozero.pins", pins)
    monkeypatch.setitem(sys.modules, "gpiozero.pins.lgpio", lgpio_mod)
    yield gpiozero


@pytest.fixture()
def fake_clock(monkeypatch):
    """A controllable monotonic clock so rate-cap tests are deterministic."""
    import time

    state = {"now": 1000.0}
    monkeypatch.setattr(time, "monotonic", lambda: state["now"])
    return state


# --------------------------------------------------------------------------- #
# 3a. Construction wires the DistanceSensor on the lgpio factory.
# --------------------------------------------------------------------------- #
def test_init_creates_distance_sensor_on_lgpio_factory(fake_gpiozero):
    from yalp.reactive.hardware import GpiozeroUltrasonicSensor

    sensor = GpiozeroUltrasonicSensor(trig_pin=5, echo_pin=6, max_distance_m=4.0)

    from gpiozero.pins.lgpio import LGPIOFactory

    assert isinstance(fake_gpiozero.Device.pin_factory, LGPIOFactory)
    assert sensor._sensor.trigger == 5
    assert sensor._sensor.echo == 6
    assert sensor._sensor.max_distance == 4.0


def test_init_satisfies_rangesensor_protocol(fake_gpiozero):
    from yalp.reactive.hardware import GpiozeroUltrasonicSensor, RangeSensor

    sensor = GpiozeroUltrasonicSensor()
    assert isinstance(sensor, RangeSensor)


# --------------------------------------------------------------------------- #
# 3b. read_distance end-to-end: a real echo returns (distance_m, True).
# --------------------------------------------------------------------------- #
def test_read_distance_returns_known_true_on_real_echo(fake_gpiozero, fake_clock):
    from yalp.reactive.hardware import GpiozeroUltrasonicSensor

    sensor = GpiozeroUltrasonicSensor(max_distance_m=4.0)
    sensor._sensor.fraction = 0.25  # 25% of 4.0 m -> 1.0 m, a real echo

    distance_m, known = sensor.read_distance()
    assert known is True
    assert distance_m == pytest.approx(1.0)


# --------------------------------------------------------------------------- #
# 3c. RATE CAP: two reads inside 60 ms return the same cached value and do NOT
#     re-pulse the sensor; the first read after 60 ms re-samples.
# --------------------------------------------------------------------------- #
def test_two_reads_inside_60ms_serve_cache_without_repulsing(fake_gpiozero, fake_clock):
    from yalp.reactive.hardware import GpiozeroUltrasonicSensor

    sensor = GpiozeroUltrasonicSensor(max_distance_m=4.0, max_poll_hz=15.0)

    sensor._sensor.fraction = 0.25  # 1.0 m
    d1, k1 = sensor.read_distance()
    assert (d1, k1) == (pytest.approx(1.0), True)
    reads_after_first = sensor._sensor.read_count
    assert reads_after_first == 1

    # Move only +30 ms (< 60 ms) and change what the hardware *would* report.
    fake_clock["now"] += 0.030
    sensor._sensor.fraction = 0.5  # would be 2.0 m if it re-pulsed
    d2, k2 = sensor.read_distance()

    # Same cached value, and the sensor was NOT sampled again.
    assert (d2, k2) == (pytest.approx(1.0), True)
    assert sensor._sensor.read_count == reads_after_first  # no re-pulse


def test_first_read_after_60ms_resamples(fake_gpiozero, fake_clock):
    from yalp.reactive.hardware import GpiozeroUltrasonicSensor

    sensor = GpiozeroUltrasonicSensor(max_distance_m=4.0, max_poll_hz=15.0)

    sensor._sensor.fraction = 0.25  # 1.0 m
    sensor.read_distance()
    assert sensor._sensor.read_count == 1

    # Advance past the cap (1/15 Hz ≈ 66.7 ms) and change the reading.
    fake_clock["now"] += 0.070
    sensor._sensor.fraction = 0.5  # 2.0 m
    d, known = sensor.read_distance()

    assert known is True
    assert d == pytest.approx(2.0)
    assert sensor._sensor.read_count == 2  # re-pulsed


# --------------------------------------------------------------------------- #
# 3d. TIMEOUT semantics: a missed echo yields known=False and the distance is
#     NOT decayed to "clear". This is the safety-critical invariant.
# --------------------------------------------------------------------------- #
def test_timeout_yields_known_false_and_does_not_decay_to_clear(fake_gpiozero, fake_clock):
    from yalp.reactive.hardware import GpiozeroUltrasonicSensor

    sensor = GpiozeroUltrasonicSensor(max_distance_m=4.0, max_poll_hz=15.0)

    # First, a real close obstacle.
    sensor._sensor.fraction = 0.05  # 0.2 m -> imminent collision
    d0, k0 = sensor.read_distance()
    assert k0 is True
    assert d0 == pytest.approx(0.2)

    # Now the echo times out: gpiozero pins .distance at full scale (1.0).
    # Advance past the cap (1/15 Hz ≈ 66.7 ms) so this is a fresh real sample.
    fake_clock["now"] += 0.070
    sensor._sensor.fraction = 1.0
    d1, k1 = sensor.read_distance()

    # known must be False so the caller STOPs...
    assert k1 is False
    # ...and we must NOT report a fresh, clear 4.0 m path. The placeholder is the
    # last good reading, never an invented "all clear".
    assert d1 != pytest.approx(4.0)
    assert d1 == pytest.approx(0.2)


def test_never_yet_read_sensor_is_unknown_when_cache_served(fake_gpiozero, fake_clock):
    """Before any real sample, a rate-capped read must report unknown (STOP)."""
    from yalp.reactive.hardware import GpiozeroUltrasonicSensor

    sensor = GpiozeroUltrasonicSensor(max_distance_m=4.0, max_poll_hz=15.0)
    sensor._sensor.fraction = 0.25

    # First read takes a real sample.
    _, k0 = sensor.read_distance()
    assert k0 is True

    # Immediately read again (cache path). Now flip the (hypothetical) state to a
    # never-known sensor by constructing a fresh one and serving the cache.
    fresh = GpiozeroUltrasonicSensor(max_distance_m=4.0, max_poll_hz=15.0)
    # Force the limiter to deny so the cache (initial unknown) is served.
    fresh._limiter._last_pass = 1000.0
    d, known = fresh.read_distance()
    assert known is False  # never decayed to clear
    assert d == pytest.approx(4.0)  # placeholder only; ignored because unknown


# --------------------------------------------------------------------------- #
# 3e. close() releases the device and is idempotent.
# --------------------------------------------------------------------------- #
def test_close_releases_device_and_is_idempotent(fake_gpiozero, fake_clock):
    from yalp.reactive.hardware import GpiozeroUltrasonicSensor

    sensor = GpiozeroUltrasonicSensor()
    underlying = sensor._sensor

    sensor.close()
    assert underlying.closed is True

    # Idempotent: a second close does not raise.
    sensor.close()

    # After close, read_distance is safe and reports unknown (STOP).
    d, known = sensor.read_distance()
    assert known is False
