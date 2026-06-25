"""``yalp bench`` — the combined-load Gate K / Gate H measurement harness.

Gates **K** and **H** (roadmap.md) are the measured GO/NO-GO decisions that decide
whether the robot's reactive loop is viable *under real, concurrent load*:

  * **Gate K** — does the SAFETY LOOP hold its deadline while everything else runs?
    The reactive tick must stay under the per-tick budget (``config.TICK_BUDGET_MS``,
    ~33 ms at 30 Hz) at the **p99** so collision-stop never starves. We drive the
    backend in FOLLOW (tracker + detector + steering + motor writes all live —
    i.e. the heaviest steady-state mode) and record EVERY tick's wall time, then
    check ``p99 < budget``.
  * **Gate H** — does the chosen detector sustain enough throughput for
    track-by-detection? We count detector invocations across the run to compute a
    SUSTAINED detector fps and compare it to ``config.GATE_H_GO_HZ``. Below the
    floor we'd swap in a cheaper blob/color detector behind the same ``Detector``
    interface.

Both gates must be measured under CONCURRENT load, which is exactly what FOLLOW
mode gives us: the camera capture thread, the (slow) detector, the cheap
between-detection tracker, the steering controller, and the motor writes all run
together while we tick as fast as we can.

This harness drives the REAL backend contract (``--backend real``; on a laptop the
GPIO drivers are unavailable so it transparently falls back to the in-repo fakes,
and ``--backend fake`` uses the fully simulated backend) and just reports the
numbers. It runs **headless** on a laptop with the synthetic camera, so CI can
exercise it end-to-end — the numbers it prints there are a laptop CEILING, not the
Pi gate verdict (which must be re-measured on the Pi).

Registered via the documented feature-module contract (``add_parser`` / ``run``)
and listed in ``yalp.cli.FEATURE_MODULES``.
"""

from __future__ import annotations

import math
import time
from typing import List, Optional, Tuple


# --------------------------------------------------------------------------- #
# CLI registration
# --------------------------------------------------------------------------- #
def add_parser(subparsers) -> None:
    """Register the ``bench`` subcommand and its flags."""
    from .. import config

    parser = subparsers.add_parser(
        "bench",
        help="Combined-load Gate K (tick deadline) + Gate H (detector fps) harness.",
        description=(
            "Drive the reactive backend in FOLLOW mode (tracker + detector + "
            "steering + live motor writes — the heaviest steady-state load) for "
            "--seconds, recording every tick's wall time and counting detector "
            "invocations. Prints tick p50/p95/p99, sustained detector fps, and the "
            "Gate K / Gate H GO/NO-GO verdicts. Runs headless on a laptop with the "
            "synthetic camera (the printed numbers are then a laptop ceiling, NOT "
            "the Pi gate verdict)."
        ),
    )
    parser.add_argument(
        "--seconds",
        type=float,
        default=20.0,
        metavar="N",
        help="How long to tick under load while measuring (default: 20).",
    )
    parser.add_argument(
        "--hz",
        type=float,
        default=None,  # resolved to config.REACTIVE_TICK_HZ
        metavar="HZ",
        help=(
            "Nominal reactive tick rate fed to the backend (default: "
            f"config.REACTIVE_TICK_HZ = {config.REACTIVE_TICK_HZ}). The harness "
            "ticks as fast as it can; this only sets the backend's per-tick dt."
        ),
    )
    parser.add_argument(
        "--detector",
        default=None,  # resolved to config.FOLLOW_DETECTOR_DEFAULT
        metavar="NAME",
        help=(
            "Detector under test: face / hog / person / auto (default: "
            f"config.FOLLOW_DETECTOR_DEFAULT = {config.FOLLOW_DETECTOR_DEFAULT!r})."
        ),
    )
    parser.add_argument(
        "--resolution",
        default=None,
        metavar="WxH",
        help="Capture resolution, e.g. 320x240 (default: the camera's native size).",
    )
    parser.add_argument(
        "--backend",
        choices=("fake", "real"),
        default="fake",
        help=(
            "Which backend to drive: 'fake' (fully simulated) or 'real' (the on-Pi "
            "RealReactiveBackend; on a laptop with no GPIO it falls back to the "
            "in-repo fake motor/range drivers). Default: fake."
        ),
    )
    parser.add_argument(
        "--camera-source",
        default="webcam",
        metavar="SRC",
        help="Camera source: webcam / image / synthetic (default: webcam).",
    )
    parser.set_defaults(handler=run)


