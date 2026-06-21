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
  * implements **FOLLOW** as real track-by-detection (software-spec.md §4): each
    FOLLOW tick grabs the latest shared-camera frame, runs a
    :class:`~yalp.reactive.person_tracker.PersonTracker`, steers toward the person
    (turn from the bbox's horizontal error; forward from the bbox-size distance
    proxy, stopping when close enough), publishes the honesty fields, and
    **degrades gracefully** — STOP + "I lost you" when the target is lost or the
    frame is too dark, never driving blindly on a stale box;
  * **owns the camera** (the reactive layer owns the camera, per the contract):
    it holds a single ``yalp.camera.Camera`` for the run — the **real webcam by
    default** (auto-falling back to a synthetic source when no camera can be
    opened, so it still runs headless / in CI). The deliberative
    ``describe_scene`` reads the latest frame from *this* same camera — one
    device, opened once. Headless callers and tests pass an explicit synthetic
    camera or ``camera_source="synthetic"``.

It also provides a ``run`` loop that ticks at a target rate and publishes state
through a ``ReactiveServer``.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Optional, Tuple

from .. import config
from ..camera import Camera
from ..contract.messages import GoalStatus, Intent, Mode, RobotState
from ..contract.ipc import IntentMailbox, ReactiveServer
from .backend import ReactiveBackend

# --- FOLLOW steering / degradation tuning (software-spec.md §2.5/§4) ----------
# Horizontal deadband (fraction of half-frame) inside which we don't turn.
FOLLOW_DEADBAND_X = 0.12
# Proportional gain on the normalized horizontal error -> turn command.
FOLLOW_TURN_GAIN = 1.0
FOLLOW_MAX_TURN = 1.0
# DISTANCE proxy: bbox height as a fraction of frame height. At/above the "stop"
# fraction the person is close enough -> hold; below it, drive forward.
FOLLOW_STOP_BBOX_FRAC = 0.6
FOLLOW_FWD_GAIN = 1.5
FOLLOW_MAX_FORWARD = 1.0
# Tracker confidence below which a box is not trusted for steering.
FOLLOW_TRACK_MIN_SCORE = 0.3
# Coast this many ticks on a lost/unconfident track before STOPping (never drive
# blindly on a stale box — software-spec.md §4 coast-then-stop).
FOLLOW_COAST_TICKS = 3
# Mean-pixel brightness below which we treat the scene as too dark to track
# (the lux-floor proxy / "good light" honesty decision, §5). Below it we STOP
# and report lost rather than steer on a guess.
FOLLOW_DARK_BRIGHTNESS = 10.0


def _frame_brightness(frame: Any) -> float:
    """Mean pixel value of a BGR frame (the lux-floor proxy)."""
    try:
        return float(frame.mean())
    except Exception:  # pragma: no cover - frame is always an ndarray here
        return 255.0


def compute_steering(
    bbox: Tuple[int, int, int, int],
    frame_w: int,
    frame_h: int,
    *,
    deadband: float = FOLLOW_DEADBAND_X,
    turn_gain: float = FOLLOW_TURN_GAIN,
    max_turn: float = FOLLOW_MAX_TURN,
    stop_bbox_frac: float = FOLLOW_STOP_BBOX_FRAC,
    fwd_gain: float = FOLLOW_FWD_GAIN,
    max_forward: float = FOLLOW_MAX_FORWARD,
) -> Tuple[float, float, float, float]:
    """Proportional FOLLOW controller (pure; software-spec.md §4 follow_controller).

    Returns ``(turn, forward, err_x, height_frac)`` where:

      * ``err_x`` is the bbox-center horizontal error vs frame center, normalized
        to ``[-1, 1]`` (``+`` = person right of center).
      * ``turn`` steers toward the person (``+`` = turn right) with a small
        deadband so a centered target doesn't jitter the wheels.
      * ``height_frac`` is the bbox height / frame height (the DISTANCE proxy);
        once it reaches ``stop_bbox_frac`` the person is "close enough" and
        ``forward`` drops to 0 (hold), otherwise we drive proportionally forward.
    """
    x, _y, w, h = bbox
    half = frame_w / 2.0 if frame_w else 1.0
    err_x = (x + w / 2.0 - half) / half  # -1..1, + = right
    if abs(err_x) < deadband:
        turn = 0.0
    else:
        turn = max(-max_turn, min(max_turn, turn_gain * err_x))
    height_frac = (h / float(frame_h)) if frame_h else 0.0
    if height_frac >= stop_bbox_frac:
        forward = 0.0
    else:
        forward = max(0.0, min(max_forward, fwd_gain * (stop_bbox_frac - height_frac)))
    return turn, forward, err_x, height_frac


def _follow_status_line(err_x: float, height_frac: float, turn: float, forward: float) -> str:
    if turn > 0:
        turn_word = "turn right"
    elif turn < 0:
        turn_word = "turn left"
    else:
        turn_word = "centered (hold heading)"
    fwd_word = "forward" if forward > 0 else "close enough (hold)"
    return (f"person at x={err_x:+.2f} -> {turn_word}; "
            f"bbox h={height_frac:.2f} -> {fwd_word}")


