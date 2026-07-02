"""The single shared reactive tick core (the anti-drift seam).

``FakeReactiveBackend`` (laptop simulation) and ``RealReactiveBackend`` (on-Pi
hardware) used to carry *two* hand-kept copies of the reactive tick — the sensor
read, the safety override, the mailbox drain, and the DRIVE_GOAL / FOLLOW stepping
(~150 lines). Every safety fix had to land twice, and the copies had already
drifted once. That is exactly the failure mode this module removes: the contract
ordering lives here, in ONE place, so simulation and hardware can never fork.

Design (justification for a separate module rather than folding this into
``backend.py``): ``ReactiveBackend`` in ``backend.py`` stays a *pure abstract
interface* (the hardware-abstraction seam, importing only the message types).
This module holds the shared *implementation* — which needs ``config``, ``time``,
and the ``follow`` steering layer (numpy) — as a concrete base
:class:`ReactiveTickCore`. Both backends subclass it. Interface and implementation
stay cleanly layered, and the fake stays dependency-free (stdlib + numpy only:
``person_tracker`` / OpenCV are still imported lazily inside the FOLLOW step).

The contract ordering (software-spec.md §2.3), implemented once in :meth:`tick`:

  1. **Read range** (non-blocking); an UNKNOWN reading biases to STOP.
  2. **Safety override FIRST** — collision-stop beats everything. Halt the motors,
     latch a **sticky** ``SAFE_STOP`` / ``BLOCKED``, and **do NOT drain the
     mailbox** while blocked (a pending intent cannot override a live safety stop).
  3. **Drain the single-slot mailbox** and adopt the newest intent (preempts the
     current mode; reaching here means the obstacle is clear, so adopting is also
     what lifts a sticky ``SAFE_STOP``).
  4. **Step the current mode** (IDLE/SAFE_STOP hold stopped; DRIVE_GOAL advances
     the timed open-loop guess; FOLLOW steers track-by-detection).

Backend-specific behavior sits behind small hooks: :meth:`read_range`,
:meth:`command_motors`, :meth:`stop_motors`, and the shared camera/frame access
(:meth:`camera` / :meth:`_latest_frame`). The fake leaves ``command_motors`` /
``stop_motors`` as no-ops (there are no wheels to drive); the real backend maps
them onto its motor driver.

Observer seam (:meth:`on_intent_adopted` / :meth:`on_motor_command` /
:meth:`on_tick_complete`): no-op by default, invoked through :meth:`_safe_notify`
so a misbehaving observer can NEVER break a tick (a later telemetry task consumes
these — nothing else here depends on them).
"""

from __future__ import annotations

import time
from abc import abstractmethod
from typing import Optional, Tuple

from .. import config
from ..camera import Camera
from ..contract.messages import GoalStatus, Intent, Mode, RobotState
from .backend import ReactiveBackend
from .follow import FollowDecision, frame_brightness


