"""Research chart adapter — consumes historical_pool_state_v1 (single source of truth)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from research_charts.live_micro_lld_v2_ch_test.active_snapshot.published_reader import (
    PublishedActiveReader,
    UnpublishedSnapshotError,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.historical_visibility import (
    LIVE_TREATS_BACKFILL_AS_SIGNAL,
)
from research_charts.live_micro_lld_v2_ch_test.identity import (
    build_live_micro_lld_v2_config,
    config_hash,
)
from research_charts.live_micro_lld_v2_ch_test.phase11u_b.adapter import AdapterStop

from .chart_live_micro_lld_v2 import _parse_close
from .clickhouse_config import load_clickhouse_config
from .historical_pool_state_v1 import (
    IMMUTABLE_PRICE_HASH,
    IMMUTABLE_REVISION_HASH,
    IMMUTABLE_SNAPSHOT_ID,
    PILOT_FROM,
    PILOT_MAX_CLOSES,
    PILOT_POOL_DETAIL,
    PILOT_SYMBOL,
    PILOT_TIMEFRAME,
    PILOT_TO,
    PIPELINE_LINE_ID,
    SNAPSHOT_ROOT,
    VISIBILITY_MODE,
    HistoricalPoolStateError,
    LineFilteredPublicationSource,
    active_episode_domain_zones,
    apply_display_filters,
    assert_research_chart_window,
    assert_static_timeline_window,
    static_timeline_import_bounds,
    STATIC_TIMELINE_IMPORT_FROM,
    STATIC_TIMELINE_IMPORT_TO_NOMINAL,
    STATIC_TIMELINE_MAX_DISPLAY_CLOSES,
    build_active_episode_records,
    decision_closes_in_range,
    episode_closes_for_chart_request,
    get_published_decision_closes,
    load_historical_pool_state_at_close,
    pilot_decision_closes,
    research_chart_data_bounds,
    resolve_as_of_close,
    remaining_domain_zones,
    _iso_z,
    _load_publication_line,
)

HISTORICAL_LMV2_LAYER = "historical_lmv2_pools"
HISTORICAL_LMV2_NAMESPACE = "HISTORICAL_LMV2_POOLS"
# Phase 5M pilot window (parity reference; static API accepts any in-bounds Apr–Sep window).
PHASE5M_STATIC_TIMELINE_FROM = "2026-09-27T00:00:00Z"
PHASE5M_STATIC_TIMELINE_TO = "2026-09-29T23:45:00Z"


class HistoricalLmv2PilotError(Exception):
    def __init__(self, code: str, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def _state_err(exc: HistoricalPoolStateError) -> HistoricalLmv2PilotError:
    return HistoricalLmv2PilotError(exc.code, exc.message, exc.details)


def _assert_request_order(from_dt: datetime, to_dt: datetime) -> None:
    if from_dt > to_dt:
        raise HistoricalLmv2PilotError("INVALID_REQUEST", "from_close must be <= to_close")


class _LineFilteredSource(LineFilteredPublicationSource):
    pass


def chart_historical_lmv2_pools(
    symbol: str,
    from_close: datetime | str,
    to_close: datetime | str,
    *,
    timeframe: str = PILOT_TIMEFRAME,
    pool_detail: int = PILOT_POOL_DETAIL,
    as_of_close: datetime | str | None = None,
    include_active: bool = True,
    include_remaining: bool = False,
    price_min: float | None = None,
    price_max: float | None = None,
    remaining_limit: int = 6500,
) -> dict[str, Any]:
    sym = str(symbol or "").strip().upper()
    tf = str(timeframe or "").strip()
    pd = int(pool_detail)
    if sym != PILOT_SYMBOL or tf != PILOT_TIMEFRAME or pd != PILOT_POOL_DETAIL:
        raise HistoricalLmv2PilotError(
            "INVALID_SCOPE",
            "pilot supports DOGEUSDT 15m PD10 only",
        )
    from_dt = _parse_close(from_close)
    to_dt = _parse_close(to_close)
    _assert_request_order(from_dt, to_dt)

    cfg_hash = config_hash(build_live_micro_lld_v2_config(pd))
    reader = PublishedActiveReader()
    published = get_published_decision_closes(reader, cfg_hash)
    try:
        assert_research_chart_window(from_dt, to_dt, published)
    except HistoricalPoolStateError as exc:
        raise _state_err(exc) from exc
    closes = decision_closes_in_range(published, from_dt, to_dt)
    if len(closes) > PILOT_MAX_CLOSES:
        raise HistoricalLmv2PilotError(
            "INVALID_REQUEST",
            f"more than {PILOT_MAX_CLOSES} closes in range",
        )
    data_bounds = research_chart_data_bounds(published)
    ch_cfg = load_clickhouse_config()
    import clickhouse_connect

    client = clickhouse_connect.get_client(**ch_cfg.connect_kwargs())
    database = ch_cfg.database
    try:
        from_unix = int(from_dt.timestamp())
        to_unix = int(to_dt.timestamp())
        zones: list[dict[str, Any]] = []
        active_count = 0
        remaining_display_meta: dict[str, Any] = {}
        as_of_dt = _parse_close(as_of_close) if as_of_close else to_dt
        try:
            as_of_dt = resolve_as_of_close(published, as_of_dt)
        except HistoricalPoolStateError as exc:
            raise _state_err(exc) from exc

        if include_active:
            try:
                episode_closes = episode_closes_for_chart_request(
                    published, from_dt, as_of_dt
                )
                pools = build_active_episode_records(
                    reader, cfg_hash, client, database, episode_closes
                )
            except HistoricalPoolStateError as exc:
                raise _state_err(exc) from exc
            active_count = len({p["pool_id"] for p in pools})
            zones.extend(
                active_episode_domain_zones(
                    pools,
                    from_unix=from_unix,
                    to_unix=to_unix,
                    layer=HISTORICAL_LMV2_LAYER,
                    namespace=HISTORICAL_LMV2_NAMESPACE,
                )
            )

        remaining_count_full = 0
        if include_remaining:
            try:
                at_close = load_historical_pool_state_at_close(
                    as_of_dt,
                    symbol=sym,
                    timeframe=tf,
                    pool_detail=pd,
                    client=client,
                    database=database,
                    reader=reader,
                    cfg_hash=cfg_hash,
                )
            except (HistoricalPoolStateError, AdapterStop) as exc:
                if isinstance(exc, HistoricalPoolStateError):
                    raise _state_err(exc) from exc
                raise HistoricalLmv2PilotError(exc.status, str(exc)) from exc
            rem = at_close.remaining_projection
            remaining_count_full = len(at_close.remaining_segments)
            rem_zones = remaining_domain_zones(
                rem,
                from_unix=from_unix,
                to_unix=to_unix,
                as_of_unix=int(as_of_dt.timestamp()),
                layer=HISTORICAL_LMV2_LAYER,
                namespace=HISTORICAL_LMV2_NAMESPACE,
            )
            rem_zones, remaining_display_meta = apply_display_filters(
                rem_zones,
                price_min=price_min,
                price_max=price_max,
                display_limit=remaining_limit,
            )
            zones.extend(rem_zones)

        pub_rows = []
        for dc in closes:
            try:
                pub = _load_publication_line(reader, cfg_hash, dc)
            except UnpublishedSnapshotError as exc:
                raise HistoricalLmv2PilotError("MISSING_PUBLICATION", str(exc)) from exc
            pub_rows.append(
                {
                    "decision_close": _iso_z(dc),
                    "snapshot_id": pub["snapshot_id"],
                    "active_state_fingerprint": pub["active_state_fingerprint"],
                }
            )

        return {
            "layer": HISTORICAL_LMV2_LAYER,
            "namespace": HISTORICAL_LMV2_NAMESPACE,
            "visibility_mode": VISIBILITY_MODE,
            "live_availability_rule": LIVE_TREATS_BACKFILL_AS_SIGNAL,
            "pipeline_line_id": PIPELINE_LINE_ID,
            "symbol": sym,
            "timeframe": tf,
            "pool_detail": pd,
            "config_hash": cfg_hash,
            "from_close": _iso_z(from_dt),
            "to_close": _iso_z(to_dt),
            "as_of_close": _iso_z(as_of_dt),
            "pilot_close_count": len(closes),
            "publications": pub_rows,
            "pool_count": active_count,
            "segment_count": len(zones),
            "remaining_segment_count": remaining_display_meta.get(
                "remaining_segment_count_display", remaining_count_full
            ),
            "remaining_segment_count_full": remaining_count_full,
            "remaining_truncated": remaining_display_meta.get("display_remaining_truncated", False),
            "remaining_display": remaining_display_meta,
            "zones": zones,
            "immutable_candle_snapshot_id": IMMUTABLE_SNAPSHOT_ID,
            "research_data_bounds": data_bounds,
        }
    finally:
        client.close()


def chart_historical_lmv2_static_timeline(
    symbol: str,
    from_close: datetime | str | None = None,
    to_close: datetime | str | None = None,
    *,
    timeframe: str = PILOT_TIMEFRAME,
    pool_detail: int = PILOT_POOL_DETAIL,
    include_active: bool = True,
) -> dict[str, Any]:
    """One SSOT episode walk + one geometry clip for the full research window (Phase 5M).

    Unlike chunked chart requests, zones are not clipped per 96-close tile before merge.
    as_of_close is always the resolved window end (causal, no future membership).
    """
    sym = str(symbol or "").strip().upper()
    tf = str(timeframe or "").strip()
    pd = int(pool_detail)
    if sym != PILOT_SYMBOL or tf != PILOT_TIMEFRAME or pd != PILOT_POOL_DETAIL:
        raise HistoricalLmv2PilotError(
            "INVALID_SCOPE",
            "static timeline supports DOGEUSDT 15m PD10 only",
        )
    if from_close is None or to_close is None:
        raise HistoricalLmv2PilotError(
            "INVALID_REQUEST",
            "static timeline requires from_close and to_close",
        )
    from_dt = _parse_close(from_close)
    to_dt = _parse_close(to_close)
    _assert_request_order(from_dt, to_dt)

    cfg_hash = config_hash(build_live_micro_lld_v2_config(pd))
    reader = PublishedActiveReader()
    published = get_published_decision_closes(reader, cfg_hash)
    try:
        assert_static_timeline_window(from_dt, to_dt, published)
        import_lo, import_hi = static_timeline_import_bounds(published)
    except HistoricalPoolStateError as exc:
        raise _state_err(exc) from exc

    display_closes = decision_closes_in_range(published, from_dt, to_dt)

    as_of_dt = resolve_as_of_close(published, to_dt)
    data_bounds = research_chart_data_bounds(published)
    ch_cfg = load_clickhouse_config()
    import clickhouse_connect

    client = clickhouse_connect.get_client(**ch_cfg.connect_kwargs())
    database = ch_cfg.database
    try:
        from_unix = int(from_dt.timestamp())
        to_unix = int(to_dt.timestamp())
        zones: list[dict[str, Any]] = []
        active_count = 0
        episode_closes: list[datetime] = []
        if include_active:
            episode_closes = episode_closes_for_chart_request(published, from_dt, as_of_dt)
            pools = build_active_episode_records(
                reader, cfg_hash, client, database, episode_closes
            )
            active_count = len({p["pool_id"] for p in pools})
            zones.extend(
                active_episode_domain_zones(
                    pools,
                    from_unix=from_unix,
                    to_unix=to_unix,
                    layer=HISTORICAL_LMV2_LAYER,
                    namespace=HISTORICAL_LMV2_NAMESPACE,
                )
            )

        return {
            "layer": HISTORICAL_LMV2_LAYER,
            "namespace": HISTORICAL_LMV2_NAMESPACE,
            "visibility_mode": VISIBILITY_MODE,
            "live_availability_rule": LIVE_TREATS_BACKFILL_AS_SIGNAL,
            "pipeline_line_id": PIPELINE_LINE_ID,
            "symbol": sym,
            "timeframe": tf,
            "pool_detail": pd,
            "config_hash": cfg_hash,
            "from_close": _iso_z(from_dt),
            "to_close": _iso_z(to_dt),
            "as_of_close": _iso_z(as_of_dt),
            "historical_lmv2_mode": "static_timeline",
            "static_timeline_import_from": _iso_z(import_lo),
            "static_timeline_import_to_effective": _iso_z(import_hi),
            "static_timeline_max_display_closes": STATIC_TIMELINE_MAX_DISPLAY_CLOSES,
            "historical_lmv2_coverage_complete": True,
            "pilot_close_count": len(display_closes),
            "episode_walk_close_count": len(episode_closes),
            "pool_count": active_count,
            "segment_count": len(zones),
            "zones": zones,
            "immutable_candle_snapshot_id": IMMUTABLE_SNAPSHOT_ID,
            "research_data_bounds": data_bounds,
        }
    finally:
        client.close()