class FakeReactiveBackend(ReactiveBackend):
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
        person_tracker: Optional[Any] = None,
        follow_coast_ticks: int = FOLLOW_COAST_TICKS,
        follow_min_score: float = FOLLOW_TRACK_MIN_SCORE,
        follow_dark_brightness: float = FOLLOW_DARK_BRIGHTNESS,
    ) -> None:
        self.mailbox = mailbox or IntentMailbox()
        self.safe_stop_threshold_m = safe_stop_threshold_m
        self.max_speed_mps = max(1e-3, max_speed_mps)
        self.turn_rate_dps = max(1e-3, turn_rate_dps)
        self.tick_hz = max(1.0, tick_hz)

        # FOLLOW: the person tracker (track-by-detection) is injectable so tests
        # can drive steering with a fake detector/tracker. A real PersonTracker is
        # built lazily on the first FOLLOW tick (keeps construction cv2-free).
        self._tracker_obj = person_tracker
        self.follow_coast_ticks = int(follow_coast_ticks)
        self.follow_min_score = float(follow_min_score)
        self.follow_dark_brightness = float(follow_dark_brightness)
        self._follow_turn = 0.0
        self._follow_forward = 0.0
        self._follow_lost_ticks = 0

        # The reactive layer owns ONE camera for the run. Default to the real
        # webcam (Camera auto-falls-back to synthetic if it can't open), so a
        # real `yalp agent` gets real eyes while CI/headless stays hardware-free.
        self._camera = camera if camera is not None else Camera(source=camera_source)
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
        # Fresh FOLLOW: drop any prior motion and force the tracker to re-detect.
        if s.mode == Mode.FOLLOW:
            self._follow_turn = 0.0
            self._follow_forward = 0.0
            self._follow_lost_ticks = 0
            reset = getattr(self._tracker_obj, "reset", None)
            if callable(reset):
                reset()
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

    def _ensure_tracker(self) -> Any:
        """Lazily build the real track-by-detection tracker (cv2 stays lazy)."""
        if self._tracker_obj is None:
            from .person_tracker import PersonTracker

            self._tracker_obj = PersonTracker()
        return self._tracker_obj

    def follow_motion(self) -> Tuple[float, float]:
        """Return the last commanded ``(turn, forward)`` of the FOLLOW loop."""
        return self._follow_turn, self._follow_forward

    def _set_follow_motion(self, turn: float, forward: float) -> None:
        # Clamp commanded speed to the current speed limit (software-spec.md §2.3).
        limit = self._state.speed_limit
        self._follow_turn = max(-limit, min(limit, turn))
        self._follow_forward = max(0.0, min(limit, forward))

    def _step_follow(self) -> None:
        """FOLLOW = track-by-detection steering with graceful degradation (§4).

        Grab the latest shared-camera frame, run the tracker, steer toward the
        person (turn from horizontal error; forward from bbox-size distance proxy)
        and STOP when close enough. Populate the honesty fields every tick and,
        if the person is lost (tracker stale / no detection for K ticks) or the
        frame is too dark, STOP and report lost rather than drive on a stale box.
        Collision-stop / SAFE_STOP already overrode this upstream in ``tick``.
        """
        s = self._state
        target = (s.goal or {}).get("target", "nearest_person")
        frame = self._camera.latest() if self._camera_started else None

        # Graceful degradation: no frame / too dark -> never steer blind (§5).
        if frame is None:
            self._follow_lost(target, "no camera frame")
            return
        if _frame_brightness(frame) < self.follow_dark_brightness:
            self._follow_lost(target, "too dark")
            return

        tracker = self._ensure_tracker()
        result = tracker.update(frame)

        # Publish honesty signals every tick (software-spec.md §2.2/§4).
        s.tracker_score = float(result.score)
        s.ticks_since_last_detector_confirmation = int(
            result.ticks_since_last_detector_confirmation
        )

        confident = (
            result.target_visible
            and result.bbox is not None
            and result.score >= self.follow_min_score
        )
        if not confident:
            self._follow_lost_ticks += 1
            s.target_visible = False
            s.target_bbox = None
            if self._follow_lost_ticks > self.follow_coast_ticks:
                self._follow_lost(target, "target lost")
            else:
                # Coast briefly on the last command (software-spec.md §4), but do
                # not re-affirm visibility — the next confirmation or the stop wins.
                self._set_follow_motion(self._follow_turn, self._follow_forward)
                s.goal_status = GoalStatus.RUNNING
                s.goal = {
                    "target": target,
                    "last_seen_bbox": None,
                    "last_seen_ts": s.ts,
                    "turn": self._follow_turn,
                    "forward": self._follow_forward,
                    "status": (f"tracking unstable -> coasting "
                               f"({self._follow_lost_ticks}/{self.follow_coast_ticks})"),
                    "reason": "coast",
                }
            return

        # Confident track: steer toward the person.
        self._follow_lost_ticks = 0
        h, w = int(frame.shape[0]), int(frame.shape[1])
        bbox = tuple(int(v) for v in result.bbox)  # type: ignore[union-attr]
        turn, forward, err_x, height_frac = compute_steering(bbox, w, h)
        self._set_follow_motion(turn, forward)
        s.target_visible = True
        s.target_bbox = bbox
        s.goal_status = GoalStatus.RUNNING
        s.goal = {
            "target": target,
            "last_seen_bbox": list(bbox),
            "last_seen_ts": s.ts,
            "turn": self._follow_turn,
            "forward": self._follow_forward,
            "status": _follow_status_line(err_x, height_frac, turn, forward),
            "reason": "follow",
        }

    def _follow_lost(self, target: str, reason: str) -> None:
        """Stop the wheels and report a lost target ("I lost you")."""
        s = self._state
        self._set_follow_motion(0.0, 0.0)
        s.target_visible = False
        s.target_bbox = None
        s.tracker_score = 0.0
        s.goal_status = GoalStatus.RUNNING  # still in FOLLOW, just stopped & searching
        s.goal = {
            "target": target,
            "last_seen_bbox": None,
            "last_seen_ts": s.ts,
            "turn": 0.0,
            "forward": 0.0,
            "status": f"lost -> stop (I lost you: {reason})",
            "reason": "lost",
        }

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


__all__ = ["FakeReactiveBackend", "compute_steering"]
