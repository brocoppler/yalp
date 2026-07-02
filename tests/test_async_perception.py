"""Regression suite for the async FOLLOW perception worker (async-perception task).

The contract this proves (backend.py / software-spec.md §2.3): a tick must complete
well under ``TICK_BUDGET_MS`` (33 ms) and must keep the independent
``MotorWatchdog`` heartbeat fresh, even while the person detector — HOG / MobileNet
-SSD, ~100–500 ms per frame on a Pi 5 — is running. The whole point of moving
``tracker.update()`` onto the :class:`~yalp.reactive.perception.PerceptionWorker`
thread is that this heavy call NEVER lands on the tick path. If it did (the old
``_step_follow`` behavior), every detector tick during FOLLOW would blow the budget
and a short-timeout watchdog would zero the motors on every tick — making
follow-on-hardware impossible.

These tests use a **deliberately slow fake detector** (~200 ms per update, Pi-like)
and assert, with GENEROUS timing tolerances:

  (a) tick durations stay far under budget (nowhere near the 200 ms detector);
  (b) a MotorWatchdog with a short test timeout (20 ms), heartbeated by the tick,
      never trips during FOLLOW;
  (c) FOLLOW still acquires and tracks once a (slow) detection lands;
  (d) a CRASHED worker freezes its last observation, the controller degrades to a
      clean ``'stale'`` stop (the crash is logged loudly, once), and the tick NEVER
      throws or wedges because of the worker.

Both backends share the SAME worker path via the shared tick core, so exercising
the fake here also exercises the real backend's FOLLOW perception path.
"""

from __future__ import annotations

import logging
import statistics
import threading
import time

import numpy as np

from yalp.camera import Camera
from yalp.contract.messages import Intent, Mode
from yalp.reactive.fake_backend import FakeReactiveBackend
from yalp.reactive.follow import REASON_STALE, FollowController
from yalp.reactive.hardware import FakeMotorDriver, FakeRangeSensor
from yalp.reactive.person_tracker import TrackResult
from yalp.reactive.real_backend import RealReactiveBackend
from yalp.reactive.watchdog import MotorWatchdog

# A box centered/small in a 320x240 frame -> a clean visible "follow" decision.
_BOX = (140, 100, 40, 40)
# A grace window big enough that a slow (200 ms) detector's confirmation gap never
# reads as stale in a fast test loop — so (a)/(b)/(c) isolate the timing/acquire
# behavior from the (separately tested) stale-degradation path.
_HUGE_COAST = 10_000_000


class _StubCamera:
    """A minimal latest-frame camera handing back ONE fixed bright frame.

    Bright (mean 180) so FOLLOW never degrades to 'dark'. Fully deterministic and
    thread-safe for the tick+worker read pattern (a fixed, read-only ndarray)."""

    source = "fake"
    width, height = 320, 240

    def __init__(self) -> None:
        self._frame = np.full((self.height, self.width, 3), 180, dtype=np.uint8)
        self._started = False

    def start(self) -> "_StubCamera":
        self._started = True
        return self

    def stop(self) -> None:
        self._started = False

    def latest(self):
        return self._frame if self._started else None


class _SlowTracker:
    """A fake tracker whose ``update()`` sleeps ~``delay_s`` (a Pi-like detector).

    Returns a FRESH detector confirmation each cycle (bbox present, tracker counter
    0), so the worker publishes a confirmed observation ~every ``delay_s``. The
    sleep stands in for real HOG / MobileNet-SSD inference latency."""

    def __init__(self, bbox=_BOX, *, delay_s: float = 0.2, score: float = 0.9) -> None:
        self._result = TrackResult(True, bbox, score, 0, True)
        self._delay_s = float(delay_s)
        self.calls = 0

    def update(self, _frame) -> TrackResult:
        self.calls += 1
        time.sleep(self._delay_s)
        return self._result


class _CrashAfterTracker:
    """Succeeds ``ok_updates`` times (a confirmed box), then RAISES on ``update()``.

    Simulates a detector that blows up mid-run: the worker thread must contain the
    crash (log once, exit), freezing the last confirmed observation."""

    def __init__(self, bbox=_BOX, *, ok_updates: int = 1, score: float = 0.9) -> None:
        self._result = TrackResult(True, bbox, score, 0, True)
        self._ok_updates = int(ok_updates)
        self.calls = 0

    def update(self, _frame) -> TrackResult:
        self.calls += 1
        if self.calls > self._ok_updates:
            raise RuntimeError("boom: fake detector exploded")
        return self._result


