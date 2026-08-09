"""Per-block trade statistics."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from orderbook_analyse.fractal_wave_fade_equity_acceleration_analysis.periods import (
    months_in_period,
)


def _longest_loss_streak(nets: np.ndarray) -> int:
    best = cur = 0
    for x in nets:
        if x <= 0:
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return int(best)


def _max_dd_additive(nets: np.ndarray) -> float:
    if len(nets) == 0:
        return 0.0
    eq = np.cumsum(nets)
    peak = np.maximum.accumulate(eq)
    dd = eq - peak
    return float(dd.min()) if len(dd) else 0.0


def _pf(nets: np.ndarray) -> float | None:
    wins = nets[nets > 0].sum()
    losses = -nets[nets < 0].sum()
    if losses <= 1e-15:
        return None if wins <= 0 else float("inf")
    return float(wins / losses)


def block_trade_stats(
    g: pd.DataFrame,
    *,
    period: str,
    data_start: pd.Timestamp,
    data_end: pd.Timestamp,
) -> dict[str, Any]:
    nets = g["net_return_pct"].astype(float).to_numpy()
    n = len(nets)
    months = months_in_period(period, data_start=data_start, data_end=data_end)
    tp = int((g["exit_reason"] == "TP").sum())
    sl = int((g["exit_reason"] == "SL").sum())
    wins = nets[nets > 0]
    losses = nets[nets <= 0]
    return {
        "period": period,
        "trades": n,
        "months_covered": float(months),
        "trades_per_month": float(n / months) if months > 0 else None,
        "tp_count": tp,
        "sl_count": sl,
        "tp_rate": float(tp / n) if n else None,
        "win_rate": float((nets > 0).mean()) if n else None,
        "expectancy": float(nets.mean()) if n else None,
        "median_net_return": float(np.median(nets)) if n else None,
        "profit_factor": _pf(nets),
        "cumulative_additive_return": float(nets.sum()) if n else 0.0,
        "max_drawdown_additive": _max_dd_additive(nets),
        "longest_loss_streak": _longest_loss_streak(nets),
        "mean_winning_trade": float(wins.mean()) if len(wins) else None,
        "median_winning_trade": float(np.median(wins)) if len(wins) else None,
        "mean_losing_trade": float(losses.mean()) if len(losses) else None,
        "median_losing_trade": float(np.median(losses)) if len(losses) else None,
        "upgrade_count": int(g["upgrade_count"].astype(int).sum()),
        "upgrade_rate": float((g["upgrade_count"].astype(int) > 0).mean()) if n else None,
        "share_15m": float((g["first_signal_tf"] == "15m").mean()) if n else None,
        "share_30m": float((g["first_signal_tf"] == "30m").mean()) if n else None,
        "share_1h": float((g["first_signal_tf"] == "1h").mean()) if n else None,
        "share_4h": float((g["first_signal_tf"] == "4h").mean()) if n else None,
        "share_1h_4h": float(g["first_signal_tf"].isin(["1h", "4h"]).mean()) if n else None,
    }


def tf_mix_rows(g: pd.DataFrame, period: str) -> list[dict[str, Any]]:
    rows = []
    n = len(g)
    for tf in ("15m", "30m", "1h", "4h"):
        sub = g[g["first_signal_tf"] == tf]
        nets = sub["net_return_pct"].astype(float).to_numpy()
        nn = len(nets)
        tp = int((sub["exit_reason"] == "TP").sum()) if nn else 0
        rows.append(
            {
                "period": period,
                "first_signal_tf": tf,
                "trades": nn,
                "share": float(nn / n) if n else None,
                "tp_rate": float(tp / nn) if nn else None,
                "expectancy": float(nets.mean()) if nn else None,
                "profit_factor": _pf(nets) if nn else None,
            }
        )
    return rows


def upgrade_rows(g: pd.DataFrame, period: str) -> dict[str, Any]:
    n = len(g)
    up = g[g["upgrade_count"].astype(int) > 0]
    no = g[g["upgrade_count"].astype(int) == 0]
    highest = (
        {str(k): int(v) for k, v in g["highest_tf_reached"].value_counts().items()}
        if n
        else {}
    )
    up_nets = up["net_return_pct"].astype(float).to_numpy()
    no_nets = no["net_return_pct"].astype(float).to_numpy()
    return {
        "period": period,
        "trades": n,
        "upgrade_count_sum": int(g["upgrade_count"].astype(int).sum()),
        "n_upgraded_trades": int(len(up)),
        "upgrade_rate": float(len(up) / n) if n else None,
        "highest_tf_counts": highest,
        "expectancy_upgraded": float(up_nets.mean()) if len(up_nets) else None,
        "expectancy_not_upgraded": float(no_nets.mean()) if len(no_nets) else None,
        "tp_rate_upgraded": float((up["exit_reason"] == "TP").mean()) if len(up) else None,
        "tp_rate_not_upgraded": float((no["exit_reason"] == "TP").mean()) if len(no) else None,
        "pf_upgraded": _pf(up_nets) if len(up_nets) else None,
        "pf_not_upgraded": _pf(no_nets) if len(no_nets) else None,
    }


def symbol_side_rows(g: pd.DataFrame, period: str) -> list[dict[str, Any]]:
    rows = []
    for sym in ("APTUSDT", "DOGEUSDT"):
        for side in ("LONG", "SHORT"):
            sub = g[(g["symbol"] == sym) & (g["side"] == side)]
            nets = sub["net_return_pct"].astype(float).to_numpy()
            nn = len(nets)
            tp = int((sub["exit_reason"] == "TP").sum()) if nn else 0
            rows.append(
                {
                    "period": period,
                    "symbol": sym,
                    "side": side,
                    "trades": nn,
                    "expectancy": float(nets.mean()) if nn else None,
                    "profit_factor": _pf(nets) if nn else None,
                    "tp_rate": float(tp / nn) if nn else None,
                    "cumulative_additive": float(nets.sum()) if nn else 0.0,
                }
            )
        # symbol total
        sub = g[g["symbol"] == sym]
        nets = sub["net_return_pct"].astype(float).to_numpy()
        nn = len(nets)
        tp = int((sub["exit_reason"] == "TP").sum()) if nn else 0
        rows.append(
            {
                "period": period,
                "symbol": sym,
                "side": "ALL",
                "trades": nn,
                "expectancy": float(nets.mean()) if nn else None,
                "profit_factor": _pf(nets) if nn else None,
                "tp_rate": float(tp / nn) if nn else None,
                "cumulative_additive": float(nets.sum()) if nn else 0.0,
            }
        )
    for side in ("LONG", "SHORT"):
        sub = g[g["side"] == side]
        nets = sub["net_return_pct"].astype(float).to_numpy()
        nn = len(nets)
        tp = int((sub["exit_reason"] == "TP").sum()) if nn else 0
        rows.append(
            {
                "period": period,
                "symbol": "BOTH",
                "side": side,
                "trades": nn,
                "expectancy": float(nets.mean()) if nn else None,
                "profit_factor": _pf(nets) if nn else None,
                "tp_rate": float(tp / nn) if nn else None,
                "cumulative_additive": float(nets.sum()) if nn else 0.0,
            }
        )
    return rows
