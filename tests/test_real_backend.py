"""Unit tests for :class:`yalp.reactive.real_backend.RealReactiveBackend`.

These run entirely on a laptop / in CI with **no GPIO hardware**: the backend is
constructed with injected :class:`~yalp.reactive.hardware.FakeMotorDriver` and
:class:`~yalp.reactive.hardware.FakeRangeSensor` plus a synthetic camera, so we
can assert that the real backend honors the **same** preemption/safety tick
contract as :class:`~yalp.reactive.fake_backend.FakeReactiveBackend` (the
contract tests assert this ordering is faithful) AND that drive/follow steering
is translated into signed motor throttles clamped to ``speed_limit``.

Covered (mirrors the contract the fake is held to, software-spec.md §2.3):

  (i)   a DRIVE_GOAL straight intent commands a FORWARD throttle, then reports a
        "timed, unverified" completion, drops to IDLE, and zeroes the motors;
  (ii)  an obstacle -> the motors stop FIRST, SAFE_STOP/BLOCKED is latched, and
        the pending mailbox intent is NOT drained while blocked;
  (iii) SAFE_STOP is sticky: a fresh intent while blocked does not resume; once
        the obstacle clears the pending intent is adopted and it resumes;
  (iv)  an UNKNOWN (echo-timeout) reading biases to SAFE_STOP with reason
        ``echo_timeout`` (never decayed to "clear");
  (v)   a commanded speed above ``speed_limit`` is clamped before it reaches PWM.

Plus: import/construction stay hardware-free, rotate spins the wheels in place,
and FOLLOW steering maps to motor throttles (clean stop when lost).
"""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest

from tests._import_isolation import (
    assert_import_leaves_module_unloaded,
    assert_isolated_program_succeeds,
)
from yalp.camera import Camera
from yalp.contract.messages import GoalStatus, Intent, Mode
from yalp.reactive.hardware import FakeMotorDriver, FakeRangeSensor
from yalp.reactive.person_tracker import TrackResult
from yalp.reactive.real_backend import RealReactiveBackend


# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #
class _FakeTracker:
    """Minimal track-by-detection stub: returns a canned :class:`TrackResult`.

    Defaults to "target lost" (a clean stop). Pass a ``result`` to simulate a
    confident, visible box. Ignores the frame entirely so FOLLOW tests are
    deterministic and need no OpenCV / real camera.
    """

    def __init__(self, result: TrackResult | None = None) -> None:
        self._result = result

    def update(self, _frame) -> TrackResult:
        if self._result is not None:
            return self._result
        return TrackResult(False, None, 0.0, 0, False)


class _BrightCamera:
    """A synthetic camera that hands back one bright frame once started.

    Bright enough to clear the FOLLOW dark-floor so a visible target is steered
    (a black/None frame degrades to "lost"). No capture thread, fully
    deterministic.
    """

    width, height = 640, 480

    def __init__(self) -> None:
        self._frame = np.full((self.height, self.width, 3), 200, dtype=np.uint8)
        self._started = False
        self.stopped = False

    def start(self) -> "_BrightCamera":
        self._started = True
        return self

    def stop(self) -> None:
        self._started = False
        self.stopped = True

    def latest(self):
        return self._frame if self._started else None


def _make_backend(*, camera=None, tracker=None, **kwargs):
    """Build a RealReactiveBackend with injected fakes (no hardware touched)."""
    motor = FakeMotorDriver()
    sensor = FakeRangeSensor()  # default: 4.0 m clear, known
    cam = camera if camera is not None else Camera(source="synthetic")
    backend = RealReactiveBackend(
        motor_driver=motor,
        range_sensor=sensor,
        camera=cam,
        max_speed_mps=1.0,
        tick_hz=50.0,
        tracker=tracker,
        **kwargs,
    )
    return backend, motor, sensor


# --------------------------------------------------------------------------- #
# Import / construction hygiene
# --------------------------------------------------------------------------- #
def test_module_imports_without_gpio_libraries():
    # Checked in a fresh subprocess so this is order-independent and holds on a
    # Pi (where these libs ARE installed) as well as on a laptop — see
    # tests/_import_isolation.py.
    assert_import_leaves_module_unloaded(
        "yalp.reactive.real_backend",
        ("gpiozero", "lgpio", "RPi", "RPi.GPIO"),
    )


