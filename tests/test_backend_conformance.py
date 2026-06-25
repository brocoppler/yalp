"""Shared loop-to-loop SAFETY CONFORMANCE suite — the fake and the real backend
must honor the collision-stop / SAFE_STOP contract *identically*.

The loop-to-loop safety seam (software-spec.md §2.3) is the hardest, least
specified boundary in the system: the deliberative loop proposes Intents, the
reactive loop owns safety and may veto them, and a SAFE_STOP latches stickily.
``FakeReactiveBackend`` is the laptop-proven reference; ``RealReactiveBackend``
is the on-robot implementation. If their safety semantics ever diverge, the
robot behaves differently from everything we validated on a laptop.

This module is an **independent, adversarial check**: it parametrizes ONE set of
assertions over BOTH backends so any divergence fails the build. It does not (and
must not) modify either implementation — it only observes them through the public
tick contract.

Each backend is wrapped in a tiny adapter exposing a uniform
``set_obstacle(distance, known)`` so the obstacle simulation flows through the
backend's native hook:

  * fake  -> ``set_sensor(distance_m, known)`` (the simulated ultrasonic);
  * real  -> the injected ``FakeRangeSensor.set_distance(distance, known)``,
             i.e. the *same* code path real hardware would feed.

The assertions are ported verbatim from ``tests/test_contract.py`` (the fake's
reference behavior) so passing here proves byte-for-byte parity.
"""

from __future__ import annotations

import pytest

from yalp.camera import Camera
from yalp.contract.messages import GoalStatus, Intent, Mode
from yalp.reactive.fake_backend import FakeReactiveBackend
from yalp.reactive.hardware import FakeMotorDriver, FakeRangeSensor
from yalp.reactive.real_backend import RealReactiveBackend


# --------------------------------------------------------------------------- #
# Uniform adapters — one ``set_obstacle`` API over two different obstacle hooks
# --------------------------------------------------------------------------- #
class _Harness:
    """A backend + a uniform obstacle hook (+ optional injected motor driver).

    ``motor`` is the injected :class:`FakeMotorDriver` for the real backend, or
    ``None`` for the fake (which has no motor driver). Tests that assert on motor
    commands skip when ``motor is None``.
    """

    def __init__(self, backend, set_obstacle, motor=None) -> None:
        self.backend = backend
        self._set_obstacle = set_obstacle
        self.motor = motor

    def set_obstacle(self, distance_m: float, known: bool = True) -> None:
        self._set_obstacle(distance_m, known)


def _make_fake() -> _Harness:
    """Fake backend: obstacle simulation goes through ``set_sensor``."""
    backend = FakeReactiveBackend(camera=Camera(source="synthetic"))

    def set_obstacle(distance_m: float, known: bool) -> None:
        backend.set_sensor(distance_m=distance_m, known=known)

    return _Harness(backend, set_obstacle, motor=None)


def _make_real() -> _Harness:
    """Real backend with injected fakes: obstacle goes through FakeRangeSensor."""
    motor = FakeMotorDriver()
    sensor = FakeRangeSensor()  # default: 4.0 m clear, known
    backend = RealReactiveBackend(
        motor_driver=motor,
        range_sensor=sensor,
        camera=Camera(source="synthetic"),
    )

    def set_obstacle(distance_m: float, known: bool) -> None:
        sensor.set_distance(distance_m, known=known)

    return _Harness(backend, set_obstacle, motor=motor)


# Each factory builds a fresh harness so parametrized tests stay isolated.
_FACTORIES = [
    pytest.param(_make_fake, id="fake"),
    pytest.param(_make_real, id="real"),
]


