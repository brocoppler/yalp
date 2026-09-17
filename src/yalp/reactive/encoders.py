"""Quadrature wheel encoders (DFRobot FIT0450) on libgpiod v2 edge events.

**What this is for.** The floor drive is open-loop: ``drive`` turns a requested
distance into a *timed* throttle through the measured speed model, and the first
field session showed that timer landing ~2x off (motor deadband, pack sag, carpet
vs. tile). The camera heading hold and the IMU already cover *heading*; nothing
covers *distance*. Two quadrature wheel encoders close that loop: a signed
per-wheel tick count → metres travelled, a per-wheel speed for a speed
controller, and a cheap dead-reckoned heading change from the wheel-distance
difference that cross-checks the camera/IMU heading.

This module is the ENCODER DRIVER only — it counts, converts and reports. The
distance/speed controller that consumes :class:`EncoderSample` lives in the tick
loop and is integrated separately.

**Hardware.** Two DFRobot FIT0450 TT gear-motors (6 V, 120:1 metal gearbox) with
an integrated two-channel Hall-effect quadrature encoder on the *motor* shaft.
Six wires per motor — two for the motor, four for the encoder::

    wire    role
    ------  --------------------------------------------------------------
    M+ M-   motor — to the DRV8833 OUTx pair (pack voltage), as today
    VCC     encoder supply, 3.3-5 V  -> Pi 3V3 (physical pin 1 or 17)
    GND     encoder ground           -> any Pi GND (pin 6/9/14/20/25/30/34/39)
    A       encoder channel A        -> GPIO (pin map below)
    B       encoder channel B        -> GPIO (pin map below)

Two wiring rules that matter:

* **Power the encoder from 3V3, not 5 V.** The A/B outputs swing to VCC and the
  Pi's GPIO inputs are 3.3 V-only — a 5 V VCC puts 5 V on GPIO16/20/21/26. At
  3.3 V the outputs are Pi-safe with no level shifter.
* **The encoder needs its OWN VCC even though the motor runs off the pack.** The
  Hall sensors are electrically separate from the windings; the DRV8833 outputs
  never power them. Encoder GND must be common with the Pi GND (it is, when the
  pack and the Pi share a ground — which the DRV8833 inputs already require).

Wire colours vary by batch: identify VCC/GND/A/B from the DFRobot FIT0450 wiki
for your unit — never guess, the encoder VCC line is not motor-voltage tolerant.

**Pin map (BCM numbering, Pi 5 40-pin header).** ::

    wheel / channel   BCM      physical   kwarg
    ---------------   ------   --------   -------------
    left  A           GPIO16   pin 36     left_a_pin
    left  B           GPIO26   pin 37     left_b_pin
    right A           GPIO20   pin 38     right_a_pin
    right B           GPIO21   pin 40     right_b_pin

Four pins at the bottom of the header; none conflict with the motor
(12/13/17/22), the ultrasonic (5/6) or the I2C lines.

**x4 decoding — the counting mode, explicitly.** Both lines of both wheels are
requested with BOTH-edge detection and EVERY edge is decoded through the
standard 4-state quadrature table (:func:`quadrature_step`), so one electrical
cycle of the encoder (A↑ B↑ A↓ B↓) is **4 ticks**. The FIT0450 gives 16 pulses
per *motor* revolution on one channel (x1) and the gearbox is 120:1, so::

    x1 (one edge of one channel):   16 x 120      = 1920 ticks / wheel rev
    x4 (both edges, both channels): 16 x 120 x 4  = 7680 ticks / wheel rev  <- THIS DRIVER

``ticks_per_wheel_rev`` is therefore expressed in **x4 terms** and defaults to
:data:`DEFAULT_TICKS_PER_WHEEL_REV` (7680). If you ever switch to counting a
single channel's rising edges, divide by four. The direction convention of the
table is "+1 when A leads B" — whether that is the wheel rolling *forward*
depends on the motor's mounting (the two motors are mirror-mounted) and on
which wire you called A. Fix a backwards-counting wheel with ``left_invert`` /
``right_invert``, never by rewiring.

**Speed is derived from KERNEL timestamps, not Python time.** The lines are
requested with ``event_clock=MONOTONIC`` so every edge carries a
``CLOCK_MONOTONIC`` nanosecond stamp taken in the kernel interrupt path. A
background daemon thread drains the events (``wait_edge_events`` +
``read_edge_events``) and, per batch, records ``(last_edge_ns, ticks)``; speed is
``Δticks / Δt`` over the samples inside the last ``speed_window_s`` (at least one
interval), so Python scheduling jitter affects *when* we notice an edge, never
the measured interval — the same lesson as the ultrasonic driver, where
Python-side timing manufactured 2x/4x errors. The event buffer is requested at
1024 entries (the kernel maximum) so a late thread loses nothing for ~50 ms even
at the x4 no-load rate (~20 k edges/s per wheel). If the buffer *does* overflow
the kernel discards the oldest events: the decoder then sees a repeated edge,
counts it as a 0-step (``stats()['left_invalid']``/``right_invalid``) and stays
in phase, so an overflow costs a few ticks of distance, never garbage direction.

**Staleness.** A wheel with no edge for ``stale_s`` (0.25 s) reports 0 m/s —
otherwise a stopped wheel would hold its last speed forever. Staleness is judged
on the driver's own monotonic clock (the injectable ``monotonic``), recorded
when the batch was *consumed*, so no cross-domain comparison against kernel
stamps is ever made.

**Heading.** ``heading_delta_deg = degrees((right_m - left_m) / track_width_m)``
since the last :meth:`reset` — **positive = LEFT / CCW** (the right wheel travels
further), the same sign convention as :class:`~yalp.reactive.visual_odometry.YawSample`.
``track_width_m`` (wheel-centre to wheel-centre) defaults to 0.14 m —
**measure it** on the chassis; it is the only knob that scales heading.

**Bring-up checklist (three measurements, three knobs).**

1. Spin each wheel 10 turns forward by hand: expect ≈ +76 800 ticks. A
   negative count → set that wheel's ``*_invert``. A count of ≈ 19 200 (or any
   other clean ratio) → the per-rev constant is wrong, fix ``ticks_per_wheel_rev``.
2. Push the robot 1.000 m straight: ``distance_m`` should read 1.00; trim
   ``wheel_diameter_m`` (tyres squash under load).
3. Rotate the robot 360° in place: ``heading_delta_deg`` should read ±360;
   trim ``track_width_m``.

**Chip selection / lazy import** — identical to the ultrasonic driver: the Pi 5
header controller (``pinctrl-rp1``) is found by LABEL via
:func:`~yalp.reactive.hardware._discover_gpiochip`, honouring ``YALP_GPIOCHIP``;
``gpiod`` is imported *inside* ``__init__`` so this module imports on a laptop
with no hardware libraries, and :class:`FakeWheelEncoders` stands in for tests
and simulation. :meth:`GpiodWheelEncoders.read` never raises.
"""

