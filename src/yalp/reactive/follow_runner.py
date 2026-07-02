"""Reusable FOLLOW-mode runtime shared by ``yalp follow`` and ``yalp agent``.

The REAL camera-follow loop (build a tracker, start the backend, enter FOLLOW, and
then LOOP — rendering a preview and/or printing readable status until the user
stops it) is the only thing that separates a live, useful follow from the
fire-and-forget handoff the deliberative agent used to do. This module hoists that
loop out of ``follow_cli`` so BOTH entry points can share it:

  * ``yalp follow`` builds its own backend and OWNS ticking — it advances the
    simulated reactive layer by calling ``backend.tick()`` itself each iteration.
  * ``yalp agent`` already runs the backend on a BACKGROUND THREAD (the simulated
    reactive process). It must NOT tick again — it only renders/monitors the
    already-published state. ``run_follow_loop(..., owns_ticking=False)`` does
    exactly that, reading ``backend.camera().latest()`` for frames and the
    published ``RobotState`` / ``backend.last_follow_decision`` for status.

Everything here is HEADLESS-SAFE: ``cv2`` is imported lazily and every
``imshow``/``waitKey`` call is gated on GUI availability, so headless runs (and
the Pi) fall back to printed status lines and tests never need a real camera,
mic, or display.
"""

from __future__ import annotations

import os
import platform
import threading
import time
from typing import Optional


# --------------------------------------------------------------------------- #
# Tracker / backend construction
# --------------------------------------------------------------------------- #
def build_follow_tracker(detector: str):
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


# Sentinel for ``build_follow_backend(observer=...)``: distinguishes "caller did
# not pass one -> auto-create the default flight recorder" from an explicit
# ``observer=None`` (telemetry off for this run).
_CREATE_DEFAULT_OBSERVER = object()


def build_follow_backend(*, source: str, detector: str, hz: float, observer=_CREATE_DEFAULT_OBSERVER):
    """Build + start a FakeReactiveBackend already in FOLLOW mode.

    Constructs the tracker for ``detector``, builds the backend on ``source`` at
    ``hz``, ``start()``s its camera, and applies the SAME ``Intent(Mode.FOLLOW,
    {"target": "nearest_person"})`` that ``enter_follow_mode`` / "follow me" does.
    The caller owns ``stop()`` (so the backend can be reused / torn down on its
    own schedule).

    Telemetry (the flight recorder): by default this auto-creates one from the
    environment (:func:`yalp.telemetry.create_logger_from_env` — ``None`` when
    ``YALP_TELEMETRY=0``) and hands the backend OWNERSHIP of it, so ``stop()``
    flushes and closes it. Pass an explicit ``observer=`` (including ``None``) to
    inject your own recorder / disable it — tests and library users stay in
    control and nothing is written unless they opt in.
    """
    from ..contract.messages import Intent, Mode
    from .fake_backend import FakeReactiveBackend

    owns_observer = False
    if observer is _CREATE_DEFAULT_OBSERVER:
        from ..telemetry import create_logger_from_env

        observer = create_logger_from_env()
        owns_observer = observer is not None

    tracker = build_follow_tracker(detector)
    backend = FakeReactiveBackend(
        camera_source=source,
        tick_hz=hz,
        tracker=tracker,
        observer=observer,
        close_observer=owns_observer,
    )
    backend.start()
    # Enter FOLLOW (this is exactly what `enter_follow_mode` / "follow me" does).
    backend.apply_intent(Intent(Mode.FOLLOW, {"target": "nearest_person"}, seq=1))
    return backend


