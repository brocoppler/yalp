#!/usr/bin/env python3
"""Step D done-signal: the end-of-laptop-phase integration checkpoint.

Proves that **A (the loop-to-loop contract + fake reactive backend) and D (the
agent loop) compose** — end to end, with **NO API key and NO network**. The
model is *mocked* (a scripted sequence of tool calls), so the only thing faked
is the cloud; everything else is the real machinery:

  * a real :class:`FakeReactiveBackend` ticking behind a real ``ReactiveServer``
    on a background thread (the simulated reactive process);
  * a real ``DeliberativeClient`` connected over a real localhost socket;
  * the real :class:`Agent` loop dispatching intents, settling goals, and
    feeding state back.

The scripted model walks the menu: drive → check_distance → turn →
describe_scene → stop, then finishes with a plain text turn (no tool call) so the
loop ends naturally. We print the scripted "scene" and the resulting state
transitions, and end with a clear ``AGENT LOOP OK`` line.

Run:  python3 scripts/agent_demo.py
"""

from __future__ import annotations

import os
import sys
import threading
from types import SimpleNamespace

# Make ``src/`` importable when run straight from a checkout (no install needed).
_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from yalp.contract.ipc import DeliberativeClient, ReactiveServer  # noqa: E402
from yalp.deliberative.agent import Agent, format_transcript  # noqa: E402
from yalp.reactive.fake_backend import FakeReactiveBackend  # noqa: E402


# --- a scripted, no-network stand-in for the Anthropic client ---------------
def _text(text):
    return SimpleNamespace(type="text", text=text)


def _tool(name, **inp):
    return SimpleNamespace(type="tool_use", name=name, input=inp, id=f"toolu_{name}")


def _response(blocks, stop_reason):
    return SimpleNamespace(
        content=blocks,
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=300, output_tokens=40),
    )


class ScriptedClient:
    """Returns one canned assistant turn per ``messages.create`` call, in order."""

    def __init__(self, script):
        self._script = list(script)
        self._i = 0
        self.calls = 0
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls += 1
        blocks = self._script[min(self._i, len(self._script) - 1)]
        self._i += 1
        stop = "tool_use" if any(b.type == "tool_use" for b in blocks) else "end_turn"
        return _response(blocks, stop)


SCRIPT = [
    [_text("Let me roll forward and have a look."), _tool("drive", distance_m=0.4, speed=0.5)],
    [_tool("check_distance")],
    [_tool("turn", angle_deg=45.0, speed=0.5)],
    [_tool("describe_scene", detail="quick")],
    [_text("Looks clear. Stopping here."), _tool("stop")],
    [_text("Done: I drove forward, checked the distance, turned, looked around, and stopped.")],
]

SCENE = "A tidy living room: a sofa, a coffee table with a mug, and a window with daylight."


def main() -> int:
    print("=" * 72)
    print("yalp — STEP D: agent loop end-to-end (model MOCKED, no key/network)")
    print("=" * 72)

    backend = FakeReactiveBackend(tick_hz=50.0)
    server = ReactiveServer(host="127.0.0.1", port=0, mailbox=backend.mailbox)
    server.start()
    print(f"[reactive] ReactiveServer listening on 127.0.0.1:{server.port}")

    stop = threading.Event()
    runner = threading.Thread(
        target=backend.run, kwargs={"server": server, "stop_event": stop}, daemon=True
    )
    runner.start()
    print("[reactive] FakeReactiveBackend tick loop started @ 50 Hz")

    client = DeliberativeClient("127.0.0.1", server.port)
    client.connect()
    server.wait_for_client(2.0)
    print("[deliberative] connected to reactive server\n")

    # The mocked model + a scripted "scene" stand-in for the vision call.
    scripted = ScriptedClient(SCRIPT)
    describe_calls = {"n": 0}

    def describe(detail="quick", question=None, model=None):
        describe_calls["n"] += 1
        return SCENE

    agent = Agent(
        client=scripted,
        reactive=client,
        describe_scene=describe,
        max_steps=10,
    )

    command = "drive forward, check the path, turn, look around, and stop"
    print(f">>> USER COMMAND: {command}\n")
    print(f"(scripted camera scene: {SCENE})\n")

    transcript = agent.run_turn(command)
    print(format_transcript(transcript))

    stop.set()
    runner.join(timeout=2.0)
    client.close()
    server.stop()

    # Verdict: the loop must have driven, observed a completed (timed, unverified)
    # move, described the scene, and ended cleanly.
    tools = [e.text.split("(")[0] for e in transcript if e.kind == "tool"]
    states = [e.text for e in transcript if e.kind == "state"]
    saw_completed = any("completed (timed, unverified)" in s for s in states)
    saw_scene = any(SCENE in e.text for e in transcript)
    ok = (
        scripted.calls == len(SCRIPT)
        and "drive" in tools and "turn" in tools and "stop" in tools
        and "check_distance" in tools and "describe_scene" in tools
        and saw_completed
        and saw_scene
        and describe_calls["n"] == 1
    )

    print()
    print(f"    model calls: {scripted.calls}  tools dispatched: {tools}")
    print(f"    observed a timed/unverified completion: {saw_completed}")
    print(f"    scene reported back to the loop: {saw_scene}")
    print()
    if ok:
        print("AGENT LOOP OK")
        return 0
    print("AGENT LOOP FAILED: expected drive/check/turn/describe/stop end-to-end")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
