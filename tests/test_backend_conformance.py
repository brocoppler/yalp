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

import numpy as np
import pytest

from yalp.camera import Camera
from yalp.contract.messages import GoalStatus, Intent, Mode
from yalp.reactive.fake_backend import FakeReactiveBackend
from yalp.reactive.hardware import FakeMotorDriver, FakeRangeSensor
from yalp.reactive.person_tracker import TrackResult
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


# --------------------------------------------------------------------------- #
# (6) FOLLOW parity — identical frame/tracker input must yield identical mode /
#     goal_status / honesty-signal transitions in BOTH backends.
# --------------------------------------------------------------------------- #
# FOLLOW is the second place the fake and real tick logic must not diverge: both
# read the SAME shared camera, run the SAME tracker/controller, and publish the
# SAME honesty signals every tick (software-spec.md §2.2). The safety tests above
# prove the collision seam; these prove the FOLLOW seam. An injected scripted
# tracker + a fixed bright frame make the input byte-for-byte identical, so any
# divergence in the published state across the two backends fails the build.
class _ScriptedTracker:
    """Replays a fixed sequence of ``TrackResult``s, ignoring the frame.

    A FRESH instance is built per backend (the sequence is consumed) so both
    backends see byte-for-byte identical tracker output on matching ticks. Once
    exhausted it repeats the last result (steady state).
    """

    def __init__(self, results) -> None:
        self._results = list(results)
        self._last = results[-1] if results else None

    def update(self, _frame) -> TrackResult:
        if self._results:
            self._last = self._results.pop(0)
        return self._last


class _BrightCamera:
    """A synthetic camera handing back ONE fixed bright frame once started.

    Bright enough (mean 180) to clear the FOLLOW dark-floor, so a visible box is
    steered rather than degraded to "lost". Fully deterministic — no capture
    thread, identical frame for both backends. Dimensions match so the
    controller computes identical steering geometry on each side.
    """

    width, height = 320, 240

    def __init__(self) -> None:
        self._frame = np.full((self.height, self.width, 3), 180, dtype=np.uint8)
        self._started = False

    def start(self) -> "_BrightCamera":
        self._started = True
        return self

    def stop(self) -> None:
        self._started = False

    def latest(self):
        return self._frame if self._started else None


def _follow_script():
    """A FOLLOW tracker script exercising acquire → coast → lost → stale → reacquire.

    A fresh copy is returned each call (the tracker consumes it). The ``ticks=30``
    entry is deliberately > the 20 Hz lost-grace window (18) so the controller
    reads it as STALE (target_visible False, bbox None) on BOTH backends — a
    genuine honesty transition, not just a moving box.
    """
    return [
        TrackResult(True, (140, 80, 40, 90), 0.90, 0, True),    # acquired
        TrackResult(True, (40, 80, 40, 90), 0.85, 1, False),    # coasting, moved left
        TrackResult(True, (240, 80, 40, 90), 0.80, 2, False),   # coasting, moved right
        TrackResult(False, None, 0.0, 3, False),                # lost (no box)
        TrackResult(True, (150, 80, 40, 90), 0.90, 30, False),  # STALE (>grace) -> stop
        TrackResult(True, (150, 40, 60, 180), 0.95, 0, True),   # reacquired, close
    ]


def _follow_signal_trace(backend, n_ticks):
    """Drive ``backend`` through FOLLOW and record the per-tick published signals.

    Returns a list of ``(mode, goal_status, target_visible, target_bbox,
    tracker_score, ticks_since_last_detector_confirmation)`` — the mode, goal
    status, and the four §2.2 honesty signals — one tuple per tick.
    """
    backend.start()
    backend.apply_intent(Intent(Mode.FOLLOW, {"target": "nearest_person"}, seq=1))
    trace = []
    for _ in range(n_ticks):
        st = backend.tick()
        trace.append(
            (
                st.mode,
                st.goal_status,
                st.target_visible,
                st.target_bbox,
                round(float(st.tracker_score), 6),
                st.ticks_since_last_detector_confirmation,
            )
        )
    return trace


def _fake_follow(script):
    return FakeReactiveBackend(
        camera=_BrightCamera(), tracker=_ScriptedTracker(script)
    )


def _real_follow(script):
    motor = FakeMotorDriver()
    sensor = FakeRangeSensor()  # default: 4.0 m clear, known
    backend = RealReactiveBackend(
        motor_driver=motor,
        range_sensor=sensor,
        camera=_BrightCamera(),
        tracker=_ScriptedTracker(script),
    )
    return backend, motor, sensor


def test_follow_honesty_signals_are_identical_across_backends():
    n = len(_follow_script())
    fake = _fake_follow(_follow_script())
    real, _motor, _sensor = _real_follow(_follow_script())
    try:
        fake_trace = _follow_signal_trace(fake, n)
        real_trace = _follow_signal_trace(real, n)
    finally:
        fake.stop()
        real.stop()

    # Byte-for-byte identical mode / goal_status / honesty-signal transitions.
    assert fake_trace == real_trace

    # And the trace is actually meaningful (it exercises real transitions, not a
    # single frozen state): a visible acquire, a lost/stale drop to not-visible
    # with bbox None, and a re-acquire.
    visibles = [row[2] for row in fake_trace]
    assert visibles[0] is True                 # acquired on the first FOLLOW tick
    assert False in visibles                   # dropped to not-visible (lost/stale)
    assert visibles[-1] is True                # re-acquired at the end
    # Every FOLLOW tick stays in FOLLOW/RUNNING (no obstacle here).
    assert all(row[0] == Mode.FOLLOW for row in fake_trace)
    assert all(row[1] == GoalStatus.RUNNING for row in fake_trace)
    # The stale entry (ticks=30) reads not-visible with a cleared bbox on BOTH.
    stale_rows = [row for row in fake_trace if row[5] == 30]
    assert stale_rows and all(
        row[2] is False and row[3] is None for row in stale_rows
    )


def test_follow_collision_override_parity():
    """A collision DURING FOLLOW overrides identically in both backends (§2.3)."""
    visible = TrackResult(True, (140, 80, 40, 90), 0.90, 0, True)
    fake = _fake_follow([visible] * 8)
    real, motor, sensor = _real_follow([visible] * 8)
    try:
        fake.start()
        real.start()
        for backend in (fake, real):
            backend.apply_intent(Intent(Mode.FOLLOW, {"target": "nearest_person"}, seq=1))

        # A couple of clear FOLLOW ticks: both acquire and steer the visible box.
        for _ in range(2):
            fs, rs = fake.tick(), real.tick()
        assert fs.mode == rs.mode == Mode.FOLLOW
        assert fs.target_visible is rs.target_visible is True

        # Obstacle appears INSIDE the threshold -> collision-stop overrides FOLLOW.
        fake.trigger_collision(0.10)
        sensor.set_distance(0.10, known=True)
        fs, rs = fake.tick(), real.tick()

        # Identical safety verdict on both backends.
        assert fs.mode == rs.mode == Mode.SAFE_STOP
        assert fs.goal_status == rs.goal_status == GoalStatus.BLOCKED
        assert fs.blocked is rs.blocked is True
        assert fs.goal["reason"] == rs.goal["reason"] == "obstacle"

        # Real-backend-only: the motors were halted this tick.
        assert motor.stop_count >= 1
        assert motor.last == (0.0, 0.0)
    finally:
        fake.stop()
        real.stop()
