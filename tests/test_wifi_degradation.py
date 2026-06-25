"""Milestone N gate: the robot must not run away when the WiFi link drops.

This is the explicit, gated deliverable — *local graceful degradation when the
deliberative client disappears mid-command*. The reactive layer is the server and
keeps ticking whether or not a client is connected: with no client, ``publish``
is a best-effort no-op push (software-spec.md §2.2 / §5). So we can verify the
guarantee on a laptop, with no hardware and no real WiFi, by:

  * standing up a real :class:`RealReactiveBackend` driven by injected
    :class:`FakeMotorDriver` / :class:`FakeRangeSensor` + a synthetic camera, and
    wiring it to a :class:`ReactiveServer` whose mailbox it shares, then
  * connecting a :class:`DeliberativeClient` over loopback (mirroring
    ``tests/test_contract.py``), issuing a command, **closing the client to
    simulate the WiFi dropping**, and continuing to tick the backend locally.

The assertions are the safety contract: a DRIVE_GOAL halts within its bounded
open-loop duration (it does NOT drive forever), FOLLOW degrades to a clean stop
on target loss rather than driving blind, an obstacle still forces SAFE_STOP with
the client absent, and *no exception* escapes the tick/publish path once the
client is gone.
"""

from __future__ import annotations

import time

import pytest

from yalp.camera import Camera
from yalp.contract.ipc import DeliberativeClient, IntentMailbox, ReactiveServer
from yalp.contract.messages import GoalStatus, Intent, Mode
from yalp.reactive.hardware import FakeMotorDriver, FakeRangeSensor
from yalp.reactive.person_tracker import TrackResult
from yalp.reactive.real_backend import RealReactiveBackend


# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #
class _LosingTracker:
    """A tracker that reports a confident, visible target for ``visible_ticks``
    FOLLOW ticks and then permanently loses it (a clean stop thereafter).

    Ignores the frame so the test is deterministic; ``RealReactiveBackend`` still
    pulls a real (bright) synthetic frame so ``FollowController`` clears its
    dark-floor and actually steers toward the visible box.
    """

    def __init__(self, visible_result: TrackResult, visible_ticks: int) -> None:
        self._result = visible_result
        self._visible_ticks = int(visible_ticks)
        self.calls = 0

    def update(self, _frame) -> TrackResult:
        self.calls += 1
        if self.calls <= self._visible_ticks:
            return self._result
        return TrackResult(False, None, 0.0, 0, False)


# --------------------------------------------------------------------------- #
# Loopback rig (mirrors tests/test_contract.py setup/teardown)
# --------------------------------------------------------------------------- #
def _make_rig(*, tracker=None):
    """Build a RealReactiveBackend + ReactiveServer (shared mailbox) + a
    connected DeliberativeClient over loopback. Returns everything plus a
    ``teardown`` callable that closes the client and stops the server."""
    # The server and the backend SHARE the single-slot mailbox, so an Intent the
    # client sends over the socket lands where the backend's tick will drain it.
    mailbox = IntentMailbox()
    server = ReactiveServer(host="127.0.0.1", port=0, mailbox=mailbox)
    server.start()

    motor = FakeMotorDriver()
    sensor = FakeRangeSensor()  # default: 4.0 m clear, known
    backend = RealReactiveBackend(
        motor_driver=motor,
        range_sensor=sensor,
        camera=Camera(source="synthetic"),
        mailbox=mailbox,
        max_speed_mps=1.0,
        tick_hz=50.0,
        tracker=tracker,
    )

    client = DeliberativeClient("127.0.0.1", server.port)
    client.connect()
    assert server.wait_for_client(2.0)

    def teardown() -> None:
        try:
            client.close()
        finally:
            backend.close()
            server.stop()

    return backend, motor, sensor, server, client, mailbox, teardown


