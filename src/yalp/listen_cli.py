"""``yalp listen`` — hands-free voice commands, replies by message.

Keeps the microphone open and runs each spoken command through the same agent
loop as ``yalp agent``. Replies print, and also go to Telegram when it is
configured (the robot has no speaker).

* While the robot is still, say the wake word and a command: "robot, turn left".
* While it is moving or working on a command, only "stop" is listened for; it
  halts the robot and ends the command. "stop" never needs the wake word.

The listening logic is in :mod:`yalp.voice.listen`; this module is the wiring.
Like ``yalp agent``, the body is the local fake by default; pass ``--host`` to
drive a real reactive server (``yalp session start`` on the Pi, then
``yalp listen --host 127.0.0.1``).
"""

from __future__ import annotations

import logging
import threading
from typing import Callable, Optional

from . import config
from .responder import KIND_ERROR, KIND_STATUS, CompositeResponder, ConsoleResponder

logger = logging.getLogger("yalp.listen")


def add_parser(subparsers) -> None:
    """Register the ``listen`` subcommand and its flags."""
    wake = ", ".join(config.WAKE_WORDS) or "(none — everything heard is a command)"
    parser = subparsers.add_parser(
        "listen",
        help="Keep listening for spoken commands; reply by message (hands-free).",
        description=(
            "Listen continuously on the microphone. While the robot is still, a "
            "sentence containing a wake word is run as a command; while it is "
            "moving, only 'stop' is listened for. Replies print and are sent to "
            "Telegram when it is configured. Wake words (YALP_WAKE_WORDS): "
            f"{wake}."
        ),
    )
    parser.add_argument(
        "--host",
        default=None,
        help="Drive a reactive server on this host instead of the local fake body.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help=f"Port of the reactive server (with --host; default {config.IPC_PORT}).",
    )
    parser.add_argument(
        "--synthetic",
        action="store_true",
        help="Use the synthetic camera test-pattern instead of the webcam.",
    )
    parser.add_argument(
        "--steps",
        type=int,
        default=12,
        metavar="N",
        help="Max deliberative steps per command (default: 12).",
    )
    parser.add_argument(
        "--file",
        default=None,
        metavar="WAV",
        help="Listen to a recorded 16 kHz WAV instead of the microphone, then exit.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only print what is heard; run no commands and move nothing.",
    )
    parser.add_argument(
        "--levels",
        action="store_true",
        help="Print the microphone level once a second (for tuning / debugging).",
    )
    parser.add_argument(
        "--save-dir",
        default=None,
        metavar="DIR",
        help="Save every clip sent to speech-to-text as a WAV in DIR (debugging).",
    )
    parser.set_defaults(handler=run)


class _LockedReactive:
    """Serialise access to the reactive link.

    The agent turn runs on a worker thread while the listening thread polls the
    robot's state and may send a stop; the underlying socket client is not safe
    for that, so every call goes through one lock.
    """

    def __init__(self, inner) -> None:
        self._inner = inner
        self._lock = threading.Lock()

    def send_intent(self, intent) -> None:
        with self._lock:
            self._inner.send_intent(intent)

    def request_state(self, timeout: float = 2.0):
        with self._lock:
            return self._inner.request_state(timeout=timeout)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _make_transcriber(sample_rate: int) -> Callable:
    """One speech-to-text backend, loaded once and reused for every clip."""
    from .voice.microphone import to_wav_bytes
    from .voice.stt import get_backend

    backend = get_backend()

    def transcribe(samples) -> str:
        return backend.transcribe(to_wav_bytes(samples, sample_rate))

    return transcribe


def _state_is_moving(state) -> bool:
    """True if the motors may be running: a drive/turn goal or FOLLOW is active.

    Decided by mode alone. The reactive layer reports ``goal_status="running"``
    in IDLE too (seen on the robot 2026-11-18), so the status is not a signal.
    """
    from .contract.messages import Mode

    return state is not None and state.mode in (Mode.DRIVE_GOAL, Mode.FOLLOW)


def _saving(transcribe: Callable, directory: str, sample_rate: int) -> Callable:
    """Wrap ``transcribe`` so each clip is also written to ``directory``."""
    import itertools
    import os

    from .voice.microphone import to_wav_bytes

    os.makedirs(directory, exist_ok=True)
    counter = itertools.count(1)

    def wrapped(samples) -> str:
        text = transcribe(samples)
        path = os.path.join(directory, f"clip-{next(counter):03d}.wav")
        with open(path, "wb") as handle:
            handle.write(to_wav_bytes(samples, sample_rate))
        print(f"    saved {path}: {text!r}", flush=True)
        return text

    return wrapped


def _open_audio(args):
    """Return ``(blocks, backlog, sample_rate, close)`` for the chosen source."""
    from .voice import listen

    if getattr(args, "file", None):
        blocks, rate = listen.wav_blocks(args.file)
        return blocks, (lambda: 0), rate, (lambda: None)
    source = listen.LiveAudioSource().open()
    return source, source.backlog, source.sample_rate, source.close


