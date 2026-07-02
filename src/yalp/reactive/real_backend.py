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

A separate watchdog process (hardware.md / software-spec.md §2.6) independently
zeroes the motor GPIO on a stale heartbeat; that lives outside this class.
"""

from __future__ import annotations

import threading
import time
from typing import Optional, Tuple

from .. import config
from ..camera import Camera
from ..contract.messages import GoalStatus, Mode, RobotState
from ..contract.ipc import IntentMailbox, ReactiveServer
from .calibration import MotorCalibration, load_if_present
from .follow import FollowController, FollowDecision
from .tick_core import ReactiveTickCore
from .watchdog import MotorWatchdog


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
    """

    def __init__(
        self,
        *,
        motor_driver: Optional[object] = None,
        range_sensor: Optional[object] = None,
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
        """Start the camera capture thread (idempotent)."""
        if not self._camera_started:
            self._camera.start()
            self._camera_started = True
        return self

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
        self.start()
        # Perception (the heavy person detector) runs on its OWN worker thread for
        # the whole run — the async-perception task. FOLLOW ticks only READ the
        # latest observation non-blockingly, so ``tracker.update()`` (100–500 ms on
        # a Pi) never runs under the tick lock and can never blow TICK_BUDGET_MS /
        # trip the watchdog. Idle (no inference) until a FOLLOW intent is adopted.
        self.start_perception()
        # Independent safety net (hardware.md / software-spec.md §2.6): a daemon
        # watchdog that zeroes the motor GPIO if this loop ever stops heartbeating
        # (wedged tick, blocking call, dead thread). It does NOT depend on the
        # tick's own logic. We refresh its heartbeat at the END of every tick.
        watchdog = MotorWatchdog(self._motor_driver)
        watchdog.start()
        try:
            while stop_event is None or not stop_event.is_set():
                t0 = time.monotonic()
                state = self.tick()
                if server is not None:
                    server.publish(state)
                watchdog.heartbeat()
                elapsed = time.monotonic() - t0
                if dt > elapsed:
                    time.sleep(dt - elapsed)
        finally:
            # Shutdown ordering (async-perception task). The loop has exited, so the
            # tick will no longer heartbeat the watchdog. Retire the watchdog FIRST:
            #   * its stop() issues a final independent motor stop, so the wheels are
            #     guaranteed zeroed BEFORE any driver release and never run free
            #     during teardown (the worker never commands motors, so nothing
            #     re-spins them after this — they stay zeroed through the rest of
            #     teardown); and
            #   * retiring it before the perception-worker join below is what keeps
            #     the join HONEST: stop_perception() can block for up to one in-flight
            #     tracker.update() (a whole detector frame, ~100–500 ms on a Pi), and
            #     with the watchdog still armed that heartbeat-less window would look
            #     exactly like a wedged tick — tripping the watchdog and logging a
            #     scary "TRIPPED" alarm on EVERY normal FOLLOW shutdown. That cry-wolf
            #     would train operators to ignore real trips, so we avoid it.
            # Then the perception worker (stops reading the camera / feeding motion),
            # then close() — which itself keeps the worker -> camera -> motors-safe
            # ordering (worker stop is idempotent; motors are re-zeroed + released).
            watchdog.stop()
            self.stop_perception()
            self.stop()

    # -- teardown ------------------------------------------------------------
    def stop(self) -> None:
        """Zero the motors and release the driver, range sensor, and camera.

        Idempotent. Named ``stop`` so ``run()``'s ``finally`` (identical to the
        fake's) tears the hardware down on exit; :meth:`close` is an alias.
        """
        self.close()

    def close(self) -> None:
        """Release everything in safe order: worker -> camera -> motors-safe.

        Idempotent. Ordering (async-perception task): (1) stop the perception
        worker so it no longer reads the camera or drives motion; (2) stop the
        camera; (3) leave the motors SAFE — zero the wheels, then release the driver
        and range sensor. Each step is best-effort so teardown never raises. (When
        reached via ``run()`` the independent watchdog has already issued a final
        motor stop, so the wheels are stopped before we get here.)
        """
        if self._closed:
            return
        self._closed = True
        # 1. Perception worker.
        try:
            self.stop_perception()
        except Exception:  # pragma: no cover - best effort during teardown
            pass
        # 2. Camera.
        if self._camera_started:
            try:
                self._camera.stop()
            except Exception:  # pragma: no cover - best effort during teardown
                pass
            finally:
                self._camera_started = False
        # 3. Motors safe: zero the wheels, then release the driver + range sensor.
        for action in (
            self._motor_driver.stop,
            self._motor_driver.close,
            self._range_sensor.close,
        ):
            try:
                action()
            except Exception:  # pragma: no cover - best effort during teardown
                pass


__all__ = ["RealReactiveBackend"]
