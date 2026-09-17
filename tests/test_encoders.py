"""Tests for the FIT0450 quadrature wheel-encoder driver (libgpiod v2).

``python3-libgpiod`` is NOT installed on the laptop/CI that runs these tests, so
the driver is exercised against a *fake* gpiod v2 module — either injected via
``_gpiod=`` or planted in ``sys.modules`` — whose fake line request delivers
scripted, kernel-style edge events (``line_offset`` / ``event_type`` /
``timestamp_ns``) to the driver's real background reader thread. We verify the
pure decode table and unit maths in isolation, then the driver contract: signed
x4 counting, direction, the invert flags, state seeding from live line levels,
speed from KERNEL timestamps (windowed, staleness → 0), distance/heading maths,
reset, close, never-raise, thread failure, chip discovery, and that the module
imports with no hardware libraries present.

Real electrical behaviour (which physical direction is "+") can only be
confirmed on hardware — that is what the ``*_invert`` flags are for.
"""

from __future__ import annotations

import math
import queue
import sys
import time
import types

import pytest

from tests._import_isolation import assert_import_leaves_module_unloaded
from yalp.reactive import encoders as enc_mod
from yalp.reactive.encoders import (
    DEFAULT_LEFT_A_PIN,
    DEFAULT_LEFT_B_PIN,
    DEFAULT_RIGHT_A_PIN,
    DEFAULT_RIGHT_B_PIN,
    DEFAULT_TICKS_PER_WHEEL_REV,
    EncoderSample,
    FakeWheelEncoders,
    GpiodWheelEncoders,
    WheelEncoders,
    meters_to_ticks,
    quadrature_step,
    ticks_to_meters,
)


# --------------------------------------------------------------------------- #
# 1. Module imports with NO gpiod / gpiozero / lgpio present (lazy imports).
# --------------------------------------------------------------------------- #
def test_module_imports_without_hardware_libs():
    assert_import_leaves_module_unloaded(
        "yalp.reactive.encoders", ("gpiod", "gpiozero", "lgpio")
    )
    assert hasattr(enc_mod, "GpiodWheelEncoders")


# --------------------------------------------------------------------------- #
# 2. Pure helpers: the decode table and the unit maths.
# --------------------------------------------------------------------------- #
# State = (A << 1) | B. "A leads B" walks 00 -> 10 -> 11 -> 01 -> 00.
_A_LEADS_CYCLE = (0b00, 0b10, 0b11, 0b01)


def _expected_step(prev, cur):
    """Reference decoder built from the cycle definition, independent of the table."""
    i = _A_LEADS_CYCLE.index(prev)
    if cur == _A_LEADS_CYCLE[(i + 1) % 4]:
        return +1
    if cur == _A_LEADS_CYCLE[(i - 1) % 4]:
        return -1
    return 0


def test_quadrature_step_table_is_exhaustive_over_all_16_transitions():
    seen = {+1: 0, -1: 0, 0: 0}
    for prev in range(4):
        for cur in range(4):
            step = quadrature_step(prev, cur)
            assert step == _expected_step(prev, cur), (prev, cur)
            seen[step] += 1
    # 4 forward, 4 reverse, 8 zero (4 no-change + 4 illegal double-bit jumps).
    assert seen == {+1: 4, -1: 4, 0: 8}


def test_quadrature_step_invalid_double_bit_transitions_are_zero():
    for prev, cur in ((0b00, 0b11), (0b11, 0b00), (0b01, 0b10), (0b10, 0b01)):
        assert quadrature_step(prev, cur) == 0


def test_quadrature_step_full_cycle_sums_to_plus_or_minus_four():
    fwd = sum(quadrature_step(_A_LEADS_CYCLE[i], _A_LEADS_CYCLE[(i + 1) % 4]) for i in range(4))
    rev = sum(quadrature_step(_A_LEADS_CYCLE[(i + 1) % 4], _A_LEADS_CYCLE[i]) for i in range(4))
    assert (fwd, rev) == (+4, -4)


