"""Tests for the camera-only yaw estimator (:mod:`yalp.reactive.visual_odometry`).

The estimator turns the horizontal image shift between consecutive frames into
a yaw angle. These pin (1) the SIGN convention — scene moving RIGHT in the
image = camera turned LEFT = positive yaw, verified on the robot 2026-09-15 with
a commanded pivot — (2) the magnitude through the HFOV, (3) that a textureless
or black frame is reported as LOW CONFIDENCE rather than "no rotation", and
(4) that the numpy fallback agrees with the OpenCV phase-correlation path.
"""

from __future__ import annotations

import numpy as np
import pytest

from yalp.reactive.visual_odometry import YawEstimator, shift_to_yaw_deg


def _textured_scene(width: int = 900, height: int = 480, seed: int = 0) -> np.ndarray:
    """A wide, smooth, textured BGR scene (random blobs) to crop frames from."""
    rng = np.random.default_rng(seed)
    coarse = rng.integers(0, 255, size=(height // 8, width // 8, 3)).astype(np.float32)
    # Bilinear-ish upsample by repetition + a box blur to get smooth blobs.
    img = np.repeat(np.repeat(coarse, 8, axis=0), 8, axis=1)
    height, width = img.shape[0], img.shape[1]  # multiples of 8, may be < requested
    k = 9
    pad = np.pad(img, ((k, k), (k, k), (0, 0)), mode="edge")
    acc = np.zeros_like(img)
    for dy in range(-k // 2, k // 2 + 1):
        for dx in range(-k // 2, k // 2 + 1):
            acc += pad[k + dy : k + dy + height, k + dx : k + dx + width]
    acc /= float(k * k)
    return np.clip(acc, 0, 255).astype(np.uint8)


def _frame(scene: np.ndarray, x0: int, width: int = 640) -> np.ndarray:
    return scene[:, x0 : x0 + width].copy()


# --------------------------------------------------------------------------- #
# 1. Pure geometry.
# --------------------------------------------------------------------------- #
def test_shift_to_yaw_scales_by_hfov_over_width():
    assert shift_to_yaw_deg(0.0, 640, 55.0) == 0.0
    assert shift_to_yaw_deg(64.0, 640, 55.0) == pytest.approx(5.5)
    assert shift_to_yaw_deg(-8.0, 160, 55.0) == pytest.approx(-2.75)
    assert shift_to_yaw_deg(10.0, 0, 55.0) == 0.0  # degenerate width -> no yaw


# --------------------------------------------------------------------------- #
# 2. First frame yields nothing; the second yields a sample.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("prefer_cv2", [True, False])
def test_first_frame_is_reference_only(prefer_cv2):
    scene = _textured_scene()
    est = YawEstimator(hfov_deg=55.0, prefer_cv2=prefer_cv2)
    assert est.update(_frame(scene, 100)) is None
    assert est.update(_frame(scene, 100)) is not None


# --------------------------------------------------------------------------- #
# 3. SIGN + MAGNITUDE: scene content moving RIGHT by k px = yaw LEFT by k*HFOV/W.
#    (Cropping further LEFT into the scene moves the content RIGHT in the frame.)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("prefer_cv2", [True, False])
@pytest.mark.parametrize("shift_px", [8, 24, -16])
def test_scene_moving_right_is_positive_left_yaw(prefer_cv2, shift_px):
    scene = _textured_scene()
    est = YawEstimator(hfov_deg=55.0, prefer_cv2=prefer_cv2)
    est.update(_frame(scene, 130))
    sample = est.update(_frame(scene, 130 - shift_px))
    assert sample is not None
    expected = shift_to_yaw_deg(shift_px, 640, 55.0)
    assert sample.delta_deg == pytest.approx(expected, abs=0.35)
    assert np.sign(sample.delta_deg) == np.sign(shift_px)
    assert sample.response > 0.5


@pytest.mark.parametrize("prefer_cv2", [True, False])
def test_static_scene_reads_zero_with_high_confidence(prefer_cv2):
    scene = _textured_scene()
    est = YawEstimator(hfov_deg=55.0, prefer_cv2=prefer_cv2)
    est.update(_frame(scene, 200))
    sample = est.update(_frame(scene, 200))
    assert sample is not None
    assert sample.delta_deg == pytest.approx(0.0, abs=0.05)
    assert sample.response > 0.9


def test_integrated_yaw_tracks_a_multi_frame_pan():
    scene = _textured_scene(width=1400)
    est = YawEstimator(hfov_deg=55.0)
    total = 0.0
    x = 600
    est.update(_frame(scene, x))
    for _ in range(10):
        x -= 12  # content moves right 12 px per frame -> yaw left
        s = est.update(_frame(scene, x))
        assert s is not None
        total += s.delta_deg
    assert total == pytest.approx(shift_to_yaw_deg(120, 640, 55.0), rel=0.15)


# --------------------------------------------------------------------------- #
# 4. Honesty: blind conditions are low-confidence, never "zero rotation".
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("prefer_cv2", [True, False])
def test_black_frames_are_low_confidence(prefer_cv2):
    est = YawEstimator(hfov_deg=55.0, prefer_cv2=prefer_cv2)
    black = np.zeros((480, 640, 3), dtype=np.uint8)
    est.update(black)
    s = est.update(black)
    assert s is None or s.response < 0.1


def test_flat_wall_is_low_confidence():
    est = YawEstimator(hfov_deg=55.0, prefer_cv2=False)
    flat = np.full((480, 640, 3), 120, dtype=np.uint8)
    est.update(flat)
    s = est.update(flat)
    assert s is None or s.response < 0.1


def test_none_and_garbage_frames_are_ignored():
    est = YawEstimator(hfov_deg=55.0)
    assert est.update(None) is None
    assert est.update(np.zeros((0, 0, 3), dtype=np.uint8)) is None
    assert est.update(np.zeros((4, 4, 4, 4), dtype=np.uint8)) is None


def test_reset_makes_next_frame_the_reference():
    scene = _textured_scene()
    est = YawEstimator(hfov_deg=55.0)
    est.update(_frame(scene, 100))
    est.reset()
    assert est.update(_frame(scene, 100)) is None  # reference again
    assert est.update(_frame(scene, 100)) is not None


def test_gray_frames_are_accepted():
    scene = _textured_scene()
    est = YawEstimator(hfov_deg=55.0)
    g0 = _frame(scene, 100).mean(axis=2).astype(np.uint8)
    g1 = _frame(scene, 92).mean(axis=2).astype(np.uint8)
    est.update(g0)
    s = est.update(g1)
    assert s is not None and s.delta_deg > 0


def test_backend_name_reports_path():
    assert YawEstimator(prefer_cv2=False).backend == "numpy"
    cv = pytest.importorskip("cv2")
    if hasattr(cv, "phaseCorrelate"):
        assert YawEstimator(prefer_cv2=True).backend == "cv2"


# --------------------------------------------------------------------------- #
# 5. Looming rejection: driving TOWARD a scene expands it (left half slides
#    left, right half slides right). That must read as ~zero yaw, and a real
#    rotation on top of it must still come through.
# --------------------------------------------------------------------------- #
def _zoom(frame: np.ndarray, factor: float) -> np.ndarray:
    """Centre-zoom a frame by ``factor`` (>1 = closer) with nearest sampling."""
    h, w = frame.shape[:2]
    ys = np.clip(((np.arange(h) - h / 2) / factor + h / 2).round().astype(int), 0, h - 1)
    xs = np.clip(((np.arange(w) - w / 2) / factor + w / 2).round().astype(int), 0, w - 1)
    return frame[ys][:, xs]


@pytest.mark.parametrize("prefer_cv2", [True, False])
def test_pure_expansion_reads_near_zero_yaw(prefer_cv2):
    scene = _textured_scene()
    est = YawEstimator(hfov_deg=55.0, prefer_cv2=prefer_cv2)
    f0 = _frame(scene, 120)
    est.update(f0)
    s = est.update(_zoom(f0, 1.06))  # ~6% closer: edges move ~19 px at 640 wide
    assert s is not None
    assert abs(s.delta_deg) < 0.4  # a whole-frame estimate would read ~1 deg+


def test_rotation_survives_expansion():
    scene = _textured_scene(width=1000)
    est = YawEstimator(hfov_deg=55.0)
    est.update(_frame(scene, 200))
    # Content moves right 16 px (yaw LEFT ~1.4 deg) AND the scene zooms 4%.
    moved = _frame(scene, 184)
    s = est.update(_zoom(moved, 1.04))
    assert s is not None
    assert s.delta_deg == pytest.approx(shift_to_yaw_deg(16, 640, 55.0), abs=0.5)
    assert s.delta_deg > 0.8


def test_half_disagreement_lowers_confidence():
    scene = _textured_scene()
    f0 = _frame(scene, 120)
    est_shift = YawEstimator(hfov_deg=55.0)
    est_shift.update(f0)
    agree = est_shift.update(_frame(scene, 112))  # pure rotation: halves agree
    est_zoom = YawEstimator(hfov_deg=55.0)
    est_zoom.update(f0)
    disagree = est_zoom.update(_zoom(f0, 1.08))  # pure looming: halves oppose
    assert agree is not None and disagree is not None
    assert disagree.response < agree.response
