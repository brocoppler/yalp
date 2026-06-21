"""Track-by-detection person tracker for FOLLOW mode (software-spec.md §4).

This realizes the spec's **THESIS**: run a *cheap* per-frame tracker every reactive
tick to keep follow-mode smooth, and re-run a *slower* person **detector** every
few frames (or whenever the tracked box goes stale) purely to **re-seed and
validate** the box. The fast tracker gives smoothness; the slow detector gives
"it's actually a person, and here's where they really are now".

Two seams keep this honest and portable:

  * **The detector is pluggable** behind :class:`Detector`. The laptop/desk
    default is the bundled-Haar-cascade **face detector** (:class:`FaceDetector`),
    because a webcam at desk range frames only the user's HEAD + UPPER TORSO, which
    the full-body **HOG people detector** (:class:`HogPersonDetector`) — trained on
    standing bodies — cannot see. HOG remains the right choice for the eventual
    ROBOT looking across a room at a *standing* person, and ``AutoDetector`` tries
    face then falls back to HOG. All ship with ``opencv-python`` and need **no
    model-file download**. On the Pi we would swap in a faster detector
    (MobileNet-SSD / YOLO-nano via onnxruntime or ncnn) **behind this same
    interface** — Gate H (roadmap.md) decides whether the Pi sustains the
    ``config.GATE_H_GO_HZ`` floor that makes track-by-detection viable. Nothing
    else in FOLLOW changes when that swap happens.
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
# The desk-range default: a Haar-cascade FACE detector
# --------------------------------------------------------------------------- #
class FaceDetector:
    """OpenCV Haar-cascade FACE detector — the right default at *desk* range.

    A laptop webcam frames only the user's HEAD + UPPER TORSO, which the full-body
    HOG detector (trained on standing people) cannot see — so HOG fails at desk
    range and FOLLOW just says "I lost you". This detector uses OpenCV's **bundled**
    frontal-face Haar cascade (``cv2.data.haarcascades`` — ships with
    ``opencv-python``, **no download**), returns the **largest face** as the target,
    and expands that box **downward** (and slightly wider) to approximate
    head+shoulders for a steadier distance proxy. The center/steering come from
    that box; its size is the distance proxy. It implements the same
    :class:`Detector` interface as HOG, so FOLLOW is unchanged behind it.

    ``upper_body=True`` additionally tries OpenCV's upper-body cascade as a
    fallback when no frontal face is found (e.g. the user looked away).
    """

    def __init__(
        self,
        *,
        detect_width: int = config.FOLLOW_DETECT_WIDTH,
        expand_down: float = config.FOLLOW_FACE_EXPAND_DOWN,
        upper_body: bool = False,
    ) -> None:
        import cv2  # lazy: only when a real detector is built

        self._cv2 = cv2
        self.detect_width = int(detect_width)
        self.expand_down = float(expand_down)
        base = cv2.data.haarcascades
        self._face = cv2.CascadeClassifier(base + "haarcascade_frontalface_default.xml")
        if self._face.empty():  # pragma: no cover - broken opencv data dir
            raise RuntimeError("could not load bundled frontalface Haar cascade")
        self._upper = None
        if upper_body:
            up = cv2.CascadeClassifier(base + "haarcascade_upperbody.xml")
            self._upper = None if up.empty() else up

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
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img

        faces = self._face.detectMultiScale(
            gray, scaleFactor=1.1, minNeighbors=5, minSize=(24, 24)
        )
        score = 0.9
        if len(faces) == 0 and self._upper is not None:
            faces = self._upper.detectMultiScale(
                gray, scaleFactor=1.1, minNeighbors=3, minSize=(48, 48)
            )
            score = 0.6

        inv = 1.0 / scale if scale > 0 else 1.0
        out: List[Detection] = []
        for (fx, fy, fw, fh) in faces:
            bbox = self._expand(
                int(round(fx * inv)), int(round(fy * inv)),
                int(round(fw * inv)), int(round(fh * inv)), w, h,
            )
            out.append(Detection(bbox=bbox, score=score))
        # Return only the largest face as the target (steadier than juggling
        # several); the tracker also picks max-area, but being explicit is clearer.
        if not out:
            return out
        return [max(out, key=lambda d: d.area)]

    def _expand(self, x: int, y: int, w: int, h: int, fw: int, fh: int) -> Bbox:
        """Grow a face box DOWN (and a bit wider) to approximate head+shoulders."""
        new_h = int(round(h * (1.0 + self.expand_down)))
        widen = int(round(w * 0.3))
        nx = max(0, x - widen)
        nw = min(fw - nx, w + 2 * widen)
        ny = max(0, y)
        nh = min(fh - ny, new_h)
        return (nx, ny, nw, nh)


class AutoDetector:
    """Try the FACE detector first; fall back to HOG when no face is found.

    Good when the user may sit at a desk *or* stand across the room: the cheap
    face pass handles desk range, and the (slower) full-body HOG pass catches the
    standing case. The HOG detector is built lazily on first fallback.
    """

    def __init__(self, *, detect_width: int = config.FOLLOW_DETECT_WIDTH) -> None:
        self.detect_width = int(detect_width)
        self._face = FaceDetector(detect_width=detect_width)
        self._hog: Optional[HogPersonDetector] = None

    def detect(self, frame) -> List[Detection]:
        faces = self._face.detect(frame)
        if faces:
            return faces
        if self._hog is None:
            self._hog = HogPersonDetector(detect_width=self.detect_width)
        return self._hog.detect(frame)


def build_detector(
    name: Optional[str] = None, *, detect_width: int = config.FOLLOW_DETECT_WIDTH
) -> Detector:
    """Construct a detector by name: ``face`` (default), ``hog`` or ``auto``.

    The default ``face`` is reliable at *desk* range (head+shoulders webcam
    framing); ``hog`` is the full-body detector for the robot/room range; ``auto``
    tries face then falls back to hog. All three share the :class:`Detector`
    interface, so the eventual robot can swap in a faster detector (Gate H)
    without touching FOLLOW.
    """
    key = (name or config.FOLLOW_DETECTOR_DEFAULT).strip().lower()
    if key == "face":
        return FaceDetector(detect_width=detect_width)
    if key == "hog":
        return HogPersonDetector(detect_width=detect_width)
    if key == "auto":
        return AutoDetector(detect_width=detect_width)
    raise ValueError(f"unknown detector {name!r} (choose face, hog or auto)")


def detect_interval_for(name: Optional[str] = None) -> int:
    """Default re-detect cadence (ticks) for a detector name.

    The face cascade is cheap, so face/auto re-detect every couple ticks; HOG is
    slower, so it uses the longer ``FOLLOW_DETECT_INTERVAL_TICKS``.
    """
    key = (name or config.FOLLOW_DETECTOR_DEFAULT).strip().lower()
    if key in ("face", "auto"):
        return config.FOLLOW_FACE_DETECT_INTERVAL_TICKS
    return config.FOLLOW_DETECT_INTERVAL_TICKS


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
        max_coast_ticks: int = config.FOLLOW_COAST_TICKS,
        min_box_area_frac: float = config.FOLLOW_MIN_BOX_AREA_FRAC,
        edge_margin_frac: float = config.FOLLOW_EDGE_MARGIN_FRAC,
        max_jump_frac: float = config.FOLLOW_MAX_JUMP_FRAC,
    ) -> None:
        self._detector = detector
        self.detect_interval = max(1, int(detect_interval))
        self.track_min_score = float(track_min_score)
        self.score_decay = float(score_decay)
        # After this many ticks coasting on the cheap tracker WITHOUT a fresh
        # detector confirmation, stop claiming the box is visible — a fresh
        # detection (not a coasting tracker) is required to re-lock.
        self.max_coast_ticks = max(0, int(max_coast_ticks))
        # Sanity-rejection thresholds (drop implausible one-off detections).
        self.min_box_area_frac = float(min_box_area_frac)
        self.edge_margin_frac = float(edge_margin_frac)
        self.max_jump_frac = float(max_jump_frac)

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
        h, w = frame.shape[:2]
        prev = self._bbox
        # Drop implausible one-off detections (tiny, hard against a frame edge
        # with no continuity, or an implausible jump) so a far-edge false positive
        # like the observed x=-0.83 can never get latched into a stale lock.
        plausible = [d for d in detections if self._plausible(d.bbox, w, h, prev)]
        if not plausible:
            # No trustworthy detection — drop the box and report lost.
            self._bbox, self._score, self._tracker = None, 0.0, None
            return TrackResult(False, None, 0.0, 0, True)

        best = max(plausible, key=lambda d: d.area)
        self._bbox = best.bbox
        self._score = best.score
        self._tracker = self._make_tracker(frame, best.bbox)
        visible = self._score >= self.track_min_score
        return TrackResult(visible, self._bbox, self._score, 0, True)

    def _plausible(self, bbox: Bbox, fw: int, fh: int, prev: Optional[Bbox]) -> bool:
        """Reject implausible detections (sanity gate before latching a box)."""
        x, y, w, h = bbox
        if w <= 0 or h <= 0 or fw <= 0 or fh <= 0:
            return False
        # Too tiny to be a real target at this range.
        if (w * h) < self.min_box_area_frac * (fw * fh):
            return False
        cx = x + w / 2.0
        if prev is None:
            # A FRESH box (no track to provide continuity) jammed against a left
            # or right edge is almost always a false positive — reject it.
            margin = self.edge_margin_frac * fw
            if cx < margin or cx > (fw - margin):
                return False
        else:
            # We are already tracking: reject an implausible teleport away from
            # the current box (a transient detection elsewhere in the frame).
            pcx = prev[0] + prev[2] / 2.0
            if abs(cx - pcx) > self.max_jump_frac * fw:
                return False
        return True

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
        # Require a FRESH detection (not a coasting tracker) to keep the lock:
        # once we have coasted past max_coast_ticks without a detector
        # confirmation, report not-visible so a dead box is dropped promptly.
        fresh_enough = self._ticks_since_detector <= self.max_coast_ticks
        visible = fresh_enough and (self._score >= self.track_min_score)
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
    "FaceDetector",
    "AutoDetector",
    "build_detector",
    "detect_interval_for",
    "PersonTracker",
]
