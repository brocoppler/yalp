"""Tests for ``yalp bench`` — the combined-load Gate K / Gate H harness.

These run HEADLESS on a laptop with the synthetic camera (no webcam, no GPIO, no
model download), so CI exercises the whole harness end-to-end:

  * a short ``--seconds 1 --backend fake --camera-source synthetic`` run exits 0
    and prints the tick p99 plus a Gate K and a Gate H verdict;
  * the pure ``gate_verdicts(p99_ms, fps)`` helper is unit-tested on both the
    PASS/GO and the NO-GO sides.
"""

from __future__ import annotations

import sys
from io import StringIO
from typing import Tuple
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# CLI registration
# ---------------------------------------------------------------------------
def test_bench_in_feature_modules():
    from yalp.cli import FEATURE_MODULES

    assert "yalp.reactive.bench_cli" in FEATURE_MODULES


def test_build_parser_has_bench_subcommand():
    from yalp.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(
        ["bench", "--seconds", "1", "--backend", "fake", "--camera-source", "synthetic"]
    )
    assert args.seconds == 1.0
    assert args.backend == "fake"
    assert args.camera_source == "synthetic"
    assert getattr(args, "handler", None) is not None


# ---------------------------------------------------------------------------
# End-to-end short run (headless, synthetic camera)
# ---------------------------------------------------------------------------
def _run_bench(*extra_args: str) -> Tuple[int, str]:
    """Run ``yalp bench <args>`` headless and return (exit_code, stdout)."""
    from yalp.cli import build_parser

    argv = ["bench", *extra_args]
    buf = StringIO()
    with patch("sys.stdout", buf):
        parser = build_parser()
        args = parser.parse_args(argv)
        handler = getattr(args, "handler", None)
        assert handler is not None, "no handler registered for 'bench'"
        rc = handler(args)
    return int(rc), buf.getvalue()


def test_short_fake_synthetic_run_exits_zero_and_reports():
    rc, out = _run_bench(
        "--seconds", "1", "--backend", "fake", "--camera-source", "synthetic"
    )
    assert rc == 0, out
    # Tick percentiles, including p99.
    assert "p99" in out
    assert "p50 / p95 / p99" in out
    # Both gate verdicts appear, each resolving to one of the allowed verdicts.
    assert "Gate K" in out
    assert "Gate H" in out
    assert ("PASS" in out) or ("NO-GO" in out)  # Gate K side
    assert ("GO" in out) or ("NO-GO" in out)  # Gate H side
    # The config triple (detector model/name, resolution, runtime).
    assert "detector=" in out
    assert "resolution=" in out
    assert "runtime=" in out
    # Sustained detector fps is reported.
    assert "detector sustained fps" in out


def test_short_run_with_resolution_reports_that_resolution():
    rc, out = _run_bench(
        "--seconds", "1",
        "--backend", "fake",
        "--camera-source", "synthetic",
        "--resolution", "320x240",
    )
    assert rc == 0, out
    assert "320x240" in out


def test_real_backend_falls_back_to_fake_hw_on_laptop():
    """--backend real runs headless on a laptop (no GPIO) via the fake drivers."""
    rc, out = _run_bench(
        "--seconds", "1", "--backend", "real", "--camera-source", "synthetic"
    )
    assert rc == 0, out
    assert "Gate K" in out and "Gate H" in out


# ---------------------------------------------------------------------------
# Pure verdict logic — both sides
# ---------------------------------------------------------------------------
def test_gate_verdicts_pass_and_go_side():
    from yalp.reactive.bench_cli import gate_verdicts

    # Well under the tick budget and well above the detector floor.
    v = gate_verdicts(p99_ms=5.0, fps=30.0, tick_budget_ms=33, gate_h_go_hz=3)
    assert v["gate_k_pass"] is True
    assert v["gate_k_verdict"] == "PASS"
    assert v["gate_k_hint"] is None
    assert v["gate_h_go"] is True
    assert v["gate_h_verdict"] == "GO"


def test_gate_verdicts_nogo_side():
    from yalp.reactive.bench_cli import gate_verdicts

    # Over the tick budget and below the detector floor.
    v = gate_verdicts(p99_ms=80.0, fps=0.5, tick_budget_ms=33, gate_h_go_hz=3)
    assert v["gate_k_pass"] is False
    assert v["gate_k_verdict"] == "NO-GO"
    assert v["gate_k_hint"]  # a non-empty recovery hint
    assert v["gate_h_go"] is False
    assert v["gate_h_verdict"] == "NO-GO"
    assert v["gate_h_hint"]


def test_gate_verdicts_boundaries():
    from yalp.reactive.bench_cli import gate_verdicts

    # p99 exactly at budget is NO-GO (strict <); fps exactly at floor is GO (>=).
    v = gate_verdicts(p99_ms=33.0, fps=3.0, tick_budget_ms=33, gate_h_go_hz=3)
    assert v["gate_k_pass"] is False
    assert v["gate_h_go"] is True


def test_gate_verdicts_defaults_from_config():
    from yalp import config
    from yalp.reactive.bench_cli import gate_verdicts

    v = gate_verdicts(p99_ms=1.0, fps=1000.0)
    assert v["tick_budget_ms"] == float(config.TICK_BUDGET_MS)
    assert v["gate_h_go_hz"] == float(config.GATE_H_GO_HZ)


# ---------------------------------------------------------------------------
# Percentile helper (pure)
# ---------------------------------------------------------------------------
def test_percentile_basic():
    from yalp.reactive.bench_cli import percentile

    data = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert percentile(data, 0) == 1.0
    assert percentile(data, 100) == 5.0
    assert percentile(data, 50) == 3.0
    assert percentile([], 99) == 0.0
    assert percentile([7.0], 99) == 7.0