def test_injected_fakes_skip_the_hardware_imports():
    # Constructing the real backend with BOTH fakes injected must not import
    # gpiozero/lgpio. We assert this in a *fresh subprocess* (mirroring
    # `_make_backend()`'s construction) so it is order-independent and holds on a
    # Pi where gpiozero IS installed and a prior test may have imported it — an
    # in-process `sys.modules` check would false-fail there. See
    # tests/_import_isolation.py.
    program = (
        "import sys\n"
        "from yalp.camera import Camera\n"
        "from yalp.reactive.hardware import FakeMotorDriver, FakeRangeSensor\n"
        "from yalp.reactive.real_backend import RealReactiveBackend\n"
        "motor = FakeMotorDriver()\n"
        "sensor = FakeRangeSensor()\n"
        "backend = RealReactiveBackend(\n"
        "    motor_driver=motor,\n"
        "    range_sensor=sensor,\n"
        "    camera=Camera(source='synthetic'),\n"
        "    max_speed_mps=1.0,\n"
        "    tick_hz=50.0,\n"
        "    tracker=None,\n"
        ")\n"
        "assert backend._motor_driver is motor, 'motor_driver not wired through'\n"
        "assert backend._range_sensor is sensor, 'range_sensor not wired through'\n"
        "_leaked = [m for m in ('gpiozero', 'lgpio') if m in sys.modules]\n"
        "if _leaked:\n"
        "    sys.stderr.write('constructing injected-fake backend imported: '"
        " + ', '.join(_leaked) + '\\n')\n"
        "raise SystemExit(1 if _leaked else 0)\n"
    )
    assert_isolated_program_succeeds(
        program,
        what=(
            "constructing RealReactiveBackend with injected fakes pulled in a "
            "hardware library (gpiozero/lgpio) or failed to wire the fakes through."
        ),
    )


# --------------------------------------------------------------------------- #
# (i) DRIVE_GOAL straight: forward throttle -> timed completion -> IDLE + zeroed
# --------------------------------------------------------------------------- #
def test_drive_goal_straight_drives_forward_then_completes_idle():
    backend, motor, sensor = _make_backend()
    backend.apply_intent(
        Intent(Mode.DRIVE_GOAL, {"kind": "straight", "target": 0.5, "speed": 0.8}, seq=1)
    )

    # First tick adopts the intent and commands a FORWARD throttle on both wheels.
    st = backend.tick()
    assert st.mode == Mode.DRIVE_GOAL
    assert st.goal_status == GoalStatus.RUNNING
    assert motor.last == (0.8, 0.8)  # forward (positive), magnitude == speed

    # Advance to completion.
    state = st
    for _ in range(5000):
        state = backend.tick()
        if GoalStatus.COMPLETED in state.goal_status:
            break

    # Open-loop, timed, unverified completion — never a bare "completed".
    assert state.goal_status == "completed (timed, unverified)"
    assert backend.get_state().mode == Mode.IDLE
    # On completion the wheels are explicitly zeroed.
    assert motor.last == (0.0, 0.0)
    assert motor.stop_count >= 1


def test_drive_goal_rotate_spins_wheels_in_place():
    backend, motor, sensor = _make_backend()
    backend.apply_intent(
        Intent(Mode.DRIVE_GOAL, {"kind": "rotate", "target": 90.0, "speed": 0.5}, seq=1)
    )
    st = backend.tick()
    assert st.mode == Mode.DRIVE_GOAL
    left, right = motor.last
    # Rotate = spin in place: the wheels oppose each other.
    assert left == -right
    assert left != 0.0
    assert abs(left) == pytest.approx(0.5)


# --------------------------------------------------------------------------- #
# (ii) Obstacle -> motors stop FIRST, SAFE_STOP/BLOCKED, mailbox NOT drained
# --------------------------------------------------------------------------- #
def test_obstacle_stops_motors_and_does_not_drain_mailbox():
    backend, motor, sensor = _make_backend()
    # A pending intent that must NOT be adopted while blocked.
    backend.apply_intent(
        Intent(Mode.DRIVE_GOAL, {"kind": "straight", "target": 1.0, "speed": 1.0}, seq=7)
    )
    sensor.trigger_obstacle()  # known, close obstacle inside the threshold

    st = backend.tick()
    assert st.mode == Mode.SAFE_STOP
    assert st.goal_status == GoalStatus.BLOCKED
    assert st.blocked is True
    assert st.goal["reason"] == "obstacle"
    # Motors were stopped this tick (and never commanded forward).
    assert motor.stop_count >= 1
    assert motor.last == (0.0, 0.0)
    assert (1.0, 1.0) not in motor.calls  # the drive intent never reached the wheels

    # The pending intent is still queued — it was deliberately NOT drained.
    pending = backend.mailbox.peek()
    assert pending is not None
    assert pending.seq == 7


# --------------------------------------------------------------------------- #
# (iii) SAFE_STOP is sticky: fresh intent while blocked doesn't resume; clearing
#       the obstacle (with the pending intent) does.
# --------------------------------------------------------------------------- #
def test_safe_stop_is_sticky_until_cleared_then_resumes():
    backend, motor, sensor = _make_backend(tracker=_FakeTracker())
    sensor.trigger_obstacle()
    assert backend.tick().mode == Mode.SAFE_STOP

    # A fresh intent arriving WHILE blocked does not resume (stays latched).
    backend.apply_intent(Intent(Mode.FOLLOW, {"target": "nearest_person"}, seq=2))
    assert backend.tick().mode == Mode.SAFE_STOP

    # Clearing the obstacle lets the (still-pending) fresh intent be adopted.
    sensor.set_distance(4.0, known=True)
    st = backend.tick()
    assert st.mode == Mode.FOLLOW


