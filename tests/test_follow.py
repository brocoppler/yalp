"""FOLLOW-mode tests — track-by-detection steering, honesty fields, degradation.

No camera, network, or display: a FAKE detector / tracker (with a controllable
"person bbox") and a fake frame source drive the steering decisions. We assert the
spec's behaviors (software-spec.md §4):

  * person to the left  -> turn left;
  * centered + far (small bbox)  -> drive forward;
  * centered + close (large bbox) -> stop;
  * no detection for K ticks -> target_visible False + stop ("lost");
  * a too-dark frame -> degrade to stop;
  * RobotState gets target_visible / target_bbox / tracker_score /
    ticks_since_last_detector_confirmation populated every FOLLOW tick.
"""

from __future__ import annotations

from typing import List, Optional

import numpy as np

from yalp.contract.messages import GoalStatus, Intent, Mode
from yalp.reactive.fake_backend import FakeReactiveBackend
from yalp.reactive.follow import (
    REASON_DARK,
    REASON_LOST,
    REASON_REACHED,
    REASON_STALE,
    FollowController,
    FollowDecision,
    FollowReporter,
    frame_brightness,
)
from yalp.reactive.person_tracker import (
    Detection,
    PersonTracker,
    TrackResult,
    detect_interval_for,
)


# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #
class FakeCamera:
    """A minimal stand-in for yalp.camera.Camera with a fixed frame."""

    source = "fake"

    def __init__(self, frame: np.ndarray) -> None:
        self._frame = frame
        self.height, self.width = frame.shape[:2]
        self.started = False

    def set_frame(self, frame: np.ndarray) -> None:
        self._frame = frame
        self.height, self.width = frame.shape[:2]

    def start(self):
        self.started = True
        return self

    def stop(self):
        self.started = False

    def latest(self):
        return self._frame


class FakeTracker:
    """Returns scripted TrackResults, ignoring the frame (steering under test)."""

    def __init__(self, results: List[TrackResult]) -> None:
        self._results = list(results)
        self._last = results[-1] if results else None

    def update(self, _frame) -> TrackResult:
        if self._results:
            self._last = self._results.pop(0)
        return self._last


class FakeDetector:
    """A pluggable detector returning a scripted sequence of detection lists."""

    def __init__(self, sequence: List[List[Detection]]) -> None:
        self._seq = list(sequence)
        self.calls = 0

    def detect(self, _frame) -> List[Detection]:
        self.calls += 1
        if self._seq:
            return self._seq.pop(0)
        return []


def _bright_frame(h: int = 240, w: int = 320, value: int = 180) -> np.ndarray:
    return np.full((h, w, 3), value, dtype=np.uint8)


def _dark_frame(h: int = 240, w: int = 320) -> np.ndarray:
    return np.zeros((h, w, 3), dtype=np.uint8)


def _visible(bbox, score=0.9, ticks=0) -> TrackResult:
    return TrackResult(True, bbox, score, ticks, detector_ran=True)


# --------------------------------------------------------------------------- #
# FollowController steering (pure)
# --------------------------------------------------------------------------- #
def test_person_to_the_left_turns_left():
    ctrl = FollowController()
    # bbox centered well to the LEFT of a 320px-wide frame.
    res = _visible((10, 100, 40, 90))
    dec = ctrl.decide(res, frame_w=320, frame_h=240, brightness=180)
    assert dec.target_visible is True
    assert dec.error_x < 0
    assert dec.turn < 0  # turn left


def test_person_to_the_right_turns_right():
    ctrl = FollowController()
    res = _visible((270, 100, 40, 90))
    dec = ctrl.decide(res, frame_w=320, frame_h=240, brightness=180)
    assert dec.error_x > 0
    assert dec.turn > 0  # turn right


def test_centered_and_far_drives_forward():
    ctrl = FollowController()
    # Centered horizontally, SMALL bbox height (far away) -> forward, no turn.
    res = _visible((140, 100, 40, 40))  # h/240 ~ 0.17 << stop fraction
    dec = ctrl.decide(res, frame_w=320, frame_h=240, brightness=180)
    assert abs(dec.turn) < 1e-9  # within deadband -> no turn
    assert dec.forward > 0.0


