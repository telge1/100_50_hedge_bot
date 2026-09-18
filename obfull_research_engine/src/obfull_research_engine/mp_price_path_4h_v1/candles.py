"""Read-only loader for signal_generator.candles_1m."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Sequence

from obfull_research_engine.mp_edge_event_study_v1.silver_mids import assert_select_only
from obfull_research_engine.mp_edge_event_study_v1.util import as_utc, dt_to_ns, ns_to_dt

from .params import CANDLE_DATABASE, CANDLE_TABLE, EXCHANGE, NS, SYMBOL


def _rows_from_result(raw: Any) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return list(raw)
    if hasattr(raw, "named_results"):
        return list(raw.named_results())
    if hasattr(raw, "result_rows") and hasattr(raw, "column_names"):
        cols = list(raw.column_names)
        return [dict(zip(cols, row)) for row in raw.result_rows]
    raise TypeError(f"unsupported query result type: {type(raw)}")


def _query(client: Any, sql: str, parameters: dict[str, Any] | None = None) -> Any:
    assert_select_only(sql)
    return client.query(sql, parameters=parameters or {})


@dataclass(frozen=True)
class Candle1m:
    open_time_ns: int
    open: float
    high: float
    low: float
    close: float
    volume: float

    @property
    def open_time(self) -> datetime:
        return ns_to_dt(self.open_time_ns)


def audit_candle_source(client: Any, *, symbol: str = SYMBOL) -> dict[str, Any]:
    database, table = CANDLE_DATABASE, CANDLE_TABLE
    cols = _rows_from_result(
        _query(
            client,
            """
SELECT name, type
FROM system.columns
WHERE database = {database:String} AND table = {table:String}
ORDER BY position
""".strip(),
            {"database": database, "table": table},
        )
    )
    meta = _rows_from_result(
        _query(
            client,
            f"""
SELECT
  count() AS n,
  uniqExact(open_time) AS uniq_ts,
  min(open_time) AS t0,
  max(open_time) AS t1,
  countIf(high < low) AS bad_hl,
  countIf(open <= 0 OR high <= 0 OR low <= 0 OR close <= 0) AS bad_px,
  uniqExact(interval) AS n_intervals
FROM {database}.{table} FINAL
WHERE symbol = {{symbol:String}}
  AND exchange = {{exchange:String}}
""".strip(),
            {"symbol": symbol, "exchange": EXCHANGE},
        )
    )[0]
    create = _rows_from_result(
        _query(
            client,
            """
SELECT engine, partition_key, sorting_key, create_table_query
FROM system.tables
WHERE database = {database:String} AND name = {table:String}
""".strip(),
            {"database": database, "table": table},
        )
    )[0]
    # Sep 2026 continuity check spanning batch + 4h buffer
    cov = _rows_from_result(
        _query(
            client,
            f"""
SELECT
  count() AS n,
  uniqExact(open_time) AS uniq_ts,
  min(open_time) AS t0,
  max(open_time) AS t1,
  dateDiff('minute', min(open_time), max(open_time)) + 1 AS span_minutes,
  (dateDiff('minute', min(open_time), max(open_time)) + 1) - uniqExact(open_time) AS missing_est
FROM {database}.{table} FINAL
WHERE symbol = {{symbol:String}}
  AND exchange = {{exchange:String}}
  AND open_time >= toDateTime64('2026-09-05 00:00:00', 3, 'UTC')
  AND open_time <  toDateTime64('2026-09-12 18:00:00', 3, 'UTC')
""".strip(),
            {"symbol": symbol, "exchange": EXCHANGE},
        )
    )[0]
    ok = (
        int(meta["n"]) > 0
        and int(meta["uniq_ts"]) == int(meta["n"])
        and int(meta["bad_hl"]) == 0
        and int(cov["missing_est"]) == 0
    )
    return {
        "ok": ok,
        "database": database,
        "table": table,
        "full_name": f"{database}.{table}",
        "symbol_column": "symbol",
        "timestamp_column": "open_time",
        "timezone": "UTC",
        "ohlc": ["open", "high", "low", "close"],
        "volume": "volume",
        "timeframe": "1m",
        "exchange": EXCHANGE,
        "columns": [{k: r[k] for k in ("name", "type")} for r in cols],
        "engine": create.get("engine"),
        "partition_key": create.get("partition_key"),
        "sorting_key": create.get("sorting_key"),
        "global_range": {
            "n": int(meta["n"]),
            "uniq_ts": int(meta["uniq_ts"]),
            "min_open_time_utc": str(meta["t0"]),
            "max_open_time_utc": str(meta["t1"]),
            "bad_hl": int(meta["bad_hl"]),
            "bad_px": int(meta["bad_px"]),
            "duplicates_est": int(meta["n"]) - int(meta["uniq_ts"]),
        },
        "batch_span_coverage": {
            "n": int(cov["n"]),
            "uniq_ts": int(cov["uniq_ts"]),
            "min_open_time_utc": str(cov["t0"]),
            "max_open_time_utc": str(cov["t1"]),
            "span_minutes": int(cov["span_minutes"]),
            "missing_est": int(cov["missing_est"]),
            "continuous": int(cov["missing_est"]) == 0,
        },
        "selected_timeframe": "1m",
        "selection_reason": "full continuous 1m BTCUSDT history available; preferred over coarser TF",
    }


def load_candles_1m(
    client: Any,
    *,
    start: datetime,
    end: datetime,
    symbol: str = SYMBOL,
    exchange: str = EXCHANGE,
) -> list[Candle1m]:
    """Load half-open [start, end) 1m candles sorted by open_time."""
    start = as_utc(start)
    end = as_utc(end)
    sql = f"""
SELECT
  open_time,
  toFloat64(open) AS open,
  toFloat64(high) AS high,
  toFloat64(low) AS low,
  toFloat64(close) AS close,
  toFloat64(volume) AS volume
FROM {CANDLE_DATABASE}.{CANDLE_TABLE} FINAL
WHERE symbol = {{symbol:String}}
  AND exchange = {{exchange:String}}
  AND interval = '1m'
  AND open_time >= {{start_ts:DateTime64(3,'UTC')}}
  AND open_time < {{end_ts:DateTime64(3,'UTC')}}
ORDER BY open_time
""".strip()
    rows = _rows_from_result(
        _query(
            client,
            sql,
            {
                "symbol": symbol,
                "exchange": exchange,
                "start_ts": start,
                "end_ts": end,
            },
        )
    )
    out: list[Candle1m] = []
    prev: int | None = None
    for r in rows:
        ts = r["open_time"]
        if not isinstance(ts, datetime):
            raise TypeError("open_time must be datetime")
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        else:
            ts = ts.astimezone(timezone.utc)
        ns = dt_to_ns(ts)
        if prev is not None and ns <= prev:
            raise RuntimeError(f"NON_MONOTONE_CANDLE:{prev}->{ns}")
        prev = ns
        out.append(
            Candle1m(
                open_time_ns=ns,
                open=float(r["open"]),
                high=float(r["high"]),
                low=float(r["low"]),
                close=float(r["close"]),
                volume=float(r["volume"]),
            )
        )
    return out


def floor_minute_ns(ts_ns: int) -> int:
    return (int(ts_ns) // (60 * NS)) * (60 * NS)
