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
    build_active_episode_records,
    load_historical_pool_state_at_close,
    pilot_decision_closes,
    remaining_domain_zones,
    _iso_z,
    _load_publication_line,
)

HISTORICAL_LMV2_LAYER = "historical_lmv2_pools"
HISTORICAL_LMV2_NAMESPACE = "HISTORICAL_LMV2_POOLS"


class HistoricalLmv2PilotError(Exception):
    def __init__(self, code: str, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def _state_err(exc: HistoricalPoolStateError) -> HistoricalLmv2PilotError:
    return HistoricalLmv2PilotError(exc.code, exc.message, exc.details)


def _assert_pilot_window(from_dt: datetime, to_dt: datetime) -> None:
    if from_dt < PILOT_FROM or to_dt > PILOT_TO:
        raise HistoricalLmv2PilotError(
            "OUT_OF_PILOT_SCOPE",
            "request window must stay within Phase 5C pilot closes",
            {
                "pilot_from": _iso_z(PILOT_FROM),
                "pilot_to": _iso_z(PILOT_TO),
                "requested_from": _iso_z(from_dt),
                "requested_to": _iso_z(to_dt),
            },
        )
    if from_dt > to_dt:
        raise HistoricalLmv2PilotError("INVALID_REQUEST", "from_close must be <= to_close")


def _closes_in_range(from_dt: datetime, to_dt: datetime) -> list[datetime]:
    return [dc for dc in pilot_decision_closes() if from_dt <= dc <= to_dt]


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
    _assert_pilot_window(from_dt, to_dt)
    closes = _closes_in_range(from_dt, to_dt)
    if len(closes) > 20:
        raise HistoricalLmv2PilotError("INVALID_REQUEST", "more than 20 closes in range")

    cfg_hash = config_hash(build_live_micro_lld_v2_config(pd))
    reader = PublishedActiveReader()
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
        pilot_candidates = [dc for dc in pilot_decision_closes() if dc <= as_of_dt]
        if not pilot_candidates:
            raise HistoricalLmv2PilotError("OUT_OF_PILOT_SCOPE", "as_of_close before pilot window")
        as_of_dt = pilot_candidates[-1]

        if include_active:
            try:
                pools = build_active_episode_records(reader, cfg_hash, client, database, closes)
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
        }
    finally:
        client.close()
