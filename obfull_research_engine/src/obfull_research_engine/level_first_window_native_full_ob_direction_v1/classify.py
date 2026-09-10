"""Transparent overall class. No score. Two-family rule is a frozen V1 contract."""

from __future__ import annotations

from typing import Any

from . import CONTRADICTION_FAMILIES, SUPPORT_FAMILIES


def overall_class(
    families: dict[str, dict[str, Any]],
    *,
    quality_ok: bool,
    undirected: bool,
    min_support_for_strong: int,
) -> dict[str, Any]:
    if undirected:
        return {
            "overall_class": "FULL_OB_DIRECTION_NOT_EVALUATED",
            "reason": "REACTION_DIRECTION_UNAVAILABLE",
            "n_support_families": 0,
            "n_contradiction_families": 0,
        }
    if not quality_ok:
        return {
            "overall_class": "FULL_OB_NOT_EVALUATED",
            "reason": "QUALITY_OR_SPATIAL_FAIL_CLOSED",
            "n_support_families": 0,
            "n_contradiction_families": 0,
        }
    support = [n for n in SUPPORT_FAMILIES if families.get(n, {}).get("status") == "SUPPORTED"]
    contra = [n for n in CONTRADICTION_FAMILIES if families.get(n, {}).get("status") == "CONTRADICTED"]
    n_s, n_c = len(support), len(contra)
    if n_s and n_c:
        label = "FULL_OB_MIXED"
        reason = "SUPPORT_AND_CONTRADICTION_FAMILIES"
    elif n_c and not n_s:
        label = "FULL_OB_CONTRADICTS_REACTION"
        reason = "CONTRADICTION_WITHOUT_SUPPORT"
    elif n_s >= int(min_support_for_strong) and not n_c:
        label = "FULL_OB_STRONGLY_SUPPORTS_REACTION"
        reason = "V1_CONTRACT_TWO_SUPPORT_FAMILIES_NO_CONTRADICTION"
    elif n_s == 1 and not n_c:
        label = "FULL_OB_PARTIALLY_SUPPORTS_REACTION"
        reason = "SINGLE_SUPPORT_FAMILY_NO_CONTRADICTION"
    else:
        label = "FULL_OB_UNCLEAR"
        reason = "NO_SUPPORT_NO_CONTRADICTION_WITH_VALID_COVERAGE"
    return {
        "overall_class": label,
        "reason": reason,
        "n_support_families": n_s,
        "n_contradiction_families": n_c,
        "support_families": support,
        "contradiction_families": contra,
        "strongly_supports_rule": "V1_CONTRACT_PREDECLARED_NOT_OUTCOME_FITTED",
    }
