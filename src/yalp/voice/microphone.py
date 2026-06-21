"""Threaded microphone capture with a single-slot latest-chunk buffer.

This MIRRORS :mod:`yalp.camera`, but for AUDIO instead of video. The
reactive/voice process owns the microphone and the blocking record runs in its
own capture thread that does nothing but loop record -> write into a single-slot,
last-write-wins "latest chunk" buffer. Callers always pull the *newest* recorded
audio and never block on the device read — a non-blocking :meth:`latest` returns
whatever was last published (or ``None``), while :meth:`record_once` blocks
(push-to-talk style) until one chunk is ready.

For laptop-first development this is reusable and hardware-optional. Three
sources are supported so it runs with or without a real microphone:

  (a) ``microphone`` — live capture via ``sounddevice`` (PortAudio).
  (b) ``file``       — a WAV file path, decoded via the stdlib ``wave`` module
                       and looped as the "live" audio.
  (c) ``synthetic``  — numpy-generated audio when no mic / lib is available.

If a microphone (or ``sounddevice``) cannot be opened, the Microphone
automatically falls back to synthetic audio, so tests and dev never require real
hardware or audio I/O.

Audio is always handed back as a **mono float32 numpy array** (shape ``(n,)``)
sampled at ``sample_rate`` Hz, normalised to roughly ``[-1, 1]`` — the format the
downstream STT layer expects. :func:`to_wav_bytes` encodes such an array to
16-bit PCM WAV bytes for backends that consume WAV.
"""

from __future__ import annotations

import io
import logging
import threading
import time
import wave
from typing import Optional

import numpy as np

from yalp.config import (
    VOICE_AUDIO_FILE,
    VOICE_CHANNELS,
    VOICE_RECORD_SECONDS,
    VOICE_SAMPLE_RATE,
    VOICE_SOURCE,
)

logger = logging.getLogger(__name__)

try:  # sounddevice (PortAudio) is an OPTIONAL dep — keep failures legible.
    import sounddevice as sd
except Exception as exc:  # pragma: no cover - exercised only without sounddevice
    sd = None  # type: ignore[assignment]
    _SD_IMPORT_ERROR: Optional[Exception] = exc
else:
    _SD_IMPORT_ERROR = None

# Default capture parameters. 16 kHz mono is the Whisper-friendly default; these
# mirror the ``VOICE_*`` config defaults but are also exposed as module
# constants for callers/tests that want them directly.
DEFAULT_SAMPLE_RATE = 16000
DEFAULT_CHANNELS = 1
DEFAULT_RECORD_SECONDS = 5.0

# How long ``record_once`` blocks by default before giving up — generous enough
# to cover device-open plus a full ``record_seconds`` capture.
DEFAULT_AUDIO_TIMEOUT = 10.0

Audio = "np.ndarray"  # mono float32, shape (n,), sampled at sample_rate Hz.


