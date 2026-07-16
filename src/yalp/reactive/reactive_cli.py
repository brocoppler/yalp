"""``yalp reactive`` — start the reactive backend and IPC server.

Registered with the CLI via the documented feature-module contract: this module
exposes ``add_parser(subparsers)`` and ``run(args) -> int`` and is listed in
``yalp.cli.FEATURE_MODULES``.

    yalp reactive                        # fake backend, default host/port
    yalp reactive --backend real         # real hardware (Pi only)
    yalp reactive --port 9000            # custom port
    yalp reactive --hz 10                # lower tick rate
    yalp reactive --camera-source synthetic  # no-camera demo
    yalp reactive --detector person      # FOLLOW body detector (front/back/side)

Gpiozero is imported LAZILY inside ``run()`` only when ``--backend real`` is
chosen, so importing this module never touches gpiozero or lgpio.
"""

from __future__ import annotations

import os
import signal
import threading


def add_parser(subparsers) -> None:
    """Register the ``reactive`` subcommand and its flags."""
    from .. import config

    parser = subparsers.add_parser(
        "reactive",
        help="Start the reactive backend and IPC server (fake or real hardware).",
        description=(
            "Run the reactive loop (software-spec.md §2): tick the chosen backend "
            "at --hz, publish RobotState through a ReactiveServer at --host:--port, "
            "and accept Intent commands from the deliberative layer. "
            "Use --backend fake (the default) for laptop development; "
            "--backend real for the Pi with gpiozero motors + ultrasonic sensor."
        ),
    )
    parser.add_argument(
        "--backend",
        choices=("fake", "real"),
        default="fake",
        help="Backend to use: 'fake' (default, simulation) or 'real' (Pi hardware).",
    )
    parser.add_argument(
        "--host",
        default=config.IPC_HOST,
        help=f"IPC server bind host (default: {config.IPC_HOST}).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=config.IPC_PORT,
        help=f"IPC server bind port (default: {config.IPC_PORT}).",
    )
    parser.add_argument(
        "--hz",
        type=float,
        default=config.REACTIVE_TICK_HZ,
        metavar="HZ",
        help=f"Reactive tick rate in Hz (default: {config.REACTIVE_TICK_HZ}).",
    )
    parser.add_argument(
        "--camera-source",
        default="webcam",
        metavar="SOURCE",
        help="Camera source: 'webcam' (default), 'synthetic', or 'image'.",
    )
    parser.add_argument(
        "--detector",
        choices=("face", "hog", "person", "auto"),
        default=config.FOLLOW_DETECTOR_DEFAULT,
        help=(
            f"Person detector for FOLLOW mode (default: {config.FOLLOW_DETECTOR_DEFAULT!r}). "
            "'face' — Haar cascade (desk range); 'person' — orientation-agnostic MobileNet-SSD; "
            "'hog' — OpenCV standing-body detector; 'auto' — person with face fallback."
        ),
    )
    parser.set_defaults(handler=run)


