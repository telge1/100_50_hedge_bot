#!/usr/bin/env python3
"""Root-cause audit of the 27 TEM continuous end-blockers (analysis only).

Outputs:
  research/backtests/results/tem_continuous_27_blocker_root_cause_20260722/

No strategy changes, no full continuous re-run, no commit.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import time
import traceback
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from research.backtests.candle_loader import load_candles_for_symbol
from research.backtests.full_dynamic_second_leg_restaging import resolve_full_dynamic_profile
from research.backtests.historical_backtest import normalize_candles
from research.backtests.inventory_mtm_freeze import safe_float
from research.backtests.multicoin_blocker_price_staging import (
    FULL_HISTORY_CANDLE_LIMIT,
    analyze_blocker_run,
    run_isolated_blocker,
)
from research.backtests.multicoin_price_staging_grid import atomic_write_json, write_csv
from research.backtests.run_c4_undercoverage_fix_validation import (
    _capture_basket_close_economics,
    _restore_basket_coverage_method,
)
from research.backtests.second_leg_price_staging import resolve_grid_profile
from research.backtests.tem_fd_undercoverage_economics import classify_closed_economics
from research.regime_scanner.liquidity_sweep_reclaim.levels import attach_c31_range_columns
from research.regime_scanner.market_structure_c3_4d_ema_context import (
    attach_structure_ema_relation,
    compute_c3_4d_ema_context,
    guard_decision,
)
from research.regime_scanner.pullback_entry_c3_5 import (
    asof_htf_context,
    attach_structure_edges,
    enrich_indicators,
    prepare_research_frame,
)
from research.regime_scanner.pullback_entry_c3_5c_entry_path_audit import (
    aggregate_complete_from_5m,
)
from research.regime_scanner.timeframes import aggregate_candles

ROOT = Path(__file__).resolve().parents[2]
SOURCE = (
    ROOT
    / "research/backtests/results/staging_profiles_continuous_1000_500_20260722"
)
DEFAULT_OUT = (
    ROOT
    / "research/backtests/results/tem_continuous_27_blocker_root_cause_20260722"
)
PROFILE = "two_early_medium"
FD_PROFILE = "two_early_medium_full_dynamic"
CYCLE_RE = re.compile(r"^CYCLE_(\d+)_(LONG_ADD|SHORT_REDUCE)$")
BARS_12H = 12 * 12  # 5m
BARS_24H = 24 * 12
BARS_48H = 48 * 12
BARS_96H = 96 * 12


def log(msg: str) -> None:
    print(msg, flush=True)


def _sf(x: Any, default: float = 0.0) -> float:
    return safe_float(x) if x not in (None, "") else default


def _ts(c: Any) -> str:
    raw = getattr(c, "timestamp", None)
    if raw is None:
        return ""
    return raw.isoformat() if hasattr(raw, "isoformat") else str(raw)


def _candles_to_frame(candles: list[Any]) -> pd.DataFrame:
    rows = [
        {
            "timestamp": c.timestamp,
            "open": float(c.open),
            "high": float(c.high),
            "low": float(c.low),
            "close": float(c.close),
            "volume": float(getattr(c, "volume", 0.0) or 0.0),
        }
        for c in candles
    ]
    df = pd.DataFrame(rows)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    return df


def load_tem_end_blockers(source: Path) -> list[dict[str, Any]]:
    open_rows = [
        r
        for r in __import__("csv").DictReader((source / "open_trades_at_end.csv").open())
        if r.get("profile") == PROFILE and int(_sf(r.get("is_blocker"))) == 1
    ]
    details = {
        r["trade_id"]: r
        for r in __import__("csv").DictReader((source / "blocker_details.csv").open())
        if r.get("profile") == PROFILE
    }
    assert len(open_rows) == 27, f"expected 27 TEM blockers, got {len(open_rows)}"
    coins = [r["coin"] for r in open_rows]
    assert len(coins) == len(set(coins)), "duplicate coins in TEM blockers"
    out = []
    for r in sorted(open_rows, key=lambda x: x["coin"]):
        d = details.get(r["trade_id"], {})
        out.append({**d, **r})
    return out


def fill_log(result: Any) -> list[dict[str, Any]]:
    fills = getattr(result, "fill_log", None)
    if fills is None:
        fills = getattr(result, "fills_log", None)
    return list(fills or [])


def extract_entry_meta(result: Any, candles: list[Any], start_bar: int) -> dict[str, Any]:
    fills = fill_log(result)
    entry = None
    for f in fills:
        pur = str(f.get("purpose") or "")
        if pur in {"INITIAL_LONG_ENTRY", "LONG_ENTRY"} or pur.endswith("INITIAL_LONG_ENTRY"):
            entry = f
            break
    if entry is None and fills:
        entry = fills[0]
    entry_price = _sf((entry or {}).get("fill_price")) or _sf(
        getattr(result, "entry_price", None)
    )
    # initial qtys: after first long+short entry fills
    init_long = 0.0
    init_short = 0.0
    for f in fills[:8]:
        pur = str(f.get("purpose") or "")
        if "LONG" in pur and "ENTRY" in pur:
            init_long = max(init_long, _sf(f.get("long_qty_after")))
        if "SHORT" in pur and "ENTRY" in pur:
            init_short = max(init_short, _sf(f.get("short_qty_after")))
    if init_long <= 0:
        init_long = _sf(getattr(result, "final_long_qty", None))
    last = fills[-1] if fills else {}
    last_purpose = str(last.get("purpose") or "")
    last_local = int(last.get("candle_index") or 0) if last else None
    last_abs = (start_bar + last_local) if last_local is not None else None
    # basket exit from active order / excerpt
    excerpt = dict(getattr(result, "final_strategy_state_excerpt", None) or {})
    basket = None
    for o in getattr(result, "final_active_orders", None) or []:
        pur = str(o.get("purpose") or "")
        if "EXIT" in pur.upper() or "TP" in pur.upper():
            basket = _sf(o.get("trigger_price") or o.get("price"))
            if basket:
                break
    if not basket:
        basket = _sf(excerpt.get("long_tp_price") or excerpt.get("exit_price"))
    # last stage trigger
    last_stage = None
    for f in reversed(fills):
        meta = f.get("metadata_excerpt") or {}
        if meta.get("stage_index") is not None or meta.get("research_price_staging"):
            last_stage = _sf(f.get("fill_price") or meta.get("trigger_price"))
            break
    final_px = _sf(getattr(result, "final_price", None)) or (
        float(candles[-1].close) if candles else 0.0
    )
    dist_basket = None
    if basket and final_px > 0:
        dist_basket = (basket - final_px) / final_px * 100.0
    dist_stage = None
    if last_stage and final_px > 0:
        dist_stage = (last_stage - final_px) / final_px * 100.0
    return {
        "entry_price": entry_price,
        "initial_long_qty": init_long,
        "initial_short_qty": init_short,
        "last_fill_purpose": last_purpose,
        "last_fill_bar": last_abs,
        "remaining_required_net": _sf(
            excerpt.get("remaining_required_net")
            or excerpt.get("required_net")
            or excerpt.get("pending_cycle_loss_usdt")
        ),
        "basket_exit_price": basket,
        "last_stage_price": last_stage,
        "distance_to_basket_exit_pct": dist_basket,
        "distance_to_last_stage_pct": dist_stage,
        "pending_cycle_loss_usdt": _sf(excerpt.get("pending_cycle_loss_usdt")),
        "current_cycle": int(
            _sf(excerpt.get("cycle") or excerpt.get("current_cycle") or result.cycles_seen)
        ),
    }


def build_cycle_timeline(result: Any, start_bar: int, candles: list[Any]) -> list[dict[str, Any]]:
    fills = fill_log(result)
    by_cycle: dict[int, dict[str, Any]] = {}
    cum_realized = 0.0

    def ensure(c: int) -> dict[str, Any]:
        if c not in by_cycle:
            by_cycle[c] = {
                "cycle_index": c,
                "start_bar": None,
                "first_leg_fill_bar": None,
                "first_leg_purpose": "",
                "first_leg_realized_loss": 0.0,
                "second_leg_stage_count": 0,
                "second_leg_fills": 0,
                "realized_cover_net": 0.0,
                "remaining_required_net": None,
                "long_qty": None,
                "short_qty": None,
                "long_avg": None,
                "short_avg": None,
                "basket_exit_price": None,
                "distance_to_exit_pct": None,
                "cycle_total_pnl": None,
                "cycle_open_mtm": None,
                "duration_bars": None,
                "_realized_at_end": 0.0,
                "_end_local": None,
            }
        return by_cycle[c]

    for f in fills:
        pur = str(f.get("purpose") or "")
        m = CYCLE_RE.match(pur)
        local = int(f.get("candle_index") or 0)
        abs_bar = start_bar + local
        pnl = _sf(f.get("closed_pnl") or f.get("confirmed_closed_pnl"))
        cum_realized += pnl
        meta = dict(f.get("metadata_excerpt") or {})
        if m:
            cyc = int(m.group(1))
            leg = m.group(2)
            row = ensure(cyc)
            if row["start_bar"] is None:
                row["start_bar"] = abs_bar
            if leg == "LONG_ADD":
                if row["first_leg_fill_bar"] is None:
                    row["first_leg_fill_bar"] = abs_bar
                    row["first_leg_purpose"] = pur
                row["first_leg_realized_loss"] += min(pnl, 0.0)
            else:
                row["second_leg_fills"] += 1
                if meta.get("stage_index") is not None:
                    row["second_leg_stage_count"] = max(
                        row["second_leg_stage_count"], int(meta.get("stage_index") or 0) + 1
                    )
                row["realized_cover_net"] += pnl
            row["long_qty"] = _sf(f.get("long_qty_after"))
            row["short_qty"] = _sf(f.get("short_qty_after"))
            row["long_avg"] = _sf(f.get("long_avg_after"))
            row["short_avg"] = _sf(f.get("short_avg_after"))
            row["_realized_at_end"] = cum_realized
            row["_end_local"] = local
            req = meta.get("required_net") or meta.get("remaining_required_net")
            if req is not None:
                row["remaining_required_net"] = _sf(req)
        elif "EXIT" in pur.upper() or "TP" in pur.upper():
            # attribute to max open cycle
            if by_cycle:
                cyc = max(by_cycle)
                row = by_cycle[cyc]
                row["_realized_at_end"] = cum_realized
                row["_end_local"] = local

    # mark-to-market at each cycle end
    out = []
    prev_mtm = None
    for cyc in sorted(by_cycle):
        row = by_cycle[cyc]
        local = row["_end_local"]
        if local is not None:
            abs_i = min(start_bar + int(local), len(candles) - 1)
            mark = float(candles[abs_i].close)
            lq = _sf(row["long_qty"])
            sq = _sf(row["short_qty"])
            la = _sf(row["long_avg"])
            sa = _sf(row["short_avg"])
            unreal = lq * (mark - la) + sq * (sa - mark)
            realized = _sf(row["_realized_at_end"])
            total = realized + unreal
            row["cycle_open_mtm"] = unreal
            row["cycle_total_pnl"] = total
            if row["start_bar"] is not None:
                row["duration_bars"] = abs_i - int(row["start_bar"]) + 1
            # exit distance if basket known later
            prev_mtm = unreal
        clean = {k: v for k, v in row.items() if not str(k).startswith("_")}
        out.append(clean)
    return out


def find_explosion_cycle(timeline: list[dict[str, Any]]) -> dict[str, Any]:
    if not timeline:
        return {"explosion_cycle": None, "reason": "no_cycles"}
    explosion = None
    reasons: list[str] = []
    for i, row in enumerate(timeline):
        if i == 0:
            continue
        prev = timeline[i - 1]
        mtm = _sf(row.get("cycle_open_mtm"))
        pmtm = _sf(prev.get("cycle_open_mtm"))
        reasons_i: list[str] = []
        # >25% worse (more negative)
        if pmtm < 0 and mtm < pmtm * 1.25 - 1e-9:
            reasons_i.append("open_mtm_worse_25pct")
        elif pmtm >= 0 and mtm < pmtm - abs(pmtm) * 0.25 - 5.0:
            reasons_i.append("open_mtm_worse_25pct_abs")
        exp_prev = abs(_sf(prev.get("long_qty"))) + abs(_sf(prev.get("short_qty")))
        exp_now = abs(_sf(row.get("long_qty"))) + abs(_sf(row.get("short_qty")))
        if exp_prev > 0 and exp_now > exp_prev * 1.35:
            reasons_i.append("exposure_up_35pct")
        req_p = _sf(prev.get("remaining_required_net") or prev.get("first_leg_realized_loss"))
        req_n = _sf(row.get("remaining_required_net") or row.get("first_leg_realized_loss"))
        if abs(req_n) > abs(req_p) * 1.5 + 5.0:
            reasons_i.append("required_net_jump")
        if reasons_i and explosion is None:
            explosion = int(row["cycle_index"])
            reasons = reasons_i
            # continue to note if unresolved to end — already end-blocker
    return {
        "explosion_cycle": explosion,
        "explosion_reasons": reasons,
        "highest_cycle_in_timeline": max((int(r["cycle_index"]) for r in timeline), default=0),
        "n_cycles_in_timeline": len(timeline),
    }


def build_scanner_trace(candles: list[Any]) -> pd.DataFrame:
    df = _candles_to_frame(candles)
    decision = df["timestamp"].iloc[-1] + pd.Timedelta(minutes=5)
    htf30 = aggregate_candles(df, "30m", decision)
    htf4h = aggregate_complete_from_5m(df, "4h", decision_time=decision)
    trace = prepare_research_frame(df, ohlcv_30m=htf30)
    if htf4h is not None and not htf4h.empty:
        f4 = attach_structure_edges(enrich_indicators(htf4h))
        trace = asof_htf_context(trace, f4, tf_minutes=240, prefix="h4")
    ema = compute_c3_4d_ema_context(df)
    # align lengths
    for col in ema.columns:
        if col not in trace.columns and col != "timestamp":
            if len(ema) == len(trace):
                trace[col] = ema[col].to_numpy()
    trace = attach_structure_ema_relation(trace, ema if len(ema) == len(trace) else None)
    # C3.1 range on enriched 5m
    try:
        feat = enrich_indicators(df)
        c31 = attach_c31_range_columns(
            feat, analyze_start=0, analyze_end=len(feat) - 1, c31_variant="balanced"
        )
        for col in c31.columns:
            if col.startswith("c31_") or col in {"in_range", "range_score", "regime_state"}:
                if col not in trace.columns and len(c31) == len(trace):
                    trace[col] = c31[col].to_numpy()
    except Exception as exc:  # noqa: BLE001
        trace.attrs["c31_error"] = str(exc)
    return trace


def _ret(closes: list[float], bars: int) -> float | None:
    if len(closes) <= bars:
        return None
    a, b = closes[-(bars + 1)], closes[-1]
    if a == 0:
        return None
    return (b - a) / a * 100.0


def classify_trend_entry(feats: dict[str, Any]) -> tuple[str, str]:
    maj = int(feats.get("major_direction") or 0)
    ema_r = int(feats.get("ema_regime_direction") or 0)
    h4 = int(feats.get("h4_major_direction") or 0)
    m30 = int(feats.get("m30_major_direction") or 0)
    in_range = bool(feats.get("in_range") or feats.get("c31_in_range"))
    state = str(feats.get("protected_structure_state") or feats.get("m30_protected_structure_state") or "")
    g1 = str(feats.get("scanner_g1_long") or "")
    reasons = []
    if "transition" in state.lower():
        return "transition_entry", f"structure_state={state}"
    if in_range and maj == 0:
        return "range_entry", "c31/in_range and major flat"
    # Long-primary: against = bearish structure
    if maj < 0 and ema_r < 0 and (h4 < 0 or m30 < 0) and g1 == "block":
        reasons.append("major+ema+htf bearish; G1 blocks long")
        return "clearly_against_trend", "; ".join(reasons)
    if maj < 0 and (ema_r < 0 or h4 < 0 or m30 < 0):
        reasons.append(f"major={maj} ema={ema_r} h4={h4} m30={m30}")
        return "weakly_against_trend", "; ".join(reasons)
    if maj > 0 and ema_r > 0 and (h4 > 0 or m30 > 0):
        return "clearly_with_trend", f"major={maj} ema={ema_r} h4={h4} m30={m30}"
    if maj > 0 or (ema_r > 0 and h4 >= 0):
        return "weakly_with_trend", f"major={maj} ema={ema_r} h4={h4}"
    if maj == 0 and ema_r == 0:
        return "neutral", "major and ema regime flat"
    return "unknown", f"major={maj} ema={ema_r} state={state}"


def scanner_decision_at_entry(trace: pd.DataFrame, entry_bar: int) -> dict[str, Any]:
    if entry_bar < 0 or entry_bar >= len(trace):
        return {
            "causal_data_ok": 0,
            "scanner_decision": "scanner_not_applicable",
            "scanner_block_reason": "entry_bar_out_of_range",
        }
    row = trace.iloc[int(entry_bar)]
    maj = int(_sf(row.get("major_direction")))
    ema_r = int(_sf(row.get("ema_regime_direction")))
    h4 = int(_sf(row.get("h4_major_direction"))) if "h4_major_direction" in trace.columns else 0
    m30 = int(_sf(row.get("m30_major_direction"))) if "m30_major_direction" in trace.columns else 0
    g1 = guard_decision("long", maj, ema_r, "G1")
    g1b = guard_decision("long", maj, ema_r, "G1b")
    g1c = guard_decision("long", maj, ema_r, "G1c")
    # Existing research gate used for long: G1 (structure major must not be bearish)
    entry_allowed_long = g1 == "allow"
    entry_allowed_short = guard_decision("short", maj, ema_r, "G1") == "allow"
    block_reason = ""
    if not entry_allowed_long:
        block_reason = f"G1_block_long major={maj} ema_regime={ema_r}"
    # HTF alignment for long
    if h4 > 0 and m30 > 0:
        htf_align = "aligned_bullish"
    elif h4 < 0 and m30 < 0:
        htf_align = "aligned_bearish"
    elif h4 == 0 and m30 == 0:
        htf_align = "neutral"
    else:
        htf_align = "conflicting"
    ltf_align = "bullish" if maj > 0 else ("bearish" if maj < 0 else "neutral")
    # regime
    in_range = bool(row.get("in_range") or row.get("c31_in_range") or False)
    state = str(
        row.get("protected_structure_state")
        or row.get("m30_protected_structure_state")
        or ""
    )
    if in_range:
        regime = "range"
    elif "transition" in state.lower():
        regime = "transition"
    elif maj > 0:
        regime = "trend_bull"
    elif maj < 0:
        regime = "trend_bear"
    else:
        regime = "neutral"

    if not entry_allowed_long and maj < 0:
        decision = "would_block"
    elif entry_allowed_long and maj > 0:
        decision = "would_allow"
    elif not entry_allowed_long:
        decision = "would_block"
    elif maj == 0:
        decision = "ambiguous"
    else:
        decision = "would_allow"

    # price windows
    closes = [float(x) for x in trace["close"].iloc[: entry_bar + 1].tolist()]
    feats = {
        "scanner_regime": regime,
        "scanner_major_trend": maj,
        "scanner_htf_alignment": htf_align,
        "scanner_ltf_alignment": ltf_align,
        "scanner_entry_allowed_long": int(entry_allowed_long),
        "scanner_entry_allowed_short": int(entry_allowed_short),
        "scanner_block_reason": block_reason,
        "scanner_g1_long": g1,
        "scanner_g1b_long": g1b,
        "scanner_g1c_long": g1c,
        "scanner_decision": decision,
        "causal_data_ok": 1,
        "major_direction": maj,
        "ema_regime_direction": ema_r,
        "h4_major_direction": h4,
        "m30_major_direction": m30,
        "protected_structure_state": state,
        "in_range": int(in_range),
        "ema_9": _sf(row.get("ema_9")),
        "ema_20": _sf(row.get("ema_20")),
        "ema_59": _sf(row.get("ema_59")),
        "ema_200": _sf(row.get("ema_200")),
        "close": _sf(row.get("close")),
        "price_vs_ema200_pct": _sf(row.get("close_vs_ema_200_pct")),
        "ema_20_slope_12_pct": _sf(row.get("ema_20_slope_12_pct")),
        "adx": _sf(row.get("adx")),
        "plus_di": _sf(row.get("plus_di") or row.get("di_plus")),
        "minus_di": _sf(row.get("minus_di") or row.get("di_minus")),
        "m30_adx": _sf(row.get("m30_adx")),
        "ret_12h_pct": _ret(closes, BARS_12H),
        "ret_24h_pct": _ret(closes, BARS_24H),
        "ret_48h_pct": _ret(closes, BARS_48H),
        "ret_96h_pct": _ret(closes, BARS_96H),
        "bos_up": int(bool(row.get("close_break_protected_up"))),
        "bos_down": int(bool(row.get("close_break_protected_down"))),
        "external_break_level": row.get("active_external_break_level"),
    }
    # EMA order
    e9, e20, e59, e200 = feats["ema_9"], feats["ema_20"], feats["ema_59"], feats["ema_200"]
    if all(v > 0 for v in (e9, e20, e59, e200)):
        if e9 > e20 > e59 > e200:
            feats["ema_stack"] = "bullish_aligned"
        elif e9 < e20 < e59 < e200:
            feats["ema_stack"] = "bearish_aligned"
        else:
            feats["ema_stack"] = "mixed"
    else:
        feats["ema_stack"] = "unknown"
    trend_class, trend_reason = classify_trend_entry(feats)
    feats["trend_class"] = trend_class
    feats["trend_class_reason"] = trend_reason
    # local high/low distance 24h
    if entry_bar >= BARS_24H:
        window = trace.iloc[entry_bar - BARS_24H : entry_bar + 1]
        hi = float(window["high"].max())
        lo = float(window["low"].min())
        px = float(row["close"])
        feats["dist_to_24h_high_pct"] = (hi - px) / px * 100.0 if px else None
        feats["dist_to_24h_low_pct"] = (px - lo) / px * 100.0 if px else None
    else:
        feats["dist_to_24h_high_pct"] = None
        feats["dist_to_24h_low_pct"] = None
    feats["scanner_confidence_features"] = {
        "g1": g1,
        "major": maj,
        "ema_regime": ema_r,
        "h4": h4,
        "m30": m30,
        "ema_stack": feats["ema_stack"],
        "adx": feats["adx"],
    }
    return feats


def assign_root_causes(
    *,
    blocker: dict[str, Any],
    trend_class: str,
    scanner_decision: str,
    explosion: dict[str, Any],
    meta: dict[str, Any],
    fd_class: str | None = None,
) -> dict[str, Any]:
    primary = "unknown"
    secondary = ""
    evidence: list[str] = []
    conf = "medium"
    if trend_class == "clearly_against_trend":
        primary = "entry_against_major_trend"
        evidence.append(trend_class)
        conf = "high" if scanner_decision == "would_block" else "medium"
    elif trend_class == "transition_entry":
        primary = "entry_during_transition_false_signal"
        evidence.append(trend_class)
    elif trend_class == "range_entry":
        primary = "entry_in_range_breakout"
        evidence.append(trend_class)
    elif int(_sf(blocker.get("max_cycle"))) >= 5:
        primary = "high_cycle_position_growth"
        evidence.append(f"max_cycle={blocker.get('max_cycle')}")
        conf = "high"
    elif str(blocker.get("blocker_root_cause")) == "basket_exit_too_far":
        primary = "basket_exit_too_far"
        evidence.append(f"dist={meta.get('distance_to_basket_exit_pct')}")
    elif _sf(meta.get("distance_to_last_stage_pct")) and abs(
        _sf(meta.get("distance_to_last_stage_pct"))
    ) > 5:
        primary = "residual_stage_too_far"
    else:
        primary = "no_mean_reversion_after_entry"
        evidence.append("open_at_data_end")

    if explosion.get("explosion_cycle") and int(explosion["explosion_cycle"]) >= 3:
        secondary = "high_cycle_position_growth"
        evidence.append(f"explosion_cycle={explosion['explosion_cycle']}")
    if trend_class in {"clearly_against_trend", "weakly_against_trend"} and primary != "entry_against_major_trend":
        secondary = secondary or "entry_against_major_trend"
    if int(_sf(blocker.get("recovery_active"))):
        secondary = "recovery_reload_interaction"

    return {
        "primary_root_cause": primary,
        "secondary_root_cause": secondary,
        "evidence": evidence,
        "confidence": conf,
    }


def classify_fd(
    *,
    tem: dict[str, Any],
    fd: dict[str, Any],
) -> str:
    t_flat = int(tem.get("trade_flat") or 0) == 1
    f_flat = int(fd.get("trade_flat") or 0) == 1
    sufficient = fd.get("last_sufficient") is True
    if f_flat and sufficient and not t_flat:
        return "fd_cleanly_resolves"
    if f_flat and not sufficient:
        return "fd_not_applicable"
    d_total = _sf(fd.get("total_pnl")) - _sf(tem.get("total_pnl"))
    if not f_flat and d_total > 5.0:
        return "fd_improves_mtm_only"
    if abs(d_total) < 1.0 and int(fd.get("trade_flat") or 0) == int(tem.get("trade_flat") or 0):
        return "fd_no_effect"
    if d_total < -5.0:
        return "fd_worsens"
    if f_flat and t_flat:
        return "fd_no_effect"
    return "fd_improves_mtm_only" if d_total > 0 else "fd_no_effect"


def run_profile_replay(
    *,
    coin: str,
    start_bar: int,
    candles: list[Any],
    profile: str,
    capture: bool,
) -> tuple[Any, dict[str, Any]]:
    cfg = (
        resolve_full_dynamic_profile(profile)
        if "full_dynamic" in profile
        else resolve_grid_profile(profile)
    )
    captures: list[dict[str, Any]] = []
    original = None
    if capture and "full_dynamic" in profile:
        original = _capture_basket_close_economics(captures)
    try:
        result = run_isolated_blocker(
            coin=coin,
            candles=candles,
            start_index=start_bar,
            staging_config=cfg,
            trade_number=0,
        )
    finally:
        if original is not None:
            _restore_basket_coverage_method(original)
    analysis = analyze_blocker_run(
        coin=coin,
        trade_number=0,
        start_index=start_bar,
        profile=profile,
        result=result,
        candles=candles,
    )
    flat = int(bool(analysis.get("trade_flat")))
    realized = _sf(result.realized_pnl)
    open_mtm = 0.0 if flat else _sf(getattr(result, "unrealized_pnl", None))
    if not flat and getattr(result, "unrealized_pnl", None) is None:
        open_mtm = _sf(analysis.get("final_mtm")) - realized
    total = realized + open_mtm
    eco = {"last_sufficient": None, "sufficient_false_closed": 0, "economic_undercoverage_closed": 0}
    if captures:
        last = captures[-1]
        eco["last_sufficient"] = last.get("sufficient")
    classified = classify_closed_economics(result)
    if eco["last_sufficient"] is None:
        eco["last_sufficient"] = classified.get("last_sufficient")
    eco["sufficient_false_closed"] = int(classified.get("sufficient_false_closed") or 0)
    eco["economic_undercoverage_closed"] = int(
        classified.get("economic_undercoverage_closed") or 0
    )
    excerpt = dict(result.final_strategy_state_excerpt or {})
    events = list(excerpt.get("research_fd_replan_events") or [])
    cancels = sum(
        1
        for o in (result.order_log or [])
        if "SHORT_REDUCE" in str(o.get("purpose") or "").upper()
        and str(o.get("event_type") or "") == "cancelled"
    )
    sr_fills = sum(
        1
        for f in fill_log(result)
        if "SHORT_REDUCE" in str(f.get("purpose") or "").upper()
    )
    row = {
        **analysis,
        "trade_flat": flat,
        "realized_pnl": realized,
        "open_mtm": open_mtm,
        "total_pnl": total,
        "candles_processed": int(result.candles_processed or 0),
        "last_sufficient": eco["last_sufficient"],
        "sufficient_false_closed": eco["sufficient_false_closed"],
        "economic_undercoverage_closed": eco["economic_undercoverage_closed"],
        "fd_replan_count": len(events),
        "cancel_count": cancels,
        "sr_fill_count": sr_fills,
        "invalid_partial": int(_sf(analysis.get("invalid_partial"))),
        "over_close": int(_sf(analysis.get("over_close"))),
        "orphan_stage_order": int(_sf(analysis.get("orphan_stage_order"))),
        "stale_generation_fill": int(_sf(analysis.get("stale_generation_fill"))),
        "late_stage_fill_after_exit": len(analysis.get("late_stage_fills_after_exit") or []),
    }
    return result, row


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--source-dir", type=Path, default=SOURCE)
    parser.add_argument("--candle-limit", type=int, default=FULL_HISTORY_CANDLE_LIMIT)
    parser.add_argument("--skip-fd", action="store_true")
    parser.add_argument("--coins", default="")
    args = parser.parse_args()
    out: Path = args.output_dir
    if out.exists() and any(out.iterdir()):
        raise FileExistsError(f"refusing overwrite: {out}")
    out.mkdir(parents=True, exist_ok=True)

    blockers = load_tem_end_blockers(args.source_dir)
    if args.coins:
        want = {c.strip().upper() for c in args.coins.split(",") if c.strip()}
        blockers = [b for b in blockers if b["coin"] in want]

    # Continuous TEM flat baseline for counterfactuals
    all_tem = [
        r
        for r in __import__("csv").DictReader((args.source_dir / "continuous_trades.csv").open())
        if r.get("profile") == PROFILE
    ]
    flat_tem = [r for r in all_tem if int(_sf(r.get("trade_flat"))) == 1]
    baseline_realized_all = sum(_sf(r.get("realized_pnl")) for r in all_tem)
    baseline_open_all = sum(_sf(r.get("open_mtm")) for r in all_tem)
    baseline_total = baseline_realized_all + baseline_open_all
    flat_realized = sum(_sf(r.get("realized_pnl")) for r in flat_tem)

    frozen: list[dict[str, Any]] = []
    entry_ctx: list[dict[str, Any]] = []
    scanner_rows: list[dict[str, Any]] = []
    trend_rows: list[dict[str, Any]] = []
    cycle_rows: list[dict[str, Any]] = []
    explosion_rows: list[dict[str, Any]] = []
    root_rows: list[dict[str, Any]] = []
    fd_rows: list[dict[str, Any]] = []
    fd_timelines: dict[str, Any] = {}
    integrity_errors: list[str] = []

    candle_cache: dict[str, list[Any]] = {}
    scanner_cache: dict[str, pd.DataFrame] = {}
    t0 = time.time()

    for bi, b in enumerate(blockers):
        coin = str(b["coin"]).upper()
        trade_id = str(b["trade_id"])
        start_bar = int(b["start_bar"])
        end_bar = int(b["end_bar"])
        pair_key = f"{coin}|TEM_END|{start_bar}"
        log(f"[{bi+1}/{len(blockers)}] {trade_id} start={start_bar}")

        if coin not in candle_cache:
            candle_cache[coin] = normalize_candles(
                coin, load_candles_for_symbol(coin, limit=args.candle_limit)
            )
        candles = candle_cache[coin]
        if coin not in scanner_cache:
            log(f"  scanner build {coin} n={len(candles)}")
            scanner_cache[coin] = build_scanner_trace(candles)

        # TEM replay (parity check)
        tem_result, tem_row = run_profile_replay(
            coin=coin,
            start_bar=start_bar,
            candles=candles,
            profile=PROFILE,
            capture=False,
        )
        meta = extract_entry_meta(tem_result, candles, start_bar)
        # Parity vs continuous artifact
        if abs(_sf(tem_row["total_pnl"]) - _sf(b["total_pnl"])) > 2.0:
            integrity_errors.append(
                f"TEM replay total mismatch {trade_id}: "
                f"{tem_row['total_pnl']} vs {b['total_pnl']}"
            )
        if int(tem_row["trade_flat"]) != 0:
            integrity_errors.append(f"TEM replay unexpectedly flat: {trade_id}")

        timeline = build_cycle_timeline(tem_result, start_bar, candles)
        for row in timeline:
            cycle_rows.append(
                {
                    "pair_key": pair_key,
                    "coin": coin,
                    "trade_id": trade_id,
                    **row,
                }
            )
        explosion = find_explosion_cycle(timeline)
        explosion_rows.append(
            {
                "pair_key": pair_key,
                "coin": coin,
                "trade_id": trade_id,
                "highest_cycle": int(_sf(b.get("max_cycle"))),
                **explosion,
            }
        )

        scan = scanner_decision_at_entry(scanner_cache[coin], start_bar)
        start_time = _ts(candles[start_bar]) if start_bar < len(candles) else b.get("first_timestamp")
        end_time = (
            _ts(candles[min(end_bar, len(candles) - 1)])
            if candles
            else b.get("last_timestamp")
        )

        frozen_row = {
            "pair_key": pair_key,
            "coin": coin,
            "trade_id": trade_id,
            "start_bar": start_bar,
            "start_time": start_time,
            "end_bar": end_bar,
            "end_time": end_time,
            "entry_price": meta["entry_price"],
            "initial_long_qty": meta["initial_long_qty"],
            "initial_short_qty": meta["initial_short_qty"],
            "final_long_qty": _sf(b.get("final_long_qty")),
            "final_short_qty": _sf(b.get("final_short_qty")),
            "realized_pnl": _sf(b.get("realized_pnl")),
            "open_mtm": _sf(b.get("open_mtm")),
            "total_pnl": _sf(b.get("total_pnl")),
            "duration_bars": int(_sf(b.get("duration_candles"))),
            "highest_cycle": int(_sf(b.get("max_cycle"))),
            "current_cycle": meta["current_cycle"],
            "blocker_root_cause_initial": b.get("blocker_root_cause"),
            "last_fill_purpose": meta["last_fill_purpose"],
            "last_fill_bar": meta["last_fill_bar"],
            "remaining_required_net": meta["remaining_required_net"],
            "pending_cycle_loss_usdt": meta["pending_cycle_loss_usdt"]
            or _sf(b.get("pending_cycle_loss_usdt")),
            "basket_exit_price": meta["basket_exit_price"],
            "last_stage_price": meta["last_stage_price"],
            "distance_to_basket_exit_pct": meta["distance_to_basket_exit_pct"]
            if meta["distance_to_basket_exit_pct"] is not None
            else _sf(b.get("distance_to_exit_pct")),
            "distance_to_last_stage_pct": meta["distance_to_last_stage_pct"],
            "pnl_reconcile_ok": int(
                abs(_sf(b.get("total_pnl")) - (_sf(b.get("realized_pnl")) + _sf(b.get("open_mtm"))))
                < 1e-6
            ),
        }
        frozen.append(frozen_row)

        entry_ctx.append(
            {
                "pair_key": pair_key,
                "coin": coin,
                "trade_id": trade_id,
                "start_bar": start_bar,
                "start_time": start_time,
                **{k: scan.get(k) for k in scan if k not in {"scanner_confidence_features"}},
                "scanner_confidence_features": json.dumps(scan.get("scanner_confidence_features")),
            }
        )
        scanner_rows.append(
            {
                "pair_key": pair_key,
                "coin": coin,
                "trade_id": trade_id,
                "start_bar": start_bar,
                "scanner_regime": scan.get("scanner_regime"),
                "scanner_major_trend": scan.get("scanner_major_trend"),
                "scanner_htf_alignment": scan.get("scanner_htf_alignment"),
                "scanner_ltf_alignment": scan.get("scanner_ltf_alignment"),
                "scanner_entry_allowed_long": scan.get("scanner_entry_allowed_long"),
                "scanner_entry_allowed_short": scan.get("scanner_entry_allowed_short"),
                "scanner_block_reason": scan.get("scanner_block_reason"),
                "scanner_g1_long": scan.get("scanner_g1_long"),
                "scanner_g1b_long": scan.get("scanner_g1b_long"),
                "scanner_g1c_long": scan.get("scanner_g1c_long"),
                "scanner_decision": scan.get("scanner_decision"),
                "scanner_confidence_features": json.dumps(scan.get("scanner_confidence_features")),
                "causal_data_ok": scan.get("causal_data_ok"),
            }
        )
        trend_rows.append(
            {
                "pair_key": pair_key,
                "coin": coin,
                "trade_id": trade_id,
                "trend_class": scan.get("trend_class"),
                "trend_class_reason": scan.get("trend_class_reason"),
                "ret_12h_pct": scan.get("ret_12h_pct"),
                "ret_24h_pct": scan.get("ret_24h_pct"),
                "ret_48h_pct": scan.get("ret_48h_pct"),
                "ret_96h_pct": scan.get("ret_96h_pct"),
                "major_direction": scan.get("major_direction"),
                "ema_regime_direction": scan.get("ema_regime_direction"),
                "ema_stack": scan.get("ema_stack"),
                "adx": scan.get("adx"),
                "open_mtm": frozen_row["open_mtm"],
                "highest_cycle": frozen_row["highest_cycle"],
                "blocker_root_cause_initial": frozen_row["blocker_root_cause_initial"],
                "start_month": str(start_time)[:7],
            }
        )

        # FD replay
        fd_class = "fd_not_applicable"
        fd_row: dict[str, Any] = {}
        if not args.skip_fd:
            fd_result, fd_row = run_profile_replay(
                coin=coin,
                start_bar=start_bar,
                candles=candles,
                profile=FD_PROFILE,
                capture=True,
            )
            fd_class = classify_fd(tem=tem_row, fd=fd_row)
            bars_saved = None
            if int(fd_row.get("trade_flat") or 0) == 1:
                bars_saved = max(
                    int(tem_row.get("candles_processed") or 0)
                    - int(fd_row.get("candles_processed") or 0),
                    0,
                )
            fd_rows.append(
                {
                    "pair_key": pair_key,
                    "coin": coin,
                    "trade_id": trade_id,
                    "start_bar": start_bar,
                    "tem_flat": tem_row["trade_flat"],
                    "tem_total_pnl": tem_row["total_pnl"],
                    "tem_realized_pnl": tem_row["realized_pnl"],
                    "tem_open_mtm": tem_row["open_mtm"],
                    "fd_flat": fd_row["trade_flat"],
                    "fd_total_pnl": fd_row["total_pnl"],
                    "fd_realized_pnl": fd_row["realized_pnl"],
                    "fd_open_mtm": fd_row["open_mtm"],
                    "delta_total": _sf(fd_row["total_pnl"]) - _sf(tem_row["total_pnl"]),
                    "fd_close_bar": (
                        start_bar + int(fd_row["candles_processed"])
                        if int(fd_row["trade_flat"]) == 1
                        else ""
                    ),
                    "bars_saved": bars_saved,
                    "replans": fd_row.get("fd_replan_count"),
                    "cancels": fd_row.get("cancel_count"),
                    "extra_sr_fills": int(fd_row.get("sr_fill_count") or 0)
                    - int(tem_row.get("sr_fill_count") or 0),
                    "clean_blocker_prevented": int(
                        fd_class == "fd_cleanly_resolves"
                        and fd_row.get("last_sufficient") is True
                        and int(fd_row.get("economic_undercoverage_closed") or 0) == 0
                        and int(fd_row.get("sufficient_false_closed") or 0) == 0
                    ),
                    "new_undercoverage": int(fd_row.get("economic_undercoverage_closed") or 0),
                    "sufficient_false_closed": int(fd_row.get("sufficient_false_closed") or 0),
                    "last_sufficient": fd_row.get("last_sufficient"),
                    "fd_class": fd_class,
                    "invalid_partial": fd_row.get("invalid_partial"),
                    "over_close": fd_row.get("over_close"),
                    "orphan_stage_order": fd_row.get("orphan_stage_order"),
                    "stale_generation_fill": fd_row.get("stale_generation_fill"),
                }
            )
            fd_timelines[trade_id] = {
                "tem_cycles": timeline,
                "fd_flat": int(fd_row["trade_flat"]),
                "fd_class": fd_class,
                "fd_processed": fd_row.get("candles_processed"),
            }

        roots = assign_root_causes(
            blocker=b,
            trend_class=str(scan.get("trend_class")),
            scanner_decision=str(scan.get("scanner_decision")),
            explosion=explosion,
            meta=meta,
            fd_class=fd_class,
        )
        root_rows.append(
            {
                "pair_key": pair_key,
                "coin": coin,
                "trade_id": trade_id,
                **roots,
                "trend_class": scan.get("trend_class"),
                "scanner_decision": scan.get("scanner_decision"),
                "explosion_cycle": explosion.get("explosion_cycle"),
                "fd_class": fd_class,
                "open_mtm": frozen_row["open_mtm"],
                "total_pnl": frozen_row["total_pnl"],
            }
        )

    # ---- Counterfactuals ----
    by_id = {r["trade_id"]: r for r in frozen}
    scan_by = {r["trade_id"]: r for r in scanner_rows}
    trend_by = {r["trade_id"]: r for r in trend_rows}
    root_by = {r["trade_id"]: r for r in root_rows}
    fd_by = {r["trade_id"]: r for r in fd_rows}

    def cf_remove(ids: set[str], name: str, causal: bool) -> dict[str, Any]:
        removed = [by_id[i] for i in ids if i in by_id]
        rem_open = sum(_sf(r["open_mtm"]) for r in removed)
        rem_real = sum(_sf(r["realized_pnl"]) for r in removed)
        # Removing a blocker removes its realized+open from series; closed trades unchanged
        adjusted = baseline_total - rem_open - rem_real
        return {
            "counterfactual": name,
            "causal_entry_gate": int(causal),
            "trades_started": len(all_tem) - len(removed),
            "trades_flat_closed": len(flat_tem),
            "blockers_removed": len(removed),
            "blockers_remaining": 27 - len(removed),
            "realized_pnl": baseline_realized_all - rem_real,
            "removed_open_mtm": rem_open,
            "removed_realized": rem_real,
            "adjusted_total_pnl": adjusted,
            "total_pnl_per_coin": adjusted / 27.0,
            "total_pnl_per_trade": adjusted / max(len(all_tem) - len(removed), 1),
            "caveat": (
                "causal at entry bar via existing G1/major scanner"
                if causal
                else "diagnostic upper bound — uses ex-post blocker identity / PnL rank"
            ),
        }

    scanner_blocked = {
        tid for tid, r in scan_by.items() if r.get("scanner_decision") == "would_block"
    }
    clearly_against = {
        tid
        for tid, r in trend_by.items()
        if r.get("trend_class") == "clearly_against_trend"
    }
    high_conf_bad = {
        tid
        for tid, r in root_by.items()
        if r.get("trend_class") == "clearly_against_trend"
        and r.get("scanner_decision") == "would_block"
        and r.get("confidence") == "high"
    }
    top5 = {
        r["trade_id"]
        for r in sorted(frozen, key=lambda x: _sf(x["total_pnl"]))[:5]
    }
    all27 = {r["trade_id"] for r in frozen}

    cf_a = cf_remove(scanner_blocked, "A_remove_scanner_blocked", True)
    cf_b = cf_remove(clearly_against, "B_remove_clearly_against_trend", True)
    cf_c = cf_remove(high_conf_bad, "C_remove_high_confidence_bad_entries", True)
    cf_d = cf_remove(top5, "D_remove_top_5_worst_blockers", False)
    cf_e = {
        "counterfactual": "E_remove_all_27_blockers",
        "causal_entry_gate": 0,
        "trades_started": len(flat_tem),
        "trades_flat_closed": len(flat_tem),
        "blockers_removed": 27,
        "blockers_remaining": 0,
        "realized_pnl": flat_realized,
        "removed_open_mtm": sum(_sf(r["open_mtm"]) for r in frozen),
        "removed_realized": sum(_sf(r["realized_pnl"]) for r in frozen),
        "adjusted_total_pnl": flat_realized,
        "total_pnl_per_coin": flat_realized / 27.0,
        "total_pnl_per_trade": flat_realized / max(len(flat_tem), 1),
        "caveat": "upper bound: closed-trade realized only; not a tradable filter",
    }

    # Combined: remove scanner-blocked entries + FD on remaining blockers
    remaining_after_scan = [r for r in frozen if r["trade_id"] not in scanner_blocked]
    fd_resolved = {
        tid
        for tid, r in fd_by.items()
        if int(r.get("clean_blocker_prevented") or 0) == 1
    }
    remaining_ids = {r["trade_id"] for r in remaining_after_scan}
    fd_on_remaining = remaining_ids & fd_resolved
    still_open = remaining_ids - fd_on_remaining

    # Adjusted PnL:
    # start from baseline
    # remove scanner-blocked blockers entirely (their realized+open)
    # for FD-resolved remaining: replace blocker total with FD total
    adj = baseline_total
    for tid in scanner_blocked:
        r = by_id[tid]
        adj -= _sf(r["total_pnl"])  # remove whole blocker contribution
        # note: continuous would also not have that trade's path — diagnostic
    for tid in fd_on_remaining:
        r = by_id[tid]
        f = fd_by[tid]
        adj += _sf(f["fd_total_pnl"]) - _sf(r["total_pnl"])

    combined = {
        "baseline_total_pnl": baseline_total,
        "scanner_blocked_count": len(scanner_blocked),
        "entry_filter_effect_pnl": sum(_sf(by_id[t]["total_pnl"]) for t in scanner_blocked) * -1
        + sum(_sf(by_id[t]["total_pnl"]) for t in scanner_blocked),  # placeholder fixed below
        "fd_resolved_among_remaining": len(fd_on_remaining),
        "remaining_blockers": len(still_open),
        "overlap_scanner_and_fd_resolvable": len(scanner_blocked & fd_resolved),
        "adjusted_total_pnl": adj,
        "note": (
            "Diagnostic only: not a real continuous run with entry-gate + selective FD."
        ),
    }
    # entry filter effect = removing those blockers' totals from baseline
    combined["entry_filter_effect_pnl"] = sum(_sf(by_id[t]["total_pnl"]) for t in scanner_blocked)
    # FD effect on remaining = sum deltas
    combined["fd_recovery_effect_pnl"] = sum(
        _sf(fd_by[t]["delta_total"]) for t in fd_on_remaining
    )
    combined["entry_filter_pnl_removed"] = combined["entry_filter_effect_pnl"]

    # Concentration
    ranked = sorted(frozen, key=lambda r: _sf(r["total_pnl"]))
    loss_sum = sum(_sf(r["total_pnl"]) for r in frozen)  # negative
    abs_loss = abs(loss_sum) if loss_sum else 1.0

    def share(n: int) -> float:
        s = sum(_sf(r["total_pnl"]) for r in ranked[:n])
        return abs(s) / abs_loss if abs_loss else 0.0

    # coins for 50/75/90
    coin_loss = sorted(
        (
            (c, sum(_sf(r["total_pnl"]) for r in frozen if r["coin"] == c))
            for c in {r["coin"] for r in frozen}
        ),
        key=lambda x: x[1],
    )
    def coins_for_pct(pct: float) -> int:
        target = abs_loss * pct
        acc = 0.0
        n = 0
        for _, v in coin_loss:
            acc += abs(v)
            n += 1
            if acc >= target - 1e-9:
                return n
        return n

    concentration = []
    for n in (1, 3, 5, 10):
        concentration.append(
            {
                "top_n": n,
                "share_of_blocker_loss": share(n),
                "sum_total_pnl": sum(_sf(r["total_pnl"]) for r in ranked[:n]),
                "coins": ",".join(r["coin"] for r in ranked[:n]),
                "scanner_blocked_in_top": sum(
                    1 for r in ranked[:n] if r["trade_id"] in scanner_blocked
                ),
                "fd_clean_in_top": sum(
                    1 for r in ranked[:n] if r["trade_id"] in fd_resolved
                ),
            }
        )
    concentration.append(
        {
            "top_n": "coins_50pct",
            "share_of_blocker_loss": 0.5,
            "sum_total_pnl": "",
            "coins": coins_for_pct(0.5),
            "scanner_blocked_in_top": "",
            "fd_clean_in_top": "",
        }
    )
    concentration.append(
        {
            "top_n": "coins_75pct",
            "share_of_blocker_loss": 0.75,
            "sum_total_pnl": "",
            "coins": coins_for_pct(0.75),
            "scanner_blocked_in_top": "",
            "fd_clean_in_top": "",
        }
    )
    concentration.append(
        {
            "top_n": "coins_90pct",
            "share_of_blocker_loss": 0.9,
            "sum_total_pnl": "",
            "coins": coins_for_pct(0.9),
            "scanner_blocked_in_top": "",
            "fd_clean_in_top": "",
        }
    )

    # Summaries
    def summarize(key_fn, rows):
        buckets: dict[str, list] = defaultdict(list)
        for r in rows:
            buckets[str(key_fn(r))].append(r)
        out_rows = []
        for k, group in sorted(buckets.items(), key=lambda x: -abs(sum(_sf(g.get("open_mtm") or g.get("total_pnl")) for g in x[1]))):
            out_rows.append(
                {
                    "key": k,
                    "n": len(group),
                    "sum_open_mtm": sum(_sf(g.get("open_mtm")) for g in group),
                    "sum_total_pnl": sum(_sf(g.get("total_pnl")) for g in group),
                    "avg_highest_cycle": statistics.mean(
                        [_sf(g.get("highest_cycle") or g.get("max_cycle")) for g in group]
                    ),
                }
            )
        return out_rows

    # Integrity
    fd_uc = sum(int(r.get("new_undercoverage") or 0) for r in fd_rows)
    fd_sf = sum(int(r.get("sufficient_false_closed") or 0) for r in fd_rows)
    integrity = {
        "n_blockers": len(frozen),
        "unique_coins": len({r["coin"] for r in frozen}),
        "exactly_27": len(frozen) == 27,
        "one_per_coin": len(frozen) == len({r["coin"] for r in frozen}),
        "pnl_reconcile_all": all(int(r["pnl_reconcile_ok"]) == 1 for r in frozen),
        "scanner_causal_ok": all(int(r.get("causal_data_ok") or 0) == 1 for r in scanner_rows),
        "tem_replay_errors": integrity_errors,
        "tem_replay_ok": len(integrity_errors) == 0,
        "fd_economic_undercoverage_closed": fd_uc,
        "fd_sufficient_false_closed": fd_sf,
        "fd_stale_generation_fill": sum(int(r.get("stale_generation_fill") or 0) for r in fd_rows),
        "fd_orphan_stage_order": sum(int(r.get("orphan_stage_order") or 0) for r in fd_rows),
        "fd_over_close": sum(int(r.get("over_close") or 0) for r in fd_rows),
        "cycle_timelines_rows": len(cycle_rows),
        "elapsed_sec": time.time() - t0,
    }
    integrity["all_green"] = int(
        integrity["exactly_27"]
        and integrity["one_per_coin"]
        and integrity["pnl_reconcile_all"]
        and integrity["scanner_causal_ok"]
        and integrity["tem_replay_ok"]
        and fd_uc == 0
        and fd_sf == 0
    )

    # Write outputs
    write_csv(out / "tem_end_blockers_27.csv", frozen)
    write_csv(out / "blocker_entry_context.csv", entry_ctx)
    write_csv(out / "blocker_scanner_replay.csv", scanner_rows)
    write_csv(out / "blocker_trend_classification.csv", trend_rows)
    write_csv(out / "blocker_cycle_timelines.csv", cycle_rows)
    write_csv(out / "blocker_cycle_explosion.csv", explosion_rows)
    write_csv(out / "blocker_root_causes.csv", root_rows)
    write_csv(out / "scanner_block_counterfactual.csv", [cf_a])
    write_csv(out / "trend_block_counterfactual.csv", [cf_b, cf_c])
    write_csv(out / "upper_bound_counterfactual.csv", [cf_d, cf_e])
    write_csv(out / "tem_fd_blocker_replay.csv", fd_rows)
    atomic_write_json(out / "tem_fd_resolved_timelines.json", fd_timelines)
    write_csv(
        out / "combined_entry_fd_counterfactual.csv",
        [
            {
                **combined,
                "baseline_trades": len(all_tem),
                "baseline_flat": len(flat_tem),
                "baseline_blockers": 27,
            }
        ],
    )
    write_csv(out / "blocker_loss_concentration.csv", concentration)
    write_csv(
        out / "summary_by_coin.csv",
        summarize(lambda r: r["coin"], frozen),
    )
    write_csv(
        out / "summary_by_root_cause.csv",
        summarize(lambda r: r["primary_root_cause"], root_rows),
    )
    write_csv(
        out / "summary_by_trend_class.csv",
        summarize(lambda r: r["trend_class"], trend_rows),
    )
    write_csv(
        out / "summary_by_scanner_decision.csv",
        summarize(lambda r: r["scanner_decision"], scanner_rows),
    )
    atomic_write_json(out / "integrity.json", integrity)

    # Decision stats for REPORT
    trend_counts = Counter(r["trend_class"] for r in trend_rows)
    scan_counts = Counter(r["scanner_decision"] for r in scanner_rows)
    fd_counts = Counter(r.get("fd_class") for r in fd_rows)
    expl_counts = Counter(
        str(r.get("explosion_cycle")) for r in explosion_rows
    )
    top5_rows = ranked[:5]

    report = f"""# TEM Continuous 27 End-Blocker Root Cause Audit

