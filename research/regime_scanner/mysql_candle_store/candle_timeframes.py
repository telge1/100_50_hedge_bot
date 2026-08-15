"""Candle-store timeframe helpers (direct import TFs including HTF).

Kept separate from ``research.regime_scanner.timeframes`` so scanner aggregation
(``SUPPORTED_TIMEFRAMES`` = 5m/15m/30m) stays unchanged.

Freqtrade/Bybit labels are case-sensitive for monthly: ``1M`` ≠ ``1m``.
"""

from __future__ import annotations

import pandas as pd

from research.regime_scanner.timeframes import ensure_utc_timestamp

from research.regime_scanner.mysql_candle_store.schema import DIRECT_IMPORT_TIMEFRAMES

# Freqtrade/Bybit labels are case-sensitive for monthly: ``1M`` ≠ ``1m``.

# Fixed-length TFs in minutes (1M is calendar-month and excluded here).
FIXED_TIMEFRAME_MINUTES: dict[str, int] = {
    "1m": 1,
    "5m": 5,
    "15m": 15,
    "30m": 30,
    "1h": 60,
    "4h": 240,
    "1d": 1440,
    "1w": 10080,
}


def normalize_timeframe(timeframe: str) -> str:
    """Normalize TF labels; preserve ``1M`` vs ``1m``."""
    s = str(timeframe).strip()
    if s == "1M":
        return "1M"
    if s.lower() == "1m":
        return "1m"
    return s.lower()


def is_importable_timeframe(timeframe: str) -> bool:
    return normalize_timeframe(timeframe) in DIRECT_IMPORT_TIMEFRAMES


def candle_close_time(open_time: object, timeframe: str) -> pd.Timestamp:
    """UTC close_time = first instant after the candle period (available_at)."""
    ts = ensure_utc_timestamp(open_time)
    key = normalize_timeframe(timeframe)
    if key == "1M":
        year = int(ts.year)
        month = int(ts.month)
        if month == 12:
            return pd.Timestamp(year=year + 1, month=1, day=1, tz="UTC")
        return pd.Timestamp(year=year, month=month + 1, day=1, tz="UTC")
    if key not in FIXED_TIMEFRAME_MINUTES:
        raise ValueError(f"unsupported timeframe for close_time: {timeframe!r}")
    return ts + pd.Timedelta(minutes=FIXED_TIMEFRAME_MINUTES[key])


def is_aligned_open(open_time: object, timeframe: str) -> bool:
    """Return True if open_time sits on the expected grid for ``timeframe``."""
    ts = ensure_utc_timestamp(open_time)
    if int(ts.second) != 0 or int(ts.microsecond) != 0:
        return False
    key = normalize_timeframe(timeframe)
    if key == "1M":
        return int(ts.day) == 1 and int(ts.hour) == 0 and int(ts.minute) == 0
    if key == "1w":
        # Bybit/Freqtrade weekly candles open Monday 00:00 UTC.
        return int(ts.weekday()) == 0 and int(ts.hour) == 0 and int(ts.minute) == 0
    if key == "1d":
        return int(ts.hour) == 0 and int(ts.minute) == 0
    if key == "4h":
        return int(ts.hour) % 4 == 0 and int(ts.minute) == 0
    if key == "1h":
        return int(ts.minute) == 0
    if key in FIXED_TIMEFRAME_MINUTES:
        minutes = FIXED_TIMEFRAME_MINUTES[key]
        total_min = int(ts.hour) * 60 + int(ts.minute)
        return (total_min % minutes) == 0
    return False


def expected_delta(timeframe: str) -> pd.Timedelta | None:
    """Expected open→open spacing for fixed TFs; None for calendar ``1M``."""
    key = normalize_timeframe(timeframe)
    if key == "1M":
        return None
    if key not in FIXED_TIMEFRAME_MINUTES:
        raise ValueError(f"unsupported timeframe: {timeframe!r}")
    return pd.Timedelta(minutes=FIXED_TIMEFRAME_MINUTES[key])


def count_gaps(opens: pd.Series, timeframe: str) -> tuple[int, list[dict]]:
    """Count unexpected spacing between successive opens."""
    key = normalize_timeframe(timeframe)
    ts = pd.to_datetime(opens, utc=True).sort_values().reset_index(drop=True)
    samples: list[dict] = []
    if len(ts) < 2:
        return 0, samples
    if key == "1M":
        gaps = 0
        for i in range(1, len(ts)):
            prev = ensure_utc_timestamp(ts.iloc[i - 1])
            cur = ensure_utc_timestamp(ts.iloc[i])
            expected = candle_close_time(prev, "1M")
            if cur != expected:
                gaps += 1
                if len(samples) < 20:
                    samples.append(
                        {
                            "previous": prev.isoformat(),
                            "current": cur.isoformat(),
                            "expected": expected.isoformat(),
                        }
                    )
        return gaps, samples

    expected = expected_delta(key)
    assert expected is not None
    deltas = ts.diff().iloc[1:]
    bad = deltas[deltas != expected]
    for idx in list(bad.index)[:20]:
        samples.append(
            {
                "previous": ensure_utc_timestamp(ts.iloc[idx - 1]).isoformat(),
                "current": ensure_utc_timestamp(ts.iloc[idx]).isoformat(),
                "delta": str(bad.loc[idx]),
            }
        )
    return int(len(bad)), samples
