"""Tests for the libgpiod v2 ultrasonic driver and the backend selector.

``python3-libgpiod`` is NOT installed on the laptop/CI that runs these tests, so
everything here runs against a *fake* gpiod API (fake chip discovery + fake
edge-event streams) injected into :class:`GpiodUltrasonicSensor`. We verify the
kernel-timestamped timing core (first-rising/falling pairing, stale draining,
timeout, over-range, re-trigger floor, close idempotency), the chip discovery
(label match, env override, index/path), and the backend selection (prefer gpiod,
fall back to gpiozero with the LOUD 2x/4x Pi 5 defect warning).

Real electrical behaviour can only be confirmed on hardware; here we verify the
contract against a scripted fake.
"""

from __future__ import annotations

import logging
import types

import pytest

from yalp.reactive import hardware as hw
from yalp.reactive.hardware import (
    GpiodUltrasonicSensor,
    make_ultrasonic_sensor,
    _discover_gpiochip,
    RangeSensor,
)


# --------------------------------------------------------------------------- #
# A fake python3-libgpiod v2 module (chip discovery + line request).
# --------------------------------------------------------------------------- #
_RISING = "RISING_EDGE"
_FALLING = "FALLING_EDGE"
_V_ACTIVE = "ACTIVE"
_V_INACTIVE = "INACTIVE"


def _event(kind, timestamp_ns, line_offset=6):
    """A fake gpiod EdgeEvent (only the fields the driver reads)."""
    return types.SimpleNamespace(
        event_type=kind, timestamp_ns=timestamp_ns, line_offset=line_offset
    )


class _FakeRequest:
    """A fake ``gpiod`` LineRequest with a scripted edge-event stream.

    ``wait_steps`` is a list of ``(available: bool, events: list)`` consumed one
    per ``wait_edge_events`` call — matching the driver's call pattern (drain
    polls with timeout 0, then capture waits). ``read_edge_events`` returns the
    events of the most recent *available* wait.
    """

    def __init__(self, wait_steps=None):
        self.wait_steps = list(wait_steps or [])
        self.set_values = []          # ordered (offset, value) writes to TRIG
        self.wait_timeouts = []       # every timeout wait_edge_events saw
        self.released = 0             # number of release() calls
        self._pending = []

    def set_value(self, offset, value):
        self.set_values.append((offset, value))

    def wait_edge_events(self, timeout):
        self.wait_timeouts.append(timeout)
        if not self.wait_steps:
            self._pending = []
            return False
        available, events = self.wait_steps.pop(0)
        self._pending = list(events) if available else []
        return available

    def read_edge_events(self):
        evs = self._pending
        self._pending = []
        return evs

    def release(self):
        self.released += 1


class _FakeChip:
    def __init__(self, path, label):
        self._path = path
        self._label = label
        self.closed = False

    def get_info(self):
        return types.SimpleNamespace(label=self._label)

    def close(self):
        self.closed = True


def _make_fake_gpiod(request, chip_labels=None):
    """Build a fake ``gpiod`` v2 module.

    ``request`` is returned by ``request_lines``. ``chip_labels`` maps a device
    path -> kernel label for the fake ``Chip`` (discovery).
    """
    chip_labels = chip_labels or {"/dev/gpiochip0": "pinctrl-rp1"}
    mod = types.ModuleType("gpiod")

    mod.EdgeEvent = types.SimpleNamespace(
        Type=types.SimpleNamespace(RISING_EDGE=_RISING, FALLING_EDGE=_FALLING)
    )

    line = types.ModuleType("gpiod.line")
    line.Value = types.SimpleNamespace(ACTIVE=_V_ACTIVE, INACTIVE=_V_INACTIVE)
    line.Direction = types.SimpleNamespace(OUTPUT="OUTPUT", INPUT="INPUT")
    line.Edge = types.SimpleNamespace(BOTH="BOTH")
    line.Clock = types.SimpleNamespace(MONOTONIC="MONOTONIC")
    mod.line = line

    captured = {}

    class _LineSettings:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    mod.LineSettings = _LineSettings

    def request_lines(path, consumer=None, config=None):
        captured["path"] = path
        captured["consumer"] = consumer
        captured["config"] = config
        return request

    mod.request_lines = request_lines
    mod.request_calls = captured
    mod.Chip = lambda path: _FakeChip(path, chip_labels.get(path, "unknown"))
    # v2 signature markers used by _gpiod_v2_available().
    mod.__version__ = "2.1.0"
    return mod


