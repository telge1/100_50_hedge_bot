"""Parametric short outcome evaluation reusing first_touch / path_arrays semantics."""

from __future__ import annotations

from typing import Any

import numpy as np

from research.regime_scanner.pullback_entry_c3_5c_fill_excursion_audit import (
    first_touch_level,
    path_arrays,
    signed_return_pct,
)


def evaluate_outcome_params(
    *,
    side: int,
    entry: float,
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    timestamps: list[Any],
    fill_i: int,
    n_bars: int,
    tp_pct: float,
    sl_pct: float,
    horizon_bars: int,
    cost_pct: float,
) -> dict[str, Any]:
    """Same logic as ``evaluate_outcome_on_fill`` with explicit TP/SL/horizon/cost.

    ``sl_pct`` must be negative for short adverse (e.g. -0.50).
    ``tp_pct`` positive favorable magnitude (e.g. 0.50).
    Same-bar TP+SL → conservative SL.
    """
    end_h = min(n_bars - 1, fill_i + int(horizon_bars) - 1)
    truncated = end_h < fill_i + int(horizon_bars) - 1
    tp_t = first_touch_level(side, entry, highs, lows, fill_i, end_h, float(tp_pct))
    sl_t = first_touch_level(side, entry, highs, lows, fill_i, end_h, float(sl_pct))
    tp_b, sl_b = tp_t["bar_offset"], sl_t["bar_offset"]
    same_bar = bool(tp_t["reached"] and sl_t["reached"] and tp_b == sl_b)
    tp_first = bool(
        tp_t["reached"] and (not sl_t["reached"] or (tp_b is not None and sl_b is not None and tp_b < sl_b))
    )
    sl_first = bool(
        sl_t["reached"] and (not tp_t["reached"] or (tp_b is not None and sl_b is not None and sl_b < tp_b))
    )
    if tp_t["reached"] and sl_t["reached"]:
        if tp_b < sl_b:
            reason, gross, exit_bar = "TP", float(tp_pct), fill_i + int(tp_b)
        elif sl_b < tp_b:
            reason, gross, exit_bar = "SL", float(sl_pct), fill_i + int(sl_b)
        else:
            reason, gross, exit_bar = "same_bar_conservative_sl", float(sl_pct), fill_i + int(sl_b)
            sl_first = True
            tp_first = False
    elif tp_t["reached"]:
        reason, gross, exit_bar = "TP", float(tp_pct), fill_i + int(tp_b)
    elif sl_t["reached"]:
        reason, gross, exit_bar = "SL", float(sl_pct), fill_i + int(sl_b)
    else:
        exit_bar = end_h
        gross = float(signed_return_pct(side, entry, float(closes[exit_bar])))
        reason = "data_end" if truncated else "time_exit"

    path = path_arrays(side, entry, highs, lows, closes, fill_i, exit_bar)
    exit_px = float(closes[exit_bar])
    if reason == "TP":
        exit_px = entry * (1 + tp_pct / 100.0) if side > 0 else entry * (1 - tp_pct / 100.0)
    elif reason in ("SL", "same_bar_conservative_sl"):
        exit_px = entry * (1 + sl_pct / 100.0) if side > 0 else entry * (1 - sl_pct / 100.0)
    net = float(gross) - float(cost_pct)

    # first favorable / adverse touch within horizon (any small level proxy via path)
    fav = path.get("fav")
    adv = path.get("adv")
    first_fav = None
    first_adv = None
    if fav is not None:
        for i, v in enumerate(fav):
            if float(v) > 1e-12:
                first_fav = int(i)
                break
    if adv is not None:
        for i, v in enumerate(adv):
            if float(v) < -1e-12:
                first_adv = int(i)
                break

    return {
        "exit_timestamp": timestamps[exit_bar],
        "exit_price": exit_px,
        "exit_reason": reason,
        "gross_pnl_pct": float(gross),
        "net_pnl_pct": net,
        "is_winner": net > 0,
        "tp_first": tp_first,
        "sl_first": sl_first or reason in ("SL", "same_bar_conservative_sl"),
        "same_bar_ambiguous": same_bar,
        "time_exit": reason == "time_exit",
        "data_end": reason == "data_end",
        "bars_held": int(exit_bar - fill_i),
        "bars_to_tp": int(tp_b) if tp_t["reached"] else None,
        "bars_to_sl": int(sl_b) if sl_t["reached"] else None,
        "mfe_pct": path.get("maximum_favorable_excursion_pct"),
        "mae_pct": path.get("maximum_adverse_excursion_pct"),
        "first_favorable_touch": first_fav,
        "first_adverse_touch": first_adv,
        "tp_reached": bool(tp_t["reached"]),
        "sl_reached": bool(sl_t["reached"]),
    }


def short_tp_sl_prices(entry: float, tp_pct: float, sl_mag: float) -> tuple[float, float]:
    """Short: TP below entry, SL above entry."""
    tp_price = entry * (1.0 - float(tp_pct) / 100.0)
    sl_price = entry * (1.0 + float(sl_mag) / 100.0)
    return tp_price, sl_price
