"""Aggregate trade metrics for frozen reproduction."""

from __future__ import annotations

import statistics
from typing import Callable


def classify_managed(t: dict) -> str:
    """Outcome labels for reporting (matches long_oos_validation_v1 summary counts)."""
    o = str(t.get("managed_outcome", ""))
    if o == "TP":
        return "TP"
    if o == "SL":
        return "SL"
    if o in ("BE", "BE_SMALL", "INTRABAR_AMBIGUOUS"):
        return "BE"
    if o == "horizon":
        return "horizon"
    return "other"


def aggregate(trades: list[dict], pnl_key: str = "managed_pnl_pct", outcome_fn: Callable[[dict], str] | None = None) -> dict:
    if not trades:
        return {
            "trades": 0,
            "TP": 0,
            "SL": 0,
            "BE": 0,
            "pnl_sum_pct": 0.0,
            "profit_factor": None,
            "max_drawdown_pct": 0.0,
            "max_loss_streak": 0,
            "median_mae_pct": None,
            "median_mfe_pct": None,
        }
    fn = outcome_fn or classify_managed
    pnls = [float(t[pnl_key]) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    pf = round(sum(wins) / abs(sum(losses)), 4) if wins and losses else None
    ordered = sorted(trades, key=lambda t: t["entry_time"])
    eq = peak = max_dd = 0.0
    streak = max_streak = 0
    for t in ordered:
        p = float(t[pnl_key])
        eq += p
        peak = max(peak, eq)
        max_dd = max(max_dd, peak - eq)
        if p < 0:
            streak += 1
            max_streak = max(max_streak, streak)
        elif p > 0:
            streak = 0
    maes = [float(t["mae_pct"]) for t in trades if t.get("mae_pct") is not None]
    mfes = [float(t["mfe_pct"]) for t in trades if t.get("mfe_pct") is not None]
    return {
        "trades": len(trades),
        "TP": sum(1 for t in trades if fn(t) == "TP"),
        "SL": sum(1 for t in trades if fn(t) == "SL"),
        "BE": sum(1 for t in trades if fn(t) == "BE"),
        "pnl_sum_pct": round(sum(pnls), 4),
        "profit_factor": pf,
        "max_drawdown_pct": round(max_dd, 4),
        "max_loss_streak": max_streak,
        "median_mae_pct": round(statistics.median(maes), 4) if maes else None,
        "median_mfe_pct": round(statistics.median(mfes), 4) if mfes else None,
    }
