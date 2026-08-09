"""Shared-slot and parallel schedulers over independent trades."""

from __future__ import annotations

from typing import Any

import pandas as pd


def _ts(x) -> pd.Timestamp:
    t = pd.Timestamp(x)
    return t.tz_convert("UTC") if t.tzinfo else t.tz_localize("UTC")


def schedule_shared_slot(
    independent: pd.DataFrame,
    *,
    tie_break: str = "APT_FIRST",  # or DOGE_FIRST
) -> dict[str, Any]:
    """
    max_concurrent=1. Candidates = independent trades at their original entry_time.
    If slot busy at entry → BLOCKED forever (no shift).
    """
    df = independent.copy()
    df["entry_time"] = pd.to_datetime(df["entry_time"], utc=True)
    df["exit_time"] = pd.to_datetime(df["exit_time"], utc=True)

    # sort key
    prio = {"APTUSDT": 0, "DOGEUSDT": 1}
    if tie_break == "DOGE_FIRST":
        prio = {"DOGEUSDT": 0, "APTUSDT": 1}

    df["_prio"] = df["symbol"].map(prio).fillna(9)
    df = df.sort_values(["entry_time", "_prio", "trade_id"]).reset_index(drop=True)

    open_until: pd.Timestamp | None = None
    open_trade: dict | None = None
    executed: list[dict] = []
    blocked: list[dict] = []
    ties = 0

    # detect exact timestamp ties across symbols
    for i in range(len(df) - 1):
        if (
            df.loc[i, "entry_time"] == df.loc[i + 1, "entry_time"]
            and df.loc[i, "symbol"] != df.loc[i + 1, "symbol"]
        ):
            ties += 1

    for _, row in df.iterrows():
        et = _ts(row["entry_time"])
        xt = _ts(row["exit_time"])
        rec = row.to_dict()
        if open_until is not None and not (et > open_until):
            # busy: entry not strictly after exit
            rem_min = (open_until - et).total_seconds() / 60.0
            blocked.append(
                {
                    **{k: rec[k] for k in (
                        "trade_id", "symbol", "side", "entry_time", "exit_time",
                        "entry_price", "exit_price", "exit_reason",
                        "gross_return_pct", "fee_pct", "net_return_pct",
                        "holding_minutes", "first_signal_tf",
                    ) if k in rec},
                    "block_reason": "SLOT_BUSY",
                    "open_symbol": open_trade["symbol"] if open_trade else None,
                    "open_side": open_trade["side"] if open_trade else None,
                    "open_exit_time": open_until,
                    "remaining_hold_minutes_at_block": rem_min,
                    "tie_break": tie_break,
                }
            )
            continue
        executed.append({**rec, "scheduler": "SHARED_SLOT", "tie_break": tie_break})
        open_until = xt
        open_trade = {"symbol": row["symbol"], "side": row["side"]}

    exec_df = pd.DataFrame(executed) if executed else pd.DataFrame()
    blk_df = pd.DataFrame(blocked) if blocked else pd.DataFrame()
    n_cand = len(df)
    n_exec = len(exec_df)
    n_blk = len(blk_df)
    return {
        "tie_break": tie_break,
        "exact_cross_symbol_entry_ties": ties,
        "candidates": n_cand,
        "executed": n_exec,
        "blocked": n_blk,
        "block_rate": float(n_blk / n_cand) if n_cand else None,
        "executed_df": exec_df,
        "blocked_df": blk_df,
        "net_pnl_additive": float(exec_df["net_return_pct"].sum()) if n_exec else 0.0,
        "by_symbol_executed": exec_df["symbol"].value_counts().to_dict() if n_exec else {},
        "by_symbol_blocked": blk_df["symbol"].value_counts().to_dict() if n_blk else {},
    }


def parallel_all(independent: pd.DataFrame) -> dict[str, Any]:
    """max_concurrent=2: all independent trades execute (1 per symbol already)."""
    df = independent.copy()
    return {
        "executed": int(len(df)),
        "executed_df": df,
        "net_pnl_additive": float(df["net_return_pct"].astype(float).sum()),
        "max_concurrent": 2,
        "note": "Equivalent to OLD_PER_SYMBOL_MAX1 (both streams free).",
    }
