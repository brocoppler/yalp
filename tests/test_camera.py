"""Tests for the threaded Camera — no real camera, network, or SDK required.

The webcam warm-up is exercised with a FAKE VideoCapture whose ``read()`` returns
several black frames followed by a bright one, mirroring how a real sensor's
auto-exposure ramps up after the device opens. We assert the warm-up discards the
black frames and hands back an exposed one, and that the image-file / synthetic
sources publish immediately without any warm-up delay.
"""

from __future__ import annotations

import time

import numpy as np
import pytest

from yalp import camera as camera_mod
from yalp.camera import (
    Camera,
    WARMUP_BRIGHTNESS_THRESHOLD,
    WARMUP_MAX_FRAMES,
)


def _black(h: int = 8, w: int = 8) -> np.ndarray:
    return np.zeros((h, w, 3), dtype=np.uint8)


def _bright(value: int = 200, h: int = 8, w: int = 8) -> np.ndarray:
    return np.full((h, w, 3), value, dtype=np.uint8)


class FakeCap:
    """A stand-in cv2.VideoCapture: yields a scripted sequence of frames."""

    def __init__(self, frames):
        self._frames = list(frames)
        self.read_count = 0
        self.released = False

    def read(self):
        self.read_count += 1
        if self._frames:
            frame = self._frames.pop(0)
        else:
            # A real device keeps returning frames; repeat the last one.
            frame = _bright()
        return True, frame

    def isOpened(self):  # noqa: N802 - mirror the OpenCV API name
        return True

    def release(self):
        self.released = True

    def set(self, *_args):  # noqa: D401 - cv2 API shim
        return True


# --- webcam warm-up ---------------------------------------------------------
def test_warmup_skips_black_frames_and_returns_bright_one():
    # Five black frames (cold sensor), then a properly exposed frame.
    frames = [_black(), _black(), _black(), _black(), _black(), _bright()]
    cam = Camera(source="webcam")
    cam._cap = FakeCap(frames)

    warmed = cam._warm_up_webcam()

    assert warmed is not None
    brightness = float(warmed.mean())
    assert brightness >= WARMUP_BRIGHTNESS_THRESHOLD
    assert brightness > 0.0
    # It had to discard the black frames to reach the bright one.
    assert cam._cap.read_count == 6


def test_warmup_returns_after_budget_when_all_dark(caplog):
    # Lens covered / dark room: every frame is black. Warm-up must not hang; it
    # returns a frame anyway (after the discard budget) and logs a hint.
    cam = Camera(source="webcam")
    cam._cap = FakeCap([_black() for _ in range(WARMUP_MAX_FRAMES + 50)])

    with caplog.at_level("WARNING"):
        warmed = cam._warm_up_webcam()

    assert warmed is not None  # returned anyway, never None on a reading device
    assert float(warmed.mean()) < WARMUP_BRIGHTNESS_THRESHOLD
    # Bounded by the frame-count budget rather than spinning forever.
    assert cam._cap.read_count <= WARMUP_MAX_FRAMES
    assert any("very dark" in r.message for r in caplog.records)


def test_started_webcam_publishes_a_warmed_frame_not_the_black_first_one():
    frames = [_black(), _black(), _black(), _bright()]
    cam = Camera(source="webcam")

    # Pretend _configure_source opened a (fake) device.
    def _fake_configure():
        cam._cap = FakeCap(frames)

    cam._configure_source = _fake_configure  # type: ignore[assignment]
    cam.start()
    try:
        frame = cam.wait_for_frame(timeout=2.0)
    finally:
        cam.stop()

    assert frame is not None
    # The very first frame was black; the published frame must be the exposed one.
    assert float(frame.mean()) >= WARMUP_BRIGHTNESS_THRESHOLD


# --- non-webcam sources return immediately (no warm-up) ---------------------
def test_synthetic_source_returns_immediately_without_warmup_delay():
    cam = Camera(source="synthetic", width=16, height=16)
    cam.start()
    try:
        start = time.monotonic()
        frame = cam.wait_for_frame(timeout=2.0)
        elapsed = time.monotonic() - start
    finally:
        cam.stop()

    assert frame is not None
    # No warm-up budget is spent on synthetic frames.
    assert elapsed < 0.5


def test_synthetic_does_not_invoke_webcam_warmup(monkeypatch):
    called = {"warmup": False}

    def _boom(self):
        called["warmup"] = True
        return None

    monkeypatch.setattr(Camera, "_warm_up_webcam", _boom)

    cam = Camera(source="synthetic", width=16, height=16)
    cam.start()
    try:
        cam.wait_for_frame(timeout=1.0)
    finally:
        cam.stop()

    assert called["warmup"] is False


def test_webcam_falls_back_to_synthetic_when_no_device(monkeypatch):
    # No cv2 available -> the webcam source must fall back to synthetic frames so
    # dev/tests never require hardware, and still publish a frame.
    monkeypatch.setattr(camera_mod, "cv2", None)
    cam = Camera(source="webcam", width=16, height=16)
    cam.start()
    try:
        frame = cam.wait_for_frame(timeout=2.0)
    finally:
        cam.stop()

    assert cam.source == "synthetic"
    assert frame is not None
