"""Causal 1m → 5m frames. No 1000-bar integration lookback."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

import pandas as pd

from .config import signal_generator_root
from .schema import REASON_LAST_5M_INCOMPLETE, REASON_TZ


def ensure_utc(ts: Any) -> datetime:
    if ts is None or (isinstance(ts, float) and pd.isna(ts)):
        raise ValueError(REASON_TZ)
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    else:
        t = t.tz_convert("UTC")
    return t.to_pydatetime()


def _ensure_sg_path() -> None:
    src = signal_generator_root() / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))


def expected_last_closed_5m(entry_time: Any) -> tuple[datetime, datetime]:
    """Immediately preceding fully closed 5m bucket at entry (not an older fallback)."""
    _ensure_sg_path()
    from signal_generator.timeframes import bucket_close, bucket_start

    et = ensure_utc(entry_time)
    containing_open = bucket_start(et, "5m")
    containing_close = bucket_close(containing_open, "5m")
    if et >= containing_close:
        return containing_open, containing_close
    prev_open = containing_open - timedelta(minutes=5)
    return prev_open, containing_open


@dataclass
class FiveMinuteSeries:
    symbol: str
    bars: pd.DataFrame  # timestamp (open), close_time, ohlcv
    one_minute_rows: int
    missing_one_minute_rows: int
    duplicate_one_minute_rows: int
    dropped_incomplete_five_minute_buckets: int
    history_start: datetime | None
    history_end: datetime | None
    candle_source: str = "clickhouse"


def _to_ohlcv_bars(rows: Sequence[dict[str, Any]]):
    _ensure_sg_path()
    from signal_generator.timeframes import OhlcvBar, ensure_utc as sg_utc

    bars = []
    for row in rows:
        ot = sg_utc(row["open_time"])
        ct = row.get("close_time")
        if ct is None:
            ct = ot + timedelta(minutes=1)
        else:
            ct = sg_utc(ct)
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
    return bars


def build_five_minute_series(symbol: str, one_minute_rows: Sequence[dict[str, Any]]) -> FiveMinuteSeries:
    _ensure_sg_path()
    from signal_generator.timeframes import aggregate_1m_to_timeframe

    if not one_minute_rows:
        return FiveMinuteSeries(
            symbol=symbol.upper(),
            bars=pd.DataFrame(columns=["timestamp", "close_time", "open", "high", "low", "close", "volume"]),
            one_minute_rows=0,
            missing_one_minute_rows=0,
            duplicate_one_minute_rows=0,
            dropped_incomplete_five_minute_buckets=0,
            history_start=None,
            history_end=None,
        )

    by_open: dict[datetime, dict[str, Any]] = {}
    dups = 0
    for row in one_minute_rows:
        ot = ensure_utc(row["open_time"])
        if ot in by_open:
            dups += 1
            continue
        by_open[ot] = {**row, "open_time": ot}

    ordered_times = sorted(by_open)
    missing = 0
    if ordered_times:
        cursor = ordered_times[0]
        end = ordered_times[-1]
        present = set(ordered_times)
        while cursor < end:
            cursor = cursor + timedelta(minutes=1)
            if cursor not in present and cursor < end:
                missing += 1

    ohlcv = _to_ohlcv_bars([by_open[t] for t in ordered_times])
    as_of = ohlcv[-1].close_time
    five = aggregate_1m_to_timeframe(ohlcv, "5m", as_of=as_of, require_complete=True)
    expected_buckets = 0
    if ordered_times:
        span_start = ordered_times[0]
        # count 5m opens in range
        t = span_start.replace(second=0, microsecond=0)
        from signal_generator.timeframes import bucket_start

        t = bucket_start(t, "5m")
        last = ordered_times[-1]
        while t <= last:
            expected_buckets += 1
            t = t + timedelta(minutes=5)
    dropped = max(0, expected_buckets - len(five))

    records = []
    for bar in five:
        records.append(
            {
                "timestamp": pd.Timestamp(bar.open_time),
                "close_time": pd.Timestamp(bar.close_time),
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "volume": bar.volume,
            }
        )
    df = pd.DataFrame.from_records(records)
    if not df.empty:
        df = df.sort_values("timestamp").reset_index(drop=True)

    return FiveMinuteSeries(
        symbol=str(symbol).upper(),
        bars=df,
        one_minute_rows=len(ordered_times),
        missing_one_minute_rows=missing,
        duplicate_one_minute_rows=dups,
        dropped_incomplete_five_minute_buckets=dropped,
        history_start=ordered_times[0] if ordered_times else None,
        history_end=ordered_times[-1] if ordered_times else None,
    )


class LastFiveIncomplete(RuntimeError):
    reason = REASON_LAST_5M_INCOMPLETE


class FutureBarInFrame(RuntimeError):
    reason = "FUTURE_BAR_IN_FRAME"


def causal_prefix(series: FiveMinuteSeries, entry_time: Any) -> pd.DataFrame:
    """Prefix with close_time <= entry. Requires the immediately expected 5m bucket."""
    et = ensure_utc(entry_time)
    expected_open, expected_close = expected_last_closed_5m(et)
    df = series.bars
    if df is None or df.empty:
        raise LastFiveIncomplete(REASON_LAST_5M_INCOMPLETE)

    match = df.loc[pd.to_datetime(df["timestamp"], utc=True) == pd.Timestamp(expected_open)]
    if match.empty:
        raise LastFiveIncomplete(REASON_LAST_5M_INCOMPLETE)
    got_close = ensure_utc(match.iloc[0]["close_time"])
    if got_close != expected_close:
        raise LastFiveIncomplete(REASON_LAST_5M_INCOMPLETE)

    closes = pd.to_datetime(df["close_time"], utc=True)
    prefix = df.loc[closes <= pd.Timestamp(et)].copy().reset_index(drop=True)
    if prefix.empty:
        raise LastFiveIncomplete(REASON_LAST_5M_INCOMPLETE)
    last_open = ensure_utc(prefix.iloc[-1]["timestamp"])
    if last_open != expected_open:
        raise LastFiveIncomplete(REASON_LAST_5M_INCOMPLETE)
    late = prefix.loc[pd.to_datetime(prefix["close_time"], utc=True) > pd.Timestamp(et)]
    if not late.empty:
        raise FutureBarInFrame("FUTURE_BAR_IN_FRAME")
    out = prefix[["timestamp", "open", "high", "low", "close", "volume"]].copy()
    out["close_time"] = pd.to_datetime(prefix["close_time"], utc=True)
    out["timestamp"] = pd.to_datetime(out["timestamp"], utc=True)
    return out


def five_minute_from_csv(path, *, close_offset_minutes: int = 5) -> FiveMinuteSeries:
    """TEST_FIXTURE_ONLY: planner CSV for isolated golden comparison. Never publish."""
    raw = pd.read_csv(path)
    raw["timestamp"] = pd.to_datetime(raw["timestamp"], utc=True)
    rows = []
    for _, row in raw.iterrows():
        ot = ensure_utc(row["timestamp"])
        rows.append(
            {
                "open_time": ot,
                "close_time": ot + timedelta(minutes=close_offset_minutes),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row.get("volume") or 0.0),
            }
        )
    df = pd.DataFrame(
        [
            {
                "timestamp": pd.Timestamp(r["open_time"]),
                "close_time": pd.Timestamp(r["close_time"]),
                "open": r["open"],
                "high": r["high"],
                "low": r["low"],
                "close": r["close"],
                "volume": r["volume"],
            }
            for r in rows
        ]
    )
    return FiveMinuteSeries(
        symbol="HYPEUSDT",
        bars=df,
        one_minute_rows=0,
        missing_one_minute_rows=0,
        duplicate_one_minute_rows=0,
        dropped_incomplete_five_minute_buckets=0,
        history_start=ensure_utc(df.iloc[0]["timestamp"]) if not df.empty else None,
        history_end=ensure_utc(df.iloc[-1]["timestamp"]) if not df.empty else None,
        candle_source="TEST_FIXTURE_ONLY",
    )
