"""Tests for the Telegram channel (:mod:`yalp.telegram`, ``yalp telegram``).

Fully offline: the HTTP opener is a fake that records each request and returns
scripted Bot API responses. Coverage:

  1. send_message posts to sendMessage and splits an over-long body.
  2. API / HTTP failures raise TelegramError WITHOUT leaking the bot token.
  3. get_updates parses text messages and tracks the highest update id.
  4. TelegramResponder delivers, prefixes errors, and never raises.
  5. poll_commands yields only the owner's messages and tells strangers their id.
  6. skip_backlog starts after everything already sent.
  7. responder_from_env needs both the token and a valid chat id.
  8. The CLI: missing token exits 2; ``agent --telegram`` flag parses.
"""

from __future__ import annotations

import argparse
import io
import json
import urllib.error

import pytest

from yalp import telegram
from yalp.responder import KIND_ERROR, Responder

TOKEN = "123456:SECRET-token"
OWNER = 111
STRANGER = 222


class FakeApi:
    """A scripted stand-in for ``urllib.request.urlopen``."""

    def __init__(self, results=None):
        self.calls: list[tuple[str, dict]] = []
        self.results = list(results or [])
        self.fail_with = None

    def __call__(self, request, timeout=None):
        method = request.full_url.rsplit("/", 1)[-1]
        self.calls.append((method, json.loads(request.data.decode())))
        if self.fail_with is not None:
            raise self.fail_with
        result = self.results.pop(0) if (method == "getUpdates" and self.results) else (
            [] if method == "getUpdates" else {}
        )
        return _Response({"ok": True, "result": result})

    def sent(self):
        return [(p["chat_id"], p["text"]) for m, p in self.calls if m == "sendMessage"]


class _Response(io.BytesIO):
    def __init__(self, body):
        super().__init__(json.dumps(body).encode())

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _update(update_id, chat_id, text):
    return {"update_id": update_id, "message": {"chat": {"id": chat_id}, "text": text}}


def _client(api):
    return telegram.TelegramClient(TOKEN, opener=api)


def test_send_message_posts_and_chunks():
    api = FakeApi()
    _client(api).send_message(OWNER, "hello")
    assert api.sent() == [(OWNER, "hello")]

    api = FakeApi()
    _client(api).send_message(OWNER, "x" * (telegram.MAX_MESSAGE_CHARS + 5))
    assert [len(text) for _, text in api.sent()] == [telegram.MAX_MESSAGE_CHARS, 5]


def test_errors_never_leak_the_token():
    api = FakeApi()
    api.fail_with = OSError(f"cannot reach https://api.telegram.org/bot{TOKEN}/sendMessage")
    with pytest.raises(telegram.TelegramError) as info:
        _client(api).send_message(OWNER, "hi")
    assert TOKEN not in str(info.value)

    api.fail_with = urllib.error.HTTPError(
        "u", 401, "Unauthorized", None, io.BytesIO(b'{"ok":false,"description":"Unauthorized"}')
    )
    with pytest.raises(telegram.TelegramError) as info:
        _client(api).send_message(OWNER, "hi")
    assert "401" in str(info.value) and TOKEN not in str(info.value)


def test_get_updates_parses_text_and_tracks_offset():
    api = FakeApi([[
        _update(5, OWNER, "drive forward"),
        {"update_id": 6, "message": {"chat": {"id": OWNER}, "photo": []}},
    ]])
    client = _client(api)
    messages = client.get_updates(offset=5)
    assert messages == [telegram.IncomingMessage(5, OWNER, "drive forward")]
    assert client.last_update_id == 6
    assert api.calls[0][1]["offset"] == 5


def test_responder_delivers_prefixes_errors_and_never_raises(caplog):
    api = FakeApi()
    responder = telegram.TelegramResponder(_client(api), OWNER)
    assert isinstance(responder, Responder)
    responder.respond("I see a wall.")
    responder.respond("camera failed", kind=KIND_ERROR)
    responder.respond("   ")
    assert api.sent() == [(OWNER, "I see a wall."), (OWNER, "⚠️ camera failed")]

    api.fail_with = OSError("network down")
    responder.respond("lost")  # must not raise
    assert "Telegram delivery failed" in caplog.text


def test_poll_commands_only_obeys_the_owner():
    api = FakeApi([[
        _update(1, STRANGER, "drive into the wall"),
        _update(2, OWNER, "  what do you see  "),
    ]])
    client = _client(api)
    polls = {"n": 0}

    def should_stop():
        polls["n"] += 1
        return polls["n"] > 1

    commands = list(telegram.poll_commands(client, OWNER, should_stop=should_stop))
    assert commands == ["what do you see"]
    assert api.sent() == [(STRANGER, telegram.unknown_chat_reply(STRANGER))]
    assert str(STRANGER) in api.sent()[0][1]


