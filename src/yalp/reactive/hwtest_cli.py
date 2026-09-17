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
    yalp hwtest --check wheels --duty 0.45 --duty 0.60      # per-wheel yaw-rate matrix (camera-measured)
    yalp hwtest --check straight --duty 0.45 --seconds 3 --reverse-first 2 --save /tmp/straight.csv

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
        choices=("all", "gpio", "motors", "ultrasonic", "camera", "wheels", "straight",
                 "encoders", "imu", "power"),
        default="all",
        metavar="{all,gpio,motors,ultrasonic,camera,wheels,straight,encoders,imu,power}",
        help=(
            "Which subsystem to test: "
            "'gpio' — toggle left-DIR pin (GPIO17, LED blink); "
            "'motors' — nudge forward/turn/stop (PUT ROBOT ON A STAND FIRST); "
            "'ultrasonic' — print 5 distance reads at ~3 Hz; "
            "'camera' — grab one still and print frame shape; "
            "'wheels' — camera-measured per-wheel yaw-rate matrix (ON THE FLOOR, "
            "~0.5 m clear all round): finds a weak/dead/reversed channel and the "
            "decay-mode signature; "
            "'straight' — drive straight at --duty for --seconds recording yaw + "
            "sonar per frame (the veer diagnostic); "
            "'encoders' — live wheel-encoder tick counts for --seconds (spin the "
            "wheels by hand; ON A STAND); "
            "'imu' — MPU-6050 gyro: bias calibration, then live yaw rate / integrated "
            "heading for --seconds (turn her by hand); "
            "'power' — INA219 pack voltage/current/state; "
            "'all' — run gpio/motors/ultrasonic/camera in sequence (default; the "
            "floor and optional-sensor checks are never part of 'all')."
        ),
    )
    # --- Floor diagnostics (wheels / straight) --------------------------------
    parser.add_argument(
        "--duty",
        type=float,
        action="append",
        default=None,
        metavar="D",
        help=(
            "wheels: a duty to pulse at (repeatable; default 0.45 and 0.60). "
            "straight: the drive duty (first value; default 0.45)."
        ),
    )
    parser.add_argument(
        "--reverse-first",
        type=float,
        default=0.0,
        metavar="S",
        help="straight: back up for S seconds first to make room (NO rear sensor).",
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
    duties = getattr(args, "duty", None)
    reverse_first = float(getattr(args, "reverse_first", 0.0) or 0.0)

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
            "wheels": _check_wheels,
            "straight": _check_straight,
            "encoders": _check_encoders,
            "imu": _check_imu,
            "power": _check_power,
        }[name]
        kwargs = dict(
            dry_run=dry_run,
            camera_source=camera_source,
            seconds=seconds,
            hz=hz,
            matrix=matrix,
            save=save,
        )
        if name in ("wheels", "straight"):
            kwargs.update(duties=duties, reverse_first=reverse_first)
        rc = fn(**kwargs)
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


