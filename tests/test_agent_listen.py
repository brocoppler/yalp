"""Push-to-talk voice input into ``yalp agent`` — no mic, model, or API key.

These exercise the ``--listen`` seam wired into the EXISTING deliberative agent
loop (``agent_cli.run``): when no typed command is given and ``--listen`` is set,
ONE spoken utterance is captured + transcribed and flows through the SAME path
into the agent. Everything hardware/cloud is stubbed:

  * ``yalp.voice.Microphone`` / ``yalp.voice.transcribe`` are monkeypatched (or
    driven via the dependency-free 'fake' STT backend + file/synthetic source),
  * the agent dispatch (``_run_one``) is patched to capture the user_text so we
    assert the transcript reached ``Agent.run_turn`` without a real model run.
"""

from __future__ import annotations

import argparse

import numpy as np
import pytest

from yalp import config
from yalp.deliberative import agent_cli
from yalp.voice.microphone import Microphone, to_wav_bytes


# --- parser plumbing --------------------------------------------------------
def _make_agent_parser():
    root = argparse.ArgumentParser()
    sub = root.add_subparsers(dest="subcommand")
    agent_cli.add_parser(sub)
    return root


def test_listen_flag_defaults_false():
    parser = _make_agent_parser()
    args = parser.parse_args(["agent", "look", "around"])
    assert args.listen is False


def test_listen_flag_sets_true():
    parser = _make_agent_parser()
    args = parser.parse_args(["agent", "--listen"])
    assert args.listen is True


def test_listen_keeps_existing_flags_intact():
    """--listen coexists with --speak / --synthetic / --steps / --command."""
    parser = _make_agent_parser()
    args = parser.parse_args(
        ["agent", "--listen", "--speak", "--synthetic", "--steps", "3"]
    )
    assert args.listen is True
    assert args.speak is True
    assert args.synthetic is True
    assert args.steps == 3
    assert args.command is None
    assert (args.words or []) == []


# --- _listen_for_command: capture + transcribe ------------------------------
class _StubMic:
    """A context-manager mic stub returning a fixed audio chunk."""

    def __init__(self, *args, **kwargs):
        self.sample_rate = config.VOICE_SAMPLE_RATE
        self.kwargs = kwargs

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def record_once(self, *args, **kwargs):
        return np.zeros(16, dtype=np.float32)


def test_listen_for_command_returns_transcript(monkeypatch, capsys):
    """A stubbed mic + transcribe yields the stripped transcript + a heard line."""
    seen = {}

    def fake_transcribe(wav_bytes, *, backend=None):
        seen["wav"] = wav_bytes
        return "  follow me  "

    import yalp.voice as voice

    monkeypatch.setattr(voice, "Microphone", _StubMic)
    monkeypatch.setattr(voice, "transcribe", fake_transcribe)

    result = agent_cli._listen_for_command()
    assert result == "follow me"
    # The WAV-bytes contract: a real RIFF/WAVE container reached transcribe().
    assert seen["wav"][:4] == b"RIFF" and seen["wav"][8:12] == b"WAVE"
    out = capsys.readouterr().out
    assert "[heard: follow me]" in out


def test_listen_for_command_empty_transcript_returns_none(monkeypatch, capsys):
    import yalp.voice as voice

    monkeypatch.setattr(voice, "Microphone", _StubMic)
    monkeypatch.setattr(voice, "transcribe", lambda wav, *, backend=None: "   ")
    assert agent_cli._listen_for_command() is None
    assert "[heard nothing]" in capsys.readouterr().out


def test_listen_for_command_swallows_capture_failure(monkeypatch):
    """A capture/STT exception is swallowed -> None (CLI never crashes)."""

    class _BoomMic:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def record_once(self, *a, **k):
            raise RuntimeError("no audio device, boom")

    import yalp.voice as voice

    monkeypatch.setattr(voice, "Microphone", _BoomMic)
    # transcribe should never be reached; make it loud if it is.
    monkeypatch.setattr(
        voice, "transcribe", lambda *a, **k: pytest.fail("transcribe reached")
    )
    assert agent_cli._listen_for_command() is None


def test_listen_for_command_swallows_transcribe_failure(monkeypatch):
    import yalp.voice as voice

    monkeypatch.setattr(voice, "Microphone", _StubMic)

    def boom(*a, **k):
        raise RuntimeError("model missing")

    monkeypatch.setattr(voice, "transcribe", boom)
    assert agent_cli._listen_for_command() is None


