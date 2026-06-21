"""Track-by-detection person tracker for FOLLOW mode (software-spec.md §4).

This realizes the spec's **THESIS**: run a *cheap* per-frame tracker every reactive
tick to keep follow-mode smooth, and re-run a *slower* person **detector** every
few frames (or whenever the tracked box goes stale) purely to **re-seed and
validate** the box. The fast tracker gives smoothness; the slow detector gives
"it's actually a person, and here's where they really are now".

Two seams keep this honest and portable:

  * **The detector is pluggable** behind :class:`Detector`. The laptop default is
    OpenCV's built-in **HOG people detector** (:class:`HogPersonDetector`), which
    ships with ``opencv-python`` and needs **no model-file download**. On the Pi we
    would swap in a faster detector (MobileNet-SSD / YOLO-nano via onnxruntime or
    ncnn) **behind this same interface** — Gate H (roadmap.md) decides whether the
    Pi sustains the ``config.GATE_H_GO_HZ`` floor that makes track-by-detection
    viable. Nothing else in FOLLOW changes when that swap happens.
  * **The between-detection tracker is pluggable too.** We prefer a real OpenCV
    legacy tracker (CSRT/KCF) when the installed OpenCV exposes one — handling the
    ``cv2.legacy.*`` vs ``cv2.Tracker*_create`` vs *missing* differences across
    OpenCV builds — and otherwise **fall back to a tiny in-repo "hold the last
    box" tracker** so FOLLOW still runs on any OpenCV build (and in CI with none).

Coordinate convention: a bbox is ``(x, y, w, h)`` in **frame pixel coordinates**
of the *original* (non-downscaled) frame. Detection runs on a downscaled copy for
speed, and boxes are scaled back to original-frame pixels before they leave this
module — so ``RobotState.target_bbox`` is always in real frame pixels (§2.2).

Import hygiene: ``cv2`` is imported **lazily** (only when a real detector/tracker
is actually constructed), so this module imports cleanly with no OpenCV present
and tests can inject a fake detector without touching hardware or heavy deps.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional, Protocol, Tuple

from .. import config

Bbox = Tuple[int, int, int, int]  # (x, y, w, h) in original-frame pixels


# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Detection:
    """One person box from a :class:`Detector`, in original-frame pixels."""

    bbox: Bbox
    score: float  # detector confidence, normalized to 0..1

    @property
    def area(self) -> int:
        return int(self.bbox[2]) * int(self.bbox[3])


@dataclass(frozen=True)
class TrackResult:
    """The tracker's per-tick estimate, with the spec's honesty signals (§2.2).

    ``ticks_since_last_detector_confirmation`` counts ticks since the *detector*
    last re-seeded/validated the box (0 on the tick a detection lands). FOLLOW
    uses ``score`` + this counter to choose **coast vs stop** instead of driving
    blindly on a stale box.
    """

    target_visible: bool
    bbox: Optional[Bbox]
    score: float
    ticks_since_last_detector_confirmation: int
    detector_ran: bool = False


# --------------------------------------------------------------------------- #
# Detector interface + the laptop default (HOG)
# --------------------------------------------------------------------------- #
class Detector(Protocol):
    """A pluggable person detector: a frame in, person boxes out.

    Implementations return boxes in **original-frame** pixel coordinates. Any
    downscaling-for-speed is the detector's own internal concern.
    """

    def detect(self, frame) -> List[Detection]:  # pragma: no cover - protocol
        ...


class HogPersonDetector:
    """OpenCV's built-in HOG + linear-SVM people detector (no weights to download).

    The frame is downscaled to ``detect_width`` before inference — the single
    biggest throughput lever on a no-NPU machine (software-spec.md §2.5) — and the
    returned boxes are scaled back to original-frame pixels. ``cv2`` is imported
    lazily on first construction so importing this module never requires OpenCV.
    """

    def __init__(self, *, detect_width: int = config.FOLLOW_DETECT_WIDTH) -> None:
        import cv2  # lazy: only when a real detector is built

        self._cv2 = cv2
        self.detect_width = int(detect_width)
        self._hog = cv2.HOGDescriptor()
        self._hog.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())

    def detect(self, frame) -> List[Detection]:
        if frame is None:
            return []
        cv2 = self._cv2
        h, w = frame.shape[:2]
        scale = 1.0
        img = frame
        if w > self.detect_width:
            scale = self.detect_width / float(w)
            img = cv2.resize(frame, (self.detect_width, max(1, int(round(h * scale)))))
        rects, weights = self._hog.detectMultiScale(
            img, winStride=(8, 8), padding=(8, 8), scale=1.05
        )
        out: List[Detection] = []
        inv = 1.0 / scale if scale > 0 else 1.0
        for (rx, ry, rw, rh), weight in zip(rects, weights):
            bbox = (
                int(round(rx * inv)),
                int(round(ry * inv)),
                int(round(rw * inv)),
                int(round(rh * inv)),
            )
            out.append(Detection(bbox=bbox, score=_squash(float(weight))))
        return out


def _squash(weight: float) -> float:
    """Map a HOG SVM decision value (~0..2) to a 0..1 confidence."""
    return float(1.0 / (1.0 + math.exp(-2.0 * weight)))


# --------------------------------------------------------------------------- #
# Between-detection tracker (cv2 if available; in-repo hold otherwise)
# --------------------------------------------------------------------------- #
def _resolve_cv2_tracker_factory():
    """Return a 0-arg factory that builds a fresh OpenCV box tracker, or ``None``.

    Handles the cross-version differences: ``cv2.legacy.TrackerCSRT_create`` (newer
    contrib), ``cv2.TrackerCSRT_create`` (older), and the same for KCF; returns
    ``None`` when the installed OpenCV exposes no usable single-object tracker (or
    OpenCV is absent), so the caller falls back to the in-repo hold tracker.
    """
    try:
        import cv2  # lazy
    except Exception:  # pragma: no cover - no opencv at all
        return None

    legacy = getattr(cv2, "legacy", None)
    candidates = []
    for name in ("TrackerCSRT_create", "TrackerKCF_create"):
        if legacy is not None and hasattr(legacy, name):
            candidates.append(getattr(legacy, name))
        if hasattr(cv2, name):
            candidates.append(getattr(cv2, name))
    return candidates[0] if candidates else None


class _HoldTracker:
    """Trivial in-repo fallback: hold the last box until the detector re-seeds.

    With no real OpenCV tracker available we cannot follow the box *between*
    detections, so we simply hold the last detected box. Track-by-detection still
    works because the detector re-seeds every ``detect_interval`` ticks; the
    staleness counter (and FOLLOW's coast/stop policy) keeps this honest — a held
    box that is never re-confirmed goes stale and FOLLOW stops.
    """

    def __init__(self, bbox: Bbox) -> None:
        self._bbox = bbox

    def update(self, _frame) -> Tuple[bool, Bbox]:
        return True, self._bbox


class _Cv2TrackerWrapper:
    """Adapt an OpenCV single-object tracker to the (ok, bbox) shape."""

    def __init__(self, factory, frame, bbox: Bbox) -> None:
        self._t = factory()
        self._t.init(frame, tuple(int(v) for v in bbox))

    def update(self, frame) -> Tuple[bool, Optional[Bbox]]:
        ok, box = self._t.update(frame)
        if not ok or box is None:
            return False, None
        x, y, w, h = box
        return True, (int(x), int(y), int(w), int(h))


# --------------------------------------------------------------------------- #
# The track-by-detection tracker
# --------------------------------------------------------------------------- #
class PersonTracker:
    """Track-by-detection: cheap per-tick tracking, periodic detector re-seed.

    Parameters
    ----------
    detector:
        A :class:`Detector` (default: :class:`HogPersonDetector`, built lazily on
        first use so construction never requires OpenCV). Tests inject a fake.
    detect_interval:
        Number of cheap-track ticks to run between detector re-seeds
        (track-by-detection cadence): after a detection the detector re-runs once
        ``detect_interval`` ticks have elapsed without a confirmation. The detector
        also runs whenever there is no current box.
    track_min_score:
        Below this the held/tracked box is reported as not-visible.
    score_decay:
        Per-tick multiplicative decay applied to the score while only the cheap
        tracker is running, so confidence visibly erodes the longer the detector
        has not re-confirmed the box.
    """

    def __init__(
        self,
        detector: Optional[Detector] = None,
        *,
        detect_interval: int = config.FOLLOW_DETECT_INTERVAL_TICKS,
        track_min_score: float = config.FOLLOW_TRACK_MIN_SCORE,
        score_decay: float = 0.92,
    ) -> None:
        self._detector = detector
        self.detect_interval = max(1, int(detect_interval))
        self.track_min_score = float(track_min_score)
        self.score_decay = float(score_decay)

        self._cv2_factory = None
        self._cv2_resolved = False

        self._bbox: Optional[Bbox] = None
        self._score = 0.0
        self._ticks_since_detector = 0
        self._tracker = None  # cv2 wrapper or _HoldTracker

    # -- detector (lazy default) --------------------------------------------
    def _get_detector(self) -> Detector:
        if self._detector is None:
            self._detector = HogPersonDetector()
        return self._detector

    def _get_cv2_factory(self):
        if not self._cv2_resolved:
            self._cv2_factory = _resolve_cv2_tracker_factory()
            self._cv2_resolved = True
        return self._cv2_factory

    # -- public API ----------------------------------------------------------
    def update(self, frame) -> TrackResult:
        """Advance one tick: detect-and-reseed on the interval, else cheap-track."""
        if frame is None:
            # No frame this tick: can't confirm or track. Report lost honestly.
            self._bbox, self._score, self._tracker = None, 0.0, None
            return TrackResult(False, None, 0.0, self._ticks_since_detector, False)

        want_detect = self._bbox is None or (
            self._ticks_since_detector >= self.detect_interval
        )
        if want_detect:
            return self._detect_and_reseed(frame)
        return self._cheap_track(frame)

    # -- internals -----------------------------------------------------------
    def _detect_and_reseed(self, frame) -> TrackResult:
        detections = self._get_detector().detect(frame)
        self._ticks_since_detector = 0
        if not detections:
            # The detector ran and confirmed nobody is there — drop the box.
            self._bbox, self._score, self._tracker = None, 0.0, None
            return TrackResult(False, None, 0.0, 0, True)

        best = max(detections, key=lambda d: d.area)
        self._bbox = best.bbox
        self._score = best.score
        self._tracker = self._make_tracker(frame, best.bbox)
        visible = self._score >= self.track_min_score
        return TrackResult(visible, self._bbox, self._score, 0, True)

    def _cheap_track(self, frame) -> TrackResult:
        self._ticks_since_detector += 1
        if self._tracker is None or self._bbox is None:
            # Nothing to follow yet; force a detect next tick.
            self._ticks_since_detector = self.detect_interval
            return TrackResult(False, self._bbox, 0.0, self._ticks_since_detector, False)

        ok, bbox = self._tracker.update(frame)
        if not ok or bbox is None:
            self._bbox, self._score, self._tracker = None, 0.0, None
            return TrackResult(False, None, 0.0, self._ticks_since_detector, False)

        self._bbox = bbox
        self._score *= self.score_decay
        visible = self._score >= self.track_min_score
        return TrackResult(visible, self._bbox, self._score,
                           self._ticks_since_detector, False)

    def _make_tracker(self, frame, bbox: Bbox):
        factory = self._get_cv2_factory()
        if factory is None:
            return _HoldTracker(bbox)
        try:
            return _Cv2TrackerWrapper(factory, frame, bbox)
        except Exception:  # pragma: no cover - defensive: bad cv2 build
            return _HoldTracker(bbox)


__all__ = [
    "Bbox",
    "Detection",
    "TrackResult",
    "Detector",
    "HogPersonDetector",
    "PersonTracker",
]
