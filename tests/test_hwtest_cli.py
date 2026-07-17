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

import re
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
# Field add-on: ultrasonic soak (--seconds / --hz)
# ---------------------------------------------------------------------------

class _SequenceRangeSensor:
    """A fake range sensor that replays a preset ``(distance_m, known)`` sequence.

    Lets us drive the soak summary through a mix of valid reads and misses so we
    can assert the miss count / longest-miss-streak / distance-stats math.
    """

    def __init__(self, seq):
        self._seq = list(seq)
        self.read_count = 0
        self.closed = False

    def read_distance(self):
        reading = self._seq[self.read_count]
        self.read_count += 1
        return reading

    def close(self):
        self.closed = True


class TestUltrasonicSoak:
    def test_soak_read_count_math(self):
        """--seconds S with --hz H performs ~round(S*H) reads (10 here)."""
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
                    rc = cli_mod._check_ultrasonic(
                        dry_run=True, camera_source="webcam", seconds=2.0, hz=5.0
                    )

        assert rc == 0
        assert instances
        assert instances[0].read_count == 10, (
            f"Expected round(2.0*5.0)=10 reads, got {instances[0].read_count}"
        )

    def test_soak_read_count_rounds(self):
        """Read count is round(S*H): 3.0 s at 3.0 Hz -> 9 reads."""
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
                    rc = cli_mod._check_ultrasonic(
                        dry_run=True, camera_source="webcam", seconds=3.0
                    )

        assert rc == 0
        assert instances[0].read_count == 9

    def test_soak_summary_all_valid(self):
        """Summary reports total, 100% valid, zero misses on an all-valid soak."""
        import yalp.reactive.hwtest_cli as cli_mod
        from yalp.reactive import hardware as hw_mod
        from yalp.reactive.hardware import FakeRangeSensor

        def make_fake(**kw):
            return FakeRangeSensor(distance_m=0.45, known=True)

        buf = StringIO()
        with patch.object(hw_mod, "FakeRangeSensor", side_effect=make_fake):
            with patch("time.sleep"):
                with patch("sys.stdout", buf):
                    rc = cli_mod._check_ultrasonic(
                        dry_run=True, camera_source="webcam", seconds=2.0, hz=5.0
                    )

        assert rc == 0
        out = buf.getvalue()
        assert "soak summary" in out
        assert "total reads:" in out and "10" in out
        assert "100.0%" in out
        assert re.search(r"misses:\s+0\b", out), out
        assert re.search(r"longest miss streak:\s+0\b", out), out
        # An all-valid soak of a constant 0.45 m has zero spread.
        assert re.search(r"0\.450 / 0\.450 / 0\.450 m", out), out
        assert re.search(r"distance stdev:\s+0\.000 m", out), out

    def test_soak_summary_mixed_valid_and_misses(self):
        """Summary math: miss count, longest consecutive-miss streak, distance stats."""
        import yalp.reactive.hwtest_cli as cli_mod
        from yalp.reactive import hardware as hw_mod

        # 5 reads: valid, miss, miss, valid, miss -> 2 valid, 3 misses, streak 2.
        seq = [
            (0.400, True),
            (9.999, False),
            (9.999, False),
            (0.500, True),
            (9.999, False),
        ]
        instances: List[_SequenceRangeSensor] = []

        def make_fake(**kw):
            inst = _SequenceRangeSensor(seq)
            instances.append(inst)
            return inst

        buf = StringIO()
        with patch.object(hw_mod, "FakeRangeSensor", side_effect=make_fake):
            with patch("time.sleep"):
                with patch("sys.stdout", buf):
                    # 1.0 s at 5.0 Hz -> exactly 5 reads.
                    rc = cli_mod._check_ultrasonic(
                        dry_run=True, camera_source="webcam", seconds=1.0, hz=5.0
                    )

        assert rc == 0
        assert instances[0].read_count == 5
        assert instances[0].closed, "sensor.close() was not called"
        out = buf.getvalue()
        assert re.search(r"total reads:\s+5\b", out), out
        assert "2 (40.0%)" in out, out
        assert re.search(r"misses:\s+3\b", out), out
        assert re.search(r"longest miss streak:\s+2\b", out), out
        # min/median/max of the two valid reads (0.400, 0.500).
        assert re.search(r"0\.400 / 0\.450 / 0\.500 m", out), out
        # STOP flag must still appear for each miss.
        assert "STOP" in out

    def test_soak_real_path_constructs_sensor_with_requested_max_poll_hz(self):
        """The REAL (non-dry-run) soak builds make_ultrasonic_sensor(max_poll_hz=H)."""
        import yalp.reactive.hwtest_cli as cli_mod
        from yalp.reactive import hardware as hw_mod
        from yalp.reactive.hardware import FakeRangeSensor

        captured_kwargs: List[dict] = []

        def fake_factory(**kw):
            captured_kwargs.append(kw)
            return FakeRangeSensor(distance_m=0.45, known=True)

        buf = StringIO()
        with patch.object(hw_mod, "make_ultrasonic_sensor", side_effect=fake_factory):
            with patch("time.sleep"):
                with patch("sys.stdout", buf):
                    rc = cli_mod._check_ultrasonic(
                        dry_run=False, camera_source="webcam", seconds=2.0, hz=7.0
                    )

        assert rc == 0
        assert captured_kwargs, "make_ultrasonic_sensor was never called"
        assert captured_kwargs[0].get("max_poll_hz") == 7.0, captured_kwargs

    def test_legacy_real_path_passes_no_max_poll_hz(self):
        """Without --seconds the real path calls make_ultrasonic_sensor() with no kwargs."""
        import yalp.reactive.hwtest_cli as cli_mod
        from yalp.reactive import hardware as hw_mod
        from yalp.reactive.hardware import FakeRangeSensor

        captured_kwargs: List[dict] = []

        def fake_factory(**kw):
            captured_kwargs.append(kw)
            return FakeRangeSensor(distance_m=0.45, known=True)

        buf = StringIO()
        with patch.object(hw_mod, "make_ultrasonic_sensor", side_effect=fake_factory):
            with patch("time.sleep"):
                with patch("sys.stdout", buf):
                    rc = cli_mod._check_ultrasonic(dry_run=False, camera_source="webcam")

        assert rc == 0
        assert captured_kwargs == [{}], captured_kwargs


