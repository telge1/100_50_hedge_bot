"""Simulate active/reserve equity paths under cashout rates."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from orderbook_analyse.fractal_wave_fade_cashout_reserve_analysis import (
    START_ACTIVE,
    START_RESERVE,
)


def simulate_cashout(
    trades: pd.DataFrame,
    cashout_rate: float,
    *,
    start_active: float = START_ACTIVE,
    start_reserve: float = START_RESERVE,
) -> dict[str, Any]:
    """Chronological cashout simulation. Returns path frame + summary metrics."""
    df = trades.sort_values(["exit_time", "trade_id"]).reset_index(drop=True)
    n = len(df)
    active = float(start_active)
    reserve = float(start_reserve)
    rate = float(cashout_rate)

    rows = []
    n_cashouts = 0
    cashout_sums = []
    total_cashed = 0.0

    for _, tr in df.iterrows():
        before_a, before_r = active, reserve
        net = float(tr["net_return_pct"])
        pnl = active * net / 100.0
        cashout = 0.0
        if pnl > 0 and rate > 0:
            cashout = pnl * rate
            active = active + pnl - cashout
            reserve = reserve + cashout
            n_cashouts += 1
            cashout_sums.append(cashout)
            total_cashed += cashout
        else:
            active = active + pnl
            # reserve unchanged (including zero pnl)

        assert reserve >= before_r - 1e-12
        rows.append(
            {
                "trade_id": int(tr["trade_id"]),
                "exit_time": pd.Timestamp(tr["exit_time"]),
                "entry_time": pd.Timestamp(tr["entry_time"]),
                "symbol": str(tr["symbol"]),
                "side": str(tr["side"]),
                "exit_reason": str(tr["exit_reason"]),
                "first_signal_tf": str(tr["first_signal_tf"]),
                "net_return_pct": net,
                "cashout_rate": rate,
                "trade_pnl": pnl,
                "cashout": cashout,
                "active_before": before_a,
                "reserve_before": before_r,
                "active_after": active,
                "reserve_after": reserve,
                "total_before": before_a + before_r,
                "total_after": active + reserve,
            }
        )

    path = pd.DataFrame(rows)
    dd_active = _drawdown_stats(path, "active_after", "active_before")
    dd_total = _drawdown_stats(path, "total_after", "total_before")

    # reserve coverage at worst active DD trough
    cover = _reserve_coverage(path, dd_active)

    summary = {
        "cashout_rate": rate,
        "cashout_rate_pct": int(round(rate * 100)),
        "start_active": float(start_active),
        "start_reserve": float(start_reserve),
        "end_active": float(active),
        "end_reserve": float(reserve),
        "end_total_wealth": float(active + reserve),
        "total_wealth_return_pct": float((active + reserve) / start_active - 1.0) * 100.0,
        "active_equity_return_pct": float(active / start_active - 1.0) * 100.0,
        "total_cashed_out": float(total_cashed),
        "n_cashouts": int(n_cashouts),
        "avg_cashout_per_win": float(np.mean(cashout_sums)) if cashout_sums else 0.0,
        "n_trades": int(n),
        "active_max_dd_pct": dd_active["max_dd_pct"],
        "active_max_dd_usdt": dd_active["max_dd_usdt"],
        "total_max_dd_pct": dd_total["max_dd_pct"],
        "total_max_dd_usdt": dd_total["max_dd_usdt"],
        "reserve_at_worst_active_dd": cover["reserve_at_trough"],
        "reserve_before_worst_active_dd": cover["reserve_before_dd"],
        "active_peak_to_trough_loss": cover["active_peak_to_trough_loss"],
        "coverage_ratio": cover["coverage_ratio"],
        "RESERVE_COVERS_MAX_DD": cover["covers"],
        "dd_active": dd_active,
        "dd_total": dd_total,
    }
    return {"path": path, "summary": summary}


def _drawdown_stats(path: pd.DataFrame, after_col: str, before_col: str) -> dict[str, Any]:
    """Drawdown on post-trade equity series (after_col)."""
    eq = path[after_col].astype(float).to_numpy()
    if len(eq) == 0:
        return {
            "max_dd_pct": 0.0,
            "max_dd_usdt": 0.0,
            "peak_trade_id": None,
            "trough_trade_id": None,
            "peak_time": None,
            "trough_time": None,
            "recovery_trade_id": None,
            "recovery_time": None,
            "trades_to_recovery": None,
            "peak_i": None,
            "trough_i": None,
        }
    peak = np.maximum.accumulate(eq)
    dd_usdt = eq - peak
    dd_pct = np.where(peak > 0, dd_usdt / peak * 100.0, 0.0)
    trough_i = int(np.argmin(dd_pct))
    # peak index = last time at running peak before trough
    peak_level = peak[trough_i]
    peak_i = trough_i
    while peak_i > 0 and peak[peak_i - 1] == peak_level:
        peak_i -= 1
    # find first index where peak equals this peak value at start of episode
    # simpler: last index <= trough where eq == peak_level
    candidates = np.where(eq[: trough_i + 1] == peak_level)[0]
    peak_i = int(candidates[-1]) if len(candidates) else 0

    recovery_i = None
    for j in range(trough_i + 1, len(eq)):
        if eq[j] >= peak_level:
            recovery_i = j
            break

    return {
        "max_dd_pct": float(dd_pct[trough_i]),
        "max_dd_usdt": float(dd_usdt[trough_i]),
        "peak_trade_id": int(path.loc[peak_i, "trade_id"]),
        "trough_trade_id": int(path.loc[trough_i, "trade_id"]),
        "peak_time": pd.Timestamp(path.loc[peak_i, "exit_time"]).isoformat(),
        "trough_time": pd.Timestamp(path.loc[trough_i, "exit_time"]).isoformat(),
        "recovery_trade_id": None if recovery_i is None else int(path.loc[recovery_i, "trade_id"]),
        "recovery_time": None
        if recovery_i is None
        else pd.Timestamp(path.loc[recovery_i, "exit_time"]).isoformat(),
        "trades_to_recovery": None if recovery_i is None else int(recovery_i - trough_i),
        "peak_i": peak_i,
        "trough_i": trough_i,
    }


def _reserve_coverage(path: pd.DataFrame, dd_active: dict[str, Any]) -> dict[str, Any]:
    ti = dd_active.get("trough_i")
    pi = dd_active.get("peak_i")
    if ti is None or pi is None or path.empty:
        return {
            "reserve_before_dd": 0.0,
            "reserve_at_trough": 0.0,
            "active_peak_to_trough_loss": 0.0,
            "coverage_ratio": None,
            "covers": "YES",
        }
    peak_active = float(path.loc[pi, "active_after"])
    # If peak_i is the trade that set the peak, trough uses after
    trough_active = float(path.loc[ti, "active_after"])
    loss = peak_active - trough_active  # positive number if drawdown
    reserve_before = float(path.loc[pi, "reserve_after"])
    reserve_trough = float(path.loc[ti, "reserve_after"])
    ratio = (reserve_trough / loss) if loss > 1e-12 else None
    covers = "YES" if (loss <= 1e-12 or reserve_trough + 1e-9 >= loss) else "NO"
    return {
        "reserve_before_dd": reserve_before,
        "reserve_at_trough": reserve_trough,
        "active_peak_to_trough_loss": float(loss),
        "coverage_ratio": float(ratio) if ratio is not None else None,
        "covers": covers,
    }