def test_quadrature_step_rejects_out_of_range_states():
    with pytest.raises(ValueError):
        quadrature_step(4, 0)
    with pytest.raises(ValueError):
        quadrature_step(0, -1)


def test_ticks_to_meters_matches_circumference():
    # One full x4 revolution of a 65 mm wheel is exactly its circumference.
    assert ticks_to_meters(7680, 7680, 0.065) == pytest.approx(math.pi * 0.065)
    # The same distance expressed in x1 ticks with an x1 per-rev constant.
    assert ticks_to_meters(1920, 1920, 0.065) == pytest.approx(math.pi * 0.065)
    assert ticks_to_meters(0, 7680, 0.065) == 0.0
    assert ticks_to_meters(-3840, 7680, 0.065) == pytest.approx(-math.pi * 0.065 / 2)
    with pytest.raises(ValueError):
        ticks_to_meters(1, 0, 0.065)


def test_meters_to_ticks_round_trips():
    for m in (0.0, 0.25, -1.0, 3.14159):
        t = meters_to_ticks(m, 7680, 0.065)
        assert isinstance(t, int)
        assert ticks_to_meters(t, 7680, 0.065) == pytest.approx(m, abs=1e-4)
    with pytest.raises(ValueError):
        meters_to_ticks(1.0, 7680, 0.0)


def test_default_ticks_per_rev_is_fit0450_x4():
    assert DEFAULT_TICKS_PER_WHEEL_REV == 16 * 120 * 4 == 7680


# --------------------------------------------------------------------------- #
# A fake python3-libgpiod v2 module whose request delivers scripted edge events.
# --------------------------------------------------------------------------- #
_RISING = "RISING_EDGE"
_FALLING = "FALLING_EDGE"
_ACTIVE = "ACTIVE"
_INACTIVE = "INACTIVE"

LA, LB, RA, RB = DEFAULT_LEFT_A_PIN, DEFAULT_LEFT_B_PIN, DEFAULT_RIGHT_A_PIN, DEFAULT_RIGHT_B_PIN


def _ev(line_offset, rising, timestamp_ns):
    """A fake gpiod EdgeEvent (only the fields the driver reads)."""
    return types.SimpleNamespace(
        line_offset=line_offset,
        event_type=_RISING if rising else _FALLING,
        timestamp_ns=timestamp_ns,
    )


def _fwd_cycle(a, b, t0_ns, dt_ns=1_000_000):
    """One A-leads-B electrical cycle (+4 ticks): A up, B up, A down, B down."""
    return [_ev(a, True, t0_ns), _ev(b, True, t0_ns + dt_ns),
            _ev(a, False, t0_ns + 2 * dt_ns), _ev(b, False, t0_ns + 3 * dt_ns)]


def _rev_cycle(a, b, t0_ns, dt_ns=1_000_000):
    """One B-leads-A electrical cycle (-4 ticks): B up, A up, B down, A down."""
    return [_ev(b, True, t0_ns), _ev(a, True, t0_ns + dt_ns),
            _ev(b, False, t0_ns + 2 * dt_ns), _ev(a, False, t0_ns + 3 * dt_ns)]


class _FakeRequest:
    """A fake ``gpiod`` LineRequest fed from a thread-safe queue of event batches.

    The driver's reader thread blocks in ``wait_edge_events(timeout)``; a test
    ``push``es a batch and it is delivered on the next ``read_edge_events``.
    """

    def __init__(self, initial_values=None):
        self._q = queue.Queue()
        self._pending = []
        self.initial_values = dict(initial_values or {})
        self.released = 0
        self.wait_timeouts = []
        self.get_values_calls = []
        self.fail_wait = False

    def push(self, events):
        self._q.put(list(events))

    def push_each(self, events):
        """Deliver every event as its own batch (one speed sample per edge)."""
        for ev in events:
            self._q.put([ev])

    def wait_edge_events(self, timeout):
        self.wait_timeouts.append(timeout)
        if self.fail_wait:
            raise RuntimeError("simulated gpiod failure")
        try:
            self._pending = self._q.get(timeout=timeout)
        except queue.Empty:
            self._pending = []
            return False
        return True

    def read_edge_events(self):
        evs, self._pending = self._pending, []
        return evs

    def get_values(self, offsets):
        self.get_values_calls.append(list(offsets))
        return [self.initial_values.get(o, _INACTIVE) for o in offsets]

    def release(self):
        self.released += 1


