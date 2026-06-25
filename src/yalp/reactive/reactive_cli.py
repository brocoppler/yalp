"""``yalp reactive`` — start the reactive backend and IPC server.

Registered with the CLI via the documented feature-module contract: this module
exposes ``add_parser(subparsers)`` and ``run(args) -> int`` and is listed in
``yalp.cli.FEATURE_MODULES``.

    yalp reactive                        # fake backend, default host/port
    yalp reactive --backend real         # real hardware (Pi only)
    yalp reactive --port 9000            # custom port
    yalp reactive --hz 10                # lower tick rate
    yalp reactive --camera-source synthetic  # no-camera demo

Gpiozero is imported LAZILY inside ``run()`` only when ``--backend real`` is
chosen, so importing this module never touches gpiozero or lgpio.
"""

from __future__ import annotations

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

    host = getattr(args, "host", config.IPC_HOST)
    port = int(getattr(args, "port", config.IPC_PORT))
    hz = float(getattr(args, "hz", config.REACTIVE_TICK_HZ))
    camera_source = getattr(args, "camera_source", "webcam")
    detector = getattr(args, "detector", config.FOLLOW_DETECTOR_DEFAULT)
    backend_kind = getattr(args, "backend", "fake")

    # Build the chosen backend — gpiozero is only imported here, when 'real' is
    # explicitly requested, so importing this module never touches gpiozero/lgpio.
    if backend_kind == "real":
        from .real_backend import RealReactiveBackend

        backend = RealReactiveBackend(
            camera_source=camera_source,
            tick_hz=hz,
        )
    else:
        from .fake_backend import FakeReactiveBackend

        backend = FakeReactiveBackend(
            camera_source=camera_source,
            tick_hz=hz,
        )

    # Share the backend's mailbox with the server so Intent lines land directly
    # in the same slot the tick loop drains.
    server = ReactiveServer(host=host, port=port, mailbox=backend.mailbox)

    stop_event = threading.Event()

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
        pass
    finally:
        stop_event.set()
        backend.stop()
        server.stop()

    return 0


__all__ = ["add_parser", "run"]
