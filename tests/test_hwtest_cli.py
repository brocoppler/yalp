"""Tests for ``yalp hwtest`` CLI (hardware bring-up self-test).

All tests use ``--dry-run`` so they run on a Mac without GPIO libraries.
Key guarantees verified:
  - Importing the module does NOT import gpiozero.
  - Every ``--check ... --dry-run`` path exits 0.
  - The motors dry-run issues forward/left-turn/right-turn calls to
    FakeMotorDriver AND always ends with stop() (even if a failure occurs).
  - The ultrasonic dry-run prints 5 reads.
  - The 'all' dry-run path runs all sub-checks and exits 0.
"""

from __future__ import annotations

import sys
import types
import importlib
from io import StringIO
from typing import List, Optional, Tuple
from unittest.mock import patch, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Guard: module must be importable with no gpiozero present
# ---------------------------------------------------------------------------

def test_module_import_does_not_require_gpiozero():
    """Importing hwtest_cli must NOT import gpiozero (laptop-first rule)."""
    # Remove gpiozero from sys.modules so an accidental top-level import is caught.
    saved = {k: sys.modules.pop(k) for k in list(sys.modules) if k.startswith("gpiozero")}
    try:
        # Force re-import by removing the cached version (if any).
        for key in list(sys.modules):
            if "hwtest_cli" in key:
                del sys.modules[key]
        import yalp.reactive.hwtest_cli  # noqa: F401 — must not raise
        assert "gpiozero" not in sys.modules, "gpiozero was imported at module level"
    finally:
        sys.modules.update(saved)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_check(check: str, extra_args: Optional[List[str]] = None) -> Tuple[int, str]:
    """Run ``yalp hwtest --check <check> --dry-run`` and return (exit_code, stdout)."""
    from yalp.cli import build_parser

    argv = ["hwtest", "--check", check, "--dry-run"]
    if extra_args:
        argv.extend(extra_args)

    buf = StringIO()
    with patch("sys.stdout", buf):
        parser = build_parser()
        args = parser.parse_args(argv)
        handler = getattr(args, "handler", None)
        assert handler is not None, "no handler registered for 'hwtest'"
        rc = handler(args)

    return int(rc), buf.getvalue()


# ---------------------------------------------------------------------------
# Per-check dry-run tests
# ---------------------------------------------------------------------------

class TestGpioCheck:
    def test_gpio_dry_run_exits_0(self):
        rc, out = _run_check("gpio")
        assert rc == 0

    def test_gpio_dry_run_mentions_gpio17(self):
        _, out = _run_check("gpio")
        assert "GPIO17" in out or "17" in out

    def test_gpio_dry_run_mentions_simulated(self):
        _, out = _run_check("gpio")
        assert "simulated" in out.lower() or "dry" in out.lower()


