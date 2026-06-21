"""Tests for spoken OUTPUT (yalp.voice) — no audio, no macOS assumptions.

The actual ``say`` subprocess spawn (``voice._spawn``) and the capability probe
(``voice.tts_available``) are monkeypatched, so these exercise the real speak
plumbing and the ``--speak`` wiring WITHOUT emitting a sound or assuming the
host is a Mac. Coverage:

  1. speak() builds + spawns the ``say`` command with the text when TTS is
     available (voice/rate from kwargs flow through).
  2. speak() is a graceful no-op (never raises, never spawns) when ``say`` is
     unavailable.
  3. speak() never raises even if the spawn itself blows up.
  4. ``yalp see --speak`` speaks the description; without --speak it does not.
  5. the agent's ``speak`` tool vocalizes via the threaded callback when speech
     is enabled, and does not when it is disabled (default off).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from yalp import voice
from yalp.deliberative import see as see_cli


# --- voice.speak / tts_available -------------------------------------------
def test_speak_spawns_say_with_text(monkeypatch):
    monkeypatch.setattr(voice, "tts_available", lambda: True)
    spawned: list[list[str]] = []
    monkeypatch.setattr(voice, "_spawn", lambda cmd: spawned.append(cmd))

    voice.speak("hello world")

    assert len(spawned) == 1
    cmd = spawned[0]
    assert cmd[0] == voice.SAY_BINARY
    assert cmd[-1] == "hello world"


def test_speak_passes_voice_and_rate(monkeypatch):
    monkeypatch.setattr(voice, "tts_available", lambda: True)
    spawned: list[list[str]] = []
    monkeypatch.setattr(voice, "_spawn", lambda cmd: spawned.append(cmd))

    voice.speak("hi", voice="Samantha", rate=180)

    cmd = spawned[0]
    assert "-v" in cmd and cmd[cmd.index("-v") + 1] == "Samantha"
    assert "-r" in cmd and cmd[cmd.index("-r") + 1] == "180"
    assert cmd[-1] == "hi"


def test_speak_ignores_blank_text(monkeypatch):
    monkeypatch.setattr(voice, "tts_available", lambda: True)
    spawned: list[list[str]] = []
    monkeypatch.setattr(voice, "_spawn", lambda cmd: spawned.append(cmd))

    voice.speak("   ")
    voice.speak("")

    assert spawned == []


def test_speak_is_noop_when_tts_unavailable(monkeypatch):
    monkeypatch.setattr(voice, "tts_available", lambda: False)

    def _boom(cmd):  # must never be reached
        raise AssertionError("must not spawn when TTS is unavailable")

    monkeypatch.setattr(voice, "_spawn", _boom)

    # Degrades gracefully: no spawn, no exception.
    voice.speak("anybody home?")


def test_speak_never_raises_on_spawn_failure(monkeypatch):
    monkeypatch.setattr(voice, "tts_available", lambda: True)

    def _boom(cmd):
        raise OSError("no audio device")

    monkeypatch.setattr(voice, "_spawn", _boom)

    # Best-effort: a broken voice must not crash the caller.
    voice.speak("this should be swallowed")


def test_tts_available_reflects_say_on_path(monkeypatch):
    monkeypatch.setattr(voice.shutil, "which", lambda name: "/usr/bin/say")
    assert voice.tts_available() is True

    monkeypatch.setattr(voice.shutil, "which", lambda name: None)
    assert voice.tts_available() is False


# --- `yalp see --speak` -----------------------------------------------------
def _see_args(speak: bool):
    return SimpleNamespace(image=None, question=[], speak=speak)


def test_see_speak_flag_speaks_description(monkeypatch, capsys):
    monkeypatch.setattr(see_cli.vision, "see", lambda **kw: "a tidy desk")
    spoken: list[str] = []
    monkeypatch.setattr(voice, "speak", lambda text, **kw: spoken.append(text))

    rc = see_cli.run(_see_args(speak=True))

    assert rc == 0
    assert spoken == ["a tidy desk"]
    # Voice is additive — the description is still printed.
    assert "a tidy desk" in capsys.readouterr().out


def test_see_without_speak_flag_is_silent(monkeypatch):
    monkeypatch.setattr(see_cli.vision, "see", lambda **kw: "a tidy desk")
    spoken: list[str] = []
    monkeypatch.setattr(voice, "speak", lambda text, **kw: spoken.append(text))

    see_cli.run(_see_args(speak=False))

    assert spoken == []


def test_see_parser_exposes_speak_flag():
    import argparse

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers()
    see_cli.add_parser(sub)
    args = parser.parse_args(["see", "--speak"])
    assert args.speak is True


# --- agent `speak` tool vocalizes when speech is enabled --------------------
def _speak_tool(text):
    return SimpleNamespace(type="tool_use", name="speak", input={"text": text},
                           id="toolu_speak")


def _make_agent(speak):
    from yalp.deliberative.agent import Agent

    return Agent(
        client=None,
        reactive=None,
        describe_scene=lambda **kw: "n/a",
        speak=speak,
    )


def test_agent_speak_tool_vocalizes_when_enabled():
    spoken: list[str] = []
    agent = _make_agent(speak=lambda text: spoken.append(text))
    agent._transcript = []

    result = agent._dispatch(_speak_tool("I see a cat"))

    assert "spoke" in result
    assert spoken == ["I see a cat"]


def test_agent_speak_tool_silent_by_default():
    agent = _make_agent(speak=None)
    agent._transcript = []

    # No callback wired (default off): records/prints but makes no sound, no error.
    result = agent._dispatch(_speak_tool("I see a cat"))

    assert "spoke" in result
    assert any(e.kind == "model" and e.text == "I see a cat" for e in agent._transcript)


def test_agent_vocalize_swallows_callback_errors():
    def _boom(text):
        raise RuntimeError("voice broke")

    agent = _make_agent(speak=_boom)
    agent._transcript = []

    # Best-effort speech must not wedge the dispatch.
    result = agent._dispatch(_speak_tool("hello"))
    assert "spoke" in result
