"""``yalp telemetry`` — read the flight recorder back: list, summary, export, plot.

Tuning the visual heading hold (``tick_core._heading_correction``: ``bias + Kp *
heading + Kd * yaw_rate``) needs, PER DRIVE, the heading trace, the distance
trace and the left/right wheel commands laid side by side on one time axis: did
the correction oscillate or converge, was the estimate LIVE when it mattered,
how far was the nose when the reflex fired, what were the wheels actually told.
``yalp drive`` prints a 5 Hz terminal timeline — fine for a verdict, useless for
a waveform — and the interesting dynamics happen between its rows. The recorder
that already captures all of this at tick rate exists (:mod:`yalp.telemetry`:
JSONL under ``config.TELEMETRY_DIR`` with ``intent_adopted`` / ``motor_command``
/ ``state_sample`` / ``state_transition`` / ``watchdog_trip`` / ``drop_summary``
events); what was missing was the READER. This module is that reader. Nothing
here writes telemetry.

What it does
------------
  * ``list``    — the live file and its rotated backups, newest first, with size
                  and first/last wall-clock time (``--count`` adds event counts).
  * ``summary`` — one row per DRIVE_GOAL adoption: kind/target/speed, outcome,
                  duration, final and max-|heading|, min distance, whether the
                  heading estimate ever went blind, the learned steering bias;
                  then run totals (goals / SAFE_STOPs / watchdog trips / drops).
  * ``export``  — flatten the state events (and optionally the motor commands)
                  to CSV for a spreadsheet or pandas.
  * ``plot``    — a DEPENDENCY-FREE SVG: three stacked panels (distance with the
                  ``config.SAFE_STOP_THRESHOLD_M`` line, heading, left/right
                  throttle) on one time axis, DRIVE_GOAL spans shaded, SAFE_STOP
                  entries marked. With matplotlib importable and a ``.png``
                  target the same figure is rendered by matplotlib instead.

How a drive is delimited
------------------------
A goal OPENS at an ``intent_adopted`` whose ``mode`` is ``DRIVE_GOAL`` (the tick
core notifies the observer inside ``_adopt``, before that tick's motor command
and state publish) and CLOSES at the first following ``state_transition`` whose
``goal_status`` is terminal: ``completed (timed, unverified)``, ``blocked`` or
``preempted`` (the tick core publishes a one-tick PREEMPTED snapshot before it
adopts a superseding intent, so the close is always observed in order). A goal
still open when the file ends is ``unfinished``; one whose terminal transition
was never recorded before the next adoption (dropped under queue overflow, or
rotated into another file) is ``unknown``. Nothing is inferred from timing.

The helpers (:func:`load_run`, :func:`summarize`, :func:`to_csv_rows`,
:func:`render_svg`, ...) are pure functions of the parsed file, so they are
unit-testable without a robot, a socket or a display. Time is the recorder's
MONOTONIC stamp relative to the first event in the file (wall-clock is used for
the ``list`` view and the CSV, never for deltas) — with one exception: the
recorder APPENDS to the live file across restarts, so a file can span a reboot,
after which the monotonic clock restarts lower; that single backwards jump is
bridged with the wall-clock gap and counted (``time_resets``) so the axis keeps
running forward and the summary says so. Corrupt lines are skipped by
:func:`yalp.telemetry.read_events` and their count is surfaced, not hidden.

Registered via the documented feature-module contract (``add_parser`` / ``run``)
and listed in ``yalp.cli.FEATURE_MODULES``.

Exit codes
----------
  * ``0`` — done.
  * ``1`` — the named file / directory does not exist, no telemetry files were
    found, or a ``--goal`` / ``--from-seq`` selection matched nothing.
"""

from __future__ import annotations

import csv
import json
import math
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import config
from .contract.messages import GoalStatus, Mode
from .telemetry import (
    EVENT_DROP_SUMMARY,
    EVENT_INTENT_ADOPTED,
    EVENT_MOTOR_COMMAND,
    EVENT_STATE_SAMPLE,
    EVENT_STATE_TRANSITION,
    EVENT_WATCHDOG_TRIP,
    read_events,
)

# --------------------------------------------------------------------------- #
# Vocabulary
# --------------------------------------------------------------------------- #
#: Goal outcomes. The first three map 1:1 to the terminal ``GoalStatus`` strings
#: of the closing ``state_transition``; the last two are reader-side verdicts.
OUTCOME_COMPLETED = "completed"
OUTCOME_BLOCKED = "blocked"
OUTCOME_PREEMPTED = "preempted"
OUTCOME_UNFINISHED = "unfinished"  # the file ended with the goal still running
OUTCOME_UNKNOWN = "unknown"  # the next adoption arrived; no terminal transition seen

_TERMINAL_OUTCOMES = {
    GoalStatus.COMPLETED: OUTCOME_COMPLETED,
    GoalStatus.BLOCKED: OUTCOME_BLOCKED,
    GoalStatus.PREEMPTED: OUTCOME_PREEMPTED,
}

#: CSV column order for ``export --csv`` (state events) and ``--motors``.
STATE_CSV_COLUMNS: Tuple[str, ...] = (
    "ts_mono",
    "ts_wall",
    "type",
    "mode",
    "goal_status",
    "distance_m",
    "distance_known",
    "obstacle",
    "speed_limit",
    "goal.kind",
    "goal.target",
    "goal.progress",
    "goal.heading_deg",
    "goal.yaw_rate_dps",
    "goal.heading_live",
    "t_rel",
)
MOTOR_CSV_COLUMNS: Tuple[str, ...] = ("ts_mono", "ts_wall", "left", "right", "t_rel")


# --------------------------------------------------------------------------- #
# Parsed model
# --------------------------------------------------------------------------- #
@dataclass
class StateEvent:
    """One ``state_sample`` / ``state_transition`` event, flattened."""

    index: int  # position in Run.events
    t: float  # seconds since the run's first event (monotonic)
    ts_mono: float
    ts_wall: Optional[float]
    kind: str  # EVENT_STATE_SAMPLE | EVENT_STATE_TRANSITION
    mode: str
    goal_status: str
    distance_m: Optional[float]
    distance_known: bool
    obstacle: bool
    speed_limit: Optional[float]
    goal: Dict[str, Any] = field(default_factory=dict)

    @property
    def heading_deg(self) -> Optional[float]:
        return _num(self.goal.get("heading_deg"))

    @property
    def heading_live(self) -> Optional[bool]:
        v = self.goal.get("heading_live")
        return None if v is None else bool(v)


@dataclass
class MotorEvent:
    """One ``motor_command`` event."""

    index: int
    t: float
    ts_mono: float
    ts_wall: Optional[float]
    left: float
    right: float


@dataclass
class Goal:
    """One DRIVE_GOAL adoption and everything recorded until it closed."""

    number: int  # 1-based position in the file
    seq: int
    kind: str
    target: Optional[float]
    speed: Optional[float]
    start_index: int
    t_start: float
    intent_goal: Dict[str, Any] = field(default_factory=dict)
    end_index: Optional[int] = None
    t_end: Optional[float] = None
    outcome: str = OUTCOME_UNFINISHED
    reason: Optional[str] = None
    final_goal: Dict[str, Any] = field(default_factory=dict)
    states: List[StateEvent] = field(default_factory=list)
    motors: List[MotorEvent] = field(default_factory=list)

    @property
    def duration_s(self) -> Optional[float]:
        return None if self.t_end is None else max(0.0, self.t_end - self.t_start)


