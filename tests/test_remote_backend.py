"""Integration tests for the deliberative ``RemoteReactiveBackend`` (the WiFi glue).

These prove the cross-machine topology over LOOPBACK, mirroring the real command
pair (``yalp reactive`` on the Pi, ``yalp agent --host`` on the laptop) with an
in-process ``yalp reactive``-equivalent: a :class:`FakeReactiveBackend` ticking
behind a :class:`ReactiveServer` on an ephemeral port. We then connect the agent's
:class:`RemoteReactiveBackend`, round-trip an intent + state (including
``speed_limit`` and ``goal_status``), kill the server to simulate the WiFi drop,
and assert the agent-visible degradation + reconnect/backoff behavior matches the
safety contract exercised by ``tests/test_wifi_degradation.py``.

No API key, camera, or hardware required.
"""

from __future__ import annotations

import threading
import time

from yalp.contract.ipc import ReactiveServer
from yalp.contract.messages import GoalStatus, Intent, Mode, RobotState
from yalp.deliberative.remote_backend import RemoteReactiveBackend
from yalp.reactive.fake_backend import FakeReactiveBackend


# --------------------------------------------------------------------------- #
# In-process "yalp reactive"-equivalent rig
# --------------------------------------------------------------------------- #
def _start_reactive(port: int = 0):
    """Start a FakeReactiveBackend ticking behind a ReactiveServer on ``port``.

    Returns ``(server, backend, shutdown)`` where ``shutdown()`` stops the tick
    thread, the camera, and the server. ``port=0`` binds an ephemeral port
    (read back via ``server.port``).
    """
    backend = FakeReactiveBackend(camera_source="synthetic", tick_hz=50.0)
    server = ReactiveServer(host="127.0.0.1", port=port, mailbox=backend.mailbox)
    server.start()
    stop = threading.Event()
    thread = threading.Thread(
        target=backend.run,
        kwargs={"server": server, "stop_event": stop},
        daemon=True,
    )
    thread.start()

    def shutdown() -> None:
        stop.set()
        thread.join(timeout=2.0)
        backend.stop()
        server.stop()

    return server, backend, shutdown