class _FakeChip:
    def __init__(self, path, label):
        self._label = label

    def get_info(self):
        return types.SimpleNamespace(label=self._label)

    def close(self):
        pass


def _make_fake_gpiod(request, chip_labels=None, accept_buffer_size=True):
    chip_labels = chip_labels or {"/dev/gpiochip0": "pinctrl-rp1"}
    mod = types.ModuleType("gpiod")
    mod.EdgeEvent = types.SimpleNamespace(
        Type=types.SimpleNamespace(RISING_EDGE=_RISING, FALLING_EDGE=_FALLING)
    )
    line = types.ModuleType("gpiod.line")
    line.Value = types.SimpleNamespace(ACTIVE=_ACTIVE, INACTIVE=_INACTIVE)
    line.Direction = types.SimpleNamespace(OUTPUT="OUTPUT", INPUT="INPUT")
    line.Edge = types.SimpleNamespace(BOTH="BOTH", RISING="RISING", FALLING="FALLING")
    line.Clock = types.SimpleNamespace(MONOTONIC="MONOTONIC", REALTIME="REALTIME")
    line.Bias = types.SimpleNamespace(PULL_UP="PULL_UP", PULL_DOWN="PULL_DOWN", DISABLED="DISABLED")
    mod.line = line

    class _LineSettings:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    mod.LineSettings = _LineSettings
    captured = {"calls": 0}

    if accept_buffer_size:
        def request_lines(path, consumer=None, config=None, event_buffer_size=None):
            captured.update(path=path, consumer=consumer, config=config,
                            event_buffer_size=event_buffer_size)
            captured["calls"] += 1
            return request
    else:
        def request_lines(path, consumer=None, config=None):  # old binding: no kwarg
            captured.update(path=path, consumer=consumer, config=config)
            captured["calls"] += 1
            return request

    mod.request_lines = request_lines
    mod.request_calls = captured
    mod.Chip = lambda path: _FakeChip(path, chip_labels.get(path, "unknown"))
    mod.__version__ = "2.1.0"
    return mod


def _wait_until(pred, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.002)
    return pred()


def _wait_events(enc, n, timeout=3.0):
    assert _wait_until(lambda: enc.stats()["events"] >= n, timeout), (
        f"reader thread consumed {enc.stats()['events']} < {n} events"
    )


@pytest.fixture()
def factory():
    """Build encoders wired to a fake gpiod + injected clock; close them all at teardown."""
    made = []

    def _make(req=None, clock=None, chip_labels=None, chip_paths=None, gpiod=None, **kw):
        req = req if req is not None else _FakeRequest()
        clock = clock if clock is not None else {"now": 100.0}
        gpiod = gpiod if gpiod is not None else _make_fake_gpiod(req, chip_labels=chip_labels)
        # Crisp geometry unless a test overrides: 1000 ticks per rev on a wheel
        # whose circumference is exactly 1 m => 1000 ticks = 1 m, 1 tick = 1 mm.
        kw.setdefault("ticks_per_wheel_rev", 1000)
        kw.setdefault("wheel_diameter_m", 1.0 / math.pi)
        kw.setdefault("track_width_m", 0.5)
        kw.setdefault("poll_s", 0.01)
        enc = GpiodWheelEncoders(
            monotonic=lambda: clock["now"],
            _gpiod=gpiod,
            _chip_paths=chip_paths if chip_paths is not None else ["/dev/gpiochip0"],
            **kw,
        )
        made.append(enc)
        return enc, req, clock, gpiod

    yield _make
    for e in made:
        e.close()