@dataclass
class Run:
    """A parsed telemetry file: events grouped for the summary / CSV / plot."""

    path: Path
    t0: float = 0.0  # monotonic ts of the first event (the time-axis origin)
    t_last: float = 0.0  # relative t of the last event
    events: List[Dict[str, Any]] = field(default_factory=list)
    times: List[float] = field(default_factory=list)  # relative t per event (index-aligned)
    time_resets: int = 0  # monotonic clock went BACKWARDS (a reboot appended to the live file)
    states: List[StateEvent] = field(default_factory=list)
    transitions: List[StateEvent] = field(default_factory=list)
    motors: List[MotorEvent] = field(default_factory=list)
    goals: List[Goal] = field(default_factory=list)
    safe_stops: List[StateEvent] = field(default_factory=list)  # transitions INTO SAFE_STOP
    watchdog_trips: int = 0
    dropped: int = 0
    corrupt: int = 0  # non-JSON lines skipped by read_events
    malformed: int = 0  # JSON, but not a recorder event (no type / ts)
    first_wall: Optional[float] = None
    last_wall: Optional[float] = None

    @property
    def duration_s(self) -> float:
        return self.t_last


# --------------------------------------------------------------------------- #
# Small pure helpers
# --------------------------------------------------------------------------- #
def _num(v: Any) -> Optional[float]:
    """``float(v)`` for a finite real number (bools excluded), else ``None``."""
    if v is None or isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if math.isfinite(f) else None


def _payload(event: Dict[str, Any]) -> Dict[str, Any]:
    p = event.get("payload")
    return p if isinstance(p, dict) else {}


def _wall_str(ts: Optional[float]) -> str:
    if ts is None:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def _human_size(nbytes: int) -> str:
    size = float(nbytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024.0 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{nbytes} B"  # pragma: no cover - unreachable


def _fmt_target(kind: str, target: Optional[float]) -> str:
    if target is None:
        return "-"
    if kind == "rotate":
        return f"{target:+.1f} deg"
    if kind == "straight":
        return f"{target:+.2f} m"
    return f"{target:g}"


def _fmt_opt(v: Optional[float], spec: str) -> str:
    return "-" if v is None else format(v, spec)


def _default_dir() -> Path:
    """The telemetry directory, honoring ``YALP_TELEMETRY_DIR`` at call time."""
    return Path(config._env_str("YALP_TELEMETRY_DIR", config.TELEMETRY_DIR)).expanduser()


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #
def _state_event(
    index: int, t: float, ts_mono: float, ts_wall: Optional[float], kind: str, payload: Dict[str, Any]
) -> StateEvent:
    goal = payload.get("goal")
    return StateEvent(
        index=index,
        t=t,
        ts_mono=ts_mono,
        ts_wall=ts_wall,
        kind=kind,
        mode=str(payload.get("mode", "")),
        goal_status=str(payload.get("goal_status", "")),
        distance_m=_num(payload.get("distance_m")),
        distance_known=bool(payload.get("distance_known", True)),
        obstacle=bool(payload.get("obstacle", False)),
        speed_limit=_num(payload.get("speed_limit")),
        goal=dict(goal) if isinstance(goal, dict) else {},
    )


def _close_goal(
    goal: Goal, *, end_index: int, t_end: float, outcome: str, final_goal: Dict[str, Any], reason: Optional[str]
) -> None:
    goal.end_index = end_index
    goal.t_end = t_end
    goal.outcome = outcome
    goal.final_goal = dict(final_goal)
    goal.reason = reason


def load_run(path: "os.PathLike[str] | str") -> Run:
    """Parse one telemetry JSONL file into a :class:`Run`.

    Pure (reads the file, touches nothing else). Corrupt lines are skipped and
    counted (``Run.corrupt``); JSON lines that are not recorder events (no
    ``type`` / numeric ``ts_monotonic``) are counted as ``Run.malformed``.
    Raises ``FileNotFoundError`` for a missing file.
    """
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"telemetry file not found: {p}")
    stats: Dict[str, int] = {}
    events: List[Dict[str, Any]] = []
    malformed = 0
    for e in read_events(p, stats=stats):
        if isinstance(e, dict) and isinstance(e.get("type"), str) and _num(e.get("ts_monotonic")) is not None:
            events.append(e)
        else:
            malformed += 1

    run = Run(path=p, events=events, corrupt=int(stats.get("corrupt", 0)), malformed=malformed)
    if not events:
        return run
    run.t0 = float(events[0]["ts_monotonic"])

    open_goal: Optional[Goal] = None
    prev_mode: Optional[str] = None
    prev_mono: Optional[float] = None
    prev_wall: Optional[float] = None
    dropped_total = 0
    dropped_sum = 0
    t = 0.0
    for i, e in enumerate(events):
        ts_mono = float(e["ts_monotonic"])
        ts_wall = _num(e.get("ts_wall"))
        # The time axis is the MONOTONIC clock relative to the first event. The
        # recorder appends to the live file across restarts, so a file can span a
        # reboot, after which the monotonic clock restarts from a smaller value:
        # bridge that (and only that) with the wall-clock gap so the axis keeps
        # running forward, and count it so the summary can say so.
        if prev_mono is None:
            t = 0.0
        elif ts_mono >= prev_mono:
            t += ts_mono - prev_mono
        else:
            run.time_resets += 1
            gap = (ts_wall - prev_wall) if (ts_wall is not None and prev_wall is not None) else 0.0
            t += max(0.0, gap)
        prev_mono = ts_mono
        run.times.append(t)
        if ts_wall is not None:
            prev_wall = ts_wall
            if run.first_wall is None:
                run.first_wall = ts_wall
            run.last_wall = ts_wall
        typ = e["type"]
        payload = _payload(e)

        if typ == EVENT_INTENT_ADOPTED:
            mode = payload.get("mode")
            if mode is None:
                continue  # control-only intent (speed-limit rider): no goal boundary
            if open_goal is not None:
                _close_goal(
                    open_goal,
                    end_index=i,
                    t_end=t,
                    outcome=OUTCOME_UNKNOWN,
                    final_goal={},
                    reason="no terminal transition recorded before the next adoption",
                )
                open_goal = None
            if mode == Mode.DRIVE_GOAL.value:
                g = payload.get("goal")
                g = dict(g) if isinstance(g, dict) else {}
                open_goal = Goal(
                    number=len(run.goals) + 1,
                    seq=int(_num(payload.get("seq")) or 0),
                    kind=str(g.get("kind", "?")),
                    target=_num(g.get("target")),
                    speed=_num(g.get("speed")),
                    start_index=i,
                    t_start=t,
                    intent_goal=g,
                )
                run.goals.append(open_goal)

        elif typ == EVENT_MOTOR_COMMAND:
            m = MotorEvent(
                index=i,
                t=t,
                ts_mono=ts_mono,
                ts_wall=ts_wall,
                left=_num(payload.get("left")) or 0.0,
                right=_num(payload.get("right")) or 0.0,
            )
            run.motors.append(m)
            if open_goal is not None:
                open_goal.motors.append(m)

        elif typ in (EVENT_STATE_SAMPLE, EVENT_STATE_TRANSITION):
            se = _state_event(i, t, ts_mono, ts_wall, typ, payload)
            run.states.append(se)
            if open_goal is not None:
                open_goal.states.append(se)
            if typ == EVENT_STATE_TRANSITION:
                run.transitions.append(se)
                if se.mode == Mode.SAFE_STOP.value and prev_mode != Mode.SAFE_STOP.value:
                    run.safe_stops.append(se)
                prev_mode = se.mode
                outcome = _TERMINAL_OUTCOMES.get(se.goal_status)
                if open_goal is not None and outcome is not None:
                    reason = se.goal.get("reason")
                    _close_goal(
                        open_goal,
                        end_index=i,
                        t_end=t,
                        outcome=outcome,
                        final_goal=se.goal,
                        reason=str(reason) if reason is not None else None,
                    )
                    open_goal = None

        elif typ == EVENT_WATCHDOG_TRIP:
            run.watchdog_trips += 1

        elif typ == EVENT_DROP_SUMMARY:
            dropped_sum += int(_num(payload.get("dropped")) or 0)
            dropped_total = max(dropped_total, int(_num(payload.get("dropped_total")) or 0))

    run.t_last = t
    run.dropped = max(dropped_total, dropped_sum)
    if open_goal is not None:
        open_goal.t_end = run.t_last  # outcome stays OUTCOME_UNFINISHED
    return run


