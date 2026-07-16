"""The two shapes that cross the loop-to-loop seam: ``Intent`` and ``RobotState``.

This module is the wire-format source of truth for the contract specified in
``docs/technical/software-spec.md`` §2 ("The Crux"). The reactive loop publishes
a ``RobotState`` every tick; the deliberative loop pushes an ``Intent`` with a
monotonically increasing ``seq``. The two shapes are *serialized as JSON*, one
object per ``\\n``-terminated line — they are never shared as live Python objects
(§2.2 wire format). A ``"type"`` discriminator (``"intent"`` / ``"state"``) lets a
reader tell them apart.

Design rule: this module is **pure standard library** (``json``, ``enum``,
``dataclasses``, ``time``, ``typing``). It must import with no third-party deps
(no ``anthropic`` / ``opencv`` / ``numpy``) so the contract is testable anywhere.
"""

from __future__ import annotations

import enum
import json
import time
from dataclasses import dataclass, field
from typing import Any, Optional, Tuple, Union


class Mode(str, enum.Enum):
    """The reactive layer's mutually-exclusive modes (software-spec.md §2.2).

    There is deliberately **no ``EXPLORE``** mode — exploration is deliberative
    sugar (a loop of ``drive`` / ``turn`` / ``describe_scene`` issued from the
    cloud side), not a reactive mode (§2.1). ``Mode`` subclasses ``str`` so it
    serializes to its plain name and compares equal to the raw string
    (``Mode.IDLE == "IDLE"``).
    """

    IDLE = "IDLE"
    DRIVE_GOAL = "DRIVE_GOAL"
    FOLLOW = "FOLLOW"
    SAFE_STOP = "SAFE_STOP"


class GoalStatus:
    """Canonical ``goal_status`` strings (software-spec.md §2.2 / §2.3).

    Not an enum because the spec pins these exact human-readable strings as the
    contract values — most importantly ``COMPLETED`` is *never* a bare
    ``"completed"``: open-loop ``drive`` / ``turn`` are timed guesses with no
    encoders, so completion is always reported as "timed, unverified".
    """

    RUNNING = "running"
    COMPLETED = "completed (timed, unverified)"
    BLOCKED = "blocked"
    PREEMPTED = "preempted"
    NONE = "none"


# Wire-format "type" discriminators.
TYPE_INTENT = "intent"
TYPE_STATE = "state"

Bbox = Tuple[float, float, float, float]

# Speed-limit clamp bounds (software-spec.md §2.3). ``set_speed_limit`` and the
# reactive ``RobotState.speed_limit`` clamp are constrained to this sane band:
# never faster than full (1.0), never so slow the wheels can't overcome stiction
# (0.1).
SPEED_LIMIT_MIN = 0.1
SPEED_LIMIT_MAX = 1.0


def clamp_speed_limit(value: float) -> float:
    """Clamp a requested speed limit into the sane ``[0.1, 1.0]`` band.

    One source of truth so the reactive core (which writes
    ``RobotState.speed_limit`` on adoption) and the deliberative layer (which
    reports the *applied* value back to the model) can never disagree on the
    number.
    """
    v = float(value)
    if v < SPEED_LIMIT_MIN:
        return SPEED_LIMIT_MIN
    if v > SPEED_LIMIT_MAX:
        return SPEED_LIMIT_MAX
    return v


def _mode_to_str(mode: Union[Mode, str]) -> str:
    return mode.value if isinstance(mode, Mode) else str(mode)