# --------------------------------------------------------------------------- #
# 3. Construction: four lines, BOTH edges, MONOTONIC clock, thread running.
# --------------------------------------------------------------------------- #
def test_construction_requests_four_input_lines_with_both_edges_and_kernel_clock(factory):
    enc, req, clock, gpiod = factory(consumer="yalp-test-enc")
    assert isinstance(enc, WheelEncoders)
    cap = gpiod.request_calls
    assert cap["path"] == "/dev/gpiochip0"
    assert cap["consumer"] == "yalp-test-enc"
    assert set(cap["config"].keys()) == {LA, LB, RA, RB} == {16, 26, 20, 21}
    for pin, settings in cap["config"].items():
        assert settings.kwargs["direction"] == "INPUT", pin
        assert settings.kwargs["edge_detection"] == "BOTH", pin
        assert settings.kwargs["event_clock"] == "MONOTONIC", pin
        assert "bias" not in settings.kwargs
    assert cap["event_buffer_size"] == 1024
    # The initial A/B levels were read to seed the decoder.
    assert req.get_values_calls == [[LA, LB, RA, RB]]
    assert enc.stats()["thread_alive"] is True

    s = enc.read()
    assert isinstance(s, EncoderSample)
    assert (s.left_ticks, s.right_ticks) == (0, 0)
    assert (s.left_m, s.right_m, s.distance_m, s.heading_delta_deg) == (0.0, 0.0, 0.0, 0.0)
    assert (s.left_mps, s.right_mps) == (0.0, 0.0)
    assert s.ts == 100.0
    assert s.ok is True


def test_custom_pins_and_pull_up_are_honoured(factory):
    enc, req, clock, gpiod = factory(left_a_pin=5, left_b_pin=6, right_a_pin=7,
                                     right_b_pin=8, pull_up=True)
    cfg = gpiod.request_calls["config"]
    assert set(cfg) == {5, 6, 7, 8}
    assert all(s.kwargs["bias"] == "PULL_UP" for s in cfg.values())


def test_construction_validates_arguments(factory):
    with pytest.raises(ValueError, match="distinct"):
        factory(left_a_pin=16, left_b_pin=16)
    with pytest.raises(ValueError, match="ticks_per_wheel_rev"):
        factory(ticks_per_wheel_rev=0)
    with pytest.raises(ValueError, match="wheel_diameter_m"):
        factory(wheel_diameter_m=-0.1)
    with pytest.raises(ValueError, match="track_width_m"):
        factory(track_width_m=0.0)


def test_old_binding_without_event_buffer_size_falls_back(factory, caplog):
    req = _FakeRequest()
    gpiod = _make_fake_gpiod(req, accept_buffer_size=False)
    enc, req, clock, gpiod = factory(req=req, gpiod=gpiod)
    assert gpiod.request_calls["calls"] == 1
    assert "event_buffer_size" not in gpiod.request_calls
    assert enc.read().ok is True


def test_chip_discovery_failure_raises_before_any_request(factory):
    req = _FakeRequest()
    with pytest.raises(RuntimeError, match="no matching GPIO chip"):
        factory(req=req, chip_labels={"/dev/gpiochip0": "gpio-brcmstb"},
                chip_paths=["/dev/gpiochip0"])
    assert req.released == 0


def test_chip_override_path_is_used_verbatim(factory):
    enc, req, clock, gpiod = factory(chip="/dev/gpiochip7")
    assert gpiod.request_calls["path"] == "/dev/gpiochip7"


def test_lazy_import_picks_up_gpiod_from_sys_modules(monkeypatch):
    """With no ``_gpiod=`` injection the driver imports ``gpiod`` lazily."""
    req = _FakeRequest()
    fake = _make_fake_gpiod(req)
    monkeypatch.setitem(sys.modules, "gpiod", fake)
    monkeypatch.setitem(sys.modules, "gpiod.line", fake.line)
    enc = GpiodWheelEncoders(poll_s=0.01, _chip_paths=["/dev/gpiochip0"])
    try:
        assert fake.request_calls["path"] == "/dev/gpiochip0"
        assert enc.read().ok is True
    finally:
        enc.close()
    assert req.released == 1


