"""The agent loop — yalp's deliberative brain (software-spec.md §3).

This is the slow loop that turns plain-language commands into **tool calls**
against the fixed ability menu (``abilities.ANTHROPIC_TOOLS``), dispatches each
resulting ``Intent`` to the reactive layer over the socket, reads the new
``RobotState`` back, and decides the next step — repeating until the model emits
no more tool calls, the goal completes/blocks, or the per-session budget is
exhausted.

The contract this honors (all pinned by the spec):

  * **The model conducts; the reactive layer plays.** The LLM never twitches a
    motor — it picks intents; the reactive layer executes them at 10–30 Hz and
    enforces safety (§2). ``intent_for()`` maps a tool call to an ``Intent``.
  * **Stills, not video** (§3). One deliberative step is perceive → think → act
    → report. Between steps the reactive layer keeps the robot safe.
  * **Tiered, auditable routing** (§3). :mod:`.model_router` defaults to Haiku
    and escalates only on enumerated signals, logging every choice.
  * **A hard per-session budget** (§3). A retry-storm can't run up cost — once
    the cap is hit the agent stops calling the model and falls back to IDLE
    (the §5 outage path).
  * **``explore`` is deliberative sugar** (§2.1). "Go look around and report" is
    an agent-level loop of drive/turn/describe_scene — there is **no reactive
    EXPLORE mode**.
  * **Honest open-loop awareness.** :func:`build_context` tells the model that
    distances/angles are approximate (no encoders) and that after a collision
    the robot **halts and reports BLOCKED rather than reversing**, so it plans
    accordingly.
  * **Graceful degradation** (§5). A failed/timed-out model call backs off and
    retries within budget rather than wedging; the reactive layer stays safe
    regardless.

The Agent takes a ``DeliberativeClient`` (duck-typed: anything exposing
``send_intent(intent)`` and ``request_state(timeout=...)``) so it can drive the
fake backend in dev or the real Pi backend later, plus a ``describe_scene``
callable (so vision can be injected/mocked). Both the LLM client and the
describe callable are injectable, so the whole loop runs in tests with no SDK,
key, camera, or network.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .. import config, llm
from ..responder import ConsoleResponder, Responder
from ..contract.abilities import (
    ABILITY_BY_NAME,
    ANTHROPIC_TOOLS,
    KIND_CONTROL,
    KIND_DELIBERATIVE,
    KIND_INTENT,
    KIND_QUERY,
    intent_for,
)
from ..contract.messages import (
    GoalStatus,
    Intent,
    Mode,
    RobotState,
    clamp_speed_limit,
)
from . import model_router
from .model_router import Budget, BudgetExceeded, RoutingContext

# Type of the injected vision callable. The agent calls it as
# ``describe(detail=..., question=..., model=...)`` and expects the text answer.
DescribeScene = Callable[..., str]

# The system prompt that frames the model as yalp's deliberative pilot.
AGENT_SYSTEM = (
    "You are the deliberative brain of yalp, a small two-wheeled home robot. You "
    "turn the user's plain-language requests into tool calls from a fixed ability "
    "menu. You never drive motors directly: you pick intents (drive / turn / stop "
    "/ follow), ask questions the robot can answer from its state (check_distance "
    "/ look), or describe what the camera sees (describe_scene). A separate fast "
    "loop on the robot executes your intents and keeps the robot safe.\n\n"
    "Ground rules of this body (plan around them):\n"
    "  - There are NO wheel encoders and NO IMU. Drive distances and turn angles "
    "are TIMED, open-loop guesses; a 'completed (timed, unverified)' status means "
    "'ran the move for about that long', not 'arrived exactly there'.\n"
    "  - Collision-stop overrides everything. After hitting something the robot "
    "HALTS and reports BLOCKED — it does NOT back up on its own (there is no rear "
    "sensor). If you are BLOCKED, do not blindly drive backward; turn to a clear "
    "heading or stop and report.\n"
    "  - distance_known == false means the ultrasonic reading is UNKNOWN (not "
    "clear) — treat it as 'might be blocked'.\n"
    "  - Work in small steps and re-check state. Stop when the request is done."
)


@dataclass
class TranscriptEntry:
    """One line of the run transcript (what the model said / a tool / a state).

    ``kind`` is one of ``"model"`` (model text), ``"tool"`` (a tool call),
    ``"state"`` (a ``RobotState`` observed after an intent), or ``"note"``
    (routing / budget / degradation events). ``data`` carries structured detail
    for callers that want more than the rendered ``text``.
    """

    kind: str
    text: str
    data: dict = field(default_factory=dict)


class Agent:
    """The deliberative step loop (software-spec.md §3)."""

    def __init__(
        self,
        client: Any,
        reactive: Any,
        describe_scene: DescribeScene,
        *,
        budget: Optional[Budget] = None,
        tools: Optional[list[dict]] = None,
        system: str = AGENT_SYSTEM,
        capture_still: Optional[Callable[[], Optional[bytes]]] = None,
        max_steps: int = 12,
        max_tokens: int = 1024,
        settle_timeout: float = 5.0,
        poll_timeout: float = 0.5,
        poll_interval: float = 0.03,
        max_retries: int = 3,
        retry_backoff: float = 0.2,
        explore_legs: int = 2,
        responder: Optional[Responder] = None,
        speak: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.client = client
        self.reactive = reactive
        self.describe_scene = describe_scene
        # The output CHANNEL the robot's user-facing answers are delivered
        # through (yalp.responder). Defaults to ConsoleResponder so a reply always
        # lands SOMEWHERE greppable even on a speakerless robot; the CLI injects a
        # CompositeResponder(Console, Tts) under --speak, and a future
        # RemoteResponder drops in the same way. This is the seam that replaces
        # the old ad-hoc ``voice.speak`` poke.
        self.responder: Responder = responder if responder is not None else ConsoleResponder()
        # Legacy spoken-OUTPUT callback (superseded by a TtsResponder). Kept for
        # back-compat: when set it vocalizes model lines exactly as before; the
        # CLI no longer wires it (speech now flows through the responder). Default
        # OFF — additive, and the printed transcript is unchanged either way.
        self._speak = speak
        self.budget = budget if budget is not None else Budget()
        self.tools = tools if tools is not None else ANTHROPIC_TOOLS
        self.system = system
        self.capture_still = capture_still
        self.max_steps = max_steps
        self.max_tokens = max_tokens
        self.settle_timeout = settle_timeout
        self.poll_timeout = poll_timeout
        self.poll_interval = poll_interval
        self.max_retries = max(1, max_retries)
        self.retry_backoff = retry_backoff
        self.explore_legs = explore_legs

        self._seq = 0
        self._user_text = ""
        self._transcript: list[TranscriptEntry] = []
        # Texts already spoken this turn, so identical narration + speak-tool text
        # (or a repeated line) is vocalized at most once (no double-speak) via the
        # legacy ``speak`` callback.
        self._spoken: set[str] = set()
        # Answers already delivered through the responder this turn — same
        # no-repeat guard for the answer CHANNEL, kept separate from ``_spoken``
        # so the two output paths never entangle.
        self._answered: set[str] = set()

    # -- public API ----------------------------------------------------------
    def run_turn(self, user_text: str) -> list[TranscriptEntry]:
        """Run one user request to completion; return the transcript.

        Loops: read state → pick model → call model with the ability menu → for
        each tool_use, map to an Intent / answer, dispatch, observe the new
        state, feed results back → repeat until the model stops calling tools,
        the goal terminates, or the budget is exhausted.
        """
        self._user_text = user_text or ""
        self._transcript = []
        self._spoken = set()
        self._answered = set()
        messages: list[dict] = [self._build_user_turn(self._user_text)]

        for _step in range(self.max_steps):
            if self.budget.exhausted():
                self._note(
                    "budget exhausted — stopping and falling back to IDLE (§5).",
                    remaining=self.budget.remaining(),
                )
                self._fall_to_idle()
                break

            decision = model_router.route(
                RoutingContext(user_text=self._user_text)
            )
            self._note(f"routed to {decision.model} ({decision.reason})",
                       model=decision.model, reason=decision.reason)

            turn = self._call_model(messages, decision.model)
            if turn is None:
                # Model unreachable after retries: issue no new intent this step
                # and stop. The reactive layer keeps the robot safe regardless.
                break

            try:
                self.budget.charge(turn.input_tokens, turn.output_tokens)
                budget_stop = False
            except BudgetExceeded as exc:
                self._note(str(exc), remaining=self.budget.remaining())
                budget_stop = True

            if turn.content is not None:
                messages.append({"role": "assistant", "content": turn.content})
            if turn.text.strip():
                self._record("model", turn.text.strip())

            if budget_stop:
                self._fall_to_idle()
                break

            if not turn.tool_uses:
                break  # the model is done — no more tool calls.

            tool_results: list[dict] = []
            for tool_use in turn.tool_uses:
                content = self._dispatch(tool_use)
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use.id,
                        "content": content,
                    }
                )
            messages.append({"role": "user", "content": tool_results})

        return self._transcript

    def explore(self, goal_text: str, *, legs: Optional[int] = None) -> list[TranscriptEntry]:
        """Deliberative-sugar 'go look around and report' (software-spec.md §2.1).

        Expands into an agent-level loop of describe_scene → drive → turn — built
        only from ordinary abilities. There is **no reactive EXPLORE mode**: every
        move is a normal ``DRIVE_GOAL`` intent and every report is a
        ``describe_scene`` call. Returns the transcript entries it produced (and
        also appends them to the current run transcript).
        """
        n = self.explore_legs if legs is None else legs
        produced: list[TranscriptEntry] = []
        self._note(f"explore: '{goal_text}' — deliberative loop over {n} leg(s) "
                   f"(no reactive EXPLORE mode).", goal_text=goal_text, legs=n)

        for leg in range(n):
            # 1. Look and report.
            model = model_router.pick_model(
                RoutingContext(intent_name="describe_scene", intent_detail="quick")
            )
            scene = self._safe_describe(
                detail="quick",
                question=f"While exploring for: {goal_text}. What do you see ahead?",
                model=model,
            )
            produced.append(self._record(
                "model", f"[explore leg {leg + 1}] {scene}", scene=scene, leg=leg))

            # 2. Edge forward (small, timed, open-loop hop).
            produced.append(self._dispatch_intent(
                "drive", {"distance_m": 0.3, "speed": 0.4}))

            # 3. Pan to a new heading for the next look.
            produced.append(self._dispatch_intent(
                "turn", {"angle_deg": 30.0, "speed": 0.4}))

        return produced

    def build_context(self, user_text: str, state: RobotState) -> str:
        """Stitch the user's words + a compact RobotState + the open-loop caveats.

        This is the whole text prompt for a step (the image, when present, is the
        still). It must tell the model the ground rules of this body (§3).
        """
        return build_context(user_text, state)

    # -- internals: model call (with graceful degradation) -------------------
    def _call_model(self, messages: list[dict], model: str) -> Optional[llm.ToolTurn]:
        """Call the model, backing off / retrying on transient failures (§5).

        Returns ``None`` if the call never succeeds within ``max_retries`` — the
        caller then issues no intent this step (the robot stays safe). Retries do
        not bypass the budget: a call only counts once it actually returns.
        """
        delay = self.retry_backoff
        for attempt in range(self.max_retries):
            try:
                return llm.call_with_tools(
                    messages,
                    tools=self.tools,
                    model=model,
                    client=self.client,
                    max_tokens=self.max_tokens,
                    system=self.system,
                )
            except Exception as exc:  # noqa: BLE001 — degrade on any cloud error
                last = exc
                if attempt + 1 < self.max_retries:
                    time.sleep(delay)
                    delay *= 2
        self._note(f"model call failed ({type(last).__name__}: {last}); no intent "
                   f"this step — reactive layer stays safe (§5).")
        return None

    # -- internals: dispatch one tool call -----------------------------------
    def _dispatch(self, tool_use: llm.ToolUse) -> str:
        """Map one tool_use to an Intent / answer, run it, return tool_result text."""
        name = tool_use.name
        params = tool_use.input or {}
        ability = ABILITY_BY_NAME.get(name)
        kind = ability.kind if ability is not None else KIND_INTENT

        self._record("tool", f"{name}({_fmt_params(params)})", name=name, params=params)

        if kind == KIND_INTENT:
            entry = self._dispatch_intent(name, params)
            return entry.text
        if kind == KIND_QUERY:
            return self._dispatch_query(name, params)
        if kind == KIND_DELIBERATIVE:
            return self._dispatch_deliberative(name, params)
        if kind == KIND_CONTROL:
            return self._dispatch_control(name, params)
        return f"unknown ability '{name}' — ignored."

    def _dispatch_intent(self, name: str, params: dict) -> TranscriptEntry:
        """Send a motion/mode intent down to the reactive layer and settle it."""
        self._seq += 1
        intent = intent_for(name, params, self._seq)
        if intent is None:
            return self._record("note", f"{name}: produced no intent.")
        state = self._send_and_settle(intent)
        summary = _state_summary(state, intent=intent)
        return self._record("state", summary, mode=_mode_str(state),
                            goal_status=getattr(state, "goal_status", None))

    def _dispatch_query(self, name: str, params: dict) -> str:
        state = self._read_state()
        if name == "check_distance":
            if state is None or not state.distance_known:
                text = ("distance UNKNOWN — the ultrasonic echo timed out; treat the "
                        "path as possibly blocked, not clear.")
            else:
                text = f"nearest obstacle ~{state.distance_m:.2f} m ahead."
            self._record("state", text, distance_m=getattr(state, "distance_m", None),
                         distance_known=getattr(state, "distance_known", None))
            return text
        if name == "look":
            handle = getattr(state, "last_frame_id", None) if state else None
            text = (f"captured still (frame handle {handle})." if handle
                    else "captured still (no frame handle yet).")
            self._record("state", text, frame=handle)
            return text
        return f"query '{name}' not handled."

    def _dispatch_deliberative(self, name: str, params: dict) -> str:
        if name == "describe_scene":
            detail = str(params.get("detail", "quick"))
            model = model_router.pick_model(
                RoutingContext(user_text=self._user_text,
                               intent_name="describe_scene", intent_detail=detail)
            )
            scene = self._safe_describe(detail=detail, model=model)
            self._record("model", scene, scene=scene, detail=detail, model=model)
            return scene
        if name == "explore":
            goal_text = str(params.get("goal_text", "")).strip() or "the area"
            self.explore(goal_text)
            return (f"explored '{goal_text}' via a drive/turn/describe_scene loop "
                    "(deliberative sugar — no reactive EXPLORE mode).")
        if name == "speak":
            text = str(params.get("text", "")).strip()
            # ``_record`` vocalizes "model" lines itself (no-op unless --speak),
            # so the speak tool needs no separate _vocalize call — and the turn's
            # dedup means narrating the same text first won't double-speak it.
            self._record("model", text, spoken=True)
            return "spoke to the user."
        return f"deliberative ability '{name}' not handled."

    def _dispatch_control(self, name: str, params: dict) -> str:
        if name == "set_speed_limit":
            requested = float(params.get("max_speed", 1.0))
            applied = clamp_speed_limit(requested)
            # A CONTROL-ONLY intent (mode=None): the reactive core writes it to
            # RobotState.speed_limit on adoption and clamps every subsequent
            # throttle to it, WITHOUT preempting the in-progress goal. Send the raw
            # request; the core is the single clamp authority. We report the
            # clamped value we know it will apply (a fresh state read here can
            # still predate the adoption tick, so trust the clamp, not the echo).
            self._seq += 1
            try:
                self.reactive.send_intent(
                    Intent(mode=None, seq=self._seq, speed_limit=requested)
                )
            except Exception:  # noqa: BLE001 — a send failure must not wedge the loop
                pass
            self._record(
                "state",
                f"speed limit {applied:.2f} applied; motion clamped.",
                max_speed=applied,
                requested=requested,
            )
            return f"speed limit {applied:.2f} applied; motion clamped to {applied:.2f}."
        return f"control ability '{name}' not handled."

    # -- internals: reactive I/O ---------------------------------------------
    def _send_and_settle(self, intent: Intent) -> Optional[RobotState]:
        """Send one Intent, then poll RobotState until the step settles.

        Settled means: a ``DRIVE_GOAL`` reaches a terminal goal_status
        (COMPLETED / BLOCKED / PREEMPTED), an ``IDLE`` lands in IDLE mode, or
        ``FOLLOW`` is handed off (returns immediately). Bounded by
        ``settle_timeout`` so a stuck goal can never hang the loop.
        """
        self.reactive.send_intent(intent)

        if intent.mode == Mode.FOLLOW:
            return self._read_state()

        deadline = time.monotonic() + self.settle_timeout
        state = self._read_state()
        saw_running = _is_running(state)
        while time.monotonic() < deadline:
            state = self._read_state()
            if state is None:
                continue
            if state.blocked:  # collision-stop — surface immediately, never reverse
                break
            if intent.mode == Mode.DRIVE_GOAL:
                status = state.goal_status
                if status in (GoalStatus.COMPLETED, GoalStatus.PREEMPTED) and (
                    saw_running or state.mode == Mode.IDLE
                ):
                    break
                if status == GoalStatus.RUNNING:
                    saw_running = True
            elif intent.mode == Mode.IDLE:
                if state.mode == Mode.IDLE:
                    break
            else:
                break
            time.sleep(self.poll_interval)
        return state

    def _read_state(self) -> Optional[RobotState]:
        try:
            return self.reactive.request_state(timeout=self.poll_timeout)
        except Exception:  # noqa: BLE001 — a read failure must not wedge the loop
            return None

    def _fall_to_idle(self) -> None:
        """Issue a stop intent so a budget/outage stop leaves the robot in IDLE."""
        self._seq += 1
        try:
            self.reactive.send_intent(Intent(Mode.IDLE, None, self._seq))
        except Exception:  # noqa: BLE001
            pass

    def _build_user_turn(self, user_text: str) -> dict:
        state = self._read_state() or RobotState()
        content: list[dict] = [{"type": "text", "text": build_context(user_text, state)}]
        if self.capture_still is not None:
            try:
                frame = self.capture_still()
            except Exception:  # noqa: BLE001 — vision is optional; never wedge
                frame = None
            if frame:
                content.append(llm.image_block(frame))
        return {"role": "user", "content": content}

    def _safe_describe(self, *, detail: str, question: Optional[str] = None,
                       model: Optional[str] = None) -> str:
        try:
            return self.describe_scene(detail=detail, question=question, model=model)
        except Exception as exc:  # noqa: BLE001 — vision failure shouldn't wedge
            return f"(could not describe scene: {type(exc).__name__})"

    # -- transcript helpers --------------------------------------------------
    def _record(self, kind: str, text: str, **data: Any) -> TranscriptEntry:
        entry = TranscriptEntry(kind=kind, text=text, data=data)
        self._transcript.append(entry)
        # User-facing OUTPUT is exactly the model's OWN words/answers — the
        # "model" lines shown in the transcript (narration, scene descriptions,
        # the speak tool, the final report) — never routing "note" lines or raw
        # tool calls. Deliver those through the injected responder (always at
        # least the console) so a reply can never vanish, and also feed the legacy
        # speak callback for back-compat. Both dedup against repeats this turn.
        if kind == "model":
            self._deliver_answer(text)
            self._vocalize(text)
        return entry

    def _note(self, text: str, **data: Any) -> TranscriptEntry:
        return self._record("note", text, **data)

    def _deliver_answer(self, text: str) -> None:
        """Deliver one user-facing answer through the injected responder.

        Best-effort by design: an output channel must never wedge the loop, so
        any responder failure is swallowed here (the ConsoleResponder default and
        CompositeResponder already isolate their own failures — this is just a
        final belt-and-braces). Identical text is delivered at most once per turn.
        """
        body = (text or "").strip()
        if not body or body in self._answered:
            return  # already delivered this turn — don't repeat the same answer.
        self._answered.add(body)
        try:
            self.responder.respond(body, kind="answer")
        except Exception:  # noqa: BLE001 - answer delivery is best-effort, never fatal
            pass

    def _vocalize(self, text: str) -> None:
        """Speak ``text`` aloud via the injected callback (no-op when disabled).

        Best-effort: speech is additive output, so a voice failure must never
        wedge the loop — exactly like the rest of the deliberative path.
        """
        if self._speak is None:
            return
        body = (text or "").strip()
        if not body:
            return
        if body in self._spoken:
            return  # already said this turn — don't double-speak identical text.
        self._spoken.add(body)
        try:
            self._speak(body)
        except Exception:  # noqa: BLE001 - voice is best-effort, never fatal
            pass


# --- module-level helpers (also handy for the CLI / tests) ------------------
def build_context(user_text: str, state: RobotState) -> str:
    """Render the step's text prompt: the request + a compact state + caveats."""
    s = state if state is not None else RobotState()
    lines = [
        f"USER: {user_text.strip() or '(no words — act on what you see)'}",
        "",
        "ROBOT STATE:",
        f"  mode: {_mode_str(s)}",
        f"  last goal: {s.goal_status}"
        + (f" ({_fmt_params(s.goal)})" if s.goal else ""),
        f"  nearest obstacle: "
        + (f"~{s.distance_m:.2f} m" if s.distance_known else "UNKNOWN (echo timeout)"),
        f"  person visible: {s.target_visible}"
        + (f" (tracker_score={s.tracker_score:.2f}, "
           f"ticks_since_confirm={s.ticks_since_last_detector_confirmation})"
           if s.target_visible else ""),
        f"  speed limit: {s.speed_limit:.2f}",
        "",
        "GROUND RULES (this body, plan around them):",
        "  - No encoders/IMU: drive/turn are TIMED, approximate, uncorrected "
        "outside FOLLOW. 'completed (timed, unverified)' = ran ~that long.",
        "  - On collision the robot HALTS and reports BLOCKED — it will NOT "
        "reverse on its own. If BLOCKED, turn to a clear heading or stop; don't "
        "blindly drive backward.",
        "  - distance UNKNOWN means 'might be blocked', never 'clear'.",
        "  - Pick one ability at a time, then re-check state.",
    ]
    return "\n".join(lines)


