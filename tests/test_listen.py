"""Continuous listening: segmentation, wake words, stop handling, agent cancel.

No microphone, speech model, or robot: audio is synthetic and transcription is a
fake that returns scripted text.
"""

from __future__ import annotations

import argparse
import threading
import time

import numpy as np
import pytest

from yalp.voice import listen

RATE = 16000


def _noise(seconds: float, level: float, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return (rng.standard_normal(int(seconds * RATE)) * level).astype(np.float32)


def _segments(audio: np.ndarray, **kwargs) -> list[np.ndarray]:
    seg = listen.UtteranceSegmenter(RATE, **kwargs)
    out = []
    for block in listen.array_blocks(audio, RATE):
        utterance = seg.feed(block)
        if utterance is not None:
            out.append(utterance)
    return out


# --------------------------------------------------------------------------- #
# Text helpers
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "text,expected",
    [
        ("stop", True),
        ("Please STOP now!", True),
        ("Halt.", True),
        ("non-stop music", True),
        ("unstoppable", False),
        ("go to the top", False),
        ("", False),
    ],
)
def test_is_stop(text, expected):
    assert listen.is_stop(text) is expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Robot, turn left.", "turn left"),
        ("hey robot what do you see?", "what do you see"),
        ("Turn left, robot.", "Turn left"),
        ("Robot.", None),
        ("turn left", None),
        ("the robotics lab", None),
        ("", None),
    ],
)
def test_extract_command_with_wake_word(text, expected):
    assert listen.extract_command(text, ("robot",)) == expected


def test_extract_command_without_wake_words_takes_everything():
    assert listen.extract_command(" turn left ", ()) == "turn left"


def test_noise_transcripts_are_recognised():
    assert listen.is_noise_transcript("")
    assert listen.is_noise_transcript(" Thank you. ")
    assert listen.is_noise_transcript("[BLANK_AUDIO]")
    assert not listen.is_noise_transcript("robot turn left")


# --------------------------------------------------------------------------- #
# Segmenter
# --------------------------------------------------------------------------- #
def test_segmenter_ignores_steady_background():
    assert _segments(_noise(10.0, 0.01)) == []


def test_segmenter_cuts_one_utterance_per_burst():
    audio = np.concatenate(
        [
            _noise(2.0, 0.003),
            _noise(1.5, 0.05, 1),
            _noise(2.0, 0.003, 2),
            _noise(1.0, 0.05, 3),
            _noise(2.0, 0.003, 4),
        ]
    )
    utterances = _segments(audio)
    assert len(utterances) == 2
    # Each holds its burst plus lead-in and trailing pause, not the whole stream.
    assert 1.5 <= utterances[0].size / RATE <= 3.0
    assert 1.0 <= utterances[1].size / RATE <= 2.5


def test_segmenter_drops_a_click():
    audio = np.concatenate([_noise(2.0, 0.003), _noise(0.2, 0.2, 1), _noise(2.0, 0.003, 2)])
    assert _segments(audio) == []


def test_segmenter_adapts_to_a_new_constant_noise():
    # Quiet, then loud forever (motors/fan): at most one forced cut, then silence.
    audio = np.concatenate([_noise(2.0, 0.003), _noise(40.0, 0.05, 1)])
    assert len(_segments(audio, max_utterance_s=5.0)) <= 1


def test_stop_window_emits_overlapping_windows():
    win = listen.StopWindow(RATE, window_s=2.0, hop_s=1.0)
    out = [w for b in listen.array_blocks(_noise(5.0, 0.01), RATE) if (w := win.feed(b)) is not None]
    assert len(out) == 5
    assert out[0].size == RATE and out[-1].size == 2 * RATE


# --------------------------------------------------------------------------- #
# VoiceLoop
# --------------------------------------------------------------------------- #
def _speech_stream(n_utterances: int) -> np.ndarray:
    parts = [_noise(1.5, 0.003)]
    for i in range(n_utterances):
        parts += [_noise(1.2, 0.05, 10 + i), _noise(1.5, 0.003, 20 + i)]
    return np.concatenate(parts)


class _Script:
    """A fake transcriber that returns the next scripted line per call."""

    def __init__(self, lines):
        self.lines = list(lines)
        self.calls = 0

    def __call__(self, _samples) -> str:
        self.calls += 1
        return self.lines.pop(0) if self.lines else ""


