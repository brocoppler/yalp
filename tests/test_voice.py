"""Tests for spoken OUTPUT (yalp.voice) — no audio, no platform assumptions.

The actual TTS subprocess spawn (``voice.tts._spawn``), the binary selection
(``voice.tts._tts_binary`` / ``platform.system``) and the capability probe
(``voice.tts.tts_available``) are monkeypatched, so these exercise the real speak
plumbing and the ``--speak`` wiring WITHOUT emitting a sound or assuming the host
is a Mac (or a Linux box). Coverage:

  1. speak() builds + spawns the platform TTS command with the text when TTS is
     available (voice/rate from kwargs flow through).
  2. _build_command builds the right argv per platform: macOS ``say -r RATE`` and
     non-macOS ``espeak-ng -s RATE``.
  3. tts_available() reflects ``shutil.which`` for the *resolved* binary on both
     platforms.
  4. speak() is a graceful no-op (never raises, never spawns) when the binary is
     unavailable, and never raises even if the spawn itself blows up.
  5. ``yalp see --speak`` speaks the description; without --speak it does not.
  6. the agent's ``speak`` tool vocalizes via the threaded callback when speech
     is enabled, and does not when it is disabled (default off).

Internals (``_spawn``, ``tts_available``, ``_tts_binary``) are patched on the
live :mod:`yalp.voice.tts` module, since :func:`speak` resolves them there; the
public surface (``voice.speak`` etc.) is still imported from the ``voice``
package exactly as production callers use it.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from yalp import voice
from yalp.deliberative import see as see_cli


def _force_platform(monkeypatch, system: str) -> None:
    """Pin the OS so TTS binary selection is deterministic in tests."""
    monkeypatch.setattr(voice.tts.platform, "system", lambda: system)


# --- voice.speak / tts_available -------------------------------------------
def test_speak_spawns_say_with_text(monkeypatch):
    _force_platform(monkeypatch, "Darwin")
    monkeypatch.setattr(voice.tts, "tts_available", lambda: True)
    spawned: list[list[str]] = []
    monkeypatch.setattr(voice.tts, "_spawn", lambda cmd: spawned.append(cmd))

    voice.speak("hello world")

    assert len(spawned) == 1
    cmd = spawned[0]
    assert cmd[0] == voice.SAY_BINARY
    assert cmd[-1] == "hello world"


def test_speak_passes_voice_and_rate(monkeypatch):
    _force_platform(monkeypatch, "Darwin")
    monkeypatch.setattr(voice.tts, "tts_available", lambda: True)
    spawned: list[list[str]] = []
    monkeypatch.setattr(voice.tts, "_spawn", lambda cmd: spawned.append(cmd))

    voice.speak("hi", voice="Samantha", rate=180)

    cmd = spawned[0]
    assert "-v" in cmd and cmd[cmd.index("-v") + 1] == "Samantha"
    assert "-r" in cmd and cmd[cmd.index("-r") + 1] == "180"
    assert cmd[-1] == "hi"


def test_speak_ignores_blank_text(monkeypatch):
    monkeypatch.setattr(voice.tts, "tts_available", lambda: True)
    spawned: list[list[str]] = []
    monkeypatch.setattr(voice.tts, "_spawn", lambda cmd: spawned.append(cmd))

    voice.speak("   ")
    voice.speak("")

    assert spawned == []


def test_speak_is_noop_when_tts_unavailable(monkeypatch):
    monkeypatch.setattr(voice.tts, "tts_available", lambda: False)

    def _boom(cmd):  # must never be reached
        raise AssertionError("must not spawn when TTS is unavailable")

    monkeypatch.setattr(voice.tts, "_spawn", _boom)

    # Degrades gracefully: no spawn, no exception.
    voice.speak("anybody home?")


def test_speak_never_raises_on_spawn_failure(monkeypatch):
    monkeypatch.setattr(voice.tts, "tts_available", lambda: True)

    def _boom(cmd):
        raise OSError("no audio device")

    monkeypatch.setattr(voice.tts, "_spawn", _boom)

    # Best-effort: a broken voice must not crash the caller.
    voice.speak("this should be swallowed")


# --- cross-platform binary selection / argv ---------------------------------
def test_build_command_macos_uses_say_with_r_rate(monkeypatch):
    _force_platform(monkeypatch, "Darwin")

    cmd = voice._build_command("hi", "Samantha", 180)

    assert cmd[0] == voice.SAY_BINARY
    assert "-v" in cmd and cmd[cmd.index("-v") + 1] == "Samantha"
    assert "-r" in cmd and cmd[cmd.index("-r") + 1] == "180"
    assert "-s" not in cmd
    assert cmd[-1] == "hi"


def test_build_command_non_macos_uses_espeak_with_s_rate(monkeypatch):
    _force_platform(monkeypatch, "Linux")

    cmd = voice._build_command("hi", "en", 180)

    assert cmd[0] == voice.ESPEAK_BINARY
    assert "-v" in cmd and cmd[cmd.index("-v") + 1] == "en"
    assert "-s" in cmd and cmd[cmd.index("-s") + 1] == "180"
    assert "-r" not in cmd
    assert cmd[-1] == "hi"


def test_speak_non_macos_spawns_espeak(monkeypatch):
    _force_platform(monkeypatch, "Linux")
    monkeypatch.setattr(voice.tts, "tts_available", lambda: True)
    spawned: list[list[str]] = []
    monkeypatch.setattr(voice.tts, "_spawn", lambda cmd: spawned.append(cmd))

    voice.speak("hello pi", rate=200)

    cmd = spawned[0]
    assert cmd[0] == voice.ESPEAK_BINARY
    assert "-s" in cmd and cmd[cmd.index("-s") + 1] == "200"
    assert cmd[-1] == "hello pi"


def test_tts_binary_selection(monkeypatch):
    _force_platform(monkeypatch, "Darwin")
    assert voice._tts_binary() == voice.SAY_BINARY

    _force_platform(monkeypatch, "Linux")
    assert voice._tts_binary() == voice.ESPEAK_BINARY


def test_tts_available_reflects_say_on_path(monkeypatch):
    _force_platform(monkeypatch, "Darwin")
    seen: list[str] = []

    def _which(name):
        seen.append(name)
        return "/usr/bin/say"

    monkeypatch.setattr(voice.shutil, "which", _which)
    assert voice.tts_available() is True
    assert seen == [voice.SAY_BINARY]

    monkeypatch.setattr(voice.shutil, "which", lambda name: None)
    assert voice.tts_available() is False


def test_tts_available_reflects_espeak_on_path(monkeypatch):
    _force_platform(monkeypatch, "Linux")
    seen: list[str] = []

    def _which(name):
        seen.append(name)
        return "/usr/bin/espeak-ng"

    monkeypatch.setattr(voice.shutil, "which", _which)
    assert voice.tts_available() is True
    assert seen == [voice.ESPEAK_BINARY]

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


# --- voice.wait_for_speech: drain the final utterance before exit -----------
class _FakeProc:
    def __init__(self):
        self.waited_for = None

    def poll(self):
        return None  # still "running" so _spawn keeps it tracked

    def wait(self, timeout=None):
        self.waited_for = timeout


def test_wait_for_speech_joins_outstanding_processes(monkeypatch):
    _force_platform(monkeypatch, "Darwin")
    monkeypatch.setattr(voice.tts, "tts_available", lambda: True)
    proc = _FakeProc()
    monkeypatch.setattr(voice.subprocess, "Popen", lambda *a, **k: proc)
    voice._live_processes.clear()

    voice.speak("final report")
    assert voice._live_processes == [proc]

    voice.wait_for_speech()
    # The outstanding TTS proc was joined (so it isn't cut off) and the registry
    # is drained afterward.
    assert proc.waited_for is not None
    assert voice._live_processes == []


def test_wait_for_speech_never_raises(monkeypatch):
    class _BoomProc(_FakeProc):
        def wait(self, timeout=None):
            raise RuntimeError("stuck say")

    voice._live_processes.clear()
    voice._live_processes.append(_BoomProc())
    # Draining speech is best-effort — a wedged TTS proc must never propagate.
    voice.wait_for_speech()
    assert voice._live_processes == []
