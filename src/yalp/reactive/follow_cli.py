"""``yalp follow`` — run FOLLOW mode live against the real webcam + fake wheels.

Registered with the CLI via the documented feature-module contract: this module
exposes ``add_parser(subparsers)`` and ``run(args) -> int`` and is listed in
``yalp.cli.FEATURE_MODULES``.

    yalp follow                 # follow a real person on the real webcam (Ctrl-C)
    yalp follow --seconds 10    # auto-stop after 10 s
    yalp follow --preview       # also show an OpenCV window (if a display exists)
    yalp follow --synthetic     # no-camera demo (synthetic frames; will report lost)
    yalp follow --benchmark     # print the laptop detector/tracker/FOLLOW fps baseline

REAL EYES + FAKE WHEELS: the wheels are simulated by ``FakeReactiveBackend`` but
the camera is real. Each tick grabs the latest frame from the reactive layer's
OWNED camera, runs the track-by-detection tracker, and steers toward the person
(turn toward the bbox center; drive forward until close). It degrades gracefully
— "lost -> stop" — when the person is lost/stale or the frame is too dark, and
collision-stop / SAFE_STOP overrides everything underneath.

``--benchmark`` de-risks **Gate H** (roadmap.md): it measures the *laptop* ceiling
for the person-detector fps (the OpenCV HOG detector here; on the Pi we'd swap in a
faster detector behind the same interface) and compares it to
``config.GATE_H_GO_HZ``. The Pi will be slower — this is the laptop ceiling, not
the gate verdict.
"""

from __future__ import annotations

import time
from typing import Optional


