"""Tests for :mod:`yalp.reactive.power_monitor` — the INA219 driver, fake, gauge.

``smbus2``/``smbus`` are NOT installed on the laptop/CI that runs these tests, so
the driver is exercised against a *fake* SMBus object injected with ``_smbus=``.
The fake records every 16-bit register write and serves canned register words
(big-endian, exactly as the INA219 does), letting us assert the CONFIG and
CALIBRATION values, the voltage/current decoding (including two's-complement
negative current) and the never-raise contract of ``read()`` with no hardware.
The lazy-import path is covered by injecting fake ``smbus2``/``smbus`` modules
into ``sys.modules``; the "importing pulls in no hardware lib" guarantee runs in
a fresh subprocess (see tests/_import_isolation.py).
"""

from __future__ import annotations

import logging
import sys
import types

import pytest

from tests._import_isolation import assert_import_leaves_module_unloaded
from yalp.reactive.power_monitor import (
    INA219_CONFIG_DEFAULT,
    REG_BUS_VOLTAGE,
    REG_CALIBRATION,
    REG_CONFIG,
    REG_CURRENT,
    REG_POWER,
    REG_SHUNT_VOLTAGE,
    FakePowerMonitor,
    Ina219PowerMonitor,
    PowerMonitor,
    ina219_calibration,
    nimh_pack_state,
)

ADDR = 0x40


# --------------------------------------------------------------------------- #
# A fake SMBus object serving big-endian 16-bit registers.
# --------------------------------------------------------------------------- #
class _FakeSmbus:
    def __init__(self, regs=None):
        self.regs = dict(regs or {})   # reg -> unsigned 16-bit word
        self.writes = []               # ordered (addr, reg, value_u16)
        self.reads = []                # ordered reg
        self.fail_reads = False
        self.fail_writes = False
        self.closed = False

    def write_i2c_block_data(self, addr, reg, data):
        assert addr == ADDR
        if self.fail_writes:
            raise OSError(121, "Remote I/O error")
        assert len(data) == 2
        value = ((data[0] & 0xFF) << 8) | (data[1] & 0xFF)
        self.writes.append((addr, reg, value))
        self.regs[reg] = value

    def read_i2c_block_data(self, addr, reg, length):
        assert addr == ADDR and length == 2
        self.reads.append(reg)
        if self.fail_reads:
            raise OSError(121, "Remote I/O error")
        v = self.regs.get(reg, 0) & 0xFFFF
        return [(v >> 8) & 0xFF, v & 0xFF]

    def close(self):
        self.closed = True


def _regs(bus_v=5.5, shunt_mv=30.0, current_raw=3000, power_raw=825, ovf=False):
    """Canned data registers: bus 4 mV/LSB << 3, shunt 10 µV/LSB, raw current/power."""
    bus_raw = (int(round(bus_v / 0.004)) << 3) | (1 if ovf else 0)
    shunt_raw = int(round(shunt_mv / 0.01)) & 0xFFFF
    return {
        REG_BUS_VOLTAGE: bus_raw,
        REG_SHUNT_VOLTAGE: shunt_raw,
        REG_CURRENT: current_raw & 0xFFFF,
        REG_POWER: power_raw & 0xFFFF,
    }


def _make_monitor(bus=None, **kwargs):
    bus = bus if bus is not None else _FakeSmbus(_regs())
    clock = {"now": 100.0}
    mon = Ina219PowerMonitor(monotonic=lambda: clock["now"], _smbus=bus, **kwargs)
    mon._test_clock = clock
    mon._test_bus = bus
    return mon


# --------------------------------------------------------------------------- #
# 1. The module must import with NO smbus binding present (lazy imports).
# --------------------------------------------------------------------------- #
def test_module_imports_without_smbus():
    assert_import_leaves_module_unloaded("yalp.reactive.power_monitor", ("smbus2", "smbus"))

    import yalp.reactive.power_monitor as m  # noqa: F401

    assert hasattr(m, "Ina219PowerMonitor")
    assert hasattr(m, "FakePowerMonitor")


