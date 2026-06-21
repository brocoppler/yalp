"""Tests for the threaded Microphone — no real mic, audio output, or SDK.

Mirrors tests/test_camera.py conventions: monkeypatching the module's optional
``sd`` (sounddevice) binding and small stand-ins, plus tiny WAV fixtures written
into ``tmp_path`` so the ``file`` source is exercised without any hardware.
"""

from __future__ import annotations

import math
import time
import wave

import numpy as np
import pytest

from yalp.voice import microphone as mic_mod
from yalp.voice.microphone import (
    DEFAULT_AUDIO_TIMEOUT,
    Microphone,
    to_wav_bytes,
)


def _write_sine_wav(path, *, sample_rate=16000, seconds=0.1, freq=440.0):
    """Write a short mono 16-bit sine WAV and return its sample count."""
    n = int(sample_rate * seconds)
    t = np.arange(n, dtype=np.float32) / float(sample_rate)
    pcm = (0.5 * np.sin(2.0 * math.pi * freq * t) * 32767.0).astype(np.int16)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm.tobytes())
    return n


# --- synthetic source -------------------------------------------------------
def test_synthetic_produces_float32_chunk_of_expected_length():
    sr, secs = 8000, 0.25
    mic = Microphone(source="synthetic", sample_rate=sr, record_seconds=secs)
    mic.start()
    try:
        chunk = mic.record_once(timeout=2.0)
    finally:
        mic.stop()

    assert chunk is not None
    assert chunk.dtype == np.float32
    assert chunk.ndim == 1
    assert chunk.shape[0] == int(sr * secs)


def test_synthetic_latest_returns_copy():
    mic = Microphone(source="synthetic", sample_rate=8000, record_seconds=0.1)
    mic.start()
    try:
        first = mic.record_once(timeout=2.0)
    finally:
        mic.stop()
    again = mic.latest()
    assert again is not None
    # A mutation of the returned array must not corrupt the internal buffer.
    again[:] = 0.0
    assert mic.latest() is not None


# --- microphone source falls back when sounddevice is absent ----------------
def test_microphone_falls_back_to_synthetic_when_sounddevice_missing(monkeypatch):
    monkeypatch.setattr(mic_mod, "sd", None)
    mic = Microphone(source="microphone", sample_rate=8000, record_seconds=0.1)
    mic.start()
    try:
        chunk = mic.record_once(timeout=2.0)
    finally:
        mic.stop()

    assert mic.source == "synthetic"
    assert chunk is not None
    assert chunk.dtype == np.float32


def test_microphone_falls_back_when_no_input_device(monkeypatch):
    # sounddevice present, but no device reports input channels.
    class FakeSd:
        @staticmethod
        def query_devices():
            return [{"name": "Speakers", "max_input_channels": 0}]

    monkeypatch.setattr(mic_mod, "sd", FakeSd())
    mic = Microphone(source="microphone", sample_rate=8000, record_seconds=0.1)
    mic.start()
    try:
        mic.record_once(timeout=2.0)
    finally:
        mic.stop()

    assert mic.source == "synthetic"


# --- file source ------------------------------------------------------------
def test_file_source_loads_wav_samples(tmp_path):
    wav_path = tmp_path / "tone.wav"
    n = _write_sine_wav(wav_path, sample_rate=16000, seconds=0.1)

    mic = Microphone(source="file", sample_rate=16000, path=str(wav_path))
    mic.start()
    try:
        chunk = mic.record_once(timeout=2.0)
    finally:
        mic.stop()

    assert mic.source == "file"
    assert chunk.dtype == np.float32
    assert chunk.shape[0] == n
    # Decoded a real (non-silent) tone.
    assert float(np.abs(chunk).max()) > 0.1


def test_file_source_falls_back_to_synthetic_when_missing(tmp_path):
    missing = tmp_path / "does_not_exist.wav"
    mic = Microphone(
        source="file", sample_rate=8000, record_seconds=0.1, path=str(missing)
    )
    mic.start()
    try:
        chunk = mic.record_once(timeout=2.0)
    finally:
        mic.stop()

    assert mic.source == "synthetic"
    assert chunk is not None


# --- to_wav_bytes round-trip ------------------------------------------------
def test_to_wav_bytes_round_trips_sample_count():
    sr = 16000
    n = 1600
    audio = (0.3 * np.sin(np.linspace(0, 8 * math.pi, n))).astype(np.float32)

    data = to_wav_bytes(audio, sr)

    import io

    with wave.open(io.BytesIO(data), "rb") as wav:
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
        assert wav.getframerate() == sr
        assert wav.getnframes() == n
        raw = wav.readframes(wav.getnframes())
    back = np.frombuffer(raw, dtype=np.int16)
    assert back.shape[0] == n


# --- lifecycle / context manager / non-blocking latest ----------------------
def test_latest_is_non_blocking_before_first_chunk():
    mic = Microphone(source="synthetic", sample_rate=8000, record_seconds=0.1)
    # Not started yet: latest must return immediately (None), never block.
    start = time.monotonic()
    assert mic.latest() is None
    assert time.monotonic() - start < 0.2


def test_context_manager_lifecycle():
    with Microphone(source="synthetic", sample_rate=8000, record_seconds=0.1) as mic:
        chunk = mic.record_once(timeout=2.0)
        assert chunk is not None
        assert mic._thread is not None
    # Exiting the context joined and cleared the capture thread.
    assert mic._thread is None


def test_record_once_times_out_when_no_audio(monkeypatch):
    mic = Microphone(source="synthetic", sample_rate=8000, record_seconds=0.1)

    # Force the capture loop to never publish, so record_once must time out.
    monkeypatch.setattr(mic, "_grab_one", lambda: None)
    mic.start()
    try:
        with pytest.raises(TimeoutError):
            mic.record_once(timeout=0.2)
    finally:
        mic.stop()


def test_default_audio_timeout_is_positive():
    assert DEFAULT_AUDIO_TIMEOUT > 0
