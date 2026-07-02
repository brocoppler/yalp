"""``yalp audio`` — inspect and select audio INPUT devices.

The robot captures voice with :class:`yalp.voice.microphone.Microphone`, which
picks WHICH input device via ``YALP_AUDIO_INPUT_DEVICE`` (see
:data:`yalp.config.AUDIO_INPUT_DEVICE`). On a laptop the empty default uses the
system mic; on the Pi you set it to the Logitech C270 webcam's built-in mono mic
(an index or a name substring like ``C270``) so voice input needs zero new
hardware.

    yalp audio --list        # list input-capable devices + the current selection

This command is the discovery tool: run it to see the indexes / names you can
put in ``YALP_AUDIO_INPUT_DEVICE``. ``sounddevice`` (the ``[voice]`` extra) is
imported LAZILY inside the handler so this module is always importable — with a
friendly install hint printed when the extra is missing.

Registered with the CLI via the feature-module contract (``add_parser`` + ``run``
listed in ``yalp.cli.FEATURE_MODULES``).
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# CLI registration
# ---------------------------------------------------------------------------

def add_parser(subparsers) -> None:
    """Register the ``audio`` subcommand and its flags."""
    parser = subparsers.add_parser(
        "audio",
        help="Inspect audio input devices (use --list to choose YALP_AUDIO_INPUT_DEVICE).",
        description=(
            "Discover which microphone yalp will use. `--list` prints every "
            "input-capable audio device (index, name, channels, default marker) "
            "and the currently-configured selection so you can set "
            "YALP_AUDIO_INPUT_DEVICE to an index or a name substring."
        ),
    )
    parser.add_argument(
        "--list",
        dest="list_devices",
        action="store_true",
        help="List input-capable audio devices and the current selection.",
    )
    parser.set_defaults(handler=run)


def run(args) -> int:
    """Handler for ``yalp audio``. Returns a process exit code."""
    # Today the only action is listing; default to it so a bare ``yalp audio``
    # is still useful rather than silent.
    _ = bool(getattr(args, "list_devices", False))
    return _list_devices()


# ---------------------------------------------------------------------------
# --list
# ---------------------------------------------------------------------------

def _list_devices() -> int:
    """Print input-capable devices and the current selection. Returns exit code."""
    try:
        import sounddevice as sd
    except Exception as exc:  # ImportError or any transitive load failure
        print(
            "Audio device listing needs the optional 'voice' extra "
            f"(sounddevice), which is not available ({exc}).\n"
            "Install it with:\n"
            "    pip install 'yalp[voice]'\n"
            "On Linux/Pi you may also need PortAudio: "
            "`sudo apt install libportaudio2`."
        )
        return 1

    from .microphone import list_input_devices

    try:
        devices = list_input_devices(sd)
    except Exception as exc:  # PortAudio host error while enumerating
        print(f"Could not query audio devices via sounddevice: {exc}")
        return 1

    print("Input-capable audio devices:")
    if devices:
        for dev in devices:
            marker = "  *default" if dev["is_default"] else ""
            print(
                f"  [{dev['index']}] {dev['name']} "
                f"({dev['max_input_channels']} in){marker}"
            )
    else:
        print("  (none found)")

    print()
    _print_current_selection(sd, devices)
    return 0


def _print_current_selection(sd, devices) -> None:
    """Print the configured YALP_AUDIO_INPUT_DEVICE and what it resolves to."""
    from yalp.config import AUDIO_INPUT_DEVICE
    from .microphone import _resolve_input_device

    selection = AUDIO_INPUT_DEVICE
    if not selection.strip():
        print(
            "Current selection (YALP_AUDIO_INPUT_DEVICE): '' — system default "
            "input device."
        )
        return

    print(f"Current selection (YALP_AUDIO_INPUT_DEVICE): {selection!r}")
    try:
        index = _resolve_input_device(selection, sd)
    except ValueError:
        print(
            "  ⚠ does not match any current input device (see the list above). "
            "Set it to an index or a name substring from that list, or '' for "
            "the system default."
        )
        return

    match = next((d for d in devices if d["index"] == index), None)
    if match is not None:
        print(f"  → resolves to [{match['index']}] {match['name']}")
    else:  # pragma: no cover - resolved index but not input-capable in listing
        print(f"  → resolves to device index {index}")


__all__ = ["add_parser", "run"]
