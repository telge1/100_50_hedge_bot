"""Frozen profile edge geometry and per-price state classification.

Contract: ``profile_edge_state_v1``. Profiles are computed with
``session_start <= trade_ts < anchor`` and frozen for observation.
"""

from __future__ import annotations

import math
from contextvars import ContextVar
from decimal import Decimal
from typing import Any

from .instrument_contract import instrument_for, price_to_tick as _inst_price_to_tick, tick_to_price as _inst_tick_to_price

PROFILE_EDGE_STATE_CONTRACT = "profile_edge_state_v1"

_active_symbol: ContextVar[str] = ContextVar("fight_active_symbol", default="BTCUSDT")


def set_active_symbol(symbol: str) -> None:
    _active_symbol.set(symbol.upper())


def get_active_symbol() -> str:
    return _active_symbol.get()


STATE_INSIDE_BOTH = "INSIDE_BOTH_PROFILES"
STATE_BETWEEN_UPPER = "BETWEEN_UPPER_PROFILE_EDGES"
STATE_BETWEEN_LOWER = "BETWEEN_LOWER_PROFILE_EDGES"
STATE_OUTSIDE_ABOVE = "OUTSIDE_BOTH_ABOVE"
STATE_OUTSIDE_BELOW = "OUTSIDE_BOTH_BELOW"
STATE_INVALID = "INVALID_PROFILE_GEOMETRY"

GROUP_INSIDE = "INSIDE_BOTH_PROFILES"
GROUP_BETWEEN = "BETWEEN_PROFILE_EDGES"
GROUP_OUTSIDE = "OUTSIDE_BOTH_PROFILES"

DIRECTION_UPPER = "UPPER"
DIRECTION_LOWER = "LOWER"
DIRECTION_NEUTRAL = "NEUTRAL"


def price_to_tick(price: float | Decimal, tick_size: float | Decimal | None = None) -> int:
    if tick_size is not None:
        return _inst_price_to_tick(price, tick_size)
    return _inst_price_to_tick(price, get_active_symbol())


def tick_to_price(tick: int, tick_size: float | Decimal | None = None) -> float:
    if tick_size is not None:
        return _inst_tick_to_price(tick, tick_size)
    return _inst_tick_to_price(tick, get_active_symbol())


def _level_from_profile(tpo_profile: dict[str, Any], volume_profile: dict[str, Any]) -> dict[str, float | None]:
    tpo = tpo_profile or {}
    vol = volume_profile or {}
    if tpo.get("tpo_profile_status") != "COMPUTED_SEPARATELY":
        return {
            "tpo_poc": None,
            "tpo_vah": None,
            "tpo_val": None,
            "volume_vpoc": None,
            "volume_vvah": None,
            "volume_vval": None,
        }
    if vol.get("volume_profile_status") != "COMPUTED_SEPARATELY":
        return {
            "tpo_poc": (tpo.get("tpoc") or {}).get("tpoc_price"),
            "tpo_vah": (tpo.get("value_area") or {}).get("tpoc_vah"),
            "tpo_val": (tpo.get("value_area") or {}).get("tpoc_val"),
            "volume_vpoc": None,
            "volume_vvah": None,
            "volume_vval": None,
        }
    return {
        "tpo_poc": float((tpo.get("tpoc") or {}).get("tpoc_price")),
        "tpo_vah": float((tpo.get("value_area") or {}).get("tpoc_vah")),
        "tpo_val": float((tpo.get("value_area") or {}).get("tpoc_val")),
        "volume_vpoc": float((vol.get("vpoc") or {}).get("vpoc_price")),
        "volume_vvah": float((vol.get("value_area") or {}).get("vvah")),
        "volume_vval": float((vol.get("value_area") or {}).get("vval")),
    }


