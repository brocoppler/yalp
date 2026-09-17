"""``yalp session`` — start / wait / status / stop / restart / logs for the
reactive server, so an SSH session never hand-rolls ``nohup`` again.

Why this exists
---------------
Operating the robot over SSH meant starting ``yalp reactive`` by hand a dozen
times a session with a fragile shell pattern: ``nohup``/``setsid`` it, poll the
port, sleep "about 4 s" for the camera (the C270 publishes black frames for ~1 s
after open), stop it with SIGINT, and occasionally chase an orphan that kept the
GPIO held. This subcommand is that pattern made first-class and deterministic:

    yalp session start                      # detach (or systemd) + wait until READY
    yalp session start --backend fake       # laptop: simulation backend
    yalp session wait --timeout 30          # block until server + camera are warm
    yalp session status                     # port / pid / one RobotState line
    yalp session stop                       # SIGINT (graceful) -> SIGTERM -> SIGKILL
    yalp session restart
    yalp session logs -n 50 -f

Two launch strategies, chosen automatically:

  * **systemd user unit** (``scripts/yalp-reactive.service``, installed by
    ``scripts/install_reactive_service.sh``): ``start``/``stop``/``logs``
    dispatch to ``systemctl --user`` / ``journalctl --user``. The unit file
    decides the backend flags and ``~/.config/yalp/reactive.env`` the ``YALP_*``
    knobs.
  * **Detached child** (no unit installed): spawn ``python -u -m yalp reactive
    --backend ...`` in its own session (``start_new_session=True``, stdin from
    ``/dev/null``, stdout+stderr appended to ``$XDG_STATE_HOME/yalp/reactive.log``)
    and record its pid in ``$XDG_STATE_HOME/yalp/reactive.pid``.

"Ready" means: a TCP connect to the IPC port succeeds AND ``request_state()``
returns a ``RobotState`` AND (with ``--camera``, the default for the real
backend) ``state.last_frame_id`` is not ``None`` — the tick core only sets that
once the camera has actually delivered a frame, so it is the honest "camera
warm" signal rather than a guessed sleep.

Stopping is graceful by design: SIGINT first, because the server's own teardown
zeroes the motors before anything else and bounds every join/close; SIGTERM is
the server's documented second-signal force-exit; SIGKILL is the last resort and
is announced LOUDLY, because a killed process can leave the camera/GPIO held —
the orphan problem this tool exists to end.

Every process-touching operation (running a command, spawning, connecting,
requesting state, checking/signalling a pid, sleeping, reading the clock,
detecting systemd) is injected through :class:`SessionDeps`, so the whole
command surface is unit-testable with no real server, no real sleeps and no
real signals. Systemd detection lives behind ONE function
(:func:`detect_systemd_unit`, reachable as ``SessionDeps.systemd()``).

Registered via the documented feature-module contract (``add_parser`` / ``run``)
and listed in ``yalp.cli.FEATURE_MODULES``.

Exit codes
----------
  * ``0`` — done (started + ready / ready / running / stopped or was not running).
  * ``1`` — not ready in time / not running / a systemctl or spawn failure.
  * ``2`` — usage error (argparse).
  * ``3`` — ``start`` refused: the IPC port is already listening (or a live pid
    from the pidfile is still starting up).
"""

from __future__ import annotations

import argparse
import os
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence, Tuple

from ..contract.messages import RobotState

UNIT_NAME = "yalp-reactive.service"
LOG_NAME = "reactive.log"
PID_NAME = "reactive.pid"

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_PORT_BUSY = 3

SYSTEMCTL_USER: Tuple[str, ...] = ("systemctl", "--user")
SYSTEMCTL_SYSTEM: Tuple[str, ...] = ("systemctl",)

# Shapes of the injectable seams (see SessionDeps).
CmdResult = Tuple[int, str]  # (returncode, captured stdout — "" when not captured)
RunCmd = Callable[..., CmdResult]  # run_cmd(argv, *, capture=False)
Spawn = Callable[[Sequence[str], Optional[str]], int]  # (argv, log_path|None) -> pid
Connect = Callable[[str, int], bool]  # (host, port) -> listening?
RequestState = Callable[[str, int, float], Optional[RobotState]]  # (host, port, timeout)


