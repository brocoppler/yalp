"""Threaded camera capture with a single-slot latest-frame buffer.

Per the spec the *reactive process owns the camera* and the blocking ``read()``
runs in its own capture thread that does nothing but loop read -> decode ->
write into a single-slot, double-buffered "latest frame" (software-spec.md §2.5).
Callers always pull the *newest* decoded frame and never block on ``read()`` — a
stale frame is fine (it's a snapshot, last-write-wins), a blocked tick is not.

For laptop-first development this is reusable and hardware-optional. Three
sources are supported so it runs with or without a camera:

  (a) ``webcam``    — cv2.VideoCapture(index): the laptop cam or the C270.
  (b) ``image``     — a still image file path, looped as the "live" frame.
  (c) ``synthetic`` — numpy-generated frames when no camera is available.

If a webcam cannot be opened, the Camera automatically falls back to synthetic
frames, so tests and dev never require real hardware.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

try:  # OpenCV is a runtime dep, but keep import failures legible.
    import cv2
except Exception as exc:  # pragma: no cover - exercised only without opencv
    cv2 = None  # type: ignore[assignment]
    _CV2_IMPORT_ERROR: Optional[Exception] = exc
else:
    _CV2_IMPORT_ERROR = None

# Default capture geometry. The detector wants ~320x240 (downscaled before
# inference); we capture a little larger and let consumers downscale as needed.
DEFAULT_WIDTH = 640
DEFAULT_HEIGHT = 480

# Cap the capture loop so we never drive the camera faster than the sensor-
# friendly cadence (software-spec.md §2.5 / hardware.md ~15 Hz ultrasonic).
DEFAULT_MAX_FPS = 15.0

# Real webcam warm-up. OpenCV's VideoCapture hands back the *first* frame the
# instant the device opens — before the sensor's auto-exposure / auto-gain have
# ramped up — so the first frames are black/very dark. Before publishing any
# frame to callers we read and DISCARD an initial burst, stopping as soon as a
# frame is non-trivially bright (mean pixel value above the threshold), or when
# the warm-up budget (time or frame count) elapses (then we publish the best
# frame seen anyway, so we never hang). This applies ONLY to the real webcam
# source; the image-file and synthetic sources publish immediately.
WARMUP_MAX_SECONDS = 1.5
WARMUP_MAX_FRAMES = 30
WARMUP_BRIGHTNESS_THRESHOLD = 16.0

# How long ``wait_for_frame`` blocks by default — generous enough to cover the
# webcam warm-up above plus the device-open cost.
DEFAULT_FRAME_TIMEOUT = 5.0

Frame = "np.ndarray"  # BGR uint8 HxWx3, the OpenCV convention.


class Camera:
    """A latest-frame camera with a background capture thread.

    Parameters
    ----------
    source:
        ``"webcam"``, ``"image"``, or ``"synthetic"``.
    index:
        VideoCapture device index for the webcam source (0 = default cam).
    path:
        Image file path for the ``image`` source.
    width, height:
        Frame geometry (used for synthetic frames and as a capture hint).
    max_fps:
        Upper bound on the capture loop rate.
    """

    def __init__(
        self,
        source: str = "webcam",
        *,
        index: int = 0,
        path: Optional[str] = None,
        width: int = DEFAULT_WIDTH,
        height: int = DEFAULT_HEIGHT,
        max_fps: float = DEFAULT_MAX_FPS,
    ) -> None:
        self.source = source
        self.index = index
        self.path = path
        self.width = width
        self.height = height
        self.max_fps = max_fps

        self._cap = None  # cv2.VideoCapture when source == "webcam"
        self._still: Optional[np.ndarray] = None  # the looped image frame
        self._latest: Optional[np.ndarray] = None  # single-slot latest frame
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        # Set once the capture thread has published a usable (warmed-up, for the
        # webcam source) frame — or given up trying. Lets the one-shot still path
        # avoid reading a black first frame.
        self._warmed = threading.Event()
        self._frame_count = 0

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> "Camera":
        """Open the source and start the background capture thread.

        Falls back to synthetic frames if a webcam or image cannot be opened.
        Returns ``self`` for convenient chaining.
        """
        self._configure_source()
        self._stop.clear()
        self._warmed.clear()
        self._thread = threading.Thread(
            target=self._capture_loop, name="yalp-camera", daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        """Stop the capture thread and release the device."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._cap is not None:
            try:
                self._cap.release()
            finally:
                self._cap = None

    def __enter__(self) -> "Camera":
        return self.start()

    def __exit__(self, *_exc) -> None:
        self.stop()

    # -- frame access --------------------------------------------------------
    def latest(self) -> Optional[np.ndarray]:
        """Return the newest decoded frame (BGR ndarray), or None if none yet.

        Never blocks on a device read — it returns whatever the capture thread
        last published.
        """
        with self._lock:
            return None if self._latest is None else self._latest.copy()

    def latest_with_id(self) -> Tuple[Optional[np.ndarray], int]:
        """Return the newest frame AND its monotonic capture id, read ATOMICALLY.

        Same single-slot / last-write-wins semantics as :meth:`latest` (never
        blocks on a device read), but also returns the capture counter for that
        frame so a consumer can tell WHICH frame a result was computed from — and
        spot a frozen/stalled source (the id stops advancing). The frame and its id
        are read under the SAME lock, so they always correspond to the same capture
        (no torn read of "this frame with that id"). ``frame`` is ``None`` (and the
        id is 0) until the first frame is published. Used by the FOLLOW perception
        worker to stamp each :class:`~yalp.reactive.perception.Observation`.
        """
        with self._lock:
            frame = None if self._latest is None else self._latest.copy()
            return frame, self._frame_count

    def wait_for_frame(
        self, timeout: float = DEFAULT_FRAME_TIMEOUT
    ) -> Optional[np.ndarray]:
        """Block (briefly) until a usable frame is available, then return it.

        For the webcam source the capture thread does not publish anything until
        it has warmed the sensor up (see :meth:`_warm_up_webcam`), so the frame
        returned here is never the camera's black first frame. The image-file and
        synthetic sources publish their first frame immediately, so this returns
        without any warm-up delay.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            frame = self.latest()
            if frame is not None:
                return frame
            time.sleep(0.01)
        return self.latest()

    # -- internals -----------------------------------------------------------
    def _configure_source(self) -> None:
        """Resolve the configured source, falling back to synthetic on failure."""
        if self.source == "webcam":
            if cv2 is None:
                self.source = "synthetic"
                return
            cap = cv2.VideoCapture(self.index)
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
            if not cap.isOpened():
                cap.release()
                self.source = "synthetic"
                return
            self._cap = cap
        elif self.source == "image":
            if cv2 is None or not self.path:
                self.source = "synthetic"
                return
            img = cv2.imread(self.path)
            if img is None:
                self.source = "synthetic"
                return
            self._still = img
        # "synthetic" needs no setup.

    def _capture_loop(self) -> None:
        min_dt = 1.0 / self.max_fps if self.max_fps > 0 else 0.0
        # Real webcams need a warm-up before the first published frame is usable;
        # other sources publish immediately so tests/dev stay fast.
        if self.source == "webcam" and self._cap is not None:
            warmed = self._warm_up_webcam()
            if warmed is not None:
                with self._lock:
                    self._latest = warmed
                    self._frame_count += 1
        self._warmed.set()
        while not self._stop.is_set():
            t0 = time.monotonic()
            frame = self._grab_one()
            if frame is not None:
                with self._lock:
                    self._latest = frame
                    self._frame_count += 1
            elapsed = time.monotonic() - t0
            if min_dt > elapsed:
                time.sleep(min_dt - elapsed)

    def _warm_up_webcam(self) -> Optional[np.ndarray]:
        """Read and discard early (dark) webcam frames until one is exposed.

        Reads up to :data:`WARMUP_MAX_FRAMES` frames for at most
        :data:`WARMUP_MAX_SECONDS` seconds, returning the first frame whose mean
        pixel value clears :data:`WARMUP_BRIGHTNESS_THRESHOLD`. If the budget is
        exhausted (lens covered, truly dark room, or a slow sensor) the brightest
        frame seen is returned anyway — we never hang — with a one-line hint.
        """
        if self._cap is None:  # pragma: no cover - guarded by caller
            return None
        deadline = time.monotonic() + WARMUP_MAX_SECONDS
        best: Optional[np.ndarray] = None
        best_brightness = -1.0
        frames_read = 0
        while frames_read < WARMUP_MAX_FRAMES and time.monotonic() < deadline:
            if self._stop.is_set():
                break
            ok, frame = self._cap.read()
            if not ok or frame is None:
                continue
            frames_read += 1
            brightness = float(np.asarray(frame).mean())
            if brightness > best_brightness:
                best_brightness = brightness
                best = frame
            if brightness >= WARMUP_BRIGHTNESS_THRESHOLD:
                return frame
        if best is not None and best_brightness < WARMUP_BRIGHTNESS_THRESHOLD:
            logger.warning(
                "camera frame is very dark — check lighting / lens cover "
                "(mean brightness %.1f after %d warm-up frame(s))",
                max(best_brightness, 0.0),
                frames_read,
            )
        return best

    def _grab_one(self) -> Optional[np.ndarray]:
        if self.source == "webcam" and self._cap is not None:
            ok, frame = self._cap.read()
            return frame if ok else None
        if self.source == "image" and self._still is not None:
            return self._still.copy()
        return self._synthetic_frame()

    def _synthetic_frame(self) -> np.ndarray:
        """A cheap moving gradient so consumers see a changing frame."""
        h, w = self.height, self.width
        frame = np.zeros((h, w, 3), dtype=np.uint8)
        shift = self._frame_count * 4
        xs = (np.arange(w) + shift) % 256
        frame[:, :, 0] = xs.astype(np.uint8)  # B
        frame[:, :, 1] = (np.arange(h)[:, None] % 256).astype(np.uint8)  # G
        frame[:, :, 2] = ((xs + shift) % 256).astype(np.uint8)  # R
        return frame


def encode_jpeg(
    frame: np.ndarray,
    *,
    max_width: int = 640,
    quality: int = 80,
) -> bytes:
    """Downscale (if wider than ``max_width``) and JPEG-encode a BGR frame.

    Returns the JPEG bytes, ready for base64 + upload to the VLM. Keeping the
    uploaded still small is the biggest controllable latency lever
    (software-spec.md §3.1).
    """
    if cv2 is None:  # pragma: no cover - exercised only without opencv
        raise RuntimeError(
            "opencv-python is required to encode frames "
            f"(import failed: {_CV2_IMPORT_ERROR})"
        )
    h, w = frame.shape[:2]
    if w > max_width:
        scale = max_width / float(w)
        frame = cv2.resize(frame, (max_width, int(round(h * scale))))
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, int(quality)])
    if not ok:
        raise RuntimeError("cv2.imencode failed to encode frame as JPEG")
    return buf.tobytes()


__all__ = [
    "Camera",
    "encode_jpeg",
    "DEFAULT_WIDTH",
    "DEFAULT_HEIGHT",
    "WARMUP_MAX_SECONDS",
    "WARMUP_MAX_FRAMES",
    "WARMUP_BRIGHTNESS_THRESHOLD",
]
