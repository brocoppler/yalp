"""Tests for the magic-moment vision path — no network, key, camera, or SDK.

The Anthropic client is *injected* as a fake (a recording stub), so these tests
exercise the real ``describe_scene`` / ``see`` plumbing without the ``anthropic``
package, an API key, a camera, or a network call. The sample JPEG bytes stand in
for a grabbed frame.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from yalp.deliberative import vision

SAMPLE = Path(__file__).parent / "assets" / "sample.jpg"


class FakeClient:
    """A stand-in Anthropic client that records the call and returns canned text."""

    def __init__(self, reply: str = "I see a cozy desk with a coffee mug and a laptop."):
        self.reply = reply
        self.calls: list[dict] = []
        # Mirror the SDK shape: client.messages.create(...).
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        block = SimpleNamespace(type="text", text=self.reply)
        return SimpleNamespace(content=[block])


def _content(call: dict) -> list:
    return call["messages"][0]["content"]


def test_describe_scene_returns_mocked_text_and_includes_image_block():
    client = FakeClient()
    frame = SAMPLE.read_bytes()  # raw JPEG bytes act as the frame

    out = vision.describe_scene(frame, client=client)

    assert out == client.reply
    assert len(client.calls) == 1

    content = _content(client.calls[0])
    image_blocks = [b for b in content if b.get("type") == "image"]
    assert image_blocks, "an image block must be included in the API call"
    # The image is base64-encoded JPEG, sent inline.
    source = image_blocks[0]["source"]
    assert source["type"] == "base64"
    assert source["media_type"] == "image/jpeg"
    assert source["data"], "image data must be non-empty"


def test_default_question_is_used_when_none_given():
    client = FakeClient()
    vision.describe_scene(SAMPLE.read_bytes(), client=client)

    content = _content(client.calls[0])
    text_blocks = [b for b in content if b.get("type") == "text"]
    assert text_blocks
    assert text_blocks[0]["text"] == vision.DEFAULT_QUESTION


def test_custom_question_is_passed_through():
    client = FakeClient()
    vision.describe_scene(SAMPLE.read_bytes(), "How many cups?", client=client)

    content = _content(client.calls[0])
    text_blocks = [b for b in content if b.get("type") == "text"]
    assert text_blocks[0]["text"] == "How many cups?"


def test_defaults_to_fast_model():
    from yalp import config

    client = FakeClient()
    vision.describe_scene(SAMPLE.read_bytes(), client=client)
    assert client.calls[0]["model"] == config.MODEL_FAST


def test_see_missing_key_is_friendly_not_an_exception(monkeypatch):
    # No key, no injected client -> friendly message, never an exception.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    out = vision.see(image_path=str(SAMPLE), question="what is this?")

    assert out == vision.MISSING_KEY_MESSAGE
    assert "SETUP.md" in out
    assert "ANTHROPIC_API_KEY" in out


def test_see_with_image_path_and_client(monkeypatch):
    # A key is present (so see() proceeds), but the injected client means no
    # network/SDK is touched.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    client = FakeClient(reply="A small robot's-eye view of a tidy room.")

    out = vision.see(image_path=str(SAMPLE), question="describe this", client=client)

    assert out == client.reply
    content = _content(client.calls[0])
    assert any(b.get("type") == "image" for b in content)


def test_format_answer_includes_question_and_body():
    rendered = vision.format_answer("It's a mug.", "what is that?")
    assert "what is that?" in rendered
    assert "It's a mug." in rendered


def test_format_answer_falls_back_to_default_question():
    rendered = vision.format_answer("Something.", None)
    assert vision.DEFAULT_QUESTION in rendered
