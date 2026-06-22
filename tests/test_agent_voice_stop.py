"""Hands-free voice "stop" that ends a voice-initiated FOLLOW tail.

No real mic, model, or GUI: a fake STT backend + a synthetic/fake mic are
monkeypatched in, and the shared ``run_follow_loop`` / the listener are stubbed
so we exercise the wiring (stop_event creation, listener lifecycle, stop-phrase
matching, never-raise/never-false-stop) without any hardware or model.
"""

from __future__ import annotations

import argparse
import threading
import time

import numpy as np
import pytest

import yalp.reactive.follow_runner as follow_runner
import yalp.voice.stt as stt_module
from yalp import config
from yalp.contract.messages import GoalStatus, Mode, RobotState
from yalp.deliberative import agent_cli


# --------------------------------------------------------------------------- #
# Parser plumbing
# --------------------------------------------------------------------------- #
def _agent_parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser()
    sub = root.add_subparsers(dest="subcommand")
    agent_cli.add_parser(sub)
    return root


def test_no_voice_stop_flag_defaults_false():
    args = _agent_parser().parse_args(["agent", "--listen"])
    assert args.no_voice_stop is False


def test_no_voice_stop_flag_sets_true():
    args = _agent_parser().parse_args(["agent", "--listen", "--no-voice-stop"])
    assert args.no_voice_stop is True


def test_no_voice_stop_keeps_existing_flags_intact():
    args = _agent_parser().parse_args(
        [
            "agent",
            "--listen",
            "--speak",
            "--synthetic",
            "--steps",
            "3",
            "--no-preview",
            "--follow-seconds",
            "5",
            "--follow-detector",
            "face",
            "--no-voice-stop",
        ]
    )
    assert args.listen is True
    assert args.speak is True
    assert args.synthetic is True
    assert args.steps == 3
    assert args.preview is False
    assert args.follow_seconds == 5.0
    assert args.follow_detector == "face"
    assert args.no_voice_stop is True


# --------------------------------------------------------------------------- #
# _is_stop_phrase: pure matcher
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "text",
    ["stop", "please stop", "HALT now", "  Stop.  ", "and then halt"],
)
def test_is_stop_phrase_true(text):
    assert agent_cli._is_stop_phrase(text) is True


@pytest.mark.parametrize(
    "text",
    ["follow me", "", "   ", "top", "go forward", None],
)
def test_is_stop_phrase_false(text):
    assert agent_cli._is_stop_phrase(text) is False


# --------------------------------------------------------------------------- #
# Fakes for the listener
# --------------------------------------------------------------------------- #
class _FakeSTT:
    """A scripted STT backend that counts transcribe calls."""

    def __init__(self, transcript: str) -> None:
        self.transcript = transcript
        self.calls = 0

    def transcribe(self, wav_bytes: bytes) -> str:
        self.calls += 1
        return self.transcript


class _FakeMic:
    """A context-manager mic stub returning a fixed audio chunk each window."""

    instances = 0

    def __init__(self, *args, **kwargs):
        type(self).instances += 1
        self.sample_rate = config.VOICE_SAMPLE_RATE
        self.kwargs = kwargs

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def record_once(self, *args, **kwargs):
        return np.zeros(16, dtype=np.float32)


def _install_fakes(monkeypatch, transcript: str):
    """Monkeypatch get_backend (ONE instance) + Microphone; return state dict."""
    state = {"get_backend_calls": 0, "backend": _FakeSTT(transcript)}
    _FakeMic.instances = 0

    def fake_get_backend(name=None):
        state["get_backend_calls"] += 1
        return state["backend"]

    # Patch where the listener imports them from.
    monkeypatch.setattr(stt_module, "get_backend", fake_get_backend)
    import yalp.voice as voice

    monkeypatch.setattr(voice, "Microphone", _FakeMic, raising=False)
    return state


# --------------------------------------------------------------------------- #
# _listen_for_voice_stop: behavior
# --------------------------------------------------------------------------- #
def test_listener_sets_event_on_stop(monkeypatch, capsys):
    """A 'stop' transcript sets the passed stop_event and prints a marker."""
    state = _install_fakes(monkeypatch, "stop")
    stop_event = threading.Event()

    # 'stop' matches on the first window, so the listener returns promptly.
    agent_cli._listen_for_voice_stop(stop_event)

    assert stop_event.is_set() is True
    assert state["get_backend_calls"] == 1  # ONE backend instance (model once)
    assert state["backend"].calls >= 1
    assert _FakeMic.instances == 1  # ONE mic
    assert "[voice: stop]" in capsys.readouterr().out


def test_listener_does_not_set_on_non_stop(monkeypatch, capsys):
    """A non-stop transcript ('hello') never sets the event (no false stop)."""
    state = _install_fakes(monkeypatch, "hello there")
    stop_event = threading.Event()

    t = threading.Thread(
        target=agent_cli._listen_for_voice_stop, args=(stop_event,), daemon=True
    )
    t.start()
    time.sleep(0.15)  # let it grind through several windows
    # The listener itself must NOT have stopped on a non-stop transcript.
    assert stop_event.is_set() is False
    # ONE backend reused across MANY transcriptions (model loads once).
    assert state["get_backend_calls"] == 1
    assert state["backend"].calls > 1

    stop_event.set()  # release the loop and reclaim the thread
    t.join(timeout=2.0)
    assert not t.is_alive()
    assert "[voice: stop]" not in capsys.readouterr().out


