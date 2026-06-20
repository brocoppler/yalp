#!/usr/bin/env python3
"""The magic moment, standalone: open the camera, ask Claude what it sees, print it.

This is the motivating first win (roadmap Step 4). Run it on a laptop with no
robot — the built-in webcam (or a synthetic frame) stands in for the camera:

    python3 scripts/magic_moment.py
    python3 scripts/magic_moment.py how many people are in the room?
    python3 scripts/magic_moment.py --image tests/assets/sample.jpg

If no ANTHROPIC_API_KEY is configured it prints a friendly pointer to SETUP.md
instead of crashing.

It works whether or not yalp is ``pip install``-ed: we add ``src/`` to the path
as a fallback so a fresh clone can run it directly.
"""

from __future__ import annotations

import argparse
import os
import sys

# Make ``import yalp`` work even from a fresh, uninstalled clone.
_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "src")
if os.path.isdir(_SRC) and _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from yalp.deliberative import vision  # noqa: E402  (after sys.path tweak)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="magic_moment",
        description="Grab a camera still and have Claude describe it.",
    )
    parser.add_argument(
        "--image",
        metavar="PATH",
        default=None,
        help="Describe this image file instead of grabbing from the camera.",
    )
    parser.add_argument(
        "question",
        nargs="*",
        help="Optional question about the scene (default: 'What do you see?').",
    )
    args = parser.parse_args(argv)

    question = " ".join(args.question).strip() or None

    source = args.image if args.image else "the camera"
    print(f"\n👁  yalp is looking at {source} ...\n")

    text = vision.see(image_path=args.image, question=question)
    print(vision.format_answer(text, question))
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
