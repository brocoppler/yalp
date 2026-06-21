"""Tests for voice-input / STT configuration in yalp.config.

Verifies that the new voice/STT constants and Config fields are present, have
the expected defaults and types, and are listed in ``__all__``. This module
must import cleanly with no audio or STT libraries installed (yalp.config is
stdlib-only by design).
"""

from __future__ import annotations

import importlib  # noqa: F401 — kept for parity / future reload tests

from yalp import config


def test_import_no_audio_libs():
    """yalp.config must be importable without any audio/STT dependency."""
    import yalp.config  # noqa: F401 — import-only test


def test_voice_constants_importable():
    """All new voice/STT module-level constants must be importable."""
    assert hasattr(config, "VOICE_SOURCE")
    assert hasattr(config, "VOICE_SAMPLE_RATE")
    assert hasattr(config, "VOICE_CHANNELS")
    assert hasattr(config, "VOICE_RECORD_SECONDS")
    assert hasattr(config, "VOICE_AUDIO_FILE")
    assert hasattr(config, "STT_BACKEND")
    assert hasattr(config, "STT_MODEL")


def test_module_constants_importable():
    """All new voice constants must be importable from yalp.config."""
    from yalp.config import (
        VOICE_SOURCE,
        VOICE_SAMPLE_RATE,
        VOICE_CHANNELS,
        VOICE_RECORD_SECONDS,
        VOICE_AUDIO_FILE,
        STT_BACKEND,
        STT_MODEL,
    )
    # Just importing without error is the primary assertion; spot-check types.
    assert isinstance(VOICE_SOURCE, str)
    assert isinstance(VOICE_SAMPLE_RATE, int)
    assert isinstance(VOICE_CHANNELS, int)
    assert isinstance(VOICE_RECORD_SECONDS, float)
    assert isinstance(VOICE_AUDIO_FILE, str)
    assert isinstance(STT_BACKEND, str)
    assert isinstance(STT_MODEL, str)


def test_voice_constant_defaults():
    assert config.VOICE_SOURCE == "microphone"
    assert config.VOICE_SAMPLE_RATE == 16000
    assert config.VOICE_CHANNELS == 1
    assert config.VOICE_RECORD_SECONDS == 5.0
    assert config.VOICE_AUDIO_FILE == ""
    assert config.STT_BACKEND == "faster-whisper"
    assert config.STT_MODEL == "tiny"


def test_voice_constant_types():
    assert isinstance(config.VOICE_SOURCE, str)
    assert isinstance(config.VOICE_SAMPLE_RATE, int)
    assert isinstance(config.VOICE_CHANNELS, int)
    assert isinstance(config.VOICE_RECORD_SECONDS, float)
    assert isinstance(config.VOICE_AUDIO_FILE, str)
    assert isinstance(config.STT_BACKEND, str)
    assert isinstance(config.STT_MODEL, str)


def test_config_dataclass_voice_defaults():
    cfg = config.Config()
    assert cfg.voice_sample_rate == 16000
    assert cfg.stt_backend == "faster-whisper"
    assert cfg.voice_source == config.VOICE_SOURCE
    assert cfg.voice_channels == config.VOICE_CHANNELS
    assert cfg.voice_record_seconds == config.VOICE_RECORD_SECONDS
    assert cfg.voice_audio_file == config.VOICE_AUDIO_FILE
    assert cfg.stt_model == config.STT_MODEL


def test_config_voice_sample_rate_default():
    from yalp.config import Config
    assert Config().voice_sample_rate == 16000


def test_config_stt_backend_default():
    from yalp.config import Config
    assert Config().stt_backend == "faster-whisper"


def test_config_voice_source_default():
    from yalp.config import Config
    assert Config().voice_source == "microphone"


def test_config_voice_channels_default():
    from yalp.config import Config
    assert Config().voice_channels == 1


def test_config_voice_record_seconds_default():
    from yalp.config import Config
    assert Config().voice_record_seconds == 5.0


def test_config_voice_audio_file_default():
    from yalp.config import Config
    assert Config().voice_audio_file == ""


def test_config_stt_model_default():
    from yalp.config import Config
    assert Config().stt_model == "tiny"


def test_voice_constants_in_all():
    """New constants must be listed in __all__."""
    for name in ("VOICE_SOURCE", "VOICE_SAMPLE_RATE", "VOICE_CHANNELS",
                 "VOICE_RECORD_SECONDS", "VOICE_AUDIO_FILE", "STT_BACKEND", "STT_MODEL"):
        assert name in config.__all__, f"{name} missing from __all__"


def test_constants_in_all():
    """All new constants must appear in yalp.config.__all__."""
    import yalp.config as cfg
    for name in (
        "VOICE_SOURCE",
        "VOICE_SAMPLE_RATE",
        "VOICE_CHANNELS",
        "VOICE_RECORD_SECONDS",
        "VOICE_AUDIO_FILE",
        "STT_BACKEND",
        "STT_MODEL",
    ):
        assert name in cfg.__all__, f"{name} missing from yalp.config.__all__"
