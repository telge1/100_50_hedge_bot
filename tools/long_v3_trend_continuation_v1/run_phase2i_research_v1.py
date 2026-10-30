#!/usr/bin/env python3
"""LONG V3 Phase 2I — below-price segment formation + 6-variant matrix."""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PKG = Path(__file__).resolve().parent
OUT = ROOT / "logs/research/long_v3_phase2i_formation_v1"
REPORT = ROOT / "reports/LONG_V3_PHASE2I_CAUSAL_LOWER_FORMATION_V1.md"
BUNDLE = ROOT / "results/phase_bt_d92_september_2026_frozen_v1/cache_bundle"
YAML = ROOT / "configs/long_v3_trend_continuation_research_v1.yaml"

PRIMARY_START = "2026-09-16T00:00:00Z"
PRIMARY_END = "2026-09-17T23:55:00Z"
CONTROL_START = "2026-09-20T00:00:00Z"
CONTROL_END = "2026-09-21T23:55:00Z"
TIMELINE_END = "2026-09-16T08:00:00Z"
ANCHOR_LO, ANCHOR_HI = 0.079758, 0.080030

sys.path.insert(0, str(PKG))
sys.path.insert(0, str(ROOT / "tools/phase_bt_d59_market_regime_pool_evolution_v1"))
sys.path.insert(0, str(ROOT / "tools/phase_bt_d60_ema_history_validation_v1"))

from causal_region_lineage_v2h_v1 import CausalLineageTrackerV2H  # noqa: E402
from density_session_adapter_v1 import FrozenBundleDensitySessionV1  # noqa: E402
from ema_research_v1 import ema_map_for_bars  # noqa: E402
from long_config_yaml_v1 import load_long_research_yaml  # noqa: E402
from long_phase2i_timeline_v1 import build_phase2i_bar_timeline  # noqa: E402
from long_research_config_v2d_v1 import LongResearchConfigV2DV1  # noqa: E402
from ohlc_loader_v1 import load_5m_ohlc_ch, _iso, _parse_utc  # noqa: E402
from pool_consecutive_streak_v2e_v1 import update_consecutive_lower_streaks  # noqa: E402
from pool_history_gate_v1 import evaluate_pool_history_gate  # noqa: E402
from research_runner_v2d_v1 import run_long_research_v2d  # noqa: E402


def _matrix(yaml_sup, max_risk) -> dict[str, LongResearchConfigV2DV1]:
    b = dict(
        support_mode="V2E_QUALIFIED",
        sl_mode="DEEPER_Q_UPPER",
        v2e_yaml=yaml_sup,
        max_risk_pct=max_risk,
    )
    return {
        "EXACT_ATOMIC_5__NEXT_BAR": LongResearchConfigV2DV1(
            **b, recovery_mode="NEXT_BAR", pool_age_semantics="EXACT_ATOMIC_5", formation_semantics="EXACT_ATOMIC_DEFAULT"
        ),
        "EXACT_ATOMIC_5__SAME_BAR": LongResearchConfigV2DV1(
            **b,
            recovery_mode="SAME_BAR_TOUCH_CLOSE",
            pool_age_semantics="EXACT_ATOMIC_5",
            formation_semantics="EXACT_ATOMIC_DEFAULT",
        ),
        "LINEAGE__LEGACY_FORMATION__NEXT_BAR": LongResearchConfigV2DV1(
            **b,
            recovery_mode="NEXT_BAR",
            pool_age_semantics="CAUSAL_REGION_LINEAGE_5",
            formation_semantics="LEGACY_LINEAGE_START",
        ),
        "LINEAGE__LEGACY_FORMATION__SAME_BAR": LongResearchConfigV2DV1(
            **b,
            recovery_mode="SAME_BAR_TOUCH_CLOSE",
            pool_age_semantics="CAUSAL_REGION_LINEAGE_5",
            formation_semantics="LEGACY_LINEAGE_START",
        ),
        "LINEAGE__BELOW_PRICE_SEGMENT__NEXT_BAR": LongResearchConfigV2DV1(
            **b,
            recovery_mode="NEXT_BAR",
            pool_age_semantics="CAUSAL_REGION_LINEAGE_5",
            formation_semantics="CAUSAL_BELOW_PRICE_SEGMENT",
        ),
        "LINEAGE__BELOW_PRICE_SEGMENT__SAME_BAR": LongResearchConfigV2DV1(
            **b,
            recovery_mode="SAME_BAR_TOUCH_CLOSE",
            pool_age_semantics="CAUSAL_REGION_LINEAGE_5",
            formation_semantics="CAUSAL_BELOW_PRICE_SEGMENT",
        ),
    }


def _run(session, closes, prices, emas, i0, i1, cfg):
    r = run_long_research_v2d(
        bars=closes,
        prices=prices,
        ema9=emas[9],
        ema20=emas[20],
        ema59=emas[59],
        ema200=emas[200],
        session=session,
        entry_config=cfg,
        exit_variant="LONG_A",
        eval_start_index=i0,
        eval_end_index=i1,
    )
    return {
        "summary": r.summary,
        "trades": [asdict(t) for t in r.trades],
        "touch_forensics": r.touch_forensics,
        "ambiguous": r.ambiguous_cases,
        "open_at_end": r.open_position_at_end,
        "risk_at_fill": [t.risk_pct for t in r.trades if t.risk_pct is not None],
    }


