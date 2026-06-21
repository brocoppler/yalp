"""``yalp follow`` — run FOLLOW mode against the real webcam + fake wheels.

Registered with the CLI via the documented feature-module contract: this module
exposes ``add_parser(subparsers)`` and ``run(args) -> int``, and is listed in
``yalp.cli.FEATURE_MODULES``.

    yalp follow                 # follow a real person seen on the real webcam
    yalp follow --seconds 10    # auto-stop after 10 s
    yalp follow --preview       # show a bbox/steering overlay window (if a display)
    yalp follow --synthetic     # no-camera demo / reproducible run (test pattern)
    yalp follow --benchmark     # print the LAPTOP fps baseline vs Gate H (3 Hz)

REAL EYES + FAKE WHEELS (software-spec.md §4 THESIS): the body is simulated but
vision comes from the real webcam. The reactive backend owns ONE shared camera,
runs a track-by-detection :class:`~yalp.reactive.person_tracker.PersonTracker`,
and the simulated wheels steer toward the person. ``--benchmark`` de-risks Gate H
(roadmap.md) by measuring the laptop's sustained detector / tracker / combined
FOLLOW-tick rates before the Pi arrives (the Pi will be slower — this is the
laptop ceiling).
"""

from __future__ import annotations

import time
from typing import Optional

from . import config
from .contract.messages import Intent, Mode


def add_parser(subparsers) -> None:
    """Register the ``follow`` subcommand and its flags."""
    parser = subparsers.add_parser(
        "follow",
        help="Follow a real person on the webcam (simulated wheels steer toward them).",
        description=(
            "Run FOLLOW mode (track-by-detection) against the real webcam and the "
            "fake reactive backend: the simulated wheels steer toward a real person. "
            "Prints a per-tick steering decision. --benchmark prints the laptop fps "
            "baseline vs the Gate H threshold; --preview shows an overlay window "
            "when a display is available (headless-safe)."
        ),
    )
    parser.add_argument(
        "--seconds",
        type=float,
        default=None,
        metavar="N",
        help="Auto-stop after N seconds (default: run until Ctrl-C).",
    )
    parser.add_argument(
        "--hz",
        type=float,
        default=config.REACTIVE_TICK_HZ,
        metavar="HZ",
        help=f"FOLLOW tick rate (default: {config.REACTIVE_TICK_HZ:g}).",
    )
    parser.add_argument(
        "--preview",
        action="store_true",
        help="Show an OpenCV window with the bbox + steering overlay if a display "
             "is available (default off; headless-safe — never crashes with no display).",
    )
    parser.add_argument(
        "--synthetic",
        action="store_true",
        help="Force the synthetic camera test-pattern (no-camera demo / CI). "
             "Default: real webcam, auto-falling back to synthetic if none opens.",
    )
    parser.add_argument(
        "--benchmark",
        action="store_true",
        help="Measure and print the laptop fps baseline (detector / tracker / "
             "combined FOLLOW tick) and compare against Gate H, then exit.",
    )
    parser.set_defaults(handler=run)


def run(args) -> int:
    """Handler for ``yalp follow``. Returns a process exit code."""
    from .reactive.fake_backend import FakeReactiveBackend

    source = "synthetic" if getattr(args, "synthetic", False) else "webcam"
    backend = FakeReactiveBackend(camera_source=source, tick_hz=args.hz)
    backend.start()
    try:
        if getattr(args, "benchmark", False):
            return _benchmark(backend)
        return _live(backend, args)
    finally:
        backend.stop()


# --- live FOLLOW loop --------------------------------------------------------
def _live(backend, args) -> int:
    """Tick FOLLOW and print a per-tick decision line until stopped."""
    # Hand the backend a FOLLOW intent (exactly what the agent's enter_follow_mode
    # sends) so the reactive loop enters FOLLOW on the next tick.
    backend.apply_intent(Intent(Mode.FOLLOW, {"target": "nearest_person"}, seq=1))

    preview = _Preview() if getattr(args, "preview", False) else None
    seconds: Optional[float] = getattr(args, "seconds", None)
    dt = 1.0 / max(1.0, args.hz)
    deadline = (time.monotonic() + seconds) if seconds else None

    print(f"yalp follow — FOLLOW mode (source={backend.camera().source}). "
          f"{'Stopping after %.0fs.' % seconds if seconds else 'Ctrl-C to stop.'}")
    try:
        while deadline is None or time.monotonic() < deadline:
            t0 = time.monotonic()
            state = backend.tick()
            print(_tick_line(state))
            if preview is not None:
                preview.show(backend.camera().latest(), state)
            elapsed = time.monotonic() - t0
            if dt > elapsed:
                time.sleep(dt - elapsed)
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        if preview is not None:
            preview.close()
    return 0


