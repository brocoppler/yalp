"""FOLLOW-mode tests — no camera, network, or display (software-spec.md §4).

Steering and degradation are proven with an injected FAKE detector / tracker and a
fake frame source carrying a controllable "person bbox", so the whole
track-by-detection pipeline is exercised with zero hardware:

  * the pure :func:`compute_steering` controller (left -> turn left, centered+far
    -> forward, centered+close -> stop);
  * the :class:`PersonTracker` track-by-detection cadence (detector seeds the box,
    the cheap tracker fills between, the detector re-validates every N ticks);
  * the FOLLOW tick on :class:`FakeReactiveBackend`: it steers via the tracker,
    populates the honesty fields on ``RobotState``, and degrades to a clean STOP
    ("I lost you") on target loss or a too-dark frame — and collision-stop still
    overrides everything.
"""

from __future__ import annotations

import numpy as np

from yalp.contract.messages import GoalStatus, Intent, Mode, RobotState
from yalp.reactive.fake_backend import FakeReactiveBackend, compute_steering
from yalp.reactive.person_tracker import (
    Detection,
    PersonTracker,
    TrackResult,
    best_detection,
)

FRAME_W, FRAME_H = 640, 480


def _bright(w: int = FRAME_W, h: int = FRAME_H, value: int = 200) -> np.ndarray:
    return np.full((h, w, 3), value, dtype=np.uint8)


def _dark(w: int = FRAME_W, h: int = FRAME_H) -> np.ndarray:
    return np.zeros((h, w, 3), dtype=np.uint8)


# --- fakes -------------------------------------------------------------------
class FakeCamera:
    """A minimal stand-in for yalp.camera.Camera returning a fixed frame."""

    def __init__(self, frame, source: str = "fake") -> None:
        self._frame = frame
        self.source = source

    def start(self):
        return self

    def stop(self) -> None:
        pass

    def latest(self):
        return self._frame

    def wait_for_frame(self, timeout: float = 0.0):
        return self._frame


class FakeTracker:
    """Returns a scripted TrackResult every update (counts its calls)."""

    def __init__(self, result: TrackResult) -> None:
        self.result = result
        self.updates = 0

    def reset(self) -> None:
        pass

    def update(self, frame) -> TrackResult:
        self.updates += 1
        return self.result


class StaticDetector:
    """A fake person detector returning a fixed list of detections."""

    def __init__(self, dets) -> None:
        self.dets = list(dets)
        self.calls = 0

    def detect(self, frame):
        self.calls += 1
        return list(self.dets)


class FakeBoxTracker:
    """A fake cheap box tracker that always reports the seeded box."""

    def __init__(self, score: float = 0.8) -> None:
        self.score = score
        self.bbox = (0, 0, 0, 0)
        self.inited = 0
        self.updates = 0

    def init(self, frame, bbox) -> None:
        self.inited += 1
        self.bbox = tuple(int(v) for v in bbox)

    def update(self, frame):
        self.updates += 1
        return True, self.bbox, self.score


def _follow_backend(camera, tracker, **kw):
    backend = FakeReactiveBackend(camera=camera, person_tracker=tracker, tick_hz=50.0, **kw)
    backend.start()
    backend.apply_intent(Intent(Mode.FOLLOW, {"target": "nearest_person"}, seq=1))
    return backend


# --- compute_steering (pure controller) -------------------------------------
def test_steering_person_left_turns_left():
    # bbox centered well left of frame center.
    turn, forward, err_x, _hfrac = compute_steering((40, 100, 100, 200), FRAME_W, FRAME_H)
    assert err_x < 0
    assert turn < 0  # turn LEFT toward the person


def test_steering_person_right_turns_right():
    turn, _forward, err_x, _hfrac = compute_steering((520, 100, 100, 200), FRAME_W, FRAME_H)
    assert err_x > 0
    assert turn > 0  # turn RIGHT toward the person


