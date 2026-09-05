"""Read finalized JSONL.zst segments into importable row dicts."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

import orjson

from orderbook_analyse.orderbook_v2_live.full_ob_edge_flight_recorder.continuity_contract import (
    CHECKPOINT_KINDS,
    levels_to_str_pairs,
)
from orderbook_analyse.orderbook_v2_live.full_ob_edge_flight_recorder.replay import _iter_zst_jsonl

from .ids import payload_hash, record_id


def _as_int(v: Any) -> int | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v)
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        if s.isdigit() or (s.startswith("-") and s[1:].isdigit()):
            return int(s)
        # ISO timestamps are not stored in Int64 columns
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _levels(obj: Any) -> list[tuple[str, str]]:
    pairs = levels_to_str_pairs(obj or [])
    return [(p, q) for p, q in pairs]


def iter_segment_records(
    path: Path,
    *,
    source_sha256: str,
    fight_event_id: str,
    symbol: str,
    topic: str,
    segment_id: str,
    segment_index: int,
    continuation_index: int,
) -> Iterator[dict[str, Any]]:
    ordinal = 0
    for obj in _iter_zst_jsonl(path):
        ordinal += 1
        kind = str(obj.get("record_kind") or obj.get("type") or obj.get("message_type") or "UNKNOWN")
        # envelope variants
        data = obj.get("data") if isinstance(obj.get("data"), dict) else {}
        if kind in ("snapshot", "delta") or kind.upper() in ("SNAPSHOT", "DELTA"):
            # legacy smoke-style
            pass
        u = obj.get("u")
        seq = obj.get("seq")
        if u is None and data:
            u = data.get("u")
        if seq is None and data:
            seq = data.get("seq")
        try:
            u_i = int(u) if u is not None else None
        except (TypeError, ValueError):
            u_i = None
        try:
            seq_i = int(seq) if seq is not None else None
        except (TypeError, ValueError):
            seq_i = None
        epoch = obj.get("continuity_epoch_id")
        try:
            epoch_i = int(epoch) if epoch is not None else None
        except (TypeError, ValueError):
            epoch_i = None
        bids_src = obj.get("b") or data.get("b") or obj.get("bids") or []
        asks_src = obj.get("a") or data.get("a") or obj.get("asks") or []
        # For markers, levels may be absent
        bids = _levels(bids_src)
        asks = _levels(asks_src)
        rid = record_id(
            source_sha256=source_sha256,
            record_ordinal=ordinal,
            record_kind=kind,
            symbol=symbol,
            fight_event_id=fight_event_id,
            continuity_epoch_id=epoch_i,
            u=u_i,
            seq=seq_i,
        )
        raw_bytes = orjson.dumps(obj)
        raw_h = payload_hash(obj)
        can_h = payload_hash(
            {
                "record_kind": kind,
                "u": u_i,
                "seq": seq_i,
                "continuity_epoch_id": epoch_i,
                "bids": bids,
                "asks": asks,
                "ts": obj.get("ts") or obj.get("exchange_ts_ms"),
            }
        )
        # Keep exact levels always; compact huge raw JSON (source file remains SoT).
        if len(raw_bytes) > 8000 and kind in CHECKPOINT_KINDS:
            raw_store = orjson.dumps(
                {
                    "note": "full_raw_retained_in_source_file_only",
                    "record_kind": kind,
                    "raw_payload_hash": raw_h,
                    "book_hash": obj.get("book_hash"),
                    "u": u_i,
                    "seq": seq_i,
                    "continuity_epoch_id": epoch_i,
                    "bid_level_count": len(bids),
                    "ask_level_count": len(asks),
                }
            ).decode("utf-8")
        elif len(raw_bytes) > 200_000:
            raw_store = orjson.dumps(
                {
                    "note": "full_raw_retained_in_source_file_only",
                    "record_kind": kind,
                    "raw_payload_hash": raw_h,
                    "u": u_i,
                    "seq": seq_i,
                }
            ).decode("utf-8")
        else:
            raw_store = raw_bytes.decode("utf-8", errors="replace")
        yield {
            "record_id": rid,
            "record_kind": kind,
            "fight_event_id": fight_event_id,
            "segment_id": segment_id,
            "segment_index": segment_index,
            "continuation_index": continuation_index,
            "record_ordinal": ordinal,
            "symbol": symbol,
            "topic": topic or obj.get("topic") or "",
            "continuity_epoch_id": epoch_i,
            "u": u_i,
            "seq": seq_i,
            "exchange_ts_ms": _as_int(obj.get("ts") if not isinstance(obj.get("ts"), str) else None)
            if obj.get("ts") is not None and not isinstance(obj.get("ts"), str)
            else _as_int(obj.get("exchange_ts_ms")),
            "cts_ms": _as_int(obj.get("cts") if not isinstance(obj.get("cts"), str) else None)
            if obj.get("cts") is not None and not isinstance(obj.get("cts"), str)
            else _as_int(obj.get("cts_ms")),
            "receive_time_ns": _as_int(obj.get("local_receive_time_ns") or obj.get("receive_time_ns")),
            "bids": bids,
            "asks": asks,
            "marker_type": obj.get("marker_type") or obj.get("event_type"),
            "book_hash": obj.get("book_hash"),
            "source_path": str(path),
            "source_sha256": source_sha256,
            "raw_payload_hash": raw_h,
            "canonical_payload_hash": can_h,
            "raw_payload": raw_store,
            "is_checkpoint": kind in CHECKPOINT_KINDS,
        }


def summarize_records(rows: list[dict[str, Any]]) -> dict[str, Any]:
    epochs = {r["continuity_epoch_id"] for r in rows if r.get("continuity_epoch_id") is not None}
    cps = sum(1 for r in rows if r.get("is_checkpoint"))
    first = rows[0] if rows else None
    last = rows[-1] if rows else None
    return {
        "record_count": len(rows),
        "checkpoint_count": cps,
        "continuity_epochs": len(epochs),
        "first_ts": str(first.get("exchange_ts_ms")) if first else None,
        "last_ts": str(last.get("exchange_ts_ms")) if last else None,
        "first_u": first.get("u") if first else None,
        "last_u": last.get("u") if last else None,
        "first_seq": first.get("seq") if first else None,
        "last_seq": last.get("seq") if last else None,
        "record_ids": [r["record_id"] for r in rows],
        "kinds": sorted({r["record_kind"] for r in rows}),
    }
