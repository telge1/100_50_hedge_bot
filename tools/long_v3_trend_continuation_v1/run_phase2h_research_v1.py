#!/usr/bin/env python3
"""LONG V3 Phase 2H — region lineage + 4-way variant matrix + SAME_BAR forensics."""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PKG = Path(__file__).resolve().parent
OUT = ROOT / "logs/research/long_v3_phase2h_lineage_v1"
BUNDLE = ROOT / "results/phase_bt_d92_september_2026_frozen_v1/cache_bundle"
YAML = ROOT / "configs/long_v3_trend_continuation_research_v1.yaml"

PRIMARY_START = "2026-09-16T00:00:00Z"
PRIMARY_END = "2026-09-17T23:55:00Z"
CONTROL_START = "2026-09-20T00:00:00Z"
CONTROL_END = "2026-09-21T23:55:00Z"
LOOKBACK_168H = "2026-09-09T00:00:00Z"
ANCHOR_LO, ANCHOR_HI = 0.079758, 0.079970

sys.path.insert(0, str(PKG))
sys.path.insert(0, str(ROOT / "tools/phase_bt_d59_market_regime_pool_evolution_v1"))
sys.path.insert(0, str(ROOT / "tools/phase_bt_d60_ema_history_validation_v1"))

from causal_region_lineage_v2h_v1 import CausalLineageTrackerV2H, build_anchor_timeline  # noqa: E402
from density_session_adapter_v1 import FrozenBundleDensitySessionV1  # noqa: E402
from ema_research_v1 import ema_map_for_bars  # noqa: E402
from long_config_yaml_v1 import load_long_research_yaml  # noqa: E402
from long_research_config_v2d_v1 import LongResearchConfigV2DV1  # noqa: E402
from long_support_selection_v2e_v1 import region_pool_height_pct  # noqa: E402
from ohlc_loader_v1 import load_5m_ohlc_ch, _iso, _parse_utc  # noqa: E402
from pool_consecutive_streak_v2e_v1 import update_consecutive_lower_streaks  # noqa: E402
from pool_history_gate_v1 import evaluate_pool_history_gate  # noqa: E402
from research_runner_v2d_v1 import run_long_research_v2d  # noqa: E402


def _variant_matrix(yaml_sup, max_risk) -> dict[str, LongResearchConfigV2DV1]:
    base = dict(
        support_mode="V2E_QUALIFIED",
        sl_mode="DEEPER_Q_UPPER",
        v2e_yaml=yaml_sup,
        max_risk_pct=max_risk,
    )
    return {
        "EXACT_ATOMIC_5__NEXT_BAR": LongResearchConfigV2DV1(
            **base, recovery_mode="NEXT_BAR", pool_age_semantics="EXACT_ATOMIC_5"
        ),
        "EXACT_ATOMIC_5__SAME_BAR": LongResearchConfigV2DV1(
            **base, recovery_mode="SAME_BAR_TOUCH_CLOSE", pool_age_semantics="EXACT_ATOMIC_5"
        ),
        "CAUSAL_REGION_LINEAGE_5__NEXT_BAR": LongResearchConfigV2DV1(
            **base, recovery_mode="NEXT_BAR", pool_age_semantics="CAUSAL_REGION_LINEAGE_5"
        ),
        "CAUSAL_REGION_LINEAGE_5__SAME_BAR": LongResearchConfigV2DV1(
            **base, recovery_mode="SAME_BAR_TOUCH_CLOSE", pool_age_semantics="CAUSAL_REGION_LINEAGE_5"
        ),
    }


def _run_window(
    *,
    name: str,
    session,
    closes,
    prices,
    emas,
    i0,
    i1,
    cfg: LongResearchConfigV2DV1,
) -> dict:
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
    risks = [t.risk_pct for t in r.trades if t.risk_pct is not None]
    return {
        "label": cfg.label(),
        "blocked": r.blocked,
        "block_reason": r.block_reason,
        "summary": r.summary,
        "open_at_end": r.open_position_at_end,
        "ambiguous": r.ambiguous_cases,
        "risk_pct_at_fill": risks,
        "trades": [asdict(t) for t in r.trades],
        "entry_events": r.entry_events,
        "touch_forensics": r.touch_forensics,
    }


