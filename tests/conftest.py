"""Shared pytest fixtures.

Telemetry is ENABLED by default in production (the flight recorder should run
from day one). But the test suite must never spawn writer threads or write JSONL
into the real ``$XDG_STATE_HOME/yalp/telemetry`` just because a test happens to
construct a backend / run loop. So we disable telemetry by default for every test
and point its directory at a per-test tmp dir as a belt-and-suspenders. Tests that
exercise telemetry opt back in explicitly (``monkeypatch.setenv('YALP_TELEMETRY',
'1', ...)`` or by constructing ``TelemetryLogger`` directly with a tmp dir).
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _telemetry_off_by_default(monkeypatch, tmp_path):
    """Disable telemetry + sandbox its directory unless a test opts back in.

    Uses ``monkeypatch`` so a test that requests telemetry can override these
    later in the same test (later ``setenv`` wins), and everything is undone on
    teardown.
    """
    monkeypatch.setenv("YALP_TELEMETRY", "0")
    monkeypatch.setenv("YALP_TELEMETRY_DIR", str(tmp_path / "telemetry"))
