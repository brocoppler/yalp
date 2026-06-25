"""Tests for the reactive hardware GPIO pin map added in Wave 3.

These tests must pass on a Mac with NO hardware libraries (gpiozero / lgpio)
installed.  They verify:
  1. Pin constants equal the documented BCM values.
  2. MOTOR_DRIVER_KIND defaults to 'drv8833'.
  3. Importing yalp.config does NOT trigger an import of gpiozero.
  4. Config() exposes all new fields with the correct defaults.
"""

from __future__ import annotations

import sys

import pytest


# ---------------------------------------------------------------------------
# Guard: gpiozero must NOT be imported as a side-effect of importing config
# ---------------------------------------------------------------------------

def test_config_import_does_not_import_gpiozero():
    """yalp.config must be importable without pulling in gpiozero."""
    # Ensure a fresh import state (other tests may have run first, but we at
    # least verify gpiozero is absent after the import of yalp.config).
    import yalp.config  # noqa: F401 — import for side-effect check
    assert "gpiozero" not in sys.modules, (
        "gpiozero was imported as a side-effect of 'import yalp.config'. "
        "Hardware libraries must only be imported lazily inside hardware modules."
    )


# ---------------------------------------------------------------------------
# Pin constant values (BCM numbering per hardware.md)
# ---------------------------------------------------------------------------

class TestPinConstants:
    def test_motor_left_pwm_pin(self):
        from yalp.config import MOTOR_LEFT_PWM_PIN
        assert MOTOR_LEFT_PWM_PIN == 12

    def test_motor_left_dir_pin(self):
        from yalp.config import MOTOR_LEFT_DIR_PIN
        assert MOTOR_LEFT_DIR_PIN == 17

    def test_motor_right_pwm_pin(self):
        from yalp.config import MOTOR_RIGHT_PWM_PIN
        assert MOTOR_RIGHT_PWM_PIN == 13

    def test_motor_right_dir_pin(self):
        from yalp.config import MOTOR_RIGHT_DIR_PIN
        assert MOTOR_RIGHT_DIR_PIN == 22

    def test_motor_stby_pin(self):
        from yalp.config import MOTOR_STBY_PIN
        assert MOTOR_STBY_PIN == 24

    def test_ultrasonic_trig_pin(self):
        from yalp.config import ULTRASONIC_TRIG_PIN
        assert ULTRASONIC_TRIG_PIN == 5

    def test_ultrasonic_echo_pin(self):
        from yalp.config import ULTRASONIC_ECHO_PIN
        assert ULTRASONIC_ECHO_PIN == 6


# ---------------------------------------------------------------------------
# Motor driver / PWM defaults
# ---------------------------------------------------------------------------

class TestMotorDefaults:
    def test_motor_driver_kind_default(self):
        from yalp.config import MOTOR_DRIVER_KIND
        assert MOTOR_DRIVER_KIND == "drv8833"

    def test_motor_pwm_frequency_hz_default(self):
        from yalp.config import MOTOR_PWM_FREQUENCY_HZ
        assert MOTOR_PWM_FREQUENCY_HZ == 1000

    def test_motor_left_invert_default(self):
        from yalp.config import MOTOR_LEFT_INVERT
        assert MOTOR_LEFT_INVERT is False

    def test_motor_right_invert_default(self):
        from yalp.config import MOTOR_RIGHT_INVERT
        assert MOTOR_RIGHT_INVERT is False


# ---------------------------------------------------------------------------
# Ultrasonic sensor constants
# ---------------------------------------------------------------------------

class TestUltrasonicConstants:
    def test_max_poll_hz(self):
        from yalp.config import ULTRASONIC_MAX_POLL_HZ
        assert ULTRASONIC_MAX_POLL_HZ == 15.0

    def test_echo_timeout(self):
        from yalp.config import ULTRASONIC_ECHO_TIMEOUT_S
        assert ULTRASONIC_ECHO_TIMEOUT_S == pytest.approx(0.06)

    def test_max_distance(self):
        from yalp.config import ULTRASONIC_MAX_DISTANCE_M
        assert ULTRASONIC_MAX_DISTANCE_M == pytest.approx(4.0)

    def test_speed_of_sound(self):
        from yalp.config import SPEED_OF_SOUND_MPS
        assert SPEED_OF_SOUND_MPS == pytest.approx(343.0)


# ---------------------------------------------------------------------------
# Config dataclass exposes all new fields
# ---------------------------------------------------------------------------

class TestConfigDataclass:
    def setup_method(self):
        from yalp.config import Config
        self.cfg = Config()

    def test_motor_left_pwm_pin(self):
        assert self.cfg.motor_left_pwm_pin == 12

    def test_motor_left_dir_pin(self):
        assert self.cfg.motor_left_dir_pin == 17

    def test_motor_right_pwm_pin(self):
        assert self.cfg.motor_right_pwm_pin == 13

    def test_motor_right_dir_pin(self):
        assert self.cfg.motor_right_dir_pin == 22

    def test_motor_stby_pin(self):
        assert self.cfg.motor_stby_pin == 24

    def test_ultrasonic_trig_pin(self):
        assert self.cfg.ultrasonic_trig_pin == 5

    def test_ultrasonic_echo_pin(self):
        assert self.cfg.ultrasonic_echo_pin == 6

    def test_motor_driver_kind(self):
        assert self.cfg.motor_driver_kind == "drv8833"

    def test_motor_pwm_frequency_hz(self):
        assert self.cfg.motor_pwm_frequency_hz == 1000

    def test_ultrasonic_max_poll_hz(self):
        assert self.cfg.ultrasonic_max_poll_hz == pytest.approx(15.0)

    def test_ultrasonic_echo_timeout_s(self):
        assert self.cfg.ultrasonic_echo_timeout_s == pytest.approx(0.06)

    def test_ultrasonic_max_distance_m(self):
        assert self.cfg.ultrasonic_max_distance_m == pytest.approx(4.0)

    def test_speed_of_sound_mps(self):
        assert self.cfg.speed_of_sound_mps == pytest.approx(343.0)

    def test_motor_left_invert(self):
        assert self.cfg.motor_left_invert is False

    def test_motor_right_invert(self):
        assert self.cfg.motor_right_invert is False


# ---------------------------------------------------------------------------
# __all__ completeness check
# ---------------------------------------------------------------------------

_EXPECTED_IN_ALL = {
    "MOTOR_LEFT_PWM_PIN",
    "MOTOR_LEFT_DIR_PIN",
    "MOTOR_RIGHT_PWM_PIN",
    "MOTOR_RIGHT_DIR_PIN",
    "MOTOR_STBY_PIN",
    "ULTRASONIC_TRIG_PIN",
    "ULTRASONIC_ECHO_PIN",
    "MOTOR_DRIVER_KIND",
    "MOTOR_PWM_FREQUENCY_HZ",
    "ULTRASONIC_MAX_POLL_HZ",
    "ULTRASONIC_ECHO_TIMEOUT_S",
    "ULTRASONIC_MAX_DISTANCE_M",
    "SPEED_OF_SOUND_MPS",
    "MOTOR_LEFT_INVERT",
    "MOTOR_RIGHT_INVERT",
}


def test_all_exports():
    import yalp.config as cfg
    missing = _EXPECTED_IN_ALL - set(cfg.__all__)
    assert not missing, f"Missing from __all__: {missing}"
