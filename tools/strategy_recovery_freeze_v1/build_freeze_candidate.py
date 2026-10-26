#!/usr/bin/env python3
"""Generate deterministic JSON artifacts for pool_landscape_v1_candidate freeze folder."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "strategy_freeze" / "pool_landscape_v1_candidate"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    sources = [
        ROOT / "Signal_definierung.md",
        ROOT / "tools/phase4a_single_position_backtest_pilot_v1/research_config_v1.json",
        ROOT / "tools/phase2a_5m_pd1_depth2_short_candidates_v1/timeline_scanner.js",
        ROOT / "tools/phase4a_single_position_backtest_pilot_v1/backtest_pilot.py",
        ROOT / "backtester/short/dashboard/research_charts/live_micro_lld_v2_ch_test/historical_json_import/phase6xt_reuse_research_poc/pd1_exact_density_trp_adapter.js",
        ROOT / "reports/DOGEUSDT_PHASE2A_5M_PD1_DEPTH2_SHORT_CANDIDATE_SCANNER_V1.md",
        ROOT / "reports/DOGEUSDT_PHASE3B_TWO_POOL_MARKET_STRUCTURE_DETECTOR_V1.md",
        ROOT / "reports/DOGEUSDT_PHASE4B_BACKTEST_TRADE_FORENSICS_V1.md",
        ROOT / "results/phase2g_upper_opportunity_dedup_v1/raw_candidates_with_opportunity_id.csv",
    ]
    provenance = {
        "freeze_candidate_id": "pool_landscape_v1_candidate",
        "status": "NOT_FINAL_APPROVED",
        "generated_utc": "2026-10-22T00:00:00Z",
        "lmv2_separation": "Original LMV2 lives under backtester/short/dashboard/research_charts/historical_lmv2/ and frozen_strategies/ — excluded from this strategy lineage.",
        "sources": [],
    }
    for p in sources:
        if p.is_file():
            provenance["sources"].append(
                {
                    "path": str(p.relative_to(ROOT)),
                    "sha256": sha256(p),
                    "role": "reference",
                }
            )

    params = {
        "strategy_family": "pool_landscape_pd1_depth2",
        "symbol": "DOGEUSDT",
        "timeframe": "5m",
        "pool_pd_entry": "PD1",
        "min_depth": 2,
        "parameters": {
            "min_pool_gap_pct": {
                "value": 0.5,
                "class": "USER_RESEARCH_START",
                "hard_entry_gate_confirmed": False,
                "note": "Pool-to-pool gap; distinct from min_tp_distance_pct",
            },
            "min_pool_age_bars": {
                "value": 5,
                "class": "USER_RESEARCH_START",
                "hard_entry_gate_confirmed": False,
                "note": "Signal_definierung.md §D supersedes earlier 10-bar draft; C003 must not be retro-labeled",
            },
            "min_tp_distance_pct": {
                "value": 0.5,
                "class": "USER_CONFIRMED",
                "applies_short": "entry to lower TP cluster TOP",
                "applies_long": "entry to upper TP cluster BOTTOM",
            },
            "sl_above_upper_pool_pct": {
                "value": 0.2,
                "class": "USER_CONFIRMED",
            },
            "sl_below_lower_pool_pct": {
                "value": 0.2,
                "class": "USER_CONFIRMED",
            },
            "roundtrip_fees_pct": {
                "value": 0.11,
                "class": "USER_CONFIRMED",
                "slippage_funding": "UNRESOLVED",
            },
            "max_open_positions": {
                "value": 1,
                "class": "USER_CONFIRMED",
            },
            "entry_proximity_pct": {
                "value": 0.05,
                "class": "IMPLEMENTATION_DEFAULT",
                "source": "phase2a timeline_scanner.js",
            },
            "rejection_mode": {
                "value": "rejection_v1",
                "class": "IMPLEMENTATION_DEFAULT",
            },
            "cooldown_candles": {
                "value": 6,
                "class": "IMPLEMENTATION_DEFAULT",
            },
        },
    }
    (OUT / "SOURCE_PROVENANCE.json").write_text(
        json.dumps(provenance, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (OUT / "RESEARCH_PARAMETERS.json").write_text(
        json.dumps(params, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps({"out": str(OUT.relative_to(ROOT)), "sources": len(provenance["sources"])}))


if __name__ == "__main__":
    main()
