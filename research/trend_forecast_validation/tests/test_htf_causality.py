"""HTF causality tests."""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from research.trend_forecast_validation.causal_replay import build_htf_ohlcv, run_causal_scanner_replay
from research.trend_forecast_validation.config import default_config


def _ohlcv(n: int = 400) -> pd.DataFrame:
    base = datetime(2026, 2, 1, tzinfo=timezone.utc)
    rows = []
    px = 5.0
    for i in range(n):
        px *= 1.0005
        ts = pd.Timestamp(base) + pd.Timedelta(minutes=5 * i)
        rows.append(
            {
                "timestamp": ts,
                "open": px,
                "high": px * 1.001,
                "low": px * 0.999,
                "close": px,
                "volume": 1.0,
            }
        )
    return pd.DataFrame(rows)


def test_open_30m_bucket_not_visible_before_close() -> None:
    df = _ohlcv(100)
    # decision inside a 30m bucket should exclude that incomplete bucket
    # e.g. open 10:00 bucket, decision 10:20 → bucket close 10:30 > decision → excluded
    decision = pd.Timestamp(df.iloc[20]["timestamp"]) + pd.Timedelta(minutes=5)  # close of bar 20
    htf = build_htf_ohlcv(df, "30m", decision)
    if htf.empty:
        return
    closes = pd.to_datetime(htf["timestamp"], utc=True) + pd.Timedelta(minutes=30)
    assert (closes <= decision).all()


def test_trace_exports_last_visible_htf_timestamps() -> None:
    cfg = default_config()
    trace, _ = run_causal_scanner_replay(_ohlcv(500), cfg)
    assert "last_visible_30m_timestamp" in trace.columns
    assert "last_visible_4h_timestamp" in trace.columns
    assert "htf_both_closed" in trace.columns
