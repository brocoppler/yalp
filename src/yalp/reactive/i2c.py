"""Tiny shared I2C helpers for the reactive layer's I2C sensor drivers.

Both I2C drivers — the :mod:`~yalp.reactive.imu` (MPU-6050 gyro) and the
:mod:`~yalp.reactive.power_monitor` (INA219 pack monitor) — need exactly the same
two things: a way to open ``/dev/i2c-<bus>`` through whichever SMBus binding the
Pi has, and 16-bit big-endian byte twiddling. They live here so the two drivers
stay independent of each other and so the import policy is stated once:

**This module imports NOTHING hardware-specific at import time.** ``smbus2``
(preferred) / ``smbus`` (the older python3-smbus binding, same method names) are
imported *lazily* inside :func:`open_smbus`, which is only called when a driver is
*instantiated* on the Pi. Importing this module — or either driver — on a laptop
or in CI with neither binding installed is fine; tests inject a fake bus object
instead (``_smbus=`` on the driver constructors).

**Pi 5 setup (once).** Enable the I2C-1 controller (GPIO2 = SDA1, physical pin 3;
GPIO3 = SCL1, physical pin 5)::

    sudo raspi-config nonint do_i2c 0      # 0 = enable; creates /dev/i2c-1
    sudo apt install -y i2c-tools python3-smbus2   # or: pip install smbus2
    i2cdetect -y 1                          # expect 40 (INA219) and 68 (MPU-6050)

If the venv was NOT created with ``--system-site-packages``, ``pip install smbus2``
inside the venv (it is pure Python, no build step). The user must be in the
``i2c`` group (Raspberry Pi OS adds the default user already).
"""

from __future__ import annotations

from typing import Any

__all__ = ["open_smbus", "to_signed16", "u16_from_bytes", "bytes_from_u16"]


def open_smbus(bus: int, *, purpose: str = "an I2C device") -> Any:
    """Open ``/dev/i2c-<bus>`` via ``smbus2`` (preferred) or ``smbus``.

    Imports lazily so callers stay laptop-importable. Raises :class:`RuntimeError`
    (with install/enable hints) if neither binding is importable or the bus device
    cannot be opened — e.g. I2C not enabled in ``raspi-config``.
    """
    smbus_mod: Any = None
    errors = []
    for name in ("smbus2", "smbus"):
        try:
            smbus_mod = __import__(name)
            break
        except Exception as exc:  # ImportError normally; be defensive
            errors.append(f"{name}: {exc!r}")
    if smbus_mod is None:
        raise RuntimeError(
            f"an SMBus binding is required to talk to {purpose} over I2C but "
            f"neither 'smbus2' nor 'smbus' could be imported ({'; '.join(errors)}). "
            "On the Raspberry Pi: `sudo apt install python3-smbus2` or "
            "`pip install smbus2` (see yalp/reactive/i2c.py)."
        )
    try:
        return smbus_mod.SMBus(int(bus))
    except Exception as exc:
        raise RuntimeError(
            f"could not open I2C bus {bus} for {purpose} ({exc!r}). Is I2C enabled? "
            "Run `sudo raspi-config nonint do_i2c 0` and check `ls /dev/i2c-*` "
            "(see yalp/reactive/i2c.py)."
        ) from exc


def to_signed16(value: int) -> int:
    """Interpret a 16-bit unsigned register value as two's-complement signed."""
    value &= 0xFFFF
    return value - 0x10000 if value & 0x8000 else value


def u16_from_bytes(msb: int, lsb: int) -> int:
    """Combine a big-endian ``(MSB, LSB)`` byte pair into an unsigned 16-bit int."""
    return ((int(msb) & 0xFF) << 8) | (int(lsb) & 0xFF)


def bytes_from_u16(value: int) -> list:
    """Split an unsigned 16-bit int into a big-endian ``[MSB, LSB]`` list."""
    value &= 0xFFFF
    return [(value >> 8) & 0xFF, value & 0xFF]