# --------------------------------------------------------------------------- #
# 2. Construction writes CONFIG (32 V, /8, 12-bit, continuous) + CALIBRATION.
# --------------------------------------------------------------------------- #
def test_init_writes_default_config_and_calibration():
    bus = _FakeSmbus(_regs())
    mon = _make_monitor(bus)

    assert isinstance(mon, PowerMonitor)
    assert INA219_CONFIG_DEFAULT == 0x399F
    assert bus.writes == [
        (ADDR, REG_CONFIG, 0x399F),   # BRNG=32V | PG=/8 | BADC=12b | SADC=12b | MODE=7
        (ADDR, REG_CALIBRATION, 4096),  # 0.04096 / (100 µA * 0.1 Ω)
    ]
    assert mon.calibration.cal_register == 4096
    assert mon.calibration.current_lsb_a == pytest.approx(1e-4)
    assert mon.calibration.power_lsb_w == pytest.approx(2e-3)
    assert mon.calibration.max_current_a == pytest.approx(3.2767)
    assert mon.address == ADDR
    assert mon.shunt_ohms == 0.1
    assert mon.config_register == 0x399F


def test_init_custom_config_and_address():
    class _Bus(_FakeSmbus):
        def write_i2c_block_data(self, addr, reg, data):
            assert addr == 0x41
            self.writes.append((addr, reg, (data[0] << 8) | data[1]))

    bus = _Bus()
    mon = Ina219PowerMonitor(address=0x41, config=0x019F, _smbus=bus)
    assert bus.writes[0] == (0x41, REG_CONFIG, 0x019F)
    assert mon.address == 0x41


@pytest.mark.parametrize(
    "shunt,amps,cal,lsb",
    [
        (0.1, 3.2, 4096, 1e-4),     # the defaults (Adafruit "32V_3.2A" pattern)
        (0.1, 2.0, 4096, 1e-4),     # Adafruit "32V_2A": same round LSB
        (0.1, 1.0, 8192, 5e-5),
        (0.01, 10.0, 8192, 5e-4),
        (0.1, 0.05, 40960, 1e-5),   # tiny current: LSB bumped until CAL fits 16 bits
    ],
)
def test_ina219_calibration_per_datasheet(shunt, amps, cal, lsb):
    c = ina219_calibration(shunt, amps)
    assert c.cal_register == cal
    assert c.cal_register <= 0xFFFE and c.cal_register % 2 == 0
    assert c.current_lsb_a == pytest.approx(lsb)
    assert c.power_lsb_w == pytest.approx(20 * lsb)
    # The chosen LSB always represents the full expected current.
    assert c.max_current_a >= amps


@pytest.mark.parametrize("shunt,amps", [(0.0, 1.0), (-0.1, 1.0), (0.1, 0.0), (0.1, -1.0)])
def test_ina219_calibration_rejects_bad_inputs(shunt, amps):
    with pytest.raises(ValueError):
        ina219_calibration(shunt, amps)


def test_warns_when_expected_current_exceeds_shunt_full_scale(caplog):
    with caplog.at_level(logging.WARNING, logger="yalp.reactive.power_monitor"):
        _make_monitor(_FakeSmbus(_regs()), shunt_ohms=0.1, max_expected_amps=5.0)
    msgs = [r.getMessage() for r in caplog.records]
    assert any("full scale" in m and "3.20 A" in m for m in msgs)


def test_no_warning_at_exactly_full_scale(caplog):
    with caplog.at_level(logging.WARNING, logger="yalp.reactive.power_monitor"):
        _make_monitor(_FakeSmbus(_regs()), shunt_ohms=0.1, max_expected_amps=3.2)
    assert not [r for r in caplog.records if "full scale" in r.getMessage()]


def test_init_i2c_failure_raises_runtime_error_and_closes_bus():
    bus = _FakeSmbus()
    bus.fail_writes = True
    with pytest.raises(RuntimeError, match="did not respond"):
        _make_monitor(bus)
    assert bus.closed is True


