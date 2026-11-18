"""``yalp telegram`` — take text commands from Telegram and reply there.

Runs the same agent loop as ``yalp agent``, but the commands arrive as Telegram
messages from the owner's chat and every reply is sent back to that chat (and
printed). This is the robot's message channel: it has no speaker.

Setup (once): create a bot with @BotFather, put its token in ``.env`` as
``YALP_TELEGRAM_BOT_TOKEN``, run ``yalp telegram`` and message the bot — it
replies with your chat id — then set ``YALP_TELEGRAM_CHAT_ID`` and restart.

Like ``yalp agent``, the body is the local fake by default; pass ``--host`` to
drive a real reactive server (``yalp session start`` on the Pi).
"""

from __future__ import annotations

import logging
from typing import Optional

from . import config, telegram
from .responder import KIND_ERROR, KIND_STATUS, CompositeResponder, ConsoleResponder

logger = logging.getLogger("yalp.telegram")


def add_parser(subparsers) -> None:
    """Register the ``telegram`` subcommand and its flags."""
    parser = subparsers.add_parser(
        "telegram",
        help="Take text commands from Telegram and reply there (the message channel).",
        description=(
            "Long-poll a Telegram bot for messages from the owner's chat, run each "
            "one through the agent loop, and send the replies back to that chat. "
            f"Needs {telegram.ENV_TOKEN} and {telegram.ENV_CHAT_ID} in the "
            "environment / .env; with only the token set it runs in setup mode and "
            "tells whoever messages the bot their chat id."
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
    parser.set_defaults(handler=run)


class _CountingResponder:
    """Wrap a responder and count deliveries, so a silent turn can be noticed."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.count = 0

    def respond(self, text: str, *, kind: str = "answer") -> None:
        self.count += 1
        self._inner.respond(text, kind=kind)


def run(args, *, client: Optional[telegram.TelegramClient] = None) -> int:
    """Handler for ``yalp telegram``. Returns a process exit code."""
    token = telegram.get_token()
    if client is None:
        if token is None:
            print(
                f"Telegram is not set up: {telegram.ENV_TOKEN} is missing.\n"
                "Create a bot with @BotFather in Telegram, then add its token to "
                f".env as {telegram.ENV_TOKEN}=... and run this again."
            )
            return 2
        client = telegram.TelegramClient(token)

    chat_id = telegram.get_chat_id()
    if chat_id is None:
        print(
            f"Setup mode: {telegram.ENV_CHAT_ID} is not set. Send your bot any "
            "message in Telegram; it will reply with your chat id. Put that in "
            f".env as {telegram.ENV_CHAT_ID}=... and run this again. Ctrl-C to stop."
        )
        try:
            for _ in telegram.poll_commands(client, None):
                pass  # never yields without an owner chat; it only answers with ids
        except KeyboardInterrupt:
            print()
        return 0

    from .deliberative import vision

    if config.get_api_key() is None:
        print(vision.MISSING_KEY_MESSAGE)
        return 0

    # Reuse the agent CLI's body setup so local/remote behave exactly the same.
    from .deliberative import agent_cli
    from .deliberative.agent import Agent, format_transcript
    from .deliberative.model_router import Budget

    if getattr(args, "host", None):
        reactive, describe, teardown = agent_cli._setup_remote(args, args.host)
        capture_still = agent_cli._make_capture_still(reactive)
    else:
        reactive, describe, backend, teardown = agent_cli._setup_local(args)
        capture_still = agent_cli._make_capture_still(backend)

    responder = _CountingResponder(
        CompositeResponder(ConsoleResponder(), telegram.TelegramResponder(client, chat_id))
    )
    agent = Agent(
        client=None,
        reactive=reactive,
        describe_scene=describe,
        capture_still=capture_still,
        max_steps=args.steps,
        responder=responder,
    )

    try:
        # Commands typed while the robot was off must not run now.
        offset = telegram.skip_backlog(client)
        responder.respond("Robot online. Send me a command.", kind=KIND_STATUS)
        for command in telegram.poll_commands(client, chat_id, offset=offset):
            print(f"\n>>> [telegram] {command}\n")
            agent.budget = Budget()  # the budget is per command, not per uptime
            before = responder.count
            try:
                print(format_transcript(agent.run_turn(command)))
            except Exception as exc:  # noqa: BLE001 - one bad command must not kill the loop
                logger.warning("command failed: %s: %s", type(exc).__name__, exc)
                responder.respond(
                    f"That command failed ({type(exc).__name__}).", kind=KIND_ERROR
                )
                continue
            if responder.count == before:
                # The agent acted but said nothing; never leave the owner guessing.
                responder.respond("Done.")
    except KeyboardInterrupt:
        print()
    except telegram.TelegramError as exc:
        print(f"Telegram error: {exc}")
        return 1
    finally:
        teardown()
    return 0