def test_centered_and_close_stops():
    ctrl = FollowController()
    # Centered, LARGE bbox (close) -> hold, forward 0.
    res = _visible((110, 20, 100, 200))  # h/240 ~ 0.83 >= stop fraction
    dec = ctrl.decide(res, frame_w=320, frame_h=240, brightness=180)
    assert dec.target_visible is True
    assert dec.forward == 0.0
    assert dec.reason == REASON_REACHED


def test_lost_when_not_visible_stops():
    ctrl = FollowController()
    res = TrackResult(False, None, 0.0, 0, False)
    dec = ctrl.decide(res, 320, 240, brightness=180)
    assert dec.target_visible is False
    assert dec.turn == 0.0 and dec.forward == 0.0
    assert dec.reason == REASON_LOST


def test_stale_box_stops_even_if_tracker_reports_visible():
    ctrl = FollowController(coast_ticks=8)
    # Tracker still "sees" a box, but the detector has not confirmed it for too
    # long -> stop, never drive blind on a stale box (§4).
    res = TrackResult(True, (140, 100, 40, 40), 0.9, ticks_since_last_detector_confirmation=20)
    dec = ctrl.decide(res, 320, 240, brightness=180)
    assert dec.target_visible is False
    assert dec.reason == REASON_STALE
    assert dec.forward == 0.0


def test_too_dark_frame_degrades_to_stop():
    ctrl = FollowController()
    res = _visible((140, 100, 40, 40))  # a perfectly good box...
    dec = ctrl.decide(res, 320, 240, brightness=2.0)  # ...but the frame is dark
    assert dec.target_visible is False
    assert dec.reason == REASON_DARK
    assert dec.forward == 0.0 and dec.turn == 0.0


def test_weak_score_treated_as_lost():
    ctrl = FollowController(track_min_score=0.2)
    res = TrackResult(True, (140, 100, 40, 40), 0.05, 0, False)
    dec = ctrl.decide(res, 320, 240, brightness=180)
    assert dec.target_visible is False
    assert dec.reason == REASON_LOST


def test_frame_brightness_helper():
    assert frame_brightness(None) == 0.0
    assert frame_brightness(_dark_frame()) < 1.0
    assert frame_brightness(_bright_frame(value=200)) > 150.0


# --------------------------------------------------------------------------- #
# PersonTracker track-by-detection (with a fake detector)
# --------------------------------------------------------------------------- #
def test_tracker_reseeds_on_detection_and_resets_counter():
    det = FakeDetector([[Detection((100, 50, 40, 90), 0.9)]])
    pt = PersonTracker(detector=det, detect_interval=5)
    res = pt.update(_bright_frame())
    assert res.target_visible is True
    assert res.bbox == (100, 50, 40, 90)
    assert res.ticks_since_last_detector_confirmation == 0
    assert res.detector_ran is True
    assert det.calls == 1


def test_tracker_holds_box_between_detections_and_counts_ticks():
    # Detector finds a person once, then is not called again until the interval.
    det = FakeDetector([[Detection((100, 50, 40, 90), 0.9)]])
    pt = PersonTracker(detector=det, detect_interval=3)
    pt.update(_bright_frame())  # tick 0: detect + seed
    r1 = pt.update(_bright_frame())  # tick 1: cheap-track (hold)
    r2 = pt.update(_bright_frame())  # tick 2: cheap-track (hold)
    assert det.calls == 1  # detector did NOT run on the in-between ticks
    assert r1.ticks_since_last_detector_confirmation == 1
    assert r2.ticks_since_last_detector_confirmation == 2
    # The box is held between detections.
    assert r2.bbox == (100, 50, 40, 90)


def test_tracker_reruns_detector_after_interval():
    det = FakeDetector(
        [
            [Detection((100, 50, 40, 90), 0.9)],   # tick 0 seed
            [Detection((120, 55, 44, 92), 0.95)],  # tick 3 re-seed
        ]
    )
    pt = PersonTracker(detector=det, detect_interval=3)
    pt.update(_bright_frame())  # 0: detect + seed (ticks=0)
    pt.update(_bright_frame())  # 1: hold (ticks=1)
    pt.update(_bright_frame())  # 2: hold (ticks=2)
    pt.update(_bright_frame())  # 3: hold (ticks=3)
    r4 = pt.update(_bright_frame())  # 4: ticks>=interval -> detect again
    assert det.calls == 2
    assert r4.ticks_since_last_detector_confirmation == 0
    assert r4.bbox == (120, 55, 44, 92)