def test_safe_stop_stays_latched_without_a_fresh_intent():
    backend, motor, sensor = _make_backend()
    backend.apply_intent(
        Intent(Mode.DRIVE_GOAL, {"kind": "straight", "target": 1.0, "speed": 1.0}, seq=1)
    )
    backend.tick()  # adopt + drive
    sensor.trigger_obstacle()
    assert backend.tick().mode == Mode.SAFE_STOP

    # Obstacle clears but NO fresh intent has arrived (the seq=1 drive was already
    # adopted/consumed): SAFE_STOP stays latched (sticky), motors stay stopped.
    sensor.set_distance(4.0, known=True)
    st = backend.tick()
    assert st.mode == Mode.SAFE_STOP
    assert motor.last == (0.0, 0.0)


# --------------------------------------------------------------------------- #
# (iv) Unknown (echo-timeout) reading -> SAFE_STOP with reason echo_timeout
# --------------------------------------------------------------------------- #
def test_echo_timeout_biases_to_safe_stop():
    backend, motor, sensor = _make_backend()
    # distance is nominally "clear" (4.0 m) but the echo is UNKNOWN -> STOP.
    sensor.timeout()
    st = backend.tick()
    assert st.mode == Mode.SAFE_STOP
    assert st.goal_status == GoalStatus.BLOCKED
    assert st.distance_known is False
    assert st.goal["reason"] == "echo_timeout"
    assert motor.stop_count >= 1
    assert motor.last == (0.0, 0.0)


# --------------------------------------------------------------------------- #
# (v) Commanded speed clamps to speed_limit before reaching PWM
# --------------------------------------------------------------------------- #
def test_commanded_speed_clamps_to_speed_limit():
    backend, motor, sensor = _make_backend()
    backend._state.speed_limit = 0.3  # tighten the safety speed cap
    backend.apply_intent(
        Intent(Mode.DRIVE_GOAL, {"kind": "straight", "target": 2.0, "speed": 1.0}, seq=1)
    )
    st = backend.tick()
    assert st.mode == Mode.DRIVE_GOAL
    left, right = motor.last
    assert left == pytest.approx(0.3)
    assert right == pytest.approx(0.3)
    assert abs(left) <= backend._state.speed_limit + 1e-9
    assert abs(right) <= backend._state.speed_limit + 1e-9


def test_speed_limit_intent_writes_state_then_clamps_motor_commands():
    """END-TO-END: a control-only speed-limit intent -> RobotState.speed_limit ->
    clamped wheel PWM. This is the honesty fix: 'go slow' actually slows the wheels.

    A control-only Intent (mode=None, speed_limit=0.4) is adopted WITHOUT changing
    the mode, writing RobotState.speed_limit. A SUBSEQUENT drive at speed=1.0 is
    then clamped to 0.4 at the pins (not driven full speed).
    """
    backend, motor, sensor = _make_backend()
    assert backend.get_state().speed_limit == 1.0  # wire default before any limit

    # 1. Control-only intent sets the limit (no motion, mode unchanged from IDLE).
    backend.apply_intent(Intent(mode=None, seq=1, speed_limit=0.4))
    st = backend.tick()
    assert st.mode == Mode.IDLE  # a control-only intent does NOT change the mode
    assert st.speed_limit == 0.4  # ...but the reactive layer recorded the clamp

    # 2. A subsequent full-speed drive is clamped to the recorded limit at the pins.
    backend.apply_intent(
        Intent(Mode.DRIVE_GOAL, {"kind": "straight", "target": 2.0, "speed": 1.0}, seq=2)
    )
    st = backend.tick()
    assert st.mode == Mode.DRIVE_GOAL
    left, right = motor.last
    assert left == pytest.approx(0.4)
    assert right == pytest.approx(0.4)


def test_speed_limit_intent_clamps_into_sane_band():
    """A requested limit outside [0.1, 1.0] is clamped by the reactive core."""
    backend, motor, sensor = _make_backend()
    backend.apply_intent(Intent(mode=None, seq=1, speed_limit=5.0))  # absurd
    assert backend.tick().speed_limit == 1.0  # clamped to the max
    backend.apply_intent(Intent(mode=None, seq=2, speed_limit=0.0))  # too slow
    assert backend.tick().speed_limit == 0.1  # clamped to the min


def test_follow_throttles_also_clamp_to_speed_limit():
    # A confident, off-center, very-far box would otherwise command > the cap.
    result = TrackResult(True, (600, 50, 200, 400), 0.95, 0, True)
    cam = _BrightCamera()
    backend, motor, sensor = _make_backend(camera=cam, tracker=_FakeTracker(result))
    backend._state.speed_limit = 0.4
    backend.start()
    backend.apply_intent(Intent(Mode.FOLLOW, {"target": "nearest_person"}, seq=1))
    st = backend.tick()
    assert st.mode == Mode.FOLLOW
    left, right = motor.last
    assert abs(left) <= 0.4 + 1e-9
    assert abs(right) <= 0.4 + 1e-9


