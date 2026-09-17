"""``yalp calibrate`` — measure motor calibration into JSON.

An interactive bring-up wizard that turns physical reality into the
:class:`~yalp.reactive.calibration.MotorCalibration` the real backend reads:

1. **Miswire check** — nudge both wheels forward briefly, then ask
   *"did both wheels go forward? (y/n)"*. On "no" it asks per wheel and sets the
   matching ``*_invert`` so a backwards-wired motor is fixed in software.
2. **Forward speed** — drive a fixed-duration straight at full throttle, ask for
   the measured distance, and derive ``max_speed_mps = distance / duration``.
3. **Turn rate** — spin in place for a fixed duration at full throttle, ask for
   the measured degrees, and derive ``turn_rate_dps = degrees / duration``.

The result is written to ``~/.config/yalp/calibration.json`` (or
``--out`` / ``YALP_CALIBRATION_PATH``).

``--dry-run`` swaps in a :class:`~yalp.reactive.hardware.FakeMotorDriver` and a
scripted set of answers, so the whole flow runs on a laptop with no GPIO present
(the real driver is imported lazily, so this module is always importable).

Registered with the CLI via the feature-module contract (``add_parser`` + ``run``
listed in ``yalp.cli.FEATURE_MODULES``).
"""

from __future__ import annotations

import time
from typing import Callable, List, Tuple

from .calibration import MotorCalibration, default_path

# --- Calibration move parameters (full throttle so the derived constants ARE
# the full-throttle model the open-loop drive uses). -------------------------
NUDGE_THROTTLE = 0.4
NUDGE_DURATION_S = 0.5
STRAIGHT_THROTTLE = 1.0
STRAIGHT_DURATION_S = 2.0
SPIN_THROTTLE = 1.0
SPIN_DURATION_S = 2.0

# Scripted answers used by --dry-run (forward looks good; 1.0 m over the straight
# and a full 360° over the spin -> clean, deterministic derived constants).
DRY_RUN_ANSWERS = ["y", "1.0", "360"]

AskFn = Callable[[str], str]


# ---------------------------------------------------------------------------
# CLI registration
# ---------------------------------------------------------------------------

def add_parser(subparsers) -> None:
    """Register the ``calibrate`` subcommand and its flags."""
    parser = subparsers.add_parser(
        "calibrate",
        help="Measure motor calibration (inverts + speed/turn-rate) into JSON.",
        description=(
            "Interactive motor bring-up: nudge the wheels to catch a backwards "
            "miswire, drive a fixed straight to measure forward speed, and spin "
            "in place to measure turn rate, then save the calibration JSON the "
            "real backend reads. Use --dry-run on a Mac/CI (fake driver + "
            "scripted answers)."
        ),
    )
    parser.add_argument(
        "--out",
        default=None,
        metavar="PATH",
        help=(
            "Where to write the calibration JSON. Default: "
            "$YALP_CALIBRATION_PATH or ~/.config/yalp/calibration.json."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Use a FakeMotorDriver and scripted answers (no GPIO, no prompts). "
            "Safe to run on a laptop."
        ),
    )
    parser.add_argument(
        "--heading",
        action="store_true",
        help=(
            "ONLY ground-truth the camera heading estimate: spin LEFT until the "
            "camera says 90 deg, ask how far she actually turned (read a floor "
            "mark), then RIGHT the same way, and write the corrected "
            "camera_hfov_deg into the existing calibration file (other fields "
            "untouched). Every visual yaw/heading number scales with this."
        ),
    )
    parser.add_argument(
        "--turn-deg",
        type=float,
        default=90.0,
        metavar="DEG",
        help="--heading: the commanded turn size (default 90).",
    )
    parser.set_defaults(handler=run)


