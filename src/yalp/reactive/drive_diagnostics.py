"""Camera-measured drivetrain diagnostics: per-wheel yaw-rate matrix + straight-drive recorder.

These two measurements are what found the 2026-09-15 root cause of "she veers"
(a DRV8833 decay-mode asymmetry between the two wheels — see
``as-built-wiring.md`` §3.6) after a day of guessing, so they are first-class
``yalp hwtest`` checks rather than a scratch script:

``wheels`` — the **per-wheel yaw-rate matrix**. Pulse ONE wheel at a time, in
each direction, at each requested duty, and measure how fast the robot yaws using
the camera (:class:`~yalp.reactive.visual_odometry.YawEstimator`). Driving one
wheel pivots the robot about the other, so the yaw rate is a clean proxy for
that wheel's effective drive *under load*. Reading the table:

* Forward: LEFT alone yaws RIGHT (negative), RIGHT alone yaws LEFT (positive).
  Reverse: the signs flip. A wrong sign = that wheel's polarity/inversion is
  wrong (``yalp calibrate`` or the ``*_invert`` flags).
* The two wheels' magnitudes should match to ~15%. A gap that FLIPS SIGN between
  duties is a decay-mode mismatch (``config.MOTOR_DECAY_MODE``); a constant gap is
  a genuinely weaker motor/wheel (the heading hold + learned bias absorb it, or
  set a trim); a dead entry is a stalled channel / a wire.

``straight`` — the **straight-drive recorder**. Drive both wheels at one duty
for N seconds (optionally backing up first to make room) while logging, per
camera frame, the yaw delta and confidence plus the sonar distance, with a sonar
guard that stops the run before the collision threshold. The integrated yaw is
the veer; the per-frame trace shows whether it is a steady bias (trim), a
launch kick (one wheel breaking stiction late), or a mid-run swing (a wire, a
stutter). ``--save`` writes the per-frame CSV.

Both functions take the driver, camera, estimator, sensor and clocks as
arguments so they run against fakes on a laptop (``yalp hwtest --dry-run``) and
in tests; ``yalp hwtest`` wires the real hardware.
"""

from __future__ import annotations

import csv
import math
import time
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional, Sequence, Tuple

from .. import config


@dataclass
class WheelPulse:
    """One single-wheel pulse and the yaw rate it produced."""

    wheel: str  # "left" | "right"
    direction: str  # "fwd" | "rev"
    duty: float
    yaw_rate_dps: float  # + = LEFT
    seconds: float
    frames: int
    stopped: str = ""  # non-empty when the sonar guard cut the pulse short

    @property
    def expected_sign(self) -> int:
        """Physics: a lone LEFT wheel forward yaws RIGHT (-); reverse flips."""
        s = -1 if self.wheel == "left" else 1
        return s if self.direction == "fwd" else -s

    @property
    def sign_ok(self) -> Optional[bool]:
        if abs(self.yaw_rate_dps) < 5.0:
            return None  # too small to call
        return (self.yaw_rate_dps > 0) == (self.expected_sign > 0)


@dataclass
class StraightSample:
    t: float
    delta_deg: float
    response: float
    distance_m: Optional[float]
    known: bool
    cum_yaw_deg: float


@dataclass
class StraightResult:
    duty: float
    seconds: float
    frames: int
    yaw_deg: float  # integrated, + = LEFT
    sonar_start_m: Optional[float]
    sonar_end_m: Optional[float]
    stopped: str = ""
    samples: List[StraightSample] = field(default_factory=list)

    @property
    def fps(self) -> float:
        return self.frames / self.seconds if self.seconds > 0 else 0.0

    @property
    def travel_m(self) -> Optional[float]:
        if self.sonar_start_m is None or self.sonar_end_m is None:
            return None
        return self.sonar_start_m - self.sonar_end_m

    @property
    def veer_deg_per_m(self) -> Optional[float]:
        d = self.travel_m
        if d is None or abs(d) < 0.05:
            return None
        return self.yaw_deg / abs(d)


class _FrameFeed:
    """Dedupe a camera's ``latest_with_id`` (or ``latest``) into NEW frames only."""

    def __init__(self, camera: Any) -> None:
        self._camera = camera
        self._last_id: Optional[int] = None
        self._last_obj: Any = None

    def next(self) -> Optional[Any]:
        getter = getattr(self._camera, "latest_with_id", None)
        if getter is not None:
            frame, fid = getter()
            if frame is None or fid == self._last_id:
                return None
            self._last_id = fid
            return frame
        frame = self._camera.latest()
        if frame is None or frame is self._last_obj:
            return None
        self._last_obj = frame
        return frame