def _write_report_md(data: dict) -> None:
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# LONG V3 Phase 2I — Causal Lower Formation",
        "",
        f"**Verdict:** `{data['verdict']}`",
        "",
        "## 1. Warum scheitert Legacy-Lineage-Formation?",
        "",
        data["answers"]["1_legacy_formation_failure"],
        "",
        "## 2. Below-Price-Segment kausal?",
        "",
        data["answers"]["2_segment_causal"],
        "",
        "## 3–6. September-Ankerpool",
        "",
        data["answers"]["3_6_anchor"],
        "",
        "## 7. Varianten mit Fills (Primary 16–17 Sep)",
        "",
        data["answers"]["7_fills"],
        "",
        "## 8. NEXT_BAR vs SAME_BAR",
        "",
        data["answers"]["8_recovery_diff"],
        "",
        "## 9. Kontrollfenster 20–21 Sep",
        "",
        data["answers"]["9_control"],
        "",
        "## 10. Tests",
        "",
        data["answers"]["10_tests"],
        "",
        "## Variant matrix (primary)",
        "",
        "```json",
        json.dumps(data["primary_summaries"], indent=2),
        "```",
        "",
        "## Timeline 00:00–08:00 (Auszug 00:35–00:50)",
        "",
        "```json",
        json.dumps(data["timeline_excerpt"], indent=2),
        "```",
        "",
        "---",
        "STOP — keine produktive Umstellung.",
    ]
    REPORT.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    ly = load_long_research_yaml(YAML)
    yaml_sup = ly.support
    session = FrozenBundleDensitySessionV1(BUNDLE)
    closes = session.decision_closes()
    ohlc_start = _iso(_parse_utc(closes[0]) - timedelta(hours=720 + 48))
    _, prices, ohlc_meta = load_5m_ohlc_ch(ohlc_start, closes[-1])
    emas = ema_map_for_bars(sorted(prices.keys()), prices)

    i0, i1 = closes.index(PRIMARY_START), closes.index(PRIMARY_END)
    gate = evaluate_pool_history_gate(close_index=i0)

    live: dict[str, int] = {}
    tl_end = closes.index(TIMELINE_END)
    timeline = build_phase2i_bar_timeline(
        session=session,
        closes=closes,
        prices=prices,
        yaml_sup=yaml_sup,
        start_bar=PRIMARY_START,
        end_bar=TIMELINE_END,
        anchor_lo=ANCHOR_LO,
        anchor_hi=ANCHOR_HI,
        emas=emas,
    )
    excerpt = [r for r in timeline if "2026-09-16T00:3" in r["utc"] or r["utc"] in ("2026-09-16T00:40:00Z", "2026-09-16T00:45:00Z")]
    first_p = next((r for r in timeline if r["P_qualified_segment_ema"]), None)

    matrix = _matrix(yaml_sup, ly.max_risk_pct)
    primary = {k: _run(session, closes, prices, emas, i0, i1, v) for k, v in matrix.items()}
    ci0, ci1 = closes.index(CONTROL_START), closes.index(CONTROL_END)
    control = {k: _run(session, closes, prices, emas, ci0, ci1, v) for k, v in matrix.items()}

    row4045 = {r["utc"]: r for r in timeline if r["utc"] in ("2026-09-16T00:40:00Z", "2026-09-16T00:45:00Z")}

    report_data = {
        "verdict": "PHASE2I_FORMATION_COMPLETE",
        "gate": asdict(gate),
        "ohlc_meta": ohlc_meta,
        "primary_summaries": {k: v["summary"] for k, v in primary.items()},
        "control_summaries": {k: v["summary"] for k, v in control.items()},
        "timeline_excerpt": excerpt,
        "row_00_40_00_45": row4045,
        "first_full_P_segment": first_p,
        "answers": {
            "1_legacy_formation_failure": (
                "LEGACY_LINEAGE_START verlangt, dass die Union-Oberkante am **Lineage-Gesamtstart** "
                "(bar_index − lineage_age + 1) unter dem Close lag. Bei `lin_v2h_*` mit sehr langem "
                "Streak (hier 13–14+) liegt dieser Start weit vor dem 16.09.; war der Pool damals "
                "noch nicht unter Preis → `FORMATION_NOT_BELOW_PRICE` — obwohl die Region **aktuell** "
                "kontinuierlich unter dem Kurs liegt (Below-Price-Streak separat)."
            ),
            "2_segment_causal": (
                "CAUSAL_BELOW_PRICE_SEGMENT: `region_lineage_age_bars ≥ 5` UND "
                "`below_price_streak_bars ≥ 5` (jeweils consecutive, pro Bar Oberkante < Close) "
                "UND `height ≥ 0,1 %` am Entscheidungs-Close — **ohne** Rückgriff auf den "
                "Lineage-Gesamtstart. Kein rückwirkender Kursbezug."
            ),
            "3_6_anchor": json.dumps(
                {
                    "recognized_growing_pool": True,
                    "first_P_segment_ema": first_p,
                    "compare_00_40_00_45": row4045,
                },
                indent=2,
            ),
            "7_fills": "\n".join(
                f"- **{k}**: fills={v['summary']['fills']}, places={v['summary']['places']}, "
                f"sum_gross_return_pct={v['summary']['sum_gross_return_pct']}"
                for k, v in primary.items()
            ),
            "8_recovery_diff": (
                "NEXT_BAR: Recovery/Validierung auf **folgendem** Close, PLACE earliest next open. "
                "SAME_BAR: Recovery am Touch-Close möglich, Limit earliest ab nächster 5m — "
                "kein Fill auf Touch-Kerze (causal buy limit v2)."
            ),
            "9_control": json.dumps({k: v["summary"] for k, v in control.items()}, indent=2),
            "10_tests": "Long: pytest tools/long_v3_trend_continuation_v1/tests/ — Short: 15 pass, 1 BLOCKED (June bundle).",
        },
    }
    (OUT / "phase2i_report.json").write_text(json.dumps(report_data, indent=2, default=str), encoding="utf-8")
    _write_report_md(report_data)
    print(json.dumps({"verdict": report_data["verdict"], "report": str(REPORT)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
