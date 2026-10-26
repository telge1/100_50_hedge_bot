"""Band-level parity comparison (exact string edges and origin sets)."""

from __future__ import annotations

from typing import Any


def _norm_ref_row(r: dict[str, Any]) -> dict[str, Any]:
    side = str(r.get("side", "")).lower()
    if side.startswith("u"):
        side = "upper"
    elif side.startswith("l"):
        side = "lower"
    return {
        "side": side,
        "lower_price": str(r.get("lower_price") or r.get("bottom_price")),
        "upper_price": str(r.get("upper_price") or r.get("top_price")),
        "unique_origin_count": int(r.get("unique_origin_count") or r.get("depth") or 0),
        "origin_ids": str(r.get("origin_ids") or "|".join(sorted(r.get("origins") or []))),
    }


def band_key(b: dict[str, Any]) -> tuple[str, str, str]:
    return (b["side"], b["lower_price"], b["upper_price"])


def compare_band_sets(
    expected: list[dict[str, Any]],
    actual: list[dict[str, Any]],
) -> dict[str, Any]:
    exp = {band_key(_norm_ref_row(r)): _norm_ref_row(r) for r in expected}
    act = {band_key(_norm_ref_row(r)): _norm_ref_row(r) for r in actual}
    only_exp = sorted(set(exp) - set(act))
    only_act = sorted(set(act) - set(exp))
    mismatches = []
    for k in sorted(set(exp) & set(act)):
        if exp[k]["unique_origin_count"] != act[k]["unique_origin_count"]:
            mismatches.append({"band": k, "field": "unique_origin_count", "expected": exp[k], "actual": act[k]})
        elif exp[k]["origin_ids"] != act[k]["origin_ids"]:
            mismatches.append({"band": k, "field": "origin_ids", "expected": exp[k], "actual": act[k]})
    return {
        "ok": not only_exp and not only_act and not mismatches,
        "expected_count": len(exp),
        "actual_count": len(act),
        "only_expected": [exp[k] for k in only_exp[:25]],
        "only_actual": [act[k] for k in only_act[:25]],
        "only_expected_total": len(only_exp),
        "only_actual_total": len(only_act),
        "field_mismatches": mismatches[:50],
        "field_mismatch_total": len(mismatches),
    }