# --------------------------------------------------------------------------- #
# Pure verdict logic (unit-tested in isolation)
# --------------------------------------------------------------------------- #
def gate_verdicts(
    p99_ms: float,
    fps: float,
    *,
    tick_budget_ms: Optional[float] = None,
    gate_h_go_hz: Optional[float] = None,
) -> dict:
    """Turn measured numbers into Gate K / Gate H verdicts (pure, side-effect-free).

    Parameters
    ----------
    p99_ms:
        The 99th-percentile per-tick wall time, in milliseconds.
    fps:
        The SUSTAINED detector throughput (detector invocations / runtime).
    tick_budget_ms / gate_h_go_hz:
        Thresholds; default to ``config.TICK_BUDGET_MS`` / ``config.GATE_H_GO_HZ``.

    Returns a dict with both verdicts:

      * **Gate K** PASSes when ``p99_ms < tick_budget_ms`` (the safety loop holds
        its deadline under load); otherwise NO-GO with a recovery hint.
      * **Gate H** is GO when ``fps >= gate_h_go_hz`` (track-by-detection is
        viable); otherwise NO-GO with a hint to swap in a cheaper detector.
    """
    from .. import config

    budget = float(config.TICK_BUDGET_MS if tick_budget_ms is None else tick_budget_ms)
    go_hz = float(config.GATE_H_GO_HZ if gate_h_go_hz is None else gate_h_go_hz)

    k_pass = float(p99_ms) < budget
    h_go = float(fps) >= go_hz

    return {
        "tick_budget_ms": budget,
        "gate_h_go_hz": go_hz,
        # --- Gate K: safety-loop deadline under load.
        "gate_k_pass": k_pass,
        "gate_k_verdict": "PASS" if k_pass else "NO-GO",
        "gate_k_hint": (
            None
            if k_pass
            else (
                "tick p99 over budget: lower --hz, shrink --resolution, raise the "
                "detector's re-detect interval, or move the detector OFF the tick "
                "(run it on a worker thread) so the safety loop holds >= 30 Hz."
            )
        ),
        # --- Gate H: detector throughput / track-by-detection viability.
        "gate_h_go": h_go,
        "gate_h_verdict": "GO" if h_go else "NO-GO",
        "gate_h_hint": (
            "track-by-detection viable at this detector throughput."
            if h_go
            else (
                "sustained detector fps below the floor: swap in a cheaper "
                "blob/color Detector (or a faster model) behind the Detector "
                "interface, or lower the detect cadence."
            )
        ),
    }


# --------------------------------------------------------------------------- #
# Percentiles (pure)
# --------------------------------------------------------------------------- #
def percentile(values: List[float], pct: float) -> float:
    """Linear-interpolated percentile of ``values`` (``pct`` in 0..100)."""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    k = (len(ordered) - 1) * (pct / 100.0)
    lo = math.floor(k)
    hi = math.ceil(k)
    if lo == hi:
        return float(ordered[int(k)])
    return float(ordered[lo] * (hi - k) + ordered[hi] * (k - lo))


# --------------------------------------------------------------------------- #
# Detector invocation counter (counts the SUSTAINED detector fps under load)
# --------------------------------------------------------------------------- #
class _CountingDetector:
    """Wrap a real :class:`Detector` and count how often ``detect`` is invoked.

    Track-by-detection only runs the (slow) detector every few ticks; counting
    actual ``detect`` calls across the run is the honest way to get the SUSTAINED
    detector fps (Gate H), as opposed to the cheap between-detection tracker rate.
    """

    def __init__(self, inner) -> None:
        from .. import config

        self._inner = inner
        self.count = 0
        self.detect_width = getattr(inner, "detect_width", config.FOLLOW_DETECT_WIDTH)

    @property
    def model_name(self) -> str:
        return type(self._inner).__name__

    def detect(self, frame):
        self.count += 1
        return self._inner.detect(frame)


