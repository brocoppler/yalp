"""An independent motor watchdog — the last-resort "dead-man's switch".

The reactive tick (``RealReactiveBackend.tick``) is the *first* line of defense:
it stops the wheels on an obstacle and zeroes them on completion. But the tick is
also the thing most likely to wedge — a blocking call, an exception in the run
loop, a hung camera read, a thread that simply stops being scheduled. When the
control loop dies, an open-loop motor command keeps the wheels spinning into
whatever is in front of the robot. For a motorized robot that is unacceptable, so
the safety net **must not depend on the tick's own logic** (hardware.md /
software-spec.md §2.6).

:class:`MotorWatchdog` is that net. It runs on its own daemon thread and watches a
single heartbeat timestamp that the control loop is expected to refresh at the end
of *every* tick. If the heartbeat goes stale — ``now - last_heartbeat`` exceeds
``timeout_ms`` — the watchdog calls ``motor_driver.stop()`` directly, independent
of whatever the (possibly wedged) tick is doing, and latches a ``tripped`` flag.

It is deliberately **driver-agnostic**: it only needs ``stop()``, so it works with
the laptop :class:`~yalp.reactive.hardware.FakeMotorDriver` exactly as it does with
the real GPIO driver, and the whole safety path is unit-testable with no hardware
present (just inject a fake and use a short ``timeout_ms``).
"""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING, Callable, Optional

from .. import config

if TYPE_CHECKING:  # import only for typing — keep the module hardware-free at load
    from .hardware import MotorDriver

logger = logging.getLogger(__name__)