class ReactiveTickCore(ReactiveBackend):
    """Shared reactive tick implementation behind small hardware hooks.

    Subclasses (:class:`~yalp.reactive.fake_backend.FakeReactiveBackend`,
    :class:`~yalp.reactive.real_backend.RealReactiveBackend`) construct the shared
    state in their own ``__init__`` and implement :meth:`read_range` (and, for the
    real backend, :meth:`command_motors` / :meth:`stop_motors`).

    Required instance attributes (set by the subclass ``__init__``): ``mailbox``,
    ``safe_stop_threshold_m``, ``max_speed_mps``, ``turn_rate_dps``, ``tick_hz``,
    ``_tracker``, ``_follow``, ``last_follow_decision``, ``_camera``,
    ``_camera_started``, ``_state``, ``_goal_duration_s``, ``_frame_id``,
    ``_lock`` (a ``threading.Lock``), and ``_lost_grace_ticks``.
    """

    # -- backend-specific hooks ---------------------------------------------
    @abstractmethod
    def read_range(self) -> Tuple[float, bool]:
        """Return ``(distance_m, known)`` for the forward range this tick.

        ``known=False`` (echo timeout / sensor unavailable) is treated as an
        obstacle upstream — bias to STOP, never decay a miss into "clear".
        """

    def command_motors(self, left: float, right: float) -> None:
        """Command signed ``(left, right)`` wheel throttles.

        Default: a no-op (pure simulation has no wheels). The real backend maps
        this onto its motor driver.
        """

    def stop_motors(self) -> None:
        """Halt the wheels. Default: a no-op (pure simulation)."""

    # -- observer seam (no-op defaults; exceptions never break a tick) -------
    def on_intent_adopted(self, intent: Intent) -> None:
        """Called after a fresh intent is adopted (preempting the current mode)."""

    def on_motor_command(self, left: float, right: float) -> None:
        """Called with every motor command issued this tick (``(0, 0)`` on a halt)."""

    def on_tick_complete(self, state: RobotState) -> None:
        """Called with the published snapshot at the end of every tick."""

    @staticmethod
    def _safe_notify(callback, *args) -> None:
        """Invoke an observer callback, swallowing any exception it raises.

        A telemetry/observer fault must NEVER break the safety tick, so every
        observer call is funneled through here.
        """
        try:
            callback(*args)
        except Exception:  # pragma: no cover - observers must not break the tick
            pass

    # -- shared camera / frame access ---------------------------------------
    def camera(self) -> Optional[Camera]:
        return self._camera

    def _latest_frame(self):
        """The newest decoded frame from the OWNED camera, or ``None``."""
        return self._camera.latest() if self._camera_started else None

    # -- contract ------------------------------------------------------------
    def apply_intent(self, intent: Intent) -> None:
        self.mailbox.put(intent)

    def get_state(self) -> RobotState:
        with self._lock:
            return self._snapshot()

    # -- motor dispatch (hook + observer, in one place) ----------------------
    def _drive_motors(self, left: float, right: float) -> None:
        self.command_motors(left, right)
        self._safe_notify(self.on_motor_command, left, right)

    def _halt_motors(self) -> None:
        self.stop_motors()
        self._safe_notify(self.on_motor_command, 0.0, 0.0)

    # -- the one true tick ---------------------------------------------------
    def tick(self) -> RobotState:
        dt = 1.0 / self.tick_hz
        with self._lock:
            s = self._state

            # 1. READ SENSORS (non-blocking). An UNKNOWN reading biases to STOP:
            #    never decay a missed echo into "clear" (§2.3).
            distance_m, known = self.read_range()
            s.distance_m = distance_m
            s.distance_known = known
            s.obstacle = (not s.distance_known) or (
                s.distance_m < self.safe_stop_threshold_m
            )

            # Refresh the latest-frame handle (a stale frame is fine).
            frame = self._latest_frame()
            if frame is not None:
                self._frame_id += 1
                s.last_frame_id = f"f-{self._frame_id}"
            s.ts = time.monotonic()

            # 2. SAFETY OVERRIDE — beats everything, every tick. HALT the motors
            #    FIRST (never open-loop reverse — no rear sensor), then latch
            #    SAFE_STOP/BLOCKED. The mailbox is deliberately NOT drained here, so
            #    a pending intent cannot override the safety stop while still
            #    blocked (sticky).
            if s.obstacle:
                self._halt_motors()
                s.mode = Mode.SAFE_STOP
                s.goal_status = GoalStatus.BLOCKED
                s.goal = {
                    "reason": "obstacle" if s.distance_known else "echo_timeout",
                    "distance": s.distance_m,
                }
                return self._finish()

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
                self._halt_motors()
            elif s.mode == Mode.DRIVE_GOAL:
                self._step_drive_goal(dt)
            elif s.mode == Mode.FOLLOW:
                self._step_follow()

            return self._finish()

    def _finish(self) -> RobotState:
        """Snapshot, fire the tick-complete observer, and return the snapshot."""
        snap = self._snapshot()
        self._safe_notify(self.on_tick_complete, snap)
        return snap

    # -- adoption / drive timing --------------------------------------------
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
        self._safe_notify(self.on_intent_adopted, intent)

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
        """Advance the timed open-loop drive AND command the wheels.

        The progress/completion bookkeeping is open-loop, timed, unverified —
        there are no encoders, so completion is reported as
        ``"completed (timed, unverified)"``, never a bare "completed". While
        running the goal is translated into signed wheel throttles (clamped to the
        speed limit) and commanded; on completion the wheels are stopped and the
        mode drops back to IDLE. In the fake, ``command_motors`` / ``stop_motors``
        are no-ops, so only the bookkeeping is observable.
        """
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
            self._halt_motors()
        else:
            s.goal_status = GoalStatus.RUNNING
            left, right = self._drive_throttles(s.goal)
            self._drive_motors(left, right)

    # -- FOLLOW --------------------------------------------------------------
    def _step_follow(self) -> None:
        """Track-by-detection FOLLOW: read the shared camera, steer, stay honest.

        Each tick grabs the latest frame from the reactive layer's OWNED camera,
        runs the track-by-detection tracker, and turns the result into a steering
        decision (turn toward the person, drive forward until close). It publishes
        the spec's honesty signals every tick (``target_visible`` / ``target_bbox``
        / ``tracker_score`` / ``ticks_since_last_detector_confirmation``, §2.2) and
        degrades gracefully: if the person is lost/stale or the frame is too dark it
        STOPS and reports "I lost you" instead of driving blind on a stale box. The
        resulting decision is finally commanded to the wheels (a lost target maps to
        a clean ``(0, 0)`` stop); collision-stop / ``SAFE_STOP`` already overrode
        this upstream (§2.3).
        """
        s = self._state
        s.goal_status = GoalStatus.RUNNING

        if self._tracker is None:
            # Lazy: build the real track-by-detection tracker (its detector lazily
            # builds the OpenCV backend) only when FOLLOW actually runs, so the
            # OpenCV import cost is never paid by callers that never follow. The
            # tracker's coast/grace budget is the backend's per-tick_hz window.
            from .person_tracker import PersonTracker

            self._tracker = PersonTracker(grace_ticks=self._lost_grace_ticks)

        frame = self._latest_frame()
        result = self._tracker.update(frame)

        if frame is not None:
            fh, fw = frame.shape[:2]
            brightness = frame_brightness(frame)
        else:
            fw = int(getattr(self._camera, "width", 640) or 640)
            fh = int(getattr(self._camera, "height", 480) or 480)
            brightness = 0.0  # no frame -> degrade to "lost" via the dark/lost path

        decision = self._follow.decide(result, fw, fh, brightness)
        self.last_follow_decision = decision

        # Publish honesty signals every tick (software-spec.md §2.2 / §2.4).
        s.target_visible = decision.target_visible
        s.target_bbox = (
            tuple(result.bbox)
            if (decision.target_visible and result.bbox is not None)
            else None
        )
        s.tracker_score = float(result.score)
        s.ticks_since_last_detector_confirmation = int(
            result.ticks_since_last_detector_confirmation
        )

        # Record the motion + last-seen box on the goal payload (the FOLLOW goal is
        # {target, last_seen_bbox, last_seen_ts}, §2.2). When the target is lost the
        # commanded turn/forward are both 0 — a clean stop.
        goal = dict(s.goal) if s.goal else {"target": "nearest_person"}
        goal.update(
            {
                "last_seen_bbox": list(result.bbox) if result.bbox is not None else None,
                "last_seen_ts": s.ts,
                "turn": decision.turn,
                "forward": decision.forward,
                "status": decision.status,
            }
        )
        s.goal = goal

        # Command the steering decision to the wheels (no-op in the fake). A lost
        # target's decision is turn=forward=0, so this issues a clean stop.
        left, right = self._follow_throttles(decision)
        self._drive_motors(left, right)

    # -- steering -> signed wheel throttles ----------------------------------
    def _drive_throttles(self, goal: Optional[dict]) -> Tuple[float, float]:
        """Translate a ``DRIVE_GOAL`` into signed ``(left, right)`` throttles.

        ``straight``: both wheels the same sign (forward for a positive target,
        reverse for a negative one). ``rotate``: the wheels oppose (``left=-turn``,
        ``right=+turn``) to spin in place. The PWM magnitude is the commanded speed
        clamped to the active ``speed_limit`` — so the safety-capped speed is what
        actually reaches the pins (software-spec.md §2.3).
        """
        if not goal:
            return (0.0, 0.0)
        kind = str(goal.get("kind", "straight"))
        target = float(goal.get("target", 0.0))
        speed = float(goal.get("speed", 0.5))

        if target > 0:
            direction = 1.0
        elif target < 0:
            direction = -1.0
        else:
            direction = 0.0
        # Magnitude is the commanded speed clamped into [0, speed_limit].
        magnitude = self._clamp_to_limit(abs(speed))
        drive = direction * magnitude

        if kind == "rotate":
            return (-drive, drive)  # spin in place (left=-turn, right=+turn)
        return (drive, drive)  # straight: both wheels the same direction

    def _follow_throttles(self, decision: FollowDecision) -> Tuple[float, float]:
        """Mix a FOLLOW decision into clamped signed ``(left, right)`` throttles.

        ``forward`` drives both wheels; ``turn`` (+ = steer right) biases them
        apart. Each wheel is clamped to ``±speed_limit`` before it reaches PWM.
        """
        forward = float(decision.forward)
        turn = float(decision.turn)
        left = self._clamp_to_limit(forward + turn)
        right = self._clamp_to_limit(forward - turn)
        return (left, right)

    def _clamp_to_limit(self, value: float) -> float:
        """Clamp a signed throttle to ``±speed_limit`` (software-spec.md §2.3)."""
        limit = float(self._state.speed_limit)
        if value > limit:
            return limit
        if value < -limit:
            return -limit
        return float(value)

    # -- snapshot ------------------------------------------------------------
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


__all__ = ["ReactiveTickCore"]
