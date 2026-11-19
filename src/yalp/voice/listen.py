"""Continuous listening — turn a live audio stream into commands.

``yalp agent --listen`` records one fixed 5 s clip. This module is the hands-free
replacement: it keeps listening and decides, block by block, what to do with the
audio. It has no agent or robot imports; ``yalp listen`` (:mod:`yalp.listen_cli`)
wires it to both.

Two listening modes, because of what the camera microphone can and cannot do on
the robot (measured 2026-11-18, ``docs/technical/status.md``):

* **Stationary** — the room is quiet enough to understand a full sentence.
  :class:`UtteranceSegmenter` cuts the stream into utterances by loudness (speech
  starts when the level rises above the background, ends after a pause), and each
  one is transcribed. A sentence only counts as a command if it contains a wake
  word (:func:`extract_command`), so ordinary conversation in the room does not
  drive the robot.
* **Moving or busy** — motor noise is louder than speech, so sentences come out
  garbled, but the single word "stop" still gets through. :class:`StopWindow`
  hands out short overlapping windows and the loop only checks each for a stop
  word. No wake word is needed to stop.

Audio arrives as an iterable of mono float32 blocks (:data:`BLOCK_SECONDS` long).
:class:`LiveAudioSource` reads the microphone; :func:`wav_blocks` and
:func:`array_blocks` replay recorded audio for tests and ``yalp listen --file``.
"""

from __future__ import annotations

import collections
import logging
import queue
import re
import threading
import time
import wave
from typing import Callable, Deque, Iterable, Iterator, Optional, Sequence

import numpy as np

from .. import config

logger = logging.getLogger("yalp.voice.listen")

# One audio block. Short enough to end an utterance promptly, long enough that a
# block's loudness is a stable number.
BLOCK_SECONDS = 0.1

# Words that stop the robot. Matched as whole words anywhere in a transcript.
STOP_WORDS = ("stop", "halt")

# What the speech model emits for noise or silence; never a command.
_NOISE_TRANSCRIPTS = {"", "you", "thank you", "thanks for watching", "bye", "so"}


def _rms(block: np.ndarray) -> float:
    if block.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(block, dtype=np.float64))))


# --------------------------------------------------------------------------- #
# Text helpers
# --------------------------------------------------------------------------- #
def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", (text or "").lower())


def is_stop(text: str) -> bool:
    """True if ``text`` contains a stop word as a whole word."""
    return any(word in STOP_WORDS for word in _words(text))


def is_noise_transcript(text: str) -> bool:
    """True for the empty / filler output the speech model gives for non-speech."""
    cleaned = " ".join(_words(text))
    return cleaned in _NOISE_TRANSCRIPTS or (text or "").strip().startswith(("[", "("))


def wake_prompt(wake_words: Sequence[str]) -> Optional[str]:
    """Text to prime the speech model with so it spells the wake words as given.

    Measured 2026-11-18 (tiny.en, 20 synthetic "Izzy, ..." clips in five voices):
    unprimed, the name came out right once ("IZ", "Aussie", "easy", "I see");
    primed with this text, 20 of 20. Noise and "stop" were unaffected.
    """
    names = [w.strip().capitalize() for w in wake_words if w and w.strip()]
    if not names:
        return None
    return " ".join(f"{n}, turn left. {n}, what do you see?" for n in names)


def extract_command(text: str, wake_words: Sequence[str]) -> Optional[str]:
    """Return the command in ``text``, or ``None`` if it is not addressed to us.

    With no wake words configured, every transcript is a command. Otherwise the
    transcript must contain one; the command is what follows it ("Izzy, turn
    left" → "turn left"), or what precedes it when nothing follows ("turn left,
    Izzy"). A bare wake word returns ``None``.
    """
    stripped = (text or "").strip()
    if not stripped:
        return None
    wakes = [w.strip().lower() for w in wake_words if w and w.strip()]
    if not wakes:
        return stripped
    for match in re.finditer(r"[A-Za-z0-9']+", stripped):
        if match.group(0).lower() in wakes:
            after = stripped[match.end():].strip(" ,.!?:;-")
            before = stripped[: match.start()].strip(" ,.!?:;-")
            return after or before or None
    return None


