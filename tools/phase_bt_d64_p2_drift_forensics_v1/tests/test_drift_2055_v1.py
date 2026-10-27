"""Expose 20:55 drift without asserting desired P2 label."""

from __future__ import annotations

import csv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def test_d62_d63_origin_ids_added_mismatch_at_2055() -> None:
    d62_path = ROOT / "results/phase_bt_d62_intent_gate_replay_v1/gate_decisions_at_intent.csv"
    d63_path = ROOT / "results/phase_bt_d63_extended_timestamps_v1/causal_gate_decisions.csv"
    d61_path = ROOT / "results/phase_bt_d61_regime_audit_v1/per_close_regime_comparison.csv"
    if not all(p.is_file() for p in (d62_path, d63_path, d61_path)):
        return
    ts = "2026-09-09T20:55:00Z"
    d61 = next(r for r in csv.DictReader(d61_path.open()) if r["decision_close_utc"] == ts)
    d63 = next(r for r in csv.DictReader(d63_path.open()) if r["decision_close_utc"] == ts)
    assert d61["origin_ids_added"] in ("False", "false", "")
    assert d63["origin_ids_added"] in ("True", "true", "1")
    assert d61["origin_ids_added"] != d63["origin_ids_added"]


def test_chop_unchanged_at_2055() -> None:
    d61_path = ROOT / "results/phase_bt_d61_regime_audit_v1/per_close_regime_comparison.csv"
    d63_path = ROOT / "results/phase_bt_d63_extended_timestamps_v1/causal_gate_decisions.csv"
    if not d61_path.is_file() or not d63_path.is_file():
        return
    ts = "2026-09-09T20:55:00Z"
    d61 = next(r for r in csv.DictReader(d61_path.open()) if r["decision_close_utc"] == ts)
    d63 = next(r for r in csv.DictReader(d63_path.open()) if r["decision_close_utc"] == ts)
    assert abs(float(d61["chop_ratio_24b"]) - float(d63["chop_ratio_24b"])) < 1e-9