# --------------------------------------------------------------------------- #
# 3. read(): register decoding.
# --------------------------------------------------------------------------- #
def test_read_decodes_bus_shunt_current_power():
    bus = _FakeSmbus(_regs(bus_v=5.5, shunt_mv=30.0, current_raw=3000, power_raw=825))
    mon = _make_monitor(bus)
    mon._test_clock["now"] = 42.0
    s = mon.read()
    assert s.ok is True
    assert s.overflow is False
    assert s.ts == 42.0
    assert s.bus_voltage_v == pytest.approx(5.5)        # (raw >> 3) * 4 mV
    assert s.shunt_voltage_mv == pytest.approx(30.0)    # raw * 10 µV
    assert s.current_a == pytest.approx(0.3)            # raw * 100 µA
    assert s.power_w == pytest.approx(1.65)             # raw * 2 mW
    assert set(bus.reads) == {REG_SHUNT_VOLTAGE, REG_BUS_VOLTAGE, REG_CURRENT, REG_POWER}


def test_read_bus_voltage_ignores_the_low_status_bits():
    # 32 V full scale: raw 0x1F40 (8000 << 3 = 0xFA00) + CNVR + OVF set.
    bus = _FakeSmbus(_regs(bus_v=32.0, ovf=True))
    bus.regs[REG_BUS_VOLTAGE] |= 0x2  # CNVR
    s = _make_monitor(bus).read()
    assert s.bus_voltage_v == pytest.approx(32.0)
    assert s.overflow is True


def test_negative_current_and_shunt_are_two_complement():
    bus = _FakeSmbus(_regs(shunt_mv=-30.0, current_raw=-3000))
    assert bus.regs[REG_CURRENT] == 0xF448 and bus.regs[REG_SHUNT_VOLTAGE] == 0xF448
    s = _make_monitor(bus).read()
    assert s.ok is True
    assert s.current_a == pytest.approx(-0.3)
    assert s.shunt_voltage_mv == pytest.approx(-30.0)


def test_read_rewrites_calibration_when_it_appears_lost(caplog):
    # A real 30 mV across the shunt but CURRENT reads 0 -> CAL was reset.
    bus = _FakeSmbus(_regs(shunt_mv=30.0, current_raw=0, power_raw=0))
    mon = _make_monitor(bus)
    assert len(bus.writes) == 2
    with caplog.at_level(logging.WARNING, logger="yalp.reactive.power_monitor"):
        s = mon.read()
    assert s.ok is True
    assert s.current_a == pytest.approx(0.3)   # served from V_shunt / R this once
    assert s.power_w == pytest.approx(5.5 * 0.3)
    assert bus.writes[-1] == (ADDR, REG_CALIBRATION, 4096)
    assert len(bus.writes) == 3
    assert mon.stats()["cal_rewrites"] == 1
    assert any("calibration" in r.getMessage().lower() for r in caplog.records)


def test_read_does_not_rewrite_calibration_for_a_genuine_zero_load():
    bus = _FakeSmbus(_regs(shunt_mv=0.03, current_raw=0, power_raw=0))  # 3 raw LSB of noise
    mon = _make_monitor(bus)
    s = mon.read()
    assert s.current_a == 0.0
    assert len(bus.writes) == 2
    assert mon.stats()["cal_rewrites"] == 0


# --------------------------------------------------------------------------- #
# 4. read() never raises: bus errors / closed -> ok=False, zeros.
# --------------------------------------------------------------------------- #
def test_read_returns_ok_false_on_bus_error_then_recovers():
    bus = _FakeSmbus(_regs())
    mon = _make_monitor(bus)
    mon._test_clock["now"] = 9.0
    bus.fail_reads = True
    s = mon.read()
    assert s.ok is False
    assert (s.bus_voltage_v, s.shunt_voltage_mv, s.current_a, s.power_w) == (0.0, 0.0, 0.0, 0.0)
    assert s.ts == 9.0
    assert mon.stats()["read_errors"] == 1

    bus.fail_reads = False
    assert mon.read().ok is True
    assert mon.stats()["reads"] == 2


def test_close_is_idempotent_and_reads_after_close_are_not_ok():
    bus = _FakeSmbus(_regs())
    mon = _make_monitor(bus)
    mon.close()
    assert bus.closed is True
    mon.close()
    s = mon.read()
    assert s.ok is False
    assert s.bus_voltage_v == 0.0