# --------------------------------------------------------------------------- #
# The live FOLLOW loop (shared)
# --------------------------------------------------------------------------- #
def run_follow_loop(
    backend,
    *,
    preview: bool,
    owns_ticking: bool,
    seconds: Optional[float] = None,
    reporter: Optional[object] = None,
    stop_event: Optional[threading.Event] = None,
    hz: float = 15.0,
) -> int:
    """Render / monitor a live FOLLOW until the user stops it. Returns exit code.

    Parameters
    ----------
    backend:
        A ``FakeReactiveBackend`` already in FOLLOW mode.
    preview:
        Show an OpenCV preview window — but ONLY when a cv2 GUI is actually
        available (headless-safe). When unavailable, readable ``FollowReporter``
        status lines are printed instead (and they are printed regardless, so the
        terminal always tells you what is happening).
    owns_ticking:
        ``True`` for ``yalp follow`` (this loop advances the backend by calling
        ``backend.tick()`` each iteration). ``False`` for ``yalp agent`` (a
        background thread already ticks the backend — we must NOT tick again, only
        read the published ``RobotState`` / ``last_follow_decision`` and render).
    seconds:
        Optional auto-stop after N seconds (default: run until Ctrl-C / ``q`` /
        ``stop_event``).
    reporter:
        An optional pre-built ``FollowReporter`` (a fresh one is made otherwise).
    stop_event:
        An optional ``threading.Event``; the loop exits cleanly once it is set.
    hz:
        Render / print cadence for the loop (default 15).
    """
    from .. import config
    from ..contract.messages import Mode
    from .follow import FollowReporter, frame_brightness

    reporter = reporter if reporter is not None else FollowReporter()
    previewer = _Previewer() if (preview and gui_available()) else None
    dark = config.FOLLOW_DARK_BRIGHTNESS
    warmup_ticks = config.FOLLOW_WARMUP_TICKS
    exposed = False
    i = 0
    hz = max(1.0, float(hz))
    dt = 1.0 / hz
    deadline = (time.monotonic() + seconds) if seconds else None
    try:
        while True:
            if stop_event is not None and stop_event.is_set():
                break
            if deadline is not None and time.monotonic() >= deadline:
                break

            t0 = time.monotonic()
            i += 1
            # TICK OWNERSHIP: yalp follow advances the backend here; yalp agent's
            # backend is ticked on its own thread, so we only READ the published
            # state (never tick twice).
            state = backend.tick() if owns_ticking else backend.get_state()
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
            if previewer is not None and previewer.show(frame, state, decision, brightness):
                break  # 'q' in the preview window -> stop

            elapsed = time.monotonic() - t0
            if dt > elapsed:
                time.sleep(dt - elapsed)
    except KeyboardInterrupt:
        print("\n[stopped]")
    finally:
        if previewer is not None:
            previewer.close()
    return 0


# --------------------------------------------------------------------------- #
# GUI availability (headless-safe, memoized)
# --------------------------------------------------------------------------- #
_GUI_AVAILABLE: Optional[bool] = None


def _display_present() -> bool:
    """True only when it is SAFE to probe cv2 for a GUI window.

    This gate exists because full ``opencv-python`` bundles a Qt that ships only
    the ``xcb`` plugin: on a headless Linux box (the robot) ``cv2.namedWindow()``
    calls ``qFatal → abort()`` — a *native C++* ``abort()`` (SIGABRT). That is
    uncatchable by Python's ``except Exception`` (and ``QT_QPA_PLATFORM=offscreen``
    doesn't help — the bundled Qt has no offscreen plugin), so the ONLY safe move
    is to never touch a cv2 window function unless a display exists. See
    docs/technical/pi-validation-2026-07.md §9 issue #1.

    On Linux, a usable display means ``DISPLAY`` (X11) or ``WAYLAND_DISPLAY``
    (Wayland) is set and non-empty. On macOS ('Darwin') a GUI session is normally
    present and there is no such env var, so we allow the (catchable) probe to run
    and decide — preserving laptop preview behavior.
    """
    if platform.system() == "Linux":
        return bool(
            os.environ.get("DISPLAY", "").strip()
            or os.environ.get("WAYLAND_DISPLAY", "").strip()
        )
    # macOS (and other desktop OSes): assume a GUI session; let the probe decide.
    return True


def gui_available() -> bool:
    """Best-effort check that a cv2 GUI backend is usable. Never raises.

    Returns ``False`` on headless builds (``opencv-python-headless``) / no display
    so callers fall back to printed status. On Linux with no display we short-
    circuit to ``False`` BEFORE touching any cv2 window function, because the
    full-opencv Qt ``abort()`` there is native and uncatchable (see
    :func:`_display_present`). Otherwise the probe creates and immediately destroys
    a window once (catchable failures fall back to ``False``). The result is
    memoized.
    """
    global _GUI_AVAILABLE
    if _GUI_AVAILABLE is not None:
        return _GUI_AVAILABLE
    ok = False
    if _display_present():
        try:
            import cv2

            cv2.namedWindow("__yalp_gui_probe__", cv2.WINDOW_NORMAL)
            cv2.destroyWindow("__yalp_gui_probe__")
            ok = True
        except Exception:
            ok = False
    _GUI_AVAILABLE = ok
    return ok


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

    def show(self, frame, state, decision, brightness: float = 0.0) -> bool:
        """Render one frame; return True if the user pressed 'q' (stop)."""
        if not self._ok or frame is None:
            return False
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
            key = cv2.waitKey(1) & 0xFF
            return key in (ord("q"), 27)  # 'q' or Esc -> stop
        except Exception as exc:  # headless build / no display
            self._disable(f"{type(exc).__name__}")
            return False

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


__all__ = [
    "build_follow_tracker",
    "build_follow_backend",
    "run_follow_loop",
    "gui_available",
]