# ---------------------------------------------------------------------------
# Floor diagnostics: per-wheel yaw-rate matrix + straight-drive recorder
# (yalp.reactive.drive_diagnostics — the camera is the measuring instrument)
# ---------------------------------------------------------------------------
def _floor_rig(dry_run: bool, camera_source: str):
    """Build (driver, camera, estimator, sensor, sleep, monotonic, cleanup, note)."""
    if dry_run:
        from .drive_diagnostics import DriverCoupledYaw, TickingFakeCamera
        from .hardware import FakeMotorDriver, FakeRangeSensor

        driver = FakeMotorDriver()
        camera = TickingFakeCamera()
        # A slightly weak RIGHT wheel so the dry-run table has something to say.
        estimator = DriverCoupledYaw(driver, right_scale=0.8)
        sensor = FakeRangeSensor(distance_m=1.5, known=True)
        clock = {"t": 0.0}

        def sleep(_s: float) -> None:  # no real waiting in dry-run
            return None

        def monotonic() -> float:
            clock["t"] += 1.0 / 15.0
            return clock["t"]

        def cleanup() -> None:
            driver.stop()

        return driver, camera, estimator, sensor, sleep, monotonic, cleanup, "  [DRY RUN — fakes]"

    from .. import config
    from ..camera import Camera
    from .calibration import load_if_present
    from .hardware import GpiozeroMotorDriver, make_ultrasonic_sensor
    from .visual_odometry import YawEstimator

    cal = load_if_present()
    if cal is not None:
        driver = GpiozeroMotorDriver(
            left_invert=cal.left_invert, right_invert=cal.right_invert,
            left_trim=cal.left_trim, right_trim=cal.right_trim,
        )
        hfov = float(getattr(cal, "camera_hfov_deg", 0.0) or config.CAMERA_HFOV_DEG)
    else:
        driver = GpiozeroMotorDriver()
        hfov = config.CAMERA_HFOV_DEG
    sensor = make_ultrasonic_sensor()
    camera = Camera(source=camera_source)
    camera.start()
    if camera.wait_for_frame(timeout=6.0) is None:
        print("  WARNING: no camera frame within 6 s — yaw measurement will be blind.")
    estimator = YawEstimator(hfov_deg=hfov)

    def cleanup() -> None:
        for fn in (driver.stop, camera.stop, sensor.close, driver.close):
            try:
                fn()
            except Exception:
                pass

    note = (
        f"  [REAL GPIO — {type(driver).__name__}, {type(sensor).__name__}, "
        f"camera {camera_source!r}, HFOV {hfov:.1f} deg]"
    )
    return driver, camera, estimator, sensor, time.sleep, time.monotonic, cleanup, note


def _check_wheels(
    *,
    dry_run: bool,
    camera_source: str,
    seconds: Optional[float] = None,
    hz: float = 3.0,
    matrix: bool = False,
    save: Optional[str] = None,
    duties=None,
    reverse_first: float = 0.0,
) -> int:
    """Camera-measured per-wheel yaw-rate matrix (see drive_diagnostics)."""
    from .drive_diagnostics import format_wheel_matrix, measure_wheel_matrix

    duties = tuple(float(d) for d in (duties or (0.45, 0.60)))
    print(f"Wheel matrix: single-wheel pulses at duties {', '.join(f'{d:.2f}' for d in duties)}.")
    print("  *** ON THE FLOOR with ~0.5 m clear ALL ROUND — she will pivot each way ***")
    print("  The camera measures the yaw rate of each pulse (+ = LEFT).")
    print()
    try:
        driver, camera, estimator, sensor, sleep, monotonic, cleanup, note = _floor_rig(
            dry_run, camera_source
        )
    except Exception as exc:
        print(f"  ERROR: could not build the floor rig — {exc}")
        return 1
    print(note)
    try:
        results = measure_wheel_matrix(
            driver, camera, estimator, sensor, duties=duties, sleep=sleep, monotonic=monotonic,
        )
    finally:
        cleanup()
    print(format_wheel_matrix(results))
    bad = [p for p in results if p.sign_ok is False]
    return 1 if bad else 0


def _check_straight(
    *,
    dry_run: bool,
    camera_source: str,
    seconds: Optional[float] = None,
    hz: float = 3.0,
    matrix: bool = False,
    save: Optional[str] = None,
    duties=None,
    reverse_first: float = 0.0,
) -> int:
    """Straight-drive recorder: yaw + sonar per camera frame (see drive_diagnostics)."""
    from .drive_diagnostics import format_straight, measure_straight, write_straight_csv

    duty = float((duties or (0.45,))[0])
    secs = float(seconds) if seconds is not None else 3.0
    print(f"Straight-drive recorder: duty {duty:.2f} for up to {secs:.1f} s.")
    print("  *** ON THE FLOOR with a clear lane ahead; the sonar guard stops at the SAFE_STOP threshold ***")
    print()
    try:
        driver, camera, estimator, sensor, sleep, monotonic, cleanup, note = _floor_rig(
            dry_run, camera_source
        )
    except Exception as exc:
        print(f"  ERROR: could not build the floor rig — {exc}")
        return 1
    print(note)
    try:
        result = measure_straight(
            driver, camera, estimator, sensor, duty=duty, seconds=secs,
            reverse_first_s=float(reverse_first or 0.0), sleep=sleep, monotonic=monotonic,
        )
    finally:
        cleanup()
    print(format_straight(result))
    if save:
        try:
            write_straight_csv(result, save)
            print(f"  per-frame CSV written -> {save}")
        except Exception as exc:
            print(f"  WARNING: could not write CSV to {save!r} ({exc})")
    return 0