def _tick_line(state) -> str:
    """One human-readable line per tick describing the steering decision."""
    if state.blocked:
        return "SAFE_STOP (obstacle) -> stop"
    goal = state.goal or {}
    status = goal.get("status")
    if status:
        return status
    # Non-FOLLOW or pre-adoption tick.
    return f"{getattr(state.mode, 'value', state.mode)} (no follow decision yet)"


# --- benchmark (Gate H de-risk) ---------------------------------------------
def _rate(fn, window: float) -> float:
    """Call ``fn`` repeatedly for ``window`` seconds; return iterations/second."""
    n = 0
    t0 = time.monotonic()
    end = t0 + window
    while time.monotonic() < end:
        fn()
        n += 1
    elapsed = time.monotonic() - t0
    return (n / elapsed) if elapsed > 0 else 0.0


def _benchmark(backend, *, window: float = 2.0) -> int:
    """Print the laptop detector / tracker / combined FOLLOW-tick fps vs Gate H."""
    from .reactive.person_tracker import HOGPersonDetector, make_box_tracker

    cam = backend.camera()
    frame = cam.wait_for_frame(timeout=5.0)
    if frame is None:
        print("benchmark: no frame available from the camera.")
        return 1

    h, w = frame.shape[:2]
    print(f"yalp follow --benchmark (source={cam.source}, frame={w}x{h}, "
          f"window={window:g}s each)\n")

    # 1) Detector throughput in isolation (the Gate H number).
    detector = HOGPersonDetector()
    detector.detect(frame)  # warm the descriptor (first call builds it)
    det_fps = _rate(lambda: detector.detect(frame), window)

    # 2) Cheap box-tracker throughput.
    tracker = make_box_tracker()
    seed = (w // 3, h // 4, w // 4, h // 2)
    tracker.init(frame, seed)
    trk_fps = _rate(lambda: tracker.update(frame), window)

    # 3) Combined FOLLOW reactive-tick rate (detector amortized over detect_every).
    backend.apply_intent(Intent(Mode.FOLLOW, {"target": "nearest_person"}, seq=1))
    backend.tick()
    tick_fps = _rate(backend.tick, window)

    tracker_kind = type(tracker).__name__
    print(f"  detector (HOG, downscaled)   : {det_fps:6.2f} Hz")
    print(f"  tracker  ({tracker_kind:<18}): {trk_fps:8.1f} Hz")
    print(f"  combined FOLLOW tick rate    : {tick_fps:6.2f} Hz")
    print()

    go = det_fps >= config.GATE_H_GO_HZ
    verdict = "GO" if go else "NO-GO"
    print(f"  Gate H threshold (GATE_H_GO_HZ) = {config.GATE_H_GO_HZ} Hz sustained")
    print(f"  detector {det_fps:.2f} Hz -> {verdict} "
          f"({'track-by-detection viable' if go else 'fall back to a blob/color tracker'}).")
    print("  NOTE: this is the LAPTOP ceiling. The Pi 5 (no NPU) will be slower; "
          "Gate H is measured on the Pi under concurrent load (roadmap.md §2.2).")
    return 0


# --- headless-safe preview ---------------------------------------------------
class _Preview:
    """Optional OpenCV overlay window. Never crashes when there is no display."""

    def __init__(self) -> None:
        self._cv2 = None
        self._ok = True
        try:
            import cv2  # noqa: F401

            self._cv2 = cv2
        except Exception as exc:  # pragma: no cover - opencv always present here
            print(f"[preview disabled: opencv import failed: {exc}]")
            self._ok = False

    def show(self, frame, state) -> None:
        if not self._ok or self._cv2 is None or frame is None:
            return
        cv2 = self._cv2
        try:
            img = frame.copy()
            bbox = state.target_bbox
            if state.target_visible and bbox is not None:
                x, y, w, h = (int(v) for v in bbox)
                cv2.rectangle(img, (x, y), (x + w, y + h), (0, 255, 0), 2)
            status = (state.goal or {}).get("status", "")
            cv2.putText(img, status, (8, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                        (0, 255, 0), 2, cv2.LINE_AA)
            cv2.imshow("yalp follow", img)
            cv2.waitKey(1)
        except Exception as exc:
            # No display (headless) or GUI build missing: disable, never crash.
            print(f"[preview disabled: no display ({type(exc).__name__})]")
            self._ok = False

    def close(self) -> None:
        if self._ok and self._cv2 is not None:
            try:
                self._cv2.destroyAllWindows()
            except Exception:
                pass


__all__ = ["add_parser", "run"]
