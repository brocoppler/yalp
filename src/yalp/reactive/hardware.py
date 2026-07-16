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

import logging
from typing import Any, Callable, Dict, List, Optional, Tuple, runtime_checkable, Protocol

from yalp import config

logger = logging.getLogger(__name__)


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
        # Observability counters mirroring GpiozeroUltrasonicSensor.stats() so the
        # fake is a faithful stand-in when injected into RealReactiveBackend: a
        # state poll / telemetry record carries the same 'ultrasonic' sub-map on a
        # laptop as on the Pi. The fake has no coast grace, so ``coasted_reads`` is
        # always 0 and every miss is served straight through (unknown).
        self._valid_reads = 0
        self._raw_misses = 0
        self._unknown_served = 0

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
        if self.known:
            self._valid_reads += 1
        else:
            self._raw_misses += 1
            self._unknown_served += 1
        return (self.distance_m, self.known)

    def stats(self) -> Dict[str, int]:
        """Cumulative read counters (mirrors :meth:`GpiozeroUltrasonicSensor.stats`)."""
        return {
            "total_reads": self.read_count,
            "valid_reads": self._valid_reads,
            "raw_misses": self._raw_misses,
            "coasted_reads": 0,  # the fake has no coast grace
            "unknown_served": self._unknown_served,
        }

    def close(self) -> None:
        self.closed = True


_PIN_FACTORY_DOCS = "docs/technical/hardware.md"