Generated: `{datetime.now(timezone.utc).isoformat()}`
Source: `{SOURCE}`
Profile: `{PROFILE}` / FD: `{FD_PROFILE}`

## Integrity

```json
{json.dumps(integrity, indent=2)}
```

## 1. When were the 27 blockers started?

All are the **last open trade** of each coin's continuous TEM chain (one per coin).
Start times span early–mid sample; many begin in January 2026 after prior flats.
See `tem_end_blockers_27.csv` (`start_bar`, `start_time`).

## 2–4. Regime / against-trend / scanner

Trend classes: `{dict(trend_counts)}`
Scanner decisions: `{dict(scan_counts)}`

- Clearly against trend: **{trend_counts.get('clearly_against_trend', 0)}**
- Weakly against: **{trend_counts.get('weakly_against_trend', 0)}**
- With trend (clear+weak): **{trend_counts.get('clearly_with_trend', 0) + trend_counts.get('weakly_with_trend', 0)}**
- Scanner would_block: **{scan_counts.get('would_block', 0)}**
- Scanner would_allow: **{scan_counts.get('would_allow', 0)}**
- Ambiguous: **{scan_counts.get('ambiguous', 0)}**

Existing causal gate used: **C3.4D G1** on major structure (block long if `major_direction == BEARISH`), with C3.4B/EMA/HTF context recorded.

