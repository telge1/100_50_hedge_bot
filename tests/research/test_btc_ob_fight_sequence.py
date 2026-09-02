"""Tests for Phase 2A.1 fight sequence and edge region validation."""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone

import pytest

from research.btc_ob_fight.contracts import FORBIDDEN_INTERPRETATION_TERMS
from research.btc_ob_fight.edge_book_coverage import (
    COVERAGE_FULL,
    COVERAGE_MISSING,
    COVERAGE_OUTSIDE,
    build_edge_book_coverage,
)
from research.btc_ob_fight.edge_regions import (
    SCOPE_EXACT_LEVEL_TICK,
    SCOPE_PROFILE_EDGE_ZONE,
    build_edge_region_catalog,
)
from research.btc_ob_fight.edge_visits import EDGE_VISIT_CONTRACT, build_edge_visits
from research.btc_ob_fight.fight_cluster_sensitivity import SENSITIVITY_LABEL, build_fight_cluster_sensitivity
from research.btc_ob_fight.fight_facts import build_fight_facts
from research.btc_ob_fight.fight_sequence import build_sequence_validation, VERDICT_READY
from research.btc_ob_fight.outside_reclaim import (
    assert_invariants_or_raise,
    build_outside_excursions,
    run_outside_reclaim_invariant_audit,
)
from research.btc_ob_fight.preflight_audit import build_preflight_audit
from research.btc_ob_fight.profile_edge_state import (
    STATE_BETWEEN_UPPER,
    STATE_INSIDE_BOTH,
    STATE_OUTSIDE_ABOVE,
    build_frozen_profile_edges,
    price_to_tick,
)
from research.btc_ob_fight.profile_price_bin_contract import (
    INTERVAL_SEMANTICS,
    PROFILE_PRICE_BIN_CONTRACT,
    build_profile_price_bin_contract,
    level_bin_for_vah_val,
    price_in_interval,
    price_to_bin_index,
)
from research.btc_ob_fight.profile_state_episodes import (
    END_REASON_STATE_CHANGE,
    END_REASON_WINDOW_END,
    build_profile_state_episodes,
)


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


def _episode(state, idx, start, end, dur=1.0, closed=True, reason=END_REASON_STATE_CHANGE):
    return {
        "episode_id": f"pstate_{idx:04d}_{state.lower()}",
        "episode_index": idx,
        "state": state,
        "start_ts": start,
        "end_ts": end,
        "duration_seconds": dur,
        "closed": closed,
        "end_reason": reason,
        "start_price": 79000.0,
        "end_price": 79000.0,
        "min_price": 78990.0,
        "max_price": 79150.0,
        "trade_count": 1,
        "base_volume": 1.0,
        "quote_notional": 79000.0,
        "taker_buy_quote": 79000.0,
        "taker_sell_quote": 0.0,
        "taker_delta_quote": 79000.0,
        "upper_inner_edge": 79080.0,
        "upper_outer_edge": 79140.0,
        "lower_inner_edge": 78230.0,
        "lower_outer_edge": 78190.0,
    }


