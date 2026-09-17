"""``python -m yalp`` — the same entrypoint as the ``yalp`` console script.

Exists so a launcher can run the CLI by *interpreter path* without depending on
the venv's ``bin/yalp`` shim: ``yalp session start`` spawns
``sys.executable -u -m yalp reactive ...`` and the systemd unit's ``ExecStart``
does the same. Guarded by ``__name__`` so importing this module never runs the
CLI.
"""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
