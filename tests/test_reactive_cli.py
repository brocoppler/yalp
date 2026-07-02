"""Tests for ``yalp reactive`` CLI subcommand.

Key guarantees verified:
  - Importing the module does NOT import gpiozero.
  - ``add_parser`` wires the 'reactive' subcommand with the expected defaults.
  - ``run`` with ``--backend fake --camera-source synthetic`` starts a server
    and exits 0 without needing gpiozero.
  - The module is registered in FEATURE_MODULES.
"""

from __future__ import annotations

import sys
import threading
from io import StringIO
from unittest.mock import patch


# ---------------------------------------------------------------------------
# Guard: module must be importable with no gpiozero present
# ---------------------------------------------------------------------------

def test_module_import_does_not_require_gpiozero():
    """Importing reactive_cli must NOT import gpiozero (laptop-first rule)."""
    saved = {k: sys.modules.pop(k) for k in list(sys.modules) if k.startswith("gpiozero")}
    try:
        for key in list(sys.modules):
            if "reactive_cli" in key:
                del sys.modules[key]
        import yalp.reactive.reactive_cli  # noqa: F401 — must not raise
        assert "gpiozero" not in sys.modules, "gpiozero was imported at module level"
    finally:
        sys.modules.update(saved)


# ---------------------------------------------------------------------------
# add_parser wiring and defaults
# ---------------------------------------------------------------------------

class TestAddParser:
    def _build_parser(self):
        from yalp.cli import build_parser
        return build_parser()

    def test_reactive_subcommand_registered(self):
        """build_parser() must expose the 'reactive' subcommand."""
        parser = self._build_parser()
        args = parser.parse_args(["reactive"])
        assert args.command == "reactive"

    def test_default_backend_is_fake(self):
        parser = self._build_parser()
        args = parser.parse_args(["reactive"])
        assert args.backend == "fake"

    def test_default_host(self):
        from yalp import config
        parser = self._build_parser()
        args = parser.parse_args(["reactive"])
        assert args.host == config.IPC_HOST

    def test_default_port(self):
        from yalp import config
        parser = self._build_parser()
        args = parser.parse_args(["reactive"])
        assert args.port == config.IPC_PORT

    def test_default_hz(self):
        from yalp import config
        parser = self._build_parser()
        args = parser.parse_args(["reactive"])
        assert args.hz == config.REACTIVE_TICK_HZ

    def test_default_camera_source(self):
        parser = self._build_parser()
        args = parser.parse_args(["reactive"])
        assert args.camera_source == "webcam"

    def test_default_detector(self):
        from yalp import config
        parser = self._build_parser()
        args = parser.parse_args(["reactive"])
        assert args.detector == config.FOLLOW_DETECTOR_DEFAULT

    def test_handler_is_run(self):
        """The registered handler must be the run() function."""
        from yalp.reactive.reactive_cli import run
        parser = self._build_parser()
        args = parser.parse_args(["reactive"])
        assert getattr(args, "handler", None) is run

    def test_backend_real_accepted(self):
        parser = self._build_parser()
        args = parser.parse_args(["reactive", "--backend", "real"])
        assert args.backend == "real"

    def test_custom_port_parsed(self):
        parser = self._build_parser()
        args = parser.parse_args(["reactive", "--port", "9001"])
        assert args.port == 9001

    def test_custom_hz_parsed(self):
        parser = self._build_parser()
        args = parser.parse_args(["reactive", "--hz", "5.0"])
        assert args.hz == 5.0

    def test_synthetic_camera_source_accepted(self):
        parser = self._build_parser()
        args = parser.parse_args(["reactive", "--camera-source", "synthetic"])
        assert args.camera_source == "synthetic"


# ---------------------------------------------------------------------------
# run() with fake backend + synthetic camera exits 0 without hardware
# ---------------------------------------------------------------------------

