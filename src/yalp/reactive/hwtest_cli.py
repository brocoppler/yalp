"""``yalp hwtest`` — hardware bring-up self-test CLI.

One repeatable command per wiring milestone. The operator runs each ``--check``
as they connect hardware so they can confirm each piece works in isolation before
wiring the next.

    yalp hwtest --check all --dry-run       # safe on a Mac (laptop fakes)
    yalp hwtest --check gpio                # toggle GPIO17 (LED blink / first light)
    yalp hwtest --check motors              # nudge wheels; PUT THE ROBOT ON A STAND
    yalp hwtest --check ultrasonic          # print 5 distance reads at ~3 Hz
    yalp hwtest --check camera              # grab one still and print frame shape

Field add-ons (opt-in flags; absent = default behavior above, unchanged):

    yalp hwtest --check ultrasonic --seconds 20 --hz 10   # soak: ~200 reads + summary
    yalp hwtest --check motors --matrix                   # 6-step per-channel triage
    yalp hwtest --check camera --save /tmp/frame.png       # also save the grabbed frame

With ``--dry-run`` every hardware constructor is replaced by a fake so the full
test logic runs on a Mac with no GPIO libraries installed.  The real constructors
are imported lazily so this module is always importable without ``gpiozero``.

Registered with the CLI via the feature-module contract (``add_parser`` +
``run`` listed in ``yalp.cli.FEATURE_MODULES``).
"""

from __future__ import annotations

import time
from typing import Optional


# ---------------------------------------------------------------------------
# CLI registration
# ---------------------------------------------------------------------------

