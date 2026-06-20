"""``yalp see`` — the camera-to-Claude visual Q&A subcommand.

Registered with the CLI via the documented feature-module contract: this module
exposes ``add_parser(subparsers)`` and ``run(args) -> int``, and is listed in
``yalp.cli.FEATURE_MODULES``.

    yalp see                         # "What do you see?" from the live camera
    yalp see how many cups are here  # ask a free-text question
    yalp see --image photo.jpg       # describe a still on disk instead
"""

from __future__ import annotations

from . import vision


def add_parser(subparsers) -> None:
    """Register the ``see`` subcommand and its flags."""
    parser = subparsers.add_parser(
        "see",
        help="Grab a camera still (or --image) and describe it with Claude.",
        description=(
            "Grab a still from the camera (or load --image), send it to a Claude "
            "vision model, and print what it sees. Pass a free-text question as "
            "trailing words; the default question is \"What do you see?\"."
        ),
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
        help="Optional question about the scene, e.g. 'how many cups are here'.",
    )
    parser.set_defaults(handler=run)


def run(args) -> int:
    """Handler for ``yalp see``. Returns a process exit code."""
    question = " ".join(getattr(args, "question", []) or []).strip() or None
    text = vision.see(image_path=args.image, question=question)
    print(vision.format_answer(text, question))
    return 0


__all__ = ["add_parser", "run"]
