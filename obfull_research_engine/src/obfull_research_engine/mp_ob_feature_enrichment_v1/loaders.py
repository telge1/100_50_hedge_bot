"""Read-only ClickHouse loaders for metrics, level-changes, public trades."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Sequence

from obfull_research_engine.breakout_xray_v1.adapters.live_data import (
    assert_price_band_width,
    normalize_level_change_rows,
    normalize_trade_rows,
    public_trades_sql,
)
from obfull_research_engine.breakout_xray_v1.ports import LevelChangeEvent
from obfull_research_engine.breakout_xray_v1.trades import XRayTrade
from obfull_research_engine.mp_edge_event_study_v1.silver_mids import (
    MidTick,
    assert_select_only,
    assess_window_chunks,
    load_mid_series,
    normalize_mid_rows,
)
from obfull_research_engine.mp_edge_event_study_v1.util import ns_to_dt
from obfull_research_engine.mp_ob_feature_enrichment_v1.params import (
    LEVEL_CHANGES_TABLE,
    MAX_LC_PRICE_SPAN_USD,
    METRICS_TABLE,
    SILVER_DATABASE,
)


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


@dataclass
class MetricTick:
    bucket_start_ns: int
    mid: float | None
    best_bid: float | None
    best_ask: float | None
    spread: float | None
    bid_depth_near: float | None
    ask_depth_near: float | None
    imbalance: float | None


def load_metrics_enriched(
    client: Any,
    *,
    database: str,
    symbol: str,
    start_ns: int,
    end_ns: int,
    chunk_keys: Sequence[str],
) -> list[MetricTick]:
    if not chunk_keys:
        return []
    database = str(database)
    sql = f"""
SELECT bucket_start_ns, payload
FROM {database}.{METRICS_TABLE} FINAL
WHERE symbol = {{symbol:String}}
  AND chunk_key IN {{chunk_keys:Array(String)}}
  AND bucket_start_ns >= {{start_ns:UInt64}}
  AND bucket_start_ns <= {{end_ns:UInt64}}
ORDER BY bucket_start_ns
""".strip()
    rows = _rows_from_result(
        _query(
            client,
            sql,
            {
                "symbol": symbol,
                "chunk_keys": list(chunk_keys),
                "start_ns": int(start_ns),
                "end_ns": int(end_ns),
            },
        )
    )
    out: list[MetricTick] = []
    for r in rows:
        payload = r.get("payload")
        if isinstance(payload, str):
            payload = json.loads(payload)
        if not isinstance(payload, dict):
            continue
        bid_n = payload.get("bid_depth_near")
        ask_n = payload.get("ask_depth_near")
        try:
            bid_f = float(bid_n) if bid_n is not None else None
            ask_f = float(ask_n) if ask_n is not None else None
        except (TypeError, ValueError):
            bid_f, ask_f = None, None
        imb = None
        if bid_f is not None and ask_f is not None and (bid_f + ask_f) > 0:
            imb = (bid_f - ask_f) / (bid_f + ask_f)
        mid = payload.get("mid")
        bb = payload.get("best_bid")
        ba = payload.get("best_ask")
        sp = payload.get("spread")
        out.append(
            MetricTick(
                bucket_start_ns=int(r["bucket_start_ns"]),
                mid=None if mid is None else float(mid),
                best_bid=None if bb is None else float(bb),
                best_ask=None if ba is None else float(ba),
                spread=None if sp is None else float(sp),
                bid_depth_near=bid_f,
                ask_depth_near=ask_f,
                imbalance=imb,
            )
        )
    return out


def load_level_changes(
    client: Any,
    *,
    database: str,
    symbol: str,
    start_ns: int,
    end_ns: int,
    chunk_keys: Sequence[str],
    price_min: float,
    price_max: float,
    side: str | None = None,
) -> list[LevelChangeEvent]:
    if not chunk_keys:
        return []
    assert_price_band_width(price_min, price_max, max_span_usd=MAX_LC_PRICE_SPAN_USD)
    database = str(database)
    side_clause = ""
    params: dict[str, Any] = {
        "symbol": symbol,
        "chunk_keys": list(chunk_keys),
        "start_ns": int(start_ns),
        "end_ns": int(end_ns),
        "price_min": float(price_min),
        "price_max": float(price_max),
    }
    if side is not None:
        side_clause = "AND JSONExtractString(payload, 'side') = {side:String}"
        params["side"] = str(side)
    sql = f"""
SELECT
  event_time_ns,
  chunk_key,
  apply_order,
  record_ordinal,
  JSONExtractString(payload, 'side') AS side,
  JSONExtractFloat(payload, 'price') AS price,
  JSONExtractString(payload, 'change_type') AS change_type,
  JSONExtractFloat(payload, 'new_size') AS new_size,
  JSONExtractFloat(payload, 'old_size') AS old_size
FROM {database}.{LEVEL_CHANGES_TABLE} FINAL
WHERE symbol = {{symbol:String}}
  AND chunk_key IN {{chunk_keys:Array(String)}}
  AND event_time_ns >= {{start_ns:UInt64}}
  AND event_time_ns < {{end_ns:UInt64}}
  AND JSONExtractFloat(payload, 'price') >= {{price_min:Float64}}
  AND JSONExtractFloat(payload, 'price') <= {{price_max:Float64}}
  {side_clause}
ORDER BY event_time_ns, apply_order
SETTINGS max_threads = 8
""".strip()
    rows = _rows_from_result(_query(client, sql, params))
    return normalize_level_change_rows(rows)


def load_public_trades(
    client: Any,
    *,
    symbol: str,
    start_ns: int,
    end_ns: int,
) -> list[XRayTrade]:
    sql = public_trades_sql()
    # public_trades_sql uses DateTime bounds; inclusive end via ns_to_dt(end_ns) with < end
    rows = _rows_from_result(
        _query(
            client,
            sql,
            {
                "symbol": symbol,
                "start_ts": ns_to_dt(int(start_ns)),
                "end_ts": ns_to_dt(int(end_ns) + 1),
            },
        )
    )
    return normalize_trade_rows(rows)


def assess_chunks(
    client: Any,
    *,
    database: str,
    symbol: str,
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    return assess_window_chunks(
        client, database=database, symbol=symbol, start=start, end=end
    )


def chunks_overlapping(
    client: Any,
    *,
    database: str,
    chain_version: str,
    start_ns: int,
    end_ns: int,
) -> list[str]:
    sql = f"""
SELECT chunk_key
FROM {database}.silver_build_chunks_v1_3 FINAL
WHERE chain_version = {{chain_version:String}}
  AND status = 'COMPLETE'
  AND chunk_end_ns > {{start_ns:UInt64}}
  AND chunk_start_ns < {{end_ns:UInt64}}
ORDER BY chunk_start_ns, chunk_key
""".strip()
    rows = _rows_from_result(
        _query(
            client,
            sql,
            {
                "chain_version": chain_version,
                "start_ns": int(start_ns),
                "end_ns": int(end_ns),
            },
        )
    )
    out = []
    for r in rows:
        ck = r["chunk_key"]
        if isinstance(ck, (bytes, bytearray)):
            ck = ck.decode("utf-8", errors="replace")
        out.append(str(ck).strip("\x00"))
    return out


__all__ = [
    "MetricTick",
    "assess_chunks",
    "chunks_overlapping",
    "load_level_changes",
    "load_metrics_enriched",
    "load_mid_series",
    "load_public_trades",
    "normalize_mid_rows",
    "SILVER_DATABASE",
    "LEVEL_CHANGES_TABLE",
    "METRICS_TABLE",
    "MidTick",
]
