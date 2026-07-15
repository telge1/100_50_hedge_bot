"""Corrected, versioned window scoring built on stored metrics (no scanner runs).

Separates versions explicitly (Phase 17):

* ``METRIC_VERSION``  : bucket counts / stability metric derivation
* ``SCORE_VERSION``   : the score formula + degenerate rules

Both operate purely on already-stored trend states and structure events, so a
score or metric change never requires a scanner or timeline rebuild.

Root cause fixed here (Phase 6): a window that is dominated by the *transition
bucket* (sum of weakening/topping/bottoming) was not flagged degenerate because
the old rule only checked single *raw-state* dominance. A long-duration state
then earned a positive stability bonus (e.g. trend_up_late_feb = +6.5). The
corrected rules operate on bucket shares.
"""

from __future__ import annotations

import statistics
from typing import Any

from research.regime_scanner.research_variants.stability import (
    PENALTY_CONFLICT,
    PENALTY_EXCESSIVE_TRANSITION_SCALE,
    PENALTY_REVERSAL_3,
    PENALTY_SHORT_RUN,
    TRANSITION_SHARE_SOFT_CAP,
    WEIGHT_STABLE_DURATION,
    WEIGHT_STRUCTURE_CONSISTENCY,
    WEIGHT_TRANSITION_RESOLUTION,
    compute_stability_metrics,
)
from research.regime_scanner.research_variants.state_buckets import bucket_counts

METRIC_VERSION = 2
SCORE_VERSION = 2

# Corrected degenerate thresholds (bucket-level).
DEGENERATE_BUCKET_SHARE = 0.90
DEGENERATE_PENALTY = 50.0
# A degenerate window must never end up non-negative regardless of duration bonus.
DEGENERATE_SCORE_CEIL = -DEGENERATE_PENALTY

# window_character_fit weights (descriptive; kept separate from stability_score).
CHARACTER_FIT_MIN = 0.0
CHARACTER_FIT_MAX = 1.0


def bucket_shares(states: list[str]) -> dict[str, float]:
    n = len(states)
    counts = bucket_counts(states)
    if n == 0:
        return {b: 0.0 for b in counts}
    return {b: c / n for b, c in counts.items()}


def detect_degenerate_v2(states: list[str], structure_events: list[dict[str, Any]]) -> tuple[bool, str | None]:
    """Corrected bucket-level degeneracy detection."""
    n = len(states)
    if n <= 0:
        return True, "no_bars"
    shares = bucket_shares(states)
    if shares.get("unknown", 0.0) > DEGENERATE_BUCKET_SHARE:
        return True, "mostly_unknown"
    if shares.get("transition", 0.0) > DEGENERATE_BUCKET_SHARE:
        return True, "excessive_transition"
    # Single directional/range bucket that leaves no room for meaningful contrast.
    meaningful = shares.get("uptrend", 0.0) + shares.get("downtrend", 0.0) + shares.get("range", 0.0)
    dominant_bucket = max(shares, key=lambda b: shares[b])
    if shares[dominant_bucket] > DEGENERATE_BUCKET_SHARE and dominant_bucket in {"uptrend", "downtrend", "range"}:
        # A single true-trend bucket dominating is only degenerate if there is no
        # structure at all (flat line); otherwise it is a legitimate strong trend.
        if not structure_events:
            return True, "single_state_dominant"
    if meaningful < (1.0 - DEGENERATE_BUCKET_SHARE):
        # <10% of bars are in a meaningful trend/range bucket -> nothing to rank.
        return True, "no_meaningful_regime"
    return False, None


