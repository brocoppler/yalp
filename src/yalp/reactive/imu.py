"""MPU-6050 (GY-521 module) gyro/accelerometer over I2C — Izzy's low-latency yaw.

**Why an IMU.** Izzy has no wheel encoders. The only heading feedback the
reactive layer has today is the camera-based heading hold in ``tick_core``: it
lags ~70 ms behind the wheels (capture + detect + tick), updates at the camera's
~15 fps, and is blind in the dark or against a featureless wall. The MPU-6050's
gyro measures the robot's TRUE yaw *rate* (internally at 1 kHz, delivered over
I2C in well under a millisecond, in any lighting), so it becomes the fast inner
yaw-rate source for heading hold / closed-loop rotate. The camera stays as the
fallback and slow re-anchor: a gyro measures RATE, and integrating rate into a
heading (:class:`YawIntegrator`) accumulates bias drift, which the camera does
not. The two are complementary — gyro for the fast loop, camera to correct drift.

**Sign convention.** :attr:`ImuSample.yaw_rate_dps` is POSITIVE when the robot
turns LEFT — counter-clockwise viewed from above — matching the reactive layer's
positive-``turn``-is-left sign. That is the right-hand rule about a Z axis that
points UP: with the GY-521 mounted flat, components facing UP, the raw gyro Z
already has that sign, so the defaults ``yaw_axis="z", yaw_sign=+1`` need no
correction. **The sign depends on mounting.** Chip-DOWN flips it (use
``yaw_sign=-1``); mounted on its side the vertical axis is X or Y (``yaw_axis``).
Verify on the bench: turn the robot left by hand and check the rate is positive.

**Wiring (Pi 5, I2C-1 on the 40-pin header).** ::

    GY-521   Pi 5 header                                note
    ------   ----------------------------------------   ------------------------------------
    VCC      3V3   (physical pin 1 or 17)               module has an on-board LDO; 5 V also OK
    GND      GND   (physical pin 6/9/14/20/25/30/34/39)
    SDA      GPIO2 = SDA1 (physical pin 3)              Pi has 1.8 kΩ pull-ups on-board
    SCL      GPIO3 = SCL1 (physical pin 5)              (the module adds its own 2.2 kΩ)
    AD0      leave open (pulled LOW on the module)      -> address 0x68; tie to VCC -> 0x69
    INT/XDA/XCL  unused

Enable I2C once: ``sudo raspi-config nonint do_i2c 0`` (0 = enable), then
``i2cdetect -y 1`` should show ``68``. Install the binding with
``sudo apt install python3-smbus2`` or ``pip install smbus2`` (see
:mod:`yalp.reactive.i2c`). The INA219 pack monitor shares the same two wires at
address 0x40.

**Register map used (MPU-6050 register map rev 4.2).** ::

    SMPLRT_DIV    0x19   sample-rate divider (left at the power-on default)
    CONFIG        0x1A   DLPF_CFG[2:0]: 3 -> ~44 Hz accel / 42 Hz gyro low-pass
    GYRO_CONFIG   0x1B   FS_SEL[4:3]: 0=±250, 1=±500, 2=±1000, 3=±2000 °/s
    ACCEL_CONFIG  0x1C   AFS_SEL[4:3]: 0=±2, 1=±4, 2=±8, 3=±16 g
    PWR_MGMT_1    0x6B   0x00 clears SLEEP (the chip powers up ASLEEP)
    WHO_AM_I      0x75   0x68 on a genuine MPU-6050 (clones report 0x69/0x70/0x71)
    ACCEL_XOUT_H  0x3B   14 data bytes, big-endian int16, in this order:
      0x3B..0x40 accel X,Y,Z   0x41..0x42 temp   0x43..0x48 gyro X,Y,Z

Sensitivity (LSB per unit): gyro 131 / 65.5 / 32.8 / 16.4 per °/s for
±250/500/1000/2000; accel 16384 / 8192 / 4096 / 2048 per g for ±2/4/8/16;
temperature ``raw / 340 + 36.53`` °C.

**Import policy.** Like :mod:`~yalp.reactive.hardware`, this module imports no
hardware library at import time; ``smbus2``/``smbus`` are resolved lazily inside
:class:`Mpu6050Imu` (via :func:`yalp.reactive.i2c.open_smbus`), and tests inject
a fake bus with ``_smbus=``. :class:`FakeImu` is the hardware-free stand-in the
reactive layer's fake wiring uses.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, List, Optional, Protocol, Sequence, Tuple, Union, runtime_checkable

from yalp.reactive.i2c import open_smbus, to_signed16

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Register-level constants (MPU-6050 register map, rev 4.2).
# --------------------------------------------------------------------------- #
MPU6050_DEFAULT_ADDRESS = 0x68  #: AD0 low (GY-521 default); AD0 high -> 0x69

REG_SMPLRT_DIV = 0x19
REG_CONFIG = 0x1A
REG_GYRO_CONFIG = 0x1B
REG_ACCEL_CONFIG = 0x1C
REG_ACCEL_XOUT_H = 0x3B  #: start of the 14-byte accel/temp/gyro block (..0x48)
REG_GYRO_ZOUT_L = 0x48
REG_PWR_MGMT_1 = 0x6B
REG_WHO_AM_I = 0x75

#: Number of bytes in one burst read of accel (6) + temp (2) + gyro (6).
DATA_BLOCK_LEN = 14

#: DLPF_CFG value for CONFIG: 3 -> accel 44 Hz / gyro 42 Hz low-pass, ~4.9 ms delay.
DLPF_CFG_44HZ = 0x03

#: WHO_AM_I values we accept silently: genuine MPU-6050 (0x68) plus the common
#: MPU-6050/6500/9250 variants and AD0-high clones. Anything else warns.
KNOWN_WHO_AM_I = frozenset({0x68, 0x69, 0x70, 0x71})

#: Gyro full-scale range (°/s) -> (FS_SEL, LSB per °/s).
GYRO_RANGES = {250: (0, 131.0), 500: (1, 65.5), 1000: (2, 32.8), 2000: (3, 16.4)}
#: Accel full-scale range (g) -> (AFS_SEL, LSB per g).
ACCEL_RANGES = {2: (0, 16384.0), 4: (1, 8192.0), 8: (2, 4096.0), 16: (3, 2048.0)}

_AXIS_INDEX = {"x": 0, "y": 1, "z": 2}


@dataclass(frozen=True)
class ImuSample:
    """One IMU reading.

    * ``yaw_rate_dps`` — bias-corrected, sign-adjusted yaw rate; POSITIVE = turning
      LEFT / counter-clockwise viewed from above (see the module docstring).
    * ``gyro_dps`` — bias-corrected ``(x, y, z)`` rates in the chip's own axes.
    * ``accel_g`` — ``(x, y, z)`` in g (≈ ``(0, 0, 1)`` at rest, chip-up).
    * ``temp_c`` — die temperature (only useful as a drift indicator).
    * ``ts`` — monotonic timestamp of the read (feed :class:`YawIntegrator`).
    * ``ok`` — ``False`` means the I2C read failed; every numeric field is then
      0 and the caller must NOT integrate or steer on it.
    """

    yaw_rate_dps: float
    gyro_dps: Tuple[float, float, float]
    accel_g: Tuple[float, float, float]
    temp_c: float
    ts: float
    ok: bool


@runtime_checkable
class Imu(Protocol):
    """The narrow IMU interface the reactive layer depends on (real or fake)."""

    def read(self) -> ImuSample:
        """Take one reading. Never raises; ``ok=False`` on a bus error."""
        ...

    def calibrate_gyro(
        self,
        samples: int = 200,
        interval_s: float = 0.005,
        sleep: Optional[Callable[[float], None]] = None,
    ) -> Tuple[float, float, float]:
        """Measure the at-rest gyro bias (°/s per axis) and store it."""
        ...

    def close(self) -> None:
        """Release the bus. Idempotent."""
        ...


class Mpu6050Imu:
    """A real MPU-6050 (GY-521) over I2C, laptop-importable, hardware only at init.

    Construction wakes the chip (``PWR_MGMT_1 = 0``), sets the digital low-pass
    filter (~44 Hz), the gyro and accel full-scale ranges, and reads ``WHO_AM_I``
    (warning, not failing, on an unexpected ID — clones abound). An I2C failure
    *during construction* raises :class:`RuntimeError` (the chip is not there or
    I2C is disabled); after that :meth:`read` never raises.

    The gyro bias is zero until :meth:`calibrate_gyro` is run **with the robot at
    rest** (or :meth:`set_gyro_bias` is given a stored bias). Do it at every start:
    the bias moves a few tenths of a °/s with temperature.

    ``yaw_axis``/``yaw_sign`` pick which chip axis is vertical and flip the sign so
    positive = LEFT turn (see the module docstring). ``monotonic``/``sleep`` are
    injectable for tests; ``_smbus`` injects a fake bus object (anything with
    ``write_byte_data`` / ``read_byte_data`` / ``read_i2c_block_data`` / ``close``).
    """

    def __init__(
        self,
        *,
        bus: int = 1,
        address: int = MPU6050_DEFAULT_ADDRESS,
        gyro_range_dps: int = 250,
        accel_range_g: int = 2,
        yaw_axis: str = "z",
        yaw_sign: int = +1,
        dlpf_cfg: int = DLPF_CFG_44HZ,
        wake_settle_s: float = 0.05,
        monotonic: Optional[Callable[[], float]] = None,
        sleep: Optional[Callable[[float], None]] = None,
        _smbus: Optional[Any] = None,
    ) -> None:
        if int(gyro_range_dps) not in GYRO_RANGES:
            raise ValueError(
                f"gyro_range_dps must be one of {sorted(GYRO_RANGES)}, got {gyro_range_dps!r}"
            )
        if int(accel_range_g) not in ACCEL_RANGES:
            raise ValueError(
                f"accel_range_g must be one of {sorted(ACCEL_RANGES)}, got {accel_range_g!r}"
            )
        axis = str(yaw_axis).strip().lower()
        if axis not in _AXIS_INDEX:
            raise ValueError(f"yaw_axis must be 'x', 'y' or 'z', got {yaw_axis!r}")
        if int(yaw_sign) not in (1, -1):
            raise ValueError(f"yaw_sign must be +1 or -1, got {yaw_sign!r}")
        if not (0 <= int(dlpf_cfg) <= 6):
            raise ValueError(f"dlpf_cfg must be 0..6 (DLPF_CFG), got {dlpf_cfg!r}")

        self._address = int(address)
        self._gyro_range_dps = int(gyro_range_dps)
        self._accel_range_g = int(accel_range_g)
        self._gyro_fs_sel, self._gyro_lsb_per_dps = GYRO_RANGES[self._gyro_range_dps]
        self._accel_fs_sel, self._accel_lsb_per_g = ACCEL_RANGES[self._accel_range_g]
        self._yaw_axis = axis
        self._yaw_index = _AXIS_INDEX[axis]
        self._yaw_sign = float(int(yaw_sign))
        self._dlpf_cfg = int(dlpf_cfg)
        self._clock: Callable[[], float] = monotonic if monotonic is not None else time.monotonic
        self._sleep: Callable[[float], None] = sleep if sleep is not None else time.sleep
        self._bias_dps: Tuple[float, float, float] = (0.0, 0.0, 0.0)
        self._closed = False
        self.who_am_i: Optional[int] = None

        # Observability counters (exposed via stats()).
        self._reads = 0
        self._read_errors = 0
        self._calibrations = 0
        self._calibration_bad_reads = 0

        # --- Lazy hardware import (keep the module laptop-importable) ---------
        self._bus: Any = (
            _smbus if _smbus is not None else open_smbus(bus, purpose="the MPU-6050 IMU")
        )

        try:
            self._init_chip(wake_settle_s)
        except Exception as exc:
            # The chip is not answering: fail LOUD at construction (not from read()).
            self.close()
            raise RuntimeError(
                f"MPU-6050 at I2C address 0x{self._address:02X} on bus {bus} did not "
                f"respond during init ({exc!r}). Check the wiring (SDA=GPIO2/pin 3, "
                "SCL=GPIO3/pin 5, 3V3, GND), that I2C is enabled "
                "(`sudo raspi-config nonint do_i2c 0`) and `i2cdetect -y 1` shows 68."
            ) from exc

    # -- init ---------------------------------------------------------------
    def _init_chip(self, wake_settle_s: float) -> None:
        who = int(self._bus.read_byte_data(self._address, REG_WHO_AM_I)) & 0xFF
        self.who_am_i = who
        if who not in KNOWN_WHO_AM_I:
            logger.warning(
                "MPU-6050 WHO_AM_I read 0x%02X at address 0x%02X (expected 0x68; "
                "0x69/0x70/0x71 variants are also known). Continuing — it may be a "
                "clone — but if readings look wrong, check the module.",
                who,
                self._address,
            )
        # Wake: the chip powers up with SLEEP set; clearing PWR_MGMT_1 starts it.
        self._bus.write_byte_data(self._address, REG_PWR_MGMT_1, 0x00)
        if wake_settle_s > 0:
            self._sleep(float(wake_settle_s))  # gyro start-up time is ~30 ms
        # ~44 Hz digital low-pass on both sensors (tames the 1 kHz raw noise).
        self._bus.write_byte_data(self._address, REG_CONFIG, self._dlpf_cfg)
        # Full-scale ranges: FS_SEL / AFS_SEL live in bits [4:3].
        self._bus.write_byte_data(self._address, REG_GYRO_CONFIG, self._gyro_fs_sel << 3)
        self._bus.write_byte_data(self._address, REG_ACCEL_CONFIG, self._accel_fs_sel << 3)

    # -- properties ---------------------------------------------------------
    @property
    def address(self) -> int:
        return self._address

    @property
    def gyro_range_dps(self) -> int:
        return self._gyro_range_dps

    @property
    def accel_range_g(self) -> int:
        return self._accel_range_g

    @property
    def gyro_bias_dps(self) -> Tuple[float, float, float]:
        """The stored at-rest gyro bias ``(x, y, z)`` in °/s (zero until calibrated)."""
        return self._bias_dps

    def set_gyro_bias(self, bias_dps: Sequence[float]) -> None:
        """Install a previously measured bias (e.g. persisted from a prior run)."""
        if len(bias_dps) != 3:
            raise ValueError("bias_dps must have 3 components (x, y, z)")
        self._bias_dps = (float(bias_dps[0]), float(bias_dps[1]), float(bias_dps[2]))

    # -- raw read -----------------------------------------------------------
    def _read_raw(self) -> Tuple[Tuple[int, int, int], int, Tuple[int, int, int]]:
        """One 14-byte burst read → ``(accel_raw, temp_raw, gyro_raw)`` (int16s).

        Raises on any I2C error or short read; :meth:`read` turns that into
        ``ok=False``.
        """
        data = self._bus.read_i2c_block_data(self._address, REG_ACCEL_XOUT_H, DATA_BLOCK_LEN)
        if data is None or len(data) < DATA_BLOCK_LEN:
            raise OSError(f"short I2C read from MPU-6050: {data!r}")
        words = [
            to_signed16((int(data[i]) << 8) | int(data[i + 1]))
            for i in range(0, DATA_BLOCK_LEN, 2)
        ]
        accel = (words[0], words[1], words[2])
        temp = words[3]
        gyro = (words[4], words[5], words[6])
        return accel, temp, gyro

    def _gyro_dps_from_raw(self, gyro_raw: Tuple[int, int, int]) -> Tuple[float, float, float]:
        s = self._gyro_lsb_per_dps
        return (gyro_raw[0] / s, gyro_raw[1] / s, gyro_raw[2] / s)

    def read(self) -> ImuSample:
        """Take one reading. NEVER raises: a bus error yields ``ok=False``.

        Gyro rates are bias-corrected on all three axes; the yaw rate is the
        ``yaw_axis`` component times ``yaw_sign`` (positive = LEFT turn).
        """
        ts = self._clock()
        self._reads += 1
        if self._closed:
            self._read_errors += 1
            return ImuSample(0.0, (0.0, 0.0, 0.0), (0.0, 0.0, 0.0), 0.0, ts, False)
        try:
            accel_raw, temp_raw, gyro_raw = self._read_raw()
        except Exception as exc:
            self._read_errors += 1
            logger.debug("MPU-6050 read failed: %r", exc)
            return ImuSample(0.0, (0.0, 0.0, 0.0), (0.0, 0.0, 0.0), 0.0, ts, False)

        gx, gy, gz = self._gyro_dps_from_raw(gyro_raw)
        bx, by, bz = self._bias_dps
        gyro = (gx - bx, gy - by, gz - bz)
        a = self._accel_lsb_per_g
        accel = (accel_raw[0] / a, accel_raw[1] / a, accel_raw[2] / a)
        temp_c = temp_raw / 340.0 + 36.53
        yaw_rate = self._yaw_sign * gyro[self._yaw_index]
        return ImuSample(yaw_rate, gyro, accel, temp_c, ts, True)

    # -- calibration --------------------------------------------------------
    def calibrate_gyro(
        self,
        samples: int = 200,
        interval_s: float = 0.005,
        sleep: Optional[Callable[[float], None]] = None,
    ) -> Tuple[float, float, float]:
        """Average ``samples`` gyro reads WITH THE ROBOT AT REST → stored bias.

        Robust to a few bad reads (they are skipped and counted); raises
        :class:`RuntimeError` only if fewer than half the reads succeed, in which
        case the previous bias is left untouched. Warns if the spread of any axis
        exceeds ~20 °/s — that almost always means the robot was moving. At the
        default 200 × 5 ms this takes ~1 s. Returns the new ``(x, y, z)`` bias.
        """
        n = int(samples)
        if n <= 0:
            raise ValueError("samples must be positive")
        if self._closed:
            raise RuntimeError("cannot calibrate a closed MPU-6050")
        do_sleep = sleep if sleep is not None else self._sleep

        sums = [0.0, 0.0, 0.0]
        mins = [float("inf")] * 3
        maxs = [float("-inf")] * 3
        good = 0
        bad = 0
        for i in range(n):
            try:
                _, _, gyro_raw = self._read_raw()
            except Exception:
                bad += 1
            else:
                gx, gy, gz = self._gyro_dps_from_raw(gyro_raw)
                for k, v in enumerate((gx, gy, gz)):
                    sums[k] += v
                    mins[k] = min(mins[k], v)
                    maxs[k] = max(maxs[k], v)
                good += 1
            if interval_s > 0 and i < n - 1:
                do_sleep(float(interval_s))

        self._calibrations += 1
        self._calibration_bad_reads += bad
        min_good = max(1, n // 2)
        if good < min_good:
            raise RuntimeError(
                f"MPU-6050 gyro calibration failed: only {good}/{n} reads succeeded "
                f"({bad} I2C errors); keeping the previous bias {self._bias_dps}."
            )
        if bad:
            logger.warning("MPU-6050 gyro calibration skipped %d/%d bad reads.", bad, n)
        spread = max(maxs[k] - mins[k] for k in range(3))
        if spread > 20.0:
            logger.warning(
                "MPU-6050 gyro calibration saw a %.1f °/s spread — was the robot "
                "moving? Bias may be wrong; recalibrate at rest.",
                spread,
            )
        self._bias_dps = (sums[0] / good, sums[1] / good, sums[2] / good)
        logger.info(
            "MPU-6050 gyro bias: x=%.3f y=%.3f z=%.3f °/s (%d samples, %d bad)",
            self._bias_dps[0], self._bias_dps[1], self._bias_dps[2], good, bad,
        )
        return self._bias_dps

    # -- observability ------------------------------------------------------
    def stats(self) -> dict:
        """Cumulative counters for telemetry (a fresh dict each call)."""
        return {
            "reads": self._reads,
            "read_errors": self._read_errors,
            "calibrations": self._calibrations,
            "calibration_bad_reads": self._calibration_bad_reads,
            "bias_dps": list(self._bias_dps),
            "who_am_i": self.who_am_i,
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


class YawIntegrator:
    """Integrate successive :class:`ImuSample` yaw rates into a heading (degrees).

    Pure and clock-free: it uses the samples' own ``ts`` (monotonic seconds).
    Positive heading = turned LEFT / CCW of where :meth:`reset` was called (the
    same sign as the rate). Rectangular integration — ``heading += rate * dt`` with
    the *current* sample's rate — is plenty at 100+ Hz.

    Dropouts are handled conservatively: a sample with ``ok=False`` is ignored
    (counted in :attr:`dropped`) and does NOT advance the clock, so the next good
    sample covers the gap at its own rate; a gap longer than ``max_dt_s`` is
    instead treated as a resync (counted in :attr:`gaps`, nothing integrated) so a
    stalled loop can never integrate seconds of stale rate into the heading. An
    optional ``deadband_dps`` zeroes tiny rates to stop residual bias creeping the
    heading while the robot sits still (0 = off).
    """

    def __init__(
        self,
        *,
        max_dt_s: float = 0.25,
        deadband_dps: float = 0.0,
        heading_deg: float = 0.0,
    ) -> None:
        self._max_dt_s = float(max_dt_s)
        self._deadband_dps = abs(float(deadband_dps))
        self.heading_deg = float(heading_deg)
        self._last_ts: Optional[float] = None
        #: Samples ignored because ``ok`` was False.
        self.dropped = 0
        #: Resyncs because the gap since the previous sample exceeded ``max_dt_s``.
        self.gaps = 0

    def update(self, sample: ImuSample) -> float:
        """Fold one sample in and return the (unwrapped) heading in degrees."""
        if not sample.ok:
            self.dropped += 1
            return self.heading_deg
        ts = float(sample.ts)
        if self._last_ts is None:
            self._last_ts = ts
            return self.heading_deg
        dt = ts - self._last_ts
        self._last_ts = ts
        if dt <= 0.0:
            return self.heading_deg
        if dt > self._max_dt_s:
            self.gaps += 1
            return self.heading_deg
        rate = float(sample.yaw_rate_dps)
        if abs(rate) < self._deadband_dps:
            rate = 0.0
        self.heading_deg += rate * dt
        return self.heading_deg

    @property
    def heading_wrapped_deg(self) -> float:
        """The heading wrapped into ``(-180, 180]``."""
        h = (self.heading_deg + 180.0) % 360.0 - 180.0
        return 180.0 if h == -180.0 else h

    def reset(self, heading_deg: float = 0.0) -> None:
        """Zero (or preset) the heading and forget the previous timestamp."""
        self.heading_deg = float(heading_deg)
        self._last_ts = None


YawScript = Union[float, Sequence[float], Callable[[float], float]]


class FakeImu:
    """A pure-software :class:`Imu` for laptop tests and the fake reactive wiring.

    The yaw rate is *scripted*: ``yaw_rates`` may be a number (constant), a list
    (served in order; the last value repeats once exhausted) or a callable
    ``f(ts) -> rate``; :meth:`set_yaw_rate` overrides with a constant at any time.
    Every read stamps ``ts`` from the injected ``monotonic`` clock (or the real
    one). ``gyro_dps`` places the yaw rate on the Z axis, ``accel_g`` is a chip-up
    rest ``(0, 0, 1)``, ``temp_c`` a plausible 25 °C. :meth:`set_ok` simulates a
    bus fault (``ok=False``, zeros) so callers' dropout paths can be tested.
    """

    def __init__(
        self,
        yaw_rates: YawScript = 0.0,
        *,
        bias_dps: Tuple[float, float, float] = (0.0, 0.0, 0.0),
        monotonic: Optional[Callable[[], float]] = None,
    ) -> None:
        self._script: YawScript = yaw_rates
        self._queue: List[float] = []
        if not callable(yaw_rates) and not isinstance(yaw_rates, (int, float)):
            self._queue = [float(v) for v in yaw_rates]
        self._clock: Callable[[], float] = monotonic if monotonic is not None else time.monotonic
        self._bias = (float(bias_dps[0]), float(bias_dps[1]), float(bias_dps[2]))
        self.ok = True
        self.closed = False
        self.calibrated = False
        self.calibrate_calls = 0
        self.read_count = 0
        self.last: Optional[ImuSample] = None

    # -- scripting ----------------------------------------------------------
    def set_yaw_rate(self, rate_dps: float) -> None:
        """Serve a constant yaw rate from now on."""
        self._script = float(rate_dps)
        self._queue = []

    def set_ok(self, ok: bool) -> None:
        """Simulate (or clear) an I2C fault: reads return ``ok=False`` while False."""
        self.ok = bool(ok)

    def _next_rate(self, ts: float) -> float:
        if callable(self._script):
            return float(self._script(ts))
        if isinstance(self._script, (int, float)):
            return float(self._script)
        if not self._queue:
            return 0.0
        if len(self._queue) > 1:
            return self._queue.pop(0)
        return self._queue[0]  # last value repeats

    # -- Imu surface --------------------------------------------------------
    def read(self) -> ImuSample:
        self.read_count += 1
        ts = self._clock()
        if self.closed or not self.ok:
            sample = ImuSample(0.0, (0.0, 0.0, 0.0), (0.0, 0.0, 0.0), 0.0, ts, False)
        else:
            rate = self._next_rate(ts)
            sample = ImuSample(rate, (0.0, 0.0, rate), (0.0, 0.0, 1.0), 25.0, ts, True)
        self.last = sample
        return sample

    def calibrate_gyro(
        self,
        samples: int = 200,
        interval_s: float = 0.005,
        sleep: Optional[Callable[[float], None]] = None,
    ) -> Tuple[float, float, float]:
        self.calibrate_calls += 1
        self.calibrated = True
        return self._bias

    @property
    def gyro_bias_dps(self) -> Tuple[float, float, float]:
        return self._bias

    def stats(self) -> dict:
        return {
            "reads": self.read_count,
            "read_errors": 0,
            "calibrations": self.calibrate_calls,
            "calibration_bad_reads": 0,
            "bias_dps": list(self._bias),
            "who_am_i": MPU6050_DEFAULT_ADDRESS,
        }

    def close(self) -> None:
        self.closed = True


__all__ = [
    "ImuSample",
    "Imu",
    "Mpu6050Imu",
    "YawIntegrator",
    "FakeImu",
    "MPU6050_DEFAULT_ADDRESS",
    "GYRO_RANGES",
    "ACCEL_RANGES",
    "KNOWN_WHO_AM_I",
    "DLPF_CFG_44HZ",
    "DATA_BLOCK_LEN",
    "REG_SMPLRT_DIV",
    "REG_CONFIG",
    "REG_GYRO_CONFIG",
    "REG_ACCEL_CONFIG",
    "REG_PWR_MGMT_1",
    "REG_WHO_AM_I",
    "REG_ACCEL_XOUT_H",
    "REG_GYRO_ZOUT_L",
]
