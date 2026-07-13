"""Unit tests for multi-week counterfactual helpers."""

from __future__ import annotations

import pandas as pd

from research.regime_scanner.pipeline_counterfactual import variant_config
from research.regime_scanner.pipeline_counterfactual_multiweek import (
    B3_CONFIG,
    R2_CONFIG,
    assert_gate_configs_unchanged,
    assign_week_id,
    choose_recommendation,
    classify_block_verdict,
    classify_market_phase,
    leave_one_week_out,
    map_quality_label,
    multi_variant_config,
    no_double_count,
    slice_weeks,
    weekly_stability,
)


def test_gate_configs_unchanged() -> None:
    cfg = assert_gate_configs_unchanged()
    assert cfg["b3"]["enabled"] is False
    assert cfg["r2"]["enabled"] is False
    assert B3_CONFIG.variant == "B3"
    assert R2_CONFIG.variant == "R2"


def test_m_variants_map_to_c() -> None:
    assert multi_variant_config("M0").use_b3 is False and multi_variant_config("M0").use_r2 is False
    assert multi_variant_config("M1").use_b3 is True and multi_variant_config("M1").use_r2 is False
    assert multi_variant_config("M2").use_b3 is False and multi_variant_config("M2").use_r2 is True
    assert multi_variant_config("M3").use_b3 is True and multi_variant_config("M3").use_r2 is True
    assert multi_variant_config("M3").confirm_candles_normal == 2
    assert multi_variant_config("M3").confirm_candles_elevated == 3
    assert multi_variant_config("M3").enabled is False
    # Same underlying C configs
    assert variant_config("C3").use_b3 == multi_variant_config("M3").use_b3


def test_week_slicing_no_overlap_complete_vs_incomplete() -> None:
    start = pd.Timestamp("2026-01-01", tz="UTC")
    end = pd.Timestamp("2026-01-22", tz="UTC")
    # Full coverage for first two weeks, thin third
    ts = []
    for d in pd.date_range(start, periods=14, freq="D", tz="UTC"):
        ts.extend(pd.date_range(d, periods=288, freq="5min", tz="UTC"))
    # partial last days
    ts.extend(pd.date_range("2026-01-15", periods=10, freq="5min", tz="UTC"))
    weeks = slice_weeks(ts, range_start=start, range_end=end)
    assert len(weeks) == 3
    assert weeks[0].is_complete and weeks[1].is_complete
    assert not weeks[2].is_complete
    # no overlap
    for i in range(len(weeks) - 1):
        assert weeks[i].end == weeks[i + 1].start
        assert weeks[i].end <= weeks[i + 1].start


def test_march_week_overlap_flagged_for_oos() -> None:
    start = pd.Timestamp("2026-01-01", tz="UTC")
    end = pd.Timestamp("2026-04-01", tz="UTC")
    ts = pd.date_range(start, end, freq="5min", tz="UTC")[:-1]
    weeks = slice_weeks(ts, range_start=start, range_end=end)
    marchish = [w for w in weeks if w.is_known_march_week]
    assert len(marchish) >= 1
    assert all(not w.is_out_of_sample for w in marchish)
    assert any(w.is_out_of_sample for w in weeks if w.is_complete)


def test_assign_week_unique() -> None:
    weeks = slice_weeks(
        pd.date_range("2026-01-01", periods=2016 * 2, freq="5min", tz="UTC"),
        range_start="2026-01-01",
        range_end="2026-01-15",
    )
    ids = [assign_week_id(w.start + pd.Timedelta(hours=1), weeks) for w in weeks]
    assert no_double_count(ids)
    assert assign_week_id("2026-01-03T12:00:00+00:00", weeks) == weeks[0].week_id


def test_quality_mapping_and_block_verdicts() -> None:
    assert map_quality_label("good") == "good"
    assert map_quality_label("weak") == "weak"
    assert map_quality_label("mixed") == "ambiguous"
    assert classify_block_verdict(baseline_quality="weak", blocked=True) == "TRUE_POSITIVE_BLOCK"
    assert classify_block_verdict(baseline_quality="good", blocked=True) == "FALSE_POSITIVE_BLOCK"
    assert classify_block_verdict(baseline_quality="mixed", blocked=True) == "AMBIGUOUS_BLOCK"
    assert (
        classify_block_verdict(baseline_quality="good", blocked=True, later_new_setup=True)
        == "BLOCKED_ENTRY_REPLACED_BY_NEW_SETUP"
    )


def test_market_phase_does_not_crash_and_is_posthoc() -> None:
    idx = pd.date_range("2026-01-01", periods=300, freq="5min", tz="UTC")
    # strong up
    close = pd.Series(range(300), dtype=float) / 100.0 + 1.0
    candles = pd.DataFrame(
        {
            "timestamp": idx,
            "open": close,
            "high": close + 0.01,
            "low": close - 0.01,
            "close": close,
        }
    )
    phase = classify_market_phase(candles, idx[0], idx[-1] + pd.Timedelta(minutes=5))
    assert "market_phase" in phase
    assert phase["market_phase"] != "insufficient_data"


def test_leave_one_week_out_and_stability() -> None:
    rows = [
        {
            "week_id": f"W{i}",
            "is_complete": True,
            "n_m0_entries": 10,
            "n_m3_blocks_on_m0_entries": i,
            "n_good_blocked_m3": 0 if i < 3 else 1,
            "n_weak_blocked_m3": 1,
            "n_good_allowed_m3": 5,
            "n_weak_m0": 2,
            "false_block_rate_m3": 0.1,
            "precision_m3": 0.5,
        }
        for i in range(4)
    ]
    lowo = leave_one_week_out(rows)
    assert len(lowo) == 4
    assert all(r["n_weeks_kept"] == 3 for r in lowo)
    stab = weekly_stability(rows, value_key="n_weak_blocked_m3")
    assert stab["n"] == 4
    assert stab["median"] == 1.0


def test_recommendation_e_when_m0_fails() -> None:
    rec = choose_recommendation(
        scenarios={"moderate": {"passes": True}, "permissive": {"passes": True}},
        b3_entry_blocks=5,
        r2_entry_blocks=5,
        r2_false_block_rate=0.05,
        b3_false_block_rate=0.05,
        third_candle_benefit=2.0,
        stable_without_march=True,
        m0_reproduced=False,
    )
    assert rec["decision"] == "E"
