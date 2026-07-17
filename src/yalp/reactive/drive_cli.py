"""``yalp drive`` — run one straight DRIVE_GOAL against a live reactive server.

Every hardware bring-up session so far has hand-rolled a throwaway ``/tmp``
script that speaks the raw NDJSON-over-TCP contract to make the robot drive a
measured distance and watch the reflex. This subcommand IS that script, made
first-class: connect to the running reactive process as a
:class:`~yalp.contract.ipc.DeliberativeClient`, pre-flight the safety state,
send the canonical ``drive`` :class:`~yalp.contract.messages.Intent`, print a
per-poll timeline, and terminate on the first of {completion, SAFE_STOP,
timeout, Ctrl-C} — halting the wheels on the way out.

Nothing here is new machinery. The client transport is
``yalp.contract.ipc.DeliberativeClient`` (the reactive process is the server);
the intent shape is exactly what ``abilities.intent_for("drive", ...)`` builds;
the safety semantics (an UNKNOWN echo biases to STOP; a sticky ``SAFE_STOP`` is
lifted only when the obstacle clears AND a fresh intent is adopted) live in
``yalp.reactive.tick_core``. This module is assembly.

Registered via the documented feature-module contract (``add_parser`` / ``run``)
and listed in ``yalp.cli.FEATURE_MODULES``.

Exit codes
----------
  * ``0`` — the drive COMPLETED ("timed, unverified"), OR the reflex latched
    ``SAFE_STOP`` after adoption (collision-stop working as designed).
  * ``1`` — the run hit ``--timeout`` without finishing (wheels halted via IDLE).
  * ``2`` — pre-flight refused to start because a GENUINE close obstacle was
    already latched (no intent is sent).
  * ``130`` — Ctrl-C (wheels halted via IDLE, then teardown).
"""

from __future__ import annotations

import time
from typing import Optional, Tuple


# --------------------------------------------------------------------------- #
# CLI registration
# --------------------------------------------------------------------------- #
def add_parser(subparsers) -> None:
    """Register the ``drive`` subcommand and its flags."""
    from .. import config

    parser = subparsers.add_parser(
        "drive",
        help="Run one straight drive against a live reactive server (hardware session in one command).",
        description=(
            "Connect to the running reactive process over the localhost/Pi IPC "
            "socket, pre-flight the safety state, send one canonical DRIVE_GOAL "
            "'straight' intent (target meters at 0..1 speed), then print a live "
            "timeline (distance, mode, goal status/progress) until the drive "
            "completes, the collision reflex latches SAFE_STOP, or --timeout "
            "expires. Halts the wheels (IDLE) on timeout / Ctrl-C. A NEGATIVE "
            "--target drives backward — allowed, but the robot has no rear sensor "
            "so collision-stop cannot protect a reverse move."
        ),
    )
    parser.add_argument(
        "--target",
        type=float,
        default=1.6,
        metavar="METERS",
        help=(
            "Signed straight-line distance in meters (default: 1.6). Negative "
            "drives BACKWARD (no rear sensor coverage — prints a loud warning)."
        ),
    )
    parser.add_argument(
        "--speed",
        type=float,
        default=0.3,
        metavar="FRACTION",
        help="Drive speed as a 0..1 fraction of full throttle (default: 0.3).",
    )
    parser.add_argument(
        "--host",
        default=config.IPC_HOST,
        metavar="HOST",
        help=f"Reactive server host (default: config.IPC_HOST = {config.IPC_HOST}).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=config.IPC_PORT,
        metavar="PORT",
        help=f"Reactive server port (default: config.IPC_PORT = {config.IPC_PORT}).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=45.0,
        metavar="SECONDS",
        help="Hard cap on the whole run in seconds (default: 45).",
    )
    parser.add_argument(
        "--poll-hz",
        type=float,
        default=5.0,
        metavar="HZ",
        help="How often to poll RobotState and print a timeline line (default: 5).",
    )
    parser.set_defaults(handler=run)


