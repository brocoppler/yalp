"""Real (on-Pi) reactive backend — documented STUB for Step A.

This is intentionally **not implemented** in Step A. It exists so the
hardware-abstraction seam (``ReactiveBackend``) has its production counterpart
declared, and so the module imports cleanly on a laptop with no GPIO/camera
hardware present. It must never make a hardware call at import time.

Implementation TODO (see ``docs/technical/hardware.md``)
--------------------------------------------------------
* **Motors — differential drive, phase/enable** (hardware.md §5): drive each
  DRV8833/TB6612FNG channel with PWM on a *hardware-PWM* pin (GPIO12/GPIO13) for
  speed/enable and a plain GPIO for direction/phase (GPIO17/GPIO22). Use
  ``gpiozero`` on the **lgpio / native pin factory** — ``RPi.GPIO`` does **not**
  work on the Pi 5 (the GPIO is behind the RP1 southbridge) and will silently
  fail. Clamp commanded speed to ``RobotState.speed_limit`` before writing PWM.
* **Ultrasonic — HC-SR04** (hardware.md §4): TRIG on GPIO5, ECHO on GPIO6 **via a
  resistor divider** (5V → ~3.3V). The echo is software-timed and jittery: a
  missed/timed-out echo is **"unknown" → bias to STOP**, never decayed to
  "clear". Cap the poll rate at ~15 Hz (≥60 ms between pings).
* **Camera** (software-spec.md §2.5): own the C270 via the threaded
  ``yalp.camera.Camera`` ("webcam" source); never call a blocking ``read()`` on
  the tick path.
* **Tick** (software-spec.md §2.3): same priority order as
  ``FakeReactiveBackend.tick`` — safety override first, then newest-intent
  preemption, then advance the current mode — but driving real motors/sensors.
  A separate watchdog process (hardware.md / software-spec.md §2.6) zeroes the
  motor GPIO on a stale heartbeat.
"""

from __future__ import annotations

from typing import Any

from ..contract.messages import Intent, RobotState
from .backend import ReactiveBackend

_NOT_IMPLEMENTED_MSG = (
    "RealReactiveBackend is a Step-A stub and is not implemented yet. "
    "TODO: wire gpiozero+lgpio phase/enable motors (PWM GPIO12/13, dir "
    "GPIO17/22) and the HC-SR04 (TRIG GPIO5, ECHO GPIO6 via divider; "
    "echo-timeout='unknown' -> STOP). See docs/technical/hardware.md."
)


class RealReactiveBackend(ReactiveBackend):
    """On-Pi reactive backend. **Stub** — every method raises until implemented.

    The class imports fine on any machine (no hardware libraries are imported at
    module load); instantiating it raises ``NotImplementedError`` so a laptop
    run fails loudly with a clear pointer rather than half-initializing hardware.
    """

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        raise NotImplementedError(_NOT_IMPLEMENTED_MSG)

    def apply_intent(self, intent: Intent) -> None:  # pragma: no cover - stub
        raise NotImplementedError(_NOT_IMPLEMENTED_MSG)

    def tick(self) -> RobotState:  # pragma: no cover - stub
        raise NotImplementedError(_NOT_IMPLEMENTED_MSG)

    def get_state(self) -> RobotState:  # pragma: no cover - stub
        raise NotImplementedError(_NOT_IMPLEMENTED_MSG)


__all__ = ["RealReactiveBackend"]