from __future__ import annotations

import dataclasses
import logging
import math
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import (
    Any,
    Callable,
    Deque,
    Dict,
    Iterable,
    List,
    Optional,
    Protocol,
    Tuple,
    runtime_checkable,
)

from yalp import config
from yalp.reactive.hardware import _PIN_FACTORY_DOCS, _RP1_CHIP_LABEL, _discover_gpiochip

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# FIT0450 geometry and the module defaults (integration will lift these into
# config.py; the constructor takes every one of them as a kwarg).
# --------------------------------------------------------------------------- #
#: Encoder pulses per MOTOR-shaft revolution on ONE channel, ONE edge (x1).
FIT0450_PULSES_PER_MOTOR_REV: int = 16
#: Gearbox reduction of the FIT0450.
FIT0450_GEAR_RATIO: int = 120
#: x1 ticks per WHEEL revolution (one edge of one channel): 16 x 120.
FIT0450_TICKS_PER_WHEEL_REV_X1: int = FIT0450_PULSES_PER_MOTOR_REV * FIT0450_GEAR_RATIO
#: This driver counts BOTH edges of BOTH channels — x4 decoding.
DECODE_MULTIPLIER: int = 4
#: Default ticks per wheel revolution IN x4 TERMS: 1920 x 4 = 7680.
DEFAULT_TICKS_PER_WHEEL_REV: int = FIT0450_TICKS_PER_WHEEL_REV_X1 * DECODE_MULTIPLIER
#: The standard 65 mm TT wheel.
DEFAULT_WHEEL_DIAMETER_M: float = 0.065
#: Wheel-centre to wheel-centre. MEASURE IT on the chassis.
DEFAULT_TRACK_WIDTH_M: float = 0.14

#: Pin map (BCM). left A = GPIO16 (pin 36), left B = GPIO26 (pin 37),
#: right A = GPIO20 (pin 38), right B = GPIO21 (pin 40).
DEFAULT_LEFT_A_PIN: int = 16
DEFAULT_LEFT_B_PIN: int = 26
DEFAULT_RIGHT_A_PIN: int = 20
DEFAULT_RIGHT_B_PIN: int = 21