def build_frozen_profile_edges(
    tpo_profile: dict[str, Any],
    volume_profile: dict[str, Any],
    *,
    anchor_cutoff_utc: str | None = None,
) -> dict[str, Any]:
    """Build frozen edge geometry from causal TPO + volume profiles."""
    levels = _level_from_profile(tpo_profile, volume_profile)
    required = ("tpo_vah", "tpo_val", "volume_vvah", "volume_vval")
    missing = [k for k in required if levels.get(k) is None or not math.isfinite(float(levels[k]))]
    if missing:
        return {
            "contract_version": PROFILE_EDGE_STATE_CONTRACT,
            "profile_state": STATE_INVALID,
            "invalid_reason": f"missing_levels:{','.join(missing)}",
            "levels": levels,
            "frozen_at_anchor_utc": anchor_cutoff_utc,
        }

    tpo_vah, tpo_val = levels["tpo_vah"], levels["tpo_val"]
    vvah, vval = levels["volume_vvah"], levels["volume_vval"]
    upper_inner = min(tpo_vah, vvah)
    upper_outer = max(tpo_vah, vvah)
    lower_inner = max(tpo_val, vval)
    lower_outer = min(tpo_val, vval)

    if lower_inner > upper_inner:
        return {
            "contract_version": PROFILE_EDGE_STATE_CONTRACT,
            "profile_state": STATE_INVALID,
            "invalid_reason": "value_areas_do_not_overlap",
            "levels": levels,
            "upper_inner_edge": upper_inner,
            "upper_outer_edge": upper_outer,
            "lower_inner_edge": lower_inner,
            "lower_outer_edge": lower_outer,
            "frozen_at_anchor_utc": anchor_cutoff_utc,
        }

    return {
        "contract_version": PROFILE_EDGE_STATE_CONTRACT,
        "profile_state": "VALID",
        "levels": levels,
        "upper_inner_edge": upper_inner,
        "upper_outer_edge": upper_outer,
        "lower_inner_edge": lower_inner,
        "lower_outer_edge": lower_outer,
        "upper_edge_zone": {"low": upper_inner, "high": upper_outer},
        "lower_edge_zone": {"low": lower_outer, "high": lower_inner},
        "fair_zone": {"low": lower_inner, "high": upper_inner},
        "tick_size": float(instrument_for(get_active_symbol()).tick_size),
        "upper_inner_edge_tick": price_to_tick(upper_inner),
        "upper_outer_edge_tick": price_to_tick(upper_outer),
        "lower_inner_edge_tick": price_to_tick(lower_inner),
        "lower_outer_edge_tick": price_to_tick(lower_outer),
        "frozen_at_anchor_utc": anchor_cutoff_utc,
    }


def classify_price_state(price: float, edges: dict[str, Any]) -> dict[str, Any]:
    """Classify one trade price against frozen edges (tick-normalized)."""
    if edges.get("profile_state") != "VALID":
        return {
            "state": STATE_INVALID,
            "state_group": STATE_INVALID,
            "direction": DIRECTION_NEUTRAL,
            "price": price,
            "price_tick": price_to_tick(price),
        }

    pt = price_to_tick(price)
    ui = int(edges["upper_inner_edge_tick"])
    uo = int(edges["upper_outer_edge_tick"])
    li = int(edges["lower_inner_edge_tick"])
    lo = int(edges["lower_outer_edge_tick"])

    if pt > uo:
        state = STATE_OUTSIDE_ABOVE
        direction = DIRECTION_UPPER
    elif pt < lo:
        state = STATE_OUTSIDE_BELOW
        direction = DIRECTION_LOWER
    elif pt > ui:
        state = STATE_BETWEEN_UPPER
        direction = DIRECTION_UPPER
    elif pt < li:
        state = STATE_BETWEEN_LOWER
        direction = DIRECTION_LOWER
    else:
        state = STATE_INSIDE_BOTH
        direction = DIRECTION_NEUTRAL

    if state in (STATE_BETWEEN_UPPER, STATE_BETWEEN_LOWER):
        group = GROUP_BETWEEN
    elif state in (STATE_OUTSIDE_ABOVE, STATE_OUTSIDE_BELOW):
        group = GROUP_OUTSIDE
    else:
        group = GROUP_INSIDE

    ref_edge = _relevant_edge_price(state, edges)
    dist_bps = abs(price - ref_edge) / price * 10000.0 if ref_edge and price else None

    return {
        "state": state,
        "state_group": group,
        "direction": direction,
        "price": price,
        "price_tick": pt,
        "relevant_edge_price": ref_edge,
        "distance_bps_from_relevant_edge": dist_bps,
    }


def _relevant_edge_price(state: str, edges: dict[str, Any]) -> float | None:
    if state == STATE_BETWEEN_UPPER:
        return edges.get("upper_inner_edge")
    if state == STATE_BETWEEN_LOWER:
        return edges.get("lower_inner_edge")
    if state == STATE_OUTSIDE_ABOVE:
        return edges.get("upper_outer_edge")
    if state == STATE_OUTSIDE_BELOW:
        return edges.get("lower_outer_edge")
    return None


def distance_bps_from_edge(price: float, edge_price: float) -> float:
    if not edge_price:
        return 0.0
    return (price - edge_price) / edge_price * 10000.0