# --------------------------------------------------------------------------- #
# Paths (XDG state dir, mirroring yalp.config._default_telemetry_dir)
# --------------------------------------------------------------------------- #
def default_state_dir() -> str:
    """``$XDG_STATE_HOME/yalp`` per the XDG spec, else ``~/.local/state/yalp``.

    The same base the telemetry flight recorder uses (its files live in the
    ``telemetry/`` subdirectory). Read at call time so tests can point it at a
    tmp dir through the environment.
    """
    xdg = os.environ.get("XDG_STATE_HOME")
    base = xdg if xdg else os.path.expanduser("~/.local/state")
    return os.path.join(base, "yalp")


def read_pidfile(path: str) -> Optional[int]:
    """The pid recorded in ``path``, or ``None`` if absent / unreadable / not a pid."""
    try:
        with open(path, encoding="utf-8") as fh:
            pid = int(fh.read().strip() or 0)
    except (OSError, ValueError):
        return None
    return pid if pid > 0 else None


def write_pidfile(path: str, pid: int) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(f"{int(pid)}\n")


def remove_pidfile(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


# --------------------------------------------------------------------------- #
# Default (real) implementations of the injectable seams
# --------------------------------------------------------------------------- #
def _run_cmd(argv: Sequence[str], *, capture: bool = False) -> CmdResult:
    """Run ``argv`` to completion and return ``(returncode, stdout)``.

    With ``capture`` both streams are taken off the terminal and stdout is
    returned as text (probes such as ``systemctl cat`` / ``show -p MainPID``);
    without it the child inherits our terminal (``systemctl start``,
    ``journalctl -f``). A missing executable is exit 127, never an exception,
    so "no systemctl on this box" reads as "no unit installed".
    """
    try:
        proc = subprocess.run(
            list(argv),
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.DEVNULL if capture else None,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return 127, ""
    return proc.returncode, (proc.stdout or "")


# Popen handles for children we spawned, kept so their finalizer never emits a
# "subprocess is still running" ResourceWarning — the child is MEANT to outlive us.
_children: List[subprocess.Popen] = []


def _spawn(argv: Sequence[str], log_path: Optional[str]) -> int:
    """Spawn ``argv`` in its OWN session and return its pid.

    ``start_new_session=True`` (setsid) detaches the child from our controlling
    terminal so a later Ctrl-C or SSH hang-up in this shell can never reach it —
    only ``yalp session stop`` (or systemd) stops it. stdin is ``/dev/null``.
    With ``log_path`` stdout+stderr are APPENDED there behind a dated header
    line; ``None`` inherits our own stdout/stderr (``start --foreground``).
    """
    if log_path is None:
        proc = subprocess.Popen(
            list(argv), stdin=subprocess.DEVNULL, start_new_session=True
        )
        _children.append(proc)
        return proc.pid
    os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
    with open(log_path, "ab") as log:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        log.write(f"\n--- yalp session start {stamp}: {' '.join(argv)} ---\n".encode("utf-8"))
        log.flush()
        proc = subprocess.Popen(
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    _children.append(proc)
    return proc.pid


def _port_listening(host: str, port: int, timeout: float = 0.25) -> bool:
    """True when a TCP connect to ``host:port`` succeeds (closed immediately)."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _request_state(host: str, port: int, timeout: float = 1.0) -> Optional[RobotState]:
    """One-shot state pull: connect (no retries), request, close.

    Exactly what ``yalp drive`` does on connect, minus the patient reconnect —
    a readiness probe must fail fast. Returns ``None`` (never raises) when the
    server is not accepting or has not answered within ``timeout``.
    """
    from ..contract.ipc import DeliberativeClient

    client = DeliberativeClient(host, port, connect_timeout=timeout)
    try:
        client.connect(retries=1)
        return client.request_state(timeout=timeout)
    except OSError:  # ConnectionError is an OSError
        return None
    finally:
        client.close()


def _pid_alive(pid: int) -> bool:
    """True if ``pid`` exists (and is not an exited-but-unreaped child of ours).

    Reaps first with a non-blocking ``waitpid`` in case the pid is OUR child
    (``start --foreground``): an exited child still answers ``kill(pid, 0)``
    until reaped and would otherwise look alive forever. A pid that is not our
    child — the normal case, a pidfile from an earlier ``start`` — raises
    ``ChildProcessError`` there, which is simply ignored.
    """
    if pid <= 0:
        return False
    try:
        reaped, _status = os.waitpid(pid, os.WNOHANG)
        if reaped == pid:
            return False
    except ChildProcessError:
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    return True


def _send_signal(pid: int, signum: int) -> bool:
    """Deliver ``signum`` to ``pid``. False if the process is already gone."""
    try:
        os.kill(pid, signum)
    except ProcessLookupError:
        return False
    return True


# --------------------------------------------------------------------------- #
# Systemd detection — the ONE place it happens
# --------------------------------------------------------------------------- #
def detect_systemd_unit(run_cmd: RunCmd, unit: str = UNIT_NAME) -> Optional[Tuple[str, ...]]:
    """Return the ``systemctl`` prefix that knows ``unit``, or ``None``.

    Tries the USER manager first (``systemctl --user cat <unit>``), then the
    system one. ``cat`` exits 0 only for an installed unit; both probes are
    silent, and a missing ``systemctl`` (macOS, a container) is exit 127 which
    reads as "not installed". Everything else asks ``SessionDeps.systemd()``,
    which caches this answer for the invocation.
    """
    for prefix in (SYSTEMCTL_USER, SYSTEMCTL_SYSTEM):
        rc, _ = run_cmd([*prefix, "cat", unit], capture=True)
        if rc == 0:
            return prefix
    return None


def _config():
    from .. import config

    return config


@dataclass
class SessionDeps:
    """Every side-effecting seam ``yalp session`` uses, in one injectable bundle.

    Production uses the defaults (real subprocesses, sockets, signals, clock);
    tests replace any subset with fakes and never touch a real server. ``out``
    is where human-readable lines go (``print`` by default).
    """

    host: str = field(default_factory=lambda: _config().IPC_HOST)
    port: int = field(default_factory=lambda: _config().IPC_PORT)
    state_dir: str = field(default_factory=default_state_dir)
    unit: str = UNIT_NAME
    python: str = sys.executable
    run_cmd: RunCmd = _run_cmd
    spawn: Spawn = _spawn
    connect: Connect = _port_listening
    request_state: RequestState = _request_state
    pid_alive: Callable[[int], bool] = _pid_alive
    send_signal: Callable[[int, int], bool] = _send_signal
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic
    #: Systemd probe ``() -> systemctl prefix | None``. ``None`` here means "use
    #: :func:`detect_systemd_unit` with this bundle's ``run_cmd``".
    detect_systemd: Optional[Callable[[], Optional[Tuple[str, ...]]]] = None
    out: Callable[[str], None] = print

    _systemd_probe: Optional[Tuple[Optional[Tuple[str, ...]]]] = field(
        default=None, init=False, repr=False
    )

    @property
    def log_path(self) -> str:
        return os.path.join(self.state_dir, LOG_NAME)

    @property
    def pid_path(self) -> str:
        return os.path.join(self.state_dir, PID_NAME)

    @property
    def endpoint(self) -> str:
        return f"{self.host}:{self.port}"

    def systemd(self) -> Optional[Tuple[str, ...]]:
        """The ``systemctl`` prefix if the unit is installed, else ``None`` (cached)."""
        if self._systemd_probe is None:
            probe = self.detect_systemd or (lambda: detect_systemd_unit(self.run_cmd, self.unit))
            self._systemd_probe = (probe(),)
        return self._systemd_probe[0]


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
def build_reactive_argv(
    python: str,
    *,
    backend: str,
    host: Optional[str] = None,
    port: Optional[int] = None,
    hz: Optional[float] = None,
    camera_source: Optional[str] = None,
    detector: Optional[str] = None,
) -> List[str]:
    """The exact ``yalp reactive`` command line a detached start runs.

    ``-u`` keeps the log unbuffered (a crash mid-startup must not lose its last
    lines); ``-m yalp`` goes through ``yalp/__main__.py`` so no ``bin/yalp`` shim
    is needed. Only flags actually given are forwarded, so ``yalp reactive``'s
    own defaults apply to the rest.
    """
    argv = [python, "-u", "-m", "yalp", "reactive", "--backend", backend]
    if host is not None:
        argv += ["--host", str(host)]
    if port is not None:
        argv += ["--port", str(int(port))]
    if hz is not None:
        argv += ["--hz", f"{float(hz):g}"]
    if camera_source is not None:
        argv += ["--camera-source", str(camera_source)]
    if detector is not None:
        argv += ["--detector", str(detector)]
    return argv


def format_state_line(state: RobotState) -> str:
    """One human-readable RobotState row: mode, goal status, distance, heading, camera."""
    goal = state.goal or {}
    known = "" if state.distance_known else " (UNKNOWN)"
    parts = [
        f"mode={state.mode.value}",
        f"goal_status={state.goal_status}",
        f"distance={state.distance_m:.2f} m{known}",
    ]
    if "heading_deg" in goal:
        live = "" if goal.get("heading_live", True) else " (blind)"
        parts.append(f"heading={float(goal['heading_deg']):+.1f} deg{live}")
    if state.last_frame_id is not None:
        parts.append(f"camera=warm ({state.last_frame_id})")
    else:
        parts.append("camera=cold (no frame yet)")
    return "  ".join(parts)


def tail_lines(path: str, n: int, block: int = 8192) -> List[str]:
    """The last ``n`` lines of ``path`` (reads from the end, so a big log is cheap)."""
    if n <= 0:
        return []
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        pos = fh.tell()
        data = b""
        while pos > 0 and data.count(b"\n") <= n:
            step = min(block, pos)
            pos -= step
            fh.seek(pos)
            data = fh.read(step) + data
    return [line.decode("utf-8", errors="replace") for line in data.splitlines()[-n:]]


def _journal_unit(unit: str) -> str:
    return unit[: -len(".service")] if unit.endswith(".service") else unit


# --------------------------------------------------------------------------- #
# Readiness
# --------------------------------------------------------------------------- #
@dataclass
class Readiness:
    ready: bool
    elapsed: float
    state: Optional[RobotState]
    reason: str


def wait_ready(
    deps: SessionDeps,
    *,
    timeout: float,
    camera: bool,
    pid: Optional[int] = None,
    poll_interval: float = 0.25,
) -> Readiness:
    """Poll until the server answers (and, with ``camera``, has a frame).

    Ready = TCP connect succeeds, ``request_state`` returns a snapshot, and
    (``camera``) ``last_frame_id`` is set. Fails FAST — before the deadline —
    when ``pid`` is given and dies: a server that crashed on startup (camera open
    failure, GPIO busy) must not cost the operator the whole timeout.
    """
    t0 = deps.monotonic()
    deadline = t0 + max(0.0, float(timeout))
    last_state: Optional[RobotState] = None
    port_seen = False
    while True:
        now = deps.monotonic()
        if pid is not None and not deps.pid_alive(pid):
            return Readiness(
                False, now - t0, last_state, f"process {pid} exited before becoming ready"
            )
        if deps.connect(deps.host, deps.port):
            if not port_seen:
                port_seen = True
                deps.out(
                    f"  port {deps.endpoint} up after {now - t0:.1f} s; requesting state"
                    + (" and waiting for the first camera frame..." if camera else "...")
                )
            state = deps.request_state(
                deps.host, deps.port, min(1.0, max(0.1, deadline - now))
            )
            if state is not None:
                last_state = state
                if not camera or state.last_frame_id is not None:
                    return Readiness(
                        True,
                        deps.monotonic() - t0,
                        state,
                        "camera warm" if camera else "server answering",
                    )
        now = deps.monotonic()
        if now >= deadline:
            break
        deps.sleep(min(poll_interval, deadline - now))
    if last_state is None:
        reason = (
            f"no RobotState from {deps.endpoint} within {timeout:.1f} s"
            + ("" if port_seen else " (port never opened)")
        )
    else:
        reason = (
            f"server answering but the camera never published a frame within "
            f"{timeout:.1f} s (last_frame_id is still None)"
        )
    return Readiness(False, deps.monotonic() - t0, last_state, reason)


def _report_readiness(deps: SessionDeps, r: Readiness) -> int:
    if r.ready and r.state is not None:
        deps.out(f"READY in {r.elapsed:.1f} s ({r.reason}) — {format_state_line(r.state)}")
        return EXIT_OK
    deps.out(f"NOT READY after {r.elapsed:.1f} s: {r.reason}")
    if r.state is not None:
        deps.out(f"  last state: {format_state_line(r.state)}")
    deps.out("  see `yalp session logs` for the server's own output.")
    return EXIT_FAIL


# --------------------------------------------------------------------------- #
# Stop sequence (SIGINT -> SIGTERM -> SIGKILL)
# --------------------------------------------------------------------------- #
def _wait_exit(deps: SessionDeps, pid: int, timeout: float, poll: float = 0.1) -> bool:
    """True once ``pid`` is gone, False if still alive after ``timeout``."""
    deadline = deps.monotonic() + max(0.0, timeout)
    while True:
        if not deps.pid_alive(pid):
            return True
        now = deps.monotonic()
        if now >= deadline:
            return False
        deps.sleep(min(poll, deadline - now))


def stop_pid(deps: SessionDeps, pid: int, *, timeout: float = 10.0) -> str:
    """Graceful-then-forceful stop of ``pid``.

    Returns how it ended: ``"not-running"``, ``"stopped"`` (SIGINT),
    ``"terminated"`` (SIGTERM), ``"killed"`` (SIGKILL) or ``"survived"``.
    SIGINT is first because the server's teardown zeroes the motors as its very
    first step (reactive_cli.py); SIGTERM is its documented second-signal
    force-exit; SIGKILL is announced loudly since it skips teardown entirely.
    """
    out = deps.out
    if not deps.pid_alive(pid):
        return "not-running"
    out(
        f"sending SIGINT to pid {pid} (graceful: the server's teardown zeroes the "
        "motors first, then releases the camera and GPIO)..."
    )
    if not deps.send_signal(pid, signal.SIGINT) or _wait_exit(deps, pid, timeout):
        return "stopped"
    out(
        f"pid {pid} still running after {timeout:.0f} s — sending SIGTERM (the "
        "server's second-signal force-exit; the motors were already zeroed by the "
        "first signal if it was processed)..."
    )
    if not deps.send_signal(pid, signal.SIGTERM) or _wait_exit(deps, pid, min(5.0, timeout)):
        return "terminated"
    out(f"!!! pid {pid} ignored SIGINT and SIGTERM — sending SIGKILL as a LAST RESORT.")
    out(
        "!!! A killed process runs no teardown: IF it processed the first SIGINT it "
        "zeroed the motors as step one, but a process wedged hard enough to ignore "
        "two signals may not have — CHECK THE WHEELS. The camera / GPIO lines may "
        "stay held until the kernel reclaims them; if the next start fails with a "
        "busy device, look for leftovers with `ps -ef | grep 'yalp reactive'`."
    )
    if not deps.send_signal(pid, signal.SIGKILL) or _wait_exit(deps, pid, 2.0):
        return "killed"
    return "survived"


# --------------------------------------------------------------------------- #
# Actions
# --------------------------------------------------------------------------- #
def cmd_start(
    deps: SessionDeps,
    *,
    backend: str = "real",
    hz: Optional[float] = None,
    camera_source: Optional[str] = None,
    detector: Optional[str] = None,
    foreground: bool = False,
    wait: bool = True,
    timeout: float = 30.0,
    camera: Optional[bool] = None,
    stop_timeout: float = 10.0,
) -> int:
    """``yalp session start``. See the module docstring for the two strategies."""
    out = deps.out

    # 1. Never start a second server: two processes fighting for the camera and
    #    the GPIO is exactly the orphan mess this tool exists to end.
    if deps.connect(deps.host, deps.port):
        pid = read_pidfile(deps.pid_path)
        if pid is not None and deps.pid_alive(pid):
            who = f"pid {pid} (from {deps.pid_path})"
        else:
            who = "owner unknown (no live pidfile — started by hand, or by systemd)"
        out(f"refusing to start: {deps.endpoint} is already listening — {who}.")
        out("run `yalp session status`, or `yalp session stop` first.")
        return EXIT_PORT_BUSY
    stale = read_pidfile(deps.pid_path)
    if stale is not None:
        if deps.pid_alive(stale):
            out(
                f"refusing to start: pid {stale} from {deps.pid_path} is alive but "
                f"{deps.endpoint} is not listening — it may still be starting up "
                "(`yalp session wait`) or be wedged (`yalp session stop`)."
            )
            return EXIT_PORT_BUSY
        out(f"removing stale pidfile {deps.pid_path} (pid {stale} is gone).")
        remove_pidfile(deps.pid_path)

    # 2. Launch.
    prefix = None if foreground else deps.systemd()
    pid: Optional[int] = None
    if prefix is not None:
        rc, _ = deps.run_cmd([*prefix, "start", deps.unit])
        if rc != 0:
            out(f"`{' '.join(prefix)} start {deps.unit}` failed (exit {rc}).")
            return EXIT_FAIL
        out(f"started {deps.unit} via `{' '.join(prefix)} start`.")
        if backend != "real" or hz is not None or camera_source is not None or detector is not None:
            out(
                "  note: under systemd the unit file and ~/.config/yalp/reactive.env "
                "decide the flags — --backend/--hz/--camera-source/--detector are ignored."
            )
        want_camera = True if camera is None else camera  # the unit runs the real backend
        rc, main_pid = deps.run_cmd(
            [*prefix, "show", "-p", "MainPID", "--value", deps.unit], capture=True
        )
        main_pid = main_pid.strip()
        if rc == 0 and main_pid.isdigit() and int(main_pid) > 0:
            pid = int(main_pid)
    else:
        argv = build_reactive_argv(
            deps.python,
            backend=backend,
            host=deps.host,
            port=deps.port,
            hz=hz,
            camera_source=camera_source,
            detector=detector,
        )
        want_camera = (backend == "real") if camera is None else camera
        if foreground:
            out(f"running in the foreground (Ctrl-C stops it gracefully): {' '.join(argv)}")
            pid = deps.spawn(argv, None)
        else:
            pid = deps.spawn(argv, deps.log_path)
            out(f"spawned pid {pid}: {' '.join(argv)}")
            out(f"  log: {deps.log_path}")
        write_pidfile(deps.pid_path, pid)
        if foreground:
            return _foreground(deps, pid, timeout=timeout, camera=want_camera, stop_timeout=stop_timeout)

    # 3. Readiness.
    if not wait:
        return EXIT_OK
    out(
        f"waiting up to {timeout:.0f} s for {deps.endpoint}"
        + (" + camera warm-up..." if want_camera else "...")
    )
    return _report_readiness(deps, wait_ready(deps, timeout=timeout, camera=want_camera, pid=pid))


def _foreground(
    deps: SessionDeps, pid: int, *, timeout: float, camera: bool, stop_timeout: float
) -> int:
    """Block on a foreground child; Ctrl-C runs the graceful stop sequence.

    The child sits in its own session, so the terminal's Ctrl-C reaches only us
    and we forward exactly ONE SIGINT (a second one would trip the server's
    force-exit path). The pidfile is kept while it runs, so ``yalp session
    status`` / ``stop`` from another shell see it too.
    """
    try:
        _report_readiness(deps, wait_ready(deps, timeout=timeout, camera=camera, pid=pid))
        while deps.pid_alive(pid):
            deps.sleep(0.25)
        deps.out(f"yalp reactive (pid {pid}) exited.")
    except KeyboardInterrupt:
        deps.out("\nCtrl-C — stopping the reactive server gracefully...")
        how = stop_pid(deps, pid, timeout=stop_timeout)
        deps.out(f"pid {pid} {how}.")
    finally:
        remove_pidfile(deps.pid_path)
    return EXIT_OK


def cmd_wait(deps: SessionDeps, *, timeout: float = 30.0, camera: bool = True) -> int:
    """``yalp session wait``: block until ready (0) or the deadline (1)."""
    pid = read_pidfile(deps.pid_path)
    if pid is not None and not deps.pid_alive(pid):
        pid = None  # a stale pidfile must not fail a wait on a systemd-run server
    deps.out(
        f"waiting up to {timeout:.0f} s for {deps.endpoint}"
        + (" + camera warm-up..." if camera else "...")
    )
    return _report_readiness(deps, wait_ready(deps, timeout=timeout, camera=camera, pid=pid))


def cmd_status(deps: SessionDeps) -> int:
    """``yalp session status``: 0 if the server is listening, else 1."""
    out = deps.out
    listening = deps.connect(deps.host, deps.port)
    out(f"endpoint : {deps.endpoint} — {'LISTENING' if listening else 'not listening'}")

    pid = read_pidfile(deps.pid_path)
    if pid is not None:
        alive = deps.pid_alive(pid)
        out(f"pidfile  : {deps.pid_path} -> pid {pid} ({'alive' if alive else 'STALE — process gone'})")
    else:
        out(f"pidfile  : none ({deps.pid_path})")

    prefix = deps.systemd()
    if prefix is not None:
        _rc, active = deps.run_cmd([*prefix, "is-active", deps.unit], capture=True)
        _rc, main_pid = deps.run_cmd(
            [*prefix, "show", "-p", "MainPID", "--value", deps.unit], capture=True
        )
        out(
            f"systemd  : {deps.unit} is {active.strip() or 'unknown'} via "
            f"`{' '.join(prefix)}` (MainPID {main_pid.strip() or '?'})"
        )
    else:
        out("systemd  : no yalp-reactive.service unit installed (detached-child mode)")
        out(f"log      : {deps.log_path}")

    if listening:
        state = deps.request_state(deps.host, deps.port, 1.0)
        if state is not None:
            out(f"state    : {format_state_line(state)}")
        else:
            out("state    : port open but no RobotState answered within 1 s")
    elif pid is not None and deps.pid_alive(pid):
        out("hint     : the process is alive but not serving yet — `yalp session wait`")

    out("RUNNING" if listening else "NOT RUNNING")
    return EXIT_OK if listening else EXIT_FAIL


def cmd_stop(deps: SessionDeps, *, timeout: float = 10.0) -> int:
    """``yalp session stop``: 0 if stopped or was not running."""
    out = deps.out
    rc = EXIT_OK
    prefix = deps.systemd()
    if prefix is not None:
        code, _ = deps.run_cmd([*prefix, "stop", deps.unit])
        if code != 0:
            out(f"`{' '.join(prefix)} stop {deps.unit}` failed (exit {code}).")
            rc = EXIT_FAIL
        else:
            out(
                f"stopped {deps.unit} via `{' '.join(prefix)} stop` "
                "(KillSignal=SIGINT: motors zeroed first by the server's teardown)."
            )

    pid = read_pidfile(deps.pid_path)
    if pid is not None:
        how = stop_pid(deps, pid, timeout=timeout)
        if how == "not-running":
            out(f"pid {pid} from {deps.pid_path} was not running (stale pidfile removed).")
        elif how == "survived":
            out(f"!!! pid {pid} survived even SIGKILL — leaving the pidfile for inspection.")
            return EXIT_FAIL
        else:
            out(f"pid {pid} {how}.")
        remove_pidfile(deps.pid_path)
    elif prefix is None:
        out(f"nothing to stop: no pidfile at {deps.pid_path}.")

    if deps.connect(deps.host, deps.port):
        out(
            f"warning: {deps.endpoint} is STILL listening — a server started outside "
            "`yalp session` owns it (`ps -ef | grep 'yalp reactive'`)."
        )
        return EXIT_FAIL
    return rc


def cmd_restart(deps: SessionDeps, *, stop_timeout: float = 10.0, **start_kwargs) -> int:
    """``yalp session restart``: stop, then start (with the readiness wait)."""
    rc = cmd_stop(deps, timeout=stop_timeout)
    if rc != EXIT_OK:
        return rc
    return cmd_start(deps, stop_timeout=stop_timeout, **start_kwargs)


def _follow_file(deps: SessionDeps, path: str, poll: float = 0.5) -> None:
    """Print lines appended to ``path`` until interrupted (``tail -f``)."""
    buf = b""
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        while True:
            chunk = fh.read()
            if chunk:
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    deps.out(line.decode("utf-8", errors="replace"))
            else:
                deps.sleep(poll)


def cmd_logs(deps: SessionDeps, *, n: int = 50, follow: bool = False) -> int:
    """``yalp session logs``: tail the log file, or the journal under systemd."""
    prefix = deps.systemd()
    if prefix is not None:
        argv = ["journalctl", *prefix[1:], "-u", _journal_unit(deps.unit), "-n", str(int(n))]
        if follow:
            argv.append("-f")
        try:
            rc, _ = deps.run_cmd(argv)
        except KeyboardInterrupt:
            return EXIT_OK
        return EXIT_OK if rc == 0 else EXIT_FAIL

    path = deps.log_path
    if not os.path.exists(path):
        deps.out(f"no log at {path} (nothing started via `yalp session start` yet?)")
        return EXIT_FAIL
    for line in tail_lines(path, n):
        deps.out(line)
    if follow:
        try:
            _follow_file(deps, path)
        except KeyboardInterrupt:
            pass
    return EXIT_OK


# --------------------------------------------------------------------------- #
# CLI registration
# --------------------------------------------------------------------------- #
def add_parser(subparsers) -> None:
    """Register the ``session`` subcommand and its actions."""
    from .. import config

    parser = subparsers.add_parser(
        "session",
        help="Start / wait for / stop the reactive server (detached or via systemd).",
        description=(
            "Manage the reactive server as a session: start it detached (or via the "
            "yalp-reactive systemd user unit when installed), wait until it is READY "
            "(port answering, RobotState flowing, camera warm), show its status, "
            "stop it gracefully (SIGINT first — the server zeroes the motors before "
            "anything else), restart it, or tail its log. Replaces the hand-rolled "
            "nohup / sleep-4-seconds / kill dance used over SSH."
        ),
    )
    parser.set_defaults(handler=run)
    actions = parser.add_subparsers(dest="action", metavar="<action>", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--host",
            default=config.IPC_HOST,
            metavar="HOST",
            help=f"Reactive server host (default: {config.IPC_HOST}).",
        )
        p.add_argument(
            "--port",
            type=int,
            default=config.IPC_PORT,
            metavar="PORT",
            help=f"Reactive server port (default: {config.IPC_PORT}).",
        )

    def start_flags(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--backend",
            choices=("real", "fake"),
            default="real",
            help="Backend for a detached start: 'real' (default, Pi hardware) or 'fake'. Ignored under systemd.",
        )
        p.add_argument(
            "--hz", type=float, default=None, metavar="HZ",
            help="Reactive tick rate (default: yalp reactive's own default).",
        )
        p.add_argument(
            "--camera-source", default=None, metavar="SOURCE",
            help="Camera source: 'webcam', 'synthetic', or 'image' (default: yalp reactive's own default).",
        )
        p.add_argument(
            "--detector", choices=("face", "hog", "person", "auto"), default=None,
            help="FOLLOW person detector (default: yalp reactive's own default).",
        )
        p.add_argument(
            "--foreground", action="store_true",
            help="Run attached to this terminal instead of detaching / using systemd; Ctrl-C stops it gracefully.",
        )
        p.add_argument(
            "--no-wait", dest="wait", action="store_false",
            help="Return right after launching instead of waiting for READY.",
        )
        p.add_argument(
            "--timeout", type=float, default=30.0, metavar="SECONDS",
            help="How long to wait for READY (default: 30).",
        )
        p.add_argument(
            "--camera", action=argparse.BooleanOptionalAction, default=None,
            help="Also wait for the first camera frame (default: on for --backend real and under systemd, off for fake).",
        )

    p = actions.add_parser("start", help="Launch the server (detached or systemd) and wait until READY.")
    common(p)
    start_flags(p)

    p = actions.add_parser("wait", help="Block until the server answers (and the camera is warm).")
    common(p)
    p.add_argument("--timeout", type=float, default=30.0, metavar="SECONDS", help="Deadline (default: 30).")
    p.add_argument(
        "--camera", action=argparse.BooleanOptionalAction, default=True,
        help="Also wait for the first camera frame (default: on; --no-camera to skip).",
    )

    p = actions.add_parser("status", help="Port / pid / one RobotState line. Exit 0 if running.")
    common(p)

    p = actions.add_parser("stop", help="Stop gracefully: SIGINT, then SIGTERM, then (loudly) SIGKILL.")
    common(p)
    p.add_argument(
        "--timeout", type=float, default=10.0, metavar="SECONDS",
        help="Seconds to allow the graceful SIGINT teardown before escalating (default: 10).",
    )

    p = actions.add_parser("restart", help="stop, then start (with the readiness wait).")
    common(p)
    start_flags(p)
    p.add_argument(
        "--stop-timeout", type=float, default=10.0, metavar="SECONDS",
        help="Seconds to allow the graceful stop before escalating (default: 10).",
    )

    p = actions.add_parser("logs", help="Tail the server log (journalctl under systemd).")
    common(p)
    p.add_argument("-n", "--lines", type=int, default=50, metavar="N", help="Lines to show (default: 50).")
    p.add_argument("-f", "--follow", action="store_true", help="Keep printing new lines until Ctrl-C.")


def run(args, deps: Optional[SessionDeps] = None) -> int:
    """Handler for ``yalp session``. Returns a process exit code.

    ``deps`` is the injection seam for tests; production leaves it ``None`` and
    gets the real subprocess / socket / signal implementations.
    """
    if deps is None:
        deps = SessionDeps(host=args.host, port=int(args.port))
    action = args.action
    if action == "start":
        return cmd_start(
            deps,
            backend=args.backend,
            hz=args.hz,
            camera_source=args.camera_source,
            detector=args.detector,
            foreground=args.foreground,
            wait=args.wait,
            timeout=args.timeout,
            camera=args.camera,
        )
    if action == "wait":
        return cmd_wait(deps, timeout=args.timeout, camera=args.camera)
    if action == "status":
        return cmd_status(deps)
    if action == "stop":
        return cmd_stop(deps, timeout=args.timeout)
    if action == "restart":
        return cmd_restart(
            deps,
            stop_timeout=args.stop_timeout,
            backend=args.backend,
            hz=args.hz,
            camera_source=args.camera_source,
            detector=args.detector,
            foreground=args.foreground,
            wait=args.wait,
            timeout=args.timeout,
            camera=args.camera,
        )
    if action == "logs":
        return cmd_logs(deps, n=args.lines, follow=args.follow)
    raise ValueError(f"unknown session action {action!r}")  # argparse prevents this


__all__ = [
    "add_parser",
    "run",
    "SessionDeps",
    "Readiness",
    "default_state_dir",
    "detect_systemd_unit",
    "build_reactive_argv",
    "format_state_line",
    "tail_lines",
    "wait_ready",
    "stop_pid",
    "cmd_start",
    "cmd_wait",
    "cmd_status",
    "cmd_stop",
    "cmd_restart",
    "cmd_logs",
    "read_pidfile",
    "write_pidfile",
    "remove_pidfile",
    "UNIT_NAME",
    "EXIT_OK",
    "EXIT_FAIL",
    "EXIT_PORT_BUSY",
]
