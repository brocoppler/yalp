"""INA219 pack voltage / current monitor over I2C — Izzy's battery gauge.

**Why.** Izzy runs on a 4×AA NiMH pack that sags hard under motor load and dies
quietly: the motors just get weaker, drive calibrations (deadband, stiction
duties) drift with pack voltage, and a brown-out reboots the Pi mid-run. The
INA219 sits IN SERIES with the pack's positive lead and reports the bus voltage
(the pack under its real load), the shunt voltage, and — once calibrated — the
current and power. The reactive layer can then refuse to drive on a flat pack,
log pack sag against commanded duty, and put a battery state in telemetry.
:func:`nimh_pack_state` turns a voltage into a rough ``full/ok/low/flat/off``.

**Wiring (breakout with the usual 0.1 Ω shunt).** ::

    INA219    connect to                                  note
    -------   -----------------------------------------   ----------------------------------------
    VIN+      pack + (the red lead)                       HIGH side, IN SERIES with the pack
    VIN-      breadboard VM rail (J1, motor V+)           where the pack + lead used to go
    VCC       Pi 3V3 (physical pin 1 or 17)               chip supply — shared with the GY-521
    GND       Pi GND (physical pin 6/9/14/...)            MUST be common with pack - (it is:
                                                          the DRV8833 already shares ground)
    SDA       GPIO2 = SDA1 (physical pin 3)               shared with the MPU-6050 (0x68)
    SCL       GPIO3 = SCL1 (physical pin 5)

The bus voltage is measured from VIN- to GND, i.e. the pack voltage *after* the
shunt (the ~30 mV drop at 0.3 A is negligible). Current flowing VIN+ → VIN- reads
POSITIVE; wire the pack the other way round and it reads negative (harmless, but
tells you the shunt is backwards). Address 0x40 with the A0/A1 solder jumpers open;
bridge A0 → 0x41, A1 → 0x44, both → 0x45 — none of which can collide with the
MPU-6050 at 0x68/0x69, so both share I2C-1. Enable I2C once with
``sudo raspi-config nonint do_i2c 0``; ``i2cdetect -y 1`` should show ``40`` (and
``68``). Install ``python3-smbus2`` / ``pip install smbus2`` (see
:mod:`yalp.reactive.i2c`).

**Registers (INA219 datasheet, SBOS448).** ::

    CONFIG        0x00   RST | BRNG | PG[1:0] | BADC[3:0] | SADC[3:0] | MODE[2:0]
    SHUNT_VOLTAGE 0x01   signed, 10 µV/LSB
    BUS_VOLTAGE   0x02   bits[15:3] = voltage at 4 mV/LSB (so value >> 3 * 4 mV);
                         bit 1 = CNVR (conversion ready), bit 0 = OVF (math overflow)
    POWER         0x03   unsigned, 20 × current_LSB per bit (only valid once calibrated)
    CURRENT       0x04   signed, current_LSB per bit (only valid once calibrated)
    CALIBRATION   0x05   trunc(0.04096 / (current_LSB × R_shunt)); bit 0 is read-only 0

The default configuration is 32 V bus range, shunt gain /8 (±320 mV full-scale,
i.e. ±3.2 A across 0.1 Ω), 12-bit ADCs, continuous shunt+bus — the datasheet's
worked example, ``0x399F``. Calibration follows the datasheet procedure in
:func:`ina219_calibration`: minimum current LSB = ``max_expected_amps / 32767``,
rounded UP to a "round" 1/2/5 × 10ⁿ value, then the register value; for the
defaults (0.1 Ω, 3.2 A) that is a 100 µA/bit current LSB, 2 mW/bit power LSB and
``CAL = 4096``.

**Import policy.** No hardware library is imported at import time; ``smbus2`` /
``smbus`` are resolved lazily in :class:`Ina219PowerMonitor` and a fake bus can be
injected with ``_smbus=``. :class:`FakePowerMonitor` is the laptop stand-in.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Optional, Protocol, Tuple, runtime_checkable

from yalp.reactive.i2c import bytes_from_u16, open_smbus, to_signed16, u16_from_bytes

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Register-level constants (INA219 datasheet, SBOS448).
# --------------------------------------------------------------------------- #
INA219_DEFAULT_ADDRESS = 0x40  #: A0/A1 open. A0 -> 0x41, A1 -> 0x44, both -> 0x45

REG_CONFIG = 0x00
REG_SHUNT_VOLTAGE = 0x01
REG_BUS_VOLTAGE = 0x02
REG_POWER = 0x03
REG_CURRENT = 0x04
REG_CALIBRATION = 0x05

# CONFIG bit fields.
CFG_RST = 1 << 15
CFG_BRNG_32V = 1 << 13  #: bus voltage range 32 V (0 = 16 V)
CFG_PG_320MV = 0b11 << 11  #: shunt gain /8 -> ±320 mV full scale
CFG_BADC_12BIT = 0b0011 << 7  #: bus ADC 12-bit, single sample
CFG_SADC_12BIT = 0b0011 << 3  #: shunt ADC 12-bit, single sample
CFG_MODE_SHUNT_BUS_CONTINUOUS = 0b111

#: 32 V range, /8 gain (320 mV), 12-bit, continuous shunt+bus — the datasheet's
#: worked example and the Adafruit "32V_2A"/"32V_3.2A" configuration.
INA219_CONFIG_DEFAULT = (
    CFG_BRNG_32V | CFG_PG_320MV | CFG_BADC_12BIT | CFG_SADC_12BIT | CFG_MODE_SHUNT_BUS_CONTINUOUS
)
assert INA219_CONFIG_DEFAULT == 0x399F

SHUNT_VOLTAGE_LSB_V = 10e-6  #: 10 µV per shunt register bit
BUS_VOLTAGE_LSB_V = 4e-3  #: 4 mV per (bus register >> 3) bit
SHUNT_FULL_SCALE_V = 0.320  #: at PG = /8
_CAL_MAX = 0xFFFE  #: calibration register: 16 bits, bit 0 read-only 0


@dataclass(frozen=True)
class Ina219Calibration:
    """The datasheet calibration result for one shunt / max-current pair."""

    cal_register: int
    current_lsb_a: float
    power_lsb_w: float
    #: Largest current the CURRENT register can represent with this LSB.
    max_current_a: float


def _round_values_from(minimum: float) -> Iterator[float]:
    """Yield "round" 1/2/5 × 10ⁿ values ≥ ``minimum``, ascending (never ends)."""
    exp = math.floor(math.log10(minimum))
    while True:
        for m in (1.0, 2.0, 5.0):
            v = m * 10.0 ** exp
            if v >= minimum * (1.0 - 1e-9):
                yield v
        exp += 1


def ina219_calibration(shunt_ohms: float, max_expected_amps: float) -> Ina219Calibration:
    """Compute the CALIBRATION register per the INA219 datasheet (section 8.5).

    1. ``min_lsb = max_expected_amps / 32767`` (so the full expected current fits
       the 15-bit signed CURRENT register);
    2. round it UP to a "round" value (1, 2 or 5 × 10ⁿ — the datasheet leaves the
       choice open; this makes it deterministic);
    3. ``CAL = trunc(0.04096 / (current_lsb × shunt_ohms))``, bit 0 cleared; if
       that overflows 16 bits (a tiny shunt or a tiny expected current) step the
       LSB up to the next round value until it fits;
    4. ``power_lsb = 20 × current_lsb``.

    For the defaults (0.1 Ω, 3.2 A): LSB 100 µA, CAL 4096, power LSB 2 mW.
    """
    if shunt_ohms <= 0:
        raise ValueError(f"shunt_ohms must be positive, got {shunt_ohms!r}")
    if max_expected_amps <= 0:
        raise ValueError(f"max_expected_amps must be positive, got {max_expected_amps!r}")
    min_lsb = float(max_expected_amps) / 32767.0
    cal = 0
    current_lsb = min_lsb
    for current_lsb in _round_values_from(min_lsb):
        # round() rather than a raw trunc: float noise (1e-4 * 0.1 is not exactly
        # 1e-5) would otherwise turn an exact 4096 into 4095.
        cal = int(round(0.04096 / (current_lsb * float(shunt_ohms))))
        if cal <= _CAL_MAX:
            break
    cal &= 0xFFFE
    if cal <= 0:
        raise ValueError(
            f"shunt_ohms={shunt_ohms!r} / max_expected_amps={max_expected_amps!r} "
            "give a zero calibration register (shunt too large for this current)"
        )
    return Ina219Calibration(
        cal_register=cal,
        current_lsb_a=current_lsb,
        power_lsb_w=20.0 * current_lsb,
        max_current_a=current_lsb * 32767.0,
    )


@dataclass(frozen=True)
class PowerSample:
    """One pack reading.

    * ``bus_voltage_v`` — pack voltage at VIN- (after the shunt) relative to GND.
    * ``shunt_voltage_mv`` — drop across the shunt (positive = VIN+ → VIN- flow).
    * ``current_a`` — signed load current from the CURRENT register.
    * ``power_w`` — from the POWER register (always ≥ 0).
    * ``ts`` — monotonic timestamp of the read.
    * ``ok`` — ``False`` means the I2C read failed; every number is then 0.
    * ``overflow`` — the chip's OVF flag: the CURRENT/POWER maths overflowed
      (``max_expected_amps`` too small for the real load); voltage stays valid.
    """

    bus_voltage_v: float
    shunt_voltage_mv: float
    current_a: float
    power_w: float
    ts: float
    ok: bool
    overflow: bool = False


@runtime_checkable
class PowerMonitor(Protocol):
    """The narrow pack-monitor interface the reactive layer depends on."""

    def read(self) -> PowerSample:
        """Take one reading. Never raises; ``ok=False`` on a bus error."""
        ...

    def close(self) -> None:
        """Release the bus. Idempotent."""
        ...


class Ina219PowerMonitor:
    """A real INA219 over I2C, laptop-importable, hardware only at init.

    Construction writes CONFIG (:data:`INA219_CONFIG_DEFAULT`) and the datasheet
    CALIBRATION value for ``shunt_ohms`` / ``max_expected_amps``; an I2C failure
    there raises :class:`RuntimeError`. After that :meth:`read` never raises.

    If the chip ever loses its calibration (a power glitch resets it to 0, after
    which CURRENT/POWER read 0 while the shunt voltage does not) :meth:`read`
    detects the mismatch, rewrites the calibration, and serves the current for
    that one sample from the shunt voltage (``V_shunt / R``) instead of 0.

    ``monotonic`` is injectable for tests; ``_smbus`` injects a fake bus object
    (anything with ``write_i2c_block_data`` / ``read_i2c_block_data`` / ``close``).
    """

    def __init__(
        self,
        *,
        bus: int = 1,
        address: int = INA219_DEFAULT_ADDRESS,
        shunt_ohms: float = 0.1,
        max_expected_amps: float = 3.2,
        config: int = INA219_CONFIG_DEFAULT,
        monotonic: Optional[Callable[[], float]] = None,
        _smbus: Optional[Any] = None,
    ) -> None:
        self._address = int(address)
        self._shunt_ohms = float(shunt_ohms)
        self._max_expected_amps = float(max_expected_amps)
        self._config = int(config) & 0xFFFF
        self._clock: Callable[[], float] = monotonic if monotonic is not None else time.monotonic
        self._closed = False
        self._reads = 0
        self._read_errors = 0
        self._cal_rewrites = 0

        self.calibration: Ina219Calibration = ina219_calibration(
            self._shunt_ohms, self._max_expected_amps
        )
        shunt_max_a = SHUNT_FULL_SCALE_V / self._shunt_ohms
        if self._max_expected_amps > shunt_max_a * (1.0 + 1e-9):
            logger.warning(
                "INA219: max_expected_amps=%.2f A exceeds the shunt's full scale of "
                "%.2f A (320 mV / %.3f Ω at gain /8); the shunt ADC will clip above "
                "that even though the CURRENT register could represent more.",
                self._max_expected_amps,
                shunt_max_a,
                self._shunt_ohms,
            )

        # --- Lazy hardware import (keep the module laptop-importable) ---------
        self._bus: Any = (
            _smbus if _smbus is not None else open_smbus(bus, purpose="the INA219 pack monitor")
        )
        try:
            self._write_u16(REG_CONFIG, self._config)
            self._write_u16(REG_CALIBRATION, self.calibration.cal_register)
        except Exception as exc:
            self.close()
            raise RuntimeError(
                f"INA219 at I2C address 0x{self._address:02X} on bus {bus} did not "
                f"respond during init ({exc!r}). Check the wiring (SDA=GPIO2/pin 3, "
                "SCL=GPIO3/pin 5, 3V3, GND), that I2C is enabled "
                "(`sudo raspi-config nonint do_i2c 0`) and `i2cdetect -y 1` shows 40."
            ) from exc

    # -- register access ----------------------------------------------------
    def _write_u16(self, reg: int, value: int) -> None:
        # The INA219 is big-endian (MSB first); SMBus word ops are little-endian,
        # so use a 2-byte block write with the bytes in the right order.
        self._bus.write_i2c_block_data(self._address, reg, bytes_from_u16(value))

    def _read_u16(self, reg: int) -> int:
        data = self._bus.read_i2c_block_data(self._address, reg, 2)
        if data is None or len(data) < 2:
            raise OSError(f"short I2C read from INA219 register 0x{reg:02X}: {data!r}")
        return u16_from_bytes(data[0], data[1])

    # -- properties ---------------------------------------------------------
    @property
    def address(self) -> int:
        return self._address

    @property
    def shunt_ohms(self) -> float:
        return self._shunt_ohms

    @property
    def config_register(self) -> int:
        return self._config

    # -- read ---------------------------------------------------------------
    def read(self) -> PowerSample:
        """Take one reading. NEVER raises: a bus error yields ``ok=False``."""
        ts = self._clock()
        self._reads += 1
        if self._closed:
            self._read_errors += 1
            return PowerSample(0.0, 0.0, 0.0, 0.0, ts, False)
        try:
            shunt_raw = to_signed16(self._read_u16(REG_SHUNT_VOLTAGE))
            bus_raw = self._read_u16(REG_BUS_VOLTAGE)
            current_raw = to_signed16(self._read_u16(REG_CURRENT))
            power_raw = self._read_u16(REG_POWER)
        except Exception as exc:
            self._read_errors += 1
            logger.debug("INA219 read failed: %r", exc)
            return PowerSample(0.0, 0.0, 0.0, 0.0, ts, False)

        shunt_v = shunt_raw * SHUNT_VOLTAGE_LSB_V
        bus_v = (bus_raw >> 3) * BUS_VOLTAGE_LSB_V
        overflow = bool(bus_raw & 0x1)
        current_a = current_raw * self.calibration.current_lsb_a
        power_w = power_raw * self.calibration.power_lsb_w

        # Calibration-loss self-heal: a real current across the shunt but a zero
        # CURRENT register means CAL was reset (power glitch). Rewrite it and, for
        # THIS sample, derive the current from the shunt voltage instead of 0.
        if current_raw == 0 and abs(shunt_raw) > 5:
            self._cal_rewrites += 1
            try:
                self._write_u16(REG_CALIBRATION, self.calibration.cal_register)
                logger.warning(
                    "INA219 CURRENT read 0 with %.2f mV across the shunt — calibration "
                    "lost (chip reset?); rewrote CAL=%d.",
                    shunt_v * 1e3,
                    self.calibration.cal_register,
                )
            except Exception as exc:  # pragma: no cover - best effort
                logger.debug("INA219 calibration rewrite failed: %r", exc)
            current_a = shunt_v / self._shunt_ohms
            power_w = abs(bus_v * current_a)

        return PowerSample(bus_v, shunt_v * 1e3, current_a, power_w, ts, True, overflow)

    # -- observability ------------------------------------------------------
    def stats(self) -> dict:
        """Cumulative counters for telemetry (a fresh dict each call)."""
        return {
            "reads": self._reads,
            "read_errors": self._read_errors,
            "cal_rewrites": self._cal_rewrites,
            "cal_register": self.calibration.cal_register,
            "current_lsb_a": self.calibration.current_lsb_a,
        }

    def close(self) -> None:
        """Close the I2C bus handle (best-effort). Idempotent; reads then return ``ok=False``."""
        if self._closed:
            return
        self._closed = True
        bus = getattr(self, "_bus", None)
        if bus is not None:
            try:
                bus.close()
            except Exception:  # pragma: no cover - best effort during teardown
                pass


class FakePowerMonitor:
    """A pure-software :class:`PowerMonitor` for laptop tests and the fake wiring.

    Holds a settable voltage / current; :meth:`read` derives the shunt voltage
    (``I × R``) and power (``V × I``) from them so telemetry sees a coherent
    sample. Defaults to a healthy 4×AA NiMH pack (5.5 V) under a light 0.3 A load.
    :meth:`set_ok` simulates a bus fault (``ok=False``).
    """

    def __init__(
        self,
        voltage_v: float = 5.5,
        current_a: float = 0.3,
        *,
        shunt_ohms: float = 0.1,
        monotonic: Optional[Callable[[], float]] = None,
    ) -> None:
        self.voltage_v = float(voltage_v)
        self.current_a = float(current_a)
        self._shunt_ohms = float(shunt_ohms)
        self._clock: Callable[[], float] = monotonic if monotonic is not None else time.monotonic
        self.ok = True
        self.closed = False
        self.read_count = 0
        self.last: Optional[PowerSample] = None

    def set_voltage(self, voltage_v: float) -> None:
        self.voltage_v = float(voltage_v)

    def set_current(self, current_a: float) -> None:
        self.current_a = float(current_a)

    def set(self, *, voltage_v: Optional[float] = None, current_a: Optional[float] = None) -> None:
        """Set either or both values in one call."""
        if voltage_v is not None:
            self.set_voltage(voltage_v)
        if current_a is not None:
            self.set_current(current_a)

    def set_ok(self, ok: bool) -> None:
        """Simulate (or clear) an I2C fault: reads return ``ok=False`` while False."""
        self.ok = bool(ok)

    def read(self) -> PowerSample:
        self.read_count += 1
        ts = self._clock()
        if self.closed or not self.ok:
            sample = PowerSample(0.0, 0.0, 0.0, 0.0, ts, False)
        else:
            shunt_mv = self.current_a * self._shunt_ohms * 1e3
            sample = PowerSample(
                self.voltage_v,
                shunt_mv,
                self.current_a,
                abs(self.voltage_v * self.current_a),
                ts,
                True,
            )
        self.last = sample
        return sample

    def stats(self) -> dict:
        return {
            "reads": self.read_count,
            "read_errors": 0,
            "cal_rewrites": 0,
            "cal_register": 0,
            "current_lsb_a": 0.0,
        }

    def close(self) -> None:
        self.closed = True


# --------------------------------------------------------------------------- #
# Battery state heuristic.
# --------------------------------------------------------------------------- #
#: Per-cell thresholds (V), checked top-down with ``>``; below the last is "off".
NIMH_CELL_THRESHOLDS_V: Tuple[Tuple[str, float], ...] = (
    ("full", 1.35),
    ("ok", 1.25),
    ("low", 1.15),
    ("flat", 0.90),
)


def nimh_pack_state(voltage_v: float, cells: int = 4) -> str:
    """Rough NiMH pack state from its terminal voltage under LIGHT load.

    Per cell: ``> 1.35 V`` → ``"full"`` (fresh off the charger), ``> 1.25`` →
    ``"ok"`` (the long flat plateau), ``> 1.15`` → ``"low"`` (the knee — recharge
    soon), ``> 0.90`` → ``"flat"`` (stop driving; below ~1.0 V/cell a NiMH cell can
    be reverse-charged by its neighbours), else ``"off"`` (no pack / switched off /
    a dead cell). These are a guide for 4×AA NiMH under a few hundred mA, not a
    fuel gauge: NiMH's discharge curve is flat, so voltage says little about
    remaining capacity until the knee, and motor load sags the reading (compare
    like with like — at rest, or at a known duty).
    """
    cells = int(cells)
    if cells <= 0:
        raise ValueError(f"cells must be positive, got {cells!r}")
    per_cell = float(voltage_v) / cells
    for name, threshold in NIMH_CELL_THRESHOLDS_V:
        if per_cell > threshold:
            return name
    return "off"


__all__ = [
    "PowerSample",
    "PowerMonitor",
    "Ina219PowerMonitor",
    "FakePowerMonitor",
    "Ina219Calibration",
    "ina219_calibration",
    "nimh_pack_state",
    "NIMH_CELL_THRESHOLDS_V",
    "INA219_DEFAULT_ADDRESS",
    "INA219_CONFIG_DEFAULT",
    "REG_CONFIG",
    "REG_SHUNT_VOLTAGE",
    "REG_BUS_VOLTAGE",
    "REG_POWER",
    "REG_CURRENT",
    "REG_CALIBRATION",
]