#: No edge for this long -> that wheel reports 0 m/s.
DEFAULT_STALE_S: float = 0.25
#: Speed is Δticks/Δt over the edge samples inside this trailing window.
DEFAULT_SPEED_WINDOW_S: float = 0.10
#: Kernel edge-event buffer (max 1024 = 16 x GPIO_V2_LINES_MAX); ~50 ms at x4 top speed.
DEFAULT_EVENT_BUFFER_SIZE: int = 1024
#: How long the reader thread blocks in ``wait_edge_events`` per loop (bounds close()).
DEFAULT_POLL_S: float = 0.05

# Bound on the per-wheel (timestamp, ticks) history kept for speed estimation.
_SAMPLE_MAXLEN = 4096


# --------------------------------------------------------------------------- #
# Pure, hardware-free helpers (unit-testable in isolation).
# --------------------------------------------------------------------------- #
# The 4-state quadrature decode table. Index = (prev_ab << 2) | cur_ab, where
# ab = (A << 1) | B.  +1 = A leads B (00 -> 10 -> 11 -> 01 -> 00), -1 = B leads A,
# 0 = no change OR an illegal two-bit jump (a missed edge).
_QUAD_TABLE: Tuple[int, ...] = (
    0, -1, +1, 0,   # prev 00 : cur 00, 01, 10, 11
    +1, 0, 0, -1,   # prev 01
    -1, 0, 0, +1,   # prev 10
    0, +1, -1, 0,   # prev 11
)


def quadrature_step(prev_ab: int, cur_ab: int) -> int:
    """Decode one quadrature transition → ``-1``, ``0`` or ``+1``.

    ``prev_ab``/``cur_ab`` are the 2-bit channel states ``(A << 1) | B`` before
    and after an edge. Returns ``+1`` when channel A leads channel B (the state
    walks ``00 → 10 → 11 → 01 → 00``), ``-1`` when B leads A, and ``0`` for no
    change or an illegal transition where BOTH bits flip (``00 ↔ 11``,
    ``01 ↔ 10`` — only possible if an edge was missed). Counting every edge this
    way is x4 decoding: one full electrical cycle sums to ``±4``.
    """
    if not (0 <= prev_ab <= 3) or not (0 <= cur_ab <= 3):
        raise ValueError(
            f"quadrature states must be 2-bit ints in 0..3, got prev={prev_ab!r} cur={cur_ab!r}"
        )
    return _QUAD_TABLE[(prev_ab << 2) | cur_ab]


def ticks_to_meters(ticks: float, ticks_per_rev: float, diameter_m: float) -> float:
    """Convert a (signed) tick count to metres of wheel travel.

    ``metres = ticks / ticks_per_rev * pi * diameter``. ``ticks_per_rev`` must be
    in the SAME counting mode as ``ticks`` (x4 here — see the module docstring).
    """
    if ticks_per_rev <= 0:
        raise ValueError(f"ticks_per_rev must be > 0, got {ticks_per_rev!r}")
    return float(ticks) / float(ticks_per_rev) * math.pi * float(diameter_m)


def meters_to_ticks(meters: float, ticks_per_rev: float, diameter_m: float) -> int:
    """Inverse of :func:`ticks_to_meters` (rounded to the nearest whole tick)."""
    if ticks_per_rev <= 0:
        raise ValueError(f"ticks_per_rev must be > 0, got {ticks_per_rev!r}")
    circumference = math.pi * float(diameter_m)
    if circumference <= 0.0:
        raise ValueError(f"diameter_m must be > 0, got {diameter_m!r}")
    return int(round(float(meters) / circumference * float(ticks_per_rev)))


# --------------------------------------------------------------------------- #
# The sample and the interface.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class EncoderSample:
    """One snapshot of both wheel encoders (immutable, safe to hand across threads).

    Ticks and metres are cumulative **since the last** :meth:`WheelEncoders.reset`
    and signed (reverse travel counts down). Speeds are signed and drop to ``0.0``
    once a wheel has been edge-free for longer than ``stale_s``. ``ok`` is the
    driver's health — ``False`` after :meth:`close`, if the reader thread died, or
    if the read itself failed — and a caller must NOT trust the numbers to be
    advancing while it is ``False``.
    """

    #: Signed x4 tick count, left wheel, since reset.
    left_ticks: int
    #: Signed x4 tick count, right wheel, since reset.
    right_ticks: int
    #: Left wheel travel in metres since reset (signed).
    left_m: float
    #: Right wheel travel in metres since reset (signed).
    right_m: float
    #: Mean of ``left_m`` and ``right_m`` — the robot's forward travel since reset.
    distance_m: float
    #: Left wheel speed, m/s, signed; 0.0 when stale.
    left_mps: float
    #: Right wheel speed, m/s, signed; 0.0 when stale.
    right_mps: float
    #: Heading change since reset, degrees, **positive = LEFT / CCW**.
    heading_delta_deg: float
    #: Monotonic time (s) the sample was taken (the injected clock if any).
    ts: float
    #: Driver health; ``False`` => do not trust the numbers to be live.
    ok: bool


