"""Tests for ``yalp drive`` — the one-command hardware-drive subcommand.

Two layers, matching the roadmap for the feature-module CLIs:

  * **CLI plumbing** (mirrors ``tests/test_bench_cli.py``): the subcommand is
    registered in ``FEATURE_MODULES``, ``build_parser`` accepts it with the right
    flags, and the handler runs through ``build_parser`` → ``parse_args`` →
    ``handler(args)`` with stdout captured.
  * **End-to-end over a real localhost socket** (reuses the ``_Harness`` pattern
    from ``tests/test_agent.py``): a real reactive backend (fake or real, with
    injected fakes) behind a real ``ReactiveServer`` + tick thread, driven by the
    CLI's own ``DeliberativeClient``. Cases: happy path, collision mid-drive,
    genuine-obstacle refusal, startup-stale-latch adoption, and timeout.

Plus a direct unit test of the halt-on-interrupt helper (no real SIGINT).

All runs are HEADLESS (synthetic camera, no GPIO, no network beyond loopback).
"""

from __future__ import annotations

import threading
import time
from io import StringIO
from types import SimpleNamespace
from typing import Optional, Tuple
from unittest.mock import patch

import pytest

from yalp.contract.ipc import ReactiveServer
from yalp.contract.messages import GoalStatus, Intent, Mode
from yalp.reactive.fake_backend import FakeReactiveBackend


# ===========================================================================
# CLI registration + plumbing (mirrors tests/test_bench_cli.py)
# ===========================================================================
def test_drive_in_feature_modules():
    from yalp.cli import FEATURE_MODULES

    assert "yalp.reactive.drive_cli" in FEATURE_MODULES


def test_build_parser_has_drive_subcommand():
    from yalp.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(
        [
            "drive",
            "--target", "1.6",
            "--speed", "0.3",
            "--host", "127.0.0.1",
            "--port", "8765",
            "--timeout", "45",
            "--poll-hz", "5",
        ]
    )
    assert args.target == 1.6
    assert args.speed == 0.3
    assert args.host == "127.0.0.1"
    assert args.port == 8765
    assert args.timeout == 45.0
    assert args.poll_hz == 5.0
    assert getattr(args, "handler", None) is not None


def test_drive_flag_defaults():
    from yalp import config
    from yalp.cli import build_parser

    args = build_parser().parse_args(["drive"])
    assert args.target == 1.6
    assert args.speed == 0.3
    assert args.host == config.IPC_HOST
    assert args.port == config.IPC_PORT
    assert args.timeout == 45.0
    assert args.poll_hz == 5.0


def _run_drive(*extra_args: str) -> Tuple[int, str]:
    """Run ``yalp drive <args>`` and return (exit_code, stdout).

    Mirrors ``tests/test_bench_cli.py::_run_bench`` — go all the way through
    ``build_parser`` so the registered handler is exercised end-to-end.
    """
    from yalp.cli import build_parser

    argv = ["drive", *extra_args]
    buf = StringIO()
    with patch("sys.stdout", buf):
        parser = build_parser()
        args = parser.parse_args(argv)
        handler = getattr(args, "handler", None)
        assert handler is not None, "no handler registered for 'drive'"
        rc = handler(args)
    return int(rc), buf.getvalue()