def _fill_forensic(run: dict) -> list[dict]:
    fills = [e for e in run["entry_events"] if e.get("action") == "FILL"]
    out = []
    for f in fills:
        pid = f.get("order_id") or f.get("detail", {}).get("order_id")
        related = [e for e in run["entry_events"] if e.get("order_id") == pid or e.get("detail", {}).get("order_id") == pid]
        places = [e for e in run["entry_events"] if e.get("action") == "PLACE"]
        place = next(
            (
                p
                for p in places
                if (p.get("bar") and f.get("bar") and p.get("bar") <= f.get("bar"))
                or abs((p.get("limit") or 0) - (f.get("fill_price") or f.get("price") or 0)) < 1e-6
            ),
            None,
        )
        trade = next((t for t in run["trades"] if t.get("fill_bar_utc") == f.get("bar")), None)
        touches = [t for t in run["touch_forensics"] if t.get("event") == "TOUCH_BEGIN"]
        out.append(
            {
                "fill_event": f,
                "matching_trade": trade,
                "place_event": place,
                "touch_begins_before_fill": touches,
                "event_timeline_utc": sorted(
                    [{"bar": e.get("bar"), "action": e.get("action"), **{k: v for k, v in e.items() if k not in ("bar", "action")}} for e in run["entry_events"]],
                    key=lambda x: x.get("bar") or "",
                ),
                "causal_validity": _assess_same_bar_causal(f, place, trade, run["touch_forensics"]),
            }
        )
    return out


def _assess_same_bar_causal(fill, place, trade, touch_audit) -> dict:
    if fill is None:
        return {"status": "NO_FILL"}
    bar = fill.get("bar")
    place_bar = place.get("bar") if place else None
    earliest = (place or {}).get("earliest") or (place or {}).get("detail", {}).get("earliest")
    touch_same = [t for t in touch_audit if t.get("bar") == bar and t.get("event") == "TOUCH_BEGIN"]
    retro_touch_fill = bool(touch_same and place_bar == bar and earliest and earliest <= bar)
    return {
        "status": "VALID_CAUSAL" if trade and place and not retro_touch_fill else "INDETERMINATE_OR_INVALID",
        "fill_bar": bar,
        "place_bar": place_bar,
        "earliest_execution_utc": earliest,
        "touch_on_fill_bar": bool(touch_same),
        "note": "NEXT_BAR baseline requires fill after place bar; SAME_BAR confirms recovery same close only",
    }


def _height_audit_snippet() -> dict:
    from lower_pool_gap_tp_v1 import LowerRegionV1, AtomicLowerBandV1

    growing = LowerRegionV1(
        0.079758,
        0.08003,
        [AtomicLowerBandV1("k", 0.079758, 0.08003, ("a", "b"), 2)],
        ("a", "b"),
    )
    h_small = region_pool_height_pct(LowerRegionV1(0.079758, 0.07997, growing.atomics, growing.distinct_origin_ids))
    h_big = region_pool_height_pct(growing)
    yaml_h = 0.1
    return {
        "operator": ">=",
        "code_reference": "height + 1e-12 >= yaml.min_pool_height_pct",
        "example_079758_07997_pct": round(h_small, 6),
        "example_079758_08003_pct": round(h_big, 6),
        "both_pass_0_1_threshold": h_small + 1e-12 >= yaml_h and h_big + 1e-12 >= yaml_h,
        "no_max_height_cap": True,
    }