@runtime_checkable
class WheelEncoders(Protocol):
    """Two quadrature wheel encoders read as one snapshot.

    Implementations must never raise from :meth:`read`; a failed read is
    reported as ``ok=False`` in the returned sample.
    """

    def read(self) -> EncoderSample:
        """Snapshot both wheels (cumulative since :meth:`reset`)."""
        ...

    def reset(self) -> None:
        """Zero the cumulative counts (and hence metres / heading)."""
        ...

    def close(self) -> None:
        """Stop the reader and release the GPIO. Idempotent."""
        ...


# --------------------------------------------------------------------------- #
# The laptop / simulation fake.
# --------------------------------------------------------------------------- #
class FakeWheelEncoders:
    """A pure-software :class:`WheelEncoders` for laptop tests and simulation.

    Distances are driven by :meth:`advance` (or :meth:`step`, which integrates
    the speeds set by :meth:`set_speeds` over ``dt_s``); :meth:`read` reports
    them tick-QUANTISED (metres are re-derived from the rounded tick count, so
    a sample is self-consistent exactly like the real driver — compare with
    ``pytest.approx``). Defaults to the real driver's geometry so the same
    ``ticks_per_wheel_rev`` / ``wheel_diameter_m`` / ``track_width_m`` maths is
    exercised on a laptop.
    """

    def __init__(
        self,
        *,
        ticks_per_wheel_rev: int = DEFAULT_TICKS_PER_WHEEL_REV,
        wheel_diameter_m: float = DEFAULT_WHEEL_DIAMETER_M,
        track_width_m: float = DEFAULT_TRACK_WIDTH_M,
        monotonic: Optional[Callable[[], float]] = None,
        ok: bool = True,
    ) -> None:
        _validate_geometry(ticks_per_wheel_rev, wheel_diameter_m, track_width_m)
        self._ticks_per_rev = int(ticks_per_wheel_rev)
        self._diameter_m = float(wheel_diameter_m)
        self._track_width_m = float(track_width_m)
        self._clock = monotonic
        #: Cumulative left/right travel (m) since reset — set via advance()/step().
        self.left_m = 0.0
        self.right_m = 0.0
        #: Reported speeds (m/s) — set via set_speeds().
        self.left_mps = 0.0
        self.right_mps = 0.0
        #: Health flag reported in every sample (see set_ok()).
        self.ok = bool(ok)
        self.closed = False
        #: Number of times read() / reset() were called.
        self.read_count = 0
        self.reset_count = 0

    # -- scripting -----------------------------------------------------------
    def advance(self, left_m: float, right_m: float) -> None:
        """Add ``left_m`` / ``right_m`` metres of travel (signed)."""
        self.left_m += float(left_m)
        self.right_m += float(right_m)

    def set_speeds(self, left_mps: float, right_mps: float) -> None:
        """Set the speeds reported by :meth:`read` (signed m/s)."""
        self.left_mps = float(left_mps)
        self.right_mps = float(right_mps)

    def step(self, dt_s: float) -> None:
        """Integrate the current speeds over ``dt_s`` seconds (simulation helper)."""
        dt = float(dt_s)
        self.advance(self.left_mps * dt, self.right_mps * dt)

    def set_ok(self, ok: bool) -> None:
        """Set the health flag reported in every sample."""
        self.ok = bool(ok)

    # -- WheelEncoders -------------------------------------------------------
    def _now(self) -> float:
        if self._clock is not None:
            return self._clock()
        return time.monotonic()

    def read(self) -> EncoderSample:
        self.read_count += 1
        left_ticks = meters_to_ticks(self.left_m, self._ticks_per_rev, self._diameter_m)
        right_ticks = meters_to_ticks(self.right_m, self._ticks_per_rev, self._diameter_m)
        return _build_sample(
            left_ticks=left_ticks,
            right_ticks=right_ticks,
            left_mps=0.0 if self.closed else self.left_mps,
            right_mps=0.0 if self.closed else self.right_mps,
            ticks_per_rev=self._ticks_per_rev,
            diameter_m=self._diameter_m,
            track_width_m=self._track_width_m,
            ts=self._now(),
            ok=self.ok and not self.closed,
        )

    def reset(self) -> None:
        self.reset_count += 1
        self.left_m = 0.0
        self.right_m = 0.0

    def close(self) -> None:
        self.closed = True

    def stats(self) -> Dict[str, Any]:
        """Counters mirroring :meth:`GpiodWheelEncoders.stats` (all zero here)."""
        return {
            "events": 0,
            "batches": 0,
            "errors": 0,
            "left_invalid": 0,
            "right_invalid": 0,
            "thread_alive": not self.closed,
        }