# --------------------------------------------------------------------------- #
# (1) DRIVE_GOAL straight -> RUNNING -> COMPLETED -> mode IDLE
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("factory", _FACTORIES)
def test_drive_goal_straight_runs_then_completes_idle(factory):
    h = factory()
    backend = h.backend
    backend.apply_intent(
        Intent(Mode.DRIVE_GOAL, {"kind": "straight", "target": 0.5, "speed": 1.0}, seq=1)
    )
    state = None
    saw_running = False
    for _ in range(5000):
        state = backend.tick()
        if state.mode == Mode.DRIVE_GOAL and state.goal_status == GoalStatus.RUNNING:
            saw_running = True
        if GoalStatus.COMPLETED in state.goal_status:
            break
    assert state is not None
    assert saw_running
    # Open-loop, timed, unverified — never a bare "completed".
    assert state.goal_status == "completed (timed, unverified)"
    assert backend.get_state().mode == Mode.IDLE


# --------------------------------------------------------------------------- #
# (2) Collision -> SAFE_STOP / BLOCKED, blocked True, reason == obstacle
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("factory", _FACTORIES)
def test_collision_latches_safe_stop_blocked(factory):
    h = factory()
    backend = h.backend
    backend.apply_intent(
        Intent(Mode.DRIVE_GOAL, {"kind": "straight", "target": 1.0, "speed": 1.0}, seq=1)
    )
    st = backend.tick()
    assert st.mode == Mode.DRIVE_GOAL

    # Obstacle appears inside the threshold -> collision-stop overrides everything.
    h.set_obstacle(0.10, known=True)
    st = backend.tick()
    assert st.mode == Mode.SAFE_STOP
    assert st.goal_status == GoalStatus.BLOCKED
    assert st.blocked is True
    assert st.goal["reason"] == "obstacle"

    # Real-backend-only: the injected motor driver was stopped this tick.
    if h.motor is not None:
        assert h.motor.stop_count >= 1
        assert h.motor.last == (0.0, 0.0)


# --------------------------------------------------------------------------- #
# (3) A fresh Intent while blocked does NOT override the safety stop
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("factory", _FACTORIES)
def test_fresh_intent_does_not_override_while_blocked(factory):
    h = factory()
    backend = h.backend
    h.set_obstacle(0.10, known=True)
    assert backend.tick().mode == Mode.SAFE_STOP

    # A new intent must NOT override the active safety stop while still blocked.
    backend.apply_intent(Intent(Mode.FOLLOW, {"target": "nearest_person"}, seq=2))
    st = backend.tick()
    assert st.mode == Mode.SAFE_STOP


# --------------------------------------------------------------------------- #
# (4) Sticky release: clear obstacle AND a pending fresh intent -> adopted
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("factory", _FACTORIES)
def test_sticky_release_adopts_pending_intent_once_cleared(factory):
    h = factory()
    backend = h.backend
    h.set_obstacle(0.10, known=True)
    assert backend.tick().mode == Mode.SAFE_STOP

    # A fresh intent arrives while blocked (queued, not adopted).
    backend.apply_intent(Intent(Mode.FOLLOW, {"target": "nearest_person"}, seq=2))
    assert backend.tick().mode == Mode.SAFE_STOP

    # SAFE_STOP is sticky: clearing the obstacle AND a fresh intent are both
    # required to resume. The pending FOLLOW (seq=2) is adopted once clear.
    h.set_obstacle(4.0, known=True)
    st = backend.tick()
    assert st.mode == Mode.FOLLOW


# --------------------------------------------------------------------------- #
# (5) Unknown echo (known=False) -> SAFE_STOP with reason echo_timeout
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("factory", _FACTORIES)
def test_unknown_echo_biases_to_safe_stop(factory):
    h = factory()
    backend = h.backend
    h.set_obstacle(10.0, known=False)  # echo timeout -> unknown
    st = backend.tick()
    assert st.mode == Mode.SAFE_STOP
    assert st.goal_status == GoalStatus.BLOCKED
    assert st.distance_known is False
    assert st.goal["reason"] == "echo_timeout"

    # Real-backend-only: an unknown reading also stops the motors.
    if h.motor is not None:
        assert h.motor.stop_count >= 1
        assert h.motor.last == (0.0, 0.0)
