"""Causal signal-timeframe bars from closed 1m ClickHouse candles."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from typing import Any, Sequence

import pandas as pd

from pool_order_plan_v1.candles import ensure_utc
from pool_order_plan_v1.config import signal_generator_root


def _ensure_sg() -> None:
    src = signal_generator_root() / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))


def tf_minutes(timeframe: str) -> int:
    raw = str(timeframe or "").strip().lower()
    mapping = {"15m": 15, "30m": 30, "1h": 60, "60m": 60, "4h": 240, "5m": 5}
    if raw not in mapping:
        raise ValueError(f"unsupported timeframe {timeframe}")
    return mapping[raw]


def aggregate_signal_tf(one_minute_rows: Sequence[dict[str, Any]], timeframe: str) -> pd.DataFrame:
    _ensure_sg()
    from signal_generator.timeframes import OhlcvBar, aggregate_1m_to_timeframe, ensure_utc as sg_utc

    bars = []
    for row in one_minute_rows:
        ot = sg_utc(row["open_time"])
        ct = row.get("close_time")
        ct = sg_utc(ct) if ct is not None else ot + timedelta(minutes=1)
        bars.append(
            OhlcvBar(
                open_time=ot,
                close_time=ct,
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row.get("volume") or 0.0),
                turnover=float(row.get("turnover") or 0.0),
            )
        )
    if not bars:
        return pd.DataFrame(columns=["timestamp", "close_time", "open", "high", "low", "close", "volume"])
    as_of = bars[-1].close_time
    agg = aggregate_1m_to_timeframe(bars, timeframe, as_of=as_of, require_complete=True)
    recs = [
        {
            "timestamp": pd.Timestamp(b.open_time),
            "close_time": pd.Timestamp(b.close_time),
            "open": b.open,
            "high": b.high,
            "low": b.low,
            "close": b.close,
            "volume": b.volume,
        }
        for b in agg
    ]
    df = pd.DataFrame.from_records(recs)
    if not df.empty:
        df = df.sort_values("timestamp").reset_index(drop=True)
    return df


def causal_tf_prefix(df: pd.DataFrame, entry_time: Any) -> pd.DataFrame:
    et = pd.Timestamp(ensure_utc(entry_time))
    if df is None or df.empty:
        return df
    closes = pd.to_datetime(df["close_time"], utc=True)
    prefix = df.loc[closes <= et].copy().reset_index(drop=True)
    late = prefix.loc[pd.to_datetime(prefix["close_time"], utc=True) > et]
    if not late.empty:
        raise RuntimeError("FUTURE_BAR_IN_FRAME")
    return prefix
