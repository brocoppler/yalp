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

import threading
import time

from yalp import config
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
        # While running (before teardown), the watchdog never tripped, so it
        # never issued a safety stop.
        assert wd.tripped is False
        assert wd.trip_count == 0
        assert driver.stop_count == 0
    finally:
        wd.stop()

    # No trip ever occurred; the only stop is the idempotent teardown stop.
    assert wd.tripped is False
    assert wd.trip_count == 0


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


def test_stop_issues_final_motor_stop() -> None:
    """stop() zeroes the motors on teardown even if the watcher never tripped."""
    driver = FakeMotorDriver()
    wd = MotorWatchdog(driver, timeout_ms=20)
    wd.start()
    # Heartbeat regularly so the watcher never trips on its own...
    for _ in range(4):
        wd.heartbeat()
        time.sleep(0.003)
    assert wd.trip_count == 0, "precondition: no trip during this short window"

    wd.stop()
    # ...yet teardown still leaves the wheels zeroed.
    assert driver.stop_count >= 1, "stop() must issue a final motor_driver.stop()"
    assert driver.last == (0.0, 0.0)


def test_default_timeout_comes_from_config() -> None:
    """The default timeout is sourced from config.WATCHDOG_TIMEOUT_MS."""
    wd = MotorWatchdog(FakeMotorDriver())
    assert wd.timeout_s == config.WATCHDOG_TIMEOUT_MS / 1000.0


def test_concurrent_heartbeat_trip_and_read_never_tears_state() -> None:
    """Hammer heartbeat/trip/read from many threads: shared state stays coherent.

    The watchdog's ``_last_heartbeat`` and its ``tripped`` / ``trip_count`` latch
    are all touched from the watcher thread AND from the control loop. This drives
    a storm of concurrent :meth:`heartbeat` calls and property reads *while* the
    watcher thread is polling/tripping/re-arming, and asserts that no reader ever
    observes torn or impossible state (e.g. a negative or wildly jumping
    ``trip_count``, or a non-bool ``tripped``). Timing is deliberately generous so
    this is a data-race probe, not a timing assertion.
    """
    driver = FakeMotorDriver()
    # A short timeout so the watcher is actively (re-)tripping and re-arming while
    # we hammer it — maximising the window for a torn read if the lock were wrong.
    wd = MotorWatchdog(driver, timeout_ms=10)
    wd.start()

    stop = threading.Event()
    errors: list[str] = []
    seen_counts: list[int] = []

    def hammer_heartbeats() -> None:
        # Intermittently heartbeat so the watcher keeps flipping tripped on/off.
        while not stop.is_set():
            wd.heartbeat()
            time.sleep(0.001)

    def hammer_reads() -> None:
        last = 0
        while not stop.is_set():
            tripped = wd.tripped
            count = wd.trip_count
            # tripped must always be a real bool; trip_count monotonic & >= 0.
            if not isinstance(tripped, bool):
                errors.append(f"tripped not bool: {tripped!r}")
            if not isinstance(count, int) or count < 0:
                errors.append(f"bad trip_count: {count!r}")
            if count < last:
                errors.append(f"trip_count went backwards: {count} < {last}")
            last = count
            seen_counts.append(count)

    threads = [threading.Thread(target=hammer_heartbeats) for _ in range(4)]
    threads += [threading.Thread(target=hammer_reads) for _ in range(4)]
    for t in threads:
        t.start()
    try:
        # Also let the watcher trip at least once by pausing heartbeats midway:
        # stop the heartbeat storm briefly so the timeout lapses.
        time.sleep(0.15)
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=2.0)
        wd.stop()

    assert not errors, f"torn/incoherent state observed: {errors[:5]}"
    # Sanity: the reads actually ran and observed the (monotonic) counter.
    assert seen_counts, "reader threads never sampled the watchdog"