def _with_levels(blocks, loop):
    """Pass blocks through, printing loudest level / background / threshold each second."""
    from .voice.listen import _rms

    peak, count = 0.0, 0
    for block in blocks:
        peak = max(peak, _rms(block))
        count += 1
        if count >= 10:
            seg = loop.segmenter
            mode = "stop-only" if loop._guarded else ("SPEECH" if seg.in_speech else "idle")
            print(
                f"    level {peak:.4f}  background {seg._noise or 0:.4f}  "
                f"threshold {seg.threshold:.4f}  {mode}",
                flush=True,
            )
            peak, count = 0.0, 0
        yield block


def _print_heard(text: str, accepted: bool) -> None:
    if accepted:
        print(f"\n>>> [heard] {text}")
    else:
        print(f">>> [ignored — no wake word] {text}")


def run(args) -> int:
    """Handler for ``yalp listen``. Returns a process exit code."""
    from .voice import listen

    if getattr(args, "levels", False):
        logging.getLogger("yalp.voice.listen").setLevel(logging.INFO)
        logging.basicConfig(level=logging.INFO, format="    %(message)s")
    try:
        blocks, backlog, sample_rate, close_audio = _open_audio(args)
    except Exception as exc:  # noqa: BLE001 - no mic / bad file: say so, don't trace
        print(f"Could not open the audio source: {type(exc).__name__}: {exc}")
        print("Check `yalp audio --list`, or pass --file WAV to replay a recording.")
        return 2
    try:
        transcribe = _make_transcriber(sample_rate)
    except Exception as exc:  # noqa: BLE001
        close_audio()
        print(f"Speech-to-text is unavailable: {type(exc).__name__}: {exc}")
        print('Install it with: pip install -e ".[voice]"')
        return 2

    if getattr(args, "save_dir", None):
        transcribe = _saving(transcribe, args.save_dir, sample_rate)

    wake = ", ".join(config.WAKE_WORDS)

    if getattr(args, "dry_run", False):
        print("Dry run — printing what is heard; nothing is run. Ctrl-C to stop.")
        loop = listen.VoiceLoop(
            transcribe=transcribe,
            on_command=lambda _text: None,
            on_stop=lambda: None,
            on_heard=_print_heard,
            wake_words=config.WAKE_WORDS,
            sample_rate=sample_rate,
        )
        try:
            loop.run(
            _with_levels(blocks, loop) if getattr(args, "levels", False) else blocks,
            backlog=backlog,
        )
        except KeyboardInterrupt:
            print()
        finally:
            close_audio()
        return 0

    from .deliberative import vision

    if config.get_api_key() is None:
        close_audio()
        print(vision.MISSING_KEY_MESSAGE)
        return 0

    from . import telegram
    from .deliberative import agent_cli
    from .deliberative.agent import Agent, format_transcript
    from .deliberative.model_router import Budget
    from .telegram_cli import _CountingResponder

    if getattr(args, "host", None):
        reactive, describe, teardown = agent_cli._setup_remote(args, args.host)
        capture_still = agent_cli._make_capture_still(reactive)
    else:
        reactive, describe, backend, teardown = agent_cli._setup_local(args)
        capture_still = agent_cli._make_capture_still(backend)
    reactive = _LockedReactive(reactive)

    channels = [ConsoleResponder()]
    telegram_responder = telegram.responder_from_env()
    if telegram_responder is not None:
        channels.append(telegram_responder)
    else:
        print("Telegram is not configured — replies will only print here.")
    responder = _CountingResponder(
        channels[0] if len(channels) == 1 else CompositeResponder(*channels)
    )

    agent = Agent(
        client=None,
        reactive=reactive,
        describe_scene=describe,
        capture_still=capture_still,
        max_steps=args.steps,
        responder=responder,
    )

    def is_moving() -> bool:
        return _state_is_moving(reactive.request_state(timeout=0.3))

    def on_heard(text: str, accepted: bool) -> None:
        _print_heard(text, accepted)
        if accepted and text != "stop":
            responder.respond(f'Heard: "{text}"', kind=KIND_STATUS)

    def on_stop() -> None:
        agent.cancel()
        responder.respond("Stopped.")

    def on_command(command: str) -> None:
        agent.budget = Budget()  # the budget is per command, not per uptime
        before = responder.count
        try:
            print(format_transcript(agent.run_turn(command)))
        except Exception as exc:  # noqa: BLE001 - one bad command must not kill the loop
            logger.warning("command failed: %s: %s", type(exc).__name__, exc)
            responder.respond(f"That command failed ({type(exc).__name__}).", kind=KIND_ERROR)
            return
        if responder.count == before and not agent.cancelled:
            responder.respond("Done.")

    loop = listen.VoiceLoop(
        transcribe=transcribe,
        on_command=on_command,
        on_stop=on_stop,
        is_moving=is_moving,
        on_heard=on_heard,
        wake_words=config.WAKE_WORDS,
        sample_rate=sample_rate,
    )
    try:
        hint = f'Say "{config.WAKE_WORDS[0]}, <command>".' if wake else "Say a command."
        responder.respond(f"Listening. {hint} Say \"stop\" at any time.", kind=KIND_STATUS)
        loop.run(
            _with_levels(blocks, loop) if getattr(args, "levels", False) else blocks,
            backlog=backlog,
        )
    except KeyboardInterrupt:
        print()
    finally:
        close_audio()
        if loop.busy:
            agent.cancel()  # never leave a command driving after we stop listening
        loop.wait(timeout=5.0)
        teardown()
    return 0
