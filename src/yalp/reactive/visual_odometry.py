"""Camera-only yaw estimation for the reactive loop (visual heading).

Izzy has **no encoders and no IMU**. The only sensor that can tell the reactive
layer "you are turning" is the camera: when the robot yaws, the whole scene
slides sideways in the image. This module turns that slide into a yaw angle.

How
---
Each new frame is reduced to a small grayscale copy (default 160x120 — ~20 KB,
sub-millisecond to process, so it fits comfortably inside the 33 ms tick budget)
and compared with the previous one. The horizontal translation between the two
is measured by 2-D **phase correlation** (:func:`cv2.phaseCorrelate` with a
Hanning window) when OpenCV is available, else by a numpy-only 1-D normalised
cross-correlation of the column-intensity profiles. The shift in pixels maps to
degrees through the camera's horizontal field of view::

    yaw_deg = dx_px * hfov_deg / width_px

Sign convention (**pinned by :func:`shift_to_yaw_deg` and verified on the robot
2026-09-15 with a commanded in-place pivot**): the scene moving RIGHT in the
image (``dx > 0``) means the camera turned LEFT, so **positive yaw = LEFT / CCW**,
the same convention as a ``rotate`` goal's target.

Looming rejection
-----------------
Driving *toward* a scene makes it expand radially: the left half of the image
slides LEFT and the right half slides RIGHT. Measured on one whole frame that
expansion nets to ~0 only if the scene is symmetric; a wall on one side and open
floor on the other (typical indoors, 0.3–1 m away) produces a spurious net shift
that reads as yaw. So the shift is measured on the LEFT and RIGHT halves
separately and averaged: a rotation moves both halves the same way (mean = the
rotation), an expansion moves them opposite ways (mean ≈ 0). The two halves'
disagreement is also folded into the confidence — see :meth:`YawEstimator.update`.

Honesty
-------
Every sample carries the correlation ``response`` (0..1). A textureless wall, a
black frame, or motion blur gives a low response; callers must treat a sample
below their confidence floor as *blind*, never as "no rotation". Forward motion
(looming) is a radial expansion that the window makes near-symmetric, so a pure
straight drive measures ~0 shift — this was checked in the field (straight drive
~1 deg/s of estimated yaw at rest-quality response).

Limits: pure sideways translation also shifts the image (a differential-drive
robot cannot translate sideways without yawing, so this is moot), and a person
walking across the frame drags the estimate (FOLLOW mode does not use it). The
per-frame shift must stay under ~1/4 of the small frame width (~40 px = ~14 deg
per frame — ~200 deg/s at 15 fps), above which phase correlation aliases.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

import numpy as np

from .. import config


@dataclass(frozen=True)
class YawSample:
    """One accepted frame-to-frame yaw measurement."""

    #: Yaw since the previous accepted frame, degrees, **positive = LEFT / CCW**.
    delta_deg: float
    #: Horizontal image shift in SMALL-frame pixels (positive = scene moved right).
    dx_px: float
    #: Correlation response 0..1 (confidence). Low = blind, not "no rotation".
    response: float


def shift_to_yaw_deg(dx_px: float, width_px: int, hfov_deg: float) -> float:
    """Map a horizontal image shift (px, scene moving right = +) to yaw (deg, LEFT = +)."""
    if width_px <= 0:
        return 0.0
    return float(dx_px) * float(hfov_deg) / float(width_px)


def _to_small_gray(frame: Any, width: int, height: int) -> Optional[np.ndarray]:
    """Reduce a BGR/gray uint8 frame to a float32 ``height x width`` gray image."""
    if frame is None:
        return None
    arr = np.asarray(frame)
    if arr.ndim == 3:
        # BGR -> luminance (OpenCV weights); cheap, no cv2 dependency.
        gray = (
            0.114 * arr[:, :, 0].astype(np.float32)
            + 0.587 * arr[:, :, 1].astype(np.float32)
            + 0.299 * arr[:, :, 2].astype(np.float32)
        )
    elif arr.ndim == 2:
        gray = arr.astype(np.float32)
    else:
        return None
    h, w = gray.shape
    if h == 0 or w == 0:
        return None
    if (h, w) == (height, width):
        return gray
    try:
        import cv2  # noqa: WPS433 - optional, lazy

        return cv2.resize(gray, (width, height), interpolation=cv2.INTER_AREA).astype(
            np.float32
        )
    except Exception:
        # Numpy fallback: block-mean downsample by integer factors (good enough
        # for a coarse shift estimate), then crop to size.
        fy = max(1, h // height)
        fx = max(1, w // width)
        hh, ww = (h // fy) * fy, (w // fx) * fx
        small = gray[:hh, :ww].reshape(hh // fy, fy, ww // fx, fx).mean(axis=(1, 3))
        return small[:height, :width].astype(np.float32)


def _ncc_shift(prev: np.ndarray, cur: np.ndarray, max_shift: int) -> tuple[float, float]:
    """Numpy-only horizontal shift estimate via 1-D column-profile NCC.

    Returns ``(dx_px, response)`` where ``dx_px`` is the shift of ``cur``
    relative to ``prev`` (scene moving right = +) with parabolic sub-pixel
    refinement, and ``response`` is the peak normalised correlation (0..1).
    """
    a = prev.mean(axis=0)
    b = cur.mean(axis=0)
    a = a - a.mean()
    b = b - b.mean()
    na = float(np.sqrt((a * a).sum()))
    nb = float(np.sqrt((b * b).sum()))
    if na < 1e-6 or nb < 1e-6:
        return 0.0, 0.0
    w = a.shape[0]
    max_shift = max(1, min(max_shift, w // 3))
    scores = np.empty(2 * max_shift + 1, dtype=np.float64)
    for i, s in enumerate(range(-max_shift, max_shift + 1)):
        # Scene moved right by s  <=>  cur[x] == prev[x - s].
        if s >= 0:
            seg_prev, seg_cur = a[: w - s], b[s:]
        else:
            seg_prev, seg_cur = a[-s:], b[: w + s]
        denom = float(np.sqrt((seg_prev * seg_prev).sum() * (seg_cur * seg_cur).sum()))
        scores[i] = float((seg_prev * seg_cur).sum()) / denom if denom > 1e-9 else 0.0
    k = int(np.argmax(scores))
    peak = float(scores[k])
    dx = float(k - max_shift)
    if 0 < k < scores.shape[0] - 1:
        y0, y1, y2 = scores[k - 1], scores[k], scores[k + 1]
        denom = y0 - 2.0 * y1 + y2
        if abs(denom) > 1e-9:
            dx += 0.5 * float((y0 - y2) / denom)
    return dx, max(0.0, min(1.0, peak))


class YawEstimator:
    """Frame-to-frame visual yaw (see the module docstring).

    Feed :meth:`update` every new camera frame; it returns a :class:`YawSample`
    for an accepted frame pair or ``None`` when there is nothing to compare yet
    (first frame after construction/:meth:`reset`) or the frame is unusable.
    The caller applies its own confidence floor on ``sample.response``.

    ``prefer_cv2=False`` forces the numpy path (tests / cv2-less hosts).
    """

    def __init__(
        self,
        *,
        hfov_deg: float = config.CAMERA_HFOV_DEG,
        width: int = 160,
        height: int = 120,
        max_shift_px: Optional[int] = None,
        prefer_cv2: bool = True,
    ) -> None:
        self.hfov_deg = float(hfov_deg)
        self.width = int(width)
        self.height = int(height)
        self.max_shift_px = int(max_shift_px) if max_shift_px else max(4, self.width // 4)
        self._prev: Optional[np.ndarray] = None
        self._cv2: Any = None
        self._window: Any = None
        self._half = self.width // 2
        if prefer_cv2:
            try:
                import cv2

                if hasattr(cv2, "phaseCorrelate"):
                    self._cv2 = cv2
                    try:
                        # One window per HALF frame (the halves are correlated
                        # separately — see "Looming rejection").
                        self._window = cv2.createHanningWindow(
                            (self._half, self.height), cv2.CV_32F
                        )
                    except Exception:
                        self._window = None
            except Exception:
                self._cv2 = None
        #: Number of accepted samples since construction/reset (diagnostics).
        self.samples = 0

    @property
    def backend(self) -> str:
        """``"cv2"`` (phase correlation) or ``"numpy"`` (1-D NCC fallback)."""
        return "cv2" if self._cv2 is not None else "numpy"

    def reset(self) -> None:
        """Forget the previous frame: the NEXT frame becomes the reference."""
        self._prev = None

    def _shift(self, prev: np.ndarray, cur: np.ndarray) -> tuple[float, float]:
        """Horizontal shift + response for one (half-)frame pair."""
        if self._cv2 is not None:
            try:
                if self._window is not None and self._window.shape == prev.shape:
                    (dx, _dy), resp = self._cv2.phaseCorrelate(prev, cur, self._window)
                else:
                    (dx, _dy), resp = self._cv2.phaseCorrelate(prev, cur)
                return float(dx), float(resp)
            except Exception:
                pass
        return _ncc_shift(prev, cur, self.max_shift_px)

    def update(self, frame: Any) -> Optional[YawSample]:
        small = _to_small_gray(frame, self.width, self.height)
        if small is None:
            return None
        prev, self._prev = self._prev, small
        if prev is None:
            return None
        h = self._half
        halves = ((prev[:, :h], small[:, :h]), (prev[:, h:], small[:, h:]))
        shifts = []
        resps = []
        for p_half, c_half in halves:
            dx_h, resp_h = self._shift(np.ascontiguousarray(p_half), np.ascontiguousarray(c_half))
            if not np.isfinite(dx_h) or not np.isfinite(resp_h):
                return None
            shifts.append(float(dx_h))
            resps.append(float(resp_h))
        # Rotation: both halves agree. Expansion (driving toward the scene):
        # they oppose and cancel in the mean. Their DISAGREEMENT is parallax /
        # looming / a moving object — not rotation — so it discounts confidence.
        dx = 0.5 * (shifts[0] + shifts[1])
        disagree = abs(shifts[0] - shifts[1])
        resp = min(resps) / (1.0 + disagree / max(1.0, 0.25 * self.max_shift_px))
        if abs(dx) > self.max_shift_px:
            # Beyond the reliable range of the estimator: report it as a blind
            # sample rather than a giant (aliased) rotation.
            return YawSample(delta_deg=0.0, dx_px=float(dx), response=0.0)
        self.samples += 1
        return YawSample(
            delta_deg=shift_to_yaw_deg(dx, self.width, self.hfov_deg),
            dx_px=dx,
            response=max(0.0, min(1.0, resp)),
        )


__all__ = ["YawEstimator", "YawSample", "shift_to_yaw_deg"]
