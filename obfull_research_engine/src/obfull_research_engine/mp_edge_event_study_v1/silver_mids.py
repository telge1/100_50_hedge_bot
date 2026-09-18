"""Read-only Silver 100ms mid series loader."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from typing import Any, Sequence

from .schema import MidTick
from .util import as_utc, dt_to_ns


FORBIDDEN_SQL_TOKENS = (
    " insert ",
    " alter ",
    " drop ",
    " truncate ",
    " optimize ",
    " delete ",
    " create ",
    " attach ",
    " detach ",
    " rename ",
    " grant ",
    " revoke ",
)


def assert_select_only(sql: str) -> None:
    cleaned = f" {' '.join(sql.lower().split())} "
    body = cleaned.lstrip()
    if not (body.startswith("select ") or body.startswith("with ")):
        raise RuntimeError("STOP_MP_PILOT_SELECT_ONLY")
    if any(tok in cleaned for tok in FORBIDDEN_SQL_TOKENS):
        raise RuntimeError("STOP_MP_PILOT_SELECT_ONLY")


def _parse_payload(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    return json.loads(str(raw))


def mid_from_payload(payload: dict[str, Any]) -> tuple[float | None, float | None, float | None, bool]:
    """Return mid, best_bid, best_ask, valid.

    Priority: canonical mid if valid; else mid from bid/ask when bid < ask.
    Crossed/invalid books → valid=False.
    """
    bid = payload.get("best_bid")
    ask = payload.get("best_ask")
    mid = payload.get("mid")
    try:
        bid_f = float(bid) if bid is not None else None
    except (TypeError, ValueError):
        bid_f = None
    try:
        ask_f = float(ask) if ask is not None else None
    except (TypeError, ValueError):
        ask_f = None
    try:
        mid_f = float(mid) if mid is not None else None
    except (TypeError, ValueError):
        mid_f = None

    book_ok = (
        bid_f is not None
        and ask_f is not None
        and bid_f > 0
        and ask_f > 0
        and bid_f < ask_f
    )
    if mid_f is not None and mid_f > 0 and book_ok:
        if bid_f <= mid_f <= ask_f:
            return mid_f, bid_f, ask_f, True
        return mid_f, bid_f, ask_f, False
    if book_ok:
        return (bid_f + ask_f) / 2.0, bid_f, ask_f, True
    if mid_f is not None and mid_f > 0 and (bid_f is None or ask_f is None):
        return mid_f, bid_f, ask_f, True
    return mid_f, bid_f, ask_f, False


def normalize_mid_rows(rows: Sequence[dict[str, Any]]) -> list[MidTick]:
    out: list[MidTick] = []
    prev_ns: int | None = None
    for r in rows:
        ts_ns = int(r["bucket_start_ns"])
        if prev_ns is not None and ts_ns < prev_ns:
            raise RuntimeError(f"NON_MONOTONE_EVENT_TIME:{prev_ns}->{ts_ns}")
        prev_ns = ts_ns
        payload = _parse_payload(r.get("payload"))
        mid, bid, ask, valid = mid_from_payload(payload)
        epoch = str(
            payload.get("epoch_id")
            or payload.get("replay_epoch")
            or r.get("epoch_id")
            or ""
        )
        if mid is None:
            valid = False
            mid = float("nan")
        out.append(
            MidTick(
                ts_ns=ts_ns,
                mid=float(mid),
                best_bid=bid,
                best_ask=ask,
                valid=bool(valid),
                epoch_id=epoch,
                chunk_key=str(r.get("chunk_key") or ""),
            )
        )
    return out


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).decode("ascii", errors="replace")
    text = str(value)
    # defensive: accidental bytes repr
    if text.startswith("b'") and text.endswith("'") and len(text) > 3:
        try:
            return text[2:-1]
        except Exception:  # noqa: BLE001
            return text
    return text


def _rows_from_result(raw: Any) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if hasattr(raw, "named_results"):
        rows = list(raw.named_results())
    elif hasattr(raw, "result_rows") and hasattr(raw, "column_names"):
        cols = list(raw.column_names)
        rows = [dict(zip(cols, row)) for row in raw.result_rows]
    elif isinstance(raw, list):
        rows = list(raw)
    else:
        raise TypeError(f"unsupported query result type: {type(raw)}")
    out: list[dict[str, Any]] = []
    for r in rows:
        cleaned = {}
        for k, v in r.items():
            if isinstance(v, (bytes, bytearray, memoryview)):
                cleaned[k] = bytes(v).decode("ascii", errors="replace")
            else:
                cleaned[k] = v
        out.append(cleaned)
    return out


def _query(client: Any, sql: str, parameters: dict[str, Any] | None = None) -> Any:
    from obfull_research_engine.clickhouse_research_store_v1.analysis_readiness_v1_3 import (
        _analysis_query,
    )

    assert_select_only(sql)
    return _analysis_query(client, sql, parameters=parameters or {})


def load_chain_meta(client: Any, *, database: str, symbol: str) -> dict[str, Any]:
    sql = f"""
