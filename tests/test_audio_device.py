"""Tests for input-device selection: config plumbing + Microphone resolution.

No real audio hardware, PortAudio, or ``sounddevice`` is touched. A tiny FAKE
``sounddevice`` module (a ``types.ModuleType`` exposing ``query_devices`` and
``default.device``) is injected where a device query is needed — either passed
directly to the pure resolver ``_resolve_input_device`` or monkeypatched onto the
microphone module's ``sd`` binding for the integration paths.
"""

from __future__ import annotations

import importlib
import types

import pytest

from yalp import config
from yalp.voice import microphone as mic_mod
from yalp.voice.microphone import (
    Microphone,
    _resolve_input_device,
    list_input_devices,
)


# ---------------------------------------------------------------------------
# A fake sounddevice
# ---------------------------------------------------------------------------

# A representative device layout: the laptop default mic, an output-only device
# (must never match an input selection), and the Logitech C270 webcam mic.
_DEVICES = [
    {"name": "MacBook Pro Microphone", "max_input_channels": 1, "max_output_channels": 0},
    {"name": "Built-in Output (Speakers)", "max_input_channels": 0, "max_output_channels": 2},
    {"name": "USB Audio Device (C270)", "max_input_channels": 1, "max_output_channels": 0},
]


def make_fake_sd(devices=_DEVICES, default_input=0):
    """Build a fake ``sounddevice`` module exposing the bits we use."""
    mod = types.ModuleType("sounddevice")
    mod.query_devices = lambda: [dict(d) for d in devices]
    mod.default = types.SimpleNamespace(device=[default_input, 1])
    return mod


# ---------------------------------------------------------------------------
# config: YALP_AUDIO_INPUT_DEVICE plumbing
# ---------------------------------------------------------------------------

def test_audio_input_device_default_is_empty():
    assert config.AUDIO_INPUT_DEVICE == ""
    assert isinstance(config.AUDIO_INPUT_DEVICE, str)


def test_audio_input_device_in_all():
    assert "AUDIO_INPUT_DEVICE" in config.__all__


def test_config_dataclass_has_audio_input_device():
    assert config.Config().audio_input_device == config.AUDIO_INPUT_DEVICE


@pytest.fixture
def reload_config(monkeypatch):
    """Reload ``yalp.config`` under a patched env; restore on teardown."""
    import yalp.config as cfg

    def _reload(**env):
        for name, value in env.items():
            if value is None:
                monkeypatch.delenv(name, raising=False)
            else:
                monkeypatch.setenv(name, value)
        return importlib.reload(cfg)

    yield _reload
    monkeypatch.undo()
    importlib.reload(cfg)


def test_env_override_plumbs_through(reload_config):
    cfg = reload_config(YALP_AUDIO_INPUT_DEVICE="C270")
    assert cfg.AUDIO_INPUT_DEVICE == "C270"
    assert cfg.Config().audio_input_device == "C270"


def test_microphone_default_device_comes_from_config():
    # A freshly-constructed Microphone inherits the configured selection.
    assert Microphone(source="synthetic").device == config.AUDIO_INPUT_DEVICE


# ---------------------------------------------------------------------------
# _resolve_input_device: the pure resolver
# ---------------------------------------------------------------------------

def test_resolve_empty_and_none_return_system_default():
    fake = make_fake_sd()
    assert _resolve_input_device("", fake) is None
    assert _resolve_input_device("   ", fake) is None
    assert _resolve_input_device(None, fake) is None


def test_resolve_by_integer_string():
    fake = make_fake_sd()
    assert _resolve_input_device("2", fake) == 2
    assert _resolve_input_device(" 0 ", fake) == 0


def test_resolve_by_int():
    fake = make_fake_sd()
    assert _resolve_input_device(0, fake) == 0
    assert _resolve_input_device(2, fake) == 2


def test_resolve_by_substring_case_insensitive():
    fake = make_fake_sd()
    # 'c270' (lowercase) matches "USB Audio Device (C270)" at index 2.
    assert _resolve_input_device("c270", fake) == 2
    # A broader substring still lands on the first input-capable match.
    assert _resolve_input_device("microphone", fake) == 0


def test_resolve_substring_skips_output_only_devices():
    fake = make_fake_sd()
    # "Speakers" only appears on the output-only device (index 1) -> no match.
    with pytest.raises(ValueError):
        _resolve_input_device("Speakers", fake)


def test_resolve_index_out_of_range_raises_with_listing():
    fake = make_fake_sd()
    with pytest.raises(ValueError) as excinfo:
        _resolve_input_device("99", fake)
    msg = str(excinfo.value)
    assert "99" in msg
    # Actionable: names the available input devices and the discovery command.
    assert "MacBook Pro Microphone" in msg
    assert "USB Audio Device (C270)" in msg
    assert "yalp audio --list" in msg
    # Output-only devices are NOT listed as input options.
    assert "Speakers" not in msg