def test_listener_swallows_transcribe_failure(monkeypatch):
    """A transcribe error neither sets the event nor propagates."""
    state = _install_fakes(monkeypatch, "stop")

    def boom(wav_bytes):
        state["backend"].calls += 1
        raise RuntimeError("model exploded")

    monkeypatch.setattr(state["backend"], "transcribe", boom)
    stop_event = threading.Event()

    t = threading.Thread(
        target=agent_cli._listen_for_voice_stop, args=(stop_event,), daemon=True
    )
    t.start()
    time.sleep(0.15)
    # Errors must NEVER falsely stop FOLLOW.
    assert stop_event.is_set() is False
    assert state["backend"].calls > 1  # kept trying, swallowing each failure

    stop_event.set()
    t.join(timeout=2.0)
    assert not t.is_alive()


def test_listener_noop_when_voice_unavailable(monkeypatch):
    """If voice deps can't be imported, the listener no-ops (never raises)."""
    import yalp.voice as voice

    # Simulate the voice extra being absent: with Microphone removed,
    # ``from ..voice import Microphone`` raises ImportError, which the listener
    # swallows into a no-op (never raises, never sets the event).
    monkeypatch.delattr(voice, "Microphone", raising=False)

    stop_event = threading.Event()
    agent_cli._listen_for_voice_stop(stop_event)
    assert stop_event.is_set() is False


def test_listener_noop_when_backend_setup_fails(monkeypatch):
    """If the STT backend can't be built (e.g. model missing), the listener no-ops."""
    import yalp.voice as voice

    monkeypatch.setattr(voice, "Microphone", _FakeMic, raising=False)

    def boom_backend(name=None):
        raise RuntimeError("faster-whisper not installed")

    monkeypatch.setattr(stt_module, "get_backend", boom_backend)

    stop_event = threading.Event()
    agent_cli._listen_for_voice_stop(stop_event)
    assert stop_event.is_set() is False


# --------------------------------------------------------------------------- #
# _maybe_follow_tail: lifecycle wiring
# --------------------------------------------------------------------------- #
class _FakeBackend:
    def __init__(self, mode: Mode) -> None:
        self._mode = mode

    def get_state(self) -> RobotState:
        return RobotState(mode=self._mode, goal_status=GoalStatus.RUNNING)


def _spy_loop(monkeypatch, calls):
    def fake_loop(backend, **kwargs):
        calls["loop_stop_event"] = kwargs.get("stop_event")
        calls["owns_ticking"] = kwargs.get("owns_ticking")
        return 0

    monkeypatch.setattr(follow_runner, "run_follow_loop", fake_loop)


def _spy_listener(monkeypatch, calls):
    def fake_listener(stop_event, **kwargs):
        calls["listener_called"] = calls.get("listener_called", 0) + 1
        calls["listener_stop_event"] = stop_event

    monkeypatch.setattr(agent_cli, "_listen_for_voice_stop", fake_listener)


def _capture_threads(monkeypatch, created):
    real_thread = threading.Thread

    def capturing(*args, **kwargs):
        th = real_thread(*args, **kwargs)
        created.append(th)
        return th

    monkeypatch.setattr(agent_cli.threading, "Thread", capturing)


def test_follow_tail_creates_and_passes_stop_event(monkeypatch):
    """A stop_event is always created and handed to run_follow_loop."""
    calls = {}
    _spy_loop(monkeypatch, calls)
    _spy_listener(monkeypatch, calls)

    args = _agent_parser().parse_args(["agent", "--no-preview", "follow", "me"])
    agent_cli._maybe_follow_tail(_FakeBackend(Mode.FOLLOW), args)

    assert isinstance(calls["loop_stop_event"], threading.Event)
    assert calls["owns_ticking"] is False  # never double-tick


def test_follow_tail_starts_listener_on_listen(monkeypatch):
    """--listen FOLLOW: the listener thread starts, shares the loop's event, joins."""
    calls = {}
    created = []
    _spy_loop(monkeypatch, calls)
    _spy_listener(monkeypatch, calls)
    _capture_threads(monkeypatch, created)

    args = _agent_parser().parse_args(["agent", "--listen", "--no-preview", "follow me"])
    agent_cli._maybe_follow_tail(_FakeBackend(Mode.FOLLOW), args)

    assert calls.get("listener_called") == 1
    # SAME event is shared between the listener and the follow loop.
    assert calls["listener_stop_event"] is calls["loop_stop_event"]
    # A listener thread was created, named, and joined (not left alive).
    assert created, "expected a listener thread to be created"
    assert all(not th.is_alive() for th in created)
    # finally: event is set so teardown is uniform.
    assert calls["loop_stop_event"].is_set() is True


def test_follow_tail_no_listener_when_disabled(monkeypatch):
    """--no-voice-stop suppresses the listener but still creates/passes the event."""
    calls = {}
    created = []
    _spy_loop(monkeypatch, calls)
    _spy_listener(monkeypatch, calls)
    _capture_threads(monkeypatch, created)

    args = _agent_parser().parse_args(
        ["agent", "--listen", "--no-voice-stop", "--no-preview", "follow me"]
    )
    agent_cli._maybe_follow_tail(_FakeBackend(Mode.FOLLOW), args)

    assert "listener_called" not in calls
    assert created == []  # no listener thread created
    assert isinstance(calls["loop_stop_event"], threading.Event)


def test_follow_tail_no_listener_for_typed_mode(monkeypatch):
    """Typed 'follow me' (no --listen) has no mic listener — only Ctrl-C / 'q'."""
    calls = {}
    created = []
    _spy_loop(monkeypatch, calls)
    _spy_listener(monkeypatch, calls)
    _capture_threads(monkeypatch, created)

    args = _agent_parser().parse_args(["agent", "--no-preview", "follow me"])
    agent_cli._maybe_follow_tail(_FakeBackend(Mode.FOLLOW), args)

    assert "listener_called" not in calls
    assert created == []
    assert isinstance(calls["loop_stop_event"], threading.Event)
