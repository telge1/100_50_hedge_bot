"""BT-D37 — structured window build tracing (no secrets)."""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from . import historical_window_v1 as hw

logger = logging.getLogger("pd1_landscape_viewer.window_trace")


def new_request_id() -> str:
    return uuid.uuid4().hex[:16]


def log_window_build_complete(
    *,
    request_id: str,
    from_utc: str,
    to_utc: str,
    timeframe: str,
    pool_detail: int,
    compute_mode: str,
    cache_hit: bool,
    http_status: int,
    wall_ms: float,
    payload: dict[str, Any] | None,
) -> None:
    prov = (payload or {}).get("provenance") or {}
    timing = (payload or {}).get("timing_ms") or {}
    rc = (payload or {}).get("response_cache") or {}
    logger.info(
        "[window_trace] id=%s status=%s wall_ms=%.1f cache_hit=%s mode=%s "
        "from=%s to=%s tf=%s pd=%s batch_gate=%s batch_active=%s "
        "prefill_ms=%s lifetimes_ms=%s total_ms=%s pools=%s module=%s digest=%s",
        request_id,
        http_status,
        wall_ms,
        cache_hit,
        compute_mode,
        from_utc,
        to_utc,
        timeframe,
        pool_detail,
        prov.get("batch_prefill_gate_enabled"),
        prov.get("phase5s_batch_reader_active"),
        timing.get("phase5s_prefill"),
        timing.get("build_lifetimes"),
        timing.get("total"),
        len((payload or {}).get("pools") or []),
        prov.get("builder_module_file") or hw.__file__,
        (rc.get("key_digest") or "")[:16],
    )
