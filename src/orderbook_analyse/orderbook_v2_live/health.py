"""Health snapshot helpers (no secrets, no raw WS payloads)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


STATES = (
    "STARTING",
    "WAITING_FOR_SNAPSHOT",
    "LIVE",
    "RECONNECTING",
    "STALE",
    "ERROR",
    "STOPPING",
    "STOPPED",
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    idx = min(len(ordered) - 1, max(0, int(round((p / 100.0) * (len(ordered) - 1)))))
    return float(ordered[idx])


def dt_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def write_health_line(path: Path | None, payload: dict[str, Any]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    clean = json.loads(json.dumps(payload, default=str))
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(clean, separators=(",", ":")) + "\n")
