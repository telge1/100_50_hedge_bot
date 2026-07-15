"""Tests for the performance/state-metric/cache root-fix (Phase 26).

All tests are deterministic and require no database or scanner runs.
"""

from __future__ import annotations

from research.regime_scanner.research_variants.scoring import (
    METRIC_VERSION,
    SCORE_VERSION,
    compute_window_character_fit,
    detect_degenerate_v2,
    evaluate_window,
    score_components,
)
from research.regime_scanner.research_variants.stability import compute_stability_metrics
from research.regime_scanner.research_variants.state_buckets import (
    ALL_BUCKETS,
    STATE_BUCKET_MAP,
    bucket_counts,
    classify_research_state_bucket,
)
from research.regime_scanner.research_variants.timeline_cache import (
    evaluation_hash,
    find_covering_completed_timeline,
    slice_timeline_for_window,
    timeline_covers_window,
    timeline_fingerprint,
)
from research.regime_scanner.trend_state_machine import TrendState
from typing import get_args


ALL_TREND_STATES = list(get_args(TrendState))


def _rows(states, start="2026-03-01T00:00:00Z", step_min=5):
    import pandas as pd

    base = pd.Timestamp(start)
    out = []
    for i, s in enumerate(states):
        out.append(
            {
                "timestamp": (base + pd.Timedelta(minutes=step_min * i)).isoformat(),
                "state": s,
                "metadata_json": {},
            }
        )
    return out


# --- Phase 3/4: bucket mapping -------------------------------------------------

def test_every_trend_state_maps_to_exactly_one_bucket():
    for st in ALL_TREND_STATES:
        assert st in STATE_BUCKET_MAP, f"{st} missing from canonical map"
        assert classify_research_state_bucket(st) in ALL_BUCKETS


def test_bucket_mapping_is_exhaustive_no_loss_no_dup():
    states = ALL_TREND_STATES * 3
    counts = bucket_counts(states)
    assert sum(counts.values()) == len(states)


def test_bullish_states_map_to_uptrend():
    for st in ("strong_bullish", "early_bullish", "bullish_warning"):
        assert classify_research_state_bucket(st) == "uptrend"


def test_bearish_states_map_to_downtrend():
    for st in ("strong_bearish", "early_bearish", "bearish_warning"):
        assert classify_research_state_bucket(st) == "downtrend"


def test_neutral_maps_to_range():
    assert classify_research_state_bucket("neutral") == "range"


def test_weakening_and_turning_map_to_transition():
    for st in ("bullish_weakening", "bearish_weakening", "topping", "bottoming"):
        assert classify_research_state_bucket(st) == "transition"


def test_unknown_only_for_unavailable_or_unmapped():
    assert classify_research_state_bucket("unavailable") == "unknown"
    assert classify_research_state_bucket("some_new_state") == "unknown"
    assert classify_research_state_bucket("") == "unknown"
    # a real directional state is never unknown
    assert classify_research_state_bucket("strong_bullish") != "unknown"


# --- Phase 5: score components -------------------------------------------------

def test_score_components_sum_to_raw_component_score():
    states = ["strong_bullish"] * 20 + ["bullish_weakening"] * 5 + ["strong_bearish"] * 20
    ev = evaluate_window(trend_states=_rows(states), structure_events=[])
    total = sum(c["weighted_value"] for c in ev["score_components"].values())
    assert abs(total - ev["raw_component_score"]) < 1e-6


# --- Phase 6: degenerate rules -------------------------------------------------

def test_full_transition_is_degenerate_excessive_transition():
    states = ["bearish_weakening"] * 900 + ["bottoming"] * 100
    degen, reason = detect_degenerate_v2(states, [])
    assert degen and reason == "excessive_transition"


def test_full_unknown_is_degenerate():
    states = ["unavailable"] * 1000
    degen, reason = detect_degenerate_v2(states, [])
    assert degen and reason == "mostly_unknown"


def test_long_duration_transition_never_positive_score():
    # reproduces the trend_up_late_feb pathology: 100% transition, huge duration
    states = ["bearish_weakening"] * 1737 + ["bottoming"] * 280
    ev = evaluate_window(trend_states=_rows(states), structure_events=[])
    assert ev["degenerate"] is True
    assert ev["rankable"] is False
    assert ev["stability_score"] <= -50.0
    # raw component score reproduces the old (buggy) positive value for transparency
    assert ev["raw_component_score"] > 0


def test_healthy_trend_is_not_degenerate():
    states = (["strong_bullish"] * 300 + ["bullish_warning"] * 50 + ["neutral"] * 100
              + ["strong_bearish"] * 300 + ["bearish_weakening"] * 50)
    struct = [{"timestamp": "2026-03-01T01:00:00Z", "event_type": "bullish_bos"}]
    degen, reason = detect_degenerate_v2(states, struct)
    assert degen is False and reason is None