_TURN = re.compile(
    r"^(?:please )?(?:turn|spin|rotate)(?: to(?: the)?| the)? (left|right|around)"
    r"(?: (?:by )?(\d+(?:\.\d+)?)(?: degrees?)?)?(?: please)?$"
)
_DRIVE = re.compile(
    r"^(?:please )?(?:go|drive|move|come|back)(?: up)? ?"
    r"(forward|forwards|ahead|straight|back|backward|backwards)?"
    r"(?: (?:by )?(\d+(?:\.\d+)?|half a|a|one|two|three) ?"
    r"(m|meter|meters|metre|metres|cm|centimeters|centimetres|foot|feet))?(?: please)?$"
)
_UNITS = {"m": 1.0, "meter": 1.0, "meters": 1.0, "metre": 1.0, "metres": 1.0,
          "cm": 0.01, "centimeters": 0.01, "centimetres": 0.01, "foot": 0.3048, "feet": 0.3048}
_AMOUNTS = {"half a": 0.5, "a": 1.0, "one": 1.0, "two": 2.0, "three": 3.0}
DEFAULT_TURN_DEG = 90.0
DEFAULT_DRIVE_M = 0.5
MAX_SIMPLE_DRIVE_M = 2.0


def parse_simple_command(text: str) -> Optional[tuple[str, dict, str]]:
    """Recognise a plain motion command without asking the language model.

    Returns ``(ability, params, description)`` — e.g. ``("turn", {"angle_deg":
    90.0}, "Turning left 90°")`` — or ``None`` for anything else, which then goes
    to the agent. Only whole-sentence matches count: "turn left and tell me what
    you see" is not a simple command.
    """
    cleaned = " ".join(re.findall(r"[a-z0-9.']+", (text or "").lower())).strip(". ")
    match = _TURN.match(cleaned)
    if match:
        direction, amount = match.group(1), match.group(2)
        degrees = float(amount) if amount else (180.0 if direction == "around" else DEFAULT_TURN_DEG)
        degrees = min(degrees, 360.0)
        signed = -degrees if direction == "right" else degrees
        label = "around" if direction == "around" and not amount else f"{direction} {degrees:g}°"
        return "turn", {"angle_deg": signed}, f"Turning {label}"
    match = _DRIVE.match(cleaned)
    if match and (match.group(1) or cleaned.startswith("back")):
        direction, amount, unit = match.group(1), match.group(2), match.group(3)
        metres = DEFAULT_DRIVE_M
        if amount and unit:
            metres = _AMOUNTS.get(amount) or float(amount)
            metres *= _UNITS[unit]
        metres = min(metres, MAX_SIMPLE_DRIVE_M)
        backward = cleaned.startswith("back") or (direction or "").startswith("back")
        word = "back" if backward else "forward"
        return "drive", {"distance_m": -metres if backward else metres}, f"Driving {word} {metres:g} m"
    if cleaned in ("follow me", "please follow me", "follow me please"):
        return "enter_follow_mode", {}, "Following you"
    return None