class TestProfilePriceBinContract:
    def test_level_representation_from_code(self):
        step = 10.0
        vah = level_bin_for_vah_val(79080.0, step, kind="VAH")
        val = level_bin_for_vah_val(78230.0, step, kind="VAL")
        assert vah["price_high"] == 79080.0
        assert vah["price_low"] == 79070.0
        assert val["price_low"] == 78230.0
        assert val["price_high"] == 78240.0

    def test_interval_semantics_half_open(self):
        step = 10.0
        lo, hi = 79070.0, 79080.0
        assert price_in_interval(79070.0, lo, hi)
        assert price_in_interval(79079.9, lo, hi)
        assert not price_in_interval(79080.0, lo, hi)

    def test_orderbook_tick_maps_to_bin(self):
        step = 10.0
        tick_price = 79075.0
        idx = price_to_bin_index(tick_price, step)
        assert idx == price_to_bin_index(79070.0, step)

    def test_contract_json_fields(self):
        tpo, vol = _golden_profiles()
        edges = build_frozen_profile_edges(tpo, vol)
        c = build_profile_price_bin_contract(tpo, vol, edges=edges)
        assert c["contract_version"] == PROFILE_PRICE_BIN_CONTRACT
        assert c["interval_semantics"] == INTERVAL_SEMANTICS
        assert c["orderbook_tick_size"] == 0.1
        assert c["level_representation"]["vah"] == "UPPER_EDGE"

    def test_upper_edge_zone_spans_tpo_vah_to_volume_vvah(self):
        tpo, vol = _golden_profiles()
        edges = build_frozen_profile_edges(tpo, vol)
        c = build_profile_price_bin_contract(tpo, vol, edges=edges)
        zone = c["upper_profile_edge_zone"]
        assert zone["price_low"] == 79070.0
        assert zone["price_high"] == 79140.0


class TestPreflightAudit:
    def test_duration_sum_matches_window(self):
        anchor = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
        end = anchor + timedelta(minutes=30)
        edges = build_frozen_profile_edges(*_golden_profiles())
        trades = [
            _trade(anchor + timedelta(seconds=i * 18), str(i), 78500.0 + i * 0.01) for i in range(100)
        ]
        bundle = build_profile_state_episodes(trades, edges, anchor=anchor, window_end=end)
        audit, _ = build_preflight_audit(bundle)
        assert audit["duration_sum_matches_trade_span"] is True

    def test_no_window_end_reclaim_in_corrected_layer(self):
        anchor = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
        end = anchor + timedelta(minutes=5)
        edges = build_frozen_profile_edges(*_golden_profiles())
        trades = [
            _trade(anchor, "1", 79200.0),
            _trade(anchor + timedelta(seconds=10), "2", 78500.0),
        ]
        bundle = build_profile_state_episodes(trades, edges, anchor=anchor, window_end=end)
        excursions, reclaims = build_outside_excursions(
            bundle["episodes"], bundle["transitions"], trades, edges
        )
        for r in reclaims:
            assert r.get("to_profile_state") != END_REASON_WINDOW_END


class TestEdgeVisits:
    def test_simple_between_visit(self):
        eps = [
            _episode(STATE_INSIDE_BOTH, 0, "2026-08-31T19:00:00Z", "2026-08-31T19:00:01Z"),
            _episode(STATE_BETWEEN_UPPER, 1, "2026-08-31T19:00:01Z", "2026-08-31T19:00:02Z"),
            _episode(STATE_INSIDE_BOTH, 2, "2026-08-31T19:00:02Z", "2026-08-31T19:00:03Z"),
        ]
        visits = build_edge_visits(eps)
        assert len(visits) == 1
        assert visits[0]["edge"] == "UPPER"
        assert visits[0]["raw_episode_count"] == 1

    def test_outside_and_reclaim_in_visit(self):
        eps = [
            _episode(STATE_INSIDE_BOTH, 0, "2026-08-31T19:00:00Z", "2026-08-31T19:00:01Z"),
            _episode(STATE_OUTSIDE_ABOVE, 1, "2026-08-31T19:00:01Z", "2026-08-31T19:00:02Z"),
            _episode(STATE_BETWEEN_UPPER, 2, "2026-08-31T19:00:02Z", "2026-08-31T19:00:03Z"),
            _episode(STATE_INSIDE_BOTH, 3, "2026-08-31T19:00:03Z", "2026-08-31T19:00:04Z"),
        ]
        visits = build_edge_visits(eps)
        assert len(visits) == 1
        assert visits[0]["outside_excursion_count"] == 1

    def test_direct_inside_to_outside(self):
        eps = [
            _episode(STATE_INSIDE_BOTH, 0, "2026-08-31T19:00:00Z", "2026-08-31T19:00:01Z"),
            _episode(STATE_OUTSIDE_ABOVE, 1, "2026-08-31T19:00:01Z", "2026-08-31T19:00:02Z"),
            _episode(STATE_INSIDE_BOTH, 2, "2026-08-31T19:00:02Z", "2026-08-31T19:00:03Z"),
        ]
        visits = build_edge_visits(eps)
        assert len(visits) == 1
        assert visits[0]["outside_excursion_count"] == 1

    def test_open_visit_at_window_end(self):
        eps = [
            _episode(STATE_INSIDE_BOTH, 0, "2026-08-31T19:00:00Z", "2026-08-31T19:00:01Z"),
            _episode(
                STATE_BETWEEN_UPPER,
                1,
                "2026-08-31T19:00:01Z",
                "2026-08-31T19:00:02Z",
                closed=False,
                reason=END_REASON_WINDOW_END,
            ),
        ]
        visits = build_edge_visits(eps)
        assert visits[0]["closed"] is False
        assert visits[0]["end_reason"] == END_REASON_WINDOW_END


