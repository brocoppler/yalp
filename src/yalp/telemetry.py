"""The reactive flight recorder — JSONL telemetry of what the robot saw and did.

Mapping and memory are explicitly "down the line" (see roadmap.md). The cheap
insurance that keeps those doors open is recording, from day one, a stable,
replayable log of the robot's perception and actions in a format any future
SLAM/memory consumer can read. That is this module.

What it records
---------------
:class:`TelemetryLogger` is an **observer** plugged into the reactive tick core's
seam (:meth:`~yalp.reactive.tick_core.ReactiveTickCore.on_intent_adopted` /
``on_motor_command`` / ``on_tick_complete``) plus the motor watchdog's trip edge.
It writes one JSON object per line (JSONL), each stamped with a **monotonic** ts
(for ordering / deltas) AND a **wall-clock** ts (for wall alignment), an event
``type``, and a structured ``payload``:

  * ``intent_adopted``  — the full :class:`~yalp.contract.messages.Intent` (contract dict).
  * ``motor_command``   — ``{"left", "right"}`` signed wheel throttles, every tick.
  * ``state_sample``    — the full :class:`~yalp.contract.messages.RobotState`,
                          RATE-LIMITED (default ~2 Hz) so the log stays small.
  * ``state_transition``— the full ``RobotState``, emitted ALWAYS on any ``mode``
                          or ``goal_status`` change (never rate-limited, so no
                          transition is ever missed between samples).
  * ``watchdog_trip``   — the independent motor watchdog fired (always).
  * ``drop_summary``    — written by the writer thread itself when the bounded
                          queue overflowed and events had to be dropped.

Never blocks the tick (the whole point)
---------------------------------------
The safety loop must never pay for telemetry. The observer hooks run on the TICK
thread and only ever **enqueue** onto a BOUNDED queue — an O(1), non-blocking
``put_nowait``. A dedicated daemon writer thread drains the queue and does all the
disk I/O (JSON encoding + the rotating write). If the queue is full the event is
**dropped** and a counter is incremented (never a block, never an exception into
the tick); the writer periodically flushes a ``drop_summary`` so the loss is
itself visible in the record. Clean :meth:`close` flushes the queue to disk.

Storage: rotating, size-capped JSONL under ``$XDG_STATE_HOME/yalp/telemetry`` (see
:func:`yalp.config._default_telemetry_dir`), default 10 MB × 5 backups. Enabled by
default; ``YALP_TELEMETRY=0`` disables it entirely (see :func:`create_logger_from_env`).

Reading it back: :func:`read_events` is the seam future mapping/memory consumers
use — a generator that yields parsed events and skips (counts) corrupt lines.

Privacy / contract hygiene: payloads are the structured contract objects ONLY
(``Intent`` / ``RobotState`` dicts, motor throttles). This module never logs
secrets, the environment, or a config dump.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, Optional

from . import config

logger = logging.getLogger(__name__)

#: Default JSONL filename inside the telemetry directory.
DEFAULT_FILENAME = "telemetry.jsonl"

# Sentinel pushed onto the queue by close() to tell the writer thread to finish.
_SENTINEL = object()

# Event type names (the stable contract future consumers key on).
EVENT_INTENT_ADOPTED = "intent_adopted"
EVENT_MOTOR_COMMAND = "motor_command"
EVENT_STATE_SAMPLE = "state_sample"
EVENT_STATE_TRANSITION = "state_transition"
EVENT_WATCHDOG_TRIP = "watchdog_trip"
EVENT_DROP_SUMMARY = "drop_summary"


def _json_default(obj: Any) -> Any:
    """Coerce anything not natively JSON-serializable into a JSON-native value.

    Contract ``to_dict()`` output is already JSON-native, but FOLLOW goal payloads
    can carry numpy scalars/arrays (bbox coords from the tracker). Rather than let
    one odd value crash the writer, convert numpy scalars via ``.item()``, arrays
    via ``.tolist()``, and fall back to ``str()`` — the record survives.
    """
    item = getattr(obj, "item", None)
    if callable(item):  # numpy scalar
        try:
            return item()
        except Exception:  # pragma: no cover - defensive
            pass
    tolist = getattr(obj, "tolist", None)
    if callable(tolist):  # numpy array
        try:
            return tolist()
        except Exception:  # pragma: no cover - defensive
            pass
    return str(obj)


class _RotatingWriter:
    """A tiny size-capped, rotating JSONL file writer (writer-thread only).

    Deliberately self-contained (not ``logging.handlers.RotatingFileHandler``) so
    the byte accounting and rotation are exactly what the tests assert and there is
    no global logging-manager state to leak. Rotation mirrors the familiar scheme:
    ``telemetry.jsonl`` is the live file; on rollover it becomes ``.1`` and the
    older backups shift up to ``.<backup_count>`` (the oldest is discarded).
    """

    def __init__(self, path: Path, max_bytes: int, backup_count: int) -> None:
        self._path = Path(path)
        self._max_bytes = max(0, int(max_bytes))
        self._backup_count = max(0, int(backup_count))
        self._file = None  # type: Optional[Any]
        self._size = 0
        self._open_existing()

    def _open_existing(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Append so a restart continues the current file rather than clobbering it.
        self._file = self._path.open("a", encoding="utf-8")
        try:
            self._size = self._path.stat().st_size
        except OSError:  # pragma: no cover - just-created / racy stat
            self._size = 0

    def _sibling(self, i: int) -> Path:
        return self._path.with_name(f"{self._path.name}.{i}")

    def write(self, line: str) -> None:
        """Write one already-serialized line (a trailing newline is ensured)."""
        data = line if line.endswith("\n") else line + "\n"
        nbytes = len(data.encode("utf-8"))
        # Roll BEFORE writing a record that would exceed the cap — but never roll a
        # still-empty file (so a single oversized line still lands, just alone).
        if self._max_bytes and self._size > 0 and self._size + nbytes > self._max_bytes:
            self._rotate()
        assert self._file is not None
        self._file.write(data)
        self._file.flush()
        self._size += nbytes

    def _rotate(self) -> None:
        if self._file is not None:
            self._file.close()
        if self._backup_count <= 0:
            # No backups kept: just start the live file over.
            self._file = self._path.open("w", encoding="utf-8")
            self._size = 0
            return
        # Discard the oldest, then shift .n-1 -> .n, ..., .1 -> .2, live -> .1.
        oldest = self._sibling(self._backup_count)
        try:
            if oldest.exists():
                oldest.unlink()
            for i in range(self._backup_count - 1, 0, -1):
                src = self._sibling(i)
                if src.exists():
                    src.replace(self._sibling(i + 1))
            if self._path.exists():
                self._path.replace(self._sibling(1))
        except OSError:  # pragma: no cover - best effort; keep recording
            logger.exception("telemetry: log rotation failed; continuing")
        self._file = self._path.open("w", encoding="utf-8")
        self._size = 0

    def close(self) -> None:
        if self._file is not None:
            try:
                self._file.flush()
                self._file.close()
            except Exception:  # pragma: no cover - best effort on teardown
                pass
            self._file = None


class TelemetryLogger:
    """A non-blocking JSONL flight recorder for the reactive layer.

    Plug it in as the reactive backends' ``observer`` (it implements the tick
    core's ``on_intent_adopted`` / ``on_motor_command`` / ``on_tick_complete``
    seam and the watchdog's ``on_watchdog_trip``). All hooks run on the TICK
    thread and only enqueue; a daemon writer thread does the disk I/O.

    Parameters
    ----------
    directory:
        Where the rotating JSONL file lives.
    filename:
        The live file name (rotated backups get ``.1`` .. ``.<backup_count>``).
    max_bytes / backup_count:
        Size cap per file and how many rotated backups to keep.
    sample_hz:
        Rate cap for the periodic ``state_sample`` events (``<= 0`` = every tick).
        ``state_transition`` / ``watchdog_trip`` are never rate-limited.
    queue_max:
        Bounded queue depth; overflow drops events (counted) rather than blocking.
    clock / wall_clock:
        Injectable time sources (monotonic + wall). Injectable so tests drive
        rate-limiting deterministically.
    """

    def __init__(
        self,
        *,
        directory: os.PathLike | str,
        filename: str = DEFAULT_FILENAME,
        max_bytes: int = 10 * 1024 * 1024,
        backup_count: int = 5,
        sample_hz: float = 2.0,
        queue_max: int = 10000,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self._clock = clock
        self._wall_clock = wall_clock
        self._path = Path(directory) / filename
        self._writer = _RotatingWriter(self._path, max_bytes, backup_count)

        # Rate-limit state for state_sample (transitions bypass this).
        self._min_sample_interval = (1.0 / sample_hz) if sample_hz and sample_hz > 0 else 0.0
        self._last_sample_ts: Optional[float] = None

        # Transition tracking. Start at sentinels so the FIRST tick logs a
        # transition capturing the initial mode/goal_status.
        self._last_mode: Any = _SENTINEL
        self._last_goal_status: Any = _SENTINEL

        # Bounded producer/consumer queue. put_nowait on the tick thread; the
        # writer drains. maxsize<=0 would be UNBOUNDED (a memory-leak trap under a
        # slow disk), so floor it at 1.
        self._queue: "queue.Queue[Any]" = queue.Queue(maxsize=max(1, int(queue_max)))

        # Drop bookkeeping (incremented on the tick thread under a tiny lock;
        # summarized on the writer thread).
        self._drop_lock = threading.Lock()
        self._dropped = 0
        self._reported_dropped = 0
        self._last_summary_ts = 0.0
        self._summary_min_interval = 1.0  # at most one drop_summary per second
        self._writer_poll_s = 0.5  # writer wakeups when idle (drop flush + stop)

        self._closed = False
        self._thread = threading.Thread(
            target=self._run_writer, name="yalp-telemetry", daemon=True
        )
        self._thread.start()

    # -- properties ----------------------------------------------------------
    @property
    def path(self) -> Path:
        """The live JSONL file path."""
        return self._path

    @property
    def dropped(self) -> int:
        """Total events dropped so far due to queue overflow (diagnostics)."""
        with self._drop_lock:
            return self._dropped

    @property
    def closed(self) -> bool:
        """True once :meth:`close` has been called (writer flushed + stopped)."""
        return self._closed

    # -- observer seam (TICK THREAD: enqueue only, never block/raise) --------
    def on_intent_adopted(self, intent: Any) -> None:
        payload = intent.to_dict() if hasattr(intent, "to_dict") else _as_dict(intent)
        self._enqueue(EVENT_INTENT_ADOPTED, payload)

    def on_motor_command(self, left: float, right: float) -> None:
        self._enqueue(EVENT_MOTOR_COMMAND, {"left": float(left), "right": float(right)})

    def on_tick_complete(self, state: Any) -> None:
        """Decide + enqueue state_transition (always) and state_sample (capped)."""
        mode = getattr(state, "mode", None)
        goal_status = getattr(state, "goal_status", None)
        is_transition = (mode != self._last_mode) or (goal_status != self._last_goal_status)
        self._last_mode = mode
        self._last_goal_status = goal_status

        now = self._clock()
        do_sample = (
            self._min_sample_interval <= 0.0
            or self._last_sample_ts is None
            or (now - self._last_sample_ts) >= self._min_sample_interval
        )
        if not (is_transition or do_sample):
            return  # rate-limited and nothing changed: cheapest possible path

        payload = state.to_dict() if hasattr(state, "to_dict") else _as_dict(state)
        if is_transition:
            self._enqueue(EVENT_STATE_TRANSITION, payload)
        if do_sample:
            self._last_sample_ts = now
            self._enqueue(EVENT_STATE_SAMPLE, payload)

    def on_watchdog_trip(
        self, timeout_ms: Optional[float] = None, trip_count: Optional[int] = None
    ) -> None:
        payload: Dict[str, Any] = {}
        if timeout_ms is not None:
            payload["timeout_ms"] = float(timeout_ms)
        if trip_count is not None:
            payload["trip_count"] = int(trip_count)
        self._enqueue(EVENT_WATCHDOG_TRIP, payload)

    # -- internals -----------------------------------------------------------
    def _make_event(self, event_type: str, payload: Any) -> Dict[str, Any]:
        return {
            "ts_monotonic": self._clock(),
            "ts_wall": self._wall_clock(),
            "type": event_type,
            "payload": payload,
        }

    def _enqueue(self, event_type: str, payload: Any) -> None:
        """Enqueue an event; drop (and count) on overflow. Never blocks/raises."""
        if self._closed:
            return
        event = self._make_event(event_type, payload)
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            with self._drop_lock:
                self._dropped += 1
        except Exception:  # pragma: no cover - telemetry must never break a tick
            with self._drop_lock:
                self._dropped += 1

    def _run_writer(self) -> None:
        """Drain the queue to disk until the sentinel; flush drop summaries."""
        try:
            while True:
                try:
                    item = self._queue.get(timeout=self._writer_poll_s)
                except queue.Empty:
                    self._maybe_write_drop_summary()
                    continue
                if item is _SENTINEL:
                    self._drain_remaining()
                    self._maybe_write_drop_summary(force=True)
                    break
                self._write_event(item)
                self._maybe_write_drop_summary()
        finally:
            self._writer.close()

    def _drain_remaining(self) -> None:
        """Write everything still queued (called after the shutdown sentinel)."""
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if item is _SENTINEL:
                continue
            self._write_event(item)

    def _write_event(self, event: Dict[str, Any]) -> None:
        try:
            line = json.dumps(event, separators=(",", ":"), default=_json_default)
        except Exception:  # pragma: no cover - _json_default makes this unlikely
            logger.exception("telemetry: failed to encode a %r event", event.get("type"))
            return
        try:
            self._writer.write(line)
        except Exception:  # pragma: no cover - disk error must not kill the thread
            logger.exception("telemetry: failed to write an event to disk")

    def _maybe_write_drop_summary(self, force: bool = False) -> None:
        """Emit a drop_summary when the queue has overflowed (writer thread only)."""
        with self._drop_lock:
            dropped = self._dropped
        if dropped <= self._reported_dropped:
            return
        now = self._clock()
        if not force and (now - self._last_summary_ts) < self._summary_min_interval:
            return
        delta = dropped - self._reported_dropped
        self._reported_dropped = dropped
        self._last_summary_ts = now
        # Built + written directly on the writer thread, bypassing the (possibly
        # full) queue so the loss is always recorded.
        self._write_event(
            self._make_event(EVENT_DROP_SUMMARY, {"dropped": delta, "dropped_total": dropped})
        )

    # -- lifecycle -----------------------------------------------------------
    def close(self, timeout: float = 5.0) -> None:
        """Flush the queue to disk and stop the writer thread. Idempotent."""
        if self._closed:
            return
        self._closed = True
        try:
            self._queue.put(_SENTINEL, timeout=timeout)
        except queue.Full:  # pragma: no cover - writer wedged; force it down anyway
            pass
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def __enter__(self) -> "TelemetryLogger":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def _as_dict(obj: Any) -> Dict[str, Any]:
    """Best-effort structured view of a contract object without a ``to_dict``."""
    if isinstance(obj, dict):
        return dict(obj)
    data = getattr(obj, "__dict__", None)
    return dict(data) if isinstance(data, dict) else {"repr": repr(obj)}


def create_logger_from_env(*, clock: Optional[Callable[[], float]] = None) -> Optional[TelemetryLogger]:
    """Build a :class:`TelemetryLogger` from the environment, or ``None`` if off.

    Reads the ``YALP_TELEMETRY*`` settings via :mod:`yalp.config`'s fail-soft
    accessors AT CALL TIME (so ``YALP_TELEMETRY=0`` disables it and tests can
    toggle it without a module reload). Returns ``None`` when disabled or when the
    logger cannot be constructed — telemetry must never prevent a run from
    starting. This is the injection point wired into real run loops.
    """
    if not config._env_bool("YALP_TELEMETRY", config.TELEMETRY_ENABLED):
        return None
    kwargs: Dict[str, Any] = {}
    if clock is not None:
        kwargs["clock"] = clock
    try:
        return TelemetryLogger(
            directory=config._env_str("YALP_TELEMETRY_DIR", config.TELEMETRY_DIR),
            max_bytes=config._env_int("YALP_TELEMETRY_MAX_BYTES", config.TELEMETRY_MAX_BYTES),
            backup_count=config._env_int(
                "YALP_TELEMETRY_BACKUP_COUNT", config.TELEMETRY_BACKUP_COUNT
            ),
            sample_hz=config._env_float("YALP_TELEMETRY_SAMPLE_HZ", config.TELEMETRY_SAMPLE_HZ),
            queue_max=config._env_int("YALP_TELEMETRY_QUEUE_MAX", config.TELEMETRY_QUEUE_MAX),
            **kwargs,
        )
    except Exception:  # pragma: no cover - telemetry must not break startup
        logger.exception("telemetry: could not start; continuing without it")
        return None


def read_events(
    path: os.PathLike | str, *, stats: Optional[Dict[str, int]] = None
) -> Iterator[Dict[str, Any]]:
    """Yield parsed telemetry events from a JSONL file, skipping corrupt lines.

    The seam future mapping / memory consumers read the flight recorder through.
    Corrupt (non-JSON) lines are skipped rather than raising — a truncated final
    line from a hard power-cut, or a partially-written record, must not stop a
    replay. Pass a mutable ``stats`` dict to receive the running counters
    ``{"events": <yielded>, "corrupt": <skipped>}`` (updated as iteration proceeds
    and finalized when the file is exhausted) — the "counter" the recorder owes
    its readers, exposed without breaking the generator contract.
    """
    corrupt = 0
    events = 0
    p = Path(path)
    with p.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except (ValueError, TypeError):
                corrupt += 1
                if stats is not None:
                    stats["corrupt"] = corrupt
                continue
            events += 1
            if stats is not None:
                stats["events"] = events
                stats["corrupt"] = corrupt
            yield obj
    if stats is not None:
        stats["events"] = events
        stats["corrupt"] = corrupt


__all__ = [
    "TelemetryLogger",
    "create_logger_from_env",
    "read_events",
    "DEFAULT_FILENAME",
    "EVENT_INTENT_ADOPTED",
    "EVENT_MOTOR_COMMAND",
    "EVENT_STATE_SAMPLE",
    "EVENT_STATE_TRANSITION",
    "EVENT_WATCHDOG_TRIP",
    "EVENT_DROP_SUMMARY",
]
