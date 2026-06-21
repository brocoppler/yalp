"""Agent-loop tests — no network, key, camera, or SDK.

The Anthropic client is *injected* as a scripted stub, the reactive layer is the
real :class:`FakeReactiveBackend` behind a real ``ReactiveServer`` /
``DeliberativeClient`` over a localhost socket (the same machinery the contract
tests exercise), and the vision callable is a recording stub. So these run the
*real* agent loop with only the cloud mocked.

Coverage (software-spec.md §3):
  1. a mocked drive tool_use → the Agent sends the right Intent to the fake
     backend and reads RobotState back.
  2. model_router: haiku by default, sonnet on multi-step explore, opus on
     describe_scene(detail='full').
  3. the hard per-session budget cap stops the loop cleanly when exceeded.
  4. explore() expands into a drive/turn/describe sugar loop (no EXPLORE mode).
  5. a collision/BLOCKED state is surfaced to the agent and it does not blindly
     reverse.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

import argparse

from yalp import config
from yalp.contract.ipc import DeliberativeClient, ReactiveServer
from yalp.contract.messages import GoalStatus, Mode
from yalp.deliberative import model_router
from yalp.deliberative.agent import Agent, build_context
from yalp.deliberative.model_router import Budget, RoutingContext
from yalp.reactive.fake_backend import FakeReactiveBackend


# --- scripted, no-network stand-in for the Anthropic client -----------------
def _text(text):
    return SimpleNamespace(type="text", text=text)


def _tool(name, **inp):
    return SimpleNamespace(type="tool_use", name=name, input=inp, id=f"toolu_{name}")


class ScriptedClient:
    """Returns one canned assistant turn per ``messages.create`` call.

    Once the script is exhausted it keeps returning the final turn (so a runaway
    loop keeps "calling tools" — used by the budget test).
    """

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
        return SimpleNamespace(
            content=blocks,
            stop_reason=stop,
            usage=SimpleNamespace(input_tokens=1000, output_tokens=50),
        )


class RecordingDescribe:
    """A stub describe_scene callable that records its calls."""

    def __init__(self, reply="A plain room with a chair."):
        self.reply = reply
        self.calls = []

    def __call__(self, detail="quick", question=None, model=None):
        self.calls.append({"detail": detail, "question": question, "model": model})
        return self.reply


class _Harness:
    """A real fake-backend + server + client, ticking on a thread."""

    def __init__(self, backend=None, tick_hz=50.0):
        # Tests must never touch real hardware: force the synthetic camera source.
        self.backend = backend or FakeReactiveBackend(
            tick_hz=tick_hz, camera_source="synthetic"
        )
        self.server = ReactiveServer(host="127.0.0.1", port=0, mailbox=self.backend.mailbox)
        self.server.start()
        self.stop = threading.Event()
        self.runner = threading.Thread(
            target=self.backend.run,
            kwargs={"server": self.server, "stop_event": self.stop},
            daemon=True,
        )
        self.runner.start()
        self.client = DeliberativeClient("127.0.0.1", self.server.port)
        self.client.connect()
        self.server.wait_for_client(2.0)

    def close(self):
        self.stop.set()
        self.runner.join(timeout=2.0)
        self.client.close()
        self.server.stop()


@pytest.fixture
def harness():
    h = _Harness()
    try:
        yield h
    finally:
        h.close()


# --- 1. drive tool_use -> Intent sent + RobotState read back ----------------
def test_agent_dispatches_drive_intent_and_reads_state(harness):
    client = ScriptedClient([
        [_text("Driving forward."), _tool("drive", distance_m=0.5, speed=1.0)],
        [_text("Done.")],  # no tool_use -> loop ends
    ])
    describe = RecordingDescribe()
    agent = Agent(client=client, reactive=harness.client, describe_scene=describe)

    transcript = agent.run_turn("drive forward half a meter")

    # The model was consulted, the tool was dispatched, and a state came back.
    tools = [e for e in transcript if e.kind == "tool"]
    assert any(e.data.get("name") == "drive" for e in tools)
    states = [e for e in transcript if e.kind == "state"]
    assert states, "the agent must read a RobotState back after the intent"
    # A timed/unverified completion is surfaced (open-loop, no encoders).
    assert any("completed (timed, unverified)" in e.text for e in states)
    # The intent actually reached the reactive backend.
    assert harness.backend.get_state().mode in (Mode.IDLE, Mode.DRIVE_GOAL)


# --- 2. model router table ---------------------------------------------------
def test_router_defaults_to_haiku():
    assert model_router.pick_model(RoutingContext(user_text="what's ahead?")) == config.MODEL_FAST


def test_router_escalates_to_sonnet_on_multistep_explore():
    ctx = RoutingContext(user_text="explore the room and tell me what's there")
    assert model_router.pick_model(ctx) == config.MODEL_MID
    assert model_router.is_multi_step_explore_request("go look around the house")


def test_router_escalates_to_opus_on_describe_scene_full():
    ctx = RoutingContext(intent_name="describe_scene", intent_detail="full")
    decision = model_router.route(ctx)
    assert decision.model == config.MODEL_BIG
    assert "describe_scene(detail=full)" in decision.reason


def test_router_escalates_to_opus_on_read_text():
    ctx = RoutingContext(user_text="read the sign on the door")
    assert model_router.pick_model(ctx) == config.MODEL_BIG


def test_router_escalates_to_sonnet_on_need_more_reasoning():
    ctx = RoutingContext(user_text="hmm", need_more_reasoning=True)
    assert model_router.pick_model(ctx) == config.MODEL_MID


# --- 3. budget cap stops the loop cleanly ------------------------------------
def test_budget_cap_stops_loop_cleanly(harness):
    # The model would keep issuing drive intents forever...
    client = ScriptedClient([[_tool("drive", distance_m=0.1, speed=1.0)]])
    describe = RecordingDescribe()
    # ...but a one-call budget halts after the first model call.
    budget = Budget(max_calls=1, max_tokens=10_000_000)
    agent = Agent(client=client, reactive=harness.client, describe_scene=describe,
                  budget=budget, max_steps=50)

    transcript = agent.run_turn("drive drive drive")

    # Exactly one model call was made; the loop did not run away.
    assert client.calls == 1
    assert budget.exhausted()
    # The stop is surfaced and the robot is told to fall back to IDLE.
    assert any(e.kind == "note" and "budget" in e.text.lower() for e in transcript)


# --- 4. explore() is deliberative sugar (no reactive EXPLORE mode) -----------
def test_explore_expands_to_drive_turn_describe_loop(harness):
    # explore() needs no model calls — it is pure agent-level sugar.
    client = ScriptedClient([[_text("(unused)")]])
    describe = RecordingDescribe(reply="An open hallway ahead.")
    agent = Agent(client=client, reactive=harness.client, describe_scene=describe)

    entries = agent.explore("find the kitchen", legs=2)

    # It described the scene on each leg...
    assert len(describe.calls) == 2
    # ...and issued drive + turn intents (DRIVE_GOAL), never an EXPLORE mode.
    tool_states = [e for e in entries if e.kind == "state"]
    assert tool_states, "explore must dispatch motion intents and read state back"
    # The reactive contract has no EXPLORE mode at all.
    assert not hasattr(Mode, "EXPLORE")
    assert "EXPLORE" not in {m.value for m in Mode}
    # The scene description is folded into the transcript (the 'report').
    assert any("hallway" in e.text.lower() for e in entries)


def test_explore_sends_only_drive_goal_intents(harness):
    client = ScriptedClient([[_text("(unused)")]])
    agent = Agent(client=client, reactive=harness.client, describe_scene=RecordingDescribe())

    agent.explore("scout the area", legs=1)
    # After an explore leg the backend has only ever seen DRIVE_GOAL / IDLE
    # modes — never anything resembling an EXPLORE reactive mode.
    assert harness.backend.get_state().mode in (Mode.IDLE, Mode.DRIVE_GOAL, Mode.SAFE_STOP)


# --- spoken OUTPUT (--speak): the agent reliably vocalizes its own words -----
class FakeSpeak:
    """A no-audio speak callback (simulates --speak) that records every line."""

    def __init__(self):
        self.said: list[str] = []

    def __call__(self, text):
        self.said.append(text)


def test_agent_vocalizes_narration_and_report_when_speak_enabled(harness):
    # The model narrates, calls a tool, then gives a final answer with no tool.
    client = ScriptedClient([
        [_text("Driving forward now."), _tool("drive", distance_m=0.5, speed=1.0)],
        [_text("I drove forward."), _tool("speak", text="All clear ahead.")],
        [_text("Final report: the path is clear.")],
    ])
    speak = FakeSpeak()
    agent = Agent(client=client, reactive=harness.client,
                  describe_scene=RecordingDescribe(), speak=speak)

    agent.run_turn("drive forward and report")

    # Narration, the speak-tool text, AND the final report were all spoken.
    assert "Driving forward now." in speak.said
    assert "I drove forward." in speak.said
    assert "All clear ahead." in speak.said
    assert "Final report: the path is clear." in speak.said
    # Only the model's words are spoken — never routing notes or raw tool calls.
    note_texts = {e.text for e in agent._transcript if e.kind == "note"}
    tool_texts = {e.text for e in agent._transcript if e.kind == "tool"}
    assert not (set(speak.said) & note_texts)
    assert not (set(speak.said) & tool_texts)


def test_agent_is_silent_without_speak_callback(harness):
    client = ScriptedClient([
        [_text("Driving forward now."), _tool("drive", distance_m=0.5, speed=1.0)],
        [_text("Done.")],
    ])
    agent = Agent(client=client, reactive=harness.client,
                  describe_scene=RecordingDescribe())  # no speak= -> default off

    assert agent._speak is None
    agent.run_turn("drive forward")
    # Nothing was ever routed through the voice path.
    assert agent._spoken == set()


def test_agent_does_not_double_speak_identical_text(harness):
    # The model narrates a line AND calls the speak tool with the SAME text, then
    # repeats it once more — it must be vocalized exactly once.
    client = ScriptedClient([
        [_text("Mission complete."), _tool("speak", text="Mission complete.")],
        [_text("Mission complete.")],
    ])
    speak = FakeSpeak()
    agent = Agent(client=client, reactive=harness.client,
                  describe_scene=RecordingDescribe(), speak=speak)

    agent.run_turn("do the thing")

    assert speak.said.count("Mission complete.") == 1


# --- 5. collision / BLOCKED is surfaced; the agent does not reverse ----------
def test_blocked_state_surfaced_and_no_blind_reverse():
    backend = FakeReactiveBackend(tick_hz=50.0, camera_source="synthetic")
    backend.trigger_collision()  # obstacle is inside the safe-stop threshold
    harness = _Harness(backend=backend)
    try:
        # The model issues a single forward drive, then stops.
        client = ScriptedClient([
            [_text("Trying to move forward."), _tool("drive", distance_m=0.5, speed=1.0)],
            [_text("I'm blocked; holding position.")],  # no reverse drive
        ])
        agent = Agent(client=client, reactive=harness.client,
                      describe_scene=RecordingDescribe(), settle_timeout=2.0)

        transcript = agent.run_turn("drive forward")

        # The BLOCKED stop is surfaced back to the agent...
        state_entries = [e for e in transcript if e.kind == "state"]
        assert any("BLOCKED" in e.text for e in state_entries)
        # ...with an explicit "did not reverse" framing.
        assert any("did not reverse" in e.text.lower() for e in state_entries)

        # The agent never synthesized a backward (negative-distance) drive on its
        # own — it only dispatched the model's forward drive.
        drives = [e for e in transcript if e.kind == "tool" and e.data.get("name") == "drive"]
        assert drives, "the forward drive should have been attempted"
        assert all(d.data["params"].get("distance_m", 0) >= 0 for d in drives)
    finally:
        harness.close()


# --- CLI parsing: positional words and --command alias -----------------------

def _make_agent_parser():
    """Build an isolated argument parser that mirrors the 'agent' subcommand."""
    from yalp.deliberative.agent_cli import add_parser
    root = argparse.ArgumentParser()
    sub = root.add_subparsers(dest="subcommand")
    add_parser(sub)
    return root


def test_agent_cli_positional_words():
    parser = _make_agent_parser()
    args = parser.parse_args(["agent", "look", "around", "and", "report"])
    assert " ".join(args.words) == "look around and report"


def test_agent_cli_positional_words_quoted():
    parser = _make_agent_parser()
    args = parser.parse_args(["agent", "look around and report"])
    assert args.words == ["look around and report"]


def test_agent_cli_command_flag_alias():
    parser = _make_agent_parser()
    args = parser.parse_args(["agent", "--command", "explore the room"])
    assert args.command == "explore the room"
    assert (args.words or []) == []


def test_agent_cli_positional_overrides_command_flag():
    """Positional words take precedence — resolved in run(), checked here via words."""
    parser = _make_agent_parser()
    args = parser.parse_args(["agent", "--command", "ignored", "actual", "command"])
    # Positional words are present and non-empty; run() will prefer them.
    assert " ".join(args.words) == "actual command"


def test_agent_cli_no_args_gives_empty_words():
    parser = _make_agent_parser()
    args = parser.parse_args(["agent"])
    assert (args.words or []) == []
    assert args.command is None


def test_agent_cli_steps_flag_unchanged():
    parser = _make_agent_parser()
    args = parser.parse_args(["agent", "--steps", "5", "go", "forward"])
    assert args.steps == 5
    assert args.words == ["go", "forward"]


# --- camera source: REAL EYES by default, --synthetic forces synthetic -------
def test_agent_cli_synthetic_flag_defaults_false():
    parser = _make_agent_parser()
    args = parser.parse_args(["agent", "look", "around"])
    assert args.synthetic is False


def test_agent_cli_synthetic_flag_sets_true():
    parser = _make_agent_parser()
    args = parser.parse_args(["agent", "--synthetic", "look", "around"])
    assert args.synthetic is True


# --- CLI helper: _camera_source maps args -> a camera source string ----------
def test_agent_cli_camera_source_defaults_to_real_webcam():
    """No --synthetic => the run wires up the REAL webcam source."""
    from yalp.deliberative.agent_cli import _camera_source

    parser = _make_agent_parser()
    args = parser.parse_args(["agent", "describe the room"])
    assert _camera_source(args) == "webcam"


def test_agent_cli_synthetic_flag_forces_synthetic_source():
    """--synthetic => the run forces the synthetic camera source."""
    from yalp.deliberative.agent_cli import _camera_source

    parser = _make_agent_parser()
    args = parser.parse_args(["agent", "--synthetic", "describe the room"])
    assert _camera_source(args) == "synthetic"


# --- REAL EYES + FAKE WHEELS: camera source & one shared camera --------------
def test_agent_default_constructs_real_webcam_camera():
    """By default (no --synthetic) the backend owns a REAL-webcam-backed Camera.

    The webcam Camera only opens the device on .start(); constructing it merely
    records the source, so this asserts the wiring with no hardware.
    """
    from yalp.deliberative.agent_cli import _make_backend

    backend = _make_backend(synthetic=False)
    cam = backend.camera()
    assert cam.source == "webcam"  # real eyes by default (auto-falls-back)


def test_agent_synthetic_forces_synthetic_camera():
    from yalp.deliberative.agent_cli import _make_backend

    backend = _make_backend(synthetic=True)
    assert backend.camera().source == "synthetic"


def test_backend_uses_monkeypatched_camera_factory(monkeypatch):
    """The backend builds its Camera from the selected source via the factory."""
    import yalp.reactive.fake_backend as fb

    built = []

    class FakeCamera:
        def __init__(self, source="webcam", **kwargs):
            self.source = source
            built.append(self)

    monkeypatch.setattr(fb, "Camera", FakeCamera)

    fb.FakeReactiveBackend()  # default
    fb.FakeReactiveBackend(camera_source="synthetic")
    assert [c.source for c in built] == ["webcam", "synthetic"]


# --- backend owns ONE camera; default is the real webcam ---------------------
class _FakeCamera:
    """A no-hardware stand-in that records its requested source."""

    instances = []

    def __init__(self, source="webcam", **kwargs):
        self.source = source
        self.kwargs = kwargs
        self.started = False
        _FakeCamera.instances.append(self)

    def start(self):
        self.started = True
        return self

    def stop(self):
        self.started = False

    def latest(self):
        return "FRAME"

    def wait_for_frame(self, timeout=5.0):
        return "FRAME"


def test_backend_defaults_to_real_webcam_camera(monkeypatch):
    """By default the backend builds a real-webcam-backed Camera (one instance)."""
    monkeypatch.setattr("yalp.reactive.fake_backend.Camera", _FakeCamera)
    _FakeCamera.instances = []

    backend = FakeReactiveBackend(tick_hz=50.0)

    assert len(_FakeCamera.instances) == 1, "exactly one Camera per run"
    assert backend.camera().source == "webcam"


def test_backend_camera_source_synthetic_is_honored(monkeypatch):
    monkeypatch.setattr("yalp.reactive.fake_backend.Camera", _FakeCamera)
    _FakeCamera.instances = []

    backend = FakeReactiveBackend(tick_hz=50.0, camera_source="synthetic")

    assert len(_FakeCamera.instances) == 1
    assert backend.camera().source == "synthetic"


def test_describe_scene_reads_from_backend_shared_camera(monkeypatch):
    """describe_scene pulls from the backend's ONE shared camera (not a fresh one)."""
    from yalp.deliberative import agent_cli, vision

    shared_camera = object()
    backend = SimpleNamespace(camera=lambda: shared_camera)

    captured = {}

    def fake_describe_scene(frame=None, question=None, *, model=None, camera=None, **kwargs):
        captured["frame"] = frame
        captured["camera"] = camera
        return "ok"

    monkeypatch.setattr(vision, "describe_scene", fake_describe_scene)

    describe = agent_cli._make_describe(backend)
    out = describe(detail="quick")

    assert out == "ok"
    # It read the latest frame FROM the backend's shared camera, not by opening
    # a new/synthetic one (frame is None => pull latest from the passed camera).
    assert captured["camera"] is shared_camera
    assert captured["frame"] is None


