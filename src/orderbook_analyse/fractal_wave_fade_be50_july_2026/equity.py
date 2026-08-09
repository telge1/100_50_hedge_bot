"""Local July equity with cashout/reimbursement."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from orderbook_analyse.fractal_wave_fade_be50_july_2026 import (
    CASHOUT_RATE,
    COVERAGE_RATE,
    START_ACTIVE,
    START_RESERVE,
)


def simulate_equity(nets: list[float]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    active = START_ACTIVE
    reserve = START_RESERVE
    rows = []
    peak_total = active + reserve
    max_dd = 0.0
    longest_sl = cur_sl = 0
    longest_nonwin = cur_nw = 0
    # need reasons too — pass separately
    return rows, {}  # placeholder


def simulate_equity_path(trades_df: pd.DataFrame, net_col: str, reason_col: str) -> tuple[pd.DataFrame, dict[str, Any]]:
    active = float(START_ACTIVE)
    reserve = float(START_RESERVE)
    rows = []
    peak_total = active + reserve
    max_dd = 0.0
    longest_sl = cur_sl = 0
    longest_nw = cur_nw = 0

    for _, tr in trades_df.iterrows():
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

        rows.append(
            {
                "july_n": int(tr["july_n"]),
                "trade_id": int(tr["trade_id"]),
                "exit_time": tr.get("be50_exit_time", tr.get("exit_time")),
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
    }
    return pd.DataFrame(rows), summary
