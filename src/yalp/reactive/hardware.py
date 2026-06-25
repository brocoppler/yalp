"""Narrow GPIO interfaces for the reactive layer (laptop-first, hardware-free).

Wave 3 puts **all** GPIO access behind two small protocols so the rest of the
reactive layer depends on an *interface*, never on a hardware library. The real
implementation (``real_backend.py`` on the Pi) constructs concrete drivers that
talk to ``gpiozero``/``lgpio``; laptop tests inject the fakes defined here —
exactly mirroring how ``FakeReactiveBackend`` injects a fake tracker.

**This module imports NOTHING hardware-specific.** It is pure stdlib + ``typing``
so it can be imported on a Mac (or in CI) with no ``gpiozero``/``lgpio``/``RPi``
present. Concrete, hardware-touching drivers live elsewhere and import their
libraries lazily.

Two interfaces:

* :class:`MotorDriver` — drive two wheel channels with signed throttles.
* :class:`RangeSensor` — read a forward distance, biased to STOP when unknown.

Plus two laptop fakes (:class:`FakeMotorDriver`, :class:`FakeRangeSensor`) for
deterministic, hardware-free tests.
"""

from __future__ import annotations

from typing import List, Optional, Tuple, runtime_checkable, Protocol


@runtime_checkable
class MotorDriver(Protocol):
    """A two-channel motor driver (left/right wheels).

    Throttles are **signed** floats in ``[-1.0, 1.0]``: the sign is the
    direction (forward/reverse) and the magnitude is the PWM duty cycle. The
    real implementation clamps out-of-range values and maps sign→direction-pin /
    magnitude→PWM duty; callers should not assume any behavior beyond clamping.
    """

    def set_motors(self, left: float, right: float) -> None:
        """Command the left and right wheel throttles (each in ``[-1.0, 1.0]``)."""
        ...

    def stop(self) -> None:
        """Bring both channels to zero (coast/brake). Safe to call repeatedly."""
        ...

    def close(self) -> None:
        """Release the underlying GPIO. Idempotent; the object is unusable after."""
        ...


@runtime_checkable
class RangeSensor(Protocol):
    """A forward-looking range sensor (e.g. an HC-SR04 ultrasonic).

    ``read_distance`` returns ``(distance_m, known)``:

    * ``known is True``  → ``distance_m`` is a fresh, trustworthy reading.
    * ``known is False`` → the echo timed out or the sensor is unavailable;
      ``distance_m`` is meaningless and the caller must **bias to STOP** (treat
      an unknown reading as an obstacle).

    ``read_distance`` must be **non-blocking-ish**: it may briefly wait for an
    echo but must bound that wait (echo timeout) and never block indefinitely.
    The caller (``real_backend``) is responsible for rate-capping how often it
    polls — this method does not throttle itself.
    """

    def read_distance(self) -> Tuple[float, bool]:
        """Return ``(distance_m, known)``; ``known=False`` means STOP."""
        ...

    def close(self) -> None:
        """Release the underlying GPIO. Idempotent."""
        ...


class FakeMotorDriver:
    """A pure-software :class:`MotorDriver` for laptop tests.

    Records the most recent ``(left, right)`` command in :attr:`last` and the
    full ordered history in :attr:`calls`, so tests can assert on what was
    commanded. ``stop()`` records ``(0.0, 0.0)``; ``close()`` flips
    :attr:`closed`.
    """

    def __init__(self) -> None:
        #: The most recent ``(left, right)`` command, or ``None`` before any.
        self.last: Optional[Tuple[float, float]] = None
        #: Ordered history of every ``(left, right)`` ever commanded.
        self.calls: List[Tuple[float, float]] = []
        #: True once :meth:`close` has been called.
        self.closed = False
        #: Number of times :meth:`stop` was called.
        self.stop_count = 0

    def set_motors(self, left: float, right: float) -> None:
        cmd = (float(left), float(right))
        self.last = cmd
        self.calls.append(cmd)

    def stop(self) -> None:
        self.stop_count += 1
        self.set_motors(0.0, 0.0)

    def close(self) -> None:
        self.closed = True


class FakeRangeSensor:
    """A pure-software :class:`RangeSensor` for laptop tests.

    Holds a settable ``(distance_m, known)`` reading returned from every
    :meth:`read_distance` call. Defaults to a clear path (4.0 m, known).
    """

    def __init__(self, distance_m: float = 4.0, known: bool = True) -> None:
        self.distance_m = float(distance_m)
        self.known = bool(known)
        self.closed = False
        #: Number of times :meth:`read_distance` was called.
        self.read_count = 0

    def set_distance(self, d: float, known: bool = True) -> None:
        """Set the reading returned by :meth:`read_distance`."""
        self.distance_m = float(d)
        self.known = bool(known)

    def trigger_obstacle(self, d: float = 0.10) -> None:
        """Simulate a close obstacle (a known, small distance)."""
        self.set_distance(d, known=True)

    def timeout(self) -> None:
        """Simulate an echo timeout / unavailable sensor (``known=False``)."""
        self.set_distance(self.distance_m, known=False)

    def read_distance(self) -> Tuple[float, bool]:
        self.read_count += 1
        return (self.distance_m, self.known)

    def close(self) -> None:
        self.closed = True


__all__ = [
    "MotorDriver",
    "RangeSensor",
    "FakeMotorDriver",
    "FakeRangeSensor",
]