def run(args) -> int:
    """Handler for ``yalp calibrate``. Returns a process exit code."""
    dry_run = bool(getattr(args, "dry_run", False))
    out = getattr(args, "out", None)
    out_path = out if out is not None else default_path()
    if bool(getattr(args, "heading", False)):
        return run_heading(out_path, dry_run=dry_run, turn_deg=float(getattr(args, "turn_deg", 90.0)))

    if dry_run:
        from .hardware import FakeMotorDriver

        driver = FakeMotorDriver()
        ask = _scripted_ask(DRY_RUN_ANSWERS)
        sleep: Callable[[float], None] = lambda _s: None  # noqa: E731 - no real waiting
        print("=== yalp calibrate [DRY RUN — fake driver, scripted answers] ===")
    else:
        try:
            from .hardware import GpiozeroMotorDriver

            driver = GpiozeroMotorDriver()
        except Exception as exc:  # pragma: no cover - needs a real Pi env
            print(f"  ERROR: could not construct GpiozeroMotorDriver — {exc}")
            return 1
        ask = input
        sleep = time.sleep
        print("=== yalp calibrate ===")
        print("  *** PUT THE ROBOT ON THE FLOOR WITH ~2 m of CLEAR SPACE AHEAD ***")
        print()

    try:
        cal = calibrate(driver, ask, sleep=sleep)
    finally:
        try:
            driver.stop()
        except Exception:  # pragma: no cover - best effort
            pass
        try:
            driver.close()
        except Exception:  # pragma: no cover - best effort
            pass

    written = cal.save(out_path)
    print()
    print(f"  saved calibration -> {written}")
    print(f"    left_invert={cal.left_invert} right_invert={cal.right_invert}")
    print(f"    left_trim={cal.left_trim} right_trim={cal.right_trim}")
    print(f"    max_speed_mps={cal.max_speed_mps:.4f} (speed at FULL throttle)")
    print(f"    turn_rate_dps={cal.turn_rate_dps:.4f}")
    print(
        f"    duty_deadband={cal.duty_deadband:.4f} "
        "(measured motor deadband; below it the wheels stall — see "
        "config.DRIVE_DUTY_DEADBAND)"
    )
    return 0


# ---------------------------------------------------------------------------
# Core calibration flow (driver + prompt injected, so it is fully testable)
# ---------------------------------------------------------------------------