def _make_sensor(request, clock, *, chip_labels=None, chip_paths=None,
                 min_retrigger_s=0.0, **kwargs):
    """Construct a GpiodUltrasonicSensor wired to the fake gpiod + injected clock.

    Defaults disable the poll cap and grace so ``read_distance`` exercises a fresh
    ``_sample`` each call and misses surface directly (isolating the timing core).
    """
    gpiod = _make_fake_gpiod(request, chip_labels=chip_labels)
    advanced = {"slept": []}

    def _sleep(secs):
        advanced["slept"].append(secs)
        clock["now"] += secs

    sensor = GpiodUltrasonicSensor(
        trig_pin=kwargs.pop("trig_pin", 5),
        echo_pin=kwargs.pop("echo_pin", 6),
        max_poll_hz=kwargs.pop("max_poll_hz", 0.0),
        grace_max_misses=kwargs.pop("grace_max_misses", 0),
        max_distance_m=kwargs.pop("max_distance_m", 4.0),
        min_retrigger_s=min_retrigger_s,
        monotonic=lambda: clock["now"],
        sleep=_sleep,
        _gpiod=gpiod,
        _chip_paths=chip_paths if chip_paths is not None else ["/dev/gpiochip0"],
        **kwargs,
    )
    sensor._fake_gpiod = gpiod
    sensor._slept = advanced["slept"]
    return sensor


# --------------------------------------------------------------------------- #
# Module imports with no gpiod present + protocol surface.
# --------------------------------------------------------------------------- #
def test_class_satisfies_rangesensor_protocol_structurally():
    assert callable(getattr(GpiodUltrasonicSensor, "read_distance", None))
    assert callable(getattr(GpiodUltrasonicSensor, "close", None))


def test_construction_configures_both_lines_on_matched_chip():
    clock = {"now": 100.0}
    req = _FakeRequest()
    sensor = _make_sensor(req, clock)

    assert isinstance(sensor, RangeSensor)
    cap = sensor._fake_gpiod.request_calls
    assert cap["path"] == "/dev/gpiochip0"
    # Both TRIG (5) and ECHO (6) are owned by the one request.
    assert set(cap["config"].keys()) == {5, 6}
    trig_cfg = cap["config"][5].kwargs
    echo_cfg = cap["config"][6].kwargs
    assert trig_cfg["direction"] == "OUTPUT"
    assert trig_cfg["output_value"] == _V_INACTIVE
    assert echo_cfg["direction"] == "INPUT"
    assert echo_cfg["edge_detection"] == "BOTH"
    assert echo_cfg["event_clock"] == "MONOTONIC"
    sensor.close()


# --------------------------------------------------------------------------- #
# Timing core: first-rising / falling pairing -> true distance.
# --------------------------------------------------------------------------- #
def _ns_for_distance(distance_m, speed=343.0):
    """Round-trip echo width (ns) that decodes to ``distance_m``."""
    return int(round((2.0 * distance_m / speed) * 1e9))


def test_first_rising_paired_with_falling_gives_true_distance():
    clock = {"now": 100.0}
    req = _FakeRequest()
    sensor = _make_sensor(req, clock)

    width = _ns_for_distance(0.30)
    req.wait_steps = [
        (False, []),  # drain: nothing pending
        (True, [_event(_RISING, 1_000_000), _event(_FALLING, 1_000_000 + width)]),
    ]
    d, known = sensor.read_distance()
    assert known is True
    assert d == pytest.approx(0.30, abs=1e-3)
    # The TRIG line was pulsed ACTIVE then INACTIVE.
    assert req.set_values == [(5, _V_ACTIVE), (5, _V_INACTIVE)]
    sensor.close()


def test_spurious_later_rising_is_ignored():
    """A second rising edge before the falling must NOT change the pairing."""
    clock = {"now": 100.0}
    req = _FakeRequest()
    sensor = _make_sensor(req, clock)

    width = _ns_for_distance(0.30)
    t_rise1 = 1_000_000
    req.wait_steps = [
        (False, []),
        (True, [
            _event(_RISING, t_rise1),
            _event(_RISING, t_rise1 + 500_000),        # spurious later rising
            _event(_FALLING, t_rise1 + width),          # pairs with the FIRST
        ]),
    ]
    d, known = sensor.read_distance()
    assert known is True
    # Distance is measured from the FIRST rising, not the spurious one.
    assert d == pytest.approx(0.30, abs=1e-3)
    sensor.close()