def test_loop_runs_wake_word_commands_and_ignores_the_rest():
    commands, heard = [], []
    loop = listen.VoiceLoop(
        transcribe=_Script(["Robot, turn left.", "what's for dinner", "Thank you."]),
        on_command=commands.append,
        on_stop=lambda: pytest.fail("no stop expected"),
        on_heard=lambda text, ok: heard.append((text, ok)),
        wake_words=("robot",),
        sample_rate=RATE,
    )
    loop.run(listen.array_blocks(_speech_stream(3), RATE))
    assert commands == ["turn left"]
    assert heard == [("turn left", True), ("what's for dinner", False)]


def test_loop_stop_needs_no_wake_word_when_still():
    stops = []
    loop = listen.VoiceLoop(
        transcribe=_Script(["Stop!"]),
        on_command=lambda _t: pytest.fail("stop is not a command"),
        on_stop=lambda: stops.append(1),
        wake_words=("robot",),
        sample_rate=RATE,
    )
    loop.run(listen.array_blocks(_speech_stream(1), RATE))
    assert stops == [1]


def test_loop_only_listens_for_stop_while_moving():
    commands, stops = [], []
    script = _Script(["mumble garble", "robot go forward", "I said stop"])
    loop = listen.VoiceLoop(
        transcribe=script,
        on_command=commands.append,
        on_stop=lambda: stops.append(1),
        is_moving=lambda: True,
        wake_words=("robot",),
        sample_rate=RATE,
        moving_poll_s=0.0,
    )
    loop.run(listen.array_blocks(_noise(3.0, 0.05), RATE))
    assert commands == []  # a full sentence heard over the motors is never run
    assert stops == [1]
    assert script.calls == 3  # one window per second


def test_loop_stops_a_running_command():
    release = threading.Event()
    started = threading.Event()
    stops = []

    def on_command(_text):
        started.set()
        release.wait(5.0)

    def on_stop():
        stops.append(1)
        release.set()

    script = _Script(["robot drive forward"])
    loop = listen.VoiceLoop(
        transcribe=script,
        on_command=on_command,
        on_stop=on_stop,
        wake_words=("robot",),
        sample_rate=RATE,
    )

    def blocks():
        yield from listen.array_blocks(_speech_stream(1), RATE)
        assert started.wait(2.0)
        script.lines = ["garble", "stop"]
        yield from listen.array_blocks(_noise(2.0, 0.05), RATE)

    loop.run(blocks())
    assert stops == [1]
    assert not loop.busy


def test_loop_survives_transcriber_and_command_errors():
    def boom(_samples):
        raise RuntimeError("model fell over")

    loop = listen.VoiceLoop(
        transcribe=boom, on_command=lambda _t: None, on_stop=lambda: None, sample_rate=RATE
    )
    loop.run(listen.array_blocks(_speech_stream(2), RATE))  # must not raise


def test_loop_skips_stale_windows_when_behind():
    script = _Script(["stop"])
    stops = []
    loop = listen.VoiceLoop(
        transcribe=script,
        on_command=lambda _t: None,
        on_stop=lambda: stops.append(1),
        is_moving=lambda: True,
        sample_rate=RATE,
        moving_poll_s=0.0,
    )
    loop.run(listen.array_blocks(_noise(3.0, 0.05), RATE), backlog=lambda: 20)
    assert script.calls == 0 and stops == []


# --------------------------------------------------------------------------- #
# Agent.cancel
# --------------------------------------------------------------------------- #
def test_agent_cancel_sends_idle_and_blocks_further_motion():
    from yalp.contract.messages import Intent, Mode, RobotState
    from yalp.deliberative.agent import Agent

    class Reactive:
        def __init__(self):
            self.sent = []

        def send_intent(self, intent):
            self.sent.append(intent)

        def request_state(self, timeout=2.0):
            return RobotState()

    reactive = Reactive()
    agent = Agent(client=None, reactive=reactive, describe_scene=lambda **_k: "")
    agent._seq = 7
    agent.cancel()
    assert agent.cancelled
    assert [i.mode for i in reactive.sent] == [Mode.IDLE]
    assert reactive.sent[0].seq > 7  # must win the reactive mailbox

    start = time.monotonic()
    agent._send_and_settle(Intent(Mode.DRIVE_GOAL, {"distance_m": 1.0}, 99))
    assert time.monotonic() - start < 1.0
    assert [i.mode for i in reactive.sent] == [Mode.IDLE]  # the drive was not sent


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def test_listen_cli_dry_run_on_a_wav(tmp_path, capsys, monkeypatch):
    from yalp import listen_cli
    from yalp.voice.microphone import to_wav_bytes

    wav = tmp_path / "clip.wav"
    wav.write_bytes(to_wav_bytes(_speech_stream(1), RATE))
    monkeypatch.setattr(listen_cli, "_make_transcriber", lambda _rate: lambda _s: "Robot, spin.")

    root = argparse.ArgumentParser()
    listen_cli.add_parser(root.add_subparsers(dest="subcommand"))
    args = root.parse_args(["listen", "--dry-run", "--file", str(wav)])
    assert args.handler(args) == 0
    assert "[heard] spin" in capsys.readouterr().out