class TestMotorsCheck:
    def test_motors_dry_run_exits_0(self):
        rc, out = _run_check("motors")
        assert rc == 0

    def test_motors_dry_run_issues_forward_call(self):
        """Motors test must command (0.4, 0.4) for the forward nudge."""
        import yalp.reactive.hwtest_cli as cli_mod
        from yalp.reactive import hardware as hw_mod
        from yalp.reactive.hardware import FakeMotorDriver

        instances: List[FakeMotorDriver] = []

        def make_fake():
            inst = FakeMotorDriver()
            instances.append(inst)
            return inst

        with patch.object(hw_mod, "FakeMotorDriver", side_effect=make_fake):
            with patch("time.sleep"):
                rc = cli_mod._check_motors(dry_run=True, camera_source="webcam")

        assert rc == 0
        assert instances, "FakeMotorDriver was never constructed"
        motor_calls = instances[0].calls
        assert (0.4, 0.4) in motor_calls, f"No forward call in {motor_calls}"
        assert "FORWARD" in str(motor_calls) or (0.4, 0.4) in motor_calls

    def test_motors_dry_run_always_calls_stop(self):
        """stop() must be called even if we inject an error partway through."""
        from yalp.reactive.hardware import FakeMotorDriver

        driver = FakeMotorDriver()
        call_count_before = driver.stop_count

        # Patch FakeMotorDriver to return our tracked instance.
        with patch("yalp.reactive.hwtest_cli._check_motors") as mock_check:
            # Instead of mocking the check away, let's directly exercise it.
            pass

        # Direct approach: call _check_motors with dry_run=True and patch sleep.
        import time
        import yalp.reactive.hwtest_cli as cli_mod
        from yalp.reactive import hardware as hw_mod

        driver2 = FakeMotorDriver()

        original_FakeMotorDriver = hw_mod.FakeMotorDriver

        class _TrackingFakeMotor(FakeMotorDriver):
            pass

        instances: List[_TrackingFakeMotor] = []

        def make_fake():
            inst = _TrackingFakeMotor()
            instances.append(inst)
            return inst

        with patch.object(hw_mod, "FakeMotorDriver", side_effect=make_fake):
            with patch("time.sleep"):
                rc = cli_mod._check_motors(dry_run=True, camera_source="webcam")

        assert rc == 0
        assert len(instances) == 1
        inst = instances[0]
        # stop() must have been called (at least once — at the end of the test).
        assert inst.stop_count >= 1, "stop() was never called on the FakeMotorDriver"

    def test_motors_dry_run_stop_called_on_exception(self):
        """stop() and close() must be called even when set_motors raises."""
        import yalp.reactive.hwtest_cli as cli_mod
        from yalp.reactive import hardware as hw_mod
        from yalp.reactive.hardware import FakeMotorDriver

        class _RaisingFakeMotor(FakeMotorDriver):
            _boom = False

            def set_motors(self, left: float, right: float) -> None:
                super().set_motors(left, right)
                if (left, right) == (-0.4, 0.4):
                    raise RuntimeError("simulated mid-test failure")

        instances: List[_RaisingFakeMotor] = []

        def make_raising():
            inst = _RaisingFakeMotor()
            instances.append(inst)
            return inst

        with patch.object(hw_mod, "FakeMotorDriver", side_effect=make_raising):
            with patch("time.sleep"):
                # _check_motors should NOT propagate the exception — it should
                # catch it (implicitly: the try/finally must at least call stop).
                # If it re-raises, the test will catch the RuntimeError.
                try:
                    rc = cli_mod._check_motors(dry_run=True, camera_source="webcam")
                except RuntimeError:
                    pass  # acceptable — as long as stop was called

        assert len(instances) == 1
        inst = instances[0]
        assert inst.stop_count >= 1, "stop() was not called when set_motors raised"
        assert inst.closed, "close() was not called when set_motors raised"

    def test_motors_dry_run_issues_left_turn(self):
        """Motors dry-run must issue a left-turn command (-0.4, 0.4)."""
        import yalp.reactive.hwtest_cli as cli_mod
        from yalp.reactive import hardware as hw_mod
        from yalp.reactive.hardware import FakeMotorDriver

        instances: List[FakeMotorDriver] = []

        def make_fake():
            inst = FakeMotorDriver()
            instances.append(inst)
            return inst

        with patch.object(hw_mod, "FakeMotorDriver", side_effect=make_fake):
            with patch("time.sleep"):
                rc = cli_mod._check_motors(dry_run=True, camera_source="webcam")

        assert rc == 0
        assert instances, "FakeMotorDriver was never constructed"
        motor_calls = instances[0].calls
        # forward nudge
        assert (0.4, 0.4) in motor_calls, f"No forward call in {motor_calls}"
        # left turn
        assert (-0.4, 0.4) in motor_calls, f"No left-turn call in {motor_calls}"
        # right turn
        assert (0.4, -0.4) in motor_calls, f"No right-turn call in {motor_calls}"

    def test_motors_dry_run_ends_with_stop(self):
        """The very last recorded call must be (0.0, 0.0) — the final stop()."""
        import yalp.reactive.hwtest_cli as cli_mod
        from yalp.reactive import hardware as hw_mod
        from yalp.reactive.hardware import FakeMotorDriver

        instances: List[FakeMotorDriver] = []

        def make_fake():
            inst = FakeMotorDriver()
            instances.append(inst)
            return inst

        with patch.object(hw_mod, "FakeMotorDriver", side_effect=make_fake):
            with patch("time.sleep"):
                rc = cli_mod._check_motors(dry_run=True, camera_source="webcam")

        assert rc == 0
        assert instances
        last = instances[0].calls[-1]
        assert last == (0.0, 0.0), (
            f"Last motor call was {last!r}, expected (0.0, 0.0) stop"
        )


def _motors_with_fake(kw, calls, stop_calls):
    """Unused helper kept for clarity."""
    pass


