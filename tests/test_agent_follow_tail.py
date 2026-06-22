"""`yalp agent` stay-and-follow tail: entered iff the run ends in FOLLOW mode.

No camera/mic/GUI. We monkeypatch the shared ``run_follow_loop`` and assert the
agent CLI enters it with the EXISTING backend and ``owns_ticking=False`` (so it
monitors the already-ticking backend without double-ticking) — and ONLY when the
final RobotState mode is FOLLOW (not IDLE / DRIVE_GOAL / etc.).
"""

from __future__ import annotations

import argparse

import yalp.reactive.follow_runner as follow_runner
from yalp.contract.messages import GoalStatus, Mode, RobotState
from yalp.deliberative import agent_cli


def _agent_parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser()
    sub = root.add_subparsers(dest="subcommand")
    agent_cli.add_parser(sub)
    return root


class _FakeBackend:
    """A backend stand-in that publishes a fixed mode for the tail to inspect."""

    def __init__(self, mode: Mode) -> None:
        self._mode = mode

    def get_state(self) -> RobotState:
        return RobotState(mode=self._mode, goal_status=GoalStatus.RUNNING)


def test_follow_tail_entered_when_run_ends_in_follow(monkeypatch):
    calls = {}

    def _spy(backend, **kwargs):
        calls["backend"] = backend
        calls["kwargs"] = kwargs
        return 0

    monkeypatch.setattr(follow_runner, "run_follow_loop", _spy)

    backend = _FakeBackend(Mode.FOLLOW)
    args = _agent_parser().parse_args(["agent", "--no-preview", "follow", "me"])
    agent_cli._maybe_follow_tail(backend, args)

    assert calls.get("backend") is backend  # reuse the EXISTING backend
    assert calls["kwargs"]["owns_ticking"] is False  # never double-tick
    assert calls["kwargs"]["preview"] is False  # --no-preview honored


def test_follow_tail_not_entered_when_run_ends_idle(monkeypatch):
    calls = {"count": 0}

    def _spy(backend, **kwargs):
        calls["count"] += 1
        return 0

    monkeypatch.setattr(follow_runner, "run_follow_loop", _spy)

    backend = _FakeBackend(Mode.IDLE)
    args = _agent_parser().parse_args(["agent", "--no-preview", "look around"])
    agent_cli._maybe_follow_tail(backend, args)

    assert calls["count"] == 0  # IDLE run -> no follow tail


def test_follow_tail_not_entered_for_drive_goal(monkeypatch):
    calls = {"count": 0}
    monkeypatch.setattr(
        follow_runner, "run_follow_loop", lambda backend, **kw: calls.__setitem__("count", calls["count"] + 1) or 0
    )
    backend = _FakeBackend(Mode.DRIVE_GOAL)
    args = _agent_parser().parse_args(["agent", "drive forward"])
    agent_cli._maybe_follow_tail(backend, args)
    assert calls["count"] == 0


def test_follow_tail_passes_follow_seconds(monkeypatch):
    calls = {}
    monkeypatch.setattr(
        follow_runner,
        "run_follow_loop",
        lambda backend, **kw: (calls.update(kw), 0)[1],
    )
    backend = _FakeBackend(Mode.FOLLOW)
    args = _agent_parser().parse_args(
        ["agent", "--no-preview", "--follow-seconds", "5", "follow me"]
    )
    agent_cli._maybe_follow_tail(backend, args)
    assert calls["seconds"] == 5.0


# --- flag parsing ----------------------------------------------------------- #
def test_preview_defaults_to_auto():
    args = _agent_parser().parse_args(["agent", "follow me"])
    assert args.preview is None  # AUTO (resolved at runtime via TTY + GUI probe)


def test_no_preview_forces_false():
    args = _agent_parser().parse_args(["agent", "--no-preview", "follow me"])
    assert args.preview is False
    assert agent_cli._resolve_preview(args) is False


def test_preview_forces_true():
    args = _agent_parser().parse_args(["agent", "--preview", "follow me"])
    assert args.preview is True
    assert agent_cli._resolve_preview(args) is True


def test_follow_seconds_defaults_none():
    args = _agent_parser().parse_args(["agent", "follow me"])
    assert args.follow_seconds is None


def test_resolve_preview_auto_is_false_without_tty(monkeypatch):
    """AUTO resolves to False when stdout is not a TTY (headless / piped / CI)."""
    import sys

    monkeypatch.setattr(sys.stdout, "isatty", lambda: False, raising=False)
    args = _agent_parser().parse_args(["agent", "follow me"])
    assert agent_cli._resolve_preview(args) is False
