#!/usr/bin/env python3
"""LONG V3 Phase 2E — qualified support (5 bars, 0.1% height) research."""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PKG = Path(__file__).resolve().parent
OUT = ROOT / "logs/research/long_v3_phase2e_research_v1"
BUNDLE = ROOT / "results/phase_bt_d92_september_2026_frozen_v1/cache_bundle"
YAML = ROOT / "configs/long_v3_trend_continuation_research_v1.yaml"
SHORT_YAML = ROOT / "configs/short_main_v2_combined_exit_research_v1.yaml"

PILOT_START = "2026-09-20T00:00:00Z"
PILOT_END = "2026-09-21T23:55:00Z"
FORENSIC_START = "2026-09-20T17:15:00Z"
FORENSIC_END = "2026-09-20T19:30:00Z"
NARROW_POOL = (0.086776, 0.08679)

sys.path.insert(0, str(PKG))
sys.path.insert(0, str(ROOT / "tools/phase_bt_d59_market_regime_pool_evolution_v1"))
sys.path.insert(0, str(ROOT / "tools/phase_bt_d60_ema_history_validation_v1"))

from density_session_adapter_v1 import FrozenBundleDensitySessionV1  # noqa: E402
from ema_research_v1 import ema_map_for_bars  # noqa: E402
from long_config_yaml_v1 import load_long_research_yaml  # noqa: E402
from long_research_config_v2d_v1 import LongResearchConfigV2DV1  # noqa: E402
from long_risk_audit_v1 import risk_pct  # noqa: E402
from long_support_selection_v2e_v1 import (  # noqa: E402
    audit_region_qualification,
    region_pool_height_pct,
    select_support_v2e,
)
from long_support_selection_v1 import select_support_below_price  # noqa: E402
from ohlc_loader_v1 import load_5m_ohlc_ch, _iso, _parse_utc  # noqa: E402
from pool_consecutive_streak_v2e_v1 import update_consecutive_lower_streaks  # noqa: E402
from pool_history_gate_v1 import evaluate_pool_history_gate  # noqa: E402


from research_runner_v2d_v1 import run_long_research_v2d  # noqa: E402


def _short_age_audit() -> dict:
    import yaml

    short_cfg = yaml.safe_load(SHORT_YAML.read_text(encoding="utf-8"))
    return {
        "active_short_main_v2_combined_exit_yaml": str(SHORT_YAML),
        "top_level_keys": list(short_cfg.keys()),
        "pool_min_age_bars_in_this_yaml": None,
        "finding": (
            "SHORT MAIN V2 Phase1 combined_exit research YAML defines only lower_pool_gap_tp + ema_cross_exit; "
            "no pool_min_age_bars. D100 research overlays (not active in SHORT MAIN V2 unit tests) define "
            "pool_min_age_bars=5 with pool_min_age_mode=consecutive_active_depth2_v1 on exact atomic upper bands."
        ),
        "d100_reference_variants": {
            "baseline_frozen": {"pool_min_age_bars": 0, "mode": "legacy_cumulative_v1"},
            "d100_b": {"pool_min_age_bars": 5, "mode": "consecutive_active_depth2_v1"},
            "d100_c": {"pool_min_age_bars": 5, "mode": "consecutive_active_depth2_v1"},
        },
        "short_main_v2_runner_legacy_age": (
            "research_execution_runner increments pool_age per ACTIVE upper band each bar when review_pending "
            "uses legacy_cumulative if min_age>0 in passed config"
        ),
        "long_v2e_alignment": (
            "incremental consecutive ACTIVE on exact lower atomic band keys (pool_consecutive_streak_v2e_v1); "
            "walk-back audit via pool_age_consecutive_lower_v1 in unit tests"
        ),
    }


