#!/usr/bin/env python3
"""LONG V3 Phase 2F — why zero V2E candidates (read-only funnel)."""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PKG = Path(__file__).resolve().parent
OUT = ROOT / "logs/research/long_v3_phase2f_forensics_v1"
BUNDLE = ROOT / "results/phase_bt_d92_september_2026_frozen_v1/cache_bundle"
YAML = ROOT / "configs/long_v3_trend_continuation_research_v1.yaml"

PILOT_START = "2026-09-20T00:00:00Z"
PILOT_END = "2026-09-21T23:55:00Z"
FORENSIC_START = "2026-09-20T17:15:00Z"
FORENSIC_END = "2026-09-20T19:30:00Z"

sys.path.insert(0, str(PKG))
sys.path.insert(0, str(ROOT / "tools/phase_bt_d59_market_regime_pool_evolution_v1"))
sys.path.insert(0, str(ROOT / "tools/phase_bt_d60_ema_history_validation_v1"))

from density_session_adapter_v1 import FrozenBundleDensitySessionV1  # noqa: E402
from long_config_yaml_v1 import load_long_research_yaml  # noqa: E402
from long_phase2f_funnel_v1 import (  # noqa: E402
    aggregate_funnel,
    analyze_bar_funnel,
    forensic_window_detail,
    narrow_pool_report,
    update_lineage_streaks,
)
from ohlc_loader_v1 import load_5m_ohlc_ch, _iso, _parse_utc  # noqa: E402
from pool_consecutive_streak_v2e_v1 import update_consecutive_lower_streaks  # noqa: E402
from pool_history_gate_v1 import evaluate_pool_history_gate  # noqa: E402


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    ly = load_long_research_yaml(YAML)
    yaml_sup = ly.support

    session = FrozenBundleDensitySessionV1(BUNDLE)
    closes = session.decision_closes()
    i0, i1 = closes.index(PILOT_START), closes.index(PILOT_END)
    gate = evaluate_pool_history_gate(close_index=i0)
    ohlc_start = _iso(_parse_utc(closes[0]) - timedelta(hours=720 + 48))
    _, prices, ohlc_meta = load_5m_ohlc_ch(ohlc_start, closes[-1])

    live: dict[str, int] = {}
    lineage: dict[str, int] = {}
    bar_reports: list[dict] = []

    for i in range(0, i1 + 1):
        snap = session.collect_at_close(closes[i])
        update_consecutive_lower_streaks(snap, live, min_unique_origins=yaml_sup.min_unique_origins)
        update_lineage_streaks(snap, lineage, yaml_sup.min_unique_origins)
        if i < i0:
            continue
        c = float(prices[closes[i]]["c"])
        bar_reports.append(
            analyze_bar_funnel(
                snapshot=snap,
                market_close=c,
                bar_utc=closes[i],
                bar_index=i,
                closes=closes,
                prices=prices,
                yaml=yaml_sup,
                live_streak=live,
                lineage_streak=lineage,
            )
        )

    agg = aggregate_funnel(bar_reports)
    narrow = narrow_pool_report(bar_reports)
    forensic = forensic_window_detail(bar_reports, FORENSIC_START, FORENSIC_END)

    # Example bars: max sequential stage G (formation pass) per window
    examples = []
    for r in bar_reports:
        g = r["sequential_funnel"]["G_formation"]
        if g > 0:
            examples.append(
                {
                    "bar": r["bar_utc"],
                    "G_count": g,
                    "funnel": r["sequential_funnel"],
                    "top_below_price": sorted(
                        [x for x in r["region_flags"] if x["below_price"]],
                        key=lambda x: -x["upper"],
                    )[:3],
                }
            )
    examples.sort(key=lambda x: -x["G_count"])
    examples = examples[:5]

    short_age_note = (
        "SHORT MAIN V2 Phase1 YAML: no pool_min_age. D100 consecutive_active_depth2_v1 applies to "
        "exact atomic upper band lo|hi walk-back (pool_age_consecutive_v1). Long V2E uses the same "
        "walk-back/incremental pattern on lower_bands exact atomic keys (Variant A)."
    )

    report = {
        "verdict": "PHASE2F_FORENSICS_COMPLETE",
        "pilot_window": {"start": PILOT_START, "end": PILOT_END, "bars": len(bar_reports)},
        "gate_at_pilot": asdict(gate),
        "ohlc_meta": ohlc_meta,
        "thresholds_unchanged": asdict(yaml_sup),
        "aggregate_funnel": agg,
        "narrow_pool_086776": narrow,
        "forensic_17_15_19_30": forensic,
        "example_bars_most_formation_passes": examples,
        "short_age_semantics_note": short_age_note,
        "recommendations": {
            "dominant_blocker": _dominant_blocker(agg),
            "decisions_needed": [
                "Confirm Variant A atomic 5-bar rule vs research Variant B lineage continuity",
                "Confirm Q must mirror P rules (Variant A) vs depth2-only Q (Variant B) for production",
                "If formation uses streak-start close only, confirm vs first SSOT publish under price",
                "Whether any pilot bar should show P after filters — if zero, expect zero touch/fill",
            ],
        },
    }
    (OUT / "phase2f_report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    (OUT / "phase2f_funnel_by_bar.json").write_text(
        json.dumps(bar_reports, indent=2, default=str), encoding="utf-8"
    )
    print(json.dumps({"aggregate": agg, "narrow": narrow, "dominant": report["recommendations"]["dominant_blocker"]}, indent=2))
    return 0


def _dominant_blocker(agg: dict) -> str:
    rc = agg.get("exclusion_reason_counts_below_price_regions") or {}
    if not rc:
        return "NO_BELOW_PRICE_REGIONS_OR_NO_FAIL_TAGS"
    return max(rc.items(), key=lambda x: x[1])[0]


if __name__ == "__main__":
    raise SystemExit(main())
