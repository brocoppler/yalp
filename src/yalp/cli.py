"""yalp command-line entrypoint.

A small argparse-based dispatcher with an extensible subcommand registry. Later
waves register real subcommands (``see``, ``agent``) by adding a feature module
that exposes two functions:

    def add_parser(subparsers) -> None:   # register the subcommand + its flags
        p = subparsers.add_parser("see", help="...")
        p.add_argument(...)
        p.set_defaults(handler=run)       # handler(args) -> int

    def run(args) -> int:                 # the handler; returns an exit code
        ...

then listing that module in ``FEATURE_MODULES`` below. Today there are no
feature subcommands yet — only ``--version`` and a friendly ``--help``.
"""

from __future__ import annotations

import argparse
import importlib
from typing import Optional, Sequence

from . import __version__

# Dotted module paths for feature modules that expose ``add_parser(subparsers)``.
# Populated by later waves, e.g. "yalp.deliberative.agent".
FEATURE_MODULES: list[str] = [
    "yalp.deliberative.see",
    "yalp.deliberative.agent_cli",
    "yalp.reactive.follow_cli",
    "yalp.reactive.hwtest_cli",
    "yalp.reactive.reactive_cli",
    "yalp.reactive.bench_cli",
]


def build_parser() -> argparse.ArgumentParser:
    """Construct the top-level parser and wire up any registered subcommands."""
    parser = argparse.ArgumentParser(
        prog="yalp",
        description="yalp — a two-loop hobby robot brain (reactive + Claude deliberative).",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"yalp {__version__}",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")

    for dotted in FEATURE_MODULES:
        module = importlib.import_module(dotted)
        module.add_parser(subparsers)  # type: ignore[attr-defined]

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Parse arguments and dispatch to the selected subcommand's handler."""
    parser = build_parser()
    args = parser.parse_args(argv)

    handler = getattr(args, "handler", None)
    if handler is None:
        # No subcommand given (or none registered yet): show help.
        parser.print_help()
        return 0
    return int(handler(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
