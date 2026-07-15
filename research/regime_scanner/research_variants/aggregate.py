"""Cross-window aggregation and robustness scoring."""

from __future__ import annotations

import math
import statistics
from typing import Any

# Documented robustness weights (transparent, not tuned to a variant).
ROBUSTNESS_MEDIAN_WEIGHT = 1.0
ROBUSTNESS_STDDEV_PENALTY = 0.5
ROBUSTNESS_WORST_PENALTY = 0.25
ROBUSTNESS_DEGENERATE_PENALTY = 10.0


def window_shares(metrics: dict[str, Any]) -> dict[str, float | None]:
    total = float(metrics.get("bars_total") or 0.0)
    if total <= 0:
        return {
            "uptrend_share": None,
            "downtrend_share": None,
            "range_share": None,
            "transition_share": None,
            "unknown_share": None,
            "dominant_state": None,
            "dominant_state_share": None,
        }
    shares = {
        "uptrend_share": float(metrics.get("uptrend_bars", 0)) / total,
        "downtrend_share": float(metrics.get("downtrend_bars", 0)) / total,
        "range_share": float(metrics.get("range_bars", 0)) / total,
        "transition_share": float(metrics.get("transition_bars", 0)) / total,
        "unknown_share": float(metrics.get("unknown_bars", 0)) / total,
    }
    state_parts = {
        "uptrend": shares["uptrend_share"],
        "downtrend": shares["downtrend_share"],
        "range": shares["range_share"],
        "transition": shares["transition_share"],
        "unknown": shares["unknown_share"],
    }
    dominant = max(state_parts, key=lambda k: state_parts[k] or 0.0)
    return {
        **shares,
        "dominant_state": dominant,
        "dominant_state_share": state_parts[dominant],
    }


def compute_robustness_score(
    *,
    scores: list[float],
    degenerate_count: int,
) -> float:
    if not scores:
        return float("-inf")
    med = float(statistics.median(scores))
    std = float(statistics.pstdev(scores)) if len(scores) > 1 else 0.0
    minimum = float(min(scores))
    worst_gap = abs(minimum - med)
    score = (
        ROBUSTNESS_MEDIAN_WEIGHT * med
        - ROBUSTNESS_STDDEV_PENALTY * std
        - ROBUSTNESS_WORST_PENALTY * worst_gap
        - ROBUSTNESS_DEGENERATE_PENALTY * int(degenerate_count)
    )
    return float(f"{score:.6g}")


def aggregate_variant_results(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate per-window rows for one variant."""
    scores = [float(r["score"]) for r in rows if r.get("score") is not None]
    ranks = [int(r["rank"]) for r in rows if r.get("rank") is not None]
    degenerate_windows = sum(1 for r in rows if r.get("degenerate"))
    non_degenerate = len(rows) - degenerate_windows

    def _char_success(char: str) -> int:
        return sum(
            1
            for r in rows
            if r.get("expected_character") == char and not r.get("degenerate")
        )

    agg = {
        "window_count": len(rows),
        "mean_score": float(statistics.mean(scores)) if scores else None,
        "median_score": float(statistics.median(scores)) if scores else None,
        "min_score": float(min(scores)) if scores else None,
        "max_score": float(max(scores)) if scores else None,
        "score_stddev": float(statistics.pstdev(scores)) if len(scores) > 1 else 0.0,
        "mean_rank": float(statistics.mean(ranks)) if ranks else None,
        "median_rank": float(statistics.median(ranks)) if ranks else None,
        "worst_rank": int(max(ranks)) if ranks else None,
        "best_rank": int(min(ranks)) if ranks else None,
        "rank_stddev": float(statistics.pstdev(ranks)) if len(ranks) > 1 else 0.0,
        "top_1_count": sum(1 for r in ranks if r == 1),
        "top_2_count": sum(1 for r in ranks if r <= 2),
        "degenerate_window_count": degenerate_windows,
        "non_degenerate_window_count": non_degenerate,
        "trend_window_success_count": _char_success("uptrend") + _char_success("downtrend"),
        "range_window_success_count": _char_success("range"),
        "transition_window_success_count": _char_success("transition"),
        "robustness_score": compute_robustness_score(
            scores=scores, degenerate_count=degenerate_windows
        ),
    }
    return agg


def check_window_plausibility(
    *,
    expected_character: str,
    metrics: dict[str, Any],
) -> dict[str, Any]:
    shares = window_shares(metrics)
    dom = shares.get("dominant_state")
    dom_share = float(shares.get("dominant_state_share") or 0.0)
    trans = float(shares.get("transition_share") or 0.0)
    up = float(shares.get("uptrend_share") or 0.0)
    down = float(shares.get("downtrend_share") or 0.0)
    rng = float(shares.get("range_share") or 0.0)
    unknown = float(shares.get("unknown_share") or 0.0)
    hard_fail = False
    notes: list[str] = []
    warnings: list[str] = []

    if metrics.get("degenerate"):
        hard_fail = True
        notes.append("degenerate_metrics")

    if expected_character == "uptrend":
        if up < down and up + down > 0.01:
            hard_fail = True
            notes.append("uptrend_share_below_downtrend")
        if trans > 0.95 and up < 0.05:
            warnings.append("mostly_transition_not_clear_uptrend")
    elif expected_character == "downtrend":
        if down < up and up + down > 0.01:
            hard_fail = True
            notes.append("downtrend_share_below_uptrend")
        if trans > 0.95 and down < 0.05:
            warnings.append("mostly_transition_not_clear_downtrend")
    elif expected_character == "range":
        if dom not in {"range", "transition"} and dom_share > 0.8:
            warnings.append(f"dominant_{dom}_not_range_like")
        if rng < 0.05 and trans > 0.9:
            warnings.append("transition_dominant_not_range")
    elif expected_character == "transition":
        if trans < 0.3 and unknown < 0.3:
            warnings.append("low_transition_share")
    elif expected_character == "mixed":
        # Mixed price windows may still be scanner-transition-dominated; only hard-fail if
        # a single *directional* state dominates almost all bars.
        if dom in {"uptrend", "downtrend", "range"} and dom_share > 0.95:
            hard_fail = True
            notes.append("single_state_dominates_mixed_window")
        elif dom == "transition" and dom_share > 0.95:
            warnings.append("scanner_transition_dominates_mixed_window")

    scanner_price_mismatch = bool(warnings) and expected_character in {
        "uptrend",
        "downtrend",
        "range",
        "mixed",
    }
    return {
        "plausible": not hard_fail,
        "notes": notes,
        "warnings": warnings,
        "scanner_price_mismatch": scanner_price_mismatch,
        "shares": shares,
    }
