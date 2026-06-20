#!/usr/bin/env python3
"""Embed yalp's markdown spec docs into index.html so the whole spec is one
self-contained, shareable page — every referenced doc opens inline, no page
ever leaves the file.

The docs live in subdirectories under docs/ (product/, technical/, archive/),
but each is embedded under its BASENAME only (e.g. "software-spec.md",
"architecture.md", "initial-plan.md") so that backticked cross-links inside the
docs resolve to the embedded copies.

All paths are resolved RELATIVE TO THIS FILE'S LOCATION (the docs/ directory)
via ``pathlib.Path(__file__).parent``, so the script can be run from anywhere.

Usage: python3 docs/build.py   (run after editing any .md doc under docs/)
"""
import os
import re
import pathlib

ROOT = pathlib.Path(__file__).parent
HTML = ROOT / "index.html"

# Source paths (read from the subdir) -> embedded under basename only.
DOCS = [
    "product/product-vision.md",
    "product/product-spec.md",
    "technical/architecture.md",
    "technical/hardware.md",
    "technical/software-spec.md",
    "technical/roadmap.md",
    "archive/initial-plan.md",
]


def main():
    html = HTML.read_text()
    blocks = []
    missing = []
    for rel in DOCS:
        p = ROOT / rel
        name = os.path.basename(rel)  # embed under basename so cross-links resolve
        if not p.exists():
            print("  skip (missing):", rel)
            missing.append(rel)
            continue
        raw = p.read_text()
        # defuse any literal </script that would prematurely close the embed block
        raw = raw.replace("</script", "<\\/script")
        blocks.append(
            '<script type="text/markdown" data-name="%s">\n%s\n</script>' % (name, raw)
        )
    payload = "<!--DOCS:START-->\n" + "\n".join(blocks) + "\n<!--DOCS:END-->"
    new, n = re.subn(
        r"<!--DOCS:START-->.*?<!--DOCS:END-->",
        lambda m: payload,
        html,
        flags=re.S,
    )
    if n == 0:
        raise SystemExit(
            "ERROR: could not find the <!--DOCS:START-->...<!--DOCS:END--> markers in index.html"
        )
    HTML.write_text(new)
    print("embedded %d docs into index.html" % len(blocks))
    for rel in DOCS:
        if rel not in missing:
            print("  +", os.path.basename(rel), "  (from %s)" % rel)
    if missing:
        print("WARNING: %d doc(s) missing and skipped: %s" % (len(missing), ", ".join(missing)))


if __name__ == "__main__":
    main()