# --------------------------------------------------------------------------- #
# 4. Counting: signed x4 ticks, direction, invert flags, seeding.
# --------------------------------------------------------------------------- #
def test_forward_cycle_counts_plus_four_on_that_wheel_only(factory):
    enc, req, clock, _ = factory()
    req.push(_fwd_cycle(LA, LB, 1_000_000))
    _wait_events(enc, 4)
    s = enc.read()
    assert (s.left_ticks, s.right_ticks) == (4, 0)
    assert s.ok is True


def test_reverse_cycle_counts_minus_four(factory):
    enc, req, clock, _ = factory()
    req.push(_rev_cycle(RA, RB, 1_000_000))
    _wait_events(enc, 4)
    s = enc.read()
    assert (s.left_ticks, s.right_ticks) == (0, -4)


def test_wheels_count_independently_and_signed(factory):
    enc, req, clock, _ = factory()
    events = _fwd_cycle(LA, LB, 1_000_000) + _fwd_cycle(LA, LB, 5_000_000)
    events += _rev_cycle(RA, RB, 1_000_000)
    req.push(events)
    _wait_events(enc, 12)
    s = enc.read()
    assert (s.left_ticks, s.right_ticks) == (8, -4)


def test_invert_flags_flip_the_counting_direction(factory):
    enc, req, clock, _ = factory(left_invert=True, right_invert=True)
    req.push(_fwd_cycle(LA, LB, 1_000_000) + _rev_cycle(RA, RB, 1_000_000))
    _wait_events(enc, 8)
    s = enc.read()
    assert (s.left_ticks, s.right_ticks) == (-4, +4)


def test_initial_line_levels_seed_the_decoder_state(factory):
    """Left A is already HIGH at start: the first B-rising edge is 10 -> 11 = +1.

    Without seeding the driver would assume 00 and decode 00 -> 01 = -1.
    """
    req = _FakeRequest(initial_values={LA: _ACTIVE})
    enc, req, clock, _ = factory(req=req)
    req.push([_ev(LB, True, 1_000_000)])
    _wait_events(enc, 1)
    assert enc.read().left_ticks == +1


def test_repeated_edge_is_counted_as_invalid_not_as_a_tick(factory):
    """A missed event upstream shows as a repeated edge: 0-step, invalid++."""
    enc, req, clock, _ = factory()
    req.push([_ev(LA, True, 1_000_000), _ev(LA, True, 2_000_000)])
    _wait_events(enc, 2)
    s = enc.read()
    assert s.left_ticks == 1
    st = enc.stats()
    assert st["left_invalid"] == 1
    assert st["right_invalid"] == 0


def test_events_on_unrelated_lines_are_ignored(factory):
    enc, req, clock, _ = factory()
    req.push([_ev(99, True, 1_000_000), _ev(99, False, 2_000_000)])
    _wait_events(enc, 2)
    s = enc.read()
    assert (s.left_ticks, s.right_ticks) == (0, 0)
    assert enc.stats()["left_invalid"] == 0


# --------------------------------------------------------------------------- #
# 5. Distance and heading maths.
# --------------------------------------------------------------------------- #
def test_distance_and_heading_derive_from_ticks(factory):
    # 1000 ticks = 1 m, track 0.5 m. Left +8 mm, right -4 mm.
    enc, req, clock, _ = factory()
    events = _fwd_cycle(LA, LB, 1_000_000) + _fwd_cycle(LA, LB, 5_000_000)
    events += _rev_cycle(RA, RB, 1_000_000)
    req.push(events)
    _wait_events(enc, 12)
    s = enc.read()
    assert s.left_m == pytest.approx(0.008)
    assert s.right_m == pytest.approx(-0.004)
    assert s.distance_m == pytest.approx(0.002)      # mean of the two
    # (right - left) / track = (-0.004 - 0.008) / 0.5 = -0.024 rad -> RIGHT / CW.
    assert s.heading_delta_deg == pytest.approx(math.degrees(-0.024))


