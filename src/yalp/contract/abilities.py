"""The ability menu — the fixed set of tools the LLM can call (software-spec.md §2.1).

This module is the **single source of truth** for the ability menu. The same
``Ability`` definitions produce both:

  * a human-readable menu (name, params, one-line effect, how it maps to an
    Intent/mode), and
  * the Anthropic tool-use schema list (``ANTHROPIC_TOOLS`` / ``ABILITY_MENU``)
    handed to the model — derived from the definitions so the two can never
    drift apart.

Abilities are *mostly modes for the reactive layer*, not direct actuation: the
model conducts, the fast loop plays the instrument (architecture.md §2). Each
ability declares its ``kind``:

  * ``intent``       — maps to an ``Intent`` sent down to the reactive layer
                       (``drive`` / ``turn`` / ``stop`` / ``enter_follow_mode``).
  * ``query``        — answered from the latest ``RobotState`` / a frame grab,
                       no Intent (``check_distance`` / ``look``).
  * ``deliberative`` — handled entirely on the cloud side, no reactive Intent
                       (``describe_scene`` / ``speak`` / ``explore``).
  * ``control``      — adjusts a reactive control value rather than a mode
                       (``set_speed_limit`` → ``RobotState.speed_limit``).

``explore`` is deliberately deliberative sugar — there is **no ``EXPLORE``
reactive mode** (§2.1). Pure standard library so it imports without third-party
deps.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Tuple

from .messages import Intent, Mode

# Sentinel meaning "this parameter has no default" (distinct from a default of
# ``None``), so the generated JSON schema omits the ``default`` key entirely.
_UNSET = object()

# Ability kinds (see module docstring).
KIND_INTENT = "intent"
KIND_QUERY = "query"
KIND_DELIBERATIVE = "deliberative"
KIND_CONTROL = "control"


@dataclass(frozen=True)
class Param:
    """One tool parameter, rich enough to render a JSON-schema property."""

    name: str
    type: str  # JSON-schema type: "number" | "string" | "boolean" | ...
    description: str
    required: bool = False
    default: Any = _UNSET
    enum: Optional[Tuple[Any, ...]] = None
    minimum: Optional[float] = None
    maximum: Optional[float] = None

    def to_schema(self) -> dict:
        schema: dict[str, Any] = {"type": self.type, "description": self.description}
        if self.enum is not None:
            schema["enum"] = list(self.enum)
        if self.minimum is not None:
            schema["minimum"] = self.minimum
        if self.maximum is not None:
            schema["maximum"] = self.maximum
        if self.default is not _UNSET:
            schema["default"] = self.default
        return schema


@dataclass(frozen=True)
class Ability:
    """One row of the stable ability menu (software-spec.md §2.1)."""

    name: str
    description: str  # the one-line effect, also used as the tool description
    kind: str
    params: Tuple[Param, ...] = field(default_factory=tuple)
    maps_to: str = ""  # how it maps to an Intent / mode / answer

    def to_anthropic_tool(self) -> dict:
        """Render this ability as an Anthropic tool-use schema entry."""
        props = {p.name: p.to_schema() for p in self.params}
        required = [p.name for p in self.params if p.required]
        input_schema: dict[str, Any] = {"type": "object", "properties": props}
        if required:
            input_schema["required"] = required
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": input_schema,
        }


# --- The menu (the stable contract; add an ability = add a row here) ---------
ABILITIES: Tuple[Ability, ...] = (
    Ability(
        name="drive",
        description=(
            "Drive straight a signed distance in meters (negative = backward). "
            "Collision-stop overrides this at all times. Completion is timed and "
            "unverified — there are no encoders."
        ),
        kind=KIND_INTENT,
        params=(
            Param("distance_m", "number", "Signed meters; negative drives backward.", required=True),
            Param("speed", "number", "Drive speed 0..1.", default=0.5, minimum=0, maximum=1),
        ),
        maps_to="Intent(mode=DRIVE_GOAL, goal={kind:'straight', target:distance_m, speed})",
    ),
    Ability(
        name="turn",
        description=(
            "Turn in place a signed angle in degrees (positive = left/CCW). "
            "Timed estimate only — no encoders, no IMU; reported as "
            "'completed (timed, unverified)'."
        ),
        kind=KIND_INTENT,
        params=(
            Param("angle_deg", "number", "Signed degrees; positive = left/CCW.", required=True),
            Param("speed", "number", "Turn speed 0..1.", default=0.5, minimum=0, maximum=1),
        ),
        maps_to="Intent(mode=DRIVE_GOAL, goal={kind:'rotate', target:angle_deg, speed})",
    ),
    Ability(
        name="stop",
        description="Cancel the current goal and halt the wheels (enter IDLE).",
        kind=KIND_INTENT,
        params=(),
        maps_to="Intent(mode=IDLE, goal=None)",
    ),
    Ability(
        name="look",
        description=(
            "Grab one still from the camera and return a frame handle for the "
            "next model turn."
        ),
        kind=KIND_QUERY,
        params=(
            Param("save", "boolean", "Persist the still to disk.", default=False),
        ),
        maps_to="No Intent: capture a still from the reactive camera buffer; return frame handle.",
    ),
    Ability(
        name="check_distance",
        description="Return the latest ultrasonic reading (meters) from shared state.",
        kind=KIND_QUERY,
        params=(),
        maps_to="No Intent: read RobotState.distance_m / distance_known.",
    ),
    Ability(
        name="describe_scene",
        description=(
            "Capture a still and describe what is visible. Use detail='full' only "
            "when the user explicitly wants a rich description or to read text."
        ),
        kind=KIND_DELIBERATIVE,
        params=(
            Param(
                "detail",
                "string",
                "Level of detail.",
                default="quick",
                enum=("quick", "full"),
            ),
        ),
        maps_to="No Intent: deliberative-side escalation to a model tier over a captured still.",
    ),
    Ability(
        name="enter_follow_mode",
        description=(
            "Hand control to the on-Pi tracker to center on and approach a target. "
            "Returns immediately; the robot follows without further model calls "
            "until told to stop."
        ),
        kind=KIND_INTENT,
        params=(
            Param(
                "target",
                "string",
                "What to follow.",
                default="nearest_person",
                enum=("nearest_person",),
            ),
        ),
        maps_to="Intent(mode=FOLLOW, goal={target})",
    ),
    Ability(
        name="explore",
        description=(
            "Explore the area toward a goal described in words. Deliberative sugar: "
            "the cloud side runs its own loop of drive/turn/describe_scene — there "
            "is no EXPLORE reactive mode."
        ),
        kind=KIND_DELIBERATIVE,
        params=(
            Param("goal_text", "string", "What to look for / where to go.", required=True),
        ),
        maps_to="No Intent: deliberative loop of drive/turn/describe_scene (§2.1).",
    ),
    Ability(
        name="speak",
        description="Say (TTS later; print for v1) the given text to the user.",
        kind=KIND_DELIBERATIVE,
        params=(
            Param("text", "string", "What to say.", required=True),
        ),
        maps_to="No Intent: deliberative-side output (print/TTS).",
    ),
    Ability(
        name="set_speed_limit",
        description="Clamp all subsequent motion speed (safety / 'go slow').",
        kind=KIND_CONTROL,
        params=(
            Param("max_speed", "number", "Speed clamp 0..1.", required=True, minimum=0, maximum=1),
        ),
        maps_to="Control: set RobotState.speed_limit; reactive clamps subsequent motion.",
    ),
)

ABILITY_BY_NAME = {a.name: a for a in ABILITIES}

# The Anthropic tool-use schema list, derived from the one source of truth above.
ANTHROPIC_TOOLS = [a.to_anthropic_tool() for a in ABILITIES]
# Spec alias: the deliberative step loop refers to this as ABILITY_MENU (§3).
ABILITY_MENU = ANTHROPIC_TOOLS


def intent_for(name: str, params: Optional[dict], seq: int) -> Optional[Intent]:
    """Map a tool call to an ``Intent``, or ``None`` for non-motion abilities.

    Only ``kind == "intent"`` abilities (``drive`` / ``turn`` / ``stop`` /
    ``enter_follow_mode``) produce an ``Intent`` sent down to the reactive layer.
    Query / deliberative / control abilities return ``None`` — they are answered
    from state, handled on the cloud side, or applied as a control value.
    """
    p = params or {}
    if name == "drive":
        return Intent(
            Mode.DRIVE_GOAL,
            {"kind": "straight", "target": float(p.get("distance_m", 0.0)), "speed": float(p.get("speed", 0.5))},
            seq,
        )
    if name == "turn":
        return Intent(
            Mode.DRIVE_GOAL,
            {"kind": "rotate", "target": float(p.get("angle_deg", 0.0)), "speed": float(p.get("speed", 0.5))},
            seq,
        )
    if name == "stop":
        return Intent(Mode.IDLE, None, seq)
    if name == "enter_follow_mode":
        return Intent(Mode.FOLLOW, {"target": p.get("target", "nearest_person")}, seq)
    return None


__all__ = [
    "Param",
    "Ability",
    "ABILITIES",
    "ABILITY_BY_NAME",
    "ANTHROPIC_TOOLS",
    "ABILITY_MENU",
    "intent_for",
    "KIND_INTENT",
    "KIND_QUERY",
    "KIND_DELIBERATIVE",
    "KIND_CONTROL",
]
