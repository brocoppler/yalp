"""Hermetic, subprocess-based import-isolation assertions.

Several tests need to prove that importing a hardware-facing module does **not**
drag a heavy GPIO library (``gpiozero`` and friends) into the interpreter as an
import *side effect* — the hardware libs must only be imported lazily, inside the
hardware code paths that actually touch a pin.

The obvious way to assert that is ``"gpiozero" not in sys.modules`` after the
import. But that reads a **global, process-wide** condition, and it is only true
where gpiozero is never installed (a laptop / CI). On a Raspberry Pi gpiozero IS
installed, and any *earlier* test in the same interpreter that legitimately
imported it leaves it in ``sys.modules`` — so the in-process assertion
false-fails purely on test ordering (all six affected tests pass in isolation,
fail in the full suite on the Pi).

Running the import in a **fresh subprocess** makes the guarantee hermetic and
order-independent: a brand-new interpreter has nothing in ``sys.modules`` except
what the import itself pulls in, so the assertion means exactly "importing X does
not pull in the forbidden lib", on both laptop and Pi, in any order.

This is a plain helper module (imported directly by the tests); the ``tests``
package deliberately keeps this kind of machinery out of ``conftest.py``.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterable, Sequence


def _child_env() -> dict[str, str]:
    """Environment for the child interpreter.

    Propagates the parent's environment (so the conftest's telemetry-off vars and
    anything else the test set carry through) and, critically, prepends the
    directory that contains the *parent's* ``yalp`` package to ``PYTHONPATH``.

    The worktree's ``yalp`` is normally on the path only via ``PYTHONPATH=src``;
    a bare subprocess would otherwise resolve ``yalp`` to whatever editable
    install is registered (which can point at a *different* checkout). Deriving
    the path from the already-imported package guarantees the child imports the
    exact same ``yalp`` the test is exercising.
    """
    import yalp  # local import: avoids import cost / side effects at module load

    src_dir = os.path.dirname(os.path.dirname(os.path.abspath(yalp.__file__)))
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    parts = [src_dir] + ([existing] if existing else [])
    env["PYTHONPATH"] = os.pathsep.join(parts)
    return env


def run_isolated(program: str) -> subprocess.CompletedProcess[str]:
    """Run ``program`` in a fresh interpreter with the worktree ``src`` on the path."""
    return subprocess.run(
        [sys.executable, "-c", program],
        env=_child_env(),
        capture_output=True,
        text=True,
        check=False,
    )


def assert_isolated_program_succeeds(program: str, *, what: str) -> None:
    """Run ``program`` in a fresh interpreter and assert it exits 0.

    On failure the child's exit code, stdout and stderr are included in the
    assertion message so a leaked import (or an outright error) is diagnosable.
    """
    proc = run_isolated(program)
    assert proc.returncode == 0, (
        f"{what}\n"
        f"child exit code: {proc.returncode}\n"
        f"--- child stdout ---\n{proc.stdout}"
        f"--- child stderr ---\n{proc.stderr}"
    )


def assert_import_leaves_module_unloaded(
    import_target: str, forbidden: str | Iterable[str]
) -> None:
    """Assert that ``import <import_target>`` does not load the forbidden module(s).

    Runs, in a fresh subprocess::

        import sys, <import_target>
        raise SystemExit(1 if "<forbidden>" in sys.modules else 0)

    and asserts the child exits 0. ``forbidden`` may be a single module name or an
    iterable of names (all checked in the one subprocess, so one test stays one
    subprocess). The child's stderr is included in the failure message.
    """
    if isinstance(forbidden, str):
        forbidden_names: Sequence[str] = (forbidden,)
    else:
        forbidden_names = tuple(forbidden)

    program = (
        "import sys\n"
        f"import {import_target}\n"
        f"_forbidden = {list(forbidden_names)!r}\n"
        "_leaked = [m for m in _forbidden if m in sys.modules]\n"
        "if _leaked:\n"
        "    sys.stderr.write('leaked into sys.modules: ' + ', '.join(_leaked) + '\\n')\n"
        "raise SystemExit(1 if _leaked else 0)\n"
    )
    assert_isolated_program_succeeds(
        program,
        what=(
            f"importing {import_target!r} in a fresh interpreter pulled in a "
            f"forbidden hardware library (one of {list(forbidden_names)}); "
            "hardware libs must only be imported lazily."
        ),
    )
