"""Console truth + anchor_profile_context_v1 (no trading rules)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from io import StringIO
from unittest.mock import patch

from research.btc_ob_fight.anchor_profile_context import (
    ANCHOR_BETWEEN_PROFILE_EDGES,
    ANCHOR_IN_LOWER_EDGE_ZONE,
    ANCHOR_IN_UPPER_EDGE_ZONE,
    ANCHOR_INSIDE_BOTH_PROFILES,
    ANCHOR_OUTSIDE_BOTH_ABOVE,
    ANCHOR_OUTSIDE_BOTH_BELOW,
    ANCHOR_PROFILE_CONTEXT_CONTRACT,
    OBS_ALREADY_INSIDE_AT_ANCHOR,
    OBS_ALREADY_OUTSIDE_AT_ANCHOR,
    OBS_EDGE_CONTACT_AT_ANCHOR,
    PRIOR_CROSS_NOT_OBSERVED_IN_WINDOW,
    PRIOR_CROSS_OBSERVED,
    build_anchor_profile_context,
)
from research.btc_ob_fight.instrument_contract import instrument_for
from research.btc_ob_fight.profile_edge_state import set_active_symbol
from research.btc_ob_fight.reporting import _fmt_count, print_console_summary


UTC = timezone.utc


def _tpo_vol(*, vah=78910.0, val=78680.0, vvah=78945.0, vval=78645.0, poc=78852.5, vpoc=78832.5):
    return (
        {
            "tpo_profile_status": "COMPUTED_SEPARATELY",
            "tpoc": {"tpoc_price": poc},
            "value_area": {"tpoc_vah": vah, "tpoc_val": val},
        },
        {
            "volume_profile_status": "COMPUTED_SEPARATELY",
            "vpoc": {"vpoc_price": vpoc},
            "value_area": {"vvah": vvah, "vval": vval},
        },
    )


def _trade(ts: datetime, price: float, tid: str, side: str = "Buy") -> dict:
    return {
        "ts": ts,
        "price": price,
        "trade_id": tid,
        "side": side,
        "quote_notional": 100.0,
        "base_size": 0.001,
    }


def test_fmt_count_zero_empty_and_missing():
    assert _fmt_count(0) == "0"
    assert _fmt_count([]) == "0"
    assert _fmt_count(None) == "NOT_AVAILABLE"
    assert _fmt_count(None, computed=False) == "NOT_AVAILABLE"
    assert _fmt_count(3) == "3"


def test_console_uses_full_result_bundle_not_lean_summary():
    summary = {
        "anchor_timestamp_utc": "2026-08-30T16:30:00Z",
        "symbol": "BTCUSDT",
        "schema_version": "btc_ob_fight_facts_v2_0",
        "data_quality": "PASS",
        "rules_frozen": False,
        "trade_verdict_evaluated": False,
        "window": {"start_utc": "2026-08-30T16:00:00Z", "end_utc": "2026-08-30T17:00:00Z"},
        "profile_facts": {"price_at_anchor": 79001.0, "tpo_poc": 78852.5, "tpo_vah": 78910.0, "tpo_val": 78680.0},
        "tpo_profile": {"status": "COMPUTED_SEPARATELY", "bracket_minutes": 30, "full_brackets": 11, "partial_brackets": 0, "total_brackets": 11},
        "volume_profile": {"status": "COMPUTED_SEPARATELY", "vpoc": 78832.5, "vvah": 78945.0, "vval": 78645.0},
        "trade_facts": {"relative_windows": []},
        "wall_summary": {"book_samples_total": 3601},
        "oi_liquidation_facts": {"oi_delta": 0, "liquidation_count": 6, "liquidation_summary": {"long_count": 0, "short_count": 6}},
        "fight_facts": {},  # lean stub — must be ignored when full bundle passed
        "sequence_validation": {},
    }
    fight = {"manifest": {"profile_state_episode_count": 1, "outside_episode_count": 1, "edge_consumption_count": 1, "post_trade_refill_count": 0, "reclaim_count": 0}}
    seq = {
        "verdict": "BTC_OB_FIGHT_CANONICAL_ELIGIBILITY_READY",
        "fight_sequence_summary": {
            "verdict": "BTC_OB_FIGHT_CANONICAL_ELIGIBILITY_READY",
            "raw_outside_observation_count": 1,
            "canonical_outside_count": 1,
            "outside_excursion_count_ambiguous": 0,
            "canonical_reclaim_count": 0,
            "ambiguous_reclaim_candidate_count": 0,
            "edge_visits_upper": 1,
            "edge_visits_lower": 0,
            "open_excursion_count": 1,
            "cluster_count_gap_0": 1,
            "gap0_invariant_ok": True,
            "nearby_ask_count": 2,
            "nearby_bid_count": 5,
            "nearby_unknown_count": 0,
            "consumption_by_scope": {
                "TPO_EDGE_BIN": {"total": 50},
                "VOLUME_EDGE_BIN": {"total": 51},
                "PROFILE_EDGE_ZONE": {"total": 418},
            },
            "edge_observability_summary": {
                "by_edge_time_scope": {
                    "UPPER|FULL_WINDOW_AUDIT|EXACT_LEVEL_TICK": [
                        {
                            "edge": "UPPER",
                            "scope": "EXACT_LEVEL_TICK",
                            "status": "EDGE_REGION_OUTSIDE_BOOK_RANGE",
                            "full_coverage_pct": 1.58,
                            "partial_coverage_pct": 0.0,
                            "outside_book_pct": 98.42,
                            "missing_pct": 0.0,
                        }
                    ]
                }
            },
        },
    }
    buf = StringIO()
    with patch("sys.stdout", buf):
        print_console_summary(
            summary,
            "/tmp/run",
            {"input_coverage_runtime_s": 2.5, "full_analysis_runtime_s": 9.8, "total_runtime_s": 9.8},
            fight_facts=fight,
            sequence_validation=seq,
            level_events=[],
            anchor_profile_context={
                "contract_version": ANCHOR_PROFILE_CONTEXT_CONTRACT,
                "anchor_context": ANCHOR_OUTSIDE_BOTH_ABOVE,
                "observation_context": OBS_ALREADY_OUTSIDE_AT_ANCHOR,
                "edges": {"outer_upper_edge": 78945.0, "outer_lower_edge": 78645.0},
                "prior_edge_cross": {"status": PRIOR_CROSS_OBSERVED, "last_outer_cross": {"cross_ts": "2026-08-30T16:20:00Z", "cross_price": 78946.0}},
            },
        )
    text = buf.getvalue()
    assert text.count("BTC OB FIGHT FACT ANALYSIS") == 1
    assert text.count("FULL ANALYSIS RUNTIME") == 1
    assert "TOTAL RUNTIME" not in text
    assert "None" not in text
    assert "Canonical outside: 1" in text
    assert "Canonical reclaims: 0" in text
    assert "Edge visits Upper/Lower: 1/0" in text
    assert "Exact frozen-edge events: 1" in text
    assert "TPO edge-bin events: 50" in text
    assert "Volume edge-bin events: 51" in text
    assert "Profile-edge-zone events: 418" in text
    assert "ANCHOR_OUTSIDE_BOTH_ABOVE" in text
    assert "ALREADY_OUTSIDE_AT_ANCHOR" in text
    assert "PASSIVE EDGE CONTROL: NOT_EVALUATED" in text
    assert "EDGE_REGION_MOSTLY_OUTSIDE_OB200_RANGE" in text
    assert "not observable" in text


def test_console_missing_field_not_available_not_zero():
    buf = StringIO()
    with patch("sys.stdout", buf):
        print_console_summary(
            {
                "profile_facts": {},
                "tpo_profile": {},
                "volume_profile": {},
                "trade_facts": {},
                "wall_summary": {},
                "oi_liquidation_facts": {},
            },
            "/tmp/x",
            {},
            fight_facts={"manifest": {}},
            sequence_validation={"fight_sequence_summary": {}},
        )
    text = buf.getvalue()
    assert "Canonical outside: NOT_AVAILABLE" in text
    assert "Canonical outside: 0" not in text


def test_anchor_outside_both_above():
    set_active_symbol("BTCUSDT")
    tpo, vol = _tpo_vol()
    ctx = build_anchor_profile_context(
        anchor_price=79001.0,
        tpo_profile=tpo,
        volume_profile=vol,
        trades=[],
        anchor=datetime(2026, 8, 30, 16, 30, tzinfo=UTC),
        before_minutes=30,
    )
    assert ctx["anchor_context"] == ANCHOR_OUTSIDE_BOTH_ABOVE
    assert ctx["edges"]["outer_upper_edge"] == 78945.0
    assert ctx["observation_context"] in {OBS_ALREADY_OUTSIDE_AT_ANCHOR, "PRIOR_CROSS_NOT_OBSERVED"}


def test_anchor_outside_both_below():
    set_active_symbol("BTCUSDT")
    tpo, vol = _tpo_vol()
    ctx = build_anchor_profile_context(
        anchor_price=78600.0,
        tpo_profile=tpo,
        volume_profile=vol,
        trades=[],
        anchor=datetime(2026, 8, 30, 16, 30, tzinfo=UTC),
        before_minutes=30,
    )
    assert ctx["anchor_context"] == ANCHOR_OUTSIDE_BOTH_BELOW


def test_anchor_inside_both():
    set_active_symbol("BTCUSDT")
    tpo, vol = _tpo_vol()
    ctx = build_anchor_profile_context(
        anchor_price=78800.0,
        tpo_profile=tpo,
        volume_profile=vol,
        trades=[],
        anchor=datetime(2026, 8, 30, 16, 30, tzinfo=UTC),
        before_minutes=30,
    )
    assert ctx["anchor_context"] == ANCHOR_INSIDE_BOTH_PROFILES
    assert ctx["observation_context"] == OBS_ALREADY_INSIDE_AT_ANCHOR


def test_anchor_between_upper_edges():
    set_active_symbol("BTCUSDT")
    tpo, vol = _tpo_vol()
    # inner upper = min(78910, 78945)=78910; outer=78945
    ctx = build_anchor_profile_context(
        anchor_price=78920.0,
        tpo_profile=tpo,
        volume_profile=vol,
        trades=[],
        anchor=datetime(2026, 8, 30, 16, 30, tzinfo=UTC),
        before_minutes=30,
    )
    assert ctx["anchor_context"] == ANCHOR_IN_UPPER_EDGE_ZONE
    assert ctx["observation_context"] == OBS_EDGE_CONTACT_AT_ANCHOR


def test_anchor_between_lower_edges():
    set_active_symbol("BTCUSDT")
    tpo, vol = _tpo_vol()
    # inner lower = max(78680, 78645)=78680; outer=78645
    ctx = build_anchor_profile_context(
        anchor_price=78660.0,
        tpo_profile=tpo,
        volume_profile=vol,
        trades=[],
        anchor=datetime(2026, 8, 30, 16, 30, tzinfo=UTC),
        before_minutes=30,
    )
    assert ctx["anchor_context"] == ANCHOR_IN_LOWER_EDGE_ZONE


def test_tick_boundary_equality_not_outside():
    set_active_symbol("BTCUSDT")
    tpo, vol = _tpo_vol()
    # exactly outer upper tick → not ABOVE
    ctx = build_anchor_profile_context(
        anchor_price=78945.0,
        tpo_profile=tpo,
        volume_profile=vol,
        trades=[],
        anchor=datetime(2026, 8, 30, 16, 30, tzinfo=UTC),
        before_minutes=30,
    )
    assert ctx["anchor_context"] != ANCHOR_OUTSIDE_BOTH_ABOVE
    assert ctx["anchor_context"] in {ANCHOR_IN_UPPER_EDGE_ZONE, ANCHOR_BETWEEN_PROFILE_EDGES}


def test_prior_outer_cross_in_window():
    set_active_symbol("BTCUSDT")
    tpo, vol = _tpo_vol()
    anchor = datetime(2026, 8, 30, 16, 30, tzinfo=UTC)
    trades = [
        _trade(anchor - timedelta(minutes=20), 78900.0, "1"),  # inside
        _trade(anchor - timedelta(minutes=15), 78950.0, "2"),  # outward outer
        _trade(anchor - timedelta(minutes=1), 79010.0, "3"),
    ]
    ctx = build_anchor_profile_context(
        anchor_price=79001.0,
        tpo_profile=tpo,
        volume_profile=vol,
        trades=trades,
        anchor=anchor,
        before_minutes=30,
    )
    prior = ctx["prior_edge_cross"]
    assert prior["status"] == PRIOR_CROSS_OBSERVED
    assert prior["last_outer_cross"]["cross_ts"] == "2026-08-30T16:15:00Z"
    assert prior["first_outer_cross"]["cross_ts"] == "2026-08-30T16:15:00Z"
    assert prior["remained_outside_until_anchor"] is True
    assert prior["future_leakage"] is False
    # no post-anchor trades used
    assert datetime.fromisoformat(prior["last_outer_cross"]["cross_ts"].replace("Z", "+00:00")) < anchor


def test_prior_cross_not_in_window():
    set_active_symbol("BTCUSDT")
    tpo, vol = _tpo_vol()
    anchor = datetime(2026, 8, 30, 16, 30, tzinfo=UTC)
    trades = [
        _trade(anchor - timedelta(minutes=1), 79010.0, "a"),
        _trade(anchor + timedelta(minutes=1), 78800.0, "future"),  # must be ignored
    ]
    ctx = build_anchor_profile_context(
        anchor_price=79001.0,
        tpo_profile=tpo,
        volume_profile=vol,
        trades=trades,
        anchor=anchor,
        before_minutes=30,
    )
    assert ctx["prior_edge_cross"]["status"] == PRIOR_CROSS_NOT_OBSERVED_IN_WINDOW
    assert ctx["prior_edge_cross"].get("last_outer_cross") in (None, {})


def test_no_future_leakage_in_prior_cross():
    set_active_symbol("BTCUSDT")
    tpo, vol = _tpo_vol()
    anchor = datetime(2026, 8, 30, 16, 30, tzinfo=UTC)
    trades = [
        _trade(anchor - timedelta(minutes=5), 78900.0, "1"),
        _trade(anchor, 78950.0, "at_anchor"),  # exclusive end
        _trade(anchor + timedelta(seconds=1), 78950.0, "after"),
    ]
    ctx = build_anchor_profile_context(
        anchor_price=79001.0,
        tpo_profile=tpo,
        volume_profile=vol,
        trades=trades,
        anchor=anchor,
        before_minutes=30,
    )
    assert ctx["prior_edge_cross"]["status"] == PRIOR_CROSS_NOT_OBSERVED_IN_WINDOW


def test_same_timestamp_ambiguity_does_not_invent_order():
    set_active_symbol("BTCUSDT")
    tpo, vol = _tpo_vol()
    anchor = datetime(2026, 8, 30, 16, 30, tzinfo=UTC)
    ts = anchor - timedelta(minutes=10)
    trades = [
        _trade(ts - timedelta(minutes=1), 78900.0, "in"),
        _trade(ts, 78900.0, "amb_in"),
        _trade(ts, 78950.0, "amb_out"),
        _trade(ts + timedelta(minutes=1), 79000.0, "out"),
    ]
    ctx = build_anchor_profile_context(
        anchor_price=79001.0,
        tpo_profile=tpo,
        volume_profile=vol,
        trades=trades,
        anchor=anchor,
        before_minutes=30,
    )
    prior = ctx["prior_edge_cross"]
    assert prior.get("same_timestamp_ambiguity") is True
    # Must not invent a cross at the ambiguous timestamp
    if prior.get("last_outer_cross"):
        assert prior["last_outer_cross"]["cross_ts"] != "2026-08-30T16:20:00Z"


def test_consumption_scopes_not_mixed():
    buf = StringIO()
    with patch("sys.stdout", buf):
        print_console_summary(
            {"profile_facts": {}, "tpo_profile": {}, "volume_profile": {}, "trade_facts": {}, "wall_summary": {}, "oi_liquidation_facts": {}},
            "/tmp/x",
            {},
            fight_facts={"manifest": {"edge_consumption_count": 1}},
            sequence_validation={
                "fight_sequence_summary": {
                    "consumption_by_scope": {
                        "TPO_EDGE_BIN": {"total": 50},
                        "VOLUME_EDGE_BIN": {"total": 51},
                        "PROFILE_EDGE_ZONE": {"total": 418},
                    }
                }
            },
        )
    text = buf.getvalue()
    assert "Exact frozen-edge events: 1" in text
    assert "TPO edge-bin events: 50" in text
    assert "604" not in text


def test_zero_observed_not_same_as_not_observable():
    buf = StringIO()
    with patch("sys.stdout", buf):
        print_console_summary(
            {"profile_facts": {}, "tpo_profile": {}, "volume_profile": {}, "trade_facts": {}, "wall_summary": {}, "oi_liquidation_facts": {}},
            "/tmp/x",
            {},
            fight_facts={"manifest": {"post_trade_refill_count": 0}},
            sequence_validation={
                "fight_sequence_summary": {
                    "canonical_reclaim_count": 0,
                    "edge_observability_summary": {
                        "by_edge_time_scope": {
                            "UPPER|FULL_WINDOW_AUDIT|EXACT_LEVEL_TICK": [
                                {
                                    "edge": "UPPER",
                                    "scope": "EXACT_LEVEL_TICK",
                                    "status": "EDGE_REGION_OUTSIDE_BOOK_RANGE",
                                    "full_coverage_pct": 0,
                                    "partial_coverage_pct": 0,
                                    "outside_book_pct": 99,
                                    "missing_pct": 0,
                                }
                            ]
                        }
                    },
                }
            },
        )
    text = buf.getvalue()
    assert "Post-trade refills: 0" in text
    assert "Canonical reclaims: 0" in text
    assert "not observable" in text
    assert "EDGE_REGION_MOSTLY_OUTSIDE_OB200_RANGE" in text


def test_golden_1630_expected_context_from_run001_levels():
    set_active_symbol("BTCUSDT")
    tpo, vol = _tpo_vol()
    ctx = build_anchor_profile_context(
        anchor_price=79001.0,
        tpo_profile=tpo,
        volume_profile=vol,
        trades=[],
        anchor=datetime(2026, 8, 30, 16, 30, tzinfo=UTC),
        before_minutes=30,
    )
    assert ctx["levels"]["tpo_vah"] == 78910.0
    assert ctx["levels"]["volume_vvah"] == 78945.0
    assert ctx["edges"]["outer_upper_edge"] == 78945.0
    assert ctx["anchor_context"] == ANCHOR_OUTSIDE_BOTH_ABOVE
    assert ctx["anchor_price"] == 79001.0


def test_instrument_ticks_symbol_dependent():
    assert instrument_for("BTCUSDT").tick_size_f() == 0.1
    assert instrument_for("DOGEUSDT").tick_size_f() == 0.00001
