"""Fail-soft env-override parsing in yalp.config.

``yalp.config`` is imported at startup by EVERY yalp process, including the
reactive safety loop. A malformed ``YALP_*`` override (e.g.
``YALP_BUDGET_MAX_CALLS=fast``) must DEGRADE to the built-in default with a
WARNING — it must NEVER raise during ``import yalp.config`` and prevent a
process (least of all the safety loop) from starting.

These tests cover three layers:
  1. The private ``_env_int`` / ``_env_float`` / ``_env_bool`` / ``_env_str``
     helpers directly (malformed -> default + WARNING; valid -> parsed; absent
     -> default silently).
  2. The module constants after an ``importlib.reload`` under a patched env
     (garbage -> canonical default; valid -> override applied).
  3. A hard guarantee, in a fresh subprocess, that ``import yalp.config`` never
     raises no matter how much ``YALP_*`` garbage is in the environment.
"""

from __future__ import annotations

import importlib
import logging
import os
import subprocess
import sys

import pytest

from yalp import config


# ---------------------------------------------------------------------------
# Layer 1: the private helpers, exercised directly (no reload needed — they
# read os.environ at call time).
# ---------------------------------------------------------------------------

class TestEnvInt:
    def test_absent_returns_default_silently(self, monkeypatch, caplog):
        monkeypatch.delenv("YALP_TEST_INT", raising=False)
        with caplog.at_level(logging.WARNING, logger="yalp.config"):
            assert config._env_int("YALP_TEST_INT", 33) == 33
        assert caplog.records == []

    def test_valid_is_parsed(self, monkeypatch):
        monkeypatch.setenv("YALP_TEST_INT", "7")
        assert config._env_int("YALP_TEST_INT", 33) == 7

    def test_malformed_returns_default_and_warns(self, monkeypatch, caplog):
        monkeypatch.setenv("YALP_TEST_INT", "fast")
        with caplog.at_level(logging.WARNING, logger="yalp.config"):
            assert config._env_int("YALP_TEST_INT", 33) == 33
        # Exactly one WARNING that names the variable, the bad value, and default.
        assert len(caplog.records) == 1
        assert caplog.records[0].levelno == logging.WARNING
        assert "YALP_TEST_INT" in caplog.text
        assert "fast" in caplog.text
        assert "33" in caplog.text

    def test_float_string_is_malformed_for_int(self, monkeypatch, caplog):
        # int("5.0") raises — must fall back, matching the old int() semantics.
        monkeypatch.setenv("YALP_TEST_INT", "5.0")
        with caplog.at_level(logging.WARNING, logger="yalp.config"):
            assert config._env_int("YALP_TEST_INT", 33) == 33
        assert "YALP_TEST_INT" in caplog.text


class TestEnvFloat:
    def test_absent_returns_default_silently(self, monkeypatch, caplog):
        monkeypatch.delenv("YALP_TEST_FLOAT", raising=False)
        with caplog.at_level(logging.WARNING, logger="yalp.config"):
            assert config._env_float("YALP_TEST_FLOAT", 0.5) == 0.5
        assert caplog.records == []

    def test_valid_is_parsed(self, monkeypatch):
        monkeypatch.setenv("YALP_TEST_FLOAT", "0.9")
        assert config._env_float("YALP_TEST_FLOAT", 0.5) == pytest.approx(0.9)

    def test_int_string_parses_as_float(self, monkeypatch):
        monkeypatch.setenv("YALP_TEST_FLOAT", "3")
        assert config._env_float("YALP_TEST_FLOAT", 0.5) == pytest.approx(3.0)

    def test_malformed_returns_default_and_warns(self, monkeypatch, caplog):
        monkeypatch.setenv("YALP_TEST_FLOAT", "high")
        with caplog.at_level(logging.WARNING, logger="yalp.config"):
            assert config._env_float("YALP_TEST_FLOAT", 0.5) == 0.5
        assert len(caplog.records) == 1
        assert caplog.records[0].levelno == logging.WARNING
        assert "YALP_TEST_FLOAT" in caplog.text
        assert "high" in caplog.text
        assert "0.5" in caplog.text