def test_stale_events_are_drained_before_triggering():
    """Pending edges from a previous ping are drained and cannot corrupt a read."""
    clock = {"now": 100.0}
    req = _FakeRequest()
    sensor = _make_sensor(req, clock)

    width = _ns_for_distance(0.84)
    req.wait_steps = [
        (True, [_event(_FALLING, 5)]),          # stale, drained
        (True, [_event(_RISING, 10)]),          # stale, drained
        (False, []),                             # drain complete
        (True, [_event(_RISING, 2_000_000),
                _event(_FALLING, 2_000_000 + width)]),
    ]
    d, known = sensor.read_distance()
    assert known is True
    assert d == pytest.approx(0.84, abs=2e-3)
    # The drain issued at least one poll with timeout 0 before the capture.
    assert 0 in req.wait_timeouts
    sensor.close()


def test_timeout_no_edges_returns_max_unknown():
    clock = {"now": 100.0}
    req = _FakeRequest()
    sensor = _make_sensor(req, clock)

    req.wait_steps = [(False, []), (False, [])]  # drain empty, capture times out
    d, known = sensor.read_distance()
    assert known is False
    assert d == pytest.approx(4.0)  # max_distance placeholder, ignored by caller
    sensor.close()


def test_rising_without_falling_returns_max_unknown():
    clock = {"now": 100.0}
    req = _FakeRequest()
    sensor = _make_sensor(req, clock)

    req.wait_steps = [
        (False, []),
        (True, [_event(_RISING, 1_000_000)]),  # rising, but the falling never comes
        (False, []),                            # then timeout
    ]
    d, known = sensor.read_distance()
    assert known is False
    assert d == pytest.approx(4.0)
    sensor.close()


def test_over_range_returns_max_unknown_never_clamped():
    clock = {"now": 100.0}
    req = _FakeRequest()
    sensor = _make_sensor(req, clock, max_distance_m=4.0)

    width = _ns_for_distance(9.0)  # ~9 m -> well past the 4 m ceiling
    req.wait_steps = [
        (False, []),
        (True, [_event(_RISING, 1_000_000),
                _event(_FALLING, 1_000_000 + width)]),
    ]
    d, known = sensor.read_distance()
    assert known is False           # over range -> unknown, never a valid reading
    assert d == pytest.approx(4.0)  # exactly the ceiling placeholder
    sensor.close()


# --------------------------------------------------------------------------- #
# Re-trigger spacing floor (reverb settling), independent of the poll cap.
# --------------------------------------------------------------------------- #
def test_retrigger_floor_sleeps_to_enforce_min_spacing():
    clock = {"now": 100.0}
    req = _FakeRequest()
    # No poll cap, but a 60 ms re-trigger floor must still be enforced.
    sensor = _make_sensor(req, clock, min_retrigger_s=0.060)

    width = _ns_for_distance(0.30)

    def _ping():
        req.wait_steps = [
            (False, []),
            (True, [_event(_RISING, 1_000_000),
                    _event(_FALLING, 1_000_000 + width)]),
        ]
        return sensor.read_distance()

    _ping()                       # first ping: no prior trigger -> no floor sleep
    assert sensor._slept == []
    _ping()                       # immediate second ping -> must sleep ~60 ms
    assert len(sensor._slept) == 1
    assert sensor._slept[0] == pytest.approx(0.060, abs=1e-6)
    sensor.close()


def test_retrigger_floor_not_enforced_when_enough_time_passed():
    clock = {"now": 100.0}
    req = _FakeRequest()
    sensor = _make_sensor(req, clock, min_retrigger_s=0.060)
    width = _ns_for_distance(0.30)

    def _ping():
        req.wait_steps = [
            (False, []),
            (True, [_event(_RISING, 1_000_000),
                    _event(_FALLING, 1_000_000 + width)]),
        ]
        return sensor.read_distance()

    _ping()
    clock["now"] += 0.100  # 100 ms elapsed > 60 ms floor
    _ping()
    assert sensor._slept == []  # floor already satisfied, no sleep
    sensor.close()


