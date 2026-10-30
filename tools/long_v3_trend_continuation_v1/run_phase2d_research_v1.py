#!/usr/bin/env python3
"""LONG V3 Phase 2D — support strength + SL@Q research matrix (read-only)."""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PKG = Path(__file__).resolve().parent
OUT = ROOT / "logs/research/long_v3_phase2d_research_v1"
BUNDLE = ROOT / "results/phase_bt_d92_september_2026_frozen_v1/cache_bundle"

PILOT_START_UTC = "2026-09-20T00:00:00Z"
PILOT_END_UTC = "2026-09-21T23:55:00Z"
LOOKBACK_168H = "2026-09-13T00:00:00Z"
EXAMPLE_BAR = "2026-09-20T17:15:00Z"  # close before 17:20 touch in 2C

sys.path.insert(0, str(PKG))
sys.path.insert(0, str(ROOT / "tools/phase_bt_d59_market_regime_pool_evolution_v1"))
sys.path.insert(0, str(ROOT / "tools/phase_bt_d60_ema_history_validation_v1"))

from contracts_v1 import BARS_PER_HOUR_5M, MIN_POOL_LOOKBACK_HOURS  # noqa: E402
from density_session_adapter_v1 import FrozenBundleDensitySessionV1  # noqa: E402
from ema_research_v1 import ema_map_for_bars  # noqa: E402
from long_research_config_v2d_v1 import MATRIX_2D_BASELINE, LongResearchConfigV2DV1  # noqa: E402
from long_support_selection_v2d_v1 import explain_v1_prefers_narrow_pool, select_support_v2d  # noqa: E402
from ohlc_loader_v1 import load_5m_ohlc_ch, _iso, _parse_utc  # noqa: E402
from pool_history_gate_v1 import evaluate_pool_history_gate  # noqa: E402
from pool_persistence_v2d_v1 import streak_at_close  # noqa: E402
from research_runner_v2d_v1 import run_long_research_v2d  # noqa: E402


