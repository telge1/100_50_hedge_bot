"""Full-OB continuity epoch + resync checkpoint contract (v1).

Contract id: full_ob_resync_checkpoint_v1

A reconnect does not invent missing order flow. It:
  1) documents the unobserved interval (RESYNC_BOUNDARY),
  2) stores a full immutable book seed (RESYNC_CHECKPOINT),
  3) starts a new continuity epoch for subsequent BOOK_DELTA records.

research_eligible requires continuous_capture=true (no unobserved transport gap).
replayable_by_epochs can be true when every epoch has a valid checkpoint seed.
"""

from __future__ import annotations

import hashlib
from typing import Any

RESYNC_CHECKPOINT_CONTRACT = "full_ob_resync_checkpoint_v1"

RECORD_INITIAL_CHECKPOINT = "INITIAL_CHECKPOINT"
RECORD_BOOK_DELTA = "BOOK_DELTA"
RECORD_RESYNC_BOUNDARY = "RESYNC_BOUNDARY"
RECORD_RESYNC_CHECKPOINT = "RESYNC_CHECKPOINT"
RECORD_EVENT_MARKER = "EVENT_MARKER"
RECORD_EVENT_END = "EVENT_END"

CHECKPOINT_KINDS = frozenset({RECORD_INITIAL_CHECKPOINT, RECORD_RESYNC_CHECKPOINT})
BOUNDARY_KINDS = frozenset({RECORD_RESYNC_BOUNDARY})
NON_DELTA_KINDS = frozenset(
    {
        RECORD_INITIAL_CHECKPOINT,
        RECORD_RESYNC_BOUNDARY,
        RECORD_RESYNC_CHECKPOINT,
        RECORD_EVENT_MARKER,
        RECORD_EVENT_END,
    }
)


def levels_to_str_pairs(levels: Any) -> list[list[str]]:
    out: list[list[str]] = []
    for row in levels or []:
        if isinstance(row, (list, tuple)) and len(row) >= 2:
            out.append([str(row[0]), str(row[1])])
        elif isinstance(row, dict):
            out.append([str(row.get("price")), str(row.get("size") or row.get("qty") or "0")])
    return out


def book_content_hash(*, bids: list, asks: list) -> str:
    """Deterministic hash over string price/qty levels (order-independent per side)."""
    b = levels_to_str_pairs(bids)
    a = levels_to_str_pairs(asks)
    parts = [f"B|{p}|{q}" for p, q in sorted(b, key=lambda x: (float(x[0]), x[0]))]
    parts += [f"A|{p}|{q}" for p, q in sorted(a, key=lambda x: (float(x[0]), x[0]))]
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def best_bid_ask_from_levels(bids: list, asks: list) -> tuple[str | None, str | None]:
    b = levels_to_str_pairs(bids)
    a = levels_to_str_pairs(asks)
    bb = max(b, key=lambda x: float(x[0]))[0] if b else None
    ba = min(a, key=lambda x: float(x[0]))[0] if a else None
    return bb, ba


def build_checkpoint_record(
    *,
    record_kind: str,
    fight_event_id: str,
    continuity_epoch_id: int,
    record_ordinal: int,
    symbol: str,
    topic: str,
    snapshot: dict[str, Any],
    receive_time_ns: int | None,
    segment_index: int,
    resync_reason: str | None = None,
    reconnect_count: int | None = None,
    prev_u: int | None = None,
    prev_seq: int | None = None,
) -> dict[str, Any]:
    bids = levels_to_str_pairs(snapshot.get("b") or [])
    asks = levels_to_str_pairs(snapshot.get("a") or [])
    u = snapshot.get("u")
    seq = snapshot.get("seq")
    ts = snapshot.get("ts")
    cts = snapshot.get("cts")
    bb, ba = best_bid_ask_from_levels(bids, asks)
    digest = book_content_hash(bids=bids, asks=asks)
    return {
        "channel": "full_ob_continuity",
        "record_kind": record_kind,
        "contract": RESYNC_CHECKPOINT_CONTRACT,
        "fight_event_id": fight_event_id,
        "continuity_epoch_id": int(continuity_epoch_id),
        "record_ordinal": int(record_ordinal),
        "symbol": symbol.upper(),
        "topic": topic,
        "segment_index": int(segment_index),
        "ts": ts,
        "cts": cts,
        "local_receive_time_ns": receive_time_ns,
        "resync_reason": resync_reason,
        "reconnect_count": reconnect_count,
        "prev_u": prev_u,
        "prev_seq": prev_seq,
        "book_hash": digest,
        "bid_level_count": len(bids),
        "ask_level_count": len(asks),
        "best_bid": bb,
        "best_ask": ba,
        "data": {
            "s": snapshot.get("s") or symbol.upper(),
            "b": bids,
            "a": asks,
            "u": u,
            "seq": seq,
        },
        "type": "snapshot",
        "flight_phase": "checkpoint",
    }


