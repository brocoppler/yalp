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
    the full-body detectors — trained on standing bodies — cannot see. For the
    ROBOT looking across a room the right default is the ORIENTATION-AGNOSTIC
    :class:`DnnPersonDetector`: a real **body** detector run through OpenCV's
    BUILT-IN ``cv2.dnn`` module (MobileNet-SSD, **no new pip dependency** — only a
    downloadable, cached model file) that fires from ANY angle — front, **back**,
    side — so follow keeps working when the user walks AWAY. :class:`HogPersonDetector`
    is OpenCV's built-in (download-free) standing-body alternative, and
    ``AutoDetector`` prefers the cv2.dnn person detector with a face fallback for
    close-ups. The cv2.dnn detector is also the **Gate H** candidate: Gate H
    (roadmap.md) decides whether the Pi sustains the ``config.GATE_H_GO_HZ`` floor
    that makes track-by-detection viable. Nothing else in FOLLOW changes across the
    swap — the model loads lazily from a cached file (see
    :func:`ensure_dnn_model_files`), so this module still imports with no OpenCV and
    no model present, and tests inject a fake net.
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

import logging
import math
from dataclasses import dataclass
from typing import List, Optional, Protocol, Tuple

from .. import config

logger = logging.getLogger(__name__)

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


# --------------------------------------------------------------------------- #
# The room-range, ORIENTATION-AGNOSTIC default: a cv2.dnn person detector
# --------------------------------------------------------------------------- #
def ensure_dnn_model_files(
    model_dir: Optional[str] = None,
    *,
    prototxt_url: str = config.FOLLOW_DNN_PROTOTXT_URL,
    caffemodel_url: str = config.FOLLOW_DNN_CAFFEMODEL_URL,
    prototxt_name: str = config.FOLLOW_DNN_PROTOTXT_NAME,
    caffemodel_name: str = config.FOLLOW_DNN_CAFFEMODEL_NAME,
    download: bool = True,
) -> Tuple[str, str]:
    """Locate (and, if missing, download once) the MobileNet-SSD model files.

    Returns ``(prototxt_path, caffemodel_path)`` for files cached under
    ``model_dir`` (default :data:`config.FOLLOW_MODEL_CACHE_DIR`). This is **lazy**:
    it is only ever called when a real :class:`DnnPersonDetector` net is first
    built — never at import and never in tests (which inject a fake net). Network
    access only happens when a file is actually missing.

    GRACEFUL FAILURE: if a file is missing and the download fails (offline, blocked
    mirror, …) this raises a clear :class:`RuntimeError` that tells the operator the
    exact filenames, the cache path to drop them into, and the source URLs — never a
    cryptic stack trace deep inside OpenCV.
    """
    import os
    from pathlib import Path

    cache = Path(model_dir or config.FOLLOW_MODEL_CACHE_DIR).expanduser()
    proto = cache / prototxt_name
    model = cache / caffemodel_name
    if proto.exists() and model.exists():
        return str(proto), str(model)

    wants = [(prototxt_url, proto), (caffemodel_url, model)]
    missing = [dest.name for _, dest in wants if not dest.exists()]

    def _instructions(extra: str = "") -> str:
        return (
            f"Person detector model files are missing{(' — ' + extra) if extra else ''}.\n"
            f"  Needed files : {prototxt_name}, {caffemodel_name}\n"
            f"  Cache path   : {cache}\n"
            f"  Sources      : {prototxt_url}\n"
            f"                 {caffemodel_url}\n"
            f"Drop both files into the cache path above (or set "
            f"YALP_MODEL_CACHE_DIR / YALP_DNN_*_URL), then retry. To use the "
            f"webcam-friendly desk detector instead, run with '--detector face'."
        )

    if not download:
        raise FileNotFoundError(_instructions(f"missing {', '.join(missing)}"))

    try:
        cache.mkdir(parents=True, exist_ok=True)
        for url, dest in wants:
            if not dest.exists():
                _download_file(url, dest)
    except Exception as exc:  # offline / blocked / partial write
        raise RuntimeError(_instructions(f"download failed: {exc}")) from exc

    return str(proto), str(model)


def _download_file(url: str, dest) -> None:
    """Download ``url`` to ``dest`` atomically (temp file + rename), stdlib only."""
    import os
    import tempfile
    import urllib.request
    from pathlib import Path

    dest = Path(dest)
    fd, tmp = tempfile.mkstemp(dir=str(dest.parent), suffix=".part")
    os.close(fd)
    try:
        with urllib.request.urlopen(url, timeout=30) as resp, open(tmp, "wb") as out:
            while True:
                chunk = resp.read(1 << 16)
                if not chunk:
                    break
                out.write(chunk)
        os.replace(tmp, str(dest))
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:  # pragma: no cover - best-effort cleanup
                pass