# --------------------------------------------------------------------------- #
# Pure helpers (unit-testable without a socket)
# --------------------------------------------------------------------------- #
# Blocked-stack reasons that mean "stale startup latch, not a real obstacle":
# the first read(s) timed out (echo_timeout) or the sensor had not warmed up
# (startup_blind — a parallel task may add this reason). Both carry
# distance_known == False and are lifted the moment a fresh intent is adopted
# (tick_core.py §2.3), so they are safe to drive through.
_STALE_LATCH_REASONS = ("echo_timeout", "startup_blind")

# Loopback / same-host addresses get the patient default reconnect; anything else
# is treated as a remote (Pi over WiFi) endpoint and gets fail-fast reconnect
# args so a dropped link never wedges this client for tens of seconds
# (ipc.py DeliberativeClient docstring, lines 319-325).
_LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1", "0.0.0.0")


def _is_remote(host: str) -> bool:
    """True when ``host`` is NOT a loopback/same-host address (fail-fast policy)."""
    return str(host).strip().lower() not in _LOCAL_HOSTS


def preflight_decision(state, threshold: float) -> Tuple[str, str]:
    """Decide whether it is safe to START a drive from the initial snapshot.

    Pure and side-effect-free so the gate can be reasoned about (and tested) in
    isolation. Returns ``("refuse", why)`` or ``("proceed", why)``.

    * A GENUINE close obstacle — a blocked stack whose ``goal.reason`` is
      ``"obstacle"`` with ``distance_known == True`` and the served distance
      below ``threshold`` (``config.SAFE_STOP_THRESHOLD_M``) — REFUSES: the robot
      is already nose-to-something, so kicking off a forward drive is unsafe.
    * A STALE STARTUP LATCH — a blocked stack whose reason is ``echo_timeout`` /
      ``startup_blind`` with ``distance_known == False`` — PROCEEDS: this is the
      first-read timeout latch, and adopting our intent lifts it (proven contract,
      tick_core.py lines 341-376).
    * Anything else (clear path, or a block we do not recognize) PROCEEDS.
    """
    if state is None:
        return "proceed", "no initial snapshot returned; proceeding (assuming clear)."
    if not state.blocked:
        return "proceed", "path is clear — starting the drive."

    goal = state.goal or {}
    reason = goal.get("reason")
    served = goal.get("distance", state.distance_m)

    if reason == "obstacle" and state.distance_known and float(served) < float(threshold):
        return "refuse", (
            f"GENUINE obstacle latched at {float(served):.2f} m "
            f"(< SAFE_STOP {float(threshold):.2f} m) — refusing to start a forward "
            "drive into it. Clear the path (or move the robot) and retry."
        )

    if reason in _STALE_LATCH_REASONS and not state.distance_known:
        return "proceed", (
            f"stale startup latch (reason={reason!r}, distance unknown) — this is "
            "the first-read timeout, not a real obstacle; adopting the drive intent "
            "lifts it."
        )

    # Some other blocked shape we do not specifically model (e.g. an obstacle
    # reason whose served distance is not below threshold). Bias to PROCEED: a
    # sticky SAFE_STOP is lifted by adopting a fresh intent only once the path is
    # actually clear, and the poll loop will surface a genuine post-adoption stop.
    return "proceed", (
        f"blocked latch (reason={reason!r}, distance_known={state.distance_known}) "
        "not recognized as a genuine close obstacle — proceeding; adoption lifts a "
        "cleared latch and the reflex re-stops a real one."
    )


def _timeline_line(elapsed: float, state) -> str:
    """One human-readable timeline row for a polled snapshot."""
    known = "known" if state.distance_known else "UNKNOWN"
    goal = state.goal or {}
    if "progress" in goal:
        detail = f"progress={float(goal['progress']):.2f}"
    elif goal.get("reason"):
        detail = f"reason={goal['reason']}"
    else:
        detail = "-"
    return (
        f"[{elapsed:6.1f}s] dist={state.distance_m:5.2f}m ({known:^7})  "
        f"mode={state.mode.value:<10}  status={state.goal_status:<24}  {detail}"
    )


