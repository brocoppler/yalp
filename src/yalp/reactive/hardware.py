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

The one concrete, hardware-touching driver that lives here is
:class:`GpiozeroMotorDriver` — but it imports ``gpiozero``/``lgpio`` *lazily*
(inside ``__init__``/methods), so this module still imports cleanly on a Mac
with no hardware libraries present. Instantiating ``GpiozeroMotorDriver`` is what
requires the libraries; merely importing it does not.
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

    def _drive_channel(self, pwm: Any, dir_dev: Any, throttle: float, invert: bool) -> None:
        throttle = self._clamp(throttle)
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
        """Command signed throttles in ``[-1.0, 1.0]`` (sign = direction)."""
        self._drive_channel(self._left_pwm, self._left_dir, left, self._left_invert)
        self._drive_channel(self._right_pwm, self._right_dir, right, self._right_invert)

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


__all__ = [
    "MotorDriver",
    "RangeSensor",
    "FakeMotorDriver",
    "FakeRangeSensor",
    "GpiozeroMotorDriver",
]
