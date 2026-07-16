"""Tests for the reactive flight recorder (``yalp.telemetry``).

Covers the contract the task pins:
  * events are written and read back (round-trip) for every event type;
  * size-capped rotation keeps only ``backup_count`` backups;
  * ``state_sample`` is RATE-LIMITED while ``state_transition`` is ALWAYS logged
    (even inside the sample window);
  * queue OVERFLOW drops events, counts them, and emits a ``drop_summary``;
  * ``YALP_TELEMETRY=0`` disables it; ``=1`` + a dir writes there;
  * a SLOW disk never blocks a ticking backend loop;
  * the reader skips corrupt lines and reports a counter;
  * payloads are the structured contract objects only (no secrets/env dumps);
  * the tick-core observer seam + watchdog trip edge are wired through.
"""

from __future__ import annotations

import json
import threading
import time

import pytest

import yalp.telemetry as tel
from yalp.telemetry import (
    TelemetryLogger,
    create_logger_from_env,
    read_events,
)
from yalp.contract.messages import GoalStatus, Intent, Mode, RobotState


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
class FakeClock:
    """A hand-advanced monotonic clock so rate-limiting is deterministic."""

    def __init__(self, start: float = 0.0) -> None:
        self.t = float(start)

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _read(path):
    return list(read_events(path))


def _types(events):
    return [e["type"] for e in events]


# --------------------------------------------------------------------------- #
# Events written + read-back round-trip
# --------------------------------------------------------------------------- #
def test_events_written_and_read_back(tmp_path):
    log = TelemetryLogger(directory=tmp_path, sample_hz=0.0)  # 0 = sample every tick
    intent = Intent(Mode.DRIVE_GOAL, {"kind": "straight", "target": 1.0}, seq=7)
    log.on_intent_adopted(intent)
    log.on_motor_command(0.5, -0.25)
    state = RobotState(mode=Mode.DRIVE_GOAL, goal_status=GoalStatus.RUNNING)
    log.on_tick_complete(state)
    log.close()

    events = _read(log.path)
    by_type = {e["type"]: e for e in events}

    assert "intent_adopted" in by_type
    assert "motor_command" in by_type
    assert "state_transition" in by_type  # first tick is always a transition
    assert "state_sample" in by_type

    # Every event carries both clocks + a type + a payload (the schema contract).
    for e in events:
        assert set(e) >= {"ts_monotonic", "ts_wall", "type", "payload"}
        assert isinstance(e["ts_monotonic"], (int, float))
        assert isinstance(e["ts_wall"], (int, float))

    # Payloads are the structured contract objects.
    assert by_type["intent_adopted"]["payload"] == intent.to_dict()
    assert by_type["motor_command"]["payload"] == {"left": 0.5, "right": -0.25}
    assert by_type["state_sample"]["payload"]["mode"] == "DRIVE_GOAL"


def test_state_sample_carries_ultrasonic_counters(tmp_path):
    # The ultrasonic miss/coast counters ride the EXISTING periodic state event
    # (no new event type invented) because they are a field of RobotState.to_dict().
    log = TelemetryLogger(directory=tmp_path, sample_hz=0.0)  # sample every tick
    counters = {
        "total_reads": 12,
        "valid_reads": 4,
        "raw_misses": 8,
        "coasted_reads": 5,
        "unknown_served": 3,
    }
    state = RobotState(
        mode=Mode.DRIVE_GOAL, goal_status=GoalStatus.RUNNING, ultrasonic=counters
    )
    log.on_tick_complete(state)
    log.close()

    by_type = {e["type"]: e for e in _read(log.path)}
    assert by_type["state_sample"]["payload"]["ultrasonic"] == counters
    # A backend with no counter-bearing sensor logs ``ultrasonic: null`` (additive).
    log2 = TelemetryLogger(directory=tmp_path / "b", sample_hz=0.0)
    log2.on_tick_complete(RobotState(mode=Mode.IDLE, goal_status=GoalStatus.NONE))
    log2.close()
    by_type2 = {e["type"]: e for e in _read(log2.path)}
    assert by_type2["state_sample"]["payload"]["ultrasonic"] is None


def test_close_is_idempotent(tmp_path):
    log = TelemetryLogger(directory=tmp_path)
    log.on_motor_command(0.0, 0.0)
    log.close()
    log.close()  # must not raise / hang
    assert log.closed is True


def test_context_manager_flushes(tmp_path):
    with TelemetryLogger(directory=tmp_path, sample_hz=0.0) as log:
        log.on_motor_command(1.0, 1.0)
        path = log.path
    assert any(e["type"] == "motor_command" for e in _read(path))


