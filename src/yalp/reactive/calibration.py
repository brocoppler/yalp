"""Persisted motor calibration — turn physical reality into honest config.

The open-loop ``DRIVE_GOAL`` timing in :mod:`yalp.reactive.real_backend` converts
a distance/angle target into a *timed* command using two physical constants:

* ``max_speed_mps`` — the robot's forward speed at full throttle (m/s), and
* ``turn_rate_dps`` — its in-place rotation rate at full throttle (deg/s).

Those were hand-guessed (``0.5`` / ``120``). Until they're *measured* the robot
will systematically over/under-shoot every drive and turn. On top of that a
motor can simply be **wired backwards** (or the left/right pair swapped in
strength), which must be fixable in software rather than by re-soldering.

This module captures all of that as a tiny JSON-serialisable dataclass that the
``yalp calibrate`` CLI writes and the real backend reads:

* ``left_invert`` / ``right_invert`` — flip a backwards-wired wheel,
* ``left_trim`` / ``right_trim`` — per-wheel magnitude scaling (drift fix),
* ``max_speed_mps`` / ``turn_rate_dps`` — the measured motion model, and
* ``duty_deadband`` / ``turn_duty_deadband`` — the commanded-duty deadband below
  which the drive motors do not overcome stiction (measured 2026-07-17; see the
  speed-model note in :mod:`yalp.config`).

It is **pure stdlib** (``json`` + ``dataclasses`` + ``pathlib``) so it imports
cleanly on a laptop with no hardware libraries present.

Default location is ``~/.config/yalp/calibration.json``; override it with the
``YALP_CALIBRATION_PATH`` environment variable (handy for tests and for keeping
per-robot calibrations side by side).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Optional, Union

#: Environment variable that overrides the default calibration file location.
ENV_VAR = "YALP_CALIBRATION_PATH"

PathLike = Union[str, "os.PathLike[str]", Path]


def default_path() -> Path:
    """Return the calibration file path (``YALP_CALIBRATION_PATH`` or the default).

    The default is ``~/.config/yalp/calibration.json``. Both the env override and
    the default are ``~``-expanded so they resolve to absolute paths.
    """
    override = os.environ.get(ENV_VAR)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "yalp" / "calibration.json"


def _resolve(path: Optional[PathLike]) -> Path:
    """Resolve an optional path argument to a concrete file path."""
    if path is None:
        return default_path()
    return Path(path).expanduser()


@dataclass
class MotorCalibration:
    """Per-robot motor calibration (miswire fixes + the open-loop motion model).

    Every field has a sensible default so an un-calibrated robot still runs with
    the historical hand-guessed constants. (The task spec lists the fields with
    ``max_speed_mps`` / ``turn_rate_dps`` *after* the trim fields; Python forbids
    a non-default field after a defaulted one, so all fields carry defaults —
    the names, types, and meaning are preserved.)

    Attributes
    ----------
    left_invert / right_invert:
        Flip a wheel whose motor is wired backwards (negate its commanded sign).
    left_trim / right_trim:
        Per-wheel magnitude multiplier (``1.0`` = unchanged) to correct a robot
        that veers because one side is mechanically/electrically stronger.
    max_speed_mps:
        Measured forward speed at FULL throttle (duty 1.0), in m/s. The open-loop
        speed model is ``v = max(0, gain * (duty - duty_deadband))`` with
        ``gain = max_speed_mps / (1 - duty_deadband)``, so this stays the speed at
        duty 1.0 regardless of the deadband (config.DRIVE_DUTY_DEADBAND note).
    turn_rate_dps:
        Measured in-place rotation rate at full throttle (deg/s). Known to
        over-predict small turns badly below ~duty 0.5 (stiction) — see
        ``config.TURN_STICTION_DUTY``; not corrected here.
    duty_deadband:
        Commanded-duty motor deadband for the SPEED model (below it the drive
        motors stall under load). Field default ``0.27`` (the 2026-07-17 measured
        midpoint). NOTE the field default (0.27) and the back-compat default that
        :meth:`from_dict` applies to a calibration file MISSING this key (``0.0``)
        differ on purpose — see :meth:`from_dict`.
    turn_duty_deadband:
        Reserved measured turn deadband for future tuning (``0.0`` = not yet
        modeled; the rotation timing is unchanged).
    straight_bias_fwd / straight_bias_rev:
        LEARNED feed-forward steering bias (duty) for straight drives, forward and
        reverse: added to the left wheel and subtracted from the right before the
        closed-loop heading hold acts (``ReactiveTickCore._heading_correction``).
        Written by the reactive layer itself after each completed straight drive
        with a live camera heading estimate (``config.TRIM_LEARNING_*``), so the
        robot adapts to a new floor without a manual recalibration. ``0.0`` = no
        bias (the default and the pre-2026-09-15 behaviour).
    """

    left_invert: bool = False
    right_invert: bool = False
    left_trim: float = 1.0
    right_trim: float = 1.0
    max_speed_mps: float = 0.5
    turn_rate_dps: float = 120.0
    duty_deadband: float = 0.27
    turn_duty_deadband: float = 0.0
    straight_bias_fwd: float = 0.0
    straight_bias_rev: float = 0.0

    def to_dict(self) -> dict:
        """Return a JSON-ready dict with normalised (bool/float) values."""
        return {
            "left_invert": bool(self.left_invert),
            "right_invert": bool(self.right_invert),
            "left_trim": float(self.left_trim),
            "right_trim": float(self.right_trim),
            "max_speed_mps": float(self.max_speed_mps),
            "turn_rate_dps": float(self.turn_rate_dps),
            "duty_deadband": float(self.duty_deadband),
            "turn_duty_deadband": float(self.turn_duty_deadband),
            "straight_bias_fwd": float(self.straight_bias_fwd),
            "straight_bias_rev": float(self.straight_bias_rev),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "MotorCalibration":
        """Build a calibration from a dict, ignoring unknown keys.

        Back-compat for ``duty_deadband``: a calibration file written BEFORE the
        deadband field existed (e.g. the live robot's ``{max_speed_mps: 0.29,
        right_trim: 0.90}``) was measured under the OLD pure-linear model
        ``v = max_speed_mps * duty``. Retroactively imposing the ``0.27`` field
        default on such a file would nearly halve its speed at the operating duty
        (duty 0.45: ``0.29 * 0.45 = 0.13`` m/s -> ``0.07`` m/s) and make every
        open-loop drive overshoot ~2x. So a MISSING key defaults to ``0.0`` — the
        deadband is disabled and the model degenerates to the exact old linear
        behaviour, reproducing measured reality (duty 0.45 with max_speed_mps 0.29
        -> ~0.13 m/s, unchanged). A file that DOES carry the key uses it verbatim.
        """
        known = {f.name for f in fields(cls)}
        kwargs = {k: v for k, v in data.items() if k in known}
        if "duty_deadband" not in data:
            kwargs["duty_deadband"] = 0.0
        return cls(**kwargs)

    def save(self, path: Optional[PathLike] = None) -> Path:
        """Write this calibration as pretty JSON, creating parent dirs.

        Returns the path written (the default path when ``path`` is ``None``).
        """
        target = _resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.to_dict(), indent=2) + "\n")
        return target

    @classmethod
    def load(cls, path: Optional[PathLike] = None) -> "MotorCalibration":
        """Load a calibration from JSON (raises if the file is missing/invalid)."""
        target = _resolve(path)
        return cls.from_dict(json.loads(target.read_text()))


def load_if_present(path: Optional[PathLike] = None) -> Optional[MotorCalibration]:
    """Load the calibration if the file exists and is valid, else return ``None``.

    This is the forgiving variant the real backend uses at startup: a missing or
    corrupt file is **not** an error — the caller simply falls back to defaults.
    """
    target = _resolve(path)
    if not target.exists():
        return None
    try:
        return MotorCalibration.load(target)
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        return None


__all__ = [
    "MotorCalibration",
    "default_path",
    "load_if_present",
    "ENV_VAR",
]
