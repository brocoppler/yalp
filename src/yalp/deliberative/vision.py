"""The magic moment: grab a still, ask Claude what it sees, return the words.

This is yalp's first real win (roadmap Step 4 — "It sees and talks"). It runs on
the laptop today with no robot: the camera falls back to the built-in webcam (or
a synthetic frame), the still is downscaled + JPEG-encoded, and a single
deliberative step sends it to a Claude vision model with a question
(default: "What do you see?").

Design notes
------------
* **Stills, not video** (software-spec.md §3): one frame per question. The
  biggest controllable latency lever is keeping the uploaded image small, so we
  reuse ``camera.encode_jpeg`` to downscale + compress before upload.
* **Fast tier by default** (the tiered-model decision, software-spec.md §3):
  visual Q&A is a quick perceive→answer turn, so it defaults to ``MODEL_FAST``
  (Haiku). The caller may pass ``MODEL_MID``/``MODEL_BIG`` for harder asks.
* **Import-clean without the SDK**: this module imports only ``config`` and
  ``llm`` at module load (both stdlib-light); ``anthropic`` is imported lazily
  by ``llm`` only when a real client is built, and ``camera``/``cv2``/``numpy``
  are imported lazily only when a frame must actually be grabbed or encoded. So
  tests can mock the LLM (inject a fake ``client``) with neither the SDK, a key,
  a camera, nor a network.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional, Union

from .. import config, llm

# The default question — the product promise is "point me at something and ask
# what it is" (product-spec.md, visual Q&A).
DEFAULT_QUESTION = "What do you see?"

# A short system prompt so the answer feels like the robot's own eyes talking:
# concrete, grounded in what's actually visible, and pleasant to read aloud.
SYSTEM_PROMPT = (
    "You are the eyes of yalp, a small, friendly home robot. You are shown a "
    "single still frame from your camera. Describe what you actually see in a "
    "warm, concrete, conversational way — name the main objects, people, and "
    "setting, and answer any question directly. Keep it to a few sentences. If "
    "the image is dark, blurry, or empty, say so plainly instead of guessing."
)

# Printed (not raised) when no API key is configured, so the magic moment never
# crashes on a fresh laptop — it points the user at the setup guide instead.
MISSING_KEY_MESSAGE = (
    "I can see, but I can't talk yet: no ANTHROPIC_API_KEY is set.\n"
    "Add your Anthropic key to start describing scenes:\n"
    "  1. cp .env.example .env\n"
    "  2. put ANTHROPIC_API_KEY=sk-ant-... in .env\n"
    "See SETUP.md (step 5) for the full walkthrough."
)

# A frame may be raw image bytes, an OpenCV BGR ndarray, or None ("grab one").
FrameLike = Union[bytes, bytearray, "Any", None]


def describe_scene(
    frame: FrameLike = None,
    question: Optional[str] = None,
    *,
    model: str = config.MODEL_FAST,
    client: Any = None,
    camera: Any = None,
    image_media_type: str = "image/jpeg",
    max_width: int = 640,
    jpeg_quality: int = 80,
) -> str:
    """Describe ``frame`` (or a freshly grabbed one) by asking a Claude model.

    Parameters
    ----------
    frame:
        Raw JPEG/PNG ``bytes`` (sent as-is), a BGR ndarray (downscaled +
        JPEG-encoded via ``camera.encode_jpeg``), or ``None`` to grab the newest
        frame from the camera.
    question:
        What to ask about the scene. Falls back to :data:`DEFAULT_QUESTION`.
    model:
        Claude model id; defaults to the fast tier (Haiku).
    client:
        An object exposing ``messages.create(...)``. Injected by tests to run
        with no SDK/key/network. If ``None``, ``llm`` builds a real client
        (which requires a key).
    camera:
        An optional pre-started camera (anything with ``latest()`` /
        ``wait_for_frame()``); used only when ``frame is None``. If omitted, a
        throwaway :class:`yalp.camera.Camera` is started and stopped here.
    image_media_type, max_width, jpeg_quality:
        Upload knobs. ``max_width``/``jpeg_quality`` apply only when encoding an
        ndarray frame.

    Returns the model's text answer.
    """
    prompt = (question or "").strip() or DEFAULT_QUESTION
    image_bytes = _to_jpeg_bytes(
        frame, camera=camera, max_width=max_width, quality=jpeg_quality
    )
    return llm.ask(
        prompt,
        model=model,
        image=image_bytes,
        image_media_type=image_media_type,
        client=client,
        system=SYSTEM_PROMPT,
    )


def see(
    *,
    image_path: Optional[str] = None,
    question: Optional[str] = None,
    model: str = config.MODEL_FAST,
    client: Any = None,
    camera: Any = None,
) -> str:
    """High-level entrypoint for the ``see`` command and the magic-moment script.

    Resolves the image source (an ``--image`` file, an injected camera, or a
    live grab), then calls :func:`describe_scene`. If no API key is configured
    (and no client was injected), returns :data:`MISSING_KEY_MESSAGE` rather than
    raising, so the first run on a fresh laptop is friendly instead of a crash.
    """
    if client is None and config.get_api_key() is None:
        return MISSING_KEY_MESSAGE

    frame: FrameLike = None
    media_type = "image/jpeg"
    if image_path is not None:
        frame, media_type = _load_image_file(image_path)

    return describe_scene(
        frame,
        question,
        model=model,
        client=client,
        camera=camera,
        image_media_type=media_type,
    )


def format_answer(text: str, question: Optional[str] = None) -> str:
    """Render an answer as a small, pleasant block for the terminal."""
    asked = (question or "").strip() or DEFAULT_QUESTION
    body = (text or "").strip() or "(no description returned)"
    line = "─" * 60
    return f"{line}\n👁  {asked}\n\n{body}\n{line}"


# --- internals --------------------------------------------------------------
def _to_jpeg_bytes(
    frame: FrameLike, *, camera: Any, max_width: int, quality: int
) -> bytes:
    """Coerce a frame (bytes / ndarray / None) into upload-ready JPEG bytes."""
    if isinstance(frame, (bytes, bytearray)):
        return bytes(frame)
    if frame is None:
        frame = _grab_frame(camera)
    # An ndarray BGR frame: downscale + JPEG-encode (lazy cv2 import lives here).
    from ..camera import encode_jpeg

    return encode_jpeg(frame, max_width=max_width, quality=quality)


def _grab_frame(camera: Any) -> Any:
    """Return the newest frame from ``camera``, starting a throwaway one if None."""
    if camera is not None:
        frame = _latest_from(camera)
        if frame is None:
            raise RuntimeError("camera produced no frame yet")
        return frame

    from ..camera import Camera

    cam = Camera().start()  # webcam, auto-falling-back to a synthetic frame
    try:
        frame = cam.wait_for_frame()
        if frame is None:
            raise RuntimeError("camera produced no frame")
        return frame
    finally:
        cam.stop()


def _latest_from(camera: Any) -> Any:
    """Pull a frame from an injected camera, preferring a brief blocking wait."""
    if hasattr(camera, "wait_for_frame"):
        return camera.wait_for_frame()
    return camera.latest()


def _load_image_file(path: str) -> tuple[bytes, str]:
    """Read an image file into upload bytes, downscaling via cv2 when available.

    Returns ``(image_bytes, media_type)``. We try to decode + downscale (cheaper
    upload) with OpenCV; if that's unavailable or the file isn't a decodable
    image, we fall back to the raw bytes with a media type guessed from the
    extension — so an explicit ``--image`` is never silently swapped for a
    synthetic frame.
    """
    raw = Path(path).read_bytes()
    try:
        import cv2  # local import: keep module import-clean without OpenCV
        import numpy as np

        arr = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
        if arr is not None:
            from ..camera import encode_jpeg

            return encode_jpeg(arr), "image/jpeg"
    except Exception:
        pass  # fall through to raw passthrough

    return raw, _guess_media_type(path)


def _guess_media_type(path: str) -> str:
    ext = Path(path).suffix.lower()
    return {
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
    }.get(ext, "image/jpeg")


__all__ = [
    "describe_scene",
    "see",
    "format_answer",
    "DEFAULT_QUESTION",
    "SYSTEM_PROMPT",
    "MISSING_KEY_MESSAGE",
]
