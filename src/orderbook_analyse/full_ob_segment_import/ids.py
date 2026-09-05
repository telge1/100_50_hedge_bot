"""Deterministic record / segment IDs for idempotent ClickHouse import."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | bytes) -> str:
    from pathlib import Path

    p = Path(path)
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def record_id(
    *,
    source_sha256: str,
    record_ordinal: int,
    record_kind: str,
    symbol: str,
    fight_event_id: str,
    continuity_epoch_id: int | None,
    u: int | None,
    seq: int | None,
) -> str:
    """Stable 64-hex id. Same logical source record → same id forever."""
    payload = "|".join(
        [
            source_sha256,
            str(int(record_ordinal)),
            str(record_kind or ""),
            str(symbol or "").upper(),
            str(fight_event_id or ""),
            "" if continuity_epoch_id is None else str(int(continuity_epoch_id)),
            "" if u is None else str(int(u)),
            "" if seq is None else str(int(seq)),
        ]
    )
    return sha256_hex(payload.encode("utf-8"))


def segment_key(*, fight_event_id: str, continuation_index: int, source_sha256: str) -> str:
    return sha256_hex(f"{fight_event_id}|{continuation_index}|{source_sha256}".encode("utf-8"))


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def payload_hash(obj: Any) -> str:
    return sha256_hex(canonical_json(obj).encode("utf-8"))
