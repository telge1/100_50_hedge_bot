"""Smoke tests for Phase 4I outputs."""

from __future__ import annotations

import csv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "results/phase4i_preorder_strategy_eligibility_v1"


def test_outputs_exist():
    for name in (
        "manual_case_eligibility.csv",
        "entry_pool_quality.csv",
        "target_pool_quality.csv",
        "pool_landscape_asymmetry.csv",
        "eligibility_comparisons.md",
        "unresolved_decisions.md",
    ):
        assert (OUT / name).is_file()


def test_c001_c002_false_intent_flagged():
    with (OUT / "entry_pool_quality.csv").open(encoding="utf-8") as f:
        rows = {r["candidate_id"]: r for r in csv.DictReader(f)}
    assert rows["C001_20260911T120000Z"]["false_order_intent_vs_manual"] == "True"
    assert rows["C002_20260911T153500Z"]["false_order_intent_vs_manual"] == "True"
