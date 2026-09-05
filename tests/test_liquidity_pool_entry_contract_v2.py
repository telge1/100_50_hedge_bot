"""Tests for Entry Contract V2 — separation, symmetry, regression, no EXP market reads."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

OA = Path(__file__).resolve().parents[1]
SRC = OA / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from orderbook_analyse.liquidity_pool_entry_contract_v2.case_spec import (
    CaseSpec,
    InvalidPoolApproachCombination,
    case_spec_from_frozen_expansion_case,
)
from orderbook_analyse.liquidity_pool_entry_contract_v2.decision import (
    MicroEvidence,
    prefix_parity,
    resolve_mechanical_decision,
)
from orderbook_analyse.liquidity_pool_entry_contract_v2.geometry import (
    mirror_aggressor,
    mirror_approach,
    mirror_price,
    mirror_side,
    resolve_geometry,
)
from orderbook_analyse.liquidity_pool_entry_contract_v2.mechanical import (
    MechanicalAuditError,
    run_mechanical_audit,
)
from orderbook_analyse.liquidity_pool_entry_contract_v2.regression import run_v1_regression
from orderbook_analyse.liquidity_pool_entry_contract_v2.unblind import run_outcome_unblind
from orderbook_analyse.liquidity_pool_min_target_distance_config_v1.config import (
    load_effective_room_config,
)
from orderbook_analyse.liquidity_pool_min_target_distance_config_v1.gate import PoolCandidate

V1_SHA = "76b79cce5ceac816feade974521f0b4f876adb5ab6960e54d6e9498b93e97494"
V3_SHA = "48b5a69f54603e2fa55f81e887d6f45b441878c5f3493ab936b5d849e9614cd5"
CFG_SHA = "905c8f6cd3b642cb356fe80baab64a80be231a905645a16b2e21a7b79a870050"


def _eff():
    return load_effective_room_config(OA)


def _ask_pool(entry: float, room_pct: float = 1.0) -> list[PoolCandidate]:
    # ASK above entry with enough room for LONG
    target = entry * (1 + room_pct / 100.0)
    return [
        PoolCandidate(
            pool_id="ask_tgt",
            source_timeframe="5m",
            side="ASK",
            lower_edge=target,
            upper_edge=target + 10,
            available_at="2026-08-25T00:00:00Z",
        )
    ]


def _bid_pool(entry: float, room_pct: float = 1.0) -> list[PoolCandidate]:
    target = entry * (1 - room_pct / 100.0)
    return [
        PoolCandidate(
            pool_id="bid_tgt",
            source_timeframe="5m",
            side="BID",
            lower_edge=target - 10,
            upper_edge=target,
            available_at="2026-08-25T00:00:00Z",
        )
    ]


def test_mechanical_api_never_reads_outcomes(tmp_path):
    spec = CaseSpec(
        expansion_case_id="SYN_01",
        source_candidate_id="x",
        symbol="BTCUSDT",
        reference_ts="2026-08-25T12:00:00Z",
        pool_id="p",
        pool_side="BID",
        approach="FROM_ABOVE",
        pool_timeframe="5m",
        pool_lower=100.0,
        pool_upper=101.0,
        pool_first_available_ts="2026-08-25T11:00:00Z",
        event_family_id="f",
        exposure_status="PROSPECTIVE_UNAUDITED",
    )
    with pytest.raises(MechanicalAuditError) as ei:
        run_mechanical_audit(
            spec,
            {
                "outcome_source": "/tmp/outcomes.csv",
                "evidence": {
                    "seen_inside": True,
                    "arrival_present": True,
                    "defense_ok": False,
                    "breakout_ok": False,
                    "breakout_contested": True,
                    "defense_entry": None,
                    "breakout_entry": 100.5,
                    "defense_first_ts": None,
                    "breakout_first_ts": "2026-08-25T12:01:00Z",
                },
                "pool_geometry_rows": [],
            },
            tmp_path,
            repo_root=OA,
        )
    assert ei.value.verdict == "MECHANICAL_UNBLIND_SEPARATION_FAILURE"


def test_unblind_is_separate_api(tmp_path):
    # mechanical does not import-call unblind
    import orderbook_analyse.liquidity_pool_entry_contract_v2.mechanical as mech

    src = Path(mech.__file__).read_text(encoding="utf-8")
    assert "run_outcome_unblind" not in src


def test_unblind_blocked_without_24_complete(tmp_path):
    # create fake mechanical artifact
    mech = {
        "case_id": "X",
        "mechanical_verdict": "NO_TRADE",
        "generated_at": "2026-08-31T00:00:00Z",
    }
    from orderbook_analyse.liquidity_pool_entry_contract_v2.mechanical import (
        atomic_write_json,
        atomic_write_text,
        payload_sha256,
    )

    mech["mechanical_payload_sha256"] = payload_sha256(mech)
    p = tmp_path / "mechanical_verdict_pre_unblind.json"
    atomic_write_json(p, mech)
    atomic_write_text(tmp_path / "mechanical_complete.marker", mech["mechanical_payload_sha256"] + "\n")
    with pytest.raises(MechanicalAuditError) as ei:
        run_outcome_unblind(
            p,
            None,
            tmp_path / "out",
            batch_release={"batch_release_granted": True, "mechanical_complete_count": 2},
        )
    assert "24" in str(ei.value)


def test_exp_spec_declarative_from_v3():
    v3 = json.loads(
        (OA / "results/liquidity_pool_entry_contract_expansion_freeze_v3/frozen_expansion_cases_v3.json").read_text()
    )
    row = v3["ordered_cases"][0]
    spec = case_spec_from_frozen_expansion_case(row)
    assert spec.expansion_case_id == "EXP_01"
    assert spec.pool_side == "ASK"
    assert "CASE_" not in spec.expansion_case_id


def test_no_case_xx_dependency_in_v2_modules():
    root = OA / "src/orderbook_analyse/liquidity_pool_entry_contract_v2"
    blob = ""
    for p in root.glob("*.py"):
        if p.name == "regression.py":
            continue  # regression may mention CASE artifact paths as strings
        blob += p.read_text(encoding="utf-8")
    assert "load_frozen_bid_case" not in blob
    assert "CASE_03_SPEC" not in blob


def test_bid_from_above_geometry():
    g = resolve_geometry(pool_side="BID", approach="FROM_ABOVE", lower=100.0, upper=110.0)
    assert g.front_edge == 110.0 and g.back_edge == 100.0
    assert g.defense_trade_direction == "LONG" and g.breakout_trade_direction == "SHORT"


def test_ask_from_below_geometry():
    g = resolve_geometry(pool_side="ASK", approach="FROM_BELOW", lower=100.0, upper=110.0)
    assert g.front_edge == 100.0 and g.back_edge == 110.0
    assert g.defense_trade_direction == "SHORT" and g.breakout_trade_direction == "LONG"


@pytest.mark.parametrize(
    "side,approach",
    [("BID", "FROM_BELOW"), ("ASK", "FROM_ABOVE"), ("XYZ", "FROM_ABOVE")],
)
def test_invalid_combinations_fail_closed(side, approach):
    with pytest.raises(InvalidPoolApproachCombination):
        resolve_geometry(pool_side=side, approach=approach, lower=1.0, upper=2.0)


def test_bid_defense_long():
    g = resolve_geometry(pool_side="BID", approach="FROM_ABOVE", lower=100.0, upper=110.0)
    d = resolve_mechanical_decision(
        geom=g,
        evidence=MicroEvidence(
            True, True, True, False, False, 105.0, None, "2026-08-25T12:00:10Z", None
        ),
        pools=_ask_pool(105.0, 1.0),
        effective=_eff(),
    )
    assert d.candidate_direction == "LONG"
    assert d.mechanical_verdict == "CLEAR_BID_DEFENSE_LONG_CANDIDATE"
    assert d.mechanical_trade_verdict == "TRADE_LONG_CANDIDATE"


def test_bid_breakout_short():
    g = resolve_geometry(pool_side="BID", approach="FROM_ABOVE", lower=100.0, upper=110.0)
    d = resolve_mechanical_decision(
        geom=g,
        evidence=MicroEvidence(
            True, True, False, True, False, None, 99.0, None, "2026-08-25T12:00:10Z"
        ),
        pools=_bid_pool(99.0, 1.0),
        effective=_eff(),
    )
    assert d.candidate_direction == "SHORT"
    assert d.mechanical_verdict == "CLEAR_BID_BREAKOUT_SHORT_CANDIDATE"
    assert d.mechanical_trade_verdict == "TRADE_SHORT_CANDIDATE"


def test_ask_defense_short():
    g = resolve_geometry(pool_side="ASK", approach="FROM_BELOW", lower=100.0, upper=110.0)
    d = resolve_mechanical_decision(
        geom=g,
        evidence=MicroEvidence(
            True, True, True, False, False, 105.0, None, "2026-08-25T12:00:10Z", None
        ),
        pools=_bid_pool(105.0, 1.0),
        effective=_eff(),
    )
    assert d.candidate_direction == "SHORT"
    assert d.mechanical_verdict == "CLEAR_ASK_DEFENSE_SHORT_CANDIDATE"
    assert d.mechanical_trade_verdict == "TRADE_SHORT_CANDIDATE"


def test_ask_breakout_long():
    g = resolve_geometry(pool_side="ASK", approach="FROM_BELOW", lower=100.0, upper=110.0)
    d = resolve_mechanical_decision(
        geom=g,
        evidence=MicroEvidence(
            True, True, False, True, False, None, 111.0, None, "2026-08-25T12:00:10Z"
        ),
        pools=_ask_pool(111.0, 1.0),
        effective=_eff(),
    )
    assert d.candidate_direction == "LONG"
    assert d.mechanical_verdict == "CLEAR_ASK_BREAKOUT_LONG_CANDIDATE"
    assert d.mechanical_trade_verdict == "TRADE_LONG_CANDIDATE"


def test_bid_contest_no_trade():
    g = resolve_geometry(pool_side="BID", approach="FROM_ABOVE", lower=100.0, upper=110.0)
    d = resolve_mechanical_decision(
        geom=g,
        evidence=MicroEvidence(
            True, True, False, False, True, None, 99.0, None, "2026-08-25T12:00:10Z"
        ),
        pools=[],
        effective=_eff(),
    )
    assert d.mechanical_verdict == "AMBIGUOUS_POOL_CONTEST_NO_TRADE"
    assert d.reaction == "BREAK_THEN_RECLAIM_CONTEST"
    assert d.mechanical_trade_verdict == "NO_TRADE"


def test_ask_contest_no_trade():
    g = resolve_geometry(pool_side="ASK", approach="FROM_BELOW", lower=100.0, upper=110.0)
    d = resolve_mechanical_decision(
        geom=g,
        evidence=MicroEvidence(
            True, True, False, False, True, None, 111.0, None, "2026-08-25T12:00:10Z"
        ),
        pools=[],
        effective=_eff(),
    )
    assert d.mechanical_verdict == "AMBIGUOUS_POOL_CONTEST_NO_TRADE"
    assert d.reaction == "BREAK_THEN_RECLAIM_CONTEST"


def test_symmetric_aggressor_labels():
    bid = resolve_geometry(pool_side="BID", approach="FROM_ABOVE", lower=1.0, upper=2.0)
    ask = resolve_geometry(pool_side="ASK", approach="FROM_BELOW", lower=1.0, upper=2.0)
    assert bid.attack_aggressor == "Sell" and ask.attack_aggressor == "Buy"
    assert bid.defense_counterflow == "Buy" and ask.defense_counterflow == "Sell"
    assert mirror_aggressor(bid.attack_aggressor) == ask.attack_aggressor


def test_symmetric_wall_retreat():
    bid = resolve_geometry(pool_side="BID", approach="FROM_ABOVE", lower=1.0, upper=2.0)
    ask = resolve_geometry(pool_side="ASK", approach="FROM_BELOW", lower=1.0, upper=2.0)
    assert bid.wall_retreat_adverse == "lower"
    assert ask.wall_retreat_adverse == "higher"


def test_room_gate_half_percent_unchanged():
    eff = _eff()
    assert abs(eff.room.min_target_distance_pct - 0.5) < 1e-12
    assert eff.config_sha256 == CFG_SHA


def test_config_loaded_once_sha_stable():
    a = _eff()
    b = _eff()
    assert a.config_sha256 == b.config_sha256 == CFG_SHA


def test_prefix_bid():
    g = resolve_geometry(pool_side="BID", approach="FROM_ABOVE", lower=100.0, upper=110.0)
    pools = _bid_pool(99.0, 1.0)
    d = resolve_mechanical_decision(
        geom=g,
        evidence=MicroEvidence(True, True, False, True, False, None, 99.0, None, "2026-08-25T12:00:10Z"),
        pools=pools,
        effective=_eff(),
    )
    p = prefix_parity(decision=d, pools=pools, effective=_eff(), geom=g, case_pool_id="p")
    assert p["prefix_status"] == "EXACT_PREFIX_PARITY"


def test_prefix_ask():
    g = resolve_geometry(pool_side="ASK", approach="FROM_BELOW", lower=100.0, upper=110.0)
    pools = _ask_pool(111.0, 1.0)
    d = resolve_mechanical_decision(
        geom=g,
        evidence=MicroEvidence(True, True, False, True, False, None, 111.0, None, "2026-08-25T12:00:10Z"),
        pools=pools,
        effective=_eff(),
    )
    p = prefix_parity(decision=d, pools=pools, effective=_eff(), geom=g, case_pool_id="p")
    assert p["prefix_status"] == "EXACT_PREFIX_PARITY"


def test_v1_regression_case_03_04_05():
    reg = run_v1_regression(OA)
    assert reg["ok"] is True, reg
    for c in reg["cases"]:
        assert c["expected"]["mechanical_verdict"] == "AMBIGUOUS_POOL_CONTEST_NO_TRADE"
        assert c["outcomes_read"] is False


def test_no_exp_market_queries_in_mechanical_success(tmp_path):
    spec = CaseSpec(
        expansion_case_id="SYN_ASK",
        source_candidate_id="x",
        symbol="BTCUSDT",
        reference_ts="2026-08-25T12:00:00Z",
        pool_id="p",
        pool_side="ASK",
        approach="FROM_BELOW",
        pool_timeframe="5m",
        pool_lower=100.0,
        pool_upper=110.0,
        pool_first_available_ts="2026-08-25T11:00:00Z",
        event_family_id="f",
        exposure_status="PROSPECTIVE_UNAUDITED",
    )
    res = run_mechanical_audit(
        spec,
        {
            "evidence": {
                "seen_inside": True,
                "arrival_present": True,
                "defense_ok": True,
                "breakout_ok": False,
                "breakout_contested": False,
                "defense_entry": 105.0,
                "breakout_entry": None,
                "defense_first_ts": "2026-08-25T12:00:10Z",
                "breakout_first_ts": None,
            },
            "pool_geometry_rows": [
                {
                    "pool_id": "bid_tgt",
                    "source_timeframe": "5m",
                    "side": "BID",
                    "lower_edge": 100.0,
                    "upper_edge": 103.0,
                    "available_at": "2026-08-25T00:00:00Z",
                }
            ],
        },
        tmp_path,
        repo_root=OA,
    )
    assert res["market_data_loaded"] is False
    assert res["outcomes_read"] is False
    assert (tmp_path / "mechanical_complete.marker").is_file()
    assert (tmp_path / "mechanical_verdict_pre_unblind.json").is_file()


def test_v1_and_v3_unchanged():
    v1 = json.loads((OA / "results/liquidity_pool_entry_contract_freeze_v1/entry_contract_v1.json").read_text())
    v3 = json.loads(
        (OA / "results/liquidity_pool_entry_contract_expansion_freeze_v3/frozen_expansion_cases_v3.json").read_text()
    )
    assert v1["entry_contract_freeze_sha256"] == V1_SHA
    assert v3["expansion_freeze_bundle_sha256"] == V3_SHA


def test_atomic_mechanical_persist_and_payload_marker(tmp_path):
    test_no_exp_market_queries_in_mechanical_success(tmp_path)
    mech = json.loads((tmp_path / "mechanical_verdict_pre_unblind.json").read_text())
    marker = (tmp_path / "mechanical_complete.marker").read_text().strip()
    assert marker == mech["mechanical_payload_sha256"]


def test_property_bid_ask_mirror_defense():
    """Mirror BID defense-long fixture → ASK defense-short with mirrored trade direction."""
    pivot = 1000.0
    bid_g = resolve_geometry(pool_side="BID", approach="FROM_ABOVE", lower=990.0, upper=1010.0)
    ask_g = resolve_geometry(
        pool_side=mirror_side("BID"),
        approach=mirror_approach("FROM_ABOVE"),
        lower=mirror_price(1010.0, pivot=pivot),
        upper=mirror_price(990.0, pivot=pivot),
    )
    assert ask_g.pool_side == "ASK"
    entry = 1000.0
    ts = "2026-08-25T12:00:10Z"
    bid_d = resolve_mechanical_decision(
        geom=bid_g,
        evidence=MicroEvidence(
            seen_inside=True,
            arrival_present=True,
            defense_ok=True,
            breakout_ok=False,
            breakout_contested=False,
            defense_entry=entry,
            breakout_entry=None,
            defense_first_ts=ts,
            breakout_first_ts=None,
        ),
        pools=_ask_pool(entry, 1.0),
        effective=_eff(),
    )
    ask_d = resolve_mechanical_decision(
        geom=ask_g,
        evidence=MicroEvidence(
            seen_inside=True,
            arrival_present=True,
            defense_ok=True,
            breakout_ok=False,
            breakout_contested=False,
            defense_entry=entry,
            breakout_entry=None,
            defense_first_ts=ts,
            breakout_first_ts=None,
        ),
        pools=_bid_pool(entry, 1.0),
        effective=_eff(),
    )
    assert bid_d.candidate_direction == "LONG"
    assert ask_d.candidate_direction == "SHORT"
    assert bid_d.mechanical_trade_verdict.replace("LONG", "X") == ask_d.mechanical_trade_verdict.replace(
        "SHORT", "X"
    )


def test_room_gate_long_short_symmetric_threshold():
    eff = _eff()
    # same min distance applies both ways
    assert eff.room.min_target_distance_pct == 0.5