def test_poll_commands_in_setup_mode_yields_nothing():
    api = FakeApi([[_update(1, OWNER, "hello")]])
    polls = {"n": 0}

    def should_stop():
        polls["n"] += 1
        return polls["n"] > 1

    assert list(telegram.poll_commands(_client(api), None, should_stop=should_stop)) == []
    assert api.sent() == [(OWNER, telegram.unknown_chat_reply(OWNER))]


def test_poll_commands_retries_after_a_failed_poll(monkeypatch):
    monkeypatch.setattr(telegram, "_sleep", lambda seconds: None)
    api = FakeApi()
    api.fail_with = OSError("down")
    polls = {"n": 0}

    def should_stop():
        polls["n"] += 1
        return polls["n"] > 2

    assert list(telegram.poll_commands(_client(api), OWNER, should_stop=should_stop)) == []
    assert len(api.calls) == 2


def test_skip_backlog_starts_after_the_latest_update():
    assert telegram.skip_backlog(_client(FakeApi([[_update(41, OWNER, "old command")]]))) == 42
    assert telegram.skip_backlog(_client(FakeApi())) is None


def test_responder_from_env(monkeypatch):
    monkeypatch.delenv(telegram.ENV_TOKEN, raising=False)
    monkeypatch.delenv(telegram.ENV_CHAT_ID, raising=False)
    assert telegram.responder_from_env() is None

    monkeypatch.setenv(telegram.ENV_TOKEN, TOKEN)
    assert telegram.responder_from_env() is None  # chat id still missing

    monkeypatch.setenv(telegram.ENV_CHAT_ID, "not-a-number")
    assert telegram.responder_from_env() is None

    monkeypatch.setenv(telegram.ENV_CHAT_ID, str(OWNER))
    api = FakeApi()
    telegram.responder_from_env(opener=api).respond("hi")
    assert api.sent() == [(OWNER, "hi")]


def test_cli_without_a_token_explains_setup(monkeypatch, capsys):
    from yalp import telegram_cli

    monkeypatch.delenv(telegram.ENV_TOKEN, raising=False)
    assert telegram_cli.run(argparse.Namespace()) == 2
    assert telegram.ENV_TOKEN in capsys.readouterr().out


def test_cli_registers_telegram_and_agent_flag():
    from yalp.cli import build_parser

    parser = build_parser()
    assert parser.parse_args(["telegram", "--host", "izzy.local"]).host == "izzy.local"
    assert parser.parse_args(["agent", "--telegram"]).telegram is True
    assert parser.parse_args(["agent"]).telegram is False


def test_cli_loop_runs_owner_commands_and_replies(monkeypatch, capsys):
    """End to end with a fake Bot API and a stubbed model turn (no network)."""
    from yalp import telegram_cli
    from yalp.deliberative import agent as agent_module

    monkeypatch.setenv(telegram.ENV_TOKEN, TOKEN)
    monkeypatch.setenv(telegram.ENV_CHAT_ID, str(OWNER))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    seen = []

    def fake_run_turn(self, text):
        seen.append(text)
        if text == "what do you see":
            self.responder.respond("A wall.")
        elif text == "explode":
            raise RuntimeError("boom")
        return []  # "turn left" acts but says nothing

    monkeypatch.setattr(agent_module.Agent, "run_turn", fake_run_turn)

    api = FakeApi([
        [_update(9, OWNER, "sent while the robot was off")],  # backlog (skipped)
        [_update(10, OWNER, "what do you see"), _update(11, STRANGER, "drive")],
        [_update(12, OWNER, "turn left"), _update(13, OWNER, "explode")],
    ])
    client = _client(api)

    real_poll = telegram.poll_commands
    polls = {"n": 0}

    def stop_after_two_polls():
        polls["n"] += 1
        return polls["n"] > 2

    monkeypatch.setattr(
        telegram, "poll_commands",
        lambda c, chat, **kw: real_poll(c, chat, should_stop=stop_after_two_polls, **kw),
    )

    args = argparse.Namespace(host=None, port=None, synthetic=True, steps=3)
    assert telegram_cli.run(args, client=client) == 0

    assert seen == ["what do you see", "turn left", "explode"]
    owner_texts = [text for chat, text in api.sent() if chat == OWNER]
    assert owner_texts[0].endswith("Robot online. Send me a command.")
    assert owner_texts[1:] == ["A wall.", "Done.", "⚠️ That command failed (RuntimeError)."]
    assert [chat for chat, _ in api.sent() if chat == STRANGER] == [STRANGER]
    assert TOKEN not in capsys.readouterr().out