def test_describe_scene_reads_from_shared_backend_camera(monkeypatch):
    """describe_scene must read the latest frame from the backend's ONE camera."""
    from yalp.deliberative import agent_cli, vision

    monkeypatch.setattr("yalp.reactive.fake_backend.Camera", _FakeCamera)
    _FakeCamera.instances = []

    backend = FakeReactiveBackend(tick_hz=50.0)  # default real webcam (faked)
    assert len(_FakeCamera.instances) == 1

    captured = {}

    def fake_describe_scene(frame=None, question=None, *, model=None,
                            camera=None, **kwargs):
        captured["frame"] = frame
        captured["camera"] = camera
        return "ok"

    monkeypatch.setattr(vision, "describe_scene", fake_describe_scene)

    describe = agent_cli._make_describe(backend)
    result = describe()

    assert result == "ok"
    # No second camera was opened; the same shared instance is used.
    assert len(_FakeCamera.instances) == 1
    assert captured["camera"] is backend.camera()
    # The frame is pulled from that camera (frame=None => grab from camera).
    assert captured["frame"] is None


# --- build_context carries the honest open-loop caveats ----------------------
def test_build_context_states_open_loop_caveats():
    from yalp.contract.messages import RobotState

    ctx = build_context("go to the kitchen", RobotState(distance_known=False))
    assert "no encoders" in ctx.lower() or "timed" in ctx.lower()
    assert "blocked" in ctx.lower()
    assert "reverse" in ctx.lower()
    assert "unknown" in ctx.lower()  # distance UNKNOWN surfaced
