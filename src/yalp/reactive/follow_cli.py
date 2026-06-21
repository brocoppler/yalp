"""``yalp follow`` — run FOLLOW mode live against the real webcam + fake wheels.

Registered with the CLI via the documented feature-module contract: this module
exposes ``add_parser(subparsers)`` and ``run(args) -> int`` and is listed in
``yalp.cli.FEATURE_MODULES``.

    yalp follow                    # follow a real person on the real webcam (Ctrl-C)
    yalp follow --seconds 10       # auto-stop after 10 s
    yalp follow --detector person  # ORIENTATION-AGNOSTIC body detector (walk away!)
    yalp follow --detector hog     # OpenCV's built-in standing-body detector
    yalp follow --preview          # also show an OpenCV window (if a display exists)
    yalp follow --synthetic        # no-camera demo (synthetic frames; will report lost)
    yalp follow --benchmark        # print the SELECTED detector/tracker/FOLLOW fps baseline

DETECTOR (``--detector``): the default ``face`` (OpenCV's bundled Haar cascade) is
reliable at DESK range, where a webcam frames only the user's head+shoulders — the
full-body detectors cannot see that and just report "lost". ``person`` is the
ORIENTATION-AGNOSTIC cv2.dnn MobileNet-SSD body detector: it tracks a person from
ANY angle (front, BACK, side) at room range, so robot-follow keeps working when the
user walks AWAY — this is the ROBOT's default (face is desk-only) and the Gate H
detector candidate. ``hog`` is OpenCV's built-in standing-body detector; ``auto``
prefers ``person`` and falls back to ``face`` for close-ups. All sit behind the
same pluggable Detector interface, so FOLLOW is unchanged behind any of them. Test
``person`` on the laptop: ``yalp follow --detector person``, then stand back and
turn around — it should still track. The ``person`` model file downloads once and
is cached (cv2.dnn — NO new pip dependency); offline, it fails with clear
instructions for dropping the file in by hand.

REAL EYES + FAKE WHEELS: the wheels are simulated by ``FakeReactiveBackend`` but
the camera is real. Each tick grabs the latest frame from the reactive layer's
OWNED camera, runs the track-by-detection tracker, and steers toward the person
(turn toward the bbox center; drive forward until close). It degrades gracefully
— "lost -> stop" — when the person is lost/stale or the frame is too dark, and
collision-stop / SAFE_STOP overrides everything underneath.

``--benchmark`` de-risks **Gate H** (roadmap.md): it measures the *laptop* ceiling
for the SELECTED detector's fps (run ``--benchmark --detector person`` for the
cv2.dnn person-detector baseline — the Gate H candidate) and compares it to
``config.GATE_H_GO_HZ``. The Pi will be slower — the laptop fps is a CEILING, not
the gate verdict; Gate H is the number measured on the Pi later.
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
        "--detector",
        choices=("face", "hog", "person", "auto"),
        default=None,  # resolved to config.FOLLOW_DETECTOR_DEFAULT ("face")
        help=(
            "Person detector: 'face' (DEFAULT, desk-only) — bundled Haar face cascade, "
            "reliable at desk range (head+shoulders webcam framing); 'hog' — OpenCV's "
            "built-in standing-body detector; 'person' — ORIENTATION-AGNOSTIC cv2.dnn "
            "MobileNet-SSD body detector that tracks from ANY angle (front/back/side) at "
            "room range, so follow keeps working when you walk AWAY (the ROBOT's default; "
            "downloads a cached model file on first use); 'auto' — person, falling back to "
            "face for close-ups. Stand back and turn around to test 'person' on the laptop."
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
    from .. import config

    source = "synthetic" if getattr(args, "synthetic", False) else "webcam"
    detector = getattr(args, "detector", None) or config.FOLLOW_DETECTOR_DEFAULT
    if getattr(args, "benchmark", False):
        return _benchmark(source=source, seconds=args.seconds or 4.0, detector=detector)
    return _live(
        source=source,
        seconds=args.seconds,
        preview=bool(getattr(args, "preview", False)),
        hz=max(1.0, float(args.hz)),
        detector=detector,
    )


# --------------------------------------------------------------------------- #
# Live FOLLOW
# --------------------------------------------------------------------------- #
def _live(
    *, source: str, seconds: Optional[float], preview: bool, hz: float, detector: str
) -> int:
    from .. import config
    from ..contract.messages import Intent, Mode
    from .fake_backend import FakeReactiveBackend
    from .follow import FollowReporter, frame_brightness

    tracker = _build_tracker(detector)
    backend = FakeReactiveBackend(camera_source=source, tick_hz=hz, tracker=tracker)
    backend.start()
    # Enter FOLLOW (this is exactly what `enter_follow_mode` / "follow me" does).
    backend.apply_intent(Intent(Mode.FOLLOW, {"target": "nearest_person"}, seq=1))

    print(
        f"yalp follow — REAL EYES + FAKE WHEELS "
        f"(camera={backend.camera().source}, detector={detector}). "
        f"{'Stopping after %.0fs.' % seconds if seconds else 'Ctrl-C to stop.'}"
    )
    print("warming up camera…")

    reporter = FollowReporter()
    previewer = _Previewer() if preview else None
    dark = config.FOLLOW_DARK_BRIGHTNESS
    warmup_ticks = config.FOLLOW_WARMUP_TICKS
    exposed = False
    i = 0
    dt = 1.0 / hz
    deadline = (time.monotonic() + seconds) if seconds else None
    try:
        while deadline is None or time.monotonic() < deadline:
            t0 = time.monotonic()
            i += 1
            state = backend.tick()
            decision = backend.last_follow_decision
            frame = backend.camera().latest()
            brightness = frame_brightness(frame)
            safe_stop = getattr(state, "mode", None) == Mode.SAFE_STOP

            # Quiet warm-up: stay silent until the camera is actually exposed (or
            # we have waited the warm-up window), so the "too dark" startup noise
            # never reaches the user.
            if not exposed and (brightness >= dark or i >= warmup_ticks):
                exposed = True
            warming_up = not exposed

            line = reporter.update(
                decision, t0, warming_up=warming_up, safe_stop=safe_stop
            )
            if line is not None:
                print(line)
            if previewer is not None:
                previewer.show(frame, state, decision, brightness)
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


def _build_tracker(detector: str):
    """Build a PersonTracker with the selected detector, or None on failure.

    Returning ``None`` lets ``FakeReactiveBackend`` fall back to its lazy default
    so a missing/odd OpenCV build still runs FOLLOW (degrading to "lost") instead
    of crashing the CLI.
    """
    try:
        from .person_tracker import PersonTracker, build_detector, detect_interval_for

        det = build_detector(detector)
        return PersonTracker(detector=det, detect_interval=detect_interval_for(detector))
    except Exception as exc:  # pragma: no cover - opencv missing / bad build
        print(f"[detector '{detector}' unavailable ({type(exc).__name__}: {exc}) — "
              f"using default]")
        return None


# --------------------------------------------------------------------------- #
# Benchmark (Gate H de-risk)
# --------------------------------------------------------------------------- #
def _benchmark(*, source: str, seconds: float, detector: str) -> int:
    from .. import config
    from ..camera import Camera
    from ..contract.messages import Intent, Mode
    from .fake_backend import FakeReactiveBackend
    from .person_tracker import PersonTracker, build_detector, detect_interval_for

    print(
        f"yalp follow --benchmark — laptop fps baseline "
        f"(source={source}, detector={detector})\n"
    )

    cam = Camera(source=source)
    cam.start()
    frame = cam.wait_for_frame(timeout=5.0)
    if frame is None:
        print("no frame available — cannot benchmark.")
        cam.stop()
        return 1

    try:
        det = build_detector(detector)
    except Exception as exc:  # pragma: no cover - opencv missing
        print(f"could not build the '{detector}' detector "
              f"({type(exc).__name__}: {exc}).")
        cam.stop()
        return 1

    width = getattr(det, "detect_width", config.FOLLOW_DETECT_WIDTH)

    # 1. Detector-only throughput (the Gate H number, in isolation).
    det_fps = _rate(seconds, lambda: det.detect(cam.latest()))

    # 2. Cheap tracker-only throughput (detect once, then track between detections).
    pt = PersonTracker(detector=det, detect_interval=10_000)
    pt.update(cam.latest())  # one detection to seed
    trk_fps = _rate(seconds, lambda: pt.update(cam.latest()))

    cam.stop()

    # 3. Combined FOLLOW tick rate (tracker + controller + state publish) using the
    #    SELECTED detector, at its normal re-detect cadence.
    bench_tracker = PersonTracker(
        detector=build_detector(detector),
        detect_interval=detect_interval_for(detector),
    )
    backend = FakeReactiveBackend(
        camera_source=source, tick_hz=1000.0, tracker=bench_tracker
    )
    backend.start()
    backend.apply_intent(Intent(Mode.FOLLOW, {"target": "nearest_person"}, seq=1))
    backend.tick()  # adopt FOLLOW
    follow_fps = _rate(seconds, backend.tick)
    backend.stop()

    gate = config.GATE_H_GO_HZ
    verdict = "GO" if det_fps >= gate else "NO-GO"
    print(f"  detector ({detector} @ {width}px) : {det_fps:6.1f} Hz")
    print(f"  cheap tracker (between detections)         : {trk_fps:6.1f} Hz")
    print(f"  combined FOLLOW tick rate                  : {follow_fps:6.1f} Hz")
    print()
    print(
        f"  Gate H threshold (config.GATE_H_GO_HZ)     : {gate} Hz sustained\n"
        f"  laptop detector verdict                    : {verdict} "
        f"({det_fps:.1f} Hz vs {gate} Hz)"
    )
    print(
        "\n  NOTE: this is the LAPTOP ceiling (OpenCV, no NPU swap). The Pi 5\n"
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

    def show(self, frame, state, decision, brightness: float = 0.0) -> None:
        if not self._ok or frame is None:
            return
        try:
            import cv2

            img = frame.copy()
            visible = decision is not None and decision.target_visible
            # BGR: green when tracking, red when lost.
            color = (0, 200, 0) if visible else (0, 0, 255)

            bbox = getattr(state, "target_bbox", None)
            if bbox is not None:
                x, y, w, h = (int(v) for v in bbox)
                cv2.rectangle(img, (x, y), (x + w, y + h), color, 2)

            status = "TRACKING" if visible else "LOST"
            cv2.putText(img, status, (8, 26), cv2.FONT_HERSHEY_SIMPLEX, 0.8,
                        color, 2, cv2.LINE_AA)

            # Steering decision: a turn arrow + forward/stop, or the lost reason.
            if visible and decision is not None:
                if decision.turn > 0.02:
                    steer = "-> RIGHT"
                elif decision.turn < -0.02:
                    steer = "<- LEFT"
                else:
                    steer = "CENTER"
                drive = "STOP" if decision.forward <= 0.0 else "FORWARD"
                steer_label = f"{steer} | {drive}"
            else:
                steer_label = decision.status if decision is not None else "no target"
            cv2.putText(img, steer_label, (8, 54), cv2.FONT_HERSHEY_SIMPLEX, 0.7,
                        color, 2, cv2.LINE_AA)

            cv2.putText(img, f"brightness={brightness:.0f}", (8, 80),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.55, (200, 200, 200), 1, cv2.LINE_AA)

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
