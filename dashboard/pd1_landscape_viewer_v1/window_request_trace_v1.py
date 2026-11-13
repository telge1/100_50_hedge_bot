"""BT-D37 — structured window build tracing (no secrets)."""

from __future__ import annotations

import logging
import os
import time
import uuid
from typing import Any

from . import historical_window_v1 as hw

logger = logging.getLogger("pd1_landscape_viewer.window_trace")
_doge_eo_logger = logging.getLogger("pd1_landscape_viewer.doge_eo_trace")


def doge_exact_origin_trace_enabled() -> bool:
    return os.environ.get("DOGE_EXACT_ORIGIN_TRACE", "").strip() == "1"


def new_request_id() -> str:
    return uuid.uuid4().hex[:16]


def log_doge_exact_origin_window_request(
    *,
    client_trace_id: str | None,
    request_id: str,
    from_utc: str,
    to_utc: str,
    timeframe: str,
    pool_detail: int,
    compute_mode: str,
    symbol: str | None = None,
    pipeline_line_id: str | None = None,
    config_hash: str | None = None,
    stage: str = "request_in",
) -> None:
    if not doge_exact_origin_trace_enabled():
        return
    _doge_eo_logger.info(
        "[doge_eo_trace] stage=%s client_trace=%s request_id=%s symbol=%s tf=%s pd=%s "
        "mode=%s from=%s to=%s pipeline=%s cfg=%s",
        stage,
        client_trace_id or "-",
        request_id,
        symbol or "DOGEUSDT",
        timeframe,
        pool_detail,
        compute_mode,
        from_utc,
        to_utc,
        (pipeline_line_id or "")[:48],
        (config_hash or "")[:16],
    )


def log_doge_exact_origin_window_complete(
    *,
    client_trace_id: str | None,
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
    error_code: str | None = None,
    error_message: str | None = None,
) -> None:
    if not doge_exact_origin_trace_enabled():
        return
    prov = (payload or {}).get("provenance") or {}
    pools_n = len((payload or {}).get("pools") or [])
    density = (payload or {}).get("density_segments")
    density_n = len(density) if isinstance(density, list) else None
    _doge_eo_logger.info(
        "[doge_eo_trace] stage=response client_trace=%s request_id=%s status=%s wall_ms=%.1f "
        "cache_hit=%s tf=%s pd=%s mode=%s from=%s to=%s pools=%s density_seg=%s "
        "publish_to=%s builder=%s err=%s",
        client_trace_id or "-",
        request_id,
        http_status,
        wall_ms,
        cache_hit,
        timeframe,
        pool_detail,
        compute_mode,
        from_utc,
        to_utc,
        pools_n,
        density_n if density_n is not None else "-",
        prov.get("data_available_to") or prov.get("publish_to"),
        prov.get("builder_module_file") or "-",
        error_code or error_message or "-",
    )


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