def _read_sonar(sensor: Any) -> Tuple[Optional[float], bool]:
    if sensor is None:
        return None, False
    try:
        d, known = sensor.read_distance()
        return (float(d) if known else None), bool(known)
    except Exception:
        return None, False


def _pulse(
    driver: Any,
    camera: Any,
    estimator: Any,
    sensor: Any,
    *,
    left: float,
    right: float,
    seconds: float,
    guard_m: Optional[float],
    min_response: float,
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
    on_sample: Optional[Callable[[float, Any, Optional[float], bool], None]] = None,
) -> Tuple[float, float, int, str]:
    """Drive ``(left, right)`` for ``seconds``; return (yaw_deg, elapsed, frames, stopped)."""
    feed = _FrameFeed(camera)
    estimator.reset()
    first = feed.next()
    if first is not None:
        estimator.update(first)  # reference frame
    cum = 0.0
    frames = 0
    stopped = ""
    t0 = monotonic()
    driver.set_motors(left, right)
    try:
        while True:
            t = monotonic() - t0
            if t >= seconds:
                break
            frame = feed.next()
            dist, known = _read_sonar(sensor)
            if frame is not None:
                sample = estimator.update(frame)
                if sample is not None and sample.response >= min_response:
                    cum += float(sample.delta_deg)
                    frames += 1
                    if on_sample is not None:
                        on_sample(t, sample, dist, known)
            if guard_m is not None and sensor is not None:
                if known and dist is not None and dist < guard_m:
                    stopped = f"sonar guard {dist:.2f} m < {guard_m:.2f} m"
                    break
                if not known and guard_m > 0 and t > 0.3:
                    stopped = "sonar unknown"
                    break
            sleep(0.01)
    finally:
        driver.stop()
    elapsed = max(1e-6, monotonic() - t0)
    return cum, elapsed, frames, stopped


def measure_wheel_matrix(
    driver: Any,
    camera: Any,
    estimator: Any,
    sensor: Any = None,
    *,
    duties: Sequence[float] = (0.45, 0.60),
    pulse_s: float = 0.8,
    pause_s: float = 0.7,
    guard_m: float = 0.12,
    min_response: float = config.HEADING_HOLD_MIN_RESPONSE,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    log: Callable[[str], None] = print,
) -> List[WheelPulse]:
    """Pulse each wheel alone, each direction, at each duty; measure the yaw rate.

    Order per duty: LEFT rev, RIGHT rev, LEFT fwd, RIGHT fwd — reverse first so
    the forward pulses (the only ones the front sonar can guard) start with a
    little more room. Displacement roughly cancels over the four.
    """
    results: List[WheelPulse] = []
    for duty in duties:
        duty = float(duty)
        for wheel, direction in (("left", "rev"), ("right", "rev"), ("left", "fwd"), ("right", "fwd")):
            sign = 1.0 if direction == "fwd" else -1.0
            l, r = (sign * duty, 0.0) if wheel == "left" else (0.0, sign * duty)
            yaw, elapsed, frames, stopped = _pulse(
                driver, camera, estimator, sensor,
                left=l, right=r, seconds=pulse_s,
                guard_m=(guard_m if direction == "fwd" else None),
                min_response=min_response, sleep=sleep, monotonic=monotonic,
            )
            pulse = WheelPulse(wheel, direction, duty, yaw / elapsed, elapsed, frames, stopped)
            results.append(pulse)
            log(
                f"  duty {duty:.2f} {wheel:5s} {direction}: yaw rate {pulse.yaw_rate_dps:+7.1f} deg/s"
                f"  ({frames} frames)"
                + ("" if pulse.sign_ok is None else ("" if pulse.sign_ok else "  <-- WRONG SIGN"))
                + (f"  [{stopped}]" if stopped else "")
            )
            sleep(pause_s)
    return results