# --------------------------------------------------------------------------- #
# Stationary mode: cut the stream into utterances
# --------------------------------------------------------------------------- #
class UtteranceSegmenter:
    """Cut a block stream into utterances by loudness.

    The background level is a low percentile of the last few seconds of block
    loudness, so it follows the room (or a fan) but is not dragged up by speech,
    which always has gaps. Speech starts when ``start_blocks`` of the last
    ``start_span`` blocks are louder than ``ratio`` × background (and above
    ``min_rms``); it ends after ``end_silence_s`` with no loud block, or at
    ``max_utterance_s``. :meth:`feed` returns the finished utterance (with a
    short lead-in so the first syllable is not clipped) or ``None``.

    The defaults are tuned to the Logitech C270 on the robot (2026-11-18), where
    speech from 1–2 m is only 1.5–3× the quiet-room level.
    """

    def __init__(
        self,
        sample_rate: int = config.VOICE_SAMPLE_RATE,
        *,
        ratio: float = 1.35,
        min_rms: float = 0.004,
        start_blocks: int = 3,
        start_span: int = 5,
        noise_window_s: float = 5.0,
        noise_percentile: float = 20.0,
        end_silence_s: float = 0.6,
        max_utterance_s: float = 12.0,
        min_speech_s: float = 0.3,
        pre_roll_s: float = 0.4,
    ) -> None:
        self.sample_rate = int(sample_rate)
        self.ratio = float(ratio)
        self.min_rms = float(min_rms)
        self.start_blocks = int(start_blocks)
        self.start_span = max(int(start_span), int(start_blocks))
        self._noise_blocks = max(10, int(round(noise_window_s / BLOCK_SECONDS)))
        self._noise_percentile = float(noise_percentile)
        self.end_silence_s = float(end_silence_s)
        self.max_utterance_s = float(max_utterance_s)
        self.min_speech_s = float(min_speech_s)
        self._pre_roll_s = float(pre_roll_s)
        self.reset()

    def reset(self) -> None:
        """Forget everything, including the learned background level."""
        self._levels: Deque[float] = collections.deque(maxlen=self._noise_blocks)
        self._noise: Optional[float] = None
        self._pre: Deque[np.ndarray] = collections.deque()
        self._pre_samples = 0
        self._recent_loud: Deque[bool] = collections.deque(maxlen=self.start_span)
        self._in_speech = False
        self._blocks: list[np.ndarray] = []
        self._speech_s = 0.0
        self._silence_s = 0.0
        self._total_s = 0.0

    @property
    def in_speech(self) -> bool:
        return self._in_speech

    @property
    def threshold(self) -> float:
        """Loudness a block must exceed to count as speech right now."""
        return max(self.min_rms, (self._noise or 0.0) * self.ratio)

    def feed(self, block: np.ndarray) -> Optional[np.ndarray]:
        level = _rms(block)
        seconds = block.size / self.sample_rate
        self._levels.append(level)
        if len(self._levels) < 10:
            # Not enough history to know the background yet (about one second).
            self._pre.append(block)
            self._pre_samples += block.size
            return None
        self._noise = float(np.percentile(self._levels, self._noise_percentile))
        loud = level > self.threshold

        if not self._in_speech:
            self._pre.append(block)
            self._pre_samples += block.size
            while self._pre_samples - self._pre[0].size >= self._pre_roll_s * self.sample_rate:
                self._pre_samples -= self._pre.popleft().size
            self._recent_loud.append(loud)
            if sum(self._recent_loud) >= self.start_blocks:
                self._in_speech = True
                self._blocks = list(self._pre)
                self._speech_s = sum(self._recent_loud) * seconds
                self._silence_s = 0.0
                self._total_s = sum(b.size for b in self._blocks) / self.sample_rate
                self._pre.clear()
                self._pre_samples = 0
                self._recent_loud.clear()
            return None

        self._blocks.append(block)
        self._total_s += seconds
        if loud:
            self._speech_s += seconds
            self._silence_s = 0.0
        else:
            self._silence_s += seconds

        hit_max = self._total_s >= self.max_utterance_s
        if self._silence_s < self.end_silence_s and not hit_max:
            return None

        utterance = np.concatenate(self._blocks)
        enough = self._speech_s >= self.min_speech_s
        self._in_speech = False
        self._blocks = []
        return utterance if enough else None


# --------------------------------------------------------------------------- #
# Moving mode: short overlapping windows, checked only for "stop"
# --------------------------------------------------------------------------- #
class StopWindow:
    """Hand out the newest ``window_s`` of audio every ``hop_s``."""

    def __init__(
        self,
        sample_rate: int = config.VOICE_SAMPLE_RATE,
        *,
        window_s: float = 2.0,
        hop_s: float = 1.0,
    ) -> None:
        self.sample_rate = int(sample_rate)
        self._window = int(window_s * self.sample_rate)
        self._hop = int(hop_s * self.sample_rate)
        self.reset()

    def reset(self) -> None:
        self._buffer = np.zeros(0, dtype=np.float32)
        self._since = 0

    def feed(self, block: np.ndarray) -> Optional[np.ndarray]:
        self._buffer = np.concatenate([self._buffer, block])[-self._window:]
        self._since += block.size
        if self._since >= self._hop and self._buffer.size >= self._hop:
            self._since = 0
            return self._buffer.copy()
        return None


# --------------------------------------------------------------------------- #
# Audio sources
# --------------------------------------------------------------------------- #
def array_blocks(
    audio: np.ndarray, sample_rate: int = config.VOICE_SAMPLE_RATE
) -> Iterator[np.ndarray]:
    """Yield a mono float32 array as consecutive blocks."""
    size = max(1, int(round(BLOCK_SECONDS * sample_rate)))
    audio = np.asarray(audio, dtype=np.float32)
    for start in range(0, audio.size, size):
        yield audio[start : start + size]