@dataclass
class Intent:
    """A high-level instruction from the deliberative loop (software-spec.md §2.2).

    The reactive layer keeps a **single-slot, last-write-wins** mailbox of these
    (see ``yalp.contract.ipc.IntentMailbox``): only the highest ``seq`` seen is
    retained, and a newer one overwrites the pending one — there is no queue. A
    dropped ``Intent`` is simply re-issued by the next deliberative step.

    Fields
    ------
    mode:
        Requested mode (``IDLE`` / ``DRIVE_GOAL`` / ``FOLLOW`` / ``SAFE_STOP``),
        or ``None`` for a **control-only intent** — one that carries no mode
        change and only adjusts a reactive control value (currently
        ``speed_limit``). A control-only intent is adopted without preempting the
        in-progress goal (see the reactive tick core), so "go slow" clamps the
        current motion instead of stopping it.
    goal:
        The mode's typed goal payload, or ``None`` (e.g. for ``IDLE`` and for a
        control-only intent).
    seq:
        Monotonically increasing sequence number; newer ``seq`` wins. It does
        **not** reset across reconnects, so a stale ``Intent`` from a half-open
        old socket can never overwrite a newer one (§2.2 reconnect decision).
    ts:
        Monotonic timestamp the intent was created (diagnostics only; not part
        of the preemption rule, which is ``seq``-ordered).
    speed_limit:
        Optional safety speed clamp to apply on adoption (software-spec.md §2.3).
        ``None`` (the wire default when the key is absent) leaves the reactive
        layer's current ``RobotState.speed_limit`` untouched — this is what keeps
        an old-style intent (no ``speed_limit`` key) wire-compatible. When
        present it is clamped into ``[0.1, 1.0]`` by the reactive core and
        written to ``RobotState.speed_limit``; every subsequent throttle is
        clamped to it.
    """

    mode: Optional[Mode] = None
    goal: Optional[dict] = None
    seq: int = 0
    ts: float = field(default_factory=time.monotonic)
    speed_limit: Optional[float] = None

    def __post_init__(self) -> None:
        if self.mode is not None and not isinstance(self.mode, Mode):
            self.mode = Mode(self.mode)

    def to_dict(self) -> dict:
        d = {
            "type": TYPE_INTENT,
            "mode": _mode_to_str(self.mode) if self.mode is not None else None,
            "goal": self.goal,
            "seq": int(self.seq),
            "ts": self.ts,
        }
        # Only emit the speed_limit key when carrying one, so an ordinary motion
        # intent stays byte-identical on the wire to a pre-speed-limit build.
        if self.speed_limit is not None:
            d["speed_limit"] = float(self.speed_limit)
        return d

    def to_json(self) -> str:
        """Serialize to a single ``\\n``-terminated JSON line."""
        return json.dumps(self.to_dict()) + "\n"

    @classmethod
    def from_dict(cls, d: dict) -> "Intent":
        m = d.get("mode")
        sl = d.get("speed_limit")
        return cls(
            mode=Mode(m) if m is not None else None,
            goal=d.get("goal"),
            seq=int(d.get("seq", 0)),
            ts=float(d.get("ts", 0.0)),
            speed_limit=None if sl is None else float(sl),
        )

    @classmethod
    def from_json(cls, line: str) -> "Intent":
        return cls.from_dict(json.loads(line))