def format_wheel_matrix(results: Sequence[WheelPulse]) -> str:
    """The per-duty table + verdicts, as printed by ``yalp hwtest --check wheels``."""
    lines = ["", "|yaw rate| deg/s per wheel (a lone wheel pivots the robot about the other):",
             "  duty   LEFT fwd  RIGHT fwd   |  LEFT rev  RIGHT rev   verdict"]
    by_duty: dict = {}
    for p in results:
        by_duty.setdefault(p.duty, {})[(p.wheel, p.direction)] = p
    for duty in sorted(by_duty):
        row = by_duty[duty]

        def mag(w, d):
            p = row.get((w, d))
            return abs(p.yaw_rate_dps) if p else float("nan")

        lf, rf, lr, rr = mag("left", "fwd"), mag("right", "fwd"), mag("left", "rev"), mag("right", "rev")
        verdict = []
        if any(p.sign_ok is False for p in row.values()):
            verdict.append("WRONG SIGN on " + ",".join(f"{p.wheel}-{p.direction}" for p in row.values() if p.sign_ok is False))
        for a, b, name in ((lf, rf, "fwd"), (lr, rr, "rev")):
            if math.isfinite(a) and math.isfinite(b):
                hi, lo = max(a, b), min(a, b)
                if hi < 5.0:
                    verdict.append(f"{name}: no motion")
                elif lo < 0.5 * hi:
                    verdict.append(f"{name}: {'LEFT' if a < b else 'RIGHT'} wheel weak ({lo:.0f} vs {hi:.0f})")
                elif lo < 0.85 * hi:
                    verdict.append(f"{name}: {'LEFT' if a < b else 'RIGHT'} slightly weak")
        lines.append(f"  {duty:4.2f}   {lf:8.1f}  {rf:9.1f}   |  {lr:8.1f}  {rr:9.1f}   {'; '.join(verdict) or 'balanced'}")
    # Cross-duty check: an imbalance that flips sign between duties is the
    # decay-mode signature.
    duties = sorted(by_duty)
    if len(duties) >= 2:
        def gap(duty, d):
            row = by_duty[duty]
            a, b = row.get(("left", d)), row.get(("right", d))
            if not a or not b:
                return None
            return abs(a.yaw_rate_dps) - abs(b.yaw_rate_dps)
        for d in ("fwd", "rev"):
            gaps = [gap(x, d) for x in duties]
            gaps = [g for g in gaps if g is not None]
            if len(gaps) >= 2 and gaps[0] * gaps[-1] < 0 and min(abs(g) for g in (gaps[0], gaps[-1])) > 8:
                lines.append(
                    f"  NOTE: the {d} left/right gap FLIPS SIGN between duties {duties[0]:.2f} and "
                    f"{duties[-1]:.2f} — decay-mode mismatch signature (check config.MOTOR_DECAY_MODE)."
                )
    lines.append("  expected signs: L fwd -, R fwd +, L rev +, R rev -  (+ = yaw LEFT)")
    return "\n".join(lines)


def measure_straight(
    driver: Any,
    camera: Any,
    estimator: Any,
    sensor: Any = None,
    *,
    duty: float = 0.45,
    seconds: float = 3.0,
    stop_m: float = config.SAFE_STOP_THRESHOLD_M,
    reverse_first_s: float = 0.0,
    min_response: float = config.HEADING_HOLD_MIN_RESPONSE,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    log: Callable[[str], None] = print,
) -> StraightResult:
    """Drive straight at ``duty`` for ``seconds`` recording yaw + sonar per frame."""
    if reverse_first_s > 0:
        log(f"  backing up for {reverse_first_s:.1f} s at duty {duty:.2f} (no rear sensor — you are the guard)...")
        _pulse(driver, camera, estimator, sensor, left=-duty, right=-duty, seconds=reverse_first_s,
               guard_m=None, min_response=min_response, sleep=sleep, monotonic=monotonic)
        sleep(0.6)
    start, _ = _read_sonar(sensor)
    samples: List[StraightSample] = []
    cum = {"yaw": 0.0}

    def on_sample(t, sample, dist, known):
        cum["yaw"] += float(sample.delta_deg)
        samples.append(StraightSample(round(t, 3), float(sample.delta_deg), float(sample.response), dist, known, round(cum["yaw"], 3)))

    log(f"  driving straight at duty {duty:.2f} for up to {seconds:.1f} s (sonar guard at {stop_m:.2f} m)...")
    yaw, elapsed, frames, stopped = _pulse(
        driver, camera, estimator, sensor, left=duty, right=duty, seconds=seconds,
        guard_m=stop_m, min_response=min_response, sleep=sleep, monotonic=monotonic, on_sample=on_sample,
    )
    end, _ = _read_sonar(sensor)
    return StraightResult(duty, elapsed, frames, yaw, start, end, stopped, samples)


