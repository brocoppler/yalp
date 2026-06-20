"""Auditable model routing + a hard per-session budget (software-spec.md §3).

Two responsibilities, both pinned by the spec as *decisions, not vibes*:

  * :func:`pick_model` — an **explicit routing table**, not a heuristic guess.
    It **defaults to** ``config.MODEL_FAST`` (Haiku) and escalates **only** on
    the enumerated signals:

      - ``describe_scene(detail='full')`` or a read-text request → ``MODEL_BIG`` (Opus)
      - a multi-step ``explore`` request or a structured ``need_more_reasoning``
        flag from the cheap tier → ``MODEL_MID`` (Sonnet)
      - everything else → ``MODEL_FAST`` (Haiku)

    Nothing else escalates. **Every decision is logged** (tier + the signal that
    chose it) so routing is reviewable after the fact.

  * :class:`Budget` — a hard per-session **token/call ceiling**. A WiFi
    retry-storm or runaway escalation must not silently run up cost: the agent
    records usage after every model call and stops issuing calls once either cap
    is hit (the §5 outage path), rather than retrying forever.

Pure standard library — imports with no third-party deps so it is testable
anywhere.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

from .. import config

logger = logging.getLogger("yalp.router")

# Model tiers (software-spec.md §3). Resolved from config so the operator can
# pin different model IDs via the environment without editing source.
FAST = config.MODEL_FAST  # Haiku — routine per-step VQA / tool-picking
MID = config.MODEL_MID    # Sonnet — multi-step explore / need_more_reasoning
BIG = config.MODEL_BIG    # Opus — describe_scene(detail=full) / read-text


# --- request-shape detectors ------------------------------------------------
# Keep these intentionally small and explicit: routing must be reviewable.
_READ_TEXT_RE = re.compile(
    r"\b(read|reads?|reading)\b.*\b(sign|signs|label|labels|text|screen|"
    r"writing|words?|page|note|menu)\b"
    r"|\bwhat\s+does\s+(it|this|that|the\s+\w+)\s+say\b"
    r"|\bsays?\s+on\b",
    re.IGNORECASE,
)
_EXPLORE_RE = re.compile(
    r"\b(explore|look\s+around|have\s+a\s+look\s+around|go\s+(and\s+)?look|"
    r"scout|wander|patrol|check\s+out\s+the\s+(room|area|place|house)|"
    r"go\s+see\s+what'?s)\b",
    re.IGNORECASE,
)


def is_read_text_request(user_text: str) -> bool:
    """True when the user is asking the robot to read text (a sign/label/screen)."""
    return bool(_READ_TEXT_RE.search(user_text or ""))


def is_multi_step_explore_request(user_text: str) -> bool:
    """True when the user is asking the robot to go explore / look around.

    ``explore`` is deliberative sugar (a loop of drive/turn/describe_scene,
    §2.1) — there is no reactive EXPLORE mode — so it is the deliberative side
    that needs the extra planning reasoning, hence the Sonnet escalation.
    """
    return bool(_EXPLORE_RE.search(user_text or ""))


@dataclass
class RoutingContext:
    """Everything :func:`route` needs to choose a tier (software-spec.md §3).

    ``user_text`` is the current request; ``intent_name`` / ``intent_detail``
    are set when routing a *specific* ability dispatch (e.g. the agent routing a
    ``describe_scene`` vision call); ``need_more_reasoning`` is the structured
    flag the cheap tier can raise to ask for a Sonnet escalation.
    """

    user_text: str = ""
    intent_name: Optional[str] = None
    intent_detail: Optional[str] = None
    need_more_reasoning: bool = False


@dataclass(frozen=True)
class Decision:
    """A routing decision: the chosen ``model`` and the ``reason`` it was chosen."""

    model: str
    reason: str


def route(context: RoutingContext) -> Decision:
    """Pick a model tier from ``context`` and log the choice (tier + signal).

    Escalation order matches the spec exactly; the first matching signal wins.
    """
    # OPUS — only for genuinely hard vision / reading.
    if context.intent_name == "describe_scene" and context.intent_detail == "full":
        return _log(BIG, "describe_scene(detail=full)")
    if is_read_text_request(context.user_text):
        return _log(BIG, "read-text intent")

    # SONNET — multi-step reasoning the cheap tier asked for, or an explore plan.
    if is_multi_step_explore_request(context.user_text):
        return _log(MID, "multi-step explore request")
    if context.need_more_reasoning:
        return _log(MID, "cheap-tier need_more_reasoning")

    # HAIKU — default for routine per-step VQA / tool-picking.
    return _log(FAST, "default")


def pick_model(context: RoutingContext) -> str:
    """Convenience wrapper: return just the model id (and log the decision)."""
    return route(context).model


def _log(model: str, reason: str) -> Decision:
    logger.info("model_router: tier=%s reason=%s", model, reason)
    return Decision(model=model, reason=reason)


class BudgetExceeded(RuntimeError):
    """Raised by :meth:`Budget.charge` when a model call would exceed the cap."""


@dataclass
class Budget:
    """A hard per-session token/call ceiling (software-spec.md §3).

    The agent checks :meth:`exhausted` *before* each model call and stops the
    loop cleanly when it returns True (falling back to IDLE/local behavior, the
    §5 outage path), and calls :meth:`charge` *after* each call to accumulate
    usage. :meth:`remaining` exposes the headroom left for diagnostics / the
    transcript.
    """

    max_calls: int = config.BUDGET_MAX_CALLS
    max_tokens: int = config.BUDGET_MAX_TOKENS
    calls_used: int = 0
    tokens_used: int = 0

    def exhausted(self) -> bool:
        """True once either cap has been reached (no further calls should issue)."""
        return self.calls_used >= self.max_calls or self.tokens_used >= self.max_tokens

    def charge(self, input_tokens: int = 0, output_tokens: int = 0) -> None:
        """Record one model call's usage; raise once a cap is crossed.

        Always books the call/tokens first (so :meth:`remaining` is accurate),
        then raises :class:`BudgetExceeded` if the session is now over a cap —
        letting the agent surface the stop cleanly.
        """
        self.calls_used += 1
        self.tokens_used += int(input_tokens) + int(output_tokens)
        if self.exhausted():
            raise BudgetExceeded(
                f"per-session budget exhausted: {self.calls_used}/{self.max_calls} "
                f"calls, {self.tokens_used}/{self.max_tokens} tokens"
            )

    def remaining(self) -> dict:
        """Return headroom left as ``{'calls': int, 'tokens': int}`` (never negative)."""
        return {
            "calls": max(0, self.max_calls - self.calls_used),
            "tokens": max(0, self.max_tokens - self.tokens_used),
        }


__all__ = [
    "FAST",
    "MID",
    "BIG",
    "RoutingContext",
    "Decision",
    "route",
    "pick_model",
    "is_read_text_request",
    "is_multi_step_explore_request",
    "Budget",
    "BudgetExceeded",
    "logger",
]
