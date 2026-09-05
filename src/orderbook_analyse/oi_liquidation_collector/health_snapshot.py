"""Atomic on-disk health snapshot for OI/liquidation collector supervision."""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class HealthSnapshot:
    process_alive: bool = True
    websocket_alive: bool = False
    writer_alive: bool = False
    clickhouse_reachable: bool = False
    last_ws_message_ts: float | None = None
    last_oi_received_ts: float | None = None
    last_liquidation_received_ts: float | None = None
    last_successful_insert_ts: float | None = None
    last_oi_persisted_ts: float | None = None
    last_liquidation_persisted_ts: float | None = None
    persistence_lag_seconds: float | None = None
    queue_depth: int = 0
    queue_capacity: int = 0
    queue_drop_count: int = 0
    writer_error_count: int = 0
    writer_restart_count: int = 0
    clickhouse_reconnect_count: int = 0
    websocket_reconnect_count: int = 0
    spool_unacked_records: int = 0
    spool_unacked_bytes: int = 0
    health_status: str = "UNKNOWN"
    health_reasons: list[str] = field(default_factory=list)
    updated_unix: float = 0.0
    pid: int = 0
    collector_instance_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def evaluate_health(
    snap: HealthSnapshot,
    *,
    persistence_lag_fail_sec: float,
    oi_stale_fail_sec: float,
    now: float | None = None,
) -> HealthSnapshot:
    """Classify health. Missing liquidations alone never makes RED."""
    now = time.time() if now is None else now
    reasons: list[str] = []
    status = "GREEN"

    if not snap.process_alive:
        reasons.append("process_not_alive")
        status = "RED"
    if not snap.writer_alive:
        reasons.append("writer_dead")
        status = "RED"
    if not snap.websocket_alive:
        reasons.append("websocket_down")
        if status != "RED":
            status = "YELLOW"

    lag = snap.persistence_lag_seconds
    if lag is None and snap.last_oi_received_ts is not None and snap.last_successful_insert_ts is not None:
        lag = max(0.0, snap.last_oi_received_ts - snap.last_successful_insert_ts)
        snap.persistence_lag_seconds = lag
    elif lag is None and snap.last_oi_received_ts is not None and snap.last_successful_insert_ts is None:
        lag = max(0.0, now - snap.last_oi_received_ts)
        snap.persistence_lag_seconds = lag

    if snap.websocket_alive and snap.last_oi_received_ts is not None:
        if snap.last_successful_insert_ts is None or (
            lag is not None and lag >= persistence_lag_fail_sec
        ):
            reasons.append("persistence_lag")
            status = "RED"
        if (now - snap.last_oi_received_ts) >= oi_stale_fail_sec:
            reasons.append("oi_receive_stale")
            if status != "RED":
                status = "YELLOW"

    if not snap.clickhouse_reachable and snap.writer_alive:
        reasons.append("clickhouse_unreachable")
        if status == "GREEN":
            status = "YELLOW"

    if snap.queue_drop_count > 0:
        reasons.append("queue_drops")
        if status == "GREEN":
            status = "YELLOW"

    # Explicit: liquidation silence is NOT a reason.
    snap.health_reasons = reasons
    snap.health_status = status
    snap.updated_unix = now
    return snap


def write_health_atomic(path: Path, snap: HealthSnapshot) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    payload = json.dumps(snap.to_dict(), separators=(",", ":"), sort_keys=True) + "\n"
    fd = os.open(str(tmp), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o644)
    try:
        os.write(fd, payload.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def read_health(path: Path) -> dict[str, Any] | None:
    path = Path(path)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
