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

The concrete, hardware-touching drivers that live here —
:class:`GpiozeroMotorDriver` and :class:`GpiozeroUltrasonicSensor` — import
``gpiozero``/``lgpio`` *lazily* (inside ``__init__``/methods), so this module
still imports cleanly on a Mac with no hardware libraries present. Instantiating
those classes is what requires the libraries; merely importing them does not.
"""

from __future__ import annotations

from typing import Any, List, Optional, Tuple, runtime_checkable, Protocol

from yalp import config


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


_PIN_FACTORY_DOCS = "docs/technical/hardware.md"


class GpiozeroMotorDriver:
    """A real :class:`MotorDriver` backed by ``gpiozero`` on a Raspberry Pi 5.

    **Phase/enable control, not 4-PWM.** The Pi 5 exposes only two hardware-PWM
    lines (GPIO12 = PWM0, GPIO13 = PWM1), so each wheel channel is driven by one
    PWM *speed* pin and one plain *direction* pin (rather than two PWM pins). This
    matches both the TB6612FNG and DRV8833 dual H-bridges in PH/EN mode:

    * speed pin → :class:`gpiozero.PWMOutputDevice` (duty = ``abs(throttle)``),
    * direction pin → :class:`gpiozero.DigitalOutputDevice` (HIGH = forward).

    **Pi 5 needs the lgpio pin factory.** ``RPi.GPIO`` silently no-ops on the Pi
    5, so this driver forces ``gpiozero``'s pin factory to
    :class:`gpiozero.pins.lgpio.LGPIOFactory` and refuses to run if ``RPi.GPIO``
    is the active factory. If ``lgpio`` cannot be loaded we fail loudly with a
    :class:`RuntimeError` pointing at :data:`{docs}`.

    **Lazy imports.** ``gpiozero``/``lgpio`` are imported *inside* ``__init__`` so
    this module imports fine on a laptop with no hardware libraries; only
    *instantiating* this class needs the libraries.
    """.format(docs=_PIN_FACTORY_DOCS)

    def __init__(
        self,
        *,
        left_pwm_pin: int = config.MOTOR_LEFT_PWM_PIN,
        left_dir_pin: int = config.MOTOR_LEFT_DIR_PIN,
        right_pwm_pin: int = config.MOTOR_RIGHT_PWM_PIN,
        right_dir_pin: int = config.MOTOR_RIGHT_DIR_PIN,
        stby_pin: Optional[int] = config.MOTOR_STBY_PIN,
        driver_kind: str = config.MOTOR_DRIVER_KIND,
        pwm_frequency: int = config.MOTOR_PWM_FREQUENCY_HZ,
        left_invert: bool = config.MOTOR_LEFT_INVERT,
        right_invert: bool = config.MOTOR_RIGHT_INVERT,
        left_trim: float = 1.0,
        right_trim: float = 1.0,
    ) -> None:
        # --- Lazy hardware imports (keep the module laptop-importable) --------
        try:
            import gpiozero  # noqa: F401  (Device + output devices)
            from gpiozero import DigitalOutputDevice, PWMOutputDevice
        except Exception as exc:  # pragma: no cover - needs a real Pi env
            raise RuntimeError(
                "gpiozero is required to drive the motors on the Pi but could "
                f"not be imported ({exc!r}). Install the 'pi' extra on the "
                f"Raspberry Pi and see {_PIN_FACTORY_DOCS}."
            ) from exc

        self._set_lgpio_pin_factory(gpiozero)

        self._left_invert = bool(left_invert)
        self._right_invert = bool(right_invert)
        # Per-wheel magnitude scaling (drift fix). 1.0 = no-op so the default
        # driver is byte-for-byte identical to the pre-calibration behaviour.
        self._left_trim = float(left_trim)
        self._right_trim = float(right_trim)
        self._driver_kind = str(driver_kind).strip().lower()
        self._closed = False

        # Speed pins: hardware PWM (GPIO12/13). Direction pins: plain GPIO.
        self._left_pwm = PWMOutputDevice(left_pwm_pin, frequency=pwm_frequency)
        self._right_pwm = PWMOutputDevice(right_pwm_pin, frequency=pwm_frequency)
        self._left_dir = DigitalOutputDevice(left_dir_pin)
        self._right_dir = DigitalOutputDevice(right_dir_pin)

        # STBY/nSLEEP: only the TB6612FNG has a software STBY we must drive HIGH
        # to enable the outputs. The DRV8833 ties nSLEEP HIGH in hardware, so we
        # ignore stby_pin for it.
        self._stby: Optional[Any] = None
        if self._driver_kind == "tb6612fng" and stby_pin is not None:
            self._stby = DigitalOutputDevice(stby_pin)
            self._stby.on()  # enable the driver outputs

        # Keep every device for a clean close().
        self._devices: List[Any] = [
            self._left_pwm,
            self._right_pwm,
            self._left_dir,
            self._right_dir,
        ]
        if self._stby is not None:
            self._devices.append(self._stby)

    @staticmethod
    def _set_lgpio_pin_factory(gpiozero: Any) -> None:
        """Force gpiozero onto the lgpio pin factory (required on the Pi 5).

        ``RPi.GPIO`` silently fails to drive pins on the Pi 5, so we (a) refuse
        to run if it is already the active factory and (b) install
        :class:`LGPIOFactory`, failing loudly if ``lgpio`` is unavailable.
        """
        existing = getattr(gpiozero.Device, "pin_factory", None)
        if existing is not None:
            factory_name = type(existing).__name__
            if "RPiGPIO" in factory_name or "RPIGPIO" in factory_name.upper():
                raise RuntimeError(
                    "gpiozero is using the RPi.GPIO pin factory, which silently "
                    "fails on the Raspberry Pi 5. Force the lgpio factory "
                    "(GPIOZERO_PIN_FACTORY=lgpio) before driving the motors — "
                    f"see {_PIN_FACTORY_DOCS}."
                )

        try:
            from gpiozero.pins.lgpio import LGPIOFactory
        except Exception as exc:
            raise RuntimeError(
                "The lgpio pin factory is required on the Raspberry Pi 5 "
                "(RPi.GPIO silently fails there) but could not be loaded "
                f"({exc!r}). Install lgpio (the 'pi' extra) and see "
                f"{_PIN_FACTORY_DOCS}."
            ) from exc

        gpiozero.Device.pin_factory = LGPIOFactory()

    @staticmethod
    def _clamp(value: float) -> float:
        """Clamp a throttle to ``[-1.0, 1.0]``."""
        if value < -1.0:
            return -1.0
        if value > 1.0:
            return 1.0
        return float(value)

    def _drive_channel(
        self,
        pwm: Any,
        dir_dev: Any,
        throttle: float,
        invert: bool,
        trim: float = 1.0,
    ) -> None:
        # Apply per-wheel trim scaling BEFORE clamping, so the trimmed magnitude
        # still lands in [-1, 1] (trim defaults to 1.0 = no-op).
        throttle = self._clamp(throttle * trim)
        if invert:
            throttle = -throttle
        # Direction pin: HIGH for forward (>= 0), LOW for reverse.
        if throttle >= 0:
            dir_dev.on()
        else:
            dir_dev.off()
        # PWM duty cycle is the magnitude.
        pwm.value = abs(throttle)

    def set_motors(self, left: float, right: float) -> None:
        """Command signed throttles in ``[-1.0, 1.0]`` (sign = direction).

        Each wheel's calibration is applied here: the commanded value is scaled
        by the per-wheel ``*_trim`` (default ``1.0`` = no-op) and its sign flipped
        if that wheel's ``*_invert`` is set (default ``False`` = no-op), so an
        un-calibrated driver behaves exactly as before.
        """
        self._drive_channel(
            self._left_pwm, self._left_dir, left, self._left_invert, self._left_trim
        )
        self._drive_channel(
            self._right_pwm, self._right_dir, right, self._right_invert, self._right_trim
        )

    def stop(self) -> None:
        """Coast both channels by zeroing the PWM duty (direction unchanged)."""
        self._left_pwm.value = 0.0
        self._right_pwm.value = 0.0

    def close(self) -> None:
        """Zero PWM, close every gpiozero device, and release the pin factory."""
        if self._closed:
            return
        self._closed = True
        # Zero the speed pins first so the motors stop before pins are released.
        for pwm in (self._left_pwm, self._right_pwm):
            try:
                pwm.value = 0.0
            except Exception:  # pragma: no cover - best effort during teardown
                pass
        for dev in self._devices:
            try:
                dev.close()
            except Exception:  # pragma: no cover - best effort during teardown
                pass
        # Best-effort: tear down the lgpio pin factory we installed.
        try:
            import gpiozero

            factory = getattr(gpiozero.Device, "pin_factory", None)
            if factory is not None:
                factory.close()
                gpiozero.Device.pin_factory = None
        except Exception:  # pragma: no cover - best effort during teardown
            pass


class GpiozeroUltrasonicSensor:
    """A real, safety-critical :class:`RangeSensor` backed by ``gpiozero``.

    Drives an HC-SR04-style ultrasonic on a Raspberry Pi 5 to feed the reactive
    layer's collision-stop. Because a missed echo is what stands between the
    robot and a wall, this class is deliberately paranoid:

    * **Timeout means UNKNOWN, never CLEAR.** If no echo returns within
      ``echo_timeout_s`` the reading is reported as ``known=False`` so the caller
      biases to STOP. We **never** decay a missed echo into a fresh "the path is
      clear" reading — the returned distance on a miss is only a best-effort
      *placeholder* (the last good value, or ``max_distance_m``) and must be
      ignored because ``known`` is ``False``.
    * **Poll rate cap.** The HC-SR04 needs ≳60 ms between pings (echoes from a
      previous ping must die down). :meth:`read_distance` enforces this with a
      monotonic clock: if called sooner than ``1 / max_poll_hz`` since the last
      *real* sample it returns the cached reading **without re-pulsing** the
      sensor, rather than firing a ping that would read garbage.

    **Prefer :class:`gpiozero.DistanceSensor`.** It owns the TRIG pulse / ECHO
    timing and exposes ``.distance`` as a fraction ``0..1`` of ``max_distance``;
    a value at (or above) the ceiling with no real echo is treated as a timeout.
    The pure unit-conversion (:meth:`_echo_seconds_to_distance`) and the
    rate-limiter (:meth:`_RateLimiter`) are factored out so they can be tested
    with no ``gpiozero`` present.

    **Pi 5 needs the lgpio pin factory** (``RPi.GPIO`` silently no-ops there), so
    we force :class:`gpiozero.pins.lgpio.LGPIOFactory`, exactly like
    :class:`GpiozeroMotorDriver`.

    **Lazy imports** keep this module laptop-importable; only *instantiating*
    this class needs ``gpiozero``/``lgpio``.
    """

    class _RateLimiter:
        """Gate that opens at most once per ``min_interval_s`` (monotonic).

        Pure, hardware-free, and unit-testable: ``allow(now)`` returns ``True``
        the first time and again only once ``min_interval_s`` has elapsed since
        the last time it returned ``True``. Used to decide whether
        :meth:`read_distance` may re-pulse the sensor or must serve the cache.
        """

        def __init__(self, min_interval_s: float) -> None:
            self.min_interval_s = float(min_interval_s)
            self._last_pass: Optional[float] = None

        def allow(self, now: float) -> bool:
            if self._last_pass is None or (now - self._last_pass) >= self.min_interval_s:
                self._last_pass = now
                return True
            return False

    def __init__(
        self,
        *,
        trig_pin: int = config.ULTRASONIC_TRIG_PIN,
        echo_pin: int = config.ULTRASONIC_ECHO_PIN,
        max_distance_m: float = config.ULTRASONIC_MAX_DISTANCE_M,
        echo_timeout_s: float = config.ULTRASONIC_ECHO_TIMEOUT_S,
        max_poll_hz: float = config.ULTRASONIC_MAX_POLL_HZ,
        speed_of_sound: float = config.SPEED_OF_SOUND_MPS,
    ) -> None:
        self._max_distance_m = float(max_distance_m)
        self._echo_timeout_s = float(echo_timeout_s)
        self._speed_of_sound = float(speed_of_sound)
        self._closed = False

        # Rate-cap: >= 1 / max_poll_hz between real samples (>= ~60 ms at 15 Hz).
        min_interval_s = (1.0 / max_poll_hz) if max_poll_hz and max_poll_hz > 0 else 0.0
        self._limiter = GpiozeroUltrasonicSensor._RateLimiter(min_interval_s)

        # Cached last reading. Until we have a real one, bias to "unknown": the
        # distance placeholder is the max range but ``known`` is False, so the
        # caller treats a never-yet-read sensor as STOP, not as clear path.
        self._last_distance_m: float = self._max_distance_m
        self._last_known: bool = False

        # --- Lazy hardware imports (keep the module laptop-importable) --------
        try:
            import gpiozero  # noqa: F401
            from gpiozero import DistanceSensor
        except Exception as exc:  # pragma: no cover - needs a real Pi env
            raise RuntimeError(
                "gpiozero is required to read the ultrasonic sensor on the Pi "
                f"but could not be imported ({exc!r}). Install the 'pi' extra on "
                f"the Raspberry Pi and see {_PIN_FACTORY_DOCS}."
            ) from exc

        # Reuse the motor driver's lgpio-factory enforcement (Pi 5 requirement).
        GpiozeroMotorDriver._set_lgpio_pin_factory(gpiozero)

        # gpiozero.DistanceSensor owns the TRIG pulse / ECHO timing. queue_len=1
        # so .distance is the latest single sample (no smoothing of stale pings);
        # threshold_distance is unused here (we don't use the event API).
        self._sensor: Any = DistanceSensor(
            echo=echo_pin,
            trigger=trig_pin,
            max_distance=self._max_distance_m,
            queue_len=1,
        )

    @staticmethod
    def _echo_seconds_to_distance(seconds: float, speed_of_sound: float = config.SPEED_OF_SOUND_MPS) -> float:
        """Convert a round-trip echo time (s) to a one-way distance (m).

        The pulse travels to the obstacle and back, so the one-way distance is
        half the round-trip path: ``distance = (echo_seconds * v) / 2``. E.g. a
        1 ms echo at 343 m/s → ``0.001 * 343 / 2`` ≈ ``0.17`` m.
        """
        return (float(seconds) * float(speed_of_sound)) / 2.0

    def read_distance(self) -> Tuple[float, bool]:
        """Return ``(distance_m, known)``; ``known=False`` means STOP.

        Rate-capped: if called sooner than ``1 / max_poll_hz`` since the last
        real sample, the cached reading is returned **without** re-pulsing. On a
        fresh successful echo returns ``(distance_m, True)``. On echo timeout /
        no echo returns ``(last_or_max_distance, False)`` — the distance is a
        placeholder that the caller must ignore because ``known`` is ``False``.
        We never fabricate a clear reading from a miss.
        """
        import time

        if self._closed:
            return (self._last_distance_m, False)

        now = time.monotonic()
        if not self._limiter.allow(now):
            # Too soon to ping again: serve the cached reading verbatim. We do
            # NOT re-pulse and we do NOT change ``known`` — a recent miss stays a
            # miss until a real sample replaces it.
            return (self._last_distance_m, self._last_known)

        distance_m, known = self._sample()
        self._last_distance_m = distance_m
        self._last_known = known
        return (distance_m, known)

    def _sample(self) -> Tuple[float, bool]:
        """Take one real reading from ``gpiozero.DistanceSensor``.

        ``.distance`` is ``0..1`` of ``max_distance``; multiply back to metres.
        A reading pinned at the ceiling (no echo returned within range) is
        treated as a **timeout → unknown**: we keep the last good distance as a
        placeholder but report ``known=False`` so the caller STOPs. Any sensor
        exception is likewise an unknown, never a clear path.
        """
        try:
            fraction = self._sensor.distance
        except Exception:  # pragma: no cover - hardware/IO error path
            # Sensor read blew up: unknown -> STOP, keep last placeholder.
            return (self._last_distance_m, False)

        if fraction is None:
            return (self._last_distance_m, False)

        distance_m = float(fraction) * self._max_distance_m

        # gpiozero reports a full-scale 1.0 when no echo returns in range — i.e.
        # an effective timeout. Treat at/over the ceiling as unknown, not clear.
        if float(fraction) >= 1.0 or distance_m >= self._max_distance_m:
            return (self._last_distance_m, False)

        return (distance_m, True)

    def close(self) -> None:
        """Release the underlying gpiozero device. Idempotent."""
        if self._closed:
            return
        self._closed = True
        sensor = getattr(self, "_sensor", None)
        if sensor is not None:
            try:
                sensor.close()
            except Exception:  # pragma: no cover - best effort during teardown
                pass


__all__ = [
    "MotorDriver",
    "RangeSensor",
    "FakeMotorDriver",
    "FakeRangeSensor",
    "GpiozeroMotorDriver",
    "GpiozeroUltrasonicSensor",
]