class TestEnvBool:
    @pytest.mark.parametrize("raw", ["1", "true", "TRUE", "Yes", " yes "])
    def test_truthy(self, monkeypatch, raw):
        monkeypatch.setenv("YALP_TEST_BOOL", raw)
        assert config._env_bool("YALP_TEST_BOOL", False) is True

    @pytest.mark.parametrize("raw", ["0", "false", "FALSE", "No", " no "])
    def test_falsy_is_silent(self, monkeypatch, caplog, raw):
        monkeypatch.setenv("YALP_TEST_BOOL", raw)
        with caplog.at_level(logging.WARNING, logger="yalp.config"):
            assert config._env_bool("YALP_TEST_BOOL", True) is False
        assert caplog.records == []

    def test_absent_returns_default_silently(self, monkeypatch, caplog):
        monkeypatch.delenv("YALP_TEST_BOOL", raising=False)
        with caplog.at_level(logging.WARNING, logger="yalp.config"):
            assert config._env_bool("YALP_TEST_BOOL", False) is False
            assert config._env_bool("YALP_TEST_BOOL", True) is True
        assert caplog.records == []

    def test_empty_returns_default_silently(self, monkeypatch, caplog):
        # Preserves the historical ``.get(name, "")`` behavior: empty == unset.
        monkeypatch.setenv("YALP_TEST_BOOL", "")
        with caplog.at_level(logging.WARNING, logger="yalp.config"):
            assert config._env_bool("YALP_TEST_BOOL", False) is False
        assert caplog.records == []

    def test_garbage_returns_default_and_warns(self, monkeypatch, caplog):
        monkeypatch.setenv("YALP_TEST_BOOL", "maybe")
        with caplog.at_level(logging.WARNING, logger="yalp.config"):
            assert config._env_bool("YALP_TEST_BOOL", False) is False
        assert len(caplog.records) == 1
        assert caplog.records[0].levelno == logging.WARNING
        assert "YALP_TEST_BOOL" in caplog.text
        assert "maybe" in caplog.text


class TestEnvStr:
    def test_absent_returns_default(self, monkeypatch):
        monkeypatch.delenv("YALP_TEST_STR", raising=False)
        assert config._env_str("YALP_TEST_STR", "default") == "default"

    def test_present_returns_value_verbatim(self, monkeypatch):
        monkeypatch.setenv("YALP_TEST_STR", "override")
        assert config._env_str("YALP_TEST_STR", "default") == "override"

    def test_empty_string_is_honored(self, monkeypatch):
        # Set-but-empty is a real (empty) value, distinct from unset.
        monkeypatch.setenv("YALP_TEST_STR", "")
        assert config._env_str("YALP_TEST_STR", "default") == ""

    def test_never_warns(self, monkeypatch, caplog):
        monkeypatch.setenv("YALP_TEST_STR", "anything at all !@#")
        with caplog.at_level(logging.WARNING, logger="yalp.config"):
            config._env_str("YALP_TEST_STR", "default")
        assert caplog.records == []


# ---------------------------------------------------------------------------
# Layer 2: module constants after importlib.reload under a patched env.
# ---------------------------------------------------------------------------

@pytest.fixture
def reload_config(monkeypatch):
    """Reload ``yalp.config`` with a patched environment.

    Yields a callable ``reload(**env)`` that sets/clears the given env vars and
    returns the freshly-reloaded module. On teardown the env patches are undone
    and the module is reloaded once more so its constants are rebuilt from the
    clean environment — leaving pristine module state for later tests.
    """
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


def test_reload_malformed_int_falls_back_and_warns(reload_config, caplog):
    with caplog.at_level(logging.WARNING, logger="yalp.config"):
        cfg = reload_config(YALP_BUDGET_MAX_CALLS="fast")
    assert cfg.BUDGET_MAX_CALLS == 40
    assert cfg.Config().budget_max_calls == 40  # frozen dataclass mirror intact
    assert "YALP_BUDGET_MAX_CALLS" in caplog.text
    assert "fast" in caplog.text


def test_reload_malformed_float_falls_back_and_warns(reload_config, caplog):
    with caplog.at_level(logging.WARNING, logger="yalp.config"):
        cfg = reload_config(YALP_DNN_CONFIDENCE="high")
    assert cfg.FOLLOW_DNN_CONFIDENCE == 0.5
    assert cfg.Config().follow_dnn_confidence == 0.5
    assert "YALP_DNN_CONFIDENCE" in caplog.text


def test_reload_malformed_bool_falls_back_and_warns(reload_config, caplog):
    with caplog.at_level(logging.WARNING, logger="yalp.config"):
        cfg = reload_config(YALP_MOTOR_LEFT_INVERT="perhaps")
    assert cfg.MOTOR_LEFT_INVERT is False
    assert cfg.Config().motor_left_invert is False
    assert "YALP_MOTOR_LEFT_INVERT" in caplog.text


def test_reload_valid_int_override_applies(reload_config, caplog):
    with caplog.at_level(logging.WARNING, logger="yalp.config"):
        cfg = reload_config(YALP_BUDGET_MAX_CALLS="7")
    assert cfg.BUDGET_MAX_CALLS == 7
    assert cfg.Config().budget_max_calls == 7
    assert caplog.records == []  # a valid value never warns