def test_tracker_reports_lost_when_detector_finds_nobody():
    det = FakeDetector([[]])  # detector runs, finds nobody
    pt = PersonTracker(detector=det, detect_interval=5)
    res = pt.update(_bright_frame())
    assert res.target_visible is False
    assert res.bbox is None
    assert det.calls == 1


def test_tracker_handles_none_frame():
    det = FakeDetector([[Detection((100, 50, 40, 90), 0.9)]])
    pt = PersonTracker(detector=det, detect_interval=5)
    res = pt.update(None)
    assert res.target_visible is False
    assert res.bbox is None
    assert det.calls == 0  # never even tried to detect on a missing frame


# --------------------------------------------------------------------------- #
# FakeReactiveBackend FOLLOW integration (state population + degradation)
# --------------------------------------------------------------------------- #
def _follow_backend(camera, tracker) -> FakeReactiveBackend:
    backend = FakeReactiveBackend(camera=camera, tracker=tracker)
    backend.start()
    backend.apply_intent(Intent(Mode.FOLLOW, {"target": "nearest_person"}, seq=1))
    backend.tick()  # adopt FOLLOW
    return backend


def test_follow_populates_honesty_fields_on_state():
    cam = FakeCamera(_bright_frame())
    tracker = FakeTracker([_visible((140, 100, 40, 40), score=0.8, ticks=0)] * 3)
    backend = _follow_backend(cam, tracker)
    try:
        state = backend.tick()
    finally:
        backend.stop()
    assert state.mode == Mode.FOLLOW
    assert state.goal_status == GoalStatus.RUNNING
    assert state.target_visible is True
    assert state.target_bbox == (140, 100, 40, 40)
    assert abs(state.tracker_score - 0.8) < 1e-6
    assert state.ticks_since_last_detector_confirmation == 0


def test_follow_left_person_makes_backend_turn_left():
    cam = FakeCamera(_bright_frame())
    tracker = FakeTracker([_visible((10, 100, 40, 90))] * 3)
    backend = _follow_backend(cam, tracker)
    try:
        backend.tick()
        dec = backend.last_follow_decision
    finally:
        backend.stop()
    assert dec.target_visible is True
    assert dec.turn < 0  # steering left


def test_follow_close_person_stops_forward():
    cam = FakeCamera(_bright_frame())
    tracker = FakeTracker([_visible((110, 20, 100, 200))] * 3)
    backend = _follow_backend(cam, tracker)
    try:
        backend.tick()
        dec = backend.last_follow_decision
    finally:
        backend.stop()
    assert dec.forward == 0.0
    assert dec.reason == REASON_REACHED


def test_follow_lost_target_sets_invisible_and_stops():
    cam = FakeCamera(_bright_frame())
    tracker = FakeTracker([TrackResult(False, None, 0.0, 0, False)] * 3)
    backend = _follow_backend(cam, tracker)
    try:
        state = backend.tick()
        dec = backend.last_follow_decision
    finally:
        backend.stop()
    assert state.target_visible is False
    assert state.target_bbox is None
    assert dec.forward == 0.0 and dec.turn == 0.0
    assert dec.reason == REASON_LOST


def test_follow_dark_frame_degrades_to_stop():
    cam = FakeCamera(_dark_frame())
    # Tracker would happily report a box, but the frame is too dark to trust.
    tracker = FakeTracker([_visible((140, 100, 40, 40))] * 3)
    backend = _follow_backend(cam, tracker)
    try:
        state = backend.tick()
        dec = backend.last_follow_decision
    finally:
        backend.stop()
    assert state.target_visible is False
    assert dec.reason == REASON_DARK
    assert dec.forward == 0.0


