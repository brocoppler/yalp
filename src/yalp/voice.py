"""Spoken OUTPUT for yalp — the first half of the "talk to it" goal.

This is the robot's *voice*: a tiny, dependency-light text-to-speech shim so the
deliberative brain can SPEAK its answers out loud. Spoken INPUT (STT) is a
separate, later task — this module is output only.

Design rules
------------
* **No dependencies.** On macOS the built-in ``say`` command does all the work
  (``subprocess: say <text>``); there is nothing to ``pip install``. ``say`` can
  be pointed at a different voice / rate, but the defaults are sane.
* **Headless-safe / graceful degradation.** On a machine with no ``say`` binary
  (Linux, a CI box, the Pi before a TTS engine is wired) :func:`speak` becomes a
  no-op — it logs the missing capability ONCE and returns. :func:`speak` NEVER
  raises; a broken voice must never wedge the agent loop or crash a command.
* **Fire-and-forget.** ``say`` is spawned with :func:`subprocess.Popen` and we do
  NOT wait for it to finish: a long sentence keeps the loop responsive instead of
  blocking it for the duration of the speech. (We accept that we don't observe a
  non-zero exit code — speech is best-effort.)
* **Mockable.** The actual process spawn is factored into :func:`_spawn` and the
  capability probe into :func:`tts_available`, so tests can monkeypatch either to
  assert behavior WITHOUT emitting audio or assuming macOS.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from typing import Optional

logger = logging.getLogger("yalp.voice")

# The macOS built-in TTS binary. Override via env for exotic setups / testing.
SAY_BINARY: str = os.environ.get("YALP_SAY_BINARY", "say")

# Optional default voice (e.g. "Samantha", "Daniel") and speaking rate (words per
# minute). Empty / unset means "let ``say`` use its own default".
DEFAULT_VOICE: Optional[str] = os.environ.get("YALP_VOICE") or None


def _default_rate() -> Optional[int]:
    """Parse the optional default speaking rate (wpm) from the environment."""
    raw = os.environ.get("YALP_VOICE_RATE")
    if not raw:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


# Guard so a missing-capability warning is logged at most once per process.
_warned_unavailable = False


def tts_available() -> bool:
    """Return True if a usable ``say`` binary is on PATH (else speech no-ops).

    This is the capability probe: on macOS ``say`` is built in, so this is True;
    on Linux / CI / a bare Pi it is typically False and :func:`speak` degrades to
    a silent no-op.
    """
    return shutil.which(SAY_BINARY) is not None


def _build_command(text: str, voice: Optional[str], rate: Optional[int]) -> list[str]:
    """Build the ``say`` argv for ``text`` with the chosen voice/rate."""
    cmd = [SAY_BINARY]
    if voice:
        cmd += ["-v", voice]
    if rate:
        cmd += ["-r", str(rate)]
    cmd.append(text)
    return cmd


def _spawn(cmd: list[str]) -> None:
    """Fire-and-forget spawn of the TTS command (factored out for mocking).

    Uses :func:`subprocess.Popen` and does NOT wait, so a long utterance never
    blocks the caller. Tests monkeypatch this to record the command instead of
    making noise.
    """
    subprocess.Popen(  # noqa: S603 - command is our own fixed binary + text arg
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def speak(text: str, *, voice: Optional[str] = None, rate: Optional[int] = None) -> None:
    """Vocalize ``text`` out loud via macOS ``say`` — best-effort, never raises.

    Degrades gracefully: empty/blank text is ignored, and if no ``say`` binary is
    available the call becomes a silent no-op (warned once). Spawned
    fire-and-forget so it doesn't wedge the agent loop. Any spawn failure is
    swallowed (logged) rather than propagated — a broken voice must not crash the
    robot.
    """
    global _warned_unavailable

    body = (text or "").strip()
    if not body:
        return

    if not tts_available():
        if not _warned_unavailable:
            logger.info(
                "TTS unavailable (no '%s' binary) — speech disabled; "
                "yalp will stay silent.",
                SAY_BINARY,
            )
            _warned_unavailable = True
        return

    chosen_voice = voice if voice is not None else DEFAULT_VOICE
    chosen_rate = rate if rate is not None else _default_rate()
    cmd = _build_command(body, chosen_voice, chosen_rate)
    try:
        _spawn(cmd)
    except Exception as exc:  # noqa: BLE001 - speech is best-effort, never fatal
        logger.warning("TTS spawn failed (%s: %s) — staying silent.",
                       type(exc).__name__, exc)


__all__ = ["speak", "tts_available", "SAY_BINARY", "DEFAULT_VOICE"]