# ---------------------------------------------------------------------------
# Optional closed-loop sensors (2026-09-16): encoders / IMU / pack monitor
# ---------------------------------------------------------------------------
def _check_encoders(
    *,
    dry_run: bool,
    camera_source: str,
    seconds: Optional[float] = None,
    hz: float = 3.0,
    matrix: bool = False,
    save: Optional[str] = None,
) -> int:
    """Live wheel-encoder counts: spin each wheel by hand and watch the ticks."""
    from .. import config

    secs = float(seconds) if seconds is not None else 8.0
    print(f"Encoder test: printing tick counts for {secs:.0f} s at ~5 Hz.")
    print("  ON A STAND: spin the LEFT wheel forward by hand, then the RIGHT.")
    print("  Forward must count UP; backward DOWN. One full turn = "
          f"{config.ENCODER_TICKS_PER_WHEEL_REV} ticks (x4 decoding) = "
          f"{3.14159 * config.ENCODER_WHEEL_DIAMETER_M:.3f} m.")
    print()
    if dry_run:
        from .encoders import FakeWheelEncoders

        enc = FakeWheelEncoders()
        enc.set_speeds(0.10, -0.05)
        note = "  [DRY RUN — FakeWheelEncoders, left +0.10 m/s, right -0.05 m/s]"
        sleeper = lambda s: enc.step(s)  # noqa: E731 - advance the fake instead of waiting
        n = 6
    else:
        try:
            from .encoders import GpiodWheelEncoders

            enc = GpiodWheelEncoders(
                left_a_pin=config.ENCODER_LEFT_A_PIN, left_b_pin=config.ENCODER_LEFT_B_PIN,
                right_a_pin=config.ENCODER_RIGHT_A_PIN, right_b_pin=config.ENCODER_RIGHT_B_PIN,
                ticks_per_wheel_rev=config.ENCODER_TICKS_PER_WHEEL_REV,
                wheel_diameter_m=config.ENCODER_WHEEL_DIAMETER_M,
                track_width_m=config.ENCODER_TRACK_WIDTH_M,
                left_invert=config.ENCODER_LEFT_INVERT, right_invert=config.ENCODER_RIGHT_INVERT,
            )
        except Exception as exc:
            print(f"  ERROR: could not open the encoders — {exc}")
            print("  (pins left A/B GPIO16/26, right A/B GPIO20/21; encoder VCC on 3V3)")
            return 1
        note = "  [REAL GPIO — GpiodWheelEncoders]"
        sleeper = time.sleep
        n = max(1, int(secs * 5))
    print(note)
    try:
        for i in range(n):
            smp = enc.read()
            print(
                f"  t={i/5:4.1f}s  L {smp.left_ticks:+7d} ticks {smp.left_m:+.3f} m {smp.left_mps:+.2f} m/s   "
                f"R {smp.right_ticks:+7d} ticks {smp.right_m:+.3f} m {smp.right_mps:+.2f} m/s   "
                f"hdg {smp.heading_delta_deg:+6.1f} deg  ok={smp.ok}"
            )
            sleeper(0.2)
        stats = enc.stats() if hasattr(enc, "stats") else {}
        if stats:
            print(f"  stats: {stats}")
        smp = enc.read()
        if not dry_run and smp.left_ticks == 0 and smp.right_ticks == 0:
            print("  !! no ticks at all — check encoder VCC (3V3), GND, and the A/B pins.")
            return 1
    finally:
        try:
            enc.close()
        except Exception:
            pass
    return 0