def build_resync_boundary_record(
    *,
    fight_event_id: str,
    continuity_epoch_id: int,
    record_ordinal: int,
    symbol: str,
    segment_index: int,
    reason: str,
    prev_u: int | None,
    prev_seq: int | None,
    prev_exchange_ts_ms: int | None,
    prev_receive_time_ns: int | None,
    disconnect_ts_iso: str,
    reconnect_ts_iso: str,
    receive_time_ns: int | None,
) -> dict[str, Any]:
    return {
        "channel": "full_ob_continuity",
        "record_kind": RECORD_RESYNC_BOUNDARY,
        "contract": RESYNC_CHECKPOINT_CONTRACT,
        "fight_event_id": fight_event_id,
        "continuity_epoch_id": int(continuity_epoch_id),
        "record_ordinal": int(record_ordinal),
        "symbol": symbol.upper(),
        "segment_index": int(segment_index),
        "marker_type": "RESYNC_BOUNDARY",
        "ts": reconnect_ts_iso,
        "local_receive_time_ns": receive_time_ns,
        "resync_reason": reason,
        "prev_u": prev_u,
        "prev_seq": prev_seq,
        "prev_exchange_ts_ms": prev_exchange_ts_ms,
        "prev_receive_time_ns": prev_receive_time_ns,
        "disconnect_ts": disconnect_ts_iso,
        "reconnect_ts": reconnect_ts_iso,
        "type": "boundary",
        "flight_phase": "resync_boundary",
    }


def annotate_delta_record(
    record: dict[str, Any],
    *,
    fight_event_id: str,
    continuity_epoch_id: int,
    record_ordinal: int,
    segment_index: int,
) -> dict[str, Any]:
    out = dict(record)
    out["record_kind"] = RECORD_BOOK_DELTA
    out["contract"] = RESYNC_CHECKPOINT_CONTRACT
    out["fight_event_id"] = fight_event_id
    out["continuity_epoch_id"] = int(continuity_epoch_id)
    out["record_ordinal"] = int(record_ordinal)
    out["segment_index"] = int(segment_index)
    return out


def annotate_marker_record(
    record: dict[str, Any],
    *,
    fight_event_id: str,
    continuity_epoch_id: int,
    record_ordinal: int,
    segment_index: int,
) -> dict[str, Any]:
    out = dict(record)
    out["record_kind"] = RECORD_EVENT_MARKER
    out["contract"] = RESYNC_CHECKPOINT_CONTRACT
    out["fight_event_id"] = fight_event_id
    out["continuity_epoch_id"] = int(continuity_epoch_id)
    out["record_ordinal"] = int(record_ordinal)
    out["segment_index"] = int(segment_index)
    return out


def is_continuity_control_record(rec: dict[str, Any]) -> bool:
    kind = rec.get("record_kind")
    if kind in NON_DELTA_KINDS:
        return True
    if rec.get("channel") == "marker" or rec.get("marker_type"):
        return True
    if rec.get("channel") == "full_ob_continuity" and kind != RECORD_BOOK_DELTA:
        return True
    return False


def is_book_delta_record(rec: dict[str, Any]) -> bool:
    if is_continuity_control_record(rec):
        return False
    if rec.get("record_kind") == RECORD_BOOK_DELTA:
        return True
    # Legacy / compact envelopes: Bybit delta with u present.
    data = rec.get("data") if isinstance(rec.get("data"), dict) else {}
    if data.get("u") is None:
        return False
    if rec.get("channel") in {"marker", "full_ob_continuity", "lifecycle"}:
        return False
    t = str(rec.get("type") or "delta").lower()
    if t in {"delta", "snapshot"} and rec.get("record_kind") in CHECKPOINT_KINDS:
        return False
    if t == "delta" or rec.get("flight_phase") in {"live", "buffer", None}:
        return True
    return False
