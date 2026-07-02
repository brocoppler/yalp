"""Real (on-Pi) reactive backend — the production counterpart of the fake.

``RealReactiveBackend`` is the hardware-driving twin of
:class:`~yalp.reactive.fake_backend.FakeReactiveBackend`. It honors the **exact
same tick contract** (software-spec.md §2.3) — because both share the SAME tick
implementation, :class:`~yalp.reactive.tick_core.ReactiveTickCore`, so simulation
and hardware can never fork. Instead of simulating motion and a sensor it drives
real GPIO behind the two narrow interfaces from :mod:`yalp.reactive.hardware`:

  * a :class:`~yalp.reactive.hardware.MotorDriver` (left/right signed throttles),
  * a :class:`~yalp.reactive.hardware.RangeSensor` (forward distance; an unknown
    reading biases to STOP — never decayed to "clear").

The only backend-specific behavior is behind the shared core's small hooks: the
range read (:meth:`read_range` → the real sensor), commanding the motors
(:meth:`command_motors`) and halting them (:meth:`stop_motors`). The open-loop
``DRIVE_GOAL`` timing, the FOLLOW steering (same
:class:`~yalp.reactive.follow.FollowController` + lazily-built
:class:`~yalp.reactive.person_tracker.PersonTracker`), and the safety/preemption
ordering all live in the shared core; the steering/throttle decisions it produces
are commanded straight to real motor PWM (clamped to ``RobotState.speed_limit``).

Import hygiene (the whole point of the seam): **nothing hardware-specific is
imported at module load.** ``gpiozero``/``lgpio`` live behind the concrete
drivers in :mod:`yalp.reactive.hardware`, which are imported **lazily inside**
``__init__`` and only when a real driver/sensor is actually constructed. Tests on
a laptop inject :class:`~yalp.reactive.hardware.FakeMotorDriver` /
:class:`~yalp.reactive.hardware.FakeRangeSensor` plus a synthetic camera, so the
full tick contract is exercised with no hardware present.

Dead-man's switch (hardware.md / software-spec.md §2.6): the backend OWNS an
in-process :class:`~yalp.reactive.watchdog.MotorWatchdog` — a daemon thread that
zeroes the motor GPIO if the control loop stops heartbeating (a wedged tick, a
blocking call, a dead thread). It is armed by :meth:`start` (which
:meth:`run` calls) and, crucially, the heartbeat lives INSIDE :meth:`tick`, so a
caller driving ``tick()`` directly — not just ``run()`` — is covered. It does not
depend on the tick's own logic. A truly independent, *process-level* (or hardware)
watchdog outside this Python process remains future hardware work and is NOT built
here.
"""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING, Optional, Tuple

from .. import config
from ..camera import Camera
from ..contract.messages import GoalStatus, Mode, RobotState
from ..contract.ipc import IntentMailbox, ReactiveServer
from .calibration import MotorCalibration, load_if_present
from .follow import FollowController, FollowDecision
from .tick_core import ReactiveTickCore
from .watchdog import MotorWatchdog

if TYPE_CHECKING:  # typing-only: hardware.py's Protocols are laptop-safe to name
    from .hardware import MotorDriver, RangeSensor


