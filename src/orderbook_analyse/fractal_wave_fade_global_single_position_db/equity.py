"""Additive and fractional compounding equity curves."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from orderbook_analyse.fractal_wave_fade_global_single_position_db import START_EQUITY


def longest_loss_streak(nets: np.ndarray) -> int:
    max_l = cur = 0
    for x in nets:
        if x < -1e-12:
            cur += 1
            max_l = max(max_l, cur)
        else:
            cur = 0
    return int(max_l)


def compound_equity(
    nets_pct: np.ndarray,
    *,
    start: float = START_EQUITY,
    fraction: float = 1.0,
) -> np.ndarray:
    """equity_next = equity_prev * (1 + f * net_return_pct / 100)."""
    eq = np.empty(len(nets_pct) + 1, dtype=float)
    eq[0] = float(start)
    f = float(fraction)
    for i, r in enumerate(nets_pct):
        eq[i + 1] = eq[i] * (1.0 + f * float(r) / 100.0)
    return eq


def equity_curve_frame(
    trades: pd.DataFrame,
    *,
    fraction: float,
    start: float = START_EQUITY,
) -> pd.DataFrame:
    if trades is None or trades.empty:
        return pd.DataFrame(
            columns=[
                "trade_id",
                "exit_time",
                "symbol",
                "side",
                "net_return_pct",
                "equity_before",
                "equity_after",
                "drawdown_pct",
                "drawdown_usdt",
            ]
        )
    df = trades.sort_values(["exit_time", "entry_time", "trade_id"]).reset_index(drop=True)
    nets = df["net_return_pct"].astype(float).to_numpy()
    eq = compound_equity(nets, start=start, fraction=fraction)
    before = eq[:-1]
    after = eq[1:]
    peak = np.maximum.accumulate(after)
    dd_usdt = after - peak
    dd_pct = np.where(peak > 0, dd_usdt / peak * 100.0, np.nan)
    return pd.DataFrame(
        {
            "trade_id": df["trade_id"].values,
            "exit_time": df["exit_time"].values,
            "symbol": df["symbol"].values,
            "side": df["side"].values,
            "net_return_pct": nets,
            "equity_before": before,
            "equity_after": after,
            "drawdown_pct": dd_pct,
            "drawdown_usdt": dd_usdt,
        }
    )


def summarize_fraction(
    trades: pd.DataFrame,
    *,
    fraction: float,
    start: float = START_EQUITY,
    window_start: pd.Timestamp | None = None,
    window_end: pd.Timestamp | None = None,
) -> dict[str, Any]:
    label = f"{int(round(fraction * 100))}pct"
    row: dict[str, Any] = {
        "fraction": fraction,
        "fraction_label": label,
        "start_equity": float(start),
    }
    if trades is None or trades.empty:
        row.update(
            {
                "end_equity": float(start),
                "pnl_usdt": 0.0,
                "total_return_pct": 0.0,
                "cagr_pct": None,
                "max_drawdown_pct": 0.0,
                "max_drawdown_usdt": 0.0,
                "peak_equity": float(start),
                "trough_equity_after_start": float(start),
                "trades": 0,
                "wins": 0,
                "losses": 0,
                "win_rate": None,
                "profit_factor": None,
                "expectancy": None,
                "median_trade": None,
                "longest_loss_streak": 0,
            }
        )
        return row

    df = trades.sort_values(["exit_time", "entry_time", "trade_id"]).reset_index(drop=True)
    nets = df["net_return_pct"].astype(float).to_numpy()
    eq = compound_equity(nets, start=start, fraction=fraction)
    after = eq[1:]
    peak = np.maximum.accumulate(after)
    dd_usdt = after - peak
    dd_pct = np.where(peak > 0, dd_usdt / peak * 100.0, 0.0)
    wins = nets[nets > 1e-12]
    losses = nets[nets < -1e-12]
    end = float(eq[-1])
    total_ret = (end / start - 1.0) * 100.0 if start else None

    cagr = None
    if window_start is not None and window_end is not None and start > 0 and end > 0:
        years = (pd.Timestamp(window_end) - pd.Timestamp(window_start)).total_seconds() / (
            365.25 * 24 * 3600
        )
        if years >= 1.0:
            cagr = ((end / start) ** (1.0 / years) - 1.0) * 100.0

    # expectancy / PF on trade net returns (strategy, independent of fraction)
    pf = (
        float(np.sum(wins) / abs(np.sum(losses)))
        if len(wins) and len(losses) and np.sum(losses) != 0
        else None
    )
    row.update(
        {
            "end_equity": end,
            "pnl_usdt": end - float(start),
            "total_return_pct": float(total_ret) if total_ret is not None else None,
            "cagr_pct": float(cagr) if cagr is not None else None,
            "max_drawdown_pct": float(dd_pct.min()) if len(dd_pct) else 0.0,
            "max_drawdown_usdt": float(dd_usdt.min()) if len(dd_usdt) else 0.0,
            "peak_equity": float(peak.max()) if len(peak) else float(start),
            "trough_equity_after_start": float(after.min()) if len(after) else float(start),
            "trades": int(len(nets)),
            "wins": int(len(wins)),
            "losses": int(len(losses)),
            "win_rate": float(np.mean(nets > 0)),
            "profit_factor": pf,
            "expectancy": float(np.mean(nets)),
            "median_trade": float(np.median(nets)),
            "longest_loss_streak": longest_loss_streak(nets),
        }
    )
    return row


def annotate_trade_equities(trades: pd.DataFrame, start: float = START_EQUITY) -> pd.DataFrame:
    """Attach equity_before/after for 25/50/100 fractions onto trades frame."""
    if trades is None or trades.empty:
        return trades.copy() if trades is not None else pd.DataFrame()
    df = trades.sort_values(["exit_time", "entry_time"]).reset_index(drop=True)
    if "trade_id" not in df.columns:
        df.insert(0, "trade_id", np.arange(1, len(df) + 1, dtype=np.int64))
    nets = df["net_return_pct"].astype(float).to_numpy()
    for frac, tag in ((0.25, "25"), (0.50, "50"), (1.00, "100")):
        eq = compound_equity(nets, start=start, fraction=frac)
        df[f"equity_before_{tag}"] = eq[:-1]
        df[f"equity_after_{tag}"] = eq[1:]
    return df


def additive_summary(trades: pd.DataFrame) -> dict[str, Any]:
    if trades is None or trades.empty:
        return {
            "trades": 0,
            "expectancy": None,
            "profit_factor": None,
            "cumulative_additive_net": 0.0,
            "max_drawdown_additive": 0.0,
            "win_rate": None,
        }
    nets = trades.sort_values(["exit_time", "entry_time"])["net_return_pct"].astype(float).to_numpy()
    wins = nets[nets > 1e-12]
    losses = nets[nets < -1e-12]
    eq = np.cumsum(nets)
    peak = np.maximum.accumulate(eq)
    dd = eq - peak
    pf = (
        float(np.sum(wins) / abs(np.sum(losses)))
        if len(wins) and len(losses) and np.sum(losses) != 0
        else None
    )
    return {
        "trades": int(len(nets)),
        "expectancy": float(np.mean(nets)),
        "profit_factor": pf,
        "cumulative_additive_net": float(np.sum(nets)),
        "max_drawdown_additive": float(dd.min()) if len(dd) else 0.0,
        "win_rate": float(np.mean(nets > 0)),
        "median_trade": float(np.median(nets)),
        "longest_loss_streak": longest_loss_streak(nets),
    }
