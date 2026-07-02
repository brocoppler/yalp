"""Tests for the ``yalp audio`` CLI (audio input-device inspection).

No real audio hardware or PortAudio: a FAKE ``sounddevice`` module is injected
into ``sys.modules`` so the lazily-imported ``import sounddevice`` inside the
handler resolves to it. The missing-extra path is forced by mapping
``sys.modules['sounddevice'] = None`` (which makes ``import sounddevice`` raise),
so the friendly install hint is exercised without uninstalling anything.
"""

from __future__ import annotations

import sys
import types
from io import StringIO
from unittest.mock import patch

import pytest

from yalp import config


_DEVICES = [
    {"name": "MacBook Pro Microphone", "max_input_channels": 1, "max_output_channels": 0},
    {"name": "Built-in Output (Speakers)", "max_input_channels": 0, "max_output_channels": 2},
    {"name": "USB Audio Device (C270)", "max_input_channels": 1, "max_output_channels": 0},
]


def make_fake_sd(devices=_DEVICES, default_input=0):
    mod = types.ModuleType("sounddevice")
    mod.query_devices = lambda: [dict(d) for d in devices]
    mod.default = types.SimpleNamespace(device=[default_input, 1])
    return mod


def _run_audio(argv, *, fake_sd="__default__"):
    """Run ``yalp <argv>`` capturing stdout; returns (exit_code, stdout).

    ``fake_sd`` is injected as ``sys.modules['sounddevice']`` for the call:
    a module (the fake) for the happy path, or ``None`` to force the
    missing-extra branch. Pass ``fake_sd=...`` explicitly to control it.
    """
    from yalp.cli import build_parser

    buf = StringIO()
    saved = sys.modules.get("sounddevice", "__absent__")
    if fake_sd == "__default__":
        fake_sd = make_fake_sd()
    sys.modules["sounddevice"] = fake_sd
    try:
        with patch("sys.stdout", buf):
            parser = build_parser()
            args = parser.parse_args(argv)
            handler = getattr(args, "handler", None)
            assert handler is not None, "no handler registered for 'audio'"
            rc = handler(args)
    finally:
        if saved == "__absent__":
            sys.modules.pop("sounddevice", None)
        else:
            sys.modules["sounddevice"] = saved
    return int(rc), buf.getvalue()


# ---------------------------------------------------------------------------
# Registration / import hygiene
# ---------------------------------------------------------------------------

def test_audio_subcommand_registered():
    from yalp.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(["audio", "--list"])
    assert getattr(args, "handler", None) is not None


def test_module_keeps_sounddevice_import_lazy():
    """The CLI module must not bind sounddevice at import (lazy in the handler).

    (The ``yalp.voice`` package's ``__init__`` transitively imports the
    microphone module, which guards its own optional sounddevice import, so we
    assert on *this* module's namespace rather than on ``sys.modules``.)
    """
    import yalp.voice.audio_cli as audio_cli  # must import without raising

    assert not hasattr(audio_cli, "sd")
    assert not hasattr(audio_cli, "sounddevice")
    assert callable(audio_cli.add_parser)
    assert callable(audio_cli.run)


# ---------------------------------------------------------------------------
# --list happy path
# ---------------------------------------------------------------------------

def test_list_prints_input_capable_devices():
    rc, out = _run_audio(["audio", "--list"], fake_sd=make_fake_sd())
    assert rc == 0
    assert "Input-capable audio devices:" in out
    assert "[0] MacBook Pro Microphone (1 in)" in out
    assert "[2] USB Audio Device (C270) (1 in)" in out


def test_list_omits_output_only_devices():
    rc, out = _run_audio(["audio", "--list"], fake_sd=make_fake_sd())
    assert rc == 0
    # The output-only device must never appear as an input option.
    assert "Speakers" not in out
    assert "[1]" not in out


def test_list_marks_default_device():
    rc, out = _run_audio(["audio", "--list"], fake_sd=make_fake_sd(default_input=2))
    assert rc == 0
    # The default marker sits on the C270 line (index 2), not the laptop mic.
    for line in out.splitlines():
        if line.strip().startswith("[2]"):
            assert "*default" in line
        if line.strip().startswith("[0]"):
            assert "*default" not in line


def test_bare_audio_defaults_to_listing():
    # `yalp audio` with no flags is still useful — it lists.
    rc, out = _run_audio(["audio"], fake_sd=make_fake_sd())
    assert rc == 0
    assert "Input-capable audio devices:" in out


# ---------------------------------------------------------------------------
# Current-selection reporting
# ---------------------------------------------------------------------------

def test_list_reports_system_default_selection(monkeypatch):
    monkeypatch.setattr(config, "AUDIO_INPUT_DEVICE", "")
    rc, out = _run_audio(["audio", "--list"], fake_sd=make_fake_sd())
    assert rc == 0
    assert "Current selection (YALP_AUDIO_INPUT_DEVICE):" in out
    assert "system default" in out


def test_list_reports_resolved_selection(monkeypatch):
    monkeypatch.setattr(config, "AUDIO_INPUT_DEVICE", "C270")
    rc, out = _run_audio(["audio", "--list"], fake_sd=make_fake_sd())
    assert rc == 0
    assert "'C270'" in out
    assert "resolves to [2] USB Audio Device (C270)" in out


def test_list_warns_on_unmatched_selection(monkeypatch):
    monkeypatch.setattr(config, "AUDIO_INPUT_DEVICE", "ghost-mic")
    rc, out = _run_audio(["audio", "--list"], fake_sd=make_fake_sd())
    assert rc == 0  # listing still succeeds; the selection is flagged
    assert "'ghost-mic'" in out
    assert "does not match any current input device" in out


# ---------------------------------------------------------------------------
# Missing 'voice' extra
# ---------------------------------------------------------------------------

def test_list_friendly_when_sounddevice_missing():
    rc, out = _run_audio(["audio", "--list"], fake_sd=None)
    assert rc == 1
    assert "pip install 'yalp[voice]'" in out
    # No traceback leaked to the user.
    assert "Traceback" not in out


def test_list_handles_query_error(monkeypatch):
    # sounddevice present but query_devices() blows up (PortAudio host error).
    broken = types.ModuleType("sounddevice")

    def _boom():
        raise RuntimeError("PortAudio host error")

    broken.query_devices = _boom
    broken.default = types.SimpleNamespace(device=[0, 1])
    rc, out = _run_audio(["audio", "--list"], fake_sd=broken)
    assert rc == 1
    assert "Could not query audio devices" in out
