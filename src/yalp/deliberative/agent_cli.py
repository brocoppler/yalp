"""``yalp agent`` — drive the deliberative agent loop against the fake backend.

Registered with the CLI via the documented feature-module contract: this module
exposes ``add_parser(subparsers)`` and ``run(args) -> int`` and is listed in
``yalp.cli.FEATURE_MODULES``.

    yalp agent drive forward a bit and tell me what you see
    yalp agent "drive forward a bit and tell me what you see"
    yalp agent --command "drive forward a bit and tell me what you see"
    yalp agent --steps 6 --command "explore the room and report"
    yalp agent --synthetic "look around"  # force the synthetic test pattern
    yalp agent                         # interactive prompt loop (real webcam)

Vision (REAL EYES + FAKE WHEELS): the body is simulated but, by default, the
agent's ``describe_scene`` sees through the REAL webcam — the reactive backend
owns one camera per run (webcam with an automatic synthetic fallback) and
``describe_scene`` reads frames from that same camera. ``--synthetic`` forces the
synthetic source for a no-camera demo / reproducible runs.

It spins up a :class:`~yalp.reactive.fake_backend.FakeReactiveBackend` behind a
:class:`~yalp.contract.ipc.ReactiveServer` on a background thread (the simulated
reactive process), connects a :class:`~yalp.contract.ipc.DeliberativeClient`, and
runs the :class:`~yalp.deliberative.agent.Agent` — exactly the laptop-first
topology from the spec (two processes, one socket). With no API key it prints the
same friendly SETUP.md pointer as ``yalp see`` instead of crashing.
"""

from __future__ import annotations

import threading
from typing import Optional

from .. import config


def add_parser(subparsers) -> None:
    """Register the ``agent`` subcommand and its flags."""
    parser = subparsers.add_parser(
        "agent",
        help="Run the Claude agent loop driving the (fake) reactive robot.",
        description=(
            "Start a fake reactive backend + server on a thread, connect a "
            "deliberative client, and run the agent loop on a command (or an "
            "interactive prompt). Prints the model / tool / robot-state transcript. "
            "REAL EYES + FAKE WHEELS: the body is simulated but vision comes from "
            "the real webcam (auto-falling back to synthetic). Pass --synthetic to "
            "force the synthetic camera test-pattern."
        ),
    )
    parser.add_argument(
        "words",
        nargs="*",
        help=(
            "Optional command as trailing words, e.g. "
            "'yalp agent drive forward and report'. "
            "Takes precedence over --command when both are given."
        ),
    )
    parser.add_argument(
        "--command",
        metavar="TEXT",
        default=None,
        help="A single natural-language command to run, then exit (alias for positional words).",
    )
    parser.add_argument(
        "--steps",
        type=int,
        default=12,
        metavar="N",
        help="Max deliberative steps per command (default: 12).",
    )
    parser.add_argument(
        "--synthetic",
        action="store_true",
        help=(
            "Force the synthetic camera test-pattern instead of the real webcam "
            "(useful for a no-camera demo / reproducible runs). Default: REAL "
            "webcam, auto-falling back to synthetic when no camera can be opened."
        ),
    )
    parser.add_argument(
        "--speak",
        action="store_true",
        help=(
            "Let the robot SPEAK its words out loud — its narration, scene "
            "descriptions, the 'speak' tool, and the final report (macOS 'say'; "
            "silent no-op without it). The printed transcript is unchanged; voice "
            "is additive. Default off so nothing makes surprise noise."
        ),
    )
    parser.add_argument(
        "--listen",
        action="store_true",
        help=(
            "Capture ONE spoken command via the microphone (push-to-talk: records "
            "~VOICE_RECORD_SECONDS seconds), transcribe it locally, and run the "
            "transcript through the agent. Only used when no positional words / "
            "--command are given (a typed command always wins). Best-effort and "
            "silent-friendly: it auto-falls back to synthetic/file audio for "
            "dev/CI and never crashes the CLI if capture or STT fails."
        ),
    )
    parser.add_argument(
        "--preview",
        dest="preview",
        action="store_true",
        default=None,
        help=(
            "When a command ends in FOLLOW mode ('follow me'), open a live OpenCV "
            "preview window and follow until Ctrl-C. Default: AUTO — preview ON "
            "when stdout is a TTY and a cv2 GUI is available, OFF on headless "
            "(where it prints readable status lines instead)."
        ),
    )
    parser.add_argument(
        "--no-preview",
        dest="preview",
        action="store_false",
        help=(
            "Force the FOLLOW tail to print status lines instead of opening a "
            "preview window (still follows until Ctrl-C). Headless-safe."
        ),
    )
    parser.add_argument(
        "--follow-seconds",
        type=float,
        default=None,
        metavar="N",
        help=(
            "Cap a FOLLOW tail at N seconds (default: follow until Ctrl-C). Only "
            "applies when a command ends in FOLLOW mode."
        ),
    )
    parser.add_argument(
        "--follow-detector",
        choices=("face", "hog", "person", "auto"),
        default=None,
        help=(
            "Person detector for 'follow me': 'person' (DEFAULT for the agent/voice "
            "follow path) — cv2.dnn MobileNet-SSD, ROOM-RANGE and ORIENTATION-AGNOSTIC "
            "(tracks from any angle, so follow keeps working when you stand across the "
            "room / walk away); 'face' — desk-only bundled Haar face cascade "
            "(head+shoulders framing); 'hog' — OpenCV's built-in standing-body detector; "
            "'auto' — person, falling back to face for close-ups. Overrides the "
            "YALP_FOLLOW_DETECTOR environment variable when given."
        ),
    )
    parser.set_defaults(handler=run)


