"""Line-framed localhost socket transport for the loop-to-loop contract.

Implements the wire described in ``docs/technical/software-spec.md`` §2.2:

  * One TCP socket on ``127.0.0.1`` (a Unix-domain socket once co-located on the
    Pi — same line protocol, different address family).
  * Each direction is a stream of UTF-8 JSON objects, **one per ``\\n``-terminated
    line** (newline-delimited JSON).
  * The **reactive process is the server**: it owns the camera/GPIO, publishes the
    latest ``RobotState`` every tick (newest wins; older unread snapshots are
    dropped), and reads ``Intent`` lines into a **single-slot, last-write-wins**
    mailbox. Its socket work is non-blocking and off the tick's critical path.
  * The **deliberative process is the client**: it connects (with backoff +
    reconnect), sends ``Intent`` lines, and *pulls* the latest ``RobotState``
    snapshot. It can never make the reactive tick block.

Standard library only.
"""

from __future__ import annotations

import json
import select
import socket
import threading
import time
from typing import Optional

from .. import config
from .messages import Intent, RobotState

# A small control line the client sends to explicitly pull the latest state.
TYPE_REQUEST = "request"
_REQUEST_LINE = (json.dumps({"type": TYPE_REQUEST}) + "\n").encode("utf-8")


class _LineBuffer:
    """Accumulate bytes and split them into complete UTF-8 lines."""

    def __init__(self) -> None:
        self._buf = b""

    def feed(self, data: bytes) -> list[str]:
        self._buf += data
        out: list[str] = []
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            line = line.strip()
            if line:
                out.append(line.decode("utf-8", errors="replace"))
        return out


class IntentMailbox:
    """Single-slot, last-write-wins intent mailbox (software-spec.md §2.2).

    Stores only the highest ``seq`` seen and overwrites on arrival — there is no
    queue and no buffer. A later-arriving *lower*-seq intent (e.g. a stale one
    from a half-open old socket) is ignored, so it can never overwrite a newer
    instruction.
    """

    def __init__(self) -> None:
        self._slot: Optional[Intent] = None
        self._lock = threading.Lock()

    def put(self, intent: Intent) -> None:
        with self._lock:
            if self._slot is None or intent.seq >= self._slot.seq:
                self._slot = intent

    def take(self) -> Optional[Intent]:
        """Return and clear the stored intent (consume it), or ``None``."""
        with self._lock:
            slot, self._slot = self._slot, None
            return slot

    def peek(self) -> Optional[Intent]:
        """Return the stored intent without consuming it, or ``None``."""
        with self._lock:
            return self._slot


class ReactiveServer:
    """Server side (reactive layer) of the line-framed socket.

    Accepts a single deliberative client at a time (a newer connection replaces
    an older one). A background reader thread drains ``Intent`` lines into the
    single-slot mailbox and answers explicit state requests. ``publish(state)``
    is called by the reactive run loop every tick; it stores the latest snapshot
    and best-effort pushes it to the connected client, dropping the send if the
    socket would block (newest wins) so it never stalls the tick.
    """

    def __init__(
        self,
        host: str = config.IPC_HOST,
        port: int = config.IPC_PORT,
        mailbox: Optional[IntentMailbox] = None,
    ) -> None:
        self.host = host
        self.port = port
        self.mailbox = mailbox or IntentMailbox()

        self._sock: Optional[socket.socket] = None
        self._client: Optional[socket.socket] = None
        self._latest = RobotState()
        self._latest_lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._running = threading.Event()
        self._client_connected = threading.Event()
        self._accept_thread: Optional[threading.Thread] = None

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> "ReactiveServer":
        """Bind, listen, and start accepting clients in the background."""
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((self.host, self.port))
        s.listen(1)
        self.port = s.getsockname()[1]  # resolve an ephemeral (port=0) bind
        self._sock = s
        self._running.set()
        self._accept_thread = threading.Thread(
            target=self._accept_loop, name="yalp-ipc-accept", daemon=True
        )
        self._accept_thread.start()
        return self

    def stop(self) -> None:
        self._running.clear()
        self._client_connected.clear()
        for sock in (self._client, self._sock):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
        self._client = None
        self._sock = None
        if self._accept_thread is not None:
            self._accept_thread.join(timeout=1.0)
            self._accept_thread = None

    def __enter__(self) -> "ReactiveServer":
        return self.start()

    def __exit__(self, *_exc) -> None:
        self.stop()

    def wait_for_client(self, timeout: float = 2.0) -> bool:
        """Block until a client has connected (or ``timeout``). Test helper."""
        return self._client_connected.wait(timeout)

    # -- publish (called from the reactive tick) -----------------------------
    def publish(self, state: RobotState) -> None:
        """Store the latest snapshot and best-effort push it to the client.

        Never blocks the caller: if the socket is not writable right now the
        push is dropped (the next tick's snapshot supersedes it anyway).
        """
        with self._latest_lock:
            self._latest = state
        self._send_to_client(state.to_json())

    # -- internals -----------------------------------------------------------
    def _accept_loop(self) -> None:
        assert self._sock is not None
        self._sock.settimeout(0.5)
        while self._running.is_set():
            try:
                conn, _addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            self._adopt_client(conn)

    def _adopt_client(self, conn: socket.socket) -> None:
        with self._send_lock:
            old, self._client = self._client, conn
        if old is not None:
            try:
                old.close()
            except OSError:
                pass
        self._client_connected.set()
        # Push the current snapshot immediately so a fresh client has state.
        self._send_to_client(self._latest_json())
        threading.Thread(
            target=self._reader_loop, args=(conn,), name="yalp-ipc-reader", daemon=True
        ).start()

    def _reader_loop(self, conn: socket.socket) -> None:
        conn.settimeout(0.5)
        lb = _LineBuffer()
        while self._running.is_set() and self._client is conn:
            try:
                data = conn.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if not data:  # client closed the connection
                break
            for line in lb.feed(data):
                self._handle_line(line)
        if self._client is conn:
            self._client = None
            self._client_connected.clear()
        try:
            conn.close()
        except OSError:
            pass

    def _handle_line(self, line: str) -> None:
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            return
        kind = d.get("type")
        if kind == "intent":
            try:
                self.mailbox.put(Intent.from_dict(d))
            except (KeyError, ValueError):
                return
        elif kind == TYPE_REQUEST:
            self._send_to_client(self._latest_json())

    def _latest_json(self) -> str:
        with self._latest_lock:
            return self._latest.to_json()

    def _send_to_client(self, text: str) -> None:
        conn = self._client
        if conn is None:
            return
        data = text.encode("utf-8")
        with self._send_lock:
            conn = self._client
            if conn is None:
                return
            try:
                _, writable, _ = select.select([], [conn], [], 0)
                if not writable:
                    return  # would block — drop, newest wins
                conn.sendall(data)
            except OSError:
                # Client vanished; the reader loop will clean up. Drop silently.
                pass


