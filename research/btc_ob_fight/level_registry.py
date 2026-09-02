"""Decision-eligible level registry from frozen causal profiles."""

from __future__ import annotations

from typing import Any

LEVEL_REGISTRY_CONTRACT = "level_registry_v1"


def build_level_registry(
    tpo_profile: dict[str, Any],
    volume_profile: dict[str, Any],
    *,
    reference_price: float | None = None,
) -> dict[str, Any]:
    levels: list[dict[str, Any]] = []
    tpo_ok = (tpo_profile or {}).get("tpo_profile_status") == "COMPUTED_SEPARATELY"
    vol_ok = (volume_profile or {}).get("volume_profile_status") == "COMPUTED_SEPARATELY"

    if tpo_ok:
        tpoc = (tpo_profile.get("tpoc") or {}).get("tpoc_price")
        va = tpo_profile.get("value_area") or {}
        for kind, price in (
            ("TPO_POC", tpoc),
            ("TPO_VAH", va.get("tpoc_vah")),
            ("TPO_VAL", va.get("tpoc_val")),
        ):
            if price is not None:
                levels.append(_level_row(kind, float(price), "TPO", decision_eligible=True))
        for node in (tpo_profile.get("hvn_candidates") or []):
            levels.append(
                _level_row("TPO_HVN", float(node["price"]), "TPO", decision_eligible=False, heuristic=True)
            )
        for node in (tpo_profile.get("lvn_candidates") or []):
            levels.append(
                _level_row("TPO_LVN", float(node["price"]), "TPO", decision_eligible=False, heuristic=True)
            )

    if vol_ok:
        vpoc = (volume_profile.get("vpoc") or {}).get("vpoc_price")
        va = volume_profile.get("value_area") or {}
        for kind, price in (
            ("VOLUME_VPOC", vpoc),
            ("VOLUME_VVAH", va.get("vvah")),
            ("VOLUME_VVAL", va.get("vval")),
        ):
            if price is not None:
                levels.append(_level_row(kind, float(price), "VOLUME", decision_eligible=True))
        for node in (volume_profile.get("hvn_candidates") or []):
            levels.append(
                _level_row("VOLUME_HVN", float(node["price"]), "VOLUME", decision_eligible=False, heuristic=True)
            )
        for node in (volume_profile.get("lvn_candidates") or []):
            levels.append(
                _level_row("VOLUME_LVN", float(node["price"]), "VOLUME", decision_eligible=False, heuristic=True)
            )

    levels.sort(key=lambda x: x["price"])
    for i, row in enumerate(levels):
        row["level_id"] = f"lvl_{i:03d}_{row['level_type'].lower()}"

    next_level = _next_eligible_level(levels, reference_price)
    return {
        "contract_version": LEVEL_REGISTRY_CONTRACT,
        "level_count": len(levels),
        "decision_eligible_count": sum(1 for x in levels if x["decision_eligible"]),
        "levels": levels,
        "reference_price": reference_price,
        "next_eligible_level": next_level,
        "free_space_status": next_level.get("free_space_status") if next_level else "NO_ELIGIBLE_LEVEL_AVAILABLE",
    }


def _level_row(
    level_type: str,
    price: float,
    profile_kind: str,
    *,
    decision_eligible: bool,
    heuristic: bool = False,
) -> dict[str, Any]:
    return {
        "level_type": level_type,
        "price": price,
        "source": profile_kind,
        "profile_kind": profile_kind,
        "causal_at_anchor": True,
        "heuristic": heuristic,
        "decision_eligible": decision_eligible,
        "distance_ticks": None,
        "distance_bps": None,
        "direction": None,
    }


def _next_eligible_level(
    levels: list[dict[str, Any]],
    reference_price: float | None,
) -> dict[str, Any] | None:
    if reference_price is None:
        return {"free_space_status": "NO_REFERENCE_PRICE", "level": None}
    eligible = [x for x in levels if x["decision_eligible"]]
    above = sorted([x for x in eligible if x["price"] > reference_price], key=lambda x: x["price"])
    below = sorted([x for x in eligible if x["price"] < reference_price], key=lambda x: -x["price"])
    out: dict[str, Any] = {}
    if above:
        n = above[0]
        n["direction"] = "ABOVE"
        n["distance_bps"] = (n["price"] - reference_price) / reference_price * 10000.0
        out["above"] = n
    if below:
        n = below[0]
        n["direction"] = "BELOW"
        n["distance_bps"] = (reference_price - n["price"]) / reference_price * 10000.0
        out["below"] = n
    if not above and not below:
        return {"free_space_status": "NO_ELIGIBLE_LEVEL_AVAILABLE", "level": None}
    out["free_space_status"] = "LEVELS_AVAILABLE"
    return out
