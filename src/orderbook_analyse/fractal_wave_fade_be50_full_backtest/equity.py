"""Cashout/reimbursement equity path (fixed trade sequence)."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from orderbook_analyse.fractal_wave_fade_be50_full_backtest import (
    CASHOUT_RATE,
    COVERAGE_RATE,
    START_ACTIVE,
    START_RESERVE,
)


def simulate_equity_path(
    trades_df: pd.DataFrame,
    net_col: str,
    reason_col: str,
    *,
    exit_time_col: str | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Apply nets in dataframe order (fixed schedule; do not re-sort by BE exit)."""
    active = float(START_ACTIVE)
    reserve = float(START_RESERVE)
    rows: list[dict[str, Any]] = []
    peak_total = active + reserve
    max_dd = 0.0
    longest_sl = cur_sl = 0
    longest_nw = cur_nw = 0
    equity = []
    dd_series = []

    for i, tr in trades_df.iterrows():
        a0, r0 = active, reserve
        net = float(tr[net_col])
        reason = str(tr[reason_col])
        pnl = a0 * net / 100.0
        cashout = reimb = 0.0
        if pnl > 1e-15:
            cashout = pnl * CASHOUT_RATE
            active = a0 + pnl - cashout
            reserve = r0 + cashout
        elif pnl < -1e-15:
            active = a0 + pnl
            reimb = min(abs(pnl) * COVERAGE_RATE, r0)
            active = active + reimb
            reserve = r0 - reimb
        reserve = max(0.0, reserve)
        total = active + reserve
        peak_total = max(peak_total, total)
        dd = (total / peak_total - 1.0) * 100.0 if peak_total > 0 else 0.0
        max_dd = min(max_dd, dd)
        equity.append(total)
        dd_series.append(dd)

        if reason == "SL":
            cur_sl += 1
            longest_sl = max(longest_sl, cur_sl)
        else:
            cur_sl = 0
        if reason in ("SL", "BE"):
            cur_nw += 1
            longest_nw = max(longest_nw, cur_nw)
        else:
            cur_nw = 0

        xt = tr[exit_time_col] if exit_time_col and exit_time_col in tr.index else tr.get(
            "exit_time", tr.get("be50_exit_time", tr.get("exit_time_baseline"))
        )
        rows.append(
            {
                "seq": int(i) if isinstance(i, (int, np.integer)) else len(rows),
                "trade_id": int(tr["trade_id"]),
                "exit_time": xt,
                "reason": reason,
                "net_return_pct": net,
                "active_before": a0,
                "reserve_before": r0,
                "raw_trade_pnl": pnl,
                "cashout_amount": cashout,
                "reimbursement_amount": reimb,
                "active_after": active,
                "reserve_after": reserve,
                "total_after": total,
                "drawdown_pct": dd,
            }
        )

    path = pd.DataFrame(rows)
    if len(path):
        path["seq"] = np.arange(len(path))
    dd_stats = drawdown_episode_stats(np.asarray(equity, dtype=float), path)
    nets = trades_df[net_col].astype(float)
    wins = nets[nets > 0]
    losses = nets[nets < 0]
    pf = float(wins.sum() / abs(losses.sum())) if len(losses) and abs(losses.sum()) > 0 else None

    summary = {
        "end_active": active,
        "end_reserve": reserve,
        "end_total": active + reserve,
        "performance_pct": (active + reserve) / START_ACTIVE * 100.0 - 100.0,
        "max_dd_pct": max_dd,
        "longest_sl_streak": longest_sl,
        "longest_nonwinner_streak": longest_nw,
        "n_tp": int((trades_df[reason_col] == "TP").sum()),
        "n_sl": int((trades_df[reason_col] == "SL").sum()),
        "n_be": int((trades_df[reason_col] == "BE").sum()),
        "n_other": int((~trades_df[reason_col].isin(["TP", "SL", "BE"])).sum()),
        "profit_factor": pf,
        "avg_trade_pct": float(nets.mean()) if len(nets) else 0.0,
        "median_trade_pct": float(nets.median()) if len(nets) else 0.0,
        "winrate": float((trades_df[reason_col] == "TP").mean()) if len(trades_df) else 0.0,
        "loss_rate": float((trades_df[reason_col] == "SL").mean()) if len(trades_df) else 0.0,
        **dd_stats,
    }
    return path, summary


def drawdown_episode_stats(equity: np.ndarray, path: pd.DataFrame) -> dict[str, Any]:
    if len(equity) == 0:
        return {
            "avg_dd_pct": 0.0,
            "median_dd_pct": 0.0,
            "n_dd_gt_2": 0,
            "n_dd_gt_5": 0,
            "n_dd_gt_10": 0,
            "longest_dd_duration_trades": 0,
            "max_dd_recovery_trades": None,
            "strongest_loss_cluster_net_pct": 0.0,
        }
    peak = np.maximum.accumulate(equity)
    dd = np.where(peak > 0, (equity / peak - 1.0) * 100.0, 0.0)
    # episode trough depths (local minima of drawdown while underwater)
    underwater = dd < -1e-12
    episode_depths = []
    episode_lens = []
    i = 0
    n = len(dd)
    while i < n:
        if not underwater[i]:
            i += 1
            continue
        j = i
        while j < n and underwater[j]:
            j += 1
        episode_depths.append(float(dd[i:j].min()))
        episode_lens.append(j - i)
        i = j
    depths = np.array(episode_depths) if episode_depths else np.array([0.0])
    # recovery trades for max DD
    ti = int(np.argmin(dd))
    peak_level = peak[ti]
    ri = None
    for j in range(ti + 1, len(equity)):
        if equity[j] >= peak_level:
            ri = j - ti
            break
    # strongest loss cluster: max consecutive negative net sum (use path nets)
    nets = path["net_return_pct"].astype(float).to_numpy()
    best = cur = 0.0
    for x in nets:
        if x < 0:
            cur += x
            best = min(best, cur)
        else:
            cur = 0.0

    return {
        "avg_dd_pct": float(depths.mean()),
        "median_dd_pct": float(np.median(depths)),
        "n_dd_gt_2": int((depths < -2.0).sum()),
        "n_dd_gt_5": int((depths < -5.0).sum()),
        "n_dd_gt_10": int((depths < -10.0).sum()),
        "longest_dd_duration_trades": int(max(episode_lens) if episode_lens else 0),
        "max_dd_recovery_trades": None if ri is None else int(ri),
        "strongest_loss_cluster_net_pct": float(best),
    }