class DeliberativeClient:
    """Client side (deliberative layer) of the line-framed socket.

    Connects with exponential backoff, sends ``Intent`` lines, and pulls the
    latest ``RobotState`` snapshot. Robust to disconnects: a failed send/recv
    triggers a transparent reconnect, and because both shapes are last-write-wins
    snapshots there is no replay or buffering to recover across a reconnect.
    """

    def __init__(
        self,
        host: str = config.IPC_HOST,
        port: int = config.IPC_PORT,
        connect_timeout: float = 2.0,
    ) -> None:
        self.host = host
        self.port = port
        self.connect_timeout = connect_timeout
        self._sock: Optional[socket.socket] = None
        self._lb = _LineBuffer()
        self._latest_state: Optional[RobotState] = None

    # -- lifecycle -----------------------------------------------------------
    def connect(
        self,
        retries: int = 40,
        backoff: float = 0.05,
        max_backoff: float = 1.0,
    ) -> "DeliberativeClient":
        """Connect, retrying with exponential backoff until the server is up."""
        delay = backoff
        last_err: Optional[Exception] = None
        for _ in range(max(1, retries)):
            try:
                s = socket.create_connection(
                    (self.host, self.port), timeout=self.connect_timeout
                )
                s.setblocking(True)
                self._sock = s
                self._lb = _LineBuffer()
                return self
            except OSError as exc:
                last_err = exc
                time.sleep(min(delay, max_backoff))
                delay *= 2
        raise ConnectionError(
            f"could not connect to reactive server at {self.host}:{self.port}"
        ) from last_err

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def __enter__(self) -> "DeliberativeClient":
        return self.connect()

    def __exit__(self, *_exc) -> None:
        self.close()

    # -- send / receive ------------------------------------------------------
    def send_intent(self, intent: Intent) -> None:
        """Send one ``Intent`` line, reconnecting once on a broken socket."""
        self._send(intent.to_json().encode("utf-8"))

    def request_state(self, timeout: float = 2.0) -> Optional[RobotState]:
        """Pull the latest ``RobotState`` snapshot (last-write-wins).

        Sends an explicit state request, then drains any pushed/answered state
        lines and returns the newest one seen. Returns the last known snapshot
        (possibly ``None``) if nothing arrives within ``timeout``.
        """
        self._send(_REQUEST_LINE)
        deadline = time.monotonic() + timeout
        had_state = self._latest_state is not None
        while time.monotonic() < deadline:
            updated = self._drain()
            if updated or had_state:
                return self._latest_state
            time.sleep(0.01)
        return self._latest_state

    # -- internals -----------------------------------------------------------
    def _send(self, data: bytes) -> None:
        if self._sock is None:
            self.connect()
        assert self._sock is not None
        try:
            self._sock.sendall(data)
        except OSError:
            self.close()
            self.connect()
            assert self._sock is not None
            self._sock.sendall(data)

    def _drain(self) -> bool:
        """Read all currently-available lines (non-blocking). Returns True if a
        new ``RobotState`` was decoded."""
        if self._sock is None:
            return False
        updated = False
        while True:
            try:
                readable, _, _ = select.select([self._sock], [], [], 0)
            except OSError:
                self.close()
                return updated
            if not readable:
                break
            try:
                data = self._sock.recv(4096)
            except (BlockingIOError, socket.timeout):
                break
            except OSError:
                self.close()
                break
            if not data:  # server closed
                self.close()
                break
            for line in self._lb.feed(data):
                if self._handle_line(line):
                    updated = True
        return updated

    def _handle_line(self, line: str) -> bool:
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            return False
        if d.get("type") == "state":
            try:
                self._latest_state = RobotState.from_dict(d)
                return True
            except (KeyError, ValueError):
                return False
        return False


__all__ = [
    "IntentMailbox",
    "ReactiveServer",
    "DeliberativeClient",
]