# --------------------------------------------------------------------------- #
# Rotation
# --------------------------------------------------------------------------- #
def test_rotation_caps_files_and_size(tmp_path):
    # Tiny cap so a handful of events forces several rotations.
    log = TelemetryLogger(
        directory=tmp_path, max_bytes=400, backup_count=2, sample_hz=0.0
    )
    for i in range(200):
        log.on_motor_command(float(i), float(-i))
    log.close()

    live = tmp_path / "telemetry.jsonl"
    b1 = tmp_path / "telemetry.jsonl.1"
    b2 = tmp_path / "telemetry.jsonl.2"
    b3 = tmp_path / "telemetry.jsonl.3"

    assert live.exists()
    assert b1.exists() and b2.exists(), "rotation did not produce backups"
    assert not b3.exists(), "kept more than backup_count backups"

    # No file exceeds the cap (rotation happens before an overflowing write).
    for p in (live, b1, b2):
        assert p.stat().st_size <= 400

    # The live file is still valid JSONL.
    for e in _read(live):
        assert e["type"] == "motor_command"


def test_oversized_single_line_still_written(tmp_path):
    # A payload bigger than max_bytes must still land (in a fresh file), not loop.
    log = TelemetryLogger(directory=tmp_path, max_bytes=50, backup_count=1, sample_hz=0.0)
    big = {"kind": "straight", "note": "x" * 500}
    log.on_intent_adopted(Intent(Mode.DRIVE_GOAL, big, seq=1))
    log.close()
    events = _read(log.path)
    assert events and events[-1]["type"] == "intent_adopted"


# --------------------------------------------------------------------------- #
# Rate limiting (state_sample) vs always-on (state_transition)
# --------------------------------------------------------------------------- #
def test_rate_limit_samples_but_always_logs_transitions(tmp_path):
    clock = FakeClock()
    log = TelemetryLogger(
        directory=tmp_path, sample_hz=2.0, clock=clock, wall_clock=clock
    )  # min sample interval = 0.5 s

    idle = RobotState(mode=Mode.IDLE, goal_status=GoalStatus.NONE)

    log.on_tick_complete(idle)              # t=0.0 -> first transition + sample
    for _ in range(4):
        clock.advance(0.1)                  # t=0.1..0.4 -> within window, nothing
        log.on_tick_complete(idle)
    clock.advance(0.1)                      # t=0.5
    log.on_tick_complete(idle)             # -> sample (0.5 s elapsed), no transition

    clock.advance(0.1)                      # t=0.6 -> inside the sample window
    running = RobotState(mode=Mode.IDLE, goal_status=GoalStatus.RUNNING)
    log.on_tick_complete(running)          # -> transition ONLY (goal_status changed)

    log.close()
    types = _types(_read(log.path))
    assert types.count("state_sample") == 2, types      # rate-limited to 2
    assert types.count("state_transition") == 2, types  # initial + the change


def test_sample_hz_zero_samples_every_tick(tmp_path):
    clock = FakeClock()
    log = TelemetryLogger(directory=tmp_path, sample_hz=0.0, clock=clock, wall_clock=clock)
    idle = RobotState(mode=Mode.IDLE, goal_status=GoalStatus.NONE)
    for _ in range(5):
        log.on_tick_complete(idle)  # clock never advances
    log.close()
    assert _types(_read(log.path)).count("state_sample") == 5


# --------------------------------------------------------------------------- #
# Overflow -> drop counter + drop_summary
# --------------------------------------------------------------------------- #
class _SlowWriter(tel._RotatingWriter):
    """A rotating writer that sleeps on every write (simulates a slow disk)."""

    delay = 0.05

    def write(self, line: str) -> None:
        time.sleep(self.delay)
        super().write(line)


def test_overflow_drops_are_counted_and_summarized(tmp_path, monkeypatch):
    monkeypatch.setattr(tel, "_RotatingWriter", _SlowWriter)
    log = TelemetryLogger(directory=tmp_path, queue_max=5, sample_hz=0.0)

    # Flood far more than the queue can hold while the writer crawls.
    for i in range(300):
        log.on_motor_command(float(i), 0.0)

    assert log.dropped > 0, "a full bounded queue must drop events"

    log.close()  # forces a final drop_summary
    events = _read(log.path)
    summaries = [e for e in events if e["type"] == "drop_summary"]
    assert summaries, "a drop_summary must be recorded"
    assert summaries[-1]["payload"]["dropped_total"] == log.dropped


