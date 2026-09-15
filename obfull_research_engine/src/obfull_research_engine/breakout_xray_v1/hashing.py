"""Deterministic hashing helpers for X-Ray reports."""

from __future__ import annotations

import hashlib
import json
from typing import Any


VOLATILE_KEYS = frozenset(
    {
        "report_hash",
        "run_log",
        "lock",
        "created_at",
        "run_started_at",
        "run_finished_at",
        "wall_clock_ms",
    }
)


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _strip_volatile(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {
            k: _strip_volatile(v)
            for k, v in obj.items()
            if k not in VOLATILE_KEYS
        }
    if isinstance(obj, list):
        return [_strip_volatile(x) for x in obj]
    return obj


def report_content_hash(payload: dict[str, Any]) -> str:
    cleaned = _strip_volatile(payload)
    return sha256_hex(canonical_json(cleaned))


def round_float(x: float, ndigits: int = 8) -> float:
    return round(float(x), ndigits)
