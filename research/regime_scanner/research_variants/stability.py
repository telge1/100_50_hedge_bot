"""Stability metrics and transparent scoring for variant comparison."""

from __future__ import annotations

import math
import statistics
from typing import Any

# State taxonomy is the canonical research bucket mapping (single source of truth).
from research.regime_scanner.research_variants.state_buckets import (
    DOWNTREND_STATES,
    RANGE_STATES,
    TRANSITION_STATES,
    UNKNOWN_STATES,
    UPTREND_STATES,
)

SHORT_RUN_THRESHOLD_BARS = 3
REVERSAL_WINDOW_3 = 3
REVERSAL_WINDOW_6 = 6

BULLISH_STRUCTURE = frozenset({"bullish", "bullish_choch", "bullish_bos", "higher_low"})
BEARISH_STRUCTURE = frozenset({"bearish", "bearish_choch", "bearish_bos", "lower_high"})
CHOCH_TYPES = frozenset({"bullish_choch", "bearish_choch"})

# Documented score weights (higher = more stable regime recognition).
WEIGHT_STRUCTURE_CONSISTENCY = 25.0
WEIGHT_STABLE_DURATION = 20.0
WEIGHT_TRANSITION_RESOLUTION = 15.0
PENALTY_SHORT_RUN = 2.0
PENALTY_REVERSAL_3 = 3.0
PENALTY_CONFLICT = 2.0
PENALTY_EXCESSIVE_TRANSITION_SCALE = 40.0
TRANSITION_SHARE_SOFT_CAP = 0.35
DEGENERATE_DOMINANT_SHARE = 0.90
DEGENERATE_PENALTY = 50.0


def _direction_of(state: str) -> str | None:
    if state in UPTREND_STATES:
        return "up"
    if state in DOWNTREND_STATES:
        return "down"
    if state in TRANSITION_STATES:
        return "transition"
    if state in RANGE_STATES:
        return "range"
    return "unknown"


def _run_lengths(states: list[str]) -> list[int]:
    if not states:
        return []
    lengths: list[int] = []
    cur = states[0]
    n = 1
    for st in states[1:]:
        if st == cur:
            n += 1
        else:
            lengths.append(n)
            cur = st
            n = 1
    lengths.append(n)
    return lengths


def _state_runs(states: list[str]) -> list[tuple[str, int]]:
    if not states:
        return []
    runs: list[tuple[str, int]] = []
    cur = states[0]
    n = 1
    for st in states[1:]:
        if st == cur:
            n += 1
        else:
            runs.append((cur, n))
            cur = st
            n = 1
    runs.append((cur, n))
    return runs