def test_resolve_index_pointing_at_output_only_raises():
    fake = make_fake_sd()
    # Index 1 exists but is output-only -> not a valid input device.
    with pytest.raises(ValueError) as excinfo:
        _resolve_input_device("1", fake)
    assert "1" in str(excinfo.value)


def test_resolve_no_substring_match_raises_with_listing():
    fake = make_fake_sd()
    with pytest.raises(ValueError) as excinfo:
        _resolve_input_device("nonexistent-mic", fake)
    msg = str(excinfo.value)
    assert "nonexistent-mic" in msg
    assert "MacBook Pro Microphone" in msg
    assert "yalp audio --list" in msg


def test_resolve_no_match_message_when_no_inputs_available():
    fake = make_fake_sd(devices=[
        {"name": "Speakers", "max_input_channels": 0, "max_output_channels": 2},
    ])
    with pytest.raises(ValueError) as excinfo:
        _resolve_input_device("mic", fake)
    assert "No audio input devices are available" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Microphone integration: resolution happens lazily at capture start
# ---------------------------------------------------------------------------

def test_configure_source_resolves_substring(monkeypatch):
    monkeypatch.setattr(mic_mod, "sd", make_fake_sd())
    mic = Microphone(source="microphone", device="C270", sample_rate=8000,
                     record_seconds=0.1)
    mic._configure_source()
    assert mic.source == "microphone"  # a real match must NOT fall back
    assert mic._resolved_device == 2


def test_configure_source_resolves_index(monkeypatch):
    monkeypatch.setattr(mic_mod, "sd", make_fake_sd())
    mic = Microphone(source="microphone", device="0", sample_rate=8000,
                     record_seconds=0.1)
    mic._configure_source()
    assert mic.source == "microphone"
    assert mic._resolved_device == 0


def test_configure_source_default_keeps_system_default(monkeypatch):
    monkeypatch.setattr(mic_mod, "sd", make_fake_sd())
    mic = Microphone(source="microphone", device="", sample_rate=8000,
                     record_seconds=0.1)
    mic._configure_source()
    assert mic.source == "microphone"
    assert mic._resolved_device is None  # None => sounddevice picks the default


def test_configure_source_unmatched_device_raises(monkeypatch):
    # A specified-but-missing device is a MISCONFIG: raise, never silently
    # degrade to synthetic.
    monkeypatch.setattr(mic_mod, "sd", make_fake_sd())
    mic = Microphone(source="microphone", device="ghost-mic", sample_rate=8000,
                     record_seconds=0.1)
    with pytest.raises(ValueError) as excinfo:
        mic._configure_source()
    assert "ghost-mic" in str(excinfo.value)
    assert mic.source == "microphone"  # unchanged — we raised, not degraded


def test_start_propagates_unmatched_device_error(monkeypatch):
    monkeypatch.setattr(mic_mod, "sd", make_fake_sd())
    mic = Microphone(source="microphone", device="ghost-mic", sample_rate=8000,
                     record_seconds=0.1)
    try:
        with pytest.raises(ValueError):
            mic.start()
    finally:
        mic.stop()


def test_default_device_no_inputs_falls_back_to_synthetic(monkeypatch):
    # Empty selection + a layout with no input devices -> synthetic fallback so
    # dev/CI never require hardware (existing behavior preserved).
    fake = make_fake_sd(devices=[
        {"name": "Speakers", "max_input_channels": 0, "max_output_channels": 2},
    ])
    monkeypatch.setattr(mic_mod, "sd", fake)
    mic = Microphone(source="microphone", device="", sample_rate=8000,
                     record_seconds=0.1)
    mic._configure_source()
    assert mic.source == "synthetic"


# ---------------------------------------------------------------------------
# list_input_devices: the CLI's data view
# ---------------------------------------------------------------------------

def test_list_input_devices_filters_and_flags_default():
    fake = make_fake_sd(default_input=2)
    listed = list_input_devices(fake)
    # Only the two input-capable devices appear (the output-only one is dropped).
    assert [d["index"] for d in listed] == [0, 2]
    assert [d["name"] for d in listed] == [
        "MacBook Pro Microphone",
        "USB Audio Device (C270)",
    ]
    assert [d["max_input_channels"] for d in listed] == [1, 1]
    # The default marker follows sd.default.device[0].
    by_index = {d["index"]: d for d in listed}
    assert by_index[2]["is_default"] is True
    assert by_index[0]["is_default"] is False


def test_list_input_devices_handles_missing_default():
    mod = types.ModuleType("sounddevice")
    mod.query_devices = lambda: [dict(d) for d in _DEVICES]
    # No ``default`` attribute at all -> nothing is flagged, no crash.
    listed = list_input_devices(mod)
    assert all(d["is_default"] is False for d in listed)
