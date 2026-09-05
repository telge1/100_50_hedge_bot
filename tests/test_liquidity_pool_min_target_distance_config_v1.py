"""Tests for configurable min target distance room gate."""

from __future__ import annotations

import inspect
import json
import textwrap
from pathlib import Path

import pytest
import yaml

from orderbook_analyse.liquidity_pool_min_target_distance_config_v1.config import (
    RoomGateConfigError,
    load_room_to_target_config,
    repo_root_from,
    validate_room_to_target_block,
)
from orderbook_analyse.liquidity_pool_min_target_distance_config_v1.gate import (
    PoolCandidate,
    evaluate_room_to_target_gate,
)

REPO = repo_root_from()


def _cfg(min_pct: float = 0.5, **overrides) -> object:
    base = load_room_to_target_config(REPO)
    if min_pct == base.min_target_distance_pct and not overrides:
        return base
    yaml_path = REPO / "strategies/strategy_lab/liquidity_pool_market_response_strategy_v0.yaml"
    doc = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    doc["room_to_target"]["min_target_distance_pct"] = min_pct
    for key, val in overrides.items():
        doc["room_to_target"][key] = val
    tmp = REPO / "results" / "liquidity_pool_min_target_distance_config_v1" / "_test_config.yaml"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(yaml.dump(doc, sort_keys=False), encoding="utf-8")
    return load_room_to_target_config(REPO, yaml_path=tmp)


def _pool(
    *,
    pool_id: str,
    side: str,
    lower: float,
    upper: float,
    tf: str = "5m",
    available_at: str = "2026-08-25T00:00:00Z",
    active: bool = True,
) -> PoolCandidate:
    return PoolCandidate(
        pool_id=pool_id,
        source_timeframe=tf,
        side=side,
        lower_edge=lower,
        upper_edge=upper,
        available_at=available_at,
        active_as_of=active,
    )


def test_long_exact_half_percent_passes():
    config = _cfg()
    entry = 100.0
    pools = [_pool(pool_id="a1", side="ASK", lower=100.5, upper=101.0)]
    gate = evaluate_room_to_target_gate(
        direction="LONG", entry_price=entry, pools=pools, config=config
    )
    assert gate["raw_target_distance_pct"] == pytest.approx(0.5)
    assert gate["raw_target_distance_bps"] == pytest.approx(50.0)
    assert gate["gate_passed"] is True
    assert gate["gate_reason"] == "TARGET_DISTANCE_SUFFICIENT"


def test_short_exact_half_percent_passes():
    config = _cfg()
    entry = 100.0
    pools = [_pool(pool_id="b1", side="BID", lower=99.0, upper=99.5)]
    gate = evaluate_room_to_target_gate(
        direction="SHORT", entry_price=entry, pools=pools, config=config
    )
    assert gate["raw_target_distance_pct"] == pytest.approx(0.5)
    assert gate["gate_passed"] is True


def test_4999_bps_fails():
    config = _cfg()
    entry = 100.0
    pools = [_pool(pool_id="a1", side="ASK", lower=100.4999, upper=101.0)]
    gate = evaluate_room_to_target_gate(
        direction="LONG", entry_price=entry, pools=pools, config=config
    )
    assert gate["raw_target_distance_pct"] == pytest.approx(0.4999)
    assert gate["gate_passed"] is False
    assert gate["gate_reason"] == "TARGET_DISTANCE_BELOW_MINIMUM"


def test_8000_bps_passes():
    config = _cfg()
    entry = 100.0
    pools = [_pool(pool_id="a1", side="ASK", lower=100.8, upper=101.0)]
    gate = evaluate_room_to_target_gate(
        direction="LONG", entry_price=entry, pools=pools, config=config
    )
    assert gate["raw_target_distance_pct"] == pytest.approx(0.8)
    assert gate["gate_passed"] is True


def test_long_uses_nearest_ask_lower_edge():
    config = _cfg()
    entry = 100.0
    pools = [
        _pool(pool_id="far", side="ASK", lower=102.0, upper=103.0),
        _pool(pool_id="near", side="ASK", lower=100.8, upper=101.5),
    ]
    gate = evaluate_room_to_target_gate(
        direction="LONG", entry_price=entry, pools=pools, config=config
    )
    assert gate["target_pool_id"] == "near"
    assert gate["target_edge"] == "lower"
    assert gate["target_price"] == 100.8


def test_short_uses_nearest_bid_upper_edge():
    config = _cfg()
    entry = 100.0
    pools = [
        _pool(pool_id="far", side="BID", lower=97.0, upper=98.0),
        _pool(pool_id="near", side="BID", lower=98.5, upper=99.2),
    ]
    gate = evaluate_room_to_target_gate(
        direction="SHORT", entry_price=entry, pools=pools, config=config
    )
    assert gate["target_pool_id"] == "near"
    assert gate["target_edge"] == "upper"
    assert gate["target_price"] == 99.2


