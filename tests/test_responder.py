"""Tests for the answer-delivery channels (:mod:`yalp.responder`).

No audio and no platform assumptions: the TtsResponder's ``speak`` / ``available``
callables are injected, so these exercise the real fan-out / isolation / warn-once
plumbing WITHOUT emitting a sound or resolving a real TTS binary. Coverage:

  1. ConsoleResponder prints a clear, greppable per-kind prefix (answer/status/error).
  2. CompositeResponder fans one reply out to every child channel...
  3. ...and ISOLATES a failing child (logged, never propagated; siblings still run).
  4. TtsResponder speaks when TTS is available.
  5. TtsResponder logs a WARNING exactly ONCE when TTS is unavailable, never
     speaks, and NEVER silently drops (it falls through).
  6. TtsResponder swallows a spawn failure and a broken availability probe.
  7. The Responder Protocol is runtime-checkable.
"""

from __future__ import annotations

import logging

import pytest

from yalp.responder import (
    KIND_ANSWER,
    KIND_ERROR,
    KIND_STATUS,
    CompositeResponder,
    ConsoleResponder,
    Responder,
    TtsResponder,
)


# --- a no-op recording channel used across the composite tests ---------------
class RecordingResponder:
    """A fake Responder that records every (text, kind) it is handed."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def respond(self, text: str, *, kind: str = KIND_ANSWER) -> None:
        self.calls.append((text, kind))


# --- 1. ConsoleResponder: greppable per-kind prefixes ------------------------
@pytest.mark.parametrize(
    "kind, prefix",
    [
        (KIND_ANSWER, "[yalp:answer]"),
        (KIND_STATUS, "[yalp:status]"),
        (KIND_ERROR, "[yalp:error]"),
    ],
)
def test_console_responder_prefixes_per_kind(capsys, kind, prefix):
    ConsoleResponder().respond("path is clear", kind=kind)
    out = capsys.readouterr().out
    assert out == f"{prefix} path is clear\n"


def test_console_responder_defaults_to_answer_kind(capsys):
    ConsoleResponder().respond("hello")
    assert capsys.readouterr().out == "[yalp:answer] hello\n"


def test_console_responder_unknown_kind_still_greppable(capsys):
    ConsoleResponder().respond("weird", kind="debug")
    assert capsys.readouterr().out == "[yalp:debug] weird\n"


def test_console_responder_honors_explicit_stream():
    import io

    buf = io.StringIO()
    ConsoleResponder(stream=buf).respond("to the buffer", kind=KIND_STATUS)
    assert buf.getvalue() == "[yalp:status] to the buffer\n"


# --- 2. CompositeResponder: fan-out ------------------------------------------
def test_composite_fans_out_to_every_child():
    a, b, c = RecordingResponder(), RecordingResponder(), RecordingResponder()
    CompositeResponder(a, b, c).respond("all clear", kind=KIND_ANSWER)

    for child in (a, b, c):
        assert child.calls == [("all clear", KIND_ANSWER)]


def test_composite_forwards_kind():
    child = RecordingResponder()
    CompositeResponder(child).respond("oops", kind=KIND_ERROR)
    assert child.calls == [("oops", KIND_ERROR)]


def test_composite_with_no_children_is_a_noop():
    # A degenerate composite must not raise (answers just go nowhere here).
    CompositeResponder().respond("into the void")


# --- 3. CompositeResponder: isolation ----------------------------------------
class _BoomResponder:
    def respond(self, text: str, *, kind: str = KIND_ANSWER) -> None:
        raise RuntimeError("channel exploded")


def test_composite_isolates_failing_child(caplog):
    good_before = RecordingResponder()
    good_after = RecordingResponder()

    with caplog.at_level(logging.WARNING, logger="yalp.responder"):
        # The bad channel sits BETWEEN two good ones: both good ones must still
        # deliver, and the exception must not propagate out of respond().
        CompositeResponder(good_before, _BoomResponder(), good_after).respond(
            "still delivered", kind=KIND_ANSWER
        )

    assert good_before.calls == [("still delivered", KIND_ANSWER)]
    assert good_after.calls == [("still delivered", KIND_ANSWER)]
    # The failure was logged (never silently swallowed), naming the bad channel.
    assert any(
        "_BoomResponder" in r.message and r.levelno == logging.WARNING
        for r in caplog.records
    )


# --- 4./5./6. TtsResponder ---------------------------------------------------
def test_tts_responder_speaks_when_available():
    spoken: list[str] = []
    responder = TtsResponder(speak=spoken.append, available=lambda: True)

    responder.respond("driving forward", kind=KIND_ANSWER)

    assert spoken == ["driving forward"]


def test_tts_responder_strips_and_ignores_blank():
    spoken: list[str] = []
    responder = TtsResponder(speak=spoken.append, available=lambda: True)

    responder.respond("   ")
    responder.respond("")
    responder.respond("  hi  ")

    assert spoken == ["hi"]  # blank ignored; text stripped before speaking


def test_tts_responder_warns_once_when_unavailable(caplog):
    spoken: list[str] = []
    responder = TtsResponder(speak=spoken.append, available=lambda: False)

    with caplog.at_level(logging.WARNING, logger="yalp.responder"):
        responder.respond("first answer")
        responder.respond("second answer")

    # Never spoke (no binary), never raised, and DID NOT silently drop: it warned.
    assert spoken == []
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1  # exactly once, not once per call
    assert "TTS unavailable" in warnings[0].message


def test_tts_responder_swallows_speak_failure(caplog):
    def boom(_text):
        raise OSError("no audio device")

    responder = TtsResponder(speak=boom, available=lambda: True)

    with caplog.at_level(logging.WARNING, logger="yalp.responder"):
        responder.respond("this should be swallowed")  # must not raise

    assert any("TTS delivery failed" in r.message for r in caplog.records)


def test_tts_responder_treats_probe_error_as_unavailable(caplog):
    spoken: list[str] = []

    def boom_probe():
        raise RuntimeError("which() blew up")

    responder = TtsResponder(speak=spoken.append, available=boom_probe)

    with caplog.at_level(logging.WARNING, logger="yalp.responder"):
        responder.respond("hello?")  # must not raise

    assert spoken == []  # a broken probe degrades to "unavailable", not a crash


def test_tts_responder_resolves_real_voice_by_default(monkeypatch):
    """With no injected callables, TtsResponder wires to yalp.voice.tts."""
    from yalp.voice import tts

    spoken: list[str] = []
    monkeypatch.setattr(tts, "tts_available", lambda: True)
    monkeypatch.setattr(tts, "speak", lambda text, **kw: spoken.append(text))

    TtsResponder().respond("via real voice module")

    assert spoken == ["via real voice module"]


# --- 7. Protocol is runtime-checkable ----------------------------------------
def test_responder_protocol_is_runtime_checkable():
    assert isinstance(ConsoleResponder(), Responder)
    assert isinstance(TtsResponder(), Responder)
    assert isinstance(CompositeResponder(), Responder)
    assert isinstance(RecordingResponder(), Responder)

    class NotAResponder:
        pass

    assert not isinstance(NotAResponder(), Responder)
