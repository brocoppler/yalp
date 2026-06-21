"""FOLLOW-mode steering controller + graceful-degradation policy (software-spec.md §4).

This is the pure, hardware-free decision layer that turns a tracker's
:class:`~yalp.reactive.person_tracker.TrackResult` (plus the frame's brightness)
into a steering command:

  * **Turn** toward the person: a proportional response to the *horizontal* error
    of the bbox center vs the frame center, with a small **deadband** so the robot
    does not jitter when the person is already roughly centered.
  * **Drive forward** while the person is far (the bbox is small) and **hold/stop**
    once they are close enough (the bbox fills enough of the frame). The bbox
    height is used as the distance proxy.

And it owns the **honesty / graceful-degradation** decisions the spec calls for:

  * If the person is **lost** — the tracker has no confident box, or the detector
    has not re-confirmed the box for too many ticks (stale) — STOP and report
    "I lost you" rather than driving blindly on a stale box (§2.2 / §4).
  * If the frame is **too dark** (mean brightness below a floor — the lux-floor
    proxy for "good light", §5) — STOP and report, because vision tracking is
    unreliable in the dark.

It commands nothing itself and imports only numpy; the reactive tick applies the
result and ``SAFE_STOP``/collision-stop still overrides everything upstream (§2.3).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np

from .. import config
from .person_tracker import TrackResult

# Short reason codes for the decision (handy for the CLI line and tests).
REASON_FOLLOW = "follow"
REASON_REACHED = "reached"
REASON_LOST = "lost"
REASON_STALE = "stale"
REASON_DARK = "dark"


@dataclass(frozen=True)
class FollowDecision:
    """The steering decision for one FOLLOW tick.

    ``turn`` is in ``-1..1`` with **positive = steer right** (toward +x); ``forward``
    is in ``0..1``. When ``target_visible`` is False both are 0 (a clean stop) and
    ``reason`` says why (``lost`` / ``stale`` / ``dark``).
    """

    target_visible: bool
    turn: float
    forward: float
    status: str
    reason: str
    error_x: float = 0.0  # normalized horizontal error, -1..1 (+ = person right)
    bbox_h: float = 0.0   # normalized bbox height, 0..1 (distance proxy)


def frame_brightness(frame: Optional[np.ndarray]) -> float:
    """Mean pixel brightness of a frame (0..255), or 0.0 when there is no frame."""
    if frame is None:
        return 0.0
    return float(np.asarray(frame).mean())


def _clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v


class FollowController:
    """Proportional center-and-approach controller with graceful degradation.

    Parameters mirror the spec's tunables; defaults come from ``config`` so the
    follow behavior is configured in one place (software-spec.md §6).
    """

    def __init__(
        self,
        *,
        turn_deadband: float = config.FOLLOW_TURN_DEADBAND,
        turn_gain: float = config.FOLLOW_TURN_GAIN,
        forward_gain: float = config.FOLLOW_FORWARD_GAIN,
        max_forward: float = 1.0,
        min_forward: float = 0.1,
        stop_bbox_h: float = config.FOLLOW_STOP_BBOX_HEIGHT,
        coast_ticks: int = config.FOLLOW_COAST_TICKS,
        track_min_score: float = config.FOLLOW_TRACK_MIN_SCORE,
        dark_brightness: float = config.FOLLOW_DARK_BRIGHTNESS,
    ) -> None:
        self.turn_deadband = float(turn_deadband)
        self.turn_gain = float(turn_gain)
        self.forward_gain = float(forward_gain)
        self.max_forward = float(max_forward)
        self.min_forward = float(min_forward)
        self.stop_bbox_h = float(stop_bbox_h)
        self.coast_ticks = int(coast_ticks)
        self.track_min_score = float(track_min_score)
        self.dark_brightness = float(dark_brightness)

    def decide(
        self,
        result: TrackResult,
        frame_w: int,
        frame_h: int,
        brightness: float,
    ) -> FollowDecision:
        """Map a tracker result + brightness to a steering decision."""
        # 1. Too dark to see -> degrade to a clean stop (the lux-floor proxy, §5).
        if brightness < self.dark_brightness:
            return FollowDecision(
                False, 0.0, 0.0,
                "I lost you — it's too dark to see", REASON_DARK,
            )

        # 2. Lost / unconfirmed / stale -> stop, never drive on a stale box (§4).
        stale = result.ticks_since_last_detector_confirmation > self.coast_ticks
        no_box = (not result.target_visible) or result.bbox is None
        weak = result.score < self.track_min_score
        if no_box or weak or stale:
            reason = REASON_STALE if (stale and result.bbox is not None) else REASON_LOST
            msg = (
                "I lost you — the box went stale, stopping"
                if reason == REASON_STALE
                else "I lost you"
            )
            return FollowDecision(False, 0.0, 0.0, msg, reason)

        # 3. We have a confident, fresh box -> center + approach.
        x, y, w, h = result.bbox
        half_w = max(1.0, frame_w / 2.0)
        error_x = _clamp((x + w / 2.0 - frame_w / 2.0) / half_w, -1.0, 1.0)
        bbox_h = _clamp(h / float(max(1, frame_h)), 0.0, 1.0)

        if abs(error_x) < self.turn_deadband:
            turn = 0.0
        else:
            turn = _clamp(self.turn_gain * error_x, -1.0, 1.0)

        if bbox_h >= self.stop_bbox_h:
            forward = 0.0
            reason = REASON_REACHED
        else:
            forward = _clamp(
                self.forward_gain * (self.stop_bbox_h - bbox_h),
                self.min_forward, self.max_forward,
            )
            reason = REASON_FOLLOW

        return FollowDecision(
            True, turn, forward, _status(error_x, turn, bbox_h, forward, reason),
            reason, error_x, bbox_h,
        )


def _status(error_x: float, turn: float, bbox_h: float, forward: float, reason: str) -> str:
    if turn > 0:
        tdir = f"person at x={error_x:+.2f} -> turn right"
    elif turn < 0:
        tdir = f"person at x={error_x:+.2f} -> turn left"
    else:
        tdir = f"person centered (x={error_x:+.2f})"
    if reason == REASON_REACHED or forward <= 0.0:
        fdir = f"bbox h={bbox_h:.2f} -> close enough, hold"
    else:
        fdir = f"bbox h={bbox_h:.2f} -> forward"
    return f"{tdir}; {fdir}"


def decision_line(decision: Optional[FollowDecision]) -> str:
    """One-line, human-readable rendering of a FOLLOW decision for the CLI."""
    if decision is None:
        return "follow: warming up (no decision yet)"
    if not decision.target_visible:
        return f"lost -> stop ({decision.status})"
    return (
        f"{decision.status} "
        f"[turn={decision.turn:+.2f}, fwd={decision.forward:.2f}]"
    )


def _action_phrase(decision: FollowDecision) -> str:
    """A compact 'what am I doing' phrase for a visible target."""
    if decision.turn > 0:
        steer = "turn right"
    elif decision.turn < 0:
        steer = "turn left"
    else:
        steer = "centered"
    drive = "hold (close enough)" if decision.forward <= 0.0 else "forward"
    return f"{steer} -> {drive}"


def _searching_phrase(decision: Optional[FollowDecision], safe_stop: bool) -> str:
    """A compact 'why am I stopped' phrase when there is no visible target."""
    if safe_stop:
        return "SAFE_STOP — collision-stop overrides follow"
    if decision is None:
        return "no target yet"
    if decision.reason == REASON_DARK:
        return "no target: too dark to see"
    if decision.reason == REASON_STALE:
        return "no target: box went stale"
    return "no target"


class FollowReporter:
    """Turn a per-tick stream of decisions into READABLE output.

    The raw loop produces one decision per tick; printing every tick spams an
    identical line and makes it impossible to tell what is happening. This reporter
    prints only what matters:

      * a clear line when it **acquires** the target (👁  acquired you),
      * a clear line when it **loses** it (❌  lost you — stop),
      * and a periodic **heartbeat** (~every ``heartbeat_s`` seconds) summarizing
        the current action while nothing else changes.

    It also **quiets the warm-up**: while ``warming_up`` is True (the camera is
    still auto-exposing) it stays silent and does not latch a transition, so the
    first real frame — not the warm-up noise — is what counts as "acquired"/"lost".

    :meth:`update` returns the line to print, or ``None`` to stay quiet this tick.
    This is the testable decision function (no stdout coupling).
    """

    def __init__(self, *, heartbeat_s: float = config.FOLLOW_HEARTBEAT_S) -> None:
        self.heartbeat_s = float(heartbeat_s)
        self._was_visible: Optional[bool] = None
        self._last_heartbeat: Optional[float] = None

    def update(
        self,
        decision: Optional[FollowDecision],
        now: float,
        *,
        warming_up: bool = False,
        safe_stop: bool = False,
    ) -> Optional[str]:
        # Quiet the warm-up window entirely (no "too dark / lost" noise).
        if warming_up:
            return None

        visible = bool(decision is not None and decision.target_visible and not safe_stop)

        # State CHANGE — always announce immediately.
        if visible != self._was_visible:
            self._was_visible = visible
            self._last_heartbeat = now
            if visible:
                return "👁  acquired you — centering / approaching"
            return f"❌  lost you — stop ({_searching_phrase(decision, safe_stop)})"

        # No change — emit at most one heartbeat per ``heartbeat_s``.
        if self._last_heartbeat is None or (now - self._last_heartbeat) >= self.heartbeat_s:
            self._last_heartbeat = now
            if visible and decision is not None:
                return (
                    f"tracking: x={decision.error_x:+.2f} size={decision.bbox_h:.2f}"
                    f" -> {_action_phrase(decision)}"
                )
            return f"searching: {_searching_phrase(decision, safe_stop)}"
        return None


__all__ = [
    "FollowDecision",
    "FollowController",
    "FollowReporter",
    "frame_brightness",
    "decision_line",
    "REASON_FOLLOW",
    "REASON_REACHED",
    "REASON_LOST",
    "REASON_STALE",
    "REASON_DARK",
]