# --------------------------------------------------------------------------- #
# Summary
# --------------------------------------------------------------------------- #
def summarize(run: Run) -> List[Dict[str, Any]]:
    """One dict per DRIVE_GOAL adoption (in file order). Pure."""
    rows: List[Dict[str, Any]] = []
    for g in run.goals:
        headings = [s.heading_deg for s in g.states if s.heading_deg is not None]
        lives = [s.heading_live for s in g.states if s.heading_live is not None]
        known = [s.distance_m for s in g.states if s.distance_known and s.distance_m is not None]
        final_heading = _num(g.final_goal.get("heading_deg"))
        if final_heading is None and headings:
            final_heading = headings[-1]  # BLOCKED replaces the goal dict: fall back to the last sample
        bias = _num(g.final_goal.get("learned_bias"))
        if bias is None:
            for s in reversed(g.states):
                bias = _num(s.goal.get("learned_bias"))
                if bias is not None:
                    break
        closure = g.final_goal.get("closure")
        rows.append(
            {
                "goal": g.number,
                "seq": g.seq,
                "kind": g.kind,
                "target": g.target,
                "speed": g.speed,
                "outcome": g.outcome,
                "reason": g.reason,
                "closure": str(closure) if closure is not None else None,
                "t_start": g.t_start,
                "t_end": g.t_end,
                "duration_s": g.duration_s,
                "final_heading_deg": final_heading,
                "max_abs_heading_deg": max(abs(h) for h in headings) if headings else None,
                "min_distance_m": min(known) if known else None,
                "heading_seen": bool(headings),
                "heading_blind": (False in lives) if lives else None,
                "learned_bias": bias,
                "n_states": len(g.states),
                "n_motor_commands": len(g.motors),
            }
        )
    return rows


def run_totals(run: Run) -> Dict[str, Any]:
    """Whole-file counters that go under the per-goal table. Pure."""
    outcomes = [g.outcome for g in run.goals]
    return {
        "goals": len(run.goals),
        "completed": outcomes.count(OUTCOME_COMPLETED),
        "blocked": outcomes.count(OUTCOME_BLOCKED),
        "preempted": outcomes.count(OUTCOME_PREEMPTED),
        "unfinished": outcomes.count(OUTCOME_UNFINISHED),
        "unknown": outcomes.count(OUTCOME_UNKNOWN),
        "safe_stops": len(run.safe_stops),
        "watchdog_trips": run.watchdog_trips,
        "dropped": run.dropped,
        "corrupt_lines": run.corrupt,
        "malformed_events": run.malformed,
        "time_resets": run.time_resets,
        "events": len(run.events),
        "duration_s": run.duration_s,
    }


_SUMMARY_COLUMNS: Tuple[Tuple[str, str, str], ...] = (
    # (key, header, align)
    ("goal", "#", ">"),
    ("seq", "seq", ">"),
    ("kind", "kind", "<"),
    ("target", "target", ">"),
    ("speed", "speed", ">"),
    ("outcome", "outcome", "<"),
    ("reason", "reason", "<"),
    ("duration_s", "dur_s", ">"),
    ("final_heading_deg", "hdg_end", ">"),
    ("max_abs_heading_deg", "|hdg|max", ">"),
    ("min_distance_m", "min_dist", ">"),
    ("heading_blind", "blind", "<"),
    ("learned_bias", "bias", ">"),
)


def _summary_cell(row: Dict[str, Any], key: str) -> str:
    v = row.get(key)
    if key == "target":
        return _fmt_target(str(row.get("kind")), v)
    if key == "speed":
        return _fmt_opt(v, ".2f")
    if key == "duration_s" or key == "max_abs_heading_deg":
        return _fmt_opt(v, ".1f")
    if key == "final_heading_deg":
        return _fmt_opt(v, "+.1f")
    if key == "min_distance_m":
        return _fmt_opt(v, ".2f")
    if key == "learned_bias":
        return _fmt_opt(v, "+.4f")
    if key == "heading_blind":
        return "-" if v is None else ("YES" if v else "no")
    if v is None:
        return "-"
    return str(v)


def format_summary(run: Run, *, last_only: bool = False) -> str:
    """Render the per-goal table + totals as text (what ``summary`` prints)."""
    rows = summarize(run)
    if last_only:
        rows = rows[-1:]
    tot = run_totals(run)
    lines = [
        f"telemetry summary: {run.path}",
        f"  events={tot['events']}  corrupt_lines={tot['corrupt_lines']}"
        + (f"  malformed_events={tot['malformed_events']}" if tot["malformed_events"] else "")
        + (f"  time_resets={tot['time_resets']} (file spans a reboot)" if tot["time_resets"] else "")
        + f"  span={_wall_str(run.first_wall)} .. {_wall_str(run.last_wall)}  ({run.duration_s:.1f} s)",
        "",
    ]
    if not rows:
        lines.append("  (no DRIVE_GOAL adoptions in this file)")
    else:
        table = [[_summary_cell(r, key) for key, _h, _a in _SUMMARY_COLUMNS] for r in rows]
        headers = [h for _k, h, _a in _SUMMARY_COLUMNS]
        widths = [max(len(h), *(len(t[i]) for t in table)) for i, h in enumerate(headers)]
        aligns = [a for _k, _h, a in _SUMMARY_COLUMNS]

        def fmt(cells: Sequence[str]) -> str:
            return "  " + "  ".join(format(c, f"{a}{w}") for c, a, w in zip(cells, aligns, widths))

        lines.append(fmt(headers))
        lines.extend(fmt(t) for t in table)
        if last_only and len(run.goals) > 1:
            lines.append(f"  (last of {len(run.goals)} goals)")
    lines.append("")
    lines.append(
        f"totals: goals={tot['goals']} (completed={tot['completed']} blocked={tot['blocked']} "
        f"preempted={tot['preempted']} unfinished={tot['unfinished']}"
        + (f" unknown={tot['unknown']}" if tot["unknown"] else "")
        + f")  safe_stops={tot['safe_stops']}  watchdog_trips={tot['watchdog_trips']}  "
        f"dropped_events={tot['dropped']}  corrupt_lines={tot['corrupt_lines']}"
    )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CSV export
