"""BT-D68 regression tests (diagnostic only)."""

from __future__ import annotations

import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PKG = Path(__file__).resolve().parents[1]
for p in (
    PKG,
    ROOT / "tools/phase_bt_d59_market_regime_pool_evolution_v1",
    ROOT / "tools/phase_bt_d60_ema_history_validation_v1",
    ROOT / "tools/phase_bt_d61_regime_gegenprobe_audit_v1",
    ROOT / "tools/phase_bt_d65_pure_ema_timestamp_review_v1",
):
    sys.path.insert(0, str(p))

REF = "2026-09-09T16:30:00Z"


def test_d65_frozen_1630_is_uncertain_not_short_ok():
    path = ROOT / "results/phase_bt_d65_pure_ema_review_v1/ema_timestamp_classifications.csv"
    if not path.is_file():
        return
    with path.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["decision_close_utc"] == REF:
                assert r["ema_structure_decision"] == "UNCERTAIN"
                assert r["decision_reason_code"] == "FALLING_EMAS_PRICE_RECLAIM"
                return
    raise AssertionError("16:30 row missing in D65 CSV")


def test_sep11_single_pool_candidate_at_1200():
    path = ROOT / "results/phase_bt_d67_canonical_ema_replay_v1/baseline_pool_candidates.csv"
    if not path.is_file():
        return
    rows = [r for r in csv.DictReader(path.open()) if r.get("pilot_id") == "sep11_midday_2026"]
    assert len(rows) == 1
    assert rows[0]["decision_close_utc"] == "2026-09-11T12:00:00Z"


def test_parity_profiles_agree_at_1630_if_ch_available():
    manifest = ROOT / "results/phase_bt_d60_ema_history_v1/ema_history_manifest.json"
    if not manifest.is_file():
        return
    from parity_audit_v1 import run_parity

    data = run_parity()
    row = data["rows_by_ts"].get(REF, {})
    a = row.get("bt_d67_pilot_load", {})
    b = row.get("bt_d65_review_load", {})
    if not a.get("ema_ready") or not b.get("ema_ready"):
        return
    assert a["ema_structure_decision"] == b["ema_structure_decision"]
    assert a["close"] == b["close"]
