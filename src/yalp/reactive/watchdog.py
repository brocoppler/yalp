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
from typing import Optional

from .. import config

logger = logging.getLogger(__name__)


class MotorWatchdog:
    """A daemon-thread dead-man's switch that zeroes motors on a stale heartbeat.

    Parameters
    ----------
    motor_driver:
        Anything with a ``stop()`` method (the
        :class:`~yalp.reactive.hardware.MotorDriver` protocol). ``stop()`` is
        expected to be idempotent/cheap, since it may be called repeatedly.
    timeout_ms:
        How stale (in milliseconds) the heartbeat may get before the watchdog
        trips and stops the motors. Defaults to
        :data:`config.WATCHDOG_TIMEOUT_MS`.

    Usage
    -----
    Call :meth:`start` to spin up the watcher thread, :meth:`heartbeat` at the end
    of every control tick, and :meth:`stop` (which joins the thread) on teardown.
    Both :meth:`start` and :meth:`stop` are idempotent.
    """

    def __init__(
        self,
        motor_driver: object,
        timeout_ms: int = config.WATCHDOG_TIMEOUT_MS,
    ) -> None:
        self._motor_driver = motor_driver
        # Guard against a nonsensical timeout (a zero/negative timeout would trip
        # instantly and continuously); keep it at least 1ms.
        self.timeout_s = max(1e-3, float(timeout_ms) / 1000.0)
        # Poll a few times per timeout window so we react promptly without
        # busy-spinning. ~timeout/3, floored so a tiny timeout still sleeps.
        self._poll_s = max(1e-3, self.timeout_s / 3.0)

        self._lock = threading.Lock()
        self._last_heartbeat = time.monotonic()
        #: True once a stale heartbeat has tripped the watchdog (latched).
        self.tripped = False
        #: Number of distinct trips (latched-edge crossings) for diagnostics.
        self.trip_count = 0

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

    # -- heartbeat -----------------------------------------------------------
    def heartbeat(self) -> None:
        """Record a fresh liveness timestamp (called at the end of every tick)."""
        with self._lock:
            self._last_heartbeat = time.monotonic()

    @property
    def last_heartbeat(self) -> float:
        with self._lock:
            return self._last_heartbeat

    # -- watcher -------------------------------------------------------------
    def _run(self) -> None:
        """Poll the heartbeat; trip (zero motors) when it goes stale."""
        # ``Event.wait`` doubles as the sleep AND the stop signal: it returns
        # early (True) the moment ``stop()`` is called, so teardown is prompt.
        while not self._stop_event.wait(self._poll_s):
            with self._lock:
                stale = (time.monotonic() - self._last_heartbeat) > self.timeout_s
            if stale:
                self._trip()
            elif self.tripped:
                # Heartbeat recovered — re-arm so a future stall trips (and logs)
                # again rather than staying silently latched forever.
                self.tripped = False

    def _trip(self) -> None:
        """Zero the motors on a stale heartbeat; log once per trip edge."""
        try:
            self._motor_driver.stop()
        except Exception:  # pragma: no cover - safety path must not raise
            logger.exception("motor watchdog: motor_driver.stop() raised")
        if not self.tripped:
            # Log once per trip edge, not once per poll, to avoid log spam while
            # the loop stays wedged.
            self.tripped = True
            self.trip_count += 1
            logger.error(
                "motor watchdog TRIPPED: heartbeat stale > %.0fms; motors zeroed",
                self.timeout_s * 1000.0,
            )


__all__ = ["MotorWatchdog"]
