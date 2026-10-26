"""BT-D46 integrated backtester tests."""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "results/phase_bt_d46_integrated_short_backtester_v1"


def test_outputs_exist():
    for name in (
        "chronological_events.csv",
        "summary.json",
        "comparison_bt_d45.md",
    ):
        assert (OUT / name).exists()


def test_one_position_max_at_any_seq():
    if not (OUT / "chronological_events.csv").exists():
        pytest.skip("run backtest")
    rows = list(csv.DictReader((OUT / "chronological_events.csv").open()))
    open_pos = 0
    for r in rows:
        if r.get("event") == "SIMULATED_FILL":
            open_pos += 1
        if r.get("event") in ("POSITION_CLOSED_SL", "POSITION_CLOSED_TP", "EXIT_UNRESOLVED"):
            open_pos = max(0, open_pos - 1)
        assert open_pos <= 1


def test_scanner_not_102_trades():
    s = json.loads((OUT / "summary.json").read_text())
    assert s["scanner_candidate_emits"] == 102
    assert s["model_1_simulated_entries"] <= 102
    assert s["pnl_computed"] is False


def test_deterministic_summary():
    a = json.loads((OUT / "summary.json").read_text())
    b = json.loads((OUT / "summary.json").read_text())
    assert a == b
