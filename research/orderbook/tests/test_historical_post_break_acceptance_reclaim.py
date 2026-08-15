"""Tests for post-break acceptance vs reclaim audit."""

from __future__ import annotations

import pytest

from research.orderbook.historical_post_break_acceptance_reclaim.extract import (
    assert_causal_ob,
    assert_causal_trades,
)
from research.orderbook.historical_post_break_acceptance_reclaim.outcomes import (
    distance_beyond_bps,
    is_beyond,
    label_from_post_path,
    map_event_outcome,
)
from research.orderbook.historical_post_break_acceptance_reclaim.stats import (
    decide_primary,
    mann_whitney_auc,
)


class _T:
    def __init__(self, ts_ms: int):
        self.ts_ms = ts_ms


def test_first_break_anchor_distance_mirroring():
    # bearish: below level → positive beyond
    assert distance_beyond_bps(mid=99.0, level=100.0, direction="bearish") > 0
    # bullish: above level → positive beyond
    assert distance_beyond_bps(mid=101.0, level=100.0, direction="bullish") > 0
    assert distance_beyond_bps(mid=101.0, level=100.0, direction="bearish") < 0


def test_bbo_beyond_mirror():
    assert is_beyond(best_bid=99.0, best_ask=99.1, level=100.0, direction="bearish")
    assert not is_beyond(best_bid=100.5, best_ask=100.6, level=100.0, direction="bearish")
    assert is_beyond(best_bid=100.5, best_ask=100.6, level=100.0, direction="bullish")
    assert not is_beyond(best_bid=99.0, best_ask=99.1, level=100.0, direction="bullish")


def test_causal_ob_cutoff_raises():
    with pytest.raises(AssertionError):
        assert_causal_ob([{"ts_ms": 100}, {"ts_ms": 200}], cutoff_ms=150)


def test_causal_trade_cutoff_raises():
    with pytest.raises(AssertionError):
        assert_causal_trades([_T(100), _T(200)], cutoff_ms=150)


def test_no_future_reclaim_in_label_path_vs_features():
    """Outcome may see reclaim; feature cutoff isolation is separate."""
    break_ms = 1_000_000
    samples = []
    for i, beyond in enumerate([True, True, True, False, False]):
        ts = break_ms + i * 5_000
        mid = 99.0 if beyond else 100.5
        samples.append(
            {
                "ts_ms": ts,
                "mid": mid,
                "best_bid": mid - 0.01,
                "best_ask": mid + 0.01,
            }
        )
    lab = label_from_post_path(samples, break_ms=break_ms, level=100.0, direction="bearish")
    assert lab["outcome"] == "RECLAIM"
    assert lab["uses_future_info"] is True
    # Features at +5s must not include reclaim time as input — reclaim at +15s
    assert lab["first_reclaim_ts_ms"] > break_ms + 5_000


def test_map_prefers_legacy_mechanism():
    path = {"outcome": "BREAK_ACCEPTED", "outcome_reason": "x", "first_reclaim_ts_ms": None}
    m = map_event_outcome(ob_classification="REFILL_THEN_RECLAIM", path_label=path)
    assert m["outcome"] == "RECLAIM"
    assert m["outcome_source"] == "legacy_ob_classification"


def test_volume_beyond_concept():
    level = 100.0
    direction = "bearish"
    prices = [99.5, 100.5, 98.0]
    beyond = [p for p in prices if (p < level if direction == "bearish" else p > level)]
    assert beyond == [99.5, 98.0]


def test_flip_depth_ratio_concept():
    defensive, blocking = 10.0, 40.0
    ratio = blocking / defensive
    assert ratio == 4.0


def test_refill_concept():
    def0, def_t = 10.0, 25.0
    gross = max(0.0, def_t - def0)
    assert gross == 15.0


def test_auc_and_sample_gate():
    assert mann_whitney_auc([0.1, 0.2, 0.9, 0.8], [0, 0, 1, 1]) == 1.0
    assert (
        decide_primary(
            n_accepted=2,
            n_reclaim=2,
            earliest="BREAK_PLUS_5S",
            dist_control_primary={"auc_distance_only": 0.9, "auc_distance_plus_ob_flow": 0.95},
            subgroup_spread=0.0,
            best_price_auc=0.9,
            best_ob_auc=0.5,
            best_flow_auc=0.5,
        )
        == "SAMPLE_INSUFFICIENT"
    )


def test_cutoff_isolation_list():
    path = [{"ts_ms": t} for t in (1000, 2000, 3000, 8000)]
    cutoff = 5000
    causal = [s for s in path if s["ts_ms"] <= cutoff]
    assert all(s["ts_ms"] <= cutoff for s in causal)
    assert_causal_ob(causal, cutoff_ms=cutoff)
