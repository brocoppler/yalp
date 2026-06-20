"""Tests for the Anthropic SDK wrapper (``yalp.llm``) — no network, key, or SDK.

The Anthropic client is *injected* as a recording stub, so these tests exercise
the exact ``messages.create(...)`` kwargs the wrapper builds without the
``anthropic`` package, an API key, or a network call.

Regression focus: the default ``ask`` / ``call_with_tools`` path must NOT send a
``thinking`` parameter. The fast tier (``claude-haiku-4-5``) used by ``yalp see``
and the per-step agent loop does not support it, and unconditionally sending it
made the live API 400 with "adaptive thinking is not supported on this model".
"""

from __future__ import annotations

from types import SimpleNamespace

from yalp import config
from yalp import llm


class FakeClient:
    """A stand-in Anthropic client that records each ``messages.create`` call."""

    def __init__(self, reply: str = "ok"):
        self.reply = reply
        self.calls: list[dict] = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        block = SimpleNamespace(type="text", text=self.reply)
        return SimpleNamespace(
            content=[block],
            stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=1, output_tokens=1),
        )


# --- the core regression: no thinking by default ----------------------------


def test_ask_default_omits_thinking_param():
    """Default ``ask`` must not send ``thinking`` — this is what unblocks Haiku."""
    client = FakeClient()
    llm.ask("hello", client=client)

    assert len(client.calls) == 1
    assert "thinking" not in client.calls[0]
    # Sanity: the call still defaults to the fast (Haiku) tier.
    assert client.calls[0]["model"] == config.MODEL_FAST


def test_call_with_tools_default_omits_thinking_param():
    """Default per-step agent call must not send ``thinking`` on the fast tier."""
    client = FakeClient()
    llm.call_with_tools([{"role": "user", "content": "hi"}], tools=[], client=client)

    assert len(client.calls) == 1
    assert "thinking" not in client.calls[0]
    assert client.calls[0]["model"] == config.MODEL_FAST


# --- opt-in thinking is still gated by model capability ---------------------


def test_ask_thinking_requested_on_unsupported_model_is_omitted():
    """Requesting thinking on Haiku silently omits it (no 400)."""
    client = FakeClient()
    llm.ask("hello", model="claude-haiku-4-5", client=client, thinking=True)

    assert "thinking" not in client.calls[0]


def test_ask_thinking_requested_on_supported_model_is_attached():
    """Requesting thinking on a capable model attaches the adaptive payload."""
    client = FakeClient()
    llm.ask("hello", model=config.MODEL_BIG, client=client, thinking=True)

    assert client.calls[0]["thinking"] == {"type": "adaptive"}


def test_call_with_tools_thinking_requested_on_supported_model_is_attached():
    client = FakeClient()
    llm.call_with_tools(
        [{"role": "user", "content": "hi"}],
        tools=[],
        model=config.MODEL_MID,
        client=client,
        thinking=True,
    )

    assert client.calls[0]["thinking"] == {"type": "adaptive"}


def test_supported_models_not_requested_still_omits_thinking():
    """Even on a capable model, thinking stays off unless explicitly requested."""
    client = FakeClient()
    llm.ask("hello", model=config.MODEL_BIG, client=client)

    assert "thinking" not in client.calls[0]


# --- capability gate ---------------------------------------------------------


def test_model_supports_thinking_gate():
    assert not llm.model_supports_thinking("claude-haiku-4-5")
    assert llm.model_supports_thinking("claude-sonnet-4-6")
    assert llm.model_supports_thinking("claude-opus-4-8")
    # Env-pinned snapshot ids still resolve via substring match.
    assert llm.model_supports_thinking("claude-sonnet-4-6-20250101")