def _matrix_key(cfg: LongResearchConfigV2DV1) -> str:
    return f"{cfg.support_mode}__{cfg.sl_mode}__{cfg.recovery_mode}"


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    if not BUNDLE.is_dir():
        (OUT / "phase2d_verdict.json").write_text(
            json.dumps({"verdict": "BLOCKED", "reason": "SEP_BUNDLE_MISSING"}), indent=2
        )
        return 1

    session = FrozenBundleDensitySessionV1(BUNDLE)
    closes = session.decision_closes()
    start_idx = closes.index(PILOT_START_UTC)
    end_idx = closes.index(PILOT_END_UTC)
    gate = evaluate_pool_history_gate(close_index=start_idx)
    lookback_idx = closes.index(LOOKBACK_168H) if LOOKBACK_168H in closes else None
    coverage = {
        "pilot_window_utc": {"start": PILOT_START_UTC, "end": PILOT_END_UTC},
        "lookback_168h_utc": LOOKBACK_168H,
        "gate_at_pilot": asdict(gate),
        "lookback_idx": lookback_idx,
    }
    if not gate.ok or lookback_idx is None:
        (OUT / "phase2d_verdict.json").write_text(
            json.dumps({"verdict": "BLOCKED", "coverage": coverage}), indent=2
        )
        return 1

    ohlc_start = _iso(_parse_utc(closes[0]) - timedelta(hours=720 + 48))
    _, prices, ohlc_meta = load_5m_ohlc_ch(ohlc_start, closes[-1])
    if any(c not in prices for c in closes):
        (OUT / "phase2d_verdict.json").write_text(
            json.dumps({"verdict": "BLOCKED", "reason": "OHLC_GAP"}), indent=2
        )
        return 1

    emas = ema_map_for_bars(sorted(prices.keys()), prices)
    ex_idx = closes.index(EXAMPLE_BAR)
    streak_ex = streak_at_close(session, closes, ex_idx)

    example_snap = session.collect_at_close(EXAMPLE_BAR)
    close_prev = float(prices[EXAMPLE_BAR]["c"])
    v1_explain = explain_v1_prefers_narrow_pool(example_snap, close_prev)
    strong_sel, _ = select_support_v2d(
        example_snap,
        market_price=close_prev,
        config=LongResearchConfigV2DV1("V2D_STRONG", "DEEPER_Q_UPPER", "NEXT_BAR"),
        persistence_bars_by_pool_key=streak_ex,
    )

    matrix_results = {}
    for cfg in MATRIX_2D_BASELINE:
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
            eval_start_index=start_idx,
            eval_end_index=end_idx,
        )
        key = _matrix_key(cfg)
        matrix_results[key] = {
            "config": cfg.label(),
            "summary": r.summary,
            "touch_forensics": r.touch_forensics,
            "places": [e for e in r.entry_events if e.get("action") == "PLACE"],
            "fills": [e for e in r.entry_events if e.get("action") == "FILL"],
        }
        (OUT / f"matrix_{key}.json").write_text(
            json.dumps(
                {
                    "summary": r.summary,
                    "order_event_ledger": r.order_event_ledger,
                    "trades": [asdict(t) for t in r.trades],
                    "touch_forensics": r.touch_forensics,
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    recovery_compare = {}
    base_cfg = LongResearchConfigV2DV1("V2D_STRONG", "DEEPER_Q_UPPER", "NEXT_BAR")
    for rec in ("NEXT_BAR", "SAME_BAR_TOUCH_CLOSE"):
        cfg = LongResearchConfigV2DV1(base_cfg.support_mode, base_cfg.sl_mode, rec)  # type: ignore[arg-type]
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
            eval_start_index=start_idx,
            eval_end_index=end_idx,
        )
        recovery_compare[rec] = r.summary

    strength_rules = {
        "MIN_REGION_WIDTH_PCT": 0.10,
        "MIN_SINGLE_ATOMIC_WIDTH_PCT": 0.12,
        "MIN_DISTINCT_ORIGINS": 2,
        "score": "1000*distinct + 100*atomics + 10*max_atomic_origins + 5*width_pct + 2*density + 0.5*persist",
        "entry_rank": "max strength score, then max upper_edge (below price), then pool_key",
        "v1_baseline_rank": "max upper_edge, min lower (wider), pool_key — prefers closest/narrow stack neighbor",
        "sl_new": "Q.upper_edge where Q is next qualified deeper pool (weak intermediates skipped in V2D_STRONG)",
        "sl_legacy": "P.lower_edge * 0.998",
        "gap": "lower_gap_pct = (P.lower - Q.upper)/P.lower * 100 >= 0.6",
        "risk_pct": "(entry_fill - SL) / entry_fill * 100; max_risk_pct config None = uncapped research",
    }

    report = {
        "verdict": "PHASE2D_RESEARCH_COMPLETE",
        "coverage": coverage,
        "ohlc_meta": ohlc_meta,
        "strength_and_sl_rules": strength_rules,
        "why_v1_picks_small_pool_at_17_15Z": v1_explain,
        "stronger_support_at_example": (
            {
                "region": strong_sel.region.to_audit_dict(),
                "sl_region": strong_sel.sl_region.to_audit_dict(),
                "entry_limit": strong_sel.entry_limit,
                "structural_sl": strong_sel.structural_sl,
                "sl_reference_mode": strong_sel.sl_reference_mode,
                "qualified_gap_pct": strong_sel.qualified_gap_pct,
                "strength_audit": strong_sel.strength_audit,
                "skipped": list(strong_sel.skipped_intermediate_pools),
            }
            if strong_sel
            else None
        ),
        "matrix_2x2_summaries": {k: v["summary"] for k, v in matrix_results.items()},
        "recovery_mode_compare_V2D_STRONG_SL_Q": recovery_compare,
        "open_decisions": [
            "max_risk_pct release threshold not set (research uncapped)",
            "Whether SAME_BAR recovery becomes production rule",
            "Whether V2D_STRONG + DEEPER_Q_UPPER replaces V1 baseline",
        ],
        "profitability_claim": "none — count only causal fills in matrix summaries",
    }
    (OUT / "phase2d_report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    (OUT / "phase2d_verdict.json").write_text(json.dumps({"verdict": report["verdict"]}, indent=2))
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
