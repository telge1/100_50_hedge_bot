"""Initial stop from last confirmed structural lower low."""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from ob_microstructure_breakout_bot.data.bars import load_5m_bars
from ob_microstructure_breakout_bot.exit_backtest.thresholds import SL_BUFFER
from research.regime_scanner.swings import (
    filter_pivots_as_of,
    find_confirmed_pivots,
    pivots_by_type,
)


def compute_long_sl(
    symbol: str,
    entry_ts: datetime,
    *,
    lookback_hours: int = 36,
) -> tuple[float, float, str]:
    """Return (sl_price, pivot_low, pivot_timestamp_iso)."""
    start = entry_ts - timedelta(hours=lookback_hours)
    end = entry_ts + timedelta(minutes=5)
    bars = load_5m_bars(symbol, start, end)
    if not bars:
        raise RuntimeError(f"No bars for SL around {entry_ts.isoformat()}")

    df = pd.DataFrame(
        [
            {
                "timestamp": b.ts,
                "high": b.high,
                "low": b.low,
                "open": b.open,
                "close": b.close,
            }
            for b in bars
        ]
    )
    pivots = find_confirmed_pivots(df)
    visible = filter_pivots_as_of(pivots, entry_ts)
    lows = pivots_by_type(visible, "low")
    if not lows:
        raise RuntimeError(f"No confirmed pivot low before {entry_ts.isoformat()}")
    last = lows[-1]
    sl = float(last.price) * (1.0 - SL_BUFFER)
    return sl, float(last.price), str(last.pivot_timestamp)
