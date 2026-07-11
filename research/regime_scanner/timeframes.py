"""Causal multi-timeframe OHLCV aggregation from 5m candles.

Higher timeframes are built only from fully closed 5m groups. Indicators must
be recomputed on the aggregated frame — never sampled from 5m indicator series.
"""

from __future__ import annotations

from typing import Iterable

import pandas as pd

SUPPORTED_TIMEFRAMES = ("5m", "15m", "30m")
TIMEFRAME_MINUTES = {"5m": 5, "15m": 15, "30m": 30}
BARS_PER_AGGREGATE = {"5m": 1, "15m": 3, "30m": 6}
REQUIRED_COLUMNS = ("timestamp", "open", "high", "low", "close", "volume")


class TimeframeAggregationError(ValueError):
    """Raised when causal aggregation inputs are invalid."""


def ensure_utc_timestamp(value: object) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        return ts.tz_localize("UTC")
    return ts.tz_convert("UTC")


def parse_timeframes(value: str | Iterable[str] | None) -> tuple[str, ...]:
    if value is None:
        return ("5m",)
    if isinstance(value, str):
        parts = [p.strip().lower() for p in value.split(",") if p.strip()]
    else:
        parts = [str(p).strip().lower() for p in value if str(p).strip()]
    if not parts:
        raise TimeframeAggregationError("at least one timeframe is required")
    unknown = [p for p in parts if p not in TIMEFRAME_MINUTES]
    if unknown:
        raise TimeframeAggregationError(
            f"unsupported timeframe(s) {unknown}; allowed={list(SUPPORTED_TIMEFRAMES)}"
        )
    # Preserve order, drop duplicates.
    out: list[str] = []
    seen: set[str] = set()
    for part in parts:
        if part not in seen:
            out.append(part)
            seen.add(part)
    return tuple(out)


def timeframe_timedelta(timeframe: str) -> pd.Timedelta:
    key = str(timeframe).strip().lower()
    if key not in TIMEFRAME_MINUTES:
        raise TimeframeAggregationError(f"unsupported timeframe: {timeframe!r}")
    return pd.Timedelta(minutes=TIMEFRAME_MINUTES[key])


def floor_to_timeframe(timestamp: object, timeframe: str) -> pd.Timestamp:
    ts = ensure_utc_timestamp(timestamp)
    minutes = TIMEFRAME_MINUTES[str(timeframe).strip().lower()]
    return ts.floor(f"{minutes}min")


def expected_5m_opens(bucket_open: object, timeframe: str) -> list[pd.Timestamp]:
    """Return the 5m open times that must exist for a complete aggregate bar."""
    key = str(timeframe).strip().lower()
    if key not in BARS_PER_AGGREGATE:
        raise TimeframeAggregationError(f"unsupported timeframe: {timeframe!r}")
    start = ensure_utc_timestamp(bucket_open)
    count = BARS_PER_AGGREGATE[key]
    return [start + pd.Timedelta(minutes=5 * i) for i in range(count)]


def _validate_5m_input(candles_5m: pd.DataFrame) -> pd.DataFrame:
    if candles_5m is None or candles_5m.empty:
        return pd.DataFrame(columns=list(REQUIRED_COLUMNS))
    missing = [c for c in REQUIRED_COLUMNS if c not in candles_5m.columns]
    if missing:
        raise TimeframeAggregationError(f"5m candles missing columns: {missing}")
    out = candles_5m.loc[:, list(REQUIRED_COLUMNS)].copy()
    out["timestamp"] = pd.to_datetime(out["timestamp"], utc=True)
    for col in ("open", "high", "low", "close", "volume"):
        out[col] = pd.to_numeric(out[col], errors="coerce")
    if int(out["timestamp"].duplicated().sum()):
        raise TimeframeAggregationError("5m candle timestamps contain duplicates")
    if not bool(out["timestamp"].is_monotonic_increasing):
        out = out.sort_values("timestamp").reset_index(drop=True)
        if int(out["timestamp"].duplicated().sum()):
            raise TimeframeAggregationError("5m candle timestamps contain duplicates")
        if not bool(out["timestamp"].is_monotonic_increasing):
            raise TimeframeAggregationError("5m candle timestamps are not ascending")
    return out.reset_index(drop=True)


