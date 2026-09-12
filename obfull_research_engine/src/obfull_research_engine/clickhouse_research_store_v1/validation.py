"""Bounded validation for the 60s Full-OB ClickHouse pilot window."""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from . import DATABASE, EVENTS_TABLE
from .helpers import iso_to_ns_exact, parse_iso_ns


def _as_text(value: Any) -> str:
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8")
    return str(value)


def deterministic_payload_sha256(payload_obj: Any) -> str:
    raw = json.dumps(
        payload_obj,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def validate_pilot_window(
    *,
    client: Any,
    symbol: str,
    window_start: str,
    window_end: str,
    expected_rows: int,
    samples: list[Any],
    source_segment_sha256: str,
) -> dict[str, Any]:
    """Validate only the filtered partition/window. Returns {ok, reason, metrics}."""
    start_ns = iso_to_ns_exact(window_start)
    end_ns = iso_to_ns_exact(window_end)

    t0 = time.monotonic()
    # FINAL only on narrow event_time_ns filter (do not trust DateTime day partition alone).
    sql = f"""
    SELECT
      count() AS cnt,
      uniqExact(record_id) AS uniq_ids,
      min(event_time_ns) AS min_et,
      max(event_time_ns) AS max_et,
      min(receive_time_ns) AS min_rt,
      max(receive_time_ns) AS max_rt,
      minIf(update_id, update_id_present = 1) AS min_u,
      maxIf(update_id, update_id_present = 1) AS max_u,
      minIf(seq, seq_present = 1) AS min_seq,
      maxIf(seq, seq_present = 1) AS max_seq
    FROM {DATABASE}.{EVENTS_TABLE} FINAL
    WHERE symbol = {{symbol:String}}
      AND event_time_ns >= {{start_ns:UInt64}}
      AND event_time_ns < {{end_ns:UInt64}}
    """
    try:
        row = client.query(
            sql,
            parameters={
                "symbol": symbol.upper(),
                "start_ns": start_ns,
                "end_ns": end_ns,
            },
        ).result_rows[0]
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"query failed: {exc}", "query_s": time.monotonic() - t0}

    cnt, uniq_ids, min_et, max_et, min_rt, max_rt, min_u, max_u, min_seq, max_seq = row
    cnt = int(cnt)
    uniq_ids = int(uniq_ids)
    query_s = time.monotonic() - t0

    if cnt != expected_rows:
        return {
            "ok": False,
            "reason": f"count {cnt} != expected_rows {expected_rows}",
            "count": cnt,
            "uniq": uniq_ids,
            "query_s": query_s,
        }
    if cnt != uniq_ids:
        return {
            "ok": False,
            "reason": f"count {cnt} != uniqExact(record_id) {uniq_ids}",
            "count": cnt,
            "uniq": uniq_ids,
            "query_s": query_s,
        }
    if cnt > 0:
        if int(min_et) < start_ns or int(max_et) >= end_ns:
            return {
                "ok": False,
                "reason": f"event_time outside window min={min_et} max={max_et}",
                "query_s": query_s,
            }

    # message_type distribution
    mt_sql = f"""
    SELECT message_type, count() AS c
    FROM {DATABASE}.{EVENTS_TABLE} FINAL
    WHERE symbol = {{symbol:String}}
      AND event_time_ns >= {{start_ns:UInt64}}
      AND event_time_ns < {{end_ns:UInt64}}
    GROUP BY message_type
    ORDER BY message_type
    """
    mt_rows = client.query(
        mt_sql,
        parameters={
            "symbol": symbol.upper(),
            "start_ns": start_ns,
            "end_ns": end_ns,
        },
    ).result_rows
    message_types = {str(a): int(b) for a, b in mt_rows}

    # ordinal monotonicity in stored set
    ord_sql = f"""
    SELECT record_ordinal
    FROM {DATABASE}.{EVENTS_TABLE} FINAL
    WHERE symbol = {{symbol:String}}
      AND event_time_ns >= {{start_ns:UInt64}}
      AND event_time_ns < {{end_ns:UInt64}}
    ORDER BY record_ordinal
    """
    ords = [
        int(r[0])
        for r in client.query(
            ord_sql,
            parameters={
                "symbol": symbol.upper(),
                "start_ns": start_ns,
                "end_ns": end_ns,
            },
        ).result_rows
    ]
    if ords != sorted(ords) or len(ords) != len(set(ords)):
        return {"ok": False, "reason": "record_ordinal not unique/sorted", "query_s": query_s}

    sample_checks: list[dict[str, Any]] = []
    for sample in samples:
        rid = _as_text(sample.record_id if hasattr(sample, "record_id") else sample["record_id"])
        s_sql = f"""
        SELECT record_id, record_ordinal, payload_sha256, event_time_ns, message_type, original_payload
        FROM {DATABASE}.{EVENTS_TABLE} FINAL
        WHERE symbol = {{symbol:String}}
          AND record_id = {{rid:String}}
        LIMIT 1
        """
        got = client.query(
            s_sql,
            parameters={"symbol": symbol.upper(), "rid": rid},
        ).result_rows
        if not got:
            return {"ok": False, "reason": f"sample record_id missing: {rid}", "query_s": query_s}
        g = got[0]
        exp_ord = sample.record_ordinal if hasattr(sample, "record_ordinal") else sample["record_ordinal"]
        exp_sha = _as_text(sample.payload_sha256 if hasattr(sample, "payload_sha256") else sample["payload_sha256"])
        exp_et = sample.event_time_ns if hasattr(sample, "event_time_ns") else sample["event_time_ns"]
        exp_mt = _as_text(sample.message_type if hasattr(sample, "message_type") else sample["message_type"])
        exp_payload = _as_text(
            sample.original_payload if hasattr(sample, "original_payload") else sample["original_payload"]
        )
        got_sha = _as_text(g[2])
        got_payload = _as_text(g[5])
        got_mt = _as_text(g[4])
        if int(g[1]) != int(exp_ord):
            return {"ok": False, "reason": f"ordinal mismatch for {rid}", "query_s": query_s}
        if got_sha != exp_sha:
            return {"ok": False, "reason": f"payload_sha256 mismatch for {rid}", "query_s": query_s}
        if int(g[3]) != int(exp_et):
            return {"ok": False, "reason": f"event_time_ns mismatch for {rid}", "query_s": query_s}
        if got_mt != exp_mt:
            return {"ok": False, "reason": f"message_type mismatch for {rid}", "query_s": query_s}
        if got_payload != exp_payload:
            return {"ok": False, "reason": f"original_payload mismatch for {rid}", "query_s": query_s}
        # Recompute hash from stored payload JSON
        try:
            obj = json.loads(got_payload)
            recomputed = deterministic_payload_sha256(obj)
        except json.JSONDecodeError:
            return {"ok": False, "reason": f"stored payload not JSON for {rid}", "query_s": query_s}
        if recomputed != exp_sha:
            return {
                "ok": False,
                "reason": f"recomputed payload hash mismatch for {rid}",
                "query_s": query_s,
            }
        sample_checks.append(
            {
                "record_id": rid,
                "record_ordinal": int(exp_ord),
                "payload_sha256": exp_sha,
                "ok": True,
            }
        )

    # Ensure no rows outside window slipped into same day partition for this segment+window insert
    # (already filtered by event_time_ns in query)

    _ = source_segment_sha256  # reserved for future lineage checks
    return {
        "ok": True,
        "reason": "",
        "count": cnt,
        "uniq": uniq_ids,
        "min_event_time_ns": int(min_et) if min_et is not None else None,
        "max_event_time_ns": int(max_et) if max_et is not None else None,
        "min_receive_time_ns": int(min_rt) if min_rt is not None else None,
        "max_receive_time_ns": int(max_rt) if max_rt is not None else None,
        "min_update_id": int(min_u) if min_u is not None else None,
        "max_update_id": int(max_u) if max_u is not None else None,
        "min_seq": int(min_seq) if min_seq is not None else None,
        "max_seq": int(max_seq) if max_seq is not None else None,
        "message_types": message_types,
        "sample_checks": sample_checks,
        "query_s": query_s,
        "ordinals_ok": True,
    }