def _wait_for_intent(mailbox: IntentMailbox, timeout: float = 2.0) -> Intent:
    """Block until the client's Intent has reached the shared mailbox."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        got = mailbox.peek()
        if got is not None:
            return got
        time.sleep(0.01)
    raise AssertionError("intent never reached the reactive mailbox")


# --------------------------------------------------------------------------- #
# Test 1 — WiFi drops mid-DRIVE_GOAL: the goal still HALTS, never runs forever.
# --------------------------------------------------------------------------- #
def test_drive_goal_halts_after_client_drops():
    backend, motor, sensor, server, client, mailbox, teardown = _make_rig()
    try:
        # Client -> server: a bounded straight drive. target/speed/max_speed give
        # a finite open-loop duration (0.5 / (1.0 * 0.5) = 1.0 s ≈ 50 ticks).
        client.send_intent(
            Intent(
                Mode.DRIVE_GOAL,
                {"kind": "straight", "target": 0.5, "speed": 0.5},
                seq=1,
            )
        )
        adopted = _wait_for_intent(mailbox)
        assert adopted.mode == Mode.DRIVE_GOAL

        # Tick a few times WITH the client connected: confirm it's RUNNING and a
        # forward throttle is on the wheels. Publish each tick (client present).
        for _ in range(5):
            st = backend.tick()
            server.publish(st)
        assert st.mode == Mode.DRIVE_GOAL
        assert st.goal_status == GoalStatus.RUNNING
        left, right = motor.last
        assert left > 0 and right > 0  # driving forward

        # ---- WiFi drops: close the client mid-command. -----------------------
        client.close()

        # Keep ticking locally with NO client. publish() is now a best-effort
        # no-op push and must never raise. The DRIVE_GOAL must still complete and
        # HALT within its bounded open-loop duration — it does NOT drive forever.
        state = st
        for _ in range(5000):
            state = backend.tick()
            server.publish(state)  # no client -> no-op, must not raise
            if GoalStatus.COMPLETED in state.goal_status:
                break

        assert state.goal_status == "completed (timed, unverified)"
        assert backend.get_state().mode == Mode.IDLE
        # Halted: the wheels are zeroed, not still driving.
        assert motor.last == (0.0, 0.0)
        assert motor.stop_count >= 1

        # And a collision still forces SAFE_STOP with the client absent.
        sensor.trigger_obstacle()
        st = backend.tick()
        server.publish(st)
        assert st.mode == Mode.SAFE_STOP
        assert st.goal_status == GoalStatus.BLOCKED
        assert st.goal["reason"] == "obstacle"
        assert motor.last == (0.0, 0.0)
    finally:
        teardown()


# --------------------------------------------------------------------------- #
# Test 2 — WiFi drops mid-FOLLOW: keep following while visible, clean stop on loss.
# --------------------------------------------------------------------------- #
def test_follow_degrades_to_clean_stop_after_client_drops():
    # Confident, centered, far-ish box -> steer forward; lost after 3 ticks.
    visible = TrackResult(True, (270, 100, 100, 100), 0.95, 0, True)
    tracker = _LosingTracker(visible, visible_ticks=3)
    backend, motor, sensor, server, client, mailbox, teardown = _make_rig(
        tracker=tracker
    )
    try:
        # The reactive layer owns the camera; start it and wait for a real
        # (bright synthetic) frame so FOLLOW clears its dark-floor and steers.
        backend.start()
        assert backend.camera().wait_for_frame(2.0) is not None

        client.send_intent(
            Intent(Mode.FOLLOW, {"target": "nearest_person"}, seq=1)
        )
        adopted = _wait_for_intent(mailbox)
        assert adopted.mode == Mode.FOLLOW

        # First FOLLOW tick (client present): target visible -> steering issued.
        st = backend.tick()
        server.publish(st)
        assert st.mode == Mode.FOLLOW
        assert st.target_visible is True
        left, right = motor.last
        assert left > 0 and right > 0  # actively following (forward) the person

        # ---- WiFi drops mid-FOLLOW. ------------------------------------------
        client.close()

        # FOLLOW continues LOCALLY while the target stays visible: more steering.
        saw_visible_steering = False
        for _ in range(2):  # tracker stays visible through tick #3 total
            st = backend.tick()
            server.publish(st)  # no client -> no-op, must not raise
            if st.target_visible and motor.last != (0.0, 0.0):
                saw_visible_steering = True
        assert saw_visible_steering, "should keep steering while target visible"

        # Then the target is LOST: FOLLOW must degrade to a CLEAN STOP — it does
        # not drive blind on a stale box.
        lost_state = st
        for _ in range(10):
            lost_state = backend.tick()
            server.publish(lost_state)
            if lost_state.target_visible is False:
                break
        assert lost_state.mode == Mode.FOLLOW
        assert lost_state.target_visible is False
        assert motor.last == (0.0, 0.0)  # clean stop: set_motors(0, 0)

        # And a collision still forces SAFE_STOP with the client absent.
        sensor.trigger_obstacle()
        st = backend.tick()
        server.publish(st)
        assert st.mode == Mode.SAFE_STOP
        assert st.goal_status == GoalStatus.BLOCKED
        assert st.goal["reason"] == "obstacle"
        assert motor.last == (0.0, 0.0)
    finally:
        teardown()


# --------------------------------------------------------------------------- #
# Belt-and-braces: no exception escapes the tick/publish path with no client.
# --------------------------------------------------------------------------- #
def test_no_exception_when_client_absent():
    backend, motor, sensor, server, client, mailbox, teardown = _make_rig()
    try:
        client.close()
        # Give the server's reader loop a moment to notice the dropped client so
        # we exercise the genuinely-clientless publish path.
        time.sleep(0.05)
        for _ in range(20):
            st = backend.tick()
            server.publish(st)  # must never raise with the client gone
        assert st.mode in (Mode.IDLE, Mode.SAFE_STOP)
    finally:
        teardown()