class Microphone:
    """A latest-chunk microphone with a background capture thread.

    Parameters
    ----------
    source:
        ``"microphone"``, ``"file"``, or ``"synthetic"``.
    sample_rate:
        Capture/decode sample rate in Hz.
    channels:
        Capture channel count (audio is always collapsed to mono on publish).
    record_seconds:
        How many seconds of audio make up one captured chunk.
    path:
        WAV file path for the ``file`` source.
    device:
        Optional ``sounddevice`` input-device index/name (``None`` = default).
    """

    def __init__(
        self,
        source: str = VOICE_SOURCE,
        *,
        sample_rate: int = VOICE_SAMPLE_RATE,
        channels: int = VOICE_CHANNELS,
        record_seconds: float = VOICE_RECORD_SECONDS,
        path: Optional[str] = VOICE_AUDIO_FILE or None,
        device: Optional[object] = None,
    ) -> None:
        self.source = source
        self.sample_rate = int(sample_rate)
        self.channels = int(channels)
        self.record_seconds = float(record_seconds)
        self.path = path
        self.device = device

        self._file_audio: Optional[np.ndarray] = None  # the loaded/looped WAV samples
        self._file_pos = 0  # read cursor into the looped file audio
        self._latest: Optional[np.ndarray] = None  # single-slot latest chunk
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._chunk_count = 0

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> "Microphone":
        """Open the source and start the background capture thread.

        Falls back to synthetic audio if a microphone or WAV file cannot be
        opened. Returns ``self`` for convenient chaining.
        """
        if self._thread is not None and self._thread.is_alive():
            return self
        self._configure_source()
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._capture_loop, name="yalp-microphone", daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        """Stop the capture thread."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def __enter__(self) -> "Microphone":
        return self.start()

    def __exit__(self, *_exc) -> None:
        self.stop()

    # -- audio access --------------------------------------------------------
    def latest(self) -> Optional[np.ndarray]:
        """Return the newest recorded chunk (mono float32), or None if none yet.

        Never blocks on a device read — it returns whatever the capture thread
        last published.
        """
        with self._lock:
            return None if self._latest is None else self._latest.copy()

    def record_once(self, timeout: float = DEFAULT_AUDIO_TIMEOUT) -> np.ndarray:
        """Block until one chunk is available, then return it (push-to-talk).

        Starts the capture thread if it is not already running, then waits for
        the next recorded chunk. This is the convenience the CLI uses to "capture
        one utterance". Raises :class:`TimeoutError` if no chunk arrives in time.
        """
        if self._thread is None or not self._thread.is_alive():
            self.start()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            chunk = self.latest()
            if chunk is not None:
                return chunk
            time.sleep(0.01)
        chunk = self.latest()
        if chunk is None:
            raise TimeoutError(
                f"no audio captured within {timeout:.1f}s (source={self.source!r})"
            )
        return chunk

    # -- internals -----------------------------------------------------------
    def _configure_source(self) -> None:
        """Resolve the configured source, falling back to synthetic on failure."""
        if self.source == "microphone":
            if sd is None:
                logger.warning(
                    "sounddevice unavailable (%s) — falling back to synthetic audio",
                    _SD_IMPORT_ERROR,
                )
                self.source = "synthetic"
                return
            if not self._has_input_device():
                logger.warning(
                    "no audio input device available — falling back to synthetic audio"
                )
                self.source = "synthetic"
                return
        elif self.source == "file":
            audio = self._load_wav(self.path) if self.path else None
            if audio is None:
                logger.warning(
                    "could not read WAV %r — falling back to synthetic audio",
                    self.path,
                )
                self.source = "synthetic"
                return
            self._file_audio = audio
            self._file_pos = 0
        # "synthetic" needs no setup.

    def _has_input_device(self) -> bool:
        """True if ``sounddevice`` reports at least one usable input device."""
        if sd is None:  # pragma: no cover - guarded by caller
            return False
        try:
            devices = sd.query_devices()
        except Exception:  # pragma: no cover - PortAudio host error
            return False
        for dev in devices:
            try:
                if int(dev.get("max_input_channels", 0)) > 0:
                    return True
            except (AttributeError, TypeError, ValueError):
                continue
        return False

    def _load_wav(self, path: Optional[str]) -> Optional[np.ndarray]:
        """Load a WAV file into a mono float32 numpy array, or None on failure.

        Uses only the standard-library :mod:`wave` module plus numpy — no
        ``soundfile`` dependency. PCM is normalised to ``[-1, 1]`` and any
        multi-channel audio is averaged down to mono.
        """
        if not path:
            return None
        try:
            with wave.open(path, "rb") as wav:
                n_channels = wav.getnchannels()
                sample_width = wav.getsampwidth()
                n_frames = wav.getnframes()
                raw = wav.readframes(n_frames)
        except (FileNotFoundError, OSError, wave.Error):
            return None
        return self._pcm_bytes_to_mono_float32(raw, n_channels, sample_width)

    @staticmethod
    def _pcm_bytes_to_mono_float32(
        raw: bytes, n_channels: int, sample_width: int
    ) -> Optional[np.ndarray]:
        """Convert raw PCM bytes to a mono float32 array in [-1, 1]."""
        if sample_width == 2:
            data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        elif sample_width == 4:
            data = np.frombuffer(raw, dtype=np.int32).astype(np.float32) / 2147483648.0
        elif sample_width == 1:
            # 8-bit WAV is unsigned, centred at 128.
            data = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
        else:  # pragma: no cover - exotic widths
            return None
        if n_channels > 1:
            data = data.reshape(-1, n_channels).mean(axis=1)
        return np.ascontiguousarray(data, dtype=np.float32)

    def _capture_loop(self) -> None:
        # Capture one chunk per iteration into the single-slot latest buffer.
        while not self._stop.is_set():
            chunk = self._grab_one()
            if chunk is not None:
                with self._lock:
                    self._latest = chunk
                    self._chunk_count += 1
            # Pace politely so we don't busy-spin. Live mic capture already
            # blocks in ``sd.wait()`` for ~record_seconds; for the synthetic and
            # file sources (which return instantly) sleep a short slice so
            # ``stop()`` stays responsive but the thread does not spin hot.
            pace = 0.05 if self.source == "microphone" else min(self.record_seconds, 0.5)
            if self._stop.wait(pace):
                break

    def _frames_per_chunk(self) -> int:
        return max(1, int(round(self.record_seconds * self.sample_rate)))

    def _grab_one(self) -> Optional[np.ndarray]:
        if self.source == "microphone" and sd is not None:
            return self._record_live()
        if self.source == "file" and self._file_audio is not None:
            return self._next_file_chunk()
        return self._synthetic_chunk()

    def _record_live(self) -> Optional[np.ndarray]:  # pragma: no cover - needs PortAudio
        """Record one ``record_seconds`` chunk from the live mic (mono float32)."""
        frames = self._frames_per_chunk()
        try:
            rec = sd.rec(
                frames,
                samplerate=self.sample_rate,
                channels=self.channels,
                dtype="float32",
                device=self.device,
            )
            sd.wait()
        except Exception as exc:
            logger.warning("live audio capture failed (%s); using synthetic", exc)
            return self._synthetic_chunk()
        rec = np.asarray(rec, dtype=np.float32)
        if rec.ndim > 1:
            rec = rec.mean(axis=1)
        return np.ascontiguousarray(rec, dtype=np.float32)

    def _next_file_chunk(self) -> np.ndarray:
        """Return the next ``record_seconds`` slice of the looped file audio."""
        assert self._file_audio is not None
        frames = self._frames_per_chunk()
        audio = self._file_audio
        n = audio.shape[0]
        if n == 0:
            return np.zeros(frames, dtype=np.float32)
        out = np.empty(frames, dtype=np.float32)
        pos = self._file_pos
        filled = 0
        while filled < frames:
            take = min(frames - filled, n - pos)
            out[filled : filled + take] = audio[pos : pos + take]
            filled += take
            pos += take
            if pos >= n:
                pos = 0
        self._file_pos = pos
        return out

    def _synthetic_chunk(self) -> np.ndarray:
        """A deterministic-ish mono float32 chunk: low noise + a 440 Hz tone burst."""
        frames = self._frames_per_chunk()
        t = np.arange(frames, dtype=np.float32) / float(self.sample_rate)
        # Low-amplitude pseudo-random noise (seeded by the chunk index so it is
        # reproducible within a run but changes between chunks).
        rng = np.random.default_rng(1234 + self._chunk_count)
        noise = (rng.standard_normal(frames).astype(np.float32)) * 0.01
        # A 440 Hz tone burst over the first half of the chunk.
        tone = 0.2 * np.sin(2.0 * np.pi * 440.0 * t).astype(np.float32)
        burst = np.zeros(frames, dtype=np.float32)
        half = frames // 2
        burst[:half] = tone[:half]
        return np.ascontiguousarray(burst + noise, dtype=np.float32)


def to_wav_bytes(audio: np.ndarray, sample_rate: int) -> bytes:
    """Encode a mono float32 numpy array to 16-bit PCM WAV bytes.

    The STT layer consumes WAV bytes; this packs a ``(n,)`` float32 array in
    ``[-1, 1]`` (multi-channel input is averaged to mono; out-of-range values are
    clipped) into a single-channel, 16-bit-PCM WAV container using only the
    standard-library :mod:`wave` module plus :mod:`io`.
    """
    arr = np.asarray(audio, dtype=np.float32)
    if arr.ndim > 1:
        arr = arr.mean(axis=1)
    arr = arr.reshape(-1)
    clipped = np.clip(arr, -1.0, 1.0)
    pcm = (clipped * 32767.0).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)  # 16-bit PCM
        wav.setframerate(int(sample_rate))
        wav.writeframes(pcm.tobytes())
    return buf.getvalue()


__all__ = [
    "Microphone",
    "to_wav_bytes",
    "DEFAULT_SAMPLE_RATE",
    "DEFAULT_CHANNELS",
    "DEFAULT_RECORD_SECONDS",
    "DEFAULT_AUDIO_TIMEOUT",
]
