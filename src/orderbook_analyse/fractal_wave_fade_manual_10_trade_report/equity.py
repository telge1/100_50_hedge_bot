"""Local cashout+reimbursement equity for the selected sample only."""

from __future__ import annotations

from typing import Any

import pandas as pd


def simulate_local_equity(
    trades: pd.DataFrame,
    *,
    start_active: float,
    start_reserve: float,
    cashout_rate: float,
    coverage_rate: float = 1.0,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    active = float(start_active)
    reserve = float(start_reserve)
    cr = float(cashout_rate)
    cov = float(coverage_rate)
    rows: list[dict[str, Any]] = []
    invariants_ok = True
    checks: list[dict[str, Any]] = []

    for _, tr in trades.iterrows():
        a0, r0 = active, reserve
        t0 = a0 + r0
        net = float(tr["net_return_pct"])
        pnl = a0 * net / 100.0
        cashout = 0.0
        reimb = 0.0

        if pnl > 1e-15:
            cashout = pnl * cr
            active = a0 + pnl - cashout
            reserve = r0 + cashout
            # A: cashout from profit only
            if cashout - abs(pnl) > 1e-9:
                invariants_ok = False
        elif pnl < -1e-15:
            active = a0 + pnl
            wanted = abs(pnl) * cov
            reimb = min(wanted, r0)
            active = active + reimb
            reserve = r0 - reimb
            # B: reimbursement from reserve only
            if reimb - r0 > 1e-9 or reserve < -1e-9:
                invariants_ok = False
        else:
            active = a0

        reserve = max(0.0, reserve)
        t1 = active + reserve

        # C/D: total changes only by raw pnl
        if abs((t1 - t0) - pnl) > 1e-6 * max(1.0, abs(pnl), abs(t0)):
            invariants_ok = False

        # F: cashout is not a wealth loss (already in D)
        # G: reimbursement is not new wealth (already in D)

        rows.append(
            {
                "trade_id": int(tr["trade_id"]),
                "active_before": a0,
                "reserve_before": r0,
                "total_before": t0,
                "raw_trade_pnl": pnl,
                "cashout_amount": cashout,
                "reimbursement_amount": reimb,
                "active_after": active,
                "reserve_after": reserve,
                "total_after": t1,
            }
        )
        checks.append(
            {
                "trade_id": int(tr["trade_id"]),
                "pnl_formula_ok": abs(pnl - a0 * net / 100.0) < 1e-12,
                "total_delta_equals_pnl": abs((t1 - t0) - pnl) < 1e-6 * max(1.0, abs(pnl), abs(t0)),
                "cashout_only_on_win": (cashout == 0.0) or (pnl > 0),
                "reimb_only_on_loss": (reimb == 0.0) or (pnl < 0),
                "reimb_le_reserve_before": reimb <= r0 + 1e-9,
                "cashout_le_pnl": cashout <= abs(pnl) + 1e-9 if pnl > 0 else cashout == 0.0,
            }
        )

    # E: fees already in net_return_pct — we never subtract fee again here
    fee_double_count = False
    for _, tr in trades.iterrows():
        # gross - fee should equal net
        g = float(tr["gross_return_pct"])
        f = float(tr["fee_pct"])
        n = float(tr["net_return_pct"])
        if abs((g - f) - n) > 1e-9:
            fee_double_count = True
            invariants_ok = False

    detail = {
        "checks_per_trade": checks,
        "fee_consistency_ok": not fee_double_count,
        "A_cashout_from_profit_only": all(c["cashout_only_on_win"] and c["cashout_le_pnl"] for c in checks),
        "B_reimb_from_reserve_only": all(c["reimb_only_on_loss"] and c["reimb_le_reserve_before"] for c in checks),
        "C_transfer_preserves_total": all(c["total_delta_equals_pnl"] for c in checks),
        "D_total_after_equals_before_plus_pnl": all(c["total_delta_equals_pnl"] for c in checks),
        "E_fees_in_net_not_double_counted": not fee_double_count,
        "F_cashout_not_treated_as_loss": all(c["total_delta_equals_pnl"] for c in checks),
        "G_reimb_not_treated_as_new_profit": all(c["total_delta_equals_pnl"] for c in checks),
    }
    detail["ACCOUNTING_INVARIANTS"] = (
        "ACCOUNTING_INVARIANTS_PASS" if invariants_ok and all(detail[k] for k in (
            "A_cashout_from_profit_only",
            "B_reimb_from_reserve_only",
            "C_transfer_preserves_total",
            "D_total_after_equals_before_plus_pnl",
            "E_fees_in_net_not_double_counted",
            "F_cashout_not_treated_as_loss",
            "G_reimb_not_treated_as_new_profit",
        )) else "ACCOUNTING_INVARIANTS_FAIL"
    )
    return rows, detail


def attach_historical_full_path(
    rows: list[dict[str, Any]],
    equity_paths_csv,
    *,
    cashout_rate: float = 0.3,
    coverage_rate: float = 1.0,
) -> list[dict[str, Any]]:
    """Optional HISTORICAL_FULL_PATH columns from prior 30%/100% simulation."""
    try:
        ep = pd.read_csv(equity_paths_csv)
    except Exception:
        for r in rows:
            r["historical_active_before"] = None
            r["historical_reserve_before"] = None
            r["historical_note"] = "HISTORICAL_FULL_PATH_UNAVAILABLE"
        return rows

    m = (
        (ep["cashout_rate"].astype(float) == float(cashout_rate))
        & (ep["coverage_rate"].astype(float) == float(coverage_rate))
    )
    if "reimburse_mode" in ep.columns:
        m = m & (ep["reimburse_mode"].astype(str) == "ALL_NEGATIVE")
    sub = ep.loc[m].set_index("trade_id")
    for r in rows:
        tid = int(r["trade_id"])
        if tid in sub.index:
            r["historical_active_before"] = float(sub.loc[tid, "active_before"])
            r["historical_reserve_before"] = float(sub.loc[tid, "reserve_before"])
            r["historical_note"] = "HISTORICAL_FULL_PATH"
        else:
            r["historical_active_before"] = None
            r["historical_reserve_before"] = None
            r["historical_note"] = "HISTORICAL_FULL_PATH_MISSING_TRADE"
    return rows