def run(args) -> int:
    """Handler for ``yalp agent``. Returns a process exit code."""
    # Friendly degrade with no key, exactly like ``yalp see`` — never crash.
    from . import vision

    if config.get_api_key() is None:
        print(vision.MISSING_KEY_MESSAGE)
        return 0

    # Heavy imports are local so importing this module stays light.
    from ..contract.ipc import DeliberativeClient, ReactiveServer
    from .agent import Agent, format_transcript

    # REAL EYES + FAKE WHEELS: the reactive layer OWNS one camera for this run.
    # Default is the real webcam (auto-falling back to synthetic); --synthetic
    # forces the synthetic test-pattern. describe_scene reads frames from THIS
    # same shared camera (see _make_describe), so the device is opened only once.
    backend = _make_backend(
        synthetic=bool(getattr(args, "synthetic", False)),
        detector=getattr(args, "follow_detector", None),
    )
    server = ReactiveServer(host="127.0.0.1", port=0, mailbox=backend.mailbox)
    server.start()
    stop = threading.Event()
    runner = threading.Thread(
        target=backend.run,
        kwargs={"server": server, "stop_event": stop},
        daemon=True,
    )
    runner.start()

    client = DeliberativeClient("127.0.0.1", server.port)
    client.connect()
    server.wait_for_client(2.0)

    describe = _make_describe(backend)
    # Spoken OUTPUT (default OFF): thread voice.speak into the agent only when
    # --speak is passed, so the robot vocalizes its narration / scene
    # descriptions / speak tool / final report (mirrors how `yalp see` wires
    # voice). Headless-safe — voice.speak never raises and no-ops without a 'say'
    # binary.
    speak_enabled = bool(getattr(args, "speak", False))
    speak_fn = None
    if speak_enabled:
        from .. import voice

        speak_fn = voice.speak

    agent = Agent(
        client=None,  # real LLM client built lazily by llm.call_with_tools
        reactive=client,
        describe_scene=describe,
        max_steps=args.steps,
        speak=speak_fn,
    )

    # Resolve the command: positional words take precedence over --command.
    command = " ".join(getattr(args, "words", []) or []).strip() or args.command or None

    # Spoken INPUT (default OFF): with --listen and NO typed command, capture one
    # spoken utterance and transcribe it, then feed the transcript through the
    # SAME path into Agent.run_turn (a typed command always wins over the mic).
    # Best-effort: _listen_for_command never raises and returns None on failure,
    # so an empty result simply falls through to the interactive loop as before.
    if not command and getattr(args, "listen", False):
        command = _listen_for_command()

    try:
        if command:
            _run_one(agent, command, format_transcript)
        else:
            _interactive(agent, format_transcript)
        # STAY-AND-FOLLOW: if the run ended in FOLLOW mode ("follow me", typed or
        # via --listen), don't exit a heartbeat later — bring up the live camera
        # loop on the EXISTING backend (it is still ticking on its thread) and
        # follow until the user stops it. Best-effort; never crashes the CLI.
        _maybe_follow_tail(backend, args)
    finally:
        # Voice is fire-and-forget, so drain any outstanding speech (the final
        # report) before we exit — otherwise the last utterance is cut off the
        # instant the process tears down. Bounded + best-effort; never raises.
        if speak_enabled:
            from .. import voice

            voice.wait_for_speech()
        stop.set()
        runner.join(timeout=2.0)
        client.close()
        server.stop()
    return 0


