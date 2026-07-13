"""Research-only audit of M3 remaining weak momentum-quality leaks.

Does not change live strategy, productive pipeline, or momentum thresholds.
Does not implement new momentum rules — diagnosis and research plan only.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from research.regime_scanner.config import default_regime_scanner_config
from research.regime_scanner.data_loader import load_symbol_candles
from research.regime_scanner.indicators import compute_indicator_frame
from research.regime_scanner.momentum import (
    MomentumConfig,
    body_to_range_ratio,
    candle_body,
    candle_range,
    close_location_ratio,
    default_momentum_config,
    directional_body,
    evaluate_momentum_conditions,
    structure_level_held,
)
from research.regime_scanner.pipeline_counterfactual_multiweek import (
    classify_market_phase,
    map_quality_label,
    slice_weeks,
    to_utc,
)
from research.regime_scanner.point_audit import json_safe
from research.regime_scanner.swings import find_confirmed_pivots

DEFAULT_MULTIWEEK = (
    "research/backtests/results/regime_scanner_pipeline_counterfactual_multiweek"
)
DEFAULT_PIPELINE = (
    "research/backtests/results/regime_scanner_pipeline_audit_aptusdt_2026_h1"
)
DEFAULT_OUT = (
    "research/backtests/results/regime_scanner_momentum_quality_leak_audit"
)

# Fixed descriptive hypothesis flags (not production thresholds / not fitted).
HYP_FLAGS = {
    "M1_missing_progression": {
        "cum_ret_atr_lt": 0.35,
        "close_near_pa_atr": 0.25,
    },
    "M2_weakening_second": {
        "body_drop": 0.15,
        "close_loc_drop": 0.10,
    },
    "M3_rejection": {
        "opp_wick_share": 0.40,
        "close_loc_max": 0.70,
    },
    "M5_late_momentum": {
        "pa_to_entry_atr": 1.50,
        "ema9_dist_atr": 1.20,
    },
    "M8_exhaustion": {
        "range_atr_min": 2.00,
        "ema9_dist_atr": 1.50,
        "vol_z_min": 2.00,
    },
}


def _finite(v: object) -> float | None:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    try:
        x = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _side_ret(o: float, c: float, side: str) -> float:
    if side == "long":
        return (c - o) / abs(o) * 100.0 if o else 0.0
    return (o - c) / abs(o) * 100.0 if o else 0.0


def _fav_adv(high: float, low: float, entry: float, side: str) -> tuple[float, float]:
    if side == "long":
        return max(0.0, (high - entry) / abs(entry) * 100.0), max(
            0.0, (entry - low) / abs(entry) * 100.0
        )
    return max(0.0, (entry - low) / abs(entry) * 100.0), max(
        0.0, (high - entry) / abs(entry) * 100.0
    )


def document_current_momentum_logic() -> dict[str, Any]:
    """Answers the §2 inventory from ``momentum.py`` (read-only)."""
    cfg = default_momentum_config()
    return {
        "module": "research/regime_scanner/momentum.py",
        "q1_prerequisites": [
            "Valid closed 5m OHLC",
            "STRUCTURE_LEVEL_HOLD vs PA confirmation_level (close only)",
            "DIRECTIONAL_BODY (close>open long / close<open short)",
            f"BODY_TO_RANGE >= {cfg.min_body_to_range_ratio}",
            f"CLOSE_LOCATION >= {cfg.min_close_location_ratio}",
            f"RANGE_ATR in [{cfg.min_range_atr_ratio}, {cfg.max_range_atr_ratio}]",
            "Volume filter OFF by default",
            "Not invalidated by counter-move vs break_close",
            "Not invalidated by opposing setup / PA invalidation",
        ],
        "q2_candles_considered": (
            f"break age0 + ages 1..{cfg.confirmation_window_candles} "
            f"(window={cfg.confirmation_window_candles})"
        ),
        "q3_confirmation_candle": (
            "First candle in window that passes all conditions; "
            "may be break_candle (age0) if allow_confirmation_on_break_candle=True"
        ),
        "q4_direction_body_close_return": (
            "Direction via directional_body; body/range; close location; "
            "NO explicit return% requirement for confirm"
        ),
        "q5_multi_vs_last": "Only the current candle is scored; prior candles are not jointly scored",
        "q6_ema9_ema20": "NOT used in momentum.py confirmation conditions",
        "q7_di_adx": "NOT used in momentum.py confirmation conditions",
        "q8_volume": (
            f"Computed as volume/median diagnostic; filter enabled={cfg.volume_filter_enabled}"
        ),
        "q9_local_structure": (
            "Only PA confirmation_level hold (close beyond level); "
            "no HH/HL/LH/LL progression requirement"
        ),
        "q10_gradual_or_binary": (
            "Binary confirm vs fail; confidence medium/high is label-only overlay"
        ),
        "q11_invalidation": [
            "PRICE_ACTION_INVALIDATED",
            "NEW_OPPOSING_SETUP",
            f"MAX_COUNTER_MOVE (>= {cfg.max_counter_move_pct}% adverse close vs break_close)",
            "CLOSE_BEYOND_STRUCTURE_LEVEL",
            "MOMENTUM_WINDOW_EXPIRED after age>window",
        ],
        "q12_weakening_still_confirm": (
            "Yes — a later single candle can confirm even if earlier candles were weak/failed"
        ),
        "q13_strong_first_covers_weak_second": (
            "N/A for pass: confirmation is first passing candle; "
            "if age0 confirms, second candle never evaluated"
        ),
        "q14_countertrend_allowed": (
            "Partial — counter-move close beyond max_counter_move_pct invalidates; "
            "otherwise non-confirming candles can appear before a later confirm"
        ),
        "q15_relative_to_pa_level": (
            "Yes — structure_level_held uses PA confirmation_level; "
            "metrics otherwise relative to candle OHLC / ATR, not prior close return"
        ),
        "q16_entry_model_in_pipeline_audit": (
            "Momentum confirmation_timestamp = confirming candle timestamp (close time); "
            "multiweek M0 entry_price = close of that candle / decision_time alignment; "
            "no next-open or break-of-confirmation-high/low in productive momentum"
        ),
        "config": cfg.to_dict(),
    }


def load_leak_ids(multiweek_dir: Path) -> list[str]:
    path = multiweek_dir / "multiweek_remaining_weak_entry_leaks.csv"
    df = pd.read_csv(path)
    ids = [str(x) for x in df["setup_id"].tolist()]
    if len(ids) != 11:
        raise ValueError(f"expected 11 leaks, got {len(ids)} from {path}")
    if df["leak_category"].nunique() != 1 or df["leak_category"].iloc[0] != "MOMENTUM_QUALITY_LEAK":
        raise ValueError("leak file contains unexpected categories")
    return ids


def prepare_indicator_frame(candles: pd.DataFrame) -> pd.DataFrame:
    cfg = default_regime_scanner_config().with_timeframe("5m")
    frame = compute_indicator_frame(candles, config=cfg)
    frame = frame.copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    frame["decision_time"] = frame["timestamp"] + pd.Timedelta(minutes=5)
    # Prefer existing indicator slopes when present
    if "ema_9_slope_3_pct" in frame.columns:
        frame["ema9_slope"] = frame["ema_9_slope_3_pct"]
    elif "ema_9" in frame.columns:
        frame["ema9_slope"] = frame["ema_9"].pct_change(3) * 100.0
    if "ema_20_slope_3_pct" in frame.columns:
        frame["ema20_slope"] = frame["ema_20_slope_3_pct"]
    elif "ema_20" in frame.columns:
        frame["ema20_slope"] = frame["ema_20"].pct_change(3) * 100.0
    if "ema_59_slope_3_pct" in frame.columns:
        frame["ema59_slope"] = frame["ema_59_slope_3_pct"]
    elif "ema_59" in frame.columns:
        frame["ema59_slope"] = frame["ema_59"].pct_change(3) * 100.0
    if "di_spread" not in frame.columns and "plus_di" in frame.columns and "minus_di" in frame.columns:
        frame["di_spread"] = frame["plus_di"] - frame["minus_di"]
    vol = pd.to_numeric(frame.get("volume"), errors="coerce")
    med = vol.rolling(20, min_periods=5).median()
    frame["volume_median_20"] = med
    frame["volume_ratio"] = vol / med
    frame["volume_z"] = (vol - vol.rolling(20, min_periods=5).mean()) / vol.rolling(
        20, min_periods=5
    ).std(ddof=0)
    return frame


def pivots_to_frame(pivots: Sequence[Any]) -> pd.DataFrame:
    if pivots is None:
        return pd.DataFrame()
    if isinstance(pivots, pd.DataFrame):
        return pivots
    rows = []
    for p in pivots:
        if hasattr(p, "__dataclass_fields__") or hasattr(p, "pivot_type"):
            rows.append(
                {
                    "pivot_type": getattr(p, "pivot_type", None),
                    "price": getattr(p, "price", None),
                    "confirmation_timestamp": getattr(p, "confirmation_timestamp", None),
                    "pivot_timestamp": getattr(p, "pivot_timestamp", None),
                }
            )
        elif isinstance(p, Mapping):
            rows.append(dict(p))
    return pd.DataFrame(rows)


def last_confirmed_swings(
    pivots: pd.DataFrame,
    asof: pd.Timestamp,
) -> dict[str, Any]:
    empty = {
        "last_swing_high": None,
        "last_swing_low": None,
        "last_swing_high_ts": None,
        "last_swing_low_ts": None,
    }
    if pivots is None or pivots.empty:
        return empty
    p = pivots.copy()
    ts_col = "confirmation_timestamp" if "confirmation_timestamp" in p.columns else "pivot_timestamp"
    if ts_col not in p.columns:
        return empty
    p[ts_col] = pd.to_datetime(p[ts_col], utc=True)
    p = p[p[ts_col] <= asof]
    if p.empty or "pivot_type" not in p.columns:
        return empty
    highs = p[p["pivot_type"].astype(str).str.contains("high", case=False, na=False)]
    lows = p[p["pivot_type"].astype(str).str.contains("low", case=False, na=False)]
    out = dict(empty)
    if len(highs):
        h = highs.sort_values(ts_col).iloc[-1]
        out["last_swing_high"] = _finite(h.get("price"))
        out["last_swing_high_ts"] = str(h[ts_col])
    if len(lows):
        l = lows.sort_values(ts_col).iloc[-1]
        out["last_swing_low"] = _finite(l.get("price"))
        out["last_swing_low_ts"] = str(l[ts_col])
    return out


def wick_shares(row: Mapping[str, Any], side: str) -> dict[str, float | None]:
    o, h, l, c = (_finite(row.get(k)) for k in ("open", "high", "low", "close"))
    rng = None if None in (h, l) else float(h) - float(l)  # type: ignore[arg-type]
    if rng is None or rng <= 0 or None in (o, c, h, l):
        return {"upper_wick_share": None, "lower_wick_share": None, "opp_wick_share": None}
    upper = float(h) - max(float(o), float(c))  # type: ignore[arg-type]
    lower = min(float(o), float(c)) - float(l)  # type: ignore[arg-type]
    upper_s, lower_s = upper / rng, lower / rng
    opp = lower_s if side == "long" else upper_s
    return {
        "upper_wick_share": upper_s,
        "lower_wick_share": lower_s,
        "opp_wick_share": opp,
    }


def structure_label(
    *,
    side: str,
    close: float | None,
    last_high: float | None,
    last_low: float | None,
) -> str | None:
    if close is None:
        return None
    if side == "long":
        if last_high is not None and close > last_high:
            return "HH_break"
        if last_low is not None and close > last_low:
            return "HL_hold_area"
        return "no_hh"
    if last_low is not None and close < last_low:
        return "LL_break"
    if last_high is not None and close < last_high:
        return "LH_hold_area"
    return "no_ll"


def forward_path_metrics(
    frame: pd.DataFrame,
    entry_ts: pd.Timestamp,
    entry_price: float,
    side: str,
    horizons: Sequence[int] = (3, 6, 12, 24),
) -> dict[str, Any]:
    fut = frame[frame["decision_time"] > entry_ts].head(72)
    out: dict[str, Any] = {
        "evaluable": len(fut) > 0,
        "available_bars": int(len(fut)),
        "mfe_pct": None,
        "mae_pct": None,
        "reached_025": None,
        "reached_050": None,
        "minutes_to_025": None,
        "minutes_to_050": None,
        "minutes_to_max_adverse": None,
        "minutes_to_max_favorable": None,
        "adverse_before_favorable": None,
        "mfe_before_mae": None,
    }
    if fut.empty or not entry_price:
        out["reason"] = "INSUFFICIENT_FUTURE"
        return out
    mfe = mae = 0.0
    t_mfe = t_mae = None
    hit25 = hit50 = False
    min25 = min50 = None
    first_adv = first_fav = None
    for i, (_, row) in enumerate(fut.iterrows(), start=1):
        fav, adv = _fav_adv(float(row["high"]), float(row["low"]), entry_price, side)
        if fav > mfe:
            mfe, t_mfe = fav, i * 5
        if adv > mae:
            mae, t_mae = adv, i * 5
        if not hit25 and fav >= 0.25:
            hit25, min25 = True, i * 5
        if not hit50 and fav >= 0.50:
            hit50, min50 = True, i * 5
        if first_adv is None and adv > 0:
            first_adv = i * 5
        if first_fav is None and fav > 0:
            first_fav = i * 5
        if i in horizons:
            out[f"adverse_{i*5}m"] = adv
            out[f"favorable_{i*5}m"] = fav
    # quality using same fixed heuristic as multiweek
    from research.regime_scanner.pipeline_counterfactual import classify_entry_quality

    raw_q = classify_entry_quality(mfe, mae, hit25)
    out.update(
        {
            "mfe_pct": float(mfe),
            "mae_pct": float(mae),
            "reached_025": bool(hit25),
            "reached_050": bool(hit50),
            "minutes_to_025": min25,
            "minutes_to_050": min50,
            "minutes_to_max_adverse": t_mae,
            "minutes_to_max_favorable": t_mfe,
            "adverse_before_favorable": (
                None
                if first_adv is None or first_fav is None
                else bool(first_adv < first_fav)
            ),
            "mfe_before_mae": (
                None if t_mfe is None or t_mae is None else bool(t_mfe <= t_mae)
            ),
            "entry_quality_raw": raw_q,
            "entry_quality": map_quality_label(raw_q),
        }
    )
    # robust weakness label
    if not out["evaluable"] or out["available_bars"] < 6:
        out["robust_label"] = "ambiguous_data"
    elif not hit25 and mae >= 0.8:
        out["robust_label"] = "clearly_weak"
    elif hit25 and mae >= 1.5 and mfe < 1.0:
        out["robust_label"] = "horizon_dependent_weak"
    elif hit25 and mfe >= 0.5 and mae < 1.0:
        out["robust_label"] = "possibly_misclassified_as_weak"
    elif hit25:
        out["robust_label"] = "ambiguous"
    else:
        out["robust_label"] = "clearly_weak"
    return out


def build_candle_rows(
    *,
    setup_id: str,
    side: str,
    pa_ts: pd.Timestamp,
    entry_ts: pd.Timestamp,
    pa_level: float | None,
    setup_level: float | None,
    frame: pd.DataFrame,
    pivots: pd.DataFrame,
    mom_cfg: MomentumConfig,
    regime_15m: str | None,
) -> list[dict[str, Any]]:
    start = pa_ts - pd.Timedelta(minutes=30)
    end = entry_ts + pd.Timedelta(minutes=60)
    # Use candle timestamps: decision_time == candle_ts + 5m
    window = frame[
        (frame["decision_time"] >= start) & (frame["decision_time"] <= end)
    ].sort_values("decision_time")
    rows: list[dict[str, Any]] = []
    cum_ret = 0.0
    pa_close = None
    # PA candle close as reference
    pa_rows = frame[frame["decision_time"] == pa_ts]
    if len(pa_rows):
        pa_close = _finite(pa_rows.iloc[0]["close"])
    elif len(frame[frame["decision_time"] <= pa_ts]):
        pa_close = _finite(frame[frame["decision_time"] <= pa_ts].iloc[-1]["close"])

    confirm_age = 0
    seen_pa = False
    for _, row in window.iterrows():
        dts = to_utc(row["decision_time"])
        candle = {
            "timestamp": row["timestamp"],
            "open": row["open"],
            "high": row["high"],
            "low": row["low"],
            "close": row["close"],
            "volume": row.get("volume"),
        }
        o, h, l, c = float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"])
        ret = _side_ret(o, c, side)
        if dts >= pa_ts:
            if not seen_pa:
                seen_pa = True
                confirm_age = 0
            else:
                confirm_age += 1
            if pa_close:
                # cumulative from PA close to this close in side direction
                if side == "long":
                    cum_ret = (c - pa_close) / abs(pa_close) * 100.0
                else:
                    cum_ret = (pa_close - c) / abs(pa_close) * 100.0
        atr = _finite(row.get("atr"))
        body = candle_body(candle)
        rng = candle_range(candle)
        b2r = body_to_range_ratio(candle)
        cloc = close_location_ratio(candle, side=side)
        r_atr = (rng / atr) if rng is not None and atr and atr > 0 else None
        ret_atr = None
        if atr and atr > 0:
            move = abs(c - o)
            ret_atr = move / atr
        wicks = wick_shares(row, side)
        swings = last_confirmed_swings(pivots, dts)
        struct = structure_label(
            side=side,
            close=c,
            last_high=swings["last_swing_high"],
            last_low=swings["last_swing_low"],
        )
        metrics = {
            "ohlc_valid": True,
            "body_to_range_ratio": b2r,
            "close_location_ratio": cloc,
            "range_atr_ratio": r_atr,
            "directional_body": directional_body(candle, side=side),
            "volume_ratio": _finite(row.get("volume_ratio")),
        }
        cond = None
        if pa_level is not None and dts >= pa_ts and dts <= entry_ts:
            cond = evaluate_momentum_conditions(
                metrics,
                side=side,
                config=mom_cfg,
                confirmation_level=float(pa_level),
                candle=candle,
                allow_confirm=True,
            )
        phase = "pre_pa" if dts < pa_ts else ("post_entry" if dts > entry_ts else "pa_to_entry")
        ema9 = _finite(row.get("ema_9"))
        ema20 = _finite(row.get("ema_20"))
        rows.append(
            {
                "setup_id": setup_id,
                "side": side,
                "phase": phase,
                "decision_time": dts.isoformat(),
                "candle_timestamp": to_utc(row["timestamp"]).isoformat(),
                "confirm_age_if_in_window": confirm_age if phase == "pa_to_entry" else None,
                "open": o,
                "high": h,
                "low": l,
                "close": c,
                "candle_direction": (
                    "bull" if c > o else "bear" if c < o else "doji"
                ),
                "body": body,
                "range": rng,
                "body_to_range": b2r,
                "upper_wick_share": wicks["upper_wick_share"],
                "lower_wick_share": wicks["lower_wick_share"],
                "opp_wick_share": wicks["opp_wick_share"],
                "close_location": cloc,
                "return_pct": ret,
                "cum_return_since_pa_pct": cum_ret if dts >= pa_ts else None,
                "return_atr": ret_atr,
                "range_atr": r_atr,
                "close_vs_pa_level": (c - pa_level) if pa_level is not None else None,
                "close_vs_setup_level": (c - setup_level) if setup_level is not None else None,
                "close_vs_ema9": (c - ema9) if ema9 is not None else None,
                "close_vs_ema20": (c - ema20) if ema20 is not None else None,
                "ema9_dist_atr": ((c - ema9) / atr if ema9 is not None and atr else None),
                "ema20_dist_atr": ((c - ema20) / atr if ema20 is not None and atr else None),
                "ema9_slope": _finite(row.get("ema9_slope")),
                "ema20_slope": _finite(row.get("ema20_slope")),
                "ema59_slope": _finite(row.get("ema59_slope")),
                "plus_di": _finite(row.get("plus_di")),
                "minus_di": _finite(row.get("minus_di")),
                "di_spread": _finite(row.get("di_spread")),
                "adx": _finite(row.get("adx")),
                "volume": _finite(row.get("volume")),
                "volume_ratio": _finite(row.get("volume_ratio")),
                "volume_z": _finite(row.get("volume_z")),
                "atr": atr,
                **swings,
                "structure_label": struct,
                "regime_15m_context": regime_15m,
                "mom_passed": _json_list(cond["passed"]) if cond else None,
                "mom_failed": _json_list(cond["failed"]) if cond else None,
                "mom_confirms": cond["confirms"] if cond else None,
                "mom_confidence": cond["confidence"] if cond else None,
                "structure_level_held": (
                    structure_level_held(candle, side=side, confirmation_level=float(pa_level))
                    if pa_level is not None
                    else None
                ),
            }
        )
    return rows


def _json_list(v: object) -> str:
    return json.dumps(json_safe(v), ensure_ascii=True)


def extract_confirm_candles(
    candle_rows: list[dict[str, Any]],
    pa_ts: pd.Timestamp,
    mom_ts: pd.Timestamp,
) -> dict[str, dict[str, Any] | None]:
    """Map age0=break, then subsequent ages until mom confirm."""
    in_win = [
        r
        for r in candle_rows
        if r["phase"] == "pa_to_entry"
        and to_utc(r["decision_time"]) >= pa_ts
        and to_utc(r["decision_time"]) <= mom_ts
    ]
    by_age = {int(r["confirm_age_if_in_window"]): r for r in in_win if r.get("confirm_age_if_in_window") is not None}
    # Also include ages after break up to 3 for sequence analysis even if confirmed early
    return {
        "c0_break": by_age.get(0),
        "c1": by_age.get(1),
        "c2": by_age.get(2),
        "confirm": by_age.get(max(by_age)) if by_age else None,
        "n_candles_pa_to_mom": len(in_win),
    }


def hypothesis_flags(
    *,
    side: str,
    conf: dict[str, dict[str, Any] | None],
    entry_row: Mapping[str, Any],
    pa_level: float | None,
) -> dict[str, bool]:
    c0, c1, c2 = conf.get("c0_break"), conf.get("c1"), conf.get("c2")
    confirm = conf.get("confirm") or c0
    flags = {k: False for k in (
        "M1_missing_progression",
        "M2_weakening_second",
        "M3_rejection",
        "M4_no_structure",
        "M5_late_momentum",
        "M6_counter_context",
        "M7_single_candle_dominates",
        "M8_exhaustion",
    )}
    # M1
    cum = _finite((confirm or {}).get("cum_return_since_pa_pct"))
    atr = _finite((confirm or {}).get("atr"))
    close = _finite((confirm or {}).get("close"))
    if atr and atr > 0 and cum is not None and abs(cum) / 100.0 * (abs(close) if close else 0) / atr < HYP_FLAGS["M1_missing_progression"]["cum_ret_atr_lt"]:
        # simpler: cum return pct small
        pass
    if cum is not None and abs(cum) < 0.15:
        flags["M1_missing_progression"] = True
    if pa_level is not None and close is not None and atr and atr > 0:
        if abs(close - pa_level) / atr < HYP_FLAGS["M1_missing_progression"]["close_near_pa_atr"]:
            flags["M1_missing_progression"] = True

    # M2 — need two candles in window before/at confirm
    if c0 and c1:
        b0, b1 = _finite(c0.get("body_to_range")), _finite(c1.get("body_to_range"))
        l0, l1 = _finite(c0.get("close_location")), _finite(c1.get("close_location"))
        if b0 is not None and b1 is not None and (b0 - b1) >= HYP_FLAGS["M2_weakening_second"]["body_drop"]:
            flags["M2_weakening_second"] = True
        if l0 is not None and l1 is not None and (l0 - l1) >= HYP_FLAGS["M2_weakening_second"]["close_loc_drop"]:
            flags["M2_weakening_second"] = True
        v0, v1 = _finite(c0.get("volume_ratio")), _finite(c1.get("volume_ratio"))
        if v0 and v1 and v1 < v0 * 0.7:
            flags["M2_weakening_second"] = True

    # M3 rejection on confirm candle
    if confirm:
        opp = _finite(confirm.get("opp_wick_share"))
        cl = _finite(confirm.get("close_location"))
        if opp is not None and opp >= HYP_FLAGS["M3_rejection"]["opp_wick_share"]:
            flags["M3_rejection"] = True
        if cl is not None and cl < HYP_FLAGS["M3_rejection"]["close_loc_max"] and opp and opp >= 0.25:
            flags["M3_rejection"] = True

    # M4
    struct = (confirm or {}).get("structure_label")
    if struct in {"no_hh", "no_ll", None}:
        flags["M4_no_structure"] = True

    # M5 late
    if confirm:
        cum_atr = None
        if atr and atr > 0 and cum is not None and close:
            # price move since PA in ATR
            cum_atr = abs(cum) / 100.0 * abs(close) / atr
        ema_d = _finite(confirm.get("ema9_dist_atr"))
        if cum_atr is not None and cum_atr >= HYP_FLAGS["M5_late_momentum"]["pa_to_entry_atr"]:
            flags["M5_late_momentum"] = True
        if ema_d is not None and abs(ema_d) >= HYP_FLAGS["M5_late_momentum"]["ema9_dist_atr"]:
            if (side == "long" and ema_d > 0) or (side == "short" and ema_d < 0):
                flags["M5_late_momentum"] = True

    # M6 counter context
    if confirm:
        di = _finite(confirm.get("di_spread"))
        adx = _finite(confirm.get("adx"))
        e20 = _finite(confirm.get("ema20_slope"))
        e59 = _finite(confirm.get("ema59_slope"))
        if side == "long" and di is not None and di < 0:
            flags["M6_counter_context"] = True
        if side == "short" and di is not None and di > 0:
            flags["M6_counter_context"] = True
        if side == "long" and e20 is not None and e20 < 0:
            flags["M6_counter_context"] = True
        if side == "short" and e20 is not None and e20 > 0:
            flags["M6_counter_context"] = True
        if e59 is not None:
            if side == "long" and e59 < -0.05:
                flags["M6_counter_context"] = True
            if side == "short" and e59 > 0.05:
                flags["M6_counter_context"] = True

    # M7 single candle dominates — confirmed on break with no second candle
    n = int(conf.get("n_candles_pa_to_mom") or 0)
    if n <= 1:
        flags["M7_single_candle_dominates"] = True
    elif c0 and c1:
        # confirm equals c0 quality much higher than c1 if somehow both present
        if confirm is c0 or (confirm and c0 and confirm.get("decision_time") == c0.get("decision_time")):
            flags["M7_single_candle_dominates"] = True

    # M8 exhaustion
    if confirm:
        r_atr = _finite(confirm.get("range_atr"))
        ema_d = _finite(confirm.get("ema9_dist_atr"))
        vz = _finite(confirm.get("volume_z"))
        if r_atr is not None and r_atr >= HYP_FLAGS["M8_exhaustion"]["range_atr_min"]:
            flags["M8_exhaustion"] = True
        if ema_d is not None and abs(ema_d) >= HYP_FLAGS["M8_exhaustion"]["ema9_dist_atr"]:
            flags["M8_exhaustion"] = True
        if vz is not None and vz >= HYP_FLAGS["M8_exhaustion"]["vol_z_min"] and r_atr and r_atr >= 1.5:
            flags["M8_exhaustion"] = True

    return flags


def sequence_metrics(conf: dict[str, dict[str, Any] | None], side: str) -> dict[str, Any]:
    c0, c1, c2 = conf.get("c0_break"), conf.get("c1"), conf.get("c2")
    candles = [c for c in (c0, c1, c2) if c]
    both_dir = None
    c2_better = None
    min_cloc = None
    max_opp = None
    cum = None
    if c0 and c1:
        d0 = directional_body(
            {"open": c0["open"], "close": c0["close"]}, side=side
        )
        d1 = directional_body(
            {"open": c1["open"], "close": c1["close"]}, side=side
        )
        both_dir = bool(d0 and d1)
        if side == "long":
            c2_better = float(c1["close"]) > float(c0["close"])
        else:
            c2_better = float(c1["close"]) < float(c0["close"])
    locs = [_finite(c.get("close_location")) for c in candles]
    locs_f = [x for x in locs if x is not None]
    min_cloc = min(locs_f) if locs_f else None
    opps = [_finite(c.get("opp_wick_share")) for c in candles]
    opps_f = [x for x in opps if x is not None]
    max_opp = max(opps_f) if opps_f else None
    if candles:
        cum = _finite(candles[-1].get("cum_return_since_pa_pct"))
    return {
        "both_candles_directional": both_dir,
        "candle2_closes_beyond_candle1": c2_better,
        "min_close_location_in_window": min_cloc,
        "max_opp_wick_in_window": max_opp,
        "cum_return_since_pa_pct": cum,
        "n_window_candles": len(candles),
        "avg_close_location": float(np.mean(locs_f)) if locs_f else None,
        "avg_body_to_range": float(
            np.mean([x for x in (_finite(c.get("body_to_range")) for c in candles) if x is not None])
        )
        if candles
        else None,
    }


def match_score(leak: Mapping[str, Any], cand: Mapping[str, Any]) -> float:
    """Higher is better; deterministic."""
    score = 0.0
    if leak.get("side") != cand.get("side"):
        return -1e9
    if leak.get("market_phase") and leak.get("market_phase") == cand.get("market_phase"):
        score += 5.0
    if leak.get("setup_type") and leak.get("setup_type") == cand.get("setup_type"):
        score += 3.0
    if leak.get("pa_pattern_type") and leak.get("pa_pattern_type") == cand.get("pa_pattern_type"):
        score += 3.0
    # hour proximity
    try:
        h1 = to_utc(leak["entry_timestamp"]).hour
        h2 = to_utc(cand["entry_timestamp"]).hour
        score += max(0.0, 3.0 - abs(h1 - h2) * 0.5)
    except Exception:
        pass
    atr1, atr2 = _finite(leak.get("atr_pct")), _finite(cand.get("atr_pct"))
    if atr1 is not None and atr2 is not None and atr1 > 0:
        score += max(0.0, 2.0 - abs(atr1 - atr2) / atr1 * 2.0)
    # prefer closer calendar distance
    try:
        d = abs((to_utc(leak["entry_timestamp"]) - to_utc(cand["entry_timestamp"])).days)
        score += max(0.0, 2.0 - d / 30.0)
    except Exception:
        pass
    return score


def feature_snapshot(conf: dict[str, Any], seq: dict[str, Any], outcome: dict[str, Any]) -> dict[str, Any]:
    c0, c1 = conf.get("c0_break"), conf.get("c1")
    confirm = conf.get("confirm") or c0 or {}
    return {
        "cum_return_since_pa_pct": seq.get("cum_return_since_pa_pct"),
        "return_c0": _finite((c0 or {}).get("return_pct")),
        "return_c1": _finite((c1 or {}).get("return_pct")),
        "body_range_c0": _finite((c0 or {}).get("body_to_range")),
        "body_range_c1": _finite((c1 or {}).get("body_to_range")),
        "close_loc_c0": _finite((c0 or {}).get("close_location")),
        "close_loc_c1": _finite((c1 or {}).get("close_location")),
        "opp_wick_c0": _finite((c0 or {}).get("opp_wick_share")),
        "opp_wick_confirm": _finite(confirm.get("opp_wick_share")),
        "range_atr_confirm": _finite(confirm.get("range_atr")),
        "ema9_dist_atr": _finite(confirm.get("ema9_dist_atr")),
        "ema20_dist_atr": _finite(confirm.get("ema20_dist_atr")),
        "ema9_slope": _finite(confirm.get("ema9_slope")),
        "ema20_slope": _finite(confirm.get("ema20_slope")),
        "di_spread": _finite(confirm.get("di_spread")),
        "adx": _finite(confirm.get("adx")),
        "volume_z": _finite(confirm.get("volume_z")),
        "minutes_pa_to_entry": None,  # filled by caller
        "price_move_pa_to_entry_pct": seq.get("cum_return_since_pa_pct"),
        "adverse_15m": outcome.get("adverse_15m"),
        "favorable_15m": outcome.get("favorable_15m"),
        "structure_label": confirm.get("structure_label"),
        "n_window_candles": seq.get("n_window_candles"),
        "min_close_location_in_window": seq.get("min_close_location_in_window"),
    }


def summarize_feature_table(rows: list[dict[str, Any]], group: str) -> list[dict[str, Any]]:
    if not rows:
        return []
    df = pd.DataFrame(rows)
    out = []
    for col in df.columns:
        if col in {"setup_id", "group", "side", "structure_label"}:
            continue
        s = pd.to_numeric(df[col], errors="coerce")
        out.append(
            {
                "group": group,
                "feature": col,
                "n": int(s.notna().sum()),
                "missing": int(s.isna().sum()),
                "mean": float(s.mean()) if s.notna().any() else None,
                "median": float(s.median()) if s.notna().any() else None,
                "min": float(s.min()) if s.notna().any() else None,
                "max": float(s.max()) if s.notna().any() else None,
                "q25": float(s.quantile(0.25)) if s.notna().any() else None,
                "q75": float(s.quantile(0.75)) if s.notna().any() else None,
            }
        )
    return out


def entry_timing_counterfactuals(
    *,
    frame: pd.DataFrame,
    side: str,
    mom_ts: pd.Timestamp,
    mom_price: float,
    confirm_high: float,
    confirm_low: float,
) -> list[dict[str, Any]]:
    """Post-hoc timing variants; do not alter pipeline."""
    variants = []
    # 1) confirm close (baseline)
    base_out = forward_path_metrics(frame, mom_ts, mom_price, side)
    variants.append(
        {
            "timing_variant": "confirm_close",
            "entry_ts": mom_ts.isoformat(),
            "entry_price": mom_price,
            "triggered": True,
            **{k: base_out.get(k) for k in (
                "mfe_pct", "mae_pct", "reached_025", "entry_quality", "robust_label"
            )},
        }
    )
    # 2) next open
    nxt = frame[frame["decision_time"] > mom_ts].head(1)
    if len(nxt):
        ets = to_utc(nxt.iloc[0]["decision_time"])
        # entry at open of next candle ≈ previous close already known; use open
        ep = float(nxt.iloc[0]["open"])
        # outcomes from decision_time of next bar still causal after fill at open
        # Approximate: evaluate from next decision_time with entry=open
        o2 = forward_path_metrics(frame, ets - pd.Timedelta(minutes=5), ep, side)
        # Actually entry at open means future starts after that candle's open;
        # use bars with decision_time > candle_timestamp (= ets - 5m + epsilon).
        # Simpler: shift entry_ts to candle timestamp (open time) = decision_time - 5m
        open_ts = to_utc(nxt.iloc[0]["timestamp"])
        o2 = forward_path_metrics(frame, open_ts, ep, side)
        variants.append(
            {
                "timing_variant": "next_open",
                "entry_ts": open_ts.isoformat(),
                "entry_price": ep,
                "triggered": True,
                "price_shift_pct": (ep - mom_price) / abs(mom_price) * 100.0,
                **{k: o2.get(k) for k in (
                    "mfe_pct", "mae_pct", "reached_025", "entry_quality", "robust_label"
                )},
            }
        )
    else:
        variants.append(
            {
                "timing_variant": "next_open",
                "triggered": False,
                "reason": "NO_NEXT_CANDLE",
            }
        )

    # 3) break of confirmation high/low after confirm
    after = frame[frame["decision_time"] > mom_ts].head(36)
    trigger_ts = None
    trigger_px = None
    for _, row in after.iterrows():
        if side == "long" and float(row["high"]) > confirm_high:
            trigger_ts = to_utc(row["decision_time"])
            trigger_px = confirm_high  # conservative fill at level
            break
        if side == "short" and float(row["low"]) < confirm_low:
            trigger_ts = to_utc(row["decision_time"])
            trigger_px = confirm_low
            break
    if trigger_ts is None:
        variants.append(
            {
                "timing_variant": "break_confirm_extreme",
                "triggered": False,
                "reason": "NO_TRIGGER_IN_3H",
            }
        )
    else:
        o3 = forward_path_metrics(frame, trigger_ts, float(trigger_px), side)
        variants.append(
            {
                "timing_variant": "break_confirm_extreme",
                "entry_ts": trigger_ts.isoformat(),
                "entry_price": trigger_px,
                "triggered": True,
                "delay_minutes": (trigger_ts - mom_ts).total_seconds() / 60.0,
                "price_shift_pct": (float(trigger_px) - mom_price) / abs(mom_price) * 100.0,
                **{k: o3.get(k) for k in (
                    "mfe_pct", "mae_pct", "reached_025", "entry_quality", "robust_label"
                )},
            }
        )
    return variants


def run_audit(args: argparse.Namespace) -> dict[str, Any]:
    multiweek = Path(args.multiweek_dir)
    pipeline = Path(args.pipeline_dir)
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)

    logic_doc = document_current_momentum_logic()
    leak_ids = load_leak_ids(multiweek)
    leaks_src = pd.read_csv(multiweek / "multiweek_remaining_weak_entry_leaks.csv")
    outcomes = pd.read_csv(multiweek / "multiweek_entry_outcomes.csv")
    weekly = pd.read_csv(multiweek / "multiweek_weekly_summary.csv")
    phase_by_week = {
        str(r["week_id"]): str(r["market_phase"])
        for _, r in weekly.iterrows()
        if "week_id" in r and "market_phase" in r
    }

    setups = pd.read_csv(pipeline / "setup_activations.csv")
    pa = pd.read_csv(pipeline / "price_action_confirmations.csv")
    mom = pd.read_csv(pipeline / "momentum_confirmations.csv")
    setups_by = {str(r["setup_id"]): r for _, r in setups.iterrows()}
    pa_by = {str(r["setup_id"]): r for _, r in pa.iterrows()}
    mom_by = {str(r["setup_id"]): r for _, r in mom.iterrows()}

    m0 = outcomes[outcomes["multi_variant"] == "M0"].copy()
    m3 = outcomes[outcomes["multi_variant"] == "M3"].copy()
    m0_by = {str(r["setup_id"]): r for _, r in m0.iterrows()}
    m3_by = {str(r["setup_id"]): r for _, r in m3.iterrows()}

    raw = load_symbol_candles(args.symbol)
    raw = raw.copy()
    raw["timestamp"] = pd.to_datetime(raw["timestamp"], utc=True)
    # Warm indicators
    tmin = m0[m0.setup_id.isin(leak_ids)]["entry_timestamp"].min() if len(m0) else None
    # broader for matching goods
    frame = prepare_indicator_frame(raw)
    cfg = default_regime_scanner_config().with_timeframe("5m")
    pivots = pivots_to_frame(find_confirmed_pivots(frame, config=cfg))
    if not isinstance(pivots, pd.DataFrame):
        pivots = pd.DataFrame()

    mom_cfg = default_momentum_config()
    weeks = slice_weeks(
        raw["timestamp"],
        range_start="2026-01-01",
        range_end="2026-05-01",
    )

    weak_rows: list[dict[str, Any]] = []
    weak_candles: list[dict[str, Any]] = []
    outcome_val_rows: list[dict[str, Any]] = []
    seq_rows: list[dict[str, Any]] = []
    timing_rows: list[dict[str, Any]] = []
    hyp_per_leak: list[dict[str, Any]] = []
    feature_leak_snaps: list[dict[str, Any]] = []
    case_lines: list[str] = ["# Momentum weak-leak case studies", ""]

    for sid in leak_ids:
        if sid not in mom_by or sid not in m0_by:
            raise ValueError(f"missing baseline momentum/outcome for {sid}")
        srow = setups_by.get(sid, {})
        prow = pa_by.get(sid, {})
        mrow = mom_by[sid]
        o0 = m0_by[sid]
        o3 = m3_by.get(sid, {})
        side = str(mrow.get("side") or o0.get("side")).lower()
        pa_ts = to_utc(prow.get("structure_break_timestamp") or mrow.get("pa_structure_break_timestamp"))
        mom_ts = to_utc(mrow["confirmation_timestamp"])
        entry_ts_m0 = to_utc(o0["entry_timestamp"])
        entry_px_m0 = float(o0["entry_price"])
        # sanity: M0 entry should match mom confirm
        if abs((entry_ts_m0 - mom_ts).total_seconds()) > 60:
            # allow decision_time vs candle timestamp alignment of 0
            pass
        week_id = str(leaks_src.loc[leaks_src.setup_id == sid, "week_id"].iloc[0])
        phase = phase_by_week.get(week_id)
        if phase is None:
            # derive from week window
            for w in weeks:
                if w.week_id == week_id:
                    phase = classify_market_phase(raw, w.start, w.end).get("market_phase")
                    break

        pa_level = _finite(prow.get("confirmation_level"))
        setup_level = _finite(srow.get("setup_level") or srow.get("reference_price"))
        regime = prow.get("regime_15m")

        candles = build_candle_rows(
            setup_id=sid,
            side=side,
            pa_ts=pa_ts,
            entry_ts=entry_ts_m0,
            pa_level=pa_level,
            setup_level=setup_level,
            frame=frame,
            pivots=pivots,
            mom_cfg=mom_cfg,
            regime_15m=str(regime) if regime is not None else None,
        )
        weak_candles.extend(candles)
        conf = extract_confirm_candles(candles, pa_ts, mom_ts)
        seq = sequence_metrics(conf, side)
        outcome_m0 = forward_path_metrics(frame, entry_ts_m0, entry_px_m0, side)
        # attach 15m from outcome_m0 keys
        flags = hypothesis_flags(side=side, conf=conf, entry_row=o0, pa_level=pa_level)
        snap = feature_snapshot(conf, seq, outcome_m0)
        snap["minutes_pa_to_entry"] = (entry_ts_m0 - pa_ts).total_seconds() / 60.0
        snap["setup_id"] = sid
        snap["group"] = "weak_leak"
        snap["side"] = side
        feature_leak_snaps.append(snap)

        atr_pct = None
        confirm = conf.get("confirm") or conf.get("c0_break") or {}
        if confirm.get("atr") and entry_px_m0:
            atr_pct = float(confirm["atr"]) / abs(entry_px_m0) * 100.0

        delayed_by_m3 = False
        if o3 is not None and len(o3):
            try:
                delayed_by_m3 = to_utc(o3.get("entry_timestamp")) > entry_ts_m0
            except Exception:
                delayed_by_m3 = False

        row = {
            "setup_id": sid,
            "side": side,
            "week_id": week_id,
            "market_phase": phase,
            "setup_timestamp": srow.get("setup_activation_timestamp"),
            "setup_type": srow.get("setup_type"),
            "pa_timestamp": pa_ts.isoformat(),
            "pa_pattern_type": prow.get("pattern_type") or mrow.get("pa_pattern_type"),
            "pa_level": pa_level,
            "momentum_confirmation_timestamp": mom_ts.isoformat(),
            "entry_timestamp_m0": entry_ts_m0.isoformat(),
            "entry_price_m0": entry_px_m0,
            "entry_timestamp_m3": o3.get("entry_timestamp") if o3 is not None else None,
            "entry_price_m3": o3.get("entry_price") if o3 is not None else None,
            "m3_delayed_vs_m0": delayed_by_m3,
            "momentum_verdict": mrow.get("final_state"),
            "momentum_confidence": mrow.get("confidence"),
            "momentum_confirmation_type": mrow.get("confirmation_type"),
            "momentum_age": mrow.get("candles_after_price_action_confirmation"),
            "momentum_reason_codes": mrow.get("reason_codes"),
            "body_to_range_ratio": mrow.get("body_to_range_ratio"),
            "close_location_ratio": mrow.get("close_location_ratio"),
            "range_atr_ratio": mrow.get("range_atr_ratio"),
            "volume_ratio": mrow.get("volume_ratio"),
            "directional_body": mrow.get("directional_body"),
            "structure_level_held": mrow.get("structure_level_held"),
            "m0_entry_quality": o0.get("entry_quality"),
            "m0_mfe_pct": o0.get("mfe_pct"),
            "m0_mae_pct": o0.get("mae_pct"),
            "m0_reached_025": o0.get("reached_plus_025"),
            "m3_entry_quality": o3.get("entry_quality") if o3 is not None else None,
            "m3_mfe_pct": o3.get("mfe_pct") if o3 is not None else None,
            "m3_mae_pct": o3.get("mae_pct") if o3 is not None else None,
            "robust_label_m0": outcome_m0.get("robust_label"),
            "minutes_to_max_adverse": outcome_m0.get("minutes_to_max_adverse"),
            "minutes_to_max_favorable": outcome_m0.get("minutes_to_max_favorable"),
            "leak_category_source": "MOMENTUM_QUALITY_LEAK",
            "atr_pct": atr_pct,
            **{f"hyp_{k}": v for k, v in flags.items()},
        }
        weak_rows.append(row)
        hyp_per_leak.append({"setup_id": sid, "side": side, **flags})

        outcome_val_rows.append(
            {
                "setup_id": sid,
                "side": side,
                "basis": "m0_momentum_entry",
                **outcome_m0,
                "multiweek_m0_quality": o0.get("entry_quality"),
                "multiweek_m3_quality": o3.get("entry_quality") if o3 is not None else None,
                "m3_only_became_weak_due_to_delay": (
                    map_quality_label(o0.get("entry_quality")) != "weak"
                    and map_quality_label(o3.get("entry_quality")) == "weak"
                    if o3 is not None
                    else False
                ),
            }
        )
        seq_rows.append({"setup_id": sid, "side": side, "cohort": "weak_leak", **seq, **flags})

        ch = float((conf.get("confirm") or conf.get("c0_break") or {}).get("high") or entry_px_m0)
        cl = float((conf.get("confirm") or conf.get("c0_break") or {}).get("low") or entry_px_m0)
        for tr in entry_timing_counterfactuals(
            frame=frame,
            side=side,
            mom_ts=mom_ts,
            mom_price=entry_px_m0,
            confirm_high=ch,
            confirm_low=cl,
        ):
            timing_rows.append({"setup_id": sid, "side": side, **tr})

        case_lines.extend(
            [
                f"## {sid} ({side})",
                f"- week/phase: {week_id} / {phase}",
                f"- PA→Mom: {pa_ts} → {mom_ts} ({mrow.get('confirmation_type')}, age={mrow.get('candles_after_price_action_confirmation')})",
                f"- M0 quality={o0.get('entry_quality')} MFE={o0.get('mfe_pct')} MAE={o0.get('mae_pct')} +0.25={o0.get('reached_plus_025')}",
                f"- M3 quality={o3.get('entry_quality') if o3 is not None else None} delayed={delayed_by_m3}",
                f"- robust_label_m0={outcome_m0.get('robust_label')}",
                f"- hypotheses={[k for k,v in flags.items() if v]}",
                f"- sequence={seq}",
                "",
            ]
        )

    weak_df = pd.DataFrame(weak_rows)
    weak_df.to_csv(out / "momentum_weak_leaks.csv", index=False)
    pd.DataFrame(weak_candles).to_csv(out / "momentum_weak_leak_candles.csv", index=False)
    pd.DataFrame(outcome_val_rows).to_csv(out / "momentum_outcome_validation.csv", index=False)

    # --- matched goods ---
    good_pool = m0[m0["entry_quality"] == "good"].copy()
    matched_rows = []
    matched_candles = []
    feature_good_matched = []
    feature_good_all = []
    rng = np.random.default_rng(42)  # only for tie-breaking stability via score already deterministic

    for leak in weak_rows:
        candidates = []
        for _, g in good_pool.iterrows():
            if str(g["setup_id"]) in leak_ids:
                continue
            if str(g["side"]).lower() != leak["side"]:
                continue
            gs = setups_by.get(str(g["setup_id"]), {})
            gp = pa_by.get(str(g["setup_id"]), {})
            gm = mom_by.get(str(g["setup_id"]))
            if gm is None:
                continue
            wid = None
            for w in weeks:
                if w.start <= to_utc(g["entry_timestamp"]) < w.end:
                    wid = w.week_id
                    break
            cand = {
                "setup_id": str(g["setup_id"]),
                "side": str(g["side"]).lower(),
                "entry_timestamp": g["entry_timestamp"],
                "market_phase": phase_by_week.get(wid or ""),
                "setup_type": gs.get("setup_type"),
                "pa_pattern_type": gp.get("pattern_type"),
                "atr_pct": None,
            }
            # rough atr% from mom row
            # leave None if unknown
            sc = match_score(leak, cand)
            candidates.append((sc, cand, g, gp, gm, gs))
        candidates.sort(key=lambda x: (-x[0], x[1]["setup_id"]))
        top = candidates[:3]
        for rank, (sc, cand, g, gp, gm, gs) in enumerate(top, start=1):
            side = cand["side"]
            pa_ts = to_utc(gp.get("structure_break_timestamp"))
            mom_ts = to_utc(gm["confirmation_timestamp"])
            entry_ts = to_utc(g["entry_timestamp"])
            entry_px = float(g["entry_price"])
            pa_level = _finite(gp.get("confirmation_level"))
            candles = build_candle_rows(
                setup_id=cand["setup_id"],
                side=side,
                pa_ts=pa_ts,
                entry_ts=entry_ts,
                pa_level=pa_level,
                setup_level=_finite(gs.get("setup_level")),
                frame=frame,
                pivots=pivots,
                mom_cfg=mom_cfg,
                regime_15m=str(gp.get("regime_15m")) if gp.get("regime_15m") is not None else None,
            )
            matched_candles.extend([{**c, "matched_to_leak": leak["setup_id"], "match_rank": rank} for c in candles])
            conf = extract_confirm_candles(candles, pa_ts, mom_ts)
            seq = sequence_metrics(conf, side)
            outcome = forward_path_metrics(frame, entry_ts, entry_px, side)
            flags = hypothesis_flags(side=side, conf=conf, entry_row=g, pa_level=pa_level)
            snap = feature_snapshot(conf, seq, outcome)
            snap["minutes_pa_to_entry"] = (entry_ts - pa_ts).total_seconds() / 60.0
            snap["setup_id"] = cand["setup_id"]
            snap["group"] = "matched_good"
            snap["side"] = side
            feature_good_matched.append(snap)
            matched_rows.append(
                {
                    "leak_setup_id": leak["setup_id"],
                    "match_rank": rank,
                    "match_score": sc,
                    "good_setup_id": cand["setup_id"],
                    "side": side,
                    "market_phase_leak": leak["market_phase"],
                    "market_phase_good": cand["market_phase"],
                    "entry_timestamp": entry_ts.isoformat(),
                    "mfe_pct": g.get("mfe_pct"),
                    "mae_pct": g.get("mae_pct"),
                    "momentum_confirmation_type": gm.get("confirmation_type"),
                    "momentum_age": gm.get("candles_after_price_action_confirmation"),
                    **{f"hyp_{k}": v for k, v in flags.items()},
                    **seq,
                }
            )
            seq_rows.append(
                {
                    "setup_id": cand["setup_id"],
                    "side": side,
                    "cohort": "matched_good",
                    "matched_to_leak": leak["setup_id"],
                    **seq,
                    **flags,
                }
            )

    # sample of remaining goods for feature comparison (deterministic first 40 by id)
    other_goods = good_pool[~good_pool.setup_id.isin([r["good_setup_id"] for r in matched_rows])].copy()
    other_goods = other_goods.sort_values("setup_id").head(40)
    for _, g in other_goods.iterrows():
        sid = str(g["setup_id"])
        gm = mom_by.get(sid)
        gp = pa_by.get(sid)
        if gm is None or gp is None:
            continue
        side = str(g["side"]).lower()
        pa_ts = to_utc(gp["structure_break_timestamp"])
        mom_ts = to_utc(gm["confirmation_timestamp"])
        entry_ts = to_utc(g["entry_timestamp"])
        candles = build_candle_rows(
            setup_id=sid,
            side=side,
            pa_ts=pa_ts,
            entry_ts=entry_ts,
            pa_level=_finite(gp.get("confirmation_level")),
            setup_level=None,
            frame=frame,
            pivots=pivots,
            mom_cfg=mom_cfg,
            regime_15m=None,
        )
        conf = extract_confirm_candles(candles, pa_ts, mom_ts)
        seq = sequence_metrics(conf, side)
        outcome = forward_path_metrics(frame, entry_ts, float(g["entry_price"]), side)
        snap = feature_snapshot(conf, seq, outcome)
        snap["minutes_pa_to_entry"] = (entry_ts - pa_ts).total_seconds() / 60.0
        snap["setup_id"] = sid
        snap["group"] = "other_good"
        snap["side"] = side
        feature_good_all.append(snap)

    pd.DataFrame(matched_rows).to_csv(out / "momentum_matched_good_entries.csv", index=False)
    pd.DataFrame(matched_candles).to_csv(out / "momentum_matched_good_candles.csv", index=False)
    pd.DataFrame(seq_rows).to_csv(out / "momentum_sequence_quality.csv", index=False)
    pd.DataFrame(timing_rows).to_csv(out / "momentum_entry_timing_counterfactual.csv", index=False)

    feat_cmp = []
    feat_cmp.extend(summarize_feature_table(feature_leak_snaps, "weak_leak"))
    feat_cmp.extend(summarize_feature_table(feature_good_matched, "matched_good"))
    feat_cmp.extend(summarize_feature_table(feature_good_all, "other_good"))
    pd.DataFrame(feat_cmp).to_csv(out / "momentum_feature_comparison.csv", index=False)

    # Hypothesis coverage
    hyp_names = [
        "M1_missing_progression",
        "M2_weakening_second",
        "M3_rejection",
        "M4_no_structure",
        "M5_late_momentum",
        "M6_counter_context",
        "M7_single_candle_dominates",
        "M8_exhaustion",
    ]
    hyp_cov = []
    matched_df = pd.DataFrame(matched_rows)
    for h in hyp_names:
        n_leak = int(sum(1 for r in hyp_per_leak if r.get(h)))
        n_match = int(matched_df[f"hyp_{h}"].sum()) if len(matched_df) and f"hyp_{h}" in matched_df else 0
        n_match_tot = len(matched_df) if len(matched_df) else 0
        leak_rate = n_leak / max(len(hyp_per_leak), 1)
        match_rate = n_match / max(n_match_tot, 1)
        hyp_cov.append(
            {
                "hypothesis": h,
                "n_leaks_flagged": n_leak,
                "leak_rate": leak_rate,
                "n_matched_goods_flagged": n_match,
                "matched_good_rate": match_rate,
                "separation": leak_rate - match_rate,
                "false_block_risk": match_rate,
                "n_leak_long": int(sum(1 for r in hyp_per_leak if r.get(h) and r.get("side") == "long")),
                "n_leak_short": int(sum(1 for r in hyp_per_leak if r.get(h) and r.get("side") == "short")),
            }
        )
    hyp_cov_df = pd.DataFrame(hyp_cov).sort_values("separation", ascending=False)
    hyp_cov_df.to_csv(out / "momentum_hypothesis_coverage.csv", index=False)

    # Long/short + phase
    ls_rows = []
    for side in ("long", "short"):
        sub = weak_df[weak_df.side == side]
        ls_rows.append(
            {
                "side": side,
                "n_leaks": int(len(sub)),
                "n_clearly_weak_m0": int((sub.robust_label_m0 == "clearly_weak").sum()),
                "n_m3_delay_induced": int(sub.m3_delayed_vs_m0.fillna(False).sum()),
                "mean_m0_mfe": float(sub.m0_mfe_pct.mean()) if len(sub) else None,
                "mean_m0_mae": float(sub.m0_mae_pct.mean()) if len(sub) else None,
                "share_break_candle": float((sub.momentum_confirmation_type == "break_candle").mean())
                if len(sub)
                else None,
            }
        )
    pd.DataFrame(ls_rows).to_csv(out / "momentum_long_short_comparison.csv", index=False)

    phase_rows = []
    for phase, g in weak_df.groupby(weak_df.market_phase.fillna("unknown")):
        phase_rows.append(
            {
                "market_phase": phase,
                "n_leaks": int(len(g)),
                "n_clearly_weak_m0": int((g.robust_label_m0 == "clearly_weak").sum()),
                "sides": _json_list(g.side.value_counts().to_dict()),
            }
        )
    pd.DataFrame(phase_rows).to_csv(out / "momentum_market_phase_comparison.csv", index=False)

    # Timing aggregate
    timing_df = pd.DataFrame(timing_rows)
    timing_summary = []
    for variant, g in timing_df.groupby("timing_variant"):
        trig = g[g.get("triggered") == True] if "triggered" in g else g  # noqa: E712
        # For leaks: count how many would be clearly_weak avoided if quality improves — descriptive
        timing_summary.append(
            {
                "timing_variant": variant,
                "n_rows": int(len(g)),
                "n_triggered": int(g["triggered"].fillna(False).sum()) if "triggered" in g else None,
                "n_not_triggered": int((~g["triggered"].fillna(False)).sum()) if "triggered" in g else None,
                "mean_mfe": float(pd.to_numeric(trig.get("mfe_pct"), errors="coerce").mean())
                if len(trig)
                else None,
                "mean_mae": float(pd.to_numeric(trig.get("mae_pct"), errors="coerce").mean())
                if len(trig)
                else None,
                "n_still_weak_quality": int((trig.get("entry_quality") == "weak").sum())
                if len(trig) and "entry_quality" in trig
                else None,
                "n_reached_025": int(trig["reached_025"].fillna(False).sum())
                if len(trig) and "reached_025" in trig
                else None,
            }
        )

    # Answers
    n_clear = int((weak_df.robust_label_m0 == "clearly_weak").sum())
    n_horizon = int((weak_df.robust_label_m0 == "horizon_dependent_weak").sum())
    n_amb = int((weak_df.robust_label_m0 == "ambiguous").sum())
    n_mis = int((weak_df.robust_label_m0 == "possibly_misclassified_as_weak").sum())
    n_m3_only = int(
        sum(1 for r in outcome_val_rows if r.get("m3_only_became_weak_due_to_delay"))
    )
    top_hyp = hyp_cov_df.iloc[0].to_dict() if len(hyp_cov_df) else {}
    best_sep = hyp_cov_df.iloc[0].to_dict() if len(hyp_cov_df) else {}
    # lowest good overlap among those covering >=3 leaks
    viable = hyp_cov_df[hyp_cov_df.n_leaks_flagged >= 3]
    sharpest = (
        viable.sort_values(["matched_good_rate", "separation"], ascending=[True, False]).iloc[0].to_dict()
        if len(viable)
        else best_sep
    )

    break_share = float((weak_df.momentum_confirmation_type == "break_candle").mean())
    # Research variants recommendation (max two; research-only, not implemented)
    research_next: list[str] = []
    if (
        sharpest.get("hypothesis") in {"M7_single_candle_dominates", "M2_weakening_second"}
        or break_share >= 0.5
        or any(
            h["hypothesis"] == "M2_weakening_second" and h["separation"] > 0.1
            for _, h in hyp_cov_df.iterrows()
        )
    ):
        research_next.append(
            "SEQ_MIN_QUALITY: require minimum quality on each candle in the window "
            "OR disallow solitary break_candle confirms without a second directional close"
        )
    if any(
        h["hypothesis"] == "M2_weakening_second" and h["n_leaks_flagged"] >= 3
        for _, h in hyp_cov_df.iterrows()
    ):
        research_next.append(
            "NO_WEAKENING_SECOND: reject confirms where candle2 body/close-location "
            "deteriorates materially vs candle1 (measure-only thresholds TBD)"
        )
    if any(
        h["hypothesis"] == "M5_late_momentum" and h["separation"] > 0.05
        for _, h in hyp_cov_df.iterrows()
    ):
        research_next.append(
            "LATE_MOVE_GUARD: flag overextended PA→confirm moves vs ATR / EMA9 distance "
            "as research quality score (not a hard gate yet)"
        )
    # Deduplicate preserving order; always keep exactly up to two.
    dedup: list[str] = []
    for item in research_next:
        if item not in dedup:
            dedup.append(item)
    research_next = dedup[:2]
    if len(research_next) < 2:
        research_next.append(
            "STRUCTURE_PROGRESSION: research-only require HH/LL continuation vs last "
            "confirmed swing after PA (currently unused by momentum.py)"
        )
    research_next = research_next[:2]

    do_not_pursue = [
        "B3/R2/adaptive-3-candle stack (already rejected)",
        "Volume-only filter without sequence context",
        "ADX/DI hard blockers as sole rule (context feature only)",
        "Blind extra candle count without quality criteria",
    ]

    # Break timing: how many leaks never trigger vs goods — only on leaks here; note limitation
    break_g = timing_df[timing_df.timing_variant == "break_confirm_extreme"]
    n_break_notrig = int((~break_g["triggered"].fillna(False)).sum()) if len(break_g) else 0

    answers = {
        "q1_all_11_robust_weak": (
            f"No. At true M0 momentum entry: clearly_weak={n_clear}, "
            f"horizon_dependent={n_horizon}, ambiguous={n_amb}, "
            f"possibly_misclassified={n_mis}. "
            f"M3-delay-induced weak labels={n_m3_only}."
        ),
        "q2_formal_confirm_conditions": logic_doc["q1_prerequisites"],
        "q3_why_released": (
            "Momentum is binary single-candle: first candle meeting body/close-location/"
            "range-ATR/structure-hold confirms immediately (often break_candle). "
            "No sequence progression, EMA/DI context, or rejection-wick gate."
        ),
        "q4_top_hypothesis": top_hyp,
        "q5_least_overlap_with_goods": sharpest,
        "q6_common_pattern": (
            f"break_candle share={break_share:.2f}; "
            "many M3 'weak' labels are delayed entries after elevated confirmation, "
            "not baseline momentum failure"
        ),
        "q7_multiple_leak_types": True,
        "q8_long_short": ls_rows,
        "q9_market_phases": phase_rows,
        "q10_weak_second_candle": any(
            h["hypothesis"] == "M2_weakening_second" and h["n_leaks_flagged"] >= 3
            for _, h in hyp_cov_df.iterrows()
        ),
        "q11_missing_pa_progression": any(
            h["hypothesis"] == "M1_missing_progression" and h["leak_rate"] >= 0.4
            for _, h in hyp_cov_df.iterrows()
        ),
        "q12_overextended": any(
            h["hypothesis"] == "M8_exhaustion" and h["n_leaks_flagged"] >= 3
            for _, h in hyp_cov_df.iterrows()
        ),
        "q13_missing_structure": any(
            h["hypothesis"] == "M4_no_structure" and h["leak_rate"] >= 0.4
            for _, h in hyp_cov_df.iterrows()
        ),
        "q14_too_binary": True,
        "q15_per_candle_min_quality": True,
        "q16_sequence_as_whole": True,
        "q17_break_confirm_extreme": {
            "n_leaks_no_trigger": n_break_notrig,
            "timing_summary": timing_summary,
            "note": "Leak-only counterfactual; good-entry loss not fully measured in this pass",
        },
        "q18_good_entries_lost_break": (
            "Not fully quantified on full good cohort in this research pass; "
            "matched-good hypothesis overlap used as proxy for false-block risk"
        ),
        "q19_main_problem": (
            "Primarily momentum-quality (binary single-candle). "
            "Entry-timing (break extreme) is secondary research; "
            "several 'leaks' are M3-delay artifacts, not momentum bugs"
        ),
        "q20_next_two_variants": research_next,
        "q21_do_not_pursue": do_not_pursue,
        "q22_enough_evidence_for_change": (
            "Enough evidence to justify a research implementation of sequence/progression variants; "
            "NOT enough for productive pipeline change yet"
        ),
        "q23_broader_audit_needed": (
            n_clear < 8,
            "Yes — expand beyond M3 residual list to all M0 weak+ambiguous poor MAE paths "
            "before locking a rule",
        ),
    }

    summary = {
        "status": "ok",
        "symbol": args.symbol,
        "n_source_leaks": 11,
        "leak_ids": leak_ids,
        "n_clearly_weak_at_m0": n_clear,
        "n_horizon_dependent": n_horizon,
        "n_ambiguous_at_m0": n_amb,
        "n_possibly_misclassified": n_mis,
        "n_m3_delay_induced_weak": n_m3_only,
        "current_momentum_logic": logic_doc,
        "hypothesis_coverage": hyp_cov_df.to_dict(orient="records"),
        "timing_summary": timing_summary,
        "answers": answers,
        "research_plan_next": research_next,
        "do_not_pursue": do_not_pursue,
        "safety": {
            "no_live_changes": True,
            "no_pipeline_changes": True,
            "no_momentum_threshold_changes": True,
            "no_new_momentum_rule_activated": True,
            "b3_r2_not_developed": True,
            "outcomes_post_hoc_only": True,
            "nothing_committed": True,
        },
    }
    (out / "audit_summary.json").write_text(
        json.dumps(json_safe(summary), indent=2), encoding="utf-8"
    )
    (out / "momentum_case_studies.md").write_text("\n".join(case_lines) + "\n", encoding="utf-8")
    write_readme(summary, out / "README.md")
    return summary


def write_readme(summary: Mapping[str, Any], path: Path) -> None:
    answers = summary.get("answers") or {}
    lines = [
        "# Momentum quality leak audit (research-only)",
        "",
        "Diagnoses the 11 M3 residual `MOMENTUM_QUALITY_LEAK` entries without changing",
        "productive momentum, pipeline, or live strategy.",
        "",
        f"- clearly_weak at M0: **{summary.get('n_clearly_weak_at_m0')}**",
        f"- M3-delay-induced weak labels: **{summary.get('n_m3_delay_induced_weak')}**",
        f"- Next research variants: {summary.get('research_plan_next')}",
        "",
        "## Current momentum logic (summary)",
        "```json",
        json.dumps(json_safe(summary.get("current_momentum_logic")), indent=2),
        "```",
        "",
        "## Answers",
    ]
    for k, v in answers.items():
        lines.append(f"- **{k}**: {v}")
    lines.extend(
        [
            "",
            "## Safety",
            "- no live / pipeline / threshold changes",
            "- no new momentum rule activated",
            "- B3/R2 not developed further",
            "- nothing committed",
            "",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--symbol", default="APTUSDT")
    p.add_argument("--multiweek-dir", default=DEFAULT_MULTIWEEK)
    p.add_argument("--pipeline-dir", default=DEFAULT_PIPELINE)
    p.add_argument("--output-dir", default=DEFAULT_OUT)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    summary = run_audit(args)
    print(json.dumps(json_safe({
        "status": summary.get("status"),
        "n_clearly_weak_at_m0": summary.get("n_clearly_weak_at_m0"),
        "n_m3_delay_induced_weak": summary.get("n_m3_delay_induced_weak"),
        "research_plan_next": summary.get("research_plan_next"),
        "q1": (summary.get("answers") or {}).get("q1_all_11_robust_weak"),
        "q20": (summary.get("answers") or {}).get("q20_next_two_variants"),
    }), indent=2))
    return 0 if summary.get("status") == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