def _spin(pred, timeout: float = 3.0, interval: float = 0.02):
    """Poll ``pred`` until it returns a truthy value or ``timeout`` elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        val = pred()
        if val:
            return val
        time.sleep(interval)
    return pred()


# --------------------------------------------------------------------------- #
# Test doubles for deterministic backoff coverage
# --------------------------------------------------------------------------- #
class _FakeClient:
    """A DeliberativeClient stand-in whose reachability we toggle via ``up``."""

    def __init__(self) -> None:
        self.up = False
        self.connect_calls = 0
        self.sent: list[Intent] = []
        self.state = RobotState(mode=Mode.IDLE, speed_limit=0.4)

    def connect(self, retries=1, backoff=0.05, max_backoff=1.0):
        self.connect_calls += 1
        if not self.up:
            raise ConnectionError("reactive server down")
        return self

    def close(self) -> None:
        pass

    def send_intent(self, intent: Intent) -> None:
        if not self.up:
            raise OSError("broken pipe")
        self.sent.append(intent)

    def request_state(self, timeout: float = 2.0):
        if not self.up:
            raise OSError("broken pipe")
        return self.state


# --------------------------------------------------------------------------- #
# 1. Round-trip: intent up, state (speed_limit + goal_status) down.
# --------------------------------------------------------------------------- #
def test_remote_backend_roundtrips_intent_and_state():
    server, backend, shutdown = _start_reactive()
    remote = RemoteReactiveBackend(
        host="127.0.0.1", port=server.port, camera_source="synthetic"
    )
    try:
        remote.connect()
        assert remote.connected is True

        # Intent UP: a bounded straight drive (duration = 0.5/(0.5*0.5) = 2s).
        remote.send_intent(
            Intent(Mode.DRIVE_GOAL, {"kind": "straight", "target": 0.5, "speed": 0.5}, seq=1)
        )

        # State DOWN: poll until the adopted DRIVE_GOAL mode has crossed the wire.
        def _drive_state():
            st = remote.request_state(timeout=0.2)
            return st if st is not None and st.mode == Mode.DRIVE_GOAL else None

        state = _spin(_drive_state)
        assert state is not None and state.mode == Mode.DRIVE_GOAL
        # goal_status crosses the wire and is a valid contract value.
        assert state.goal_status in (GoalStatus.RUNNING, GoalStatus.COMPLETED)
        # speed_limit round-trips (recently added contract field).
        assert isinstance(state.speed_limit, float)
        assert state.speed_limit == 1.0

        # get_state() serves the last snapshot without any I/O.
        assert remote.get_state().mode == Mode.DRIVE_GOAL
    finally:
        remote.close()
        shutdown()


# --------------------------------------------------------------------------- #
# 2. WiFi drops: degrade to last-known snapshot, then reconnect on server return.
# --------------------------------------------------------------------------- #
def test_remote_backend_degrades_then_reconnects_over_loopback():
    server, backend, shutdown = _start_reactive()
    port = server.port
    remote = RemoteReactiveBackend(
        host="127.0.0.1", port=port, camera_source="synthetic",
        backoff=0.02, max_backoff=0.1,
    )
    try:
        remote.connect()
        assert remote.connected is True
        baseline = _spin(lambda: remote.request_state(timeout=0.2))
        assert baseline is not None

        # ---- WiFi drops: kill the reactive process entirely. -----------------
        shutdown()

        # Sends now fail; the link flips to disconnected WITHOUT raising to the
        # caller (the agent must never wedge). A stale-but-present snapshot is
        # still served so the agent has state to reason over.
        def _dropped() -> bool:
            remote.send_intent(Intent(Mode.IDLE, None, seq=99))
            remote.request_state(timeout=0.1)
            return remote.connected is False

        assert _spin(_dropped, timeout=3.0), "link should register as dropped"
        degraded = remote.request_state(timeout=0.1)
        assert degraded is not None  # last-known snapshot, not None / not a crash
        assert degraded.mode == baseline.mode
        assert remote.get_state().mode == baseline.mode

        # ---- The Pi comes back on the SAME port (WiFi restored). -------------
        server2, backend2, shutdown2 = _start_reactive(port=port)
        try:
            def _reconnected() -> bool:
                remote.request_state(timeout=0.1)
                return remote.connected

            assert _spin(_reconnected, timeout=3.0, interval=0.05), (
                "should transparently reconnect once the server returns"
            )
            fresh = remote.request_state(timeout=0.5)
            assert fresh is not None
        finally:
            shutdown2()
    finally:
        remote.close()
        # ``shutdown`` already ran mid-test; calling the original again is a no-op
        # on already-stopped resources, so we skip it here.


# --------------------------------------------------------------------------- #
# 3. Backoff schedule is deterministic: gate attempts, grow, reset on success.
# --------------------------------------------------------------------------- #
def test_reconnect_backoff_gates_attempts_and_resets_on_success():
    fake = _FakeClient()  # starts DOWN
    remote = RemoteReactiveBackend(
        host="pi", port=1, client=fake, backoff=0.05, max_backoff=0.2
    )

    # Initial connect fails cleanly (no raise) and schedules a retry.
    remote.connect()
    assert remote.connected is False
    assert fake.connect_calls == 1
    assert remote._backoff == 0.1  # doubled after one failed attempt

    # An immediate op is INSIDE the backoff window -> no new attempt (don't hammer).
    remote.request_state(timeout=0.1)
    assert fake.connect_calls == 1

    # After the window elapses, the next op attempts again (still down) -> grows.
    time.sleep(0.06)
    remote.request_state(timeout=0.1)
    assert fake.connect_calls == 2
    assert remote._backoff == 0.2  # min(0.1*2, 0.2)

    # Bring the link up; after the window the next op reconnects and RESETS backoff.
    fake.up = True
    time.sleep(0.22)
    state = remote.request_state(timeout=0.1)
    assert remote.connected is True
    assert fake.connect_calls == 3
    assert remote._backoff == 0.05  # reset to base on success
    # And a real snapshot (with its speed_limit) now flows through.
    assert state is not None
    assert state.speed_limit == 0.4

    # Sends now land in the fake client's mailbox.
    remote.send_intent(Intent(Mode.IDLE, None, seq=7))
    assert fake.sent and fake.sent[-1].seq == 7


# --------------------------------------------------------------------------- #
# 4. A best-effort initial connect to a dead host never raises.
# --------------------------------------------------------------------------- #
def test_connect_to_absent_server_degrades_without_raising():
    # Nothing is listening on this port; connect must not raise, just degrade.
    remote = RemoteReactiveBackend(host="127.0.0.1", port=1, backoff=0.02, max_backoff=0.05)
    remote.connect()  # must not raise
    assert remote.connected is False
    # Reads serve None (never connected) and never raise.
    assert remote.request_state(timeout=0.05) is None
    assert isinstance(remote.get_state(), RobotState)
    # Sends are dropped silently while disconnected.
    remote.send_intent(Intent(Mode.IDLE, None, seq=1))
    assert remote.connected is False
    remote.close()