# --------------------------------------------------------------------------- #
# FOLLOW steering -> motor throttles
# --------------------------------------------------------------------------- #
def test_follow_lost_target_issues_clean_stop():
    backend, motor, sensor = _make_backend(tracker=_FakeTracker())  # always lost
    backend.apply_intent(Intent(Mode.FOLLOW, {"target": "nearest_person"}, seq=1))
    st = backend.tick()
    assert st.mode == Mode.FOLLOW
    assert st.target_visible is False
    # Lost -> turn=forward=0 -> a clean stop commanded via set_motors.
    assert motor.last == (0.0, 0.0)


def test_follow_visible_target_drives_forward():
    # A confident, centered, far box -> forward, (roughly) no turn.
    result = TrackResult(True, (270, 100, 100, 100), 0.95, 0, True)
    cam = _BrightCamera()
    backend, motor, sensor = _make_backend(camera=cam, tracker=_FakeTracker(result))
    backend.start()
    backend.apply_intent(Intent(Mode.FOLLOW, {"target": "nearest_person"}, seq=1))
    st = backend.tick()
    assert st.mode == Mode.FOLLOW
    assert st.target_visible is True
    left, right = motor.last
    assert left > 0 and right > 0  # driving forward toward the person
    backend.close()


# --------------------------------------------------------------------------- #
# Teardown
# --------------------------------------------------------------------------- #
def test_close_zeroes_motors_and_releases_hardware():
    cam = _BrightCamera()
    backend, motor, sensor = _make_backend(camera=cam)
    backend.start()
    backend.close()
    assert motor.stop_count >= 1
    assert motor.closed is True
    assert sensor.closed is True
    assert cam.stopped is True
    # Idempotent.
    backend.close()


def test_close_order_motors_first_then_watchdog_then_sensor_before_motor():
    """Teardown order (instrumented fakes): motors zeroed FIRST, then the watchdog
    retired, then the range sensor closed BEFORE the motor driver.

    Motors-first is the 2026-07-16 regression fix: the wheels are commanded to zero
    before ANY potentially-slow watchdog/perception/camera/sensor cleanup, so they
    are guaranteed idle the instant shutdown begins no matter what blocks next.
    The watchdog is still retired before the (potentially blocking) perception join
    so teardown never looks like a wedged tick, and the range sensor is still closed
    before the motor driver — whose close() tears down the process-global gpiozero
    pin factory — so the factory is never yanked out from under an open sensor.
    """
    log: list[str] = []

    class _LogMotor(FakeMotorDriver):
        def stop(self) -> None:
            log.append("motor.stop")
            super().stop()

        def close(self) -> None:
            log.append("motor.close")
            super().close()

    class _LogSensor(FakeRangeSensor):
        def close(self) -> None:
            log.append("sensor.close")
            super().close()

    class _LogWatchdog:
        """A watchdog stand-in that only records its lifecycle calls."""

        def __init__(self) -> None:
            self.started = False
            self.stopped = False

        def start(self):
            self.started = True
            return self

        def stop(self, timeout: float = 1.0) -> None:
            log.append("watchdog.stop")
            self.stopped = True

        def heartbeat(self) -> None:
            pass

    motor = _LogMotor()
    sensor = _LogSensor()
    wd = _LogWatchdog()
    backend = RealReactiveBackend(
        motor_driver=motor,
        range_sensor=sensor,
        camera=Camera(source="synthetic"),
        tick_hz=50.0,
        watchdog=wd,
    )
    backend.close()

    assert wd.stopped is True
    # 1) Motors are zeroed FIRST, before anything else in teardown.
    assert log[0] == "motor.stop"
    # 2) The watchdog is retired before the range sensor / motor driver are closed.
    assert log.index("watchdog.stop") < log.index("sensor.close")
    # 3) Motors are stopped before any pins are released.
    assert log.index("motor.stop") < log.index("sensor.close")
    # 4) The range sensor is closed BEFORE the motor driver (pin-factory teardown).
    assert log.index("sensor.close") < log.index("motor.close")


def test_watchdog_armed_by_start_and_retired_by_close():
    """start() arms the backend-owned watchdog; close() joins/retires it."""
    backend, motor, sensor = _make_backend(camera=_BrightCamera())
    assert backend._watchdog._thread is None  # not armed until start()
    backend.start()
    thread = backend._watchdog._thread
    assert thread is not None and thread.is_alive()
    backend.close()
    assert backend._watchdog._thread is None
    assert not thread.is_alive()


