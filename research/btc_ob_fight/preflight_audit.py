"""Preflight audit of Phase-2A raw episodes (read-only validation)."""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from datetime import datetime
from typing import Any

from .profile_edge_state import (
    STATE_BETWEEN_LOWER,
    STATE_BETWEEN_UPPER,
    STATE_INSIDE_BOTH,
    STATE_OUTSIDE_ABOVE,
    STATE_OUTSIDE_BELOW,
)
from .profile_state_episodes import END_REASON_STATE_CHANGE, END_REASON_WINDOW_END

DURATION_BUCKETS = (
    (0.1, "under_100ms"),
    (0.5, "under_500ms"),
    (1.0, "under_1s"),
    (2.0, "under_2s"),
    (5.0, "under_5s"),
    (10.0, "under_10s"),
    (30.0, "under_30s"),
)


def _percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    idx = min(len(s) - 1, max(0, int(round(p * (len(s) - 1)))))
    return s[idx]


def _duration_stats(durations: list[float]) -> dict[str, Any]:
    if not durations:
        return {"count": 0}
    return {
        "count": len(durations),
        "min": min(durations),
        "p25": _percentile(durations, 0.25),
        "median": statistics.median(durations),
        "p75": _percentile(durations, 0.75),
        "p90": _percentile(durations, 0.90),
        "p95": _percentile(durations, 0.95),
        "max": max(durations),
        "total_seconds": sum(durations),
    }


def build_preflight_audit(
    episode_bundle: dict[str, Any],
    *,
    phase2a_reclaims: list[dict[str, Any]] | None = None,
    edge_consumption: list[dict[str, Any]] | None = None,
    trades_meta: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    episodes = episode_bundle.get("episodes") or []
    transitions = episode_bundle.get("transitions") or []
    durations_all = [float(e.get("duration_seconds") or 0) for e in episodes]

    state_counts = Counter(e.get("state") for e in episodes)
    by_state_dur: dict[str, list[float]] = defaultdict(list)
    for e in episodes:
        by_state_dur[e.get("state")].append(float(e.get("duration_seconds") or 0))

    bucket_counts = {label: 0 for _, label in DURATION_BUCKETS}
    bucket_counts["at_least_30s"] = 0
    for d in durations_all:
        placed = False
        for thresh, label in DURATION_BUCKETS:
            if d < thresh:
                bucket_counts[label] += 1
                placed = True
                break
        if not placed:
            bucket_counts["at_least_30s"] += 1

    outside_eps = [e for e in episodes if e.get("state") in (STATE_OUTSIDE_ABOVE, STATE_OUTSIDE_BELOW)]
    outside_closed = all(e.get("closed") for e in outside_eps)
    window_end_reclaims = sum(
        1 for r in (phase2a_reclaims or []) if r.get("to_profile_state") == END_REASON_WINDOW_END
    )

    phase2a_cross_ts = [r.get("cross_ts") for r in (phase2a_reclaims or [])]
    unique_cross = len(set(phase2a_cross_ts))
    phase2a_reclaim_bug = (
        len(phase2a_reclaims or []) > 1
        and unique_cross == 1
        and len(outside_eps) == len(phase2a_reclaims or [])
    )

    edge_ticks_checked = sorted(
        {int(e.get("price_tick") or 0) for e in (edge_consumption or []) if e.get("price_tick")}
    )

    obs_start = episode_bundle.get("observation_start_utc")
    obs_end = episode_bundle.get("observation_end_utc")
    total_obs = None
    trade_span = None
    if obs_start and obs_end:
        s = datetime.fromisoformat(obs_start.replace("Z", "+00:00"))
        e = datetime.fromisoformat(obs_end.replace("Z", "+00:00"))
        total_obs = (e - s).total_seconds()
    episodes = episode_bundle.get("episodes") or []
    if episodes:
        first_ts = datetime.fromisoformat(episodes[0]["start_ts"].replace("Z", "+00:00"))
        last_ts = datetime.fromisoformat(episodes[-1]["end_ts"].replace("Z", "+00:00"))
        trade_span = (last_ts - first_ts).total_seconds()

    distribution_rows: list[dict[str, Any]] = []
    for state in (
        STATE_INSIDE_BOTH,
        STATE_BETWEEN_UPPER,
        STATE_BETWEEN_LOWER,
        STATE_OUTSIDE_ABOVE,
        STATE_OUTSIDE_BELOW,
    ):
        stats = _duration_stats(by_state_dur.get(state, []))
        distribution_rows.append({"state": state, **stats})

    audit = {
        "preflight_version": "phase_2a1_v1",
        "q1_why_episode_count": {
            "episode_count": len(episodes),
            "cause": "profile_state_changes_on_each_deduplicated_public_trade_post_anchor_crossing_edge_tick_boundary",
            "state_change_source": "deduplicated_public_trade_chronological",
            "not_candle_or_bucket": True,
            "trade_count_observed": episode_bundle.get("trade_count_observed"),
        },
        "q2_state_distribution": dict(state_counts),
        "q3_duration_by_state": {s: _duration_stats(by_state_dur.get(s, [])) for s in state_counts},
        "q4_duration_buckets": bucket_counts,
        "q5_outside_episodes_closed": {
            "count": len(outside_eps),
            "all_closed": outside_closed,
            "window_end_outside": sum(1 for e in outside_eps if e.get("end_reason") == END_REASON_WINDOW_END),
        },
        "q6_reclaim_chronological_cross": {
            "phase2a_reclaim_count": len(phase2a_reclaims or []),
            "unique_cross_ts_in_phase2a": unique_cross,
            "phase2a_global_first_reclaim_bug": phase2a_reclaim_bug,
        },
        "q7_window_end_as_reclaim": {
            "window_end_reclaim_count_phase2a": window_end_reclaims,
            "note": "Phase 2A reclaim builder does not filter WINDOW_END; corrected in Phase 2A.1",
        },
        "q8_reclaim_equals_outside_count": {
            "outside_count": len(outside_eps),
            "phase2a_reclaim_count": len(phase2a_reclaims or []),
            "equal": len(outside_eps) == len(phase2a_reclaims or []),
            "explanation_if_equal": "Phase 2A assigns one reclaim per outside episode via flawed global-first transition match",
        },
        "q9_state_change_data_source": "each_deduplicated_public_trade_sorted_by_ts_trade_id",
        "q10_trade_dedup_and_sort": {
            "deduplicated_by_trade_id": True,
            "sort_key": "(ts, trade_id)",
            "meta": trades_meta,
        },
        "q11_identical_timestamp_order": "deterministic_trade_id_tiebreak",
        "q12_edge_event_ticks": edge_ticks_checked,
        "q13_exact_tick_only": {
            "exact_level_ticks_only_in_phase2a": True,
            "ticks_observed": edge_ticks_checked,
        },
        "q14_ob_coverage_at_edges": "requires_phase_2a1_edge_book_coverage",
        "q15_profile_edges_in_ob200": "requires_phase_2a1_edge_book_coverage",
        "duration_sum_seconds": sum(durations_all),
        "observation_window_seconds": total_obs,
        "trade_span_seconds": trade_span,
        "duration_sum_matches_window": (
            total_obs is not None and abs(sum(durations_all) - total_obs) < 1.0
        ),
        "duration_sum_matches_trade_span": (
            trade_span is not None and abs(sum(durations_all) - trade_span) < 1.0
        ),
        "transition_count": len(transitions),
    }
    return audit, distribution_rows