# --- end-to-end: wav-bytes contract via the REAL fake STT backend -----------
def test_listen_for_command_file_source_fake_backend(monkeypatch, tmp_path, capsys):
    """Drive the file Microphone source + to_wav_bytes + the 'fake' STT backend.

    No mic, no model, no API key — proves the wav-bytes contract end-to-end: a
    real WAV file is decoded by Microphone(source='file'), encoded by
    to_wav_bytes, and decoded by the dependency-free fake STT backend.
    """
    # Write a tiny real WAV file the file-source Microphone can decode.
    tone = (0.2 * np.sin(2 * np.pi * 440 * np.arange(8000) / 16000)).astype(np.float32)
    wav_path = tmp_path / "cmd.wav"
    wav_path.write_bytes(to_wav_bytes(tone, 16000))

    monkeypatch.setattr(config, "VOICE_SOURCE", "file")
    monkeypatch.setattr(config, "VOICE_AUDIO_FILE", str(wav_path))
    monkeypatch.setattr(config, "STT_BACKEND", "fake")

    result = agent_cli._listen_for_command()
    # The dependency-free fake backend's default transcript.
    assert result == "follow me"
    assert "[heard: follow me]" in capsys.readouterr().out


# --- run() seam: transcript flows into the agent path -----------------------
def _patch_run_pipeline(monkeypatch, captured):
    """Stub everything run() needs except the command-resolution seam."""
    import types

    # Always look like we have a key so run() does not early-return.
    monkeypatch.setattr(config, "get_api_key", lambda: "sk-test")

    # Stub the heavy IPC + backend machinery to no-ops.
    fake_vision = types.SimpleNamespace(MISSING_KEY_MESSAGE="missing")
    monkeypatch.setattr(
        "yalp.deliberative.vision", fake_vision, raising=False
    )

    class _FakeServer:
        def __init__(self, *a, **k):
            self.port = 0

        def start(self):
            pass

        def wait_for_client(self, *a, **k):
            pass

        def stop(self):
            pass

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        def connect(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(
        "yalp.contract.ipc.ReactiveServer", _FakeServer, raising=False
    )
    monkeypatch.setattr(
        "yalp.contract.ipc.DeliberativeClient", _FakeClient, raising=False
    )

    class _FakeBackend:
        def __init__(self, *a, **k):
            self.mailbox = None

        def run(self, *a, **k):
            pass

        def camera(self):
            return None

    monkeypatch.setattr(agent_cli, "_make_backend", lambda **k: _FakeBackend())
    monkeypatch.setattr(agent_cli, "_make_describe", lambda backend: (lambda **k: ""))

    # Capture the command that reaches the agent dispatch.
    def fake_run_one(agent, command, fmt):
        captured["command"] = command

    def fake_interactive(agent, fmt):
        captured["interactive"] = True

    monkeypatch.setattr(agent_cli, "_run_one", fake_run_one)
    monkeypatch.setattr(agent_cli, "_interactive", fake_interactive)

    # Don't construct a real Agent.
    monkeypatch.setattr(
        "yalp.deliberative.agent.Agent", lambda **k: object(), raising=False
    )


def test_run_listen_feeds_transcript_to_agent(monkeypatch):
    """`yalp agent --listen` (no words / no --command) runs the transcript."""
    captured = {}
    _patch_run_pipeline(monkeypatch, captured)
    monkeypatch.setattr(agent_cli, "_listen_for_command", lambda: "follow me")

    parser = _make_agent_parser()
    args = parser.parse_args(["agent", "--listen"])
    rc = agent_cli.run(args)

    assert rc == 0
    assert captured.get("command") == "follow me"
    assert "interactive" not in captured


def test_run_typed_command_takes_precedence_over_listen(monkeypatch):
    """A typed command wins; _listen_for_command is NOT called."""
    captured = {}
    _patch_run_pipeline(monkeypatch, captured)
    monkeypatch.setattr(
        agent_cli,
        "_listen_for_command",
        lambda: pytest.fail("_listen_for_command should not run with a typed command"),
    )

    parser = _make_agent_parser()
    args = parser.parse_args(["agent", "--listen", "go", "forward"])
    rc = agent_cli.run(args)

    assert rc == 0
    assert captured.get("command") == "go forward"


def test_run_command_flag_takes_precedence_over_listen(monkeypatch):
    captured = {}
    _patch_run_pipeline(monkeypatch, captured)
    monkeypatch.setattr(
        agent_cli,
        "_listen_for_command",
        lambda: pytest.fail("_listen_for_command should not run with --command"),
    )

    parser = _make_agent_parser()
    args = parser.parse_args(["agent", "--listen", "--command", "explore the room"])
    rc = agent_cli.run(args)

    assert rc == 0
    assert captured.get("command") == "explore the room"


def test_run_listen_none_falls_through_to_interactive(monkeypatch):
    """If listening yields nothing, run() falls through to interactive (as today)."""
    captured = {}
    _patch_run_pipeline(monkeypatch, captured)
    monkeypatch.setattr(agent_cli, "_listen_for_command", lambda: None)

    parser = _make_agent_parser()
    args = parser.parse_args(["agent", "--listen"])
    rc = agent_cli.run(args)

    assert rc == 0
    assert captured.get("interactive") is True
    assert "command" not in captured
