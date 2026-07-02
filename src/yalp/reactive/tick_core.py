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
     what lifts a sticky ``SAFE_STOP``). A mode-changing intent that replaces an
     ACTIVE (``RUNNING``) goal first publishes a one-tick ``PREEMPTED`` transition
     for the outgoing goal, then adopts on the next tick. A **control-only**
     intent (no mode, e.g. ``set_speed_limit``) writes ``RobotState.speed_limit``
     without preempting or changing the mode.
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

FOLLOW perception runs OFF the tick (the async-perception task)
---------------------------------------------------------------
The person detector is far too heavy for the tick path (HOG / MobileNet-SSD is
~100–500 ms per frame on a Pi 5, versus the 33 ms ``TICK_BUDGET_MS`` / 100 ms
``WATCHDOG_TIMEOUT_MS``). So ``tracker.update()`` runs on a
:class:`~yalp.reactive.perception.PerceptionWorker` thread; :meth:`_step_follow`
only reads the worker's freshest :class:`~yalp.reactive.perception.Observation`
**non-blockingly** and feeds it to the controller. The worker starts/stops with
the backend's ``run()`` / ``close()``; before that (unit tests / laptop demos that
drive ``tick()`` directly) the worker pumps synchronously, 1:1 with ticks, so
behavior stays deterministic. See :meth:`_step_follow` for the precise honesty-
signal semantics shift (``ticks_since_last_detector_confirmation`` now measures
observation age in *reactive ticks*).
"""

from __future__ import annotations

import time
from abc import abstractmethod
from typing import TYPE_CHECKING, Optional, Tuple

from .. import config
from ..camera import Camera
from ..contract.messages import (
    GoalStatus,
    Intent,
    Mode,
    RobotState,
    clamp_speed_limit,
)
from .backend import ReactiveBackend
from .follow import FollowDecision, frame_brightness
from .perception import PerceptionWorker

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .person_tracker import TrackResult


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
    ``_lock`` (a ``threading.Lock``), and ``_lost_grace_ticks``. The FOLLOW
    perception worker (``_perception``) and its reactive-tick confirmation clock
    (``_ticks_since_confirmation`` / ``_seen_confirmations``) are created lazily by
    :meth:`_ensure_perception` on the first FOLLOW tick.
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
        """The newest decoded frame from the OWNED camera, or ``None``.

        Single-slot / last-write-wins (a stale frame is fine); never blocks on a
        device read. Read cheaply on the tick for frame geometry + brightness.
        """
        return self._camera.latest() if self._camera_started else None

    def _frame_with_id(self) -> Tuple[object, Optional[int]]:
        """The newest frame AND its camera capture id, non-blocking.

        The perception worker's frame source: it stamps each published
        :class:`~yalp.reactive.perception.Observation` with the id of the frame the
        detector actually saw (diagnostics / frozen-source detection). Reads the
        frame and its id ATOMICALLY when the owned camera exposes ``latest_with_id``
        (the real :class:`~yalp.camera.Camera` does); a test-double camera that only
        implements ``latest()`` yields a ``None`` id. Never blocks on a device read.
        """
        if not self._camera_started:
            return None, None
        getter = getattr(self._camera, "latest_with_id", None)
        if getter is not None:
            return getter()
        return self._camera.latest(), None

    # -- FOLLOW perception worker (heavy detector OFF the tick) --------------
    def _ensure_perception(self) -> PerceptionWorker:
        """Return the FOLLOW perception worker, building it (once) on first use.

        The worker itself is cheap to construct; it defers building the tracker
        (and its OpenCV detector) until it actually runs a FOLLOW cycle, so a
        backend that never follows never pays the CV import cost. Also initializes
        the reactive-tick confirmation clock (see :meth:`_step_follow`).
        """
        worker = getattr(self, "_perception", None)
        if worker is None:
            # Reactive-tick "ticks since the detector last confirmed the box"
            # clock, plus the highest confirmation count we have consumed so far.
            self._ticks_since_confirmation = 0
            self._seen_confirmations = 0
            worker = PerceptionWorker(
                frame_source=self._frame_with_id,
                get_tracker=self._get_tracker,
                is_active=self._perception_active,
            )
            self._perception = worker
        return worker

    def _get_tracker(self):
        """Return the FOLLOW tracker, lazily building the default one.

        Called only from the perception worker (its own thread in async mode, or
        the tick thread in synchronous-pump mode) — never concurrently — so the
        lazy assignment needs no extra lock. Building the real
        :class:`~yalp.reactive.person_tracker.PersonTracker` also lazily builds its
        OpenCV detector, so that cost is paid on the worker, never on the tick.
        """
        if self._tracker is None:
            from .person_tracker import PersonTracker

            self._tracker = PersonTracker(grace_ticks=self._lost_grace_ticks)
        return self._tracker

    def _perception_active(self) -> bool:
        """Whether the worker should run the detector (i.e. we are in FOLLOW).

        Read from the worker thread; a plain attribute read of the mode enum is a
        benign race (at worst one extra/skipped cycle at a mode boundary).
        """
        return self._state.mode == Mode.FOLLOW

    def start_perception(self) -> PerceptionWorker:
        """Build (if needed) and start the async perception worker thread.

        Called by the backend's ``run()`` so the detector runs off-thread for the
        whole session. Idempotent.
        """
        worker = self._ensure_perception()
        worker.start()
        return worker

    def stop_perception(self) -> None:
        """Stop the perception worker thread if one was ever started (idempotent)."""
        worker = getattr(self, "_perception", None)
        if worker is not None:
            worker.stop()

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
            #
            #    PREEMPTION (§2.2): a newly arrived MODE-changing intent that would
            #    replace an ACTIVE, in-progress motion goal (a RUNNING DRIVE_GOAL or
            #    FOLLOW) first surfaces a one-tick PREEMPTED transition for the
            #    outgoing goal — cancel it, halt, and publish PREEMPTED — then adopt
            #    the pending intent on the NEXT tick. This is the only way the
            #    deliberative layer's poller can actually OBSERVE the preemption
            #    (each tick publishes a single snapshot). It never fires while
            #    blocked (step 2 already returned), never for a terminal goal
            #    (COMPLETED/BLOCKED/NONE are not RUNNING — so a sticky SAFE_STOP is
            #    lifted by immediate adoption below, untouched), never for an IDLE
            #    hold (a stop is not a goal to preempt), and never for a control-only
            #    intent (which carries no mode change).
            pending = self.mailbox.peek()
            if (
                pending is not None
                and pending.mode is not None
                and s.mode in (Mode.DRIVE_GOAL, Mode.FOLLOW)
                and s.goal_status == GoalStatus.RUNNING
            ):
                self._halt_motors()
                s.goal_status = GoalStatus.PREEMPTED
                s.goal = {
                    **(s.goal or {}),
                    "reason": "superseded",
                    "preempted_by_seq": int(pending.seq),
                }
                # Leave the pending intent in the mailbox: it is adopted next tick.
                return self._finish()

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
        # A speed-limit rider applies FIRST, clamped into the sane band, so it is
        # in force for THIS adoption's own drive timing/throttle math and every
        # subsequent tick (software-spec.md §2.3).
        if intent.speed_limit is not None:
            s.speed_limit = clamp_speed_limit(intent.speed_limit)
        # A CONTROL-ONLY intent (no mode) carries no motion change: it only
        # adjusts the control value above, leaving the current mode/goal running
        # (so "go slow" clamps the in-progress drive rather than stopping it).
        if intent.mode is None:
            self._safe_notify(self.on_intent_adopted, intent)
            return
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
        """Steer FOLLOW from the LATEST perception observation — stay honest.

        The heavy detector does NOT run here. It runs on the
        :class:`~yalp.reactive.perception.PerceptionWorker`; this tick only reads
        the worker's freshest observation **non-blockingly** (:meth:`poll`), turns
        it into a steering decision (turn toward the person, drive forward until
        close), publishes the spec's honesty signals every tick, and commands the
        wheels (a lost/stale target maps to a clean ``(0, 0)`` stop). Collision-stop
        / ``SAFE_STOP`` already overrode this upstream (§2.3).

        Honesty-signal semantics shift (async-perception task)
        ------------------------------------------------------
        Previously ``tracker.update()`` ran once per tick, so the published
        ``ticks_since_last_detector_confirmation`` was the tracker's own per-tick
        counter. Now the detector runs on the worker at its OWN (slower, variable)
        cadence, so the tracker's counter is in worker-cycle units — the wrong
        clock for the controller's stale gate. Instead we recompute the signal here
        in **reactive-tick units**: a monotonic per-tick counter
        (``_ticks_since_confirmation``) that resets to 0 whenever a NEW detector
        confirmation appears in the observation stream (tracked via the
        observation's cumulative ``confirmations`` count, so a tick can never *miss*
        a confirmation even if it samples slower than the worker publishes).

        This keeps the stale / lost / dark degradation identical to before from the
        controller's and agent's perspective: ``FollowController``'s stale gate
        (``ticks_since_last_detector_confirmation > coast_ticks``) is compared
        against ``config.lost_grace_ticks(tick_hz)`` — also reactive ticks — so the
        ~0.9 s real-world grace window is preserved regardless of detector latency.
        Concretely: a fresh confirmation -> counter 0 -> steer a visible box; a live
        box that goes un-reconfirmed for longer than the grace -> counter exceeds it
        -> ``'stale'`` clean stop; no box / weak score -> ``'lost'``; a dark frame
        (brightness read live below) -> ``'dark'``. A **crashed/stalled worker**
        publishes nothing new, so the counter simply climbs every tick and the
        controller degrades to ``'stale'`` (last box present) or ``'lost'`` — the
        tick itself never blocks or throws.
        """
        s = self._state
        s.goal_status = GoalStatus.RUNNING

        worker = self._ensure_perception()

        # Read the freshest observation NON-BLOCKINGLY. In async mode this is a
        # cheap mailbox read (the detector is on the worker thread); before the
        # worker is started it pumps one cycle inline (deterministic tests / demos).
        observation = worker.poll()

        # Advance the reactive-tick confirmation clock, resetting it when a new
        # detector confirmation has landed (see the docstring for why this — not the
        # tracker's own counter — is what the controller must see).
        self._ticks_since_confirmation += 1
        fresh_confirmation = False
        if (
            observation is not None
            and observation.confirmations > self._seen_confirmations
        ):
            self._seen_confirmations = observation.confirmations
            self._ticks_since_confirmation = 0
            fresh_confirmation = True

        # Frame geometry + brightness are read live on the TICK from the OWNED
        # camera — both are O(pixels) numpy reads (sub-millisecond), NOT the
        # detector. Reading brightness here keeps the dark-degradation truthful to
        # the CURRENT frame even if the worker has stalled/crashed.
        frame = self._latest_frame()
        if frame is not None:
            fh, fw = frame.shape[:2]
            brightness = frame_brightness(frame)
        else:
            fw = int(getattr(self._camera, "width", 640) or 640)
            fh = int(getattr(self._camera, "height", 480) or 480)
            brightness = 0.0  # no frame -> degrade to "lost" via the dark/lost path

        # Rebuild the TrackResult the controller consumes from the latest
        # observation, substituting the REACTIVE-TICK confirmation age for the
        # tracker's worker-cadence counter.
        bbox = observation.bbox if observation is not None else None
        result = self._observation_to_result(
            observation, self._ticks_since_confirmation, fresh_confirmation
        )

        decision = self._follow.decide(result, fw, fh, brightness)
        self.last_follow_decision = decision

        # Publish honesty signals every tick (software-spec.md §2.2 / §2.4).
        s.target_visible = decision.target_visible
        s.target_bbox = (
            tuple(bbox) if (decision.target_visible and bbox is not None) else None
        )
        s.tracker_score = float(result.score)
        s.ticks_since_last_detector_confirmation = int(self._ticks_since_confirmation)

        # Record the motion + last-seen box on the goal payload (the FOLLOW goal is
        # {target, last_seen_bbox, last_seen_ts}, §2.2). When the target is lost the
        # commanded turn/forward are both 0 — a clean stop.
        goal = dict(s.goal) if s.goal else {"target": "nearest_person"}
        goal.update(
            {
                "last_seen_bbox": list(bbox) if bbox is not None else None,
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

    @staticmethod
    def _observation_to_result(
        observation, ticks_since_confirmation: int, fresh_confirmation: bool
    ) -> "TrackResult":
        """Adapt a perception :class:`Observation` into a controller ``TrackResult``.

        ``ticks_since_last_detector_confirmation`` is the caller's REACTIVE-TICK age
        (not the observation's own worker-cadence counter). A missing observation
        (worker still warming up, or none ever produced) reads as a clean "lost".
        """
        from .person_tracker import TrackResult

        if observation is None:
            return TrackResult(False, None, 0.0, ticks_since_confirmation, False)
        return TrackResult(
            bool(observation.target_visible),
            observation.bbox,
            float(observation.score),
            ticks_since_confirmation,
            fresh_confirmation,
        )

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