def compute_stability_metrics(
    *,
    trend_states: list[dict[str, Any]],
    structure_events: list[dict[str, Any]],
) -> dict[str, Any]:
    states = [str(r.get("state") or "") for r in trend_states]
    bars_total = len(states)

    def _count(st_set: frozenset[str]) -> int:
        return sum(1 for s in states if s in st_set)

    uptrend_bars = _count(UPTREND_STATES)
    downtrend_bars = _count(DOWNTREND_STATES)
    range_bars = _count(RANGE_STATES)
    transition_bars = _count(TRANSITION_STATES)
    unknown_bars = _count(UNKNOWN_STATES)

    state_change_count = 0
    direction_change_count = 0
    trend_to_transition_count = 0
    transition_to_trend_count = 0
    direct_up_to_down_count = 0
    direct_down_to_up_count = 0

    prev_state = None
    prev_dir = None
    for st in states:
        cur_dir = _direction_of(st)
        if prev_state is not None and st != prev_state:
            state_change_count += 1
            if prev_dir and cur_dir and prev_dir != cur_dir:
                direction_change_count += 1
            if prev_dir in {"up", "down"} and cur_dir == "transition":
                trend_to_transition_count += 1
            if prev_dir == "transition" and cur_dir in {"up", "down"}:
                transition_to_trend_count += 1
            if prev_dir == "up" and cur_dir == "down":
                direct_up_to_down_count += 1
            if prev_dir == "down" and cur_dir == "up":
                direct_down_to_up_count += 1
        prev_state = st
        prev_dir = cur_dir

    run_lengths = _run_lengths(states)
    avg_duration = float(statistics.mean(run_lengths)) if run_lengths else 0.0
    med_duration = float(statistics.median(run_lengths)) if run_lengths else 0.0
    min_duration = float(min(run_lengths)) if run_lengths else 0.0
    max_duration = float(max(run_lengths)) if run_lengths else 0.0

    def _avg_for(st_set: frozenset[str]) -> float:
        lens = [n for st, n in _state_runs(states) if st in st_set]
        return float(statistics.mean(lens)) if lens else 0.0

    short_state_run_count = sum(1 for n in run_lengths if n < SHORT_RUN_THRESHOLD_BARS)

    reversal_within_3_bars_count = 0
    reversal_within_6_bars_count = 0
    for i in range(len(states) - 2):
        a, b, c = states[i], states[i + 1], states[i + 2]
        da, dc = _direction_of(a), _direction_of(c)
        if da == "up" and dc == "up" and _direction_of(b) == "down":
            reversal_within_3_bars_count += 1
        if da == "down" and dc == "down" and _direction_of(b) == "up":
            reversal_within_3_bars_count += 1
    for i in range(len(states) - 5):
        window = states[i : i + 6]
        dirs = [_direction_of(s) for s in window]
        if "up" in dirs and "down" in dirs and dirs.index("up") < dirs.index("down"):
            if "up" in dirs[dirs.index("down") + 1 :]:
                reversal_within_6_bars_count += 1
        if "down" in dirs and "up" in dirs and dirs.index("down") < dirs.index("up"):
            if "down" in dirs[dirs.index("up") + 1 :]:
                reversal_within_6_bars_count += 1

    transition_share = float(transition_bars / bars_total) if bars_total else 0.0
    trans_runs = [n for st, n in _state_runs(states) if st in TRANSITION_STATES]
    average_transition_duration = float(statistics.mean(trans_runs)) if trans_runs else 0.0

    transition_to_up_count = 0
    transition_to_down_count = 0
    transition_without_following_trend_count = 0
    for i in range(len(states) - 1):
        if states[i] in TRANSITION_STATES:
            nxt = states[i + 1]
            if nxt in UPTREND_STATES:
                transition_to_up_count += 1
            elif nxt in DOWNTREND_STATES:
                transition_to_down_count += 1
            elif nxt not in TRANSITION_STATES:
                transition_without_following_trend_count += 1

    # Structure consistency: compare trend state direction vs structure_5m.bias in metadata.
    uptrend_bullish = 0
    uptrend_total = 0
    downtrend_bearish = 0
    downtrend_total = 0
    trend_structure_conflict_count = 0
    for row in trend_states:
        st = str(row.get("state") or "")
        meta = row.get("metadata_json") or {}
        bias = None
        if isinstance(meta, dict):
            s5 = meta.get("structure_5m") or {}
            if isinstance(s5, dict):
                bias = s5.get("bias")
        if st in UPTREND_STATES:
            uptrend_total += 1
            if bias == "bullish":
                uptrend_bullish += 1
            elif bias == "bearish":
                trend_structure_conflict_count += 1
        elif st in DOWNTREND_STATES:
            downtrend_total += 1
            if bias == "bearish":
                downtrend_bearish += 1
            elif bias == "bullish":
                trend_structure_conflict_count += 1

    uptrend_with_bullish_structure_share = (
        float(uptrend_bullish / uptrend_total) if uptrend_total else None
    )
    downtrend_with_bearish_structure_share = (
        float(downtrend_bearish / downtrend_total) if downtrend_total else None
    )

    # Turn detection (descriptive only).
    choch_times: list[tuple[str, str]] = []
    for ev in structure_events:
        et = str(ev.get("event_type") or "")
        if et in CHOCH_TYPES:
            choch_times.append((str(ev.get("timestamp") or ""), et))

    detected_turn_count = 0
    direct_flip_without_transition_count = direct_up_to_down_count + direct_down_to_up_count
    bars_from_choch_to_new_trend: list[int] = []
    bars_from_first_opposite_structure_to_new_trend: list[int] = []

    ts_to_idx = {str(r.get("timestamp")): i for i, r in enumerate(trend_states)}
    for i in range(1, len(states)):
        prev, cur = states[i - 1], states[i]
        if prev in UPTREND_STATES and cur in DOWNTREND_STATES:
            detected_turn_count += 1
        elif prev in DOWNTREND_STATES and cur in UPTREND_STATES:
            detected_turn_count += 1

    for choch_ts, choch_type in choch_times:
        idx = ts_to_idx.get(choch_ts)
        if idx is None:
            continue
        target = DOWNTREND_STATES if choch_type == "bearish_choch" else UPTREND_STATES
        for j in range(idx + 1, min(idx + 48, len(states))):
            if states[j] in target:
                bars_from_choch_to_new_trend.append(j - idx)
                break

    degenerate, degenerate_reason = _detect_degenerate(
        states=states,
        bars_total=bars_total,
        state_change_count=state_change_count,
        transition_bars=transition_bars,
        unknown_bars=unknown_bars,
    )

    metrics = {
        "bars_total": float(bars_total),
        "uptrend_bars": float(uptrend_bars),
        "downtrend_bars": float(downtrend_bars),
        "range_bars": float(range_bars),
        "transition_bars": float(transition_bars),
        "unknown_bars": float(unknown_bars),
        "state_change_count": float(state_change_count),
        "direction_change_count": float(direction_change_count),
        "trend_to_transition_count": float(trend_to_transition_count),
        "transition_to_trend_count": float(transition_to_trend_count),
        "direct_up_to_down_count": float(direct_up_to_down_count),
        "direct_down_to_up_count": float(direct_down_to_up_count),
        "average_state_duration_bars": avg_duration,
        "median_state_duration_bars": med_duration,
        "minimum_state_duration_bars": min_duration,
        "maximum_state_duration_bars": max_duration,
        "avg_uptrend_duration": _avg_for(UPTREND_STATES),
        "avg_downtrend_duration": _avg_for(DOWNTREND_STATES),
        "avg_range_duration": _avg_for(RANGE_STATES),
        "avg_transition_duration": _avg_for(TRANSITION_STATES),
        "short_state_run_count": float(short_state_run_count),
        "reversal_within_3_bars_count": float(reversal_within_3_bars_count),
        "reversal_within_6_bars_count": float(reversal_within_6_bars_count),
        "transition_share": transition_share,
        "average_transition_duration": average_transition_duration,
        "transition_without_following_trend_count": float(transition_without_following_trend_count),
        "transition_to_up_count": float(transition_to_up_count),
        "transition_to_down_count": float(transition_to_down_count),
        "uptrend_with_bullish_structure_share": uptrend_with_bullish_structure_share,
        "downtrend_with_bearish_structure_share": downtrend_with_bearish_structure_share,
        "trend_structure_conflict_count": float(trend_structure_conflict_count),
        "detected_turn_count": float(detected_turn_count),
        "direct_flip_without_transition_count": float(direct_flip_without_transition_count),
        "avg_bars_choch_to_new_trend": (
            float(statistics.mean(bars_from_choch_to_new_trend))
            if bars_from_choch_to_new_trend
            else None
        ),
        "degenerate": degenerate,
        "degenerate_reason": degenerate_reason,
    }
    metrics["score"] = compute_stability_score(metrics)
    return metrics


