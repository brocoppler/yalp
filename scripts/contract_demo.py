#!/usr/bin/env python3
"""Step A done-signal: prove the loop-to-loop seam works by execution.

Runs with **no hardware and no network beyond localhost**:

  1. start a ``FakeReactiveBackend`` behind a ``ReactiveServer`` on a background
     thread (the reactive process, simulated);
  2. connect a ``DeliberativeClient`` (the cloud side, simulated);
  3. send ONE hand-authored ``Intent`` — drive forward 0.5 m;
  4. poll ``RobotState`` for a few ticks and print both the Intent sent and the
     states received, showing the mode transitions and the
     ``"completed (timed, unverified)"`` status;
  5. print ``STEP A OK``.

Run:  python3 scripts/contract_demo.py
"""

from __future__ import annotations

import os
import sys
import threading
import time

# Make ``src/`` importable when run straight from a checkout (no install needed).
_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from yalp.contract.ipc import DeliberativeClient, ReactiveServer  # noqa: E402
from yalp.contract.messages import GoalStatus, Intent, Mode, RobotState  # noqa: E402
from yalp.reactive.fake_backend import FakeReactiveBackend  # noqa: E402


def _fmt(state: RobotState) -> str:
    goal = state.goal or {}
    return (
        f"mode={state.mode.value:<10} status={state.goal_status!r:<30} "
        f"elapsed={state.goal_elapsed_s:5.2f}s progress={goal.get('progress', 0.0):.2f} "
        f"obstacle={state.obstacle}"
    )


def main() -> int:
    print("=" * 72)
    print("yalp — STEP A: loop-to-loop contract handshake (fake reactive backend)")
    print("=" * 72)

    # 1. Reactive side: fake backend + server sharing one mailbox.
    backend = FakeReactiveBackend(max_speed_mps=0.5, tick_hz=50.0)
    server = ReactiveServer(host="127.0.0.1", port=0, mailbox=backend.mailbox)
    server.start()
    print(f"[reactive] ReactiveServer listening on 127.0.0.1:{server.port}")

    stop = threading.Event()
    runner = threading.Thread(
        target=backend.run, kwargs={"server": server, "stop_event": stop}, daemon=True
    )
    runner.start()
    print("[reactive] FakeReactiveBackend tick loop started @ 50 Hz")

    # 2. Deliberative side: connect.
    client = DeliberativeClient("127.0.0.1", server.port)
    client.connect()
    server.wait_for_client(2.0)
    print("[deliberative] connected to reactive server\n")

    # 3. Send ONE hand-authored Intent: drive forward 0.5 m.
    intent = Intent(
        mode=Mode.DRIVE_GOAL,
        goal={"kind": "straight", "target": 0.5, "speed": 0.5},
        seq=1,
    )
    print(">>> INTENT SENT (deliberative -> reactive):")
    print("    " + intent.to_json().strip())
    client.send_intent(intent)
    print()

    # 4. Poll RobotState across a few ticks; show the transitions.
    print("<<< ROBOTSTATE RECEIVED (reactive -> deliberative), polled per step:")
    modes_seen: list[str] = []
    completed = False
    deadline = time.monotonic() + 5.0
    last_line = ""
    while time.monotonic() < deadline:
        state = client.request_state(timeout=1.0)
        if state is None:
            time.sleep(0.05)
            continue
        if not modes_seen or modes_seen[-1] != state.mode.value:
            modes_seen.append(state.mode.value)
        line = _fmt(state)
        if line != last_line:
            print("    " + line)
            last_line = line
        if GoalStatus.COMPLETED in state.goal_status:
            completed = True
            break
        time.sleep(0.08)

    print()
    print(f"    mode transitions observed: {' -> '.join(modes_seen)}")

    # 5. Verdict.
    stop.set()
    runner.join(timeout=2.0)
    client.close()
    server.stop()

    ok = (
        completed
        and "DRIVE_GOAL" in modes_seen
        and modes_seen[-1] == "IDLE"
    )
    print()
    if ok:
        print("Goal reported: 'completed (timed, unverified)' — open-loop, no encoders.")
        print("STEP A OK")
        return 0
    print("STEP A FAILED: did not observe the expected drive -> completed transition")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
