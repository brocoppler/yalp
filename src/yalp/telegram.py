"""Telegram channel for yalp — replies out to a phone, text commands back in.

The robot has no speaker, so a reply has to land somewhere a person will see it.
This module is that channel. It has three parts:

* :class:`TelegramClient` — the two Bot API calls we need (``sendMessage`` and
  long-polling ``getUpdates``) over the standard library, so there is no new
  dependency. The HTTP opener is injectable, which keeps the tests offline.
* :class:`TelegramResponder` — a :class:`yalp.responder.Responder` that sends
  each reply to ONE configured chat.
* :func:`poll_commands` — yields text messages from that same chat, for the
  ``yalp telegram`` command loop.

Security model
--------------
A bot's username is public: anyone can message it. So nothing a stranger sends is
ever executed. Only messages from the configured chat id
(``YALP_TELEGRAM_CHAT_ID``) are treated as commands. A message from any other
chat gets one fixed reply telling the sender their own chat id (which is how the
owner discovers theirs during setup) and is otherwise ignored.

The bot token (``YALP_TELEGRAM_BOT_TOKEN``) is a secret. It is read from the
environment at use time, never stored on :mod:`yalp.config`, and never written
to a log: every error message from this module redacts it.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Optional

from .responder import KIND_ANSWER, KIND_ERROR, KIND_STATUS

logger = logging.getLogger("yalp.telegram")

ENV_TOKEN = "YALP_TELEGRAM_BOT_TOKEN"
ENV_CHAT_ID = "YALP_TELEGRAM_CHAT_ID"

API_BASE = "https://api.telegram.org"
# Telegram rejects a message body longer than this many characters.
MAX_MESSAGE_CHARS = 4096
# Seconds the server holds a getUpdates call open waiting for a message.
LONG_POLL_SECONDS = 25
# Extra seconds on the socket timeout so the long poll returns before we give up.
_TIMEOUT_MARGIN_SECONDS = 10

_KIND_PREFIXES = {KIND_ANSWER: "", KIND_STATUS: "… ", KIND_ERROR: "⚠️ "}


class TelegramError(RuntimeError):
    """A Bot API call failed. The message never contains the bot token."""


def get_token() -> Optional[str]:
    """The bot token from the environment, or ``None`` if unset/empty."""
    return (os.environ.get(ENV_TOKEN) or "").strip() or None


def get_chat_id() -> Optional[int]:
    """The owner's chat id from the environment, or ``None`` if unset/invalid."""
    raw = (os.environ.get(ENV_CHAT_ID) or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        logger.warning("%s is not an integer chat id — ignoring it.", ENV_CHAT_ID)
        return None


@dataclass(frozen=True)
class IncomingMessage:
    """One text message received by the bot."""

    update_id: int
    chat_id: int
    text: str


class TelegramClient:
    """Minimal Telegram Bot API client (``sendMessage`` + ``getUpdates``)."""

    def __init__(
        self,
        token: str,
        *,
        opener: Optional[Callable[..., Any]] = None,
        api_base: str = API_BASE,
    ) -> None:
        if not token:
            raise ValueError("a Telegram bot token is required")
        self._token = token
        self._opener = opener or urllib.request.urlopen
        self._api_base = api_base.rstrip("/")
        # Highest update id seen by the last get_updates call (None if none).
        self.last_update_id: Optional[int] = None

    def _redact(self, text: str) -> str:
        return text.replace(self._token, "<token>")

    def _call(self, method: str, payload: dict, *, timeout: float) -> Any:
        """POST one Bot API method and return its ``result``; raise on failure."""
        request = urllib.request.Request(
            f"{self._api_base}/bot{self._token}/{method}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self._opener(request, timeout=timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # Telegram puts the reason in the JSON body of an error response.
            detail = ""
            try:
                detail = json.loads(exc.read().decode("utf-8")).get("description", "")
            except Exception:  # noqa: BLE001 - best-effort detail only
                pass
            raise TelegramError(
                self._redact(f"{method} failed: HTTP {exc.code} {detail}".strip())
            ) from None
        except Exception as exc:  # noqa: BLE001 - network / decode failure
            raise TelegramError(
                self._redact(f"{method} failed: {type(exc).__name__}: {exc}")
            ) from None
        if not isinstance(body, dict) or not body.get("ok"):
            description = body.get("description", "") if isinstance(body, dict) else ""
            raise TelegramError(self._redact(f"{method} failed: {description}".strip()))
        return body.get("result")

    def send_message(self, chat_id: int, text: str) -> None:
        """Send ``text`` to ``chat_id``, split into chunks Telegram will accept."""
        for chunk in _chunks(text, MAX_MESSAGE_CHARS):
            self._call("sendMessage", {"chat_id": chat_id, "text": chunk}, timeout=15)

    def get_updates(
        self, offset: Optional[int] = None, *, timeout: int = LONG_POLL_SECONDS
    ) -> list[IncomingMessage]:
        """Long-poll for new text messages after ``offset``.

        Non-text updates (photos, edits, joins) are returned as nothing but still
        advance the caller's offset through :attr:`last_update_id`.
        """
        payload: dict[str, Any] = {"timeout": timeout, "allowed_updates": ["message"]}
        if offset is not None:
            payload["offset"] = offset
        result = self._call(
            "getUpdates", payload, timeout=timeout + _TIMEOUT_MARGIN_SECONDS
        )
        messages: list[IncomingMessage] = []
        self.last_update_id = None
        for update in result or []:
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                self.last_update_id = update_id
            message = update.get("message") or {}
            text = message.get("text")
            chat_id = (message.get("chat") or {}).get("id")
            if isinstance(text, str) and isinstance(chat_id, int) and isinstance(update_id, int):
                messages.append(IncomingMessage(update_id, chat_id, text))
        return messages


def _chunks(text: str, size: int) -> Iterator[str]:
    body = (text or "").strip()
    for start in range(0, len(body), size):
        yield body[start : start + size]


class TelegramResponder:
    """Deliver replies to one Telegram chat. Best-effort: never raises."""

    def __init__(self, client: TelegramClient, chat_id: int) -> None:
        self._client = client
        self._chat_id = chat_id

    def respond(self, text: str, *, kind: str = KIND_ANSWER) -> None:
        body = (text or "").strip()
        if not body:
            return
        try:
            self._client.send_message(self._chat_id, _KIND_PREFIXES.get(kind, "") + body)
        except TelegramError as exc:
            logger.warning("Telegram delivery failed (%s) — reply not sent there.", exc)


def responder_from_env(
    *, opener: Optional[Callable[..., Any]] = None
) -> Optional[TelegramResponder]:
    """Build a :class:`TelegramResponder` from the environment, or ``None``.

    ``None`` means the channel is not configured (token or chat id missing); the
    caller keeps its other channels and carries on.
    """
    token, chat_id = get_token(), get_chat_id()
    if token is None or chat_id is None:
        return None
    return TelegramResponder(TelegramClient(token, opener=opener), chat_id)


def unknown_chat_reply(chat_id: int) -> str:
    """The one fixed reply a chat other than the owner's ever receives."""
    return (
        f"This robot only takes commands from its owner. Your chat id is {chat_id}. "
        f"If this is your robot, set {ENV_CHAT_ID}={chat_id} in its .env and restart."
    )


def skip_backlog(client: TelegramClient) -> Optional[int]:
    """Return the offset that skips every message sent before now.

    A command typed while the robot was off must not make it drive when it next
    boots, so the command loop starts from here.
    """
    client.get_updates(offset=-1, timeout=0)
    last = client.last_update_id
    return None if last is None else last + 1


def poll_commands(
    client: TelegramClient,
    allowed_chat_id: Optional[int],
    *,
    offset: Optional[int] = None,
    should_stop: Callable[[], bool] = lambda: False,
) -> Iterator[str]:
    """Yield command text from the owner's chat, forever (until ``should_stop``).

    Messages from any other chat are answered with :func:`unknown_chat_reply`
    and never yielded. A failed poll is logged and retried on the next loop.
    """
    while not should_stop():
        try:
            messages = client.get_updates(offset)
        except TelegramError as exc:
            logger.warning("Telegram poll failed (%s) — retrying.", exc)
            _sleep(_RETRY_SECONDS)
            continue
        if client.last_update_id is not None:
            offset = client.last_update_id + 1
        for message in messages:
            if allowed_chat_id is not None and message.chat_id == allowed_chat_id:
                text = message.text.strip()
                if text:
                    yield text
            else:
                try:
                    client.send_message(message.chat_id, unknown_chat_reply(message.chat_id))
                except TelegramError as exc:
                    logger.warning("Telegram reply to unknown chat failed (%s).", exc)


_RETRY_SECONDS = 5.0


def _sleep(seconds: float) -> None:
    import time

    time.sleep(seconds)


__all__ = [
    "ENV_CHAT_ID",
    "ENV_TOKEN",
    "IncomingMessage",
    "TelegramClient",
    "TelegramError",
    "TelegramResponder",
    "get_chat_id",
    "get_token",
    "poll_commands",
    "responder_from_env",
    "skip_backlog",
    "unknown_chat_reply",
]