class MotorWatchdog:
    """A daemon-thread dead-man's switch that zeroes motors on a stale heartbeat.

    Parameters
    ----------
    motor_driver:
        A :class:`~yalp.reactive.hardware.MotorDriver` (the watchdog only ever
        calls ``stop()``, which is expected to be idempotent/cheap since it may be
        called repeatedly).
    timeout_ms:
        How stale (in milliseconds) the heartbeat may get before the watchdog
        trips and stops the motors. Defaults to
        :data:`config.WATCHDOG_TIMEOUT_MS`.

    Usage
    -----
    Call :meth:`start` to spin up the watcher thread, :meth:`heartbeat` at the end
    of every control tick, and :meth:`stop` (which joins the thread) on teardown.
    Both :meth:`start` and :meth:`stop` are idempotent.

    Thread-safety
    -------------
    The watcher runs on its own thread while the control loop calls
    :meth:`heartbeat` (and reads :attr:`tripped` / :attr:`trip_count`) from
    another. **All** mutable shared state — the heartbeat timestamp and the
    ``tripped`` / ``trip_count`` latch — is guarded by a single lock so no reader
    ever sees torn state. The lock is only ever held around cheap in-memory work;
    ``motor_driver.stop()`` is called *outside* the lock so a slow driver can
    never block :meth:`heartbeat`.
    """

    def __init__(
        self,
        motor_driver: "MotorDriver",
        timeout_ms: int = config.WATCHDOG_TIMEOUT_MS,
        on_trip: Optional[Callable[[], None]] = None,
    ) -> None:
        self._motor_driver = motor_driver
        #: Optional callback fired ONCE per trip edge (for telemetry). It runs on
        #: the watchdog thread after the motors are zeroed; it must be cheap and
        #: non-blocking (a bounded-queue enqueue) — it is guarded so a raising
        #: callback can never break the safety net. Settable after construction.
        self.on_trip = on_trip
        # Guard against a nonsensical timeout (a zero/negative timeout would trip
        # instantly and continuously); keep it at least 1ms.
        self.timeout_s = max(1e-3, float(timeout_ms) / 1000.0)
        # Poll a few times per timeout window so we react promptly without
        # busy-spinning. ~timeout/3, floored so a tiny timeout still sleeps.
        self._poll_s = max(1e-3, self.timeout_s / 3.0)

        # One lock guards ALL of: _last_heartbeat, _tripped, _trip_count.
        self._lock = threading.Lock()
        self._last_heartbeat = time.monotonic()
        #: True once a stale heartbeat has tripped the watchdog (latched).
        self._tripped = False
        #: Number of distinct trips (latched-edge crossings) for diagnostics.
        self._trip_count = 0

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> "MotorWatchdog":
        """Start the daemon watcher thread (idempotent)."""
        if self._thread is not None and self._thread.is_alive():
            return self
        self._stop_event.clear()
        # Fresh heartbeat so we don't trip on construction-to-start latency.
        self.heartbeat()
        self._thread = threading.Thread(
            target=self._run, name="motor-watchdog", daemon=True
        )
        self._thread.start()
        return self

    def stop(self, timeout: float = 1.0) -> None:
        """Signal the watcher thread to exit, join it, and zero the motors.

        Leaves the motors zeroed: the watchdog never *re-enables* motors, and on
        teardown it issues a final ``motor_driver.stop()`` so the wheels are
        guaranteed stopped even if the watcher thread never ran (the backend's
        own ``stop()`` also zeroes them — both are idempotent). Joining
        guarantees the thread is gone before we return. Idempotent.
        """
        self._stop_event.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
            self._thread = None
        # Final safety stop on teardown: leave the wheels zeroed no matter what.
        try:
            self._motor_driver.stop()
        except Exception:  # pragma: no cover - teardown must not raise
            logger.exception("motor watchdog: motor_driver.stop() raised on teardown")

    # -- shared-state accessors (all reads go through the lock) --------------
    @property
    def tripped(self) -> bool:
        """True while the watchdog is latched after a stale-heartbeat trip."""
        with self._lock:
            return self._tripped

    @property
    def trip_count(self) -> int:
        """Number of distinct trip edges seen (diagnostics)."""
        with self._lock:
            return self._trip_count

    @property
    def last_heartbeat(self) -> float:
        with self._lock:
            return self._last_heartbeat

    # -- heartbeat -----------------------------------------------------------
    def heartbeat(self) -> None:
        """Record a fresh liveness timestamp (called at the end of every tick).

        Deliberately cheap: it only takes the lock around a single monotonic
        timestamp write, so heartbeating from the tick's hot path is negligible.
        """
        with self._lock:
            self._last_heartbeat = time.monotonic()

    # -- watcher -------------------------------------------------------------
    def _run(self) -> None:
        """Poll the heartbeat; trip (zero motors) when it goes stale."""
        # ``Event.wait`` doubles as the sleep AND the stop signal: it returns
        # early (True) the moment ``stop()`` is called, so teardown is prompt.
        while not self._stop_event.wait(self._poll_s):
            with self._lock:
                stale = (time.monotonic() - self._last_heartbeat) > self.timeout_s
                if not stale and self._tripped:
                    # Heartbeat recovered — re-arm (under the same lock as the
                    # staleness read, so the latch flip is atomic) so a future
                    # stall trips (and logs) again rather than staying silently
                    # latched forever.
                    self._tripped = False
            if stale:
                self._trip()

    def _trip(self) -> None:
        """Zero the motors on a stale heartbeat; log once per trip edge.

        ``motor_driver.stop()`` runs OUTSIDE the lock (a slow/blocking driver
        must never stall a concurrent :meth:`heartbeat`); only the tiny latch
        update is taken under the lock, and the log line is emitted once per
        edge, after the lock is released.
        """
        try:
            self._motor_driver.stop()
        except Exception:  # pragma: no cover - safety path must not raise
            logger.exception("motor watchdog: motor_driver.stop() raised")
        with self._lock:
            first_edge = not self._tripped
            if first_edge:
                # Latch and count once per trip edge, not once per poll, so a
                # sustained stall is a single counted/logged event.
                self._tripped = True
                self._trip_count += 1
        if first_edge:
            logger.error(
                "motor watchdog TRIPPED: heartbeat stale > %.0fms; motors zeroed",
                self.timeout_s * 1000.0,
            )
            # Notify the trip observer (telemetry) once per edge, guarded so a bad
            # callback can never break the safety net.
            cb = self.on_trip
            if cb is not None:
                try:
                    cb()
                except Exception:  # pragma: no cover - safety path must not raise
                    logger.exception("motor watchdog: on_trip callback raised")


__all__ = ["MotorWatchdog"]