class GpiozeroMotorDriver:
    """A real :class:`MotorDriver` backed by ``gpiozero`` on a Raspberry Pi 5.

    **Two GPIO lines per channel, but the meaning depends on the chip.** The Pi 5
    exposes only two hardware-PWM lines (GPIO12 = PWM0, GPIO13 = PWM1), so each
    wheel channel is driven by one hardware-PWM pin (xIN1) plus one plain digital
    pin (xIN2). How those two lines map to motor behaviour is **driver-specific**
    and was the source of a safety-critical field bug on 2026-07-06 (see below):

    * ``drv8833`` — an **IN/IN** dual H-bridge. Both inputs are logical; there is
      no dedicated "phase" pin. Forward/reverse and speed are encoded jointly in
      the two inputs (see the truth table on :meth:`_drive_channel`). This is the
      wiring on the robot and the default (:data:`config.MOTOR_DRIVER_KIND`).
    * ``tb6612fng`` — a phase/enable-style mapping (direction pin HIGH = forward,
      duty = ``abs(throttle)``). **UNVERIFIED on hardware** with our single-DIR-
      pin-per-channel layout — see the note on :meth:`_drive_channel`.

    **DRV8833 is IN/IN, not PH/EN — history.** An earlier version of this driver
    treated the DRV8833 as a phase/enable device (direction pin HIGH for
    ``throttle >= 0``, duty = ``abs(throttle)``). On the real DRV8833 that dialect
    is not merely mis-scaled, it is *dangerous*: an "idle" channel (duty 0, dir
    HIGH) becomes IN1=0/IN2=1 = **full-speed reverse**, and ``stop()`` — the path
    the :class:`~yalp.reactive.watchdog.MotorWatchdog` dead-man's switch calls —
    latched the direction pins HIGH and commanded full reverse instead of coast.
    The fix (this class) drives the DRV8833 by its actual IN/IN truth table.

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
        """Drive one wheel channel from a signed throttle.

        Calibration is applied first and **in this order**: scale by ``trim``,
        clamp to ``[-1, 1]``, then flip the sign if ``invert`` is set. The sign
        flip happens *before* the direction/duty mapping below, so an inverted
        wheel's forward/reverse and decay mode are chosen from the already-
        corrected throttle (this is what makes ``left_invert``/``right_invert``
        actually reverse a miswired wheel rather than just its duty).

        **DRV8833 (IN/IN) — the wiring on the robot.** With PWM pin → xIN1 and
        DIR pin → xIN2, the DRV8833 datasheet truth table (per H-bridge) is::

            xIN1   xIN2   OUTx   function
            ----   ----   ----   --------------------------------------------
            PWM     0     fwd    forward at duty, FAST decay
            PWM     1     rev    reverse at (1 - duty), SLOW decay
             0      1     rev    full-speed reverse
             1      0     fwd    full-speed forward
             0      0     Z      coast (outputs high-Z)
             1      1     brake  brake (both outputs low)

        (DRV8833 datasheet, "H-bridge control" truth table.) Only xIN1
        (GPIO12/13) is hardware-PWM-capable on the Pi 5, so we PWM xIN1 and hold
        xIN2 as a plain digital line. That forces an intentional **fwd/rev decay
        asymmetry**: forward is fast-decay PWM on xIN1 (xIN2 low), but reverse
        cannot fast-decay (xIN2 is not PWM-capable) so it uses **slow decay with
        inverted duty** — xIN2 held HIGH while xIN1 PWMs at ``1 - abs(throttle)``.
        Consequences that matter: a *zero* throttle must set xIN2 LOW + duty 0
        (true coast); it must NOT leave xIN2 latched HIGH, or an "idle" channel
        becomes full-speed reverse (the 2026-07-06 field bug).

        **TB6612FNG.** Kept as a phase/enable mapping (dir HIGH = forward, duty =
        ``abs(throttle)``) for backwards compatibility, but this is **UNVERIFIED
        on hardware**: the TB6612FNG is itself an IN/IN part (it wants PWMx plus
        *two* IN pins per channel), which our single-DIR-pin-per-channel layout
        does not fully wire. Flagged for a future hardware bring-up session; do
        not trust this path until it has been checked on a real TB6612FNG.
        """
        # Apply per-wheel trim scaling BEFORE clamping, so the trimmed magnitude
        # still lands in [-1, 1] (trim defaults to 1.0 = no-op).
        throttle = self._clamp(throttle * trim)
        if invert:
            throttle = -throttle

        if self._driver_kind == "drv8833":
            # IN/IN decay-mode dialect (see truth table above).
            if throttle > 0:
                dir_dev.off()  # xIN2 = 0
                pwm.value = throttle  # xIN1 = PWM  -> forward, fast decay
            elif throttle < 0:
                dir_dev.on()  # xIN2 = 1
                pwm.value = 1.0 - abs(throttle)  # xIN1 = PWM -> reverse, slow decay
            else:
                dir_dev.off()  # xIN2 = 0
                pwm.value = 0.0  # xIN1 = 0    -> true coast (both inputs low)
            return

        # TB6612FNG (and any other kind): legacy phase/enable mapping. UNVERIFIED
        # on hardware — see docstring.
        if throttle >= 0:
            dir_dev.on()
        else:
            dir_dev.off()
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
        """Bring both channels to a **true coast** (both inputs LOW).

        On the DRV8833 (IN/IN) coast is xIN1=0 **and** xIN2=0; zeroing only the
        PWM duty is not enough, because after any forward/reverse command the
        direction (xIN2) pin may be latched HIGH, and duty-0 + xIN2-HIGH is
        IN1=0/IN2=1 = **full-speed reverse**, not coast. This method is what the
        :class:`~yalp.reactive.watchdog.MotorWatchdog` dead-man's switch and the
        collision-stop safety paths call, so it MUST land in coast: we zero BOTH
        PWM duties AND drive BOTH direction pins LOW. Idempotent and never raises
        (best-effort during a safety stop).
        """
        for pwm in (self._left_pwm, self._right_pwm):
            try:
                pwm.value = 0.0
            except Exception:  # pragma: no cover - best effort during a safety stop
                pass
        for dir_dev in (self._left_dir, self._right_dir):
            try:
                dir_dev.off()
            except Exception:  # pragma: no cover - best effort during a safety stop
                pass

    def close(self) -> None:
        """Coast the motors, close every gpiozero device, and release the factory.

        Teardown ends in coast: :meth:`stop` runs first (zeroing both PWM duties
        and dropping both direction pins to LOW) so the motors are truly coasting
        before any pin is released — never latched into full reverse.

        The process-global pin factory is only torn down when no other device
        still holds pins on it (see :meth:`_factory_has_open_reservations`), so
        closing the motors never yanks the factory out from under a still-open
        ultrasonic sensor.
        """
        if self._closed:
            return
        # Coast first (zero BOTH PWM duties AND drop BOTH direction pins), so the
        # motors are in true coast before any pin is released — on the DRV8833
        # zeroing PWM alone can leave a latched-HIGH direction pin driving full
        # reverse (see :meth:`stop`). Do this while ``_closed`` is still False so
        # stop()'s writes are not short-circuited by a closed guard.
        try:
            self.stop()
        except Exception:  # pragma: no cover - best effort during teardown
            pass
        self._closed = True
        for dev in self._devices:
            try:
                dev.close()
            except Exception:  # pragma: no cover - best effort during teardown
                pass
        # Best-effort: tear down the lgpio pin factory we installed — but ONLY if
        # no other device still holds pins on it. The pin factory is a
        # PROCESS-GLOBAL that the ultrasonic sensor shares; closing it out from
        # under a still-open sensor would break that sensor's own close(). If any
        # reservation remains we leave the factory in place (harmless — the OS
        # reclaims GPIO at process exit); the caller's teardown order should close
        # the sensor first so this branch normally does reset the factory.
        try:
            import gpiozero

            factory = getattr(gpiozero.Device, "pin_factory", None)
            if factory is not None and not self._factory_has_open_reservations(factory):
                factory.close()
                gpiozero.Device.pin_factory = None
        except Exception:  # pragma: no cover - best effort during teardown
            pass

    @staticmethod
    def _factory_has_open_reservations(factory: Any) -> bool:  # pragma: no cover - needs a real Pi env
        """True if another live device still holds pins on the shared factory.

        gpiozero tracks live pin reservations on the factory (releasing them as
        each device closes). If any remain after we've closed our own devices,
        something else — e.g. the ultrasonic sensor — is still using the shared
        factory, so we must NOT close it. Defensive across gpiozero versions: an
        unknown/absent reservation map is treated as "nothing open" so we fall
        back to the historical reset behaviour.
        """
        reservations = getattr(factory, "_reservations", None)
        try:
            return bool(reservations)
        except Exception:
            return False


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
    * **Bounded "coast last-known" grace (Pi 5 phantom-STOP fix).** On the Pi 5
      the echo is timed in *software* (pigpio is unavailable), so isolated reads
      spuriously time out — especially at longer range where the return echo is
      weak — and, since a miss correctly biases to STOP, the robot phantom-stops
      every second or two and cannot drive. To fix this WITHOUT weakening the
      invariant, a single miss (or a brief burst) re-serves the last VALID
      distance instead of instantly declaring blindness, bounded by BOTH
      ``grace_ms`` (a wall-clock window since the last valid reading) AND
      ``grace_max_misses`` (a consecutive-miss budget). Whichever bound trips
      FIRST ends the grace; the read then reverts to ``(placeholder, False)``
      exactly as before → STOP. A single valid read resets both bounds. The
      grace only ever RE-SERVES the exact last measured distance — it never
      fabricates a larger/clear value — so a near obstacle (last valid inside
      ``safe_stop_threshold_m``) is coasted as an obstacle (still STOP), and
      **sustained** sensor loss still STOPs. This is a bounded, deliberate risk:
      the robot coasts on the last good reading for at most ~``grace_ms`` /
      ``grace_max_misses`` misses (~0.15 m of travel at 1 m/s), well inside the
      ``safe_stop_threshold_m`` stopping margin. It is independent of the motor
      watchdog (a stalled tick loop still trips the dead-man's switch).

    * **Observability counters (true miss rate).** The coast grace re-serves a
      missed echo as ``known=True``, so an absorbed miss is INDISTINGUISHABLE over
      IPC/state from a genuine valid echo — an external observer's ``known=False``
      rate is therefore only a LOWER BOUND on the real miss rate (2026-07-16 field
      finding). To make the real numbers visible, cheap monotonic counters —
      ``total_reads`` (every :meth:`read_distance` call), ``valid_reads`` (fresh
      valid echoes), ``raw_misses`` (real echo timeouts, the TRUE miss count before
      any grace), ``coasted_reads`` (raw misses the grace absorbed) and
      ``unknown_served`` (reads that surfaced ``known=False`` to the caller) — are
      incremented on the hot path (integer bumps only) and exposed read-only via
      :meth:`stats`. They are threaded into the reactive state snapshot / telemetry
      so a field session can see the true miss rate, not just the absorbed remainder.

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
        grace_ms: float = config.ULTRASONIC_GRACE_MS,
        grace_max_misses: int = config.ULTRASONIC_GRACE_MAX_MISSES,
        safe_stop_threshold_m: float = config.SAFE_STOP_THRESHOLD_M,
        monotonic: Optional[Callable[[], float]] = None,
    ) -> None:
        self._max_distance_m = float(max_distance_m)
        self._echo_timeout_s = float(echo_timeout_s)
        self._speed_of_sound = float(speed_of_sound)
        self._closed = False

        # Injectable monotonic clock so the rate-cap AND grace logic are testable
        # with a fake clock (no real sleeping). ``None`` => live ``time.monotonic``
        # resolved at CALL time, which also honours a test that monkeypatches it.
        self._clock: Optional[Callable[[], float]] = monotonic

        # Rate-cap: >= 1 / max_poll_hz between real samples (>= ~60 ms at 15 Hz).
        min_interval_s = (1.0 / max_poll_hz) if max_poll_hz and max_poll_hz > 0 else 0.0
        self._limiter = GpiozeroUltrasonicSensor._RateLimiter(min_interval_s)

        # --- Bounded "coast last-known" grace (Pi 5 phantom-STOP fix) ---------
        # A miss briefly re-serves the last VALID reading, bounded by BOTH a
        # wall-clock window (grace_ms since the last valid read) AND a
        # consecutive-miss budget (grace_max_misses). Whichever trips first ends
        # the grace -> (placeholder, False) -> STOP. See the class docstring.
        self._grace_s: float = max(0.0, float(grace_ms) / 1000.0)
        self._grace_max_misses: int = int(grace_max_misses)
        self._safe_stop_threshold_m: float = float(safe_stop_threshold_m)
        # Last VALID (known=True) reading and when it was taken; ``None`` until we
        # have ever measured one. Grace can ONLY coast off a real prior valid.
        self._last_valid_distance_m: Optional[float] = None
        self._last_valid_at: Optional[float] = None
        # Consecutive misses currently being coasted (reset by any valid read).
        self._grace_misses: int = 0

        # One-time sanity warning (Pi 5 field finding, 2026-07-15): the coast
        # grace can only re-serve a miss while its wall-clock window is still
        # OPEN, so that window MUST exceed one re-pulse interval (1 / max_poll_hz)
        # or coasting is mathematically impossible — the first miss always arrives
        # AFTER the window has already tripped, so 0 misses are absorbed (a silent
        # no-op, identical to grace-off; measured 0/10 at 6 Hz / 150 ms on real
        # hardware). Warn once, at construction, when the grace is ENABLED but
        # inert at this poll rate so the operator can raise the window or the poll
        # rate rather than run with a grace that silently does nothing.
        # (max_poll_hz <= 0 disables the rate cap, so every read re-pulses and the
        # grace is NOT inert — no warning in that case.)
        grace_enabled = self._grace_s > 0.0 and self._grace_max_misses > 0
        if grace_enabled and max_poll_hz and max_poll_hz > 0:
            repulse_interval_s = 1.0 / max_poll_hz
            if self._grace_s < repulse_interval_s:
                logger.warning(
                    "ultrasonic coast-grace is INERT at this poll rate: the grace "
                    "window (%.0f ms) is shorter than one re-pulse interval "
                    "(%.0f ms at max_poll_hz=%g Hz), so the wall-clock bound trips "
                    "before the first missed echo can be coasted and the grace "
                    "absorbs nothing (a silent no-op). Raise "
                    "YALP_ULTRASONIC_GRACE_MS above %.0f ms, or raise the poll "
                    "rate, to make coasting effective.",
                    self._grace_s * 1000.0,
                    repulse_interval_s * 1000.0,
                    max_poll_hz,
                    repulse_interval_s * 1000.0,
                )

        # Cached last reading. Until we have a real one, bias to "unknown": the
        # distance placeholder is the max range but ``known`` is False, so the
        # caller treats a never-yet-read sensor as STOP, not as clear path.
        self._last_distance_m: float = self._max_distance_m
        self._last_known: bool = False

        # --- Observability counters (see the class docstring) -----------------
        # Cheap monotonic tallies, bumped on the hot path (integer increments
        # only) and exposed read-only via stats(). ``raw_misses`` is the TRUE
        # miss count (every real echo timeout), of which ``coasted_reads`` is the
        # subset the grace absorbed and re-served as known=True — the difference an
        # external observer could never see before, because a coasted miss looks
        # exactly like a valid echo. ``unknown_served`` is what an external
        # observer DOES count (reads that returned known=False).
        self._total_reads: int = 0
        self._valid_reads: int = 0
        self._raw_misses: int = 0
        self._coasted_reads: int = 0
        self._unknown_served: int = 0

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

    def _now(self) -> float:
        """Read the (possibly injected) monotonic clock.

        Defaults to live ``time.monotonic`` resolved at CALL time so a test that
        monkeypatches ``time.monotonic`` (or injects ``monotonic=`` at
        construction) fully controls both the rate-cap and the grace timing.
        """
        if self._clock is not None:
            return self._clock()
        import time

        return time.monotonic()

    def read_distance(self) -> Tuple[float, bool]:
        """Return ``(distance_m, known)``; ``known=False`` means STOP.

        Rate-capped: if called sooner than ``1 / max_poll_hz`` since the last
        real sample, the cached reading is returned **without** re-pulsing. On a
        fresh successful echo returns ``(distance_m, True)`` and RESETS the grace.
        On an echo timeout / no echo, the bounded "coast last-known" grace may
        briefly re-serve the last VALID ``(distance_m, True)`` (see
        :meth:`_register_sample`); once the grace is exhausted (wall-clock window
        OR miss budget) it returns ``(placeholder, False)`` — the distance is a
        placeholder the caller must ignore because ``known`` is ``False``. We
        never fabricate a clear reading from a miss.
        """
        # Count EVERY served read (including closed / rate-capped cache serves),
        # so ``unknown_served / total_reads`` is exactly the miss fraction an
        # external observer sees per served reading.
        self._total_reads += 1

        if self._closed:
            self._unknown_served += 1
            return (self._last_distance_m, False)

        now = self._now()
        if not self._limiter.allow(now):
            # Too soon to ping again: serve the cached reading verbatim. We do
            # NOT re-pulse and we do NOT change ``known`` — during a grace coast
            # the cache already holds (last_valid, True); otherwise a recent miss
            # stays a miss until a real sample replaces it. Rate-capped reads
            # therefore never consume the miss budget (only real re-pulses do), but
            # a cache-served miss is still an unknown SURFACED to the caller.
            if not self._last_known:
                self._unknown_served += 1
            return (self._last_distance_m, self._last_known)

        distance_m, known = self._sample()
        return self._register_sample(distance_m, known, now)

    def stats(self) -> Dict[str, int]:
        """Read-only snapshot of the cumulative observability counters.

        Returns a fresh dict (a copy, safe to serialize/mutate) of the monotonic
        tallies described on the class docstring::

            total_reads     every read_distance() call (incl. cache serves)
            valid_reads     fresh valid echoes (known=True from a new sample)
            raw_misses      real echo timeouts — the TRUE miss count, before grace
            coasted_reads   raw misses the grace absorbed (re-served known=True)
            unknown_served  reads that surfaced known=False to the caller

        Invariants (useful for readers): ``raw_misses >= coasted_reads`` and the
        real-sample count is ``valid_reads + raw_misses``. ``coasted_reads`` is the
        externally-invisible slice; comparing it to ``unknown_served`` recovers the
        true miss rate an IPC/state observer could otherwise only lower-bound.
        """
        return {
            "total_reads": self._total_reads,
            "valid_reads": self._valid_reads,
            "raw_misses": self._raw_misses,
            "coasted_reads": self._coasted_reads,
            "unknown_served": self._unknown_served,
        }

    def _register_sample(self, distance_m: float, known: bool, now: float) -> Tuple[float, bool]:
        """Fold one REAL sample into the grace state and return what to report.

        * A **valid** reading (``known=True``) resets both grace bounds and is
          returned verbatim as the new last-valid.
        * A **miss** (``known=False``) is coasted as the last VALID reading while
          BOTH bounds still hold (:meth:`_can_coast`); each coasted miss spends
          one unit of the miss budget. Once either bound trips, the read reverts
          to ``(placeholder, False)`` → STOP, exactly as before the grace.
        """
        if known:
            # A fresh valid reading RESETS the grace entirely.
            self._valid_reads += 1
            self._last_valid_distance_m = distance_m
            self._last_valid_at = now
            self._grace_misses = 0
            self._last_distance_m = distance_m
            self._last_known = True
            return (distance_m, True)

        # A real echo miss — the TRUE miss count, independent of whether the grace
        # goes on to absorb it.
        self._raw_misses += 1

        # Coast the last VALID reading iff BOTH bounds hold.
        if self._can_coast(now):
            self._grace_misses += 1
            # This raw miss is being ABSORBED by the grace: it is re-served as
            # known=True and so is invisible to any external observer — the very
            # gap this counter exists to expose.
            self._coasted_reads += 1
            # Re-serve the EXACT last valid distance — never larger/clear. If that
            # reading was already inside safe_stop_threshold_m it still reports an
            # obstacle (< threshold → STOP); if it was clear we briefly coast.
            self._last_distance_m = self._last_valid_distance_m  # type: ignore[assignment]
            self._last_known = True
            return (self._last_distance_m, True)

        # Grace exhausted (window elapsed OR miss budget spent) — or we never had
        # a valid reading to coast: behave exactly as before → (placeholder,
        # False) so the caller SAFE_STOPs. We do NOT reset the last-valid: a later
        # valid read is what clears the STOP.
        self._unknown_served += 1
        self._last_known = False
        return (self._last_distance_m, False)

    def _can_coast(self, now: float) -> bool:
        """True iff a miss may be coasted as the last VALID reading right now.

        Requires a prior valid reading and BOTH bounds unspent: the miss budget
        (``grace_max_misses``) AND the wall-clock window (``grace_ms`` since the
        last valid read). A zero/negative bound disables the grace entirely, in
        which case a miss immediately reverts to the pre-grace STOP behaviour.
        """
        if self._last_valid_distance_m is None or self._last_valid_at is None:
            return False  # never measured a valid reading -> cannot coast, STOP
        if self._grace_s <= 0.0 or self._grace_max_misses <= 0:
            return False  # grace disabled -> immediate STOP (pre-grace behaviour)
        if self._grace_misses >= self._grace_max_misses:
            return False  # consecutive-miss budget spent
        if (now - self._last_valid_at) > self._grace_s:
            return False  # wall-clock window since the last valid read elapsed
        return True

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