def test_listen_cli_reports_a_missing_file(capsys):
    from yalp import listen_cli

    args = argparse.Namespace(file="/nonexistent/clip.wav", dry_run=True)
    assert listen_cli.run(args) == 2
    assert "Could not open the audio source" in capsys.readouterr().out


def test_idle_is_not_moving_even_when_goal_status_says_running():
    # Seen on the robot: after a stop the state is mode=IDLE, goal_status=running.
    # Treating that as "moving" left the listener deaf to commands for good.
    from yalp import listen_cli
    from yalp.contract.messages import GoalStatus, Mode, RobotState

    assert not listen_cli._state_is_moving(RobotState(mode=Mode.IDLE, goal_status=GoalStatus.RUNNING))
    assert not listen_cli._state_is_moving(RobotState(mode=Mode.SAFE_STOP))
    assert not listen_cli._state_is_moving(None)
    assert listen_cli._state_is_moving(RobotState(mode=Mode.DRIVE_GOAL))
    assert listen_cli._state_is_moving(RobotState(mode=Mode.FOLLOW))


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Turn left.", ("turn", {"angle_deg": 90.0})),
        ("turn right", ("turn", {"angle_deg": -90.0})),
        ("Spin right 45 degrees", ("turn", {"angle_deg": -45.0})),
        ("turn around", ("turn", {"angle_deg": 180.0})),
        ("Go forward.", ("drive", {"distance_m": 0.5})),
        ("drive forward 1 meter", ("drive", {"distance_m": 1.0})),
        ("move back 30 cm", ("drive", {"distance_m": -0.3})),
        ("back up", ("drive", {"distance_m": -0.5})),
        ("go forward 50 meters", ("drive", {"distance_m": 2.0})),  # capped
        ("Follow me.", ("enter_follow_mode", {})),
    ],
)
def test_parse_simple_command(text, expected):
    name, params, _description = listen.parse_simple_command(text)
    assert name == expected[0]
    assert params == pytest.approx(expected[1])


@pytest.mark.parametrize(
    "text",
    ["what do you see", "turn left and tell me what you see", "turn rain", "go", "", "go to the kitchen"],
)
def test_parse_simple_command_leaves_the_rest_to_the_agent(text):
    assert listen.parse_simple_command(text) is None


def test_agent_run_direct_sends_one_intent_without_a_model():
    from yalp.contract.messages import GoalStatus, Mode, RobotState
    from yalp.deliberative.agent import Agent

    class Reactive:
        def __init__(self):
            self.sent = []

        def send_intent(self, intent):
            self.sent.append(intent)

        def request_state(self, timeout=2.0):
            done = bool(self.sent)
            return RobotState(
                mode=Mode.IDLE, goal_status=GoalStatus.COMPLETED if done else GoalStatus.NONE
            )

    reactive = Reactive()
    agent = Agent(client=None, reactive=reactive, describe_scene=lambda **_k: "")
    entries = agent.run_direct("turn", {"angle_deg": -90.0})
    assert len(reactive.sent) == 1
    assert reactive.sent[0].goal == {"kind": "rotate", "target": -90.0, "speed": 0.5}
    assert "completed" in entries[-1].text


def test_wake_prompt_names_each_wake_word():
    assert listen.wake_prompt(()) is None
    prompt = listen.wake_prompt(("izzy",))
    assert prompt.startswith("Izzy, ")
    assert "Robot" in listen.wake_prompt(("izzy", "robot"))


def test_default_wake_word_is_the_robots_name():
    from yalp import config

    assert config.WAKE_WORDS == ("izzy",)
    assert listen.extract_command("Hey Izzy, turn around.", config.WAKE_WORDS) == "turn around"
