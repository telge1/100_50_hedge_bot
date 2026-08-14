"""Read-only Frozen Tier-A signals from ClickHouse. No writes."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from .candles import ensure_utc
from .config import CLICKHOUSE_ENV_FILE

try:
    from research_charts.clickhouse_config import load_clickhouse_config
except ImportError:  # pragma: no cover
    load_clickhouse_config = None  # type: ignore[assignment]


def _client():
    import clickhouse_connect

    if load_clickhouse_config is None:
        raise RuntimeError("clickhouse config unavailable")
    cfg = load_clickhouse_config()
    return cfg, clickhouse_connect.get_client(**cfg.connect_kwargs())


def _parse_metadata(raw: Any) -> dict[str, Any]:
    if raw is None or raw == "":
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def load_tier_a_signals(
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    symbols: list[str] | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    cfg, client = _client()
    where = ["tier_a = 1"]
    params: dict[str, Any] = {}
    if start is not None:
        where.append("candle_close_time >= {start:DateTime64(3, 'UTC')}")
        params["start"] = ensure_utc(start)
    if end is not None:
        where.append("candle_close_time <= {end:DateTime64(3, 'UTC')}")
        params["end"] = ensure_utc(end)
    if symbols:
        where.append("symbol IN {symbols:Array(String)}")
        params["symbols"] = [s.strip().upper() for s in symbols]
    sql = f"""
        SELECT
            signal_id, symbol, timeframe, direction,
            candle_open_time, candle_close_time, generated_at,
            signal_price, metadata, strategy_version
        FROM {cfg.database}.signals FINAL
        WHERE {' AND '.join(where)}
        ORDER BY candle_close_time ASC, generated_at ASC, signal_id ASC
    """
    if limit:
        sql += " LIMIT {lim:UInt32}"
        params["lim"] = int(limit)
    try:
        result = client.query(sql, parameters=params)
        rows = [dict(zip(result.column_names, row, strict=True)) for row in result.result_rows]
    finally:
        client.close()

    out: list[dict[str, Any]] = []
    for row in rows:
        meta = _parse_metadata(row.get("metadata"))
        plan = meta.get("trade_plan") if isinstance(meta.get("trade_plan"), dict) else meta
        entry_time = plan.get("entry_time")
        entry_price = plan.get("entry_price")
        if entry_price is None:
            entry_price = row.get("signal_price")
        try:
            entry_f = float(entry_price) if entry_price is not None else None
        except (TypeError, ValueError):
            entry_f = None
        if not plan.get("entry_valid", True):
            continue
        if entry_f is None or entry_f <= 0 or not entry_time:
            continue
        out.append(
            {
                "signal_id": str(row["signal_id"]),
                "symbol": str(row["symbol"]).strip().upper(),
                "timeframe": str(row.get("timeframe") or ""),
                "direction": str(row.get("direction") or "").upper(),
                "entry_time": entry_time,
                "entry_price": entry_f,
                "available_at": row.get("candle_close_time"),
                "created_at": row.get("generated_at"),
                "baseline_tp": plan.get("tp_price"),
                "baseline_sl": plan.get("sl_price"),
                "strategy_version": row.get("strategy_version"),
            }
        )
    return out


def load_closed_1m(symbol: str, *, start: datetime | None = None, end: datetime | None = None) -> list[dict[str, Any]]:
    cfg, client = _client()
    where = [
        "exchange = {exchange:String}",
        "symbol = {symbol:String}",
        "interval = {interval:String}",
        "is_closed = 1",
    ]
    params: dict[str, Any] = {
        "exchange": cfg.exchange,
        "symbol": str(symbol).strip().upper(),
        "interval": "1m",
    }
    if start is not None:
        where.append("open_time >= {start:DateTime64(3, 'UTC')}")
        params["start"] = ensure_utc(start)
    if end is not None:
        where.append("open_time <= {end:DateTime64(3, 'UTC')}")
        params["end"] = ensure_utc(end)
    sql = f"""
        SELECT open_time, close_time, open, high, low, close, volume, turnover
        FROM {cfg.database}.{cfg.table} FINAL
        WHERE {' AND '.join(where)}
        ORDER BY open_time ASC
    """
    try:
        result = client.query(sql, parameters=params)
        rows = [dict(zip(result.column_names, row, strict=True)) for row in result.result_rows]
    finally:
        client.close()
    return rows


def load_candle_history_bounds(symbol: str) -> tuple[datetime | None, datetime | None]:
    """Read-only min/max open_time for the canonical 1m table. Not used as plan input."""
    cfg, client = _client()
    sql = f"""
        SELECT min(open_time) AS mn, max(open_time) AS mx
        FROM {cfg.database}.{cfg.table} FINAL
        WHERE exchange = {{exchange:String}}
          AND symbol = {{symbol:String}}
          AND interval = {{interval:String}}
          AND is_closed = 1
    """
    params = {
        "exchange": cfg.exchange,
        "symbol": str(symbol).strip().upper(),
        "interval": "1m",
    }
    try:
        result = client.query(sql, parameters=params)
        if not result.result_rows:
            return None, None
        mn, mx = result.result_rows[0]
        return (
            None if mn is None else ensure_utc(mn),
            None if mx is None else ensure_utc(mx),
        )
    finally:
        client.close()


def clickhouse_source_public() -> dict[str, Any]:
    if load_clickhouse_config is None:
        return {"database": "signal_generator", "table": "candles_1m"}
    cfg = load_clickhouse_config()
    return {
        "host": cfg.host,
        "port": cfg.port,
        "database": cfg.database,
        "table": cfg.table,
        "exchange": cfg.exchange,
        "final": True,
        "is_closed": 1,
        "env_file": str(CLICKHOUSE_ENV_FILE),
    }
