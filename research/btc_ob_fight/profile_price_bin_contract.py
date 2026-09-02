"""Profile price-bin semantics contract (code-derived, not golden-inferred)."""

from __future__ import annotations

import math
from typing import Any

from .config import BTCUSDT_TICK_SIZE
from .profile_edge_state import price_to_tick

PROFILE_PRICE_BIN_CONTRACT = "profile_price_bin_contract_v1"

# OA compute_value_area: POC = price_mid; VAH = price_high; VAL = price_low
# Bin assignment: bin_index = floor(price / step); interval [price_low, price_high)
LEVEL_VAH_REPRESENTATION = "UPPER_EDGE"
LEVEL_VAL_REPRESENTATION = "LOWER_EDGE"
INTERVAL_SEMANTICS = "[LOWER,UPPER)"


def price_to_bin_index(price: float, step: float) -> int:
    return int(math.floor(float(price) / float(step)))


def bin_interval(bin_index: int, step: float) -> tuple[float, float]:
    lo = bin_index * float(step)
    return lo, lo + float(step)


def bin_for_price(price: float, step: float) -> dict[str, Any]:
    idx = price_to_bin_index(price, step)
    lo, hi = bin_interval(idx, step)
    return {
        "bin_index": idx,
        "price_low": lo,
        "price_high": hi,
        "price_mid": lo + float(step) / 2.0,
    }


def level_bin_for_vah_val(level_price: float, step: float, *, kind: str) -> dict[str, Any]:
    """Map VAH/VVAH (upper edge = price_high) or VAL/VVAL (lower edge = price_low) to bin."""
    if kind in ("VAH", "VVAH", "UPPER"):
        idx = int(math.floor((float(level_price) - 1e-9) / float(step)))
    else:
        idx = int(math.floor(float(level_price) / float(step)))
    lo, hi = bin_interval(idx, step)
    return {
        "bin_index": idx,
        "price_low": lo,
        "price_high": hi,
        "price_mid": lo + float(step) / 2.0,
        "level_representation": LEVEL_VAH_REPRESENTATION if kind in ("VAH", "VVAH", "UPPER") else LEVEL_VAL_REPRESENTATION,
        "level_price": level_price,
    }


