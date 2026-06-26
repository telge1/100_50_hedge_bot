from __future__ import annotations

import json
import logging
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import log_throttle


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
    "fixed_cycle_fast_path_skip",
    "fixed_cycle_structure_skip",
    "fixed_cycle_downside_skip",
    "fixed_cycle_exit_skip",
    "fixed_cycle_fill_state",
    "fixed_cycle_rebuild_state",
    "fixed_cycle_pre_break_even_state",
    "fixed_cycle_post_tp_state",
    "fixed_cycle_exit_lock_check",
    "fixed_cycle_downside_build_result",
    "order_reconciled_open",
}


def _current_timestamp_iso() -> str:
    target_zone = timezone(timedelta(hours=3))
    return datetime.now(timezone.utc).astimezone(target_zone).isoformat()


class AuditLogger:
    def __init__(
        self,
        logger: logging.Logger,
        audit_log_path: str | Path | None = None,
        *,
        extra_fields: dict[str, Any] | None = None,
        runtime_state: Any | None = None,
    ) -> None:
        self.logger = logger
        self.audit_log_path = Path(audit_log_path) if audit_log_path else None
        if self.audit_log_path:
            self.audit_log_path.parent.mkdir(parents=True, exist_ok=True)
        self.extra_fields = dict(extra_fields) if extra_fields else {}
        self._runtime_state = runtime_state

    def bind_runtime_state(self, runtime_state: Any) -> None:
        self._runtime_state = runtime_state

    def _resolve_strategy_state(self) -> dict[str, Any] | None:
        runtime_state = self._runtime_state
        if runtime_state is None:
            return None
        strategy_state = getattr(runtime_state, "strategy_state", None)
        if isinstance(strategy_state, dict):
            return strategy_state
        return None

    def log_event(self, event: str, **payload: Any) -> None:
        record = {"event": event, **self.extra_fields, **payload}
        if log_throttle.is_throttled_info_event(event):
            strategy_state = self._resolve_strategy_state()
            throttle_state = log_throttle.resolve_throttle_state(record, strategy_state)
            decision = log_throttle.should_log_throttled_event(event, record, throttle_state)
            if not decision.should_log:
                return
            if decision.suppressed_count > 0:
                record["suppressed_count"] = decision.suppressed_count
                record["throttle_interval_sec"] = decision.throttle_interval_sec

        record["timestamp"] = _current_timestamp_iso()
        log_method = self.logger.debug if event in DEBUG_EVENTS else self.logger.info
        log_method("%s %s", event, json.dumps(record, default=_json_default, sort_keys=True))
        if not self.audit_log_path:
            return
        with self.audit_log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, default=_json_default, sort_keys=True) + "\n")

    def update_extra_fields(self, fields: dict[str, Any]) -> None:
        self.extra_fields.update(fields)

    def set_audit_log_path(self, audit_log_path: str | Path | None) -> None:
        if audit_log_path:
            self.audit_log_path = Path(audit_log_path)
            self.audit_log_path.parent.mkdir(parents=True, exist_ok=True)
        else:
            self.audit_log_path = None