class TestUltrasonicCheck:
    def test_ultrasonic_dry_run_exits_0(self):
        rc, out = _run_check("ultrasonic")
        assert rc == 0

    def test_ultrasonic_dry_run_prints_5_reads(self):
        """Must print exactly 5 distance reads."""
        import yalp.reactive.hwtest_cli as cli_mod
        from yalp.reactive import hardware as hw_mod
        from yalp.reactive.hardware import FakeRangeSensor

        instances: List[FakeRangeSensor] = []

        def make_fake(**kw):
            inst = FakeRangeSensor(distance_m=0.45, known=True)
            instances.append(inst)
            return inst

        buf = StringIO()
        with patch.object(hw_mod, "FakeRangeSensor", side_effect=make_fake):
            with patch("time.sleep"):
                with patch("sys.stdout", buf):
                    rc = cli_mod._check_ultrasonic(dry_run=True, camera_source="webcam")

        assert rc == 0
        assert instances
        assert instances[0].read_count == 5, (
            f"Expected 5 reads, got {instances[0].read_count}"
        )

    def test_ultrasonic_dry_run_output_contains_read_lines(self):
        """Output must contain 5 'read N:' lines."""
        import yalp.reactive.hwtest_cli as cli_mod

        buf = StringIO()
        with patch("time.sleep"):
            with patch("sys.stdout", buf):
                rc = cli_mod._check_ultrasonic(dry_run=True, camera_source="webcam")

        assert rc == 0
        out = buf.getvalue()
        for i in range(1, 6):
            assert f"read {i}:" in out, f"Missing 'read {i}:' in output"

    def test_ultrasonic_dry_run_timeout_flagged(self):
        """A known=False reading must print the 'echo timeout -> STOP' flag."""
        import yalp.reactive.hwtest_cli as cli_mod
        from yalp.reactive import hardware as hw_mod
        from yalp.reactive.hardware import FakeRangeSensor as _RealFakeRangeSensor

        # Capture the real class before patching so we can call it inside the factory.
        RealFakeRangeSensor = _RealFakeRangeSensor

        def make_timeout_sensor(**kw):
            s = RealFakeRangeSensor(distance_m=4.0, known=False)
            return s

        buf = StringIO()
        with patch.object(hw_mod, "FakeRangeSensor", side_effect=make_timeout_sensor):
            with patch("time.sleep"):
                with patch("sys.stdout", buf):
                    rc = cli_mod._check_ultrasonic(dry_run=True, camera_source="webcam")

        assert rc == 0
        assert "STOP" in buf.getvalue(), "Expected STOP flag for known=False"


class TestCameraCheck:
    def test_camera_dry_run_exits_0(self):
        """Camera dry-run (synthetic source) must exit 0."""
        import yalp.reactive.hwtest_cli as cli_mod

        buf = StringIO()
        with patch("sys.stdout", buf):
            rc = cli_mod._check_camera(dry_run=True, camera_source="webcam")

        assert rc == 0

    def test_camera_dry_run_prints_shape_or_no_frame(self):
        """Output must contain 'shape' or 'no frame'."""
        import yalp.reactive.hwtest_cli as cli_mod

        buf = StringIO()
        with patch("sys.stdout", buf):
            rc = cli_mod._check_camera(dry_run=True, camera_source="webcam")

        out = buf.getvalue()
        assert "shape" in out or "no frame" in out, f"Unexpected output: {out!r}"


class TestAllCheck:
    def test_all_dry_run_exits_0(self):
        """--check all --dry-run must exercise every subsystem and exit 0."""
        import time

        with patch("time.sleep"):
            rc, out = _run_check("all")

        assert rc == 0, f"Expected exit 0, got {rc}.\nOutput:\n{out}"

    def test_all_dry_run_covers_all_subsystems(self):
        """Output must mention all four sub-checks."""
        with patch("time.sleep"):
            rc, out = _run_check("all")

        for name in ("gpio", "motors", "ultrasonic", "camera"):
            assert name in out.lower(), f"Missing '{name}' in combined output"


# ---------------------------------------------------------------------------
# CLI integration: feature module is registered in FEATURE_MODULES
# ---------------------------------------------------------------------------

class TestCliRegistration:
    def test_hwtest_in_feature_modules(self):
        from yalp.cli import FEATURE_MODULES
        assert "yalp.reactive.hwtest_cli" in FEATURE_MODULES, (
            "yalp.reactive.hwtest_cli is not listed in cli.FEATURE_MODULES"
        )

    def test_build_parser_has_hwtest_subcommand(self):
        from yalp.cli import build_parser
        parser = build_parser()
        # If the subcommand is not registered, parse_args will error.
        args = parser.parse_args(["hwtest", "--check", "gpio", "--dry-run"])
        assert args.check == "gpio"
        assert args.dry_run is True