class DnnPersonDetector:
    """Orientation-agnostic person detector via OpenCV's BUILT-IN ``cv2.dnn``.

    This is the REAL person detector for the robot's FOLLOW: a **body** detector
    (not a face detector), so it fires whether the person faces the camera, turns
    their **back**, or is seen from the **side** — which is exactly what makes
    robot-follow work when the user walks AWAY. It runs MobileNet-SSD (Caffe) with
    ``cv2.dnn.readNetFromCaffe`` (NO new pip dependency — only a downloadable model
    file), keeps the Pascal-VOC ``person`` class (index 15), applies a confidence
    threshold, and returns the **largest** person as the target.

    Laziness / mockability (so tests need neither the model nor a camera):

      * ``cv2`` is imported lazily, only inside :meth:`detect` / net construction.
      * The net is built lazily on the FIRST :meth:`detect` call (never at import
        or construction), via :func:`ensure_dnn_model_files` (download-once-cache).
      * Tests inject a fake ``net`` (with a ``setInput``/``forward`` pair returning
        canned raw detections) so the model loader is never invoked.
    """

    def __init__(
        self,
        *,
        net=None,
        confidence: float = config.FOLLOW_DNN_CONFIDENCE,
        input_size: int = config.FOLLOW_DNN_INPUT_SIZE,
        person_class_id: int = config.FOLLOW_DNN_PERSON_CLASS_ID,
        model_dir: Optional[str] = None,
        loader=ensure_dnn_model_files,
    ) -> None:
        self._net = net  # injected fake (tests) or None -> lazy real load
        self.confidence = float(confidence)
        self.input_size = int(input_size)
        self.person_class_id = int(person_class_id)
        self.model_dir = model_dir
        self._loader = loader
        # Reported for the benchmark read-out (the net resizes to a square input,
        # so the effective "detect width" is the network input edge, not the frame).
        self.detect_width = int(input_size)

    # -- lazy net (real load only on first use) ------------------------------
    def _get_net(self):
        if self._net is None:
            import cv2  # lazy: only when a REAL net is actually built

            proto, model = self._loader(self.model_dir)
            self._net = cv2.dnn.readNetFromCaffe(proto, model)
        return self._net

    def detect(self, frame) -> List[Detection]:
        if frame is None:
            return []
        import cv2  # lazy

        net = self._get_net()
        h, w = frame.shape[:2]
        # MobileNet-SSD preprocessing: scale 1/127.5, mean 127.5, 300x300 input.
        blob = cv2.dnn.blobFromImage(
            frame,
            scalefactor=0.007843,
            size=(self.input_size, self.input_size),
            mean=127.5,
        )
        net.setInput(blob)
        raw = net.forward()  # shape (1, 1, N, 7): [_, class, conf, x1, y1, x2, y2]

        out: List[Detection] = []
        n = raw.shape[2]
        for i in range(n):
            cls = int(raw[0, 0, i, 1])
            if cls != self.person_class_id:  # keep ONLY the person class
                continue
            conf = float(raw[0, 0, i, 2])
            if conf < self.confidence:  # confidence threshold
                continue
            x1 = raw[0, 0, i, 3] * w
            y1 = raw[0, 0, i, 4] * h
            x2 = raw[0, 0, i, 5] * w
            y2 = raw[0, 0, i, 6] * h
            bx = max(0, int(round(x1)))
            by = max(0, int(round(y1)))
            bw = int(round(x2 - x1))
            bh = int(round(y2 - y1))
            if bw <= 0 or bh <= 0:
                continue
            out.append(Detection(bbox=(bx, by, bw, bh), score=conf))
        if not out:
            return out
        # The nearest person is the largest box -> the FOLLOW target.
        return [max(out, key=lambda d: d.area)]


