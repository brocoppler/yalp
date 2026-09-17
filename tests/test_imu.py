"""Tests for :mod:`yalp.reactive.imu` — the MPU-6050 driver, yaw integrator, fake.

``smbus2``/``smbus`` are NOT installed on the laptop/CI that runs these tests, so
the driver is exercised against a *fake* SMBus object injected with ``_smbus=``.
The fake records every register write and serves canned 14-byte data blocks (or
raises, to script I2C faults), letting us assert the wake/config sequence, the
range scaling, the bias calibration, the sign convention and the never-raise
contract of ``read()`` without any hardware. The lazy-import path is covered by
injecting fake ``smbus2``/``smbus`` modules into ``sys.modules``; the "importing
the module pulls in no hardware lib" guarantee runs in a fresh subprocess (see
tests/_import_isolation.py).

Real electrical behaviour — and the mounting-dependent sign — can only be
confirmed on the robot; here we verify the contract against the scripted fake.
"""

from __future__ import annotations

import logging
import sys
import types

import pytest

from tests._import_isolation import assert_import_leaves_module_unloaded
from yalp.reactive.imu import (
    ACCEL_RANGES,
    DATA_BLOCK_LEN,
    GYRO_RANGES,
    REG_ACCEL_CONFIG,
    REG_ACCEL_XOUT_H,
    REG_CONFIG,
    REG_GYRO_CONFIG,
    REG_PWR_MGMT_1,
    REG_WHO_AM_I,
    FakeImu,
    Imu,
    ImuSample,
    Mpu6050Imu,
    YawIntegrator,
)

ADDR = 0x68


# --------------------------------------------------------------------------- #
# A fake SMBus object + canned register blocks.
# --------------------------------------------------------------------------- #
def _i16(value):
    """Big-endian two's-complement bytes of a 16-bit int."""
    value &= 0xFFFF
    return [(value >> 8) & 0xFF, value & 0xFF]


def _block(ax=0, ay=0, az=16384, temp=0, gx=0, gy=0, gz=0):
    """A 14-byte accel/temp/gyro burst (raw counts), as the chip serves it."""
    out = []
    for v in (ax, ay, az, temp, gx, gy, gz):
        out += _i16(v)
    assert len(out) == DATA_BLOCK_LEN
    return out


class _FakeSmbus:
    """Fake ``smbus2.SMBus``: records writes, serves canned data blocks.

    ``blocks`` is consumed one per ``read_i2c_block_data`` call; the LAST entry
    repeats forever. An entry that is an exception instance is raised instead
    (scripts an I2C fault).
    """

    def __init__(self, *, who_am_i=0x68, blocks=None):
        self.regs = {REG_WHO_AM_I: who_am_i}
        self.writes = []            # ordered (addr, reg, value)
        self.byte_reads = []        # ordered (addr, reg)
        self.blocks = list(blocks) if blocks is not None else [_block()]
        self.block_reads = 0
        self.fail_byte_reads = False
        self.closed = False

    def write_byte_data(self, addr, reg, value):
        assert addr == ADDR
        self.writes.append((addr, reg, value))
        self.regs[reg] = value

    def read_byte_data(self, addr, reg):
        self.byte_reads.append((addr, reg))
        if self.fail_byte_reads:
            raise OSError(121, "Remote I/O error")
        return self.regs.get(reg, 0)

    def read_i2c_block_data(self, addr, reg, length):
        assert addr == ADDR
        assert reg == REG_ACCEL_XOUT_H and length == DATA_BLOCK_LEN
        self.block_reads += 1
        item = self.blocks.pop(0) if len(self.blocks) > 1 else self.blocks[0]
        if isinstance(item, Exception):
            raise item
        return list(item)

    def close(self):
        self.closed = True


def _make_imu(bus=None, **kwargs):
    """Construct an Mpu6050Imu on a fake bus with an injected clock + sleep."""
    bus = bus if bus is not None else _FakeSmbus()
    clock = {"now": 100.0}
    slept = []

    def _sleep(secs):
        slept.append(secs)
        clock["now"] += secs

    imu = Mpu6050Imu(
        monotonic=lambda: clock["now"],
        sleep=_sleep,
        _smbus=bus,
        **kwargs,
    )
    imu._test_clock = clock
    imu._test_slept = slept
    imu._test_bus = bus
    return imu


