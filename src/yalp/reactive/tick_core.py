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
     ``goal["reason"]`` distinguishes THREE blind/blocked causes so a client need
     not guess: ``obstacle`` (a KNOWN reading inside the stop threshold);
     ``echo_timeout`` (an UNKNOWN reading *after* the sensor has ever produced a
     valid read — she went blind mid-run, a sensor dropout); and ``startup_blind``
     (an UNKNOWN reading *before any* valid read has EVER landed — a cold-boot
     blind latch, so it is safe to send an intent). The distinction is a goal-dict
     string only: additive and wire-compatible — the ``RobotState`` schema is
     unchanged.
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

import logging
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
from .visual_odometry import YawEstimator

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .person_tracker import TrackResult

logger = logging.getLogger(__name__)


class ReactiveTickCore(ReactiveBackend):
    """Shared reactive tick implementation behind small hardware hooks.

    Subclasses (:class:`~yalp.reactive.fake_backend.FakeReactiveBackend`,
    :class:`~yalp.reactive.real_backend.RealReactiveBackend`) construct the shared
    state in their own ``__init__`` and implement :meth:`read_range` (and, for the
    real backend, :meth:`command_motors` / :meth:`stop_motors`).

    Required instance attributes (set by the subclass ``__init__``): ``mailbox``,
    ``safe_stop_threshold_m``, ``max_speed_mps``, ``turn_rate_dps``,
    ``duty_deadband``, ``turn_duty_deadband``, ``tick_hz``,
    ``_tracker``, ``_follow``, ``last_follow_decision``, ``_camera``,
    ``_camera_started``, ``_state``, ``_goal_duration_s``, ``_frame_id``,
    ``_lock`` (a ``threading.Lock``), and ``_lost_grace_ticks``. The FOLLOW
    perception worker (``_perception``) and its reactive-tick confirmation clock
    (``_ticks_since_confirmation`` / ``_seen_confirmations``) are created lazily by
    :meth:`_ensure_perception` on the first FOLLOW tick.

    :attr:`_ever_valid` (a core-level ``bool``, class-default ``False``, also set
    explicitly in each subclass ``__init__``) latches ``True`` the first time a
    range read reports ``distance_known=True`` — the ONE piece of state that tells
    a cold-boot blind latch (``startup_blind``) apart from a mid-run sensor dropout
    (``echo_timeout``) in :meth:`tick`. It reads the ``known`` flag off
    :meth:`read_range` directly, NOT :meth:`read_range_stats` (which is ``None`` on
    some backends).
    """

    #: Has any range read EVER reported a valid (``known=True``) distance? Latched
    #: ``True`` in step 1 of :meth:`tick`; drives the ``startup_blind`` vs
    #: ``echo_timeout`` goal reason in step 2. Class-level default so the safety
    #: tick never ``AttributeError``s even for a subclass that forgets to init it.
    _ever_valid: bool = False

    #: Open-loop speed-model deadband defaults — class-level so the drive-timing
    #: math never ``AttributeError``s for a subclass/test double that skips them
    #: (both subclass ``__init__``s set them explicitly). See
    #: :meth:`_model_speed_mps` and :mod:`yalp.config`.
    duty_deadband: float = config.DRIVE_DUTY_DEADBAND
    turn_duty_deadband: float = config.TURN_DUTY_DEADBAND

    #: Visual heading hold (camera yaw estimate; see visual_odometry.py and the
    #: HEADING_HOLD_* notes in :mod:`yalp.config`). Class-level defaults are
    #: DISABLED / no estimator so every existing subclass and test double keeps
    #: the pure open-loop ``(drive, drive)`` split unless it opts in. The real
    #: backend opts in from config when it has a real camera.
    heading_hold_enabled: bool = False
    heading_hold_gain: float = config.HEADING_HOLD_GAIN
    heading_hold_kd: float = config.HEADING_HOLD_KD
    heading_hold_max: float = config.HEADING_HOLD_MAX
    heading_hold_min_response: float = config.HEADING_HOLD_MIN_RESPONSE
    heading_hold_blind_ticks: int = config.HEADING_HOLD_BLIND_TICKS
    rotate_closed_loop: bool = False
    rotate_timeout_factor: float = config.ROTATE_TIMEOUT_FACTOR
    rotate_stop_lead_s: float = config.ROTATE_STOP_LEAD_S
    #: Learned feed-forward steering bias (duty; + = push left wheel harder),
    #: forward and reverse. Mirrors MotorCalibration.straight_bias_*.
    straight_bias_fwd: float = 0.0
    straight_bias_rev: float = 0.0
    trim_learning_enabled: bool = False
    trim_learning_rate: float = config.TRIM_LEARNING_RATE
    _yaw: Optional[YawEstimator] = None
    #: Integrated visual heading since the current goal was adopted (deg, + = LEFT).
    _heading_deg: float = 0.0
    #: Latest visual yaw rate (deg/s, + = LEFT); 0.0 while blind.
    _yaw_rate_dps: float = 0.0
    _heading_blind_ticks: int = 10**9  # blind until the first accepted sample
    _heading_last_fid: Optional[int] = None
    _heading_last_ts: float = 0.0
    _heading_corr_sum: float = 0.0
    _heading_corr_n: int = 0

    #: Optional closed-loop sensors (2026-09-16). ``_imu`` (read() -> sample with
    #: ``yaw_rate_dps``/``ok``/``ts``) is the PRIMARY heading source when live —
    #: no camera latency, works in the dark and with people in frame; the camera
    #: estimator stays the fallback. ``_encoders`` (read() -> sample with
    #: ``distance_m``/``left_mps``/``right_mps``/``ok``) closes the DISTANCE loop
    #: on straight goals. ``_power`` (read() -> ``bus_voltage_v``/``current_a``/
    #: ``ok``) is sampled slowly into the published ``sensors`` sub-map.
    _imu: Optional[object] = None
    _imu_last_ts: Optional[float] = None
    _imu_ok: bool = False
    _encoders: Optional[object] = None
    _odometry_m: float = 0.0
    _odometry_ok: bool = False
    _odometry_start_m: float = 0.0
    odometry_closed_loop: bool = False
    odometry_timeout_factor: float = config.ODOMETRY_TIMEOUT_FACTOR
    _power: Optional[object] = None
    _power_next_ts: float = 0.0
    _power_sample: Optional[dict] = None
    _pack_warned: bool = False
    power_sample_hz: float = config.POWER_MONITOR_SAMPLE_HZ
    pack_low_voltage_v: float = config.PACK_LOW_VOLTAGE_V
    heading_source: str = "none"

    # -- backend-specific hooks ---------------------------------------------
    @abstractmethod
    def read_range(self) -> Tuple[float, bool]:
        """Return ``(distance_m, known)`` for the forward range this tick.

        ``known=False`` (echo timeout / sensor unavailable) is treated as an
        obstacle upstream — bias to STOP, never decay a miss into "clear".
        """

    def read_range_stats(self) -> Optional[dict]:
        """Return cumulative range-sensor observability counters, or ``None``.

        A small read-only sub-map of ints (``total_reads`` / ``valid_reads`` /
        ``raw_misses`` / ``coasted_reads`` / ``unknown_served``) published into the
        :class:`~yalp.contract.messages.RobotState` snapshot under ``ultrasonic`` so
        a state poll / telemetry record shows the TRUE miss rate — including the
        grace-coasted misses that ``distance_known`` alone hides.

        Default: ``None`` (pure simulation / a sensor with no counters). The real
        backend forwards its ultrasonic sensor's ``stats()`` when available. Read on
        the tick right after :meth:`read_range`, so the counters are fresh for this
        tick; it must be cheap and must never raise (biased to ``None`` on error).
        """
        return None

    def command_motors(self, left: float, right: float) -> None:
        """Command signed ``(left, right)`` wheel throttles.

        Default: a no-op (pure simulation has no wheels). The real backend maps
        this onto its motor driver.
        """

    def on_bias_learned(self, forward: bool, bias: float) -> None:
        """Hook: a straight drive completed and the learned bias moved.

        The real backend persists it to the calibration file; the default is a
        no-op (pure simulation has nothing to remember).
        """
        return None

    def stop_motors(self) -> None:
        """Halt the wheels. Default: a no-op (pure simulation)."""

    # -- observer seam -------------------------------------------------------
    # The default hooks forward to an INJECTED observer (``self._observer``, set by
    # the subclass ``__init__``; ``None`` = no observer). Injection — not
    # subclassing — is the extension point, so tests and library users can pass
    # their own recorder or disable it (the telemetry-logging task's consumer). All
    # three are invoked through :meth:`_safe_notify`, so an observer that raises can
    # NEVER break the safety tick.
    def on_intent_adopted(self, intent: Intent) -> None:
        """Forward a freshly-adopted intent to the injected observer (if any)."""
        observer = getattr(self, "_observer", None)
        if observer is not None:
            observer.on_intent_adopted(intent)

    def on_motor_command(self, left: float, right: float) -> None:
        """Forward every motor command (``(0, 0)`` on a halt) to the observer."""
        observer = getattr(self, "_observer", None)
        if observer is not None:
            observer.on_motor_command(left, right)

    def on_tick_complete(self, state: RobotState) -> None:
        """Forward the published end-of-tick snapshot to the observer."""
        observer = getattr(self, "_observer", None)
        if observer is not None:
            observer.on_tick_complete(state)

    def _close_owned_observer(self) -> None:
        """Close the observer IFF this backend owns its lifecycle (best-effort).

        A run loop that auto-creates the recorder sets ``close_observer=True`` so
        teardown flushes it; an injected, caller-owned observer is left untouched.
        Never raises during teardown.
        """
        if not getattr(self, "_close_observer", False):
            return
        observer = getattr(self, "_observer", None)
        close = getattr(observer, "close", None)
        if close is None:
            return
        try:
            close()
        except Exception:  # pragma: no cover - teardown must not raise
            pass

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
            if known:
                # Latch that the sensor has produced at least one valid reading
                # this session, so a later blind latch can be told apart from a
                # never-yet-sighted cold boot (echo_timeout vs startup_blind in
                # step 2). Read off the ``known`` flag directly — NOT
                # read_range_stats(), which is None on some backends.
                self._ever_valid = True
            s.obstacle = (not s.distance_known) or (
                s.distance_m < self.safe_stop_threshold_m
            )
            # Fresh cumulative range-sensor counters for observability (grace-coast
            # / raw-miss tallies invisible to ``distance_known``). Captured right
            # after the read so the sub-map reflects THIS tick's sample.
            s.ultrasonic = self.read_range_stats()

            # Refresh the latest-frame handle (a stale frame is fine).
            frame, capture_id = self._frame_with_id()
            if frame is not None:
                self._frame_id += 1
                s.last_frame_id = f"f-{self._frame_id}"
            s.ts = time.monotonic()
            # Heading — the IMU when fitted and answering, else the camera yaw
            # (phase correlation on a small frame) — runs every tick, on every
            # path, so the estimate is live the moment a goal needs it. Then the
            # wheel odometry and the (slow) pack monitor, and the published
            # ``sensors`` sub-map.
            self._update_heading(frame, capture_id, s.ts)
            self._update_odometry()
            self._update_power(s.ts)
            s.sensors = self._sensors_snapshot()

            # 2. SAFETY OVERRIDE — beats everything, every tick. HALT the motors
            #    FIRST (never open-loop reverse — no rear sensor), then latch
            #    SAFE_STOP/BLOCKED. The mailbox is deliberately NOT drained here, so
            #    a pending intent cannot override the safety stop while still
            #    blocked (sticky).
            if s.obstacle and not self._escape_permitted(s):
                prev_status, prev_mode = s.goal_status, s.mode
                self._halt_motors()
                # A straight drive cut short by the reflex still taught us how
                # much steering it needed: fold that in (no-op below the sample
                # floor / when learning is off) BEFORE the goal dict is replaced.
                if s.mode == Mode.DRIVE_GOAL and s.goal_status == GoalStatus.RUNNING:
                    self._learn_straight_bias(s.goal or {})
                s.mode = Mode.SAFE_STOP
                s.goal_status = GoalStatus.BLOCKED
                # A KNOWN close reading is an ``obstacle``. An UNKNOWN reading is a
                # blind latch — distinguish a cold-boot blindness we have never
                # sighted past (``startup_blind``, safe to send an intent) from a
                # mid-run sensor dropout (``echo_timeout``, she went blind). This is
                # a goal-dict string only: additive/wire-compatible (§2.3).
                if s.distance_known:
                    reason = "obstacle"
                elif self._ever_valid:
                    reason = "echo_timeout"
                else:
                    reason = "startup_blind"
                latched = {
                    "reason": reason,
                    "distance": s.distance_m,
                }
                # An escape goal (rotate / reverse) that COMPLETED last tick with
                # the obstacle still in view re-latches here. Keep the evidence
                # that it finished so a client polling at 5 Hz (which will miss
                # the one-tick COMPLETED state) can tell "escape done, latch
                # re-armed" apart from "the reflex stopped it".
                prev = s.goal or {}
                if prev_status == GoalStatus.COMPLETED and prev_mode == Mode.IDLE:
                    latched["after"] = GoalStatus.COMPLETED
                    for k in ("kind", "target", "closure", "heading_deg"):
                        if k in prev:
                            latched[k] = prev[k]
                s.goal = latched
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
                if s.mode == Mode.DRIVE_GOAL:
                    self._learn_straight_bias(s.goal or {})  # partial drive still teaches
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

    @staticmethod
    def _goal_is_escape(goal: Optional[dict]) -> bool:
        """A goal that never moves the nose FORWARD: a rotate, or a reverse straight."""
        if not goal:
            return False
        kind = str(goal.get("kind", "straight"))
        if kind == "rotate":
            return True
        return kind == "straight" and float(goal.get("target", 0.0)) < 0

    def _escape_permitted(self, s: RobotState) -> bool:
        """Whether this tick may run despite a KNOWN obstacle inside the threshold.

        software-spec.md §2.3: the reflex halts forward motion, but recovery is
        an explicit NEW intent — "a turn, or a short reverse the operator
        explicitly asks for". Without this rule a robot that stopped 0.25 m from
        a wall could never move again under its own control (every goal was
        refused until someone lifted her). The rule is deliberately narrow:

        * the obstacle reading must be KNOWN (a blind sensor still latches — a
          reverse or a pivot while blind is driving blind), and
        * the goal that would run this tick — the pending mailbox intent if there
          is one, else the goal already RUNNING — must be an *escape* goal: a
          ``rotate``, or a ``straight`` with a NEGATIVE target. A pending FORWARD
          intent keeps the latch sticky exactly as before (the mailbox is not
          drained), and a running forward drive is still halted.

        The published ``obstacle`` / ``distance_m`` stay truthful throughout, and
        the moment the escape goal ends the ordinary latch returns.
        """
        if not s.distance_known:
            return False
        pending = self.mailbox.peek()
        if pending is not None:
            return pending.mode == Mode.DRIVE_GOAL and self._goal_is_escape(pending.goal)
        return (
            s.mode == Mode.DRIVE_GOAL
            and s.goal_status == GoalStatus.RUNNING
            and self._goal_is_escape(s.goal)
        )

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
            self._warn_if_below_deadband(s.goal)
            s.goal["progress"] = 0.0
            s.goal["elapsed_s"] = 0.0
            # Heading is measured RELATIVE to the pose at adoption: zero the
            # integrator here (the estimator keeps its reference frame so the
            # very next frame pair already yields a sample).
            self._heading_deg = 0.0
            self._heading_corr_sum = 0.0
            self._heading_corr_n = 0
            s.goal["closure"] = "timed"
            self._odometry_start_m = self._odometry_m
            if self._straight_is_closed_loop(s.goal):
                # Closed-loop DISTANCE: the encoders end the goal; the timer is a
                # generous upper bound (the open-loop speed model has been ~2x off).
                self._goal_duration_s *= max(1.0, float(self.odometry_timeout_factor))
            if self._rotate_is_closed_loop(s.goal):
                # Closed-loop turn: the visual yaw integral ends the goal; the
                # timer becomes a generous upper bound so a blind estimator can
                # never hang it (the open-loop turn model is documented as being
                # wrong by 5-10x below duty 0.5, so a tight bound would cut real
                # turns short).
                self._goal_duration_s *= max(1.0, float(self.rotate_timeout_factor))
        self._safe_notify(self.on_intent_adopted, intent)

    def _model_speed_mps(self, duty: float) -> float:
        """Open-loop forward speed (m/s) for a commanded ``duty`` (motor deadband).

        The model is ``v = max(0, gain * (duty - duty_deadband))`` with
        ``gain = max_speed_mps / (1 - duty_deadband)``. Parameterisation choice
        (2026-07-17 field tuning, documented in :mod:`yalp.config`):

        * ``max_speed_mps`` KEEPS its meaning — the speed at FULL throttle: at
          ``duty == 1.0`` the deadband term is ``1 - duty_deadband`` and the gain
          cancels it, so ``v == max_speed_mps`` for any deadband. Calibration files
          that only carry ``max_speed_mps`` therefore still mean what they meant.
        * A commanded ``duty`` at/below the deadband predicts **no motion** (0.0):
          the measured stall (duty 0.30 -> 0 displacement) is now in the model.
        * ``duty_deadband == 0`` recovers the exact old linear ``max_speed_mps *
          duty`` — how a pre-deadband calibration file behaves (its missing field
          loads as 0.0), so e.g. ``max_speed_mps 0.29`` still gives duty 0.45 ->
          ~0.13 m/s, matching the field measurement.
        """
        dead = float(self.duty_deadband)
        span = 1.0 - dead
        if span <= 1e-9:  # degenerate deadband -> nothing ever moves
            return 0.0
        gain = self.max_speed_mps / span
        return max(0.0, gain * (float(duty) - dead))

    def _drive_duration(self, goal: dict) -> float:
        """Convert a drive/turn target into a timed open-loop duration (s).

        Straight drives use the deadband speed model (:meth:`_model_speed_mps`): a
        commanded duty at/below the motor deadband models NO motion, so the
        duration is ``+inf`` — the timed goal cannot complete, which is the honest
        open-loop read on a stalled command (no encoders to prove otherwise). The
        raw duty still reaches the pins via :meth:`_drive_throttles`; only the
        *timing* changes. Rotation keeps the linear ``turn_rate_dps`` estimate
        (documented as stiction-over-predicting below ``config.TURN_STICTION_DUTY``
        — see :meth:`_warn_if_below_deadband`; a real turn model needs field data
        we do not yet have).
        """
        kind = goal.get("kind", "straight")
        target = abs(float(goal.get("target", 0.0)))
        if target == 0.0:
            return 0.0  # nothing to travel -> completes immediately (as before)
        # Clamp commanded speed to the current speed limit (software-spec.md §2.3).
        duty = min(float(goal.get("speed", 0.5)), self._state.speed_limit)
        duty = max(0.0, duty)
        if kind == "rotate":
            rate = self.turn_rate_dps * max(1e-3, duty)
            return target / rate if rate > 0 else float("inf")
        v = self._model_speed_mps(duty)
        if v <= 0.0:
            return float("inf")  # commanded below the motor deadband -> stalled
        return target / v

    def _warn_if_below_deadband(self, goal: dict) -> None:
        """Log an honesty warning when a commanded duty likely will not move.

        The open-loop timing has no encoders, so it cannot detect a stall or a
        stiction-shortened turn; it commands the duty and reports a *timed*
        completion. When the commanded (speed-limited) duty is at/below the motor
        deadband — a straight drive that will not overcome stiction, or a rotation
        in the stiction-dominated band where ``turn_rate_dps`` over-predicts by
        ~5-10x — we surface a WARNING so the operator is not misled by a
        "completed (timed, unverified)" the hardware never actually performed.
        """
        kind = str(goal.get("kind", "straight"))
        duty = min(float(goal.get("speed", 0.5)), float(self._state.speed_limit))
        if kind == "rotate":
            if duty < config.TURN_STICTION_DUTY:
                logger.warning(
                    "commanded turn duty %.2f is below the stiction-dominated "
                    "rotation floor %.2f — turn_rate_dps OVER-predicts actual "
                    "rotation by ~5-10x here (measured 2026-07-17: 30-60 deg "
                    "commanded delivered only 2-15 deg); the reported 'completed "
                    "(timed, unverified)' will greatly overstate the true angle.",
                    duty,
                    config.TURN_STICTION_DUTY,
                )
            return
        if duty <= self.duty_deadband:
            logger.warning(
                "commanded duty %.2f is at/below the motor deadband %.2f — the "
                "robot will likely NOT move (measured 2026-07-17: duty 0.30 "
                "produced zero displacement); the open-loop timing cannot detect "
                "the stall, so the drive runs without displacement.",
                duty,
                self.duty_deadband,
            )

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
        goal = s.goal
        visual_done = False
        if goal is not None:
            progress = 1.0 if duration <= 0 else min(1.0, s.goal_elapsed_s / duration)
            odom_done = False
            if self._straight_is_closed_loop(goal) and self._odometry_live():
                target = abs(float(goal.get("target", 0.0)))
                gone = abs(self._odometry_m - self._odometry_start_m)
                if target > 0:
                    progress = max(progress, min(1.0, gone / target))
                odom_done = target > 0 and gone >= target
                goal["odometry_m"] = round(gone, 3)
            if self._rotate_is_closed_loop(goal) and self._heading_live():
                target = abs(float(goal.get("target", 0.0)))
                turned = abs(self._heading_deg)
                if target > 0:
                    progress = max(progress, min(1.0, turned / target))
                # Predictive stop: where the heading will be after the sensing
                # latency + coast-down, only counting rate in the turn's direction.
                sign = 1.0 if float(goal.get("target", 0.0)) >= 0 else -1.0
                ahead = max(0.0, sign * self._yaw_rate_dps) * max(0.0, float(self.rotate_stop_lead_s))
                visual_done = target > 0 and (turned + ahead) >= target
            goal["progress"] = progress
            goal["elapsed_s"] = s.goal_elapsed_s
            self._publish_heading(goal)
        if visual_done or odom_done or s.goal_elapsed_s >= duration:
            # Never a bare "completed": open-loop, timed, unverified (§2.3). A
            # closed-loop turn/drive keeps the SAME contract string (clients
            # match on it) and says how it closed in the additive ``closure``.
            if goal is not None:
                goal["closure"] = "visual" if visual_done else ("odometry" if odom_done else "timed")
            s.goal_status = GoalStatus.COMPLETED
            s.mode = Mode.IDLE
            self._halt_motors()
            if goal is not None and not visual_done:
                self._learn_straight_bias(goal)
        else:
            s.goal_status = GoalStatus.RUNNING
            left, right = self._drive_throttles(goal)
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
        # Straight: both wheels the same direction, plus the heading-hold
        # correction (0.0 unless enabled AND the camera estimate is live) — added
        # to the left wheel and subtracted from the right, so a positive
        # correction steers RIGHT. Skipped entirely for a zero drive.
        if direction == 0.0:
            return (0.0, 0.0)
        corr = self._heading_correction(direction > 0)
        if corr == 0.0:
            return (drive, drive)
        return (self._clamp_to_limit(drive + corr), self._clamp_to_limit(drive - corr))

    # -- visual heading hold ---------------------------------------------------
    def _update_heading(self, frame, capture_id: Optional[int], now: float) -> None:
        """Integrate the camera yaw estimate for this tick (blind-safe).

        A tick is *blind* (no new sample) when there is no estimator, no frame,
        the same capture id as last tick (the camera runs slower than the tick,
        so most ticks see a repeat), an unusable frame, or a low-confidence
        correlation. Blind ticks leave the heading integral untouched — "no new
        information" is NOT "no rotation" — and count up ``_heading_blind_ticks``
        so the controller can drop its closed-loop term after a sustained
        dropout. ``_yaw_rate_dps`` is measured between ACCEPTED frames.
        """
        if self._update_heading_imu(now):
            return
        est = self._yaw
        if est is None or frame is None:
            self._heading_blind_ticks += 1
            self._yaw_rate_dps = 0.0
            self.heading_source = "none"
            return
        if capture_id is not None and capture_id == self._heading_last_fid:
            # Same frame as last tick: nothing new to measure, not a dropout.
            return
        self._heading_last_fid = capture_id
        sample = est.update(frame)
        if sample is None or sample.response < float(self.heading_hold_min_response):
            self._heading_blind_ticks += 1
            self._yaw_rate_dps = 0.0
            return
        self._heading_deg += float(sample.delta_deg)
        gap = now - self._heading_last_ts
        self._heading_last_ts = now
        if self._heading_blind_ticks == 0 and 1e-3 < gap < 1.0:
            self._yaw_rate_dps = float(sample.delta_deg) / gap
        else:
            self._yaw_rate_dps = 0.0  # first sample after a gap: no rate yet
        self._heading_blind_ticks = 0
        self.heading_source = "camera"

    def _update_heading_imu(self, now: float) -> bool:
        """Integrate the gyro if an IMU is fitted and answering. True = applied.

        ``heading += yaw_rate * dt`` between consecutive OK samples (dt capped at
        0.25 s so a stall in the bus cannot inject a huge step). A sample that is
        not OK — or a missing IMU — returns False so the camera path runs. The
        IMU beats the camera whenever it answers: it has no frame latency, no
        looming, and does not care about the light or a person in the frame.
        """
        imu = self._imu
        if imu is None:
            return False
        try:
            smp = imu.read()
        except Exception:
            smp = None
        if smp is None or not getattr(smp, "ok", False):
            self._imu_ok = False
            self._imu_last_ts = None
            return False
        ts = float(getattr(smp, "ts", now))
        rate = float(getattr(smp, "yaw_rate_dps", 0.0))
        if self._imu_last_ts is not None:
            dt = ts - self._imu_last_ts
            if 0.0 < dt <= 0.25:
                self._heading_deg += rate * dt
        self._imu_last_ts = ts
        self._imu_ok = True
        self._yaw_rate_dps = rate
        self._heading_blind_ticks = 0
        self.heading_source = "imu"
        return True

    def _update_odometry(self) -> None:
        enc = self._encoders
        if enc is None:
            return
        try:
            smp = enc.read()
        except Exception:
            self._odometry_ok = False
            return
        ok = bool(getattr(smp, "ok", False))
        self._odometry_ok = ok
        if ok:
            self._odometry_m = float(getattr(smp, "distance_m", self._odometry_m))
            self._last_encoder_sample = smp

    def _odometry_live(self) -> bool:
        return self._encoders is not None and self._odometry_ok

    def _straight_is_closed_loop(self, goal: Optional[dict]) -> bool:
        return (
            bool(self.odometry_closed_loop)
            and self._encoders is not None
            and goal is not None
            and str(goal.get("kind", "straight")) == "straight"
        )

    def _update_power(self, now: float) -> None:
        mon = self._power
        if mon is None or now < self._power_next_ts:
            return
        hz = max(0.1, float(self.power_sample_hz))
        self._power_next_ts = now + 1.0 / hz
        try:
            smp = mon.read()
        except Exception:
            return
        if not getattr(smp, "ok", False):
            self._power_sample = {"pack_ok": False}
            return
        v = float(getattr(smp, "bus_voltage_v", 0.0))
        i = float(getattr(smp, "current_a", 0.0))
        try:
            from .power_monitor import nimh_pack_state

            state = nimh_pack_state(v)
        except Exception:
            state = "unknown"
        self._power_sample = {
            "pack_voltage_v": round(v, 3),
            "pack_current_a": round(i, 3),
            "pack_state": state,
            "pack_ok": True,
        }
        if v < float(self.pack_low_voltage_v) and not self._pack_warned:
            self._pack_warned = True
            logger.warning(
                "pack voltage %.2f V is below %.2f V (%s) — expect speed sag; "
                "the drive is NOT stopped automatically",
                v, float(self.pack_low_voltage_v), state,
            )

    def _sensors_snapshot(self) -> Optional[dict]:
        """The additive ``RobotState.sensors`` sub-map, or None if no source."""
        if self._imu is None and self._yaw is None and self._encoders is None and self._power is None:
            return None
        out: dict = {
            "heading_deg": round(self._heading_deg, 2),
            "yaw_rate_dps": round(self._yaw_rate_dps, 1),
            "heading_source": self.heading_source if self._heading_live() else "none",
        }
        if self._encoders is not None:
            smp = getattr(self, "_last_encoder_sample", None)
            out["odometry_m"] = round(self._odometry_m, 3)
            out["odometry_ok"] = self._odometry_ok
            if smp is not None:
                out["left_mps"] = round(float(getattr(smp, "left_mps", 0.0)), 3)
                out["right_mps"] = round(float(getattr(smp, "right_mps", 0.0)), 3)
        if self._power_sample:
            out.update(self._power_sample)
        return out

    def _heading_live(self) -> bool:
        """Whether the visual heading is trustworthy right now."""
        return (
            (self._yaw is not None or self._imu is not None)
            and self._heading_blind_ticks <= int(self.heading_hold_blind_ticks)
        )

    def _rotate_is_closed_loop(self, goal: Optional[dict]) -> bool:
        return (
            bool(self.rotate_closed_loop)
            and (self._yaw is not None or self._imu is not None)
            and goal is not None
            and str(goal.get("kind", "straight")) == "rotate"
        )

    def _heading_correction(self, forward: bool) -> float:
        """Steering correction (duty, + = steer RIGHT) for a straight drive.

        ``bias + Kp * heading + Kd * yaw_rate``, where ``heading`` is the
        integrated visual yaw since adoption (deg, + = drifted LEFT) and the
        closed-loop part is clamped to ``±heading_hold_max``. The learned
        feed-forward ``bias`` is always applied (it costs nothing and encodes
        the last drives' mean correction); the closed-loop part only while the
        estimate is live and the hold is enabled. Also accumulates the closed-
        loop part for trim learning.
        """
        bias = float(self.straight_bias_fwd if forward else self.straight_bias_rev)
        if not self.heading_hold_enabled or not self._heading_live():
            return bias
        loop = (
            float(self.heading_hold_gain) * self._heading_deg
            + float(self.heading_hold_kd) * self._yaw_rate_dps
        )
        cap = abs(float(self.heading_hold_max))
        loop = max(-cap, min(cap, loop))
        self._heading_corr_sum += loop
        self._heading_corr_n += 1
        return bias + loop

    def _publish_heading(self, goal: dict) -> None:
        """Additive, wire-compatible heading fields on the goal dict."""
        if self._yaw is None and self._imu is None:
            return
        goal["heading_deg"] = round(self._heading_deg, 2)
        goal["yaw_rate_dps"] = round(self._yaw_rate_dps, 1)
        goal["heading_live"] = self._heading_live()
        goal["heading_source"] = self.heading_source if self._heading_live() else "none"

    def _learn_straight_bias(self, goal: dict) -> None:
        """Fold this completed straight drive's mean correction into the bias.

        Only for straight drives that ran with a live heading estimate for a
        meaningful number of ticks; EMA at ``trim_learning_rate``, clamped to
        ``±heading_hold_max``. Fires :meth:`on_bias_learned` so the real backend
        can persist it. Learning a bias that is already applied is stable: the
        closed-loop part converges towards zero as the bias absorbs it.
        """
        if not self.trim_learning_enabled or not self.heading_hold_enabled:
            return
        if str(goal.get("kind", "straight")) != "straight":
            return
        n = self._heading_corr_n
        if n < 10:
            return
        mean = self._heading_corr_sum / n
        forward = float(goal.get("target", 0.0)) >= 0
        old = float(self.straight_bias_fwd if forward else self.straight_bias_rev)
        rate = max(0.0, min(1.0, float(self.trim_learning_rate)))
        cap = abs(float(self.heading_hold_max))
        new = max(-cap, min(cap, old + rate * mean))
        if forward:
            self.straight_bias_fwd = new
        else:
            self.straight_bias_rev = new
        goal["learned_bias"] = round(new, 4)
        if abs(new - old) > 1e-4:
            self._safe_notify(self.on_bias_learned, forward, new)

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
            ultrasonic=dict(s.ultrasonic) if s.ultrasonic is not None else None,
            sensors=dict(s.sensors) if s.sensors is not None else None,
            ts=s.ts,
        )


__all__ = ["ReactiveTickCore"]
