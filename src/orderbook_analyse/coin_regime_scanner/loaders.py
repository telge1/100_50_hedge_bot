"""Read-only ClickHouse loaders for COIN_REGIME_SCANNER_V1 (SELECT only)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

import pandas as pd

from orderbook_analyse.orderbook_v2.ch_client import get_clickhouse_client

from .config import MARKET_ANCHOR, QSET


def as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_naive_utc(value: Any) -> Any:
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(timezone.utc).replace(tzinfo=None)
        return value
    return value


def parse_as_of(raw: str) -> datetime:
    s = raw.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    dt = as_utc(dt)
    return dt.replace(second=0, microsecond=0)


def q(client: Any, sql: str, params: dict[str, Any] | None = None) -> list[tuple]:
    return client.query(sql, parameters=params or {}, settings=QSET).result_rows


def get_client() -> Any:
    return get_clickhouse_client()


def fetch_max_candle_times(client: Any, symbols: Iterable[str]) -> dict[str, datetime]:
    syms = list(symbols)
    if not syms:
        return {}
    rows = q(
        client,
        """
        SELECT symbol, max(open_time) AS mx
        FROM signal_generator.candles_1m FINAL
        WHERE interval = '1m'
          AND symbol IN {syms:Array(String)}
        GROUP BY symbol
        """,
        {"syms": syms},
    )
    out: dict[str, datetime] = {}
    for sym, mx in rows:
        out[str(sym)] = as_utc(mx) if isinstance(mx, datetime) else mx
    return out


def resolve_as_of(
    client: Any,
    symbols: list[str],
    as_of: datetime | None,
) -> datetime:
    """Default: latest common closed 1m across universe (+ BTC if needed)."""
    if as_of is not None:
        return as_utc(as_of).replace(second=0, microsecond=0)
    need = list(dict.fromkeys([*symbols, MARKET_ANCHOR]))
    mx = fetch_max_candle_times(client, need)
    missing = [s for s in need if s not in mx]
    if missing:
        raise RuntimeError(f"missing candle max for symbols: {missing[:10]}")
    common = min(mx[s] for s in need)
    return as_utc(common).replace(second=0, microsecond=0)


def fetch_candles_1m_batch(
    client: Any,
    symbols: list[str],
    start: datetime,
    end_inclusive: datetime,
) -> pd.DataFrame:
    rows = q(
        client,
        """
        SELECT symbol, open_time, open, high, low, close, volume
        FROM signal_generator.candles_1m FINAL
        WHERE interval = '1m'
          AND symbol IN {syms:Array(String)}
          AND open_time >= {a:DateTime64(3,'UTC')}
          AND open_time <= {b:DateTime64(3,'UTC')}
        ORDER BY symbol, open_time
        """,
        {"syms": symbols, "a": as_utc(start), "b": as_utc(end_inclusive)},
    )
    df = pd.DataFrame(
        rows, columns=["symbol", "open_time", "open", "high", "low", "close", "volume"]
    )
    if df.empty:
        return df
    df["open_time"] = df["open_time"].map(to_naive_utc)
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def fetch_trades_1m_batch(
    client: Any,
    symbols: list[str],
    start: datetime,
    end_exclusive: datetime,
) -> pd.DataFrame:
    rows = q(
        client,
        """
        SELECT
          symbol,
          toStartOfMinute(trade_ts) AS minute,
          count() AS trade_count,
          sum(size) AS total_volume,
          sumIf(size, side = 'Buy') AS aggressive_buy_volume,
          sumIf(size, side = 'Sell') AS aggressive_sell_volume,
          sumIf(size, side = 'Buy') - sumIf(size, side = 'Sell') AS trade_delta
        FROM orderbook_analysis.public_trades_canonical
        WHERE symbol IN {syms:Array(String)}
          AND trade_ts >= {a:DateTime64(3,'UTC')}
          AND trade_ts <  {b:DateTime64(3,'UTC')}
        GROUP BY symbol, minute
        ORDER BY symbol, minute
        """,
        {"syms": symbols, "a": as_utc(start), "b": as_utc(end_exclusive)},
    )
    cols = [
        "symbol",
        "minute",
        "trade_count",
        "total_volume",
        "aggressive_buy_volume",
        "aggressive_sell_volume",
        "trade_delta",
    ]
    df = pd.DataFrame(rows, columns=cols)
    if df.empty:
        return df
    df["minute"] = df["minute"].map(to_naive_utc)
    for c in cols[2:]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["tps"] = df["trade_count"] / 60.0
    denom = df["aggressive_buy_volume"] + df["aggressive_sell_volume"]
    df["delta_ratio"] = 0.0
    mask = denom > 0
    df.loc[mask, "delta_ratio"] = df.loc[mask, "trade_delta"] / denom.loc[mask]
    return df


def fetch_orderbook_1m_batch(
    client: Any,
    symbols: list[str],
    start: datetime,
    end_exclusive: datetime,
) -> pd.DataFrame:
    rows = q(
        client,
        """
        SELECT
          symbol,
          toStartOfMinute(bucket_start) AS minute,
          count() AS seconds,
          countIf(is_valid = 1) AS valid_seconds,
          avgIf(spread_bps, is_valid = 1) AS spread_bps,
          avgIf(imbalance_l50, is_valid = 1) AS imbalance_l50,
          avgIf(bid_qty_l50, is_valid = 1) AS bid_depth_l50,
          avgIf(ask_qty_l50, is_valid = 1) AS ask_depth_l50,
          sumIf(ofi, is_valid = 1 AND ofi IS NOT NULL) AS ofi
        FROM orderbook_analysis.orderbook_features_1s_v2 FINAL
        WHERE symbol IN {syms:Array(String)}
          AND parser_version = 'ob200_v3'
          AND depth = 200
          AND bucket_start >= {a:DateTime64(3,'UTC')}
          AND bucket_start <  {b:DateTime64(3,'UTC')}
        GROUP BY symbol, minute
        ORDER BY symbol, minute
        """,
        {"syms": symbols, "a": as_utc(start), "b": as_utc(end_exclusive)},
    )
    cols = [
        "symbol",
        "minute",
        "seconds",
        "valid_seconds",
        "spread_bps",
        "imbalance_l50",
        "bid_depth_l50",
        "ask_depth_l50",
        "ofi",
    ]
    df = pd.DataFrame(rows, columns=cols)
    if df.empty:
        return df
    df["minute"] = df["minute"].map(to_naive_utc)
    for c in cols[2:]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    # causal ofi_5m per symbol
    parts = []
    for sym, g in df.groupby("symbol", sort=False):
        g = g.sort_values("minute").copy()
        g["ofi_5m"] = g["ofi"].rolling(5, min_periods=1).sum()
        parts.append(g)
    return pd.concat(parts, ignore_index=True)


def warmup_start(as_of: datetime, hours: int) -> datetime:
    return as_utc(as_of) - timedelta(hours=hours)


def end_exclusive(as_of: datetime) -> datetime:
    """Trades/OB end exclusive = as_of + 1m (include the as_of minute)."""
    return as_utc(as_of) + timedelta(minutes=1)
