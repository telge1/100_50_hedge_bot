"""Simulate cashout + loss reimbursement equity paths."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from orderbook_analyse.fractal_wave_fade_cashout_reimbursement_analysis import (
    START_ACTIVE,
    START_RESERVE,
)


def simulate(
    trades: pd.DataFrame,
    *,
    cashout_rate: float,
    coverage_rate: float = 1.0,
    reimburse_mode: str = "ALL_NEGATIVE",  # or "SL_ONLY"
    start_active: float = START_ACTIVE,
    start_reserve: float = START_RESERVE,
) -> dict[str, Any]:
    df = trades.sort_values(["exit_time", "trade_id"]).reset_index(drop=True)
    active = float(start_active)
    reserve = float(start_reserve)
    cr = float(cashout_rate)
    cov = float(coverage_rate)

    rows: list[dict[str, Any]] = []
    stats = {
        "n_losses": 0,
        "fully_reimbursed": 0,
        "partially_reimbursed": 0,
        "unreimbursed": 0,
        "total_loss_usdt": 0.0,
        "total_reimbursed_usdt": 0.0,
        "total_unreimbursed_usdt": 0.0,
        "reserve_hit_zero_events": 0,
        "n_cashouts": 0,
        "total_cashed": 0.0,
    }
    prev_reserve_positive = reserve > 1e-12

    for _, tr in df.iterrows():
        a0, r0 = active, reserve
        t0 = a0 + r0
        net = float(tr["net_return_pct"])
        pnl = a0 * net / 100.0
        cashout = 0.0
        reimb = 0.0
        fully = partially = depleted = False

        if pnl > 1e-15:
            cashout = pnl * cr
            active = a0 + pnl - cashout
            reserve = r0 + cashout
            if cashout > 0:
                stats["n_cashouts"] += 1
                stats["total_cashed"] += cashout
        elif pnl < -1e-15:
            # apply loss
            active = a0 + pnl
            loss_abs = -pnl
            stats["n_losses"] += 1
            stats["total_loss_usdt"] += loss_abs

            eligible = False
            if reimburse_mode == "ALL_NEGATIVE":
                eligible = True
            elif reimburse_mode == "SL_ONLY":
                eligible = str(tr["exit_reason"]) == "SL"

            if eligible and cov > 0 and reserve > 0:
                wanted = loss_abs * cov
                reimb = min(wanted, reserve)
                active = active + reimb
                reserve = reserve - reimb
                stats["total_reimbursed_usdt"] += reimb
                uncovered = loss_abs - reimb
                # relative to full loss (not just coverage target)
                if reimb + 1e-12 >= loss_abs:
                    fully = True
                    stats["fully_reimbursed"] += 1
                elif reimb > 1e-12:
                    partially = True
                    stats["partially_reimbursed"] += 1
                    stats["unreimbursed"] += 1
                    stats["total_unreimbursed_usdt"] += uncovered
                else:
                    stats["unreimbursed"] += 1
                    stats["total_unreimbursed_usdt"] += uncovered
            else:
                stats["unreimbursed"] += 1
                stats["total_unreimbursed_usdt"] += loss_abs
        else:
            # flat
            active = a0

        # clamp numerical dust
        if reserve < 0 and abs(reserve) < 1e-9:
            reserve = 0.0
        assert reserve >= -1e-9
        reserve = max(0.0, reserve)

        t1 = active + reserve
        # transfer invariance: wealth change equals raw pnl only
        # (cashout/reimb are internal)
        wealth_delta = t1 - t0
        assert abs(wealth_delta - pnl) < 1e-6 * max(1.0, abs(pnl), abs(t0))

        if prev_reserve_positive and reserve <= 1e-12:
            stats["reserve_hit_zero_events"] += 1
            depleted = True
        prev_reserve_positive = reserve > 1e-12

        rows.append(
            {
                "trade_id": int(tr["trade_id"]),
                "entry_time": pd.Timestamp(tr["entry_time"]),
                "exit_time": pd.Timestamp(tr["exit_time"]),
                "symbol": str(tr["symbol"]),
                "side": str(tr["side"]),
                "exit_reason": str(tr["exit_reason"]),
                "first_signal_tf": str(tr["first_signal_tf"]),
                "net_return_pct": net,
                "cashout_rate": cr,
                "coverage_rate": cov,
                "reimburse_mode": reimburse_mode,
                "active_before": a0,
                "reserve_before": r0,
                "total_before": t0,
                "raw_trade_pnl": pnl,
                "cashout_amount": cashout,
                "reimbursement_amount": reimb,
                "active_after": active,
                "reserve_after": reserve,
                "total_after": t1,
                "loss_fully_covered": fully,
                "loss_partially_covered": partially,
                "reserve_depleted": depleted,
            }
        )

    path = pd.DataFrame(rows)
    dd_a = _dd(path, "active_after")
    dd_t = _dd(path, "total_after")
    cov_rate = (
        float(stats["fully_reimbursed"] / stats["n_losses"])
        if stats["n_losses"]
        else None
    )
    # broader: share of loss USDT reimbursed
    usdt_cov = (
        float(stats["total_reimbursed_usdt"] / stats["total_loss_usdt"])
        if stats["total_loss_usdt"] > 0
        else None
    )

    summary = {
        "cashout_rate": cr,
        "cashout_rate_pct": int(round(cr * 100)),
        "coverage_rate": cov,
        "coverage_rate_pct": int(round(cov * 100)),
        "reimburse_mode": reimburse_mode,
        "start_active": float(start_active),
        "start_reserve": float(start_reserve),
        "end_active": float(active),
        "end_reserve": float(reserve),
        "end_total_wealth": float(active + reserve),
        "active_return_pct": float(active / start_active - 1.0) * 100.0,
        "total_wealth_return_pct": float((active + reserve) / start_active - 1.0) * 100.0,
        "active_max_dd_pct": dd_a["max_dd_pct"],
        "active_max_dd_usdt": dd_a["max_dd_usdt"],
        "total_max_dd_pct": dd_t["max_dd_pct"],
        "total_max_dd_usdt": dd_t["max_dd_usdt"],
        "dd_active": dd_a,
        "dd_total": dd_t,
        "n_cashouts": stats["n_cashouts"],
        "total_cashed": stats["total_cashed"],
        "n_losses": stats["n_losses"],
        "fully_reimbursed": stats["fully_reimbursed"],
        "partially_reimbursed": stats["partially_reimbursed"],
        "unreimbursed": stats["unreimbursed"],
        "full_cover_rate": cov_rate,
        "usdt_reimbursement_coverage": usdt_cov,
        "total_loss_usdt": stats["total_loss_usdt"],
        "total_reimbursed_usdt": stats["total_reimbursed_usdt"],
        "total_unreimbursed_usdt": stats["total_unreimbursed_usdt"],
        "reserve_hit_zero_events": stats["reserve_hit_zero_events"],
        "reserve_zero_share": float((path["reserve_after"] <= 1e-12).mean()),
        "reserve_positive_share": float((path["reserve_after"] > 1e-12).mean()),
        **_reserve_quantiles(path),
        **_reserve_empty_streak(path),
    }
    return {"path": path, "summary": summary}


def simulate_cashout_only(
    trades: pd.DataFrame,
    cashout_rate: float,
    *,
    start_active: float = START_ACTIVE,
    start_reserve: float = START_RESERVE,
) -> dict[str, Any]:
    """Cashout without reimbursement (coverage_rate=0)."""
    return simulate(
        trades,
        cashout_rate=cashout_rate,
        coverage_rate=0.0,
        reimburse_mode="ALL_NEGATIVE",
        start_active=start_active,
        start_reserve=start_reserve,
    )


def _dd(path: pd.DataFrame, col: str) -> dict[str, Any]:
    eq = path[col].astype(float).to_numpy()
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
        }
    peak = np.maximum.accumulate(eq)
    dd_u = eq - peak
    dd_p = np.where(peak > 0, dd_u / peak * 100.0, 0.0)
    ti = int(np.argmin(dd_p))
    peak_level = peak[ti]
    cands = np.where(eq[: ti + 1] == peak_level)[0]
    pi = int(cands[-1]) if len(cands) else 0
    ri = None
    for j in range(ti + 1, len(eq)):
        if eq[j] >= peak_level:
            ri = j
            break
    return {
        "max_dd_pct": float(dd_p[ti]),
        "max_dd_usdt": float(dd_u[ti]),
        "peak_trade_id": int(path.loc[pi, "trade_id"]),
        "trough_trade_id": int(path.loc[ti, "trade_id"]),
        "peak_time": pd.Timestamp(path.loc[pi, "exit_time"]).isoformat(),
        "trough_time": pd.Timestamp(path.loc[ti, "exit_time"]).isoformat(),
        "recovery_trade_id": None if ri is None else int(path.loc[ri, "trade_id"]),
        "recovery_time": None
        if ri is None
        else pd.Timestamp(path.loc[ri, "exit_time"]).isoformat(),
        "trades_to_recovery": None if ri is None else int(ri - ti),
    }


def _reserve_quantiles(path: pd.DataFrame) -> dict[str, float]:
    r = path["reserve_after"].astype(float).to_numpy()
    qs = {}
    for q in (10, 25, 50, 75, 90):
        qs[f"reserve_p{q}"] = float(np.percentile(r, q))
    return qs


def _reserve_empty_streak(path: pd.DataFrame) -> dict[str, Any]:
    empty = (path["reserve_after"].astype(float) <= 1e-12).to_numpy()
    max_empty = cur = 0
    for e in empty:
        if e:
            cur += 1
            max_empty = max(max_empty, cur)
        else:
            cur = 0
    return {"max_consecutive_reserve_empty_trades": int(max_empty)}