def calibrate(
    driver,
    ask: AskFn,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> MotorCalibration:
    """Run the calibration sequence against ``driver``, asking via ``ask``.

    ``driver`` is any :class:`~yalp.reactive.hardware.MotorDriver`; ``ask`` is a
    prompt-> answer callable (``input`` for the real CLI, a scripted function for
    ``--dry-run``/tests). ``sleep`` lets the dry-run/tests skip the real move
    durations. Returns the measured :class:`MotorCalibration` (NOT yet saved).
    """
    left_invert, right_invert = _calibrate_inverts(driver, ask, sleep)
    max_speed_mps = _calibrate_max_speed(driver, ask, sleep)
    turn_rate_dps = _calibrate_turn_rate(driver, ask, sleep)
    return MotorCalibration(
        left_invert=left_invert,
        right_invert=right_invert,
        # Trim is left at its no-op default; this wizard measures the motion
        # model and corrects miswires, not fine per-wheel drift.
        left_trim=1.0,
        right_trim=1.0,
        max_speed_mps=max_speed_mps,
        turn_rate_dps=turn_rate_dps,
    )


def _calibrate_inverts(driver, ask: AskFn, sleep) -> Tuple[bool, bool]:
    """Nudge forward; flip the invert of whichever wheel went the wrong way."""
    print("[1/3] Miswire check: nudging both wheels FORWARD briefly.")
    driver.set_motors(NUDGE_THROTTLE, NUDGE_THROTTLE)
    sleep(NUDGE_DURATION_S)
    driver.stop()

    if _yes(ask("  did both wheels go forward? (y/n) ")):
        return (False, False)

    left_invert = not _yes(ask("  did the LEFT wheel go forward? (y/n) "))
    right_invert = not _yes(ask("  did the RIGHT wheel go forward? (y/n) "))
    if left_invert:
        print("    -> left wheel inverted in software.")
    if right_invert:
        print("    -> right wheel inverted in software.")
    return (left_invert, right_invert)


def _calibrate_max_speed(driver, ask: AskFn, sleep) -> float:
    """Drive a fixed straight; derive max_speed_mps from the measured distance."""
    print(f"[2/3] Forward speed: driving straight for {STRAIGHT_DURATION_S:.1f}s.")
    driver.set_motors(STRAIGHT_THROTTLE, STRAIGHT_THROTTLE)
    sleep(STRAIGHT_DURATION_S)
    driver.stop()
    distance_m = _ask_float(ask, "  measured forward distance in METERS: ")
    max_speed_mps = distance_m / STRAIGHT_DURATION_S
    print(f"    -> max_speed_mps = {distance_m:.3f} / {STRAIGHT_DURATION_S:.1f}"
          f" = {max_speed_mps:.4f}")
    return max_speed_mps


def _calibrate_turn_rate(driver, ask: AskFn, sleep) -> float:
    """Spin in place; derive turn_rate_dps from the measured rotation."""
    print(f"[3/3] Turn rate: spinning in place for {SPIN_DURATION_S:.1f}s.")
    # Spin in place: wheels oppose (left reverse, right forward).
    driver.set_motors(-SPIN_THROTTLE, SPIN_THROTTLE)
    sleep(SPIN_DURATION_S)
    driver.stop()
    degrees = _ask_float(ask, "  measured rotation in DEGREES: ")
    turn_rate_dps = degrees / SPIN_DURATION_S
    print(f"    -> turn_rate_dps = {degrees:.1f} / {SPIN_DURATION_S:.1f}"
          f" = {turn_rate_dps:.4f}")
    return turn_rate_dps


# ---------------------------------------------------------------------------
# Prompt helpers
# ---------------------------------------------------------------------------

def _scripted_ask(answers: List[str]) -> AskFn:
    """Return an ``ask`` that pops from ``answers`` (echoing the Q&A)."""
    queue = list(answers)

    def ask(prompt: str) -> str:
        answer = queue.pop(0) if queue else ""
        print(f"{prompt}{answer}   [scripted]")
        return answer

    return ask


def _yes(answer: str) -> bool:
    """Interpret a yes/no answer; anything but an explicit yes is treated no."""
    return str(answer).strip().lower() in ("y", "yes", "1", "true")


def _ask_float(ask: AskFn, prompt: str) -> float:
    """Ask until a finite, parseable float is given (re-prompting on bad input)."""
    while True:
        raw = ask(prompt)
        try:
            return float(str(raw).strip())
        except (TypeError, ValueError):
            print(f"    '{raw}' is not a number — try again.")


__all__ = ["add_parser", "run", "calibrate"]


# ---------------------------------------------------------------------------
# Heading ground truth: calibrate the camera HFOV against a floor mark
# ---------------------------------------------------------------------------
HEADING_SPIN_DUTY = 0.5
HEADING_SPIN_MAX_S = 4.0
DRY_RUN_HEADING_ANSWERS = ["90", "90"]


def run_heading(out_path, *, dry_run: bool, turn_deg: float = 90.0) -> int:
    """``yalp calibrate --heading`` — write a ground-truthed ``camera_hfov_deg``."""
    from .. import config
    from .calibration import load_if_present

    cal = load_if_present(out_path) or MotorCalibration()
    hfov_old = cal.effective_hfov_deg(config.CAMERA_HFOV_DEG)

    if dry_run:
        from .drive_diagnostics import DriverCoupledYaw, TickingFakeCamera
        from .hardware import FakeMotorDriver

        driver = FakeMotorDriver()
        camera = TickingFakeCamera()
        estimator = DriverCoupledYaw(driver)
        ask = _scripted_ask(DRY_RUN_HEADING_ANSWERS)
        sleep: Callable[[float], None] = lambda _s: None  # noqa: E731
        clock = {"t": 0.0}

        def monotonic() -> float:
            clock["t"] += 1.0 / 15.0
            return clock["t"]

        cleanup = driver.stop
        print("=== yalp calibrate --heading [DRY RUN — fake driver, scripted answers] ===")
    else:
        try:
            from ..camera import Camera
            from .hardware import GpiozeroMotorDriver
            from .visual_odometry import YawEstimator

            driver = GpiozeroMotorDriver(
                left_invert=cal.left_invert, right_invert=cal.right_invert,
                left_trim=cal.left_trim, right_trim=cal.right_trim,
            )
            camera = Camera(source="webcam")
            camera.start()
            if camera.wait_for_frame(timeout=6.0) is None:
                print("  ERROR: no camera frame within 6 s.")
                camera.stop()
                driver.close()
                return 1
            estimator = YawEstimator(hfov_deg=hfov_old)
        except Exception as exc:  # pragma: no cover - needs a real Pi env
            print(f"  ERROR: could not build the rig — {exc}")
            return 1
        ask = input
        sleep = time.sleep
        monotonic = time.monotonic

        def cleanup() -> None:
            for fn in (driver.stop, camera.stop, driver.close):
                try:
                    fn()
                except Exception:
                    pass

        print("=== yalp calibrate --heading ===")
        print("  Put a tape mark on the floor along her nose direction, and a second")
        print("  mark at 90 deg (a floor tile edge or a protractor works). She will")
        print(f"  spin LEFT until the camera says {turn_deg:.0f} deg, stop, and ask what you saw.")
        print()

    try:
        hfov_new = calibrate_heading(
            driver, camera, estimator, ask, hfov_deg=hfov_old, turn_deg=turn_deg,
            sleep=sleep, monotonic=monotonic,
        )
    finally:
        try:
            cleanup()
        except Exception:
            pass

    cal.camera_hfov_deg = float(hfov_new)
    written = cal.save(out_path)
    print()
    print(f"  saved calibration -> {written}")
    print(f"    camera_hfov_deg={cal.camera_hfov_deg:.2f} (was {hfov_old:.2f}; other fields untouched)")
    return 0


def calibrate_heading(
    driver,
    camera,
    estimator,
    ask: AskFn,
    *,
    hfov_deg: float,
    turn_deg: float = 90.0,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> float:
    """Spin LEFT then RIGHT to the camera's ``turn_deg``; return the corrected HFOV.

    The camera integrates yaw in units of ``hfov_deg / width`` per pixel, so if
    it *claims* ``turn_deg`` when the robot actually turned ``measured`` degrees,
    the true HFOV is ``hfov_deg * measured / claimed``. Both directions are
    measured and averaged (a mounting tilt biases them slightly differently).
    """
    from .drive_diagnostics import _FrameFeed

    ratios: List[float] = []
    for label, (l, r) in (("LEFT", (-HEADING_SPIN_DUTY, HEADING_SPIN_DUTY)), ("RIGHT", (HEADING_SPIN_DUTY, -HEADING_SPIN_DUTY))):
        feed = _FrameFeed(camera)
        estimator.reset()
        first = feed.next()
        if first is not None:
            estimator.update(first)
        claimed = 0.0
        t0 = monotonic()
        print(f"  spinning {label} until the camera reads {turn_deg:.0f} deg (cap {HEADING_SPIN_MAX_S:.0f} s)...")
        driver.set_motors(l, r)
        try:
            while monotonic() - t0 < HEADING_SPIN_MAX_S:
                frame = feed.next()
                if frame is not None:
                    smp = estimator.update(frame)
                    if smp is not None and smp.response >= 0.05:
                        claimed += float(smp.delta_deg)
                if abs(claimed) >= turn_deg:
                    break
                sleep(0.005)
        finally:
            driver.stop()
        sleep(0.8)
        if abs(claimed) < 0.25 * turn_deg:
            print(f"    camera only saw {claimed:+.1f} deg — did she move? skipping this direction.")
            continue
        measured = abs(_ask_float(ask, f"    camera says {claimed:+.1f} deg — how many degrees did she ACTUALLY turn? "))
        ratio = measured / abs(claimed)
        ratios.append(ratio)
        print(f"    -> actual/camera = {ratio:.3f}")
    if not ratios:
        print("  no usable measurement; keeping the current HFOV.")
        return float(hfov_deg)
    ratio = sum(ratios) / len(ratios)
    hfov_new = float(hfov_deg) * ratio
    print(f"  camera_hfov_deg: {hfov_deg:.2f} x {ratio:.3f} = {hfov_new:.2f}")
    return hfov_new