# --------------------------------------------------------------------------- #
# Backend / tracker construction
# --------------------------------------------------------------------------- #
def _parse_resolution(text: Optional[str]) -> Optional[Tuple[int, int]]:
    """Parse ``"320x240"`` -> ``(320, 240)`` (``None`` for the camera default)."""
    if not text:
        return None
    parts = str(text).lower().replace(" ", "").split("x")
    if len(parts) != 2:
        raise ValueError(f"bad --resolution {text!r} (expected WxH, e.g. 320x240)")
    return int(parts[0]), int(parts[1])


def _build_counting_tracker(detector: str):
    """Build a FOLLOW tracker whose detector is wrapped to count invocations.

    Reuses :func:`follow_runner.build_follow_tracker` (so the detector + re-detect
    cadence match a live ``yalp follow``), then wraps the detector with a counter.
    Returns ``(tracker, counter)``; ``counter`` is ``None`` if the tracker fell
    back to its lazy default (e.g. OpenCV missing), in which case detector fps is
    reported as unavailable.
    """
    from .follow_runner import build_follow_tracker

    tracker = build_follow_tracker(detector)
    counter = None
    inner = getattr(tracker, "_detector", None) if tracker is not None else None
    if inner is not None:
        counter = _CountingDetector(inner)
        tracker._detector = counter  # type: ignore[attr-defined]
    return tracker, counter


def _build_backend(*, backend: str, camera, hz: float, tracker):
    """Build the chosen backend in (about-to-be) FOLLOW mode. Returns (backend, hw_label)."""
    if backend == "fake":
        from .fake_backend import FakeReactiveBackend

        return (
            FakeReactiveBackend(camera=camera, tick_hz=hz, tracker=tracker),
            "fake wheels (simulated)",
        )

    # backend == "real": drive the on-Pi RealReactiveBackend. On a laptop the
    # gpiozero/lgpio drivers are unavailable, so fall back to the in-repo fake
    # motor/range drivers — motor writes still happen (live), just into a fake.
    from .real_backend import RealReactiveBackend

    try:
        be = RealReactiveBackend(camera=camera, tick_hz=hz, tracker=tracker)
        return be, "real GPIO (live motors)"
    except Exception as exc:  # gpiozero/lgpio missing (laptop / CI)
        from .hardware import FakeMotorDriver, FakeRangeSensor

        print(
            f"[real backend: no GPIO ({type(exc).__name__}) — using fake "
            f"motor/range drivers; this is a LAPTOP run]"
        )
        be = RealReactiveBackend(
            camera=camera,
            tick_hz=hz,
            tracker=tracker,
            motor_driver=FakeMotorDriver(),
            range_sensor=FakeRangeSensor(),
        )
        return be, "real backend + fake hw (laptop)"


