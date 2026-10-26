"""Factual landscape contrast metrics (no scores / thresholds)."""

from __future__ import annotations

from typing import Any


def _f(x: Any) -> float:
    return float(x)


def nearest_upper_envelope(intervals: list[dict], ref: float) -> dict[str, Any]:
    uppers = [iv for iv in intervals if iv.get("side") == "upper"]
    containing = [
        u
        for u in uppers
        if _f(u["bottom_price"]) <= ref <= _f(u["top_price"])
    ]
    below = sorted(
        [u for u in uppers if _f(u["top_price"]) < ref],
        key=lambda u: _f(u["top_price"]),
        reverse=True,
    )
    above = sorted(
        [u for u in uppers if _f(u["bottom_price"]) > ref],
        key=lambda u: _f(u["bottom_price"]),
    )
    return {
        "depth2_contains_ref": len(containing) > 0,
        "containing_bands": containing,
        "nearest_below": below[0] if below else None,
        "nearest_above": above[0] if above else None,
        "upper_band_count_total": len(uppers),
    }


def upper_gap_chain_above_ref(
    gaps_flat: list[dict], united_upper: list[dict], ref: float
) -> dict[str, Any]:
    """Gaps and united upper intervals with bottom edge strictly above ref."""
    u_above = sorted(
        [u for u in united_upper if _f(u["bottom_price"]) > ref],
        key=lambda u: _f(u["bottom_price"]),
    )
    g_above = sorted(
        [
            g
            for g in gaps_flat
            if g.get("side") == "upper" and _f(g["gap_bottom_price"]) >= ref
        ],
        key=lambda g: _f(g["gap_bottom_price"]),
    )
    large = [g for g in g_above if g.get("large_gap")]
    small = [g for g in g_above if not g.get("large_gap")]
    gap_pcts = [_f(g["gap_pct"]) for g in g_above]
    return {
        "united_upper_intervals_above_ref": len(u_above),
        "upper_gaps_above_ref": len(g_above),
        "upper_large_gaps_above_ref": len(large),
        "upper_small_gaps_above_ref": len(small),
        "upper_gap_pct_values_above_ref": gap_pcts,
        "first_gap_above_ref_pct": gap_pcts[0] if gap_pcts else None,
    }


def lower_density_below_ref(
    intervals: list[dict], united_lower: list[dict], ref: float
) -> dict[str, Any]:
    lowers = [iv for iv in intervals if iv.get("side") == "lower"]
    u_below = sorted(
        [u for u in united_lower if _f(u["top_price"]) < ref],
        key=lambda u: _f(u["top_price"]),
        reverse=True,
    )
    overlapping = [
        u
        for u in lowers
        if _f(u["bottom_price"]) < ref and _f(u["top_price"]) > ref - 0.02
    ]
    return {
        "lower_depth2_band_count_total": len(lowers),
        "united_lower_intervals_below_ref": len(u_below),
        "lower_bands_within_2pct_below_ref": len(overlapping),
        "nearest_united_lower_below_ref": u_below[0] if u_below else None,
    }


def landscape_fingerprint(bundle: dict[str, Any]) -> dict[str, Any]:
    ref = _f(bundle.get("reference_price") or bundle.get("spec", {}).get("manual_entry_hint"))
    intervals = bundle.get("depth2_intervals") or []
    gaps = bundle.get("gaps_flat") or []
    ranked = bundle.get("gaps_ranked") or {}
    united_u = ranked.get("united_upper") or []
    united_l = ranked.get("united_lower") or []
    env = nearest_upper_envelope(intervals, ref)
    return {
        "case_id": bundle.get("case_id"),
        "as_of_utc": bundle.get("spec", {}).get("as_of_utc"),
        "reference_price": ref,
        "parity_ok": (bundle.get("parity") or {}).get("ok"),
        "upper_envelope": env,
        "upper_gaps_above_ref": upper_gap_chain_above_ref(gaps, united_u, ref),
        "lower_below_ref": lower_density_below_ref(intervals, united_l, ref),
        "ema_status": (bundle.get("ema") or {}).get("status"),
    }


def contrast_narrative_check(
    s5_fp: dict[str, Any], other_fp: dict[str, Any]
) -> dict[str, Any]:
    """Document whether metrics directionally match user stories (no pass/fail)."""
    s5_g = s5_fp["upper_gaps_above_ref"]
    o_g = other_fp["upper_gaps_above_ref"]
    s5_l = s5_fp["lower_below_ref"]
    o_l = other_fp["lower_below_ref"]
    return {
        "s5_vs_other": f"{other_fp.get('case_id')} vs S5",
        "s5_has_depth2_upper_at_ref": s5_fp["upper_envelope"]["depth2_contains_ref"],
        "other_has_depth2_upper_at_ref": other_fp["upper_envelope"]["depth2_contains_ref"],
        "s5_upper_large_gaps_above_ref": s5_g["upper_large_gaps_above_ref"],
        "other_upper_large_gaps_above_ref": o_g["upper_large_gaps_above_ref"],
        "s5_upper_small_gaps_above_ref": s5_g["upper_small_gaps_above_ref"],
        "other_upper_small_gaps_above_ref": o_g["upper_small_gaps_above_ref"],
        "s5_united_lower_below_ref": s5_l["united_lower_intervals_below_ref"],
        "other_united_lower_below_ref": o_l["united_lower_intervals_below_ref"],
        "user_story_s5_isolation": (
            "S5 accepted: more large upper gaps above ref than small — "
            f"large={s5_g['upper_large_gaps_above_ref']} small={s5_g['upper_small_gaps_above_ref']}"
        ),
        "user_story_n3_stacked_uppers": (
            "N3 rejected (unanchored): cannot measure; if anchored, expect many "
            "small upper gaps — not computed."
        ),
        "analyzer_can_explain_s5_vs_unanchored_n3": (
            "Partial: S5 metrics computed; N3 landscape absent."
        ),
    }
