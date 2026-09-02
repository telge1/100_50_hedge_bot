"""Tests for Phase 2A causal fight fact engine."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import math
import pytest

from research.btc_ob_fight.aggression_facts import (
    CALC_INSUFFICIENT,
    aggression_for_trades,
    classify_aggression_direction,
)
from research.btc_ob_fight.contracts import FORBIDDEN_INTERPRETATION_TERMS
from research.btc_ob_fight.fight_facts import build_fight_facts, INTERPRETATION_NOT_EVALUATED
from research.btc_ob_fight.level_registry import build_level_registry
from research.btc_ob_fight.profile_edge_state import (
    STATE_BETWEEN_LOWER,
    STATE_BETWEEN_UPPER,
    STATE_INSIDE_BOTH,
    STATE_INVALID,
    STATE_OUTSIDE_ABOVE,
    STATE_OUTSIDE_BELOW,
    build_frozen_profile_edges,
    classify_price_state,
    price_to_tick,
)
from research.btc_ob_fight.profile_state_episodes import build_profile_state_episodes


def _golden_profiles() -> tuple[dict, dict]:
    tpo = {
        "tpo_profile_status": "COMPUTED_SEPARATELY",
        "tpoc": {"tpoc_price": 78545.0},
        "value_area": {"tpoc_vah": 79080.0, "tpoc_val": 78230.0},
        "hvn_candidates": [{"price": 78555.0}],
        "lvn_candidates": [{"price": 79125.0}],
    }
    vol = {
        "volume_profile_status": "COMPUTED_SEPARATELY",
        "vpoc": {"vpoc_price": 78565.0},
        "value_area": {"vvah": 79140.0, "vval": 78190.0},
        "hvn_candidates": [{"price": 78575.0}],
        "lvn_candidates": [{"price": 79145.0}],
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


class TestProfileEdgeGeometry:
    def test_golden_frozen_edges(self):
        edges = build_frozen_profile_edges(*_golden_profiles())
        assert edges["profile_state"] == "VALID"
        assert edges["upper_inner_edge"] == 79080.0
        assert edges["upper_outer_edge"] == 79140.0
        assert edges["lower_inner_edge"] == 78230.0
        assert edges["lower_outer_edge"] == 78190.0

    def test_inside_both_profiles(self):
        edges = build_frozen_profile_edges(*_golden_profiles())
        cls = classify_price_state(78500.0, edges)
        assert cls["state"] == STATE_INSIDE_BOTH

    def test_between_upper_zone(self):
        edges = build_frozen_profile_edges(*_golden_profiles())
        cls = classify_price_state(79100.0, edges)
        assert cls["state"] == STATE_BETWEEN_UPPER

    def test_between_lower_zone(self):
        edges = build_frozen_profile_edges(*_golden_profiles())
        cls = classify_price_state(78200.0, edges)
        assert cls["state"] == STATE_BETWEEN_LOWER

    def test_outside_above(self):
        edges = build_frozen_profile_edges(*_golden_profiles())
        cls = classify_price_state(79200.0, edges)
        assert cls["state"] == STATE_OUTSIDE_ABOVE

    def test_outside_below(self):
        edges = build_frozen_profile_edges(*_golden_profiles())
        cls = classify_price_state(78100.0, edges)
        assert cls["state"] == STATE_OUTSIDE_BELOW

    def test_equality_on_upper_inner_edge(self):
        edges = build_frozen_profile_edges(*_golden_profiles())
        cls = classify_price_state(79080.0, edges)
        assert cls["state"] == STATE_INSIDE_BOTH

    def test_equality_on_upper_outer_edge(self):
        edges = build_frozen_profile_edges(*_golden_profiles())
        cls = classify_price_state(79140.0, edges)
        assert cls["state"] == STATE_BETWEEN_UPPER

    def test_equality_on_lower_inner_edge(self):
        edges = build_frozen_profile_edges(*_golden_profiles())
        cls = classify_price_state(78230.0, edges)
        assert cls["state"] == STATE_INSIDE_BOTH

    def test_equality_on_lower_outer_edge(self):
        edges = build_frozen_profile_edges(*_golden_profiles())
        cls = classify_price_state(78190.0, edges)
        assert cls["state"] == STATE_BETWEEN_LOWER

    def test_tick_normalization_boundary(self):
        edges = build_frozen_profile_edges(*_golden_profiles())
        tick = edges["upper_inner_edge_tick"]
        cls = classify_price_state(float(tick) * 0.1 + 79080.0 - tick * 0.1, edges)
        assert cls["price_tick"] == price_to_tick(79080.0)

    def test_invalid_non_overlapping_geometry(self):
        tpo = {
            "tpo_profile_status": "COMPUTED_SEPARATELY",
            "tpoc": {"tpoc_price": 100.0},
            "value_area": {"tpoc_vah": 110.0, "tpoc_val": 105.0},
        }
        vol = {
            "volume_profile_status": "COMPUTED_SEPARATELY",
            "vpoc": {"vpoc_price": 100.0},
            "value_area": {"vvah": 120.0, "vval": 115.0},
        }
        edges = build_frozen_profile_edges(tpo, vol)
        assert edges["profile_state"] == STATE_INVALID


class TestProfileStateEpisodes:
    def test_chronological_transitions(self):
        anchor = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
        end = anchor + timedelta(minutes=5)
        edges = build_frozen_profile_edges(*_golden_profiles())
        trades = [
            _trade(anchor, "1", 78500.0),
            _trade(anchor + timedelta(seconds=30), "2", 79200.0),
            _trade(anchor + timedelta(seconds=60), "3", 78500.0),
        ]
        bundle = build_profile_state_episodes(trades, edges, anchor=anchor, window_end=end)
        assert bundle["episode_count"] >= 2
        assert len(bundle["transitions"]) >= 1

    def test_open_episode_at_window_end(self):
        anchor = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
        end = anchor + timedelta(minutes=5)
        edges = build_frozen_profile_edges(*_golden_profiles())
        trades = [_trade(anchor, "1", 79200.0), _trade(anchor + timedelta(seconds=10), "2", 79300.0)]
        bundle = build_profile_state_episodes(trades, edges, anchor=anchor, window_end=end)
        last = bundle["episodes"][-1]
        assert last["closed"] is False
        assert last["end_reason"] == "WINDOW_END"

    def test_deterministic_episode_ids(self):
        anchor = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
        end = anchor + timedelta(minutes=2)
        edges = build_frozen_profile_edges(*_golden_profiles())
        trades = [_trade(anchor, "1", 78500.0)]
        a = build_profile_state_episodes(trades, edges, anchor=anchor, window_end=end)
        b = build_profile_state_episodes(trades, edges, anchor=anchor, window_end=end)
        assert a["episodes"][0]["episode_id"] == b["episodes"][0]["episode_id"]


class TestAggressionFacts:
    def test_buy_delta(self):
        ts = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
        trades = [
            _trade(ts, "1", 100.0, "Buy", 2.0),
            _trade(ts + timedelta(seconds=1), "2", 101.0, "Sell", 1.0),
        ]
        facts = aggression_for_trades(trades)
        assert facts["taker_delta_quote"] > 0
        assert facts["aggression"]["direction_observed"] == "NET_BUY_AGGRESSION_OBSERVED"

    def test_sell_delta(self):
        ts = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
        trades = [_trade(ts, "1", 100.0, "Sell", 5.0)]
        facts = aggression_for_trades(trades)
        assert facts["taker_delta_quote"] < 0

    def test_zero_delta_no_division_by_zero(self):
        ts = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
        trades = [_trade(ts, "1", 100.0, "Buy", 1.0), _trade(ts, "2", 100.0, "Sell", 1.0)]
        facts = aggression_for_trades(trades)
        assert facts["price_impact_per_1m_delta"] is None
        assert facts["price_impact_calculation_status"] == CALC_INSUFFICIENT

    def test_balanced_heuristic_flagged(self):
        d = classify_aggression_direction(100.0, 10000.0, balanced_frac=0.05)
        assert d["balanced_heuristic"] == "UNFROZEN_REPORTING_HEURISTIC"


class TestLevelRegistry:
    def test_decision_eligible_poc_vah_val_only(self):
        reg = build_level_registry(*_golden_profiles(), reference_price=78500.0)
        eligible = [x for x in reg["levels"] if x["decision_eligible"]]
        types = {x["level_type"] for x in eligible}
        assert "TPO_POC" in types
        assert "TPO_VAH" in types
        assert "TPO_VAL" in types
        assert "VOLUME_VPOC" in types
        hvns = [x for x in reg["levels"] if "HVN" in x["level_type"]]
        assert all(not x["decision_eligible"] for x in hvns)
        assert all(x["heuristic"] for x in hvns)


class TestFightFactsIntegration:
    def test_build_fight_facts_synthetic(self):
        anchor = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
        end = anchor + timedelta(minutes=3)
        trades = [
            _trade(anchor - timedelta(minutes=1), "p0", 78500.0),
            _trade(anchor, "1", 78500.0),
            _trade(anchor + timedelta(seconds=30), "2", 79200.0),
            _trade(anchor + timedelta(seconds=90), "3", 78500.0),
        ]
        wall_bundle = {
            "transitions": [
                {
                    "transition_type": "TRADE_ASSOCIATED_QTY_DECREASE",
                    "side": "ASK",
                    "price": 79140.0,
                    "price_tick": price_to_tick(79140.0),
                    "previous_ts": "2026-08-31T19:00:30Z",
                    "current_ts": "2026-08-31T19:00:31Z",
                    "previous_qty": 10.0,
                    "current_qty": 5.0,
                    "qty_reduced": 5.0,
                    "matching_aggressor_qty": 5.0,
                    "trades_at_level_between_samples": 1,
                }
            ],
            "trade_matches": [],
        }
        bundle = build_fight_facts(
            tpo_profile=_golden_profiles()[0],
            volume_profile=_golden_profiles()[1],
            trades=trades,
            wall_bundle=wall_bundle,
            oi_rows=[],
            liq_rows=[],
            anchor=anchor,
            window_end=end,
            reference_price=78500.0,
        )
        assert bundle["interpretation_status"] == INTERPRETATION_NOT_EVALUATED
        assert bundle["direction"] is None
        assert bundle["rules_frozen"] is False
        assert bundle["manifest"]["profile_state_episode_count"] >= 1

    def test_no_forbidden_terms_in_json_keys(self):
        anchor = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
        bundle = build_fight_facts(
            tpo_profile=_golden_profiles()[0],
            volume_profile=_golden_profiles()[1],
            trades=[_trade(anchor, "1", 78500.0)],
            wall_bundle={"transitions": [], "trade_matches": []},
            oi_rows=[],
            liq_rows=[],
            anchor=anchor,
            window_end=anchor + timedelta(minutes=1),
        )
        blob = str(bundle).upper()
        for term in FORBIDDEN_INTERPRETATION_TERMS:
            if term in ("LONG", "SHORT"):
                continue
            assert term not in blob

    def test_json_finite_values(self):
        anchor = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
        bundle = build_fight_facts(
            tpo_profile=_golden_profiles()[0],
            volume_profile=_golden_profiles()[1],
            trades=[_trade(anchor, "1", 78500.0)],
            wall_bundle={"transitions": [], "trade_matches": []},
            oi_rows=[],
            liq_rows=[],
            anchor=anchor,
            window_end=anchor + timedelta(minutes=1),
        )

        def walk(obj):
            if isinstance(obj, float):
                assert math.isfinite(obj)
            elif isinstance(obj, dict):
                for v in obj.values():
                    walk(v)
            elif isinstance(obj, list):
                for v in obj:
                    walk(v)

        walk(bundle)