class RealReactiveBackend(ReactiveTickCore):
    """On-Pi reactive layer: same preemption/safety contract, real motors/sensor.

    Parameters
    ----------
    motor_driver:
        A :class:`~yalp.reactive.hardware.MotorDriver`. ``None`` (the default)
        lazily constructs a real
        :class:`~yalp.reactive.hardware.GpiozeroMotorDriver` on the Pi; tests
        inject a :class:`~yalp.reactive.hardware.FakeMotorDriver`.
    range_sensor:
        A :class:`~yalp.reactive.hardware.RangeSensor`. ``None`` lazily
        constructs a real
        :class:`~yalp.reactive.hardware.GpiozeroUltrasonicSensor`; tests inject a
        :class:`~yalp.reactive.hardware.FakeRangeSensor`.
    camera / camera_source:
        The reactive layer OWNS one :class:`~yalp.camera.Camera` for the run
        (exactly like the fake). An explicit ``camera`` wins; otherwise one is
        built from ``camera_source`` (``"webcam"`` by default, with the camera's
        own synthetic fallback so it still runs headless).
    safe_stop_threshold_m:
        Distance (m) under which collision-stop fires this tick.
    max_speed_mps / turn_rate_dps:
        Open-loop motion model used to convert a drive/turn target into a timed
        duration — the honest stand-in for odometry, identical to the fake.
    tick_hz:
        Default tick rate for ``run()`` and the per-tick time step.
    tracker / follow_controller:
        Injected FOLLOW collaborators (so tests drive FOLLOW with a fake
        detector/tracker). The ``PersonTracker`` is built lazily on the first
        FOLLOW tick, so constructing the backend stays light.
    watchdog:
        The dead-man's switch (:class:`~yalp.reactive.watchdog.MotorWatchdog`).
        ``None`` (the default) lazily builds one over ``motor_driver``. It is
        armed by :meth:`start` and heartbeated from inside :meth:`tick`, so even
        a direct-``tick()`` caller gets the safety net; :meth:`close` retires it.
    """

    def __init__(
        self,
        *,
        motor_driver: "Optional[MotorDriver]" = None,
        range_sensor: "Optional[RangeSensor]" = None,
        camera: Optional[Camera] = None,
        camera_source: str = "webcam",
        mailbox: Optional[IntentMailbox] = None,
        safe_stop_threshold_m: float = config.SAFE_STOP_THRESHOLD_M,
        max_speed_mps: Optional[float] = None,
        turn_rate_dps: Optional[float] = None,
        tick_hz: float = config.REACTIVE_TICK_HZ,
        tracker: Optional[object] = None,
        follow_controller: Optional[FollowController] = None,
        calibration: Optional[MotorCalibration] = None,
        calibration_path: Optional[object] = None,
        watchdog: Optional[MotorWatchdog] = None,
    ) -> None:
        self.mailbox = mailbox or IntentMailbox()
        self.safe_stop_threshold_m = safe_stop_threshold_m

        # --- Calibration (software-fixable physical reality) -------------------
        # Load the persisted calibration if one exists (a missing/corrupt file ->
        # None -> fall back to the historical hand-guessed defaults). An explicit
        # ``calibration`` wins; an explicit ``max_speed_mps``/``turn_rate_dps``
        # overrides even the calibration (handy for tests/one-offs).
        if calibration is None:
            calibration = load_if_present(calibration_path)
        self.calibration = calibration
        cal_speed = calibration.max_speed_mps if calibration is not None else 0.5
        cal_turn = calibration.turn_rate_dps if calibration is not None else 120.0
        self.max_speed_mps = max(
            1e-3, max_speed_mps if max_speed_mps is not None else cal_speed
        )
        self.turn_rate_dps = max(
            1e-3, turn_rate_dps if turn_rate_dps is not None else cal_turn
        )
        self.tick_hz = max(1.0, tick_hz)
        # Convert the seconds-domain lost-grace window to ticks at OUR actual tick
        # rate (not the 20 Hz import-time default), so the ~0.9 s window holds.
        self._lost_grace_ticks = config.lost_grace_ticks(self.tick_hz)

        # --- Hardware drivers (LAZY imports — keep the module laptop-importable).
        # gpiozero/lgpio are only touched when we actually have to BUILD a real
        # driver/sensor; an injected fake skips the import entirely.
        if motor_driver is None or range_sensor is None:
            from .hardware import GpiozeroMotorDriver, GpiozeroUltrasonicSensor

            if motor_driver is None:
                # Honor the calibration's miswire fixes on the real driver too.
                if calibration is not None:
                    motor_driver = GpiozeroMotorDriver(
                        left_invert=calibration.left_invert,
                        right_invert=calibration.right_invert,
                        left_trim=calibration.left_trim,
                        right_trim=calibration.right_trim,
                    )
                else:
                    motor_driver = GpiozeroMotorDriver()
            if range_sensor is None:
                range_sensor = GpiozeroUltrasonicSensor()
        self._motor_driver = motor_driver
        self._range_sensor = range_sensor

        # Dead-man's switch (software-spec.md §2.6). Owned by the backend so it is
        # available to ANY caller, not just run(): armed in start(), heartbeated
        # from inside tick(), retired in close(). Built (not started) here.
        self._watchdog = watchdog or MotorWatchdog(self._motor_driver)

        # FOLLOW mode (software-spec.md §4): reuse the SAME track-by-detection
        # tracker + steering controller as the fake. Both are injectable so tests
        # drive FOLLOW with a fake detector/tracker. PersonTracker (which lazily
        # builds the OpenCV detector) is created on the first FOLLOW tick.
        self._tracker = tracker
        self._follow = follow_controller or FollowController(
            coast_ticks=self._lost_grace_ticks
        )
        #: The most recent FOLLOW steering decision (for the CLI live read-out).
        self.last_follow_decision: Optional[FollowDecision] = None

        # The reactive layer owns ONE camera for the run (exactly like the fake).
        self._camera = camera if camera is not None else Camera(source=camera_source)
        self._camera_started = False

        self._state = RobotState(mode=Mode.IDLE, goal_status=GoalStatus.NONE)
        self._goal_duration_s = 0.0
        self._frame_id = 0
        self._lock = threading.Lock()
        self._closed = False

    # -- shared-core hooks: real range read + real motor commands ------------
    def read_range(self) -> Tuple[float, bool]:
        """Read the real forward range sensor (rate-capped/echo-bounded in driver).

        An UNKNOWN reading (echo timeout / unavailable) is surfaced as
        ``known=False``; the shared core biases it to STOP (§2.3).
        """
        return self._range_sensor.read_distance()

    def command_motors(self, left: float, right: float) -> None:
        """Write signed ``(left, right)`` throttles to the real motor driver."""
        self._motor_driver.set_motors(left, right)

    def stop_motors(self) -> None:
        """Zero the wheels via the real motor driver (HALT, never reverse)."""
        self._motor_driver.stop()

    # -- camera --------------------------------------------------------------
    def start(self) -> "RealReactiveBackend":
        """Start the camera capture thread and arm the watchdog (idempotent).

        Arming the dead-man's switch here (not just in :meth:`run`) means the
        standard direct-drive lifecycle — ``start()`` then a ``tick()`` loop —
        is covered by the safety net, since :meth:`tick` refreshes the heartbeat.
        """
        if not self._camera_started:
            self._camera.start()
            self._camera_started = True
        self._watchdog.start()  # idempotent; heartbeats fresh on (re)arm
        return self

    # -- tick (heartbeat the dead-man's switch on EVERY path) ----------------
    def tick(self) -> RobotState:
        """Run the shared reactive tick, then heartbeat the watchdog.

        The heartbeat lives here — after the full tick returns — so it fires on
        every tick regardless of which internal path (obstacle halt, preemption,
        normal step) produced the snapshot, and so a wedged/blocking tick fails
        to heartbeat and correctly trips the watchdog. It is a no-op cost when
        the watchdog thread has not been armed (e.g. a bare ``tick()`` in a unit
        test that never called :meth:`start`).
        """
        state = super().tick()
        self._watchdog.heartbeat()
        return state

    # -- run loop ------------------------------------------------------------
    def run(
        self,
        server: Optional[ReactiveServer] = None,
        stop_event: Optional[threading.Event] = None,
        hz: Optional[float] = None,
    ) -> None:
        """Tick at ``hz`` and publish each snapshot via ``server`` until stopped.

        The reactive process keeps running whether or not a deliberative client
        is connected (software-spec.md §5): with no client, ``publish`` is simply
        a no-op push. On exit the motors are zeroed and the hardware released.
        """
        rate = hz or self.tick_hz
        dt = 1.0 / rate
        # start() arms the backend-owned watchdog (the independent safety net,
        # hardware.md / software-spec.md §2.6) and starts the camera. tick()
        # heartbeats the watchdog on every path, so this loop needs no explicit
        # heartbeat; publish() only ENQUEUES (a dedicated writer thread does the
        # socket I/O), so a slow/stalled client can never stall a tick.
        self.start()
        # Perception (the heavy person detector) runs on its OWN worker thread for
        # the whole run — the async-perception task. FOLLOW ticks only READ the
        # latest observation non-blockingly, so ``tracker.update()`` (100–500 ms on
        # a Pi) never runs under the tick lock and can never blow TICK_BUDGET_MS /
        # trip the watchdog. Idle (no inference) until a FOLLOW intent is adopted.
        # (The independent watchdog is the backend-owned ``self._watchdog``, armed
        # by start() above and heartbeated from inside tick() — no local one here.)
        self.start_perception()
        try:
            while stop_event is None or not stop_event.is_set():
                t0 = time.monotonic()
                state = self.tick()
                if server is not None:
                    server.publish(state)
                elapsed = time.monotonic() - t0
                if dt > elapsed:
                    time.sleep(dt - elapsed)
        finally:
            # Shutdown ordering (async-perception task + independent watchdog).
            # The loop has exited, so tick() will no longer heartbeat the watchdog.
            # Delegate to close()/stop(), which retires the backend-owned watchdog
            # FIRST — before the (potentially blocking) perception-worker join and
            # any hardware release — so teardown never looks like a wedged tick.
            #   * its stop() issues a final independent motor stop, so the wheels are
            #     guaranteed zeroed BEFORE any driver release and never run free
            #     during teardown (the worker never commands motors, so nothing
            #     re-spins them — they stay zeroed through the rest of teardown); and
            #   * retiring it before the perception-worker join keeps that join
            #     HONEST: stop_perception() can block for up to one in-flight
            #     tracker.update() (a whole detector frame, ~100–500 ms on a Pi), and
            #     with the watchdog still armed that heartbeat-less window would look
            #     exactly like a wedged tick — tripping the watchdog and logging a
            #     scary "TRIPPED" alarm on EVERY normal FOLLOW shutdown. That cry-wolf
            #     would train operators to ignore real trips, so we avoid it.
            self.stop()

    # -- teardown ------------------------------------------------------------
    def stop(self) -> None:
        """Zero the motors and release the driver, range sensor, and camera.

        Idempotent. Named ``stop`` so ``run()``'s ``finally`` (identical to the
        fake's) tears the hardware down on exit; :meth:`close` is an alias.
        """
        self.close()

    def close(self) -> None:
        """Release everything in safe order: watchdog -> worker -> camera -> motors-safe.

        Idempotent. Ordering (async-perception task + independent watchdog):
        (1) retire the watchdog FIRST — it joins its own thread and issues a final
        independent motor stop, and (unlike the steps below) can never look like a
        wedged tick, so retiring it before the potentially blocking perception join
        avoids a cry-wolf "TRIPPED" alarm on every normal FOLLOW shutdown; (2) stop
        the perception worker so it no longer reads the camera or drives motion;
        (3) stop the camera; (4) leave the motors SAFE — zero the wheels, release
        the RANGE SENSOR before the motor driver, and only then close the motor
        driver. Each step is best-effort so teardown never raises. (When reached via
        ``run()`` the watchdog has already issued a final motor stop, so the wheels
        are stopped before we get here.)
        """
        if self._closed:
            return
        self._closed = True
        # 1. Retire the watchdog FIRST. It joins its own thread and issues a final
        #    independent motor stop, and — unlike the perception join and hardware
        #    release below — it can never look like a wedged tick. Retiring it
        #    before the (potentially blocking) perception join is what keeps the
        #    watchdog from crying wolf: stop_perception() can block for a whole
        #    in-flight tracker.update() (~100–500 ms on a Pi), a heartbeat-less
        #    window that would otherwise look exactly like a wedged tick and trip
        #    the alarm. It never re-enables motors, so the wheels stay stopped
        #    through the rest of teardown.
        try:
            self._watchdog.stop()
        except Exception:  # pragma: no cover - best effort during teardown
            pass
        # 2. Perception worker (stops reading the camera / feeding motion).
        try:
            self.stop_perception()
        except Exception:  # pragma: no cover - best effort during teardown
            pass
        # 3. Camera.
        if self._camera_started:
            try:
                self._camera.stop()
            except Exception:  # pragma: no cover - best effort during teardown
                pass
            finally:
                self._camera_started = False
        # 4. Motors safe: zero the wheels, close the RANGE SENSOR before the motor
        #    driver, and only then close the motor driver. The motor driver's
        #    close() tears down the process-global gpiozero pin factory, which would
        #    break an ultrasonic sensor still holding pins — so the sensor must be
        #    released first (motors are already stopped, so nothing is driving).
        for action in (
            self._motor_driver.stop,
            self._range_sensor.close,
            self._motor_driver.close,
        ):
            try:
                action()
            except Exception:  # pragma: no cover - best effort during teardown
                pass


__all__ = ["RealReactiveBackend"]
