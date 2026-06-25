"""Tests for scripts/pi_setup.sh and scripts/verify_gpio_stack.py.

These tests are laptop-safe — they do not execute pi_setup.sh (only parse it)
and they do not import gpiozero or touch any hardware.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).parent.parent
PI_SETUP_SH = REPO_ROOT / "scripts" / "pi_setup.sh"
VERIFY_GPIO_SCRIPT = REPO_ROOT / "scripts" / "verify_gpio_stack.py"


# ---------------------------------------------------------------------------
# Helper: import verify_gpio_stack without executing main()
# ---------------------------------------------------------------------------

def _import_verify_gpio_stack():
    """Import verify_gpio_stack.py as a module without running main()."""
    spec = importlib.util.spec_from_file_location(
        "verify_gpio_stack", VERIFY_GPIO_SCRIPT
    )
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


# ---------------------------------------------------------------------------
# pi_setup.sh existence and syntax
# ---------------------------------------------------------------------------

class TestPiSetupSh:
    def test_script_exists(self):
        """scripts/pi_setup.sh must exist."""
        assert PI_SETUP_SH.exists(), f"{PI_SETUP_SH} not found"

    def test_script_is_file(self):
        assert PI_SETUP_SH.is_file()

    def test_bash_syntax_check(self):
        """bash -n must pass (syntax check, no execution)."""
        result = subprocess.run(
            ["bash", "-n", str(PI_SETUP_SH)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"bash -n {PI_SETUP_SH} failed:\n{result.stderr}"
        )

    def test_script_contains_set_euo_pipefail(self):
        """Script must use set -euo pipefail for safety."""
        content = PI_SETUP_SH.read_text()
        assert "set -euo pipefail" in content

    def test_script_contains_apt_update(self):
        content = PI_SETUP_SH.read_text()
        assert "apt update" in content

    def test_script_contains_full_upgrade(self):
        content = PI_SETUP_SH.read_text()
        assert "full-upgrade" in content

    def test_script_installs_python3_lgpio(self):
        content = PI_SETUP_SH.read_text()
        assert "python3-lgpio" in content

    def test_script_creates_venv(self):
        content = PI_SETUP_SH.read_text()
        assert "python3-venv" in content or "venv" in content

    def test_script_installs_pi_extra(self):
        content = PI_SETUP_SH.read_text()
        assert "[pi]" in content

    def test_script_installs_dev_extra(self):
        content = PI_SETUP_SH.read_text()
        assert "[dev]" in content

    def test_script_ends_with_setup_complete(self):
        """Final output must include 'Pi setup complete'."""
        content = PI_SETUP_SH.read_text()
        assert "Pi setup complete" in content

    def test_script_checks_python_version(self):
        content = PI_SETUP_SH.read_text()
        # Should mention 3.11 or MIN_PYTHON_MINOR=11
        assert "3.11" in content or "MIN_PYTHON_MINOR" in content


# ---------------------------------------------------------------------------
# classify_factory() unit tests
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def verify_gpio_mod():
    return _import_verify_gpio_stack()


class TestClassifyFactory:
    def test_lgpio_returns_ok(self, verify_gpio_mod):
        assert verify_gpio_mod.classify_factory("gpiozero.pins.lgpio") == "ok"

    def test_native_returns_ok(self, verify_gpio_mod):
        assert verify_gpio_mod.classify_factory("gpiozero.pins.native") == "ok"

    def test_mock_returns_ok(self, verify_gpio_mod):
        """Mock factory (used in unit tests) is treated as ok."""
        assert verify_gpio_mod.classify_factory("gpiozero.pins.mock") == "ok"

    def test_rpigpio_module_name_returns_rpigpio(self, verify_gpio_mod):
        assert verify_gpio_mod.classify_factory("gpiozero.pins.rpigpio") == "rpigpio"

    def test_rpi_gpio_dotted_returns_rpigpio(self, verify_gpio_mod):
        assert verify_gpio_mod.classify_factory("RPi.GPIO") == "rpigpio"

    def test_rpi_gpio_underscore_returns_rpigpio(self, verify_gpio_mod):
        assert verify_gpio_mod.classify_factory("rpi_gpio_backend") == "rpigpio"

    def test_unknown_module_returns_unknown(self, verify_gpio_mod):
        assert verify_gpio_mod.classify_factory("some.other.backend") == "unknown"

    def test_empty_string_returns_unknown(self, verify_gpio_mod):
        assert verify_gpio_mod.classify_factory("") == "unknown"

    def test_lgpio_uppercase_returns_ok(self, verify_gpio_mod):
        """Classification is case-insensitive."""
        assert verify_gpio_mod.classify_factory("gpiozero.pins.LGPIO") == "ok"


# ---------------------------------------------------------------------------
# verify_gpio_stack.py existence
# ---------------------------------------------------------------------------

class TestVerifyGpioStackScript:
    def test_script_exists(self):
        assert VERIFY_GPIO_SCRIPT.exists(), f"{VERIFY_GPIO_SCRIPT} not found"

    def test_classify_factory_is_importable(self):
        """classify_factory must be importable without gpiozero installed."""
        mod = _import_verify_gpio_stack()
        assert callable(mod.classify_factory)

    def test_main_is_defined(self):
        mod = _import_verify_gpio_stack()
        assert callable(mod.main)