def _sample(rate, ts, ok=True):
    return ImuSample(rate, (0.0, 0.0, rate), (0.0, 0.0, 1.0), 25.0, ts, ok)


# --------------------------------------------------------------------------- #
# 1. The module must import with NO smbus binding present (lazy imports).
# --------------------------------------------------------------------------- #
def test_module_imports_without_smbus():
    """Importing imu.py must not require smbus2/smbus (checked in a fresh subprocess)."""
    assert_import_leaves_module_unloaded("yalp.reactive.imu", ("smbus2", "smbus"))

    import yalp.reactive.imu as m  # noqa: F401  (import must not raise)

    assert hasattr(m, "Mpu6050Imu")
    assert hasattr(m, "FakeImu")


# --------------------------------------------------------------------------- #
# 2. Construction: WHO_AM_I read, wake, DLPF, ranges — in that order.
# --------------------------------------------------------------------------- #
def test_init_wakes_and_configures_defaults():
    bus = _FakeSmbus()
    imu = _make_imu(bus)

    assert isinstance(imu, Imu)
    assert (ADDR, REG_WHO_AM_I) in bus.byte_reads
    assert imu.who_am_i == 0x68
    # Wake FIRST (PWR_MGMT_1 = 0 clears SLEEP), then DLPF ~44 Hz, then the
    # ±250 °/s and ±2 g ranges (FS_SEL/AFS_SEL = 0 in bits [4:3]).
    assert bus.writes == [
        (ADDR, REG_PWR_MGMT_1, 0x00),
        (ADDR, REG_CONFIG, 0x03),
        (ADDR, REG_GYRO_CONFIG, 0x00),
        (ADDR, REG_ACCEL_CONFIG, 0x00),
    ]
    # A settle sleep follows the wake (gyro start-up ~30 ms).
    assert imu._test_slept == [pytest.approx(0.05)]
    assert imu.gyro_bias_dps == (0.0, 0.0, 0.0)


def test_init_custom_address_and_no_settle():
    class _Bus(_FakeSmbus):
        def write_byte_data(self, addr, reg, value):
            assert addr == 0x69
            self.writes.append((addr, reg, value))

        def read_byte_data(self, addr, reg):
            assert addr == 0x69
            return 0x68

    bus = _Bus()
    imu = _make_imu(bus, address=0x69, wake_settle_s=0.0)
    assert imu.address == 0x69
    assert imu._test_slept == []
    assert bus.writes[0] == (0x69, REG_PWR_MGMT_1, 0x00)


@pytest.mark.parametrize(
    "rng,fs_sel,lsb",
    [(250, 0, 131.0), (500, 1, 65.5), (1000, 2, 32.8), (2000, 3, 16.4)],
)
def test_gyro_range_sets_fs_sel_and_scales(rng, fs_sel, lsb):
    bus = _FakeSmbus(blocks=[_block(gz=1310)])
    imu = _make_imu(bus, gyro_range_dps=rng)
    assert (ADDR, REG_GYRO_CONFIG, fs_sel << 3) in bus.writes
    assert GYRO_RANGES[rng] == (fs_sel, lsb)
    s = imu.read()
    assert s.ok is True
    assert s.gyro_dps[2] == pytest.approx(1310 / lsb)
    assert imu.gyro_range_dps == rng


@pytest.mark.parametrize(
    "rng,afs_sel,lsb",
    [(2, 0, 16384.0), (4, 1, 8192.0), (8, 2, 4096.0), (16, 3, 2048.0)],
)
def test_accel_range_sets_afs_sel_and_scales(rng, afs_sel, lsb):
    bus = _FakeSmbus(blocks=[_block(az=4096)])
    imu = _make_imu(bus, accel_range_g=rng)
    assert (ADDR, REG_ACCEL_CONFIG, afs_sel << 3) in bus.writes
    assert ACCEL_RANGES[rng] == (afs_sel, lsb)
    s = imu.read()
    assert s.accel_g[2] == pytest.approx(4096 / lsb)
    assert imu.accel_range_g == rng


