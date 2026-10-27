"""Smoke checks for BT-D70 replay artifacts (no full canonical collect)."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "results/phase_bt_d70_ema_v3_v1"


def test_d70_artifacts_present_after_replay():
    manifest = OUT / "coverage_manifest.json"
    if not manifest.is_file():
        return
    data = json.loads(manifest.read_text())
    assert data.get("verdict") in (
        "EMA_V3_ISOLATED_REPLAY_PASS",
        "EMA_V3_ISOLATED_REPLAY_PARTIAL",
        "EMA_V3_ISOLATED_REPLAY_BLOCKED",
    )
    for name in (
        "exact_rule_provenance.json",
        "v1_vs_v3_ema_decisions.csv",
        "reference_case_parity.csv",
        "chronological_replay_comparison.csv",
    ):
        assert (OUT / name).is_file()