def test_closer_pool_not_skipped():
    config = _cfg()
    entry = 100.0
    pools = [
        _pool(pool_id="second", side="ASK", lower=101.0, upper=102.0),
        _pool(pool_id="first", side="ASK", lower=100.6, upper=101.0),
    ]
    gate = evaluate_room_to_target_gate(
        direction="LONG", entry_price=entry, pools=pools, config=config
    )
    assert gate["target_pool_id"] == "first"


def test_entry_inside_opposing_pool_fails():
    config = _cfg()
    entry = 100.5
    pools = [_pool(pool_id="inside", side="ASK", lower=100.0, upper=101.0, tf="5m")]
    gate = evaluate_room_to_target_gate(
        direction="LONG", entry_price=entry, pools=pools, config=config
    )
    assert gate["gate_passed"] is False
    assert gate["gate_reason"] == "ENTRY_INSIDE_OPPOSING_POOL"


def test_htf_overlap_fails():
    config = _cfg()
    entry = 100.0
    pools = [
        _pool(pool_id="htf", side="ASK", lower=99.5, upper=100.5, tf="15m"),
        _pool(pool_id="tgt", side="ASK", lower=100.8, upper=101.2, tf="5m"),
    ]
    gate = evaluate_room_to_target_gate(
        direction="LONG", entry_price=entry, pools=pools, config=config
    )
    assert gate["gate_passed"] is False
    assert gate["gate_reason"] == "HTF_OPPOSING_POOL_OVERLAP"
    assert gate["overlap_detected"] is True


def test_missing_target_observed():
    config = _cfg()
    gate = evaluate_room_to_target_gate(
        direction="LONG", entry_price=100.0, pools=[], config=config
    )
    assert gate["gate_reason"] == "TARGET_NOT_OBSERVED"
    assert gate["gate_passed"] is False


def test_future_pool_not_used():
    config = _cfg()
    entry = 100.0
    pools = [
        _pool(
            pool_id="future",
            side="ASK",
            lower=100.8,
            upper=101.0,
            available_at="2026-08-25T02:00:00Z",
            active=False,
        )
    ]
    gate = evaluate_room_to_target_gate(
        direction="LONG",
        entry_price=entry,
        pools=pools,
        config=config,
        as_of_iso="2026-08-25T01:00:00Z",
    )
    assert gate["gate_reason"] == "TARGET_NOT_CAUSALLY_AVAILABLE"
    assert gate["gate_passed"] is False


def test_invalid_yaml_fail_closed(tmp_path: Path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        textwrap.dedent(
            """
            room_to_target:
              enabled: true
              min_target_distance_pct: -1
              measurement_origin: mechanical_entry_price
              target_edge_policy: first_reachable_edge
              long_target: {pool_side: ask, edge: lower}
              short_target: {pool_side: bid, edge: upper}
              comparison: greater_than_or_equal
              overlap_policy: block
              missing_target_policy: block
              cost_scenarios_bps: [11]
            """
        ),
        encoding="utf-8",
    )
    with pytest.raises(RoomGateConfigError):
        load_room_to_target_config(REPO, yaml_path=bad)


def test_yaml_value_change_adopted_by_engine():
    config = _cfg(min_pct=0.75)
    entry = 100.0
    pools = [_pool(pool_id="a1", side="ASK", lower=100.6, upper=101.0)]
    gate = evaluate_room_to_target_gate(
        direction="LONG", entry_price=entry, pools=pools, config=config
    )
    assert config.min_target_distance_pct == 0.75
    assert gate["min_required_distance_pct"] == 0.75
    assert gate["gate_passed"] is False


def test_no_hardcoded_half_in_gate_function():
    src = inspect.getsource(evaluate_room_to_target_gate)
    assert "0.5" not in src
    assert "50.0" not in src


def test_config_loaded_from_yaml_only():
    config = load_room_to_target_config(REPO)
    assert config.min_target_distance_pct == 0.5
    assert config.min_target_distance_bps == 50.0
    assert "liquidity_pool_market_response_strategy_v0.yaml" in config.config_source_path


def test_cost_scenarios_separate_from_gate_threshold():
    config = _cfg()
    entry = 100.0
    pools = [_pool(pool_id="a1", side="ASK", lower=100.8, upper=101.0)]
    gate = evaluate_room_to_target_gate(
        direction="LONG", entry_price=entry, pools=pools, config=config
    )
    assert gate["room_after_cost_11bps"] == pytest.approx(69.0)
    assert gate["room_after_cost_15bps"] == pytest.approx(65.0)
    assert gate["room_after_cost_20bps"] == pytest.approx(60.0)


