"""Regression tests for Phase 2A.3 canonical eligibility and edge observability."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from research.btc_ob_fight.contracts import FORBIDDEN_INTERPRETATION_TERMS
from research.btc_ob_fight.edge_observability import STATUS_NO_SAMPLES, build_edge_observability
from research.btc_ob_fight.edge_region_consumption import build_nearby_liquidity_increases
from research.btc_ob_fight.edge_regions import build_edge_region_catalog
from research.btc_ob_fight.fight_facts import build_fight_facts
from research.btc_ob_fight.fight_sequence import VERDICT_READY, build_sequence_validation
from research.btc_ob_fight.first_outside_bin_contract import build_first_outside_bin_contract
from research.btc_ob_fight.outside_reclaim import (
    EVENT_CANONICAL,
    RECLAIM_EVENT_CONTRACT_V3,
    build_canonical_reclaim_pipeline,
)
from research.btc_ob_fight.profile_edge_state import build_frozen_profile_edges
from research.btc_ob_fight.profile_price_bin_contract import price_in_interval
from research.btc_ob_fight.profile_state_episodes import build_profile_state_episodes
from research.btc_ob_fight.sequence_metrics import build_nearby_liquidity_metrics


def _golden_profiles() -> tuple[dict, dict]:
    step = 10.0
    tpo = {
        "tpo_profile_status": "COMPUTED_SEPARATELY",
        "provenance": {"price_increment": step},
        "tpoc": {"tpoc_price": 78545.0},
        "value_area": {"tpoc_vah": 79080.0, "tpoc_val": 78230.0},
    }
    vol = {
        "volume_profile_status": "COMPUTED_SEPARATELY",
        "provenance": {"price_increment": step},
        "vpoc": {"vpoc_price": 78565.0},
        "value_area": {"vvah": 79140.0, "vval": 78190.0},
    }
    return tpo, vol


def _trade(ts, tid, price, side="Buy", size=1.0):
    return {
        "ts": ts,
        "trade_id": tid,
        "side": side,
        "price": price,
        "size": size,
        "notional": price * size,
    }


class TestCanonicalEligibilitySplit:
    def test_invariants_raw_equals_canonical_plus_ambiguous(self):
        anchor = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
        end = anchor + timedelta(minutes=30)
        edges = build_frozen_profile_edges(*_golden_profiles())
        trades = [
            _trade(anchor + timedelta(seconds=i * 3), str(i), 78500.0 + (i % 5) * 100)
            for i in range(40)
        ]
        bundle = build_profile_state_episodes(trades, edges, anchor=anchor, window_end=end)
        pipe = build_canonical_reclaim_pipeline(
            bundle["episodes"], bundle["transitions"], trades, edges
        )
        es = pipe["canonical_eligibility_summary"]
        assert es["raw_outside_count"] == es["canonical_outside_count"] + es["ambiguous_outside_count"]
        assert es["canonical_reclaim_count"] <= es["canonical_outside_count"]
        assert len(pipe["reclaim_events"]) == es["canonical_reclaim_count"]
        assert len(pipe["ambiguous_reclaim_candidates"]) == es["ambiguous_reclaim_candidate_count"]

    def test_ambiguous_never_canonical_eligible(self):
        anchor = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
        end = anchor + timedelta(minutes=10)
        edges = build_frozen_profile_edges(*_golden_profiles())
        ts = anchor + timedelta(seconds=5)
        trades = [_trade(ts, "a", 78500.0), _trade(ts, "b", 79200.0), _trade(ts + timedelta(seconds=5), "c", 78500.0)]
        bundle = build_profile_state_episodes(trades, edges, anchor=anchor, window_end=end)
        pipe = build_canonical_reclaim_pipeline(
            bundle["episodes"], bundle["transitions"], trades, edges
        )
        for c in pipe["ambiguous_reclaim_candidates"]:
            assert c["canonical_eligible"] is False
        for r in pipe["reclaim_events"]:
            assert r["canonical_eligible"] is True
            assert r["event_status"] == EVENT_CANONICAL
            assert r["source_contract"] == RECLAIM_EVENT_CONTRACT_V3

    def test_fight_facts_v3_contract(self):
        tpo, vol = _golden_profiles()
        anchor = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
        end = anchor + timedelta(minutes=10)
        trades = [_trade(anchor, "1", 78500.0)]
        fight = build_fight_facts(
            tpo_profile=tpo,
            volume_profile=vol,
            trades=trades,
            wall_bundle={"transitions": [], "trade_matches": []},
            oi_rows=[],
            liq_rows=[],
            anchor=anchor,
            window_end=end,
        )
        assert fight["canonical_reclaim_contract"] == RECLAIM_EVENT_CONTRACT_V3
        assert fight["canonical_reclaims_only_in_primary_output"] is True


class TestFirstOutsideBin:
    def test_upper_lower_intervals_half_open(self):
        tpo, vol = _golden_profiles()
        edges = build_frozen_profile_edges(tpo, vol)
        fo = build_first_outside_bin_contract(tpo, vol, edges)
        assert fo["status"] == "COMPUTED"
        upper = fo["edges"]["UPPER"]
        lower = fo["edges"]["LOWER"]
        assert upper["price_low"] == edges["upper_outer_edge"]
        assert upper["requested_tick_count"] >= 1
        assert price_in_interval(upper["price_low"], upper["price_low"], upper["price_high"])
        assert not price_in_interval(upper["price_high"], upper["price_low"], upper["price_high"])
        assert lower["price_high"] == edges["lower_outer_edge"]


class TestNearbySideLineage:
    def test_side_from_parent_consumption(self):
        consumption = [
            {
                "consumption_event_id": "erc_test",
                "scope": "TPO_EDGE_BIN",
                "edge": "UPPER",
                "side": "ASK",
                "matching_status": "TRADE_ASSOCIATED",
                "observation_ts": "2026-08-31T19:00:01Z",
                "price_tick": 791400,
                "coverage_status": "FULL_EDGE_REGION_COVERAGE",
            }
        ]
        wall = {
            "transitions": [
                {
                    "transition_type": "QTY_INCREASE_OBSERVED",
                    "side": "ASK",
                    "price_tick": 791401,
                    "current_ts": "2026-08-31T19:00:02Z",
                    "qty_added": 1.0,
                }
            ]
        }
        nearby = build_nearby_liquidity_increases(consumption, wall, {"upper": [], "lower": []})
        assert len(nearby) == 1
        assert nearby[0]["side"] == "ASK"
        assert nearby[0]["side_source"] == "PARENT_CONSUMPTION_EVENT"

    def test_unknown_side_counted_separately(self):
        nearby = [{"side": "ASK"}, {"side": "BID"}, {"side": "UNKNOWN"}, {"side": "UNKNOWN"}]
        m = build_nearby_liquidity_metrics(nearby)
        assert m["ask_count"] == 1
        assert m["bid_count"] == 1
        assert m["unknown_count"] == 2
        assert m["ask_plus_bid_plus_unknown_equals_total"]


class TestEdgeObservability:
    def test_lower_no_visits_no_relevant_samples(self):
        tpo, vol = _golden_profiles()
        edges = build_frozen_profile_edges(tpo, vol)
        catalog = build_edge_region_catalog(tpo, vol, edges)
        visits = [{"edge_visit_id": "v1", "edge": "UPPER", "start_ts": "2026-08-31T19:00:00Z", "end_ts": "2026-08-31T19:01:00Z", "raw_episode_ids": []}]
        rows, _ = build_edge_observability([], catalog, visits, [], [], [], window_start=datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc), window_end=datetime(2026, 8, 31, 19, 30, tzinfo=timezone.utc))
        lower_visit_rows = [r for r in rows if r.get("edge") == "LOWER" and r.get("time_context") == "EDGE_VISIT_ACTIVE"]
        assert lower_visit_rows
        assert all(r["status"] == STATUS_NO_SAMPLES for r in lower_visit_rows)


class TestReportingIntegration:
    def test_sequence_json_no_trading_interpretation(self):
        tpo, vol = _golden_profiles()
        anchor = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
        end = anchor + timedelta(minutes=5)
        trades = [_trade(anchor, "1", 78500.0)]
        fight = build_fight_facts(
            tpo_profile=tpo,
            volume_profile=vol,
            trades=trades,
            wall_bundle={"transitions": [], "trade_matches": []},
            oi_rows=[],
            liq_rows=[],
            anchor=anchor,
            window_end=end,
        )
        seq = build_sequence_validation(
            tpo_profile=tpo,
            volume_profile=vol,
            fight_bundle=fight,
            wall_bundle={"transitions": [], "trade_matches": []},
            ob_rows=[],
            oi_rows=[],
            liq_rows=[],
            trades=trades,
            anchor=anchor,
            window_end=end,
        )
        blob = json.dumps(seq)
        assert "NaN" not in blob
        assert not any(t in blob for t in FORBIDDEN_INTERPRETATION_TERMS)
        sq = seq["fight_sequence_summary"]
        assert sq["canonical_reclaim_count"] <= sq["canonical_outside_count"]