# --------------------------------------------------------------------------- #
def to_csv_rows(run: Run) -> List[Dict[str, Any]]:
    """Flatten ``state_sample`` + ``state_transition`` events (file order). Pure."""
    rows: List[Dict[str, Any]] = []
    for s in run.states:
        g = s.goal
        rows.append(
            {
                "ts_mono": s.ts_mono,
                "ts_wall": s.ts_wall,
                "type": s.kind,
                "mode": s.mode,
                "goal_status": s.goal_status,
                "distance_m": s.distance_m,
                "distance_known": s.distance_known,
                "obstacle": s.obstacle,
                "speed_limit": s.speed_limit,
                "goal.kind": g.get("kind"),
                "goal.target": _num(g.get("target")),
                "goal.progress": _num(g.get("progress")),
                "goal.heading_deg": s.heading_deg,
                "goal.yaw_rate_dps": _num(g.get("yaw_rate_dps")),
                "goal.heading_live": s.heading_live,
                "t_rel": s.t,
            }
        )
    return rows


def motor_csv_rows(run: Run) -> List[Dict[str, Any]]:
    """Flatten ``motor_command`` events. Pure."""
    return [
        {"ts_mono": m.ts_mono, "ts_wall": m.ts_wall, "left": m.left, "right": m.right, "t_rel": m.t}
        for m in run.motors
    ]


def _csv_cell(v: Any) -> Any:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    return v


def write_csv(path: "os.PathLike[str] | str", columns: Sequence[str], rows: Iterable[Dict[str, Any]]) -> int:
    """Write ``rows`` (dicts) under ``columns`` to ``path``; returns the row count."""
    n = 0
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(columns), extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            writer.writerow({c: _csv_cell(r.get(c)) for c in columns})
            n += 1
    return n


# --------------------------------------------------------------------------- #
# Plot: view selection (shared by the SVG and matplotlib renderers)
# --------------------------------------------------------------------------- #
@dataclass
class PlotView:
    """The time window and the events / spans / markers that fall inside it."""

    t_lo: float
    t_hi: float
    states: List[StateEvent]
    motors: List[MotorEvent]
    goal_spans: List[Tuple[float, float, str]]  # (t_start, t_end, label)
    safe_stops: List[Tuple[float, str]]  # (t, label)
    subtitle: str


def _safe_stop_label(s: StateEvent) -> str:
    label = "SAFE_STOP"
    reason = s.goal.get("reason")
    if reason:
        label += f" {reason}"
    dist = _num(s.goal.get("distance"))
    if dist is not None:
        label += f" {dist:.2f} m"
    return label


def select_view(run: Run, *, goal: Optional[int] = None, from_seq: Optional[int] = None) -> PlotView:
    """Pick the plotted time window: whole file, ``--goal K`` or ``--from-seq N``.

    Raises ``ValueError`` when the selection matches nothing (the CLI turns that
    into a message + exit 1).
    """
    if goal is not None and from_seq is not None:
        raise ValueError("--goal and --from-seq are mutually exclusive")
    t_end_run = run.duration_s
    if goal is not None:
        k = int(goal)
        if k < 1 or k > len(run.goals):
            raise ValueError(f"--goal {k}: this file has {len(run.goals)} DRIVE_GOAL adoption(s)")
        g = run.goals[k - 1]
        t_lo = g.t_start
        t_hi = g.t_end if g.t_end is not None else t_end_run
        pad = max(0.25, 0.05 * (t_hi - t_lo))
        t_lo, t_hi = max(0.0, t_lo - pad), t_hi + pad
        subtitle = f"goal #{k}: {g.kind} {_fmt_target(g.kind, g.target)} (seq {g.seq}, {g.outcome})"
    elif from_seq is not None:
        n = int(from_seq)
        starts = [
            run.times[i]
            for i, e in enumerate(run.events)
            if e["type"] == EVENT_INTENT_ADOPTED and (_num(_payload(e).get("seq")) or -math.inf) >= n
        ]
        if not starts:
            raise ValueError(f"--from-seq {n}: no intent with seq >= {n} was adopted in this file")
        t_lo, t_hi = starts[0], t_end_run
        subtitle = f"from intent seq >= {n}"
    else:
        t_lo, t_hi = 0.0, t_end_run
        subtitle = "whole file"
    if t_hi <= t_lo:
        t_hi = t_lo + 1.0

    spans: List[Tuple[float, float, str]] = []
    for g in run.goals:
        g_end = g.t_end if g.t_end is not None else t_end_run
        if g_end < t_lo or g.t_start > t_hi:
            continue
        spans.append(
            (max(g.t_start, t_lo), min(g_end, t_hi), f"#{g.number} {g.kind} {_fmt_target(g.kind, g.target)}")
        )
    return PlotView(
        t_lo=t_lo,
        t_hi=t_hi,
        states=[s for s in run.states if t_lo <= s.t <= t_hi],
        motors=[m for m in run.motors if t_lo <= m.t <= t_hi],
        goal_spans=spans,
        safe_stops=[(s.t, _safe_stop_label(s)) for s in run.safe_stops if t_lo <= s.t <= t_hi],
        subtitle=subtitle,
    )


# --------------------------------------------------------------------------- #
# Plot: hand-written SVG (no dependencies)
# --------------------------------------------------------------------------- #
# Chart chrome on a light surface (a static file has no viewer theme to follow).
_C_SURFACE = "#fcfcfb"
_C_INK = "#0b0b0b"
_C_INK2 = "#52514e"
_C_MUTED = "#898781"
_C_GRID = "#e1e0d9"
_C_AXIS = "#c3c2b7"
# Series: fixed categorical slots (blue / orange are an adjacent, CVD-checked
# pair for the two-series motors panel); aqua for the single-series heading.
_C_DISTANCE = "#2a78d6"
_C_HEADING = "#1baf7a"
_C_LEFT = "#2a78d6"
_C_RIGHT = "#eb6834"
# Status "critical": the SAFE_STOP threshold and SAFE_STOP entries, always
# paired with a text label so the color never carries the meaning alone.
_C_CRITICAL = "#d03b3b"
_C_GOAL_FILL = "#2a78d6"
_FONT = "system-ui, -apple-system, 'Segoe UI', sans-serif"


