"""Canonical entry: next signal-timeframe open after the signal bar."""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from .semantics import ENTRY_RULE


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def next_signal_tf_open(
    df: pd.DataFrame,
    bar_index: int,
) -> tuple[datetime | None, float | None]:
    """Return (entry_at, entry_open) for the bar after ``bar_index`` on the signal TF.

    This is the original successful XRP entry rule (``mfe_runner._next_open``).
    """
    if bar_index + 1 >= len(df):
        return None, None
    nxt = df.iloc[bar_index + 1]
    ts = _utc(pd.Timestamp(nxt["open_time"]).to_pydatetime())
    return ts, float(nxt["open"])


def entry_rule_id() -> str:
    return ENTRY_RULE
