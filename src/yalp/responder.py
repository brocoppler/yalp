"""Answer delivery channels for yalp — *where a reply actually lands*.

This module is the output-channel abstraction the deliberative brain was missing.
Answer delivery used to be ad hoc: the agent loop poked ``voice.speak`` directly,
and :func:`yalp.voice.tts.speak` silently no-ops when no ``say`` / ``espeak-ng``
binary exists. On the assembled robot — which today ships with **no speaker at
all** (a deferred purchase) — a spoken answer therefore vanished without a
trace. That is exactly the failure mode this seam removes: **an answer is ALWAYS
delivered somewhere.**

Why top-level (NOT under ``voice/``)?
-------------------------------------
Delivering an answer is not a *voice* concern. Speech is only ONE channel;
plain text on the console is another, and a future remote/Telegram bridge is a
third. Putting :class:`Responder` under ``voice/`` would wrongly imply answers
are a text-to-speech feature and couple every non-voice channel to the audio
stack. It lives at the package top level because it is the neutral seam the
whole deliberative layer delivers through, of which ``voice`` is just one
implementation (:class:`TtsResponder`).

The shape
---------
* :class:`Responder` — a tiny ``Protocol``: ``respond(text, *, kind=...)`` where
  ``kind`` is one of :data:`KIND_ANSWER` / :data:`KIND_STATUS` / :data:`KIND_ERROR`.
* :class:`ConsoleResponder` — text-first, always available; prints with a clear,
  greppable per-kind prefix so any reply is trivially found in stdout/logs.
* :class:`TtsResponder` — an *additive* speech plug-in wrapping
  :func:`yalp.voice.tts.speak`. When TTS is unavailable it logs a WARNING **once**
  and falls through — it NEVER silently drops the answer (in a composite the text
  channel has already delivered it).
* :class:`CompositeResponder` — fan-out to several channels; a failure in one is
  logged and never propagated, so a broken channel can't sink the others.

Extension point (NOT implemented here — future work)
-----------------------------------------------------
A ``RemoteResponder`` could stream answers back to an operator over the existing
IPC seam (:mod:`yalp.contract.ipc`) or a Telegram bridge, so the robot can be
driven and answered headlessly. It would only need to implement
``respond(text, *, kind=...)`` — **the Responder interface IS the extension
point**; nothing else in the deliberative layer changes to gain a new channel.
"""

from __future__ import annotations

import logging
import sys
from typing import Callable, Optional, Protocol, runtime_checkable

logger = logging.getLogger("yalp.responder")

# The recognized answer kinds.
#   answer -> the robot's actual reply to the user (the payload that must land).
#   status -> progress / heartbeat ("still driving…").
#   error  -> a failure surfaced to the user ("couldn't reach the camera").
KIND_ANSWER = "answer"
KIND_STATUS = "status"
KIND_ERROR = "error"
KINDS = (KIND_ANSWER, KIND_STATUS, KIND_ERROR)


@runtime_checkable
class Responder(Protocol):
    """Something that can deliver a reply to the user, on some channel.

    Implementations promise to be *best-effort and non-fatal*: delivering an
    answer must never raise into or wedge the deliberative loop. ``kind`` is one
    of :data:`KINDS` (defaulting to ``"answer"``) so a channel can render / route
    progress and errors differently from the answer itself.
    """

    def respond(self, text: str, *, kind: str = KIND_ANSWER) -> None:  # pragma: no cover - Protocol
        ...


# Clear, greppable per-kind prefixes: ``grep '\[yalp:answer\]'`` finds every
# reply the robot ever gave, even with no screen and no speaker attached.
_CONSOLE_PREFIXES = {
    KIND_ANSWER: "[yalp:answer]",
    KIND_STATUS: "[yalp:status]",
    KIND_ERROR: "[yalp:error]",
}


