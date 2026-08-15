"""Tests for subgroup validation (no CH required)."""

from __future__ import annotations

from research.orderbook.ch_break_reclaim_subgroup_validation.load import (
    assert_no_time_to_touch_feature,
    event_in_subgroup,
    normalize_timepoint,
    subgroup_name,
)
from research.orderbook.ch_break_reclaim_subgroup_validation.metrics import (
    average_rank_score,
    best_auc,
    jackknife_auc,
    mann_whitney_auc,
)


def test_subgroup_mapping():
    assert subgroup_name("APTUSDT", "bearish") == "APT_bearish"
    assert subgroup_name("DOGEUSDT", "bullish") == "DOGE_bullish"
    assert event_in_subgroup("APTUSDT", "bearish", "all_bearish")
    assert not event_in_subgroup("APTUSDT", "bullish", "all_bearish")
    assert event_in_subgroup("DOGEUSDT", "bullish", "DOGE_bullish")


def test_timepoint_alias_pre_touch_60s():
    assert normalize_timepoint("PRE_TOUCH_1M") == "PRE_TOUCH_60S"
    assert normalize_timepoint("PRE_TOUCH_30S") == "PRE_TOUCH_30S"


def test_only_data_valid_loader_filters(tmp_path):
    # minimal synthetic CSV
    feat = tmp_path / "event_features.csv"
    feat.write_text(
        "event_id,symbol,break_direction,outcome_label,timepoint,data_quality,"
        "distance_to_level_bps,support_near_depth,break_side_near_depth,"
        "flow_30s_signed_move_bps,flow_30s_price_move_bps,imbalance_0_10\n"
        "e1,APTUSDT,bearish,BREAK_ACCEPTED,PRE_TOUCH_1M,DATA_VALID,-5,100,50,1,2,0.6\n"
        "e2,APTUSDT,bearish,RECLAIM_FAST,PRE_TOUCH_1M,DATA_INVALID,-4,90,40,1,2,0.5\n"
        "e3,DOGEUSDT,bearish,EXCLUDED,PRE_TOUCH_1M,DATA_VALID,-3,80,30,1,2,0.4\n"
        "e4,DOGEUSDT,bullish,HOLD_NO_BREAK,PRE_TOUCH_30S,DATA_VALID,2,70,60,-1,3,0.55\n"
    )
    from research.orderbook.ch_break_reclaim_subgroup_validation.load import load_valid_feature_rows

    rows, events = load_valid_feature_rows(prior_dir=tmp_path)
    assert all(r["data_quality"] == "DATA_VALID" for r in rows)
    assert all(e["outcome_label"] != "EXCLUDED" for e in events)
    assert any(r["timepoint"] == "PRE_TOUCH_60S" for r in rows)
    ids = {e["event_id"] for e in events}
    assert ids == {"e1", "e4"}


def test_direction_normalization_support_frac():
    from research.orderbook.ch_break_reclaim_subgroup_validation.load import load_valid_feature_rows
    import tempfile
    from pathlib import Path

    d = Path(tempfile.mkdtemp())
    (d / "event_features.csv").write_text(
        "event_id,symbol,break_direction,outcome_label,timepoint,data_quality,"
        "distance_to_level_bps,support_near_depth,break_side_near_depth,"
        "flow_30s_signed_move_bps,flow_30s_price_move_bps,bid_depth_bps_0_5,ask_depth_bps_0_5\n"
        "e1,APTUSDT,bearish,BREAK_ACCEPTED,FIRST_TOUCH,DATA_VALID,-1,75,25,0,1,10,2\n"
        "e2,APTUSDT,bullish,BREAK_ACCEPTED,FIRST_TOUCH,DATA_VALID,1,20,80,0,1,3,9\n"
    )
    rows, _ = load_valid_feature_rows(prior_dir=d)
    by = {r["event_id"]: r for r in rows}
    assert abs(by["e1"]["support_frac_0"] - 0.75) < 1e-9
    assert abs(by["e2"]["support_frac_0"] - 0.2) < 1e-9
    assert by["e1"]["support_depth_0_5"] == 10.0  # bid for bearish
    assert by["e2"]["support_depth_0_5"] == 9.0  # ask for bullish


def test_distance_baseline_helpers_and_no_time_to_touch():
    row = {"abs_distance_to_level_bps": 5.0, "signed_distance_beyond_bps": -3.0}
    assert_no_time_to_touch_feature(row)
    # average rank score deterministic
    matrix = [[1.0, 10.0], [2.0, 5.0], [3.0, 1.0]]
    scores = average_rank_score(matrix, higher_is_break=[True, False])
    assert scores[0] is not None
    assert len(scores) == 3


def test_jackknife_deterministic():
    pairs = [
        ("a", 1.0, "BREAK_ACCEPTED"),
        ("b", 2.0, "BREAK_ACCEPTED"),
        ("c", 3.0, "BREAK_ACCEPTED"),
        ("d", 4.0, "BREAK_ACCEPTED"),
        ("e", 0.0, "RECLAIM_FAST"),
        ("f", -1.0, "RECLAIM_FAST"),
        ("g", -2.0, "RECLAIM_FAST"),
    ]
    j1 = jackknife_auc(pairs)
    j2 = jackknife_auc(pairs)
    assert j1 == j2
    assert j1["full_auc"] is not None
    assert j1["loo_auc_min"] is not None


def test_auc_orientation():
    pos = [5.0, 6.0, 7.0, 8.0]
    neg = [1.0, 2.0, 3.0]
    assert mann_whitney_auc(pos, neg) == 1.0
    auc, ori = best_auc(neg, pos)  # swapped labels intentionally via values
    # best_auc(neg_as_pos_list, pos_as_neg_list) — if we pass low as pos:
    a, o = best_auc([1, 2, 3], [5, 6, 7, 8])
    assert a == 1.0
    assert o == "higher→OTHER"