# --- Phase 6.2: character fit --------------------------------------------------

def test_uptrend_states_in_range_window_get_poor_character_fit():
    states = ["strong_bullish"] * 1000
    fit = compute_window_character_fit(expected_character="range", states=states)
    assert fit["window_character_fit"] == 0.0


def test_uptrend_window_with_uptrend_states_gets_high_fit():
    states = ["strong_bullish"] * 900 + ["bullish_warning"] * 100
    fit = compute_window_character_fit(expected_character="uptrend", states=states)
    assert fit["window_character_fit"] > 0.9


# --- Phase 12/21: slice + covering -------------------------------------------

def test_slice_trend_inclusive_structure_exclusive():
    trend = _rows(["neutral"] * 5, start="2026-03-01T00:00:00Z")
    struct = [
        {"timestamp": "2026-03-01T00:00:00Z", "event_type": "x"},
        {"timestamp": "2026-03-01T00:20:00Z", "event_type": "y"},  # == end
    ]
    sl_t, sl_s = slice_timeline_for_window(
        trend, struct, start="2026-03-01T00:00:00Z", end="2026-03-01T00:20:00Z"
    )
    # trend end inclusive: keeps the 00:20 bar (index 4)
    assert sl_t[-1]["timestamp"].startswith("2026-03-01T00:20")
    # structure end exclusive: drops the 00:20 event
    assert all(not r["timestamp"].startswith("2026-03-01T00:20") for r in sl_s)


def test_slice_is_deterministic():
    trend = _rows(["neutral", "strong_bullish", "neutral"], start="2026-03-01T00:00:00Z")
    a = slice_timeline_for_window(trend, [], start="2026-03-01T00:00:00Z", end="2026-03-01T00:10:00Z")
    b = slice_timeline_for_window(trend, [], start="2026-03-01T00:00:00Z", end="2026-03-01T00:10:00Z")
    assert [r["state"] for r in a[0]] == [r["state"] for r in b[0]]


def test_timeline_covers_window():
    run = {"status": "completed", "start_time": "2026-02-01T00:00:00Z", "end_time": "2026-03-15T00:00:00Z"}
    assert timeline_covers_window(run, start="2026-02-25T00:00:00Z", end="2026-03-04T00:00:00Z")
    assert not timeline_covers_window(run, start="2026-02-25T00:00:00Z", end="2026-04-01T00:00:00Z")


def test_incomplete_status_never_covers():
    for status in ("running", "failed", "interrupted", "building"):
        run = {"status": status, "start_time": "2026-01-01T00:00:00Z", "end_time": "2027-01-01T00:00:00Z"}
        assert not timeline_covers_window(run, start="2026-02-25T00:00:00Z", end="2026-03-04T00:00:00Z")


# --- Phase 15/17: fingerprint reuse semantics --------------------------------

def _fp(**over):
    base = dict(
        prepared_context_hash="pc", parameter_hash="ph", scanner_version="v1",
        warmup_start="2025-12-27T00:00:00Z", timeline_start="2026-02-01T00:00:00Z",
        timeline_end="2026-03-15T00:00:00Z",
    )
    base.update(over)
    return timeline_fingerprint(**base)


def test_identical_fingerprint_inputs_reuse():
    assert _fp() == _fp()


def test_changed_parameter_hash_changes_fingerprint():
    assert _fp() != _fp(parameter_hash="other")


def test_changed_scanner_version_changes_fingerprint():
    assert _fp() != _fp(scanner_version="v2")


def test_changed_prepared_context_changes_fingerprint():
    assert _fp() != _fp(prepared_context_hash="other")


def test_changed_window_range_changes_fingerprint():
    assert _fp() != _fp(timeline_end="2026-03-16T00:00:00Z")


def test_evaluation_hash_changes_with_score_version():
    a = evaluation_hash(timeline_id="t", window_hash="w", metric_version=2, score_version=2)
    b = evaluation_hash(timeline_id="t", window_hash="w", metric_version=2, score_version=3)
    assert a != b


# --- purity: re-scoring never mutates raw rows -------------------------------

def test_rescoring_does_not_mutate_stored_rows():
    states = ["strong_bullish"] * 50 + ["neutral"] * 50
    rows = _rows(states)
    snapshot = [dict(r) for r in rows]
    evaluate_window(trend_states=rows, structure_events=[])
    compute_stability_metrics(trend_states=rows, structure_events=[])
    assert rows == snapshot


def test_metric_and_score_versions_exposed():
    ev = evaluate_window(trend_states=_rows(["neutral"] * 10), structure_events=[])
    assert ev["metric_version"] == METRIC_VERSION
    assert ev["score_version"] == SCORE_VERSION
