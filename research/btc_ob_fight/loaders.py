"""Read-only ClickHouse loaders for BTC OB Fight fact CLI."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import OA_SRC, utc

ROOT = Path(__file__).resolve().parents[2]


def _ensure_import_paths() -> None:
    dash = str(ROOT / "dashboard")
    oa = str(OA_SRC)
    if dash not in sys.path:
        sys.path.insert(0, dash)
    if oa not in sys.path:
        sys.path.insert(0, oa)


def clickhouse_client():
    _ensure_import_paths()
    import clickhouse_connect

    from research_charts.clickhouse_config import load_clickhouse_config

    cfg = load_clickhouse_config()
    return clickhouse_connect.get_client(**cfg.connect_kwargs())


def _dt_sql(dt: datetime) -> str:
    return utc(dt).strftime("%Y-%m-%d %H:%M:%S")


def query_rows(cl, sql: str) -> list[tuple]:
    return cl.query(sql).result_rows


def load_public_trades(
    cl,
    symbol: str,
    start: datetime,
    end: datetime,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows = query_rows(
        cl,
        f"""
        SELECT trade_ts, trade_id, side, toFloat64(price), toFloat64(size), toFloat64(notional)
        FROM orderbook_analysis.public_trades_canonical FINAL
        WHERE symbol='{symbol}'
          AND trade_ts >= toDateTime64('{_dt_sql(start)}', 3, 'UTC')
          AND trade_ts < toDateTime64('{_dt_sql(end)}', 3, 'UTC')
        ORDER BY trade_ts, trade_id
        """,
    )
    raw_count = len(rows)
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for r in rows:
        tid = str(r[1])
        if tid in seen:
            continue
        seen.add(tid)
        out.append(
            {
                "ts": utc(r[0]),
                "trade_id": tid,
                "side": str(r[2]),
                "price": float(r[3]),
                "size": float(r[4]),
                "notional": float(r[5]),
            }
        )
    meta = {
        "table": "orderbook_analysis.public_trades_canonical",
        "raw_count": raw_count,
        "deduped_count": len(out),
        "dedup_removed": raw_count - len(out),
        "aggressor_semantics": "side=Buy/Sell is Bybit taker/aggressor",
        "sort": "trade_ts, trade_id",
    }
    return out, meta


def coverage_public_trades(cl, symbol: str, start: datetime, end: datetime) -> dict[str, Any]:
    row = query_rows(
        cl,
        f"""
        SELECT count(), min(trade_ts), max(trade_ts), uniqExact(trade_id),
               countIf(side='Buy'), countIf(side='Sell'), sum(notional)
        FROM orderbook_analysis.public_trades_canonical FINAL
        WHERE symbol='{symbol}'
          AND trade_ts >= toDateTime64('{_dt_sql(start)}', 3, 'UTC')
          AND trade_ts < toDateTime64('{_dt_sql(end)}', 3, 'UTC')
        """,
    )[0]
    return {
        "table": "orderbook_analysis.public_trades_canonical",
        "count": int(row[0]),
        "min_ts": utc(row[1]).isoformat().replace("+00:00", "Z") if row[1] else None,
        "max_ts": utc(row[2]).isoformat().replace("+00:00", "Z") if row[2] else None,
        "uniq_trade_id": int(row[3]),
        "buy_count": int(row[4]),
        "sell_count": int(row[5]),
        "sum_notional": float(row[6]) if row[6] is not None else 0.0,
        "dedup_ok": int(row[0]) == int(row[3]),
    }


def coverage_candles(cl, symbol: str, start: datetime, end: datetime) -> dict[str, Any]:
    row = query_rows(
        cl,
        f"""
        SELECT count(), min(open_time), max(open_time)
        FROM signal_generator.candles_1m FINAL
        WHERE exchange='bybit' AND symbol='{symbol}' AND interval='1m' AND is_closed=1
          AND open_time >= toDateTime64('{_dt_sql(start)}', 3, 'UTC')
          AND open_time < toDateTime64('{_dt_sql(end)}', 3, 'UTC')
        """,
    )[0]
    expected_min = int((end - start).total_seconds() // 60)
    return {
        "table": "signal_generator.candles_1m",
        "count": int(row[0]),
        "min_ts": utc(row[1]).isoformat().replace("+00:00", "Z") if row[1] else None,
        "max_ts": utc(row[2]).isoformat().replace("+00:00", "Z") if row[2] else None,
        "expected_minutes": expected_min,
        "complete": int(row[0]) >= expected_min - 1,
    }


def coverage_open_interest(cl, symbol: str, start: datetime, end: datetime) -> dict[str, Any]:
    row = query_rows(
        cl,
        f"""
        SELECT count(), min(bucket_time), max(bucket_time)
        FROM orderbook_analysis.open_interest_5s
        WHERE symbol='{symbol}'
          AND bucket_time >= toDateTime64('{_dt_sql(start)}', 3, 'UTC')
          AND bucket_time < toDateTime64('{_dt_sql(end)}', 3, 'UTC')
        """,
    )[0]
    return {
        "table": "orderbook_analysis.open_interest_5s",
        "count": int(row[0]),
        "min_ts": utc(row[1]).isoformat().replace("+00:00", "Z") if row[1] else None,
        "max_ts": utc(row[2]).isoformat().replace("+00:00", "Z") if row[2] else None,
        "resolution": "5s",
    }


def coverage_liquidations(cl, symbol: str, start: datetime, end: datetime) -> dict[str, Any]:
    rows = query_rows(
        cl,
        f"""
        SELECT liquidated_position_side, count(), sum(notional_estimate)
        FROM orderbook_analysis.all_liquidations
        WHERE symbol='{symbol}'
          AND event_time >= toDateTime64('{_dt_sql(start)}', 3, 'UTC')
          AND event_time < toDateTime64('{_dt_sql(end)}', 3, 'UTC')
        GROUP BY liquidated_position_side
        """,
    )
    return {
        "table": "orderbook_analysis.all_liquidations",
        "by_side": [
            {"side": str(r[0]), "count": int(r[1]), "notional": float(r[2]) if r[2] else 0.0}
            for r in rows
        ],
        "price_column": "bankruptcy_price",
    }


def load_open_interest(cl, symbol: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
    rows = query_rows(
        cl,
        f"""
        SELECT bucket_time, toFloat64(open_interest), toFloat64(open_interest_value)
        FROM orderbook_analysis.open_interest_5s
        WHERE symbol='{symbol}'
          AND bucket_time >= toDateTime64('{_dt_sql(start)}', 3, 'UTC')
          AND bucket_time < toDateTime64('{_dt_sql(end)}', 3, 'UTC')
        ORDER BY bucket_time
        """,
    )
    return [{"ts": utc(r[0]), "oi": float(r[1]), "oi_value": float(r[2])} for r in rows]


def load_liquidations(cl, symbol: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
    rows = query_rows(
        cl,
        f"""
        SELECT event_time, liquidated_position_side, toFloat64(notional_estimate),
               toFloat64(bankruptcy_price)
        FROM orderbook_analysis.all_liquidations
        WHERE symbol='{symbol}'
          AND event_time >= toDateTime64('{_dt_sql(start)}', 3, 'UTC')
          AND event_time < toDateTime64('{_dt_sql(end)}', 3, 'UTC')
        ORDER BY event_time
        """,
    )
    return [
        {
            "ts": utc(r[0]),
            "side": str(r[1]),
            "notional": float(r[2]),
            "price": float(r[3]),
        }
        for r in rows
    ]


def load_liquidation_events(
    cl,
    symbol: str,
    start: datetime,
    end: datetime,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Load deduplicated liquidation events with event_key and executed size."""
    rows = query_rows(
        cl,
        f"""
        SELECT event_time, liquidated_position_side, position_side_raw,
               toFloat64(size), toFloat64(notional_estimate), toFloat64(bankruptcy_price),
               event_key, source_topic
        FROM orderbook_analysis.all_liquidations
        WHERE symbol='{symbol}'
          AND event_time >= toDateTime64('{_dt_sql(start)}', 3, 'UTC')
          AND event_time < toDateTime64('{_dt_sql(end)}', 3, 'UTC')
        ORDER BY event_time, event_key
        """,
    )
    raw_count = len(rows)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for r in rows:
        key = str(r[6])
        if key in seen:
            continue
        seen.add(key)
        side = str(r[1])
        base = float(r[3])
        bp = float(r[5])
        out.append(
            {
                "event_time": utc(r[0]),
                "liquidated_side": side,
                "position_side_raw": str(r[2]),
                "forced_trade_direction": "FORCED_BUY" if side == "LIQUIDATED_SHORT" else "FORCED_SELL",
                "executed_base_size": base,
                "bankruptcy_price": bp,
                "bankruptcy_reference_quote": base * bp,
                "notional_estimate": float(r[4]),
                "event_key": key,
                "dedup_key": key,
                "source_topic": str(r[7]),
            }
        )
    meta = {
        "table": "orderbook_analysis.all_liquidations",
        "raw_row_count": raw_count,
        "unique_event_count": len(out),
        "duplicate_event_count": raw_count - len(out),
        "dedup_key": "event_key",
    }
    return out, meta


def price_at_timestamp(trades: list[dict[str, Any]], at: datetime) -> float | None:
    xs = [t for t in trades if t["ts"] <= at]
    return xs[-1]["price"] if xs else None
