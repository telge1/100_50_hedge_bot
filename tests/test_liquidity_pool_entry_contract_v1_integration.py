"""Integration tests for room-gate entry contract in CASE audit pipeline."""

from __future__ import annotations

import csv
import inspect
import json
from pathlib import Path

import pytest
import yaml

from orderbook_analyse.case_03_frozen_bid_pool_causal_reaction_audit_v1.entry_contract import (
    geom_rows_to_pool_candidates,
    prefix_room_gate_parity,
    resolve_mechanical_decision,
)
from orderbook_analyse.liquidity_pool_min_target_distance_config_v1.config import (
    RoomGateConfigError,
    load_effective_room_config,
    repo_root_from,
)
from orderbook_analyse.liquidity_pool_min_target_distance_config_v1.gate import (
    PoolCandidate,
    evaluate_room_to_target_gate,
)

REPO = repo_root_from()


def _effective(min_pct: float | None = None):
    if min_pct is None:
        return load_effective_room_config(REPO)
    yaml_path = REPO / "strategies/strategy_lab/liquidity_pool_market_response_strategy_v0.yaml"
    doc = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    doc["room_to_target"]["min_target_distance_pct"] = min_pct
    tmp = REPO / "results" / "liquidity_pool_entry_contract_freeze_v1" / "_test_effective.yaml"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(yaml.dump(doc, sort_keys=False), encoding="utf-8")
    return load_effective_room_config(REPO, yaml_path=tmp)


def _pool(side: str, lower: float, upper: float, pool_id: str = "p1", tf: str = "5m") -> PoolCandidate:
    return PoolCandidate(
        pool_id=pool_id,
        source_timeframe=tf,
        side=side,
        lower_edge=lower,
        upper_edge=upper,
        available_at="2026-08-25T01:00:00Z",
        active_as_of=True,
    )


def _decide(**kwargs):
    defaults = dict(
        seen_inside=True,
        arrival_ms=1,
        long_ok=False,
        short_ok=False,
        short_contested=False,
        long_entry=None,
        short_entry=None,
        long_first_ts=None,
        short_first_ts=None,
        sell_eff=0,
        buy_rec=0,
        two=0,
        pools=[],
        effective=_effective(),
    )
    defaults.update(kwargs)
    return resolve_mechanical_decision(**defaults)


def test_micro_pass_room_pass_entry_eligible():
    pools = [_pool("BID", 98.0, 99.5)]
    d = _decide(
        short_ok=True,
        short_entry=100.0,
        short_first_ts="2026-08-25T01:05:00Z",
        sell_eff=5,
        pools=pools,
    )
    assert d.microstructure_gate_passed is True
    assert d.room_gate["gate_passed"] is True
    assert d.mechanical_trade_verdict == "TRADE_SHORT_CANDIDATE"


def test_micro_pass_room_fail_no_trade():
    pools = [_pool("ASK", 100.2, 100.5)]
    d = _decide(
        long_ok=True,
        long_entry=100.0,
        long_first_ts="2026-08-25T01:05:00Z",
        buy_rec=2,
        pools=pools,
    )
    assert d.microstructure_gate_passed is True
    assert d.room_gate["gate_passed"] is False
    assert d.mechanical_trade_verdict == "NO_TRADE"


def test_contest_room_pass_still_no_trade():
    pools = [_pool("BID", 98.0, 99.5)]
    d = _decide(
        short_contested=True,
        short_entry=100.0,
        short_first_ts="2026-08-25T01:05:00Z",
        pools=pools,
    )
    assert d.short_branch.room_gate["gate_passed"] is True
    assert d.mechanical_trade_verdict == "NO_TRADE"
    assert d.mechanical_verdict == "AMBIGUOUS_POOL_CONTEST_NO_TRADE"


def test_contest_room_fail_no_trade():
    pools = [_pool("BID", 99.8, 99.95)]
    d = _decide(
        short_contested=True,
        short_entry=100.0,
        short_first_ts="2026-08-25T01:05:00Z",
        pools=pools,
    )
    assert d.short_branch.room_gate["gate_passed"] is False
    assert d.mechanical_trade_verdict == "NO_TRADE"