@pytest.mark.parametrize(
    "kwargs",
    [
        {"gyro_range_dps": 300},
        {"accel_range_g": 3},
        {"yaw_axis": "q"},
        {"yaw_sign": 0},
        {"yaw_sign": 2},
        {"dlpf_cfg": 9},
    ],
)
def test_invalid_construction_args_raise_value_error(kwargs):
    with pytest.raises(ValueError):
        _make_imu(_FakeSmbus(), **kwargs)


@pytest.mark.parametrize(
    "who,warns",
    [(0x68, False), (0x69, False), (0x70, False), (0x71, False), (0x00, True), (0x72, True)],
)
def test_who_am_i_mismatch_warns_but_continues(who, warns, caplog):
    bus = _FakeSmbus(who_am_i=who)
    with caplog.at_level(logging.WARNING, logger="yalp.reactive.imu"):
        imu = _make_imu(bus)
    msgs = [r.getMessage() for r in caplog.records if "WHO_AM_I" in r.getMessage()]
    assert bool(msgs) is warns
    # Construction completed either way: the chip was still configured.
    assert (ADDR, REG_PWR_MGMT_1, 0x00) in bus.writes
    assert imu.who_am_i == who
    assert imu.read().ok is True


def test_init_i2c_failure_raises_runtime_error_and_closes_bus():
    bus = _FakeSmbus()
    bus.fail_byte_reads = True
    with pytest.raises(RuntimeError, match="did not respond"):
        _make_imu(bus)
    assert bus.closed is True


# --------------------------------------------------------------------------- #
# 3. read(): scaling of every field, two's complement, timestamps.
# --------------------------------------------------------------------------- #
def test_read_scales_accel_temp_and_gyro():
    bus = _FakeSmbus(blocks=[_block(ax=16384, ay=-8192, az=0, temp=340, gx=131, gy=-262, gz=655)])
    imu = _make_imu(bus)
    imu._test_clock["now"] = 123.5
    s = imu.read()
    assert s.ok is True
    assert s.ts == 123.5
    assert s.accel_g == pytest.approx((1.0, -0.5, 0.0))
    assert s.temp_c == pytest.approx(340 / 340.0 + 36.53)
    assert s.gyro_dps == pytest.approx((1.0, -2.0, 5.0))
    assert s.yaw_rate_dps == pytest.approx(5.0)  # z axis, +1 sign by default


def test_read_full_scale_negative_two_complement():
    bus = _FakeSmbus(blocks=[_block(gz=-32768, az=-32768)])
    imu = _make_imu(bus)
    s = imu.read()
    assert s.gyro_dps[2] == pytest.approx(-32768 / 131.0)
    assert s.accel_g[2] == pytest.approx(-2.0)


# --------------------------------------------------------------------------- #
# 4. Sign convention: positive yaw = LEFT / CCW from above; yaw_sign flips it;
#    yaw_axis picks the vertical axis.
# --------------------------------------------------------------------------- #
def test_positive_z_rate_is_positive_yaw_by_default():
    imu = _make_imu(_FakeSmbus(blocks=[_block(gz=131)]))
    assert imu.read().yaw_rate_dps == pytest.approx(+1.0)


def test_yaw_sign_flips_the_convention_for_chip_down_mounting():
    imu = _make_imu(_FakeSmbus(blocks=[_block(gz=131)]), yaw_sign=-1)
    assert imu.read().yaw_rate_dps == pytest.approx(-1.0)
    # The per-axis gyro tuple is NOT flipped — only the derived yaw rate is.
    assert imu.read().gyro_dps[2] == pytest.approx(+1.0)


@pytest.mark.parametrize(
    "axis,sign,block,expected",
    [
        ("x", +1, _block(gx=262, gy=0, gz=0), +2.0),
        ("y", -1, _block(gx=0, gy=-131, gz=0), +1.0),
        ("Z", +1, _block(gx=131, gy=131, gz=-393), -3.0),
    ],
)
def test_yaw_axis_selects_the_vertical_axis(axis, sign, block, expected):
    imu = _make_imu(_FakeSmbus(blocks=[block]), yaw_axis=axis, yaw_sign=sign)
    assert imu.read().yaw_rate_dps == pytest.approx(expected)