def test_case_03_04_verdicts_unchanged():
    for dirname in (
        "case_03_frozen_bid_pool_causal_reaction_audit_v1",
        "case_04_frozen_bid_pool_causal_reaction_audit_v1",
    ):
        summary = REPO / "results" / dirname / "summary.json"
        if not summary.exists():
            pytest.skip("audit results missing")
        before = json.loads(summary.read_text(encoding="utf-8"))
        assert before["mechanical_trade_verdict"] == "NO_TRADE"
        assert before["verdict"] == "AMBIGUOUS_POOL_CONTEST_NO_TRADE"


def test_retrospective_case_03_long_fail_short_pass_distance():
    config = _cfg()
    geom = REPO / "results/case_03_frozen_bid_pool_causal_reaction_audit_v1/causal_pool_geometry.csv"
    mech = REPO / "results/case_03_frozen_bid_pool_causal_reaction_audit_v1/mechanical_verdict_pre_unblind.json"
    if not geom.exists() or not mech.exists():
        pytest.skip("CASE_03 audit missing")
    import csv

    pools = []
    with geom.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            pools.append(
                PoolCandidate(
                    pool_id=row["pool_id"],
                    source_timeframe=row["source_timeframe"],
                    side=row["side"],
                    lower_edge=float(row["lower_edge"]),
                    upper_edge=float(row["upper_edge"]),
                    available_at=row["available_at"],
                    active_as_of=True,
                )
            )
    m = json.loads(mech.read_text(encoding="utf-8"))
    ref = m["arrival_ts"]
    short_entry = m["short_branch"]["entry_price"]
    ref_mid = json.loads(
        (REPO / "results/case_03_frozen_bid_pool_causal_reaction_audit_v1/reference_mid.json").read_text(
            encoding="utf-8"
        )
    )
    long_gate = evaluate_room_to_target_gate(
        direction="LONG",
        entry_price=float(ref_mid["mid"]),
        pools=pools,
        config=config,
        as_of_iso=ref,
    )
    short_gate = evaluate_room_to_target_gate(
        direction="SHORT",
        entry_price=float(short_entry),
        pools=pools,
        config=config,
        as_of_iso=ref,
    )
    assert long_gate["raw_target_distance_bps"] == pytest.approx(9.15, rel=0.05)
    assert long_gate["gate_passed"] is False
    assert short_gate["raw_target_distance_bps"] == pytest.approx(95.2, rel=0.05)
    assert short_gate["gate_passed"] is True


def test_retrospective_case_04_both_fail_distance():
    config = _cfg()
    geom = REPO / "results/case_04_frozen_bid_pool_causal_reaction_audit_v1/causal_pool_geometry.csv"
    mech = REPO / "results/case_04_frozen_bid_pool_causal_reaction_audit_v1/mechanical_verdict_pre_unblind.json"
    if not geom.exists() or not mech.exists():
        pytest.skip("CASE_04 audit missing")
    import csv

    pools = []
    with geom.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            pools.append(
                PoolCandidate(
                    pool_id=row["pool_id"],
                    source_timeframe=row["source_timeframe"],
                    side=row["side"],
                    lower_edge=float(row["lower_edge"]),
                    upper_edge=float(row["upper_edge"]),
                    available_at=row["available_at"],
                    active_as_of=True,
                )
            )
    m = json.loads(mech.read_text(encoding="utf-8"))
    ref = m["arrival_ts"]
    ref_mid = json.loads(
        (REPO / "results/case_04_frozen_bid_pool_causal_reaction_audit_v1/reference_mid.json").read_text(
            encoding="utf-8"
        )
    )
    short_entry = m["short_branch"]["entry_price"]
    long_gate = evaluate_room_to_target_gate(
        direction="LONG",
        entry_price=float(ref_mid["mid"]),
        pools=pools,
        config=config,
        as_of_iso=ref,
    )
    short_gate = evaluate_room_to_target_gate(
        direction="SHORT",
        entry_price=float(short_entry),
        pools=pools,
        config=config,
        as_of_iso=ref,
    )
    assert long_gate["raw_target_distance_bps"] == pytest.approx(6.36, rel=0.05)
    assert long_gate["gate_passed"] is False
    assert short_gate["raw_target_distance_bps"] == pytest.approx(9.1, rel=0.05)
    assert short_gate["gate_passed"] is False


def test_validate_room_to_target_block_accepts_canonical():
    yaml_path = REPO / "strategies/strategy_lab/liquidity_pool_market_response_strategy_v0.yaml"
    block = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))["room_to_target"]
    result = validate_room_to_target_block(block)
    assert result["valid"] is True