def _ultrasonic_line(label: str, state) -> str:
    """Render the RobotState.ultrasonic counter sub-map (miss-rate visibility)."""
    counters = getattr(state, "ultrasonic", None) if state is not None else None
    if counters:
        body = ", ".join(f"{k}={v}" for k, v in counters.items())
    else:
        body = "n/a (no counter-bearing sensor — e.g. pure simulation)"
    return f"  ultrasonic ({label}): {body}"


def send_idle_halt(client, seq: int) -> None:
    """Send an ``Intent(Mode.IDLE, None, seq)`` to halt the wheels.

    The one place this CLI stops the robot (timeout / Ctrl-C), factored out so it
    can be unit-tested directly rather than by delivering a real SIGINT. Mirrors
    ``agent._fall_to_idle`` (agent.py lines 489-495): best-effort, never raises —
    a broken socket during a halt must not mask the exit path.
    """
    from ..contract.messages import Intent, Mode

    try:
        client.send_intent(Intent(Mode.IDLE, None, seq))
    except Exception:  # noqa: BLE001 — a failed halt-send must not wedge teardown
        pass


# --------------------------------------------------------------------------- #
# Client construction
# --------------------------------------------------------------------------- #
def _connect(host: str, port: int, remote: bool):
    """Build and connect a :class:`DeliberativeClient` with the right backoff.

    Remote (Pi-over-WiFi) endpoints get fail-fast reconnect args so a dropped
    link never blocks; loopback keeps the patient default suited to the always-up
    local server (ipc.py lines 319-325).
    """
    from ..contract.ipc import DeliberativeClient

    if remote:
        client = DeliberativeClient(
            host,
            port,
            connect_timeout=2.0,
            reconnect_retries=1,
            reconnect_backoff=0.1,
            reconnect_max_backoff=0.5,
        )
        client.connect(retries=3, backoff=0.1, max_backoff=0.5)
    else:
        client = DeliberativeClient(host, port)
        client.connect()
    return client


# --------------------------------------------------------------------------- #
# Poll loop
# --------------------------------------------------------------------------- #
def _poll_loop(client, base_seq: int, timeout: float, poll_interval: float) -> int:
    """Poll RobotState, print a timeline, and return the process exit code.

    Terminates on the FIRST of: our drive COMPLETED (exit 0), a SAFE_STOP latched
    AFTER our intent was adopted (exit 0, reflex working), or ``timeout`` expiry
    (send IDLE, exit 1). A ``SAFE_STOP`` seen BEFORE adoption is the residual
    stale startup latch and is deliberately NOT a termination — adoption lifts it.
    ``KeyboardInterrupt`` is left to propagate to :func:`run`, which halts + exits.
    """
    from ..contract.messages import GoalStatus, Mode

    t_start = time.monotonic()
    deadline = t_start + timeout
    adopted = False
    last_state = None

    while time.monotonic() < deadline:
        state = client.request_state(timeout=1.0)
        elapsed = time.monotonic() - t_start
        if state is not None:
            last_state = state
            print(_timeline_line(elapsed, state))

            # Adoption: once the reactive layer is actually running our DRIVE_GOAL,
            # a SAFE_STOP/COMPLETED is OURS — before that a SAFE_STOP is the stale
            # startup latch (see docstring) and COMPLETED could be a prior goal.
            if state.mode == Mode.DRIVE_GOAL or state.goal_status == GoalStatus.RUNNING:
                adopted = True

            if adopted and state.goal_status == GoalStatus.COMPLETED:
                print(
                    f"\nVERDICT: COMPLETE — drive reported {state.goal_status!r} "
                    f"after {elapsed:.1f}s (open-loop, timed, UNVERIFIED — no "
                    "encoders). Wheels are stopped (mode dropped to IDLE)."
                )
                print(_ultrasonic_line("end", state))
                return 0

            if adopted and state.blocked:
                goal = state.goal or {}
                reason = goal.get("reason", "unknown")
                served = goal.get("distance", state.distance_m)
                print(
                    f"\nVERDICT: SAFE_STOP — the collision reflex stopped the drive "
                    f"after {elapsed:.1f}s (reason={reason!r}, served distance="
                    f"{float(served):.2f} m). This is the reflex working as designed."
                )
                print(_ultrasonic_line("end", state))
                return 0

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(poll_interval, remaining))

    # Timeout: halt the wheels FIRST, then report (cf. agent._fall_to_idle).
    send_idle_halt(client, base_seq + 1)
    print(
        f"\nVERDICT: TIMEOUT — the drive did not finish within {timeout:.1f}s. "
        "Sent an IDLE intent to halt the wheels."
    )
    if last_state is not None:
        print(_ultrasonic_line("end", last_state))
    return 1


