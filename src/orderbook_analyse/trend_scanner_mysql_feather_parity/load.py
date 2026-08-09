"""Thin read-only MySQL 5m loader → scanner OHLCV schema."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pandas as pd

DEFAULT_ENV_FILE = Path(
    "/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/"
    "research/regime_scanner/.env.regime_db"
)
DEFAULT_FEATHER_DIR = Path(
    "/home/telgenbuescher/projects/Signal_Generator_Ralf/data/bybit/futures"
)
APT_FEATHER = "APT_USDT_USDT-5m-futures.feather"
PRICE_ATOL = 1e-10
VOLUME_ATOL = 1e-8
LEVEL_ATOL = 1e-8


def load_env_file(path: Path = DEFAULT_ENV_FILE) -> dict[str, str]:
    """Load KEY=VALUE lines into os.environ (no overwrite of existing)."""
    loaded: dict[str, str] = {}
    if not path.exists():
        raise FileNotFoundError(path)
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip("'").strip('"')
        if k and k not in os.environ:
            os.environ[k] = v
        loaded[k] = v
    return loaded


def _engine():
    from sqlalchemy import create_engine
    from urllib.parse import quote_plus

    host = os.environ.get("REGIME_DB_HOST", "127.0.0.1")
    port = int(os.environ.get("REGIME_DB_PORT", "3306"))
    name = os.environ.get("REGIME_DB_NAME", "regime_scanner_research")
    user = os.environ.get("REGIME_DB_USER", "")
    password = os.environ.get("REGIME_DB_PASSWORD", "")
    if not user:
        raise RuntimeError("REGIME_DB_USER missing; source .env.regime_db first")
    url = (
        f"mysql+pymysql://{quote_plus(user)}:{quote_plus(password)}"
        f"@{host}:{port}/{name}?charset=utf8mb4"
    )
    return create_engine(url)


def load_mysql_5m_ohlcv(
    *,
    symbol: str = "APTUSDT",
    exchange: str = "bybit",
    env_file: Path = DEFAULT_ENV_FILE,
) -> pd.DataFrame:
    """Load closed 5m candles as scanner schema: timestamp/open/high/low/close/volume.

    ``timestamp`` = UTC candle **open** (same as feather ``date`` → load_ohlcv_feather).
    """
    load_env_file(env_file)
    from sqlalchemy import text

    eng = _engine()
    sql = text(
        """
        SELECT open_time, open, high, low, close, volume, close_time, is_closed
        FROM market_candles
        WHERE exchange = :exchange
          AND symbol = :symbol
          AND timeframe = '5m'
          AND is_closed = 1
        ORDER BY open_time
        """
    )
    with eng.connect() as conn:
        rows = conn.execute(sql, {"exchange": exchange, "symbol": symbol}).mappings().all()
    if not rows:
        raise RuntimeError(f"no MySQL 5m rows for {exchange}/{symbol}")

    df = pd.DataFrame(
        {
            "timestamp": pd.to_datetime([r["open_time"] for r in rows], utc=True),
            "open": pd.to_numeric([r["open"] for r in rows], errors="coerce"),
            "high": pd.to_numeric([r["high"] for r in rows], errors="coerce"),
            "low": pd.to_numeric([r["low"] for r in rows], errors="coerce"),
            "close": pd.to_numeric([r["close"] for r in rows], errors="coerce"),
            "volume": pd.to_numeric([r["volume"] for r in rows], errors="coerce"),
            "close_time": pd.to_datetime([r["close_time"] for r in rows], utc=True),
        }
    )
    df = df.sort_values("timestamp").drop_duplicates("timestamp").reset_index(drop=True)
    return df


def mysql_quality_checks(df: pd.DataFrame) -> dict[str, Any]:
    """Causal / integrity checks on loaded 5m frame."""
    ts = pd.to_datetime(df["timestamp"], utc=True)
    gaps = int((ts.diff().dropna() != pd.Timedelta(minutes=5)).sum())
    nulls = int(df[["open", "high", "low", "close", "volume"]].isna().any(axis=1).sum())
    bad_ohlc = int(
        (
            (df["high"] < df["low"])
            | (df["high"] < df["open"])
            | (df["high"] < df["close"])
            | (df["low"] > df["open"])
            | (df["low"] > df["close"])
        ).sum()
    )
    close_ok = True
    if "close_time" in df.columns:
        expected = ts + pd.Timedelta(minutes=5)
        close_ok = bool((pd.to_datetime(df["close_time"], utc=True) == expected).all())
    return {
        "n": int(len(df)),
        "start": str(ts.iloc[0]) if len(df) else None,
        "end": str(ts.iloc[-1]) if len(df) else None,
        "n_duplicate_timestamps": int(len(df) - df["timestamp"].nunique()),
        "n_gaps_5m": gaps,
        "n_null_ohlcv": nulls,
        "n_bad_ohlc": bad_ohlc,
        "close_time_equals_open_plus_5m": close_ok,
        "sorted_ascending": bool(ts.is_monotonic_increasing),
        "timestamp_is_utc_open": True,
    }


def load_feather_5m_ohlcv(
    *,
    candle_dir: Path = DEFAULT_FEATHER_DIR,
    feather_name: str = APT_FEATHER,
) -> pd.DataFrame:
    from orderbook_analyse.trend_scanner_adapter import load_ohlcv_feather

    path = Path(candle_dir) / feather_name
    return load_ohlcv_feather(path)


def clip_ohlcv(
    df: pd.DataFrame,
    *,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> pd.DataFrame:
    """Inclusive open-time window [start, end]."""
    ts = pd.to_datetime(df["timestamp"], utc=True)
    start = pd.Timestamp(start)
    end = pd.Timestamp(end)
    if start.tzinfo is None:
        start = start.tz_localize("UTC")
    else:
        start = start.tz_convert("UTC")
    if end.tzinfo is None:
        end = end.tz_localize("UTC")
    else:
        end = end.tz_convert("UTC")
    out = df.loc[(ts >= start) & (ts <= end)].copy()
    return out.sort_values("timestamp").reset_index(drop=True)


def comparison_window(
    mysql_df: pd.DataFrame,
    feather_df: pd.DataFrame,
) -> dict[str, Any]:
    m0 = pd.to_datetime(mysql_df["timestamp"], utc=True).iloc[0]
    m1 = pd.to_datetime(mysql_df["timestamp"], utc=True).iloc[-1]
    f0 = pd.to_datetime(feather_df["timestamp"], utc=True).iloc[0]
    f1 = pd.to_datetime(feather_df["timestamp"], utc=True).iloc[-1]
    start = max(m0, f0)
    end = min(m1, f1)
    if start > end:
        raise RuntimeError(f"no overlapping 5m window: mysql=[{m0},{m1}] feather=[{f0},{f1}]")
    return {
        "comparison_start": start,
        "comparison_end": end,
        "mysql_start": m0,
        "mysql_end": m1,
        "feather_start": f0,
        "feather_end": f1,
    }
