"""Tests for voice-input / STT configuration in yalp.config.

Verifies that the new voice constants are importable from yalp.config with no
audio or STT libraries installed (stdlib-only), and that Config() defaults
match the module-level constants.
"""

from yalp import config


def test_voice_constants_importable():
    """All new voice/STT module-level constants must be importable."""
    assert hasattr(config, "VOICE_SOURCE")
    assert hasattr(config, "VOICE_SAMPLE_RATE")
    assert hasattr(config, "VOICE_CHANNELS")
    assert hasattr(config, "VOICE_RECORD_SECONDS")
    assert hasattr(config, "VOICE_AUDIO_FILE")
    assert hasattr(config, "STT_BACKEND")
    assert hasattr(config, "STT_MODEL")


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


def test_voice_constants_in_all():
    """New constants must be listed in __all__."""
    for name in ("VOICE_SOURCE", "VOICE_SAMPLE_RATE", "VOICE_CHANNELS",
                 "VOICE_RECORD_SECONDS", "VOICE_AUDIO_FILE", "STT_BACKEND", "STT_MODEL"):
        assert name in config.__all__, f"{name} missing from __all__"
