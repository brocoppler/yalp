"""The yalp *voice* package — "talk to it", both directions.

This package is the robot's voice, split across two halves of the same goal:

* **Spoken OUTPUT (TTS).** Turning the deliberative brain's answers into speech
  out loud. Implemented today in :mod:`yalp.voice.tts` (macOS ``say`` /
  ``espeak-ng`` elsewhere) and re-exported here for back-compat.
* **Spoken INPUT (STT).** Turning what a human says back into text for the brain.
  That lives in sibling modules added later; this package is the home it will
  share with TTS so callers keep a single ``from yalp import voice`` import.

The public TTS surface is re-exported at the package level so existing callers
keep working *identically* — ``from yalp import voice`` then ``voice.speak(...)``,
``voice.wait_for_speech()`` and ``voice.tts_available()`` behave exactly as they
did when ``voice`` was a single module.

``shutil``/``subprocess``/``platform`` and the internal ``_spawn`` / module-level
binary names are also re-exported (the underlying modules are process-wide
singletons, so patching ``voice.shutil.which`` or ``voice.subprocess.Popen``
reaches the real implementation in :mod:`yalp.voice.tts`).
"""

from __future__ import annotations

# Standard-library modules used by the TTS implementation, re-exported so that
# callers/tests can reach them as ``voice.shutil`` / ``voice.subprocess`` /
# ``voice.platform`` (they are module singletons shared with ``.tts``).
import platform  # noqa: F401 - re-exported for back-compat / patchability
import shutil  # noqa: F401 - re-exported for back-compat / patchability
import subprocess  # noqa: F401 - re-exported for back-compat / patchability

from . import tts
from .tts import (  # noqa: F401 - re-exported public + back-compat names
    DEFAULT_VOICE,
    ESPEAK_BINARY,
    SAY_BINARY,
    _build_command,
    _spawn,
    _tts_binary,
    speak,
    tts_available,
    wait_for_speech,
)

# The live fire-and-forget process registry. Bound to the SAME list object as
# ``tts._live_processes`` (mutated in place by ``_spawn`` / ``wait_for_speech``),
# so ``voice._live_processes`` always reflects the real state.
_live_processes = tts._live_processes

__all__ = [
    "speak",
    "wait_for_speech",
    "tts_available",
    "SAY_BINARY",
    "ESPEAK_BINARY",
    "DEFAULT_VOICE",
]
