"""Price-location vs TPO / Volume / Range — levels stay separate."""

from __future__ import annotations

from typing import Any

from . import PROXIMITY_BINS, TOUCH_FRAC_OF_STEP

LOCATIONS = (
    "BELOW_RANGE_LOW",
    "AT_RANGE_LOW",
    "BETWEEN_RANGE_LOW_AND_VAL",
    "AT_VAL",
    "LOWER_VALUE_AREA",
    "AT_POC",
    "UPPER_VALUE_AREA",
    "AT_VAH",
    "BETWEEN_VAH_AND_RANGE_HIGH",
    "AT_RANGE_HIGH",
    "ABOVE_RANGE_HIGH",
    "UNKNOWN",
)


def _bps(price: float, level: float | None) -> float | None:
    if level is None or price <= 0:
        return None
    return (price - level) / price * 1e4


def _ticks(delta: float | None, step: float | None) -> float | None:
    if delta is None or not step:
        return None
    return delta / step


def locate(
    price: float | None,
    *,
    range_low: float | None,
    val: float | None,
    poc: float | None,
    vah: float | None,
    range_high: float | None,
    price_step: float | None,
    profile_kind: str,
    level_family: str,
) -> dict[str, Any]:
    step = float(price_step or 0.0) or None
    touch = (step or 0.0) * TOUCH_FRAC_OF_STEP
    prox = (step or 0.0) * PROXIMITY_BINS

    base: dict[str, Any] = {
        "profile_kind": profile_kind,
        "level_family": level_family,
        "price": price,
        "price_step": step,
        "location": "UNKNOWN",
        "range_low": range_low,
        "val": val,
        "poc": poc,
        "vah": vah,
        "range_high": range_high,
        "dist_range_low": None,
        "dist_val": None,
        "dist_poc": None,
        "dist_vah": None,
        "dist_range_high": None,
        "dist_range_low_ticks": None,
        "dist_val_ticks": None,
        "dist_poc_ticks": None,
        "dist_vah_ticks": None,
        "dist_range_high_ticks": None,
        "dist_range_low_bps": None,
        "dist_val_bps": None,
        "dist_poc_bps": None,
        "dist_vah_bps": None,
        "dist_range_high_bps": None,
        "range_position_pct": None,
        "value_area_position_pct": None,
        "nearest_edge": None,
        "nearest_edge_distance": None,
        "near_lower_edge": False,
        "near_upper_edge": False,
    }
    if price is None or range_low is None or range_high is None:
        return base

    def d(level: float | None) -> float | None:
        return None if level is None else price - level

    dists = {
        "range_low": d(range_low),
        "val": d(val),
        "poc": d(poc),
        "vah": d(vah),
        "range_high": d(range_high),
    }
    base["dist_range_low"] = dists["range_low"]
    base["dist_val"] = dists["val"]
    base["dist_poc"] = dists["poc"]
    base["dist_vah"] = dists["vah"]
    base["dist_range_high"] = dists["range_high"]
    base["dist_range_low_ticks"] = _ticks(dists["range_low"], step)
    base["dist_val_ticks"] = _ticks(dists["val"], step)
    base["dist_poc_ticks"] = _ticks(dists["poc"], step)
    base["dist_vah_ticks"] = _ticks(dists["vah"], step)
    base["dist_range_high_ticks"] = _ticks(dists["range_high"], step)
    base["dist_range_low_bps"] = _bps(price, range_low)
    base["dist_val_bps"] = _bps(price, val)
    base["dist_poc_bps"] = _bps(price, poc)
    base["dist_vah_bps"] = _bps(price, vah)
    base["dist_range_high_bps"] = _bps(price, range_high)

    rng = range_high - range_low
    if rng > 0:
        base["range_position_pct"] = (price - range_low) / rng * 100.0
    if val is not None and vah is not None and vah > val:
        base["value_area_position_pct"] = (price - val) / (vah - val) * 100.0

    def at(level: float | None) -> bool:
        return level is not None and abs(price - level) <= max(touch, 1e-12)

    if at(range_low):
        loc = "AT_RANGE_LOW"
    elif at(range_high):
        loc = "AT_RANGE_HIGH"
    elif at(val):
        loc = "AT_VAL"
    elif at(vah):
        loc = "AT_VAH"
    elif at(poc):
        loc = "AT_POC"
    elif price < range_low:
        loc = "BELOW_RANGE_LOW"
    elif price > range_high:
        loc = "ABOVE_RANGE_HIGH"
    elif val is not None and price < val:
        loc = "BETWEEN_RANGE_LOW_AND_VAL"
    elif vah is not None and price > vah:
        loc = "BETWEEN_VAH_AND_RANGE_HIGH"
    elif poc is not None and val is not None and vah is not None:
        loc = "LOWER_VALUE_AREA" if price < poc else "UPPER_VALUE_AREA"
    else:
        loc = "UNKNOWN"
    base["location"] = loc

    lower_cands = [(name, abs(price - lvl)) for name, lvl in (("range_low", range_low), ("val", val)) if lvl is not None]
    upper_cands = [(name, abs(price - lvl)) for name, lvl in (("vah", vah), ("range_high", range_high)) if lvl is not None]
    all_cands = lower_cands + upper_cands + ([("poc", abs(price - poc))] if poc is not None else [])
    if all_cands:
        nearest = min(all_cands, key=lambda x: x[1])
        base["nearest_edge"] = nearest[0]
        base["nearest_edge_distance"] = nearest[1]
    if prox > 0:
        base["near_lower_edge"] = any(dist <= prox for _, dist in lower_cands)
        base["near_upper_edge"] = any(dist <= prox for _, dist in upper_cands)
    return base


def locate_profile(price: float | None, profile: dict[str, Any] | None) -> dict[str, Any]:
    empty = {
        "tpo": locate(price, range_low=None, val=None, poc=None, vah=None, range_high=None, price_step=None, profile_kind="MISSING", level_family="tpo"),
        "volume": locate(price, range_low=None, val=None, poc=None, vah=None, range_high=None, price_step=None, profile_kind="MISSING", level_family="volume"),
        "range": locate(price, range_low=None, val=None, poc=None, vah=None, range_high=None, price_step=None, profile_kind="MISSING", level_family="range"),
    }
    if not profile:
        return empty
    kind = str(profile.get("kind") or "")
    step = profile.get("price_step")
    rl, rh = profile.get("range_low"), profile.get("range_high")
    tpo = profile.get("tpo") or {}
    vol = profile.get("volume") or {}
    return {
        "tpo": locate(
            price,
            range_low=rl,
            val=tpo.get("val"),
            poc=tpo.get("poc"),
            vah=tpo.get("vah"),
            range_high=rh,
            price_step=step,
            profile_kind=kind,
            level_family="tpo",
        ),
        "volume": locate(
            price,
            range_low=rl,
            val=vol.get("val"),
            poc=vol.get("vpoc"),
            vah=vol.get("vah"),
            range_high=rh,
            price_step=step,
            profile_kind=kind,
            level_family="volume",
        ),
        "range": locate(
            price,
            range_low=rl,
            val=None,
            poc=None,
            vah=None,
            range_high=rh,
            price_step=step,
            profile_kind=kind,
            level_family="range",
        ),
    }
