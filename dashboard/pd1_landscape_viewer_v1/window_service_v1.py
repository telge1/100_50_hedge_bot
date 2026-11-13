"""Serve PD1 landscape window with BT-D34 cache + inflight dedup."""

from __future__ import annotations

import copy
import logging
import time
from typing import Any

from .historical_window_v1 import LandscapeWindowError, build_landscape_window, landscape_window_from_state_err
from .window_request_trace_v1 import (
    doge_exact_origin_trace_enabled,
    log_doge_exact_origin_window_complete,
    log_doge_exact_origin_window_request,
    log_window_build_complete,
    new_request_id,
)
from .on_demand_ch1m_pd1_v1 import (
    OnDemandCh1mPd1Scope,
    build_landscape_window_on_demand_ch1m_pd1_v1,
    on_demand_ch1m_scope_for_timeframe,
)
from .window_response_cache_v1 import (
    COMPUTE_MODE_EXACT_ORIGIN_V1,
    COMPUTE_MODE_FULL,
    WINDOW_BUILDER_VERSION_V1,
    attach_response_cache_meta,
    begin_inflight,
    cache_stats,
    finish_inflight,
    get_cached_window,
    normalize_compute_mode,
    payload_cache_eligible,
    store_cached_window,
    wait_inflight,
    WindowCacheKeyParts,
)


def _on_demand_ch1m_scope(timeframe: str, pool_detail: int) -> OnDemandCh1mPd1Scope | None:
    if int(pool_detail) != 1:
        return None
    return on_demand_ch1m_scope_for_timeframe(timeframe)


def _key_parts(
    *,
    from_utc: str,
    to_utc: str,
    timeframe: str,
    pool_detail: int,
    compute_mode: str,
) -> WindowCacheKeyParts:
    from research_charts.historical_lmv2_pd_contract_v1 import resolve_viewer_historical_scope

    tf = str(timeframe or "").strip()
    pd = int(pool_detail)
    scope = _on_demand_ch1m_scope(tf, pd)
    if scope and pd != 1:
        raise LandscapeWindowError(
            "INVALID_SCOPE",
            f"{tf} on-demand supports PD1 only",
            {"timeframe": tf, "pool_detail": pd},
        )
    if scope is not None:
        ident = scope.cache_identity()
        return WindowCacheKeyParts(
            symbol=str(ident["symbol"]),
            from_utc=str(from_utc),
            to_utc=str(to_utc),
            timeframe=str(ident["timeframe"]),
            pool_detail=int(ident["pool_detail"]),
            pipeline_line_id=str(ident["pipeline_line_id"]),
            config_hash=str(ident["config_hash"]),
            compute_mode=compute_mode,
            builder_version=str(ident["builder_version"]),
        )
    if tf in ("1h", "30m", "4h") and pd != 1:
        raise LandscapeWindowError(
            "INVALID_SCOPE",
            f"{tf} on-demand supports PD1 only",
            {"timeframe": tf, "pool_detail": pd},
        )
    hcfg = resolve_viewer_historical_scope(timeframe=timeframe, pool_detail=pool_detail)
    return WindowCacheKeyParts(
        symbol="DOGEUSDT",
        from_utc=str(from_utc),
        to_utc=str(to_utc),
        timeframe=str(hcfg.timeframe),
        pool_detail=int(hcfg.pool_detail),
        pipeline_line_id=str(hcfg.pipeline_line_id),
        config_hash=str(hcfg.config_hash),
        compute_mode=compute_mode,
        builder_version=WINDOW_BUILDER_VERSION_V1,
    )


def _build_uncached(
    from_utc: str,
    to_utc: str,
    *,
    timeframe: str,
    pool_detail: int,
    compute_mode: str,
) -> dict[str, Any]:
    tf = str(timeframe or "").strip()
    pd = int(pool_detail)
    scope = _on_demand_ch1m_scope(tf, pd)
    if scope is not None:
        return build_landscape_window_on_demand_ch1m_pd1_v1(
            scope,
            from_utc,
            to_utc,
            pool_detail=pd,
            compute_mode=compute_mode,
        )
    if tf in ("1h", "30m", "4h") and pd != 1:
        raise LandscapeWindowError(
            "INVALID_SCOPE",
            f"{tf} on-demand supports PD1 only",
            {"timeframe": tf, "pool_detail": pd},
        )
    try:
        density_only = compute_mode == COMPUTE_MODE_EXACT_ORIGIN_V1
        return build_landscape_window(
            from_utc,
            to_utc,
            timeframe=tf,
            pool_detail=pd,
            density_only=density_only,
        )
    except LandscapeWindowError:
        raise
    except Exception as exc:
        from research_charts.historical_pool_state_v1 import HistoricalPoolStateError

        if not isinstance(exc, HistoricalPoolStateError):
            raise
        err = landscape_window_from_state_err(exc)
        raise LandscapeWindowError(err.code, err.message, err.details) from exc


