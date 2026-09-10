"""Serialize chart LLD engine output. Does not recompute pools."""

from __future__ import annotations

import inspect
from typing import Any

from orderbook_analyse.market_profile.anchor import as_utc
from orderbook_analyse.liquidity_pool_signal.canonical import (
    CANONICAL_PROVIDER_VERSION,
    liquidity_settings_dict,
)
from orderbook_analyse.liquidity_pool_signal.chart_pool_adapter import (
    DEFAULT_LIQUIDITY,
    chart_lookback_start,
    get_engine_function,
    pool_row_from_engine,
    run_chart_backend_lld,
)

from ..timeparse import format_utc_z
from . import (
    CONTRACT_NAME,
    CONTRACT_VERSION,
    LLD_SOURCE_NAME,
    LLD_SOURCE_VERSION,
    ZONE_STATUSES,
    ZONE_TYPES,
)
from .hashing import sha256_hex

LLD_CANDLE_TIMEFRAME = "15m"


def chart_lld_generator_identity() -> dict[str, Any]:
    eng = get_engine_function()
    return {
        "engine_qualname": f"{eng.__module__}.{eng.__qualname__}",
        "engine_file": inspect.getsourcefile(eng),
        "engine_module": eng.__module__,
        "engine_name": eng.__name__,
        "uses_forced_liq_event_table": False,
        "uses_image_pixels": False,
        "reimplements_lld": False,
    }


def _lld_config() -> dict[str, Any]:
    base = liquidity_settings_dict(dict(DEFAULT_LIQUIDITY))
    return {
        "candle_timeframe": LLD_CANDLE_TIMEFRAME,
        "amount": int(base.get("amount") or 300),
        "highest_len": int(base.get("highest_len") or 2),
        "lowest_len": int(base.get("lowest_len") or 2),
        "clusters_enabled": bool(base.get("clusters_enabled", True)),
        "canonical_provider_version": CANONICAL_PROVIDER_VERSION,
        "liquidity_location_as_of_required": True,
        "normalized_volume_strength_is_not_usdt_notional": True,
    }


def config_hash() -> str:
    return sha256_hex({"contract": CONTRACT_NAME, "lld": _lld_config()})


def _zone_type(engine_side: str) -> str:
    if engine_side == "upper":
        return "LLD_RESISTANCE_ZONE"
    if engine_side == "lower":
        return "LLD_SUPPORT_ZONE"
    raise ValueError(f"unknown engine side: {engine_side}")


def _pool_to_zone(
    pool: Any,
    row: dict[str, Any],
    *,
    snapshot_id: str,
    request_as_of: datetime,
    cfg_hash: str,
) -> dict[str, Any] | None:
    avail = row["available_at"]
    as_of_z = format_utc_z(as_utc(request_as_of))
    if avail and avail > as_of_z:
        return None
    invalidated = row.get("invalidated_ts")
    status = "ACTIVE" if row.get("active_as_of") else "INVALIDATED"
    assert status in ZONE_STATUSES
    zone_type = _zone_type(str(row["engine_side"]))
    assert zone_type in ZONE_TYPES
    meta = row.get("raw_metadata") or {}
    source_end = meta.get("source_bar_end") or row.get("source_timestamp")
    confirm_end = row.get("confirmation_bar_end")
    zone = {
        "zone_id": str(row["pool_id"]),
        "snapshot_id": snapshot_id,
        "symbol": str(row["symbol"]),
        "zone_type": zone_type,
        "price_low": float(row["lower_edge"]),
        "price_high": float(row["upper_edge"]),
        "source_candle_start": row.get("source_timestamp"),
        "source_candle_end": source_end,
        "confirmation_candle_end": confirm_end,
        "created_at": row.get("created_ts"),
        "available_at": avail,
        "state_ts": as_of_z,
        "status": status,
        "invalidated_at": invalidated,
        "normalized_volume_strength": row.get("strength"),
        "source_candle_volume": None if pool.source_volume is None else float(pool.source_volume),
        "source_candle_high": None if pool.source_high is None else float(pool.source_high),
        "source_candle_low": None if pool.source_low is None else float(pool.source_low),
        "source_version": LLD_SOURCE_VERSION,
        "config_hash": cfg_hash,
        "engine_side": row.get("engine_side"),
        "not_usdt_notional": True,
        "not_liquidation_event": True,
    }
    zone["canonical_zone_hash"] = sha256_hex(
        {
            k: zone[k]
            for k in (
                "zone_id",
                "zone_type",
                "price_low",
                "price_high",
                "available_at",
                "status",
                "normalized_volume_strength",
                "invalidated_at",
            )
        }
    )
    # Price bounds must equal engine attributes exactly.
    if float(zone["price_low"]) != float(pool.bottom_price):
        raise RuntimeError("LLD price_low mutated")
    if float(zone["price_high"]) != float(pool.top_price):
        raise RuntimeError("LLD price_high mutated")
    return zone