def test_direct_tick_caller_is_covered_by_the_watchdog():
    """A direct start()+tick() loop heartbeats the watchdog; stalling trips it.

    Proves item (b): a caller driving tick() directly (not run()) still gets the
    dead-man's switch, because the heartbeat lives inside tick().
    """
    from yalp.reactive.watchdog import MotorWatchdog

    motor = FakeMotorDriver()
    sensor = FakeRangeSensor()
    wd = MotorWatchdog(motor, timeout_ms=30)
    backend = RealReactiveBackend(
        motor_driver=motor,
        range_sensor=sensor,
        camera=Camera(source="synthetic"),
        tick_hz=200.0,
        watchdog=wd,
    )
    backend.start()
    try:
        # Ticking keeps the heartbeat fresh -> never trips.
        deadline = time.monotonic() + 0.2
        while time.monotonic() < deadline:
            backend.tick()
            time.sleep(0.002)
        assert wd.tripped is False
        assert wd.trip_count == 0

        # Stop ticking -> the heartbeat goes stale -> the watchdog trips.
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not wd.tripped:
            time.sleep(0.005)
        assert wd.tripped is True
        assert wd.trip_count >= 1
        assert motor.last == (0.0, 0.0)
    finally:
        backend.close()


# --------------------------------------------------------------------------- #
# (vi) END-TO-END grace: a SINGLE spurious echo-timeout during a DRIVE_GOAL no
#      longer forces SAFE_STOP (the real GpiozeroUltrasonicSensor coasts on its
#      last VALID reading), while SUSTAINED timeouts still latch SAFE_STOP/BLOCKED.
#
# Unlike the FakeRangeSensor tests above, this wires the REAL grace-aware sensor
# (against a fake gpiozero + injected clock) into the backend, proving the
# emergent behaviour: the reactive override still simply trusts (distance, known),
# and the bounded grace lives entirely in the sensor wrapper.
# --------------------------------------------------------------------------- #
def _install_fake_gpiozero(monkeypatch):
    """Inject a minimal fake ``gpiozero`` package with a controllable sensor."""
    import sys
    import types

    class _FakeDistanceSensor:
        def __init__(self, echo=None, trigger=None, max_distance=1.0, queue_len=1):
            self.max_distance = max_distance
            self.fraction = 1.0  # default full-scale == no echo == timeout
            self.closed = False

        @property
        def distance(self):
            return self.fraction

        def close(self):
            self.closed = True

    class _FakeLGPIOFactory:
        def close(self):
            pass

    gpiozero = types.ModuleType("gpiozero")

    class _Device:
        pin_factory = None

    gpiozero.Device = _Device
    gpiozero.DistanceSensor = _FakeDistanceSensor
    pins = types.ModuleType("gpiozero.pins")
    lgpio_mod = types.ModuleType("gpiozero.pins.lgpio")
    lgpio_mod.LGPIOFactory = _FakeLGPIOFactory
    pins.lgpio = lgpio_mod
    gpiozero.pins = pins

    monkeypatch.setitem(sys.modules, "gpiozero", gpiozero)
    monkeypatch.setitem(sys.modules, "gpiozero.pins", pins)
    monkeypatch.setitem(sys.modules, "gpiozero.pins.lgpio", lgpio_mod)
    return gpiozero


def _make_backend_with_real_sensor(monkeypatch, clock):
    """RealReactiveBackend wired to the REAL GpiozeroUltrasonicSensor (grace-aware).

    The sensor uses ``max_poll_hz=0`` (no rate cap: every tick re-pulses) and an
    injected clock so the grace window/miss budget are driven deterministically.
    """
    _install_fake_gpiozero(monkeypatch)
    from yalp.reactive.hardware import GpiozeroUltrasonicSensor

    sensor = GpiozeroUltrasonicSensor(
        max_distance_m=4.0,
        max_poll_hz=0.0,
        grace_ms=150,
        grace_max_misses=2,
        monotonic=lambda: clock["now"],
    )
    motor = FakeMotorDriver()
    backend = RealReactiveBackend(
        motor_driver=motor,
        range_sensor=sensor,
        camera=Camera(source="synthetic"),
        max_speed_mps=1.0,
        tick_hz=50.0,
        tracker=None,
    )
    return backend, motor, sensor