def format_straight(result: StraightResult) -> str:
    r = result
    s = [
        f"  {r.seconds:.2f} s, {r.frames} frames ({r.fps:.1f} fps), integrated yaw {r.yaw_deg:+.1f} deg (+ = LEFT)"
        + (f"  [{r.stopped}]" if r.stopped else ""),
    ]
    if r.sonar_start_m is not None or r.sonar_end_m is not None:
        s.append(
            f"  sonar {('%.2f' % r.sonar_start_m) if r.sonar_start_m is not None else '?'} -> "
            f"{('%.2f' % r.sonar_end_m) if r.sonar_end_m is not None else '?'} m"
            + (f"  (~{r.travel_m:.2f} m of travel, veer {r.veer_deg_per_m:+.1f} deg/m)" if r.veer_deg_per_m is not None else "")
        )
    # Character of the veer: launch kick vs steady drift vs mid-run swing.
    if len(r.samples) >= 6:
        n = len(r.samples)
        first = r.samples[max(0, n // 5) - 1].cum_yaw_deg if n >= 5 else 0.0
        mid = r.samples[n // 2].cum_yaw_deg
        last = r.samples[-1].cum_yaw_deg
        swing = max(abs(x.cum_yaw_deg) for x in r.samples)
        if abs(first) > 5 and abs(last - first) < 0.5 * abs(first):
            kind = "launch kick (one wheel broke free late) then roughly straight"
        elif swing > abs(last) + 8:
            kind = f"mid-run swing (peak {swing:.0f} deg) — a stutter, a wire, or a surface change"
        elif abs(last) > 3:
            kind = f"steady drift {'LEFT' if last > 0 else 'RIGHT'} (a static imbalance the bias/trim absorbs)"
        else:
            kind = "straight within noise"
        s.append(f"  character: {kind}  (cum yaw at 20%/50%/100%: {first:+.1f} / {mid:+.1f} / {last:+.1f} deg)")
    if abs(r.yaw_deg) > 10:
        s.append("  !! more than 10 deg of veer — run `yalp hwtest --check wheels` for the per-wheel picture")
    return "\n".join(s)


def write_straight_csv(result: StraightResult, path: str) -> None:
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t_s", "delta_deg", "response", "distance_m", "known", "cum_yaw_deg"])
        for x in result.samples:
            w.writerow([x.t, x.delta_deg, x.response, "" if x.distance_m is None else x.distance_m, int(x.known), x.cum_yaw_deg])


class DriverCoupledYaw:
    """A yaw-estimator double whose "camera" sees the wheel differential.

    For ``--dry-run`` and tests: ``update()`` returns a sample whose yaw delta is
    ``gain * (right - left) * dt`` from the driver's last command, so a
    single-wheel pulse produces the physically right sign and a straight drive
    produces zero — the diagnostics' logic runs end-to-end with no hardware.
    """

    def __init__(self, driver: Any, *, gain_dps: float = 200.0, dt: float = 1.0 / 15.0,
                 response: float = 0.9, left_scale: float = 1.0, right_scale: float = 1.0) -> None:
        self._driver = driver
        self.gain_dps = gain_dps
        self.dt = dt
        self.response = response
        self.left_scale = left_scale
        self.right_scale = right_scale
        self._primed = False

    def reset(self) -> None:
        self._primed = False

    def update(self, frame: Any):
        from .visual_odometry import YawSample

        if not self._primed:
            self._primed = True
            return None
        last = getattr(self._driver, "last", None) or (0.0, 0.0)
        left, right = float(last[0]) * self.left_scale, float(last[1]) * self.right_scale
        return YawSample(delta_deg=self.gain_dps * (right - left) * self.dt, dx_px=0.0, response=self.response)


class TickingFakeCamera:
    """A camera double that hands out a fresh frame id on every ``latest_with_id``."""

    def __init__(self) -> None:
        import numpy as np

        self._frame = np.zeros((12, 16, 3), dtype="uint8")
        self._id = 0

    def start(self):
        return self

    def stop(self) -> None:
        return None

    def latest(self):
        return self._frame

    def latest_with_id(self):
        self._id += 1
        return self._frame, self._id

    def wait_for_frame(self, timeout: float = 0.0):
        return self._frame


__all__ = [
    "WheelPulse", "StraightSample", "StraightResult",
    "measure_wheel_matrix", "format_wheel_matrix",
    "measure_straight", "format_straight", "write_straight_csv",
    "DriverCoupledYaw", "TickingFakeCamera",
]