SELECT
  run_id,
  chain_version,
  status,
  chunk_complete,
  chunk_total,
  canonical_chain_hash
FROM {database}.silver_build_runs_v1_3 FINAL
WHERE symbol = {{symbol:String}}
ORDER BY version_ms DESC
LIMIT 1
""".strip()
    rows = _rows_from_result(_query(client, sql, {"symbol": symbol}))
    if not rows:
        raise RuntimeError("SILVER_BUILD_RUN_MISSING")
    row = rows[0]
    # normalize bytes
    for k, v in list(row.items()):
        if isinstance(v, (bytes, bytearray)):
            row[k] = bytes(v).decode("ascii", errors="replace")
    return row


def assess_window_chunks(
    client: Any,
    *,
    database: str,
    symbol: str,
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    """Single-epoch chunk coverage via ledger + assess_analysis_window (read-only)."""
    from obfull_research_engine.clickhouse_research_store_v1.analysis_readiness_v1_3 import (
        ChunkAssessment,
        assess_analysis_window,
    )

    start = as_utc(start)
    end = as_utc(end)
    start_ns = dt_to_ns(start)
    end_ns = dt_to_ns(end)

    meta = load_chain_meta(client, database=database, symbol=symbol)
    chain_version = str(meta.get("chain_version") or "")
    if not chain_version:
        raise RuntimeError("SILVER_CHAIN_VERSION_MISSING")

    chunk_sql = f"""
SELECT
  chunk_key,
  epoch_id,
  chunk_start_ns,
  chunk_end_ns,
  status,
  level_change_count,
  state_count,
  output_hash
FROM {database}.silver_build_chunks_v1_3 FINAL
WHERE chain_version = {{chain_version:String}}
  AND status = 'COMPLETE'
  AND chunk_end_ns > {{start_ns:UInt64}}
  AND chunk_start_ns < {{end_ns:UInt64}}
ORDER BY chunk_start_ns, chunk_key
""".strip()
    chunk_rows = _rows_from_result(
        _query(
            client,
            chunk_sql,
            {
                "chain_version": chain_version,
                "start_ns": start_ns,
                "end_ns": end_ns,
            },
        )
    )
    ready = [
        ChunkAssessment(
            chunk_key=_as_text(r["chunk_key"]),
            epoch_id=_as_text(r["epoch_id"]),
            start_ns=int(r["chunk_start_ns"]),
            end_ns=int(r["chunk_end_ns"]),
            level_change_count=int(r.get("level_change_count") or 0),
            state_count=int(r.get("state_count") or 0),
            output_hash=_as_text(r.get("output_hash") or ""),
            ledger_status=str(r.get("status") or ""),
            observed_level_changes=None,
            observed_states=None,
            status="READY",
            reason="",
        )
        for r in chunk_rows
    ]

    gap_sql = f"""
SELECT gap_time_ns
FROM {database}.silver_epoch_gaps_v1_3 FINAL
WHERE chain_version = {{chain_version:String}}
""".strip()
    gap_rows = _rows_from_result(
        _query(client, gap_sql, {"chain_version": chain_version})
    )
    gap_times = [int(r["gap_time_ns"]) for r in gap_rows]

    assessment = assess_analysis_window(
        start_ns=start_ns,
        end_ns=end_ns,
        ready_chunks=ready,
        gap_times=gap_times,
    )
    if assessment.status != "READY":
        raise RuntimeError(
            f"MP_PILOT_BLOCKED_DATA:{assessment.reason or assessment.status}"
        )
    return {
        "status": assessment.status,
        "reason": assessment.reason,
        "epoch_id": assessment.epoch_id,
        "chunk_keys": list(assessment.chunk_keys),
        "start_ns": start_ns,
        "end_ns": end_ns,
        "state_count": assessment.state_count,
        "level_change_count": assessment.level_change_count,
        "chain_version": chain_version,
        "build_run": meta,
    }


def metrics_sql(*, database: str) -> str:
    # Project only needed payload fields to keep CH memory under analysis limit.
    return f"""
