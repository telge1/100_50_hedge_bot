"""Thin causal candle loader wrapper around research.backtests.candle_loader."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from research.backtests.candle_loader import (
    DEFAULT_DATA_DIR,
    load_candles_for_symbol,
    symbol_to_feather_name,
)

from .config import RegimeScannerConfig, default_regime_scanner_config

REQUIRED_COLUMNS = ("timestamp", "open", "high", "low", "close", "volume")


class CandleDataError(ValueError):
    """Raised when candle data fails causal / integrity validation."""


def _to_utc_timestamp(value: datetime | pd.Timestamp | str) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    return ts


def candles_to_dataframe(rows: list[dict[str, Any]]) -> pd.DataFrame:
    """Normalize loader rows to a typed OHLCV DataFrame."""
    if not rows:
        return pd.DataFrame(columns=list(REQUIRED_COLUMNS))

    frame = pd.DataFrame(rows)
    if "timestamp" not in frame.columns and "date" in frame.columns:
        frame = frame.rename(columns={"date": "timestamp"})

    missing = [col for col in REQUIRED_COLUMNS if col not in frame.columns]
    if missing:
        raise CandleDataError(f"candle rows missing required columns: {missing}")

    frame = frame.loc[:, list(REQUIRED_COLUMNS)].copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    for col in ("open", "high", "low", "close", "volume"):
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    return frame


def validate_candle_dataframe(
    df: pd.DataFrame,
    *,
    interval_minutes: int = 5,
) -> pd.DataFrame:
    """Validate sort order, uniqueness, and 5m spacing."""
    if df.empty:
        return df.reset_index(drop=True)

    missing = [col for col in REQUIRED_COLUMNS if col not in df.columns]
    if missing:
        raise CandleDataError(f"candle frame missing required columns: {missing}")

    out = df.loc[:, list(REQUIRED_COLUMNS)].copy()
    out["timestamp"] = pd.to_datetime(out["timestamp"], utc=True)

    dup_count = int(out["timestamp"].duplicated().sum())
    if dup_count:
        raise CandleDataError(f"candle timestamps contain {dup_count} duplicate(s)")

    if not bool(out["timestamp"].is_monotonic_increasing):
        raise CandleDataError("candle timestamps are not strictly ascending")

    if len(out) >= 2:
        expected = pd.Timedelta(minutes=int(interval_minutes))
        deltas = out["timestamp"].diff().iloc[1:]
        bad = deltas[deltas != expected]
        if not bad.empty:
            sample = bad.iloc[0]
            raise CandleDataError(
                f"candle spacing is not a uniform {interval_minutes}m grid "
                f"(found delta={sample} at index {bad.index[0]})"
            )

    return out.reset_index(drop=True)


def load_symbol_candles(
    symbol: str = "APTUSDT",
    *,
    data_dir: str | Path | None = None,
    limit: int | None = None,
    config: RegimeScannerConfig | None = None,
) -> pd.DataFrame:
    """Load full (or tail-limited) symbol candles via the existing feather loader."""
    cfg = config or default_regime_scanner_config()
    resolved_dir = Path(data_dir) if data_dir is not None else DEFAULT_DATA_DIR
    rows = load_candles_for_symbol(
        symbol=symbol,
        timeframe="5m",
        data_dir=resolved_dir,
        limit=limit,
    )
    frame = candles_to_dataframe(rows)
    return validate_candle_dataframe(
        frame,
        interval_minutes=cfg.candle_interval_minutes,
    )


def load_closed_candles_as_of(
    symbol: str,
    decision_time: datetime | pd.Timestamp | str,
    *,
    data_dir: str | Path | None = None,
    limit: int | None = None,
    config: RegimeScannerConfig | None = None,
) -> pd.DataFrame:
    """Return only candles with ``timestamp < decision_time`` (strict).

    For decision mode candle-open at ``T``, the candle that opens at ``T`` is
    excluded. Example: decision ``2026-01-13T23:00:00Z`` keeps last open
    ``2026-01-13T22:55:00Z``.
    """
    cfg = config or default_regime_scanner_config()
    decision_ts = _to_utc_timestamp(decision_time)
    frame = load_symbol_candles(
        symbol,
        data_dir=data_dir,
        limit=limit,
        config=cfg,
    )
    closed = frame.loc[frame["timestamp"] < decision_ts].copy()
    return closed.reset_index(drop=True)


def feather_path_for_symbol(
    symbol: str,
    *,
    data_dir: str | Path | None = None,
) -> Path:
    resolved_dir = Path(data_dir) if data_dir is not None else DEFAULT_DATA_DIR
    return resolved_dir / symbol_to_feather_name(symbol, timeframe="5m")
