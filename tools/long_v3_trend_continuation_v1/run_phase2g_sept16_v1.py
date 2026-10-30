#!/usr/bin/env python3
"""LONG V3 Phase 2G — Sept 16–17 pool proof + V2E eligibility (read-only)."""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PKG = Path(__file__).resolve().parent
OUT = ROOT / "logs/research/long_v3_phase2g_sept16_v1"
BUNDLE = ROOT / "results/phase_bt_d92_september_2026_frozen_v1/cache_bundle"
YAML = ROOT / "configs/long_v3_trend_continuation_research_v1.yaml"

PILOT_START = "2026-09-16T00:00:00Z"
PILOT_END = "2026-09-17T23:55:00Z"
LOOKBACK_168H = "2026-09-09T00:00:00Z"
EARLY_DETAIL_START = "2026-09-16T00:00:00Z"
EARLY_DETAIL_END = "2026-09-16T08:00:00Z"

sys.path.insert(0, str(PKG))
sys.path.insert(0, str(ROOT / "tools/phase_bt_d59_market_regime_pool_evolution_v1"))
sys.path.insert(0, str(ROOT / "tools/phase_bt_d60_ema_history_validation_v1"))

from density_session_adapter_v1 import FrozenBundleDensitySessionV1  # noqa: E402
from ema_research_v1 import ema_map_for_bars  # noqa: E402
from long_config_yaml_v1 import load_long_research_yaml  # noqa: E402
from long_phase2g_audit_v1 import (  # noqa: E402
    checklist_for_region,
    discover_region_first_active_bar,
    rank_candidate_regions_early_window,
    regions_below_price,
    _stacked,
)
from long_research_config_v2d_v1 import LongResearchConfigV2DV1  # noqa: E402
from long_support_selection_v2e_v1 import select_support_v2e, _region_key  # noqa: E402
from lower_atomic_pool_key_v1 import canonical_lower_atomic_pool_key  # noqa: E402
from ohlc_loader_v1 import load_5m_ohlc_ch, _iso, _parse_utc  # noqa: E402
from pool_consecutive_streak_v2e_v1 import streak_for_band, update_consecutive_lower_streaks  # noqa: E402
from pool_history_gate_v1 import evaluate_pool_history_gate  # noqa: E402
from research_runner_v2d_v1 import run_long_research_v2d  # noqa: E402


def _pool_key_bug_status() -> dict:
    lo, hi = 0.067456, 0.0676
    naive = f"{lo}|{hi}"
    canon = canonical_lower_atomic_pool_key(lo, hi)
    return {
        "phase2f_issue": "streak cache key must match qualification lookup",
        "fixed_in_repo": naive != canon or streak_for_band({canon: 7}, lo, hi) == 7,
        "canonical_key_example": {"lo": lo, "hi": hi, "naive_f_string": naive, "canonical": canon},
    }