# --------------------------------------------------------------------------- #
# Handler
# --------------------------------------------------------------------------- #
def run(args) -> int:
    """Handler for ``yalp drive``. Returns a process exit code."""
    from .. import config
    from ..contract.messages import Intent, Mode

    target = float(getattr(args, "target", 1.6))
    speed = min(1.0, max(0.0, float(getattr(args, "speed", 0.3))))
    host = getattr(args, "host", None) or config.IPC_HOST
    port = int(getattr(args, "port", None) or config.IPC_PORT)
    timeout = max(0.1, float(getattr(args, "timeout", 45.0)))
    poll_hz = max(0.1, float(getattr(args, "poll_hz", 5.0)))
    poll_interval = 1.0 / poll_hz
    threshold = float(config.SAFE_STOP_THRESHOLD_M)
    remote = _is_remote(host)

    # A seq that cannot lose to a prior client's intents in this stack lifetime.
    # IntentMailbox keeps the highest seq (ipc.py line 73: ``intent.seq >=
    # slot.seq`` — "newer seq wins", ties to the later arrival), and the agent /
    # other clients count up from small numbers, so a wall-clock second stamp is
    # comfortably larger than anything they will have sent. The IDLE halt uses
    # ``seq + 1`` so it in turn beats our own drive.
    seq = int(time.time())

    print(
        "yalp drive — one-command straight drive against the reactive server\n"
        f"  endpoint : {host}:{port}"
        f"{' (remote — fail-fast reconnect)' if remote else ' (loopback)'}\n"
        f"  goal     : straight  target={target:+.2f} m  speed={speed:.2f}\n"
        f"  limits   : timeout={timeout:.1f}s  poll={poll_hz:g} Hz  "
        f"SAFE_STOP<{threshold:.2f} m\n"
        f"  intent   : Intent(DRIVE_GOAL, kind=straight, seq={seq})"
    )
    if target < 0:
        print(
            "  !!! REVERSE DRIVE: --target is NEGATIVE. The robot has NO rear "
            "ultrasonic — collision-stop CANNOT protect a backward move. The "
            "wheels will reverse open-loop. Proceed only if the path behind is "
            "known clear."
        )
    print()

    try:
        client = _connect(host, port, remote)
    except Exception as exc:  # noqa: BLE001 — connect failure is a clean, reported exit
        print(f"could not connect to reactive server at {host}:{port}: {exc}")
        return 1

    try:
        try:
            # 1. Initial snapshot + pre-flight safety gate.
            state = client.request_state(timeout=2.0)
            action, why = preflight_decision(state, threshold)
            print(f"pre-flight: {why}")
            print(_ultrasonic_line("start", state))
            if action == "refuse":
                print("\nVERDICT: REFUSED — no intent sent.")
                return 2

            # 2. Send the canonical drive intent (abilities.intent_for('drive')).
            client.send_intent(
                Intent(
                    Mode.DRIVE_GOAL,
                    {"kind": "straight", "target": target, "speed": speed},
                    seq,
                )
            )
            print(f"sent drive intent (seq={seq}); watching the timeline...\n")

            # 3. Poll + terminate on the first event.
            return _poll_loop(client, seq, timeout, poll_interval)
        except KeyboardInterrupt:
            print("\ninterrupted (Ctrl-C) — sending IDLE to halt the wheels.")
            send_idle_halt(client, seq + 1)
            return 130
    finally:
        client.close()


__all__ = [
    "add_parser",
    "run",
    "preflight_decision",
    "send_idle_halt",
]