class ConsoleResponder:
    """Text-first channel: print the reply with a greppable per-kind prefix.

    This is the always-available default so answers can NEVER vanish — even on a
    speakerless robot, a reply lands on stdout (or logs) tagged by kind. The
    stream is resolved lazily at call time (defaulting to ``sys.stdout``) so it
    honors pytest's captured stdout and any later stream redirection.
    """

    def __init__(self, *, stream: Optional[object] = None) -> None:
        self._stream = stream

    def respond(self, text: str, *, kind: str = KIND_ANSWER) -> None:
        prefix = _CONSOLE_PREFIXES.get(kind, f"[yalp:{kind}]")
        stream = self._stream if self._stream is not None else sys.stdout
        print(f"{prefix} {text}", file=stream)


class TtsResponder:
    """Speak the reply aloud via :func:`yalp.voice.tts.speak` — an additive plug-in.

    Speech is *fire-and-forget* (see :mod:`yalp.voice.tts`), so this never blocks
    the caller. If TTS is unavailable (no ``say`` / ``espeak-ng`` binary — e.g.
    the robot's speaker is still a deferred purchase) it logs a WARNING **once**
    and falls through: it NEVER silently drops the answer. Paired with a
    :class:`ConsoleResponder` inside a :class:`CompositeResponder`, the reply has
    already landed as text, so nothing is lost — the warning just records that the
    *spoken* copy was skipped. Any spawn failure is likewise logged, never raised.

    ``speak`` / ``available`` are injectable (defaulting to the real
    :mod:`yalp.voice.tts` entry points, resolved lazily so importing this module
    never drags in the voice stack) which keeps unit tests audio-free.
    """

    def __init__(
        self,
        speak: Optional[Callable[[str], None]] = None,
        available: Optional[Callable[[], bool]] = None,
    ) -> None:
        self._speak = speak
        self._available = available
        # Guard so the "no TTS" warning is logged at most once per responder.
        self._warned_unavailable = False

    def _resolve(self) -> tuple[Callable[[str], None], Callable[[], bool]]:
        """Return the (speak, available) callables — injected or the real ones."""
        if self._speak is not None and self._available is not None:
            return self._speak, self._available
        from yalp.voice import tts  # lazy: don't import the voice stack at import time

        return (self._speak or tts.speak), (self._available or tts.tts_available)

    def respond(self, text: str, *, kind: str = KIND_ANSWER) -> None:
        body = (text or "").strip()
        if not body:
            return
        speak, available = self._resolve()

        try:
            can_speak = bool(available())
        except Exception:  # noqa: BLE001 - a broken probe must not wedge delivery
            can_speak = False

        if not can_speak:
            if not self._warned_unavailable:
                self._warned_unavailable = True
                logger.warning(
                    "TTS unavailable (no speaker / 'say' / 'espeak-ng') — spoken "
                    "answers are disabled; replies still reach other channels "
                    "(e.g. the console). Logged once per responder."
                )
            return  # fall through — never silently drop, other channels deliver.

        try:
            speak(body)
        except Exception as exc:  # noqa: BLE001 - speech is best-effort, never fatal
            logger.warning(
                "TTS delivery failed (%s: %s) — this reply was not spoken.",
                type(exc).__name__, exc,
            )


class CompositeResponder:
    """Fan out one reply to several channels, isolating failures.

    Each child responder is called in turn; an exception in one is logged and
    swallowed so the remaining channels still deliver (a broken speaker must not
    sink the console copy). This is how "text now, speech as a plug-in, a remote
    channel as a drop-in" is composed: ``CompositeResponder(console, tts, remote)``.
    """

    def __init__(self, *responders: Responder) -> None:
        self._responders: list[Responder] = list(responders)

    def respond(self, text: str, *, kind: str = KIND_ANSWER) -> None:
        for responder in self._responders:
            try:
                responder.respond(text, kind=kind)
            except Exception as exc:  # noqa: BLE001 - one bad channel can't sink the rest
                logger.warning(
                    "responder %s failed on kind=%s (%s: %s) — continuing fan-out.",
                    type(responder).__name__, kind, type(exc).__name__, exc,
                )


__all__ = [
    "Responder",
    "ConsoleResponder",
    "TtsResponder",
    "CompositeResponder",
    "KIND_ANSWER",
    "KIND_STATUS",
    "KIND_ERROR",
    "KINDS",
]
