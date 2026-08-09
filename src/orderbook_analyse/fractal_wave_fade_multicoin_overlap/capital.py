"""Capital-normalized comparisons M1/M2/M3."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from orderbook_analyse.fractal_wave_fade_multicoin_overlap.intervals import (
    active_intervals,
    intersect_duration,
    trade_intervals,
)


def _span_days(start: pd.Timestamp, end: pd.Timestamp) -> float:
    return (pd.Timestamp(end) - pd.Timestamp(start)).total_seconds() / 86400.0


def _max_dd_additive(nets: np.ndarray) -> float:
    if len(nets) == 0:
        return 0.0
    eq = np.cumsum(nets)
    peak = np.maximum.accumulate(eq)
    return float((eq - peak).min())


def capital_time_units(trades: pd.DataFrame, weight_col: str | None = None) -> float:
    """Sum of (holding_hours * capital_weight). Default weight=1."""
    if trades.empty:
        return 0.0
    hold_h = (trades["exit_time"] - trades["entry_time"]).dt.total_seconds() / 3600.0
    w = trades[weight_col].astype(float) if weight_col else 1.0
    return float((hold_h * w).sum())


def m1_single(trades: pd.DataFrame, span_start, span_end, *, model: str = "M1_SINGLE") -> dict[str, Any]:
    nets = trades["net_return_pct"].astype(float).to_numpy() if len(trades) else np.array([])
    days = _span_days(span_start, span_end)
    ct = capital_time_units(trades)
    pnl = float(nets.sum()) if len(nets) else 0.0
    return {
        "model": model,
        "total_capital_base": 1.0,
        "executed_trades": int(len(trades)),
        "net_return_additive": pnl,
        "pnl_per_day": pnl / days if days else None,
        "capital_time_hours": ct,
        "pnl_per_capital_hour": pnl / ct if ct else None,
        "max_concurrent_capital": 1.0,
        "avg_capital_deployed": None,
        "max_dd_additive": _max_dd_additive(nets),
    }


def m2_shared(executed: pd.DataFrame, span_start, span_end) -> dict[str, Any]:
    nets = executed["net_return_pct"].astype(float).to_numpy() if len(executed) else np.array([])
    days = _span_days(span_start, span_end)
    ct = capital_time_units(executed)
    pnl = float(nets.sum()) if len(nets) else 0.0
    return {
        "model": "M2_SHARED_SLOT",
        "total_capital_base": 1.0,
        "executed_trades": int(len(executed)),
        "net_return_additive": pnl,
        "pnl_per_day": pnl / days if days else None,
        "capital_time_hours": ct,
        "pnl_per_capital_hour": pnl / ct if ct else None,
        "max_concurrent_capital": 1.0,
        "max_dd_additive": _max_dd_additive(nets),
    }


def m3_parallel_scaled(
    independent: pd.DataFrame,
    *,
    span_start,
    span_end,
) -> dict[str, Any]:
    """
    Both streams free, total capital=1.
    When alone: weight=1.0; when overlapping other symbol: weight=0.5.
    Scaled net = net_return_pct * weight.
    """
    df = independent.sort_values(["entry_time", "trade_id"]).reset_index(drop=True)
    if df.empty:
        return {
            "model": "M3_PARALLEL_50_50",
            "total_capital_base": 1.0,
            "executed_trades": 0,
            "net_return_additive": 0.0,
            "pnl_per_day": 0.0,
            "capital_time_hours": 0.0,
            "pnl_per_capital_hour": None,
            "max_concurrent_capital": 0.0,
            "max_dd_additive": 0.0,
            "unscaled_net_return": 0.0,
        }

    apt_iv = active_intervals(df[df["symbol"] == "APTUSDT"])
    doge_iv = active_intervals(df[df["symbol"] == "DOGEUSDT"])

    weights = []
    scaled = []
    for _, tr in df.iterrows():
        a = int(pd.Timestamp(tr["entry_time"]).tz_convert("UTC").value)
        b = int(pd.Timestamp(tr["exit_time"]).tz_convert("UTC").value)
        other = doge_iv if tr["symbol"] == "APTUSDT" else apt_iv
        overlap = intersect_duration([(a, b)], other)
        hold = b - a
        # fraction of this trade's life overlapping the other symbol
        frac_overlap = overlap / hold if hold > 0 else 0.0
        # capital weight: 1 when alone, 0.5 when overlapping (time-average)
        w = 1.0 * (1.0 - frac_overlap) + 0.5 * frac_overlap
        weights.append(w)
        scaled.append(float(tr["net_return_pct"]) * w)

    out = df.copy()
    out["capital_weight"] = weights
    out["scaled_net_return_pct"] = scaled
    nets = np.array(scaled, dtype=float)
    days = _span_days(span_start, span_end)
    hold_h = (out["exit_time"] - out["entry_time"]).dt.total_seconds() / 3600.0
    ct = float((hold_h * out["capital_weight"]).sum())
    pnl = float(nets.sum())
    # occupancy: average concurrent capital over span
    # max concurrent capital = 1.0 always under 50/50 rule
    return {
        "model": "M3_PARALLEL_50_50",
        "total_capital_base": 1.0,
        "executed_trades": int(len(out)),
        "net_return_additive": pnl,
        "unscaled_net_return": float(df["net_return_pct"].astype(float).sum()),
        "pnl_per_day": pnl / days if days else None,
        "capital_time_hours": ct,
        "pnl_per_capital_hour": pnl / ct if ct else None,
        "max_concurrent_capital": 1.0,
        "mean_capital_weight": float(np.mean(weights)),
        "max_dd_additive": _max_dd_additive(nets),
        "scaled_trades_df": out,
    }


def parallel_occupancy(
    apt: pd.DataFrame,
    doge: pd.DataFrame,
    *,
    span_start,
    span_end,
) -> dict[str, Any]:
    from orderbook_analyse.fractal_wave_fade_multicoin_overlap.intervals import timeline_state_stats

    st = timeline_state_stats(apt, doge, span_start=span_start, span_end=span_end)
    # avg concurrent = 0*neither + 1*(apt_only+doge_only) + 2*both
    p0 = st["pct_both_flat"] / 100.0
    p1 = (st["pct_apt_only"] + st["pct_doge_only"]) / 100.0
    p2 = st["pct_both_active"] / 100.0
    return {
        **st,
        "pct_0_positions": st["pct_both_flat"],
        "pct_1_position": st["pct_apt_only"] + st["pct_doge_only"],
        "pct_2_positions": st["pct_both_active"],
        "avg_concurrent_positions": p1 * 1.0 + p2 * 2.0,
        "max_concurrent_positions": 2 if p2 > 0 else (1 if p1 > 0 else 0),
    }
