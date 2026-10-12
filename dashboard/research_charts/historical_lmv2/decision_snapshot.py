"""Load and validate a single historical decision snapshot for chart parity."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .catalog import ProductionCatalog
from .catalog_registry import resolve_registered_catalog
from .chart_adapter import (
    HISTORICAL_LMV2_SNAPSHOT_LAYER,
    active_pool_records,
    build_render_zones,
    snapshot_pools_to_chart_zones,
    zone_geometry_by_pool_id,
)
from .errors import (
    FrozenInputMismatchError,
    ManifestGateError,
    RequestFrozenMismatchError,
    SnapshotMissingError,
    SnapshotValidationError,
    UnknownCatalogError,
)
from .legacy_map import legacy_pools_from_snapshot_payload
from .provider import HistoricalLmV2PoolStore, decision_close_to_stem, validate_frozen_input


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def parse_decision_close(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return _utc(value)
    text = str(value).strip().replace("Z", "+00:00")
    return _utc(datetime.fromisoformat(text))


def validate_snapshot_active_state(
    payload: dict[str, Any],
    *,
    symbol: str,
    timeframe: str,
    pool_detail: int,
    decision_close: datetime,
) -> None:
    """Fail-closed checks on loaded JSON (before chart mapping)."""
    decision_close = _utc(decision_close)
    sym = str(symbol).strip().upper()
    if str(payload.get("symbol") or "").upper() != sym:
        raise SnapshotValidationError(f"symbol mismatch payload={payload.get('symbol')!r} request={sym}")
    if str(payload.get("timeframe")) != timeframe:
        raise SnapshotValidationError("timeframe mismatch")
    if int(payload.get("pool_detail")) != int(pool_detail):
        raise SnapshotValidationError("pool_detail mismatch")
    dc_payload = parse_decision_close(str(payload.get("decision_close") or ""))
    if dc_payload != decision_close:
        raise SnapshotValidationError("decision_close mismatch")
    stem = decision_close_to_stem(decision_close)
    if decision_close_to_stem(dc_payload) != stem:
        raise SnapshotValidationError("decision_close vs stem")

    fp = str(payload.get("active_state_fingerprint") or "")
    if not fp or len(fp) < 16:
        raise SnapshotValidationError("active_state_fingerprint missing or invalid")

    ids = list(payload.get("active_pool_ids") or [])
    if len(ids) != len(set(ids)):
        raise SnapshotValidationError("duplicate active_pool_ids")

    records = active_pool_records(payload)
    for row in records:
        known = parse_decision_close(str(row["known_at"]))
        if known > decision_close:
            raise SnapshotValidationError(f"known_at after decision_close for {row['pool_id']}")
        bottom = float(row["bottom_price"])
        top = float(row["top_price"])
        if not (top >= bottom > 0):
            raise SnapshotValidationError(f"invalid bounds for {row['pool_id']}")


def pool_records_for_api(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in records:
        out.append(
            {
                "pool_id": str(row["pool_id"]),
                "side": str(row["side"]),
                "bottom_price": float(row["bottom_price"]),
                "top_price": float(row["top_price"]),
                "strength": row.get("strength"),
                "strength_class": row.get("strength_class"),
                "known_at": row.get("known_at"),
                "row_fingerprint": row.get("row_fingerprint"),
            }
        )
    return out


def assert_render_zone_parity(payload: dict[str, Any], zones: list[dict[str, Any]]) -> None:
    records = active_pool_records(payload)
    if len(zones) != len(records):
        raise SnapshotValidationError(
            f"zone count {len(zones)} != active pool count {len(records)}"
        )
    zgeom = zone_geometry_by_pool_id(zones)
    record_ids = {str(r["pool_id"]) for r in records}
    if set(zgeom.keys()) != record_ids:
        raise SnapshotValidationError("zone pool_id set != active_pool_ids set")
    for row in records:
        pid = str(row["pool_id"])
        zb, zt = zgeom[pid]
        if zb != float(row["bottom_price"]) or zt != float(row["top_price"]):
            raise SnapshotValidationError(f"zone bounds != snapshot for {pid}")


def load_decision_snapshot(
    *,
    catalog_id: str,
    symbol: str,
    timeframe: str,
    pool_detail: int,
    decision_close: str | datetime,
    expected_frozen_input_id: str,
) -> dict[str, Any]:
    """
    Shared entry for API and tests. Uses HistoricalLmV2PoolStore only for file load.
    """
    decision_close_dt = parse_decision_close(decision_close)
    catalog = resolve_registered_catalog(catalog_id)
    expected = str(expected_frozen_input_id or "").strip()
    if not expected:
        raise RequestFrozenMismatchError("expected_frozen_input_id required")
    if expected != catalog.frozen_input_id:
        raise RequestFrozenMismatchError(
            f"expected_frozen_input_id {expected!r} != catalog {catalog.frozen_input_id!r}"
        )

    store = HistoricalLmV2PoolStore(catalog)
    payload = store.load_payload(timeframe, int(pool_detail), decision_close_dt)
    validate_snapshot_active_state(
        payload,
        symbol=symbol,
        timeframe=timeframe,
        pool_detail=int(pool_detail),
        decision_close=decision_close_dt,
    )
    validate_frozen_input(payload, catalog)

    records = active_pool_records(payload)
    zones = snapshot_pools_to_chart_zones(
        payload,
        decision_close=decision_close_dt,
        symbol=symbol,
        timeframe=timeframe,
        pool_detail=int(pool_detail),
    )
    assert_render_zone_parity(payload, zones)

    # Backtester path: legacy ACTIVE pools at same decision close
    legacy = legacy_pools_from_snapshot_payload(payload, decision_close=decision_close_dt)
    legacy_ids = {p["pool_id"] for p in legacy}
    active_ids = {str(r["pool_id"]) for r in records}
    if legacy_ids != active_ids:
        raise SnapshotValidationError("legacy_map pool_id set != active_pool_ids")

    frozen = payload.get("frozen_input") or {}
    return {
        "success": True,
        "layer": HISTORICAL_LMV2_SNAPSHOT_LAYER,
        "catalog_id": catalog_id,
        "symbol": str(symbol).upper(),
        "timeframe": timeframe,
        "pool_detail": int(pool_detail),
        "decision_close": decision_close_dt.isoformat(),
        "frozen_input_id": str(frozen.get("snapshot_id") or catalog.frozen_input_id),
        "active_state_fingerprint": str(payload.get("active_state_fingerprint") or ""),
        "active_pool_count": len(records),
        "active_pool_ids": [str(r["pool_id"]) for r in records],
        "pools": pool_records_for_api(records),
        "zones": zones,
        "render_zones": build_render_zones(zones),
        "parity_validation": {
            "status": "PASS",
            "scope": "stored_historical_snapshot_vs_backtester_provider",
            "publication_parity": "NOT_PROVEN",
            "legacy_pool_count": len(legacy),
            "zone_count": len(zones),
            "message": (
                "Chart zones and HistoricalLmV2PoolStore payload share the same "
                "ACTIVE membership and exact bounds; not a live publication replay."
            ),
        },
        "display_label": (
            f"HISTORICAL DECISION SNAPSHOT @ {decision_close_dt.strftime('%Y-%m-%d %H:%M')} UTC"
        ),
    }


__all__ = [
    "load_decision_snapshot",
    "parse_decision_close",
    "validate_snapshot_active_state",
    "assert_render_zone_parity",
    "UnknownCatalogError",
    "RequestFrozenMismatchError",
]