def run(args) -> int:
    """Handler for ``yalp reactive``. Returns a process exit code."""
    from .. import config
    from ..contract.ipc import ReactiveServer
    from ..telemetry import create_logger_from_env
    from .follow_runner import build_follow_tracker

    host = getattr(args, "host", config.IPC_HOST)
    port = int(getattr(args, "port", config.IPC_PORT))
    hz = float(getattr(args, "hz", config.REACTIVE_TICK_HZ))
    camera_source = getattr(args, "camera_source", "webcam")
    detector = getattr(args, "detector", config.FOLLOW_DETECTOR_DEFAULT)
    backend_kind = getattr(args, "backend", "fake")

    # Build the FOLLOW tracker for the chosen --detector up front and hand it to
    # whichever backend we construct. Without this the backend would lazily build a
    # default PersonTracker (HOG) on the first FOLLOW tick, silently ignoring
    # --detector. ``build_follow_tracker`` returns None (falling back to that lazy
    # default) only if the detector cannot be built at all — e.g. a broken OpenCV —
    # so a bad build still runs FOLLOW (degrading to "lost") instead of crashing.
    # cv2 is imported lazily inside the detector constructors, never at module load.
    tracker = build_follow_tracker(detector)

    # Flight recorder (yalp.telemetry): record what the robot saw + did as JSONL
    # so future mapping/memory work has a corpus from day one. Injected into the
    # backend (so it drives the tick-core observer hooks + the watchdog trip edge);
    # None when YALP_TELEMETRY=0. We own its lifecycle and close it in finally.
    telemetry = create_logger_from_env()

    # Build the chosen backend — gpiozero is only imported here, when 'real' is
    # explicitly requested, so importing this module never touches gpiozero/lgpio.
    if backend_kind == "real":
        from .real_backend import RealReactiveBackend

        backend = RealReactiveBackend(
            camera_source=camera_source,
            tick_hz=hz,
            tracker=tracker,
            observer=telemetry,
        )
    else:
        from .fake_backend import FakeReactiveBackend

        backend = FakeReactiveBackend(
            camera_source=camera_source,
            tick_hz=hz,
            tracker=tracker,
            observer=telemetry,
        )

    # Share the backend's mailbox with the server so Intent lines land directly
    # in the same slot the tick loop drains.
    server = ReactiveServer(host=host, port=port, mailbox=backend.mailbox)

    stop_event = threading.Event()

    # --- Reliable graceful shutdown under SIGINT/SIGTERM --------------------------
    # Regression fix (2026-07-16): during a sustained ultrasonic echo-timeout storm
    # the stack survived two SIGINTs and only died to SIGTERM. The PRIMARY fix is in
    # the backend teardown (motors zeroed first, every join/close time-bounded — so
    # a single SIGINT reliably shuts down even while blind). Here we make the signal
    # itself drive that path directly rather than relying on a KeyboardInterrupt
    # landing at an arbitrary bytecode: the FIRST SIGINT/SIGTERM sets ``stop_event``
    # so the reactive loop exits at the next iteration boundary and the bounded
    # teardown runs. A SECOND SIGINT is belt-and-braces ONLY: if teardown itself ever
    # wedged we force-exit rather than requiring an external kill (the wheels are
    # already zeroed by then — that is the first thing teardown does).
    signal_count = {"n": 0}

    def _handle_shutdown_signal(signum, _frame) -> None:
        signal_count["n"] += 1
        if signal_count["n"] == 1:
            stop_event.set()
            print(
                f"\nyalp reactive — signal {signum} received; shutting down "
                "gracefully (press Ctrl-C again to force-exit)...",
                flush=True,
            )
        else:
            print(
                f"\nyalp reactive — second signal {signum}; forcing exit.",
                flush=True,
            )
            os._exit(130)

    # signal.signal() only works on the main thread; if we are not there (e.g. the
    # CLI is being driven from within a worker thread in a test/embedding) fall back
    # to the default KeyboardInterrupt behaviour rather than crashing.
    installed_handlers: list = []
    try:
        for _sig in (signal.SIGINT, signal.SIGTERM):
            installed_handlers.append((_sig, signal.signal(_sig, _handle_shutdown_signal)))
    except (ValueError, OSError):  # pragma: no cover - only on non-main-thread use
        installed_handlers = []

    print(
        f"yalp reactive — backend={backend_kind!r} "
        f"server={host}:{port} "
        f"hz={hz} "
        f"camera={camera_source!r} "
        f"detector={detector!r}"
    )

    server.start()
    try:
        backend.run(server=server, stop_event=stop_event)
    except KeyboardInterrupt:
        # Belt-and-braces: if handler installation failed (non-main-thread) the
        # default SIGINT still lands here — treat it as a shutdown request.
        stop_event.set()
    finally:
        stop_event.set()
        # Teardown is time-bounded inside the backend (motors zeroed first, every
        # join/close bounded), so this never hangs even during a sensor storm.
        backend.stop()
        server.stop()
        # Flush + join the telemetry writer AFTER the loop/teardown so late events
        # are captured (idempotent; a no-op when telemetry is disabled).
        if telemetry is not None:
            telemetry.close()
        # Restore whatever signal handlers we replaced (hygiene for embedders).
        for _sig, _previous in installed_handlers:
            try:
                signal.signal(_sig, _previous)
            except (ValueError, OSError):  # pragma: no cover
                pass

    return 0


__all__ = ["add_parser", "run"]