def test_long_nearest_ask_lower_edge():
    pools = [
        _pool("ASK", 100.8, 101.0, pool_id="near"),
        _pool("ASK", 102.0, 103.0, pool_id="far"),
    ]
    d = _decide(long_ok=True, long_entry=100.0, long_first_ts="2026-08-25T01:05:00Z", buy_rec=1, pools=pools)
    assert d.long_branch.room_gate["target_pool_id"] == "near"
    assert d.long_branch.room_gate["target_edge"] == "lower"


def test_short_nearest_bid_upper_edge():
    pools = [
        _pool("BID", 97.0, 99.2, pool_id="near"),
        _pool("BID", 95.0, 96.0, pool_id="far"),
    ]
    d = _decide(
        short_ok=True,
        short_entry=100.0,
        short_first_ts="2026-08-25T01:05:00Z",
        sell_eff=2,
        pools=pools,
    )
    assert d.short_branch.room_gate["target_pool_id"] == "near"
    assert d.short_branch.room_gate["target_edge"] == "upper"


def test_future_target_pool_blocked():
    pools = [
        PoolCandidate(
            pool_id="future",
            source_timeframe="5m",
            side="ASK",
            lower_edge=100.8,
            upper_edge=101.0,
            available_at="2026-08-25T02:00:00Z",
            active_as_of=False,
        )
    ]
    d = _decide(long_ok=True, long_entry=100.0, long_first_ts="2026-08-25T01:05:00Z", buy_rec=1, pools=pools)
    assert d.long_branch.room_gate["gate_reason"] == "TARGET_NOT_CAUSALLY_AVAILABLE"
    assert d.mechanical_trade_verdict == "NO_TRADE"


def test_closer_pool_not_skipped():
    pools = [
        _pool("ASK", 100.6, 101.0, pool_id="first"),
        _pool("ASK", 101.0, 102.0, pool_id="second"),
    ]
    d = _decide(long_ok=True, long_entry=100.0, long_first_ts="2026-08-25T01:05:00Z", buy_rec=1, pools=pools)
    assert d.long_branch.room_gate["target_pool_id"] == "first"


def test_htf_overlap_blocks():
    pools = [
        _pool("ASK", 99.5, 100.5, pool_id="htf", tf="15m"),
        _pool("ASK", 100.8, 101.2, pool_id="tgt"),
    ]
    d = _decide(long_ok=True, long_entry=100.0, long_first_ts="2026-08-25T01:05:00Z", buy_rec=1, pools=pools)
    assert d.long_branch.room_gate["gate_reason"] == "HTF_OPPOSING_POOL_OVERLAP"
    assert d.mechanical_trade_verdict == "NO_TRADE"


def test_missing_target_blocks():
    d = _decide(long_ok=True, long_entry=100.0, long_first_ts="2026-08-25T01:05:00Z", buy_rec=1, pools=[])
    assert d.long_branch.room_gate["gate_reason"] == "TARGET_NOT_OBSERVED"


