"""Verify BT-D60/D61 inputs for intent replay."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def verify_inputs() -> dict:
    d60_manifest = ROOT / "results/phase_bt_d60_ema_history_v1/ema_history_manifest.json"
    d46_events = ROOT / "results/phase_bt_d46_integrated_short_backtester_v1/chronological_events.csv"
    d61_per_close = ROOT / "results/phase_bt_d61_regime_audit_v1/per_close_regime_comparison.csv"
    out = {
        "baseline_commit": "2ea423681aadedd03248346b8e235afab6d076d3",
        "ema_contract": "TRP SMA-seed (BT-D60)",
        "pool_contract": "PD1 depth2 D54/D9 cache; 168h context documented in BT-D60",
        "d60_manifest_exists": d60_manifest.is_file(),
        "d46_events_exists": d46_events.is_file(),
        "d61_per_close_exists": d61_per_close.is_file(),
        "all_ok": d60_manifest.is_file() and d46_events.is_file(),
    }
    if d60_manifest.is_file():
        m = json.loads(d60_manifest.read_text(encoding="utf-8"))
        out["d60_verdict"] = m.get("verdict")
        out["all_ok"] = out["all_ok"] and m.get("verdict") == "EMA_HISTORY_VALIDATED"
    return out