def test_enqueue_never_raises_into_the_tick(tmp_path, monkeypatch):
    monkeypatch.setattr(tel, "_RotatingWriter", _SlowWriter)
    log = TelemetryLogger(directory=tmp_path, queue_max=1, sample_hz=0.0)
    # Even hammered well past capacity, the producer side never raises.
    for i in range(500):
        log.on_motor_command(float(i), float(i))
        log.on_tick_complete(RobotState(mode=Mode.IDLE, goal_status=GoalStatus.NONE))
    log.close()


# --------------------------------------------------------------------------- #
# YALP_TELEMETRY env toggle
# --------------------------------------------------------------------------- #
def test_env_disables_telemetry(monkeypatch):
    monkeypatch.setenv("YALP_TELEMETRY", "0")
    assert create_logger_from_env() is None


def test_env_enables_and_writes_to_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("YALP_TELEMETRY", "1")
    monkeypatch.setenv("YALP_TELEMETRY_DIR", str(tmp_path))
    log = create_logger_from_env()
    assert log is not None
    try:
        log.on_motor_command(0.1, 0.2)
    finally:
        log.close()
    events = _read(tmp_path / "telemetry.jsonl")
    assert any(e["type"] == "motor_command" for e in events)


def test_env_reload_sets_disabled_constant(monkeypatch):
    """YALP_TELEMETRY=0 flows through config's fail-soft accessor to the constant."""
    import importlib

    import yalp.config as cfg

    monkeypatch.setenv("YALP_TELEMETRY", "0")
    try:
        reloaded = importlib.reload(cfg)
        assert reloaded.TELEMETRY_ENABLED is False
    finally:
        monkeypatch.undo()
        importlib.reload(cfg)


# --------------------------------------------------------------------------- #
# Slow disk never blocks a ticking loop (the safety invariant)
# --------------------------------------------------------------------------- #
def test_slow_disk_does_not_block_ticking_loop(tmp_path, monkeypatch):
    from yalp.reactive.fake_backend import FakeReactiveBackend

    monkeypatch.setattr(tel, "_RotatingWriter", _SlowWriter)
    log = TelemetryLogger(directory=tmp_path, queue_max=5, sample_hz=0.0)
    backend = FakeReactiveBackend(camera_source="synthetic", tick_hz=1000.0, observer=log)

    n = 300
    t0 = time.monotonic()
    for _ in range(n):
        backend.tick()
    elapsed = time.monotonic() - t0

    # Each disk write sleeps 50 ms; if the tick blocked on I/O, 300 ticks (each
    # enqueuing ~2 events) would take many seconds. Non-blocking enqueue keeps the
    # loop far under that even though the writer falls hopelessly behind (dropping).
    assert elapsed < 2.0, f"ticking loop was blocked by slow disk ({elapsed:.2f}s)"
    assert log.dropped > 0  # the writer could not keep up -> events dropped, not stalled

    log.close()
    backend.stop()


# --------------------------------------------------------------------------- #
# Reader: round-trip + corrupt-line skipping with a counter
# --------------------------------------------------------------------------- #
def test_reader_skips_corrupt_lines_with_counter(tmp_path):
    p = tmp_path / "telemetry.jsonl"
    p.write_text(
        json.dumps({"type": "a", "payload": {}}) + "\n"
        + "this is not json\n"
        + "\n"  # blank line: skipped silently, not counted as corrupt
        + json.dumps({"type": "b", "payload": {}}) + "\n"
        + "{broken: ",  # truncated final line (power-cut) -> corrupt
        encoding="utf-8",
    )
    stats: dict[str, int] = {}
    events = list(read_events(p, stats=stats))
    assert [e["type"] for e in events] == ["a", "b"]
    assert stats["events"] == 2
    assert stats["corrupt"] == 2


def test_reader_round_trip_from_logger(tmp_path):
    log = TelemetryLogger(directory=tmp_path, sample_hz=0.0)
    log.on_intent_adopted(Intent(Mode.FOLLOW, {"target": "nearest_person"}, seq=3))
    log.on_motor_command(0.3, 0.3)
    log.close()
    events = list(read_events(log.path))
    assert len(events) >= 2
    adopted = next(e for e in events if e["type"] == "intent_adopted")
    assert adopted["payload"]["mode"] == "FOLLOW"


# --------------------------------------------------------------------------- #
# Watchdog trip edge is recorded
# --------------------------------------------------------------------------- #
class _StopDriver:
    def __init__(self) -> None:
        self.stops = 0

    def stop(self) -> None:
        self.stops += 1


