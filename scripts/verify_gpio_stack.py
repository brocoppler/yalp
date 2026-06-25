#!/usr/bin/env python3
"""verify_gpio_stack.py — Verify that gpiozero is using the lgpio/native pin factory.

Run this on the Raspberry Pi after provisioning with pi_setup.sh:

    python3 scripts/verify_gpio_stack.py

Exit codes:
    0 — pin factory is lgpio/native and RPi.GPIO is absent from sys.modules
    1 — wrong pin factory or RPi.GPIO is loaded (see error message)
    2 — gpiozero is not installed (run: pip install 'yalp[pi]')

The classify_factory() function is kept pure/testable — it accepts a module name
string and returns 'ok' | 'rpigpio' | 'unknown' without touching any hardware.
"""
from __future__ import annotations

import sys

HARDWARE_DOC = "docs/technical/hardware.md"


# ---------------------------------------------------------------------------
# Public testable helper — no I/O, no imports
# ---------------------------------------------------------------------------

def classify_factory(module_name: str) -> str:
    """Classify a pin factory by its module name string.

    Parameters
    ----------
    module_name:
        The value of ``type(Device.pin_factory).__module__`` as returned by
        gpiozero (e.g. ``'gpiozero.pins.lgpio'``, ``'gpiozero.pins.rpigpio'``).

    Returns
    -------
    'ok'       — factory is lgpio or the native gpiozero backend (good)
    'rpigpio'  — factory is RPi.GPIO-based (not allowed; see hardware.md)
    'unknown'  — factory is something else entirely
    """
    lower = module_name.lower()
    if "lgpio" in lower or "native" in lower or "mock" in lower:
        # 'mock' is acceptable in unit-test/simulation contexts on a laptop
        return "ok"
    if "rpigpio" in lower or "rpi.gpio" in lower or "rpi_gpio" in lower:
        return "rpigpio"
    return "unknown"


# ---------------------------------------------------------------------------
# Main verification logic
# ---------------------------------------------------------------------------

def main() -> int:
    # --- 1. Lazy import of gpiozero ---
    try:
        from gpiozero import Device  # type: ignore[import]
    except ImportError:
        print(
            "gpiozero is not installed in this environment.\n"
            "Install the [pi] extra to use this script on a Raspberry Pi:\n"
            "    pip install 'yalp[pi]'\n"
            "(Running on a laptop without hardware is expected — this script is "
            "intended for Pi bring-up verification only.)"
        )
        return 2

    # --- 2. Ensure the pin factory is initialised ---
    Device.ensure_pin_factory()

    factory_module = type(Device.pin_factory).__module__
    print(f"Pin factory module: {factory_module}")

    # --- 3. Classify the factory ---
    classification = classify_factory(factory_module)

    errors: list[str] = []

    if classification == "rpigpio":
        errors.append(
            f"Pin factory '{factory_module}' uses RPi.GPIO, which is not allowed.\n"
            f"See {HARDWARE_DOC} for the required lgpio-based setup."
        )
    elif classification == "unknown":
        errors.append(
            f"Pin factory '{factory_module}' is unrecognised.\n"
            f"Expected an lgpio-backed factory. See {HARDWARE_DOC}."
        )

    # --- 4. Assert RPi.GPIO is absent from sys.modules ---
    if "RPi.GPIO" in sys.modules:
        errors.append(
            "RPi.GPIO is loaded in sys.modules, which indicates an unexpected "
            "dependency.\n"
            f"See {HARDWARE_DOC} for the required driver stack."
        )

    if errors:
        print("\nERROR: GPIO stack verification FAILED:", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    print("GPIO stack OK — lgpio/native factory confirmed, RPi.GPIO absent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