# --------------------------------------------------------------------------- #
# Handler
# --------------------------------------------------------------------------- #
def run(args) -> int:
    """Handler for ``yalp bench``. Returns a process exit code."""
    from .. import config
    from ..camera import Camera
    from ..contract.messages import Intent, Mode

    seconds = max(0.1, float(getattr(args, "seconds", 20.0) or 20.0))
    hz = float(getattr(args, "hz", None) or config.REACTIVE_TICK_HZ)
    detector = getattr(args, "detector", None) or config.FOLLOW_DETECTOR_DEFAULT
    backend_kind = getattr(args, "backend", "fake")
    source = getattr(args, "camera_source", "webcam")
    try:
        resolution = _parse_resolution(getattr(args, "resolution", None))
    except ValueError as exc:
        print(str(exc))
        return 2

    # Camera (the reactive layer owns it). Resolution drives the detector cost.
    cam_kwargs = {}
    if resolution is not None:
        cam_kwargs = {"width": resolution[0], "height": resolution[1]}
    camera = Camera(source=source, **cam_kwargs)
    res_label = f"{camera.width}x{camera.height}"

    tracker, counter = _build_counting_tracker(detector)
    backend, hw_label = _build_backend(
        backend=backend_kind, camera=camera, hz=hz, tracker=tracker
    )
    det_model = counter.model_name if counter is not None else "default (uncounted)"
    det_width = counter.detect_width if counter is not None else config.FOLLOW_DETECT_WIDTH

    print(
        f"yalp bench — combined-load Gate K / Gate H harness\n"
        f"  backend   : {backend_kind} — {hw_label}\n"
        f"  detector  : {detector} ({det_model} @ {det_width}px)\n"
        f"  camera    : source={source}, resolution={res_label}\n"
        f"  load      : FOLLOW mode (tracker + detector + steering + motor writes)\n"
        f"  measuring : ~{seconds:.1f}s at nominal {hz:g} Hz\n"
    )

    backend.start()
    # Enter FOLLOW so the tracker + detector + steering all run under load.
    backend.apply_intent(Intent(Mode.FOLLOW, {"target": "nearest_person"}, seq=1))
    # Warm up: make sure a frame exists, then tick once to ADOPT FOLLOW (so the
    # adopt tick isn't counted as a measured tick), and reset the detector count.
    try:
        backend.camera().wait_for_frame(timeout=5.0)
        backend.tick()
    except Exception:  # pragma: no cover - defensive warm-up
        pass
    if counter is not None:
        counter.count = 0

    samples: List[float] = []
    t_start = time.perf_counter()
    deadline = t_start + seconds
    try:
        while time.perf_counter() < deadline:
            t0 = time.perf_counter()
            backend.tick()
            samples.append((time.perf_counter() - t0) * 1000.0)
    finally:
        elapsed = time.perf_counter() - t_start
        backend.stop()

    if not samples:
        print("no ticks recorded — nothing to measure.")
        return 1

    p50 = percentile(samples, 50)
    p95 = percentile(samples, 95)
    p99 = percentile(samples, 99)
    det_fps = (counter.count / elapsed) if (counter is not None and elapsed > 0) else 0.0
    det_fps_label = f"{det_fps:.1f}" if counter is not None else "n/a (uncounted)"

    verdicts = gate_verdicts(p99, det_fps)

    print(f"  ticks recorded            : {len(samples)} over {elapsed:.2f}s")
    print(f"  tick p50 / p95 / p99 (ms) : {p50:.2f} / {p95:.2f} / {p99:.2f}")
    print(f"  detector sustained fps    : {det_fps_label} Hz "
          f"({counter.count if counter is not None else 0} invocations)")
    print()
    # The triple: (detector model/name, resolution, runtime).
    print(f"  config triple             : detector={detector}/{det_model}, "
          f"resolution={res_label}, runtime={elapsed:.2f}s")
    print()
    print(
        f"  Gate K (tick p99 < {verdicts['tick_budget_ms']:g} ms budget) : "
        f"{verdicts['gate_k_verdict']}  ({p99:.2f} ms vs "
        f"{verdicts['tick_budget_ms']:g} ms)"
    )
    if verdicts["gate_k_hint"]:
        print(f"      hint: {verdicts['gate_k_hint']}")
    print(
        f"  Gate H (detector fps >= {verdicts['gate_h_go_hz']:g} Hz)      : "
        f"{verdicts['gate_h_verdict']}  ({det_fps:.1f} Hz vs "
        f"{verdicts['gate_h_go_hz']:g} Hz)"
    )
    if verdicts["gate_h_hint"]:
        print(f"      hint: {verdicts['gate_h_hint']}")
    print(
        "\n  NOTE: laptop run (synthetic/webcam, OpenCV, no NPU) — these numbers are\n"
        "  a CEILING, not the Pi verdict. Gates K and H must be re-measured ON THE\n"
        "  PI under concurrent load (--backend real on the robot)."
    )
    return 0


__all__ = ["add_parser", "run", "gate_verdicts", "percentile"]