def test_collision_stop_overrides_follow():
    cam = FakeCamera(_bright_frame())
    tracker = FakeTracker([_visible((140, 100, 40, 40))] * 5)
    backend = _follow_backend(cam, tracker)
    try:
        backend.trigger_collision(0.10)  # obstacle inside the safe-stop threshold
        state = backend.tick()
    finally:
        backend.stop()
    # Safety reflex beats FOLLOW entirely this tick (§2.3).
    assert state.mode == Mode.SAFE_STOP
    assert state.goal_status == GoalStatus.BLOCKED


def test_follow_stale_box_stops_via_backend():
    cam = FakeCamera(_bright_frame())
    # Tracker keeps "seeing" the box but the detector hasn't confirmed in 20 ticks.
    stale = TrackResult(True, (140, 100, 40, 40), 0.9, 20, False)
    tracker = FakeTracker([stale] * 3)
    backend = _follow_backend(cam, tracker)
    try:
        state = backend.tick()
        dec = backend.last_follow_decision
    finally:
        backend.stop()
    assert state.target_visible is False
    assert state.ticks_since_last_detector_confirmation == 20
    assert dec.reason == REASON_STALE


# --------------------------------------------------------------------------- #
# Desk-range detector selection (FIX 1) — no camera / no OpenCV needed
# --------------------------------------------------------------------------- #
def test_follow_cli_defaults_to_face_detector():
    """`yalp follow` defaults to the desk-range FACE detector (not HOG)."""
    import argparse

    from yalp import config
    from yalp.reactive import follow_cli

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers()
    follow_cli.add_parser(sub)
    args = parser.parse_args(["follow"])
    assert args.detector is None  # unset -> resolved to the config default
    resolved = args.detector or config.FOLLOW_DETECTOR_DEFAULT
    assert resolved == "face"
    # face/auto re-detect more often than the slower HOG.
    assert detect_interval_for("face") <= detect_interval_for("hog")
    assert detect_interval_for(None) == detect_interval_for("face")


def test_follow_cli_accepts_detector_choices():
    import argparse

    from yalp.reactive import follow_cli

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers()
    follow_cli.add_parser(sub)
    for name in ("face", "hog", "person", "auto"):
        assert parser.parse_args(["follow", "--detector", name]).detector == name


# --------------------------------------------------------------------------- #
# Full detect -> track -> steer path with an injected fake detector (FIX 1/2)
# --------------------------------------------------------------------------- #
def _steer_from_detection(bbox) -> FollowDecision:
    """Run a single detection through the real tracker + controller."""
    frame = _bright_frame()  # 240x320
    det = FakeDetector([[Detection(bbox, 0.9)]])
    pt = PersonTracker(detector=det, detect_interval=detect_interval_for("face"))
    res = pt.update(frame)
    ctrl = FollowController()
    return ctrl.decide(res, frame_w=320, frame_h=240, brightness=180)


def test_face_detection_centered_drives_forward():
    dec = _steer_from_detection((140, 100, 40, 40))  # centered, small/far
    assert dec.target_visible is True
    assert abs(dec.turn) < 1e-9
    assert dec.forward > 0.0


def test_face_detection_offcenter_turns_toward():
    dec = _steer_from_detection((10, 100, 40, 90))  # off to the LEFT
    assert dec.target_visible is True
    assert dec.turn < 0  # turn left toward the person


def test_face_detection_large_stops():
    dec = _steer_from_detection((110, 20, 100, 200))  # big bbox -> close
    assert dec.target_visible is True
    assert dec.forward == 0.0
    assert dec.reason == REASON_REACHED


def test_one_off_edge_detection_is_not_latched():
    """A single far-edge false positive (the observed x=-0.83) must NOT latch.

    The sanity gate drops a FRESH detection jammed against the frame edge, so the
    tracker never claims it visible and cannot coast on it for many ticks.
    """
    # One detection hard against the LEFT edge, then nothing.
    det = FakeDetector([[Detection((0, 100, 18, 36), 0.9)], [], [], [], []])
    pt = PersonTracker(detector=det, detect_interval=detect_interval_for("face"))
    results = [pt.update(_bright_frame()) for _ in range(6)]
    assert all(r.target_visible is False for r in results)
    assert all(r.bbox is None for r in results)