class AutoDetector:
    """Prefer the orientation-agnostic PERSON detector; fall back to FACE close-up.

    Blend (documented): at room range a standing person — from ANY angle — is best
    caught by the cv2.dnn :class:`DnnPersonDetector`, so we try it FIRST. When it
    finds nobody (e.g. a desk-range close-up that frames only head+shoulders, which
    a whole-body detector can miss) we fall back to the cheap :class:`FaceDetector`.
    Both are built lazily.

    Failure handling is DELIBERATELY not trigger-happy. A single person-detector
    exception is often TRANSIENT (a momentary decode hiccup, a one-off bad frame),
    and permanently disabling the room-range body detector on the first stumble is a
    silent, in-the-field quality downgrade to face-only (desk-range) tracking. So:

      * every person-detector exception is logged at WARNING (with the exception),
      * we only DOWNGRADE to face-only after :attr:`max_consecutive_failures`
        failures **in a row** (default 3), announced once, loudly, at WARNING,
      * any SUCCESSFUL person-detector run resets the streak to zero.

    The failure streak (:attr:`person_failure_count`) and the latched
    :attr:`person_downgraded` flag are public so operators and tests can observe the
    degradation instead of guessing at it.
    """

    #: Consecutive person-detector failures tolerated before downgrading to face-only.
    max_consecutive_failures = 3

    def __init__(self, *, detect_width: int = config.FOLLOW_DETECT_WIDTH) -> None:
        self.detect_width = int(detect_width)
        self._person: Optional[DnnPersonDetector] = None
        self._face: Optional[FaceDetector] = None
        #: Number of consecutive person-detector failures (reset on any success).
        self.person_failure_count = 0
        #: True once the person detector has been permanently disabled (downgraded).
        self.person_downgraded = False

    def detect(self, frame) -> List[Detection]:
        if not self.person_downgraded:
            try:
                if self._person is None:
                    self._person = DnnPersonDetector()
                people = self._person.detect(frame)
            except Exception as exc:
                # Do NOT disable on a single (often transient) failure — count
                # consecutive misses and only downgrade after a sustained streak.
                self.person_failure_count += 1
                logger.warning(
                    "AutoDetector: person detector failed "
                    "(%d/%d consecutive): %s: %s",
                    self.person_failure_count,
                    self.max_consecutive_failures,
                    type(exc).__name__,
                    exc,
                )
                if self.person_failure_count >= self.max_consecutive_failures:
                    # Latch the downgrade and announce it ONCE, loudly. From here
                    # on the person branch is skipped, so this never repeats.
                    self.person_downgraded = True
                    logger.warning(
                        "AutoDetector: DOWNGRADING to face-only detection — the "
                        "person (body) detector failed %d times in a row and is now "
                        "disabled for the rest of this run. FOLLOW will track only "
                        "head+shoulders at desk range (no front/back/side body "
                        "tracking). Last error: %s: %s",
                        self.person_failure_count,
                        type(exc).__name__,
                        exc,
                    )
            else:
                # A successful person-detector run clears the failure streak so an
                # isolated blip never accumulates toward a downgrade.
                self.person_failure_count = 0
                if people:
                    return people
        if self._face is None:
            self._face = FaceDetector(detect_width=self.detect_width)
        return self._face.detect(frame)


def build_detector(
    name: Optional[str] = None, *, detect_width: int = config.FOLLOW_DETECT_WIDTH
) -> Detector:
    """Construct a detector by name: ``face`` (default), ``hog``, ``person`` or ``auto``.

    The default ``face`` is reliable at *desk* range (head+shoulders webcam
    framing); ``hog`` is OpenCV's built-in standing-body detector; ``person`` is
    the ORIENTATION-AGNOSTIC cv2.dnn MobileNet-SSD body detector (front/back/side
    at room range — what the ROBOT's follow defaults to); ``auto`` prefers the
    person detector and falls back to face for close-ups. All share the
    :class:`Detector` interface, so FOLLOW is unchanged behind any of them. The
    ``person`` net + model files load lazily on first use (never here / at import).
    """
    key = (name or config.FOLLOW_DETECTOR_DEFAULT).strip().lower()
    if key == "face":
        return FaceDetector(detect_width=detect_width)
    if key == "hog":
        return HogPersonDetector(detect_width=detect_width)
    if key == "person":
        return DnnPersonDetector()
    if key == "auto":
        return AutoDetector(detect_width=detect_width)
    raise ValueError(f"unknown detector {name!r} (choose face, hog, person or auto)")


