"""Edge region scopes for consumption and book coverage (deterministic from bin contract)."""

from __future__ import annotations

from typing import Any

from .profile_edge_state import price_to_tick, tick_to_price
from .profile_price_bin_contract import (
    PROFILE_PRICE_BIN_CONTRACT,
    build_profile_price_bin_contract,
    price_in_interval,
    ticks_in_region,
)

SCOPE_EXACT_LEVEL_TICK = "EXACT_LEVEL_TICK"
SCOPE_TPO_EDGE_BIN = "TPO_EDGE_BIN"
SCOPE_VOLUME_EDGE_BIN = "VOLUME_EDGE_BIN"
SCOPE_PROFILE_EDGE_ZONE = "PROFILE_EDGE_ZONE"
SCOPE_FIRST_OUTSIDE_BIN = "FIRST_OUTSIDE_BIN"

ALL_SCOPES = (
    SCOPE_EXACT_LEVEL_TICK,
    SCOPE_TPO_EDGE_BIN,
    SCOPE_VOLUME_EDGE_BIN,
    SCOPE_PROFILE_EDGE_ZONE,
    SCOPE_FIRST_OUTSIDE_BIN,
)


def build_edge_region_catalog(
    tpo_profile: dict[str, Any],
    volume_profile: dict[str, Any],
    edges: dict[str, Any],
) -> dict[str, Any]:
    contract = build_profile_price_bin_contract(tpo_profile, volume_profile, edges=edges)
    upper = _side_regions(contract, edges, side="UPPER")
    lower = _side_regions(contract, edges, side="LOWER")
    return {
        "contract_version": PROFILE_PRICE_BIN_CONTRACT,
        "profile_price_bin_contract": contract,
        "upper": upper,
        "lower": lower,
    }


def _side_regions(contract: dict[str, Any], edges: dict[str, Any], *, side: str) -> list[dict[str, Any]]:
    if edges.get("profile_state") != "VALID":
        return []
    if side == "UPPER":
        inner = edges.get("upper_inner_edge")
        outer = edges.get("upper_outer_edge")
        tpo_bin = contract.get("tpo_vah_bin")
        vol_bin = contract.get("volume_vvah_bin")
        zone = contract.get("upper_profile_edge_zone") or {}
        first_out = (contract.get("first_outside_bins") or {}).get("upper") or {}
    else:
        inner = edges.get("lower_inner_edge")
        outer = edges.get("lower_outer_edge")
        tpo_bin = contract.get("tpo_val_bin")
        vol_bin = contract.get("volume_vval_bin")
        zone = contract.get("lower_profile_edge_zone") or {}
        first_out = (contract.get("first_outside_bins") or {}).get("lower") or {}

    regions: list[dict[str, Any]] = []
    for label, price in (("inner", inner), ("outer", outer)):
        if price is None:
            continue
        tick = price_to_tick(price)
        regions.append(
            {
                "scope": SCOPE_EXACT_LEVEL_TICK,
                "edge": side,
                "edge_role": label,
                "price": price,
                "price_tick": tick,
                "price_low": price,
                "price_high": price,
                "ticks": [tick],
            }
        )
    if tpo_bin:
        regions.append(_bin_scope(SCOPE_TPO_EDGE_BIN, side, "TPO", tpo_bin))
    if vol_bin:
        regions.append(_bin_scope(SCOPE_VOLUME_EDGE_BIN, side, "VOLUME", vol_bin))
    if zone.get("price_low") is not None:
        lo, hi = zone["price_low"], zone["price_high"]
        regions.append(
            {
                "scope": SCOPE_PROFILE_EDGE_ZONE,
                "edge": side,
                "price_low": lo,
                "price_high": hi,
                "price_mid": (lo + hi) / 2.0,
                "ticks": ticks_in_region(lo, hi),
                "bin_indices": zone.get("bin_indices"),
            }
        )
    if first_out.get("price_low") is not None:
        lo, hi = first_out["price_low"], first_out["price_high"]
        regions.append(
            {
                "scope": SCOPE_FIRST_OUTSIDE_BIN,
                "edge": side,
                "price_low": lo,
                "price_high": hi,
                "price_mid": first_out.get("price_mid"),
                "ticks": ticks_in_region(lo, hi),
                "bin_index": first_out.get("bin_index"),
                "context_only": True,
            }
        )
    return regions


def _bin_scope(scope: str, edge: str, profile_kind: str, b: dict[str, Any]) -> dict[str, Any]:
    lo, hi = b["price_low"], b["price_high"]
    return {
        "scope": scope,
        "edge": edge,
        "profile_kind": profile_kind,
        "price_low": lo,
        "price_high": hi,
        "price_mid": b["price_mid"],
        "bin_index": b["bin_index"],
        "ticks": ticks_in_region(lo, hi),
    }


def classify_price_to_scopes(
    price: float,
    catalog: dict[str, Any],
    *,
    edge: str | None = None,
) -> list[dict[str, Any]]:
    """Return all scopes whose region contains ``price``."""
    tick = price_to_tick(price)
    hits: list[dict[str, Any]] = []
    sides = [edge] if edge else ("UPPER", "LOWER")
    for side in sides:
        for reg in catalog.get(side.lower(), []) or catalog.get(side, []):
            if reg["scope"] == SCOPE_EXACT_LEVEL_TICK:
                if tick == reg["price_tick"]:
                    hits.append({**reg, "price": price, "price_tick": tick})
            else:
                lo, hi = reg["price_low"], reg["price_high"]
                if price_in_interval(price, lo, hi):
                    hits.append({**reg, "price": price, "price_tick": tick})
    return hits


def distance_to_edges(
    price: float,
    edges: dict[str, Any],
    *,
    edge_side: str,
) -> dict[str, Any]:
    tick = price_to_tick(price)
    if edge_side == "UPPER":
        inner = edges.get("upper_inner_edge")
        outer = edges.get("upper_outer_edge")
    else:
        inner = edges.get("lower_inner_edge")
        outer = edges.get("lower_outer_edge")
    inner_tick = price_to_tick(inner) if inner else None
    outer_tick = price_to_tick(outer) if outer else None
    dist_inner = abs(tick - inner_tick) if inner_tick is not None else None
    dist_outer = abs(tick - outer_tick) if outer_tick is not None else None
    mid = price if price else tick_to_price(tick)
    bps_inner = abs(price - inner) / mid * 10000.0 if inner else None
    bps_outer = abs(price - outer) / mid * 10000.0 if outer else None
    return {
        "price": price,
        "price_tick": tick,
        "distance_ticks_to_inner_edge": dist_inner,
        "distance_ticks_to_outer_edge": dist_outer,
        "distance_bps_to_inner_edge": bps_inner,
        "distance_bps_to_outer_edge": bps_outer,
    }