class TestOutsideReclaim:
    def test_max_one_reclaim_per_excursion(self):
        anchor = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
        end = anchor + timedelta(minutes=10)
        edges = build_frozen_profile_edges(*_golden_profiles())
        trades = [
            _trade(anchor, "1", 78500.0),
            _trade(anchor + timedelta(seconds=5), "2", 79200.0),
            _trade(anchor + timedelta(seconds=10), "3", 79100.0),
            _trade(anchor + timedelta(seconds=15), "4", 78500.0),
        ]
        bundle = build_profile_state_episodes(trades, edges, anchor=anchor, window_end=end)
        excursions, reclaims = build_outside_excursions(
            bundle["episodes"], bundle["transitions"], trades, edges
        )
        audit = run_outside_reclaim_invariant_audit(excursions, reclaims)
        assert_invariants_or_raise(audit)

    def test_chronological_reclaim_not_global_first(self):
        anchor = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
        end = anchor + timedelta(minutes=20)
        edges = build_frozen_profile_edges(*_golden_profiles())
        trades = []
        ts = anchor
        for i in range(6):
            price = 79200.0 if i % 2 else 78500.0
            trades.append(_trade(ts, str(i), price))
            ts += timedelta(seconds=30)
        bundle = build_profile_state_episodes(trades, edges, anchor=anchor, window_end=end)
        _, reclaims = build_outside_excursions(bundle["episodes"], bundle["transitions"], trades, edges)
        cross_ts = {r["cross_ts"] for r in reclaims}
        if len(reclaims) > 1:
            assert len(cross_ts) > 1


class TestClusterSensitivity:
    def test_gap_zero_preserves_visit_count(self):
        eps = [
            _episode(STATE_INSIDE_BOTH, 0, "2026-08-31T19:00:00Z", "2026-08-31T19:00:01Z"),
            _episode(STATE_BETWEEN_UPPER, 1, "2026-08-31T19:00:01Z", "2026-08-31T19:00:02Z"),
            _episode(STATE_INSIDE_BOTH, 2, "2026-08-31T19:00:02Z", "2026-08-31T19:00:03Z"),
            _episode(STATE_BETWEEN_UPPER, 3, "2026-08-31T19:00:03Z", "2026-08-31T19:00:04Z"),
            _episode(STATE_INSIDE_BOTH, 4, "2026-08-31T19:00:04Z", "2026-08-31T19:00:05Z"),
        ]
        visits = build_edge_visits(eps)
        rows, _ = build_fight_cluster_sensitivity(visits, eps)
        gap0 = next(r for r in rows if r["max_inside_gap_seconds"] == 0)
        assert gap0["cluster_count"] == len(visits)
        assert all(r["sensitivity_status"] == SENSITIVITY_LABEL for r in rows)

    def test_larger_gap_never_increases_clusters(self):
        eps = [
            _episode(STATE_INSIDE_BOTH, 0, "2026-08-31T19:00:00Z", "2026-08-31T19:00:00.5Z", dur=0.5),
            _episode(STATE_BETWEEN_UPPER, 1, "2026-08-31T19:00:00.5Z", "2026-08-31T19:00:01Z", dur=0.5),
            _episode(STATE_INSIDE_BOTH, 2, "2026-08-31T19:00:01Z", "2026-08-31T19:00:01.5Z", dur=0.5),
            _episode(STATE_BETWEEN_UPPER, 3, "2026-08-31T19:00:01.5Z", "2026-08-31T19:00:02Z", dur=0.5),
            _episode(STATE_INSIDE_BOTH, 4, "2026-08-31T19:00:02Z", "2026-08-31T19:00:02.5Z", dur=0.5),
        ]
        visits = build_edge_visits(eps)
        rows, _ = build_fight_cluster_sensitivity(visits, eps)
        counts = [r["cluster_count"] for r in rows]
        assert counts == sorted(counts, reverse=True)