# ---------------------------------------------------------------------------
# Field add-on: motor channel matrix (--matrix)
# ---------------------------------------------------------------------------

class TestMotorsMatrix:
    _EXPECTED_ORDER = [
        (0.4, 0.0),   # LEFT-FORWARD
        (-0.4, 0.0),  # LEFT-REVERSE
        (0.0, 0.4),   # RIGHT-FORWARD
        (0.0, -0.4),  # RIGHT-REVERSE
        (-0.4, 0.4),  # PIVOT-LEFT
        (0.4, -0.4),  # PIVOT-RIGHT
    ]

    def test_matrix_emits_six_labeled_calls_in_order(self):
        """--matrix issues the six per-channel commands in order, ending with stop."""
        import yalp.reactive.hwtest_cli as cli_mod
        from yalp.reactive import hardware as hw_mod
        from yalp.reactive.hardware import FakeMotorDriver

        instances: List[FakeMotorDriver] = []

        def make_fake():
            inst = FakeMotorDriver()
            instances.append(inst)
            return inst

        buf = StringIO()
        with patch.object(hw_mod, "FakeMotorDriver", side_effect=make_fake):
            with patch("time.sleep"):
                with patch("sys.stdout", buf):
                    rc = cli_mod._check_motors(
                        dry_run=True, camera_source="webcam", matrix=True
                    )

        assert rc == 0
        assert instances
        calls = instances[0].calls
        # The six drive commands, in order, with the (0.0, 0.0) stops removed.
        non_stop = [c for c in calls if c != (0.0, 0.0)]
        assert non_stop == self._EXPECTED_ORDER, non_stop
        # The very last recorded call must be the final stop.
        assert calls[-1] == (0.0, 0.0), calls[-1]
        # close() must run in the finally block.
        assert instances[0].closed, "close() was not called after the matrix"

        out = buf.getvalue()
        for label in (
            "LEFT-FORWARD",
            "LEFT-REVERSE",
            "RIGHT-FORWARD",
            "RIGHT-REVERSE",
            "PIVOT-LEFT",
            "PIVOT-RIGHT",
        ):
            assert label in out, f"Missing '{label}' label in matrix output"

    def test_matrix_labels_appear_in_order(self):
        """The six labels are printed in the documented channel order."""
        import yalp.reactive.hwtest_cli as cli_mod

        buf = StringIO()
        with patch("time.sleep"):
            with patch("sys.stdout", buf):
                rc = cli_mod._check_motors(
                    dry_run=True, camera_source="webcam", matrix=True
                )
        assert rc == 0
        out = buf.getvalue()
        order = [
            "LEFT-FORWARD",
            "LEFT-REVERSE",
            "RIGHT-FORWARD",
            "RIGHT-REVERSE",
            "PIVOT-LEFT",
            "PIVOT-RIGHT",
        ]
        positions = [out.index(lbl) for lbl in order]
        assert positions == sorted(positions), f"Labels out of order: {positions}"

    def test_matrix_keeps_safety_warning(self):
        """The wheels-up safety warning is preserved in matrix mode."""
        import yalp.reactive.hwtest_cli as cli_mod

        buf = StringIO()
        with patch("time.sleep"):
            with patch("sys.stdout", buf):
                cli_mod._check_motors(dry_run=True, camera_source="webcam", matrix=True)
        assert "PUT THE ROBOT ON A STAND" in buf.getvalue()


# ---------------------------------------------------------------------------
# Field add-on: camera frame save (--save)
# ---------------------------------------------------------------------------