def _build_lineage_timeline(session, closes, prices, i0, i1, yaml_sup) -> list[dict]:
    live: dict[str, int] = {}
    tracker = CausalLineageTrackerV2H(
        min_unique_origins=yaml_sup.min_unique_origins,
        min_age_bars=yaml_sup.min_pool_age_bars,
    )
    for i in range(0, i1 + 1):
        snap = session.collect_at_close(closes[i])
        update_consecutive_lower_streaks(snap, live, min_unique_origins=yaml_sup.min_unique_origins)
        tracker.advance(
            bar_utc=closes[i],
            regions=tracker.regions_from_snapshot(snap),
            market_close=float(prices[closes[i]]["c"]),
            atomic_streak=live,
        )
    return build_anchor_timeline(
        tracker,
        anchor_lo=ANCHOR_LO,
        anchor_hi=ANCHOR_HI,
        start_bar=closes[i0],
        end_bar=closes[i1],
    )


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    ly = load_long_research_yaml(YAML)
    yaml_sup = ly.support

    session = FrozenBundleDensitySessionV1(BUNDLE)
    closes = session.decision_closes()
    ohlc_start = _iso(_parse_utc(closes[0]) - timedelta(hours=720 + 48))
    _, prices, ohlc_meta = load_5m_ohlc_ch(ohlc_start, closes[-1])
    emas = ema_map_for_bars(sorted(prices.keys()), prices)

    i0 = closes.index(PRIMARY_START)
    i1 = closes.index(PRIMARY_END)
    gate = evaluate_pool_history_gate(close_index=i0)

    timeline = _build_lineage_timeline(session, closes, prices, i0, min(i0 + 96, i1), yaml_sup)
    reset_rows = [r for r in timeline if r["utc"] in ("2026-09-16T00:40:00Z", "2026-09-16T00:45:00Z")]

    matrix = _variant_matrix(yaml_sup, ly.max_risk_pct)
    primary_runs = {
        k: _run_window(name=k, session=session, closes=closes, prices=prices, emas=emas, i0=i0, i1=i1, cfg=v)
        for k, v in matrix.items()
    }

    same_bar = primary_runs["EXACT_ATOMIC_5__SAME_BAR"]
    fill_forensics = _fill_forensic(same_bar)
    if len(fill_forensics) < 2:
        alt = primary_runs["CAUSAL_REGION_LINEAGE_5__SAME_BAR"]
        fill_forensics = _fill_forensic(alt) + fill_forensics

    ci0, ci1 = closes.index(CONTROL_START), closes.index(CONTROL_END)
    gate_c = evaluate_pool_history_gate(close_index=ci0)
    control_runs = {}
    if gate_c.ok:
        control_runs = {
            k: _run_window(name=k, session=session, closes=closes, prices=prices, emas=emas, i0=ci0, i1=ci1, cfg=v)
            for k, v in matrix.items()
        }
    else:
        control_runs = {"_blocked": {"reason": gate_c.reason_code}}

    report = {
        "verdict": "PHASE2H_LINEAGE_COMPLETE",
        "height_audit": _height_audit_snippet(),
        "causal_region_lineage_5_definition": {
            "summary": (
                "Pro Bar: jede Depth-2-Union wird höchstens einer Vorgänger-Union zugeordnet, wenn "
                "Preis-Overlap (oder adjazent) UND mindestens ein gemeinsamer ACTIVE-Origin-ID. "
                "Streak zählt consecutive Bars pro lineage_id; kein Match → neues lin_v2h_* (Reset). "
                "Datenlücke → UNDETERMINED, kein Altertransfer. Formation nutzt Union-Oberkante am Lineage-Streak-Startbar."
            ),
            "continuity_classes": [c.value for c in __import__(
                "causal_region_lineage_v2h_v1", fromlist=["LineageContinuityClassV2H"]
            ).LineageContinuityClassV2H],
        },
        "anchor_region_timeline_079758_079970": timeline,
        "age_reset_00_40_00_45": reset_rows,
        "primary_window": {"start": PRIMARY_START, "end": PRIMARY_END, "gate": asdict(gate)},
        "control_window_predeclared": {
            "start": CONTROL_START,
            "end": CONTROL_END,
            "gate": asdict(gate_c),
            "note": "48h Kontrolle Sep 20–21 (Phase 2E), Regeln nicht daran angepasst",
        },
        "variant_comparison_primary": {k: {kk: vv for kk, vv in v.items() if kk != "entry_events"} for k, v in primary_runs.items()},
        "same_bar_fill_forensics": fill_forensics[:4],
        "control_variant_summaries": {
            k: v.get("summary") if isinstance(v, dict) else v for k, v in control_runs.items()
        },
        "ohlc_meta": ohlc_meta,
        "recommendations": {
            "region_persistence": (
                "Atomic-5 reset bei Union-Wachstum ist kausal korrekt für EXACT_ATOMIC_5, "
                "behandelt fortbestehende SSOT-Unterstützung aber als 'neu'. "
                "CAUSAL_REGION_LINEAGE_5 hält Streak bei Origin+Preis-Overlap — nur Forschung."
            ),
            "same_bar_recovery": (
                "SAME_BAR erzeugt Fills ohne NEXT_BAR-PLACE-Pfad; kausal nur wenn PLACE earliest >= nächster Open "
                "und kein Fill auf Touch-Kerze. Forensik pro Fill ausweisen."
            ),
            "profitability": "Keine Ableitung aus einem 48h-Fenster.",
        },
    }
    (OUT / "phase2h_report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    for k, v in primary_runs.items():
        if v.get("entry_events"):
            (OUT / f"events_{k}.json").write_text(
                json.dumps({"events": v["entry_events"], "trades": v["trades"]}, indent=2, default=str),
                encoding="utf-8",
            )
    print(json.dumps({"verdict": report["verdict"], "variants": {k: v["summary"] for k, v in primary_runs.items()}}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
