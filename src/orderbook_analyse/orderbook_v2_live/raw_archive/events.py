"""Canonical raw OB200 event serialization (replay-compatible)."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import orjson

from orderbook_analyse.orderbook_v2.book import BookState, sorted_asks, sorted_bids
from orderbook_analyse.orderbook_v2_live.raw_archive.config import FORMAT_VERSION, PARSER_VERSION

LIFECYCLE_TYPES = frozenset(
    {
        "CONNECT",
        "DISCONNECT",
        "RECONNECT",
        "RESYNC",
        "SEQUENCE_GAP",
        "QUEUE_OVERFLOW",
        "WRITER_ERROR",
        "ROTATION_CHECKPOINT",
        "CLEAN_CLOSE",
    }
)


def utc_iso(ts: datetime) -> str:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def book_state_to_data(book: BookState, symbol: str) -> dict[str, Any]:
    bids = [[format(p, "f"), format(q, "f")] for p, q in sorted_bids(book)]
    asks = [[format(p, "f"), format(q, "f")] for p, q in sorted_asks(book)]
    return {
        "s": symbol,
        "b": bids,
        "a": asks,
        "u": int(book.last_u),
        "seq": int(book.last_seq),
    }


def serialize_market_payload(
    payload: dict[str, Any],
    *,
    received_at: datetime,
    depth: int = 200,
) -> bytes:
    """Preserve native Bybit WS payload (snapshot/delta) for replayer compatibility."""
    record = dict(payload)
    record.setdefault("format_version", FORMAT_VERSION)
    record.setdefault("parser_version", PARSER_VERSION)
    record.setdefault("depth", depth)
    record["local_receive_ts"] = utc_iso(received_at)
    return orjson.dumps(record) + b"\n"


def serialize_lifecycle(
    event_type: str,
    *,
    symbol: str | None = None,
    ts: datetime | None = None,
    received_at: datetime | None = None,
    depth: int = 200,
    details: dict[str, Any] | None = None,
) -> bytes:
    now = ts or datetime.now(timezone.utc)
    record: dict[str, Any] = {
        "archive_event": event_type,
        "format_version": FORMAT_VERSION,
        "parser_version": PARSER_VERSION,
        "depth": depth,
        "ts": int(now.timestamp() * 1000),
        "local_receive_ts": utc_iso(received_at or now),
    }
    if symbol:
        record["symbol"] = symbol
    if details:
        record["details"] = details
    return orjson.dumps(record) + b"\n"


def serialize_rotation_checkpoint(
    book: BookState,
    symbol: str,
    *,
    topic: str,
    ts_ms: int,
    received_at: datetime,
    depth: int = 200,
) -> bytes:
    record = {
        "topic": topic,
        "type": "rotation_checkpoint",
        "source": "local_book_state",
        "format_version": FORMAT_VERSION,
        "parser_version": PARSER_VERSION,
        "depth": depth,
        "ts": ts_ms,
        "cts": None,
        "local_receive_ts": utc_iso(received_at),
        "data": book_state_to_data(book, symbol),
    }
    return orjson.dumps(record) + b"\n"


def is_replayable_line(obj: dict[str, Any]) -> bool:
    if "archive_event" in obj:
        return False
    msg_type = obj.get("type")
    if msg_type in ("snapshot", "delta"):
        return True
    if msg_type == "rotation_checkpoint":
        return True
    return False


def line_to_replay_payload(obj: dict[str, Any]) -> dict[str, Any]:
    """Convert archive line to parse_ob200_obj-compatible WS payload."""
    if obj.get("type") == "rotation_checkpoint":
        return {
            "topic": obj.get("topic") or "",
            "type": "snapshot",
            "ts": obj.get("ts"),
            "cts": obj.get("cts"),
            "data": obj.get("data") or {},
        }
    return {
        "topic": obj.get("topic") or "",
        "type": obj.get("type"),
        "ts": obj.get("ts"),
        "cts": obj.get("cts"),
        "data": obj.get("data") or {},
    }


def books_equal(a: dict[Decimal, Decimal], b: dict[Decimal, Decimal]) -> bool:
    return dict(a) == dict(b)
