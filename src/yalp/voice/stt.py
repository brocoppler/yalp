"""Pluggable speech-to-text (STT) for yalp — local-first and dependency-guarded.

This is the spoken-INPUT brain: it turns recorded audio (16-bit PCM mono WAV
bytes, exactly what :func:`yalp.voice.microphone.to_wav_bytes` produces) into
text for the deliberative loop. It is deliberately *pluggable* so the same call
site works against a real on-device model or a deterministic test fake:

  * :class:`FasterWhisperBackend` — local, on-CPU inference via
    `faster-whisper` (CTranslate2). The heavy dependency is imported **lazily**
    (never at module import time), so ``import yalp.voice.stt`` always works even
    when the package is absent. When it *is* missing, construction/transcription
    raises a clear, actionable :class:`RuntimeError` pointing at
    ``pip install 'yalp[voice]'`` — never a bare ``ImportError``.
  * :class:`FakeSTTBackend` — zero third-party deps; returns a scripted
    transcript and records the WAV bytes it received. Powers all unit tests.

Pick a backend by name via :func:`get_backend` (defaulting to
:data:`yalp.config.STT_BACKEND`) or call the module-level :func:`transcribe`
convenience.
"""

from __future__ import annotations

import io
import logging
import tempfile
from abc import ABC, abstractmethod
from typing import List, Optional

from yalp import config

logger = logging.getLogger(__name__)


# --- abstract base ----------------------------------------------------------
class STTBackend(ABC):
    """Abstract speech-to-text backend.

    Input contract: ``transcribe`` receives 16-bit PCM **mono** WAV bytes at
    :data:`yalp.config.VOICE_SAMPLE_RATE` (the container produced by
    :func:`yalp.voice.microphone.to_wav_bytes`) and returns the decoded text
    (stripped). Empty / no-speech audio returns ``''`` rather than raising.
    """

    @abstractmethod
    def transcribe(self, wav_bytes: bytes) -> str:
        """Transcribe WAV bytes to text. Subclasses must implement."""
        raise NotImplementedError


# --- real, local backend (lazy heavy import) --------------------------------
class FasterWhisperBackend(STTBackend):
    """Local on-CPU STT backend using ``faster-whisper`` (CTranslate2).

    The ``faster_whisper`` import and model construction are both deferred to
    avoid a hard dependency: importing this module never pulls in the heavy
    package. The model is built once, lazily, on first use.
    """

    def __init__(self, model_size: str = config.STT_MODEL) -> None:
        self.model_size = model_size
        self._model = None  # built lazily in _ensure_model()
        # Construct eagerly so a misconfigured / missing dependency fails loudly
        # and early (at backend creation) rather than deep inside transcribe().
        self._ensure_model()

    def _ensure_model(self):
        """Lazily import faster-whisper and build the WhisperModel (once)."""
        if self._model is not None:
            return self._model
        try:
            from faster_whisper import WhisperModel
        except Exception as exc:  # ImportError or any transitive failure
            raise RuntimeError(
                "The 'faster-whisper' speech-to-text backend is not available "
                f"({exc}). Install the optional voice extra with:\n"
                "    pip install 'yalp[voice]'\n"
                "or choose a different backend (e.g. STT_BACKEND='fake')."
            ) from exc
        # CPU + int8 keeps it laptop-friendly with no GPU / no NPU.
        self._model = WhisperModel(
            self.model_size, device="cpu", compute_type="int8"
        )
        return self._model

    def transcribe(self, wav_bytes: bytes) -> str:
        model = self._ensure_model()
        # faster-whisper accepts a file path or a binary stream. Prefer an
        # in-memory stream; fall back to a temp file if the stream is rejected.
        try:
            segments, _info = model.transcribe(io.BytesIO(wav_bytes))
        except Exception:  # pragma: no cover - depends on installed lib version
            with tempfile.NamedTemporaryFile(suffix=".wav") as tmp:
                tmp.write(wav_bytes)
                tmp.flush()
                segments, _info = model.transcribe(tmp.name)
        parts: List[str] = [seg.text for seg in segments]
        return "".join(parts).strip()


# --- deterministic test fake (no third-party deps) --------------------------
class FakeSTTBackend(STTBackend):
    """A scripted, dependency-free STT backend for tests.

    Returns ``transcript`` for every call and records each received WAV payload
    on :attr:`calls`, so tests can assert what bytes were handed to the STT
    layer without any model, network, or microphone.
    """

    def __init__(self, transcript: str = "follow me") -> None:
        self.transcript = transcript
        self.calls: List[bytes] = []

    def transcribe(self, wav_bytes: bytes) -> str:
        self.calls.append(wav_bytes)
        return self.transcript


# --- factory + convenience --------------------------------------------------
def get_backend(name: Optional[str] = None) -> STTBackend:
    """Return an STT backend by name (default :data:`yalp.config.STT_BACKEND`).

    ``'faster-whisper'`` -> :class:`FasterWhisperBackend`; ``'fake'`` ->
    :class:`FakeSTTBackend`. An unknown name raises a clear :class:`ValueError`.
    """
    resolved = (name or config.STT_BACKEND or "").strip().lower()
    if resolved == "faster-whisper":
        return FasterWhisperBackend()
    if resolved == "fake":
        return FakeSTTBackend()
    raise ValueError(
        f"unknown STT backend {resolved!r}; expected 'faster-whisper' or 'fake'"
    )


def transcribe(wav_bytes: bytes, *, backend: Optional[str] = None) -> str:
    """Transcribe WAV bytes using the named backend (default from config).

    Resolves the backend via :func:`get_backend` and returns its transcription.
    If the chosen heavy backend's dependencies are missing, a clear, actionable
    :class:`RuntimeError` is raised (never a bare ``ImportError``).
    """
    return get_backend(backend).transcribe(wav_bytes)


__all__ = [
    "STTBackend",
    "FasterWhisperBackend",
    "FakeSTTBackend",
    "get_backend",
    "transcribe",
]