@dataclass
class RobotState:
    """The reactive layer's published snapshot (software-spec.md §2.2).

    Written by the reactive loop every tick and *published* (serialized to one
    JSON line) to the deliberative loop on a last-write-wins basis — there is no
    history. The deliberative loop reads the latest snapshot when it builds a
    prompt or polls a goal (pull model, §2.4).

    Field names map field-for-field to JSON keys (the wire format in §2.2).
    """

    mode: Mode = Mode.IDLE
    goal: Optional[dict] = None
    goal_status: str = GoalStatus.NONE
    goal_elapsed_s: float = 0.0
    distance_m: float = 10.0
    distance_known: bool = True
    obstacle: bool = False
    target_visible: bool = False
    target_bbox: Optional[Bbox] = None
    tracker_score: float = 0.0
    ticks_since_last_detector_confirmation: int = 0
    last_frame_id: Optional[str] = None
    speed_limit: float = 1.0
    #: Optional cumulative range-sensor counters for observability (a sub-map of
    #: ints: ``total_reads`` / ``valid_reads`` / ``raw_misses`` / ``coasted_reads``
    #: / ``unknown_served``), or ``None`` when the backend has no counter-bearing
    #: sensor (e.g. the pure simulation). ADDITIVE, backward-compatible: an old
    #: consumer that never reads this key is unaffected, and an old wire message
    #: without it decodes to ``None``. The grace-coasted miss count that this
    #: exposes is invisible to a plain ``distance_known`` observer, so this is the
    #: only place a state poll / telemetry record can show the TRUE miss rate.
    ultrasonic: Optional[dict] = None
    ts: float = field(default_factory=time.monotonic)

    def __post_init__(self) -> None:
        if not isinstance(self.mode, Mode):
            self.mode = Mode(self.mode)
        if self.target_bbox is not None and not isinstance(self.target_bbox, tuple):
            self.target_bbox = tuple(self.target_bbox)  # type: ignore[assignment]

    # -- convenience views (not part of the wire format) ---------------------
    @property
    def blocked(self) -> bool:
        """True when a safety stop is active / the last goal hit something.

        Convenience over the canonical fields: ``SAFE_STOP`` mode or a
        ``"blocked"`` ``goal_status`` (software-spec.md §2.3 BLOCKED surface).
        """
        return self.mode == Mode.SAFE_STOP or self.goal_status == GoalStatus.BLOCKED

    @property
    def last_distance_cm(self) -> Optional[float]:
        """Latest ultrasonic reading in centimetres, or ``None`` if unknown."""
        return self.distance_m * 100.0 if self.distance_known else None

    def to_dict(self) -> dict:
        return {
            "type": TYPE_STATE,
            "mode": _mode_to_str(self.mode),
            "goal": self.goal,
            "goal_status": self.goal_status,
            "goal_elapsed_s": self.goal_elapsed_s,
            "distance_m": self.distance_m,
            "distance_known": self.distance_known,
            "obstacle": self.obstacle,
            "target_visible": self.target_visible,
            "target_bbox": list(self.target_bbox) if self.target_bbox is not None else None,
            "tracker_score": self.tracker_score,
            "ticks_since_last_detector_confirmation": self.ticks_since_last_detector_confirmation,
            "last_frame_id": self.last_frame_id,
            "speed_limit": self.speed_limit,
            "ultrasonic": self.ultrasonic,
            "ts": self.ts,
        }

    def to_json(self) -> str:
        """Serialize to a single ``\\n``-terminated JSON line."""
        return json.dumps(self.to_dict()) + "\n"

    @classmethod
    def from_dict(cls, d: dict) -> "RobotState":
        bbox = d.get("target_bbox")
        return cls(
            mode=Mode(d.get("mode", "IDLE")),
            goal=d.get("goal"),
            goal_status=d.get("goal_status", GoalStatus.NONE),
            goal_elapsed_s=float(d.get("goal_elapsed_s", 0.0)),
            distance_m=float(d.get("distance_m", 10.0)),
            distance_known=bool(d.get("distance_known", True)),
            obstacle=bool(d.get("obstacle", False)),
            target_visible=bool(d.get("target_visible", False)),
            target_bbox=tuple(bbox) if bbox is not None else None,
            tracker_score=float(d.get("tracker_score", 0.0)),
            ticks_since_last_detector_confirmation=int(
                d.get("ticks_since_last_detector_confirmation", 0)
            ),
            last_frame_id=d.get("last_frame_id"),
            speed_limit=float(d.get("speed_limit", 1.0)),
            ultrasonic=d.get("ultrasonic"),
            ts=float(d.get("ts", 0.0)),
        )

    @classmethod
    def from_json(cls, line: str) -> "RobotState":
        return cls.from_dict(json.loads(line))


def parse_line(line: str) -> Union[Intent, RobotState]:
    """Parse one JSON line into the right shape using its ``"type"`` field.

    Raises ``ValueError`` on an unknown / missing type so callers can ignore or
    log a malformed line rather than silently mis-decode it.
    """
    d: dict[str, Any] = json.loads(line)
    t = d.get("type")
    if t == TYPE_INTENT:
        return Intent.from_dict(d)
    if t == TYPE_STATE:
        return RobotState.from_dict(d)
    raise ValueError(f"unknown message type: {t!r}")


__all__ = [
    "Mode",
    "GoalStatus",
    "Intent",
    "RobotState",
    "Bbox",
    "TYPE_INTENT",
    "TYPE_STATE",
    "SPEED_LIMIT_MIN",
    "SPEED_LIMIT_MAX",
    "clamp_speed_limit",
    "parse_line",
]
