"""Unit tests for :class:`yalp.reactive.watchdog.MotorWatchdog`.

These run entirely on a laptop / in CI with **no GPIO hardware**: the watchdog is
driver-agnostic (it only needs ``stop()``), so we drive it with the laptop
:class:`~yalp.reactive.hardware.FakeMotorDriver` and short timeouts. The watchdog
is the *independent* safety net (hardware.md / software-spec.md §2.6) — it zeroes
the motors when the control loop stops heartbeating, regardless of the tick's own
logic.

Timing tolerances are deliberately generous (sleep well past the timeout, poll
for the trip) so the tests are not flaky under CI scheduling jitter.
"""

from __future__ import annotations

import time

from yalp.reactive.hardware import FakeMotorDriver
from yalp.reactive.watchdog import MotorWatchdog


def _wait_until(predicate, timeout_s: float = 2.0, poll_s: float = 0.005) -> bool:
    """Poll ``predicate`` until true or ``timeout_s`` elapses; return its truth."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(poll_s)
    return predicate()


def test_regular_heartbeats_never_trip() -> None:
    """With heartbeats well within the timeout, the watchdog never stops motors."""
    driver = FakeMotorDriver()
    wd = MotorWatchdog(driver, timeout_ms=20)
    wd.start()
    try:
        # Heartbeat every ~5ms (< 20ms timeout) for a good while — many poll
        # cycles (~timeout/3 ≈ 6.6ms) pass without ever going stale.
        for _ in range(40):
            wd.heartbeat()
            time.sleep(0.005)
    finally:
        wd.stop()

    assert wd.tripped is False
    assert wd.trip_count == 0
    assert driver.stop_count == 0


def test_withholding_heartbeats_trips_and_stops_motors() -> None:
    """Letting the heartbeat go stale trips the watchdog and zeroes the motors."""
    driver = FakeMotorDriver()
    wd = MotorWatchdog(driver, timeout_ms=20)
    # Simulate the loop running normally, then wedging.
    wd.heartbeat()
    wd.start()
    try:
        # Withhold heartbeats for far longer than the timeout.
        tripped = _wait_until(lambda: wd.tripped, timeout_s=2.0)
        assert tripped, "watchdog should trip when the heartbeat goes stale"
        assert driver.stop_count >= 1, "a trip must call motor_driver.stop()"
        assert driver.last == (0.0, 0.0), "motors must be zeroed on a trip"
        assert wd.trip_count >= 1
    finally:
        wd.stop()


def test_trip_logs_once_per_edge_not_per_poll() -> None:
    """A single sustained stall is one trip edge (logged once), not one per poll."""
    driver = FakeMotorDriver()
    wd = MotorWatchdog(driver, timeout_ms=20)
    wd.start()
    try:
        assert _wait_until(lambda: wd.tripped, timeout_s=2.0)
        # Stay wedged for many more poll cycles; trip_count stays at 1 even though
        # stop() keeps being (idempotently) called.
        time.sleep(0.2)
        assert wd.trip_count == 1, "the trip edge should be counted exactly once"
    finally:
        wd.stop()
    # stop() is idempotent on the driver: many calls, but each zeroes the motors.
    assert driver.stop_count >= 1
    assert driver.last == (0.0, 0.0)


def test_recovered_heartbeat_re_arms_then_trips_again() -> None:
    """A recovered heartbeat re-arms the watchdog so a later stall trips again."""
    driver = FakeMotorDriver()
    wd = MotorWatchdog(driver, timeout_ms=20)
    wd.start()
    try:
        assert _wait_until(lambda: wd.tripped, timeout_s=2.0)
        assert wd.trip_count == 1

        # Resume heartbeating: the watchdog re-arms (tripped clears).
        re_armed = _wait_until(
            lambda: (wd.heartbeat() or True) and not wd.tripped,
            timeout_s=1.0,
        )
        assert re_armed, "a fresh heartbeat should clear the tripped latch"

        # Stop heartbeating again -> a brand-new trip edge.
        assert _wait_until(lambda: wd.tripped, timeout_s=2.0)
        assert wd.trip_count == 2
    finally:
        wd.stop()


def test_stop_cleanly_joins_thread() -> None:
    """stop() joins the daemon thread; the watcher is gone afterwards."""
    driver = FakeMotorDriver()
    wd = MotorWatchdog(driver, timeout_ms=20)
    wd.start()
    thread = wd._thread
    assert thread is not None and thread.is_alive()

    wd.stop()
    assert wd._thread is None
    assert not thread.is_alive(), "stop() must join the watchdog thread"

    # stop() is idempotent — a second call is a no-op and does not raise.
    wd.stop()


def test_start_is_idempotent() -> None:
    """Calling start() twice does not spawn a second thread."""
    driver = FakeMotorDriver()
    wd = MotorWatchdog(driver, timeout_ms=20)
    wd.start()
    first = wd._thread
    wd.start()
    try:
        assert wd._thread is first, "start() must not replace a live thread"
    finally:
        wd.stop()