def _summary_table_rows(checklists: list) -> list[dict]:
    rows = []
    for c in checklists:
        qual = c.LONG_ENTRY_ELIGIBLE
        reason = c.disqualify_reason or ("OK" if qual else "UNKNOWN")
        rows.append(
            {
                "UTC": c.utc,
                "Lower-Pool P": c.region_key,
                "Höhe %": c.height_pct,
                "Alter Bars": c.age_bars,
                "Qualified?": qual,
                "Grund": reason,
            }
        )
    return rows


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    key_status = _pool_key_bug_status()
    ly = load_long_research_yaml(YAML)
    yaml_sup = ly.support

    session = FrozenBundleDensitySessionV1(BUNDLE)
    closes = session.decision_closes()
    if PILOT_START not in closes or PILOT_END not in closes:
        (OUT / "phase2g_report.json").write_text(
            json.dumps({"verdict": "PHASE2G_BLOCKED", "reason": "WINDOW_NOT_IN_BUNDLE"}),
            indent=2,
        )
        return 1

    i0, i1 = closes.index(PILOT_START), closes.index(PILOT_END)
    lookback_i = closes.index(LOOKBACK_168H) if LOOKBACK_168H in closes else None
    gate = evaluate_pool_history_gate(close_index=i0)
    ohlc_start = _iso(_parse_utc(closes[0]) - timedelta(hours=720 + 48))
    _, prices, ohlc_meta = load_5m_ohlc_ch(ohlc_start, closes[-1])
    emas = ema_map_for_bars(sorted(prices.keys()), prices)

    ed0 = closes.index(EARLY_DETAIL_START)
    ed1 = closes.index(EARLY_DETAIL_END)
    early_candidates = rank_candidate_regions_early_window(
        session=session,
        closes=closes,
        prices=prices,
        start_i=ed0,
        end_i=ed1,
        min_unique=yaml_sup.min_unique_origins,
    )

    live: dict[str, int] = {}
    for i in range(0, i0):
        update_consecutive_lower_streaks(
            session.collect_at_close(closes[i]), live, min_unique_origins=yaml_sup.min_unique_origins
        )

    # Focus checklists: top 8 early candidates at pilot bars where height>=0.1 or v2e-relevant
    focus_keys = [c["region_key"] for c in early_candidates[:8]]
    checklists = []
    v2e_timeline: list[dict] = []

    for i in range(i0, i1 + 1):
        bar = closes[i]
        snap = session.collect_at_close(bar)
        update_consecutive_lower_streaks(snap, live, min_unique_origins=yaml_sup.min_unique_origins)
        close = float(prices[bar]["c"])
        stacked = _stacked(snap, yaml_sup.min_unique_origins)
        e200_p6 = emas[200].get(closes[i - 6]) if i >= 6 else None

        sel, reason, _ = select_support_v2e(
            session,
            decision_close_utc=bar,
            market_price=close,
            prices=prices,
            yaml=yaml_sup,
            live_consecutive_streak=live,
            bar_index=i,
            closes=closes,
        )
        if sel is not None:
            v2e_timeline.append(
                {
                    "bar_utc": bar,
                    "close": close,
                    "P": sel.region_key,
                    "Q": sel.sl_region_key,
                    "entry_limit": sel.entry_limit,
                    "sl": sel.structural_sl,
                    "gap_pct": sel.qualified_gap_pct,
                }
            )

        if bar.endswith(":00:00Z") or bar.endswith(":30:00Z") or sel is not None:
            for rk in focus_keys:
                reg = next((r for r in stacked if _region_key(r) == rk), None)
                if reg is None or reg.upper_edge >= close - 1e-12:
                    continue
                checklists.append(
                    checklist_for_region(
                        bar_utc=bar,
                        bar_index=i,
                        closes=closes,
                        region=reg,
                        stacked=stacked,
                        market_close=close,
                        prices=prices,
                        yaml=yaml_sup,
                        live_streak=live,
                        ema9=emas[9].get(bar),
                        ema20=emas[20].get(bar),
                        ema59=emas[59].get(bar),
                        ema200=emas[200].get(bar),
                        ema200_prev6=e200_p6,
                    )
                )

    region_provenance = []
    for cand in early_candidates[:8]:
        first = discover_region_first_active_bar(
            session,
            closes,
            cand["region_key"],
            yaml_sup.min_unique_origins,
            search_end_index=i1,
        )
        region_provenance.append({**cand, "first_active_bar_utc": first})

    cfg_a = LongResearchConfigV2DV1(
        "V2E_QUALIFIED",
        "DEEPER_Q_UPPER",
        "NEXT_BAR",
        v2e_yaml=yaml_sup,
        max_risk_pct=ly.max_risk_pct,
    )
    cfg_b = LongResearchConfigV2DV1(
        "V2E_QUALIFIED",
        "DEEPER_Q_UPPER",
        "NEXT_BAR",
        v2e_yaml=yaml_sup,
        max_risk_pct=ly.max_risk_pct,
    )
    run_a = run_long_research_v2d(
        bars=closes,
        prices=prices,
        ema9=emas[9],
        ema20=emas[20],
        ema59=emas[59],
        ema200=emas[200],
        session=session,
        entry_config=cfg_a,
        exit_variant="LONG_A",
        eval_start_index=i0,
        eval_end_index=i1,
    )
    run_b = run_long_research_v2d(
        bars=closes,
        prices=prices,
        ema9=emas[9],
        ema20=emas[20],
        ema59=emas[59],
        ema200=emas[200],
        session=session,
        entry_config=cfg_b,
        exit_variant="LONG_B",
        eval_start_index=i0,
        eval_end_index=i1,
    )

    same_bar = run_long_research_v2d(
        bars=closes,
        prices=prices,
        ema9=emas[9],
        ema20=emas[20],
        ema59=emas[59],
        ema200=emas[200],
        session=session,
        entry_config=LongResearchConfigV2DV1(
            "V2E_QUALIFIED",
            "DEEPER_Q_UPPER",
            "SAME_BAR_TOUCH_CLOSE",
            v2e_yaml=yaml_sup,
            max_risk_pct=ly.max_risk_pct,
        ),
        exit_variant="LONG_A",
        eval_start_index=i0,
        eval_end_index=i1,
    )

    table = _summary_table_rows(checklists)
    primary = early_candidates[0] if early_candidates else None
    primary_checks = [c for c in checklists if primary and c.region_key == primary["region_key"]]
    first_eligible = next((c for c in primary_checks if c.LONG_ENTRY_ELIGIBLE), None)
    first_v2e = v2e_timeline[0] if v2e_timeline else None

    report = {
        "verdict": "PHASE2G_SEPT16_COMPLETE",
        "pool_key_bug": key_status,
        "pilot_window": {"start": PILOT_START, "end": PILOT_END, "bars": i1 - i0 + 1},
        "lookback_168h": {"anchor": LOOKBACK_168H, "index": lookback_i, "gate": asdict(gate)},
        "ohlc_meta": ohlc_meta,
        "thresholds_unchanged": asdict(yaml_sup),
        "early_sept16_candidate_regions": early_candidates,
        "region_provenance": region_provenance,
        "summary_table": table,
        "primary_chart_candidate_note": (
            "Ohne Screenshot-ID: höchste Unter-Preis-Region (nearest upper) im frühen 16.09. "
            "als visueller Anker — SSOT-Zuordnung nicht als bewiesen markiert."
        ),
        "primary_candidate": primary,
        "primary_checklists_sample": [asdict(c) for c in primary_checks[:12]],
        "first_long_entry_eligible_primary": asdict(first_eligible) if first_eligible else None,
        "v2e_engine_selection_timeline_count": len(v2e_timeline),
        "v2e_first_selection": first_v2e,
        "v2e_selection_sample": v2e_timeline[:20],
        "research_runs": {
            "LONG_A_NEXT_BAR": {
                "summary": run_a.summary,
                "fills": [e for e in run_a.entry_events if e.get("action") == "FILL"],
                "places": [e for e in run_a.entry_events if e.get("action") == "PLACE"],
                "touch_forensics": run_a.touch_forensics[:30],
            },
            "LONG_B_NEXT_BAR": {
                "summary": run_b.summary,
                "fills": [e for e in run_b.entry_events if e.get("action") == "FILL"],
            },
            "LONG_A_SAME_BAR_RESEARCH": {"summary": same_bar.summary},
        },
        "answers": _build_answers(
            primary=primary,
            first_eligible=first_eligible,
            first_v2e=first_v2e,
            run_a=run_a,
            early_candidates=early_candidates,
        ),
    }
    (OUT / "phase2g_report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    (OUT / "summary_table.json").write_text(json.dumps(table, indent=2), encoding="utf-8")
    print(json.dumps({"verdict": report["verdict"], "table_rows": len(table), "fills_a": run_a.summary}, indent=2))
    return 0


def _build_answers(*, primary, first_eligible, first_v2e, run_a, early_candidates) -> dict:
    return {
        "1_pool_found_in_ssot": primary is not None,
        "2_first_active": primary.get("first_active_bar_utc") if isinstance(primary, dict) else None,
        "3_five_bars": first_eligible.AGE_5_BARS_OK if first_eligible else None,
        "4_height_0_1": first_eligible.HEIGHT_0_1_PCT_OK if first_eligible else None,
        "5_formation_below": first_eligible.FORMATION_BELOW_PRICE_OK if first_eligible else None,
        "6_deeper_Q": first_eligible.q_region_key if first_eligible else None,
        "7_entry_sl": {
            "buy_limit": first_eligible.entry_limit if first_eligible else None,
            "sl": first_eligible.structural_sl if first_eligible else None,
            "gap_pct": first_eligible.gap_pct if first_eligible else None,
            "risk_pct": first_eligible.risk_pct if first_eligible else None,
        },
        "8_price_reached_limit": "see touch_forensics in research_runs",
        "9_recovery_and_order": run_a.summary,
        "10_blockers_if_any": first_eligible.disqualify_reason if first_eligible and not first_eligible.LONG_ENTRY_ELIGIBLE else None,
        "alternate_candidates_count": len(early_candidates),
    }


if __name__ == "__main__":
    raise SystemExit(main())
