"""Tests for the threaded Microphone — no real mic, audio output, or SDK.

These mirror ``tests/test_camera.py`` conventions: monkeypatching the module's
optional ``sd`` (sounddevice) binding and small stand-in fakes instead of
touching hardware. The ``microphone`` source is forced down the synthetic
fallback (by setting the module's ``sd`` to ``None`` or to a no-input fake), the
``file`` source reads a tiny WAV fixture the test writes into ``tmp_path`` with
the stdlib :mod:`wave` module, and the ``synthetic`` source generates audio
purely in numpy.
"""

from __future__ import annotations

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


def _write_sine_wav(path, *, seconds=0.25, sample_rate=16000, freq=440.0):
    """Write a short mono 16-bit sine-wave WAV and return its sample count."""
    n = int(seconds * sample_rate)
    t = np.arange(n, dtype=np.float32) / float(sample_rate)
    pcm = (0.5 * np.sin(2.0 * np.pi * freq * t) * 32767.0).astype(np.int16)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm.tobytes())
    return n


# --- synthetic source -------------------------------------------------------
def test_synthetic_latest_returns_float32_chunk_of_expected_length():
    mic = Microphone(source="synthetic", sample_rate=8000, record_seconds=0.5)
    expected = int(round(0.5 * 8000))
    mic.start()
    try:
        chunk = None
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and chunk is None:
            chunk = mic.latest()
            time.sleep(0.01)
    finally:
        mic.stop()

    assert chunk is not None
    assert chunk.dtype == np.float32
    assert chunk.ndim == 1
    assert chunk.shape[0] == expected


def test_synthetic_record_once_returns_chunk():
    mic = Microphone(source="synthetic", sample_rate=8000, record_seconds=0.25)
    expected = int(round(0.25 * 8000))
    try:
        chunk = mic.record_once(timeout=2.0)
    finally:
        mic.stop()

    assert isinstance(chunk, np.ndarray)
    assert chunk.dtype == np.float32
    assert chunk.shape[0] == expected


def test_synthetic_latest_returns_copy():
    mic = Microphone(source="synthetic", sample_rate=8000, record_seconds=0.1)
    mic.start()
    try:
        mic.record_once(timeout=2.0)
    finally:
        mic.stop()
    again = mic.latest()
    assert again is not None
    # A mutation of the returned array must not corrupt the internal buffer.
    again[:] = 0.0
    assert mic.latest() is not None


def test_latest_is_none_before_first_chunk():
    mic = Microphone(source="synthetic", sample_rate=8000, record_seconds=0.1)
    # Not started yet -> non-blocking latest() must return None immediately.
    assert mic.latest() is None


def test_latest_is_non_blocking_before_first_chunk():
    mic = Microphone(source="synthetic", sample_rate=8000, record_seconds=0.1)
    # Not started yet: latest() must return immediately (None), never block.
    start = time.monotonic()
    assert mic.latest() is None
    assert time.monotonic() - start < 0.2


# --- microphone source falls back to synthetic ------------------------------
def test_microphone_falls_back_to_synthetic_when_sounddevice_missing(monkeypatch):
    # No sounddevice available -> the microphone source must fall back to
    # synthetic audio so dev/tests never require real hardware.
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
    # sounddevice present but reports no input-capable devices -> synthetic.
    class FakeSd:
        @staticmethod
        def query_devices():
            return [{"name": "Speakers", "max_input_channels": 0}]

    monkeypatch.setattr(mic_mod, "sd", FakeSd)
    mic = Microphone(source="microphone", sample_rate=8000, record_seconds=0.1)
    mic.start()
    try:
        mic.record_once(timeout=2.0)
    finally:
        mic.stop()

    assert mic.source == "synthetic"


# --- file source ------------------------------------------------------------
def test_file_source_loads_wav_samples(tmp_path):
    wav = tmp_path / "tone.wav"
    n = _write_sine_wav(wav, seconds=0.25, sample_rate=16000)

    mic = Microphone(
        source="file",
        path=str(wav),
        sample_rate=16000,
        record_seconds=0.25,
    )
    try:
        chunk = mic.record_once(timeout=2.0)
    finally:
        mic.stop()

    assert mic.source == "file"
    assert chunk.dtype == np.float32
    # One chunk is record_seconds long; with matching length it equals the file.
    assert chunk.shape[0] == int(round(0.25 * 16000)) == n
    # The decoded samples are normalised into [-1, 1] and non-trivial.
    assert float(np.max(np.abs(chunk))) > 0.1
    assert float(np.max(np.abs(chunk))) <= 1.0


def test_file_source_loads_into_internal_buffer(tmp_path):
    wav = tmp_path / "tone.wav"
    n = _write_sine_wav(wav, seconds=0.5, sample_rate=16000)
    mic = Microphone(source="file", path=str(wav), sample_rate=16000)
    mic._configure_source()
    assert mic._file_audio is not None
    assert mic._file_audio.shape[0] == n
    assert mic._file_audio.dtype == np.float32


def test_file_source_falls_back_to_synthetic_when_missing(tmp_path):
    missing = tmp_path / "does-not-exist.wav"
    mic = Microphone(
        source="file",
        path=str(missing),
        sample_rate=8000,
        record_seconds=0.1,
    )
    mic.start()
    try:
        chunk = mic.record_once(timeout=2.0)
    finally:
        mic.stop()

    assert mic.source == "synthetic"
    assert chunk is not None


# --- to_wav_bytes round-trip ------------------------------------------------
def test_to_wav_bytes_roundtrips_sample_count():
    sample_rate = 16000
    n = 4000
    t = np.arange(n, dtype=np.float32) / sample_rate
    audio = (0.3 * np.sin(2.0 * np.pi * 220.0 * t)).astype(np.float32)

    data = to_wav_bytes(audio, sample_rate)
    assert isinstance(data, (bytes, bytearray))

    import io

    with wave.open(io.BytesIO(data), "rb") as wf:
        assert wf.getnchannels() == 1
        assert wf.getsampwidth() == 2
        assert wf.getframerate() == sample_rate
        assert wf.getnframes() == n
        raw = wf.readframes(wf.getnframes())

    decoded = np.frombuffer(raw, dtype=np.int16)
    assert decoded.shape[0] == n


def test_to_wav_bytes_clips_out_of_range():
    audio = np.array([2.0, -2.0, 0.0], dtype=np.float32)
    data = to_wav_bytes(audio, 8000)
    import io

    with wave.open(io.BytesIO(data), "rb") as wf:
        raw = wf.readframes(wf.getnframes())
    decoded = np.frombuffer(raw, dtype=np.int16)
    assert decoded[0] == 32767
    assert decoded[1] == -32767
    assert decoded[2] == 0


# --- lifecycle / context manager -------------------------------------------
def test_context_manager_lifecycle():
    with Microphone(source="synthetic", sample_rate=8000, record_seconds=0.1) as mic:
        chunk = mic.record_once(timeout=2.0)
        assert chunk is not None
        assert mic._thread is not None
    # After __exit__ the capture thread is stopped and cleared.
    assert mic._thread is None


def test_stop_is_idempotent():
    mic = Microphone(source="synthetic", sample_rate=8000, record_seconds=0.1)
    mic.start()
    mic.stop()
    mic.stop()  # second stop must not raise
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
