"""Tests for the shared FOLLOW runtime (``yalp.reactive.follow_runner``).

No real camera, mic, or GUI: a stub backend (and the synthetic camera source)
drives the loop deterministically via a preset ``stop_event`` / a tiny ``seconds``
cap. We assert the loop ADVANCES the backend when it OWNS ticking (``yalp follow``)
and only MONITORS — never ticks — when it does NOT (``yalp agent``, whose backend
ticks on its own background thread). Preview is always False so no display is
needed; cv2 GUI is lazy-imported so headless CI passes.
"""

from __future__ import annotations

import threading

from yalp.contract.messages import GoalStatus, Mode, RobotState
from yalp.reactive.follow_runner import (
    build_follow_backend,
    gui_available,
    run_follow_loop,
)


# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #
class _StubCamera:
    """Minimal camera: ``latest()`` returns None and trips a stop after one read.

    Returning ``None`` is deliberate — ``frame_brightness(None) == 0.0`` so the
    loop runs without numpy/opencv, and tripping the stop_event guarantees a
    single, deterministic iteration (no timing flakiness).
    """

    source = "stub"

    def __init__(self, stop_event: threading.Event) -> None:
        self._stop = stop_event
        self.calls = 0

    def latest(self):
        self.calls += 1
        self._stop.set()  # exactly one full iteration, then the loop exits
        return None


class _StubBackend:
    """Counts tick() vs get_state() so we can assert TICK OWNERSHIP."""

    def __init__(self, stop_event: threading.Event, mode: Mode = Mode.FOLLOW) -> None:
        self.ticks = 0
        self.state_reads = 0
        self.last_follow_decision = None
        self._mode = mode
        self._cam = _StubCamera(stop_event)

    def tick(self) -> RobotState:
        self.ticks += 1
        return RobotState(mode=self._mode, goal_status=GoalStatus.RUNNING)

    def get_state(self) -> RobotState:
        self.state_reads += 1
        return RobotState(mode=self._mode, goal_status=GoalStatus.RUNNING)

    def camera(self):
        return self._cam


# --------------------------------------------------------------------------- #
# TICK OWNERSHIP — the crucial double-tick guard
# --------------------------------------------------------------------------- #
def test_owns_ticking_advances_backend():
    """`yalp follow` case: owns_ticking=True calls backend.tick() each iteration."""
    stop = threading.Event()
    backend = _StubBackend(stop)
    rc = run_follow_loop(
        backend, preview=False, owns_ticking=True, stop_event=stop, hz=1000.0
    )
    assert rc == 0
    assert backend.ticks >= 1  # it ADVANCED the simulation itself
    assert backend.state_reads == 0  # ...and did not also pull get_state()


def test_not_owns_ticking_only_monitors():
    """`yalp agent` case: owns_ticking=False must NOT tick (a thread already does)."""
    stop = threading.Event()
    backend = _StubBackend(stop)
    rc = run_follow_loop(
        backend, preview=False, owns_ticking=False, stop_event=stop, hz=1000.0
    )
    assert rc == 0
    assert backend.ticks == 0  # never double-ticked
    assert backend.state_reads >= 1  # only read the published state


def test_preset_stop_event_exits_immediately():
    """A stop_event set up front exits cleanly without ticking or reading."""
    stop = threading.Event()
    stop.set()
    backend = _StubBackend(stop)
    rc = run_follow_loop(
        backend, preview=False, owns_ticking=True, stop_event=stop, hz=1000.0
    )
    assert rc == 0
    assert backend.ticks == 0
    assert backend.state_reads == 0


def test_seconds_cap_exits_without_camera_or_gui():
    """A tiny `seconds` cap exits on its own (no stop_event needed)."""
    backend = _StubBackend(threading.Event())
    # No stop trip here — rely solely on the seconds deadline.
    backend._cam._stop = threading.Event()  # detach the auto-stop
    rc = run_follow_loop(
        backend, preview=False, owns_ticking=True, seconds=0.02, hz=1000.0
    )
    assert rc == 0
    assert backend.ticks >= 1


# --------------------------------------------------------------------------- #
# Synthetic source — real backend/tracker, no hardware or GUI
# --------------------------------------------------------------------------- #
def test_run_follow_loop_synthetic_source_owns_ticking():
    """End-to-end with the synthetic camera + real backend: advances, returns 0."""
    backend = build_follow_backend(source="synthetic", detector="face", hz=30.0)
    try:
        rc = run_follow_loop(
            backend, preview=False, owns_ticking=True, seconds=0.1, hz=30.0
        )
    finally:
        backend.stop()
    assert rc == 0
    # The backend really entered FOLLOW and produced steering decisions.
    assert backend.get_state().mode == Mode.FOLLOW


# --------------------------------------------------------------------------- #
# Headless safety
# --------------------------------------------------------------------------- #
class _RecordingCv2:
    """A fake ``cv2`` that RECORDS any window-probe call instead of making it.

    On headless Linux, real full ``opencv-python`` would ``qFatal → abort()``
    (a native C++ SIGABRT) inside ``namedWindow`` — uncatchable by Python. The
    only safe behavior is to never call it, so we assert ``calls`` stays empty.
    Recording lets us prove "never invoked" WITHOUT simulating an actual
    (uncatchable) abort. See docs/technical/pi-validation-2026-07.md §9 issue #1.
    """

    WINDOW_NORMAL = 0

    def __init__(self) -> None:
        self.calls: list[str] = []

    def namedWindow(self, *args, **kwargs):
        self.calls.append("namedWindow")

    def destroyWindow(self, *args, **kwargs):
        self.calls.append("destroyWindow")


def _force_headless_linux(monkeypatch):
    """Force the Linux 'no display' path and reset the memoized GUI result.

    Installs and returns a recording fake ``cv2`` in ``sys.modules`` so ANY probe
    attempt would be captured (we then assert it stayed empty).
    """
    import sys

    from yalp.reactive import follow_runner

    monkeypatch.setattr(follow_runner.platform, "system", lambda: "Linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    # Clear the memoized result so the display pre-check actually runs.
    monkeypatch.setattr(follow_runner, "_GUI_AVAILABLE", None)
    fake = _RecordingCv2()
    monkeypatch.setitem(sys.modules, "cv2", fake)
    return fake


def test_gui_available_is_boolean_and_never_raises(monkeypatch):
    """Headless Linux (no DISPLAY/WAYLAND_DISPLAY): gui_available() returns a bool,
    never raises, is False, and NEVER touches a cv2 window function.

    The pre-check must short-circuit BEFORE any probe, because the full-opencv Qt
    ``abort()`` on a headless box is native and uncatchable (pi-validation §9 #1).
    """
    fake = _force_headless_linux(monkeypatch)
    result = gui_available()
    assert isinstance(result, bool)
    assert result is False
    assert fake.calls == []  # the uncatchable probe was never invoked


def test_preview_true_is_headless_safe(monkeypatch):
    """preview=True must not require a display: on headless Linux it falls back
    gracefully AND never invokes the cv2 window probe (which would abort())."""
    fake = _force_headless_linux(monkeypatch)
    stop = threading.Event()
    backend = _StubBackend(stop)
    rc = run_follow_loop(
        backend, preview=True, owns_ticking=True, stop_event=stop, hz=1000.0
    )
    assert rc == 0  # no crash — falls back to printed status
    assert fake.calls == []  # gui_available()'s pre-check kept us away from cv2
