"""Chronological time-block definitions (no random splits)."""

from __future__ import annotations

from typing import Any

import pandas as pd


def _ensure_utc(ts: pd.Timestamp) -> pd.Timestamp:
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        return t.tz_localize("UTC")
    return t.tz_convert("UTC")


def quarter_blocks(start: pd.Timestamp, end: pd.Timestamp) -> list[dict[str, Any]]:
    start, end = _ensure_utc(start), _ensure_utc(end)
    total = (end - start).total_seconds()
    if total <= 0:
        return []
    blocks = []
    for i in range(4):
        a = start + pd.Timedelta(seconds=total * i / 4)
        b = start + pd.Timedelta(seconds=total * (i + 1) / 4)
        blocks.append({"block_type": "QUARTER", "block_id": f"Q{i+1}", "start": a, "end": b})
    return blocks


def half_blocks(start: pd.Timestamp, end: pd.Timestamp) -> list[dict[str, Any]]:
    start, end = _ensure_utc(start), _ensure_utc(end)
    mid = start + (end - start) / 2
    return [
        {"block_type": "HALF", "block_id": "FIRST_50", "start": start, "end": mid},
        {"block_type": "HALF", "block_id": "LAST_50", "start": mid, "end": end},
    ]


def rolling_6m_blocks(start: pd.Timestamp, end: pd.Timestamp) -> list[dict[str, Any]]:
    start, end = _ensure_utc(start), _ensure_utc(end)
    blocks = []
    cur = start
    i = 1
    while cur < end:
        nxt = min(cur + pd.DateOffset(months=6), end)
        # require at least ~60 days to count
        if (nxt - cur) >= pd.Timedelta(days=60):
            blocks.append(
                {
                    "block_type": "ROLLING_6M",
                    "block_id": f"R6M_{i:02d}",
                    "start": pd.Timestamp(cur),
                    "end": pd.Timestamp(nxt),
                }
            )
        cur = nxt
        i += 1
        if i > 40:
            break
    return blocks


def filter_trades_in_block(df: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    """Assign trades by entry_time ∈ [start, end)."""
    if df is None or df.empty:
        return df.iloc[0:0] if df is not None else pd.DataFrame()
    et = pd.to_datetime(df["entry_time"], utc=True)
    start, end = _ensure_utc(start), _ensure_utc(end)
    return df.loc[(et >= start) & (et < end)].copy()


def with_fee(df: pd.DataFrame, fee_pct: float) -> pd.DataFrame:
    if df is None or df.empty:
        return df
    out = df.copy()
    out["fees"] = fee_pct
    out["net_return"] = out["gross_return"].astype(float) - fee_pct
    return out
