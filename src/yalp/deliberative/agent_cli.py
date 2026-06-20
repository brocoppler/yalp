"""``yalp agent`` — drive the deliberative agent loop against the fake backend.

Registered with the CLI via the documented feature-module contract: this module
exposes ``add_parser(subparsers)`` and ``run(args) -> int`` and is listed in
``yalp.cli.FEATURE_MODULES``.

    yalp agent --command "drive forward a bit and tell me what you see"
    yalp agent --steps 6 --command "explore the room and report"
    yalp agent                         # interactive prompt loop

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
            "interactive prompt). Prints the model / tool / robot-state transcript."
        ),
    )
    parser.add_argument(
        "--command",
        metavar="TEXT",
        default=None,
        help="A single natural-language command to run, then exit.",
    )
    parser.add_argument(
        "--steps",
        type=int,
        default=12,
        metavar="N",
        help="Max deliberative steps per command (default: 12).",
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
    from ..reactive.fake_backend import FakeReactiveBackend
    from .agent import Agent, format_transcript

    backend = FakeReactiveBackend(tick_hz=50.0)
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
    agent = Agent(
        client=None,  # real LLM client built lazily by llm.call_with_tools
        reactive=client,
        describe_scene=describe,
        max_steps=args.steps,
    )

    try:
        if args.command:
            _run_one(agent, args.command, format_transcript)
        else:
            _interactive(agent, format_transcript)
    finally:
        stop.set()
        runner.join(timeout=2.0)
        client.close()
        server.stop()
    return 0


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


def _make_describe(backend):
    """Build the agent's describe_scene callable bound to the backend camera.

    Routes nothing itself — the agent picks the model tier and passes it in; we
    just grab the latest still from the (synthetic) camera and ask the vision
    path. The real Anthropic client is built lazily (a key is present here).
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