def _xml(text: Any) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _nice_step(raw: float) -> float:
    if raw <= 0 or not math.isfinite(raw):
        return 1.0
    mag = 10.0 ** math.floor(math.log10(raw))
    for m in (1.0, 2.0, 2.5, 5.0, 10.0):
        if m * mag >= raw - 1e-12:
            return m * mag
    return 10.0 * mag  # pragma: no cover - the loop always returns


def _nice_ticks(lo: float, hi: float, n: int = 5) -> List[float]:
    """Round tick positions covering ``[lo, hi]`` with about ``n`` steps."""
    if hi <= lo:
        hi = lo + 1.0
    step = _nice_step((hi - lo) / max(1, n))
    v = math.ceil(lo / step - 1e-9) * step
    ticks: List[float] = []
    while v <= hi + 1e-9:
        ticks.append(round(v, 10) + 0.0)
        v += step
    return ticks


def _nice_ceil(v: float) -> float:
    """The smallest 1/2/2.5/5 x 10^k at or above ``v`` (``1.0`` for ``v <= 0``)."""
    if v <= 0 or not math.isfinite(v):
        return 1.0
    return _nice_step(v)


def _polylines(
    points: Iterable[Optional[Tuple[float, float]]], cls: str, color: str, dash: str = ""
) -> List[str]:
    """``<polyline>`` elements through ``points``; a ``None`` breaks the line (gap)."""
    out: List[str] = []
    seg: List[str] = []
    dash_attr = f' stroke-dasharray="{dash}"' if dash else ""

    def flush() -> None:
        if len(seg) >= 2:
            out.append(
                f'<polyline class="{cls}" fill="none" stroke="{color}" stroke-width="2" '
                f'stroke-linejoin="round" stroke-linecap="round"{dash_attr} points="{" ".join(seg)}"/>'
            )
        elif len(seg) == 1:  # a lone point still deserves a mark
            x, y = seg[0].split(",")
            out.append(f'<circle class="{cls}" cx="{x}" cy="{y}" r="2.5" fill="{color}"/>')
        seg.clear()

    for pt in points:
        if pt is None:
            flush()
            continue
        seg.append(f"{pt[0]:.1f},{pt[1]:.1f}")
    flush()
    return out


def _text(
    x: float,
    y: float,
    s: Any,
    *,
    fill: str = _C_INK2,
    size: int = 11,
    anchor: str = "start",
    weight: str = "",
) -> str:
    w = f' font-weight="{weight}"' if weight else ""
    return (
        f'<text x="{x:.1f}" y="{y:.1f}" fill="{fill}" font-size="{size}" '
        f'text-anchor="{anchor}"{w}>{_xml(s)}</text>'
    )


def render_svg(
    run: Run,
    *,
    goal: Optional[int] = None,
    from_seq: Optional[int] = None,
    threshold: Optional[float] = None,
    width: int = 960,
    panel_height: int = 150,
) -> str:
    """Render the three-panel chart as an SVG string. Pure; no dependencies.

    Panels (top to bottom, one shared time axis in seconds since the first
    event): ``distance_m`` with the SAFE_STOP threshold (``threshold`` or
    ``config.SAFE_STOP_THRESHOLD_M``), ``goal.heading_deg``, and the left/right
    motor commands. DRIVE_GOAL spans are shaded; entries into SAFE_STOP are
    marked with a labeled vertical line. Unknown distance samples break the
    distance line and are drawn as open markers; blind heading samples
    (``heading_live == False``) are drawn as open markers.
    """
    view = select_view(run, goal=goal, from_seq=from_seq)
    thr = float(config.SAFE_STOP_THRESHOLD_M if threshold is None else threshold)
    return _svg_document(run, view, thr, int(width), int(panel_height))


