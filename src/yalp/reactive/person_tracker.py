"""Track-by-detection person tracker (software-spec.md §4).

This realizes the spec's **THESIS**: a slow, robust *person detector* re-seeds and
validates a cheap, fast *box tracker* that fills the gaps between detections. The
fast tracker provides smoothness at the reactive tick rate; the slow detector
provides the "it's actually a person, and here's where they really are now"
correction. That is the only design that satisfies both robustness and the
reactive frame budget on a no-NPU Pi.

Two pluggable seams keep the swap Gate H (roadmap.md) ultimately decides cheap:

  * ``Detector`` — anything with ``detect(frame) -> list[Detection]``. The default
    is :class:`HOGPersonDetector` (OpenCV's built-in HOG people detector, which
    ships with opencv and needs **no model download**). On the Pi we would swap in
    a faster detector (MobileNet-SSD / YOLO-nano via onnxruntime/ncnn) behind this
    same interface — Gate H's fps spike decides whether that swap is even needed.
  * ``BoxTracker`` — anything with ``init(frame, bbox)`` + ``update(frame) ->
    (ok, bbox, score)``. :func:`make_box_tracker` prefers a real OpenCV tracker
    (CSRT / KCF, across the several OpenCV API spellings) and **falls back** to the
    pure-Python :class:`SimpleBoxTracker` when no cv2 tracker is available — so
    FOLLOW still runs on an OpenCV build without the (contrib) tracking module.

Coordinates: bounding boxes are ``(x, y, w, h)`` in **frame pixels** (the
OpenCV convention, top-left origin), consistent end-to-end — the detector scales
its downscaled-frame boxes back up to full-frame pixels before returning them, and
``RobotState.target_bbox`` is populated with the same pixel box.

Import-clean: the module top-level is **pure standard library** (no cv2 / numpy),
so tests can import it and inject a fake detector/tracker with no hardware. cv2
and numpy are imported lazily, only inside the HOG detector / real-tracker paths.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, List, Optional, Protocol, Tuple, runtime_checkable

# A bounding box in frame pixels: (x, y, w, h), top-left origin.
Bbox = Tuple[int, int, int, int]


# --- lazy heavy-dep accessors ------------------------------------------------
def _cv2():
    import cv2  # local import keeps the module import-clean

    return cv2


def _np():
    import numpy as np

    return np


# --- detection value type ----------------------------------------------------
@dataclass(frozen=True)
class Detection:
    """One detected person box in full-frame pixels with a 0..1 confidence."""

    x: float
    y: float
    w: float
    h: float
    score: float = 1.0

    @property
    def bbox(self) -> Bbox:
        return (int(round(self.x)), int(round(self.y)),
                int(round(self.w)), int(round(self.h)))

    @property
    def area(self) -> float:
        return max(0.0, self.w) * max(0.0, self.h)


@dataclass
class TrackResult:
    """The per-tick output the reactive FOLLOW loop consumes.

    Mirrors the honesty fields of ``RobotState`` (software-spec.md §2.2) so the
    backend can choose to coast vs stop instead of driving blindly on a stale box.
    """

    target_visible: bool
    bbox: Optional[Bbox]
    score: float
    ticks_since_last_detector_confirmation: int
    detector_ran: bool = False


# --- detector interface + default HOG impl -----------------------------------
@runtime_checkable
class Detector(Protocol):
    """A pluggable person detector. Returns boxes in full-frame pixels."""

    def detect(self, frame) -> List[Detection]:  # pragma: no cover - protocol
        ...


def _sigmoid(x: float) -> float:
    import math

    # Clamp to avoid overflow on extreme SVM margins.
    if x < -60.0:
        return 0.0
    if x > 60.0:
        return 1.0
    return 1.0 / (1.0 + math.exp(-x))


class HOGPersonDetector:
    """OpenCV's built-in HOG + linear-SVM people detector (no weights to download).

    The frame is **downscaled** to ``detect_width`` before inference (the main
    throughput lever — software-spec.md §2.5) and the resulting boxes are scaled
    back up to full-frame pixels. The SVM margin per detection is squashed through
    a sigmoid into a 0..1 ``score``. cv2/numpy are imported lazily and the
    descriptor is built on first use, so importing this class is cheap and
    hardware-free.
    """

    def __init__(
        self,
        *,
        detect_width: int = 320,
        win_stride: Tuple[int, int] = (8, 8),
        padding: Tuple[int, int] = (8, 8),
        scale: float = 1.05,
    ) -> None:
        self.detect_width = int(detect_width)
        self.win_stride = win_stride
        self.padding = padding
        self.scale = float(scale)
        self._hog = None  # built lazily

    def _descriptor(self):
        if self._hog is None:
            cv2 = _cv2()
            hog = cv2.HOGDescriptor()
            hog.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())
            self._hog = hog
        return self._hog

    def detect(self, frame) -> List[Detection]:
        cv2 = _cv2()
        np = _np()
        h0, w0 = frame.shape[:2]
        scale = self.detect_width / float(w0) if w0 > self.detect_width else 1.0
        if scale != 1.0:
            small = cv2.resize(frame, (int(w0 * scale), int(h0 * scale)))
        else:
            small = frame
        rects, weights = self._descriptor().detectMultiScale(
            small,
            winStride=self.win_stride,
            padding=self.padding,
            scale=self.scale,
        )
        out: List[Detection] = []
        inv = 1.0 / scale
        flat = list(np.asarray(weights).ravel()) if len(rects) else []
        for (x, y, w, h), wt in zip(rects, flat):
            out.append(
                Detection(
                    x=float(x) * inv,
                    y=float(y) * inv,
                    w=float(w) * inv,
                    h=float(h) * inv,
                    score=_sigmoid(float(wt)),
                )
            )
        return out


def best_detection(dets: List[Detection]) -> Optional[Detection]:
    """Pick the largest (closest / most prominent) person box, or None."""
    if not dets:
        return None
    return max(dets, key=lambda d: d.area)


# --- box tracker interface + impls -------------------------------------------
@runtime_checkable
class BoxTracker(Protocol):
    """A cheap per-frame box tracker that fills between detections."""

    def init(self, frame, bbox: Bbox) -> None:  # pragma: no cover - protocol
        ...

    def update(self, frame) -> Tuple[bool, Bbox, float]:  # pragma: no cover
        ...


def iou(a: Bbox, b: Bbox) -> float:
    """Intersection-over-union of two pixel boxes (centroid/IoU reconcile helper)."""
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix1, iy1 = max(ax, bx), max(ay, by)
    ix2, iy2 = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    iw, ih = max(0, ix2 - ix1), max(0, iy2 - iy1)
    inter = iw * ih
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


class SimpleBoxTracker:
    """Pure-Python fallback tracker — no cv2 required.

    OpenCV's appearance trackers (CSRT/KCF) live in the contrib ``tracking``
    module and are absent from many opencv builds (and from the wheel used here).
    Without them we cannot do appearance tracking from raw pixels cheaply, so this
    fallback simply **holds the last detector box and linearly decays its
    confidence** every tick until the next detector re-seed. It deliberately leans
    on the detector cadence (track-by-detection still works; it is just less
    smooth between detections). This keeps FOLLOW running on a stock opencv wheel
    rather than hard-failing — honest degradation, not a silent stub.
    """

    def __init__(self, *, decay: float = 0.12) -> None:
        self.decay = float(decay)
        self._bbox: Optional[Bbox] = None
        self._conf = 0.0

    def init(self, frame, bbox: Bbox) -> None:
        self._bbox = tuple(int(v) for v in bbox)  # type: ignore[assignment]
        self._conf = 1.0

    def update(self, frame) -> Tuple[bool, Bbox, float]:
        if self._bbox is None:
            return False, (0, 0, 0, 0), 0.0
        self._conf = max(0.0, self._conf - self.decay)
        return self._conf > 0.0, self._bbox, self._conf


class _Cv2BoxTracker:
    """Adapter over an OpenCV tracker object (CSRT/KCF), if one is available."""

    def __init__(self, factory: Callable[[], object]) -> None:
        self._factory = factory
        self._tracker = None

    def init(self, frame, bbox: Bbox) -> None:
        self._tracker = self._factory()  # type: ignore[assignment]
        x, y, w, h = (int(v) for v in bbox)
        self._tracker.init(frame, (x, y, w, h))  # type: ignore[union-attr]

    def update(self, frame) -> Tuple[bool, Bbox, float]:
        if self._tracker is None:
            return False, (0, 0, 0, 0), 0.0
        ok, box = self._tracker.update(frame)  # type: ignore[union-attr]
        if not ok:
            return False, (0, 0, 0, 0), 0.0
        x, y, w, h = (int(round(v)) for v in box)
        # cv2 trackers expose no scalar confidence; a successful update == 1.0.
        return True, (x, y, w, h), 1.0


def _cv2_tracker_factory() -> Optional[Callable[[], object]]:
    """Return a zero-arg cv2 tracker factory, handling API-spelling differences.

    OpenCV moved tracker constructors around across versions:
      * ``cv2.legacy.TrackerCSRT_create`` (contrib, 4.5.x legacy namespace)
      * ``cv2.TrackerCSRT_create``        (older contrib)
      * ``cv2.TrackerCSRT.create``        (newer object-style)
    We probe CSRT (accurate) then KCF (faster) across all three spellings and
    return None if the build ships no tracker at all.
    """
    try:
        cv2 = _cv2()
    except Exception:
        return None
    legacy = getattr(cv2, "legacy", None)
    for name in ("TrackerCSRT", "TrackerKCF"):
        for ns in (cv2, legacy):
            if ns is None:
                continue
            create = getattr(ns, f"{name}_create", None)
            if callable(create):
                return create
            klass = getattr(ns, name, None)
            create2 = getattr(klass, "create", None) if klass is not None else None
            if callable(create2):
                return create2
    return None


def make_box_tracker() -> BoxTracker:
    """Build the best available box tracker (real cv2 tracker, else the fallback)."""
    factory = _cv2_tracker_factory()
    if factory is not None:
        return _Cv2BoxTracker(factory)
    return SimpleBoxTracker()


# --- the track-by-detection tracker ------------------------------------------
class PersonTracker:
    """Track-by-detection: a fast tracker re-seeded by a slow detector (§4).

    Each :meth:`update` runs the cheap box tracker; every ``detect_every`` ticks
    (or whenever there is no live track) it re-runs the detector to **re-seed and
    validate** the box. ``ticks_since_last_detector_confirmation`` is the honesty
    signal the FOLLOW loop uses to decide coast-vs-stop (software-spec.md §2.2/§4):
    it is reset to 0 on every detector confirmation and incremented otherwise.

    On the Pi the detector would run in a child *process* (GIL, §2.6); here on the
    laptop it runs inline — fine for dev, and the ``yalp follow --benchmark`` path
    measures exactly the fps this inline cost yields (Gate H, roadmap.md).
    """

    def __init__(
        self,
        detector: Optional[Detector] = None,
        tracker_factory: Optional[Callable[[], BoxTracker]] = None,
        *,
        detect_every: int = 8,
        min_score: float = 0.0,
        reseed_iou: float = 0.2,
    ) -> None:
        self.detector = detector if detector is not None else HOGPersonDetector()
        self.tracker_factory = tracker_factory or make_box_tracker
        self.detect_every = max(1, int(detect_every))
        self.min_score = float(min_score)
        self.reseed_iou = float(reseed_iou)

        self._tracker: Optional[BoxTracker] = None
        self._bbox: Optional[Bbox] = None
        self._score = 0.0
        self._ticks_since_confirm = 0

    def reset(self) -> None:
        """Drop any live track so the next update re-detects from scratch."""
        self._tracker = None
        self._bbox = None
        self._score = 0.0
        self._ticks_since_confirm = 0

    def update(self, frame) -> TrackResult:
        # Re-detect when there is no live track or the box is due for validation.
        if self._tracker is None or self._ticks_since_confirm >= self.detect_every:
            return self._run_detector(frame)
        return self._run_tracker(frame)

    # -- internals -----------------------------------------------------------
    def _run_detector(self, frame) -> TrackResult:
        det = best_detection(self.detector.detect(frame))
        if det is None:
            # The detector ran and saw no person: this is a strong "lost" signal.
            self.reset()
            self._ticks_since_confirm = 0  # we *did* just run the detector
            return TrackResult(
                target_visible=False,
                bbox=None,
                score=0.0,
                ticks_since_last_detector_confirmation=0,
                detector_ran=True,
            )
        # Re-seed the cheap tracker on the fresh, validated detector box.
        self._bbox = det.bbox
        self._score = det.score
        self._ticks_since_confirm = 0
        self._tracker = self.tracker_factory()
        self._tracker.init(frame, self._bbox)
        return TrackResult(
            target_visible=True,
            bbox=self._bbox,
            score=self._score,
            ticks_since_last_detector_confirmation=0,
            detector_ran=True,
        )

    def _run_tracker(self, frame) -> TrackResult:
        assert self._tracker is not None
        ok, bbox, score = self._tracker.update(frame)
        self._ticks_since_confirm += 1
        if not ok or score < self.min_score:
            self.reset()
            return TrackResult(
                target_visible=False,
                bbox=None,
                score=0.0,
                ticks_since_last_detector_confirmation=self._ticks_since_confirm,
                detector_ran=False,
            )
        self._bbox = tuple(int(v) for v in bbox)  # type: ignore[assignment]
        self._score = float(score)
        return TrackResult(
            target_visible=True,
            bbox=self._bbox,
            score=self._score,
            ticks_since_last_detector_confirmation=self._ticks_since_confirm,
            detector_ran=False,
        )


__all__ = [
    "Bbox",
    "Detection",
    "TrackResult",
    "Detector",
    "HOGPersonDetector",
    "best_detection",
    "BoxTracker",
    "SimpleBoxTracker",
    "make_box_tracker",
    "iou",
    "PersonTracker",
]
