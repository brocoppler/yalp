"""Tests for ``yalp telemetry`` — the flight-recorder READER.

The fixture is a real recording: the synthetic run below is pushed through the
real :class:`yalp.telemetry.TelemetryLogger` (hand-advanced clocks, sample every
tick) so the JSONL under test is byte-for-byte what the recorder writes — two
drives (one completes, one SAFE_STOPs) with the heading-hold goal fields, a
watchdog trip, then a hand-appended ``drop_summary`` and one corrupt line. A
second, hand-written file covers the reader-side outcomes (preempted / unknown /
unfinished).

Layers:
  * pure helpers — ``load_run`` (goal delimiting), ``summarize`` / ``run_totals``,
    ``to_csv_rows`` / ``motor_csv_rows``, ``select_view`` / ``render_svg``;
  * the matplotlib branch through an injected fake ``plt`` (no matplotlib needed)
    plus the SVG fallback when it is not importable;
  * ``list`` file discovery (newest first, rotated backups, first/last wall);
  * argparse wiring — ``add_parser`` on a fresh subparsers object, ``run()`` exit
    codes, the missing-file error path.

Everything is HEADLESS: no robot, no socket, no display.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from yalp import config
from yalp import telemetry_cli as tc
from yalp.contract.messages import GoalStatus, Intent, Mode, RobotState
from yalp.telemetry import TelemetryLogger


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
class _Clock:
    def __init__(self, start: float) -> None:
        self.t = float(start)

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


T0_MONO = 100.0
T0_WALL = 1_700_000_000.0
DT = 0.2

# Drive 1 running samples: (distance_m, heading_deg, yaw_rate_dps, heading_live)
DRIVE1 = [(1.4, 0.8, 4.0, True), (1.2, 1.9, 5.5, True), (1.1, 2.4, 2.5, True), (1.0, 1.1, -6.5, True), (0.9, 0.3, -4.0, True)]
# Drive 2 running samples: one BLIND sample in the middle.
DRIVE2 = [(0.8, -0.4, -2.0, True), (0.6, -1.2, 0.0, False), (0.4, -1.0, 1.0, True)]


def _goal(kind="straight", target=1.6, speed=0.45, **extra):
    g = {"kind": kind, "target": target, "speed": speed, "progress": 0.0, "elapsed_s": 0.0, "closure": "timed"}
    g.update(extra)
    return g


def _record_two_drives(directory: Path) -> Path:
    """Two drives through the REAL recorder; returns the JSONL path.

    Timeline (relative seconds): idle at 0; drive 1 adopted at 1.0, completes at
    2.2 (learned_bias +0.012); drive 2 adopted at 3.0, SAFE_STOP (obstacle at
    0.25 m) at 3.8; watchdog trip at 4.3; hand-appended drop_summary at 5.0 and a
    corrupt trailing line.
    """
    mono, wall = _Clock(T0_MONO), _Clock(T0_WALL)
    log = TelemetryLogger(directory=directory, sample_hz=0.0, clock=mono, wall_clock=wall)

    def step(dt: float) -> None:
        mono.advance(dt)
        wall.advance(dt)

    # t=0: idle.
    log.on_tick_complete(RobotState(mode=Mode.IDLE, goal_status=GoalStatus.NONE, distance_m=1.8))

    # Drive 1: adopt at t=1.0.
    step(1.0)
    log.on_intent_adopted(Intent(Mode.DRIVE_GOAL, {"kind": "straight", "target": 1.6, "speed": 0.45}, seq=1))
    log.on_motor_command(0.45, 0.45)
    log.on_tick_complete(
        RobotState(
            mode=Mode.DRIVE_GOAL,
            goal=_goal(heading_deg=0.0, yaw_rate_dps=0.0, heading_live=True),
            goal_status=GoalStatus.RUNNING,
            distance_m=1.5,
        )
    )
    for i, (d, h, yr, live) in enumerate(DRIVE1):
        step(DT)
        corr = 0.02 * h
        log.on_motor_command(0.45 + corr, 0.45 - corr)
        log.on_tick_complete(
            RobotState(
                mode=Mode.DRIVE_GOAL,
                goal=_goal(progress=(i + 1) / 6, elapsed_s=(i + 1) * DT, heading_deg=h, yaw_rate_dps=yr, heading_live=live),
                goal_status=GoalStatus.RUNNING,
                distance_m=d,
            )
        )
    step(DT)  # t=2.2: completion (mode drops to IDLE; goal keeps its fields + learned_bias).
    log.on_motor_command(0.0, 0.0)
    log.on_tick_complete(
        RobotState(
            mode=Mode.IDLE,
            goal=_goal(progress=1.0, elapsed_s=1.2, heading_deg=0.3, yaw_rate_dps=-4.0, heading_live=True, learned_bias=0.012),
            goal_status=GoalStatus.COMPLETED,
            distance_m=0.9,
        )
    )

    # Drive 2: adopt at t=3.0, blocked at t=3.8.
    step(0.8)
    log.on_intent_adopted(Intent(Mode.DRIVE_GOAL, {"kind": "straight", "target": 1.6, "speed": 0.45}, seq=2))
    log.on_motor_command(0.45, 0.45)
    log.on_tick_complete(
        RobotState(
            mode=Mode.DRIVE_GOAL,
            goal=_goal(heading_deg=0.0, yaw_rate_dps=0.0, heading_live=True),
            goal_status=GoalStatus.RUNNING,
            distance_m=0.9,
        )
    )
    for i, (d, h, yr, live) in enumerate(DRIVE2):
        step(DT)
        log.on_motor_command(0.45, 0.45)
        log.on_tick_complete(
            RobotState(
                mode=Mode.DRIVE_GOAL,
                goal=_goal(progress=(i + 1) / 6, elapsed_s=(i + 1) * DT, heading_deg=h, yaw_rate_dps=yr, heading_live=live),
                goal_status=GoalStatus.RUNNING,
                distance_m=d,
            )
        )
    step(DT)  # t=3.8: the reflex fires — the goal dict is REPLACED (tick_core).
    log.on_motor_command(0.0, 0.0)
    log.on_tick_complete(
        RobotState(
            mode=Mode.SAFE_STOP,
            goal={"reason": "obstacle", "distance": 0.25},
            goal_status=GoalStatus.BLOCKED,
            distance_m=0.25,
            obstacle=True,
        )
    )

    # Watchdog trip at t=4.3.
    step(0.5)
    log.on_watchdog_trip(timeout_ms=100.0, trip_count=1)
    log.close()

    path = log.path
    with path.open("a", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {
                    "ts_monotonic": T0_MONO + 5.0,
                    "ts_wall": T0_WALL + 5.0,
                    "type": "drop_summary",
                    "payload": {"dropped": 3, "dropped_total": 3},
                }
            )
            + "\n"
        )
        fh.write("{this line is corrupt\n")
    return path


def _write_events(path: Path, events) -> Path:
    with path.open("w", encoding="utf-8") as fh:
        for t, typ, payload in events:
            fh.write(
                json.dumps({"ts_monotonic": 500.0 + t, "ts_wall": T0_WALL + 100 + t, "type": typ, "payload": payload})
                + "\n"
            )
    return path


def _state(mode, status, goal=None, **kw):
    return RobotState(mode=mode, goal=goal, goal_status=status, **kw).to_dict()


def _write_edge_cases(path: Path) -> Path:
    """Hand-written: goal 1 PREEMPTED, goal 2 UNKNOWN (no terminal transition
    before the next adoption), goal 3 UNFINISHED (file ends mid-drive)."""
    rot = {"kind": "rotate", "target": 90.0, "speed": 0.5}
    st = {"kind": "straight", "target": 1.0, "speed": 0.45}
    return _write_events(
        path,
        [
            (0.0, "state_transition", _state(Mode.IDLE, GoalStatus.NONE)),
            (1.0, "intent_adopted", Intent(Mode.DRIVE_GOAL, rot, seq=5).to_dict()),
            (1.0, "state_transition", _state(Mode.DRIVE_GOAL, GoalStatus.RUNNING, {**rot, "progress": 0.0})),
            (1.5, "state_sample", _state(Mode.DRIVE_GOAL, GoalStatus.RUNNING, {**rot, "progress": 0.3, "heading_deg": 20.0, "heading_live": True})),
            (2.0, "state_transition", _state(Mode.DRIVE_GOAL, GoalStatus.PREEMPTED, {**rot, "progress": 0.5, "reason": "superseded", "preempted_by_seq": 6})),
            (2.05, "intent_adopted", Intent(Mode.DRIVE_GOAL, st, seq=6).to_dict()),
            (2.05, "state_transition", _state(Mode.DRIVE_GOAL, GoalStatus.RUNNING, {**st, "progress": 0.0})),
            (2.5, "state_sample", _state(Mode.DRIVE_GOAL, GoalStatus.RUNNING, {**st, "progress": 0.4})),
            # A control-only intent (mode None) must NOT delimit anything.
            (2.7, "intent_adopted", Intent(None, None, seq=7, speed_limit=0.5).to_dict()),
            # Terminal transition for goal 2 never recorded; the next adoption arrives.
            (3.0, "intent_adopted", Intent(Mode.IDLE, None, seq=8).to_dict()),
            (3.0, "state_transition", _state(Mode.IDLE, GoalStatus.NONE)),
            (3.5, "intent_adopted", Intent(Mode.DRIVE_GOAL, st, seq=9).to_dict()),
            (3.5, "state_transition", _state(Mode.DRIVE_GOAL, GoalStatus.RUNNING, {**st, "progress": 0.0})),
            (4.0, "state_sample", _state(Mode.DRIVE_GOAL, GoalStatus.RUNNING, {**st, "progress": 0.5})),
        ],
    )


@pytest.fixture
def two_drives(tmp_path) -> Path:
    return _record_two_drives(tmp_path / "tel")


@pytest.fixture
def run(two_drives) -> tc.Run:
    return tc.load_run(two_drives)


@pytest.fixture
def edge_run(tmp_path) -> tc.Run:
    return tc.load_run(_write_edge_cases(tmp_path / "edge.jsonl"))


# --------------------------------------------------------------------------- #
# load_run: goal delimiting + counters
# --------------------------------------------------------------------------- #
def test_load_run_delimits_two_goals(run):
    assert len(run.goals) == 2
    g1, g2 = run.goals
    assert (g1.seq, g1.kind, g1.target, g1.speed) == (1, "straight", 1.6, 0.45)
    assert g1.outcome == tc.OUTCOME_COMPLETED and g1.reason is None
    assert g1.t_start == pytest.approx(1.0) and g1.t_end == pytest.approx(2.2)
    assert g1.duration_s == pytest.approx(1.2)
    assert g1.final_goal["closure"] == "timed" and g1.final_goal["learned_bias"] == 0.012

    assert g2.seq == 2 and g2.outcome == tc.OUTCOME_BLOCKED and g2.reason == "obstacle"
    assert g2.t_start == pytest.approx(3.0) and g2.t_end == pytest.approx(3.8)
    assert g2.final_goal == {"reason": "obstacle", "distance": 0.25}

    # Motor commands are attributed to the open goal (adoption tick included).
    assert len(g1.motors) == 1 + len(DRIVE1) + 1
    assert len(g2.motors) == 1 + len(DRIVE2) + 1
    # Every state event inside the goal is captured (transition + samples).
    assert all(s.goal_status in (GoalStatus.RUNNING, GoalStatus.COMPLETED) for s in g1.states)


def test_load_run_counters_and_time_origin(run, two_drives):
    assert run.path == two_drives
    assert run.t0 == pytest.approx(T0_MONO)
    assert run.corrupt == 1  # the hand-appended non-JSON line
    assert run.malformed == 0
    assert run.dropped == 3
    assert run.watchdog_trips == 1
    assert len(run.safe_stops) == 1 and run.safe_stops[0].t == pytest.approx(3.8)
    assert run.duration_s == pytest.approx(5.0)
    assert run.first_wall == pytest.approx(T0_WALL)
    assert run.last_wall == pytest.approx(T0_WALL + 5.0)
    # Everything is keyed off the MONOTONIC clock, relative to the first event.
    assert run.states[0].t == 0.0
    assert all(b.t >= a.t for a, b in zip(run.states, run.states[1:]))


def test_load_run_edge_outcomes(edge_run):
    assert [g.outcome for g in edge_run.goals] == [tc.OUTCOME_PREEMPTED, tc.OUTCOME_UNKNOWN, tc.OUTCOME_UNFINISHED]
    g1, g2, g3 = edge_run.goals
    assert g1.kind == "rotate" and g1.reason == "superseded" and g1.duration_s == pytest.approx(1.0)
    # The control-only intent (seq 7) did not close goal 2; the IDLE adoption did.
    assert g2.t_end == pytest.approx(3.0) and "no terminal transition" in (g2.reason or "")
    assert g3.t_end == pytest.approx(4.0) and g3.end_index is None
    assert edge_run.safe_stops == []


def test_load_run_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        tc.load_run(tmp_path / "nope.jsonl")


def test_load_run_bridges_monotonic_reset_with_wall_gap(tmp_path):
    # The recorder APPENDS to the live file across restarts, so one file can span
    # a reboot after which the monotonic clock restarts lower. The time axis must
    # keep running forward (bridged by the wall-clock gap), and the reset is counted.
    p = tmp_path / "reboot.jsonl"
    W = T0_WALL
    idle = _state(Mode.IDLE, GoalStatus.NONE)
    with p.open("w") as fh:
        for mono, wall in ((100.0, W), (101.0, W + 1), (102.0, W + 2), (50.0, W + 62), (51.0, W + 63)):
            fh.write(json.dumps({"ts_monotonic": mono, "ts_wall": wall, "type": "state_sample", "payload": idle}) + "\n")
    r = tc.load_run(p)
    assert r.time_resets == 1
    assert r.times == pytest.approx([0.0, 1.0, 2.0, 62.0, 63.0])
    assert [s.t for s in r.states] == pytest.approx([0.0, 1.0, 2.0, 62.0, 63.0])
    assert r.duration_s == pytest.approx(63.0)
    assert "time_resets=1" in tc.format_summary(r)
    # A normal file has none, and its axis is plain ts_monotonic - t0.
    r2 = tc.load_run(_write_edge_cases(tmp_path / "edge.jsonl"))
    assert r2.time_resets == 0 and "time_resets" not in tc.format_summary(r2)
    assert r2.times[-1] == pytest.approx(4.0)


def test_load_run_malformed_json_events_are_counted(tmp_path):
    p = tmp_path / "odd.jsonl"
    p.write_text('{"type": "state_sample"}\n[1, 2]\n{"ts_monotonic": 1.0, "type": "motor_command", "payload": {"left": 0.1, "right": 0.2}}\n')
    r = tc.load_run(p)
    assert r.malformed == 2 and len(r.events) == 1 and len(r.motors) == 1


# --------------------------------------------------------------------------- #
# summarize / totals / table
# --------------------------------------------------------------------------- #
def test_summarize_rows(run):
    rows = tc.summarize(run)
    assert len(rows) == 2
    r1, r2 = rows
    assert r1["goal"] == 1 and r1["seq"] == 1 and r1["kind"] == "straight"
    assert r1["target"] == 1.6 and r1["speed"] == 0.45
    assert r1["outcome"] == "completed" and r1["reason"] is None and r1["closure"] == "timed"
    assert r1["duration_s"] == pytest.approx(1.2)
    assert r1["final_heading_deg"] == pytest.approx(0.3)
    assert r1["max_abs_heading_deg"] == pytest.approx(2.4)
    assert r1["min_distance_m"] == pytest.approx(0.9)
    assert r1["heading_seen"] is True and r1["heading_blind"] is False
    assert r1["learned_bias"] == pytest.approx(0.012)

    assert r2["outcome"] == "blocked" and r2["reason"] == "obstacle"
    assert r2["duration_s"] == pytest.approx(0.8)
    assert r2["min_distance_m"] == pytest.approx(0.25)  # the served distance on the BLOCKED transition
    assert r2["max_abs_heading_deg"] == pytest.approx(1.2)
    assert r2["final_heading_deg"] == pytest.approx(-1.0)  # last sample: BLOCKED replaced the goal dict
    assert r2["heading_blind"] is True
    assert r2["learned_bias"] is None and r2["closure"] is None


def test_run_totals(run):
    tot = tc.run_totals(run)
    assert tot["goals"] == 2 and tot["completed"] == 1 and tot["blocked"] == 1 and tot["preempted"] == 0
    assert tot["safe_stops"] == 1 and tot["watchdog_trips"] == 1
    assert tot["dropped"] == 3 and tot["corrupt_lines"] == 1


def test_format_summary_table(run):
    text = tc.format_summary(run)
    assert str(run.path) in text
    assert "corrupt_lines=1" in text
    header = next(line for line in text.splitlines() if "outcome" in line and "|hdg|max" in line)
    for col in ("#", "seq", "kind", "target", "speed", "outcome", "reason", "dur_s", "hdg_end", "min_dist", "blind", "bias"):
        assert col in header
    rows = [line for line in text.splitlines() if "straight" in line]
    assert len(rows) == 2
    assert "completed" in rows[0] and "+0.0120" in rows[0] and "no" in rows[0]
    assert "blocked" in rows[1] and "obstacle" in rows[1] and "YES" in rows[1] and "0.25" in rows[1]
    assert "totals: goals=2 (completed=1 blocked=1 preempted=0 unfinished=0)  safe_stops=1  watchdog_trips=1  dropped_events=3  corrupt_lines=1" in text


def test_format_summary_last_only(run):
    text = tc.format_summary(run, last_only=True)
    rows = [line for line in text.splitlines() if "straight" in line]
    assert len(rows) == 1 and "blocked" in rows[0]
    assert "(last of 2 goals)" in text


def test_format_summary_no_goals(tmp_path):
    p = _write_events(tmp_path / "idle.jsonl", [(0.0, "state_transition", _state(Mode.IDLE, GoalStatus.NONE))])
    text = tc.format_summary(tc.load_run(p))
    assert "no DRIVE_GOAL adoptions" in text and "totals: goals=0" in text


# --------------------------------------------------------------------------- #
# CSV
# --------------------------------------------------------------------------- #
def test_to_csv_rows_columns_and_content(run):
    rows = tc.to_csv_rows(run)
    n_state_events = sum(1 for e in run.events if e["type"] in ("state_sample", "state_transition"))
    assert len(rows) == n_state_events
    for col in ("ts_mono", "ts_wall", "type", "mode", "goal_status", "distance_m", "distance_known", "obstacle", "speed_limit", "goal.kind", "goal.target", "goal.progress", "goal.heading_deg", "goal.yaw_rate_dps", "goal.heading_live"):
        assert col in tc.STATE_CSV_COLUMNS
        assert all(col in r for r in rows)
    completed = next(r for r in rows if r["goal_status"] == GoalStatus.COMPLETED)
    assert completed["mode"] == "IDLE" and completed["goal.kind"] == "straight"
    assert completed["goal.heading_deg"] == pytest.approx(0.3) and completed["goal.heading_live"] is True
    blocked = next(r for r in rows if r["goal_status"] == GoalStatus.BLOCKED)
    assert blocked["mode"] == "SAFE_STOP" and blocked["obstacle"] is True and blocked["goal.kind"] is None
    blind = [r for r in rows if r["goal.heading_live"] is False]
    assert len(blind) == 1 and blind[0]["goal.heading_deg"] == pytest.approx(-1.2)


def test_write_csv_files(run, tmp_path):
    out = tmp_path / "states.csv"
    n = tc.write_csv(out, tc.STATE_CSV_COLUMNS, tc.to_csv_rows(run))
    with out.open(newline="") as fh:
        reader = csv.DictReader(fh)
        assert reader.fieldnames == list(tc.STATE_CSV_COLUMNS)
        data = list(reader)
    assert len(data) == n == len(run.states)
    assert {d["distance_known"] for d in data} <= {"true", "false"}  # booleans are lowercase
    assert any(d["goal.heading_live"] == "false" for d in data)
    assert all(d["goal.kind"] == "" for d in data if d["goal_status"] == GoalStatus.BLOCKED)  # None -> empty cell

    mout = tmp_path / "motors.csv"
    m = tc.write_csv(mout, tc.MOTOR_CSV_COLUMNS, tc.motor_csv_rows(run))
    with mout.open(newline="") as fh:
        mrows = list(csv.DictReader(fh))
    assert m == len(mrows) == sum(1 for e in run.events if e["type"] == "motor_command")
    assert float(mrows[1]["left"]) == pytest.approx(0.45 + 0.02 * DRIVE1[0][1])


# --------------------------------------------------------------------------- #
# select_view + render_svg
# --------------------------------------------------------------------------- #
def test_select_view_whole_file(run):
    v = tc.select_view(run)
    assert (v.t_lo, v.t_hi) == (0.0, pytest.approx(5.0))
    assert len(v.goal_spans) == 2 and len(v.safe_stops) == 1
    assert v.goal_spans[0][2].startswith("#1 straight")
    assert v.safe_stops[0] == (pytest.approx(3.8), "SAFE_STOP obstacle 0.25 m")


def test_select_view_goal_and_from_seq(run):
    v1 = tc.select_view(run, goal=1)
    assert v1.t_lo <= 1.0 and v1.t_hi >= 2.2 and v1.t_hi < 3.0
    assert len(v1.goal_spans) == 1 and v1.safe_stops == []
    v2 = tc.select_view(run, from_seq=2)
    assert v2.t_lo == pytest.approx(3.0) and len(v2.goal_spans) == 1 and len(v2.safe_stops) == 1
    with pytest.raises(ValueError):
        tc.select_view(run, goal=3)
    with pytest.raises(ValueError):
        tc.select_view(run, goal=0)
    with pytest.raises(ValueError):
        tc.select_view(run, from_seq=99)
    with pytest.raises(ValueError):
        tc.select_view(run, goal=1, from_seq=1)


def test_render_svg_structure(run):
    svg = tc.render_svg(run)
    assert svg.startswith("<svg") and svg.rstrip().endswith("</svg>")
    root = ET.fromstring(svg)  # well-formed XML
    assert root.tag.endswith("svg")
    for panel in ("panel-distance", "panel-heading", "panel-motors"):
        assert f'id="{panel}"' in svg
    assert 'id="safe-stop-threshold"' in svg
    assert f"SAFE_STOP {config.SAFE_STOP_THRESHOLD_M:.2f} m" in svg
    for label in ("distance (m)", "heading (deg", "motor command", "time since first event (s)", ">left<", ">right<"):
        assert label in svg
    assert svg.count('class="goal-span"') == 2 * 3  # both goals shaded in each of the three panels
    assert svg.count('class="safe-stop"') == 1 * 3  # one SAFE_STOP entry marked in each panel
    assert "SAFE_STOP obstacle 0.25 m" in svg
    assert svg.count('class="heading-blind"') == 1
    assert 'class="series distance"' in svg and 'class="series heading"' in svg
    assert 'class="series motor-left"' in svg and 'class="series motor-right"' in svg
    assert run.path.name in svg and "1 corrupt lines" in svg and "3 dropped" in svg


def test_render_svg_threshold_position_and_override(run):
    # The threshold line sits at the y for config.SAFE_STOP_THRESHOLD_M; an explicit
    # threshold moves it and relabels it.
    svg_default = tc.render_svg(run)
    svg_custom = tc.render_svg(run, threshold=0.5)
    assert "SAFE_STOP 0.50 m" in svg_custom and "SAFE_STOP 0.50 m" not in svg_default
    line_d = next(l for l in svg_default.splitlines() if 'id="safe-stop-threshold"' in l)
    line_c = next(l for l in svg_custom.splitlines() if 'id="safe-stop-threshold"' in l)
    assert line_d != line_c


def test_render_svg_goal_and_from_seq_windows(run):
    svg1 = tc.render_svg(run, goal=1)
    assert svg1.count('class="goal-span"') == 3 and 'class="safe-stop"' not in svg1
    assert "goal #1" in svg1
    svg2 = tc.render_svg(run, from_seq=2)
    assert svg2.count('class="goal-span"') == 3 and svg2.count('class="safe-stop"') == 3
    with pytest.raises(ValueError):
        tc.render_svg(run, goal=5)


def test_render_svg_empty_run(tmp_path):
    p = tmp_path / "empty.jsonl"
    p.write_text("")
    svg = tc.render_svg(tc.load_run(p))
    ET.fromstring(svg)
    assert "no heading data" in svg and "no motor commands" in svg


def test_nice_ticks():
    assert tc._nice_ticks(0.0, 5.0, 5) == [0.0, 1.0, 2.0, 3.0, 4.0, 5.0]
    assert tc._nice_ticks(-2.0, 2.0, 4) == [-2.0, -1.0, 0.0, 1.0, 2.0]
    assert tc._nice_ceil(0.45) == 0.5 and tc._nice_ceil(2.4) == 2.5 and tc._nice_ceil(0.0) == 1.0


# --------------------------------------------------------------------------- #
# matplotlib branch (injected fake) + fallback
# --------------------------------------------------------------------------- #
def test_render_png_with_injected_plt(run, tmp_path):
    plt = MagicMock()
    fig = MagicMock()
    axes = (MagicMock(), MagicMock(), MagicMock())
    plt.subplots.return_value = (fig, axes)
    out = tmp_path / "plot.png"
    assert tc.render_png(run, out, goal=2, plt=plt) is True
    fig.savefig.assert_called_once()
    assert fig.savefig.call_args.args[0] == str(out)
    axes[0].axhline.assert_called_once()
    assert axes[0].axhline.call_args.args[0] == pytest.approx(config.SAFE_STOP_THRESHOLD_M)
    assert axes[0].axvspan.call_count == 1  # only goal 2 in the window
    assert axes[2].plot.call_count == 2  # left + right
    plt.close.assert_called_once_with(fig)


def test_render_png_without_matplotlib_returns_false(run, tmp_path, monkeypatch):
    monkeypatch.setattr(tc, "_import_matplotlib", lambda: None)
    assert tc.render_png(run, tmp_path / "x.png") is False
    assert not (tmp_path / "x.png").exists()


# --------------------------------------------------------------------------- #
# list
# --------------------------------------------------------------------------- #
def test_telemetry_files_newest_first_with_rotated_backup(two_drives):
    d = two_drives.parent
    backup = d / "telemetry.jsonl.1"
    _write_events(backup, [(0.0, "state_transition", _state(Mode.IDLE, GoalStatus.NONE)), (2.0, "motor_command", {"left": 0, "right": 0})])
    old = two_drives.stat().st_mtime - 3600
    os.utime(backup, (old, old))
    (d / "notes.txt").write_text("ignored")
    files = tc.telemetry_files(d)
    assert [p.name for p in files] == ["telemetry.jsonl", "telemetry.jsonl.1"]
    assert tc.newest_file(d) == two_drives

    info = tc.describe_file(two_drives, count=True)
    assert info.size > 0 and info.events == len(tc.load_run(two_drives).events) and info.corrupt == 1
    assert info.first_wall == pytest.approx(T0_WALL) and info.last_wall == pytest.approx(T0_WALL + 5.0)
    binfo = tc.describe_file(backup)
    assert binfo.events is None and binfo.first_wall == pytest.approx(T0_WALL + 100)

    text = tc.format_list(d, [info, binfo])
    lines = text.splitlines()
    assert lines[0].startswith(f"telemetry files in {d}")
    assert "telemetry.jsonl " in lines[1] and "telemetry.jsonl.1" in lines[2]
    assert f"({info.events} events, 1 corrupt)" in lines[1]
    assert tc._wall_str(T0_WALL) in lines[1]


def test_resolve_file(two_drives, tmp_path):
    assert tc.resolve_file(str(two_drives), None) == two_drives
    assert tc.resolve_file(str(two_drives.parent), None) == two_drives  # a directory -> its newest file
    assert tc.resolve_file(None, str(two_drives.parent)) == two_drives
    with pytest.raises(FileNotFoundError):
        tc.resolve_file(str(tmp_path / "missing.jsonl"), None)
    with pytest.raises(FileNotFoundError):
        tc.resolve_file(None, str(tmp_path / "no-such-dir"))
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(FileNotFoundError):
        tc.resolve_file(None, str(empty))


# --------------------------------------------------------------------------- #
# argparse wiring + run() exit codes
# --------------------------------------------------------------------------- #
def _parse(argv):
    parser = argparse.ArgumentParser(prog="yalp")
    sub = parser.add_subparsers(dest="command", metavar="<command>")
    tc.add_parser(sub)
    return parser.parse_args(argv)


def test_add_parser_registers_actions_and_handler():
    args = _parse(["telemetry", "list", "--dir", "/x", "--count"])
    assert args.handler is tc.run and args.action == "list" and args.dir == "/x" and args.count is True
    args = _parse(["telemetry", "summary", "f.jsonl", "--last"])
    assert args.action == "summary" and args.file == "f.jsonl" and args.last is True
    args = _parse(["telemetry", "export", "--csv", "o.csv", "--motors", "m.csv"])
    assert args.action == "export" and args.file is None and args.csv == "o.csv" and args.motors == "m.csv"
    args = _parse(["telemetry", "plot", "f.jsonl", "--out", "o.svg", "--goal", "2", "--width", "800"])
    assert args.action == "plot" and args.goal == 2 and args.from_seq is None and args.width == 800
    with pytest.raises(SystemExit):
        _parse(["telemetry", "plot", "--out", "o.svg", "--goal", "1", "--from-seq", "1"])  # mutually exclusive
    with pytest.raises(SystemExit):
        _parse(["telemetry"])  # an action is required
    with pytest.raises(SystemExit):
        _parse(["telemetry", "export"])  # --csv is required


def test_cli_summary_ok(two_drives, capsys):
    args = _parse(["telemetry", "summary", str(two_drives)])
    assert args.handler(args) == 0
    out = capsys.readouterr().out
    assert "completed" in out and "blocked" in out and "safe_stops=1" in out


def test_cli_summary_missing_file_exits_nonzero(tmp_path, capsys):
    missing = tmp_path / "nope.jsonl"
    args = _parse(["telemetry", "summary", str(missing)])
    assert tc.run(args) == 1
    err = capsys.readouterr().err
    assert "not found" in err and str(missing) in err


def test_cli_default_file_is_newest_in_env_dir(two_drives, monkeypatch, capsys):
    # No FILE and no --dir: honors $YALP_TELEMETRY_DIR (conftest points it at an
    # empty tmp path by default; re-point it at the recording).
    monkeypatch.setenv("YALP_TELEMETRY_DIR", str(two_drives.parent))
    assert tc.run(_parse(["telemetry", "summary", "--last"])) == 0
    out = capsys.readouterr().out
    assert "(last of 2 goals)" in out


def test_cli_missing_dir_exits_nonzero(tmp_path, capsys):
    assert tc.run(_parse(["telemetry", "list", "--dir", str(tmp_path / "nowhere")])) == 1
    assert "directory not found" in capsys.readouterr().err
    assert tc.run(_parse(["telemetry", "summary", "--dir", str(tmp_path / "nowhere")])) == 1


def test_cli_list(two_drives, capsys):
    assert tc.run(_parse(["telemetry", "list", "--dir", str(two_drives.parent), "--count"])) == 0
    out = capsys.readouterr().out
    assert "telemetry.jsonl" in out and "events, 1 corrupt" in out
    empty = two_drives.parent / "sub"
    empty.mkdir()
    assert tc.run(_parse(["telemetry", "list", "--dir", str(empty)])) == 0
    assert "(none" in capsys.readouterr().out


def test_cli_export(two_drives, tmp_path, capsys):
    out_csv, out_m = tmp_path / "s.csv", tmp_path / "m.csv"
    args = _parse(["telemetry", "export", str(two_drives), "--csv", str(out_csv), "--motors", str(out_m)])
    assert tc.run(args) == 0
    text = capsys.readouterr().out
    assert "state rows" in text and "motor_command rows" in text and "1 corrupt line" in text
    assert out_csv.read_text().splitlines()[0] == ",".join(tc.STATE_CSV_COLUMNS)
    assert out_m.read_text().splitlines()[0] == ",".join(tc.MOTOR_CSV_COLUMNS)


def test_cli_plot_svg(two_drives, tmp_path, capsys):
    out = tmp_path / "drive.svg"
    assert tc.run(_parse(["telemetry", "plot", str(two_drives), "--out", str(out), "--goal", "2"])) == 0
    assert out.read_text(encoding="utf-8").startswith("<svg")
    assert "goal #2" in capsys.readouterr().out


def test_cli_plot_bad_goal_exits_nonzero(two_drives, tmp_path, capsys):
    out = tmp_path / "drive.svg"
    assert tc.run(_parse(["telemetry", "plot", str(two_drives), "--out", str(out), "--goal", "9"])) == 1
    assert "--goal 9" in capsys.readouterr().err
    assert not out.exists()


def test_cli_plot_png_falls_back_to_svg_without_matplotlib(two_drives, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(tc, "_import_matplotlib", lambda: None)
    out = tmp_path / "drive.png"
    assert tc.run(_parse(["telemetry", "plot", str(two_drives), "--out", str(out)])) == 0
    text = capsys.readouterr().out
    assert "matplotlib" in text
    assert not out.exists() and (tmp_path / "drive.svg").read_text(encoding="utf-8").startswith("<svg")


def test_cli_plot_png_uses_matplotlib_when_available(two_drives, tmp_path, monkeypatch, capsys):
    plt = MagicMock()
    plt.subplots.return_value = (MagicMock(), (MagicMock(), MagicMock(), MagicMock()))
    monkeypatch.setattr(tc, "_import_matplotlib", lambda: plt)
    out = tmp_path / "drive.png"
    assert tc.run(_parse(["telemetry", "plot", str(two_drives), "--out", str(out), "--from-seq", "2"])) == 0
    assert "matplotlib" in capsys.readouterr().out and not (tmp_path / "drive.svg").exists()


def test_run_unknown_action(capsys):
    assert tc.run(SimpleNamespace(action="bogus")) == 1
    assert "unknown action" in capsys.readouterr().err