# --------------------------------------------------------------------------- #
# close() releases the request and is idempotent.
# --------------------------------------------------------------------------- #
def test_close_releases_request_and_is_idempotent():
    clock = {"now": 100.0}
    req = _FakeRequest()
    sensor = _make_sensor(req, clock)

    sensor.close()
    assert req.released == 1
    sensor.close()               # idempotent: no second release
    assert req.released == 1

    # A read after close is safe and reports unknown (STOP).
    d, known = sensor.read_distance()
    assert known is False


# --------------------------------------------------------------------------- #
# stats() parity — the shared counters behave for the gpiod driver too.
# --------------------------------------------------------------------------- #
def test_stats_start_at_zero_and_count_valid_and_miss():
    clock = {"now": 100.0}
    req = _FakeRequest()
    sensor = _make_sensor(req, clock)
    assert sensor.stats() == {
        "total_reads": 0, "valid_reads": 0, "raw_misses": 0,
        "coasted_reads": 0, "unknown_served": 0,
    }

    width = _ns_for_distance(0.30)
    req.wait_steps = [
        (False, []),
        (True, [_event(_RISING, 1_000_000), _event(_FALLING, 1_000_000 + width)]),
    ]
    assert sensor.read_distance()[1] is True

    req.wait_steps = [(False, []), (False, [])]  # a real miss (grace disabled)
    assert sensor.read_distance()[1] is False

    st = sensor.stats()
    assert st["total_reads"] == 2
    assert st["valid_reads"] == 1
    assert st["raw_misses"] == 1
    assert st["unknown_served"] == 1
    assert st["coasted_reads"] == 0
    sensor.close()


def test_grace_coasts_isolated_miss_for_gpiod_driver():
    """Grace/counter parity: an isolated miss coasts the last valid reading."""
    clock = {"now": 100.0}
    req = _FakeRequest()
    sensor = _make_sensor(
        req, clock, grace_max_misses=3, grace_ms=150, max_poll_hz=0.0
    )
    width = _ns_for_distance(2.0)
    req.wait_steps = [
        (False, []),
        (True, [_event(_RISING, 1_000_000), _event(_FALLING, 1_000_000 + width)]),
    ]
    d0, k0 = sensor.read_distance()
    assert (k0, round(d0, 3)) == (True, pytest.approx(2.0, abs=2e-3))

    clock["now"] += 0.040
    req.wait_steps = [(False, []), (False, [])]  # isolated timeout
    d1, k1 = sensor.read_distance()
    assert k1 is True                 # coasted -> NO stop
    assert d1 == pytest.approx(2.0, abs=2e-3)  # re-serves the last valid distance

    st = sensor.stats()
    assert st["raw_misses"] == 1
    assert st["coasted_reads"] == 1
    assert st["unknown_served"] == 0
    sensor.close()


# --------------------------------------------------------------------------- #
# Chip discovery — never hardcodes the index; matches by label, honors override.
# --------------------------------------------------------------------------- #
def test_discover_gpiochip_matches_label_new_kernel_gpiochip0():
    gpiod = _make_fake_gpiod(_FakeRequest(), chip_labels={
        "/dev/gpiochip0": "pinctrl-rp1",
        "/dev/gpiochip1": "something-else",
    })
    path = _discover_gpiochip(
        gpiod, chip_paths=["/dev/gpiochip0", "/dev/gpiochip1"]
    )
    assert path == "/dev/gpiochip0"


def test_discover_gpiochip_matches_label_old_kernel_gpiochip4():
    gpiod = _make_fake_gpiod(_FakeRequest(), chip_labels={
        "/dev/gpiochip0": "gpio-brcmstb",
        "/dev/gpiochip4": "pinctrl-rp1",
    })
    path = _discover_gpiochip(
        gpiod, chip_paths=["/dev/gpiochip0", "/dev/gpiochip4"]
    )
    assert path == "/dev/gpiochip4"


def test_discover_gpiochip_returns_none_when_no_match():
    gpiod = _make_fake_gpiod(_FakeRequest(), chip_labels={
        "/dev/gpiochip0": "gpio-brcmstb",
    })
    assert _discover_gpiochip(gpiod, chip_paths=["/dev/gpiochip0"]) is None