def test_single_spurious_timeout_during_drive_does_not_safe_stop(monkeypatch):
    clock = {"now": 1000.0}
    backend, motor, sensor = _make_backend_with_real_sensor(monkeypatch, clock)

    # Path is CLEAR (1.0 m). Start driving forward.
    sensor._sensor.fraction = 0.25  # 25% of 4.0 m -> 1.0 m clear
    backend.apply_intent(
        Intent(Mode.DRIVE_GOAL, {"kind": "straight", "target": 100.0, "speed": 0.5}, seq=1)
    )
    st = backend.tick()
    assert st.mode == Mode.DRIVE_GOAL
    assert st.goal_status == GoalStatus.RUNNING
    assert st.distance_known is True
    assert motor.last == (0.5, 0.5)  # actually driving forward

    # A SINGLE spurious echo-timeout, a hair later (inside the grace window and
    # miss budget). WITHOUT the grace this would instantly SAFE_STOP; with it the
    # sensor coasts the last VALID 1.0 m clear reading, so the drive CONTINUES.
    sensor._sensor.fraction = 1.0  # echo timeout
    clock["now"] += 0.020
    st = backend.tick()
    assert st.mode == Mode.DRIVE_GOAL  # NOT phantom-stopped
    assert st.goal_status == GoalStatus.RUNNING
    assert st.distance_known is True  # coasted -> looks like a valid clear read
    assert motor.last == (0.5, 0.5)  # still driving forward

    # A single VALID read again -> business as usual.
    sensor._sensor.fraction = 0.25
    clock["now"] += 0.020
    st = backend.tick()
    assert st.mode == Mode.DRIVE_GOAL
    assert st.distance_known is True


def test_sustained_timeout_during_drive_still_latches_safe_stop(monkeypatch):
    clock = {"now": 2000.0}
    backend, motor, sensor = _make_backend_with_real_sensor(monkeypatch, clock)

    sensor._sensor.fraction = 0.25  # 1.0 m clear
    backend.apply_intent(
        Intent(Mode.DRIVE_GOAL, {"kind": "straight", "target": 100.0, "speed": 0.5}, seq=1)
    )
    assert backend.tick().mode == Mode.DRIVE_GOAL

    # SUSTAINED loss of sensing: hammer timeouts past BOTH grace bounds. The grace
    # coasts a couple, then gives up and the invariant re-asserts -> SAFE_STOP.
    sensor._sensor.fraction = 1.0
    saw_safe_stop = False
    final = None
    for _ in range(10):
        clock["now"] += 0.050  # 50 ms/read -> quickly exceeds the 150 ms window
        final = backend.tick()
        if final.mode == Mode.SAFE_STOP:
            saw_safe_stop = True
            break
    assert saw_safe_stop is True
    assert final.mode == Mode.SAFE_STOP
    assert final.goal_status == GoalStatus.BLOCKED
    assert final.distance_known is False
    assert final.goal["reason"] == "echo_timeout"
    assert motor.last == (0.0, 0.0)  # motors stopped on sustained blindness


# --------------------------------------------------------------------------- #
# (vii) The env-configured poll cap (YALP_ULTRASONIC_MAX_POLL_HZ) actually reaches
#       the sensor the real backend BUILDS ITSELF. RealReactiveBackend constructs
#       GpiozeroUltrasonicSensor() with NO args, so the cap can only arrive via the
#       config default — this proves that whole chain end-to-end in a *fresh
#       interpreter* (env set at startup, exactly like the field), against a fake
#       gpiozero so no hardware is needed. The parent's env is propagated to the
#       child by the isolation helper, so monkeypatch.setenv here is inherited.
# --------------------------------------------------------------------------- #
def test_env_poll_cap_reaches_backend_built_sensor(monkeypatch):
    monkeypatch.setenv("YALP_ULTRASONIC_MAX_POLL_HZ", "6")
    program = (
        "import sys, types\n"
        # --- minimal fake gpiozero (only what the ultrasonic sensor touches) ---
        "class _FakeDistanceSensor:\n"
        "    def __init__(self, echo=None, trigger=None, max_distance=1.0, queue_len=1):\n"
        "        self.max_distance = max_distance\n"
        "        self.fraction = 1.0\n"
        "        self.closed = False\n"
        "    @property\n"
        "    def distance(self):\n"
        "        return self.fraction\n"
        "    def close(self):\n"
        "        self.closed = True\n"
        "class _FakeLGPIOFactory:\n"
        "    def close(self):\n"
        "        pass\n"
        "gpiozero = types.ModuleType('gpiozero')\n"
        "class _Device:\n"
        "    pin_factory = None\n"
        "gpiozero.Device = _Device\n"
        "gpiozero.DistanceSensor = _FakeDistanceSensor\n"
        "pins = types.ModuleType('gpiozero.pins')\n"
        "lgpio_mod = types.ModuleType('gpiozero.pins.lgpio')\n"
        "lgpio_mod.LGPIOFactory = _FakeLGPIOFactory\n"
        "pins.lgpio = lgpio_mod\n"
        "gpiozero.pins = pins\n"
        "sys.modules['gpiozero'] = gpiozero\n"
        "sys.modules['gpiozero.pins'] = pins\n"
        "sys.modules['gpiozero.pins.lgpio'] = lgpio_mod\n"
        # --- build the backend via its DEFAULT (range_sensor=None) sensor path --
        "from yalp.camera import Camera\n"
        "from yalp.reactive.hardware import FakeMotorDriver\n"
        "from yalp.reactive.real_backend import RealReactiveBackend\n"
        "backend = RealReactiveBackend(\n"
        "    motor_driver=FakeMotorDriver(),\n"
        "    range_sensor=None,   # default path -> backend builds the REAL sensor\n"
        "    camera=Camera(source='synthetic'),\n"
        "    tick_hz=50.0,\n"
        "    tracker=None,\n"
        ")\n"
        "got = backend._range_sensor._limiter.min_interval_s\n"
        "expected = 1.0 / 6.0\n"
        "sys.stderr.write('min_interval_s=%r expected=%r\\n' % (got, expected))\n"
        "raise SystemExit(0 if abs(got - expected) < 1e-9 else 1)\n"
    )
    assert_isolated_program_succeeds(
        program,
        what=(
            "YALP_ULTRASONIC_MAX_POLL_HZ=6 did not reach the sensor the real "
            "backend builds itself (expected a 1/6 s rate-cap interval), so the "
            "env override never reaches the field's GpiozeroUltrasonicSensor()."
        ),
    )