def test_watchdog_trip_is_recorded(tmp_path):
    from yalp.reactive.watchdog import MotorWatchdog

    log = TelemetryLogger(directory=tmp_path, sample_hz=0.0)
    driver = _StopDriver()
    wd = MotorWatchdog(driver, timeout_ms=5)
    wd.on_trip = lambda: log.on_watchdog_trip(
        timeout_ms=wd.timeout_s * 1000.0, trip_count=wd.trip_count
    )
    wd.start()
    deadline = time.time() + 2.0
    while wd.trip_count < 1 and time.time() < deadline:
        time.sleep(0.01)
    wd.stop()
    log.close()

    trips = [e for e in _read(log.path) if e["type"] == "watchdog_trip"]
    assert trips, "watchdog trip must be recorded"
    assert trips[0]["payload"]["trip_count"] >= 1
    assert trips[0]["payload"]["timeout_ms"] == pytest.approx(5.0)


def test_watchdog_on_trip_fires_once_per_edge():
    from yalp.reactive.watchdog import MotorWatchdog

    calls = []
    driver = _StopDriver()
    wd = MotorWatchdog(driver, timeout_ms=5, on_trip=lambda: calls.append(1))
    wd.start()
    deadline = time.time() + 2.0
    while wd.trip_count < 1 and time.time() < deadline:
        time.sleep(0.01)
    # Stay stale a bit longer: the trip is latched, so no extra callbacks fire.
    time.sleep(0.1)
    wd.stop()
    assert wd.trip_count == 1
    assert len(calls) == 1


# --------------------------------------------------------------------------- #
# Tick-core observer seam wiring (FakeReactiveBackend -> logger)
# --------------------------------------------------------------------------- #
def test_backend_observer_seam_records_intent_and_motor(tmp_path):
    from yalp.reactive.fake_backend import FakeReactiveBackend

    log = TelemetryLogger(directory=tmp_path, sample_hz=0.0)
    backend = FakeReactiveBackend(camera_source="synthetic", tick_hz=20.0, observer=log)
    backend.apply_intent(
        Intent(Mode.DRIVE_GOAL, {"kind": "straight", "target": 1.0, "speed": 0.5}, seq=1)
    )
    backend.tick()  # adopts the intent, commands motors, publishes a snapshot
    backend.tick()
    log.close()
    backend.stop()

    types = set(_types(_read(log.path)))
    assert "intent_adopted" in types
    assert "motor_command" in types
    assert "state_transition" in types


def test_observer_exception_never_breaks_the_tick(tmp_path):
    """A misbehaving observer must never break the safety tick (it is guarded)."""
    from yalp.reactive.fake_backend import FakeReactiveBackend

    class _Boom:
        def on_intent_adopted(self, intent):
            raise RuntimeError("boom")

        def on_motor_command(self, left, right):
            raise RuntimeError("boom")

        def on_tick_complete(self, state):
            raise RuntimeError("boom")

    backend = FakeReactiveBackend(camera_source="synthetic", tick_hz=20.0, observer=_Boom())
    backend.apply_intent(Intent(Mode.DRIVE_GOAL, {"kind": "straight", "target": 1.0}, seq=1))
    state = backend.tick()  # must NOT raise
    assert state is not None


# --------------------------------------------------------------------------- #
# build_follow_backend wiring (default-on when enabled; injectable/off)
# --------------------------------------------------------------------------- #
def test_build_follow_backend_auto_creates_owned_logger(monkeypatch, tmp_path):
    monkeypatch.setenv("YALP_TELEMETRY", "1")
    monkeypatch.setenv("YALP_TELEMETRY_DIR", str(tmp_path))
    from yalp.reactive.follow_runner import build_follow_backend

    backend = build_follow_backend(source="synthetic", detector="face", hz=30.0)
    try:
        assert isinstance(backend._observer, TelemetryLogger)
        assert backend._close_observer is True
    finally:
        backend.stop()  # owns the logger -> closes it
    assert backend._observer.closed is True


def test_build_follow_backend_respects_explicit_none(monkeypatch, tmp_path):
    monkeypatch.setenv("YALP_TELEMETRY", "1")
    monkeypatch.setenv("YALP_TELEMETRY_DIR", str(tmp_path))
    from yalp.reactive.follow_runner import build_follow_backend

    backend = build_follow_backend(
        source="synthetic", detector="face", hz=30.0, observer=None
    )
    try:
        assert backend._observer is None
        assert backend._close_observer is False
    finally:
        backend.stop()


def test_build_follow_backend_disabled_env_no_observer(tmp_path):
    # Autouse conftest sets YALP_TELEMETRY=0, so the default is no recorder.
    from yalp.reactive.follow_runner import build_follow_backend

    backend = build_follow_backend(source="synthetic", detector="face", hz=30.0)
    try:
        assert backend._observer is None
    finally:
        backend.stop()