def _resolve_preview(args) -> bool:
    """Decide whether the FOLLOW tail shows a preview window.

    ``--preview`` / ``--no-preview`` force it; the default (``None``) is AUTO:
    preview ON when stdout is a TTY *and* a cv2 GUI is actually available, OFF on
    headless (where readable status lines print instead). GUI is probed lazily so
    headless runs / tests never need a display.
    """
    pref = getattr(args, "preview", None)
    if pref is not None:
        return bool(pref)
    import sys

    from ..reactive.follow_runner import gui_available

    try:
        is_tty = bool(sys.stdout.isatty())
    except Exception:
        is_tty = False
    return is_tty and gui_available()


def _maybe_follow_tail(backend, args) -> None:
    """If the run ended in FOLLOW mode, enter the live follow loop; else no-op.

    Reuses the EXISTING backend (already ticking on its background thread), so we
    pass ``owns_ticking=False`` — the loop only renders / monitors the published
    state and must NOT tick again (double-ticking would corrupt the simulation).
    Best-effort and never-crash, mirroring the rest of the CLI: any error logs a
    warning and returns so teardown still runs cleanly.
    """
    from ..contract.messages import Mode

    try:
        state = backend.get_state()
    except Exception:  # pragma: no cover - defensive
        return
    if getattr(state, "mode", None) != Mode.FOLLOW:
        return

    preview = _resolve_preview(args)
    seconds = getattr(args, "follow_seconds", None)
    print(
        "\n>>> following you — live camera "
        f"({'preview window' if preview else 'status output'}). "
        f"{'Stopping after %.0fs.' % seconds if seconds else 'Ctrl-C to stop.'}"
    )
    try:
        from ..reactive.follow_runner import run_follow_loop

        run_follow_loop(
            backend,
            preview=preview,
            owns_ticking=False,
            seconds=seconds,
        )
    except KeyboardInterrupt:  # pragma: no cover - interactive
        print("\n[stopped]")
    except Exception as exc:  # never crash the CLI on a follow-tail failure
        import logging

        logging.getLogger(__name__).warning("follow tail failed: %s", exc)


def _run_one(agent, command: str, fmt) -> None:
    print(f"\n>>> {command}\n")
    transcript = agent.run_turn(command)
    print(fmt(transcript))
    print(f"\n[budget remaining: {agent.budget.remaining()}]\n")


def _interactive(agent, fmt) -> None:
    print("yalp agent — type a command (blank line or 'quit' to exit).")
    while True:
        try:
            command = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not command or command.lower() in {"quit", "exit"}:
            break
        _run_one(agent, command, fmt)


def _listen_for_command() -> Optional[str]:
    """Capture ONE spoken command via the microphone and transcribe it.

    Push-to-talk: opens a :class:`~yalp.voice.Microphone` using the configured
    defaults (``config.VOICE_SOURCE``, honoring ``config.VOICE_AUDIO_FILE`` for
    the ``"file"`` source), records a single chunk, encodes it to WAV bytes, and
    runs it through :func:`yalp.voice.transcribe` (backend from
    ``config.STT_BACKEND``). Prints a friendly ``>>> [heard: ...]`` line and
    returns the stripped transcript, or ``None`` if nothing was heard.

    Best-effort by design — mirrors the never-raises ethos of ``voice.speak``:
    ANY capture/STT failure is logged and yields ``None`` so the CLI never
    crashes (it simply falls through to the interactive loop as today).
    """
    # Heavy imports stay local so importing this module stays light.
    from .. import voice
    from ..voice import Microphone
    from ..voice.microphone import to_wav_bytes

    print(">>> [listening… speak your command]")
    try:
        # Honor the configured source; only the 'file' source needs an explicit
        # path (the Microphone auto-falls back to synthetic audio otherwise).
        kwargs = {}
        if config.VOICE_SOURCE == "file" and config.VOICE_AUDIO_FILE:
            kwargs["path"] = config.VOICE_AUDIO_FILE
        with Microphone(source=config.VOICE_SOURCE, **kwargs) as mic:
            audio = mic.record_once()
        wav_bytes = to_wav_bytes(audio, mic.sample_rate)
        transcript = (voice.transcribe(wav_bytes) or "").strip()
    except Exception as exc:  # never crash the CLI on capture/STT failure
        import logging

        logging.getLogger(__name__).warning("voice capture/STT failed: %s", exc)
        return None

    if not transcript:
        print(">>> [heard nothing]")
        return None
    print(f">>> [heard: {transcript}]")
    return transcript


