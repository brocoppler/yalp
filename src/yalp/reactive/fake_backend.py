"""A fake reactive backend for laptop-first development (software-spec.md THESIS).

``FakeReactiveBackend`` implements the full reactive contract in pure simulation
so the entire deliberative path and the loop-to-loop seam can be exercised with
**no hardware and no network**:

  * maintains a simulated ``RobotState``;
  * adopts ``Intent``s through the single-slot, last-write-wins mailbox;
  * advances ``DRIVE_GOAL`` straight/rotate motions as **timed, open-loop**
    guesses and reports ``"completed (timed, unverified)"`` (never a bare
    "completed") — there are no encoders;
  * simulates a configurable obstacle that fires collision-stop → ``SAFE_STOP`` /
    ``BLOCKED`` (sticky: clears only when the obstacle is gone **and** a fresh
    intent arrives), and never open-loop reverses;
  * **owns the camera** (the reactive layer owns the camera, per the contract):
    it holds a single ``yalp.camera.Camera`` for the run — the **real webcam by
    default** (auto-falling back to a synthetic source when no camera can be
    opened, so it still runs headless / in CI). The deliberative
    ``describe_scene`` reads the latest frame from *this* same camera — one
    device, opened once. Headless callers and tests pass an explicit synthetic
    camera or ``camera_source="synthetic"``.

The tick contract itself (sensor read → sticky safety override → mailbox drain →
mode step) lives ONCE in :class:`~yalp.reactive.tick_core.ReactiveTickCore`, shared
byte-for-byte with :class:`~yalp.reactive.real_backend.RealReactiveBackend`; the
fake only supplies the simulated range read (``read_range``) and leaves the motor
hooks as no-ops (there are no wheels to drive). This class stays dependency-free
(stdlib + numpy; OpenCV is imported lazily inside the FOLLOW step only).

It also provides a ``run`` loop that ticks at a target rate and publishes state
through a ``ReactiveServer``.
"""

from __future__ import annotations

import threading
import time
from typing import Optional, Tuple

from .. import config
from ..camera import Camera
from ..contract.messages import GoalStatus, Mode, RobotState
from ..contract.ipc import IntentMailbox, ReactiveServer
from .follow import FollowController, FollowDecision
from .tick_core import ReactiveTickCore


