"""Sequential non-overlapping selection per variant × coin × side."""

from __future__ import annotations

import pandas as pd


def apply_sequential(trades: pd.DataFrame) -> pd.DataFrame:
    """Mark taken_sequential per variant×symbol×side×exit_id in fill order."""
    if trades.empty:
        out = trades.copy()
        out["taken_sequential"] = []
        out["sequential_skip_reason"] = []
        return out
    out = trades.sort_values(
        ["variant", "symbol", "side", "exit_id", "fill_timestamp"]
    ).copy()
    keys = ["variant", "symbol", "side", "exit_id"]
    taken_col: list[bool] = []
    skip_col: list[str | None] = []
    for _, g in out.groupby(keys, sort=False):
        free_at = None
        for _, r in g.iterrows():
            fill_ts = pd.Timestamp(r["fill_timestamp"])
            if fill_ts.tzinfo is None:
                fill_ts = fill_ts.tz_localize("UTC")
            held = int(r["bars_held"]) if pd.notna(r.get("bars_held")) else 0
            exit_ts = fill_ts + pd.Timedelta(minutes=15 * max(held, 0))
            if free_at is not None and fill_ts < free_at:
                taken_col.append(False)
                skip_col.append("open_trade")
            else:
                taken_col.append(True)
                skip_col.append(None)
                free_at = exit_ts + pd.Timedelta(minutes=15)
    out["taken_sequential"] = taken_col
    out["sequential_skip_reason"] = skip_col
    return out
