"""Document EMA-only SHORT_OK on borderline timestamps (no threshold changes)."""

from __future__ import annotations

import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/phase_bt_d68_ema_parity_v1"))
sys.path.insert(0, str(ROOT / "tools/phase_bt_d60_ema_history_validation_v1"))
sys.path.insert(0, str(ROOT / "tools/phase_bt_d59_market_regime_pool_evolution_v1"))
sys.path.insert(0, str(ROOT / "tools/phase_bt_d61_regime_gegenprobe_audit_v1"))
sys.path.insert(0, str(ROOT / "tools/phase_bt_d65_pure_ema_timestamp_review_v1"))
sys.path.insert(0, str(ROOT / "tools/phase_downtrend_isolated_upper_short_v1"))

from parity_audit_v1 import row_at, _load_profile  # noqa: E402

CASES = [
    ("2026-09-11T12:05:00Z", "Sep-11 midday — EMA SHORT_OK, sideways context in user chart"),
    ("2026-08-06T00:00:00Z", "BT-D65 known borderline false-allow risk"),
    ("2026-08-12T08:30:00Z", "BT-D65 known borderline false-allow risk"),
]


def run_false_allow_audit() -> list[dict]:
    _, keys, prices, emas = _load_profile("audit", "2026-08-01T00:00:00Z", "2026-09-30T00:00:00Z")
    d65_csv = ROOT / "results/phase_bt_d65_pure_ema_review_v1/ema_timestamp_classifications.csv"
    frozen = {}
    if d65_csv.is_file():
        with d65_csv.open(encoding="utf-8") as f:
            for r in csv.DictReader(f):
                frozen[r["decision_close_utc"]] = r

    out = []
    for ts, note in CASES:
        row = row_at(keys, prices, emas, ts) if ts in keys else {"ema_ready": False}
        fr = frozen.get(ts, {})
        weakness = []
        if row.get("ema_structure_decision") == "SHORT_OK":
            if not row.get("close_below_ema59") or not row.get("close_below_ema20"):
                weakness.append("price_not_below_both_ema20_and_ema59")
            if not row.get("ema_bear_stack_9_20_59_200"):
                weakness.append("imperfect_bear_stack_allowed_via_falling_slow_ema_rule")
            if row.get("ema_tight_cluster"):
                weakness.append("tight_ema_cluster")
        out.append(
            {
                "decision_close_utc": ts,
                "note": note,
                "ema_decision": row.get("ema_structure_decision"),
                "decision_reason_code": row.get("decision_reason_code"),
                "close_below_ema20": row.get("close_below_ema20"),
                "close_below_ema59": row.get("close_below_ema59"),
                "ema_bear_stack": row.get("ema_bear_stack_9_20_59_200"),
                "ema59_slope_12b_pct": row.get("ema59_slope_12b_pct"),
                "ema200_slope_12b_pct": row.get("ema200_slope_12b_pct"),
                "d65_frozen_decision": fr.get("ema_structure_decision"),
                "documented_weakness_flags": ";".join(weakness) if weakness else "review_chart_context",
            }
        )
    return out
