"""Read-only MySQL candle loader for fractal wave analysis."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from orderbook_analyse.fractal_cycle_wave_analysis import ALL_TFS, EXCHANGE
from orderbook_analyse.trend_scanner_mysql_feather_parity.load import (
    DEFAULT_ENV_FILE,
    _engine,
    load_env_file,
)


def load_mysql_ohlcv_tf(
    *,
    symbol: str,
    timeframe: str,
    exchange: str = EXCHANGE,
    env_file: Path = DEFAULT_ENV_FILE,
) -> pd.DataFrame:
    """Load closed candles; ``timestamp`` = UTC open, ``available_at`` = close_time."""
    load_env_file(env_file)
    from sqlalchemy import text

    eng = _engine()
    # BINARY timeframe: distinguish ``1m`` vs ``1M`` under case-sensitive identity.
    sql = text(
        """
        SELECT open_time, open, high, low, close, volume, close_time
        FROM market_candles
        WHERE exchange = :exchange
          AND symbol = :symbol
          AND timeframe = BINARY :timeframe
          AND is_closed = 1
        ORDER BY open_time
        """
    )
    print(f"[load] {symbol} {timeframe} …", flush=True)
    with eng.connect() as conn:
        df = pd.read_sql(
            sql,
            conn,
            params={"exchange": exchange, "symbol": symbol, "timeframe": timeframe},
        )
    if df.empty:
        print(f"[load] {symbol} {timeframe}: empty", flush=True)
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

    df = df.rename(columns={"open_time": "timestamp"})
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df["close_time"] = pd.to_datetime(df["close_time"], utc=True)
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.sort_values("timestamp").drop_duplicates("timestamp").reset_index(drop=True)
    df["available_at"] = df["close_time"]
    print(f"[load] {symbol} {timeframe}: n={len(df)}", flush=True)
    return df


def coverage_audit(
    *,
    symbol: str,
    exchange: str = EXCHANGE,
    env_file: Path = DEFAULT_ENV_FILE,
    timeframes: tuple[str, ...] = ALL_TFS,
) -> list[dict[str, Any]]:
    load_env_file(env_file)
    from sqlalchemy import text

    eng = _engine()
    out: list[dict[str, Any]] = []
    sql = text(
        """
        SELECT COUNT(*) AS n,
               MIN(open_time) AS min_ot,
               MAX(open_time) AS max_ot,
               SUM(is_closed) AS closed_n
        FROM market_candles
        WHERE exchange = :exchange
          AND symbol = :symbol
          AND timeframe = BINARY :timeframe
        """
    )
    with eng.connect() as conn:
        for tf in timeframes:
            r = conn.execute(
                sql, {"exchange": exchange, "symbol": symbol, "timeframe": tf}
            ).mappings().one()
            out.append(
                {
                    "symbol": symbol,
                    "timeframe": tf,
                    "n": int(r["n"] or 0),
                    "closed_n": int(r["closed_n"] or 0),
                    "min_open": None if r["min_ot"] is None else str(r["min_ot"]),
                    "max_open": None if r["max_ot"] is None else str(r["max_ot"]),
                    "present": int(r["n"] or 0) > 0,
                }
            )
    return out


def full_stack_window(coverage: list[dict[str, Any]], tfs: tuple[str, ...] = ALL_TFS) -> dict[str, Any]:
    by_tf = {c["timeframe"]: c for c in coverage if c.get("present")}
    missing = [tf for tf in tfs if tf not in by_tf]
    if missing:
        return {"ok": False, "missing": missing, "start": None, "end": None}
    starts = [pd.Timestamp(by_tf[tf]["min_open"], tz="UTC") for tf in tfs]
    ends = [pd.Timestamp(by_tf[tf]["max_open"], tz="UTC") for tf in tfs]
    start, end = max(starts), min(ends)
    return {
        "ok": bool(start <= end),
        "missing": [],
        "start": start.isoformat(),
        "end": end.isoformat(),
        "empty": bool(start > end),
    }
