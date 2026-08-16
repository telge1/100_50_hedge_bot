"""Map signal_generator HTF / 1m bars into freeze OHLCV DataFrame shape."""

from __future__ import annotations

from datetime import datetime
from typing import Iterable, Sequence

import pandas as pd

from signal_generator.timeframes import OhlcvBar, ensure_utc


def bars_to_ohlcv_df(bars: Sequence[OhlcvBar]) -> pd.DataFrame:
    """Freeze schema: timestamp=open_time UTC, available_at=close_time UTC."""
    if not bars:
        return pd.DataFrame(
            columns=[
                "timestamp",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "close_time",
                "available_at",
            ]
        )
    rows = []
    for b in bars:
        ot = ensure_utc(b.open_time)
        ct = ensure_utc(b.close_time)
        rows.append(
            {
                "timestamp": ot,
                "open": float(b.open),
                "high": float(b.high),
                "low": float(b.low),
                "close": float(b.close),
                "volume": float(b.volume),
                "close_time": ct,
                "available_at": ct,
            }
        )
    df = pd.DataFrame(rows)
    df = df.sort_values("timestamp").drop_duplicates("timestamp").reset_index(drop=True)
    return df


def records_to_ohlcv_df(records: Iterable[dict]) -> pd.DataFrame:
    """Accept dicts with open_time/close_time or timestamp/available_at."""
    rows = []
    for r in records:
        if "timestamp" in r:
            ts = pd.Timestamp(r["timestamp"])
        else:
            ts = pd.Timestamp(r["open_time"])
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        else:
            ts = ts.tz_convert("UTC")
        if "available_at" in r:
            av = pd.Timestamp(r["available_at"])
        elif "close_time" in r:
            av = pd.Timestamp(r["close_time"])
        else:
            raise ValueError("record needs available_at or close_time")
        if av.tzinfo is None:
            av = av.tz_localize("UTC")
        else:
            av = av.tz_convert("UTC")
        rows.append(
            {
                "timestamp": ts,
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
                "volume": float(r.get("volume", 0.0)),
                "close_time": av,
                "available_at": av,
            }
        )
    if not rows:
        return bars_to_ohlcv_df([])
    df = pd.DataFrame(rows)
    return df.sort_values("timestamp").drop_duplicates("timestamp").reset_index(drop=True)


def one_minute_books(
    ohlcv_1m: pd.DataFrame,
) -> tuple[object, object]:
    """Return (open_times ndarray, opens ndarray) for resolve_entries."""
    import numpy as np

    ts = pd.to_datetime(ohlcv_1m["timestamp"], utc=True)
    # Freeze books use tz-naive UTC ns for searchsorted vs confirmation.
    open_times = ts.dt.tz_convert(None).to_numpy(dtype="datetime64[ns]")
    opens = ohlcv_1m["open"].astype(float).to_numpy()
    return open_times, opens


def ensure_confirmation_before_entry(
    confirmation: datetime | pd.Timestamp, entry: datetime | pd.Timestamp
) -> bool:
    """Causal check: entry open must be strictly after confirmation."""
    c = pd.Timestamp(confirmation)
    e = pd.Timestamp(entry)
    if c.tzinfo is None:
        c = c.tz_localize("UTC")
    else:
        c = c.tz_convert("UTC")
    if e.tzinfo is None:
        e = e.tz_localize("UTC")
    else:
        e = e.tz_convert("UTC")
    return e > c
