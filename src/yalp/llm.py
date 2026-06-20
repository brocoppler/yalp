"""A thin wrapper over the Anthropic SDK for yalp's deliberative layer.

One job: send a text prompt plus an optional image to a Claude model and return
the text reply. The model id is chosen by the caller (the tiered router in
software-spec.md §3 picks fast/mid/big); adaptive thinking stays on at every
tier so the model spends more reasoning on hard turns and little on easy ones.

Designed to be trivially mockable in tests:

  * The Anthropic client is *injectable* — pass any object exposing
    ``messages.create(...)`` to ``ask()`` and no real client is constructed.
  * The API key is read *lazily*, only when a real client must be built — so
    importing this module (and calling ``ask`` with a fake client) needs no key
    and no network.

The ``anthropic`` package is imported lazily inside the client factory for the
same reason: tests that inject a fake client never import the SDK.
"""

from __future__ import annotations

import base64
from typing import Any, Optional

from . import config


def build_client(api_key: Optional[str] = None) -> Any:
    """Construct a real ``anthropic.Anthropic`` client.

    The key is resolved lazily: explicit ``api_key`` wins, otherwise
    ``config.require_api_key()`` reads the environment (raising a friendly error
    if unset). Import of the SDK is deferred to here so the module imports with
    no dependency on ``anthropic`` being installed.
    """
    import anthropic  # local import: keeps module import light + mockable

    key = api_key or config.require_api_key()
    return anthropic.Anthropic(api_key=key)


def _image_block(image: bytes, media_type: str = "image/jpeg") -> dict:
    """Build a base64 image content block for the Messages API."""
    data = base64.standard_b64encode(image).decode("utf-8")
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": media_type, "data": data},
    }


def ask(
    prompt: str,
    *,
    model: str = config.MODEL_FAST,
    image: Optional[bytes] = None,
    image_media_type: str = "image/jpeg",
    client: Any = None,
    max_tokens: int = 1024,
    system: Optional[str] = None,
) -> str:
    """Send ``prompt`` (and an optional ``image``) to ``model``; return the text reply.

    Parameters
    ----------
    prompt:
        The user text.
    model:
        Claude model id (defaults to the fast tier). The caller's router decides.
    image:
        Optional raw image bytes (e.g. JPEG from ``camera.encode_jpeg``); base64
        encoded into an image content block.
    client:
        An object exposing ``messages.create(...)``. If None, a real client is
        built via ``build_client()`` (which requires a key). Inject a fake here
        in tests to run with no network and no key.
    max_tokens, system:
        Standard Messages API knobs.

    Returns the concatenated text of the response's text blocks.
    """
    if client is None:
        client = build_client()

    content: list[dict] = [{"type": "text", "text": prompt}]
    if image is not None:
        content.append(_image_block(image, image_media_type))

    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        # Adaptive thinking on at every tier (software-spec.md §3).
        "thinking": {"type": "adaptive"},
        "messages": [{"role": "user", "content": content}],
    }
    if system is not None:
        kwargs["system"] = system

    response = client.messages.create(**kwargs)
    return _extract_text(response)


def _extract_text(response: Any) -> str:
    """Join the text blocks of a Messages API response into a single string."""
    parts: list[str] = []
    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", None) == "text":
            parts.append(getattr(block, "text", ""))
    return "".join(parts)


__all__ = ["ask", "build_client"]
