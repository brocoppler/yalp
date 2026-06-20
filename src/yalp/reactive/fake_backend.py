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
  * uses ``yalp.camera.Camera`` (synthetic source by default) so it runs headless.

It also provides a ``run`` loop that ticks at a target rate and publishes state
through a ``ReactiveServer``.
"""

from __future__ import annotations

import threading
import time
from typing import Optional

from .. import config
from ..camera import Camera
from ..contract.messages import GoalStatus, Intent, Mode, RobotState
from ..contract.ipc import IntentMailbox, ReactiveServer
from .backend import ReactiveBackend


class FakeReactiveBackend(ReactiveBackend):
    """A simulated reactive layer that honors the full preemption/safety contract.

    Parameters
    ----------
    camera:
        A ``Camera`` to read frames from; defaults to a synthetic-source camera
        so it runs with no hardware.
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
        mailbox: Optional[IntentMailbox] = None,
        safe_stop_threshold_m: float = config.SAFE_STOP_THRESHOLD_M,
        max_speed_mps: float = 0.5,
        turn_rate_dps: float = 120.0,
        tick_hz: float = config.REACTIVE_TICK_HZ,
    ) -> None:
        self.mailbox = mailbox or IntentMailbox()
        self.safe_stop_threshold_m = safe_stop_threshold_m
        self.max_speed_mps = max(1e-3, max_speed_mps)
        self.turn_rate_dps = max(1e-3, turn_rate_dps)
        self.tick_hz = max(1.0, tick_hz)

        self._camera = camera if camera is not None else Camera(source="synthetic")
        self._camera_started = False

        self._state = RobotState(mode=Mode.IDLE, goal_status=GoalStatus.NONE)
        self._goal_duration_s = 0.0
        self._frame_id = 0
        self._lock = threading.Lock()

        # Simulated ultrasonic reading (clear by default).
        self._sensor_distance_m = 10.0
        self._sensor_known = True

    # -- camera --------------------------------------------------------------
    def start(self) -> "FakeReactiveBackend":
        """Start the camera capture thread (idempotent)."""
        if not self._camera_started:
            self._camera.start()
            self._camera_started = True
        return self

    def stop(self) -> None:
        if self._camera_started:
            self._camera.stop()
            self._camera_started = False

    def camera(self) -> Camera:
        return self._camera

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

    # -- contract ------------------------------------------------------------
    def apply_intent(self, intent: Intent) -> None:
        self.mailbox.put(intent)

    def get_state(self) -> RobotState:
        with self._lock:
            return self._snapshot()

    def tick(self) -> RobotState:
        dt = 1.0 / self.tick_hz
        with self._lock:
            s = self._state

            # 1. READ SENSORS (simulated; non-blocking).
            s.distance_m = self._sensor_distance_m
            s.distance_known = self._sensor_known
            s.obstacle = (not s.distance_known) or (
                s.distance_m < self.safe_stop_threshold_m
            )

            # Refresh the latest-frame handle (a stale frame is fine).
            frame = self._camera.latest() if self._camera_started else None
            if frame is not None:
                self._frame_id += 1
                s.last_frame_id = f"f-{self._frame_id}"
            s.ts = time.monotonic()

            # 2. SAFETY OVERRIDE — beats everything, every tick.
            #    HALT, never open-loop reverse (no rear sensor). A pending intent
            #    in the mailbox is deliberately NOT drained here, so it cannot
            #    override the safety stop while still blocked.
            if s.obstacle:
                s.mode = Mode.SAFE_STOP
                s.goal_status = GoalStatus.BLOCKED
                s.goal = {
                    "reason": "obstacle" if s.distance_known else "echo_timeout",
                    "distance": s.distance_m,
                }
                return self._snapshot()

            # 3. DRAIN SINGLE-SLOT MAILBOX, then adopt (preempt in-progress mode).
            #    Reaching here means the obstacle is clear, so adopting a fresh
            #    intent is also what lifts a sticky SAFE_STOP.
            new = self.mailbox.take()
            if new is not None:
                self._adopt(new)

            # 4. EXECUTE CURRENT MODE (no EXPLORE branch — deliberative sugar).
            if s.mode in (Mode.IDLE, Mode.SAFE_STOP):
                # Wheels stopped. A SAFE_STOP with the obstacle now clear but no
                # fresh intent stays latched (sticky) — that is intentional.
                pass
            elif s.mode == Mode.DRIVE_GOAL:
                self._step_drive_goal(dt)
            elif s.mode == Mode.FOLLOW:
                self._step_follow()

            return self._snapshot()

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
            self.stop()

    # -- internals -----------------------------------------------------------
    def _adopt(self, intent: Intent) -> None:
        s = self._state
        s.mode = intent.mode
        s.goal = dict(intent.goal) if intent.goal else None
        s.goal_status = GoalStatus.RUNNING
        s.goal_elapsed_s = 0.0
        self._goal_duration_s = 0.0
        if s.mode == Mode.DRIVE_GOAL and s.goal is not None:
            self._goal_duration_s = self._drive_duration(s.goal)
            s.goal["progress"] = 0.0
            s.goal["elapsed_s"] = 0.0

    def _drive_duration(self, goal: dict) -> float:
        """Convert a drive/turn target into a timed open-loop duration (s)."""
        kind = goal.get("kind", "straight")
        target = abs(float(goal.get("target", 0.0)))
        # Clamp commanded speed to the current speed limit (software-spec.md §2.3).
        speed = min(float(goal.get("speed", 0.5)), self._state.speed_limit)
        speed = max(1e-3, speed)
        if kind == "rotate":
            return target / (self.turn_rate_dps * speed)
        return target / (self.max_speed_mps * speed)

    def _step_drive_goal(self, dt: float) -> None:
        s = self._state
        s.goal_elapsed_s += dt
        duration = self._goal_duration_s
        if s.goal is not None:
            progress = 1.0 if duration <= 0 else min(1.0, s.goal_elapsed_s / duration)
            s.goal["progress"] = progress
            s.goal["elapsed_s"] = s.goal_elapsed_s
        if s.goal_elapsed_s >= duration:
            # Never a bare "completed": open-loop, timed, unverified (§2.3).
            s.goal_status = GoalStatus.COMPLETED
            s.mode = Mode.IDLE
        else:
            s.goal_status = GoalStatus.RUNNING

    def _step_follow(self) -> None:
        """A minimal follow simulation that publishes the honesty signals (§4)."""
        s = self._state
        s.goal_status = GoalStatus.RUNNING
        s.target_visible = True
        s.target_bbox = (270, 190, 100, 200)
        s.tracker_score = 0.85
        s.ticks_since_last_detector_confirmation = 0

    def _snapshot(self) -> RobotState:
        s = self._state
        return RobotState(
            mode=s.mode,
            goal=dict(s.goal) if s.goal else None,
            goal_status=s.goal_status,
            goal_elapsed_s=s.goal_elapsed_s,
            distance_m=s.distance_m,
            distance_known=s.distance_known,
            obstacle=s.obstacle,
            target_visible=s.target_visible,
            target_bbox=s.target_bbox,
            tracker_score=s.tracker_score,
            ticks_since_last_detector_confirmation=s.ticks_since_last_detector_confirmation,
            last_frame_id=s.last_frame_id,
            speed_limit=s.speed_limit,
            ts=s.ts,
        )


__all__ = ["FakeReactiveBackend"]