def detect_interval_for(name: Optional[str] = None) -> int:
    """Default re-detect cadence (ticks) for a detector name.

    The face cascade is cheap, so face re-detects every couple ticks; the slower
    HOG / cv2.dnn ``person`` / ``auto`` (person-first) detectors use the longer
    ``FOLLOW_DETECT_INTERVAL_TICKS``.
    """
    key = (name or config.FOLLOW_DETECTOR_DEFAULT).strip().lower()
    if key == "face":
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
    grace_ticks:
        HYSTERESIS window: how many ticks the cheap tracker may COAST a box across
        a gap between detector hits while still reporting it visible. As long as
        the last SUCCESSFUL detection is younger than this, a coasted box stays
        "tracking" (so brief, normal detection gaps don't flip to "lost"); only
        after the grace elapses with no fresh detection does the box get dropped.
        Kept >= ``detect_interval`` so a normal detector cadence never reads stale.
    """

    def __init__(
        self,
        detector: Optional[Detector] = None,
        *,
        detect_interval: int = config.FOLLOW_DETECT_INTERVAL_TICKS,
        track_min_score: float = config.FOLLOW_TRACK_MIN_SCORE,
        score_decay: float = 0.92,
        grace_ticks: int = config.FOLLOW_LOST_GRACE_TICKS,
        min_box_area_frac: float = config.FOLLOW_MIN_BOX_AREA_FRAC,
        edge_margin_frac: float = config.FOLLOW_EDGE_MARGIN_FRAC,
        max_jump_frac: float = config.FOLLOW_MAX_JUMP_FRAC,
    ) -> None:
        self._detector = detector
        self.detect_interval = max(1, int(detect_interval))
        self.track_min_score = float(track_min_score)
        self.score_decay = float(score_decay)
        # HYSTERESIS / lost-grace window: keep coasting (and reporting visible) for
        # up to this many ticks after the last SUCCESSFUL detection. The detector
        # only fires intermittently, so a brief, normal detection gap is NOT "lost"
        # — the cheap tracker bridges it. Only after this window elapses with NO
        # fresh detection do we genuinely let go — a short window, never a
        # permanently latched dead box. This is the flicker fix: a single missed
        # detection no longer nukes a live box.
        self.grace_ticks = max(1, int(grace_ticks))
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
        return self._coast(frame, detector_ran=False)

    # -- internals -----------------------------------------------------------
    def _detect_and_reseed(self, frame) -> TrackResult:
        detections = self._get_detector().detect(frame)
        h, w = frame.shape[:2]
        prev = self._bbox
        # Drop implausible one-off detections (tiny, hard against a frame edge
        # with no continuity, or an implausible jump) so a far-edge false positive
        # like the observed x=-0.83 can never get latched into a stale lock.
        plausible = [d for d in detections if self._plausible(d.bbox, w, h, prev)]
        if not plausible:
            # No fresh/trustworthy detection THIS tick. Do NOT nuke a live box on a
            # single miss (that strictness caused the acquired/lost flicker): within
            # the grace window the cheap tracker COASTS the existing box, so a brief,
            # normal detector gap (or a single missed/teleport-rejected detection)
            # does not read as "lost". With no box at all there is nothing to coast,
            # so this honestly reports lost. Only once the grace elapses with no
            # fresh confirmation do we genuinely let go (see _coast).
            return self._coast(frame, detector_ran=True)

        best = max(plausible, key=lambda d: d.area)
        self._bbox = best.bbox
        self._score = best.score
        self._tracker = self._make_tracker(frame, best.bbox)
        # A fresh, plausible detection re-confirms the box: reset the grace clock.
        self._ticks_since_detector = 0  # fresh detector confirmation -> reset
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

    def _coast(self, frame, detector_ran: bool = False) -> TrackResult:
        """Coast the box on the cheap tracker within the lost-grace window (HYSTERESIS).

        The box is kept alive — and reported VISIBLE — for as long as the cheap
        tracker can still hold it AND its last SUCCESSFUL detection is younger than
        ``grace_ticks``. The cheap tracker BRIDGES the normal gap between detector
        hits, so the state stays "tracking" (matching the drawn green box) instead
        of flip-flopping to "lost". Once the grace elapses with no fresh detection,
        or the tracker can no longer hold a box, we genuinely drop the box -> lost
        (never a permanently latched dead box). It is reached both from a between-
        detection tick and from a detection tick that found nobody; ``detector_ran``
        records whether a detector actually fired this tick.
        """
        self._ticks_since_detector += 1
        if self._tracker is None or self._bbox is None:
            # Nothing to coast on (never acquired / already released) -> lost. A
            # fresh detect runs next tick because the box is None.
            self._bbox, self._score, self._tracker = None, 0.0, None
            return TrackResult(False, None, 0.0, self._ticks_since_detector, detector_ran)

        if self._ticks_since_detector > self.grace_ticks:
            # Grace elapsed with no fresh detection -> let go (no dead-box latch).
            self._bbox, self._score, self._tracker = None, 0.0, None
            return TrackResult(False, None, 0.0, self._ticks_since_detector, detector_ran)

        ok, bbox = self._tracker.update(frame)
        if not ok or bbox is None:
            # The cheap tracker can no longer hold the box -> lost.
            self._bbox, self._score, self._tracker = None, 0.0, None
            return TrackResult(False, None, 0.0, self._ticks_since_detector, detector_ran)

        # A coasted box WITHIN the grace IS a valid (non-stale, drawable, steerable)
        # target. Score erodes gently as a reported-confidence signal, but the grace
        # — not a single missed detection — governs the lock.
        self._bbox = bbox
        self._score *= self.score_decay
        visible = self._score >= self.track_min_score
        return TrackResult(visible, self._bbox, self._score,
                           self._ticks_since_detector, detector_ran)

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
    "DnnPersonDetector",
    "AutoDetector",
    "ensure_dnn_model_files",
    "build_detector",
    "detect_interval_for",
    "PersonTracker",
]
