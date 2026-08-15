"""Read-only join of market_candles 5m + curated derivatives_5m_v1."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import numpy as np
import pandas as pd

from research.regime_scanner.derivatives.config import load_target_config
from research.regime_scanner.derivatives.store_mysql import MySQLDerivativeStore
from research.regime_scanner.liquidation_exhaustion.config import (
    IMPORT_VERSION_DEFAULT,
    KNOWN_OUTAGE,
    UNAVAILABLE_SYMBOLS,
)

logger = logging.getLogger(__name__)


def _utc(ts: Any) -> pd.Timestamp:
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        return t.tz_localize("UTC")
    return t.tz_convert("UTC")


def _naive(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts
    return ts.astimezone(timezone.utc).replace(tzinfo=None)


def validate_symbols(symbols: list[str]) -> list[str]:
    out: list[str] = []
    for s in symbols:
        u = str(s).strip().upper()
        if u in UNAVAILABLE_SYMBOLS:
            raise ValueError(f"symbol {u} has no derivative data (known unavailable)")
        if not u.endswith("USDT"):
            raise ValueError(f"invalid symbol: {u}")
        out.append(u)
    if not out:
        raise ValueError("empty symbol list")
    return out


def load_joined_5m(
    *,
    symbols: list[str],
    start: datetime,
    end: datetime,
    import_version: str = IMPORT_VERSION_DEFAULT,
    exchange: str = "bybit",
) -> pd.DataFrame:
    """Inner-join OHLCV 5m with derivative buckets on (symbol, bucket_start).

    Rules:
    - import_version exact match
    - data_available must be true
    - one row per (symbol, bucket_start)
    - no forward-fill of missing derivative buckets
    """
    symbols = validate_symbols(symbols)
    cfg = load_target_config()
    store = MySQLDerivativeStore(cfg)
    try:
        deriv_rows = store.get_buckets(
            symbols=symbols,
            import_version=import_version,
            start=start,
            end=end,
        )
    finally:
        store.close()

    if not deriv_rows:
        return pd.DataFrame()

    ddf = pd.DataFrame(deriv_rows)
    ddf["bucket_start"] = pd.to_datetime(ddf["bucket_start"], utc=True)
    ddf = ddf[ddf["data_available"].astype(bool)].copy()
    ddf = ddf.drop_duplicates(subset=["symbol", "bucket_start", "import_version"], keep="last")

    # OHLCV via store helper
    ohlcv_map = store_fetch_ohlcv(cfg, symbols=symbols, start=start, end=end, exchange=exchange)
    if ohlcv_map.empty:
        return pd.DataFrame()

    joined = ddf.merge(
        ohlcv_map,
        left_on=["symbol", "bucket_start"],
        right_on=["symbol", "open_time"],
        how="inner",
        validate="one_to_one",
    )
    joined = joined.sort_values(["symbol", "bucket_start"]).reset_index(drop=True)
    joined["timestamp"] = joined["bucket_start"]
    return joined


def store_fetch_ohlcv(
    cfg: Any,
    *,
    symbols: list[str],
    start: datetime,
    end: datetime,
    exchange: str = "bybit",
) -> pd.DataFrame:
    from sqlalchemy import bindparam, create_engine, text

    eng = create_engine(cfg.sqlalchemy_url, pool_pre_ping=True, future=True)
    sql = text(
        """
        SELECT symbol, open_time, close_time, open, high, low, close, volume
        FROM market_candles
        WHERE exchange = :exchange AND timeframe = '5m'
          AND symbol IN :symbols
          AND open_time >= :start_ts AND open_time < :end_ts
        ORDER BY symbol, open_time
        """
    ).bindparams(bindparam("symbols", expanding=True))
    try:
        with eng.connect() as conn:
            rows = conn.execute(
                sql,
                {
                    "exchange": exchange,
                    "symbols": tuple(symbols),
                    "start_ts": _naive(start),
                    "end_ts": _naive(end),
                },
            ).mappings().all()
    finally:
        eng.dispose()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame([dict(r) for r in rows])
    df["open_time"] = pd.to_datetime(df["open_time"], utc=True)
    df = df.drop_duplicates(subset=["symbol", "open_time"], keep="last")
    return df


def coverage_report(joined: pd.DataFrame, deriv_n: int, ohlcv_n: int) -> dict[str, Any]:
    return {
        "joined_rows": int(len(joined)),
        "derivative_available_rows": int(deriv_n),
        "ohlcv_rows": int(ohlcv_n),
        "symbols": sorted(joined["symbol"].unique().tolist()) if len(joined) else [],
    }


def mark_known_outage(ts: pd.Series) -> pd.Series:
    a = pd.Timestamp(KNOWN_OUTAGE[0])
    b = pd.Timestamp(KNOWN_OUTAGE[1])
    t = pd.to_datetime(ts, utc=True)
    return (t >= a) & (t < b)
