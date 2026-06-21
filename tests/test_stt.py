"""Tests for the pluggable speech-to-text layer — no model, network, or keys.

Everything here runs against :class:`FakeSTTBackend` (a dependency-free, scripted
stand-in) or simulates the *absence* of ``faster-whisper`` by forcing its lazy
import to fail. Importing :mod:`yalp.voice.stt` must always succeed regardless of
whether the heavy optional package is installed — that invariant is asserted at
module import time (this test file imports it at the top) and again explicitly.
"""

from __future__ import annotations

import builtins
import importlib

import pytest

import yalp.voice  # noqa: F401 - importing the package must not need the heavy dep

from yalp import config
from yalp.voice import stt as stt_mod
from yalp.voice.stt import (
    FakeSTTBackend,
    FasterWhisperBackend,
    STTBackend,
    get_backend,
    transcribe,
)

# A tiny non-empty WAV-ish payload. The fake backend never decodes it; it only
# records the exact bytes it was handed.
_WAV = b"RIFF....WAVEfmt fake-pcm-bytes"


# --- importability invariant ------------------------------------------------
def test_module_imports_without_faster_whisper():
    """Importing the module must not require the heavy optional package.

    The import at the top of this file already proves it; re-fetch from the
    module cache (NOT a reload, which would rebind class identities) and assert
    the public surface is present.
    """
    mod = importlib.import_module("yalp.voice.stt")
    assert hasattr(mod, "get_backend")
    assert issubclass(mod.FakeSTTBackend, mod.STTBackend)
    assert "faster_whisper" not in repr(mod.__dict__.get("WhisperModel", None))


# --- factory ----------------------------------------------------------------
def test_get_backend_fake_returns_fake_backend():
    backend = get_backend("fake")
    assert isinstance(backend, FakeSTTBackend)
    assert isinstance(backend, STTBackend)


def test_get_backend_unknown_name_raises_value_error():
    with pytest.raises(ValueError) as excinfo:
        get_backend("totally-not-a-backend")
    assert "totally-not-a-backend" in str(excinfo.value)


def test_get_backend_defaults_to_config_backend(monkeypatch):
    monkeypatch.setattr(config, "STT_BACKEND", "fake")
    backend = get_backend()  # no explicit name -> falls back to config
    assert isinstance(backend, FakeSTTBackend)


# --- transcribe convenience -------------------------------------------------
def test_transcribe_with_fake_returns_scripted_transcript():
    assert transcribe(_WAV, backend="fake") == "follow me"


def test_fake_backend_records_received_wav_bytes():
    backend = FakeSTTBackend(transcript="come here")
    result = backend.transcribe(_WAV)
    assert result == "come here"
    assert backend.calls == [_WAV]


def test_transcribe_default_backend_from_config(monkeypatch):
    monkeypatch.setattr(config, "STT_BACKEND", "fake")
    assert transcribe(_WAV) == "follow me"


# --- faster-whisper backend is lazy / fails legibly when dep is missing ------
def _block_faster_whisper(monkeypatch):
    """Make ``import faster_whisper`` raise, simulating the package's absence."""
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "faster_whisper" or name.startswith("faster_whisper."):
            raise ImportError("No module named 'faster_whisper'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)


def test_faster_whisper_backend_raises_runtime_error_when_missing(monkeypatch):
    _block_faster_whisper(monkeypatch)
    with pytest.raises(RuntimeError) as excinfo:
        FasterWhisperBackend()
    msg = str(excinfo.value)
    assert "yalp[voice]" in msg
    # A clear RuntimeError, never a bare ImportError leaking out.
    assert not isinstance(excinfo.value, ImportError)


def test_transcribe_faster_whisper_missing_raises_runtime_error(monkeypatch):
    _block_faster_whisper(monkeypatch)
    with pytest.raises(RuntimeError):
        transcribe(_WAV, backend="faster-whisper")