def test_steering_centered_far_drives_forward():
    # Centered (x≈center) and a SMALL bbox (far away) -> no turn, drive forward.
    bbox = (FRAME_W // 2 - 50, 100, 100, 120)  # h/H = 0.25, below stop frac
    turn, forward, _err, hfrac = compute_steering(bbox, FRAME_W, FRAME_H)
    assert turn == 0.0
    assert forward > 0.0
    assert hfrac < 0.6


def test_steering_centered_close_stops():
    # Centered and a LARGE bbox (close) -> hold (forward 0).
    bbox = (FRAME_W // 2 - 60, 40, 120, 360)  # h/H = 0.75, above stop frac
    turn, forward, _err, hfrac = compute_steering(bbox, FRAME_W, FRAME_H)
    assert turn == 0.0
    assert forward == 0.0
    assert hfrac >= 0.6


# --- PersonTracker track-by-detection cadence -------------------------------
def test_best_detection_picks_largest():
    small = Detection(0, 0, 10, 10, 0.5)
    big = Detection(0, 0, 100, 200, 0.9)
    assert best_detection([small, big]) is big
    assert best_detection([]) is None


def test_tracker_detects_then_tracks_then_redetects():
    frame = _bright()
    det = Detection(100, 100, 80, 200, 0.9)
    detector = StaticDetector([det])
    created = []

    def factory():
        t = FakeBoxTracker()
        created.append(t)
        return t

    pt = PersonTracker(detector=detector, tracker_factory=factory,
                       detect_every=3, min_score=0.0)

    r0 = pt.update(frame)  # no live track -> detector seeds
    assert r0.detector_ran and r0.target_visible
    assert r0.ticks_since_last_detector_confirmation == 0
    assert r0.bbox == det.bbox
    assert detector.calls == 1 and len(created) == 1

    r1 = pt.update(frame)  # cheap tracker fills the gap
    assert not r1.detector_ran and r1.target_visible
    assert r1.ticks_since_last_detector_confirmation == 1
    r2 = pt.update(frame)
    assert r2.ticks_since_last_detector_confirmation == 2
    r3 = pt.update(frame)
    assert r3.ticks_since_last_detector_confirmation == 3
    assert detector.calls == 1  # still no re-detect yet

    r4 = pt.update(frame)  # ticks_since >= detect_every -> detector re-validates
    assert r4.detector_ran
    assert r4.ticks_since_last_detector_confirmation == 0
    assert detector.calls == 2 and len(created) == 2


def test_tracker_reports_lost_when_detector_sees_nobody():
    detector = StaticDetector([])  # nobody in frame
    pt = PersonTracker(detector=detector, tracker_factory=FakeBoxTracker)
    r = pt.update(_bright())
    assert r.detector_ran
    assert not r.target_visible
    assert r.bbox is None
    assert r.score == 0.0


# --- FOLLOW on the backend: steering + honesty fields -----------------------
def test_follow_person_left_turns_left_and_populates_state():
    bbox = (40, 100, 100, 200)
    tracker = FakeTracker(TrackResult(True, bbox, 0.9, 0))
    backend = _follow_backend(FakeCamera(_bright()), tracker)
    try:
        state = backend.tick()
    finally:
        backend.stop()

    turn, forward = backend.follow_motion()
    assert turn < 0  # toward the person on the left
    # RobotState honesty fields are populated every tick (software-spec.md §2.2).
    assert state.target_visible is True
    assert state.target_bbox == bbox
    assert state.tracker_score == 0.9
    assert state.ticks_since_last_detector_confirmation == 0
    assert tracker.updates == 1


def test_follow_centered_far_drives_forward():
    bbox = (FRAME_W // 2 - 50, 100, 100, 120)  # small -> far
    tracker = FakeTracker(TrackResult(True, bbox, 0.9, 0))
    backend = _follow_backend(FakeCamera(_bright()), tracker)
    try:
        backend.tick()
    finally:
        backend.stop()
    turn, forward = backend.follow_motion()
    assert turn == 0.0
    assert forward > 0.0


def test_follow_centered_close_stops():
    bbox = (FRAME_W // 2 - 60, 40, 120, 360)  # large -> close
    tracker = FakeTracker(TrackResult(True, bbox, 0.9, 0))
    backend = _follow_backend(FakeCamera(_bright()), tracker)
    try:
        backend.tick()
    finally:
        backend.stop()
    _turn, forward = backend.follow_motion()
    assert forward == 0.0


def test_follow_lost_target_stops_and_reports():
    # Tracker never sees the person; with coast_ticks=0 a lost tick STOPs at once.
    tracker = FakeTracker(TrackResult(False, None, 0.0, 5))
    backend = _follow_backend(FakeCamera(_bright()), tracker, follow_coast_ticks=0)
    try:
        state = backend.tick()
    finally:
        backend.stop()

    _turn, forward = backend.follow_motion()
    assert forward == 0.0
    assert state.target_visible is False
    assert state.target_bbox is None
    assert "lost" in (state.goal or {}).get("status", "").lower()
    assert "I lost you" in (state.goal or {}).get("status", "")


def test_follow_too_dark_degrades_to_stop():
    # A too-dark frame degrades to STOP before the tracker is ever consulted.
    tracker = FakeTracker(TrackResult(True, (40, 100, 100, 200), 0.9, 0))
    backend = _follow_backend(FakeCamera(_dark()), tracker)
    try:
        state = backend.tick()
    finally:
        backend.stop()

    _turn, forward = backend.follow_motion()
    assert forward == 0.0
    assert state.target_visible is False
    assert tracker.updates == 0  # never tracked on a dark frame
    assert "dark" in (state.goal or {}).get("status", "").lower()


def test_follow_coasts_briefly_before_stopping():
    tracker = FakeTracker(TrackResult(False, None, 0.0, 9))
    backend = _follow_backend(FakeCamera(_bright()), tracker, follow_coast_ticks=2)
    try:
        # first FOLLOW tick adopts + one lost tick -> still within coast budget
        s1 = backend.tick()
        assert (s1.goal or {}).get("reason") == "coast"
        s2 = backend.tick()
        assert (s2.goal or {}).get("reason") == "coast"
        s3 = backend.tick()  # exceeds coast budget -> STOP/lost
        assert (s3.goal or {}).get("reason") == "lost"
    finally:
        backend.stop()
    _turn, forward = backend.follow_motion()
    assert forward == 0.0


def test_collision_stop_overrides_follow():
    tracker = FakeTracker(TrackResult(True, (40, 100, 100, 200), 0.9, 0))
    backend = _follow_backend(FakeCamera(_bright()), tracker)
    backend.trigger_collision(0.10)  # obstacle inside the safe-stop threshold
    try:
        state = backend.tick()
    finally:
        backend.stop()

    assert state.mode == Mode.SAFE_STOP
    assert state.blocked is True
    assert tracker.updates == 0  # safety override returns before FOLLOW runs


def test_robotstate_follow_honesty_fields_roundtrip():
    # The populated honesty fields survive JSON serialization (the wire contract).
    bbox = (40, 100, 100, 200)
    tracker = FakeTracker(TrackResult(True, bbox, 0.77, 0))
    backend = _follow_backend(FakeCamera(_bright()), tracker)
    try:
        state = backend.tick()
    finally:
        backend.stop()

    restored = RobotState.from_json(state.to_json())
    assert restored.target_visible is True
    assert restored.target_bbox == bbox
    assert restored.tracker_score == 0.77
    assert restored.ticks_since_last_detector_confirmation == 0
    assert restored.goal_status == GoalStatus.RUNNING