# --------------------------------------------------------------------------- #
# 5. FakePowerMonitor.
# --------------------------------------------------------------------------- #
def test_fake_power_monitor_defaults_and_setters():
    clock = {"now": 3.0}
    fake = FakePowerMonitor(monotonic=lambda: clock["now"])
    assert isinstance(fake, PowerMonitor)
    s = fake.read()
    assert s.ok is True
    assert s.ts == 3.0
    assert s.bus_voltage_v == 5.5
    assert s.current_a == 0.3
    assert s.shunt_voltage_mv == pytest.approx(30.0)  # 0.3 A * 0.1 Ω
    assert s.power_w == pytest.approx(1.65)

    fake.set_voltage(4.4)
    fake.set_current(1.5)
    s = fake.read()
    assert (s.bus_voltage_v, s.current_a) == (4.4, 1.5)
    assert s.shunt_voltage_mv == pytest.approx(150.0)
    fake.set(voltage_v=5.0)
    assert fake.read().bus_voltage_v == 5.0 and fake.read().current_a == 1.5
    fake.set(current_a=-0.2)
    assert fake.read().current_a == -0.2
    assert fake.read_count == 5
    assert fake.last is not None and fake.last.current_a == -0.2


def test_fake_power_monitor_fault_and_close():
    fake = FakePowerMonitor(voltage_v=6.0, current_a=0.1)
    fake.set_ok(False)
    assert fake.read().ok is False
    fake.set_ok(True)
    assert fake.read().ok is True
    fake.close()
    assert fake.closed is True
    assert fake.read().ok is False
    assert fake.stats()["reads"] == 3


# --------------------------------------------------------------------------- #
# 6. nimh_pack_state thresholds (per cell: >1.35 full, >1.25 ok, >1.15 low,
#    >0.9 flat, else off).
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "voltage,cells,state",
    [
        (5.6, 4, "full"),    # 1.40 / cell
        (5.44, 4, "full"),   # 1.36
        (5.4, 4, "ok"),      # exactly 1.35 -> not > 1.35
        (5.2, 4, "ok"),      # 1.30
        (5.0, 4, "low"),     # exactly 1.25 -> not > 1.25
        (4.8, 4, "low"),     # 1.20
        (4.6, 4, "flat"),    # exactly 1.15 -> not > 1.15
        (4.0, 4, "flat"),    # 1.00
        (3.6, 4, "off"),     # exactly 0.90 -> not > 0.90
        (0.0, 4, "off"),
        (1.4, 1, "full"),
        (2.4, 2, "low"),     # 1.20
    ],
)
def test_nimh_pack_state_thresholds(voltage, cells, state):
    assert nimh_pack_state(voltage, cells=cells) == state


def test_nimh_pack_state_defaults_to_four_cells_and_rejects_bad_cells():
    assert nimh_pack_state(5.5) == "full"
    with pytest.raises(ValueError):
        nimh_pack_state(5.5, cells=0)


# --------------------------------------------------------------------------- #
# 7. Lazy smbus import: prefer smbus2, fall back to smbus, else fail loudly.
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
        Ina219PowerMonitor()


def test_lazy_import_prefers_smbus2(monkeypatch):
    mod = _fake_smbus_module("smbus2", _FakeSmbus(_regs()))
    monkeypatch.setitem(sys.modules, "smbus2", mod)
    monkeypatch.setitem(sys.modules, "smbus", None)
    mon = Ina219PowerMonitor(bus=1)
    assert mod.calls == [1]
    assert mon.read().bus_voltage_v == pytest.approx(5.5)


def test_lazy_import_falls_back_to_smbus(monkeypatch):
    monkeypatch.setitem(sys.modules, "smbus2", None)
    mod = _fake_smbus_module("smbus", _FakeSmbus(_regs()))
    monkeypatch.setitem(sys.modules, "smbus", mod)
    mon = Ina219PowerMonitor(bus=3)
    assert mod.calls == [3]
    assert mon.read().ok is True
