"""Tests for frozen liquidation_flow_facts_v1 contract."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from research.btc_ob_fight.facts import json_safe
from research.btc_ob_fight.liquidation_flow_contract import (
    BYBIT_ALL_LIQUIDATION_DOCS_URL,
    LIQUIDATION_FLOW_CONTRACT,
    SUPERSEDED_EXPLANATORY_AUDIT,
    assert_canonical_input_allowed,
    map_bybit_position_side,
)
from research.btc_ob_fight.liquidation_flow_facts import (
    ATTRIBUTION_METHOD,
    build_liquidation_flow_facts,
)


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _liq(ts: str, side: str, base: float, bp: float, key: str) -> dict:
    mapped = map_bybit_position_side("Sell" if side == "short" else "Buy")
    return {
        "event_time": _ts(ts),
        "liquidated_side": mapped["liquidated_position_side"],
        "position_side_raw": mapped["position_side_raw"],
        "forced_trade_direction": mapped["forced_trade_direction"],
        "executed_base_size": base,
        "bankruptcy_price": bp,
        "bankruptcy_reference_quote": base * bp,
        "event_key": key,
        "dedup_key": key,
    }


def _trade(ts: str, tid: str, side: str, price: float, size: float) -> dict:
    return {
        "ts": _ts(ts),
        "trade_id": tid,
        "side": side,
        "price": price,
        "size": size,
        "notional": price * size,
    }


def _flow(**kwargs):
    defaults = dict(
        liq_load_meta={"raw_row_count": 0, "duplicate_event_count": 0, "dedup_key": "event_key"},
        oi_rows=[],
        window_start=_ts("2026-08-31T18:30:00Z"),
        window_end=_ts("2026-08-31T19:30:00Z"),
        anchor=_ts("2026-08-31T19:00:00Z"),
        outer_edge_price=79140.0,
        trades=[],
        liq_events=[],
    )
    defaults.update(kwargs)
    return build_liquidation_flow_facts(**defaults)


def test_bybit_raw_sell_to_liquidated_short_forced_buy():
    m = map_bybit_position_side("Sell")
    assert m["liquidated_position_side"] == "LIQUIDATED_SHORT"
    assert m["forced_trade_direction"] == "FORCED_BUY"


def test_bybit_raw_buy_to_liquidated_long_forced_sell():
    m = map_bybit_position_side("Buy")
    assert m["liquidated_position_side"] == "LIQUIDATED_LONG"
    assert m["forced_trade_direction"] == "FORCED_SELL"


def test_bybit_docs_url_in_manifest():
    flow = _flow()
    assert flow["manifest"]["bybit_documentation_url"] == BYBIT_ALL_LIQUIDATION_DOCS_URL


def test_bankruptcy_reference_quote_not_execution():
    liqs = [_liq("2026-08-31T19:00:00Z", "short", 2.0, 79000.0, "a")]
    flow = _flow(liq_events=liqs, liq_load_meta={"raw_row_count": 1, "duplicate_event_count": 0})
    s = flow["summary"]
    assert s["short_liquidation_bankruptcy_reference_quote"] == 158000.0
    assert s["execution_price"] is None
    assert s["execution_notional"] is None
    assert flow["events"][0]["execution_notional"] is None


def test_no_double_trade_volume():
    t0 = "2026-08-31T19:00:00Z"
    flow = _flow(
        liq_events=[_liq(t0, "short", 1.0, 79000.0, "l1"), _liq(t0, "short", 1.0, 79000.0, "l2")],
        trades=[_trade(t0, "t1", "Buy", 79000.0, 1.5)],
        liq_load_meta={"raw_row_count": 2, "duplicate_event_count": 0},
    )
    sens = next(r for r in flow["sensitivity"] if r["sensitivity_window_ms"] == 500)
    assert sens["double_counted_trade_volume_base"] == 0.0


def test_allocated_lte_total_and_identity():
    flow = _flow(
        trades=[_trade("2026-08-31T19:00:00Z", "b1", "Buy", 79000.0, 10.0)],
        liq_events=[_liq("2026-08-31T19:00:00Z", "short", 2.0, 79000.0, "l1")],
        liq_load_meta={"raw_row_count": 1, "duplicate_event_count": 0},
    )
    sens = flow["sensitivity"][0]
    assert sens["allocated_liquidation_base"] <= sens["total_taker_buy_base"]
    assert sens["volume_identity_check"] is True
    assert sens["total_taker_buy_base"] == pytest.approx(
        sens["allocated_liquidation_base"] + sens["remaining_unattributed_taker_buy_base"]
    )


def test_required_metric_names_present():
    flow = _flow(
        trades=[_trade("2026-08-31T19:00:00Z", "b1", "Buy", 79000.0, 5.0)],
        liq_events=[_liq("2026-08-31T19:00:00Z", "short", 1.0, 79000.0, "l1")],
        liq_load_meta={"raw_row_count": 1, "duplicate_event_count": 0},
    )
    sens = flow["sensitivity"][0]
    for key in (
        "allocated_liquidation_base",
        "total_taker_buy_base",
        "allocated_liquidation_share_of_total_taker_buy_base",
        "union_window_taker_buy_base",
        "liquidation_capacity_coverage_pct",
        "remaining_unattributed_taker_buy_base",
    ):
        assert key in sens
    forbidden = (
        "A_share_of_total_taker_buy_base_volume",
        "allocated_public_trade_base_volume",
        "estimated_liquidation_execution_notional",
    )
    for key in forbidden:
        assert key not in sens


def test_hindsight_phases_not_live_usable():
    flow = _flow(
        trades=[_trade("2026-08-31T19:08:20Z", "b1", "Buy", 79150.0, 1.0)],
        reclaim_events=[
            {
                "event_status": "CANONICAL_RECLAIM_OBSERVED",
                "cross_ts": "2026-08-31T19:10:58.515Z",
                "cross_price": 79136.0,
            }
        ],
    )
    for row in flow["phases"]:
        if row["phase_role"] == "HINDSIGHT":
            assert row["usable_for_live_signal"] is False


def test_causal_phase_can_be_live_usable():
    flow = _flow(
        trades=[
            _trade("2026-08-31T19:01:00Z", "b1", "Buy", 79000.0, 1.0),
            _trade("2026-08-31T19:08:20Z", "b2", "Buy", 79150.0, 1.0),
        ],
        reclaim_events=[
            {
                "event_status": "CANONICAL_RECLAIM_OBSERVED",
                "cross_ts": "2026-08-31T19:10:58.515Z",
                "cross_price": 79136.0,
            }
        ],
    )
    anchor_phase = next(p for p in flow["phases"] if p["phase"] == "ANCHOR_TO_OUTER_CROSS")
    assert anchor_phase["phase_role"] == "CAUSAL"
    assert anchor_phase["usable_for_live_signal"] is True


def test_superseded_explanatory_audit_marker():
    flow = _flow()
    assert flow["summary"]["superseded_explanatory_audit"]["do_not_use_for_research"] is True
    assert flow["summary"]["superseded_explanatory_audit"]["superseded_by"] == LIQUIDATION_FLOW_CONTRACT
    assert SUPERSEDED_EXPLANATORY_AUDIT["reason"] == "overlapping-window double counting and denominator mismatch"


def test_canonical_loader_blocks_superseded_path():
    with pytest.raises(ValueError, match="superseded"):
        assert_canonical_input_allowed("results/btc_ob_fight_explanatory_audit_20260831_1900_v1/REPORT.md")


def test_canonical_loader_allows_run_018():
    assert_canonical_input_allowed("results/btc_ob_fight_cases/20260831T190000Z/run_018")


def test_short_matches_buy_long_matches_sell():
    flow = _flow(
        liq_events=[
            _liq("2026-08-31T19:00:00Z", "short", 1.0, 79000.0, "s1"),
            _liq("2026-08-31T19:00:00Z", "long", 1.0, 79000.0, "l1"),
        ],
        trades=[
            _trade("2026-08-31T19:00:00Z", "b1", "Buy", 79000.0, 1.0),
            _trade("2026-08-31T19:00:00Z", "s1", "Sell", 79000.0, 1.0),
        ],
        liq_load_meta={"raw_row_count": 2, "duplicate_event_count": 0},
    )
    sides = {(a["liquidated_side"], a["trade_side"]) for a in flow["allocations"]}
    assert ("LIQUIDATED_SHORT", "Buy") in sides
    assert ("LIQUIDATED_LONG", "Sell") in sides


def test_manifest_has_fingerprints_and_git_head():
    flow = _flow(git_head="abc123")
    m = flow["manifest"]
    assert m["git_head"] == "abc123"
    assert m["input_fingerprint_sha256"]
    assert m["output_fingerprint_sha256"]
    assert m["frozen"] is True
    assert m["event_key_version"]


def test_contract_frozen_flag():
    flow = _flow()
    assert flow["summary"]["contract_frozen"] is True
    assert flow["summary"]["contract_version"] == LIQUIDATION_FLOW_CONTRACT


def test_rules_not_frozen_for_trading():
    flow = _flow()
    assert flow["summary"]["rules_frozen"] is False
    assert flow["summary"]["trade_verdict_evaluated"] is False
    assert flow["summary"]["direction"] is None


def test_json_no_nan():
    dumped = json.dumps(json_safe(_flow()))
    assert "NaN" not in dumped
    assert "Infinity" not in dumped


def test_heuristic_not_direct_id():
    flow = _flow(
        trades=[_trade("2026-08-31T19:00:00Z", "b1", "Buy", 79000.0, 1.0)],
        liq_events=[_liq("2026-08-31T19:00:00Z", "short", 1.0, 79000.0, "l1")],
        liq_load_meta={"raw_row_count": 1, "duplicate_event_count": 0},
    )
    assert flow["summary"]["liquidation_to_trade_direct_id_available"] is False
    assert flow["summary"]["attribution_method"] == ATTRIBUTION_METHOD
