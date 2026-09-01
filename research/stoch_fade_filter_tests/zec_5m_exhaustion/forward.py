"""Forward 1m path at fixed horizons. Outcome is never rewritten from later prices."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from .config import HORIZONS_MIN


def aligned_return_pct(direction: str, entry: float, price: float) -> float | None:
    if entry == 0 or not np.isfinite(entry) or not np.isfinite(price):
        return None
    if str(direction).upper() == "LONG":
        return (price / entry - 1.0) * 100.0
    return ((entry - price) / entry) * 100.0


def raw_return_pct(entry: float, price: float) -> float | None:
    if entry == 0 or not np.isfinite(entry) or not np.isfinite(price):
        return None
    return (price / entry - 1.0) * 100.0


def mfe_mae_pct(direction: str, entry: float, high: np.ndarray, low: np.ndarray) -> tuple[float | None, float | None]:
    if high.size == 0:
        return None, None
    if str(direction).upper() == "LONG":
        mfe = (float(np.max(high)) - entry) / entry * 100.0
        mae = (entry - float(np.min(low))) / entry * 100.0
    else:
        mfe = (entry - float(np.min(low))) / entry * 100.0
        mae = (float(np.max(high)) - entry) / entry * 100.0
    return mfe, mae


def first_barrier(
    *,
    direction: str,
    high: np.ndarray,
    low: np.ndarray,
    tp: float,
    sl: float,
) -> tuple[str | None, int | None]:
    if high.size == 0:
        return None, None
    side = str(direction).upper()
    if side == "LONG":
        hit_tp = high >= tp
        hit_sl = low <= sl
    else:
        hit_tp = low <= tp
        hit_sl = high >= sl
    any_tp = bool(np.any(hit_tp))
    any_sl = bool(np.any(hit_sl))
    i_tp = int(np.argmax(hit_tp)) if any_tp else -1
    i_sl = int(np.argmax(hit_sl)) if any_sl else -1
    if not any_tp and not any_sl:
        return None, None
    if not any_tp or (any_sl and i_sl <= i_tp):
        return "SL", i_sl
    return "TP", i_tp


def _frac(move_pct: float | None, entry: float, distance: float) -> float | None:
    if move_pct is None or not np.isfinite(distance) or distance == 0:
        return None
    return (move_pct / 100.0 * entry) / distance


def summarize_bars(
    *,
    direction: str,
    entry_price: float,
    tp_price: float,
    sl_price: float,
    high: np.ndarray,
    low: np.ndarray,
    price_at_end: float | None,
) -> dict[str, Any]:
    mfe, mae = mfe_mae_pct(direction, entry_price, high, low)
    touch, touch_i = first_barrier(
        direction=direction, high=high, low=low, tp=tp_price, sl=sl_price
    )
    tp_dist = abs(float(tp_price) - float(entry_price))
    sl_dist = abs(float(sl_price) - float(entry_price))
    return {
        "price": price_at_end,
        "raw_return_pct": None if price_at_end is None else raw_return_pct(entry_price, price_at_end),
        "aligned_return_pct": None if price_at_end is None else aligned_return_pct(direction, entry_price, price_at_end),
        "mfe_pct": mfe,
        "mae_pct": mae,
        "mfe_frac_tp": _frac(mfe, entry_price, tp_dist),
        "mae_frac_sl": _frac(mae, entry_price, sl_dist),
        "tp_touched": touch == "TP",
        "sl_touched": touch == "SL",
        "first_touch": touch,
        "first_touch_bar_offset": touch_i,
    }


def trade_forward_paths(
    *,
    direction: str,
    entry_price: float,
    tp_price: float,
    sl_price: float,
    entry_time: np.datetime64,
    exit_time: np.datetime64 | None,
    exit_reason: str | None,
    times: np.ndarray,
    open_: np.ndarray,
    high: np.ndarray,
    low: np.ndarray,
) -> dict[str, Any]:
    out: dict[str, Any] = {"entry_bar_missing": False, "original_exit_reason": exit_reason}
    entry_i = int(np.searchsorted(times, entry_time, side="left"))
    if entry_i >= times.size or times[entry_i] != entry_time:
        for name in HORIZONS_MIN:
            out[f"{name}_status"] = "HORIZON_UNAVAILABLE"
        out["entry_bar_missing"] = True
        return out

    exit_i: int | None = None
    if exit_time is not None and not pd.isna(exit_time):
        cand = int(np.searchsorted(times, exit_time, side="left"))
        if cand < times.size and times[cand] == exit_time:
            exit_i = cand

    for name, minutes in HORIZONS_MIN.items():
        horizon_i = entry_i + minutes
        expected = times[entry_i] + np.timedelta64(minutes, "m")
        if horizon_i >= times.size or times[horizon_i] != expected:
            out[f"{name}_status"] = "HORIZON_UNAVAILABLE"
            continue
        still_open = exit_i is None or exit_i > horizon_i
        if still_open:
            in_end = horizon_i
        else:
            assert exit_i is not None
            in_end = min(horizon_i, exit_i + 1)
        in_trade = summarize_bars(
            direction=direction,
            entry_price=entry_price,
            tp_price=tp_price,
            sl_price=sl_price,
            high=high[entry_i:in_end],
            low=low[entry_i:in_end],
            price_at_end=float(open_[horizon_i]) if still_open else None,
        )
        market = summarize_bars(
            direction=direction,
            entry_price=entry_price,
            tp_price=tp_price,
            sl_price=sl_price,
            high=high[entry_i:horizon_i],
            low=low[entry_i:horizon_i],
            price_at_end=float(open_[horizon_i]),
        )
        post_available = (not still_open) and exit_i is not None and (exit_i + 1) <= horizon_i
        if post_available:
            post = summarize_bars(
                direction=direction,
                entry_price=entry_price,
                tp_price=tp_price,
                sl_price=sl_price,
                high=high[exit_i + 1 : horizon_i],
                low=low[exit_i + 1 : horizon_i],
                price_at_end=float(open_[horizon_i]),
            )
        else:
            post = {}
        aligned = market["aligned_return_pct"]
        out[f"{name}_status"] = "OK"
        out[f"{name}_still_open"] = bool(still_open)
        out[f"{name}_horizon_time"] = str(np.datetime_as_string(times[horizon_i], timezone="UTC")).replace("+00:00", "Z")
        out[f"{name}_price"] = market["price"]
        out[f"{name}_raw_return_pct"] = market["raw_return_pct"]
        out[f"{name}_aligned_return_pct"] = aligned
        out[f"{name}_in_trade_mfe_pct"] = in_trade["mfe_pct"]
        out[f"{name}_in_trade_mae_pct"] = in_trade["mae_pct"]
        out[f"{name}_in_trade_mfe_frac_tp"] = in_trade["mfe_frac_tp"]
        out[f"{name}_in_trade_mae_frac_sl"] = in_trade["mae_frac_sl"]
        out[f"{name}_in_trade_tp_touched"] = in_trade["tp_touched"]
        out[f"{name}_in_trade_sl_touched"] = in_trade["sl_touched"]
        out[f"{name}_in_trade_first_touch"] = in_trade["first_touch"]
        out[f"{name}_market_mfe_pct"] = market["mfe_pct"]
        out[f"{name}_market_mae_pct"] = market["mae_pct"]
        out[f"{name}_market_mfe_frac_tp"] = market["mfe_frac_tp"]
        out[f"{name}_market_mae_frac_sl"] = market["mae_frac_sl"]
        out[f"{name}_market_tp_touched"] = market["tp_touched"]
        out[f"{name}_market_sl_touched"] = market["sl_touched"]
        out[f"{name}_market_first_touch"] = market["first_touch"]
        out[f"{name}_post_exit_available"] = bool(post_available)
        out[f"{name}_post_exit_aligned_from_entry_pct"] = market["aligned_return_pct"] if post_available else None
        out[f"{name}_post_exit_aligned_return_pct"] = post.get("aligned_return_pct")
        out[f"{name}_in_direction"] = None if aligned is None else bool(aligned > 0)
    return out