def test_discover_gpiochip_override_path_and_index_and_label():
    gpiod = _make_fake_gpiod(_FakeRequest(), chip_labels={
        "/dev/gpiochip9": "custom-label",
    })
    # Full path used verbatim.
    assert _discover_gpiochip(gpiod, override="/dev/gpiochip7") == "/dev/gpiochip7"
    # Bare index -> /dev/gpiochipN.
    assert _discover_gpiochip(gpiod, override="3") == "/dev/gpiochip3"
    # Label substring -> matched against the chips.
    assert _discover_gpiochip(
        gpiod, override="custom", chip_paths=["/dev/gpiochip9"]
    ) == "/dev/gpiochip9"


def test_construction_raises_when_no_chip_matches():
    clock = {"now": 100.0}
    with pytest.raises(RuntimeError, match="no matching GPIO chip"):
        _make_sensor(
            _FakeRequest(), clock,
            chip_labels={"/dev/gpiochip0": "gpio-brcmstb"},
            chip_paths=["/dev/gpiochip0"],
        )


# --------------------------------------------------------------------------- #
# Backend selection: prefer gpiod, else gpiozero + LOUD 2x/4x defect warning.
# --------------------------------------------------------------------------- #
class _StubGpiod:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.backend = "gpiod"


class _StubGpiozero:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.backend = "gpiozero"


@pytest.fixture()
def stub_backends(monkeypatch):
    monkeypatch.setattr(hw, "GpiodUltrasonicSensor", _StubGpiod)
    monkeypatch.setattr(hw, "GpiozeroUltrasonicSensor", _StubGpiozero)
    return None


def _loud_warnings(caplog):
    return [
        r for r in caplog.records
        if r.name == "yalp.reactive.hardware" and "UNSAFE" in r.getMessage()
    ]


def test_auto_prefers_gpiod_when_v2_available(stub_backends, monkeypatch, caplog):
    monkeypatch.setattr(hw, "_gpiod_v2_available", lambda: True)
    with caplog.at_level(logging.WARNING, logger="yalp.reactive.hardware"):
        sensor = make_ultrasonic_sensor(backend="auto")
    assert sensor.backend == "gpiod"
    assert _loud_warnings(caplog) == []  # no scary warning on the safe path


def test_auto_falls_back_to_gpiozero_with_loud_warning(stub_backends, monkeypatch, caplog):
    monkeypatch.setattr(hw, "_gpiod_v2_available", lambda: False)
    with caplog.at_level(logging.WARNING, logger="yalp.reactive.hardware"):
        sensor = make_ultrasonic_sensor(backend="auto")
    assert sensor.backend == "gpiozero"
    warnings = _loud_warnings(caplog)
    assert len(warnings) == 1
    msg = warnings[0].getMessage()
    assert "2x/4x" in msg          # names the even-multiple Pi 5 defect
    assert "0.30 m" in msg         # cites the proven-on-hardware reading


def test_auto_falls_back_when_gpiod_construction_raises(stub_backends, monkeypatch, caplog):
    monkeypatch.setattr(hw, "_gpiod_v2_available", lambda: True)

    def _boom(**kwargs):
        raise RuntimeError("no chip")

    monkeypatch.setattr(hw, "GpiodUltrasonicSensor", _boom)
    with caplog.at_level(logging.WARNING, logger="yalp.reactive.hardware"):
        sensor = make_ultrasonic_sensor(backend="auto")
    assert sensor.backend == "gpiozero"
    assert len(_loud_warnings(caplog)) == 1


def test_env_override_forces_gpiod(stub_backends, monkeypatch):
    # Even if v2 looks unavailable, forcing gpiod must build the gpiod driver.
    monkeypatch.setattr(hw, "_gpiod_v2_available", lambda: False)
    sensor = make_ultrasonic_sensor(backend="gpiod")
    assert sensor.backend == "gpiod"


def test_env_override_forces_gpiozero_with_warning(stub_backends, caplog):
    with caplog.at_level(logging.WARNING, logger="yalp.reactive.hardware"):
        sensor = make_ultrasonic_sensor(backend="gpiozero")
    assert sensor.backend == "gpiozero"
    assert len(_loud_warnings(caplog)) == 1


def test_backend_default_read_from_config(stub_backends, monkeypatch):
    # backend=None -> falls back to config.ULTRASONIC_BACKEND.
    monkeypatch.setattr(hw.config, "ULTRASONIC_BACKEND", "gpiod")
    monkeypatch.setattr(hw, "_gpiod_v2_available", lambda: False)
    sensor = make_ultrasonic_sensor()
    assert sensor.backend == "gpiod"