# --------------------------------------------------------------------------- #
# Shared maths.
# --------------------------------------------------------------------------- #
def _validate_geometry(ticks_per_wheel_rev: int, wheel_diameter_m: float, track_width_m: float) -> None:
    if int(ticks_per_wheel_rev) <= 0:
        raise ValueError(f"ticks_per_wheel_rev must be > 0, got {ticks_per_wheel_rev!r}")
    if float(wheel_diameter_m) <= 0.0:
        raise ValueError(f"wheel_diameter_m must be > 0, got {wheel_diameter_m!r}")
    if float(track_width_m) <= 0.0:
        raise ValueError(f"track_width_m must be > 0, got {track_width_m!r}")


def _build_sample(
    *,
    left_ticks: int,
    right_ticks: int,
    left_mps: float,
    right_mps: float,
    ticks_per_rev: int,
    diameter_m: float,
    track_width_m: float,
    ts: float,
    ok: bool,
) -> EncoderSample:
    """Turn signed tick counts + speeds into a fully derived :class:`EncoderSample`."""
    left_m = ticks_to_meters(left_ticks, ticks_per_rev, diameter_m)
    right_m = ticks_to_meters(right_ticks, ticks_per_rev, diameter_m)
    heading_rad = (right_m - left_m) / track_width_m
    return EncoderSample(
        left_ticks=int(left_ticks),
        right_ticks=int(right_ticks),
        left_m=left_m,
        right_m=right_m,
        distance_m=(left_m + right_m) / 2.0,
        left_mps=float(left_mps),
        right_mps=float(right_mps),
        heading_delta_deg=math.degrees(heading_rad),
        ts=float(ts),
        ok=bool(ok),
    )


class _Wheel:
    """Per-wheel decoder state, mutated ONLY under the driver lock."""

    __slots__ = (
        "name", "ab", "raw", "offset", "sign", "last_edge_ns", "last_seen_at",
        "samples", "invalid",
    )

    def __init__(self, name: str, invert: bool) -> None:
        self.name = name
        self.ab = 0                     # current (A << 1) | B
        self.raw = 0                    # signed ticks since construction (invert applied)
        self.offset = 0                 # raw at the last reset(); reported = raw - offset
        self.sign = -1 if invert else 1
        self.last_edge_ns: Optional[int] = None    # kernel stamp of the newest edge
        self.last_seen_at: Optional[float] = None  # driver clock when it was consumed
        self.samples: Deque[Tuple[int, int]] = deque(maxlen=_SAMPLE_MAXLEN)  # (edge_ns, raw)
        self.invalid = 0                # 0-step decodes (missed edges / glitches)