def aggregate_candles(
    candles_5m: pd.DataFrame,
    timeframe: str,
    decision_time: object,
) -> pd.DataFrame:
    """Aggregate 5m OHLCV into fully closed ``timeframe`` candles as of decision_time.

    Rules
    -----
    * Only groups with a complete set of 5m bars are emitted.
    * Aggregated ``close_time = open + timeframe`` must be ``<= decision_time``.
    * Bars whose open equals or exceeds ``decision_time`` are excluded.
    * Output timestamp is the open time of the aggregated candle.
    """
    key = str(timeframe).strip().lower()
    if key not in TIMEFRAME_MINUTES:
        raise TimeframeAggregationError(f"unsupported timeframe: {timeframe!r}")

    decision_ts = ensure_utc_timestamp(decision_time)
    base = _validate_5m_input(candles_5m)
    # Causal 5m universe: only bars that have already opened before decision_time.
    base = base.loc[base["timestamp"] < decision_ts].copy().reset_index(drop=True)
    if base.empty:
        return pd.DataFrame(columns=list(REQUIRED_COLUMNS))

    if key == "5m":
        out = base.copy().reset_index(drop=True)
        # Defensive: never emit open >= decision_time.
        out = out.loc[out["timestamp"] < decision_ts].reset_index(drop=True)
        return out

    minutes = TIMEFRAME_MINUTES[key]
    expected_count = BARS_PER_AGGREGATE[key]
    duration = pd.Timedelta(minutes=minutes)
    base = base.copy()
    base["bucket_open"] = base["timestamp"].dt.floor(f"{minutes}min")

    rows: list[dict[str, object]] = []
    for bucket_open, group in base.groupby("bucket_open", sort=True):
        bucket_ts = ensure_utc_timestamp(bucket_open)
        if bucket_ts >= decision_ts:
            continue
        close_time = bucket_ts + duration
        if close_time > decision_ts:
            continue

        group = group.sort_values("timestamp")
        expected = expected_5m_opens(bucket_ts, key)
        actual = [ensure_utc_timestamp(t) for t in group["timestamp"].tolist()]
        if len(actual) < expected_count:
            continue
        if actual != expected:
            # Incomplete / gapped group — never emit a partial aggregate.
            continue

        rows.append(
            {
                "timestamp": bucket_ts,
                "open": float(group["open"].iloc[0]),
                "high": float(group["high"].max()),
                "low": float(group["low"].min()),
                "close": float(group["close"].iloc[-1]),
                "volume": float(group["volume"].sum()),
            }
        )

    if not rows:
        return pd.DataFrame(columns=list(REQUIRED_COLUMNS))

    out = pd.DataFrame(rows)
    out["timestamp"] = pd.to_datetime(out["timestamp"], utc=True)
    if int(out["timestamp"].duplicated().sum()):
        raise TimeframeAggregationError("aggregated timestamps contain duplicates")
    if not bool(out["timestamp"].is_monotonic_increasing):
        raise TimeframeAggregationError("aggregated timestamps are not ascending")
    # Final causal guards.
    out = out.loc[out["timestamp"] < decision_ts].reset_index(drop=True)
    close_times = out["timestamp"] + duration
    if bool((close_times > decision_ts).any()):
        raise TimeframeAggregationError("internal error: aggregate close_time > decision_time")
    return out


def required_5m_history_candles(history_candles: int, timeframes: Iterable[str]) -> int:
    """Expand a per-TF history budget into the needed 5m lookback length."""
    multipliers = [BARS_PER_AGGREGATE[str(tf).strip().lower()] for tf in timeframes]
    return int(max(1, int(history_candles)) * max(multipliers or [1]))