def _camera_source(args) -> str:
    """Map the parsed args to a camera source for the shared backend camera.

    Default is the real webcam (``"webcam"``, auto-falling back to synthetic when
    no device opens); ``--synthetic`` forces the synthetic test-pattern.
    """
    return "synthetic" if getattr(args, "synthetic", False) else "webcam"


def _make_backend(synthetic: bool, detector: Optional[str] = None):
    """Build the run's single FakeReactiveBackend, choosing the camera + tracker.

    The reactive layer owns the camera, so the SOURCE is decided here, once: the
    real webcam by default (REAL EYES + FAKE WHEELS — Camera auto-falls-back to
    synthetic if no device opens), or the synthetic test-pattern when
    ``--synthetic`` is passed. ``describe_scene`` later reads frames from *this*
    same camera, so the webcam is opened at most once per run.

    FOLLOW DETECTOR: we PRE-BUILD the PersonTracker up front (exactly like ``yalp
    follow`` does) so the first "follow me" tick doesn't fall into
    ``FakeReactiveBackend``'s lazy HOG default — HOG is brittle (loses the target
    on a turn / partial view), which is wrong for the agent/voice use case where
    the user stands across the room. Detector NAME precedence:
      1. the explicit ``detector`` arg (the ``--follow-detector`` flag);
      2. else ``YALP_FOLLOW_DETECTOR`` if it is explicitly set in the environment;
      3. else ``"person"`` — the ROOM-RANGE, orientation-agnostic MobileNet-SSD
         body detector (NOT ``config.FOLLOW_DETECTOR_DEFAULT``, which is the
         desk-only ``"face"`` default meant for ``yalp follow``).
    If the tracker can't be built (model/OpenCV unavailable), we pass NO tracker
    and let the existing lazy fallback happen — never crash.
    """
    import os

    from ..reactive.fake_backend import FakeReactiveBackend
    from ..reactive.follow_runner import build_follow_tracker

    source = "synthetic" if synthetic else "webcam"

    if detector is not None:
        det = detector
    elif "YALP_FOLLOW_DETECTOR" in os.environ:
        det = os.environ["YALP_FOLLOW_DETECTOR"]
    else:
        det = "person"

    tracker = build_follow_tracker(det)
    if tracker is None:
        import logging

        logging.getLogger(__name__).warning(
            "follow detector '%s' unavailable — falling back to the lazy default "
            "tracker (follow may be less robust)",
            det,
        )
        return FakeReactiveBackend(tick_hz=50.0, camera_source=source)
    return FakeReactiveBackend(tick_hz=50.0, camera_source=source, tracker=tracker)


def _make_describe(backend):
    """Build the agent's describe_scene callable bound to the backend camera.

    Routes nothing itself — the agent picks the model tier and passes it in; we
    just grab the latest still from the backend's OWNED/SHARED camera (real
    webcam by default, synthetic with ``--synthetic``) and ask the vision path.
    Reading ``backend.camera()`` here — rather than opening a fresh camera —
    keeps the reactive layer the sole owner of the device and guarantees only ONE
    camera is opened per run. The real Anthropic client is built lazily (a key is
    present here).
    """
    from . import vision

    def describe(detail: str = "quick", question: Optional[str] = None,
                 model: Optional[str] = None) -> str:
        q = question or (
            "Describe this scene in detail." if detail == "full" else "What do you see?"
        )
        return vision.describe_scene(
            frame=None,
            question=q,
            model=model or config.MODEL_FAST,
            camera=backend.camera(),
        )

    return describe


__all__ = ["add_parser", "run"]
