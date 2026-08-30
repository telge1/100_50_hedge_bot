"""Read-only trade / OI loaders (ClickHouse or fixtures)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from orderbook_analyse.aggressor_efficiency_flip.contracts import (
    CANONICAL_TRADES_TABLE,
    OI_5S_TABLE,
)
from orderbook_analyse.aggressor_efficiency_flip.models import Trade
from orderbook_analyse.aggressor_efficiency_flip.timeutil import ensure_utc, iso_z


class AEFIntegrityError(RuntimeError):
    pass


def trades_from_rows(rows: list[dict[str, Any]]) -> list[Trade]:
    out: list[Trade] = []
    seen: set[str] = set()
    for r in rows:
        tid = str(r["trade_id"])
        if tid in seen:
            continue  # dedupe by trade_id
        seen.add(tid)
        side = str(r["side"])
        if side not in {"Buy", "Sell"}:
            raise AEFIntegrityError(f"invalid_side:{side}")
        out.append(
            Trade(
                trade_ts=ensure_utc(r["trade_ts"]),
                trade_id=tid,
                side=side,
                price=float(r["price"]),
                size=float(r["size"]),
                notional=float(r["notional"]),
            )
        )
    return out


def load_trades_clickhouse(
    *,
    symbol: str,
    start: datetime,
    end: datetime,
    query_log: Optional[list[dict[str, Any]]] = None,
) -> tuple[list[Trade], dict[str, Any]]:
    """SELECT-only from public_trades_canonical."""
    from orderbook_analyse.orderbook_v2.ch_client import get_clickhouse_client, load_clickhouse_settings

    load_clickhouse_settings()
    client = get_clickhouse_client()
    sym = str(symbol).upper()
    sql = f"""
    SELECT trade_ts, trade_id, side, price, size, notional
    FROM {CANONICAL_TRADES_TABLE}
    WHERE symbol = {{sym:String}}
      AND trade_ts >= toDateTime64({{t0:String}}, 3, 'UTC')
      AND trade_ts <  toDateTime64({{t1:String}}, 3, 'UTC')
    ORDER BY trade_ts, trade_id
    """
    params = {
        "sym": sym,
        "t0": iso_z(start).replace("Z", "")[:19],
        "t1": iso_z(end).replace("Z", "")[:19],
    }
    import time

    t0 = time.perf_counter()
    result = client.query(sql, parameters=params)
    ms = (time.perf_counter() - t0) * 1000
    if query_log is not None:
        query_log.append({"title": "load_trades", "ms": round(ms, 1), "rows": len(result.result_rows), "sql": sql.strip()})
    rows = []
    for trade_ts, trade_id, side, price, size, notional in result.result_rows:
        rows.append(
            {
                "trade_ts": trade_ts,
                "trade_id": trade_id,
                "side": str(side),
                "price": float(price),
                "size": float(size),
                "notional": float(notional),
            }
        )
    trades = trades_from_rows(rows)
    preflight = {
        "table": CANONICAL_TRADES_TABLE,
        "symbol": sym,
        "start": iso_z(start),
        "end": iso_z(end),
        "rows_loaded": len(rows),
        "rows_after_dedupe": len(trades),
        "sides": sorted({t.side for t in trades}),
    }
    if preflight["sides"] and set(preflight["sides"]) - {"Buy", "Sell"}:
        raise AEFIntegrityError("invalid_side_semantics")
    return trades, preflight


def load_oi_labels_clickhouse(
    *,
    symbol: str,
    start: datetime,
    end: datetime,
    query_log: Optional[list[dict[str, Any]]] = None,
) -> dict[datetime, str]:
    """Optional OI 5s labels; missing → empty dict (MISSING later)."""
    try:
        from orderbook_analyse.orderbook_v2.ch_client import get_clickhouse_client, load_clickhouse_settings

        load_clickhouse_settings()
        client = get_clickhouse_client()
    except Exception:
        return {}
    sql = f"""
    SELECT bucket_time, open_interest
    FROM {OI_5S_TABLE}
    WHERE symbol = {{sym:String}}
      AND bucket_time >= toDateTime64({{t0:String}}, 3, 'UTC')
      AND bucket_time <  toDateTime64({{t1:String}}, 3, 'UTC')
    ORDER BY bucket_time
    """
    params = {
        "sym": str(symbol).upper(),
        "t0": iso_z(start).replace("Z", "")[:19],
        "t1": iso_z(end).replace("Z", "")[:19],
    }
    import time
    from orderbook_analyse.aggressor_efficiency_flip.timeutil import floor_second

    t0 = time.perf_counter()
    try:
        result = client.query(sql, parameters=params)
    except Exception:
        return {}
    ms = (time.perf_counter() - t0) * 1000
    if query_log is not None:
        query_log.append({"title": "load_oi", "ms": round(ms, 1), "rows": len(result.result_rows), "sql": sql.strip()})
    labels: dict[datetime, str] = {}
    prev = None
    prev_oi = None
    for bucket_time, oi in result.result_rows:
        bt = floor_second(ensure_utc(bucket_time))
        oi_f = float(oi)
        if prev is not None and prev_oi is not None:
            # crude class vs previous OI; price class filled later in runner if needed
            if oi_f > prev_oi:
                labels[bt] = "OI_UP"
            elif oi_f < prev_oi:
                labels[bt] = "OI_DOWN"
            else:
                labels[bt] = "OI_FLAT"
        else:
            labels[bt] = "OI_FLAT"
        prev, prev_oi = bt, oi_f
    return labels


def preflight_canonical(
    *,
    symbol: str,
    start: datetime,
    end: datetime,
    query_log: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    from orderbook_analyse.orderbook_v2.ch_client import get_clickhouse_client, load_clickhouse_settings

    load_clickhouse_settings()
    client = get_clickhouse_client()
    sql = f"""
    SELECT
      count() AS rows,
      uniqExact(trade_id) AS uniq_tid,
      countIf(side NOT IN ('Buy','Sell')) AS bad_side,
      min(trade_ts), max(trade_ts)
    FROM {CANONICAL_TRADES_TABLE}
    WHERE symbol = {{sym:String}}
      AND trade_ts >= toDateTime64({{t0:String}}, 3, 'UTC')
      AND trade_ts <  toDateTime64({{t1:String}}, 3, 'UTC')
    """
    params = {
        "sym": str(symbol).upper(),
        "t0": iso_z(start).replace("Z", "")[:19],
        "t1": iso_z(end).replace("Z", "")[:19],
    }
    import time

    t0 = time.perf_counter()
    r = client.query(sql, parameters=params)
    ms = (time.perf_counter() - t0) * 1000
    if query_log is not None:
        query_log.append({"title": "preflight", "ms": round(ms, 1), "rows": 1, "sql": sql.strip()})
    rows, uniq, bad, mn, mx = r.result_rows[0]
    out = {
        "rows": int(rows),
        "uniq_trade_id": int(uniq),
        "duplicate_surplus": int(rows) - int(uniq),
        "bad_side": int(bad),
        "min_ts": str(mn),
        "max_ts": str(mx),
        "table": CANONICAL_TRADES_TABLE,
    }
    if out["bad_side"] > 0:
        raise AEFIntegrityError("invalid_side_semantics")
    if out["duplicate_surplus"] < 0:
        raise AEFIntegrityError("trade_id_integrity")
    if out["rows"] == 0:
        raise AEFIntegrityError("no_trades_in_window")
    return out