def get_landscape_window_response(
    *,
    from_utc: str,
    to_utc: str,
    timeframe: str = "15m",
    pool_detail: int = 1,
    response_mode: str | None = None,
    request_id: str | None = None,
    client_trace_id: str | None = None,
) -> dict[str, Any]:
    rid = request_id or new_request_id()
    t0 = time.perf_counter()
    compute_mode = normalize_compute_mode(response_mode)
    parts = _key_parts(
        from_utc=from_utc,
        to_utc=to_utc,
        timeframe=timeframe,
        pool_detail=pool_detail,
        compute_mode=compute_mode,
    )
    digest = parts.digest()
    if doge_exact_origin_trace_enabled():
        log_doge_exact_origin_window_request(
            client_trace_id=client_trace_id,
            request_id=rid,
            from_utc=from_utc,
            to_utc=to_utc,
            timeframe=timeframe,
            pool_detail=pool_detail,
            compute_mode=compute_mode,
            symbol=parts.symbol,
            pipeline_line_id=parts.pipeline_line_id,
            config_hash=parts.config_hash,
            stage="scope_resolved",
        )

    cached = get_cached_window(digest)
    if cached is not None:
        out = attach_response_cache_meta(
            cached,
            hit=True,
            key_digest=digest,
            compute_mode=compute_mode,
            stored_at_unix=time.time(),
        )
        wall = (time.perf_counter() - t0) * 1000
        log_window_build_complete(
            request_id=rid,
            from_utc=from_utc,
            to_utc=to_utc,
            timeframe=timeframe,
            pool_detail=pool_detail,
            compute_mode=compute_mode,
            cache_hit=True,
            http_status=200,
            wall_ms=wall,
            payload=out,
        )
        log_doge_exact_origin_window_complete(
            client_trace_id=client_trace_id,
            request_id=rid,
            from_utc=from_utc,
            to_utc=to_utc,
            timeframe=timeframe,
            pool_detail=pool_detail,
            compute_mode=compute_mode,
            cache_hit=True,
            http_status=200,
            wall_ms=wall,
            payload=out,
        )
        if client_trace_id or doge_exact_origin_trace_enabled():
            if client_trace_id:
                out["client_trace_id"] = client_trace_id
            out["request_id"] = rid
        return out

    is_leader, wait_ev = begin_inflight(digest)
    if not is_leader and wait_ev is not None:
        follower_payload = wait_inflight(digest, wait_ev)
        out = attach_response_cache_meta(
            follower_payload,
            hit=True,
            key_digest=digest,
            compute_mode=compute_mode,
            stored_at_unix=time.time(),
        )
        wall = (time.perf_counter() - t0) * 1000
        log_window_build_complete(
            request_id=rid,
            from_utc=from_utc,
            to_utc=to_utc,
            timeframe=timeframe,
            pool_detail=pool_detail,
            compute_mode=compute_mode,
            cache_hit=True,
            http_status=200,
            wall_ms=wall,
            payload=out,
        )
        log_doge_exact_origin_window_complete(
            client_trace_id=client_trace_id,
            request_id=rid,
            from_utc=from_utc,
            to_utc=to_utc,
            timeframe=timeframe,
            pool_detail=pool_detail,
            compute_mode=compute_mode,
            cache_hit=True,
            http_status=200,
            wall_ms=wall,
            payload=out,
        )
        if client_trace_id:
            out["client_trace_id"] = client_trace_id
        out["request_id"] = rid
        return out

    try:
        payload = _build_uncached(
            from_utc,
            to_utc,
            timeframe=timeframe,
            pool_detail=pool_detail,
            compute_mode=compute_mode,
        )
        if payload_cache_eligible(payload):
            store_cached_window(digest, payload)
        out = copy.deepcopy(payload)
        finish_inflight(digest, out, None)
        result = attach_response_cache_meta(
            out,
            hit=False,
            key_digest=digest,
            compute_mode=compute_mode,
        )
        wall = (time.perf_counter() - t0) * 1000
        log_window_build_complete(
            request_id=rid,
            from_utc=from_utc,
            to_utc=to_utc,
            timeframe=timeframe,
            pool_detail=pool_detail,
            compute_mode=compute_mode,
            cache_hit=False,
            http_status=200,
            wall_ms=wall,
            payload=result,
        )
        log_doge_exact_origin_window_complete(
            client_trace_id=client_trace_id,
            request_id=rid,
            from_utc=from_utc,
            to_utc=to_utc,
            timeframe=timeframe,
            pool_detail=pool_detail,
            compute_mode=compute_mode,
            cache_hit=False,
            http_status=200,
            wall_ms=wall,
            payload=result,
        )
        if client_trace_id or doge_exact_origin_trace_enabled():
            if client_trace_id:
                result["client_trace_id"] = client_trace_id
            result["request_id"] = rid
        timing = result.get("timing_ms") or {}
        if float(timing.get("build_lifetimes") or 0) > 30_000:
            logging.getLogger("pd1_landscape_viewer.window_trace").warning(
                "[window_trace] slow_lifetimes id=%s lifetimes_ms=%s total_ms=%s batch_active=%s gate=%s",
                rid,
                timing.get("build_lifetimes"),
                timing.get("total"),
                (result.get("provenance") or {}).get("phase5s_batch_reader_active"),
                (result.get("provenance") or {}).get("batch_prefill_gate_enabled"),
            )
        return result
    except BaseException as exc:
        finish_inflight(digest, None, exc)
        raise


def service_cache_stats() -> dict[str, Any]:
    return cache_stats()