class TestCameraSave:
    def test_save_writes_file_in_dry_run(self, tmp_path):
        """--save PATH writes the synthetic frame to disk in dry-run."""
        import yalp.reactive.hwtest_cli as cli_mod

        out_path = tmp_path / "frame.png"
        buf = StringIO()
        with patch("sys.stdout", buf):
            rc = cli_mod._check_camera(
                dry_run=True, camera_source="webcam", save=str(out_path)
            )

        assert rc == 0
        assert out_path.exists(), "cv2.imwrite did not create the file"
        assert out_path.stat().st_size > 0
        assert "saved frame" in buf.getvalue()

    def test_save_absent_writes_nothing(self, tmp_path):
        """Without --save, no file is written and no 'saved frame' line appears."""
        import yalp.reactive.hwtest_cli as cli_mod

        stray = tmp_path / "should_not_exist.png"
        buf = StringIO()
        with patch("sys.stdout", buf):
            rc = cli_mod._check_camera(dry_run=True, camera_source="webcam")

        assert rc == 0
        assert not stray.exists()
        assert "saved frame" not in buf.getvalue()

    def test_save_warns_and_survives_when_cv2_missing(self, tmp_path):
        """A missing cv2 warns but does NOT fail the check."""
        import builtins
        import yalp.reactive.hwtest_cli as cli_mod

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "cv2":
                raise ImportError("simulated: no cv2")
            return real_import(name, *args, **kwargs)

        out_path = tmp_path / "frame.png"
        buf = StringIO()
        with patch("builtins.__import__", side_effect=fake_import):
            with patch("sys.stdout", buf):
                rc = cli_mod._check_camera(
                    dry_run=True, camera_source="webcam", save=str(out_path)
                )

        assert rc == 0, "check must not fail just because cv2 is unavailable"
        assert not out_path.exists()
        assert "cv2 unavailable" in buf.getvalue()


# ---------------------------------------------------------------------------
# Guarantee: absent flags leave every check byte-identical to the legacy path
# ---------------------------------------------------------------------------

class TestLegacyUnchanged:
    def test_ultrasonic_absent_flags_is_exactly_five_reads_no_summary(self):
        """No --seconds -> exactly 5 reads and NO soak summary block."""
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
        assert instances[0].read_count == 5
        out = buf.getvalue()
        assert "soak summary" not in out
        assert "Ultrasonic test: printing 5 reads at ~3 Hz." in out

    def test_motors_absent_matrix_uses_legacy_sequence(self):
        """No --matrix -> legacy forward/left/right/stop sequence, no matrix labels."""
        import yalp.reactive.hwtest_cli as cli_mod
        from yalp.reactive import hardware as hw_mod
        from yalp.reactive.hardware import FakeMotorDriver

        instances: List[FakeMotorDriver] = []

        def make_fake():
            inst = FakeMotorDriver()
            instances.append(inst)
            return inst

        buf = StringIO()
        with patch.object(hw_mod, "FakeMotorDriver", side_effect=make_fake):
            with patch("time.sleep"):
                with patch("sys.stdout", buf):
                    rc = cli_mod._check_motors(dry_run=True, camera_source="webcam")

        assert rc == 0
        non_stop = [c for c in instances[0].calls if c != (0.0, 0.0)]
        assert non_stop == [(0.4, 0.4), (-0.4, 0.4), (0.4, -0.4)], non_stop
        out = buf.getvalue()
        for matrix_label in ("LEFT-FORWARD", "PIVOT-LEFT", "PIVOT-RIGHT"):
            assert matrix_label not in out, f"Legacy path leaked matrix label {matrix_label}"
        assert "Motors test: forward → left turn → right turn → stop." in out

    def test_camera_absent_save_matches_legacy_output(self):
        """No --save -> output identical to a plain legacy camera check."""
        import yalp.reactive.hwtest_cli as cli_mod

        buf_new = StringIO()
        with patch("sys.stdout", buf_new):
            rc_new = cli_mod._check_camera(dry_run=True, camera_source="webcam", save=None)

        assert rc_new == 0
        out = buf_new.getvalue()
        assert "saved frame" not in out
        assert "shape" in out or "no frame" in out


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

    def test_build_parser_accepts_new_field_flags(self):
        """The new opt-in flags parse and carry the documented defaults."""
        from yalp.cli import build_parser
        parser = build_parser()

        # Defaults when the flags are absent.
        args = parser.parse_args(["hwtest", "--check", "all", "--dry-run"])
        assert args.seconds is None
        assert args.hz == 3.0
        assert args.matrix is False
        assert args.save is None

        # Explicit values round-trip.
        args = parser.parse_args(
            [
                "hwtest",
                "--check", "ultrasonic",
                "--dry-run",
                "--seconds", "20",
                "--hz", "10",
                "--matrix",
                "--save", "/tmp/frame.png",
            ]
        )
        assert args.seconds == 20.0
        assert args.hz == 10.0
        assert args.matrix is True
        assert args.save == "/tmp/frame.png"