def format_transcript(entries: list[TranscriptEntry]) -> str:
    """Pretty-print a run transcript for the terminal."""
    glyphs = {"model": "🗣  model", "tool": "🔧 tool ", "state": "🤖 state",
              "note": "·  note "}
    out: list[str] = []
    for e in entries:
        label = glyphs.get(e.kind, f"   {e.kind}")
        body = e.text.replace("\n", "\n          ")
        out.append(f"{label} | {body}")
    return "\n".join(out)


def _state_summary(state: Optional[RobotState], intent: Optional[Intent] = None) -> str:
    if state is None:
        return "no state read back (reactive layer unreachable this step)."
    if state.blocked:
        reason = (state.goal or {}).get("reason", "obstacle")
        dist = ("unknown distance" if not state.distance_known
                else f"~{state.distance_m:.2f} m ahead")
        return (f"BLOCKED ({reason}, {dist}) — the robot HALTED and did not "
                "reverse. Turn to a clear heading or stop; do not drive backward.")
    if intent is not None and intent.mode == Mode.IDLE:
        return f"stopped — wheels halted (mode {_mode_str(state)})."
    if intent is not None and intent.mode == Mode.FOLLOW:
        return (f"following (mode {_mode_str(state)}); the on-robot tracker now "
                "drives without further model calls until told to stop.")
    status = state.goal_status
    if status == GoalStatus.COMPLETED:
        return (f"{status} — open-loop timed move, position not verified "
                f"(mode now {_mode_str(state)}).")
    if status == GoalStatus.RUNNING:
        return f"still running (mode {_mode_str(state)})."
    return f"{status} (mode {_mode_str(state)})."


def _is_running(state: Optional[RobotState]) -> bool:
    return state is not None and state.goal_status == GoalStatus.RUNNING


def _mode_str(state: Optional[RobotState]) -> str:
    mode = getattr(state, "mode", None)
    return mode.value if isinstance(mode, Mode) else str(mode)


def _fmt_params(params: Optional[dict]) -> str:
    if not params:
        return ""
    parts = []
    for k, v in params.items():
        if isinstance(v, float):
            parts.append(f"{k}={v:g}")
        else:
            parts.append(f"{k}={v}")
    return ", ".join(parts)


__all__ = [
    "Agent",
    "TranscriptEntry",
    "AGENT_SYSTEM",
    "build_context",
    "format_transcript",
]