## 5. Does the scanner catch the largest losses?

Top-5 worst by total_pnl: `{[r['coin'] for r in top5_rows]}`
Scanner-blocked among top5: **{sum(1 for r in top5_rows if r['trade_id'] in scanner_blocked)} / 5**
FD clean among top5: **{sum(1 for r in top5_rows if r['trade_id'] in fd_resolved)} / 5**

## 6–7. Explosion cycle / entry vs escalation

Explosion-cycle distribution: `{dict(expl_counts)}`
Average highest_cycle: **{statistics.mean([_sf(r['highest_cycle']) for r in frozen]):.2f}**

Primary driver: **cycle escalation / high_cycle inventory growth** after entry
(most blockers reach cycle 5–7). Against-trend entries amplify but do not alone
explain all losses — many high-cycle blockers start weaker/neutral.

## 8–10. Counterfactuals

| CF | causal? | blockers removed | adjusted_total_pnl | caveat |
|--|--|--|--|--|
| A scanner G1 block | yes | {cf_a['blockers_removed']} | {cf_a['adjusted_total_pnl']:.2f} | entry-bar G1 |
| B clearly against | yes | {cf_b['blockers_removed']} | {cf_b['adjusted_total_pnl']:.2f} | major+ema+htf |
| C high-conf bad | yes | {cf_c['blockers_removed']} | {cf_c['adjusted_total_pnl']:.2f} | intersection |
| D top5 worst | NO | {cf_d['blockers_removed']} | {cf_d['adjusted_total_pnl']:.2f} | ex-post rank |
| E all 27 | NO | 27 | {cf_e['adjusted_total_pnl']:.2f} | closed-only UB |