def read_wav(path: str) -> tuple[np.ndarray, int]:
    """Read a 16-bit PCM WAV as (mono float32 samples, sample rate)."""
    with wave.open(path, "rb") as wav:
        if wav.getsampwidth() != 2:
            raise ValueError(f"{path}: only 16-bit PCM WAV is supported")
        channels = wav.getnchannels()
        rate = wav.getframerate()
        raw = wav.readframes(wav.getnframes())
    data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)
    return np.ascontiguousarray(data, dtype=np.float32), rate


def wav_blocks(path: str) -> tuple[Iterator[np.ndarray], int]:
    """Blocks of a WAV file, plus its sample rate."""
    audio, rate = read_wav(path)
    return array_blocks(audio, rate), rate


class LiveAudioSource:
    """Microphone blocks from ``sounddevice``, as an iterable.

    Opens one input stream and queues every block it delivers. Iterating yields
    blocks forever (until :meth:`close`). :meth:`backlog` says how many blocks
    are waiting, so the loop can tell when it has fallen behind real time.
    """

    def __init__(
        self,
        *,
        sample_rate: int = config.VOICE_SAMPLE_RATE,
        device: Optional[object] = config.AUDIO_INPUT_DEVICE,
    ) -> None:
        self.sample_rate = int(sample_rate)
        self._device = device
        self._queue: "queue.Queue[np.ndarray]" = queue.Queue()
        self._stream = None
        self._closed = threading.Event()

    def open(self) -> "LiveAudioSource":
        import sounddevice as sd  # lazy: needs PortAudio

        from .microphone import _resolve_input_device

        def callback(indata, _frames, _time, status) -> None:
            if status:
                logger.debug("audio stream status: %s", status)
            # Copy: PortAudio reuses ``indata`` for the next block, and a
            # one-channel column slice is a view, not a copy.
            self._queue.put(np.array(indata[:, 0], dtype=np.float32, copy=True))

        self._stream = sd.InputStream(
            samplerate=self.sample_rate,
            channels=1,
            dtype="float32",
            blocksize=max(1, int(round(BLOCK_SECONDS * self.sample_rate))),
            device=_resolve_input_device(self._device, sd),
            callback=callback,
        )
        self._stream.start()
        return self

    def close(self) -> None:
        self._closed.set()
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception as exc:  # noqa: BLE001 - best-effort teardown
                logger.debug("audio stream close failed: %s", exc)
            self._stream = None

    def __enter__(self) -> "LiveAudioSource":
        return self.open()

    def __exit__(self, *_exc) -> None:
        self.close()

    def backlog(self) -> int:
        return self._queue.qsize()

    def __iter__(self) -> Iterator[np.ndarray]:
        while not self._closed.is_set():
            try:
                yield self._queue.get(timeout=0.5)
            except queue.Empty:
                continue