def test_target_visible_flips_false_promptly_after_face_lost():
    """Once the detector stops returning a face, the lock drops within a few ticks.

    A fresh detection — not a coasting tracker — is required to stay locked, so a
    dead box can't be held for ~6 ticks like the observed stale coast.
    """
    seq = [[Detection((140, 90, 50, 60), 0.9)]] + [[] for _ in range(10)]
    det = FakeDetector(seq)
    pt = PersonTracker(detector=det, detect_interval=detect_interval_for("face"))
    visibility = [pt.update(_bright_frame()).target_visible for _ in range(8)]
    assert visibility[0] is True  # acquired on the first detection
    # It must go (and stay) not-visible well before 6 ticks of stale coasting.
    assert visibility[-1] is False
    assert any(v is False for v in visibility[1:4])


def test_implausible_jump_between_frames_is_rejected():
    """While tracking, a transient detection that teleports across the frame is dropped."""
    det = FakeDetector(
        [
            [Detection((20, 90, 50, 60), 0.9)],      # seed on the left
            [Detection((290, 90, 40, 50), 0.9)],     # teleport to the right -> reject
        ]
    )
    pt = PersonTracker(detector=det, detect_interval=1)
    r0 = pt.update(_bright_frame())  # detect: seed on the left
    pt.update(_bright_frame())       # cheap-track hold (counter ticks up)
    r2 = pt.update(_bright_frame())  # detector reruns -> teleport rejected
    assert r0.target_visible is True
    assert r2.target_visible is False  # the teleport was not latched


# --------------------------------------------------------------------------- #
# Readable output: transitions + heartbeat, warm-up quiet (FIX 3)
# --------------------------------------------------------------------------- #
def _vis_decision() -> FollowDecision:
    return FollowDecision(True, 0.1, 0.4, "tracking", "follow", error_x=0.1, bbox_h=0.4)


def _lost_decision() -> FollowDecision:
    return FollowDecision(False, 0.0, 0.0, "I lost you", REASON_LOST)


def test_reporter_emits_on_acquire_and_lose_transitions():
    rep = FollowReporter(heartbeat_s=1.5)
    # First visible decision -> ACQUIRE line.
    acq = rep.update(_vis_decision(), now=0.0)
    assert acq is not None and "acquired" in acq
    # Same visible state again right away -> quiet (no spam).
    assert rep.update(_vis_decision(), now=0.1) is None
    # Target lost -> LOSE line.
    lost = rep.update(_lost_decision(), now=0.2)
    assert lost is not None and "lost" in lost.lower()


def test_reporter_heartbeat_is_periodic_not_per_tick():
    rep = FollowReporter(heartbeat_s=1.5)
    rep.update(_vis_decision(), now=0.0)  # acquire
    # Within the heartbeat window: quiet.
    assert rep.update(_vis_decision(), now=0.5) is None
    assert rep.update(_vis_decision(), now=1.0) is None
    # Past the window: one heartbeat summarizing the action.
    hb = rep.update(_vis_decision(), now=2.0)
    assert hb is not None and "tracking" in hb


def test_reporter_is_silent_during_warmup():
    rep = FollowReporter()
    # Warm-up: even a "lost / too dark" decision is suppressed entirely.
    assert rep.update(_lost_decision(), now=0.0, warming_up=True) is None
    # First real (post-warmup) frame with a target -> acquire, not a stale "lost".
    line = rep.update(_vis_decision(), now=0.1, warming_up=False)
    assert line is not None and "acquired" in line


def test_reporter_reports_safe_stop_as_lost():
    rep = FollowReporter()
    line = rep.update(_vis_decision(), now=0.0, safe_stop=True)
    assert line is not None and "lost" in line.lower()


def test_enter_follow_mode_is_fire_and_forget():
    # apply_intent only stores the intent; it must return immediately without
    # blocking on the tracker/camera (the contract: enter_follow_mode returns now).
    cam = FakeCamera(_bright_frame())
    tracker = FakeTracker([_visible((140, 100, 40, 40))])
    backend = FakeReactiveBackend(camera=cam, tracker=tracker)
    backend.start()
    try:
        backend.apply_intent(Intent(Mode.FOLLOW, {"target": "nearest_person"}, seq=1))
        # No tick yet -> the tracker was never consulted.
        assert backend.last_follow_decision is None
    finally:
        backend.stop()