def add_parser(subparsers) -> None:
    """Register the ``follow`` subcommand and its flags."""
    parser = subparsers.add_parser(
        "follow",
        help="Run FOLLOW mode: steer the (fake) wheels toward a real person on the webcam.",
        description=(
            "Track-by-detection FOLLOW mode (software-spec.md §4): grab frames from "
            "the real webcam, detect/track the nearest person, and steer the "
            "simulated wheels toward them (turn to center, drive forward until "
            "close). Degrades to a clean stop when the person is lost/stale or it's "
            "too dark. --benchmark prints the laptop detector/tracker/FOLLOW fps "
            "baseline and compares it to the Gate H threshold."
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
        "--preview",
        action="store_true",
        help=(
            "Show an OpenCV window with the bbox + steering overlay IF a display is "
            "available (headless-safe: silently disabled when there is no display)."
        ),
    )
    parser.add_argument(
        "--benchmark",
        action="store_true",
        help=(
            "Measure the laptop detector / tracker / FOLLOW-tick fps baseline and "
            "compare to the Gate H GO threshold, then exit."
        ),
    )
    parser.add_argument(
        "--synthetic",
        action="store_true",
        help=(
            "Force the synthetic camera test-pattern (no-camera demo / reproducible "
            "runs). Default: the real webcam, auto-falling back to synthetic."
        ),
    )
    parser.add_argument(
        "--hz",
        type=float,
        default=15.0,
        metavar="HZ",
        help="Tick / print rate for the live loop (default: 15).",
    )
    parser.set_defaults(handler=run)


def run(args) -> int:
    """Handler for ``yalp follow``. Returns a process exit code."""
    source = "synthetic" if getattr(args, "synthetic", False) else "webcam"
    if getattr(args, "benchmark", False):
        return _benchmark(source=source, seconds=args.seconds or 4.0)
    return _live(
        source=source,
        seconds=args.seconds,
        preview=bool(getattr(args, "preview", False)),
        hz=max(1.0, float(args.hz)),
    )


# --------------------------------------------------------------------------- #
# Live FOLLOW
# --------------------------------------------------------------------------- #
def _live(*, source: str, seconds: Optional[float], preview: bool, hz: float) -> int:
    from ..contract.messages import Intent, Mode
    from .fake_backend import FakeReactiveBackend
    from .follow import decision_line

    backend = FakeReactiveBackend(camera_source=source, tick_hz=hz)
    backend.start()
    # Enter FOLLOW (this is exactly what `enter_follow_mode` / "follow me" does).
    backend.apply_intent(Intent(Mode.FOLLOW, {"target": "nearest_person"}, seq=1))

    print(
        f"yalp follow — REAL EYES + FAKE WHEELS (camera={backend.camera().source}). "
        f"{'Stopping after %.0fs.' % seconds if seconds else 'Ctrl-C to stop.'}"
    )
    previewer = _Previewer() if preview else None
    dt = 1.0 / hz
    deadline = (time.monotonic() + seconds) if seconds else None
    try:
        while deadline is None or time.monotonic() < deadline:
            t0 = time.monotonic()
            state = backend.tick()
            decision = backend.last_follow_decision
            print(_line(state, decision, decision_line))
            if previewer is not None:
                previewer.show(backend.camera().latest(), state, decision)
            elapsed = time.monotonic() - t0
            if dt > elapsed:
                time.sleep(dt - elapsed)
    except KeyboardInterrupt:
        print("\n[stopped]")
    finally:
        if previewer is not None:
            previewer.close()
        backend.stop()
    return 0


def _line(state, decision, decision_line) -> str:
    # SAFE_STOP (collision) overrides FOLLOW entirely — surface it clearly.
    from ..contract.messages import Mode

    if getattr(state, "mode", None) == Mode.SAFE_STOP:
        return "SAFE_STOP -> stop (collision-stop overrides follow)"
    return decision_line(decision)


# --------------------------------------------------------------------------- #
# Benchmark (Gate H de-risk)
# --------------------------------------------------------------------------- #
def _benchmark(*, source: str, seconds: float) -> int:
    from .. import config
    from ..camera import Camera
    from ..contract.messages import Intent, Mode
    from .fake_backend import FakeReactiveBackend
    from .person_tracker import HogPersonDetector, PersonTracker

    print(f"yalp follow --benchmark — laptop fps baseline (source={source})\n")

    cam = Camera(source=source)
    cam.start()
    frame = cam.wait_for_frame(timeout=5.0)
    if frame is None:
        print("no frame available — cannot benchmark.")
        cam.stop()
        return 1

    try:
        detector = HogPersonDetector()
    except Exception as exc:  # pragma: no cover - opencv missing
        print(f"could not build the HOG detector ({type(exc).__name__}: {exc}).")
        cam.stop()
        return 1

    # 1. Detector-only throughput (the Gate H number, in isolation).
    det_fps = _rate(seconds, lambda: detector.detect(cam.latest()))

    # 2. Cheap tracker-only throughput (detect once, then track between detections).
    pt = PersonTracker(detector=detector, detect_interval=10_000)
    pt.update(cam.latest())  # one detection to seed
    trk_fps = _rate(seconds, lambda: pt.update(cam.latest()))

    cam.stop()

    # 3. Combined FOLLOW tick rate (tracker + controller + state publish).
    backend = FakeReactiveBackend(camera_source=source, tick_hz=1000.0)
    backend.start()
    backend.apply_intent(Intent(Mode.FOLLOW, {"target": "nearest_person"}, seq=1))
    backend.tick()  # adopt FOLLOW
    follow_fps = _rate(seconds, backend.tick)
    backend.stop()

    gate = config.GATE_H_GO_HZ
    verdict = "GO" if det_fps >= gate else "NO-GO"
    print(f"  detector (HOG @ {detector.detect_width}px) : {det_fps:6.1f} Hz")
    print(f"  cheap tracker (between detections)         : {trk_fps:6.1f} Hz")
    print(f"  combined FOLLOW tick rate                  : {follow_fps:6.1f} Hz")
    print()
    print(
        f"  Gate H threshold (config.GATE_H_GO_HZ)     : {gate} Hz sustained\n"
        f"  laptop detector verdict                    : {verdict} "
        f"({det_fps:.1f} Hz vs {gate} Hz)"
    )
    print(
        "\n  NOTE: this is the LAPTOP ceiling (OpenCV HOG, no NPU swap). The Pi 5\n"
        "  is slower — Gate H must be re-run on the Pi under concurrent load, and\n"
        "  on the Pi we'd swap in a faster detector (MobileNet-SSD / YOLO-nano)\n"
        "  behind the same Detector interface."
    )
    return 0


def _rate(seconds: float, fn) -> float:
    """Call ``fn`` in a tight loop for ``seconds`` and return calls/second."""
    count = 0
    t0 = time.monotonic()
    deadline = t0 + seconds
    while time.monotonic() < deadline:
        fn()
        count += 1
    elapsed = time.monotonic() - t0
    return count / elapsed if elapsed > 0 else 0.0


# --------------------------------------------------------------------------- #
# Optional, headless-safe OpenCV preview
# --------------------------------------------------------------------------- #
class _Previewer:
    """A best-effort OpenCV preview window. Never crashes when headless."""

    WINDOW = "yalp follow"

    def __init__(self) -> None:
        self._ok = True
        self._warned = False
        try:
            import cv2  # noqa: F401
        except Exception:
            self._disable("opencv unavailable")

    def show(self, frame, state, decision) -> None:
        if not self._ok or frame is None:
            return
        try:
            import cv2

            img = frame.copy()
            bbox = getattr(state, "target_bbox", None)
            if bbox is not None:
                x, y, w, h = (int(v) for v in bbox)
                cv2.rectangle(img, (x, y), (x + w, y + h), (0, 255, 0), 2)
            label = "lost" if decision is None or not decision.target_visible else (
                f"turn={decision.turn:+.2f} fwd={decision.forward:.2f}"
            )
            cv2.putText(img, label, (8, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.7,
                        (0, 255, 0), 2, cv2.LINE_AA)
            cv2.imshow(self.WINDOW, img)
            cv2.waitKey(1)
        except Exception as exc:  # headless build / no display
            self._disable(f"{type(exc).__name__}")

    def close(self) -> None:
        if not self._ok:
            return
        try:
            import cv2

            cv2.destroyAllWindows()
        except Exception:
            pass

    def _disable(self, why: str) -> None:
        self._ok = False
        if not self._warned:
            print(f"[preview unavailable ({why}) — running headless]")
            self._warned = True


__all__ = ["add_parser", "run"]