# ===========================================================================
# End-to-end harness (mirrors tests/test_agent.py::_Harness)
# ===========================================================================
class _Harness:
    """A real reactive backend + server, ticking on a background thread.

    Exposes ``.port`` for the CLI's own ``DeliberativeClient`` to dial. Unlike
    ``test_agent``'s harness this does not need its own client — the CLI builds
    one. Any backend with ``run(server, stop_event)`` and a ``.mailbox`` works
    (both the fake and the real backend qualify).
    """

    def __init__(self, backend=None, tick_hz: float = 50.0):
        self.backend = backend or FakeReactiveBackend(
            tick_hz=tick_hz, camera_source="synthetic"
        )
        self.server = ReactiveServer(
            host="127.0.0.1", port=0, mailbox=self.backend.mailbox
        )
        self.server.start()
        self.stop = threading.Event()
        self.runner = threading.Thread(
            target=self.backend.run,
            kwargs={"server": self.server, "stop_event": self.stop},
            daemon=True,
        )
        self.runner.start()

    @property
    def port(self) -> int:
        return self.server.port

    def wait_first_tick(self, timeout: float = 2.0) -> None:
        """Block until the backend has published at least one snapshot."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.backend.get_state().ts > 0:
                return
            time.sleep(0.005)

    def close(self):
        self.stop.set()
        self.runner.join(timeout=2.0)
        self.server.stop()


def _drive_args(port: int, **overrides) -> SimpleNamespace:
    """Build a parsed-args namespace pointed at a harness server on ``port``."""
    base = dict(
        target=0.2,
        speed=1.0,
        host="127.0.0.1",
        port=port,
        timeout=8.0,
        poll_hz=50.0,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _run_handler_capture(args) -> Tuple[int, str]:
    """Invoke the drive handler directly with stdout captured."""
    from yalp.reactive.drive_cli import run

    buf = StringIO()
    with patch("sys.stdout", buf):
        rc = run(args)
    return int(rc), buf.getvalue()


# ---------------------------------------------------------------------------
# (a) happy path — drive completes, exit 0, timeline printed
# ---------------------------------------------------------------------------
def test_happy_path_drive_completes_exit_zero():
    h = _Harness()
    try:
        h.wait_first_tick()
        # Small target at full speed so the timed open-loop drive finishes fast.
        rc, out = _run_handler_capture(_drive_args(h.port, target=0.2, speed=1.0))
    finally:
        h.close()

    assert rc == 0, out
    # A live timeline was printed (per-poll rows) ...
    assert "[" in out and "mode=" in out and "status=" in out
    assert "DRIVE_GOAL" in out
    # ... and the completion verdict fired.
    assert "COMPLETE" in out
    assert "completed (timed, unverified)" in out
    # Ultrasonic counters surfaced at start and end.
    assert "ultrasonic (start)" in out
    assert "ultrasonic (end)" in out


# ---------------------------------------------------------------------------
# (b) collision mid-drive -> SAFE_STOP verdict, exit 0
# ---------------------------------------------------------------------------
def test_collision_mid_drive_safe_stop_exit_zero():
    # A longer drive so it is still RUNNING when we trigger the collision.
    h = _Harness()
    try:
        h.wait_first_tick()
        result: dict = {}

        def _go():
            result["rc"], result["out"] = _run_handler_capture(
                _drive_args(h.port, target=2.0, speed=1.0, timeout=8.0)
            )

        t = threading.Thread(target=_go, daemon=True)
        t.start()
        # Let the drive be adopted and start RUNNING, then slam an obstacle in.
        time.sleep(0.3)
        h.backend.trigger_collision(0.10)
        t.join(timeout=6.0)
        assert not t.is_alive(), "drive handler did not terminate after collision"
    finally:
        h.close()

    rc, out = result["rc"], result["out"]
    assert rc == 0, out
    assert "SAFE_STOP" in out
    assert "reflex" in out.lower()
    # The served (close) distance is reported in the stop verdict.
    assert "0.10 m" in out


# ---------------------------------------------------------------------------
# (c) genuine-obstacle refusal -> exit 2, no intent sent
# ---------------------------------------------------------------------------
def test_genuine_obstacle_refusal_exit_two_no_intent():
    h = _Harness()
    try:
        # Latch a genuine close obstacle BEFORE the CLI connects.
        h.backend.trigger_collision(0.10)
        h.wait_first_tick()
        # Wait until the published state is actually SAFE_STOP/obstacle.
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            st = h.backend.get_state()
            if st.blocked and (st.goal or {}).get("reason") == "obstacle":
                break
            time.sleep(0.01)

        rc, out = _run_handler_capture(_drive_args(h.port, target=1.6))
        # No intent must have reached the mailbox (a blocked stack never drains it,
        # so a sent intent would still be peekable).
        assert h.backend.mailbox.peek() is None, "an intent was sent despite refusal"
    finally:
        h.close()

    assert rc == 2, out
    assert "REFUSED" in out
    assert "GENUINE obstacle" in out


# ---------------------------------------------------------------------------
# (d) startup-stale-latch -> proceeds and adopts, exit 0
#     (RealReactiveBackend with injected fakes; FakeRangeSensor.timeout() then
#      set_distance() to clear — mirrors tests/test_real_backend.py::_make_backend)
# ---------------------------------------------------------------------------
def _make_real_backend(sensor, *, tick_hz: float = 50.0):
    from yalp.camera import Camera
    from yalp.reactive.hardware import FakeMotorDriver
    from yalp.reactive.real_backend import RealReactiveBackend

    return RealReactiveBackend(
        motor_driver=FakeMotorDriver(),
        range_sensor=sensor,
        camera=Camera(source="synthetic"),
        max_speed_mps=1.0,
        tick_hz=tick_hz,
    )


def test_startup_stale_latch_proceeds_and_adopts_exit_zero():
    from yalp.reactive.hardware import FakeRangeSensor

    sensor = FakeRangeSensor()
    sensor.timeout()  # first reads are echo-timeouts -> stale startup SAFE_STOP latch
    backend = _make_real_backend(sensor)
    h = _Harness(backend=backend)
    try:
        h.wait_first_tick()
        # Confirm the stale latch really is published (echo_timeout, unknown).
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            st = backend.get_state()
            if st.blocked and (st.goal or {}).get("reason") == "echo_timeout":
                break
            time.sleep(0.01)
        assert not backend.get_state().distance_known

        result: dict = {}

        def _go():
            result["rc"], result["out"] = _run_handler_capture(
                _drive_args(h.port, target=0.2, speed=1.0, timeout=8.0)
            )

        t = threading.Thread(target=_go, daemon=True)
        t.start()
        # The CLI pre-flights the stale latch and SENDS the drive intent, but the
        # sticky SAFE_STOP holds it pending until the echo comes back. Clear it.
        time.sleep(0.3)
        sensor.set_distance(4.0, known=True)
        t.join(timeout=6.0)
        assert not t.is_alive(), "drive handler did not terminate after latch cleared"
    finally:
        h.close()

    rc, out = result["rc"], result["out"]
    assert rc == 0, out
    # Pre-flight recognized the stale latch and proceeded, then the drive adopted
    # and completed.
    assert "stale startup latch" in out
    assert "COMPLETE" in out
    # Real backend has a counter-bearing sensor, so the map is populated.
    assert "total_reads=" in out


# ---------------------------------------------------------------------------
# (e) timeout path -> sends IDLE and exits 1
# ---------------------------------------------------------------------------
def test_timeout_sends_idle_and_exits_one():
    h = _Harness()
    try:
        h.wait_first_tick()
        # A far target at a crawl so it cannot finish within the short timeout.
        rc, out = _run_handler_capture(
            _drive_args(h.port, target=100.0, speed=0.1, timeout=0.6, poll_hz=50.0)
        )
        assert rc == 1, out
        assert "TIMEOUT" in out
        assert "IDLE" in out
        # The IDLE intent actually reached the backend and halted it: the reactive
        # layer preempts the RUNNING drive then settles into IDLE.
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if h.backend.get_state().mode == Mode.IDLE:
                break
            time.sleep(0.02)
        assert h.backend.get_state().mode == Mode.IDLE
    finally:
        h.close()


# ---------------------------------------------------------------------------
# Reverse (negative target) prints a loud warning but still drives.
# ---------------------------------------------------------------------------
def test_negative_target_warns_and_drives():
    h = _Harness()
    try:
        h.wait_first_tick()
        rc, out = _run_handler_capture(_drive_args(h.port, target=-0.2, speed=1.0))
    finally:
        h.close()

    assert rc == 0, out
    assert "REVERSE DRIVE" in out
    assert "no rear" in out.lower()


# ===========================================================================
# Unit test: the halt-on-interrupt helper (no real SIGINT)
# ===========================================================================
class _RecordingClient:
    """Captures every intent sent through ``send_intent``."""

    def __init__(self, raise_on_send: bool = False):
        self.sent: list = []
        self._raise = raise_on_send

    def send_intent(self, intent):
        if self._raise:
            raise OSError("socket is down")
        self.sent.append(intent)


def test_send_idle_halt_emits_idle_intent():
    from yalp.reactive.drive_cli import send_idle_halt

    client = _RecordingClient()
    send_idle_halt(client, seq=1234)

    assert len(client.sent) == 1
    intent = client.sent[0]
    assert isinstance(intent, Intent)
    assert intent.mode == Mode.IDLE
    assert intent.goal is None
    assert intent.seq == 1234


def test_send_idle_halt_swallows_send_errors():
    from yalp.reactive.drive_cli import send_idle_halt

    # A broken socket during a halt must not raise (teardown path must be robust).
    send_idle_halt(_RecordingClient(raise_on_send=True), seq=1)


# ===========================================================================
# Unit test: the pure pre-flight decision helper
# ===========================================================================
def test_preflight_decision_matrix():
    from yalp import config
    from yalp.contract.messages import RobotState
    from yalp.reactive.drive_cli import preflight_decision

    t = float(config.SAFE_STOP_THRESHOLD_M)

    # Clear path -> proceed.
    assert preflight_decision(RobotState(), t)[0] == "proceed"

    # No snapshot -> proceed (cannot gate; assume clear).
    assert preflight_decision(None, t)[0] == "proceed"

    # Genuine close obstacle -> refuse.
    genuine = RobotState(
        mode=Mode.SAFE_STOP,
        goal_status=GoalStatus.BLOCKED,
        goal={"reason": "obstacle", "distance": 0.10},
        distance_m=0.10,
        distance_known=True,
        obstacle=True,
    )
    assert preflight_decision(genuine, t)[0] == "refuse"

    # Stale startup latches (echo_timeout / startup_blind, distance unknown) -> proceed.
    for reason in ("echo_timeout", "startup_blind"):
        stale = RobotState(
            mode=Mode.SAFE_STOP,
            goal_status=GoalStatus.BLOCKED,
            goal={"reason": reason, "distance": 4.0},
            distance_m=4.0,
            distance_known=False,
            obstacle=True,
        )
        assert preflight_decision(stale, t)[0] == "proceed"
