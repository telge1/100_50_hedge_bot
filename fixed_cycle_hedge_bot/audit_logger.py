from __future__ import annotations

import json
import logging
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, Path):
        return str(value)
    return str(value)


DEBUG_EVENTS = {
    "snapshot_refreshed",
    "strategy_noop",
    "position_ws_synced",
    "ws_event",
    "order_payload_ready",
    "intent_submitted",
    "closed_pnl_fetch_started",
    "closed_pnl_not_yet_available",
    "closed_pnl_row_found",
    "short_tp_build_deferred",
}


def _current_timestamp_iso() -> str:
    target_zone = timezone(timedelta(hours=3))
    return datetime.now(timezone.utc).astimezone(target_zone).isoformat()


class AuditLogger:
    def __init__(self, logger: logging.Logger, audit_log_path: str | Path | None = None) -> None:
        self.logger = logger
        self.audit_log_path = Path(audit_log_path) if audit_log_path else None
        if self.audit_log_path:
            self.audit_log_path.parent.mkdir(parents=True, exist_ok=True)

    def log_event(self, event: str, **payload: Any) -> None:
        record = {"event": event, **payload}
        record["timestamp"] = _current_timestamp_iso()
        log_method = self.logger.debug if event in DEBUG_EVENTS else self.logger.info
        log_method("%s %s", event, json.dumps(record, default=_json_default, sort_keys=True))
        if not self.audit_log_path:
            return
        with self.audit_log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, default=_json_default, sort_keys=True) + "\n")
