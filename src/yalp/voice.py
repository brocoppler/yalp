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
import time
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


# Outstanding fire-and-forget ``say`` processes, so a caller (e.g. the agent
# CLI) can briefly join them before the process exits — otherwise the FINAL
# utterance is cut off the instant the agent finishes and Python tears down.
_live_processes: list[subprocess.Popen] = []


def _spawn(cmd: list[str]) -> None:
    """Fire-and-forget spawn of the TTS command (factored out for mocking).

    Uses :func:`subprocess.Popen` and does NOT wait, so a long utterance never
    blocks the caller. The handle is tracked in ``_live_processes`` so
    :func:`wait_for_speech` can drain the last utterance before the process
    exits. Tests monkeypatch this to record the command instead of making noise.
    """
    proc = subprocess.Popen(  # noqa: S603 - command is our own fixed binary + text arg
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    # Prune finished handles so a long interactive session can't leak them.
    _live_processes[:] = [p for p in _live_processes if p.poll() is None]
    _live_processes.append(proc)


def wait_for_speech(timeout: float = 10.0) -> None:
    """Block until outstanding fire-and-forget speech drains — best-effort.

    Mid-loop speech is fire-and-forget so the loop stays responsive, but if the
    process exits the instant the agent finishes the LAST utterance (the final
    report) would be cut off. A caller can invoke this right before returning to
    let that speech finish. Bounded by ``timeout`` (total seconds) so a wedged
    ``say`` can never hang the CLI, and it NEVER raises — draining voice is
    best-effort, exactly like :func:`speak`.
    """
    procs = list(_live_processes)
    _live_processes.clear()
    deadline = time.monotonic() + max(0.0, timeout)
    for proc in procs:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            proc.wait(timeout=remaining)
        except Exception:  # noqa: BLE001 - incl. TimeoutExpired; never hang/raise
            pass


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


__all__ = ["speak", "wait_for_speech", "tts_available", "SAY_BINARY", "DEFAULT_VOICE"]