def test_heading_positive_when_right_wheel_travels_further(factory):
    enc, req, clock, _ = factory()
    req.push(_fwd_cycle(RA, RB, 1_000_000))
    _wait_events(enc, 4)
    s = enc.read()
    assert s.heading_delta_deg > 0.0   # positive = LEFT / CCW, matches YawSample
    assert s.heading_delta_deg == pytest.approx(math.degrees(0.004 / 0.5))


def test_real_geometry_defaults_convert_one_rev_to_circumference(factory):
    enc, req, clock, _ = factory(ticks_per_wheel_rev=DEFAULT_TICKS_PER_WHEEL_REV,
                                 wheel_diameter_m=0.065, track_width_m=0.14)
    events = []
    t = 1_000_000
    for _ in range(7680 // 4):
        events += _fwd_cycle(LA, LB, t, dt_ns=1000)
        t += 4000
    req.push(events)
    _wait_events(enc, 7680)
    s = enc.read()
    assert s.left_ticks == 7680
    assert s.left_m == pytest.approx(math.pi * 0.065)


# --------------------------------------------------------------------------- #
# 6. Speed from KERNEL timestamps; window; staleness -> 0.
# --------------------------------------------------------------------------- #
def test_speed_comes_from_kernel_timestamps(factory):
    """8 edges 1 ms apart (per-edge batches) -> 7 ticks / 7 ms = 1000 ticks/s = 1 m/s.

    The Python clock is frozen throughout, so the only source of Δt is the
    event timestamps.
    """
    enc, req, clock, _ = factory()
    t0 = 1_000_000_000
    req.push_each(_fwd_cycle(LA, LB, t0) + _fwd_cycle(LA, LB, t0 + 4_000_000))
    _wait_events(enc, 8)
    s = enc.read()
    assert s.left_ticks == 8
    assert s.left_mps == pytest.approx(1.0)
    assert s.right_mps == 0.0


def test_reverse_speed_is_negative(factory):
    enc, req, clock, _ = factory()
    t0 = 1_000_000_000
    req.push_each(_rev_cycle(RA, RB, t0) + _rev_cycle(RA, RB, t0 + 4_000_000))
    _wait_events(enc, 8)
    assert enc.read().right_mps == pytest.approx(-1.0)


def test_speed_uses_the_trailing_window_not_the_whole_history(factory):
    enc, req, clock, _ = factory(speed_window_s=0.10)
    ms = 1_000_000
    t0 = 1_000_000_000
    # Phase 1: one edge every 50 ms for 10 edges (t = 0 .. 450 ms) -> 20 ticks/s.
    seq = [_ev(LA, True, t0), _ev(LB, True, t0 + 50 * ms), _ev(LA, False, t0 + 100 * ms),
           _ev(LB, False, t0 + 150 * ms)]
    seq += [_ev(LA, True, t0 + 200 * ms), _ev(LB, True, t0 + 250 * ms),
            _ev(LA, False, t0 + 300 * ms), _ev(LB, False, t0 + 350 * ms)]
    seq += [_ev(LA, True, t0 + 400 * ms), _ev(LB, True, t0 + 450 * ms)]
    req.push_each(seq)
    _wait_events(enc, 10)
    assert enc.read().left_mps == pytest.approx(0.020)

    # Phase 2: one edge every 10 ms for the next 100 ms -> 100 ticks/s. A whole-
    # history average would be 19 ticks / 550 ms = 34.5 ticks/s; the window must
    # report the recent rate instead.
    t = t0 + 450 * ms
    state = [(LA, False), (LB, False), (LA, True), (LB, True)]  # continue from ab=11
    seq2 = []
    for i in range(10):
        line, rising = state[i % 4]
        t += 10 * ms
        seq2.append(_ev(line, rising, t))
    req.push_each(seq2)
    _wait_events(enc, 20)
    s = enc.read()
    assert s.left_ticks == 20
    assert s.left_mps == pytest.approx(0.100)


def test_speed_is_zero_once_stale_but_ticks_are_kept(factory):
    enc, req, clock, _ = factory(stale_s=0.25)
    t0 = 1_000_000_000
    req.push_each(_fwd_cycle(LA, LB, t0) + _fwd_cycle(LA, LB, t0 + 4_000_000))
    _wait_events(enc, 8)
    assert enc.read().left_mps == pytest.approx(1.0)

    clock["now"] += 0.20                 # inside the 0.25 s staleness budget
    assert enc.read().left_mps == pytest.approx(1.0)

    clock["now"] += 0.10                 # 0.30 s since the last edge -> stale
    s = enc.read()
    assert s.left_mps == 0.0
    assert s.left_ticks == 8             # distance is NOT forgotten
    assert s.ok is True


def test_single_edge_gives_no_speed_yet(factory):
    enc, req, clock, _ = factory()
    req.push([_ev(LA, True, 1_000_000)])
    _wait_events(enc, 1)
    s = enc.read()
    assert s.left_ticks == 1
    assert s.left_mps == 0.0


# --------------------------------------------------------------------------- #
# 7. reset(), close(), never-raise, thread failure.
# --------------------------------------------------------------------------- #
def test_reset_zeroes_counts_but_keeps_the_speed_estimate(factory):
    enc, req, clock, _ = factory()
    t0 = 1_000_000_000
    req.push_each(_fwd_cycle(LA, LB, t0) + _fwd_cycle(LA, LB, t0 + 4_000_000)
                  + _rev_cycle(RA, RB, t0))
    _wait_events(enc, 12)
    before = enc.read()
    assert (before.left_ticks, before.right_ticks) == (8, -4)

    enc.reset()
    s = enc.read()
    assert (s.left_ticks, s.right_ticks) == (0, 0)
    assert (s.left_m, s.right_m, s.distance_m, s.heading_delta_deg) == (0.0, 0.0, 0.0, 0.0)
    assert s.left_mps == pytest.approx(1.0)   # speed window is on raw ticks

    # Counting continues from zero after the reset.
    req.push(_fwd_cycle(LA, LB, t0 + 20_000_000))
    _wait_events(enc, 16)
    assert enc.read().left_ticks == 4


def test_close_stops_thread_releases_once_and_read_still_works(factory):
    enc, req, clock, _ = factory()
    req.push(_fwd_cycle(LA, LB, 1_000_000))
    _wait_events(enc, 4)

    enc.close()
    assert req.released == 1
    assert _wait_until(lambda: not enc.stats()["thread_alive"], 2.0)
    enc.close()                          # idempotent
    assert req.released == 1

    s = enc.read()                       # never raises after close
    assert s.ok is False
    assert s.left_ticks == 4             # the final counts are still readable
    assert (s.left_mps, s.right_mps) == (0.0, 0.0)


def test_reader_thread_polls_with_bounded_timeout_so_close_cannot_hang(factory):
    enc, req, clock, _ = factory(poll_s=0.01)
    assert _wait_until(lambda: len(req.wait_timeouts) >= 3, 2.0)
    assert set(req.wait_timeouts) == {0.01}
    t0 = time.monotonic()
    enc.close()
    assert time.monotonic() - t0 < 1.0


def test_read_never_raises_even_if_the_sampler_blows_up(factory):
    enc, req, clock, _ = factory()
    req.push(_fwd_cycle(LA, LB, 1_000_000))
    _wait_events(enc, 4)
    good = enc.read()
    assert good.left_ticks == 4 and good.ok is True

    def _boom(now):
        raise RuntimeError("simulated sampler failure")

    enc._sample = _boom                  # instance attribute shadows the method
    s = enc.read()
    assert s.ok is False
    assert s.left_ticks == 4             # re-serves the last good counts
    assert (s.left_mps, s.right_mps) == (0.0, 0.0)


def test_reader_thread_failure_is_reported_as_not_ok(factory):
    enc, req, clock, _ = factory()
    assert enc.read().ok is True
    req.fail_wait = True
    assert _wait_until(lambda: not enc.stats()["thread_alive"], 2.0)
    s = enc.read()
    assert s.ok is False
    assert enc.stats()["errors"] == 1


def test_stats_keys_match_between_real_and_fake(factory):
    enc, req, clock, _ = factory()
    assert set(enc.stats()) == set(FakeWheelEncoders().stats())


# --------------------------------------------------------------------------- #
# 8. FakeWheelEncoders — the laptop / simulation stand-in.
# --------------------------------------------------------------------------- #
def test_fake_satisfies_protocol_and_starts_at_zero():
    fake = FakeWheelEncoders(monotonic=lambda: 42.0)
    assert isinstance(fake, WheelEncoders)
    s = fake.read()
    assert s == EncoderSample(0, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 42.0, True)
    assert fake.read_count == 1


def test_fake_advance_reports_tick_quantised_distances_and_heading():
    fake = FakeWheelEncoders(ticks_per_wheel_rev=1000, wheel_diameter_m=1.0 / math.pi,
                             track_width_m=0.5)
    fake.advance(1.0, 0.5)
    s = fake.read()
    assert (s.left_ticks, s.right_ticks) == (1000, 500)
    assert s.left_m == pytest.approx(1.0)
    assert s.right_m == pytest.approx(0.5)
    assert s.distance_m == pytest.approx(0.75)
    assert s.heading_delta_deg == pytest.approx(math.degrees((0.5 - 1.0) / 0.5))
    # Sub-tick travel is quantised to whole ticks (self-consistent like the real driver).
    fake.advance(0.0004, 0.0)
    s = fake.read()
    assert s.left_ticks == 1000
    assert s.left_m == pytest.approx(ticks_to_meters(1000, 1000, 1.0 / math.pi))


def test_fake_default_geometry_matches_the_real_driver():
    fake = FakeWheelEncoders()
    fake.advance(math.pi * 0.065, -math.pi * 0.065)   # exactly one wheel rev each way
    s = fake.read()
    assert (s.left_ticks, s.right_ticks) == (7680, -7680)


def test_fake_set_speeds_and_step_integrate():
    fake = FakeWheelEncoders(ticks_per_wheel_rev=1000, wheel_diameter_m=1.0 / math.pi)
    fake.set_speeds(0.2, -0.2)
    s = fake.read()
    assert (s.left_mps, s.right_mps) == (0.2, -0.2)
    fake.step(0.5)
    s = fake.read()
    assert s.left_m == pytest.approx(0.1)
    assert s.right_m == pytest.approx(-0.1)
    assert s.distance_m == pytest.approx(0.0)


def test_fake_reset_and_close_and_ok():
    fake = FakeWheelEncoders(ticks_per_wheel_rev=1000, wheel_diameter_m=1.0 / math.pi)
    fake.advance(0.3, 0.3)
    fake.set_speeds(0.1, 0.1)
    fake.reset()
    s = fake.read()
    assert (s.left_ticks, s.right_ticks) == (0, 0)
    assert s.left_mps == 0.1                       # reset keeps speeds, like the real one
    assert fake.reset_count == 1

    fake.set_ok(False)
    assert fake.read().ok is False
    fake.set_ok(True)
    assert fake.read().ok is True

    fake.close()
    assert fake.closed is True
    s = fake.read()
    assert s.ok is False
    assert (s.left_mps, s.right_mps) == (0.0, 0.0)
    assert fake.stats()["thread_alive"] is False


def test_fake_validates_geometry():
    with pytest.raises(ValueError):
        FakeWheelEncoders(ticks_per_wheel_rev=0)