def _detect_degenerate(
    *,
    states: list[str],
    bars_total: int,
    state_change_count: int,
    transition_bars: int,
    unknown_bars: int,
) -> tuple[bool, str | None]:
    if bars_total <= 0:
        return True, "no_bars"
    if unknown_bars / bars_total > DEGENERATE_DOMINANT_SHARE:
        return True, "mostly_unknown"
    counts: dict[str, int] = {}
    for s in states:
        counts[s] = counts.get(s, 0) + 1
    dominant = max(counts.values()) / bars_total
    if dominant > DEGENERATE_DOMINANT_SHARE:
        return True, "single_state_dominant"
    if state_change_count == 0 and transition_bars == 0 and bars_total > 10:
        return True, "no_changes"
    return False, None


def compute_stability_score(metrics: dict[str, Any]) -> float:
    if metrics.get("degenerate"):
        return -DEGENERATE_PENALTY

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

    flip_flop_penalty = (
        float(metrics.get("short_state_run_count") or 0.0) * PENALTY_SHORT_RUN
        + float(metrics.get("reversal_within_3_bars_count") or 0.0) * PENALTY_REVERSAL_3
    )
    conflict_penalty = float(metrics.get("trend_structure_conflict_count") or 0.0) * PENALTY_CONFLICT
    trans_share = float(metrics.get("transition_share") or 0.0)
    excessive_transition = max(0.0, trans_share - TRANSITION_SHARE_SOFT_CAP) * PENALTY_EXCESSIVE_TRANSITION_SCALE

    score = (
        structure_consistency * WEIGHT_STRUCTURE_CONSISTENCY
        + stable_duration * WEIGHT_STABLE_DURATION
        + transition_resolution * WEIGHT_TRANSITION_RESOLUTION
        - flip_flop_penalty
        - conflict_penalty
        - excessive_transition
    )
    return float(f"{score:.6g}")


def stability_metrics_to_run_metrics(metrics: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for key, value in sorted(metrics.items()):
        if key in {"degenerate", "degenerate_reason"}:
            continue
        if value is None:
            rows.append({"metric_name": f"stability_{key}", "metric_value": None, "metric_text": None})
        elif isinstance(value, bool):
            rows.append(
                {
                    "metric_name": f"stability_{key}",
                    "metric_value": None,
                    "metric_text": "true" if value else "false",
                }
            )
        elif isinstance(value, (int, float)) and math.isfinite(float(value)):
            rows.append(
                {"metric_name": f"stability_{key}", "metric_value": float(value), "metric_text": None}
            )
        else:
            rows.append(
                {"metric_name": f"stability_{key}", "metric_value": None, "metric_text": str(value)}
            )
    rows.append(
        {
            "metric_name": "stability_degenerate",
            "metric_value": 1.0 if metrics.get("degenerate") else 0.0,
            "metric_text": metrics.get("degenerate_reason"),
        }
    )
    return rows
