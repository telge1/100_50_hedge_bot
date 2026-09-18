"""Full-book checkpoint construction and canonical hashing."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from orderbook_analyse.orderbook_v2_live.full_book_state import ConsistentBookSnapshot

from .config import SCHEMA_VERSION
from .envelope import deterministic_json_bytes, payload_sha256

CHECKPOINT_REASONS = frozenset(
    {"periodic_5m", "segment_start", "exchange_snapshot", "reconnect_resync", "shutdown"}
)


def _decimal_string(value: Any) -> str:
    text = format(Decimal(str(value)), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


def levels_to_str_pairs(levels: list[list[Any]]) -> list[list[str]]:
    return [[_decimal_string(row[0]), _decimal_string(row[1])] for row in levels]


def canonical_book_levels(
    bids: list[list[Any]], asks: list[list[Any]]
) -> tuple[list[list[str]], list[list[str]]]:
    bid_pairs = levels_to_str_pairs(bids)
    ask_pairs = levels_to_str_pairs(asks)
    bid_pairs.sort(key=lambda row: Decimal(row[0]), reverse=True)
    ask_pairs.sort(key=lambda row: Decimal(row[0]))
    return bid_pairs, ask_pairs


def book_sha256(bids: list[list[Any]], asks: list[list[Any]]) -> str:
    cbids, casks = canonical_book_levels(bids, asks)
    return hashlib.sha256(deterministic_json_bytes({"asks": casks, "bids": cbids})).hexdigest()


def build_checkpoint_record(
    snapshot: ConsistentBookSnapshot,
    *,
    reason: str,
    archive_instance_id: str,
    collector_instance_id: str,
    source_snapshot_id: str | None = None,
    checkpoint_time: datetime | None = None,
) -> dict[str, Any]:
    if reason not in CHECKPOINT_REASONS:
        raise ValueError(f"invalid checkpoint reason: {reason}")
    if not snapshot.book_ready:
        raise ValueError("checkpoint requires ready book")
    raw_bids, raw_asks = snapshot.full_levels()
    bids, asks = canonical_book_levels(raw_bids, raw_asks)
    for side_name, side in (("bids", bids), ("asks", asks)):
        for price, size in side:
            if Decimal(size) < 0:
                raise ValueError(f"negative {side_name} size at {price}")
            if Decimal(price) <= 0:
                raise ValueError(f"non-positive {side_name} price {price}")
    if bids and asks and Decimal(bids[0][0]) >= Decimal(asks[0][0]):
        raise ValueError("crossed book in checkpoint")
    now = checkpoint_time or datetime.now(timezone.utc)
    now_ns = int(now.timestamp() * 1_000_000_000)
    event_ns = None if snapshot.event_ts_ms is None else int(snapshot.event_ts_ms) * 1_000_000
    payload = {
        "schema_version": SCHEMA_VERSION,
        "symbol": snapshot.symbol.upper(),
        "checkpoint_time": now.isoformat().replace("+00:00", "Z"),
        "event_time": event_ns,
        "receive_time": snapshot.receive_time_ns,
        "u": snapshot.update_id,
        "seq": snapshot.seq,
        "bids": bids,
        "asks": asks,
        "bid_level_count": len(bids),
        "ask_level_count": len(asks),
        "best_bid": bids[0][0] if bids else None,
        "best_ask": asks[0][0] if asks else None,
        "book_sha256": book_sha256(bids, asks),
        "source_snapshot_id": source_snapshot_id,
        "checkpoint_reason": reason,
        "archive_instance_id": archive_instance_id,
        "collector_instance_id": collector_instance_id,
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "archive_instance_id": archive_instance_id,
        "collector_instance_id": collector_instance_id,
        "symbol": snapshot.symbol.upper(),
        "topic": f"orderbook.full.{snapshot.symbol.upper()}",
        "message_type": "checkpoint",
        "event_time_ns": event_ns,
        "receive_time_ns": snapshot.receive_time_ns or now_ns,
        "archive_time_ns": now_ns,
        "u": snapshot.update_id,
        "seq": snapshot.seq,
        "original_payload": payload,
        "payload_sha256": payload_sha256(payload),
    }