def score_components(metrics: dict[str, Any]) -> dict[str, dict[str, float]]:
    """Return each score component's raw value, weight and weighted contribution."""
    up_share = metrics.get("uptrend_with_bullish_structure_share")
    down_share = metrics.get("downtrend_with_bearish_structure_share")
    parts = [x for x in (up_share, down_share) if x is not None]
    structure_consistency = float(statistics.mean(parts)) if parts else 0.5

    med_dur = float(metrics.get("median_state_duration_bars") or 0.0)
    stable_duration = min(med_dur / 6.0, 1.0)

    trans_to_trend = float(metrics.get("transition_to_trend_count") or 0.0)
    trans_total = float(metrics.get("transition_bars") or 0.0)
    transition_resolution = (
        min(trans_to_trend / max(trans_total, 1.0), 1.0) if trans_total else 0.5
    )

    short_runs = float(metrics.get("short_state_run_count") or 0.0)
    reversals3 = float(metrics.get("reversal_within_3_bars_count") or 0.0)
    conflicts = float(metrics.get("trend_structure_conflict_count") or 0.0)
    trans_share = float(metrics.get("transition_share") or 0.0)
    excessive_transition_raw = max(0.0, trans_share - TRANSITION_SHARE_SOFT_CAP)

    return {
        "structure_consistency_component": {
            "raw_value": structure_consistency,
            "weight": WEIGHT_STRUCTURE_CONSISTENCY,
            "weighted_value": structure_consistency * WEIGHT_STRUCTURE_CONSISTENCY,
        },
        "median_duration_component": {
            "raw_value": stable_duration,
            "weight": WEIGHT_STABLE_DURATION,
            "weighted_value": stable_duration * WEIGHT_STABLE_DURATION,
        },
        "transition_resolution_component": {
            "raw_value": transition_resolution,
            "weight": WEIGHT_TRANSITION_RESOLUTION,
            "weighted_value": transition_resolution * WEIGHT_TRANSITION_RESOLUTION,
        },
        "short_run_penalty": {
            "raw_value": short_runs,
            "weight": -PENALTY_SHORT_RUN,
            "weighted_value": -short_runs * PENALTY_SHORT_RUN,
        },
        "reversal_penalty": {
            "raw_value": reversals3,
            "weight": -PENALTY_REVERSAL_3,
            "weighted_value": -reversals3 * PENALTY_REVERSAL_3,
        },
        "structure_conflict_penalty": {
            "raw_value": conflicts,
            "weight": -PENALTY_CONFLICT,
            "weighted_value": -conflicts * PENALTY_CONFLICT,
        },
        "excessive_transition_penalty": {
            "raw_value": excessive_transition_raw,
            "weight": -PENALTY_EXCESSIVE_TRANSITION_SCALE,
            "weighted_value": -excessive_transition_raw * PENALTY_EXCESSIVE_TRANSITION_SCALE,
        },
    }


def _sum_components(components: dict[str, dict[str, float]]) -> float:
    return float(sum(c["weighted_value"] for c in components.values()))


def compute_window_character_fit(*, expected_character: str, states: list[str]) -> dict[str, Any]:
    """Descriptive fit of baseline state distribution to the intended window character.

    Kept strictly separate from stability_score (never mixed in).
    """
    shares = bucket_shares(states)
    up = shares.get("uptrend", 0.0)
    down = shares.get("downtrend", 0.0)
    rng = shares.get("range", 0.0)
    trans = shares.get("transition", 0.0)
    unknown = shares.get("unknown", 0.0)

    ec = str(expected_character)
    if ec == "uptrend":
        fit = up
    elif ec == "downtrend":
        fit = down
    elif ec == "range":
        fit = rng
    elif ec == "transition":
        # real transition needs transition bars AND at least some directional contrast.
        fit = trans * (1.0 if (up + down) > 0.02 else 0.5)
    elif ec == "mixed":
        # reward presence of several meaningful buckets, penalize single dominance.
        meaningful = [s for s in (up, down, rng) if s > 0.02]
        fit = min(len(meaningful) / 3.0, 1.0) * (1.0 - unknown)
    else:
        fit = 0.0
    fit = max(CHARACTER_FIT_MIN, min(CHARACTER_FIT_MAX, float(fit)))
    return {
        "expected_character": ec,
        "window_character_fit": float(f"{fit:.6g}"),
        "bucket_shares": {k: float(f"{v:.6g}") for k, v in shares.items()},
    }


def evaluate_window(
    *,
    trend_states: list[dict[str, Any]],
    structure_events: list[dict[str, Any]],
    expected_character: str | None = None,
) -> dict[str, Any]:
    """Full corrected evaluation from stored rows only (no scanner)."""
    metrics = compute_stability_metrics(
        trend_states=trend_states, structure_events=structure_events
    )
    states = [str(r.get("state") or "") for r in trend_states]

    degenerate, reason = detect_degenerate_v2(states, structure_events)
    components = score_components(metrics)
    raw_component_score = float(f"{_sum_components(components):.6g}")

    if degenerate:
        final_score = min(raw_component_score, DEGENERATE_SCORE_CEIL)
    else:
        final_score = raw_component_score

    rankable = not degenerate

    result = {
        "metric_version": METRIC_VERSION,
        "score_version": SCORE_VERSION,
        "bars_total": metrics.get("bars_total"),
        "bucket_shares": bucket_shares(states),
        "metrics": metrics,
        "score_components": components,
        "raw_component_score": raw_component_score,
        "stability_score": float(f"{final_score:.6g}"),
        "degenerate": degenerate,
        "degenerate_reason": reason,
        "rankable": rankable,
    }
    if expected_character is not None:
        result["character_fit"] = compute_window_character_fit(
            expected_character=expected_character, states=states
        )
    return result