# --------------------------------------------------------------------------- #
# 5. Bias calibration: averages at-rest reads, skips bad ones, then corrects.
# --------------------------------------------------------------------------- #
def test_calibrate_gyro_averages_bias_and_skips_bad_reads(caplog):
    rest = _block(gx=13, gy=-26, gz=131)  # ~0.099, -0.198, 1.0 °/s at rest
    blocks = [rest, rest, rest, OSError(121, "Remote I/O error"), rest, rest,
              OSError(121, "Remote I/O error"), rest, rest, rest]
    bus = _FakeSmbus(blocks=blocks + [rest])
    imu = _make_imu(bus)
    imu._test_slept.clear()
    cal_sleeps = []

    with caplog.at_level(logging.WARNING, logger="yalp.reactive.imu"):
        bias = imu.calibrate_gyro(samples=10, interval_s=0.005, sleep=cal_sleeps.append)

    assert bias == pytest.approx((13 / 131.0, -26 / 131.0, 1.0))
    assert imu.gyro_bias_dps == bias
    # The explicit sleep= override was used (n-1 intervals), not the ctor sleep.
    assert cal_sleeps == [pytest.approx(0.005)] * 9
    assert imu._test_slept == []
    st = imu.stats()
    assert st["calibrations"] == 1
    assert st["calibration_bad_reads"] == 2
    assert any("bad reads" in r.getMessage() for r in caplog.records)

    # Subsequent reads are bias-corrected: the same at-rest block now reads ~0.
    s = imu.read()
    assert s.ok is True
    assert s.yaw_rate_dps == pytest.approx(0.0, abs=1e-9)
    assert s.gyro_dps == pytest.approx((0.0, 0.0, 0.0), abs=1e-9)


def test_calibrate_gyro_uses_ctor_sleep_by_default():
    imu = _make_imu(_FakeSmbus(blocks=[_block(gz=131)]))
    imu._test_slept.clear()
    imu.calibrate_gyro(samples=4, interval_s=0.01)
    assert imu._test_slept == [pytest.approx(0.01)] * 3


def test_calibrate_gyro_raises_when_too_few_good_reads_and_keeps_old_bias():
    err = OSError(121, "Remote I/O error")
    bus = _FakeSmbus(blocks=[err, err, err, _block(gz=131), _block(gz=131)])
    imu = _make_imu(bus)
    imu.set_gyro_bias((0.0, 0.0, 0.25))
    with pytest.raises(RuntimeError, match="calibration failed"):
        imu.calibrate_gyro(samples=4, interval_s=0.0)
    assert imu.gyro_bias_dps == (0.0, 0.0, 0.25)  # untouched


def test_calibrate_gyro_warns_when_robot_was_moving(caplog):
    blocks = [_block(gz=0), _block(gz=131 * 30)] * 5
    imu = _make_imu(_FakeSmbus(blocks=blocks + [_block()]))
    with caplog.at_level(logging.WARNING, logger="yalp.reactive.imu"):
        imu.calibrate_gyro(samples=10, interval_s=0.0)
    assert any("moving" in r.getMessage() for r in caplog.records)


def test_calibrate_gyro_rejects_bad_sample_count_and_closed():
    imu = _make_imu(_FakeSmbus())
    with pytest.raises(ValueError):
        imu.calibrate_gyro(samples=0)
    imu.close()
    with pytest.raises(RuntimeError):
        imu.calibrate_gyro(samples=4, interval_s=0.0)


def test_set_gyro_bias_installs_stored_bias():
    imu = _make_imu(_FakeSmbus(blocks=[_block(gz=131)]))
    imu.set_gyro_bias([0.0, 0.0, 0.5])
    assert imu.read().yaw_rate_dps == pytest.approx(0.5)
    with pytest.raises(ValueError):
        imu.set_gyro_bias((1.0, 2.0))


# --------------------------------------------------------------------------- #
# 6. read() never raises: bus errors / short reads / closed -> ok=False.
# --------------------------------------------------------------------------- #
def test_read_returns_ok_false_on_bus_error_then_recovers():
    bus = _FakeSmbus(blocks=[OSError(121, "Remote I/O error"), _block(gz=131)])
    imu = _make_imu(bus)
    imu._test_clock["now"] = 7.0

    s = imu.read()  # the fault
    assert s.ok is False
    assert s.yaw_rate_dps == 0.0
    assert s.gyro_dps == (0.0, 0.0, 0.0)
    assert s.accel_g == (0.0, 0.0, 0.0)
    assert s.ts == 7.0
    assert imu.stats()["read_errors"] == 1

    s2 = imu.read()  # recovers on the next read
    assert s2.ok is True
    assert s2.yaw_rate_dps == pytest.approx(1.0)
    assert imu.stats()["reads"] == 2


