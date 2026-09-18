"""Decision- and touch-relative MFE/MAE outcomes in percent."""

from __future__ import annotations

from typing import Any, Sequence

from obfull_research_engine.mp_price_path_4h_v1.candles import Candle1m
from obfull_research_engine.mp_price_path_4h_v1.geometry import mae_from_extremes, mfe_from_extremes, signed_return_pct
from obfull_research_engine.mp_price_path_4h_v1.path_engine import (
    _first_index_mfe_at_least,
    _mae_before_index,
    _running_excursions,
    first_hit,
    select_path_candles,
)

from . import COST_ROUNDTRIP_PCT, HORIZONS_MIN, REACH_THRESHOLDS_PCT, SL_STOPS_PCT, TARGET_REACH_PCT, TP_SL_HORIZONS_MIN


def compute_path_outcomes(
    *,
    candles: Sequence[Candle1m],
    entry_ts_ns: int,
    entry_price: float,
    trade_side: str,
    entry_price_source: str,
    perspective: str,
) -> dict[str, Any]:
    state = select_path_candles(candles, trigger_ts_ns=int(entry_ts_ns), horizon_min=240)
    base = {
        "perspective": perspective,
        "entry_time_ns": int(entry_ts_ns),
        "entry_price": float(entry_price),
        "entry_price_source": entry_price_source,
        "executable_entry_available": entry_price_source.startswith("EXECUTABLE"),
        "entry_is_proxy": "PROXY" in entry_price_source or entry_price_source.endswith("CANDLE_OPEN"),
        "latency_assumption": "DECISION_CAUSAL" if perspective == "DECISION_RELATIVE" else "TOUCH_RESEARCH",
        "trade_side": trade_side,
        "candle_coverage_complete_4h": False,
    }
    if state is None:
        base["censor_reason"] = "INCOMPLETE_OR_MISSING_CANDLES"
        return base

    rows = _running_excursions(state.candles, trade_side=trade_side, trigger_price=float(entry_price))
    base["candle_coverage_complete_4h"] = len(rows) >= 240
    base["entry_candle_partial"] = state.entry_candle_partial

    horizons = []
    for h in HORIZONS_MIN:
        if len(rows) < h:
            horizons.append(
                {
                    "horizon_min": h,
                    "mfe_pct": None,
                    "mae_pct": None,
                    "close_return_gross_pct": None,
                    "close_return_net_008_pct": None,
                    "close_return_net_012_pct": None,
                    "time_to_mfe_peak_minutes": None,
                    "time_to_mae_peak_minutes": None,
                    "mfe_peak_time_ns": None,
                    "mae_peak_time_ns": None,
                    "mfe_before_mae": None,
                    "mae_before_mfe": None,
                    "candle_coverage_complete": False,
                    "intrabar_ambiguity": None,
                }
            )
            continue
        sub = rows[:h]
        mfe = sub[-1]["running_mfe_pct"]
        mae = sub[-1]["running_mae_pct"]
        mfe_i = next(i for i, r in enumerate(sub) if r["running_mfe_pct"] + 1e-15 >= mfe)
        mae_i = next(i for i, r in enumerate(sub) if r["running_mae_pct"] + 1e-15 >= mae)
        close = sub[-1]["signed_close_return_pct"]
        horizons.append(
            {
                "horizon_min": h,
                "mfe_pct": mfe,
                "mae_pct": mae,
                "close_return_gross_pct": close,
                "close_return_net_008_pct": close - COST_ROUNDTRIP_PCT[0],
                "close_return_net_012_pct": close - COST_ROUNDTRIP_PCT[1],
                "time_to_mfe_peak_minutes": float(mfe_i),
                "time_to_mae_peak_minutes": float(mae_i),
                "mfe_peak_time_ns": sub[mfe_i]["candle_ts_ns"],
                "mae_peak_time_ns": sub[mae_i]["candle_ts_ns"],
                "mfe_before_mae": mfe_i < mae_i,
                "mae_before_mfe": mae_i < mfe_i,
                "candle_coverage_complete": True,
                "intrabar_ambiguity": bool(
                    sub[mfe_i]["bar_mfe_pct"] > 1e-15 and sub[mfe_i]["bar_mae_pct"] > 1e-15
                ),
            }
        )

    # 0.41% and other reach thresholds
    reach_rows = []
    for thr in REACH_THRESHOLDS_PCT:
        idx = _first_index_mfe_at_least(rows[:240], thr)
        if idx is None:
            reach_rows.append(
                {
                    "threshold_pct": thr,
                    "reached": False,
                    "first_reach_time_ns": None,
                    "minutes_to_reach": None,
                    "mae_before_pct": None,
                    "max_mae_before_pct": None,
                    "price_at_first_reach": None,
                    "reached_within_30m": False,
                    "reached_within_60m": False,
                    "reached_within_120m": False,
                    "reached_within_240m": False,
                }
            )
            continue
        mae_ex, _, _ = _mae_before_index(rows[:240], idx, inclusive=False)
        mae_in, _, amb = _mae_before_index(rows[:240], idx, inclusive=True)
        reach_rows.append(
            {
                "threshold_pct": thr,
                "reached": True,
                "first_reach_time_ns": rows[idx]["candle_ts_ns"],
                "minutes_to_reach": float(idx),
                "mae_before_pct": mae_ex,
                "max_mae_before_pct": mae_in,
                "price_at_first_reach": rows[idx]["close"],
                "reached_within_30m": idx < 30,
                "reached_within_60m": idx < 60,
                "reached_within_120m": idx < 120,
                "reached_within_240m": idx < 240,
                "intrabar_ambiguity": amb,
            }
        )

    # Primary 0.41 fields
    r041 = next(r for r in reach_rows if abs(r["threshold_pct"] - TARGET_REACH_PCT) < 1e-12)
    mae_before_target = {
        "reached_0_41_pct": r041["reached"],
        "first_reach_0_41_time_ns": r041["first_reach_time_ns"],
        "minutes_to_0_41": r041["minutes_to_reach"],
        "mae_before_0_41_pct": r041["mae_before_pct"],
        "max_mae_before_0_41_pct": r041["max_mae_before_pct"],
        "price_at_first_reach": r041["price_at_first_reach"],
        "reached_within_30m": r041["reached_within_30m"],
        "reached_within_60m": r041["reached_within_60m"],
        "reached_within_120m": r041["reached_within_120m"],
        "reached_within_240m": r041["reached_within_240m"],
    }

    # TP/SL first passage
    tp_sl = []
    for h in TP_SL_HORIZONS_MIN:
        for sl in SL_STOPS_PCT:
            hit = first_hit(rows, target_pct=TARGET_REACH_PCT, stop_pct=sl, horizon_min=h)
            res = hit.get("result")
            # Map CENSORED incomplete → NEITHER if complete horizon else keep
            if res == "CENSORED" and len(rows) >= h:
                res = "NEITHER"
            gross = None
            if res == "TARGET_FIRST":
                gross = TARGET_REACH_PCT
            elif res == "STOP_FIRST":
                gross = -float(sl)
            elif res == "NEITHER":
                gross = rows[h - 1]["signed_close_return_pct"] if len(rows) >= h else None
            elif res == "AMBIGUOUS":
                gross = None
            tp_sl.append(
                {
                    "horizon_min": h,
                    "tp_pct": TARGET_REACH_PCT,
                    "sl_pct": sl,
                    "result": res,
                    "minutes_to_first_exit": hit.get("minutes_to_decision"),
                    "intrabar_order_ambiguous": hit.get("intrabar_order_ambiguous"),
                    "gross_result_pct": gross,
                    "net_result_008_pct": (None if gross is None else gross - COST_ROUNDTRIP_PCT[0]),
                    "net_result_012_pct": (None if gross is None else gross - COST_ROUNDTRIP_PCT[1]),
                }
            )

    # 4h peak order
    h240 = next((h for h in horizons if h["horizon_min"] == 240), None)
    peak_order = {}
    if h240 and h240.get("mfe_pct") is not None:
        mae_ex, _, _ = _mae_before_index(rows[:240], int(h240["time_to_mfe_peak_minutes"]), inclusive=False)
        mae_in, _, _ = _mae_before_index(rows[:240], int(h240["time_to_mfe_peak_minutes"]), inclusive=True)
        peak_order = {
            "mae_before_4h_mfe_peak_exclusive_pct": mae_ex,
            "mae_before_4h_mfe_peak_inclusive_pct": mae_in,
            "mfe_before_4h_mae_peak": h240["mfe_before_mae"],
            "extreme_order": (
                "MFE_FIRST"
                if h240["mfe_before_mae"]
                else ("MAE_FIRST" if h240["mae_before_mfe"] else "SAME_OR_AMBIGUOUS")
            ),
        }

    return {
        **base,
        **mae_before_target,
        **peak_order,
        "horizons": horizons,
        "reach_thresholds": reach_rows,
        "tp_sl": tp_sl,
    }