def add_parser(subparsers) -> None:
    """Register the ``hwtest`` subcommand and its flags."""
    parser = subparsers.add_parser(
        "hwtest",
        help="Hardware bring-up self-test (run each --check as you wire each piece).",
        description=(
            "One repeatable command per wiring milestone. Each --check exercises "
            "a single hardware subsystem so the operator can confirm it before "
            "connecting the next. Use --dry-run on a Mac/CI with no GPIO present."
        ),
    )
    parser.add_argument(
        "--check",
        choices=("all", "gpio", "motors", "ultrasonic", "camera"),
        default="all",
        metavar="{all,gpio,motors,ultrasonic,camera}",
        help=(
            "Which subsystem to test: "
            "'gpio' — toggle left-DIR pin (GPIO17, LED blink); "
            "'motors' — nudge forward/turn/stop (PUT ROBOT ON A STAND FIRST); "
            "'ultrasonic' — print 5 distance reads at ~3 Hz; "
            "'camera' — grab one still and print frame shape; "
            "'all' — run all in sequence (default)."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Substitute laptop fakes for all hardware (FakeMotorDriver, "
            "FakeRangeSensor, synthetic Camera). Safe to run on a Mac."
        ),
    )
    parser.add_argument(
        "--camera-source",
        default="webcam",
        metavar="SOURCE",
        help="Camera source passed to Camera(source=...). Default: 'webcam'.",
    )
    # --- Ultrasonic soak (opt-in; absent = legacy 5 reads at ~3 Hz) -----------
    parser.add_argument(
        "--seconds",
        type=float,
        default=None,
        metavar="S",
        help=(
            "Ultrasonic soak: run ~S*HZ reads (paced at --hz) instead of the "
            "legacy 5, then print a summary (valid%%, miss count, min/median/max/"
            "stdev, longest miss streak). Absent: legacy 5 reads at ~3 Hz."
        ),
    )
    parser.add_argument(
        "--hz",
        type=float,
        default=3.0,
        metavar="H",
        help=(
            "Ultrasonic soak read rate in Hz (only used with --seconds). The real "
            "sensor is built with make_ultrasonic_sensor(max_poll_hz=H). Default: 3.0."
        ),
    )
    # --- Motor channel matrix (opt-in; absent = legacy 3-step sequence) -------
    parser.add_argument(
        "--matrix",
        action="store_true",
        help=(
            "Motors: run the 6-step per-channel matrix (LEFT/RIGHT forward+reverse, "
            "PIVOT-LEFT/RIGHT) for battery/driver-channel triage instead of the "
            "legacy forward/left/right sequence."
        ),
    )
    # --- Camera frame save (opt-in; absent = no file written) ----------------
    parser.add_argument(
        "--save",
        default=None,
        metavar="PATH",
        help=(
            "Camera: also write the grabbed frame to PATH via cv2.imwrite. Works "
            "with --dry-run. If cv2 is unavailable, warns and continues."
        ),
    )
    parser.set_defaults(handler=run)


def run(args) -> int:
    """Handler for ``yalp hwtest``. Returns a process exit code."""
    check = getattr(args, "check", "all")
    dry_run = bool(getattr(args, "dry_run", False))
    camera_source = getattr(args, "camera_source", "webcam")
    seconds = getattr(args, "seconds", None)
    hz = getattr(args, "hz", 3.0)
    matrix = bool(getattr(args, "matrix", False))
    save = getattr(args, "save", None)

    checks = ("gpio", "motors", "ultrasonic", "camera") if check == "all" else (check,)

    overall = 0
    for name in checks:
        print(f"\n{'='*60}")
        print(f"  hwtest: {name}{'  [DRY RUN]' if dry_run else ''}")
        print(f"{'='*60}")
        fn = {
            "gpio": _check_gpio,
            "motors": _check_motors,
            "ultrasonic": _check_ultrasonic,
            "camera": _check_camera,
        }[name]
        rc = fn(
            dry_run=dry_run,
            camera_source=camera_source,
            seconds=seconds,
            hz=hz,
            matrix=matrix,
            save=save,
        )
        if rc != 0:
            print(f"  !! {name} FAILED (exit {rc})")
            overall = rc
        else:
            print(f"  OK — {name} passed.")

    return overall


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def _check_gpio(
    *,
    dry_run: bool,
    camera_source: str,
    seconds: Optional[float] = None,
    hz: float = 3.0,
    matrix: bool = False,
    save: Optional[str] = None,
) -> int:
    """Toggle GPIO17 (left DIR pin) ~10 times — 'first light' LED-blink test."""
    print("GPIO test: toggling left DIR pin (GPIO17) 10 times.")
    print("  → watch the indicator LED (or oscilloscope on GPIO17).")

    if dry_run:
        # Dry-run: simulate toggling via a counter; no gpiozero needed.
        for i in range(10):
            state = "HIGH" if i % 2 == 0 else "LOW"
            print(f"  step {i+1:2d}: GPIO17 → {state}  [simulated]")
        print("  Dry-run: 10 simulated GPIO17 toggles completed.")
        return 0

    # Real path — lazy import so module stays Mac-importable.
    try:
        import gpiozero  # noqa: F401
        from gpiozero import DigitalOutputDevice
        from .hardware import GpiozeroMotorDriver
        GpiozeroMotorDriver._set_lgpio_pin_factory(gpiozero)
    except Exception as exc:
        print(f"  ERROR: could not initialise GPIO — {exc}")
        return 1

    try:
        pin = DigitalOutputDevice(17)
    except Exception as exc:
        print(f"  ERROR: could not open GPIO17 — {exc}")
        return 1

    try:
        for i in range(10):
            if i % 2 == 0:
                pin.on()
                print(f"  step {i+1:2d}: GPIO17 → HIGH")
            else:
                pin.off()
                print(f"  step {i+1:2d}: GPIO17 → LOW")
            time.sleep(0.1)
    finally:
        try:
            pin.off()
            pin.close()
        except Exception:
            pass

    return 0


def _check_motors(
    *,
    dry_run: bool,
    camera_source: str,
    seconds: Optional[float] = None,
    hz: float = 3.0,
    matrix: bool = False,
    save: Optional[str] = None,
) -> int:
    """Nudge motors through forward/left-turn/right-turn/stop sequence.

    With ``matrix=True`` the legacy 3-step sequence is replaced by a 6-step
    per-channel matrix (each channel forward+reverse, then both pivots) — the
    ~30-second battery/driver-channel triage from the field playbook after the
    2026-07-16 actuation sag. The wheels-up safety warning and the always-run
    ``stop()``/``close()`` teardown are shared by both paths.
    """
    if matrix:
        print("Motors test [MATRIX]: 6-step per-channel triage.")
    else:
        print("Motors test: forward → left turn → right turn → stop.")
    print()
    print("  *** PUT THE ROBOT ON A STAND (wheels off the ground) BEFORE CONTINUING ***")
    print("  The wheels WILL spin. Confirm each movement matches the prompt.")
    print()

    if dry_run:
        from .hardware import FakeMotorDriver
        driver = FakeMotorDriver()
        _note = "  [DRY RUN — fake driver, no real pins]"
    else:
        try:
            from .hardware import GpiozeroMotorDriver
            driver = GpiozeroMotorDriver()
            _note = "  [REAL GPIO]"
        except Exception as exc:
            print(f"  ERROR: could not construct GpiozeroMotorDriver — {exc}")
            return 1

    try:
        print(f"{_note}")
        print()

        if matrix:
            _run_motor_matrix(driver)
        else:
            # Forward
            print("  → FORWARD (left=0.4, right=0.4) for ~0.6 s")
            print("     Confirm: both wheels spin forward.")
            driver.set_motors(0.4, 0.4)
            time.sleep(0.6)
            driver.stop()
            print("     stop() called — wheels should coast to rest.")
            time.sleep(0.3)

            # Left turn (left wheel backward, right wheel forward)
            print("  → LEFT TURN (left=-0.4, right=0.4) for ~0.6 s")
            print("     Confirm: left wheel reverses, right wheel forward.")
            driver.set_motors(-0.4, 0.4)
            time.sleep(0.6)
            driver.stop()
            print("     stop() called — wheels should coast to rest.")
            time.sleep(0.3)

            # Right turn (left wheel forward, right wheel backward)
            print("  → RIGHT TURN (left=0.4, right=-0.4) for ~0.6 s")
            print("     Confirm: left wheel forward, right wheel reverses.")
            driver.set_motors(0.4, -0.4)
            time.sleep(0.6)
            driver.stop()
            print("     stop() called — wheels should coast to rest.")
            time.sleep(0.3)

            print("  → STOP (both channels zeroed).")
            driver.stop()

    finally:
        # ALWAYS stop and close — even if an exception fires mid-test.
        try:
            driver.stop()
        except Exception:
            pass
        try:
            driver.close()
        except Exception:
            pass

    return 0


# Per-channel matrix: (label, (left, right)). Six 0.5 s pulses at the existing
# 0.4 magnitude, each followed by stop() and a 0.3 s pause — the ~30-second
# battery/driver-channel triage from the field playbook (2026-07-16 actuation sag).
_MOTOR_MATRIX_STEPS = (
    ("LEFT-FORWARD", (0.4, 0.0)),
    ("LEFT-REVERSE", (-0.4, 0.0)),
    ("RIGHT-FORWARD", (0.0, 0.4)),
    ("RIGHT-REVERSE", (0.0, -0.4)),
    ("PIVOT-LEFT", (-0.4, 0.4)),
    ("PIVOT-RIGHT", (0.4, -0.4)),
)


def _run_motor_matrix(driver) -> None:
    """Run the 6-step per-channel matrix on ``driver`` (labels printed per step)."""
    total = len(_MOTOR_MATRIX_STEPS)
    for i, (label, (left, right)) in enumerate(_MOTOR_MATRIX_STEPS, start=1):
        print(f"  → [{i}/{total}] {label} (left={left}, right={right}) for ~0.5 s")
        driver.set_motors(left, right)
        time.sleep(0.5)
        driver.stop()
        print("     stop() called — 0.3 s pause before next channel.")
        time.sleep(0.3)


def _check_ultrasonic(
    *,
    dry_run: bool,
    camera_source: str,
    seconds: Optional[float] = None,
    hz: float = 3.0,
    matrix: bool = False,
    save: Optional[str] = None,
) -> int:
    """Print distance reads from the ultrasonic sensor.

    Legacy (``seconds`` is None): exactly 5 reads at ~3 Hz — unchanged. Soak
    (``seconds`` set): ~``seconds * hz`` reads paced at ``hz``, building the real
    sensor with ``make_ultrasonic_sensor(max_poll_hz=hz)``, then a summary.
    """
    soak = seconds is not None
    if soak:
        rate_hz = float(hz) if (hz and float(hz) > 0) else 3.0
        n_reads = max(1, int(round(float(seconds) * rate_hz)))
        print(f"Ultrasonic soak: {n_reads} reads over ~{float(seconds):g} s at ~{rate_hz:g} Hz.")
    else:
        rate_hz = 3.0
        n_reads = 5
        print("Ultrasonic test: printing 5 reads at ~3 Hz.")
    print("  Move your hand toward/away from the sensor and watch the values change.")
    print()

    if dry_run:
        from .hardware import FakeRangeSensor
        sensor = FakeRangeSensor(distance_m=0.45, known=True)
        _note = "  [DRY RUN — fake sensor]"
    else:
        try:
            # Use the SAME backend selection the real stack uses (prefer the safe
            # libgpiod v2 driver on the Pi 5; fall back to gpiozero with a loud
            # 2x/4x-defect warning). Honors YALP_ULTRASONIC_BACKEND.
            from .hardware import make_ultrasonic_sensor
            if soak:
                sensor = make_ultrasonic_sensor(max_poll_hz=rate_hz)
            else:
                sensor = make_ultrasonic_sensor()
            _note = f"  [REAL GPIO — {type(sensor).__name__}]"
        except Exception as exc:
            print(f"  ERROR: could not construct the ultrasonic sensor — {exc}")
            return 1

    print(_note)
    valid_distances: list = []
    miss_count = 0
    longest_miss_streak = 0
    current_miss_streak = 0
    try:
        for i in range(n_reads):
            distance_m, known = sensor.read_distance()
            if known:
                flag = f"{distance_m:.3f} m"
                valid_distances.append(distance_m)
                current_miss_streak = 0
            else:
                flag = f"{distance_m:.3f} m  *** echo timeout -> STOP ***"
                miss_count += 1
                current_miss_streak += 1
                if current_miss_streak > longest_miss_streak:
                    longest_miss_streak = current_miss_streak
            print(f"  read {i+1}: ({distance_m:.3f}, {known!r})  →  {flag}")
            time.sleep(1.0 / rate_hz)
    finally:
        try:
            sensor.close()
        except Exception:
            pass

    if soak:
        _print_soak_summary(
            total=n_reads,
            valid_distances=valid_distances,
            miss_count=miss_count,
            longest_miss_streak=longest_miss_streak,
        )

    return 0


def _print_soak_summary(
    *,
    total: int,
    valid_distances: list,
    miss_count: int,
    longest_miss_streak: int,
) -> None:
    """Print the ultrasonic soak summary block (valid%, distance stats, streaks)."""
    import statistics

    valid_n = len(valid_distances)
    valid_pct = (100.0 * valid_n / total) if total else 0.0

    print()
    print("  --- soak summary ---")
    print(f"  total reads:              {total}")
    print(f"  valid:                    {valid_n} ({valid_pct:.1f}%)")
    print(f"  misses:                   {miss_count}")
    if valid_distances:
        d_min = min(valid_distances)
        d_max = max(valid_distances)
        d_median = statistics.median(valid_distances)
        d_stdev = statistics.stdev(valid_distances) if valid_n >= 2 else 0.0
        print(f"  distance min/median/max:  {d_min:.3f} / {d_median:.3f} / {d_max:.3f} m")
        print(f"  distance stdev:           {d_stdev:.3f} m")
    else:
        print("  distance min/median/max:  n/a (no valid reads)")
        print("  distance stdev:           n/a (no valid reads)")
    print(f"  longest miss streak:      {longest_miss_streak}")


def _check_camera(
    *,
    dry_run: bool,
    camera_source: str,
    seconds: Optional[float] = None,
    hz: float = 3.0,
    matrix: bool = False,
    save: Optional[str] = None,
) -> int:
    """Open the camera, grab one still via .latest(), and print the frame shape.

    With ``save`` set, the grabbed frame is also written to that path via
    ``cv2.imwrite`` (works in dry-run too). A missing ``cv2`` warns but does not
    fail the check.
    """
    if dry_run:
        source = "synthetic"
        print(f"Camera test: opening synthetic camera [DRY RUN].")
    else:
        source = camera_source
        print(f"Camera test: opening camera source={source!r}.")

    try:
        from ..camera import Camera
    except Exception as exc:
        print(f"  ERROR: could not import Camera — {exc}")
        return 1

    cam = Camera(source=source)
    try:
        cam.start()
        # Give it a moment to capture the first frame.
        frame = cam.wait_for_frame(timeout=5.0)
        if frame is None:
            # Fall back to .latest() (might still be None on a cold start).
            frame = cam.latest()
        if frame is None:
            print("  no frame — camera returned None.")
        else:
            print(f"  frame shape: {frame.shape}  dtype={frame.dtype}")
            if save:
                _save_frame(frame, save)
    finally:
        try:
            cam.stop()
        except Exception:
            pass

    return 0


def _save_frame(frame, path: str) -> None:
    """Write ``frame`` to ``path`` with cv2.imwrite; warn (don't fail) if unavailable."""
    try:
        import cv2  # noqa: F401 — lazy so the module stays importable without cv2.
    except Exception as exc:
        print(f"  WARNING: cv2 unavailable — cannot save frame to {path!r} ({exc}).")
        return
    try:
        ok = cv2.imwrite(str(path), frame)
    except Exception as exc:
        print(f"  WARNING: could not write frame to {path!r} ({exc}).")
        return
    if ok:
        print(f"  saved frame → {path}")
    else:
        print(f"  WARNING: cv2.imwrite returned False for {path!r} (frame not saved).")


__all__ = ["add_parser", "run"]