def test_read_returns_ok_false_on_short_block():
    imu = _make_imu(_FakeSmbus(blocks=[[0, 0, 0, 0, 0]]))
    assert imu.read().ok is False


def test_close_is_idempotent_and_reads_after_close_are_not_ok():
    bus = _FakeSmbus()
    imu = _make_imu(bus)
    imu.close()
    assert bus.closed is True
    imu.close()  # idempotent
    s = imu.read()
    assert s.ok is False
    assert s.yaw_rate_dps == 0.0


# --------------------------------------------------------------------------- #
# 7. YawIntegrator: pure, timestamp-driven, dropout-safe.
# --------------------------------------------------------------------------- #
def test_yaw_integrator_accumulates_from_sample_timestamps_and_resets():
    yi = YawIntegrator()
    assert yi.update(_sample(10.0, 0.0)) == 0.0  # first sample only anchors time
    assert yi.update(_sample(10.0, 0.1)) == pytest.approx(1.0)
    assert yi.update(_sample(10.0, 0.2)) == pytest.approx(2.0)
    assert yi.update(_sample(-20.0, 0.3)) == pytest.approx(0.0)
    assert yi.heading_deg == pytest.approx(0.0)

    yi.update(_sample(90.0, 0.5))  # 0.2 s * 90 = 18°
    assert yi.heading_deg == pytest.approx(18.0)
    yi.reset()
    assert yi.heading_deg == 0.0
    # After a reset the next sample re-anchors and does not integrate a stale gap.
    assert yi.update(_sample(90.0, 5.0)) == 0.0
    yi.reset(heading_deg=45.0)
    assert yi.heading_deg == 45.0


def test_yaw_integrator_ignores_not_ok_samples_and_bridges_the_gap():
    yi = YawIntegrator(max_dt_s=0.25)
    yi.update(_sample(10.0, 0.0))
    assert yi.update(_sample(999.0, 0.05, ok=False)) == 0.0  # ignored entirely
    assert yi.dropped == 1
    # The next good sample covers the whole 0.1 s at ITS rate (10 °/s).
    assert yi.update(_sample(10.0, 0.1)) == pytest.approx(1.0)


def test_yaw_integrator_resyncs_instead_of_integrating_a_long_gap():
    yi = YawIntegrator(max_dt_s=0.25)
    yi.update(_sample(10.0, 0.0))
    yi.update(_sample(10.0, 0.1))
    assert yi.heading_deg == pytest.approx(1.0)
    # A 2 s stall: nothing integrated, gap counted, clock resynced.
    assert yi.update(_sample(10.0, 2.1)) == pytest.approx(1.0)
    assert yi.gaps == 1
    assert yi.update(_sample(10.0, 2.2)) == pytest.approx(2.0)


def test_yaw_integrator_deadband_and_non_monotonic_timestamps():
    # max_dt_s raised so the 1 s steps below are integrated, not treated as gaps.
    yi = YawIntegrator(deadband_dps=0.5, max_dt_s=5.0)
    yi.update(_sample(0.3, 0.0))
    yi.update(_sample(0.3, 1.0))
    assert yi.heading_deg == 0.0  # below the deadband -> treated as at rest
    yi.update(_sample(1.0, 2.0))
    assert yi.heading_deg == pytest.approx(1.0)
    yi.update(_sample(1.0, 1.5))  # time went backwards: ignored
    assert yi.heading_deg == pytest.approx(1.0)


@pytest.mark.parametrize(
    "heading,wrapped",
    [(0.0, 0.0), (190.0, -170.0), (-190.0, 170.0), (180.0, 180.0), (-180.0, 180.0), (720.0, 0.0)],
)
def test_yaw_integrator_wrapped_heading(heading, wrapped):
    yi = YawIntegrator(heading_deg=heading)
    assert yi.heading_wrapped_deg == pytest.approx(wrapped)


