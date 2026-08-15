"""Unit tests for protected-level approach pull break-risk audit."""

from __future__ import annotations

from research.orderbook.historical_protected_level_pull_break_risk.approaches import (
    ApproachEpisode,
    _abs_dist_bps,
    _dist_bps,
    cluster_overlaps,
)
from research.orderbook.historical_protected_level_pull_break_risk.features import choose_anchor
from research.orderbook.historical_protected_level_pull_break_risk.stats import (
    BREAK,
    HOLD,
    auc_distance_pull,
    decide_primary,
    feature_comparison,
    match_controls,
    mann_whitney_auc,
)


def test_distance_mirroring_bearish_bullish():
    # bearish low: price above level → positive safe distance
    assert _dist_bps(100.5, 100.0, side="low") > 0
    # bullish high: price below level → positive safe distance
    assert _dist_bps(99.5, 100.0, side="high") > 0
    assert _abs_dist_bps(100.5, 100.0) == _abs_dist_bps(99.5, 100.0)


def test_choose_anchor_prefers_10bps_no_break_lookahead():
    ep = ApproachEpisode(
        approach_id="x",
        symbol="APTUSDT",
        date="2026-01-06",
        timeframe="1h",
        side="low",
        direction="bearish",
        level=1.0,
        level_available_at="2026-01-06T00:00:00.000Z",
        episode_start_ts="2026-01-06T01:00:00.000Z",
        episode_start_ms=0,
        approach_50bps_ts="2026-01-06T01:00:00.000Z",
        approach_10bps_ts="2026-01-06T01:05:00.000Z",
        first_break_ts="2026-01-06T01:10:00.000Z",
        outcome=BREAK,
    )
    label, ms = choose_anchor(ep)
    assert label == "approach_10bps"
    assert ms is not None
    # anchor must be before break
    from datetime import datetime, timezone

    break_ms = int(
        datetime.fromisoformat(ep.first_break_ts.replace("Z", "+00:00")).timestamp() * 1000
    )
    assert ms < break_ms


def test_outcome_labels_documented_set():
    assert BREAK == "LEVEL_BREAK"
    assert HOLD == "LEVEL_HOLD_REJECT"


def test_mann_whitney_auc_perfect():
    scores = [0.1, 0.2, 0.8, 0.9]
    labels = [0, 0, 1, 1]
    auc = mann_whitney_auc(scores, labels)
    assert auc == 1.0


def test_feature_comparison_excludes_ambiguous():
    rows = [
        {"outcome": BREAK, "pull": 0.5},
        {"outcome": HOLD, "pull": 0.1},
        {"outcome": "AMBIGUOUS", "pull": 0.9},
    ]
    cmp_ = feature_comparison(rows, "pull")
    assert cmp_["n_break"] == 1
    assert cmp_["n_hold"] == 1


def test_matched_control_same_bucket():
    rows = [
        {
            "approach_id": "b1",
            "outcome": BREAK,
            "symbol": "APTUSDT",
            "direction": "bearish",
            "timeframe": "1h",
            "distance_to_level_bps": 10.0,
            "approach_speed_bps_per_min": 5.0,
            "short_term_vol_bps": 1.0,
            "primary_pull_feature": 0.4,
        },
        {
            "approach_id": "h1",
            "outcome": HOLD,
            "symbol": "APTUSDT",
            "direction": "bearish",
            "timeframe": "1h",
            "distance_to_level_bps": 12.0,
            "approach_speed_bps_per_min": 6.0,
            "short_term_vol_bps": 1.2,
            "primary_pull_feature": 0.1,
        },
        {
            "approach_id": "h2",
            "outcome": HOLD,
            "symbol": "DOGEUSDT",
            "direction": "bearish",
            "timeframe": "1h",
            "distance_to_level_bps": 10.0,
            "approach_speed_bps_per_min": 5.0,
            "short_term_vol_bps": 1.0,
            "primary_pull_feature": 0.0,
        },
    ]
    m = match_controls(rows)
    assert len(m) == 1
    assert m[0]["matched"] is True
    assert m[0]["hold_approach_id"] == "h1"


def test_auc_distance_plus_pull_runs():
    rows = [
        {"outcome": BREAK, "p": 0.9, "distance_to_level_bps": 5.0},
        {"outcome": BREAK, "p": 0.8, "distance_to_level_bps": 6.0},
        {"outcome": HOLD, "p": 0.1, "distance_to_level_bps": 20.0},
        {"outcome": HOLD, "p": 0.2, "distance_to_level_bps": 18.0},
        {"outcome": HOLD, "p": 0.15, "distance_to_level_bps": 22.0},
        {"outcome": BREAK, "p": 0.7, "distance_to_level_bps": 7.0},
    ]
    out = auc_distance_pull(rows, "p")
    assert out["auc_pull_only"] is not None
    assert out["auc_distance_only"] is not None
    assert out["auc_distance_plus_pull"] is not None


def test_decide_insufficient_sample():
    assert (
        decide_primary(
            n_break=2,
            n_hold=2,
            n_ambiguous=10,
            auc_pull=0.9,
            auc_dist=0.5,
            auc_combo=0.95,
            matched_pull_diff_median=0.2,
            cliffs=0.5,
            fragile=False,
            subgroup_spread=0.0,
        )
        == "PROTECTED_LEVEL_CONTROL_SAMPLE_INSUFFICIENT"
    )


def test_cluster_overlaps_marks_pair():
    a = ApproachEpisode(
        approach_id="a",
        symbol="APTUSDT",
        date="2026-01-06",
        timeframe="1h",
        side="low",
        direction="bearish",
        level=1.0,
        level_available_at="x",
        episode_start_ts="2026-01-06T01:00:00.000Z",
        episode_start_ms=1_000_000,
    )
    b = ApproachEpisode(
        approach_id="b",
        symbol="APTUSDT",
        date="2026-01-06",
        timeframe="4h",
        side="low",
        direction="bearish",
        level=1.0005,
        level_available_at="x",
        episode_start_ts="2026-01-06T01:10:00.000Z",
        episode_start_ms=1_000_000 + 10 * 60_000,
    )
    cluster_overlaps([a, b])
    assert a.overlap_cluster is not None
    assert a.overlap_cluster == b.overlap_cluster


def test_passive_excess_concept():
    """Reduction beyond matched aggression = passive removal excess."""
    zone0, zone1 = 100.0, 40.0
    flow = 20.0
    abs_red = zone0 - zone1
    excess = max(0.0, abs_red - flow)
    assert excess == 40.0
