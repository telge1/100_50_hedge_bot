"""Simulate Active/Reserve paths under cashout + reimbursement with leverage."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd


def simulate_levered_path(
    trades: pd.DataFrame,
    *,
    leverage: float,
    cashout_rate: float = 0.30,
    coverage_rate: float = 1.0,
    start_active: float = 1000.0,
    start_reserve: float = 0.0,
) -> dict[str, Any]:
    df = trades.sort_values(["exit_time", "trade_id"]).reset_index(drop=True)
    active = float(start_active)
    reserve = float(start_reserve)
    cr = float(cashout_rate)
    cov = float(coverage_rate)
    lev = float(leverage)

    rows: list[dict[str, Any]] = []
    capital_depleted = False
    depleted_at_trade: int | None = None
    depleted_at_time: str | None = None

    peak_active = active
    peak_total = active + reserve
    max_reserve = reserve
    max_reserve_time: str | None = None
    max_reserve_trade: int | None = None

    total_cashout = 0.0
    total_reimb = 0.0
    min_active = active
    max_active = active

    worst_active_dd = 0.0
    worst_active_dd_time: str | None = None
    worst_active_dd_trade: int | None = None
    worst_total_dd = 0.0
    worst_total_dd_time: str | None = None

    for i, tr in df.iterrows():
        tid = int(tr["trade_id"])
        exit_t = pd.Timestamp(tr["exit_time"])
        if exit_t.tzinfo is None:
            exit_t = exit_t.tz_localize("UTC")
        else:
            exit_t = exit_t.tz_convert("UTC")

        a0, r0 = active, reserve
        t0 = a0 + r0
        net = float(tr["net_return_pct"])
        lev_net = net * lev
        cashout = 0.0
        reimb = 0.0
        pnl = 0.0
        skipped = False

        if capital_depleted or a0 <= 0:
            # freeze: no further trading once capital gone
            capital_depleted = True
            skipped = True
            if depleted_at_trade is None:
                depleted_at_trade = tid
                depleted_at_time = exit_t.isoformat()
            active = max(0.0, a0)
            reserve = max(0.0, r0)
            pnl = 0.0
            lev_net = 0.0
        else:
            pnl = a0 * lev_net / 100.0

            if pnl > 1e-15:
                cashout = pnl * cr
                active = a0 + pnl - cashout
                reserve = r0 + cashout
                total_cashout += cashout
            elif pnl < -1e-15:
                active = a0 + pnl
                reimb = min(abs(pnl) * cov, r0)
                active = active + reimb
                reserve = r0 - reimb
                total_reimb += reimb
            else:
                active = a0

            if reserve < 0 and abs(reserve) < 1e-9:
                reserve = 0.0
            reserve = max(0.0, reserve)

            if active <= 0:
                active = 0.0
                capital_depleted = True
                depleted_at_trade = tid
                depleted_at_time = exit_t.isoformat()

        t1 = active + reserve
        peak_active = max(peak_active, active)
        peak_total = max(peak_total, t1)
        max_active = max(max_active, active)
        min_active = min(min_active, active)

        if reserve >= max_reserve - 1e-12:
            max_reserve = reserve
            max_reserve_time = exit_t.isoformat()
            max_reserve_trade = tid

        active_dd = (active / peak_active - 1.0) * 100.0 if peak_active > 0 else 0.0
        total_dd = (t1 / peak_total - 1.0) * 100.0 if peak_total > 0 else 0.0

        if active_dd < worst_active_dd:
            worst_active_dd = active_dd
            worst_active_dd_time = exit_t.isoformat()
            worst_active_dd_trade = tid
        if total_dd < worst_total_dd:
            worst_total_dd = total_dd
            worst_total_dd_time = exit_t.isoformat()

        rows.append(
            {
                "trade_number": int(i) + 1,
                "trade_id": tid,
                "exit_time": exit_t,
                "symbol": str(tr["symbol"]),
                "side": str(tr["side"]),
                "exit_reason": str(tr["exit_reason"]),
                "first_signal_tf": str(tr["first_signal_tf"]),
                "net_return_pct": net,
                "leverage": lev,
                "leveraged_net_return_pct": lev_net if not skipped else 0.0,
                "active_before": a0,
                "raw_trade_pnl": pnl,
                "cashout_amount": cashout,
                "reimbursement_amount": reimb,
                "active_after": active,
                "reserve_after": reserve,
                "total_wealth_after": t1,
                "active_drawdown_pct": float(active_dd),
                "total_drawdown_pct": float(total_dd),
                "capital_depleted": bool(capital_depleted),
                "skipped_after_depletion": bool(skipped),
            }
        )

    path = pd.DataFrame(rows)
    summary: dict[str, Any] = {
        "leverage": lev,
        "start_active": float(start_active),
        "start_reserve": float(start_reserve),
        "end_active": float(active),
        "end_reserve": float(reserve),
        "end_total_wealth": float(active + reserve),
        "max_active": float(max_active),
        "min_active": float(min_active),
        "active_max_dd_pct": float(worst_active_dd),
        "active_max_dd_time": worst_active_dd_time,
        "active_max_dd_trade_id": worst_active_dd_trade,
        "total_max_dd_pct": float(worst_total_dd),
        "total_max_dd_time": worst_total_dd_time,
        "total_cashout_generated": float(total_cashout),
        "total_reimbursement_used": float(total_reimb),
        "max_reserve": float(max_reserve),
        "max_reserve_time": max_reserve_time,
        "max_reserve_trade_id": max_reserve_trade,
        "capital_depleted": bool(capital_depleted),
        "depleted_at_trade_id": depleted_at_trade,
        "depleted_at_time": depleted_at_time,
        "n_trades_simulated": int((~path["skipped_after_depletion"]).sum()) if len(path) else 0,
        "n_trades_total": int(len(path)),
    }
    return {"path": path, "summary": summary}


def monthly_snapshots(path: pd.DataFrame, leverage: float) -> pd.DataFrame:
    if path.empty:
        return pd.DataFrame(
            columns=["month", "leverage", "active_equity", "reserve", "total_wealth"]
        )
    p = path.copy()
    p["exit_time"] = pd.to_datetime(p["exit_time"], utc=True)
    p["month"] = p["exit_time"].dt.strftime("%Y-%m")
    # last trade of each month
    idx = p.groupby("month", sort=True)["trade_number"].idxmax()
    last = p.loc[idx].sort_values("month")
    return pd.DataFrame(
        {
            "month": last["month"].astype(str).values,
            "leverage": float(leverage),
            "active_equity": last["active_after"].astype(float).values,
            "reserve": last["reserve_after"].astype(float).values,
            "total_wealth": last["total_wealth_after"].astype(float).values,
        }
    )


def reserve_events(path: pd.DataFrame) -> pd.DataFrame:
    m = (path["cashout_amount"].astype(float) > 0) | (
        path["reimbursement_amount"].astype(float) > 0
    )
    cols = [
        "trade_id",
        "exit_time",
        "symbol",
        "side",
        "raw_trade_pnl",
        "cashout_amount",
        "reimbursement_amount",
        "active_after",
        "reserve_after",
        "total_wealth_after",
        "leverage",
    ]
    out = path.loc[m, [c for c in cols if c in path.columns]].copy()
    return out.reset_index(drop=True)
