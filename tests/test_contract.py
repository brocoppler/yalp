"""Contract tests — no network/hardware/API key required.

Covers the loop-to-loop seam specified in software-spec.md §2:

  1. Intent / RobotState JSON round-trip.
  2. The single-slot mailbox keeps only the highest-seq Intent (last-write-wins).
  3. Over a REAL loopback socket, a client sends an Intent and reads a RobotState.
  4. The fake backend applies a drive Intent and reports a "timed, unverified" goal.
  5. A simulated collision drives the backend to SAFE_STOP/BLOCKED and a new
     Intent does not override it while blocked.
"""

from __future__ import annotations

import time

from yalp.contract.abilities import ANTHROPIC_TOOLS, intent_for
from yalp.contract.ipc import DeliberativeClient, IntentMailbox, ReactiveServer
from yalp.contract.messages import (
    GoalStatus,
    Intent,
    Mode,
    RobotState,
    parse_line,
)
from yalp.reactive.fake_backend import FakeReactiveBackend


# --- 1. JSON round-trip ------------------------------------------------------
def test_intent_json_roundtrip():
    intent = Intent(
        mode=Mode.DRIVE_GOAL,
        goal={"kind": "straight", "target": 0.5, "speed": 0.5},
        seq=7,
    )
    line = intent.to_json()
    assert line.endswith("\n")
    back = Intent.from_json(line)
    assert back.mode == Mode.DRIVE_GOAL
    assert back.goal == {"kind": "straight", "target": 0.5, "speed": 0.5}
    assert back.seq == 7
    # parse_line dispatches on the "type" discriminator.
    assert isinstance(parse_line(line), Intent)


def test_state_json_roundtrip():
    state = RobotState(
        mode=Mode.FOLLOW,
        goal={"target": "nearest_person"},
        goal_status=GoalStatus.RUNNING,
        distance_m=0.8,
        distance_known=True,
        target_visible=True,
        target_bbox=(1, 2, 3, 4),
        tracker_score=0.9,
        ticks_since_last_detector_confirmation=3,
        last_frame_id="f-10231",
    )
    line = state.to_json()
    assert line.endswith("\n")
    back = RobotState.from_json(line)
    assert back.mode == Mode.FOLLOW
    assert back.goal == {"target": "nearest_person"}
    assert back.goal_status == GoalStatus.RUNNING
    assert back.target_bbox == (1, 2, 3, 4)  # list -> tuple on parse
    assert back.tracker_score == 0.9
    assert back.ticks_since_last_detector_confirmation == 3
    assert isinstance(parse_line(line), RobotState)


def test_abilities_are_single_source_of_truth():
    names = {t["name"] for t in ANTHROPIC_TOOLS}
    assert {"drive", "turn", "stop", "enter_follow_mode"} <= names
    assert "explore" in names  # deliberative sugar, present in the menu
    # drive maps to a DRIVE_GOAL straight intent; describe_scene maps to no intent.
    di = intent_for("drive", {"distance_m": 0.5}, seq=1)
    assert di is not None and di.mode == Mode.DRIVE_GOAL
    assert di.goal["kind"] == "straight" and di.goal["target"] == 0.5
    assert intent_for("describe_scene", {"detail": "quick"}, seq=2) is None


# --- 2. Single-slot, last-write-wins mailbox ---------------------------------
def test_mailbox_keeps_highest_seq():
    mb = IntentMailbox()
    mb.put(Intent(Mode.IDLE, None, seq=1))
    mb.put(Intent(Mode.DRIVE_GOAL, {"target": 1.0}, seq=3))
    mb.put(Intent(Mode.FOLLOW, None, seq=2))  # lower seq must NOT overwrite
    got = mb.take()
    assert got is not None
    assert got.seq == 3
    assert got.mode == Mode.DRIVE_GOAL
    # take() consumes — the slot is empty afterwards (no queue).
    assert mb.take() is None


# --- 3. Real loopback socket round-trip --------------------------------------
def test_loopback_intent_and_state():
    server = ReactiveServer(host="127.0.0.1", port=0)
    server.start()
    client = None
    try:
        client = DeliberativeClient("127.0.0.1", server.port)
        client.connect()
        assert server.wait_for_client(2.0)

        # Client -> server: an Intent lands in the server's mailbox.
        client.send_intent(Intent(Mode.FOLLOW, {"target": "nearest_person"}, seq=1))
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and server.mailbox.peek() is None:
            time.sleep(0.01)
        got = server.mailbox.peek()
        assert got is not None
        assert got.seq == 1
        assert got.mode == Mode.FOLLOW

        # Server -> client: a published RobotState is read back.
        server.publish(RobotState(mode=Mode.DRIVE_GOAL, goal_status=GoalStatus.RUNNING))
        state = client.request_state(timeout=2.0)
        assert state is not None
        assert state.mode == Mode.DRIVE_GOAL
        assert state.goal_status == GoalStatus.RUNNING
    finally:
        if client is not None:
            client.close()
        server.stop()


# --- 4. Fake backend: timed, unverified drive completion ---------------------
def test_fake_backend_drive_reports_timed_unverified():
    backend = FakeReactiveBackend(max_speed_mps=1.0, tick_hz=50.0)
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
    assert "timed, unverified" in state.goal_status
    assert state.goal_status == "completed (timed, unverified)"
    assert backend.get_state().mode == Mode.IDLE


# --- 5. Collision -> SAFE_STOP/BLOCKED, sticky, intent cannot override --------
def test_fake_backend_collision_latches_safe_stop():
    backend = FakeReactiveBackend()
    backend.apply_intent(
        Intent(Mode.DRIVE_GOAL, {"kind": "straight", "target": 1.0, "speed": 1.0}, seq=1)
    )
    st = backend.tick()
    assert st.mode == Mode.DRIVE_GOAL

    # Obstacle appears -> collision-stop overrides everything.
    backend.trigger_collision()
    st = backend.tick()
    assert st.mode == Mode.SAFE_STOP
    assert st.goal_status == GoalStatus.BLOCKED
    assert st.blocked is True
    assert st.goal["reason"] == "obstacle"

    # A new intent must NOT override the active safety stop while still blocked.
    backend.apply_intent(Intent(Mode.FOLLOW, {"target": "nearest_person"}, seq=2))
    st = backend.tick()
    assert st.mode == Mode.SAFE_STOP

    # SAFE_STOP is sticky: clearing the obstacle AND a fresh intent are both
    # required to resume. The pending FOLLOW (seq=2) is adopted once clear.
    backend.clear_obstacle()
    st = backend.tick()
    assert st.mode == Mode.FOLLOW


def test_fake_backend_unknown_echo_biases_to_stop():
    backend = FakeReactiveBackend()
    backend.set_sensor(distance_m=10.0, known=False)  # echo timeout -> unknown
    st = backend.tick()
    assert st.mode == Mode.SAFE_STOP
    assert st.goal["reason"] == "echo_timeout"
