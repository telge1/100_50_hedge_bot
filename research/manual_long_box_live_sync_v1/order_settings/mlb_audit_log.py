"""Structured MLB order audit (no secrets)."""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

DEFAULT_AUDIT_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "mlb_order_audit.jsonl"
)

_FORBIDDEN_KEYS = frozenset(
    {
        "api_key",
        "api_secret",
        "secret",
        "password",
        "signature",
        "authorization",
        "cookie",
        "session",
    }
)


def new_correlation_id() -> str:
    return uuid.uuid4().hex[:16]


def _sanitize(fields: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, val in fields.items():
        lk = str(key).lower()
        if lk in _FORBIDDEN_KEYS or "secret" in lk or "api_key" in lk:
            continue
        if isinstance(val, dict):
            out[key] = _sanitize(val)
        else:
            out[key] = val
    return out


def audit_event(
    event: str,
    *,
    source: str,
    drawing_id: str | None = None,
    correlation_id: str | None = None,
    path: Path | None = None,
    duration_ms: float | None = None,
    **fields: Any,
) -> dict[str, Any]:
    rec: dict[str, Any] = {
        "ts_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "event": event,
        "source": source,
    }
    if drawing_id:
        rec["drawing_id"] = drawing_id
    if correlation_id:
        rec["correlation_id"] = correlation_id
    if duration_ms is not None:
        rec["duration_ms"] = round(float(duration_ms), 2)
    rec.update(_sanitize(fields))
    line = json.dumps(rec, ensure_ascii=False, separators=(",", ":"))
    dest = path or DEFAULT_AUDIT_PATH
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    return rec
