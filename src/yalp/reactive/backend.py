"""The reactive backend interface — the hardware-abstraction seam.

The reactive layer is built against this abstract interface so the same agent /
contract code runs on the laptop (``FakeReactiveBackend``) and on the Pi
(``RealReactiveBackend``); only the thin motor/sensor/camera adapter crosses over
to hardware (architecture.md §7).

The tick contract (software-spec.md §2.3)
-----------------------------------------
``tick()`` runs exactly one reactive step and **must** honor:

  1. **Stay under the budget.** A tick must complete well under
     ``config.TICK_BUDGET_MS`` (the p99 deadline that keeps the safety loop at
     ≥30 Hz). Nothing on the tick path may block — no synchronous network read,
     no blocking camera ``read()``, no heavy CV op.
  2. **Safety override first.** The collision-stop reflex is evaluated before
     anything else: if an obstacle is detected (or the ultrasonic echo is
     unknown), the wheels stop this tick and the backend enters ``SAFE_STOP``
     with ``goal_status = "blocked"`` — regardless of mode or any in-flight
     intent. It does **not** open-loop reverse (no rear sensor).
  3. **Newest intent preempts.** Otherwise, the single-slot, last-write-wins
     mailbox is drained and any newer intent replaces the current mode/goal.
  4. **Then the current mode advances** its goal by elapsed time (open-loop).

``SAFE_STOP`` is sticky: leaving it requires *both* the obstacle clearing **and**
a fresh intent arriving.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional

from ..contract.messages import Intent, RobotState


class ReactiveBackend(ABC):
    """Abstract reactive layer: apply intents, tick, report state, own a camera."""

    @abstractmethod
    def apply_intent(self, intent: Intent) -> None:
        """Hand an ``Intent`` to the single-slot, last-write-wins mailbox.

        This only *stores* the intent; it is adopted (or rejected, if a safety
        stop is active) on the next ``tick()`` per the preemption rules.
        """

    @abstractmethod
    def tick(self) -> RobotState:
        """Run one reactive step and return the resulting ``RobotState`` snapshot.

        Must obey the tick contract documented at module level: under budget,
        safety first, newest-intent preempts, then advance the current mode.
        """

    @abstractmethod
    def get_state(self) -> RobotState:
        """Return the latest ``RobotState`` snapshot without advancing a tick."""

    # -- camera access (default no-op; backends with a camera override) -------
    def camera(self) -> Optional[Any]:
        """Return the backend's camera, or ``None`` if it has none."""
        return None

    def capture_still(self) -> Optional[Any]:
        """Grab the newest decoded frame (a snapshot), or ``None``."""
        cam = self.camera()
        return None if cam is None else cam.latest()


__all__ = ["ReactiveBackend"]