class TestOBCoverage:
    def test_full_coverage_when_region_in_book(self):
        tpo, vol = _golden_profiles()
        edges = build_frozen_profile_edges(tpo, vol)
        catalog = build_edge_region_catalog(tpo, vol, edges)
        reg = catalog["upper"][0]
        ob_rows = [
            {
                "ok": True,
                "ts": "2026-08-31T19:00:00Z",
                "best_bid": 78900.0,
                "best_ask": 78901.0,
                "mid": 78900.5,
                "bids": [(78900.0 - i * 0.1, 1.0) for i in range(200)],
                "asks": [(78901.0 + i * 0.1, 1.0) for i in range(200)],
            }
        ]
        rows, _, _ = build_edge_book_coverage(ob_rows, catalog)
        statuses = {r["coverage_status"] for r in rows if r.get("scope") == reg["scope"]}
        assert COVERAGE_FULL in statuses or COVERAGE_OUTSIDE in statuses

    def test_missing_sample_status(self):
        tpo, vol = _golden_profiles()
        edges = build_frozen_profile_edges(tpo, vol)
        catalog = build_edge_region_catalog(tpo, vol, edges)
        rows, _, _ = build_edge_book_coverage([{"ok": False, "ts": "2026-08-31T19:00:00Z"}], catalog)
        assert any(r["coverage_status"] == COVERAGE_MISSING for r in rows)


class TestSequenceValidationIntegration:
    def test_build_sequence_validation_minimal(self):
        tpo, vol = _golden_profiles()
        anchor = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
        end = anchor + timedelta(minutes=5)
        trades = [_trade(anchor, "1", 78500.0)]
        edges = build_frozen_profile_edges(tpo, vol)
        bundle = build_profile_state_episodes(trades, edges, anchor=anchor, window_end=end)
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
        assert seq["interpretation_status"] == "NOT_EVALUATED"
        assert seq["rules_frozen"] is False
        blob = json.dumps(seq)
        assert "NaN" not in blob
        assert not any(term in blob for term in FORBIDDEN_INTERPRETATION_TERMS)

    def test_edge_visit_contract_version(self):
        eps = [
            _episode(STATE_INSIDE_BOTH, 0, "2026-08-31T19:00:00Z", "2026-08-31T19:00:01Z"),
            _episode(STATE_BETWEEN_UPPER, 1, "2026-08-31T19:00:01Z", "2026-08-31T19:00:02Z"),
            _episode(STATE_INSIDE_BOTH, 2, "2026-08-31T19:00:02Z", "2026-08-31T19:00:03Z"),
        ]
        visits = build_edge_visits(eps)
        assert visits[0]["contract_version"] == EDGE_VISIT_CONTRACT
        assert visits[0]["interpretation_status"] == "NOT_EVALUATED"
