"""Regression tests for Phase 2A.2 canonical reclaim and sequence consistency."""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone

import pytest

from research.btc_ob_fight.contracts import FORBIDDEN_INTERPRETATION_TERMS
from research.btc_ob_fight.fight_cluster_sensitivity import build_fight_cluster_sensitivity
from research.btc_ob_fight.fight_facts import build_fight_facts
from research.btc_ob_fight.fight_sequence import (
    VERDICT_READY,
    build_sequence_validation,
)
from research.btc_ob_fight.outside_reclaim import (
    RECLAIM_EVENT_CONTRACT_V3,
    build_canonical_reclaim_pipeline,
    build_outside_excursions,
    run_outside_reclaim_invariant_audit,
)
from research.btc_ob_fight.profile_edge_state import (
    STATE_BETWEEN_UPPER,
    STATE_INSIDE_BOTH,
    STATE_OUTSIDE_ABOVE,
    build_frozen_profile_edges,
)
from research.btc_ob_fight.profile_state_episodes import (
    END_REASON_STATE_CHANGE,
    END_REASON_WINDOW_END,
    build_profile_state_episodes,
)
from research.btc_ob_fight.same_timestamp_audit import (
    AMBIGUOUS_MULTI_STATE,
    UNAMBIGUOUS_SINGLE_STATE,
    build_same_timestamp_ordering_audit,
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


class TestCanonicalReclaims:
    def test_v3_contract_fields(self):
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
        pipe = build_canonical_reclaim_pipeline(
            bundle["episodes"], bundle["transitions"], trades, edges
        )
        for r in pipe["reclaim_events"]:
            assert r["source_contract"] == RECLAIM_EVENT_CONTRACT_V3
            assert r["interpretation_status"] == "NOT_EVALUATED"
            assert r["reclaim_event_id"]
            assert r["outside_excursion_id"]
            assert r["edge"] in ("UPPER", "LOWER")
            assert r["cross_ts"] >= r["outside_start_ts"]

    def test_no_global_first_match(self):
        anchor = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
        end = anchor + timedelta(minutes=30)
        edges = build_frozen_profile_edges(*_golden_profiles())
        trades = []
        ts = anchor
        for i in range(8):
            price = 79200.0 if i % 2 else 78500.0
            trades.append(_trade(ts, str(i), price))
            ts += timedelta(seconds=45)
        bundle = build_profile_state_episodes(trades, edges, anchor=anchor, window_end=end)
        pipe = build_canonical_reclaim_pipeline(
            bundle["episodes"], bundle["transitions"], trades, edges
        )
        reclaims = pipe["reclaim_events"]
        if len(reclaims) > 1:
            cross_ts = {r["cross_ts"] for r in reclaims}
            assert len(cross_ts) > 1

    def test_max_one_reclaim_per_excursion(self):
        anchor = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
        end = anchor + timedelta(minutes=10)
        edges = build_frozen_profile_edges(*_golden_profiles())
        trades = [
            _trade(anchor, "1", 78500.0),
            _trade(anchor + timedelta(seconds=5), "2", 79200.0),
            _trade(anchor + timedelta(seconds=10), "3", 79100.0),
        ]
        bundle = build_profile_state_episodes(trades, edges, anchor=anchor, window_end=end)
        excursions, reclaims = build_outside_excursions(
            bundle["episodes"], bundle["transitions"], trades, edges
        )
        audit = run_outside_reclaim_invariant_audit(excursions, reclaims)
        assert audit["passed"]

    def test_window_end_not_reclaim(self):
        anchor = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
        end = anchor + timedelta(minutes=5)
        edges = build_frozen_profile_edges(*_golden_profiles())
        trades = [
            _trade(anchor, "1", 79200.0),
            _trade(anchor + timedelta(seconds=10), "2", 78500.0),
        ]
        bundle = build_profile_state_episodes(trades, edges, anchor=anchor, window_end=end)
        pipe = build_canonical_reclaim_pipeline(
            bundle["episodes"], bundle["transitions"], trades, edges
        )
        for r in pipe["reclaim_events"]:
            assert r.get("end_reason") != END_REASON_WINDOW_END

    def test_fight_facts_reclaim_csv_is_v3_only(self):
        tpo, vol = _golden_profiles()
        anchor = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
        end = anchor + timedelta(minutes=10)
        trades = [
            _trade(anchor, "1", 78500.0),
            _trade(anchor + timedelta(seconds=5), "2", 79200.0),
            _trade(anchor + timedelta(seconds=10), "3", 79100.0),
        ]
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
        assert fight["legacy_global_first_reclaim_enabled"] is False
        for r in fight["reclaim_events"]:
            assert r["source_contract"] == RECLAIM_EVENT_CONTRACT_V3
        assert "reclaim_events_corrected" not in fight


class TestSameTimestamp:
    def test_unambiguous_single_state_group(self):
        anchor = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
        end = anchor + timedelta(minutes=5)
        edges = build_frozen_profile_edges(*_golden_profiles())
        ts = anchor + timedelta(seconds=10)
        trades = [
            _trade(ts, "a", 78500.0),
            _trade(ts, "b", 78500.1),
            _trade(ts, "c", 78500.2),
        ]
        audit, rows = build_same_timestamp_ordering_audit(trades, edges, anchor=anchor, window_end=end)
        assert audit["timestamp_groups_with_multiple_trades"] >= 1
        assert all(r["ordering_quality"] == UNAMBIGUOUS_SINGLE_STATE for r in rows)

    def test_ambiguous_multi_state_group(self):
        anchor = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
        end = anchor + timedelta(minutes=5)
        edges = build_frozen_profile_edges(*_golden_profiles())
        ts = anchor + timedelta(seconds=10)
        trades = [
            _trade(ts, "a", 78500.0),
            _trade(ts, "b", 79200.0),
        ]
        audit, rows = build_same_timestamp_ordering_audit(trades, edges, anchor=anchor, window_end=end)
        assert audit["groups_with_multiple_profile_states"] >= 1
        assert any(r["ordering_quality"] == AMBIGUOUS_MULTI_STATE for r in rows)

    def test_trade_id_not_claimed_as_exchange_order(self):
        anchor = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
        end = anchor + timedelta(minutes=5)
        edges = build_frozen_profile_edges(*_golden_profiles())
        ts = anchor + timedelta(seconds=10)
        trades = [_trade(ts, "z", 78500.0), _trade(ts, "a", 79200.0)]
        audit, _ = build_same_timestamp_ordering_audit(trades, edges, anchor=anchor, window_end=end)
        assert audit["exchange_order_proven"] is False
        assert "EXCHANGE_SEQUENCE" not in audit["trade_id_semantics"].upper()


class TestVisitsAndClusters:
    def test_gap_zero_equals_visit_count(self):
        eps = [
            _episode(STATE_INSIDE_BOTH, 0, "2026-08-31T19:00:00Z", "2026-08-31T19:00:01Z"),
            _episode(STATE_BETWEEN_UPPER, 1, "2026-08-31T19:00:01Z", "2026-08-31T19:00:02Z"),
            _episode(STATE_INSIDE_BOTH, 2, "2026-08-31T19:00:02Z", "2026-08-31T19:00:03Z"),
            _episode(STATE_BETWEEN_UPPER, 3, "2026-08-31T19:00:03Z", "2026-08-31T19:00:04Z"),
            _episode(STATE_INSIDE_BOTH, 4, "2026-08-31T19:00:04Z", "2026-08-31T19:00:05Z"),
        ]
        from research.btc_ob_fight.edge_visits import build_edge_visits

        visits = build_edge_visits(eps)
        rows, _ = build_fight_cluster_sensitivity(visits, eps)
        gap0 = next(r for r in rows if r["max_inside_gap_seconds"] == 0)
        assert gap0["cluster_count"] == len(visits)

    def test_cluster_counts_monotone_non_increasing(self):
        eps = [
            _episode(STATE_INSIDE_BOTH, 0, "2026-08-31T19:00:00Z", "2026-08-31T19:00:00.5Z", dur=0.5),
            _episode(STATE_BETWEEN_UPPER, 1, "2026-08-31T19:00:00.5Z", "2026-08-31T19:00:01Z", dur=0.5),
            _episode(STATE_INSIDE_BOTH, 2, "2026-08-31T19:00:01Z", "2026-08-31T19:00:01.5Z", dur=0.5),
            _episode(STATE_BETWEEN_UPPER, 3, "2026-08-31T19:00:01.5Z", "2026-08-31T19:00:02Z", dur=0.5),
            _episode(STATE_INSIDE_BOTH, 4, "2026-08-31T19:00:02Z", "2026-08-31T19:00:02.5Z", dur=0.5),
        ]
        from research.btc_ob_fight.edge_visits import build_edge_visits

        visits = build_edge_visits(eps)
        rows, by_gap = build_fight_cluster_sensitivity(visits, eps)
        counts = [r["cluster_count"] for r in rows]
        assert counts == sorted(counts, reverse=True)
        for gap_str, payload in by_gap.items():
            cluster_ids = set()
            for c in payload["clusters"]:
                for vid in c["visit_ids"]:
                    assert vid not in cluster_ids
                    cluster_ids.add(vid)
            assert len(cluster_ids) == len(visits)


class TestReportingMetrics:
    def test_sequence_json_no_nan(self):
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
            strict_invariants=True,
        )
        blob = json.dumps(seq)
        assert "NaN" not in blob
        assert "Infinity" not in blob
        assert not any(term in blob for term in FORBIDDEN_INTERPRETATION_TERMS)
        sq = seq["fight_sequence_summary"]
        assert sq["gap0_invariant_ok"] is True
        assert sq["cluster_count_gap_0"] == sq["edge_visit_count"]
        assert sq["canonical_reclaim_contract"] == RECLAIM_EVENT_CONTRACT_V3
