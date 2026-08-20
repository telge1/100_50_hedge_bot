"""Read-only ClickHouse loaders for market-event reports (SELECT only)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd

from orderbook_analyse.orderbook_v2.ch_client import get_clickhouse_client

QSET = {
    "max_execution_time": 300,
    "receive_timeout": 320,
    "max_memory_usage": 4_000_000_000,
}

SIDE_SEMANTICS = {
    "Buy": "taker/aggressor buy",
    "Sell": "taker/aggressor sell",
    "source": "orderbook_analysis.public_trades_canonical",
}


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


def parse_event_time(raw: str) -> datetime:
    s = raw.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    dt = as_utc(dt)
    # Floor to minute
    return dt.replace(second=0, microsecond=0)


def q(client: Any, sql: str, params: dict[str, Any] | None = None) -> list[tuple]:
    return client.query(sql, parameters=params or {}, settings=QSET).result_rows


def fetch_candles_1m(
    client: Any,
    symbol: str,
    start: datetime,
    end_inclusive: datetime,
) -> pd.DataFrame:
    rows = q(
        client,
        """
        SELECT open_time, open, high, low, close, volume
        FROM signal_generator.candles_1m FINAL
        WHERE symbol = {s:String} AND interval = '1m'
          AND open_time >= {a:DateTime64(3,'UTC')}
          AND open_time <= {b:DateTime64(3,'UTC')}
        ORDER BY open_time
        """,
        {"s": symbol, "a": as_utc(start), "b": as_utc(end_inclusive)},
    )
    df = pd.DataFrame(rows, columns=["open_time", "open", "high", "low", "close", "volume"])
    if df.empty:
        return df
    df["open_time"] = df["open_time"].map(to_naive_utc)
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def fetch_trades_1m(
    client: Any,
    symbol: str,
    start: datetime,
    end_exclusive: datetime,
) -> pd.DataFrame:
    rows = q(
        client,
        """
        SELECT
          toStartOfMinute(trade_ts) AS minute,
          count() AS trade_count,
          sum(size) AS total_volume,
          sumIf(size, side = 'Buy') AS aggressive_buy_volume,
          sumIf(size, side = 'Sell') AS aggressive_sell_volume,
          sumIf(size, side = 'Buy') - sumIf(size, side = 'Sell') AS trade_delta
        FROM orderbook_analysis.public_trades_canonical
        WHERE symbol = {s:String}
          AND trade_ts >= {a:DateTime64(3,'UTC')}
          AND trade_ts <  {b:DateTime64(3,'UTC')}
        GROUP BY minute
        ORDER BY minute
        """,
        {"s": symbol, "a": as_utc(start), "b": as_utc(end_exclusive)},
    )
    cols = [
        "minute",
        "trade_count",
        "total_volume",
        "aggressive_buy_volume",
        "aggressive_sell_volume",
        "trade_delta",
    ]
    df = pd.DataFrame(rows, columns=cols)
    if df.empty:
        df["tps"] = []
        df["delta_ratio"] = []
        return df
    df["minute"] = df["minute"].map(to_naive_utc)
    for c in cols[1:]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["tps"] = df["trade_count"] / 60.0
    denom = df["aggressive_buy_volume"] + df["aggressive_sell_volume"]
    df["delta_ratio"] = 0.0
    mask = denom > 0
    df.loc[mask, "delta_ratio"] = df.loc[mask, "trade_delta"] / denom.loc[mask]
    return df


def fetch_orderbook_1m(
    client: Any,
    symbol: str,
    start: datetime,
    end_exclusive: datetime,
) -> pd.DataFrame:
    rows = q(
        client,
        """
        SELECT
          toStartOfMinute(bucket_start) AS minute,
          count() AS seconds,
          countIf(is_valid = 1) AS valid_seconds,
          countIf(is_valid = 0) AS invalid_seconds,
          countIf(quality_flags = 'carried_forward') AS carried_forward_seconds,
          avgIf(spread_bps, is_valid = 1) AS spread_bps,
          avgIf(imbalance_l10, is_valid = 1) AS imbalance_l10,
          avgIf(imbalance_l50, is_valid = 1) AS imbalance_l50,
          avgIf(bid_qty_l50, is_valid = 1) AS bid_depth_l50,
          avgIf(ask_qty_l50, is_valid = 1) AS ask_depth_l50,
          sumIf(ofi, is_valid = 1 AND ofi IS NOT NULL) AS ofi
        FROM orderbook_analysis.orderbook_features_1s_v2 FINAL
        WHERE symbol = {s:String}
          AND parser_version = 'ob200_v3'
          AND depth = 200
          AND bucket_start >= {a:DateTime64(3,'UTC')}
          AND bucket_start <  {b:DateTime64(3,'UTC')}
        GROUP BY minute
        ORDER BY minute
        """,
        {"s": symbol, "a": as_utc(start), "b": as_utc(end_exclusive)},
    )
    cols = [
        "minute",
        "seconds",
        "valid_seconds",
        "invalid_seconds",
        "carried_forward_seconds",
        "spread_bps",
        "imbalance_l10",
        "imbalance_l50",
        "bid_depth_l50",
        "ask_depth_l50",
        "ofi",
    ]
    df = pd.DataFrame(rows, columns=cols)
    if df.empty:
        return df
    df["minute"] = df["minute"].map(to_naive_utc)
    for c in cols[1:]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    # Rolling OFI windows (causal: include current minute in sum of last N)
    df["ofi_1m"] = df["ofi"]
    df["ofi_5m"] = df["ofi"].rolling(5, min_periods=1).sum()
    df["ofi_15m"] = df["ofi"].rolling(15, min_periods=1).sum()
    return df


def fetch_oi_liq_optional(
    client: Any,
    symbol: str,
    start: datetime,
    end_exclusive: datetime,
) -> dict[str, Any]:
    """Probe OI/liq tables; mark unavailable when empty or missing."""
    out: dict[str, Any] = {
        "available": False,
        "reason": None,
        "open_interest_5m": [],
        "liquidations": [],
    }
    try:
        oi_rows = q(
            client,
            """
            SELECT
              bucket_time,
              argMax(open_interest, inserted_at) AS open_interest,
              argMax(open_interest_value, inserted_at) AS open_interest_value
            FROM orderbook_analysis.open_interest_5m_history
            WHERE symbol = {s:String}
              AND bucket_time >= {a:DateTime64(3,'UTC')}
              AND bucket_time <  {b:DateTime64(3,'UTC')}
            GROUP BY bucket_time
            ORDER BY bucket_time
            """,
            {"s": symbol, "a": as_utc(start), "b": as_utc(end_exclusive)},
        )
        liq_rows = q(
            client,
            """
            SELECT
              event_time,
              liquidated_position_side,
              size,
              bankruptcy_price,
              notional_estimate
            FROM orderbook_analysis.all_liquidations
            WHERE symbol = {s:String}
              AND event_time >= {a:DateTime64(3,'UTC')}
              AND event_time <  {b:DateTime64(3,'UTC')}
            ORDER BY event_time
            LIMIT 5000
            """,
            {"s": symbol, "a": as_utc(start), "b": as_utc(end_exclusive)},
        )
    except Exception as exc:  # noqa: BLE001
        out["reason"] = f"query_failed:{type(exc).__name__}:{exc}"
        return out

    oi = [
        {
            "bucket_time": str(to_naive_utc(r[0])),
            "open_interest": float(r[1]) if r[1] is not None else None,
            "open_interest_value": float(r[2]) if r[2] is not None else None,
        }
        for r in oi_rows
    ]
    liq = [
        {
            "event_time": str(to_naive_utc(r[0])),
            "liquidated_position_side": r[1],
            "size": float(r[2]) if r[2] is not None else None,
            "bankruptcy_price": float(r[3]) if r[3] is not None else None,
            "notional_estimate": float(r[4]) if r[4] is not None else None,
        }
        for r in liq_rows
    ]
    if not oi and not liq:
        out["reason"] = "no_oi_or_liq_rows_in_window"
        return out
    out["available"] = True
    out["reason"] = None
    out["open_interest_5m"] = oi
    out["liquidations"] = liq
    out["n_oi_buckets"] = len(oi)
    out["n_liquidations"] = len(liq)
    return out


def default_fetch_window(event_t: datetime) -> tuple[datetime, datetime, datetime]:
    """Return (warmup_start, event_t, end_inclusive) for report fetches."""
    warmup = event_t - timedelta(days=2)
    end_incl = event_t + timedelta(minutes=240)
    return warmup, event_t, end_incl