def _wait_until(predicate, *, timeout: float = 5.0, interval: float = 0.005) -> bool:
    """Poll ``predicate`` until true or ``timeout`` elapses (generous by default)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


def _follow_backend(tracker, *, coast_ticks: int) -> FakeReactiveBackend:
    """A fake backend in FOLLOW with the ASYNC perception worker started.

    Mirrors the backend's own ``run()`` lifecycle (camera -> perception worker),
    then adopts FOLLOW with one tick so the worker becomes active (it only runs the
    detector while the backend is in FOLLOW). Returns after the mode is FOLLOW."""
    backend = FakeReactiveBackend(
        camera=_StubCamera(),
        tracker=tracker,
        follow_controller=FollowController(coast_ticks=coast_ticks),
    )
    backend.start()
    backend.start_perception()  # heavy detector runs OFF the tick from here on
    backend.apply_intent(Intent(Mode.FOLLOW, {"target": "nearest_person"}, seq=1))
    backend.tick()  # adopt FOLLOW -> the worker starts running the (slow) detector
    return backend


# --------------------------------------------------------------------------- #
# (a) + (b): a slow detector keeps ticks fast and the watchdog happy
# --------------------------------------------------------------------------- #
def test_slow_detector_keeps_ticks_far_under_budget_and_watchdog_never_trips():
    """~200 ms detector on the worker -> ticks stay tiny + a 20 ms watchdog holds.

    Inline (the pre-task behavior) each FOLLOW tick would take ~200 ms, blowing the
    33 ms budget and tripping any short-timeout watchdog on EVERY tick. Off-thread,
    the tick only reads the latest observation, so it stays sub-millisecond and can
    heartbeat a 20 ms watchdog far more often than it can go stale."""
    backend = _follow_backend(_SlowTracker(delay_s=0.2), coast_ticks=_HUGE_COAST)
    # Short test timeout (the task's suggested 20 ms) — heartbeated at the END of
    # every tick exactly like RealReactiveBackend.run() does.
    watchdog = MotorWatchdog(FakeMotorDriver(), timeout_ms=20)
    watchdog.start()
    tick_durations = []
    try:
        # Run past several ~200 ms detector cycles, ticking as fast as possible and
        # heartbeating each time (no artificial sleep — a real 30 Hz loop's 33 ms
        # cadence would exceed a 20 ms timeout, so the point is a fast, unblocked
        # tick, which is exactly what off-thread perception delivers).
        deadline = time.monotonic() + 0.6
        while time.monotonic() < deadline:
            t0 = time.perf_counter()
            backend.tick()
            tick_durations.append(time.perf_counter() - t0)
            watchdog.heartbeat()
    finally:
        watchdog.stop()
        backend.stop()

    assert tick_durations
    # (a) Every tick completes FAR under the ~200 ms detector latency — proof the
    # heavy detector runs OFF the tick path (inline it would be ~200 ms/tick).
    assert max(tick_durations) < 0.1, f"slowest tick {max(tick_durations)*1e3:.1f}ms"
    # And the TYPICAL tick is nowhere near the 33 ms budget (sub-millisecond).
    assert statistics.median(tick_durations) < 0.02
    # (b) The 20 ms watchdog, heartbeated by the tick, NEVER tripped during FOLLOW.
    assert watchdog.tripped is False
    assert watchdog.trip_count == 0
    # Sanity: the tick loop actually ran many times per detector cycle (proof the
    # tick was not gated on the 200 ms detector).
    assert len(tick_durations) > 20


# --------------------------------------------------------------------------- #
# (c): FOLLOW still acquires and tracks once a slow detection lands
# --------------------------------------------------------------------------- #
def test_slow_detector_still_acquires_and_tracks():
    """FOLLOW acquires the box once the ~200 ms worker detection lands, then steers."""
    backend = _follow_backend(_SlowTracker(bbox=_BOX, delay_s=0.2), coast_ticks=_HUGE_COAST)
    state = None
    try:
        # Tick (non-blockingly) until the slow detector's observation flows through.
        acquired = _wait_until(
            lambda: _visible_tick(backend), timeout=5.0, interval=0.01
        )
        state = backend.get_state()
    finally:
        backend.stop()

    assert acquired is True
    assert state.mode == Mode.FOLLOW
    assert state.target_visible is True
    assert state.target_bbox == _BOX
    assert state.tracker_score > 0.0
    dec = backend.last_follow_decision
    assert dec is not None and dec.target_visible is True


def _visible_tick(backend) -> bool:
    """Run one tick; True once FOLLOW has acquired a visible target."""
    return bool(backend.tick().target_visible)


def test_observation_carries_camera_frame_id():
    """Each published observation is stamped with the CAMERA frame id it saw.

    The worker reads ``(frame, frame_id)`` atomically from the owned camera and
    records the id on the Observation, so a consumer can tell WHICH frame the
    detector saw and detect a frozen source (id stops advancing)."""
    from yalp.camera import Camera

    cam = Camera(source="synthetic")  # real Camera exposes latest_with_id()
    backend = FakeReactiveBackend(
        camera=cam,
        tracker=_SlowTracker(delay_s=0.0),  # instant, still a confirmed box
        follow_controller=FollowController(coast_ticks=_HUGE_COAST),
    )
    backend.start()
    worker = backend.start_perception()
    backend.apply_intent(Intent(Mode.FOLLOW, {"target": "nearest_person"}, seq=1))
    backend.tick()  # adopt FOLLOW -> worker active
    try:
        assert _wait_until(lambda: _obs_has_real_frame(worker), timeout=5.0)
        obs = worker.latest()
    finally:
        backend.stop()
    assert obs is not None
    assert obs.bbox == _BOX
    assert isinstance(obs.frame_id, int)
    assert obs.frame_id >= 1  # a real captured synthetic frame, not the None/0 start


def _obs_has_real_frame(worker) -> bool:
    obs = worker.latest()
    return obs is not None and obs.frame_id is not None and obs.frame_id >= 1


# --------------------------------------------------------------------------- #
# (d): a crashed worker degrades to a clean 'stale' stop and NEVER throws
# --------------------------------------------------------------------------- #
def test_worker_crash_degrades_to_stale_and_tick_never_throws(caplog):
    """A crashing detector -> frozen observation -> 'stale' stop, tick never throws.

    The worker's ``tracker.update()`` raises after one good confirmation. The worker
    thread must contain that (log loudly, ONCE, then exit), freezing its last
    observation. The tick keeps reading that frozen observation non-blockingly, so
    the reactive-tick confirmation age climbs every tick until the controller
    degrades to a clean ``'stale'`` stop — and NO tick ever raises."""
    # Configure capture BEFORE the worker can run (avoids racing its crash log).
    caplog.set_level(logging.ERROR, logger="yalp.reactive.perception")
    coast = 3
    tracker = _CrashAfterTracker(bbox=_BOX, ok_updates=1)  # 1 good update, then boom
    backend = _follow_backend(tracker, coast_ticks=coast)
    worker = backend._perception  # the started async worker

    visibles = []
    reasons = []
    try:
        # Wait for the worker to publish its one confirmation and then CRASH.
        assert _wait_until(lambda: worker.crashed, timeout=5.0)
        # Now the last observation is frozen. Tick past the coast window: every tick
        # MUST return normally (never raise), the box is first acquired (visible),
        # then the confirmation age climbs it into a clean 'stale' stop.
        for _ in range(coast + 8):
            st = backend.tick()  # must NOT raise despite the dead worker
            visibles.append(st.target_visible)
            reasons.append(backend.last_follow_decision.reason)
            time.sleep(0.005)
    finally:
        backend.stop()

    # The worker crashed and latched its flag.
    assert worker.crashed is True
    # It acquired the confirmed box at least once (so the degradation is 'stale',
    # driven by a live box going un-reconfirmed — not 'lost' from never seeing one).
    assert True in visibles
    # It ends in a clean STALE stop with the target no longer reported visible.
    assert reasons[-1] == REASON_STALE
    assert visibles[-1] is False
    # The crash was logged LOUDLY, exactly ONCE (not once per worker cycle/tick).
    crash_records = [r for r in caplog.records if "crashed" in r.getMessage().lower()]
    assert len(crash_records) == 1
    assert crash_records[0].levelno >= logging.ERROR


def test_worker_crash_in_sync_pump_mode_never_throws_on_tick():
    """Even WITHOUT the async thread (sync pump), a crashing detector can't throw.

    Unit-test / laptop-demo callers drive ``tick()`` directly without ``run()``, so
    the worker pumps synchronously on the tick thread. A raising ``tracker.update()``
    there must STILL be contained (a stale observation), never surfacing as a raised
    tick — otherwise the safety loop would die on a detector bug."""
    coast = 3
    tracker = _CrashAfterTracker(bbox=_BOX, ok_updates=1)
    backend = FakeReactiveBackend(
        camera=_StubCamera(),
        tracker=tracker,
        follow_controller=FollowController(coast_ticks=coast),
    )
    backend.start()  # camera only — NO start_perception(), so the worker is sync-pump
    backend.apply_intent(Intent(Mode.FOLLOW, {"target": "nearest_person"}, seq=1))
    reasons = []
    try:
        for _ in range(coast + 8):
            st = backend.tick()  # sync pump: update() runs inline but crash is caught
            reasons.append(backend.last_follow_decision.reason)
    finally:
        backend.stop()
    # First tick acquired the box (one good update), then the inline update crashes
    # every subsequent pump -> the frozen observation ages into a clean STALE stop.
    assert backend._perception.crashed is True
    assert reasons[-1] == REASON_STALE


# --------------------------------------------------------------------------- #
# Shutdown ordering: retiring run()'s internal watchdog must precede the
# (potentially blocking) perception-worker join, so shutdown never fakes a wedge.
# --------------------------------------------------------------------------- #
class _BlockingTracker:
    """Blocks inside ``update()`` on an Event so the test can freeze the worker.

    Lets a test hold the worker INSIDE ``tracker.update()`` (as a slow real detector
    would be mid-frame) at the exact moment ``run()`` shuts down — so the worker
    join in teardown deterministically outlasts a watchdog heartbeat window."""

    def __init__(self, bbox=_BOX) -> None:
        self._result = TrackResult(True, bbox, 0.9, 0, True)
        self.in_update = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def update(self, _frame) -> TrackResult:
        self.calls += 1
        self.in_update.set()
        self.release.wait(timeout=5.0)  # block (bounded) until the test releases us
        return self._result


def test_run_shutdown_mid_detection_never_spuriously_trips_watchdog(caplog):
    """run()'s teardown must retire the watchdog BEFORE the blocking worker join.

    This drives the REAL RealReactiveBackend.run() production loop (async worker +
    the internal MotorWatchdog + the run()/close() lifecycle). We freeze the worker
    INSIDE ``tracker.update()`` and then shut down: the perception-worker join in
    teardown blocks for the whole (frozen) detector frame. If the watchdog were
    still armed during that heartbeat-less join it would fire a false 'TRIPPED'
    alarm — so run() must retire it first. Asserts NO trip was logged."""
    caplog.set_level(logging.ERROR, logger="yalp.reactive.watchdog")
    tracker = _BlockingTracker()
    backend = RealReactiveBackend(
        motor_driver=FakeMotorDriver(),
        range_sensor=FakeRangeSensor(),  # default: clear path, known
        camera=Camera(source="synthetic"),
        tracker=tracker,
        tick_hz=50.0,
    )
    stop = threading.Event()
    th = threading.Thread(target=backend.run, kwargs={"stop_event": stop, "hz": 50.0})
    th.start()
    try:
        backend.apply_intent(Intent(Mode.FOLLOW, {"target": "nearest_person"}, seq=1))
        # Wait until the worker is BLOCKED inside update() (a detector mid-frame).
        assert tracker.in_update.wait(timeout=5.0)
        # Shut down while the worker is stuck: teardown's worker-join will block on
        # it. Give an armed (old-ordering) watchdog well past its 100 ms timeout to
        # (wrongly) trip before we release the worker.
        stop.set()
        time.sleep(0.15)
    finally:
        tracker.release.set()  # let update() return so the worker + run() exit
        th.join(timeout=6.0)

    assert th.is_alive() is False, "run() thread did not shut down cleanly"
    trips = [r for r in caplog.records if "tripped" in r.getMessage().lower()]
    assert trips == [], (
        "watchdog spuriously tripped during FOLLOW shutdown: "
        f"{[r.getMessage() for r in trips]}"
    )