def test_invalid_config_fail_closed(tmp_path: Path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("room_to_target:\n  enabled: true\n  min_target_distance_pct: -1\n", encoding="utf-8")
    with pytest.raises(RoomGateConfigError):
        load_effective_room_config(REPO, yaml_path=bad)


def test_yaml_value_used_in_decision():
    eff = _effective(min_pct=0.75)
    pools = [_pool("ASK", 100.6, 101.0)]
    d = _decide(
        long_ok=True,
        long_entry=100.0,
        long_first_ts="2026-08-25T01:05:00Z",
        buy_rec=1,
        pools=pools,
        effective=eff,
    )
    assert eff.room.min_target_distance_pct == 0.75
    assert d.long_branch.room_gate["min_required_distance_pct"] == 0.75
    assert d.mechanical_trade_verdict == "NO_TRADE"


def test_no_hardcoded_threshold_in_entry_contract():
    src = inspect.getsource(resolve_mechanical_decision)
    assert "0.5" not in src
    assert "50.0" not in src


def test_prefix_uses_identical_config_sha():
    pools = [_pool("BID", 98.0, 99.5)]
    d = _decide(
        short_ok=True,
        short_entry=100.0,
        short_first_ts="2026-08-25T01:05:00Z",
        sell_eff=3,
        pools=pools,
    )
    eff = _effective()
    parity = prefix_room_gate_parity(decision=d, pools=pools, effective=eff)
    assert parity["prefix_status"] == "EXACT_PREFIX_PARITY"
    assert parity["room_gate_config_sha256"] == eff.config_sha256


def _regression_from_stored(case_dirname: str):
    audit = REPO / "results" / case_dirname
    geom = audit / "causal_pool_geometry.csv"
    mech = audit / "mechanical_verdict_pre_unblind.json"
    if not geom.exists() or not mech.exists():
        pytest.skip(f"{case_dirname} artefacts missing")
    pools = geom_rows_to_pool_candidates(
        list(csv.DictReader(geom.open(encoding="utf-8")))
    )
    m = json.loads(mech.read_text(encoding="utf-8"))
    eff = load_effective_room_config(REPO)
    d = _decide(
        seen_inside=True,
        arrival_ms=1,
        long_ok=m["long_branch"]["eligible"],
        short_ok=m["short_branch"]["eligible"],
        short_contested=m["short_branch"].get("contested", False),
        long_entry=m["long_branch"].get("entry_price"),
        short_entry=m["short_branch"].get("entry_price"),
        long_first_ts=m["long_branch"].get("first_available_ts"),
        short_first_ts=m["short_branch"].get("first_available_ts"),
        sell_eff=m["aggressor_counts"]["sell_effective"],
        buy_rec=m["aggressor_counts"]["buy_counter_reclaim"],
        two=m["aggressor_counts"]["two_sided"],
        pools=pools,
        effective=eff,
    )
    return m, d


def test_case_03_regression():
    stored, d = _regression_from_stored("case_03_frozen_bid_pool_causal_reaction_audit_v1")
    assert stored["mechanical_verdict"] == "AMBIGUOUS_POOL_CONTEST_NO_TRADE"
    assert stored["mechanical_trade_verdict"] == "NO_TRADE"
    # Room gate recomputed from stored branch inputs (not legacy insufficient_room fields)
    assert d.long_branch.room_gate["gate_passed"] is False
    assert d.short_branch.room_gate["gate_passed"] is True
    assert d.mechanical_trade_verdict == "NO_TRADE"


def test_case_04_regression():
    stored, d = _regression_from_stored("case_04_frozen_bid_pool_causal_reaction_audit_v1")
    assert stored["mechanical_verdict"] == "AMBIGUOUS_POOL_CONTEST_NO_TRADE"
    assert stored["mechanical_trade_verdict"] == "NO_TRADE"
    assert d.long_branch.room_gate["gate_passed"] is False
    assert d.short_branch.room_gate["gate_passed"] is False
    assert d.mechanical_trade_verdict == "NO_TRADE"


def test_outcome_access_after_pre_unblind_only():
    for dirname in (
        "case_03_frozen_bid_pool_causal_reaction_audit_v1",
        "case_04_frozen_bid_pool_causal_reaction_audit_v1",
    ):
        blind = REPO / "results" / dirname / "outcome_blindness_audit.json"
        if not blind.exists():
            pytest.skip("blindness audit missing")
        b = json.loads(blind.read_text(encoding="utf-8"))
        assert b.get("outcome_read_before_mechanical_persist") is False
        assert b.get("mechanical_payload_sha256")


def test_pipeline_imports_entry_contract():
    from orderbook_analyse.case_03_frozen_bid_pool_causal_reaction_audit_v1 import pipeline

    assert hasattr(pipeline, "run_audit")
    src = inspect.getsource(pipeline.run_audit)
    assert "resolve_mechanical_decision" in src
    assert "load_effective_room_config" in src
    assert "room_payload" not in src
