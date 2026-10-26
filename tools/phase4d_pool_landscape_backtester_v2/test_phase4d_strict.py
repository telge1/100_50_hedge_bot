"""Strict strategy formula and backtest tests."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parents[1]

from strategy_rules import (  # noqa: E402
    long_sl,
    plan_long,
    plan_short,
    select_long_tp_depth2,
    select_short_tp_depth2,
    short_sl,
)


def test_short_sl_formula():
    assert abs(short_sl(0.085982, 0.2) - 0.085982 * 1.002) < 1e-12


def test_long_sl_formula():
    assert abs(long_sl(0.08412, 0.2) - 0.08412 * 0.998) < 1e-12


def test_short_tp_uses_lower_top_depth2_only():
    intervals = [
        {"side": "lower", "bottom_price": 0.082, "top_price": 0.0825, "depth": 2},
        {"side": "lower", "bottom_price": 0.081, "top_price": 0.0826, "depth": 3},
    ]
    entry = 0.085
    tp, _ = select_short_tp_depth2(entry, intervals, 0.5)
    assert tp is not None
    assert tp["top"] == 0.0825
    assert tp["depth"] == 2


def test_long_tp_uses_upper_bottom_depth2():
    uppers = [
        {"bottom_price": 0.086, "top_price": 0.0861, "depth": 2},
        {"bottom_price": 0.0855, "top_price": 0.0859, "depth": 2},
    ]
    entry = 0.084
    tp, _ = select_long_tp_depth2(entry, uppers, 0.5)
    assert tp["bottom"] == 0.0855


def test_short_entry_level_is_pool_lower():
    cfg = {"sl_above_upper_pool_pct": 0.2, "min_tp_distance_pct": 0.5}
    plan = plan_short(0.08593, 0.085982, [], 0.08593, cfg, quality_gate=False)
    assert plan.entry_level == 0.08593


def test_long_entry_level_is_pool_upper():
    cfg = {"sl_below_lower_pool_pct": 0.2, "min_tp_distance_pct": 0.5}
    plan = plan_long(0.08412, 0.08453, [], 0.08453, cfg, quality_gate=False)
    assert plan.entry_level == 0.08453


@pytest.mark.integration
def test_run_deterministic():
    proc = subprocess.run(
        ["python3", str(TOOLS / "run_phase4d_backtest.py")],
        cwd=str(TOOLS),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().split("\n")[-1])
    assert out["trades"] >= 0
    csv_path = ROOT / "results/phase4d_pool_landscape_backtester_v2/simulated_trades.csv"
    assert csv_path.is_file()