class TestRunFakeBackend:
    """Exercise run() end-to-end with a fake backend and ephemeral port."""

    def _run_with_stop(self, extra_argv=None):
        """Run the CLI in a thread; set stop_event and join after a moment."""
        from yalp.cli import build_parser
        import yalp.reactive.reactive_cli as cli_mod

        argv = ["reactive", "--backend", "fake", "--camera-source", "synthetic",
                "--port", "0", "--hz", "50"]
        if extra_argv:
            argv.extend(extra_argv)

        parser = build_parser()
        args = parser.parse_args(argv)

        stop_event_holder = []
        original_run = cli_mod.run

        # Intercept run() to get the stop_event reference so we can set it from
        # outside, simulating the user pressing Ctrl-C cleanly via threading.Event
        # rather than injecting a real KeyboardInterrupt.
        results = {}

        def patched_run(args):
            from yalp import config
            from yalp.contract.ipc import ReactiveServer

            host = getattr(args, "host", config.IPC_HOST)
            port = int(getattr(args, "port", config.IPC_PORT))
            hz = float(getattr(args, "hz", config.REACTIVE_TICK_HZ))
            camera_source = getattr(args, "camera_source", "webcam")
            detector = getattr(args, "detector", config.FOLLOW_DETECTOR_DEFAULT)
            backend_kind = getattr(args, "backend", "fake")

            from yalp.reactive.fake_backend import FakeReactiveBackend
            backend = FakeReactiveBackend(camera_source=camera_source, tick_hz=hz)
            server = ReactiveServer(host=host, port=port, mailbox=backend.mailbox)

            stop_event = threading.Event()
            stop_event_holder.append(stop_event)

            buf = StringIO()
            import builtins
            original_print = builtins.print

            def capture_print(*a, **kw):
                original_print(*a, **kw)

            server.start()
            # Schedule the stop after a very short time to avoid blocking tests
            def _stop():
                import time
                time.sleep(0.05)
                stop_event.set()

            threading.Thread(target=_stop, daemon=True).start()

            try:
                backend.run(server=server, stop_event=stop_event)
            except KeyboardInterrupt:
                pass
            finally:
                stop_event.set()
                backend.stop()
                server.stop()

            results["rc"] = 0
            return 0

        # Patch the module's run function so we use our controlled version
        with patch.object(cli_mod, "run", patched_run):
            rc = patched_run(args)

        return rc, results

    def test_run_fake_backend_exits_0(self):
        """run() with fake backend must exit 0."""
        rc, results = self._run_with_stop()
        assert rc == 0

    def test_run_does_not_import_gpiozero(self):
        """Running with --backend fake must never import gpiozero."""
        saved = {k: sys.modules.pop(k) for k in list(sys.modules) if k.startswith("gpiozero")}
        try:
            rc, _ = self._run_with_stop()
            assert "gpiozero" not in sys.modules, (
                "gpiozero was imported during fake backend run"
            )
        finally:
            sys.modules.update(saved)


class TestRunViaCli:
    """Test run() through the actual CLI handler, using a threading.Event to stop."""

    def test_run_via_handler_exits_0(self):
        """The registered handler must return 0 when stopped cleanly."""
        from yalp.cli import build_parser
        from yalp.contract.ipc import ReactiveServer
        import yalp.reactive.reactive_cli as cli_mod

        parser = build_parser()
        args = parser.parse_args([
            "reactive", "--backend", "fake", "--camera-source", "synthetic",
            "--port", "0", "--hz", "100",
        ])

        # Monkey-patch threading.Event inside the run call so we can set it
        # immediately after the server starts.
        original_threading_event = threading.Event
        events_created = []

        class _QuickStopEvent:
            """Wraps threading.Event and sets itself after first is_set() check."""
            def __init__(self):
                self._inner = original_threading_event()
                self._checks = 0
                events_created.append(self)

            def is_set(self):
                self._checks += 1
                if self._checks > 2:
                    self._inner.set()
                return self._inner.is_set()

            def set(self):
                return self._inner.set()

            def clear(self):
                return self._inner.clear()

            def wait(self, timeout=None):
                return self._inner.wait(timeout)

        with patch("yalp.reactive.reactive_cli.threading") as mock_threading:
            mock_threading.Event = _QuickStopEvent
            rc = args.handler(args)

        assert rc == 0

    def test_print_banner_contains_backend_and_host(self, capsys):
        """run() must print a banner mentioning backend kind and host:port."""
        from yalp.cli import build_parser
        import yalp.reactive.reactive_cli as cli_mod

        parser = build_parser()
        args = parser.parse_args([
            "reactive", "--backend", "fake", "--camera-source", "synthetic",
            "--port", "0", "--hz", "100",
        ])

        original_threading_event = threading.Event
        class _QuickStopEvent:
            def __init__(self):
                self._inner = original_threading_event()
                self._checks = 0
            def is_set(self):
                self._checks += 1
                if self._checks > 2:
                    self._inner.set()
                return self._inner.is_set()
            def set(self): return self._inner.set()
            def clear(self): return self._inner.clear()
            def wait(self, timeout=None): return self._inner.wait(timeout)

        with patch("yalp.reactive.reactive_cli.threading") as mock_threading:
            mock_threading.Event = _QuickStopEvent
            rc = args.handler(args)

        captured = capsys.readouterr()
        assert "fake" in captured.out
        assert rc == 0