class FakeReactiveBackend(ReactiveTickCore):
    """A simulated reactive layer that honors the full preemption/safety contract.

    Parameters
    ----------
    camera:
        An explicit (pre-built) ``Camera`` to read frames from (highest
        precedence). If omitted, one is created from ``camera_source``. The
        reactive layer OWNS this single camera for the run; the deliberative
        ``describe_scene`` reads its latest frame from the SAME instance (never
        opens a second device). Tests and headless callers pass a synthetic/mock
        camera so they never touch hardware.
    camera_source:
        Source string used to build the owned ``Camera`` when ``camera`` is not
        given: ``"webcam"`` (the default — real webcam with an automatic
        synthetic fallback if no device opens), ``"synthetic"`` to force the test
        pattern (for headless/CI/reproducible runs), or ``"image"``.
    safe_stop_threshold_m:
        Distance (m) under which collision-stop fires.
    max_speed_mps / turn_rate_dps:
        Open-loop motion model used to convert a drive/turn target into a timed
        duration (the honest stand-in for odometry).
    tick_hz:
        Default tick rate for ``run()`` and the per-tick simulated time step.
    """

    def __init__(
        self,
        *,
        camera: Optional[Camera] = None,
        camera_source: str = "webcam",
        mailbox: Optional[IntentMailbox] = None,
        safe_stop_threshold_m: float = config.SAFE_STOP_THRESHOLD_M,
        max_speed_mps: float = 0.5,
        turn_rate_dps: float = 120.0,
        tick_hz: float = config.REACTIVE_TICK_HZ,
        tracker: Optional[object] = None,
        follow_controller: Optional[FollowController] = None,
        observer: Optional[object] = None,
        close_observer: bool = False,
    ) -> None:
        # Observer seam (telemetry / any recorder). Injected so tests and library
        # users can pass their own or leave it None. ``close_observer`` = this
        # backend OWNS the observer's lifecycle and closes it on ``stop()`` (set
        # when a run loop auto-creates the recorder); an injected, caller-owned
        # observer is never closed by the backend.
        self._observer = observer
        self._close_observer = bool(close_observer)
        self.mailbox = mailbox or IntentMailbox()
        self.safe_stop_threshold_m = safe_stop_threshold_m
        self.max_speed_mps = max(1e-3, max_speed_mps)
        self.turn_rate_dps = max(1e-3, turn_rate_dps)
        self.tick_hz = max(1.0, tick_hz)
        # Convert the seconds-domain lost-grace window to ticks at OUR actual tick
        # rate (not the 20 Hz import-time default), so the ~0.9 s window holds.
        self._lost_grace_ticks = config.lost_grace_ticks(self.tick_hz)

        # FOLLOW mode (software-spec.md §4): a track-by-detection tracker reads the
        # shared camera and a controller turns its result into steering. Both are
        # injectable so tests drive FOLLOW with a fake detector/tracker and no
        # hardware. The PersonTracker (which lazily builds the OpenCV detector) is
        # created on first FOLLOW tick so constructing the backend stays light.
        self._tracker = tracker
        self._follow = follow_controller or FollowController(
            coast_ticks=self._lost_grace_ticks
        )
        #: The most recent FOLLOW steering decision (for the CLI live read-out).
        self.last_follow_decision: Optional[FollowDecision] = None

        # The reactive layer owns ONE camera for the run. Default to the real
        # webcam (Camera auto-falls-back to synthetic if it can't open), so a
        # real `yalp agent` gets real eyes while CI/headless stays hardware-free.
        self._camera = camera if camera is not None else Camera(source=camera_source)
        self._camera_started = False

        self._state = RobotState(mode=Mode.IDLE, goal_status=GoalStatus.NONE)
        self._goal_duration_s = 0.0
        self._frame_id = 0
        self._lock = threading.Lock()
        # Has any range read EVER reported a valid distance? (See ReactiveTickCore:
        # drives startup_blind vs echo_timeout on a blind safety latch.)
        self._ever_valid = False

        # Simulated ultrasonic reading (clear by default).
        self._sensor_distance_m = 10.0
        self._sensor_known = True

    # -- shared-core hook: the simulated range read --------------------------
    def read_range(self) -> Tuple[float, bool]:
        """Return the simulated ultrasonic reading (non-blocking)."""
        return self._sensor_distance_m, self._sensor_known

    # (command_motors / stop_motors intentionally left as the core's no-op
    # defaults: pure simulation has no wheels to drive.)

    # -- camera --------------------------------------------------------------
    def start(self) -> "FakeReactiveBackend":
        """Start the camera capture thread (idempotent)."""
        if not self._camera_started:
            self._camera.start()
            self._camera_started = True
        return self

    def stop(self) -> None:
        """Tear down in safe order: perception WORKER first, then the camera.

        Stopping the worker before the camera means it never reads from a
        torn-down device. The fake has no wheels, so "motors-safe" is a no-op here
        (the shared shutdown ordering worker -> camera -> motors-safe still holds).
        Idempotent.
        """
        self.stop_perception()
        if self._camera_started:
            self._camera.stop()
            self._camera_started = False
        self._close_owned_observer()

    # -- obstacle simulation (test / demo hooks) -----------------------------
    def set_sensor(self, distance_m: float, known: bool = True) -> None:
        """Set the simulated ultrasonic reading."""
        self._sensor_distance_m = distance_m
        self._sensor_known = known

    def trigger_collision(self, distance_m: float = 0.10) -> None:
        """Simulate an obstacle inside the safe-stop threshold."""
        self.set_sensor(distance_m, known=True)

    def clear_obstacle(self, distance_m: float = 10.0) -> None:
        """Simulate a clear path ahead."""
        self.set_sensor(distance_m, known=True)

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
        a no-op push.
        """
        rate = hz or self.tick_hz
        dt = 1.0 / rate
        self.start()
        # Perception (the heavy detector) runs on its OWN worker thread for the
        # whole run, so FOLLOW ticks only READ the latest observation and never
        # block on inference (the async-perception task). Idle until FOLLOW.
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
            self.stop()  # worker -> camera (see stop()); fake has no motors


__all__ = ["FakeReactiveBackend"]