def _forensic_bar(
    session,
    prices,
    closes,
    bar,
    bar_index,
    yaml_sup,
    live_streak,
) -> dict:
    o = prices[bar]
    close = float(o["c"])
    v1, _ = select_support_below_price(session.collect_at_close(bar), market_price=close)
    v2e, _, pa = select_support_v2e(
        session,
        decision_close_utc=bar,
        market_price=close,
        prices=prices,
        yaml=yaml_sup,
        live_consecutive_streak=live_streak,
        bar_index=bar_index,
        closes=closes,
    )
    narrow_h = region_pool_height_pct(
        type("R", (), {"lower_edge": NARROW_POOL[0], "upper_edge": NARROW_POOL[1]})()
    )
    row = {
        "bar_utc": bar,
        "close": close,
        "narrow_pool_height_pct": round(narrow_h, 6),
        "narrow_excluded_by_height": narrow_h + 1e-12 < yaml_sup.min_pool_height_pct,
        "v1_entry_upper": v1.region.upper_edge if v1 else None,
        "v2e_P": v2e.region.to_audit_dict() if v2e else None,
        "v2e_Q": v2e.sl_region.to_audit_dict() if v2e else None,
        "v2e_limit": v2e.entry_limit if v2e else None,
        "v2e_sl": v2e.structural_sl if v2e else None,
        "v2e_gap_pct": v2e.qualified_gap_pct if v2e else None,
        "v2e_risk_at_limit_pct": round(risk_pct(v2e.entry_limit, v2e.structural_sl), 6)
        if v2e and v2e.structural_sl < v2e.entry_limit
        else None,
        "v2e_audit": pa.__dict__ if pa else None,
    }
    return row


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    lyaml = load_long_research_yaml(YAML)
    yaml_sup = lyaml.support

    variants = {
        "V1_BASELINE": LongResearchConfigV2DV1("V1_BASELINE", "LEGACY_BELOW_P_LOWER", "NEXT_BAR"),
        "V2D_STRONG": LongResearchConfigV2DV1("V2D_STRONG", "DEEPER_Q_UPPER", "NEXT_BAR"),
        "V2E_NEXT_BAR": LongResearchConfigV2DV1(
            "V2E_QUALIFIED",
            "DEEPER_Q_UPPER",
            "NEXT_BAR",
            v2e_yaml=yaml_sup,
            max_risk_pct=lyaml.max_risk_pct,
        ),
        "V2E_SAME_BAR": LongResearchConfigV2DV1(
            "V2E_QUALIFIED",
            "DEEPER_Q_UPPER",
            "SAME_BAR_TOUCH_CLOSE",
            v2e_yaml=yaml_sup,
            max_risk_pct=lyaml.max_risk_pct,
        ),
    }

    session = FrozenBundleDensitySessionV1(BUNDLE)
    closes = session.decision_closes()
    start_idx = closes.index(PILOT_START)
    gate = evaluate_pool_history_gate(close_index=start_idx)
    ohlc_start = _iso(_parse_utc(closes[0]) - timedelta(hours=720 + 48))
    _, prices, ohlc_meta = load_5m_ohlc_ch(ohlc_start, closes[-1])
    emas = ema_map_for_bars(sorted(prices.keys()), prices)

    matrix = {}
    for name, cfg in variants.items():
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
            eval_end_index=closes.index(PILOT_END),
        )
        matrix[name] = r.summary
        (OUT / f"run_{name}.json").write_text(
            json.dumps(
                {
                    "summary": r.summary,
                    "touch_forensics": r.touch_forensics[-50:],
                    "places": [e for e in r.entry_events if e.get("action") == "PLACE"],
                    "fills": [e for e in r.entry_events if e.get("action") == "FILL"],
                },
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )

    forensic = []
    live_streak: dict[str, int] = {}
    i0, i1 = closes.index(FORENSIC_START), closes.index(FORENSIC_END)
    for i in range(0, i1 + 1):
        update_consecutive_lower_streaks(session.collect_at_close(closes[i]), live_streak)
    for i in range(i0, i1 + 1):
        forensic.append(
            _forensic_bar(session, prices, closes, closes[i], i, yaml_sup, live_streak)
        )

    report = {
        "verdict": "PHASE2E_RESEARCH_COMPLETE",
        "long_yaml_loaded": {
            "path": str(YAML),
            "enabled": lyaml.enabled,
            "support": asdict(yaml_sup),
            "max_risk_pct": lyaml.max_risk_pct,
        },
        "short_pool_age_audit": _short_age_audit(),
        "pilot_window": {"start": PILOT_START, "end": PILOT_END},
        "matrix_summaries": matrix,
        "forensic_17_15_to_19_30": forensic,
        "narrow_pool_086776_exclusion": {
            "height_pct": region_pool_height_pct(
                type("R", (), {"lower_edge": NARROW_POOL[0], "upper_edge": NARROW_POOL[1]})()
            ),
            "min_required_pct": yaml_sup.min_pool_height_pct,
            "excluded": region_pool_height_pct(
                type("R", (), {"lower_edge": NARROW_POOL[0], "upper_edge": NARROW_POOL[1]})()
            )
            < yaml_sup.min_pool_height_pct,
        },
        "ohlc_meta": ohlc_meta,
        "gate": asdict(gate),
        "open_decisions": [
            "max_risk_pct sensitivity 1.0/1.5/2.0/3.0 (research only)",
            "Hybrid nearest-qualified within max distance below price",
            "Production enable gate in YAML remains false",
        ],
    }
    (OUT / "phase2e_report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"matrix": matrix, "narrow_excluded": report["narrow_pool_086776_exclusion"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
