"""Anchor profile facts combining genuine TPO and separate volume profile."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from .config import iso_z, utc
from .tpo_profile import assess_tpo_volume_independence, tpo_anchor_levels
from .volume_profile import profile_session_window, volume_anchor_levels


def _valid_reference_level(price: Any) -> bool:
    if price is None:
        return False
    try:
        val = float(price)
    except (TypeError, ValueError):
        return False
    return math.isfinite(val) and val > 0.0


def _confluence_entry(
    tpo_kind: str,
    vol_kind: str,
    tpo_val: Any,
    vol_val: Any,
) -> dict[str, Any]:
    if not (_valid_reference_level(tpo_val) and _valid_reference_level(vol_val)):
        return {
            "tpo_kind": tpo_kind,
            "volume_kind": vol_kind,
            "tpo_price": float(tpo_val) if _valid_reference_level(tpo_val) else tpo_val,
            "volume_price": float(vol_val) if _valid_reference_level(vol_val) else vol_val,
            "same_price_bin": None,
            "same_bin": None,
            "distance_bps": None,
            "evaluation_status": "INVALID_OR_MISSING_REFERENCE_LEVEL",
        }
    tpo_f = float(tpo_val)
    vol_f = float(vol_val)
    same_bin = abs(tpo_f - vol_f) < 1e-9
    return {
        "tpo_kind": tpo_kind,
        "volume_kind": vol_kind,
        "tpo_price": tpo_f,
        "volume_price": vol_f,
        "same_price_bin": same_bin,
        "same_bin": same_bin,
        "distance_bps": abs(tpo_f - vol_f) / tpo_f * 10000.0,
        "evaluation_status": "EVALUATED",
    }


def build_session_profile_metadata(cl, symbol: str, anchor: datetime) -> dict[str, Any]:
    """Session metadata only — no OA volume-at-price relabeled as TPO."""
    anchor = utc(anchor)
    session_start, _, session_id = profile_session_window(anchor)
    return {
        "settings": {
            "causal_cutoff": iso_z(anchor),
            "profile_definition_uses_outcome": False,
            "tpo_profile_engine": "research.btc_ob_fight.tpo_profile",
            "volume_profile_engine": "research.btc_ob_fight.volume_profile",
            "oa_volume_path_not_used_for_tpo": True,
        },
        "session_start_utc": iso_z(session_start),
        "primary_session_id": session_id,
        "profiles": {},
    }


def nearest_tpo_levels(price: float, tpo_profile: dict[str, Any]) -> list[dict[str, Any]]:
    if tpo_profile.get("tpo_profile_status") != "COMPUTED_SEPARATELY":
        return []
    cands: list[tuple[str, float]] = []
    tpoc = (tpo_profile.get("tpoc") or {}).get("tpoc_price")
    va = tpo_profile.get("value_area") or {}
    for kind, val in (("poc", tpoc), ("vah", va.get("tpoc_vah")), ("val", va.get("tpoc_val"))):
        if val is not None:
            cands.append((kind, float(val)))
    for node in (tpo_profile.get("hvn_candidates") or [])[:5]:
        cands.append(("hvn", float(node["price"])))
    for node in (tpo_profile.get("lvn_candidates") or [])[:5]:
        cands.append(("lvn", float(node["price"])))
    cands.sort(key=lambda x: abs(price - x[1]))
    out = []
    for kind, lvl in cands[:8]:
        dist_bps = (price - lvl) / price * 10000.0 if price else None
        out.append(
            {
                "family": "tpo",
                "kind": kind,
                "price": lvl,
                "distance": price - lvl,
                "distance_bps": dist_bps,
            }
        )
    return out


def nearest_volume_levels(price: float, volume_profile: dict[str, Any]) -> list[dict[str, Any]]:
    if volume_profile.get("volume_profile_status") != "COMPUTED_SEPARATELY":
        return []
    cands: list[tuple[str, float]] = []
    vpoc = (volume_profile.get("vpoc") or {}).get("vpoc_price")
    va = volume_profile.get("value_area") or {}
    for kind, val in (("vpoc", vpoc), ("vvah", va.get("vvah")), ("vval", va.get("vval"))):
        if val is not None:
            cands.append((kind, float(val)))
    for node in (volume_profile.get("hvn_candidates") or [])[:5]:
        cands.append(("hvn", float(node["price"])))
    for node in (volume_profile.get("lvn_candidates") or [])[:5]:
        cands.append(("lvn", float(node["price"])))
    cands.sort(key=lambda x: abs(price - x[1]))
    out = []
    for kind, lvl in cands[:8]:
        dist_bps = (price - lvl) / price * 10000.0 if price else None
        out.append(
            {
                "family": "volume",
                "kind": kind,
                "price": lvl,
                "distance": price - lvl,
                "distance_bps": dist_bps,
            }
        )
    return out


def anchor_profile_facts(
    anchor: datetime,
    price_at_anchor: float | None,
    *,
    tpo_profile: dict[str, Any] | None = None,
    volume_profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    tpo = tpo_profile or {}
    vp = volume_profile or {}
    tpo_status = tpo.get("tpo_profile_status") or "NOT_SEPARATELY_COMPUTED"
    vp_status = vp.get("volume_profile_status") or "NOT_SEPARATELY_COMPUTED"

    tpoc = (tpo.get("tpoc") or {}).get("tpoc_price")
    tvah = (tpo.get("value_area") or {}).get("tpoc_vah")
    tval = (tpo.get("value_area") or {}).get("tpoc_val")
    vpoc = (vp.get("vpoc") or {}).get("vpoc_price")
    vvah = (vp.get("value_area") or {}).get("vvah")
    vval = (vp.get("value_area") or {}).get("vval")

    inside_tpo_va = None
    if price_at_anchor is not None and tvah is not None and tval is not None:
        inside_tpo_va = float(tval) <= price_at_anchor <= float(tvah)

    inside_vol_va = None
    if price_at_anchor is not None and vvah is not None and vval is not None:
        inside_vol_va = float(vval) <= price_at_anchor <= float(vvah)

    def _dist_signed(level: float | None) -> float | None:
        if price_at_anchor is None or level is None:
            return None
        return (price_at_anchor - float(level)) / float(price_at_anchor) * 10000.0

    def _dist_abs(level: float | None) -> float | None:
        d = _dist_signed(level)
        return abs(d) if d is not None else None

    independence = assess_tpo_volume_independence(tpo, vp)
    confluence_status = independence.get("status")
    confluence = [
        _confluence_entry(tpo_kind, vol_kind, tpo_val, vol_val)
        for tpo_kind, vol_kind, tpo_val, vol_val in (
            ("poc", "vpoc", tpoc, vpoc),
            ("vah", "vvah", tvah, vvah),
            ("val", "vval", tval, vval),
        )
    ]

    nearest_tpo = nearest_tpo_levels(price_at_anchor, tpo) if price_at_anchor else []
    nearest_vol = nearest_volume_levels(price_at_anchor, vp) if price_at_anchor else []

    session_start, _, session_id = profile_session_window(utc(anchor))
    tpo_prov = tpo.get("provenance") or {}

    return {
        "profile_session_id": session_id,
        "profile_start_utc": iso_z(session_start),
        "profile_cutoff_utc": iso_z(anchor),
        "tpo_poc": tpoc,
        "tpo_vah": tvah,
        "tpo_val": tval,
        "tpo_provenance": tpo_prov.get("engine"),
        "tpo_profile_kind": tpo_prov.get("profile_kind"),
        "tpo_profile_status": tpo_status,
        "tpo_value_area_share": (tpo.get("value_area") or {}).get("actual_value_area_share"),
        "volume_profile_status": vp_status,
        "volume_poc": vpoc,
        "volume_vah": vvah,
        "volume_val": vval,
        "volume_provenance": (vp.get("provenance") or {}).get("engine"),
        "primary_volume_basis": (vp.get("provenance") or {}).get("primary_volume_basis"),
        "volume_profile_computed_separately": vp.get("volume_profile_computed_separately"),
        "volume_profile_future_trades_used": vp.get("future_trade_count_used", 0),
        "price_at_anchor": price_at_anchor,
        "inside_tpo_value_area": inside_tpo_va,
        "inside_volume_value_area": inside_vol_va,
        "distance_to_tpo_poc_bps": _dist_signed(tpoc),
        "distance_to_tpo_vah_bps": _dist_signed(tvah),
        "distance_to_tpo_val_bps": _dist_signed(tval),
        "distance_to_volume_poc_bps": _dist_signed(vpoc),
        "distance_to_volume_vah_bps": _dist_signed(vvah),
        "distance_to_volume_val_bps": _dist_signed(vval),
        "distance_abs_to_volume_poc_bps": _dist_abs(vpoc),
        "distance_abs_to_volume_vah_bps": _dist_abs(vvah),
        "distance_abs_to_volume_val_bps": _dist_abs(vval),
        "tpo_volume_level_confluence": confluence,
        "tpo_volume_confluence_status": confluence_status,
        "tpo_volume_independence": independence,
        "nearest_profile_levels": nearest_tpo,
        "nearest_tpo_levels": nearest_tpo,
        "nearest_volume_levels": nearest_vol,
        "all_anchor_levels": _collect_anchor_levels(tpo, vp),
    }


def _collect_anchor_levels(tpo_profile: dict[str, Any], volume_profile: dict[str, Any]) -> list[dict[str, Any]]:
    levels: list[dict[str, Any]] = []
    levels.extend(tpo_anchor_levels(tpo_profile))
    levels.extend(volume_anchor_levels(volume_profile))
    levels.sort(key=lambda x: (x["price"], x["level_id"]))
    return levels
