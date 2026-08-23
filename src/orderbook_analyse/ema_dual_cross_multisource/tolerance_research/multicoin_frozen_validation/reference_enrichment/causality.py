"""Causal window helpers: only completed bars / rows with ts <= decision_at."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd


def as_utc(dt: datetime | str) -> datetime:
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def bar_close_time(open_time: datetime, tf_minutes: int) -> datetime:
    return as_utc(open_time) + timedelta(minutes=tf_minutes)


def completed_bars(
    candles: pd.DataFrame,
    decision_at: datetime | str,
    *,
    tf_minutes: int = 5,
    time_col: str = "open_time",
) -> pd.DataFrame:
    """Return bars fully closed by decision_at (close_time <= decision_at)."""
    if candles is None or candles.empty:
        return pd.DataFrame()
    dec = as_utc(decision_at)
    out = candles.copy()
    out[time_col] = pd.to_datetime(out[time_col], utc=True)
    close_t = out[time_col] + pd.Timedelta(minutes=tf_minutes)
    mask = close_t <= pd.Timestamp(dec)
    return out.loc[mask].sort_values(time_col).reset_index(drop=True)


def rows_at_or_before(
    df: pd.DataFrame,
    decision_at: datetime | str,
    *,
    time_col: str,
) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    dec = as_utc(decision_at)
    out = df.copy()
    out[time_col] = pd.to_datetime(out[time_col], utc=True)
    mask = out[time_col] <= pd.Timestamp(dec)
    return out.loc[mask].sort_values(time_col).reset_index(drop=True)


def window_slice(
    df: pd.DataFrame,
    *,
    time_col: str,
    end: datetime,
    lookback: timedelta,
) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    end_u = as_utc(end)
    start_u = end_u - lookback
    t = pd.to_datetime(df[time_col], utc=True)
    mask = (t > pd.Timestamp(start_u)) & (t <= pd.Timestamp(end_u))
    return df.loc[mask].copy()


def normalize_direction(direction: str) -> str:
    """Map checkpoint directions to LONG/SHORT (EDC uses BULLISH/BEARISH)."""
    d = str(direction).upper().strip()
    if d in ("LONG", "BUY", "BULLISH"):
        return "LONG"
    if d in ("SHORT", "SELL", "BEARISH"):
        return "SHORT"
    raise ValueError(f"Unknown direction: {direction}")


def direction_sign(direction: str) -> int:
    d = normalize_direction(direction)
    if d == "LONG":
        return 1
    return -1


def mirror_for_direction(value: float | None, direction: str) -> float | None:
    """Long keeps sign; short flips so supportive = positive."""
    if value is None:
        return None
    return float(value) * direction_sign(direction)
