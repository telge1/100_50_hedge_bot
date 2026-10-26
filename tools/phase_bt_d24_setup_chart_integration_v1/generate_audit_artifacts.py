#!/usr/bin/env python3
"""BT-D24 read-only audit artifacts from setups.json (no scanner changes)."""

from __future__ import annotations

import json
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SETUPS_PATH = REPO / "results/setup_scanner_runs/bt_d23_pilot_sep11_24h_v1/setups.json"
OUT = REPO / "results/phase_bt_d24_setup_chart_integration_v1"


def _parse_utc(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    doc = json.loads(SETUPS_PATH.read_text(encoding="utf-8"))
    setups = doc["setups"]
    assert len(setups) == 261

    by_hour: Counter[str] = Counter()
    for s in setups:
        by_hour[s["decision_utc"][:13] + ":00"] += 1

    origin_sets = {s["origin_set_key"] for s in setups if s.get("origin_set_key")}
    price_buckets: Counter[str] = Counter()
    for s in setups:
        price_buckets[f"{s['planned_entry']:.5f}"] += 1

    entries = sorted(s["planned_entry"] for s in setups)
    gaps = [entries[i + 1] - entries[i] for i in range(len(entries) - 1) if entries[i + 1] > entries[i]]

    triples: Counter[tuple] = Counter()
    for s in setups:
        triples[(round(s["planned_entry"], 8), round(s["planned_sl"], 8), round(s["planned_tp"], 8))] += 1

    ref_cases = {}
    for label, decision, lo in [
        ("2026-09-11T15:35:00Z", "2026-09-11T15:35:00Z", 0.08681),
        ("2026-09-11T17:00:00Z", "2026-09-11T17:00:00Z", 0.08593),
    ]:
        matches = [
            s
            for s in setups
            if s["decision_utc"] == decision and abs(float(s["pool_lower"]) - lo) < 1e-5
        ]
        before = [
            s
            for s in setups
            if s["decision_utc"] < decision
            and abs(float(s["pool_lower"]) - lo) < 1e-5
            and set((s.get("origin_set_key") or "").split("|"))
            & set((matches[0].get("origin_set_key") or "").split("|") if matches else [])
        ]
        ref_cases[label] = {
            "matches_at_decision": matches,
            "earlier_same_pool_band": before,
            "scanner_fields": matches[0] if matches else None,
        }

    distribution = {
        "setup_count": len(setups),
        "setups_per_hour_utc": dict(sorted(by_hour.items())),
        "unique_origin_sets": len(origin_sets),
        "duplicate_entry_price_levels": {k: v for k, v in price_buckets.items() if v > 1},
        "entry_level_gaps_stats": {
            "count": len(gaps),
            "min": min(gaps) if gaps else None,
            "median": statistics.median(gaps) if gaps else None,
            "mean": statistics.mean(gaps) if gaps else None,
        },
        "identical_entry_sl_tp_triples": sum(1 for c in triples.values() if c > 1),
        "top_repeated_triples": [
            {"entry_sl_tp": k, "count": v} for k, v in triples.most_common(5) if v > 1
        ],
    }

    api_parity = {
        "setups_json_path": str(SETUPS_PATH.relative_to(REPO)),
        "setup_count": len(setups),
        "contract_version": doc.get("contract_version"),
        "payload_kind": "bt_d23_setup_scanner_chart_integration_v1",
        "chart_api_routes": [
            "GET /api/research/backtester/bt-d23-setup-scanner/runs",
            "POST /api/research/backtester/bt-d23-setup-scanner/load",
            "POST /api/research/backtester/bt-d23-setup-scanner/sync-viewport",
        ],
        "strategy_id": "pool_first_setup_scanner_bt_d23",
    }

    (OUT / "setup_distribution.json").write_text(json.dumps(distribution, indent=2), encoding="utf-8")
    (OUT / "visual_reference_cases.json").write_text(json.dumps(ref_cases, indent=2), encoding="utf-8")
    (OUT / "api_parity.json").write_text(json.dumps(api_parity, indent=2), encoding="utf-8")
    print("wrote", OUT)


if __name__ == "__main__":
    main()