def test_reload_valid_bool_override_applies(reload_config):
    cfg = reload_config(YALP_MOTOR_RIGHT_INVERT="yes")
    assert cfg.MOTOR_RIGHT_INVERT is True
    assert cfg.Config().motor_right_invert is True


def test_reload_stby_none_spelling(reload_config):
    cfg = reload_config(YALP_MOTOR_STBY_PIN="none")
    assert cfg.MOTOR_STBY_PIN is None
    assert cfg.Config().motor_stby_pin is None


def test_reload_stby_valid_int(reload_config):
    cfg = reload_config(YALP_MOTOR_STBY_PIN="19")
    assert cfg.MOTOR_STBY_PIN == 19


def test_reload_stby_garbage_falls_back_and_warns(reload_config, caplog):
    with caplog.at_level(logging.WARNING, logger="yalp.config"):
        cfg = reload_config(YALP_MOTOR_STBY_PIN="pin7")
    assert cfg.MOTOR_STBY_PIN == 24
    assert "YALP_MOTOR_STBY_PIN" in caplog.text


def test_reload_record_seconds_stays_float(reload_config):
    # Absent default must remain a float (old behavior: float("5") == 5.0).
    cfg = reload_config(YALP_VOICE_RECORD_SECONDS=None)
    assert isinstance(cfg.VOICE_RECORD_SECONDS, float)
    assert cfg.VOICE_RECORD_SECONDS == 5.0


# ---------------------------------------------------------------------------
# Layer 3: the hard guarantee — import never raises, in a real subprocess, no
# matter how much YALP_* garbage is present. This is the safety-critical
# invariant (a bad env var must not kill the reactive safety process).
# ---------------------------------------------------------------------------

_GARBAGE_ENV = {
    "YALP_BUDGET_MAX_CALLS": "fast",
    "YALP_BUDGET_MAX_TOKENS": "lots",
    "YALP_DNN_PERSON_CLASS_ID": "person",
    "YALP_DNN_INPUT_SIZE": "big",
    "YALP_DNN_CONFIDENCE": "high",
    "YALP_VOICE_SAMPLE_RATE": "fast",
    "YALP_VOICE_CHANNELS": "stereo",
    "YALP_VOICE_RECORD_SECONDS": "awhile",
    "YALP_MOTOR_LEFT_PWM_PIN": "left",
    "YALP_MOTOR_RIGHT_PWM_PIN": "right",
    "YALP_MOTOR_STBY_PIN": "standby",
    "YALP_ULTRASONIC_TRIG_PIN": "trig",
    "YALP_ULTRASONIC_ECHO_PIN": "echo",
    "YALP_MOTOR_PWM_HZ": "quiet",
    "YALP_MOTOR_LEFT_INVERT": "maybe",
    "YALP_MOTOR_RIGHT_INVERT": "perhaps",
}


def _child_env(**extra):
    env = dict(os.environ)
    # Ensure the child can import the in-tree package regardless of how pytest
    # was launched.
    src = os.path.join(os.path.dirname(os.path.dirname(__file__)), "src")
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = src + (os.pathsep + existing if existing else "")
    env.update(extra)
    return env


def test_import_never_raises_with_garbage_env():
    """`import yalp.config` must succeed even with every YALP_* var garbage."""
    proc = subprocess.run(
        [sys.executable, "-c", "import yalp.config"],
        env=_child_env(**_GARBAGE_ENV),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, (
        f"import yalp.config crashed on garbage env.\n"
        f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
    )


def test_garbage_env_degrades_to_defaults_in_subprocess():
    """The garbage values must degrade to the canonical defaults, not the env."""
    code = (
        "import yalp.config as c; "
        "print(c.BUDGET_MAX_CALLS, c.FOLLOW_DNN_CONFIDENCE, "
        "c.VOICE_SAMPLE_RATE, c.MOTOR_STBY_PIN, c.MOTOR_LEFT_INVERT)"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        env=_child_env(**_GARBAGE_ENV),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.split() == ["40", "0.5", "16000", "24", "False"]
    # The default logging "lastResort" handler emits WARNINGs to stderr, so the
    # degrade is observable and names the offending variable.
    assert "YALP_BUDGET_MAX_CALLS" in proc.stderr


def test_import_clean_env_is_silent():
    """With no YALP_* overrides, import prints no config warnings to stderr."""
    clean = _child_env()  # full env with PYTHONPATH ensured
    for key in list(clean):
        if key.startswith("YALP_"):
            del clean[key]
    proc = subprocess.run(
        [sys.executable, "-c", "import yalp.config"],
        env=clean,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0
    assert "Invalid value for" not in proc.stderr