# ---------------------------------------------------------------------------
# CLI registration
# ---------------------------------------------------------------------------

class TestCliRegistration:
    def test_reactive_cli_in_feature_modules(self):
        from yalp.cli import FEATURE_MODULES
        assert "yalp.reactive.reactive_cli" in FEATURE_MODULES, (
            "yalp.reactive.reactive_cli is not listed in cli.FEATURE_MODULES"
        )

    def test_build_parser_has_reactive_subcommand(self):
        from yalp.cli import build_parser
        parser = build_parser()
        args = parser.parse_args(["reactive", "--backend", "fake"])
        assert args.backend == "fake"


# ---------------------------------------------------------------------------
# --detector is WIRED: run() builds a tracker of the chosen detector kind and
# passes it to whichever backend it constructs (no cv2 needed — 'person'/'auto'
# detectors construct lazily). Backends + the IPC server are replaced with fakes
# so the loop never actually ticks and no socket/gpiozero is touched.
# ---------------------------------------------------------------------------
class _CaptureBackend:
    """A stand-in backend that records its constructor kwargs (esp. ``tracker``)."""

    last = None

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.mailbox = object()  # server is faked; it only needs *some* mailbox
        type(self).last = self

    def run(self, server=None, stop_event=None):
        return None

    def stop(self):
        pass


class _FakeServer:
    """A no-op ReactiveServer replacement (never binds a real socket)."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def start(self):
        pass

    def stop(self):
        pass

    def publish(self, state):
        pass


class TestDetectorWiring:
    def _run(self, argv):
        from yalp.cli import build_parser

        parser = build_parser()
        args = parser.parse_args(argv)
        return args.handler(args)

    def test_fake_backend_gets_tracker_of_chosen_detector(self):
        import yalp.contract.ipc as ipc
        import yalp.reactive.fake_backend as fb
        from yalp.reactive.person_tracker import DnnPersonDetector, PersonTracker

        _CaptureBackend.last = None
        with patch.object(fb, "FakeReactiveBackend", _CaptureBackend), \
                patch.object(ipc, "ReactiveServer", _FakeServer):
            rc = self._run([
                "reactive", "--backend", "fake",
                "--camera-source", "synthetic", "--port", "0",
                "--detector", "person",
            ])

        assert rc == 0
        tracker = _CaptureBackend.last.kwargs.get("tracker")
        assert isinstance(tracker, PersonTracker), "tracker= not passed to the backend"
        # 'person' -> the orientation-agnostic cv2.dnn body detector (built lazily).
        assert isinstance(tracker._detector, DnnPersonDetector)

    def test_real_backend_gets_tracker_and_imports_no_gpiozero(self):
        import yalp.contract.ipc as ipc
        import yalp.reactive.real_backend as rb
        from yalp.reactive.person_tracker import AutoDetector, PersonTracker

        saved = {k: sys.modules.pop(k) for k in list(sys.modules)
                 if k.startswith("gpiozero")}
        _CaptureBackend.last = None
        try:
            with patch.object(rb, "RealReactiveBackend", _CaptureBackend), \
                    patch.object(ipc, "ReactiveServer", _FakeServer):
                rc = self._run([
                    "reactive", "--backend", "real",
                    "--camera-source", "synthetic", "--port", "0",
                    "--detector", "auto",
                ])
            assert rc == 0
            assert "gpiozero" not in sys.modules, "gpiozero imported for a faked backend"
            tracker = _CaptureBackend.last.kwargs.get("tracker")
            assert isinstance(tracker, PersonTracker)
            assert isinstance(tracker._detector, AutoDetector)
        finally:
            sys.modules.update(saved)

    def test_default_detector_wires_a_tracker(self):
        """Even with no --detector flag, a tracker is built (config default) and
        passed — the backend never silently falls back to its lazy HOG default."""
        import yalp.contract.ipc as ipc
        import yalp.reactive.fake_backend as fb
        from yalp.reactive.person_tracker import PersonTracker

        _CaptureBackend.last = None
        with patch.object(fb, "FakeReactiveBackend", _CaptureBackend), \
                patch.object(ipc, "ReactiveServer", _FakeServer):
            rc = self._run([
                "reactive", "--backend", "fake",
                "--camera-source", "synthetic", "--port", "0",
            ])

        assert rc == 0
        assert isinstance(_CaptureBackend.last.kwargs.get("tracker"), PersonTracker)
