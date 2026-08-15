"""Deterministic hashing helpers for derivatives import (no secrets)."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping


def json_hash(payload: Mapping[str, Any] | list[Any] | dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def bucket_source_hash(
    *,
    symbol: str,
    bucket_start_iso: str,
    import_version: str,
    field_payload: Mapping[str, Any],
) -> str:
    return json_hash(
        {
            "symbol": symbol,
            "bucket_start": bucket_start_iso,
            "import_version": import_version,
            "fields": dict(field_payload),
        }
    )