def _check_imu(
    *,
    dry_run: bool,
    camera_source: str,
    seconds: Optional[float] = None,
    hz: float = 3.0,
    matrix: bool = False,
    save: Optional[str] = None,
) -> int:
    """MPU-6050: gyro bias calibration, then live yaw rate + integrated heading."""
    from .. import config

    secs = float(seconds) if seconds is not None else 8.0
    print(f"IMU test: calibrating the gyro bias (hold her STILL), then {secs:.0f} s of live yaw.")
    print("  Turn her LEFT by hand: the heading must go POSITIVE (+ = LEFT/CCW).")
    print("  If it goes negative, set YALP_IMU_YAW_SIGN=-1 (or YALP_IMU_YAW_AXIS if mounted on its side).")
    print()
    if dry_run:
        from .imu import FakeImu

        clock = {"t": 0.0}

        def _clock() -> float:
            return clock["t"]

        imu = FakeImu(yaw_rates=[0.0] * 5 + [30.0] * 20 + [0.0] * 5, monotonic=_clock)
        note = "  [DRY RUN — FakeImu, scripted +30 deg/s burst]"

        def sleeper(s: float) -> None:  # advance the fake clock instead of waiting
            clock["t"] += s

        n = 30
    else:
        try:
            from .imu import Mpu6050Imu

            imu = Mpu6050Imu(
                bus=config.IMU_I2C_BUS, address=config.IMU_I2C_ADDRESS,
                yaw_axis=config.IMU_YAW_AXIS, yaw_sign=config.IMU_YAW_SIGN,
            )
        except Exception as exc:
            print(f"  ERROR: could not open the MPU-6050 — {exc}")
            print("  (I2C-1: SDA pin 3, SCL pin 5, 3V3, GND; `sudo raspi-config nonint do_i2c 0`; "
                  "`i2cdetect -y 1` should show 68; pip install smbus2 in the venv)")
            return 1
        note = "  [REAL I2C — Mpu6050Imu]"
        sleeper = time.sleep
        n = max(1, int(secs * 10))
    print(note)
    try:
        try:
            bias = imu.calibrate_gyro(samples=config.IMU_CALIBRATION_SAMPLES if not dry_run else 10)
            print(f"  gyro bias (deg/s): {bias}")
        except Exception as exc:
            print(f"  WARNING: bias calibration failed — {exc}")
        heading = 0.0
        last_ts = None
        for i in range(n):
            smp = imu.read()
            if last_ts is not None and smp.ts > last_ts:
                heading += smp.yaw_rate_dps * (smp.ts - last_ts)
            last_ts = smp.ts
            if i % 5 == 0:
                print(f"  t={i/10:4.1f}s  yaw rate {smp.yaw_rate_dps:+7.1f} deg/s  heading {heading:+7.1f} deg  ok={smp.ok}")
            sleeper(0.1)
    finally:
        try:
            imu.close()
        except Exception:
            pass
    return 0


def _check_power(
    *,
    dry_run: bool,
    camera_source: str,
    seconds: Optional[float] = None,
    hz: float = 3.0,
    matrix: bool = False,
    save: Optional[str] = None,
) -> int:
    """INA219: pack voltage / current / a rough NiMH state."""
    from .. import config

    print("Power test: reading the INA219 pack monitor 5 times.")
    if dry_run:
        from .power_monitor import FakePowerMonitor

        mon = FakePowerMonitor(voltage_v=5.4, current_a=0.35)
        note = "  [DRY RUN — FakePowerMonitor]"
    else:
        try:
            from .power_monitor import Ina219PowerMonitor

            mon = Ina219PowerMonitor(
                bus=config.IMU_I2C_BUS, address=config.POWER_MONITOR_I2C_ADDRESS,
                shunt_ohms=config.POWER_MONITOR_SHUNT_OHMS,
            )
        except Exception as exc:
            print(f"  ERROR: could not open the INA219 — {exc}")
            print("  (I2C-1 shared with the IMU; addr 0x40; pack + -> VIN+, VIN- -> VM/J1)")
            return 1
        note = "  [REAL I2C — Ina219PowerMonitor]"
    print(note)
    try:
        from .power_monitor import nimh_pack_state

        for i in range(5):
            smp = mon.read()
            state = nimh_pack_state(smp.bus_voltage_v)
            print(f"  read {i+1}: {smp.bus_voltage_v:5.2f} V  {smp.current_a:+6.2f} A  {smp.power_w:5.2f} W  pack={state}  ok={smp.ok}")
            if not dry_run:
                time.sleep(0.2)
        if not dry_run and not smp.ok:
            return 1
    finally:
        try:
            mon.close()
        except Exception:
            pass
    return 0
