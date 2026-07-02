"""``RemoteReactiveBackend`` — drive a reactive layer running on ANOTHER machine.

This is the cross-machine glue the two-process topology has always implied but
never shipped (``docs/technical/pi-bringup.md`` §3 called it "integration work,
not a CLI flag"). It lets ``yalp agent`` on the laptop pilot the reactive loop on
the Pi over the exact same line-framed socket contract the tests already exercise:
Intents up, ``RobotState`` snapshots down, single-slot last-write-wins, monotonic
``seq`` (``docs/technical/software-spec.md`` §2.2).

Layering (why this lives in ``deliberative/`` and not ``reactive/``)
--------------------------------------------------------------------
The reactive layer is the SERVER (``yalp.contract.ipc.ReactiveServer``) and owns
the body; it must never import a client concern. So the CLIENT-side wrapper that
speaks to a remote reactive server belongs on the deliberative side. This keeps
the one-way import rule intact — ``reactive/`` stays free of any knowledge that a
remote deliberative brain exists.

What it implements
------------------
The surface the agent + ``agent_cli`` actually consume, spoken over a
:class:`~yalp.contract.ipc.DeliberativeClient`:

  * ``send_intent(intent)`` / ``apply_intent(intent)`` — push an Intent to the Pi.
    (``send_intent`` is the duck-typed name :class:`~yalp.deliberative.agent.Agent`
    calls; ``apply_intent`` mirrors the reactive
    :class:`~yalp.reactive.backend.ReactiveBackend` surface for parity.)
  * ``request_state(timeout=...)`` — pull the newest published ``RobotState``.
  * ``get_state()`` — the last snapshot without any I/O.
  * ``camera()`` — see below (a LOCAL camera).

Degradation contract (software-spec.md §5 / ``tests/test_wifi_degradation.py``)
-------------------------------------------------------------------------------
When the WiFi link drops, the reactive layer on the Pi keeps ticking and stays
safe on its own (that guarantee is proven, over loopback, in the wifi-degradation
tests). This client mirrors that on the deliberative side WITHOUT ever wedging the
agent loop:

  * Sends/reads that fail mark the link disconnected and return immediately — an
    Intent is simply dropped (the agent re-issues it next step; last-write-wins
    means no replay is needed), and ``request_state`` serves the LAST snapshot it
    saw so the agent still has state to reason over.
  * Reconnection is attempted with EXPONENTIAL BACKOFF owned here (not by the
    underlying client, which is configured to fail fast): between attempts we
    short-circuit so a dead link can never hammer the socket or block a tick.
  * ``connected`` exposes the live link state for callers/tests.

Camera stays LOCAL (known follow-up)
------------------------------------
``describe_scene`` reads the MACHINE-LOCAL camera (the laptop webcam in dev), via
:meth:`camera`. Streaming stills from the **Pi's** camera back to the laptop over
the contract is a deliberate, documented FOLLOW-UP — it is **not built here**. In
remote mode the robot's *body* is real (on the Pi) while its *eyes* are still the
laptop's; wiring remote vision is tracked separately.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Optional

from .. import config
from ..contract.ipc import DeliberativeClient
from ..contract.messages import Intent, RobotState

log = logging.getLogger(__name__)


class RemoteReactiveBackend:
    """Deliberative-side client that pilots a reactive layer on another machine.

    Parameters
    ----------
    host / port:
        Address of the remote :class:`~yalp.contract.ipc.ReactiveServer` (e.g. the
        Pi at ``izzy.local:8765``).
    camera / camera_source:
        The LOCAL camera used by ``describe_scene``. Pass an explicit ``Camera``
        (tests/headless) or a source string (``"webcam"`` default, ``"synthetic"``
        for a no-camera run). Built + started lazily on first :meth:`camera` call
        so constructing the backend never opens a device.
    connect_timeout:
        Per-attempt socket connect timeout.
    backoff / max_backoff:
        Exponential-backoff schedule for reconnection after a dropped link. The
        underlying client is configured to fail fast (single quick retry) so THIS
        layer owns the pacing.
    client:
        Injectable :class:`~yalp.contract.ipc.DeliberativeClient` (tests). When
        omitted one is built for ``host:port`` with fail-fast reconnect.
    """

    def __init__(
        self,
        host: str = config.IPC_HOST,
        port: int = config.IPC_PORT,
        *,
        camera: Optional[Any] = None,
        camera_source: str = "webcam",
        connect_timeout: float = 2.0,
        backoff: float = 0.1,
        max_backoff: float = 2.0,
        client: Optional[DeliberativeClient] = None,
    ) -> None:
        self.host = host
        self.port = int(port)
        self._client = client or DeliberativeClient(
            host,
            int(port),
            connect_timeout=connect_timeout,
            # Fail FAST inside the client; THIS class owns the real backoff so a
            # dropped link never blocks the agent for tens of seconds.
            reconnect_retries=1,
            reconnect_backoff=backoff,
            reconnect_max_backoff=max_backoff,
        )

        # LOCAL camera (see module docstring: remote Pi stills are a follow-up).
        self._camera = camera
        self._camera_source = camera_source
        self._camera_started = False

        # Link state + backoff schedule.
        self._connected = False
        self._last_state: Optional[RobotState] = None
        self._base_backoff = float(backoff)
        self._max_backoff = float(max_backoff)
        self._backoff = float(backoff)
        self._next_attempt_at = 0.0  # monotonic deadline; <= now means "may try"
        self._lock = threading.Lock()

    # -- lifecycle -----------------------------------------------------------
    def connect(self) -> "RemoteReactiveBackend":
        """Best-effort initial connect; never raises if the Pi isn't up yet.

        Degrades gracefully exactly like a mid-run drop: a failed initial connect
        just leaves the link marked disconnected with a backoff scheduled, and the
        agent loop keeps running (reads serve ``None``/last-known until the Pi
        appears). Returns ``self`` for chaining.
        """
        self._try_reconnect(force=True)
        if not self._connected:
            log.warning(
                "reactive server at %s:%s not reachable yet — will retry with "
                "backoff; the agent stays up and degrades until it connects.",
                self.host,
                self.port,
            )
        return self

    def close(self) -> None:
        """Close the socket and release the local camera. Idempotent, never raises."""
        try:
            self._client.close()
        except Exception:  # noqa: BLE001 - teardown is best-effort
            pass
        if self._camera_started and self._camera is not None:
            try:
                self._camera.stop()
            except Exception:  # noqa: BLE001
                pass
            self._camera_started = False

    def __enter__(self) -> "RemoteReactiveBackend":
        return self.connect()

    def __exit__(self, *_exc) -> None:
        self.close()

    @property
    def connected(self) -> bool:
        """Whether the last send/receive succeeded (live link)."""
        return self._connected

    # -- intent path ---------------------------------------------------------
    def send_intent(self, intent: Intent) -> None:
        """Push one ``Intent`` to the remote reactive layer (agent-facing name).

        On a dropped link the intent is dropped and the link is marked
        disconnected — the agent re-issues on its next step, and single-slot
        last-write-wins means nothing needs to be replayed.
        """
        if not self._ensure_connected():
            return
        try:
            self._client.send_intent(intent)
            self._on_success()
        except (OSError, ConnectionError) as exc:
            self._on_failure(exc)

    def apply_intent(self, intent: Intent) -> None:
        """Alias for :meth:`send_intent` — mirrors the reactive backend surface."""
        self.send_intent(intent)

    # -- state path ----------------------------------------------------------
    def request_state(self, timeout: float = 2.0) -> Optional[RobotState]:
        """Pull the newest published ``RobotState`` (single-slot last-write-wins).

        While disconnected this serves the LAST snapshot we saw (or ``None`` if we
        never connected) so the agent still has state to reason over and degrades
        exactly as the wifi-degradation contract specifies — the robot itself
        stays safe on the Pi regardless of what the laptop believes.
        """
        if not self._ensure_connected():
            return self._last_state
        try:
            state = self._client.request_state(timeout=timeout)
            self._on_success()
        except (OSError, ConnectionError) as exc:
            self._on_failure(exc)
            return self._last_state
        if state is not None:
            # The client already drains to the NEWEST line (last-write-wins); we
            # simply cache it. A remote reactive restart resets its monotonic
            # clock, so we deliberately do NOT reject a lower ``ts`` here — that
            # would wrongly freeze state across a Pi reboot.
            self._last_state = state
        return self._last_state

    def get_state(self) -> RobotState:
        """Return the last snapshot without any I/O (empty default if none yet)."""
        return self._last_state if self._last_state is not None else RobotState()

    # -- camera (LOCAL — see module docstring) -------------------------------
    def camera(self) -> Optional[Any]:
        """Return the LOCAL camera for ``describe_scene``, building/starting lazily.

        Remote stills from the Pi camera are a documented follow-up; in remote mode
        the eyes are the laptop's. Built + started on first use so no device opens
        unless vision is actually requested.
        """
        if self._camera is None:
            from ..camera import Camera

            self._camera = Camera(source=self._camera_source)
        if not self._camera_started:
            self._camera.start()
            self._camera_started = True
        return self._camera

    def capture_still(self) -> Optional[Any]:
        """Grab the newest decoded frame from the LOCAL camera, or ``None``."""
        cam = self.camera()
        return None if cam is None else cam.latest()

    # -- connection / backoff internals --------------------------------------
    def _ensure_connected(self) -> bool:
        """True if the link is (or just became) usable; False while backing off."""
        if self._connected:
            return True
        now = time.monotonic()
        if now < self._next_attempt_at:
            return False  # inside the backoff window — don't hammer the socket
        return self._try_reconnect()

    def _try_reconnect(self, force: bool = False) -> bool:
        """Attempt a single reconnect, honoring the backoff window unless forced."""
        with self._lock:
            now = time.monotonic()
            if not force and now < self._next_attempt_at:
                return False
            try:
                self._client.close()
                self._client.connect(
                    retries=1,
                    backoff=self._base_backoff,
                    max_backoff=self._max_backoff,
                )
                self._mark_connected()
                return True
            except (OSError, ConnectionError) as exc:
                self._schedule_retry(exc)
                return False

    def _on_success(self) -> None:
        with self._lock:
            self._mark_connected()

    def _on_failure(self, exc: Exception) -> None:
        with self._lock:
            self._schedule_retry(exc)

    def _mark_connected(self) -> None:
        # Caller holds the lock.
        if not self._connected:
            log.info("connected to reactive server at %s:%s", self.host, self.port)
        self._connected = True
        self._backoff = self._base_backoff
        self._next_attempt_at = 0.0

    def _schedule_retry(self, exc: Exception) -> None:
        # Caller holds the lock.
        was_connected = self._connected
        self._connected = False
        self._next_attempt_at = time.monotonic() + self._backoff
        if was_connected:
            log.warning(
                "reactive link to %s:%s dropped (%s) — serving last snapshot; "
                "retrying with backoff.",
                self.host,
                self.port,
                exc,
            )
        self._backoff = min(self._backoff * 2.0, self._max_backoff)


__all__ = ["RemoteReactiveBackend"]
