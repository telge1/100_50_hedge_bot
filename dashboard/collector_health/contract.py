"""Status model, thresholds, JSON sanitization."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

# Derived from Phase A production frequencies (see results/collector_health_backfill_audit_v1/).
THRESHOLDS = {
    "public_trades_lag_warn_s": 30.0,
    "public_trades_lag_stale_s": 120.0,
    "oi_live_heartbeat_stale_s": 60.0,
    "oi_5s_age_stale_s": 60.0,
    "health_cache_ttl_s": 8.0,
    "db_query_timeout_s": 5.0,
    "http_timeout_s": 3.0,
}

STATUSES = frozenset(
    {"HEALTHY", "DEGRADED", "STALE", "STOPPED", "BACKFILLING", "UNKNOWN"}
)

COLLECTOR_IDS = (
    "full_ob_raw_archive",
    "oi_liquidation_live",
    "oi_5m_history",
    "public_trades_live",
    "candles_1m_live",
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def sanitize_json(value: Any) -> Any:
    """Convert NaN/Inf to null; recurse dict/list."""
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    if isinstance(value, dict):
        return {str(k): sanitize_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize_json(v) for v in value]
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return value


def empty_collector(
    collector_id: str,
    *,
    display_name: str,
    status: str = "UNKNOWN",
    evidence: str = "",
) -> dict[str, Any]:
    return {
        "collector_id": collector_id,
        "display_name": display_name,
        "status": status if status in STATUSES else "UNKNOWN",
        "process_running": False,
        "pid": None,
        "process_started_at": None,
        "source_connected": None,
        "last_source_message_at": None,
        "last_successful_write_at": None,
        "latest_exchange_timestamp": None,
        "latest_ingest_timestamp": None,
        "lag_seconds": None,
        "expected_symbol_count": None,
        "fresh_symbol_count": None,
        "stale_symbols": [],
        "write_rate": None,
        "queue_depth": None,
        "dropped_events": None,
        "reconnect_count": None,
        "last_error": None,
        "backfill_supported": False,
        "backfill_status": "DISABLED",
        "gap_count": None,
        "coverage_status": None,
        "source": None,
        "granularity": None,
        "writer_status": None,
        "persistence_lag_seconds": None,
        "checked_at": utc_now().isoformat().replace("+00:00", "Z"),
        "evidence": evidence,
        "reason": evidence,
    }
