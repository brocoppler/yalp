"""Real (on-Pi) reactive backend — the production counterpart of the fake.

``RealReactiveBackend`` is the hardware-driving twin of
:class:`~yalp.reactive.fake_backend.FakeReactiveBackend`. It honors the **exact
same tick contract** (software-spec.md §2.3) — the contract tests assert this
ordering is byte-for-byte faithful — but instead of simulating motion and a
sensor it drives real GPIO behind the two narrow interfaces from
:mod:`yalp.reactive.hardware`:

  * a :class:`~yalp.reactive.hardware.MotorDriver` (left/right signed throttles),
  * a :class:`~yalp.reactive.hardware.RangeSensor` (forward distance; an unknown
    reading biases to STOP — never decayed to "clear").

The FOLLOW path **reuses** the same :class:`~yalp.reactive.follow.FollowController`
and lazily-built :class:`~yalp.reactive.person_tracker.PersonTracker` as the fake,
and the open-loop ``DRIVE_GOAL`` timing is identical — the only difference is that
the steering/throttle decisions are translated into real motor PWM (clamped to
``RobotState.speed_limit`` before it reaches a pin).

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
from ..contract.messages import GoalStatus, Intent, Mode, RobotState
from ..contract.ipc import IntentMailbox, ReactiveServer
from .backend import ReactiveBackend
from .follow import FollowController, FollowDecision, frame_brightness


class RealReactiveBackend(ReactiveBackend):
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
        max_speed_mps: float = 0.5,
        turn_rate_dps: float = 120.0,
        tick_hz: float = config.REACTIVE_TICK_HZ,
        tracker: Optional[object] = None,
        follow_controller: Optional[FollowController] = None,
    ) -> None:
        self.mailbox = mailbox or IntentMailbox()
        self.safe_stop_threshold_m = safe_stop_threshold_m
        self.max_speed_mps = max(1e-3, max_speed_mps)
        self.turn_rate_dps = max(1e-3, turn_rate_dps)
        self.tick_hz = max(1.0, tick_hz)

        # --- Hardware drivers (LAZY imports — keep the module laptop-importable).
        # gpiozero/lgpio are only touched when we actually have to BUILD a real
        # driver/sensor; an injected fake skips the import entirely.
        if motor_driver is None or range_sensor is None:
            from .hardware import GpiozeroMotorDriver, GpiozeroUltrasonicSensor

            if motor_driver is None:
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
        self._follow = follow_controller or FollowController()
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

    # -- camera --------------------------------------------------------------
    def start(self) -> "RealReactiveBackend":
        """Start the camera capture thread (idempotent)."""
        if not self._camera_started:
            self._camera.start()
            self._camera_started = True
        return self

    def camera(self) -> Camera:
        return self._camera

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

            # 1. READ SENSORS — the real forward range sensor (rate-capped and
            #    echo-bounded inside the driver, so this stays non-blocking-ish).
            #    An UNKNOWN reading (echo timeout / unavailable) is treated as an
            #    obstacle: bias to STOP, never decay a miss into "clear" (§2.3).
            distance_m, known = self._range_sensor.read_distance()
            s.distance_m = distance_m
            s.distance_known = known
            s.obstacle = (not s.distance_known) or (
                s.distance_m < self.safe_stop_threshold_m
            )

            # Refresh the latest-frame handle (a stale frame is fine).
            frame = self._camera.latest() if self._camera_started else None
            if frame is not None:
                self._frame_id += 1
                s.last_frame_id = f"f-{self._frame_id}"
            s.ts = time.monotonic()

            # 2. SAFETY OVERRIDE — beats everything, every tick. STOP the motors
            #    FIRST (HALT, never open-loop reverse — no rear sensor), then
            #    latch SAFE_STOP/BLOCKED. The mailbox is deliberately NOT drained
            #    here, so a pending intent cannot override the safety stop while
            #    still blocked (sticky).
            if s.obstacle:
                self._motor_driver.stop()
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
                self._motor_driver.stop()
            elif s.mode == Mode.DRIVE_GOAL:
                self._step_drive_goal(dt)
            elif s.mode == Mode.FOLLOW:
                self._step_follow()
                self._command_follow()

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
        a no-op push. On exit the motors are zeroed and the hardware released.
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

    # -- teardown ------------------------------------------------------------
    def stop(self) -> None:
        """Zero the motors and release the driver, range sensor, and camera.

        Idempotent. Named ``stop`` so ``run()``'s ``finally`` (identical to the
        fake's) tears the hardware down on exit; :meth:`close` is an alias.
        """
        self.close()

    def close(self) -> None:
        """Zero the motors and release driver/sensor/camera. Idempotent."""
        if self._closed:
            return
        self._closed = True
        # Zero the wheels FIRST so the robot is stopped before pins are released,
        # then release each resource best-effort (teardown must not raise).
        for action in (
            self._motor_driver.stop,
            self._motor_driver.close,
            self._range_sensor.close,
        ):
            try:
                action()
            except Exception:  # pragma: no cover - best effort during teardown
                pass
        if self._camera_started:
            try:
                self._camera.stop()
            except Exception:  # pragma: no cover - best effort during teardown
                pass
            finally:
                self._camera_started = False

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
        """Advance the timed open-loop drive AND command the wheels.

        The progress/completion bookkeeping is IDENTICAL to the fake (open-loop,
        timed, unverified — there are no encoders). The only addition is the
        physical command: while running, translate the goal into signed wheel
        throttles (clamped to the speed limit) and write them; on completion stop
        the wheels and drop back to IDLE.
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
            self._motor_driver.stop()
        else:
            s.goal_status = GoalStatus.RUNNING
            left, right = self._drive_throttles(s.goal)
            self._motor_driver.set_motors(left, right)

    def _step_follow(self) -> None:
        """Track-by-detection FOLLOW: read the shared camera, steer, stay honest.

        Identical to the fake: each tick grabs the latest frame from the OWNED
        camera, runs the track-by-detection tracker, turns the result into a
        steering decision, and publishes the spec's honesty signals every tick
        (``target_visible`` / ``target_bbox`` / ``tracker_score`` /
        ``ticks_since_last_detector_confirmation``, §2.2). It degrades gracefully:
        if the person is lost/stale or the frame is too dark the decision is a
        clean stop ("I lost you"). Collision-stop already overrode this upstream.
        """
        s = self._state
        s.goal_status = GoalStatus.RUNNING

        if self._tracker is None:
            # Lazy: build the real track-by-detection tracker only when FOLLOW
            # actually runs, so the OpenCV import cost is never paid otherwise.
            from .person_tracker import PersonTracker

            self._tracker = PersonTracker()

        frame = self._camera.latest() if self._camera_started else None
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

        # Record the motion + last-seen box on the goal payload (the FOLLOW goal
        # is {target, last_seen_bbox, last_seen_ts}, §2.2). When the target is
        # lost the commanded turn/forward are both 0 — a clean stop.
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

    def _command_follow(self) -> None:
        """Translate the latest FOLLOW decision into wheel throttles and command.

        ``turn`` (+ = steer right) and ``forward`` are mixed into a differential
        command, each wheel clamped to the speed limit. On a lost target both are
        0, so this issues a clean ``set_motors(0, 0)`` stop.
        """
        decision = self.last_follow_decision
        if decision is None:
            self._motor_driver.set_motors(0.0, 0.0)
            return
        left, right = self._follow_throttles(decision)
        self._motor_driver.set_motors(left, right)

    # -- steering -> signed wheel throttles ----------------------------------
    def _drive_throttles(self, goal: Optional[dict]) -> Tuple[float, float]:
        """Translate a ``DRIVE_GOAL`` into signed ``(left, right)`` throttles.

        ``straight``: both wheels the same sign (forward for a positive target,
        reverse for a negative one). ``rotate``: the wheels oppose (``left=-turn``,
        ``right=+turn``) to spin in place. The PWM magnitude is the commanded
        speed clamped to the active ``speed_limit`` — so the safety-capped speed
        is what actually reaches the pins (software-spec.md §2.3).
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


__all__ = ["RealReactiveBackend"]