# --------------------------------------------------------------------------- #
# 8. FakeImu: scripted rates, faults, protocol surface, feeds the integrator.
# --------------------------------------------------------------------------- #
def test_fake_imu_scripted_list_serves_in_order_and_repeats_last():
    clock = {"now": 1.0}
    imu = FakeImu([1.0, 2.0, 3.0], monotonic=lambda: clock["now"])
    assert isinstance(imu, Imu)
    rates = []
    for _ in range(5):
        s = imu.read()
        assert s.ok is True
        assert s.ts == 1.0
        assert s.gyro_dps == (0.0, 0.0, s.yaw_rate_dps)
        assert s.accel_g == (0.0, 0.0, 1.0)
        rates.append(s.yaw_rate_dps)
    assert rates == [1.0, 2.0, 3.0, 3.0, 3.0]
    assert imu.read_count == 5
    assert imu.last is not None and imu.last.yaw_rate_dps == 3.0


def test_fake_imu_constant_callable_and_setter():
    clock = {"now": 1.5}
    imu = FakeImu(lambda ts: ts * 2.0, monotonic=lambda: clock["now"])
    assert imu.read().yaw_rate_dps == pytest.approx(3.0)
    clock["now"] = 2.0
    assert imu.read().yaw_rate_dps == pytest.approx(4.0)
    imu.set_yaw_rate(7.0)
    assert imu.read().yaw_rate_dps == 7.0
    assert FakeImu(12.0).read().yaw_rate_dps == 12.0
    assert FakeImu().read().yaw_rate_dps == 0.0


def test_fake_imu_fault_calibrate_and_close():
    imu = FakeImu(5.0, bias_dps=(0.1, 0.2, 0.3))
    imu.set_ok(False)
    s = imu.read()
    assert s.ok is False and s.yaw_rate_dps == 0.0
    imu.set_ok(True)
    assert imu.read().ok is True

    assert imu.calibrated is False
    assert imu.calibrate_gyro() == (0.1, 0.2, 0.3)
    assert imu.calibrated is True
    assert imu.calibrate_calls == 1
    assert imu.gyro_bias_dps == (0.1, 0.2, 0.3)
    assert imu.stats()["calibrations"] == 1

    imu.close()
    assert imu.closed is True
    assert imu.read().ok is False


def test_fake_imu_feeds_yaw_integrator():
    clock = {"now": 0.0}
    imu = FakeImu(30.0, monotonic=lambda: clock["now"])
    yi = YawIntegrator()
    for _ in range(11):  # 10 x 0.1 s at 30 °/s = 30°
        yi.update(imu.read())
        clock["now"] += 0.1
    assert yi.heading_deg == pytest.approx(30.0)


# --------------------------------------------------------------------------- #
# 9. Lazy smbus import: prefer smbus2, fall back to smbus, else fail loudly.
# --------------------------------------------------------------------------- #
def _fake_smbus_module(name, bus_obj):
    mod = types.ModuleType(name)
    calls = []

    def SMBus(bus):
        calls.append(bus)
        return bus_obj

    mod.SMBus = SMBus
    mod.calls = calls
    return mod


def test_construct_without_any_smbus_binding_fails_loudly(monkeypatch):
    monkeypatch.setitem(sys.modules, "smbus2", None)
    monkeypatch.setitem(sys.modules, "smbus", None)
    with pytest.raises(RuntimeError, match="smbus2"):
        Mpu6050Imu(sleep=lambda s: None)


def test_lazy_import_prefers_smbus2(monkeypatch):
    mod = _fake_smbus_module("smbus2", _FakeSmbus(blocks=[_block(gz=131)]))
    monkeypatch.setitem(sys.modules, "smbus2", mod)
    monkeypatch.setitem(sys.modules, "smbus", None)
    imu = Mpu6050Imu(bus=1, sleep=lambda s: None)
    assert mod.calls == [1]
    assert imu.read().yaw_rate_dps == pytest.approx(1.0)


def test_lazy_import_falls_back_to_smbus(monkeypatch):
    monkeypatch.setitem(sys.modules, "smbus2", None)
    mod = _fake_smbus_module("smbus", _FakeSmbus())
    monkeypatch.setitem(sys.modules, "smbus", mod)
    imu = Mpu6050Imu(bus=3, sleep=lambda s: None)
    assert mod.calls == [3]
    assert imu.read().ok is True