def materialize_lld_snapshot(
    *,
    symbol: str,
    request_as_of: datetime,
    case_id: str,
    timeframe: str = LLD_CANDLE_TIMEFRAME,
) -> dict[str, Any]:
    as_of = as_utc(request_as_of)
    as_of_z = format_utc_z(as_of)
    cfg = _lld_config()
    cfg_hash = config_hash()
    identity = chart_lld_generator_identity()
    if identity["engine_module"] != "indicators.liquidity_location.engine":
        raise RuntimeError(f"LLD engine mismatch: {identity}")
    if identity["engine_name"] != "run_liquidity_location":
        raise RuntimeError(f"LLD engine name mismatch: {identity}")

    lookback_start = chart_lookback_start(as_of, timeframe)
    bundle = run_chart_backend_lld(
        symbol=symbol.upper(),
        timeframe=timeframe,
        start=lookback_start,
        end=as_of,
        liquidity=dict(DEFAULT_LIQUIDITY),
    )
    if as_of != as_utc(as_of):
        raise RuntimeError("as_of lost")
    # Prove the chart backend was called with end=as_of (causal clip).
    result = bundle["engine_result"]
    cfg_obj = bundle["config"]
    snapshot_id = (
        f"lldsnap:{symbol.upper()}:{timeframe}:{int(as_of.timestamp())}:{cfg_hash[:12]}"
    )
    zones: list[dict[str, Any]] = []
    raw_pools: list[dict[str, Any]] = []
    for pool in result.pools:
        row = pool_row_from_engine(pool, cfg=cfg_obj, as_of=as_of, market_price=None)
        raw_pools.append(
            {
                "pool_id": pool.pool_id,
                "side": pool.side,
                "top_price": pool.top_price,
                "bottom_price": pool.bottom_price,
                "strength": pool.strength,
                "active": pool.active,
                "created_timestamp": format_utc_z(pool.created_timestamp),
                "source_timestamp": format_utc_z(pool.source_timestamp),
                "invalidated_timestamp": (
                    None
                    if pool.invalidated_timestamp is None
                    else format_utc_z(pool.invalidated_timestamp)
                ),
                "source_high": pool.source_high,
                "source_low": pool.source_low,
                "source_volume": pool.source_volume,
                "liquidity_location_as_of": as_of_z,
            }
        )
        zone = _pool_to_zone(
            pool, row, snapshot_id=snapshot_id, request_as_of=as_of, cfg_hash=cfg_hash
        )
        if zone is not None:
            zones.append(zone)

    zones.sort(key=lambda z: (z["zone_id"], z["status"]))
    n_active = sum(1 for z in zones if z["status"] == "ACTIVE")
    n_inv = sum(1 for z in zones if z["status"] == "INVALIDATED")
    snapshot = {
        "snapshot_id": snapshot_id,
        "symbol": symbol.upper(),
        "source_timeframe": timeframe,
        "request_as_of": as_of_z,
        "liquidity_location_as_of": as_of_z,
        "state_ts": as_of_z,
        "available_at": as_of_z,
        "source_name": LLD_SOURCE_NAME,
        "source_version": LLD_SOURCE_VERSION,
        "config": cfg,
        "config_hash": cfg_hash,
        "zone_count_active": n_active,
        "zone_count_invalidated": n_inv,
        "lookback_start": format_utc_z(lookback_start),
        "as_of_passed_to_engine_end": as_of_z,
        "generator": identity,
        "case_id": case_id,
        "contract_name": CONTRACT_NAME,
        "contract_version": CONTRACT_VERSION,
    }
    snapshot["canonical_payload_hash"] = sha256_hex(
        {
            "snapshot_id": snapshot_id,
            "request_as_of": as_of_z,
            "config_hash": cfg_hash,
            "zone_ids": [z["zone_id"] for z in zones],
            "zone_hashes": [z["canonical_zone_hash"] for z in zones],
        }
    )
    return {
        "snapshot": snapshot,
        "zones": zones,
        "raw_pools": raw_pools,
        "raw_payload_hash": sha256_hex(raw_pools),
        "generator": identity,
        "engine_end_as_of": as_of_z,
    }