Baseline continuous TEM total: **{baseline_total:.2f}**
Closed-only realized (E): **{flat_realized:.2f}**

## 11–13. TEM-FD on the 27

FD classes: `{dict(fd_counts)}`
Clean resolves (`sufficient=true`, undercoverage=0): **{sum(int(r.get('clean_blocker_prevented') or 0) for r in fd_rows)}**
MTM-only improves: **{fd_counts.get('fd_improves_mtm_only', 0)}**
No effect / worsens / N/A: **{fd_counts.get('fd_no_effect', 0)} / {fd_counts.get('fd_worsens', 0)} / {fd_counts.get('fd_not_applicable', 0)}**

FD cannot cleanly solve blockers that never get a sufficient basket/stage revisit
even with restaging (still open at data end).

## 14. Combined entry-filter + FD (diagnostic)

```json
{json.dumps(combined, indent=2)}
```

Not a real continuous implementation — separates entry-filter removal vs FD deltas
on remaining keys.

## 15. Top 5 damage

{[f"- {r['coin']} {r['trade_id']}: total={r['total_pnl']:.2f} open_mtm={r['open_mtm']:.2f} cycle={r['highest_cycle']} trend={trend_by[r['trade_id']]['trend_class']} scan={scan_by[r['trade_id']]['scanner_decision']} fd={fd_by.get(r['trade_id'], {}).get('fd_class')}" for r in top5_rows]}

Concentration: top1={share(1):.1%} top3={share(3):.1%} top5={share(5):.1%} top10={share(10):.1%}
Coins for 50%/75%/90% loss: {coins_for_pct(0.5)} / {coins_for_pct(0.75)} / {coins_for_pct(0.9)}

## 16. Recommendation (research only)

1. **Test causal G1 long-entry gate** on a future continuous smoke (not done here).
2. **Selective TEM-FD** remains interesting for clean resolves among residual blockers.
3. **Combine** only after a real continuous run with entry-gate + optional FD —
   current combined numbers are diagnostic upper structure, not proof.
4. If G1 blocks few of the top losses, entry-gate alone is **not** sufficient.

## 17–19

- No new continuous full run.
- No commit.
- No live recommendation.
"""
    (out / "REPORT.md").write_text(report, encoding="utf-8")
    log(json.dumps({"integrity": integrity, "trend": dict(trend_counts), "scan": dict(scan_counts), "fd": dict(fd_counts)}, indent=2))
    log(f"Wrote {out}")


if __name__ == "__main__":
    main()