def _svg_document(run: Run, view: PlotView, thr: float, width: int, panel_height: int) -> str:
    L, R, TOP, GAP, BOT = 64, 28, 74, 34, 46
    plot_w = max(120, width - L - R)
    n_panels = 3
    height = TOP + n_panels * panel_height + (n_panels - 1) * GAP + BOT
    span = max(1e-9, view.t_hi - view.t_lo)

    def sx(t: float) -> float:
        return L + (t - view.t_lo) / span * plot_w

    xticks = _nice_ticks(view.t_lo, view.t_hi, 8)

    known = [s.distance_m for s in view.states if s.distance_known and s.distance_m is not None]
    d_hi = _nice_ceil(max([*known, thr * 1.5, 0.5]))
    heads = [abs(s.heading_deg) for s in view.states if s.heading_deg is not None]
    h_hi = _nice_ceil(max([*heads, 2.0]))
    any_blind = any(s.heading_live is False for s in view.states)
    any_unknown = any((not s.distance_known) and s.distance_m is not None for s in view.states)

    panels = (
        ("panel-distance", "distance (m)", 0.0, d_hi),
        ("panel-heading", "heading (deg, + = drifted left)", -h_hi, h_hi),
        ("panel-motors", "motor command (signed throttle)", -1.0, 1.0),
    )

    out: List[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" font-family="{_FONT}" font-size="11">',
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="{_C_SURFACE}"/>',
        _text(L, 22, f"yalp telemetry - {run.path.name}", fill=_C_INK, size=14, weight="600"),
        _text(
            L,
            40,
            f"{view.subtitle} | {len(run.events)} events, {run.corrupt} corrupt lines, "
            f"{run.dropped} dropped | window {view.t_lo:.1f}-{view.t_hi:.1f} s",
        ),
    ]

    for pi, (gid, title, lo, hi) in enumerate(panels):
        y0 = TOP + pi * (panel_height + GAP)
        y1 = y0 + panel_height
        yspan = max(1e-9, hi - lo)

        def sy(v: float, _y1: float = y1, _lo: float = lo, _hi: float = hi, _yspan: float = yspan) -> float:
            v = min(_hi, max(_lo, v))
            return _y1 - (v - _lo) / _yspan * panel_height

        out.append(f'<g id="{gid}">')
        out.append(_text(L, y0 - 8, title, fill=_C_INK, weight="600"))
        # DRIVE_GOAL spans (shaded), labeled on the top panel only.
        for a, b, label in view.goal_spans:
            xa, xb = sx(a), sx(b)
            out.append(
                f'<rect class="goal-span" x="{xa:.1f}" y="{y0}" width="{max(1.0, xb - xa):.1f}" '
                f'height="{panel_height}" fill="{_C_GOAL_FILL}" fill-opacity="0.08"/>'
            )
            if pi == 0:
                out.append(_text(xa + 4, y0 + 12, label, size=10))
        # Grid + y ticks.
        for v in _nice_ticks(lo, hi, 4):
            y = sy(v)
            out.append(
                f'<line class="grid" x1="{L}" y1="{y:.1f}" x2="{L + plot_w}" y2="{y:.1f}" '
                f'stroke="{_C_GRID}" stroke-width="1"/>'
            )
            out.append(_text(L - 8, y + 4, f"{v:g}", fill=_C_MUTED, size=10, anchor="end"))
        for v in xticks:
            x = sx(v)
            out.append(
                f'<line class="grid" x1="{x:.1f}" y1="{y0}" x2="{x:.1f}" y2="{y1}" '
                f'stroke="{_C_GRID}" stroke-width="1"/>'
            )
        if lo < 0.0 < hi:
            out.append(
                f'<line class="zero" x1="{L}" y1="{sy(0.0):.1f}" x2="{L + plot_w}" y2="{sy(0.0):.1f}" '
                f'stroke="{_C_AXIS}" stroke-width="1"/>'
            )
        # Axes.
        out.append(
            f'<line class="axis" x1="{L}" y1="{y1}" x2="{L + plot_w}" y2="{y1}" stroke="{_C_AXIS}" stroke-width="1"/>'
        )
        out.append(f'<line class="axis" x1="{L}" y1="{y0}" x2="{L}" y2="{y1}" stroke="{_C_AXIS}" stroke-width="1"/>')

        # Series.
        if gid == "panel-distance":
            pts: List[Optional[Tuple[float, float]]] = []
            for s in view.states:
                if s.distance_m is None:
                    continue
                if s.distance_known:
                    pts.append((sx(s.t), sy(s.distance_m)))
                else:
                    pts.append(None)
                    out.append(
                        f'<circle class="distance-unknown" cx="{sx(s.t):.1f}" cy="{sy(s.distance_m):.1f}" '
                        f'r="3" fill="{_C_SURFACE}" stroke="{_C_MUTED}" stroke-width="1.5"/>'
                    )
            out.extend(_polylines(pts, "series distance", _C_DISTANCE))
            yt = sy(thr)
            out.append(
                f'<line id="safe-stop-threshold" x1="{L}" y1="{yt:.1f}" x2="{L + plot_w}" y2="{yt:.1f}" '
                f'stroke="{_C_CRITICAL}" stroke-width="1.5" stroke-dasharray="6 4"/>'
            )
            out.append(
                _text(L + plot_w - 4, yt - 4, f"SAFE_STOP {thr:.2f} m", fill=_C_CRITICAL, size=10, anchor="end")
            )
            if any_unknown:
                out.append(
                    _text(
                        L + plot_w - 4,
                        y0 + 12,
                        "open markers: distance UNKNOWN (echo miss)",
                        fill=_C_MUTED,
                        size=10,
                        anchor="end",
                    )
                )
        elif gid == "panel-heading":
            hp = [(s, s.heading_deg) for s in view.states if s.heading_deg is not None]
            out.extend(_polylines([(sx(s.t), sy(h)) for s, h in hp], "series heading", _C_HEADING))
            for s, h in hp:
                if s.heading_live is False:
                    out.append(
                        f'<circle class="heading-blind" cx="{sx(s.t):.1f}" cy="{sy(h):.1f}" r="3.5" '
                        f'fill="{_C_SURFACE}" stroke="{_C_HEADING}" stroke-width="1.5"/>'
                    )
            if any_blind:
                out.append(
                    _text(
                        L + plot_w - 4,
                        y0 + 12,
                        "open markers: heading estimate BLIND",
                        fill=_C_MUTED,
                        size=10,
                        anchor="end",
                    )
                )
            if not hp:
                out.append(
                    _text(
                        L + plot_w / 2,
                        y0 + panel_height / 2,
                        "no heading data in window (no yaw estimator?)",
                        fill=_C_MUTED,
                        anchor="middle",
                    )
                )
        else:
            out.extend(_polylines([(sx(m.t), sy(m.left)) for m in view.motors], "series motor-left", _C_LEFT))
            out.extend(
                _polylines([(sx(m.t), sy(m.right)) for m in view.motors], "series motor-right", _C_RIGHT, dash="5 3")
            )
            # Legend (two series in one panel).
            lx = L + plot_w - 120
            ly = y0 + 10
            out.append('<g class="legend">')
            out.append(f'<line x1="{lx}" y1="{ly}" x2="{lx + 18}" y2="{ly}" stroke="{_C_LEFT}" stroke-width="2"/>')
            out.append(_text(lx + 24, ly + 4, "left", size=10))
            out.append(
                f'<line x1="{lx + 60}" y1="{ly}" x2="{lx + 78}" y2="{ly}" stroke="{_C_RIGHT}" '
                f'stroke-width="2" stroke-dasharray="5 3"/>'
            )
            out.append(_text(lx + 84, ly + 4, "right", size=10))
            out.append("</g>")
            if not view.motors:
                out.append(
                    _text(
                        L + plot_w / 2,
                        y0 + panel_height / 2,
                        "no motor commands in window",
                        fill=_C_MUTED,
                        anchor="middle",
                    )
                )

        # SAFE_STOP entries: a vertical marker in every panel, labeled on the top one.
        for t, label in view.safe_stops:
            x = sx(t)
            out.append(
                f'<line class="safe-stop" x1="{x:.1f}" y1="{y0}" x2="{x:.1f}" y2="{y1}" '
                f'stroke="{_C_CRITICAL}" stroke-width="1.5" stroke-dasharray="3 3"/>'
            )
            if pi == 0:
                near_right = x > L + plot_w * 0.8
                out.append(
                    _text(
                        x - 4 if near_right else x + 4,
                        y1 - 6,
                        label,
                        fill=_C_CRITICAL,
                        size=10,
                        anchor="end" if near_right else "start",
                    )
                )

        # x tick labels only under the bottom panel.
        if pi == n_panels - 1:
            for v in xticks:
                out.append(_text(sx(v), y1 + 14, f"{v:g}", fill=_C_MUTED, size=10, anchor="middle"))
            out.append(_text(L + plot_w / 2, y1 + 32, "time since first event (s)", anchor="middle"))
        out.append("</g>")

    out.append("</svg>")
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# Plot: matplotlib PNG (optional; same view, same semantics)
# --------------------------------------------------------------------------- #
def _import_matplotlib():
    """``matplotlib.pyplot`` on the headless Agg backend, or ``None`` if absent."""
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        return plt
    except Exception:  # noqa: BLE001 — any import trouble means "not available"
        return None


