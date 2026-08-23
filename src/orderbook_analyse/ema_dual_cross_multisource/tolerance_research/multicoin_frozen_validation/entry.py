"""Causal entry: first 1m open at or after decision_at."""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd


def _utc(dt: datetime | str) -> datetime:
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def first_1m_open_at_or_after(
    candles_1m: pd.DataFrame,
    decision_at: datetime | str,
) -> tuple[datetime | None, float | None]:
    """Return (entry_at, entry_open) for the first 1m bar with open_time >= decision_at.

    ``decision_at`` is the close of the completed signal bar. The 1m bar that opens
    exactly at ``decision_at`` is valid. Bars with ``open_time < decision_at`` are never used
    (including the signal bar's own open). Long and short share this rule.
    """
    if candles_1m is None or candles_1m.empty:
        return None, None
    tcol = pd.to_datetime(candles_1m["open_time"])
    dec = _utc(decision_at)
    if getattr(tcol.dt, "tz", None) is not None:
        mask = tcol >= pd.Timestamp(dec)
    else:
        mask = tcol >= pd.Timestamp(dec.replace(tzinfo=None))
    sub = candles_1m.loc[mask].sort_values("open_time")
    if sub.empty:
        return None, None
    row = sub.iloc[0]
    ts = pd.Timestamp(row["open_time"]).to_pydatetime()
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    else:
        ts = ts.astimezone(timezone.utc)
    if ts < dec:
        return None, None
    return ts, float(row["open"])


def assert_no_future_features(decision_at: datetime | str, feature_ts: datetime | str | None) -> bool:
    """True iff feature timestamp is causal (None allowed = missing)."""
    if feature_ts is None:
        return True
    return _utc(feature_ts) <= _utc(decision_at)