def build_profile_price_bin_contract(
    tpo_profile: dict[str, Any],
    volume_profile: dict[str, Any],
    *,
    edges: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Derive bin contract from TPO/volume profile provenance and OA semantics."""
    prov = (tpo_profile or {}).get("provenance") or {}
    step = prov.get("price_increment") or (volume_profile or {}).get("provenance", {}).get("price_increment")
    if step is None:
        rows = (volume_profile or {}).get("rows") or []
        if rows:
            step = rows[0].get("price_bin_high", 0) - rows[0].get("price_bin_low", 0)
    step = float(step or 10.0)

    tpo_va = (tpo_profile or {}).get("value_area") or {}
    vol_va = (volume_profile or {}).get("value_area") or {}
    levels = edges.get("levels") if edges else None
    if not levels:
        levels = {
            "tpo_vah": tpo_va.get("tpoc_vah"),
            "tpo_val": tpo_va.get("tpoc_val"),
            "volume_vvah": vol_va.get("vvah"),
            "volume_vval": vol_va.get("vval"),
        }

    tpo_vah_bin = level_bin_for_vah_val(float(levels["tpo_vah"]), step, kind="VAH") if levels.get("tpo_vah") else None
    tpo_val_bin = level_bin_for_vah_val(float(levels["tpo_val"]), step, kind="VAL") if levels.get("tpo_val") else None
    vol_vvah_bin = level_bin_for_vah_val(float(levels["volume_vvah"]), step, kind="VVAH") if levels.get("volume_vvah") else None
    vol_vval_bin = level_bin_for_vah_val(float(levels["volume_vval"]), step, kind="VVAL") if levels.get("volume_vval") else None

    upper_zone = _edge_zone_bins(tpo_vah_bin, vol_vvah_bin, step, side="UPPER")
    lower_zone = _edge_zone_bins(tpo_val_bin, vol_vval_bin, step, side="LOWER")

    return {
        "contract_version": PROFILE_PRICE_BIN_CONTRACT,
        "price_step": step,
        "orderbook_tick_size": BTCUSDT_TICK_SIZE,
        "bin_index_rule": "floor(price / price_step)",
        "interval_semantics": INTERVAL_SEMANTICS,
        "level_representation": {
            "poc": "CENTER",
            "vah": LEVEL_VAH_REPRESENTATION,
            "val": LEVEL_VAL_REPRESENTATION,
            "vvah": LEVEL_VAH_REPRESENTATION,
            "vval": LEVEL_VAL_REPRESENTATION,
            "display_price_in_rows": "CENTER",
        },
        "tpo_bin_orientation": "same_as_volume",
        "volume_bin_orientation": "same_as_tpo",
        "rounding_rule": "floor_to_bin_index_no_float_equality",
        "orderbook_tick_to_bin": "tick_price maps to bin via floor(price/step)",
        "source_references": [
            "orderbook_analyse.market_profile.loader.fetch_volume_at_price",
            "orderbook_analyse.market_profile.profile.compute_value_area",
            "research.btc_ob_fight.tpo_profile.price_to_bin_index",
            "research.btc_ob_fight.volume_profile._aggregate_bins",
        ],
        "tpo_vah_bin": tpo_vah_bin,
        "tpo_val_bin": tpo_val_bin,
        "volume_vvah_bin": vol_vvah_bin,
        "volume_vval_bin": vol_vval_bin,
        "upper_profile_edge_zone": upper_zone,
        "lower_profile_edge_zone": lower_zone,
        "first_outside_bins": {
            "upper": _first_outside_bin(upper_zone, step, direction="above"),
            "lower": _first_outside_bin(lower_zone, step, direction="below"),
        },
    }


def _edge_zone_bins(
    inner_bin: dict[str, Any] | None,
    outer_bin: dict[str, Any] | None,
    step: float,
    *,
    side: str,
) -> dict[str, Any]:
    if not inner_bin or not outer_bin:
        return {"price_low": None, "price_high": None, "bin_indices": []}
    lo_idx = min(inner_bin["bin_index"], outer_bin["bin_index"])
    hi_idx = max(inner_bin["bin_index"], outer_bin["bin_index"])
    lo, _ = bin_interval(lo_idx, step)
    _, hi = bin_interval(hi_idx, step)
    return {
        "edge": side,
        "price_low": lo,
        "price_high": hi,
        "bin_indices": list(range(lo_idx, hi_idx + 1)),
        "inner_level_bin": inner_bin,
        "outer_level_bin": outer_bin,
    }


def _first_outside_bin(zone: dict[str, Any], step: float, *, direction: str) -> dict[str, Any]:
    if zone.get("price_high") is None:
        return {}
    if direction == "above":
        idx = price_to_bin_index(zone["price_high"] - 1e-9, step) + 1
        if zone["price_high"] == bin_interval(price_to_bin_index(zone["price_high"], step), step)[1]:
            idx = price_to_bin_index(zone["price_high"], step)
    else:
        idx = price_to_bin_index(zone["price_low"], step) - 1
    lo, hi = bin_interval(idx, step)
    return {"bin_index": idx, "price_low": lo, "price_high": hi, "price_mid": lo + step / 2.0}


def tick_in_bin(tick: int, bin_lo: float, bin_hi: float) -> bool:
    price = tick * BTCUSDT_TICK_SIZE
    return bin_lo <= price < bin_hi


def price_in_interval(price: float, lo: float, hi: float) -> bool:
    return lo <= float(price) < float(hi)


def ticks_in_region(lo: float, hi: float) -> list[int]:
    """All orderbook ticks with prices in [lo, hi)."""
    t0 = price_to_tick(lo)
    t1 = price_to_tick(hi - 1e-9)
    return list(range(t0, t1 + 1))
