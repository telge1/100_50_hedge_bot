"""Deterministic, versioned NDJSON envelopes."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from copy import deepcopy
from datetime import datetime
from typing import Any

from .config import SCHEMA_VERSION


def deterministic_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def payload_sha256(payload: Any) -> str:
    return hashlib.sha256(deterministic_json_bytes(payload)).hexdigest()


def serialize_envelope(envelope: dict[str, Any]) -> bytes:
    return deterministic_json_bytes(envelope) + b"\n"


def _event_time_ns(payload: dict[str, Any]) -> int | None:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    value = payload.get("ts")
    if value is None:
        value = data.get("ts")
    return None if value is None else int(value) * 1_000_000


def build_envelope(
    payload: dict[str, Any],
    *,
    archive_instance_id: str,
    collector_instance_id: str,
    symbol: str,
    receive_time_ns: int,
    message_type: str | None = None,
    archive_time_ns: int | None = None,
) -> dict[str, Any]:
    original = deepcopy(payload)
    data = original.get("data") if isinstance(original.get("data"), dict) else {}
    kind = str(message_type or original.get("type") or data.get("type") or "delta").lower()
    return {
        "schema_version": SCHEMA_VERSION,
        "archive_instance_id": archive_instance_id,
        "collector_instance_id": collector_instance_id,
        "symbol": symbol.upper(),
        "topic": original.get("topic"),
        "message_type": kind,
        "event_time_ns": _event_time_ns(original),
        "receive_time_ns": int(receive_time_ns),
        "archive_time_ns": int(archive_time_ns or time.time_ns()),
        "u": None if data.get("u") is None else int(data["u"]),
        "seq": None if data.get("seq") is None else int(data["seq"]),
        "original_payload": original,
        "payload_sha256": payload_sha256(original),
    }


def build_marker_envelope(
    *,
    archive_instance_id: str,
    collector_instance_id: str,
    symbol: str,
    message_type: str,
    details: dict[str, Any],
    receive_time_ns: int | None = None,
) -> dict[str, Any]:
    now_ns = int(receive_time_ns or time.time_ns())
    payload = {"details": deepcopy(details)}
    return {
        "schema_version": SCHEMA_VERSION,
        "archive_instance_id": archive_instance_id,
        "collector_instance_id": collector_instance_id,
        "symbol": symbol.upper(),
        "topic": f"orderbook.full.{symbol.upper()}",
        "message_type": message_type,
        "event_time_ns": now_ns,
        "receive_time_ns": now_ns,
        "archive_time_ns": time.time_ns(),
        "u": details.get("u"),
        "seq": details.get("seq"),
        "original_payload": payload,
        "payload_sha256": payload_sha256(payload),
    }


def new_archive_instance_id() -> str:
    return f"full-ob-archive-{uuid.uuid4().hex}"
