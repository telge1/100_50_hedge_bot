"""Pure LLD overlay projection for Market-Profile (no I/O, no ClickHouse).

Projects the fields the MP frontend actually consumes from /api/research/pane
into a versioned slim contract without candle duplication.
"""
from __future__ import annotations

from typing import Any, Mapping

CONTRACT_VERSION = "lld_overlay_v1"

# Empty EMA shape applied by applyLldPaneBundle when nothing is present.
_EMPTY_LLD_EMA: dict[str, Any] = {
    "fast": [],
    "slow": [],
    "fast_visible": False,
    "slow_visible": False,
}


def is_lld_overlay_payload(payload: Mapping[str, Any] | None) -> bool:
    """Mirror dashboard/static/market_profile_v1/app.js overlayNamespace === 'LLD'."""
    if not payload or not isinstance(payload, Mapping):
        return False
    ns = payload.get("namespace")
    if ns is not None and str(ns) == "LLD":
        return True
    oid = str(payload.get("id") or "")
    if oid.startswith("lld:") or oid.startswith("lldc:"):
        return True
    return False


def filter_lld_overlays(overlays: list | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in overlays or []:
        if isinstance(item, Mapping) and item.get("id") and is_lld_overlay_payload(item):
            out.append(dict(item))
    return out


def extract_mp_consumed_lld_state(body: Mapping[str, Any] | None) -> dict[str, Any]:
    """Reference extraction: pane|slim payload → internal MP LLD state.

    Matches applyLldPaneBundle consumption (overlays → lldPayloads by id,
    lld_ema / liquidity.ema, clusters / liquidity.clusters) plus identity
    metadata the slim contract preserves for correctness.
    """
    body = body or {}
    next_payloads: dict[str, Any] = {}
    ordered_ids: list[str] = []
    for item in body.get("overlays") or []:
        if (
            isinstance(item, Mapping)
            and item.get("id")
            and is_lld_overlay_payload(item)
        ):
            oid = str(item["id"])
            if oid not in next_payloads:
                ordered_ids.append(oid)
            next_payloads[oid] = dict(item)

    liquidity = body.get("liquidity") if isinstance(body.get("liquidity"), Mapping) else {}
    lld_ema = body.get("lld_ema")
    if lld_ema is None:
        lld_ema = liquidity.get("ema")
    if lld_ema is None:
        lld_ema = dict(_EMPTY_LLD_EMA)
    elif isinstance(lld_ema, Mapping):
        lld_ema = dict(lld_ema)
    else:
        lld_ema = dict(_EMPTY_LLD_EMA)

    clusters = body.get("clusters")
    if clusters is None:
        clusters = liquidity.get("clusters")

    return {
        "ordered_ids": ordered_ids,
        "lld_payloads": next_payloads,
        "lld_ema": lld_ema,
        "clusters": clusters,
        "liquidity_location_mode": body.get("liquidity_location_mode"),
        "liquidity_location_as_of": body.get("liquidity_location_as_of"),
        "canonical_snapshot_sha256": body.get("canonical_snapshot_sha256"),
        "symbol": body.get("symbol"),
        "timeframe": body.get("timeframe"),
        "from": body.get("from"),
        "to": body.get("to"),
        "contract_version": body.get("contract_version"),
    }


def project_lld_overlay_response(
    *,
    symbol: str,
    timeframe: str,
    start: int | None,
    end: int | None,
    overlays: list | None,
    lld_ema: Mapping[str, Any] | None,
    clusters: Any,
    liquidity_meta: Mapping[str, Any] | None = None,
    lld_serialized: list | None = None,
    liquidity_config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build versioned slim response: LLD/overlays only, never candles."""
    meta = dict(liquidity_meta or {})
    lld_only = filter_lld_overlays(overlays)
    # Prefer explicit serialized LLD list when caller already computed it;
    # otherwise reuse filtered composed overlays.
    liq_overlays = list(lld_serialized) if lld_serialized is not None else list(lld_only)
    ema = dict(lld_ema) if isinstance(lld_ema, Mapping) else dict(_EMPTY_LLD_EMA)
    return {
        "contract_version": CONTRACT_VERSION,
        "success": True,
        "feed_ready": True,
        "symbol": str(symbol or "").upper(),
        "timeframe": str(timeframe or ""),
        "from": start,
        "to": end,
        "overlays": lld_only,
        "lld_ema": ema,
        "clusters": clusters,
        "liquidity": {
            "overlays": liq_overlays,
            "ema": ema,
            "clusters": clusters,
            "liquidity_location": meta,
        },
        "liquidity_config": dict(liquidity_config) if isinstance(liquidity_config, Mapping) else None,
        "liquidity_location_mode": meta.get("mode"),
        "liquidity_location_as_of": meta.get("liquidity_location_as_of"),
        "canonical_snapshot_sha256": meta.get("canonical_snapshot_sha256"),
    }


def assert_no_candles(payload: Mapping[str, Any]) -> None:
    if "candles" in payload:
        raise AssertionError("slim LLD payload must not include candles")


def measure_payload_bytes(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Offline JSON size accounting for pane vs slim comparison."""
    import json

    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    total = len(raw.encode("utf-8"))
    candles = payload.get("candles") if isinstance(payload.get("candles"), list) else []
    candle_raw = json.dumps(candles, separators=(",", ":"), ensure_ascii=False)
    candle_bytes = len(candle_raw.encode("utf-8")) if candles else 0
    return {
        "total_bytes": total,
        "candle_count": len(candles) if isinstance(candles, list) else 0,
        "candle_bytes": candle_bytes,
        "field_count": len(payload.keys()),
        "has_candles_key": "candles" in payload,
    }
