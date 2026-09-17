"""Tests for ``yalp session`` — the reactive-server session manager.

Everything is hermetic and fast: every side-effecting seam (commands, spawn,
connect, request_state, pid checks, signals, sleep, clock, systemd detection)
is a fake injected through ``SessionDeps``. No server is started, no real
sleep happens (a fake clock advances on ``sleep``), no signal is sent. The one
subprocess test proves ``python -m yalp`` is a working entrypoint.
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

import pytest

from yalp.contract.messages import Mode, RobotState
from yalp.reactive import session_cli
from yalp.reactive.session_cli import (
    EXIT_FAIL,
    EXIT_OK,
    EXIT_PORT_BUSY,
    SessionDeps,
    build_reactive_argv,
    detect_systemd_unit,
    format_state_line,
    tail_lines,
)

REPO_ROOT = Path(__file__).parent.parent
UNIT_FILE = REPO_ROOT / "scripts" / "yalp-reactive.service"
INSTALL_SH = REPO_ROOT / "scripts" / "install_reactive_service.sh"


# ===========================================================================
# Fakes
# ===========================================================================
class FakeClock:
    """``monotonic``/``sleep`` pair: time only moves when something sleeps."""

    def __init__(self, start: float = 1000.0) -> None:
        self.t = start
        self.sleeps: List[float] = []

    def monotonic(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


@dataclass
class World:
    """Mutable state the fakes read/write, so a test can script a scenario."""

    listening: bool = False
    state: Optional[RobotState] = None
    alive_pids: set = field(default_factory=set)
    systemd: Optional[Tuple[str, ...]] = None
    cmd_rc: int = 0
    cmd_out: dict = field(default_factory=dict)  # substring -> stdout
    spawn_pid: int = 4242
    spawn_starts_listening: bool = True
    # recorders
    out: List[str] = field(default_factory=list)
    cmds: List[Tuple[List[str], bool]] = field(default_factory=list)
    spawned: List[Tuple[List[str], Optional[str]]] = field(default_factory=list)
    signals: List[Tuple[int, int]] = field(default_factory=list)
    state_requests: int = 0

    def text(self) -> str:
        return "\n".join(self.out)


def make_deps(tmp_path, world: World, clock: Optional[FakeClock] = None, **overrides) -> SessionDeps:
    clock = clock or FakeClock()

    def run_cmd(argv, *, capture=False):
        world.cmds.append((list(argv), capture))
        joined = " ".join(argv)
        for needle, text in world.cmd_out.items():
            if needle in joined:
                return world.cmd_rc, text
        return world.cmd_rc, ""

    def spawn(argv, log_path):
        world.spawned.append((list(argv), log_path))
        world.alive_pids.add(world.spawn_pid)
        if world.spawn_starts_listening:
            world.listening = True
        return world.spawn_pid

    def connect(host, port):
        return world.listening

    def request_state(host, port, timeout):
        world.state_requests += 1
        return world.state

    def pid_alive(pid):
        return pid in world.alive_pids

    def send_signal(pid, signum):
        world.signals.append((pid, signum))
        return pid in world.alive_pids

    kwargs = dict(
        host="127.0.0.1",
        port=8765,
        state_dir=str(tmp_path / "state"),
        python="/venv/bin/python",
        run_cmd=run_cmd,
        spawn=spawn,
        connect=connect,
        request_state=request_state,
        pid_alive=pid_alive,
        send_signal=send_signal,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        detect_systemd=lambda: world.systemd,
        out=world.out.append,
    )
    kwargs.update(overrides)
    return SessionDeps(**kwargs)


def warm_state(**kw) -> RobotState:
    return RobotState(last_frame_id="f-12", distance_m=0.83, **kw)


def cold_state(**kw) -> RobotState:
    return RobotState(last_frame_id=None, **kw)


# ===========================================================================
# argparse wiring (add_parser on a fresh parser)
# ===========================================================================
def _parse(*argv: str):
    parser = argparse.ArgumentParser(prog="yalp")
    sub = parser.add_subparsers(dest="command")
    session_cli.add_parser(sub)
    return parser.parse_args(["session", *argv])


class TestAddParser:
    def test_handler_is_run(self):
        args = _parse("status")
        assert args.command == "session"
        assert args.action == "status"
        assert getattr(args, "handler", None) is session_cli.run

    def test_start_flags(self):
        args = _parse(
            "start", "--backend", "fake", "--hz", "10", "--camera-source", "synthetic",
            "--detector", "person", "--foreground", "--no-wait", "--timeout", "12",
            "--no-camera", "--port", "9000",
        )
        assert args.action == "start"
        assert args.backend == "fake"
        assert args.hz == 10.0
        assert args.camera_source == "synthetic"
        assert args.detector == "person"
        assert args.foreground is True
        assert args.wait is False
        assert args.timeout == 12.0
        assert args.camera is False
        assert args.port == 9000

    def test_start_defaults(self):
        from yalp import config

        args = _parse("start")
        assert args.backend == "real"
        assert args.hz is None
        assert args.camera_source is None
        assert args.detector is None
        assert args.foreground is False
        assert args.wait is True
        assert args.timeout == 30.0
        assert args.camera is None  # auto: on for real, off for fake
        assert args.host == config.IPC_HOST
        assert args.port == config.IPC_PORT

    def test_wait_flags(self):
        args = _parse("wait", "--timeout", "5", "--no-camera")
        assert args.action == "wait"
        assert args.timeout == 5.0
        assert args.camera is False
        assert _parse("wait").camera is True
        assert _parse("wait").timeout == 30.0

    def test_stop_restart_logs_flags(self):
        assert _parse("stop", "--timeout", "3").timeout == 3.0
        assert _parse("stop").timeout == 10.0
        r = _parse("restart", "--backend", "fake", "--stop-timeout", "4")
        assert r.action == "restart" and r.backend == "fake" and r.stop_timeout == 4.0
        lg = _parse("logs", "-n", "10", "-f")
        assert lg.action == "logs" and lg.lines == 10 and lg.follow is True
        assert _parse("logs").lines == 50 and _parse("logs").follow is False

    def test_action_is_required(self):
        with pytest.raises(SystemExit):
            _parse()

    def test_bad_backend_rejected(self):
        with pytest.raises(SystemExit):
            _parse("start", "--backend", "sim")


# ===========================================================================
# Pure helpers
# ===========================================================================
class TestHelpers:
    def test_build_reactive_argv_full(self):
        argv = build_reactive_argv(
            "/py", backend="real", host="127.0.0.1", port=8765, hz=10,
            camera_source="webcam", detector="person",
        )
        assert argv == [
            "/py", "-u", "-m", "yalp", "reactive", "--backend", "real",
            "--host", "127.0.0.1", "--port", "8765", "--hz", "10",
            "--camera-source", "webcam", "--detector", "person",
        ]

    def test_build_reactive_argv_minimal(self):
        assert build_reactive_argv("/py", backend="fake") == [
            "/py", "-u", "-m", "yalp", "reactive", "--backend", "fake",
        ]

    def test_default_state_dir_honors_xdg(self, monkeypatch, tmp_path):
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        assert session_cli.default_state_dir() == str(tmp_path / "yalp")
        monkeypatch.delenv("XDG_STATE_HOME")
        assert session_cli.default_state_dir() == os.path.expanduser("~/.local/state/yalp")

    def test_deps_paths(self, tmp_path):
        deps = SessionDeps(host="h", port=1, state_dir=str(tmp_path))
        assert deps.log_path == str(tmp_path / "reactive.log")
        assert deps.pid_path == str(tmp_path / "reactive.pid")
        assert deps.endpoint == "h:1"

    def test_format_state_line(self):
        s = warm_state(mode=Mode.DRIVE_GOAL, goal_status="running",
                       goal={"heading_deg": -3.25, "heading_live": False})
        line = format_state_line(s)
        assert "mode=DRIVE_GOAL" in line
        assert "goal_status=running" in line
        assert "distance=0.83 m" in line
        assert "heading=-3.2 deg (blind)" in line
        assert "camera=warm (f-12)" in line
        cold = format_state_line(cold_state(distance_known=False))
        assert "camera=cold" in cold and "UNKNOWN" in cold

    def test_tail_lines(self, tmp_path):
        p = tmp_path / "log"
        p.write_text("".join(f"l{i}\n" for i in range(1, 101)))
        assert tail_lines(str(p), 3) == ["l98", "l99", "l100"]
        assert tail_lines(str(p), 500)[0] == "l1"
        assert tail_lines(str(p), 0) == []
        # small block size forces the reverse-read loop to iterate
        assert tail_lines(str(p), 5, block=7) == ["l96", "l97", "l98", "l99", "l100"]

    def test_pidfile_roundtrip(self, tmp_path):
        path = str(tmp_path / "d" / "reactive.pid")
        assert session_cli.read_pidfile(path) is None
        session_cli.write_pidfile(path, 123)
        assert session_cli.read_pidfile(path) == 123
        session_cli.remove_pidfile(path)
        assert session_cli.read_pidfile(path) is None
        session_cli.remove_pidfile(path)  # idempotent
        (tmp_path / "junk").write_text("not a pid")
        assert session_cli.read_pidfile(str(tmp_path / "junk")) is None


# ===========================================================================
# systemd detection (the one injectable probe)
# ===========================================================================
class TestDetectSystemd:
    def _run_cmd_factory(self, ok: set):
        calls = []

        def run_cmd(argv, *, capture=False):
            calls.append((list(argv), capture))
            return (0, "") if tuple(argv[:-2]) in ok else (1, "")

        return run_cmd, calls

    def test_user_unit_wins(self):
        run_cmd, calls = self._run_cmd_factory({("systemctl", "--user"), ("systemctl",)})
        assert detect_systemd_unit(run_cmd) == ("systemctl", "--user")
        assert calls == [(["systemctl", "--user", "cat", "yalp-reactive.service"], True)]

    def test_falls_back_to_system_unit(self):
        run_cmd, calls = self._run_cmd_factory({("systemctl",)})
        assert detect_systemd_unit(run_cmd) == ("systemctl",)
        assert [c[0][:2] for c in calls] == [["systemctl", "--user"], ["systemctl", "cat"]]

    def test_none_when_absent(self):
        run_cmd, _ = self._run_cmd_factory(set())
        assert detect_systemd_unit(run_cmd) is None

    def test_missing_systemctl_is_not_installed(self):
        assert detect_systemd_unit(lambda argv, capture=False: (127, "")) is None

    def test_deps_caches_probe(self, tmp_path):
        world = World()
        calls = []

        def probe():
            calls.append(1)
            return ("systemctl", "--user")

        deps = make_deps(tmp_path, world, detect_systemd=probe)
        assert deps.systemd() == ("systemctl", "--user")
        assert deps.systemd() == ("systemctl", "--user")
        assert len(calls) == 1

    def test_deps_default_probe_uses_run_cmd(self, tmp_path):
        world = World(cmd_rc=0)
        deps = make_deps(tmp_path, world, detect_systemd=None)
        assert deps.systemd() == ("systemctl", "--user")
        assert world.cmds[0] == (["systemctl", "--user", "cat", "yalp-reactive.service"], True)


# ===========================================================================
# start
# ===========================================================================
class TestStart:
    def test_refuses_when_port_busy_exit_3(self, tmp_path):
        world = World(listening=True, alive_pids={999})
        deps = make_deps(tmp_path, world)
        session_cli.write_pidfile(deps.pid_path, 999)
        rc = session_cli.run(_parse("start"), deps=deps)
        assert rc == EXIT_PORT_BUSY == 3
        assert world.spawned == []
        assert "already listening" in world.text()
        assert "pid 999" in world.text()

    def test_refuses_when_port_busy_unknown_owner(self, tmp_path):
        world = World(listening=True)
        deps = make_deps(tmp_path, world)
        assert session_cli.run(_parse("start"), deps=deps) == 3
        assert "owner unknown" in world.text()

    def test_refuses_when_pidfile_alive_but_port_down(self, tmp_path):
        world = World(listening=False, alive_pids={55})
        deps = make_deps(tmp_path, world)
        session_cli.write_pidfile(deps.pid_path, 55)
        assert session_cli.run(_parse("start"), deps=deps) == 3
        assert "still be starting" in world.text()
        assert world.spawned == []

    def test_spawns_expected_argv_writes_pidfile_and_waits(self, tmp_path):
        world = World(state=warm_state())
        clock = FakeClock()
        deps = make_deps(tmp_path, world, clock)
        args = _parse("start", "--backend", "real", "--hz", "10",
                      "--camera-source", "webcam", "--detector", "person")
        rc = session_cli.run(args, deps=deps)
        assert rc == EXIT_OK
        assert world.spawned == [(
            ["/venv/bin/python", "-u", "-m", "yalp", "reactive", "--backend", "real",
             "--host", "127.0.0.1", "--port", "8765", "--hz", "10",
             "--camera-source", "webcam", "--detector", "person"],
            deps.log_path,
        )]
        assert session_cli.read_pidfile(deps.pid_path) == 4242
        assert "READY in" in world.text()
        assert "camera=warm" in world.text()
        assert clock.sleeps == []  # ready on the first poll: no sleeping

    def test_no_wait_returns_immediately(self, tmp_path):
        world = World(state=None)
        deps = make_deps(tmp_path, world)
        rc = session_cli.run(_parse("start", "--no-wait"), deps=deps)
        assert rc == EXIT_OK
        assert world.state_requests == 0
        assert session_cli.read_pidfile(deps.pid_path) == 4242

    def test_fake_backend_does_not_wait_for_camera(self, tmp_path):
        world = World(state=cold_state())
        deps = make_deps(tmp_path, world)
        rc = session_cli.run(_parse("start", "--backend", "fake"), deps=deps)
        assert rc == EXIT_OK
        assert "server answering" in world.text()

    def test_real_backend_waits_for_camera_and_times_out(self, tmp_path):
        world = World(state=cold_state())
        clock = FakeClock()
        deps = make_deps(tmp_path, world, clock)
        rc = session_cli.run(_parse("start", "--timeout", "3"), deps=deps)
        assert rc == EXIT_FAIL
        assert "camera never published a frame" in world.text()
        assert clock.t - 1000.0 == pytest.approx(3.0)

    def test_stale_pidfile_is_cleaned_before_start(self, tmp_path):
        world = World(state=warm_state())
        deps = make_deps(tmp_path, world)
        session_cli.write_pidfile(deps.pid_path, 31337)  # not alive
        rc = session_cli.run(_parse("start"), deps=deps)
        assert rc == EXIT_OK
        assert "stale pidfile" in world.text()
        assert session_cli.read_pidfile(deps.pid_path) == 4242

    def test_child_dying_fails_fast(self, tmp_path):
        world = World(state=None, spawn_starts_listening=False)
        clock = FakeClock()
        deps = make_deps(tmp_path, world, clock)

        # The child is alive for the first two aliveness checks, then gone.
        checks = {"n": 0}

        def pid_alive(pid):
            checks["n"] += 1
            return checks["n"] <= 2

        deps.pid_alive = pid_alive
        rc = session_cli.run(_parse("start", "--timeout", "30"), deps=deps)
        assert rc == EXIT_FAIL
        assert "exited before becoming ready" in world.text()
        assert clock.t - 1000.0 < 30.0  # did not burn the whole timeout

    def test_foreground_runs_attached_and_removes_pidfile_on_exit(self, tmp_path):
        world = World(state=warm_state())
        deps = make_deps(tmp_path, world)
        pidfile_seen = {"during": None}

        def pid_alive(pid):
            # First call (readiness) -> alive; record the pidfile exists; then exit.
            if pidfile_seen["during"] is None:
                pidfile_seen["during"] = session_cli.read_pidfile(deps.pid_path)
                return True
            return False

        deps.pid_alive = pid_alive
        rc = session_cli.run(_parse("start", "--foreground"), deps=deps)
        assert rc == EXIT_OK
        assert world.spawned[0][1] is None  # attached: no log file
        assert pidfile_seen["during"] == 4242
        assert session_cli.read_pidfile(deps.pid_path) is None
        assert "exited" in world.text()

    def test_foreground_ctrl_c_runs_graceful_stop(self, tmp_path):
        world = World(state=warm_state())
        deps = make_deps(tmp_path, world)

        def sleep(_s):
            raise KeyboardInterrupt

        deps.sleep = sleep

        def send_signal(pid, signum):
            world.signals.append((pid, signum))
            world.alive_pids.discard(pid)
            return True

        deps.send_signal = send_signal
        rc = session_cli.run(_parse("start", "--foreground"), deps=deps)
        assert rc == EXIT_OK
        assert world.signals == [(4242, signal.SIGINT)]
        assert session_cli.read_pidfile(deps.pid_path) is None
        assert "Ctrl-C" in world.text()

    def test_foreground_bypasses_systemd(self, tmp_path):
        world = World(state=warm_state(), systemd=("systemctl", "--user"))
        deps = make_deps(tmp_path, world)
        deps.pid_alive = lambda pid: False
        rc = session_cli.run(_parse("start", "--foreground"), deps=deps)
        assert rc == EXIT_OK
        assert world.cmds == []
        assert len(world.spawned) == 1


# ===========================================================================
# wait
# ===========================================================================
class TestWait:
    def test_ready_when_state_has_frame(self, tmp_path):
        world = World(listening=True, state=warm_state(mode=Mode.IDLE))
        clock = FakeClock()
        deps = make_deps(tmp_path, world, clock)
        rc = session_cli.run(_parse("wait"), deps=deps)
        assert rc == EXIT_OK
        text = world.text()
        assert "READY in 0.0 s" in text
        assert "mode=IDLE" in text and "distance=0.83 m" in text
        assert clock.sleeps == []

    def test_becomes_ready_after_some_polls(self, tmp_path):
        world = World(listening=False, state=None)
        clock = FakeClock()
        deps = make_deps(tmp_path, world, clock)

        def connect(host, port):
            # port opens after 1 s of fake time, frames after 2 s
            if clock.t - 1000.0 >= 2.0:
                world.state = warm_state()
            elif clock.t - 1000.0 >= 1.0:
                world.state = cold_state()
            return clock.t - 1000.0 >= 1.0

        deps.connect = connect
        rc = session_cli.run(_parse("wait", "--timeout", "30"), deps=deps)
        assert rc == EXIT_OK
        assert "READY in 2.0 s" in world.text()
        assert "port 127.0.0.1:8765 up after 1.0 s" in world.text()

    def test_timeout_returns_1_without_real_sleep(self, tmp_path):
        world = World(listening=False, state=None)
        clock = FakeClock()
        deps = make_deps(tmp_path, world, clock)
        rc = session_cli.run(_parse("wait", "--timeout", "30"), deps=deps)
        assert rc == EXIT_FAIL
        assert "NOT READY after 30.0 s" in world.text()
        assert "port never opened" in world.text()
        assert clock.t - 1000.0 == pytest.approx(30.0)
        assert all(s <= 0.25 for s in clock.sleeps)

    def test_no_camera_accepts_cold_state(self, tmp_path):
        world = World(listening=True, state=cold_state())
        deps = make_deps(tmp_path, world)
        assert session_cli.run(_parse("wait", "--no-camera"), deps=deps) == EXIT_OK
        assert session_cli.run(_parse("wait", "--timeout", "1"), deps=deps) == EXIT_FAIL

    def test_stale_pidfile_does_not_fail_wait(self, tmp_path):
        world = World(listening=True, state=warm_state())
        deps = make_deps(tmp_path, world)
        session_cli.write_pidfile(deps.pid_path, 777)  # not alive (e.g. systemd now runs it)
        assert session_cli.run(_parse("wait"), deps=deps) == EXIT_OK

    def test_live_pid_dying_fails_fast(self, tmp_path):
        world = World(listening=False, state=None, alive_pids={777})
        clock = FakeClock()
        deps = make_deps(tmp_path, world, clock)
        session_cli.write_pidfile(deps.pid_path, 777)

        def sleep(s):
            clock.sleep(s)
            world.alive_pids.discard(777)  # dies after the first poll

        deps.sleep = sleep
        rc = session_cli.run(_parse("wait", "--timeout", "30"), deps=deps)
        assert rc == EXIT_FAIL
        assert "process 777 exited" in world.text()
        assert clock.t - 1000.0 < 1.0


# ===========================================================================
# status
# ===========================================================================
class TestStatus:
    def test_running_exit_0(self, tmp_path):
        world = World(listening=True, alive_pids={4242},
                      state=warm_state(goal={"heading_deg": 1.5}, goal_status="running"))
        deps = make_deps(tmp_path, world)
        session_cli.write_pidfile(deps.pid_path, 4242)
        rc = session_cli.run(_parse("status"), deps=deps)
        assert rc == EXIT_OK
        text = world.text()
        assert "LISTENING" in text
        assert "pid 4242 (alive)" in text
        assert "heading=+1.5 deg" in text and "camera=warm" in text
        assert "goal_status=running" in text
        assert text.endswith("RUNNING")

    def test_not_running_exit_1(self, tmp_path):
        world = World(listening=False)
        deps = make_deps(tmp_path, world)
        rc = session_cli.run(_parse("status"), deps=deps)
        assert rc == EXIT_FAIL
        assert "not listening" in world.text()
        assert "pidfile  : none" in world.text()
        assert world.text().endswith("NOT RUNNING")

    def test_stale_pidfile_reported(self, tmp_path):
        world = World(listening=False)
        deps = make_deps(tmp_path, world)
        session_cli.write_pidfile(deps.pid_path, 1)
        session_cli.run(_parse("status"), deps=deps)
        assert "STALE" in world.text()

    def test_systemd_status_line(self, tmp_path):
        world = World(listening=True, state=warm_state(), systemd=("systemctl", "--user"),
                      cmd_out={"is-active": "active\n", "MainPID": "888\n"})
        deps = make_deps(tmp_path, world)
        assert session_cli.run(_parse("status"), deps=deps) == EXIT_OK
        assert "yalp-reactive.service is active" in world.text()
        assert "MainPID 888" in world.text()
        assert (["systemctl", "--user", "is-active", "yalp-reactive.service"], True) in world.cmds


# ===========================================================================
# stop
# ===========================================================================
class TestStop:
    def test_sigint_then_escalates_and_removes_pidfile(self, tmp_path):
        world = World(alive_pids={4242})
        clock = FakeClock()
        deps = make_deps(tmp_path, world, clock)
        session_cli.write_pidfile(deps.pid_path, 4242)

        # Ignores SIGINT; dies on SIGTERM.
        def send_signal(pid, signum):
            world.signals.append((pid, signum))
            if signum == signal.SIGTERM:
                world.alive_pids.discard(pid)
            return True

        deps.send_signal = send_signal
        rc = session_cli.run(_parse("stop", "--timeout", "10"), deps=deps)
        assert rc == EXIT_OK
        assert [s for _, s in world.signals] == [signal.SIGINT, signal.SIGTERM]
        assert clock.t - 1000.0 == pytest.approx(10.0)  # waited the full grace, no more
        assert session_cli.read_pidfile(deps.pid_path) is None
        text = world.text()
        assert "zeroes the motors first" in text
        assert "pid 4242 terminated" in text

    def test_graceful_sigint_only(self, tmp_path):
        world = World(alive_pids={4242})
        clock = FakeClock()
        deps = make_deps(tmp_path, world, clock)
        session_cli.write_pidfile(deps.pid_path, 4242)

        def send_signal(pid, signum):
            world.signals.append((pid, signum))
            world.alive_pids.discard(pid)
            return True

        deps.send_signal = send_signal
        assert session_cli.run(_parse("stop"), deps=deps) == EXIT_OK
        assert [s for _, s in world.signals] == [signal.SIGINT]
        assert clock.sleeps == []
        assert "pid 4242 stopped" in world.text()
        assert not os.path.exists(deps.pid_path)

    def test_sigkill_last_resort_is_loud(self, tmp_path):
        world = World(alive_pids={4242})
        clock = FakeClock()
        deps = make_deps(tmp_path, world, clock)
        session_cli.write_pidfile(deps.pid_path, 4242)

        def send_signal(pid, signum):
            world.signals.append((pid, signum))
            if signum == signal.SIGKILL:
                world.alive_pids.discard(pid)
            return True

        deps.send_signal = send_signal
        assert session_cli.run(_parse("stop", "--timeout", "2"), deps=deps) == EXIT_OK
        assert [s for _, s in world.signals] == [signal.SIGINT, signal.SIGTERM, signal.SIGKILL]
        text = world.text()
        assert "!!!" in text and "LAST RESORT" in text and "CHECK THE WHEELS" in text
        assert "pid 4242 killed" in text
        assert session_cli.read_pidfile(deps.pid_path) is None

    def test_not_running_is_exit_0_and_cleans_pidfile(self, tmp_path):
        world = World()
        deps = make_deps(tmp_path, world)
        session_cli.write_pidfile(deps.pid_path, 31337)
        assert session_cli.run(_parse("stop"), deps=deps) == EXIT_OK
        assert world.signals == []
        assert "was not running" in world.text()
        assert session_cli.read_pidfile(deps.pid_path) is None

    def test_nothing_to_stop(self, tmp_path):
        world = World()
        deps = make_deps(tmp_path, world)
        assert session_cli.run(_parse("stop"), deps=deps) == EXIT_OK
        assert "nothing to stop" in world.text()

    def test_foreign_server_still_listening_is_exit_1(self, tmp_path):
        world = World(listening=True)
        deps = make_deps(tmp_path, world)
        assert session_cli.run(_parse("stop"), deps=deps) == EXIT_FAIL
        assert "STILL listening" in world.text()

    def test_stop_pid_helper_statuses(self, tmp_path):
        world = World(alive_pids=set())
        deps = make_deps(tmp_path, world)
        assert session_cli.stop_pid(deps, 5) == "not-running"
        world.alive_pids = {5}
        deps.send_signal = lambda pid, signum: False  # vanished between check and kill
        assert session_cli.stop_pid(deps, 5) == "stopped"


# ===========================================================================
# systemd dispatch
# ===========================================================================
class TestSystemdDispatch:
    def test_start_uses_systemctl_user_and_waits(self, tmp_path):
        world = World(state=warm_state(), systemd=("systemctl", "--user"),
                      cmd_out={"MainPID": "888\n"}, alive_pids={888})
        deps = make_deps(tmp_path, world)
        # The unit brings the port up.
        deps.connect = lambda h, p: any("start" in c[0] for c in world.cmds)
        rc = session_cli.run(_parse("start"), deps=deps)
        assert rc == EXIT_OK
        assert world.cmds[0] == (["systemctl", "--user", "start", "yalp-reactive.service"], False)
        assert (["systemctl", "--user", "show", "-p", "MainPID", "--value", "yalp-reactive.service"], True) in world.cmds
        assert world.spawned == []
        assert not os.path.exists(deps.pid_path)
        assert "READY in" in world.text()

    def test_start_under_systemd_notes_ignored_flags(self, tmp_path):
        world = World(state=warm_state(), systemd=("systemctl", "--user"))
        deps = make_deps(tmp_path, world)
        deps.connect = lambda h, p: any("start" in c[0] for c in world.cmds)
        assert session_cli.run(_parse("start", "--backend", "fake"), deps=deps) == EXIT_OK
        assert "are ignored" in world.text()

    def test_start_systemctl_failure(self, tmp_path):
        world = World(systemd=("systemctl", "--user"), cmd_rc=5)
        deps = make_deps(tmp_path, world)
        assert session_cli.run(_parse("start"), deps=deps) == EXIT_FAIL
        assert "failed (exit 5)" in world.text()

    def test_system_unit_without_user_flag(self, tmp_path):
        world = World(state=warm_state(), systemd=("systemctl",))
        deps = make_deps(tmp_path, world)
        deps.connect = lambda h, p: any("start" in c[0] for c in world.cmds)
        assert session_cli.run(_parse("start"), deps=deps) == EXIT_OK
        assert world.cmds[0] == (["systemctl", "start", "yalp-reactive.service"], False)

    def test_stop_uses_systemctl_user(self, tmp_path):
        world = World(systemd=("systemctl", "--user"))
        deps = make_deps(tmp_path, world)
        assert session_cli.run(_parse("stop"), deps=deps) == EXIT_OK
        assert world.cmds == [(["systemctl", "--user", "stop", "yalp-reactive.service"], False)]
        assert "KillSignal=SIGINT" in world.text()

    def test_stop_under_systemd_also_stops_pidfile_child(self, tmp_path):
        world = World(systemd=("systemctl", "--user"), alive_pids={4242})
        deps = make_deps(tmp_path, world)
        session_cli.write_pidfile(deps.pid_path, 4242)

        def send_signal(pid, signum):
            world.signals.append((pid, signum))
            world.alive_pids.discard(pid)
            return True

        deps.send_signal = send_signal
        assert session_cli.run(_parse("stop"), deps=deps) == EXIT_OK
        assert world.signals == [(4242, signal.SIGINT)]
        assert not os.path.exists(deps.pid_path)

    def test_logs_uses_journalctl_user(self, tmp_path):
        world = World(systemd=("systemctl", "--user"))
        deps = make_deps(tmp_path, world)
        assert session_cli.run(_parse("logs", "-n", "20"), deps=deps) == EXIT_OK
        assert world.cmds == [(["journalctl", "--user", "-u", "yalp-reactive", "-n", "20"], False)]
        world.cmds.clear()
        session_cli.run(_parse("logs", "-f"), deps=deps)
        assert world.cmds == [(["journalctl", "--user", "-u", "yalp-reactive", "-n", "50", "-f"], False)]

    def test_logs_system_journal(self, tmp_path):
        world = World(systemd=("systemctl",))
        deps = make_deps(tmp_path, world)
        session_cli.run(_parse("logs"), deps=deps)
        assert world.cmds == [(["journalctl", "-u", "yalp-reactive", "-n", "50"], False)]

    def test_restart_is_stop_then_start(self, tmp_path):
        world = World(state=warm_state(), systemd=("systemctl", "--user"))
        deps = make_deps(tmp_path, world)
        deps.connect = lambda h, p: any("start" in c[0] for c in world.cmds)
        assert session_cli.run(_parse("restart"), deps=deps) == EXIT_OK
        verbs = [c[0][2] for c in world.cmds if c[0][:2] == ["systemctl", "--user"]]
        assert verbs[:2] == ["stop", "start"]


# ===========================================================================
# restart (detached mode)
# ===========================================================================
class TestRestartDetached:
    def test_stop_then_spawn(self, tmp_path):
        world = World(state=warm_state(), alive_pids={100}, listening=True)
        deps = make_deps(tmp_path, world)
        session_cli.write_pidfile(deps.pid_path, 100)

        def send_signal(pid, signum):
            world.signals.append((pid, signum))
            world.alive_pids.discard(pid)
            world.listening = False
            return True

        deps.send_signal = send_signal
        rc = session_cli.run(_parse("restart", "--backend", "fake", "--no-camera"), deps=deps)
        assert rc == EXIT_OK
        assert world.signals == [(100, signal.SIGINT)]
        assert len(world.spawned) == 1
        assert "--backend" in world.spawned[0][0] and "fake" in world.spawned[0][0]
        assert session_cli.read_pidfile(deps.pid_path) == 4242


# ===========================================================================
# logs (file mode)
# ===========================================================================
class TestLogs:
    def test_tails_last_n_lines(self, tmp_path):
        world = World()
        deps = make_deps(tmp_path, world)
        os.makedirs(deps.state_dir)
        Path(deps.log_path).write_text("".join(f"line {i}\n" for i in range(1, 101)))
        rc = session_cli.run(_parse("logs", "-n", "3"), deps=deps)
        assert rc == EXIT_OK
        assert world.out == ["line 98", "line 99", "line 100"]

    def test_missing_log_is_exit_1(self, tmp_path):
        world = World()
        deps = make_deps(tmp_path, world)
        assert session_cli.run(_parse("logs"), deps=deps) == EXIT_FAIL
        assert "no log at" in world.text()

    def test_follow_prints_appended_lines_until_interrupted(self, tmp_path):
        world = World()
        deps = make_deps(tmp_path, world)
        os.makedirs(deps.state_dir)
        log = Path(deps.log_path)
        log.write_text("old 1\nold 2\n")
        polls = {"n": 0}

        def sleep(_s):
            polls["n"] += 1
            if polls["n"] == 1:
                with log.open("a") as fh:
                    fh.write("new 1\npartial")
            elif polls["n"] == 2:
                with log.open("a") as fh:
                    fh.write(" line\n")
            else:
                raise KeyboardInterrupt

        deps.sleep = sleep
        rc = session_cli.run(_parse("logs", "-n", "1", "-f"), deps=deps)
        assert rc == EXIT_OK
        assert world.out == ["old 2", "new 1", "partial line"]


# ===========================================================================
# python -m yalp entrypoint + the shipped scripts
# ===========================================================================
def test_python_m_yalp_is_an_entrypoint():
    from tests._import_isolation import _child_env

    proc = subprocess.run(
        [sys.executable, "-m", "yalp", "--version"],
        env=_child_env(), capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().startswith("yalp ")


def test_importing_main_module_does_not_run_cli():
    import yalp.__main__  # noqa: F401 — guarded by __name__, must not SystemExit


class TestUnitFile:
    def test_exists(self):
        assert UNIT_FILE.is_file()

    def test_required_directives(self):
        text = UNIT_FILE.read_text()
        for needle in (
            "[Unit]", "[Service]", "[Install]",
            "ExecStart=%h/yalp/.venv/bin/python -u -m yalp reactive --backend real",
            "WorkingDirectory=%h/yalp",
            "EnvironmentFile=-%h/.config/yalp/reactive.env",
            "Restart=on-failure", "RestartSec=2", "KillSignal=SIGINT",
            "TimeoutStopSec=15", "WantedBy=default.target",
        ):
            assert needle in text, needle

    def test_comment_header_explains_sigint_and_linger(self):
        text = UNIT_FILE.read_text()
        assert "loginctl enable-linger izzy" in text
        assert "SIGINT" in text and "motors" in text.lower()


class TestInstallScript:
    def test_exists_and_executable(self):
        assert INSTALL_SH.is_file()
        assert os.access(INSTALL_SH, os.X_OK)

    def test_bash_syntax(self):
        result = subprocess.run(["bash", "-n", str(INSTALL_SH)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr

    def test_contents(self):
        text = INSTALL_SH.read_text()
        assert text.startswith("#!/usr/bin/env bash")
        assert "set -euo pipefail" in text
        assert ".config/systemd/user" in text
        assert ".config/yalp/reactive.env" in text
        assert "YALP_ULTRASONIC_BACKEND=gpiod" in text
        assert "#YALP_HEADING_HOLD_GAIN=" in text
        assert "systemctl --user daemon-reload" in text
        assert "systemctl --user enable" in text
        assert "loginctl enable-linger" in text
        assert "yalp session start" in text