# --------------------------------------------------------------------------- #
# (viii) END-TO-END observability: the range sensor's cumulative miss/coast
#        counters are threaded into the RobotState snapshot the IPC server serves
#        (under an additive 'ultrasonic' sub-map) AND survive the JSON round-trip.
#        This is what lets a field session read the TRUE miss rate over IPC/state
#        instead of only the grace-absorbed remainder (2026-07-16 field finding).
# --------------------------------------------------------------------------- #
def test_state_snapshot_carries_ultrasonic_counters():
    from yalp.contract.messages import RobotState

    backend, motor, sensor = _make_backend()  # injected FakeRangeSensor (clear)
    st = backend.tick()

    assert st.ultrasonic is not None
    assert set(st.ultrasonic) == {
        "total_reads",
        "valid_reads",
        "raw_misses",
        "coasted_reads",
        "unknown_served",
    }
    # One tick -> one clear (valid) read.
    assert st.ultrasonic["total_reads"] == 1
    assert st.ultrasonic["valid_reads"] == 1
    assert st.ultrasonic["raw_misses"] == 0

    # get_state() (an on-demand poll, no fresh tick) surfaces the same sub-map.
    assert backend.get_state().ultrasonic == st.ultrasonic

    # ADDITIVE + backward-compatible: the counters ride the existing 'state' wire
    # message and round-trip through JSON unchanged.
    d = st.to_dict()
    assert d["ultrasonic"] == st.ultrasonic
    assert RobotState.from_json(st.to_json()).ultrasonic == st.ultrasonic


def test_state_snapshot_reveals_coasted_misses_hidden_by_distance_known(monkeypatch):
    """The coast-absorbed miss count is invisible to ``distance_known`` but VISIBLE
    in the state's ultrasonic sub-map — the whole point of the counters."""
    clock = {"now": 3000.0}
    backend, motor, sensor = _make_backend_with_real_sensor(monkeypatch, clock)

    # A clear valid read, then a single spurious timeout the grace COASTS.
    sensor._sensor.fraction = 0.25  # 1.0 m clear
    backend.apply_intent(
        Intent(Mode.DRIVE_GOAL, {"kind": "straight", "target": 100.0, "speed": 0.5}, seq=1)
    )
    st = backend.tick()
    assert st.distance_known is True
    assert st.ultrasonic["valid_reads"] == 1
    assert st.ultrasonic["raw_misses"] == 0
    assert st.ultrasonic["coasted_reads"] == 0

    sensor._sensor.fraction = 1.0  # echo timeout
    clock["now"] += 0.020
    st = backend.tick()

    # distance_known STILL True (the grace coasted the miss) — indistinguishable
    # from a valid echo to any plain observer...
    assert st.distance_known is True
    # ...but the state snapshot now records the raw miss AND that it was coasted,
    # so the true miss rate is recoverable over IPC/state.
    assert st.ultrasonic["raw_misses"] == 1
    assert st.ultrasonic["coasted_reads"] == 1
    assert st.ultrasonic["unknown_served"] == 0  # nothing surfaced as known=False