def render_png(
    run: Run,
    out_path: "os.PathLike[str] | str",
    *,
    goal: Optional[int] = None,
    from_seq: Optional[int] = None,
    threshold: Optional[float] = None,
    width: int = 960,
    panel_height: int = 150,
    plt: Any = None,
) -> bool:
    """Render the same three panels with matplotlib to ``out_path``.

    Returns ``False`` (writing nothing) when matplotlib is not importable, so the
    caller can fall back to :func:`render_svg`. ``plt`` is injectable for tests.
    """
    plt = plt if plt is not None else _import_matplotlib()
    if plt is None:
        return False
    view = select_view(run, goal=goal, from_seq=from_seq)
    thr = float(config.SAFE_STOP_THRESHOLD_M if threshold is None else threshold)
    dpi = 96.0
    fig, axes = plt.subplots(
        3, 1, sharex=True, figsize=(width / dpi, (3 * panel_height + 150) / dpi), dpi=dpi
    )
    ax_d, ax_h, ax_m = axes
    for ax in axes:
        for a, b, _label in view.goal_spans:
            ax.axvspan(a, b, color=_C_GOAL_FILL, alpha=0.08, lw=0)
        for t, _label in view.safe_stops:
            ax.axvline(t, color=_C_CRITICAL, ls=":", lw=1.2)
        ax.grid(True, color=_C_GRID, lw=0.8)
        ax.set_xlim(view.t_lo, view.t_hi)

    nan = float("nan")
    xs = [s.t for s in view.states if s.distance_m is not None]
    ys = [s.distance_m if s.distance_known else nan for s in view.states if s.distance_m is not None]
    ax_d.plot(xs, ys, color=_C_DISTANCE, lw=2)
    ax_d.axhline(thr, color=_C_CRITICAL, ls="--", lw=1.2, label=f"SAFE_STOP {thr:.2f} m")
    for a, _b, label in view.goal_spans:
        ax_d.text(a, thr, label, fontsize=8, va="bottom", color=_C_INK2)
    for t, label in view.safe_stops:
        ax_d.text(t, thr, label, fontsize=8, va="top", color=_C_CRITICAL)
    ax_d.set_ylabel("distance (m)")
    ax_d.legend(loc="upper right", frameon=False)

    hp = [(s.t, s.heading_deg, s.heading_live) for s in view.states if s.heading_deg is not None]
    if hp:
        ax_h.plot([p[0] for p in hp], [p[1] for p in hp], color=_C_HEADING, lw=2)
        blind = [(t, h) for t, h, live in hp if live is False]
        if blind:
            ax_h.scatter(
                [b[0] for b in blind],
                [b[1] for b in blind],
                s=28,
                facecolors="none",
                edgecolors=_C_HEADING,
                label="estimate blind",
            )
            ax_h.legend(loc="upper right", frameon=False)
    ax_h.axhline(0.0, color=_C_AXIS, lw=1)
    ax_h.set_ylabel("heading (deg)")

    ax_m.plot([m.t for m in view.motors], [m.left for m in view.motors], color=_C_LEFT, lw=2, label="left")
    ax_m.plot(
        [m.t for m in view.motors], [m.right for m in view.motors], color=_C_RIGHT, lw=2, ls="--", label="right"
    )
    ax_m.set_ylim(-1.05, 1.05)
    ax_m.set_ylabel("throttle")
    ax_m.set_xlabel("time since first event (s)")
    ax_m.legend(loc="upper right", frameon=False)

    fig.suptitle(f"yalp telemetry - {run.path.name} ({view.subtitle})")
    fig.tight_layout()
    fig.savefig(os.fspath(out_path), dpi=dpi)
    plt.close(fig)
    return True


# --------------------------------------------------------------------------- #
# Listing files
# --------------------------------------------------------------------------- #
@dataclass
class FileInfo:
    path: Path
    size: int
    mtime: float
    first_wall: Optional[float]
    last_wall: Optional[float]
    events: Optional[int] = None  # only with count=True
    corrupt: Optional[int] = None


def _is_telemetry_file(name: str) -> bool:
    """``*.jsonl`` and its rotated backups ``*.jsonl.<n>``."""
    stem, dot, suffix = name.rpartition(".")
    if dot and suffix.isdigit():
        name = stem
    return name.endswith(".jsonl")


def telemetry_files(directory: "os.PathLike[str] | str") -> List[Path]:
    """Telemetry files (live + rotated) in ``directory``, newest first by mtime."""
    d = Path(directory).expanduser()
    if not d.is_dir():
        return []
    files = [p for p in d.iterdir() if p.is_file() and _is_telemetry_file(p.name)]
    return sorted(files, key=lambda p: (p.stat().st_mtime, p.name), reverse=True)


def _wall_of_line(raw: bytes) -> Optional[float]:
    raw = raw.strip()
    if not raw:
        return None
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return _num(obj.get("ts_wall")) if isinstance(obj, dict) else None


def _first_last_wall(path: Path) -> Tuple[Optional[float], Optional[float]]:
    """First and last ``ts_wall`` without reading a multi-MB file end to end."""
    first = last = None
    try:
        with path.open("rb") as fh:
            for _ in range(64):
                line = fh.readline()
                if not line:
                    break
                first = _wall_of_line(line)
                if first is not None:
                    break
            size = path.stat().st_size
            fh.seek(max(0, size - 65536))
            for line in reversed(fh.read().splitlines()):
                last = _wall_of_line(line)
                if last is not None:
                    break
    except OSError:
        pass
    return first, last


def describe_file(path: "os.PathLike[str] | str", *, count: bool = False) -> FileInfo:
    p = Path(path)
    st = p.stat()
    first, last = _first_last_wall(p)
    info = FileInfo(path=p, size=st.st_size, mtime=st.st_mtime, first_wall=first, last_wall=last)
    if count:
        stats: Dict[str, int] = {}
        for _ in read_events(p, stats=stats):
            pass
        info.events = int(stats.get("events", 0))
        info.corrupt = int(stats.get("corrupt", 0))
    return info


def format_list(directory: Path, infos: Sequence[FileInfo]) -> str:
    lines = [f"telemetry files in {directory} (newest first):"]
    if not infos:
        lines.append("  (none — the recorder writes *.jsonl here once a reactive run starts)")
        return "\n".join(lines)
    name_w = max(len(i.path.name) for i in infos)
    for i in infos:
        counts = ""
        if i.events is not None:
            counts = f"   ({i.events} events, {i.corrupt} corrupt)"
        lines.append(
            f"  {i.path.name:<{name_w}}  {_human_size(i.size):>9}   "
            f"{_wall_str(i.first_wall)} .. {_wall_str(i.last_wall)}{counts}"
        )
    return "\n".join(lines)


def newest_file(directory: "os.PathLike[str] | str") -> Path:
    d = Path(directory).expanduser()
    if not d.is_dir():
        raise FileNotFoundError(f"telemetry directory not found: {d}")
    files = telemetry_files(d)
    if not files:
        raise FileNotFoundError(f"no telemetry files (*.jsonl) in {d}")
    return files[0]


def resolve_file(file_arg: Optional[str], directory: Optional[str]) -> Path:
    """``FILE`` if given (a directory means "newest in it"), else newest in ``--dir``."""
    if file_arg:
        p = Path(file_arg).expanduser()
        if p.is_dir():
            return newest_file(p)
        if not p.is_file():
            raise FileNotFoundError(f"telemetry file not found: {p}")
        return p
    return newest_file(directory if directory else _default_dir())


