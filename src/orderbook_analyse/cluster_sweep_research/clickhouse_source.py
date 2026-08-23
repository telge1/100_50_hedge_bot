"""Read-only ClickHouse loaders + 15m aggregation from 1m candles."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd

from orderbook_analyse.orderbook_v2.ch_client import get_clickhouse_client

QSET = {"max_execution_time": 180, "receive_timeout": 200}


def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _q(client, sql: str, params: dict | None = None):
    return client.query(sql, parameters=params or {}, settings=QSET).result_rows


def fetch_candles_1m(client, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
    rows = _q(
        client,
        """
        SELECT open_time, open, high, low, close, volume
        FROM signal_generator.candles_1m FINAL
        WHERE symbol={s:String} AND interval='1m'
          AND open_time>={a:DateTime64(3,'UTC')} AND open_time<{b:DateTime64(3,'UTC')}
        ORDER BY open_time
        """,
        {"s": symbol, "a": _as_utc(start), "b": _as_utc(end)},
    )
    df = pd.DataFrame(rows, columns=["open_time", "open", "high", "low", "close", "volume"])
    if df.empty:
        return df
    df["open_time"] = pd.to_datetime(df["open_time"])
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def _timeframe_minutes(timeframe: str) -> int:
    """Parse signal timeframe into minutes. Supports Nm and Nh (research + production)."""
    tf = str(timeframe).strip().lower()
    if tf.endswith("m") and not tf.endswith("min"):
        return int(tf[:-1])
    if tf.endswith("h"):
        return int(tf[:-1]) * 60
    if tf.endswith("min"):
        return int(tf[:-3])
    raise ValueError(f"unsupported timeframe: {timeframe}")


def aggregate_timeframe(df_1m: pd.DataFrame, timeframe: str = "15m") -> pd.DataFrame:
    """Deterministic closed buckets from 1m (left-labeled). Drop incomplete last bucket."""
    if df_1m.empty:
        return df_1m
    minutes = _timeframe_minutes(timeframe)
    # pandas offset aliases: "5min"/"15min" for minutes; "1h"/"4h" for whole hours
    if minutes >= 60 and minutes % 60 == 0:
        rule = f"{minutes // 60}h"
    else:
        rule = f"{minutes}min"
    g = (
        df_1m.set_index("open_time")
        .resample(rule, label="left", closed="left")
        .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
        .dropna(subset=["open", "close"])
    )
    # drop last bucket if incomplete relative to expected minutes
    expected = minutes
    counts = df_1m.set_index("open_time").resample(rule, label="left", closed="left").size()
    complete = counts[counts >= expected].index
    g = g.loc[g.index.isin(complete)]
    out = g.reset_index().rename(columns={"index": "open_time"})
    if "open_time" not in out.columns:
        out = g.reset_index()
        out = out.rename(columns={out.columns[0]: "open_time"})
    return out


def coverage_report(client, symbol: str, start: datetime, end: datetime) -> dict[str, Any]:
    s, e = _as_utc(start), _as_utc(end)
    p = {"s": symbol, "a": s, "b": e}
    out: dict[str, Any] = {"symbol": symbol, "start": s.isoformat(), "end": e.isoformat(), "timezone": "UTC"}

    def one(name: str, sql: str) -> None:
        mn, mx, n = _q(client, sql, p)[0]
        n = int(n)
        out[name] = {
            "first_ts": None if n == 0 else str(mn),
            "last_ts": None if n == 0 else str(mx),
            "row_count": n,
            "status": "MISSING" if n == 0 else "VALID",
        }

    one(
        "candles_1m",
        """SELECT min(open_time),max(open_time),count() FROM signal_generator.candles_1m FINAL
           WHERE symbol={s:String} AND interval='1m'
             AND open_time>={a:DateTime64(3,'UTC')} AND open_time<{b:DateTime64(3,'UTC')}""",
    )
    one(
        "public_trades",
        """SELECT min(trade_ts),max(trade_ts),count() FROM orderbook_analysis.public_trades_canonical
           WHERE symbol={s:String}
             AND trade_ts>={a:DateTime64(3,'UTC')} AND trade_ts<{b:DateTime64(3,'UTC')}""",
    )
    one(
        "ob200_v3",
        """SELECT min(bucket_start),max(bucket_start),count() FROM orderbook_analysis.orderbook_features_1s_v2 FINAL
           WHERE symbol={s:String} AND parser_version='ob200_v3' AND depth=200
             AND bucket_start>={a:DateTime64(3,'UTC')} AND bucket_start<{b:DateTime64(3,'UTC')}""",
    )
    one(
        "open_interest_5s",
        """SELECT min(bucket_time),max(bucket_time),count() FROM orderbook_analysis.open_interest_5s
           WHERE symbol={s:String}
             AND bucket_time>={a:DateTime64(3,'UTC')} AND bucket_time<{b:DateTime64(3,'UTC')}""",
    )
    one(
        "liquidations",
        """SELECT min(event_time),max(event_time),count() FROM orderbook_analysis.all_liquidations
           WHERE symbol={s:String}
             AND event_time>={a:DateTime64(3,'UTC')} AND event_time<{b:DateTime64(3,'UTC')}""",
    )
    if out["liquidations"]["row_count"] == 0:
        out["liquidations"]["status"] = "EMPTY_OR_MISSING"
        out["liquidations"]["note"] = "Do not interpret as market had zero liquidations"
    return out


def fetch_trades_1m(client, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
    rows = _q(
        client,
        """
        SELECT toStartOfMinute(trade_ts) AS minute,
               count() AS trade_count,
               sum(if(side='Buy', toFloat64(size)*toFloat64(price), 0.)) AS buy_notional,
               sum(if(side='Sell', toFloat64(size)*toFloat64(price), 0.)) AS sell_notional
        FROM orderbook_analysis.public_trades_canonical
        WHERE symbol={s:String}
          AND trade_ts>={a:DateTime64(3,'UTC')} AND trade_ts<{b:DateTime64(3,'UTC')}
        GROUP BY minute ORDER BY minute
        """,
        {"s": symbol, "a": _as_utc(start), "b": _as_utc(end)},
    )
    df = pd.DataFrame(rows, columns=["minute", "trade_count", "buy_notional", "sell_notional"])
    if not df.empty:
        df["minute"] = pd.to_datetime(df["minute"])
    return df


def fetch_ob_1m(client, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
    """1m aggregates from ob200_v3; missing minutes stay absent (no fill)."""
    rows = _q(
        client,
        """
        SELECT
          toStartOfMinute(bucket_start) AS minute,
          count() AS n_buckets,
          avgIf(spread_bps, is_valid = 1) AS spread_bps,
          avgIf(imbalance_l50, is_valid = 1) AS imbalance_l50,
          avgIf(bid_qty_l50, is_valid = 1) AS bid_depth_l50,
          avgIf(ask_qty_l50, is_valid = 1) AS ask_depth_l50
        FROM orderbook_analysis.orderbook_features_1s_v2 FINAL
        WHERE symbol={s:String}
          AND parser_version='ob200_v3' AND depth=200
          AND bucket_start>={a:DateTime64(3,'UTC')} AND bucket_start<{b:DateTime64(3,'UTC')}
        GROUP BY minute
        ORDER BY minute
        """,
        {"s": symbol, "a": _as_utc(start), "b": _as_utc(end)},
    )
    cols = ["minute", "n_buckets", "spread_bps", "imbalance_l50", "bid_depth_l50", "ask_depth_l50"]
    df = pd.DataFrame(rows, columns=cols)
    if not df.empty:
        df["minute"] = pd.to_datetime(df["minute"])
    return df


def fetch_oi_1m(client, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
    rows = _q(
        client,
        """
        SELECT
          toStartOfMinute(bucket_time) AS minute,
          argMax(open_interest, bucket_time) AS open_interest
        FROM orderbook_analysis.open_interest_5s
        WHERE symbol={s:String}
          AND bucket_time>={a:DateTime64(3,'UTC')} AND bucket_time<{b:DateTime64(3,'UTC')}
        GROUP BY minute
        ORDER BY minute
        """,
        {"s": symbol, "a": _as_utc(start), "b": _as_utc(end)},
    )
    df = pd.DataFrame(rows, columns=["minute", "open_interest"])
    if not df.empty:
        df["minute"] = pd.to_datetime(df["minute"])
        df["open_interest"] = pd.to_numeric(df["open_interest"], errors="coerce")
    return df


def fetch_liquidations(client, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
    rows = _q(
        client,
        """
        SELECT
          event_time,
          liquidated_position_side AS side,
          toFloat64(notional_estimate) AS notional
        FROM orderbook_analysis.all_liquidations
        WHERE symbol={s:String}
          AND event_time>={a:DateTime64(3,'UTC')} AND event_time<{b:DateTime64(3,'UTC')}
        ORDER BY event_time
        """,
        {"s": symbol, "a": _as_utc(start), "b": _as_utc(end)},
    )
    df = pd.DataFrame(rows, columns=["event_time", "side", "notional"])
    if not df.empty:
        df["event_time"] = pd.to_datetime(df["event_time"])
        df["notional"] = pd.to_numeric(df["notional"], errors="coerce")
    return df


def default_client():
    return get_clickhouse_client()