# --------------------------------------------------------------------------- #
# (ix) SIGINT-shutdown regression (2026-07-16): teardown MUST stay bounded and
#      zero the motors first even while the ultrasonic sensor is storming echo
#      timeouts — the field failure where the stack survived two SIGINTs and
#      only died to SIGTERM because a gpiozero close() wedged mid software-timed
#      echo wait. These reproduce the storm with fake/mocked hardware (no
#      gpiozero) and assert the backend/loop shuts down within a bounded time.
# --------------------------------------------------------------------------- #
class _BlockingCloseSensor(FakeRangeSensor):
    """A range sensor whose ``close()`` BLOCKS until explicitly released.

    Models a gpiozero ``DistanceSensor.close()`` wedged joining its internal
    sampling thread while that thread is stuck in a software-timed echo wait during
    a timeout storm — the unbounded blocking call that made shutdown hang.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.close_entered = threading.Event()
        self.close_returned = threading.Event()
        self.release = threading.Event()

    def close(self) -> None:  # type: ignore[override]
        self.close_entered.set()
        # Block like a wedged gpiozero close() until the test releases us (bounded
        # by a generous ceiling so a forgotten release can never hang the suite).
        self.release.wait(30.0)
        super().close()
        self.close_returned.set()


class _StormingSensor(FakeRangeSensor):
    """A sensor that STORMS echo timeouts: every read blocks briefly (a Pi-5
    software-timed echo wait) then reports UNKNOWN (bias to STOP), and whose
    ``close()`` blocks like a gpiozero close() wedged mid-storm.
    """

    def __init__(self, read_block_s: float = 0.02) -> None:
        super().__init__(distance_m=4.0, known=False)
        self._read_block_s = float(read_block_s)
        self.close_entered = threading.Event()
        self.release = threading.Event()

    def read_distance(self):  # type: ignore[override]
        # Simulate the software-timed echo wait that dominates the read path during
        # a storm; bounded (like the real echo_timeout) so the loop still turns over.
        time.sleep(self._read_block_s)
        self.read_count += 1
        return (self.distance_m, False)  # UNKNOWN -> STOP every tick

    def close(self) -> None:  # type: ignore[override]
        self.close_entered.set()
        self.release.wait(30.0)
        super().close()


def test_close_is_bounded_and_zeroes_motors_when_sensor_close_blocks(monkeypatch):
    """close() must return within a bounded time — and have zeroed the motors —
    even though the range sensor's close() is wedged (the storm failure mode).
    """
    from yalp.reactive import real_backend

    # Shrink the per-step teardown ceiling so the bounded guard trips fast in-test.
    monkeypatch.setattr(real_backend, "TEARDOWN_STEP_TIMEOUT_S", 0.2)

    motor = FakeMotorDriver()
    sensor = _BlockingCloseSensor()
    backend = RealReactiveBackend(
        motor_driver=motor,
        range_sensor=sensor,
        camera=Camera(source="synthetic"),
        tick_hz=50.0,
        tracker=None,
    )
    try:
        t0 = time.monotonic()
        backend.close()
        elapsed = time.monotonic() - t0

        # Teardown stayed BOUNDED despite the wedged sensor close.
        assert elapsed < 5.0, f"close() hung for {elapsed:.2f}s during a sensor storm"
        # The blocking close WAS attempted (teardown reached step 4)...
        assert sensor.close_entered.is_set()
        # ...but has NOT returned (still blocked) — proving close() did not wait it out.
        assert not sensor.close_returned.is_set()
        # Motors were zeroed FIRST regardless of the wedged sensor cleanup.
        assert motor.stop_count >= 1
        assert motor.last == (0.0, 0.0)
        # And teardown PROCEEDED past the wedged sensor close to release the driver.
        assert motor.closed is True
    finally:
        sensor.release.set()  # let the daemon close thread finish (no leak)


def test_run_loop_shuts_down_promptly_during_timeout_storm(monkeypatch):
    """run() must exit and fully tear down within a bounded window when the sensor
    is storming timeouts (slow, UNKNOWN reads) AND its close() is wedged — zeroing
    the motors — reproducing the 2026-07-16 unkillable-by-SIGINT field failure.
    """
    from yalp.reactive import real_backend

    monkeypatch.setattr(real_backend, "TEARDOWN_STEP_TIMEOUT_S", 0.2)

    motor = FakeMotorDriver()
    sensor = _StormingSensor(read_block_s=0.02)
    backend = RealReactiveBackend(
        motor_driver=motor,
        range_sensor=sensor,
        camera=Camera(source="synthetic"),
        tick_hz=50.0,
        tracker=None,
    )
    stop_event = threading.Event()

    runner = threading.Thread(
        target=lambda: backend.run(stop_event=stop_event),
        name="storm-run",
        daemon=True,
    )
    try:
        runner.start()
        # Let the storm run for a bit: several blind reads -> SAFE_STOP each tick.
        time.sleep(0.2)
        assert sensor.read_count >= 1, "run() loop never ticked/read the sensor"

        # Request shutdown. The loop must exit and the (bounded) teardown must
        # complete within a bounded window despite storming reads + a wedged close.
        t0 = time.monotonic()
        stop_event.set()
        runner.join(timeout=5.0)
        elapsed = time.monotonic() - t0

        assert not runner.is_alive(), "run() did not return during the sensor storm"
        assert elapsed < 5.0, f"shutdown took {elapsed:.2f}s during a sensor storm"
        # Motors were zeroed (blind reads latch SAFE_STOP; teardown zeroes first).
        assert motor.stop_count >= 1
        assert motor.last == (0.0, 0.0)
        # Teardown reached the (blocking) sensor close but was not hung by it.
        assert sensor.close_entered.is_set()
    finally:
        sensor.release.set()
        stop_event.set()
        runner.join(timeout=5.0)