# --------------------------------------------------------------------------- #
# CLI registration
# --------------------------------------------------------------------------- #
def add_parser(subparsers) -> None:
    """Register the ``telemetry`` subcommand and its ``list/summary/export/plot`` actions."""
    parser = subparsers.add_parser(
        "telemetry",
        help="Read the flight recorder back: list files, per-drive summary, CSV export, SVG/PNG plot.",
        description=(
            "Read the reactive flight recorder (JSONL under the telemetry directory) "
            "back: list the live + rotated files, print a per-DRIVE_GOAL summary "
            "(outcome, duration, heading and distance extremes, blind spells, learned "
            "bias), export state/motor events to CSV, or plot distance / heading / "
            "motor traces on one time axis as a dependency-free SVG (or PNG via "
            "matplotlib when installed). FILE defaults to the newest file in --dir."
        ),
    )
    parser.set_defaults(handler=run)
    actions = parser.add_subparsers(dest="action", metavar="<action>", required=True)

    def add_dir(p) -> None:
        p.add_argument(
            "--dir",
            default=None,
            metavar="D",
            help=(
                "Telemetry directory (default: $YALP_TELEMETRY_DIR or "
                f"config.TELEMETRY_DIR = {config.TELEMETRY_DIR})."
            ),
        )

    def add_file(p) -> None:
        p.add_argument(
            "file",
            nargs="?",
            default=None,
            metavar="FILE",
            help="Telemetry JSONL file, or a directory (its newest file). Default: newest in --dir.",
        )

    p_list = actions.add_parser("list", help="List telemetry files (live + rotated backups), newest first.")
    add_dir(p_list)
    p_list.add_argument(
        "--count", action="store_true", help="Also count events per file (reads every file fully)."
    )

    p_sum = actions.add_parser("summary", help="Per-DRIVE_GOAL summary table plus run totals.")
    add_file(p_sum)
    add_dir(p_sum)
    p_sum.add_argument(
        "--last",
        action="store_true",
        help="Show only the most recent goal's row (totals still cover the whole file).",
    )

    p_exp = actions.add_parser("export", help="Flatten state (and optionally motor) events to CSV.")
    add_file(p_exp)
    add_dir(p_exp)
    p_exp.add_argument(
        "--csv", required=True, metavar="OUT", help="Write state_sample + state_transition rows here."
    )
    p_exp.add_argument(
        "--motors", default=None, metavar="OUT2", help="Also write motor_command rows (ts, left, right) here."
    )

    p_plot = actions.add_parser("plot", help="Plot distance / heading / motor traces on one time axis.")
    add_file(p_plot)
    add_dir(p_plot)
    p_plot.add_argument(
        "--out",
        required=True,
        metavar="OUT",
        help=(
            "Output path: .svg (dependency-free) or .png (matplotlib; falls back to "
            ".svg when it is not installed)."
        ),
    )
    sel = p_plot.add_mutually_exclusive_group()
    sel.add_argument(
        "--from-seq",
        type=int,
        default=None,
        metavar="N",
        help="Start the window at the adoption of the first intent with seq >= N.",
    )
    sel.add_argument(
        "--goal", type=int, default=None, metavar="K", help="Restrict to the K-th DRIVE_GOAL of the file (1-based)."
    )
    p_plot.add_argument("--width", type=int, default=960, metavar="PX", help="Chart width in pixels (default: 960).")
    p_plot.add_argument(
        "--panel-height", type=int, default=150, metavar="PX", help="Height of each panel in pixels (default: 150)."
    )


# --------------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------------- #
def _err(msg: str) -> None:
    print(f"yalp telemetry: {msg}", file=sys.stderr)


def _run_list(args) -> int:
    directory = Path(getattr(args, "dir", None) or _default_dir()).expanduser()
    if not directory.is_dir():
        raise FileNotFoundError(f"telemetry directory not found: {directory}")
    infos = [describe_file(p, count=bool(getattr(args, "count", False))) for p in telemetry_files(directory)]
    print(format_list(directory, infos))
    return 0


def _run_summary(args) -> int:
    path = resolve_file(getattr(args, "file", None), getattr(args, "dir", None))
    print(format_summary(load_run(path), last_only=bool(getattr(args, "last", False))))
    return 0


def _run_export(args) -> int:
    path = resolve_file(getattr(args, "file", None), getattr(args, "dir", None))
    run_ = load_run(path)
    out = Path(args.csv)
    n = write_csv(out, STATE_CSV_COLUMNS, to_csv_rows(run_))
    print(f"wrote {n} state rows from {path.name} -> {out}")
    motors_out = getattr(args, "motors", None)
    if motors_out:
        m = write_csv(Path(motors_out), MOTOR_CSV_COLUMNS, motor_csv_rows(run_))
        print(f"wrote {m} motor_command rows -> {motors_out}")
    if run_.corrupt:
        print(f"note: {run_.corrupt} corrupt line(s) skipped")
    return 0


def _run_plot(args) -> int:
    path = resolve_file(getattr(args, "file", None), getattr(args, "dir", None))
    run_ = load_run(path)
    out = Path(args.out)
    goal = getattr(args, "goal", None)
    from_seq = getattr(args, "from_seq", None)
    width = int(getattr(args, "width", 960) or 960)
    panel_height = int(getattr(args, "panel_height", 150) or 150)
    view = select_view(run_, goal=goal, from_seq=from_seq)  # validates the selection first
    if out.suffix.lower() == ".png":
        if render_png(run_, out, goal=goal, from_seq=from_seq, width=width, panel_height=panel_height):
            print(
                f"wrote {out} (matplotlib; {view.subtitle}; {len(view.states)} state events, "
                f"{len(view.motors)} motor commands)"
            )
            return 0
        fallback = out.with_suffix(".svg")
        print(f"matplotlib is not importable — writing the dependency-free SVG instead: {fallback}")
        out = fallback
    svg = render_svg(run_, goal=goal, from_seq=from_seq, width=width, panel_height=panel_height)
    out.write_text(svg, encoding="utf-8")
    print(f"wrote {out} ({view.subtitle}; {len(view.states)} state events, {len(view.motors)} motor commands)")
    if run_.corrupt:
        print(f"note: {run_.corrupt} corrupt line(s) skipped")
    return 0


_ACTIONS = {"list": _run_list, "summary": _run_summary, "export": _run_export, "plot": _run_plot}


def run(args) -> int:
    """Handler for ``yalp telemetry <action>``. Returns a process exit code."""
    action = getattr(args, "action", None)
    fn = _ACTIONS.get(action or "")
    if fn is None:
        _err(f"unknown action {action!r} (expected one of: {', '.join(_ACTIONS)})")
        return 1
    try:
        return fn(args)
    except FileNotFoundError as exc:
        _err(str(exc))
        return 1
    except ValueError as exc:  # a --goal / --from-seq selection that matched nothing
        _err(str(exc))
        return 1


__all__ = [
    "add_parser",
    "run",
    "Run",
    "Goal",
    "StateEvent",
    "MotorEvent",
    "PlotView",
    "FileInfo",
    "load_run",
    "summarize",
    "run_totals",
    "format_summary",
    "to_csv_rows",
    "motor_csv_rows",
    "write_csv",
    "select_view",
    "render_svg",
    "render_png",
    "telemetry_files",
    "describe_file",
    "format_list",
    "newest_file",
    "resolve_file",
    "STATE_CSV_COLUMNS",
    "MOTOR_CSV_COLUMNS",
    "OUTCOME_COMPLETED",
    "OUTCOME_BLOCKED",
    "OUTCOME_PREEMPTED",
    "OUTCOME_UNFINISHED",
    "OUTCOME_UNKNOWN",
]
