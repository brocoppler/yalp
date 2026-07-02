"""The FOLLOW perception worker — heavy detector inference OFF the tick path.

Why this exists (the whole point of the task)
----------------------------------------------
FOLLOW steering needs a person detector, but running one is *expensive*: HOG /
MobileNet-SSD inference is ~100–500 ms per frame on a Pi 5. The reactive tick
contract (``backend.py`` module docstring, software-spec.md §2.3) forbids any
heavy CV on the tick path: a tick **must** complete well under
``config.TICK_BUDGET_MS`` (33 ms) so the safety loop stays ≥30 Hz, and the
independent :class:`~yalp.reactive.watchdog.MotorWatchdog`
(``config.WATCHDOG_TIMEOUT_MS`` = 100 ms) must never see a stale heartbeat. If
``tracker.update()`` ran inline under the tick lock, EVERY detector tick during
normal FOLLOW would blow both budgets and the watchdog would zero the motors —
making follow-on-hardware impossible.

So the detector runs on **this worker's own thread**. It:

  1. consumes the *latest* camera frame (single-slot, drop-stale — the same
     latest-frame design as :mod:`yalp.camera`; a stale frame is fine, a blocked
     tick is not),
  2. runs ``tracker.update(frame)`` (the heavy step),
  3. publishes the resulting observation into a **single-slot, last-write-wins**
     result mailbox (:class:`Observation`), overwriting any older result — the
     worker never queues a backlog.

The reactive tick then reads that latest observation **non-blockingly**
(:meth:`poll`) and feeds it to :class:`~yalp.reactive.follow.FollowController`.
The tick never touches the tracker or the detector.

Two run modes, one code path
----------------------------
* **Async (production).** :meth:`start` spins a daemon thread that loops
  :meth:`_run_once` (frame → ``update`` → publish). Started by the backend's
  ``run()``; the tick only ever :meth:`poll`\\ s the freshest published result.
* **Synchronous pump (unit tests / laptop demos).** When :meth:`start` was never
  called, :meth:`poll` runs exactly one :meth:`_run_once` inline first, so the
  tracker still produces observations deterministically, 1:1 with ticks. This is
  what keeps the parity/conformance suite deterministic and lets ``yalp follow``
  / the benchmark drive the backend by calling ``tick()`` directly. Once
  :meth:`start` has been called the worker is async **for good** — :meth:`poll`
  will NEVER pump inline again, so a slow or crashed worker can never re-introduce
  the heavy call onto the tick path.

Crash containment
-----------------
A raising ``tracker.update()`` must never wedge or throw on the tick. The worker
thread catches any exception, logs it **loudly, once**, and exits; the last
observation then simply freezes and ages out, so the controller degrades to a
``'stale'`` (or ``'lost'``) clean stop. The synchronous-pump path swallows the
same way (a stale observation instead of a raised tick).
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional, Tuple

from .person_tracker import Bbox

logger = logging.getLogger(__name__)

# Pacing. When idle (not in FOLLOW) the worker sleeps this long between cheap
# "am I active yet?" checks — it must NOT busy-spin while nothing is following.
IDLE_PERIOD_S = 0.02
# A tiny yield after each active cycle so an *instant* (fake/test) detector does
# not peg a core at 100%. A real detector's own latency (~100–500 ms) dwarfs this,
# so it never slows real perception. ``Event.wait`` doubles as the stop signal, so
# teardown stays prompt regardless.
ACTIVE_YIELD_S = 0.002


@dataclass(frozen=True)
class Observation:
    """One published perception result — the single-slot mailbox payload.

    Immutable (frozen dataclass + a tuple ``bbox``) so it is safe to hand from the
    worker thread to the tick thread without copying.

    ``confirmations`` is a **monotonic cumulative count** of detector
    confirmations (a fresh, plausible detection that re-seeded the box). The tick
    resets its reactive-tick "since last confirmation" clock whenever this count
    advances — which is robust even if the tick samples the mailbox slower than the
    worker publishes (it can never *miss* a confirmation, because the count is
    carried forward on every subsequent observation). ``frame_id`` is the camera
    capture id of the frame this observation was computed from (``None`` if the
    frame source does not expose one) — so a consumer can tell WHICH frame the
    detector saw and spot a frozen source. ``seq`` is the analogous count of *all*
    published observations (diagnostics / freshness). ``published_at`` is
    ``time.monotonic()`` at publish time (diagnostics / staleness logging).
    """

    target_visible: bool
    bbox: Optional[Bbox]
    score: float
    confirmations: int
    frame_id: Optional[int]
    seq: int
    published_at: float


class PerceptionWorker:
    """Runs ``tracker.update()`` off-thread and publishes the latest observation.

    Parameters
    ----------
    frame_source:
        ``() -> (frame, frame_id)`` returning the newest decoded camera frame (or
        ``None``) and its camera capture id (or ``None`` if the source has none),
        read atomically. Typically the backend's ``_frame_with_id`` (which reads the
        OWNED :class:`~yalp.camera.Camera`'s single-slot latest frame + capture id).
    get_tracker:
        ``() -> tracker`` returning the (lazily built) tracker. Called only while
        active, so the OpenCV detector import cost is deferred to the first FOLLOW
        cycle and is paid on THIS thread, never on a caller's.
    is_active:
        ``() -> bool``; the worker only runs the detector while this is True
        (i.e. the backend is in FOLLOW). While inactive it idles cheaply and
        publishes nothing, so a robot that never follows never runs inference.
    name:
        Thread name (diagnostics).
    """

    def __init__(
        self,
        *,
        frame_source: Callable[[], Tuple[object, Optional[int]]],
        get_tracker: Callable[[], object],
        is_active: Callable[[], bool],
        name: str = "yalp-perception",
    ) -> None:
        self._frame_source = frame_source
        self._get_tracker = get_tracker
        self._is_active = is_active
        self._name = name

        # Single-slot, last-write-wins result mailbox (cheap lock).
        self._lock = threading.Lock()
        self._obs: Optional[Observation] = None
        self._seq = 0
        self._confirmations = 0

        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        # True once start() has spun the async thread. Latches ON for good: even
        # after the thread stops or crashes we never fall back to inline pumping,
        # so a slow/dead worker can never re-inject the heavy call onto the tick.
        self._started_async = False

        #: True if the worker thread has crashed (its update raised). Latched.
        self.crashed = False
        self._crash_logged = False

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> "PerceptionWorker":
        """Spin the daemon worker thread (idempotent)."""
        if self._thread is not None and self._thread.is_alive():
            return self
        self._stop.clear()
        self._started_async = True
        self._thread = threading.Thread(target=self._run, name=self._name, daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout: float = 2.0) -> None:
        """Signal the worker thread to exit and join it (idempotent)."""
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
            self._thread = None

    @property
    def started_async(self) -> bool:
        return self._started_async

    # -- result access -------------------------------------------------------
    def latest(self) -> Optional[Observation]:
        """The freshest published observation, or ``None`` (non-blocking)."""
        with self._lock:
            return self._obs

    def poll(self) -> Optional[Observation]:
        """Return the freshest observation for the tick — NEVER blocks.

        In async mode (``start()`` was called) this is a pure, cheap read of the
        latest published :class:`Observation`; the detector is running on the
        worker thread. In synchronous-pump mode (``start()`` never called) it runs
        exactly one :meth:`_run_once` inline first so the tracker still produces
        observations 1:1 with ticks (deterministic tests / laptop demos). Either
        way it swallows a raising update so the tick can never throw because of the
        worker.
        """
        if not self._started_async:
            try:
                self._run_once()
            except Exception:  # a crashing detector must never break the tick
                self._note_crash("perception update crashed (synchronous pump)")
        with self._lock:
            return self._obs

    # -- worker loop ---------------------------------------------------------
    def _run(self) -> None:
        """Loop frame → update → publish until stopped; contain any crash."""
        while not self._stop.is_set():
            try:
                did_work = self._run_once()
            except Exception:
                # A raising tracker.update() must not spin or take the process
                # down: log loudly ONCE and exit. The last observation then ages
                # out and the controller degrades to a 'stale'/'lost' clean stop.
                self._note_crash(
                    "perception worker crashed; FOLLOW observations will go STALE"
                )
                return
            # Pace: idle sleep when not following, a tiny yield when active (the
            # detector's own latency paces real work). Event.wait also delivers the
            # stop signal promptly.
            pause = ACTIVE_YIELD_S if did_work else IDLE_PERIOD_S
            if self._stop.wait(pause):
                break

    def _run_once(self) -> bool:
        """One perception cycle: read latest frame, update tracker, publish.

        Returns True if the detector ran this cycle (active), False if idle. Raises
        only if ``tracker.update`` itself raises — callers contain that.
        """
        if not self._is_active():
            return False
        tracker = self._get_tracker()
        # Newest frame + its capture id (single-slot, atomic); frame may be None.
        frame, frame_id = self._frame_source()
        result = tracker.update(frame)
        # A fresh detector confirmation = a plausible detection re-seeded the box
        # this cycle (the tracker resets its own counter to 0 and holds a bbox).
        confirmed = (
            result.bbox is not None
            and int(result.ticks_since_last_detector_confirmation) == 0
        )
        with self._lock:
            self._seq += 1
            if confirmed:
                self._confirmations += 1
            self._obs = Observation(
                target_visible=bool(result.target_visible),
                bbox=tuple(result.bbox) if result.bbox is not None else None,
                score=float(result.score),
                confirmations=self._confirmations,
                frame_id=frame_id,
                seq=self._seq,
                published_at=time.monotonic(),
            )
        return True

    def _note_crash(self, message: str) -> None:
        """Latch the crash flag and log the traceback loudly exactly once."""
        self.crashed = True
        if not self._crash_logged:
            self._crash_logged = True
            logger.exception("%s", message)


__all__ = ["Observation", "PerceptionWorker", "IDLE_PERIOD_S", "ACTIVE_YIELD_S"]