SELECT
  bucket_start_ns,
  chunk_key,
  JSONExtractFloat(payload, 'mid') AS mid,
  JSONExtractFloat(payload, 'best_bid') AS best_bid,
  JSONExtractFloat(payload, 'best_ask') AS best_ask,
  JSONExtractString(payload, 'epoch_id') AS epoch_id
FROM {database}.ob_metrics_100ms_v1_3 FINAL
WHERE symbol = {{symbol:String}}
  AND chunk_key = {{chunk_key:String}}
  AND bucket_start_ns >= {{start_ns:UInt64}}
  AND bucket_start_ns < {{end_ns:UInt64}}
ORDER BY bucket_start_ns, chunk_key
""".strip()


def count_metrics_sql(*, database: str) -> str:
    return f"""
SELECT count() AS n
FROM {database}.ob_metrics_100ms_v1_3 FINAL
WHERE symbol = {{symbol:String}}
  AND chunk_key IN {{chunk_keys:Array(String)}}
  AND bucket_start_ns >= {{start_ns:UInt64}}
  AND bucket_start_ns < {{end_ns:UInt64}}
""".strip()


def normalize_projected_rows(rows: Sequence[dict[str, Any]]) -> list[MidTick]:
    out: list[MidTick] = []
    prev_ns: int | None = None
    for r in rows:
        ts_ns = int(r["bucket_start_ns"])
        if prev_ns is not None and ts_ns < prev_ns:
            raise RuntimeError(f"NON_MONOTONE_EVENT_TIME:{prev_ns}->{ts_ns}")
        prev_ns = ts_ns
        payload = {
            "mid": r.get("mid"),
            "best_bid": r.get("best_bid"),
            "best_ask": r.get("best_ask"),
            "epoch_id": r.get("epoch_id"),
        }
        mid, bid, ask, valid = mid_from_payload(payload)
        epoch = _as_text(payload.get("epoch_id") or "")
        if mid is None:
            valid = False
            mid = float("nan")
        out.append(
            MidTick(
                ts_ns=ts_ns,
                mid=float(mid),
                best_bid=bid,
                best_ask=ask,
                valid=bool(valid),
                epoch_id=epoch,
                chunk_key=_as_text(r.get("chunk_key") or ""),
            )
        )
    return out


def load_mid_series(
    client: Any,
    *,
    database: str,
    symbol: str,
    start: datetime,
    end: datetime,
    chunk_keys: Sequence[str],
    expected_epoch_id: str | None = None,
) -> list[MidTick]:
    if not chunk_keys:
        raise RuntimeError("DATA_SOURCE_UNAVAILABLE:empty_chunk_keys")
    start_ns = dt_to_ns(as_utc(start))
    end_ns = dt_to_ns(as_utc(end))
    sql = metrics_sql(database=database)
    series: list[MidTick] = []
    # One chunk_key per query keeps CH under max_memory_usage=512MiB.
    for ck in chunk_keys:
        raw = _query(
            client,
            sql,
            {
                "symbol": symbol,
                "chunk_key": _as_text(ck),
                "start_ns": int(start_ns),
                "end_ns": int(end_ns),
            },
        )
        part = normalize_projected_rows(_rows_from_result(raw))
        series.extend(part)
    series.sort(key=lambda t: (t.ts_ns, t.chunk_key))
    # re-check global monotonicity after merge
    prev: int | None = None
    for tick in series:
        if prev is not None and tick.ts_ns < prev:
            raise RuntimeError(f"NON_MONOTONE_EVENT_TIME_AFTER_MERGE:{prev}->{tick.ts_ns}")
        prev = tick.ts_ns
        if tick.ts_ns < start_ns or tick.ts_ns >= end_ns:
            raise RuntimeError("MID_OUTSIDE_PILOT_WINDOW")
        if expected_epoch_id and tick.epoch_id and tick.epoch_id != expected_epoch_id:
            raise RuntimeError(
                f"EPOCH_MISMATCH: got={tick.epoch_id} expected={expected_epoch_id}"
            )
    return series


def estimate_mid_rows(
    client: Any,
    *,
    database: str,
    symbol: str,
    start_ns: int,
    end_ns: int,
    chunk_keys: Sequence[str],
) -> int:
    sql = count_metrics_sql(database=database)
    raw = _query(
        client,
        sql,
        {
            "symbol": symbol,
            "chunk_keys": list(chunk_keys),
            "start_ns": int(start_ns),
            "end_ns": int(end_ns),
        },
    )
    rows = _rows_from_result(raw)
    if not rows:
        return 0
    return int(rows[0].get("n") or rows[0].get("count()") or 0)


def mid_tick_to_row(t: MidTick) -> dict[str, Any]:
    return asdict(t)