# --------------------------------------------------------------------------- #
# The real driver.
# --------------------------------------------------------------------------- #
class GpiodWheelEncoders:
    """The real :class:`WheelEncoders`: FIT0450 x4 quadrature on **libgpiod v2**.

    See the module docstring for wiring, the pin map, the x4 decoding choice and
    the speed/staleness/heading semantics. Construction requests the four lines
    (BOTH-edge, ``CLOCK_MONOTONIC`` stamps), seeds the A/B states from the live
    line values so the very first edge decodes in the right direction, and
    starts a daemon reader thread. :meth:`read` never raises; :meth:`close` stops
    the thread and releases the lines (idempotent). Instantiating this class is
    what needs ``python3-libgpiod`` v2 — importing the module does not.
    """

    def __init__(
        self,
        *,
        left_a_pin: int = DEFAULT_LEFT_A_PIN,
        left_b_pin: int = DEFAULT_LEFT_B_PIN,
        right_a_pin: int = DEFAULT_RIGHT_A_PIN,
        right_b_pin: int = DEFAULT_RIGHT_B_PIN,
        chip: str = config.GPIOCHIP,
        ticks_per_wheel_rev: int = DEFAULT_TICKS_PER_WHEEL_REV,
        wheel_diameter_m: float = DEFAULT_WHEEL_DIAMETER_M,
        track_width_m: float = DEFAULT_TRACK_WIDTH_M,
        left_invert: bool = False,
        right_invert: bool = False,
        stale_s: float = DEFAULT_STALE_S,
        speed_window_s: float = DEFAULT_SPEED_WINDOW_S,
        pull_up: bool = False,
        event_buffer_size: int = DEFAULT_EVENT_BUFFER_SIZE,
        poll_s: float = DEFAULT_POLL_S,
        consumer: str = "yalp-encoders",
        monotonic: Optional[Callable[[], float]] = None,
        _gpiod: Optional[Any] = None,
        _chip_paths: Optional[Iterable[str]] = None,
    ) -> None:
        # --- Validate BEFORE touching hardware --------------------------------
        pins = [int(left_a_pin), int(left_b_pin), int(right_a_pin), int(right_b_pin)]
        if len(set(pins)) != 4:
            raise ValueError(f"encoder pins must be four distinct GPIOs, got {pins!r}")
        _validate_geometry(ticks_per_wheel_rev, wheel_diameter_m, track_width_m)
        if float(speed_window_s) <= 0.0:
            raise ValueError(f"speed_window_s must be > 0, got {speed_window_s!r}")
        if float(poll_s) <= 0.0:
            raise ValueError(f"poll_s must be > 0, got {poll_s!r}")

        self._left_a, self._left_b, self._right_a, self._right_b = pins
        self._ticks_per_rev = int(ticks_per_wheel_rev)
        self._diameter_m = float(wheel_diameter_m)
        self._track_width_m = float(track_width_m)
        self._stale_s = max(0.0, float(stale_s))
        self._window_ns = int(float(speed_window_s) * 1e9)
        self._poll_s = float(poll_s)
        self._clock: Optional[Callable[[], float]] = monotonic

        self._left = _Wheel("left", bool(left_invert))
        self._right = _Wheel("right", bool(right_invert))
        # line offset -> (wheel, bit mask): A is bit 1 (mask 2), B is bit 0 (mask 1).
        self._line_map: Dict[int, Tuple[_Wheel, int]] = {
            self._left_a: (self._left, 2),
            self._left_b: (self._left, 1),
            self._right_a: (self._right, 2),
            self._right_b: (self._right, 1),
        }

        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._request: Any = None
        self._closed = False
        self._error: Optional[str] = None
        self._events = 0
        self._batches = 0
        self._errors = 0
        self._last_sample: Optional[EncoderSample] = None

        # --- Lazy hardware imports (keep the module laptop-importable) --------
        gpiod = _gpiod
        if gpiod is None:
            try:
                import gpiod  # type: ignore  # noqa: F401
            except Exception as exc:  # pragma: no cover - needs a real Pi env
                raise RuntimeError(
                    "python3-libgpiod v2 is required for GpiodWheelEncoders but "
                    f"could not be imported ({exc!r}). Install it on the Raspberry "
                    f"Pi (apt: python3-libgpiod, >= v2) and see {_PIN_FACTORY_DOCS}."
                ) from exc

        line = getattr(gpiod, "line", None)
        if line is None:  # pragma: no cover - defensive for odd builds
            import importlib

            line = importlib.import_module("gpiod.line")

        # Cache the enum members used on the hot path.
        self._RISING = gpiod.EdgeEvent.Type.RISING_EDGE
        self._FALLING = gpiod.EdgeEvent.Type.FALLING_EDGE
        self._VALUE_ACTIVE = line.Value.ACTIVE

        # Resolve the header GPIO chip by LABEL (never a hardcoded index).
        chip_path = _discover_gpiochip(gpiod, override=chip, chip_paths=_chip_paths)
        if chip_path is None:
            raise RuntimeError(
                "no matching GPIO chip found for GpiodWheelEncoders: could not "
                f"locate a controller labelled {_RP1_CHIP_LABEL!r} (the Pi 5 header "
                "GPIO). Set YALP_GPIOCHIP to a device path, index, or label "
                f"substring. See {_PIN_FACTORY_DOCS}."
            )

        # All four lines in ONE request: INPUT, BOTH edges, kernel MONOTONIC stamps.
        settings_kwargs: Dict[str, Any] = {
            "direction": line.Direction.INPUT,
            "edge_detection": line.Edge.BOTH,
            "event_clock": line.Clock.MONOTONIC,
        }
        if pull_up:
            settings_kwargs["bias"] = line.Bias.PULL_UP
        line_config = {pin: gpiod.LineSettings(**settings_kwargs) for pin in pins}
        self._request = self._request_lines(
            gpiod, chip_path, consumer, line_config, int(event_buffer_size)
        )

        try:
            self._seed_states()
            self._thread = threading.Thread(
                target=self._run, name="yalp-encoders", daemon=True
            )
            self._thread.start()
        except Exception:
            # Never leak a line request out of a failed constructor.
            try:
                self._request.release()
            except Exception:  # pragma: no cover - best effort
                pass
            self._request = None
            raise

    # -- construction helpers ------------------------------------------------
    @staticmethod
    def _request_lines(
        gpiod: Any, chip_path: str, consumer: str, line_config: Dict[int, Any],
        event_buffer_size: int,
    ) -> Any:
        """``gpiod.request_lines`` with a big event buffer, tolerating old bindings.

        ``event_buffer_size`` landed with the v2 Python bindings; if this binding's
        ``request_lines`` does not accept it (``TypeError``) fall back to the
        kernel default (16 x lines = 64 events — only ~3 ms of headroom at top
        speed, so prefer a binding that takes the argument).
        """
        try:
            return gpiod.request_lines(
                chip_path,
                consumer=consumer,
                config=line_config,
                event_buffer_size=event_buffer_size,
            )
        except TypeError:
            logger.warning(
                "this python3-libgpiod build does not accept event_buffer_size; "
                "falling back to the kernel default edge-event buffer (64 events) — "
                "expect dropped edges at speed."
            )
            return gpiod.request_lines(chip_path, consumer=consumer, config=line_config)

    def _seed_states(self) -> None:
        """Read the live A/B levels so the first edge decodes in the right direction."""
        offsets = [self._left_a, self._left_b, self._right_a, self._right_b]
        try:
            values = list(self._request.get_values(offsets))
        except Exception:  # pragma: no cover - defensive: assume all low
            values = []

        def bit(i: int) -> int:
            return 1 if (i < len(values) and values[i] == self._VALUE_ACTIVE) else 0

        with self._lock:
            self._left.ab = (bit(0) << 1) | bit(1)
            self._right.ab = (bit(2) << 1) | bit(3)

    # -- clock ---------------------------------------------------------------
    def _now(self) -> float:
        """The (possibly injected) monotonic clock, resolved at CALL time."""
        if self._clock is not None:
            return self._clock()
        return time.monotonic()

    # -- reader thread -------------------------------------------------------
    def _run(self) -> None:
        req = self._request
        poll = self._poll_s
        stop = self._stop
        while not stop.is_set():
            try:
                if not req.wait_edge_events(poll):
                    continue
                events = req.read_edge_events()
            except Exception as exc:
                if stop.is_set():
                    break  # the request was released under us during close()
                with self._lock:
                    self._error = repr(exc)
                    self._errors += 1
                logger.warning("wheel encoder reader thread stopped: %r", exc)
                break
            if not events:
                continue
            self._ingest(events, self._now())

    def _ingest(self, events: Iterable[Any], now: float) -> None:
        """Decode one batch of kernel edge events into the per-wheel states."""
        line_map = self._line_map
        table = _QUAD_TABLE
        rising = self._RISING
        falling = self._FALLING
        touched: List[_Wheel] = []
        seen = 0
        with self._lock:
            for ev in events:
                seen += 1
                entry = line_map.get(ev.line_offset)
                if entry is None:
                    continue  # not one of our four lines
                wheel, mask = entry
                prev = wheel.ab
                kind = ev.event_type
                if kind == rising:
                    cur = prev | mask
                elif kind == falling:
                    cur = prev & ~mask
                else:  # pragma: no cover - unknown event kind
                    continue
                step = table[(prev << 2) | cur]
                wheel.ab = cur
                if step:
                    wheel.raw += step * wheel.sign
                else:
                    wheel.invalid += 1  # repeated edge: a missed event upstream
                wheel.last_edge_ns = int(ev.timestamp_ns)
                if wheel not in touched:
                    touched.append(wheel)
            window = self._window_ns
            for wheel in touched:
                wheel.last_seen_at = now
                samples = wheel.samples
                samples.append((wheel.last_edge_ns, wheel.raw))  # type: ignore[arg-type]
                newest = samples[-1][0]
                # Keep the trailing window, but always at least one interval.
                while len(samples) > 2 and newest - samples[0][0] > window:
                    samples.popleft()
            self._events += seen
            self._batches += 1

    # -- reading -------------------------------------------------------------
    def _ticks_per_s(self, wheel: _Wheel, now: float) -> float:
        """Windowed Δticks/Δt from KERNEL stamps; 0 when stale or too few edges."""
        if wheel.last_seen_at is None or (now - wheel.last_seen_at) > self._stale_s:
            return 0.0
        samples = wheel.samples
        if len(samples) < 2:
            return 0.0
        t0, n0 = samples[0]
        t1, n1 = samples[-1]
        dt_ns = t1 - t0
        if dt_ns <= 0:
            return 0.0
        return (n1 - n0) * 1e9 / dt_ns

    def _healthy(self) -> bool:
        thread = self._thread
        return (
            not self._closed
            and self._error is None
            and self._request is not None
            and thread is not None
            and thread.is_alive()
        )

    def _sample(self, now: float) -> EncoderSample:
        with self._lock:
            left_ticks = self._left.raw - self._left.offset
            right_ticks = self._right.raw - self._right.offset
            left_tps = self._ticks_per_s(self._left, now)
            right_tps = self._ticks_per_s(self._right, now)
            ok = self._healthy()
        circumference = math.pi * self._diameter_m
        return _build_sample(
            left_ticks=left_ticks,
            right_ticks=right_ticks,
            left_mps=left_tps / self._ticks_per_rev * circumference,
            right_mps=right_tps / self._ticks_per_rev * circumference,
            ticks_per_rev=self._ticks_per_rev,
            diameter_m=self._diameter_m,
            track_width_m=self._track_width_m,
            ts=now,
            ok=ok,
        )

    def read(self) -> EncoderSample:
        """Snapshot both wheels. NEVER raises: a failed read re-serves the last
        sample (or zeros) with ``ok=False`` and the speeds zeroed."""
        try:
            now = self._now()
        except Exception:  # pragma: no cover - a broken injected clock
            now = 0.0
        try:
            sample = self._sample(now)
        except Exception as exc:
            logger.warning("wheel encoder read failed: %r", exc)
            last = self._last_sample
            if last is not None:
                return dataclasses.replace(last, left_mps=0.0, right_mps=0.0, ts=now, ok=False)
            return EncoderSample(0, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, now, False)
        self._last_sample = sample
        return sample

    def reset(self) -> None:
        """Zero the reported counts (metres / heading follow). Speed is unaffected:
        the speed window works on raw ticks, so a reset mid-drive does not blank
        the speed estimate."""
        with self._lock:
            self._left.offset = self._left.raw
            self._right.offset = self._right.raw

    def stats(self) -> Dict[str, Any]:
        """Read-only observability counters (a fresh dict)::

            events         every edge event consumed (incl. ignored lines)
            batches        read_edge_events() batches consumed
            errors         reader-thread failures (the thread stops on the first)
            left_invalid   0-step decodes on the left wheel (missed edges / glitches)
            right_invalid  same for the right wheel
            thread_alive   whether the reader thread is running
        """
        thread = self._thread
        with self._lock:
            return {
                "events": self._events,
                "batches": self._batches,
                "errors": self._errors,
                "left_invalid": self._left.invalid,
                "right_invalid": self._right.invalid,
                "thread_alive": bool(thread is not None and thread.is_alive()),
            }

    def close(self) -> None:
        """Stop the reader thread, then release the line request. Idempotent and
        never raises; :meth:`read` afterwards returns the final counts with
        ``ok=False``."""
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            try:
                thread.join(timeout=max(0.5, 4.0 * self._poll_s))
            except Exception:  # pragma: no cover - best effort during teardown
                pass
        req = self._request
        self._request = None
        if req is not None:
            try:
                req.release()
            except Exception:  # pragma: no cover - best effort during teardown
                pass


__all__ = [
    "EncoderSample",
    "WheelEncoders",
    "FakeWheelEncoders",
    "GpiodWheelEncoders",
    "quadrature_step",
    "ticks_to_meters",
    "meters_to_ticks",
    "DEFAULT_TICKS_PER_WHEEL_REV",
    "DEFAULT_WHEEL_DIAMETER_M",
    "DEFAULT_TRACK_WIDTH_M",
    "DEFAULT_LEFT_A_PIN",
    "DEFAULT_LEFT_B_PIN",
    "DEFAULT_RIGHT_A_PIN",
    "DEFAULT_RIGHT_B_PIN",
    "DEFAULT_STALE_S",
    "DEFAULT_SPEED_WINDOW_S",
]
