"""Ensure D46 contract id stable and existing D46 result artifacts unchanged."""

import json
from pathlib import Path

from d46_adapter_v1 import d46_contract_id, run_d46_baseline_reference

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "results/phase_bt_d46_integrated_short_backtester_v1"


def test_d46_contract_frozen():
    assert d46_contract_id() == "bt_d46_integrated_short_backtester_v1"


def test_d46_summary_unchanged_if_present():
    path = OUT / "summary.json"
    if not path.exists():
        return
    s = json.loads(path.read_text())
    assert s["pnl_computed"] is False
    assert s["scanner_candidate_emits"] == 102


def test_d46_adapter_is_callable():
    assert callable(run_d46_baseline_reference)