# --------------------------------------------------------------------------- #
# The loop
# --------------------------------------------------------------------------- #
class VoiceLoop:
    """Drive the two listening modes from one block stream.

    Everything outside audio is injected, so this is testable with no microphone,
    model, agent or robot:

    * ``transcribe(samples) -> str`` — speech to text for a mono float32 array.
    * ``on_command(text)`` — run one command to completion (blocking). Called on a
      worker thread, one command at a time; audio heard meanwhile is only checked
      for "stop".
    * ``on_stop()`` — stop the robot now and end the running command.
    * ``is_moving() -> bool`` — whether the motors are (or may be) running.
    * ``on_heard(text, accepted)`` — optional, for showing what was understood.
    """

    def __init__(
        self,
        *,
        transcribe: Callable[[np.ndarray], str],
        on_command: Callable[[str], None],
        on_stop: Callable[[], None],
        is_moving: Callable[[], bool] = lambda: False,
        on_heard: Optional[Callable[[str, bool], None]] = None,
        wake_words: Sequence[str] = (),
        sample_rate: int = config.VOICE_SAMPLE_RATE,
        segmenter: Optional[UtteranceSegmenter] = None,
        stop_window: Optional[StopWindow] = None,
        moving_poll_s: float = 0.3,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._transcribe = transcribe
        self._on_command = on_command
        self._on_stop = on_stop
        self._is_moving = is_moving
        self._on_heard = on_heard
        self.wake_words = tuple(wake_words)
        self.segmenter = segmenter or UtteranceSegmenter(sample_rate)
        self.stop_window = stop_window or StopWindow(sample_rate)
        self._moving_poll_s = moving_poll_s
        self._clock = clock
        self._worker: Optional[threading.Thread] = None
        self._moving_cached = False
        self._moving_checked_at: Optional[float] = None
        self._guarded = False

    # -- state ---------------------------------------------------------------
    @property
    def busy(self) -> bool:
        """True while a command is running."""
        return self._worker is not None and self._worker.is_alive()

    def _moving(self) -> bool:
        now = self._clock()
        if (
            self._moving_checked_at is None
            or now - self._moving_checked_at >= self._moving_poll_s
        ):
            self._moving_checked_at = now
            try:
                self._moving_cached = bool(self._is_moving())
            except Exception as exc:  # noqa: BLE001 - a state hiccup must not end listening
                logger.debug("is_moving failed: %s", exc)
        return self._moving_cached

    def _safe_transcribe(self, samples: np.ndarray) -> str:
        try:
            return (self._transcribe(samples) or "").strip()
        except Exception as exc:  # noqa: BLE001 - one bad clip must not end listening
            logger.warning("transcription failed: %s: %s", type(exc).__name__, exc)
            return ""

    # -- the loop ------------------------------------------------------------
    def run(
        self,
        blocks: Iterable[np.ndarray],
        *,
        backlog: Callable[[], int] = lambda: 0,
        should_stop: Callable[[], bool] = lambda: False,
    ) -> None:
        """Consume ``blocks`` until they run out or ``should_stop`` is true."""
        for block in blocks:
            if should_stop():
                break
            guarded = self.busy or self._moving()
            if guarded != self._guarded:
                # Audio from the other mode is the wrong kind of audio: drop it.
                self._guarded = guarded
                self.segmenter.reset()
                self.stop_window.reset()
            if guarded:
                window = self.stop_window.feed(block)
                # Only transcribe when caught up with real time; a stale window
                # is a late stop.
                if window is not None and backlog() <= 1:
                    if is_stop(self._safe_transcribe(window)):
                        self._stop()
            else:
                utterance = self.segmenter.feed(block)
                if utterance is not None:
                    self._handle_utterance(self._safe_transcribe(utterance))
        self.wait()

    def wait(self, timeout: Optional[float] = None) -> None:
        """Block until the running command (if any) finishes."""
        if self._worker is not None:
            self._worker.join(timeout)

    def _stop(self) -> None:
        if self._on_heard is not None:
            self._on_heard("stop", True)
        try:
            self._on_stop()
        except Exception as exc:  # noqa: BLE001
            logger.warning("stop handler failed: %s: %s", type(exc).__name__, exc)
        # Re-read the motion state straight away rather than trusting the cache.
        self._moving_checked_at = None

    def _handle_utterance(self, text: str) -> None:
        if is_noise_transcript(text):
            logger.info("heard only noise: %r", text)
            return
        command = extract_command(text, self.wake_words)
        if command is None:
            if is_stop(text):  # "stop" never needs the wake word
                self._stop()
            elif self._on_heard is not None:
                self._on_heard(text, False)
            return
        if is_stop(command) and len(_words(command)) <= 3:
            self._stop()
            return
        if self._on_heard is not None:
            self._on_heard(command, True)
        self._worker = threading.Thread(
            target=self._run_command, args=(command,), name="yalp-voice-command", daemon=True
        )
        self._worker.start()

    def _run_command(self, command: str) -> None:
        try:
            self._on_command(command)
        except Exception as exc:  # noqa: BLE001 - one bad command must not end listening
            logger.warning("command failed: %s: %s", type(exc).__name__, exc)


__all__ = [
    "BLOCK_SECONDS",
    "STOP_WORDS",
    "LiveAudioSource",
    "StopWindow",
    "UtteranceSegmenter",
    "VoiceLoop",
    "array_blocks",
    "extract_command",
    "is_noise_transcript",
    "is_stop",
    "parse_simple_command",
    "read_wav",
    "wav_blocks",
]
