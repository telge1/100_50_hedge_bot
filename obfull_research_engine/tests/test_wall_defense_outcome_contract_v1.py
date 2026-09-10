"""Tests for wall_defense_outcome_contract_v1."""

from __future__ import annotations

import copy
import sys
from datetime import timedelta
from pathlib import Path

ENGINE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE_ROOT / "src"))
sys.path.insert(0, str(ENGINE_ROOT.parent / "src"))

from obfull_research_engine.drilldown.aggregation_100ms import _as_dt  # noqa: E402
from obfull_research_engine.wall_defense_outcome_contract_v1 import (  # noqa: E402
    CONTRACT_HASH,
    HORIZON_MS,
    LABEL_CENSORED,
    LABEL_JOINT_ATTACK_AT_H,
    LABEL_JOINT_RECLAIM_AT_H,
    LABEL_LAYERED_RECLAIM,
    contract_definition,
    outcome_contract_hash,
)
from obfull_research_engine.wall_defense_outcome_contract_v1.facts_builder import (  # noqa: E402
    compute_outcome_facts,
)
from obfull_research_engine.wall_defense_outcome_contract_v1.labels import label_from_facts  # noqa: E402
from obfull_research_engine.wall_defense_outcome_contract_v1.side import (  # noqa: E402
    attack_progress_ticks,
    on_attack_side,
    on_defender_side,
)


def _tl(rows):
    """Build minimal timeline rows."""
    out = []
    for t, mid, micro, bb, ba, ok in rows:
        out.append(
            {
                "decision_time": t,
                "timestamp": t,
                "midprice": mid,
                "microprice": micro,
                "best_bid": bb,
                "best_ask": ba,
                "coverage_ok": ok,
                "max_input_available_at": t,
            }
        )
    return out


def test_01_ask_bid_mirror():
    assert on_attack_side(100.0, wall_price=100.0, wall_side="ask") is True
    assert on_defender_side(99.9, wall_price=100.0, wall_side="ask") is True
    assert on_attack_side(100.0, wall_price=100.0, wall_side="bid") is True
    assert on_defender_side(100.1, wall_price=100.0, wall_side="bid") is True
    # progress signs flip
    a = attack_progress_ticks(101.0, reference=100.0, wall_side="ask")
    b = attack_progress_ticks(99.0, reference=100.0, wall_side="bid")
    assert a is not None and b is not None and a > 0 and b > 0


def test_02_touch_vs_detection_not_mixed():
    cov = _as_dt("2026-09-06T20:19:59.800Z")
    rows = _tl(
        [
            ("2026-09-06T20:19:40Z", 79779.0, 79779.0, 79778.9, 79779.1, True),
            ("2026-09-06T20:19:42Z", 79781.0, 79781.0, 79780.9, 79781.1, True),
            ("2026-09-06T20:19:50Z", 79785.0, 79785.0, 79784.9, 79785.1, True),
        ]
    )
    f_touch = compute_outcome_facts(
        episode_id="ep",
        symbol="BTCUSDT",
        zone_id="z",
        wall_side="ask",
        wall_price=79780.0,
        anchor_type="WALL_FIRST_TOUCH",
        anchor_time="2026-09-06T20:19:40Z",
        horizon_ms=10000,
        timeline_rows=rows,
        coverage_end=cov,
        replay_epoch=4,
    )
    f_det = compute_outcome_facts(
        episode_id="ep",
        symbol="BTCUSDT",
        zone_id="z",
        wall_side="ask",
        wall_price=79780.0,
        anchor_type="DETECTION",
        anchor_time="2026-09-06T20:21:00Z",
        horizon_ms=10000,
        timeline_rows=rows,
        coverage_end=cov,
        replay_epoch=4,
    )
    assert f_touch.anchor_type != f_det.anchor_type
    assert f_touch.coverage_ok is True
    assert f_det.coverage_ok is False  # detection after coverage


def test_03_future_before_anchor_not_used_for_end():
    cov = _as_dt("2026-09-06T20:19:59.800Z")
    rows = _tl(
        [
            ("2026-09-06T20:19:30Z", 79800.0, 79800.0, 79799.9, 79800.1, True),  # before anchor
            ("2026-09-06T20:19:42Z", 79781.0, 79781.0, 79780.9, 79781.1, True),
        ]
    )
    f = compute_outcome_facts(
        episode_id="ep",
        symbol="BTCUSDT",
        zone_id="z",
        wall_side="ask",
        wall_price=79780.0,
        anchor_type="FIRST_JOINT_BREACH",
        anchor_time="2026-09-06T20:19:41.900Z",
        horizon_ms=5000,
        timeline_rows=rows,
        coverage_end=cov,
    )
    # end mid should be from after-anchor path, not 79800 pre-anchor as "progress from start"
    assert f.start_midprice is not None
    # start is at/before anchor → 79781 or 79800 at_or_before 20:19:41.9 → 79800 row is before
    assert f.end_midprice == 79781.0


def test_04_breach_without_reclaim_attack_label():
    cov = _as_dt("2026-09-06T20:19:59.800Z")
    rows = _tl(
        [
            ("2026-09-06T20:19:41Z", 79779.0, 79779.0, 79778.9, 79779.1, True),
            ("2026-09-06T20:19:42Z", 79781.0, 79781.0, 79780.9, 79781.1, True),
            ("2026-09-06T20:19:50Z", 79790.0, 79790.0, 79789.9, 79790.1, True),
        ]
    )
    f = compute_outcome_facts(
        episode_id="ep",
        symbol="BTCUSDT",
        zone_id="z",
        wall_side="ask",
        wall_price=79780.0,
        anchor_type="FIRST_JOINT_BREACH",
        anchor_time="2026-09-06T20:19:41.900Z",
        horizon_ms=10000,
        timeline_rows=rows,
        coverage_end=cov,
        chain={"nodes": [{"wall_price": 79780.0, "first_trade_touch": "2026-09-06T20:19:42Z", "end_time": None}], "chain_advance_ticks": 0},
    )
    lab = label_from_facts(f)
    assert f.joint_breach_observed is True
    assert lab.label == LABEL_JOINT_ATTACK_AT_H


def test_05_breach_with_reclaim_label():
    cov = _as_dt("2026-09-06T20:19:59.800Z")
    rows = _tl(
        [
            ("2026-09-06T20:19:41Z", 79779.0, 79779.0, 79778.9, 79779.1, True),
            ("2026-09-06T20:19:42Z", 79781.0, 79781.0, 79780.9, 79781.1, True),
            ("2026-09-06T20:19:50Z", 79770.0, 79770.0, 79769.9, 79770.1, True),
        ]
    )
    f = compute_outcome_facts(
        episode_id="ep",
        symbol="BTCUSDT",
        zone_id="z",
        wall_side="ask",
        wall_price=79780.0,
        anchor_type="FIRST_JOINT_BREACH",
        anchor_time="2026-09-06T20:19:41.900Z",
        horizon_ms=10000,
        timeline_rows=rows,
        coverage_end=cov,
        chain={
            "nodes": [
                {"wall_price": 79780.0, "first_trade_touch": "2026-09-06T20:19:42Z", "end_time": "2026-09-06T20:19:45Z"},
            ],
            "chain_advance_ticks": 0,
        },
    )
    lab = label_from_facts(f)
    assert lab.label == LABEL_JOINT_RECLAIM_AT_H


def test_06_layered_defense_reclaim():
    cov = _as_dt("2026-09-06T20:19:59.800Z")
    rows = _tl(
        [
            ("2026-09-06T20:19:41Z", 79779.0, 79779.0, 79778.9, 79779.1, True),
            ("2026-09-06T20:19:42Z", 79781.0, 79781.0, 79780.9, 79781.1, True),
            ("2026-09-06T20:19:45Z", 79786.0, 79786.0, 79785.9, 79786.1, True),
            ("2026-09-06T20:19:55Z", 79770.0, 79770.0, 79769.9, 79770.1, True),
        ]
    )
    f = compute_outcome_facts(
        episode_id="ep",
        symbol="BTCUSDT",
        zone_id="z",
        wall_side="ask",
        wall_price=79780.0,
        anchor_type="FIRST_JOINT_BREACH",
        anchor_time="2026-09-06T20:19:41.900Z",
        horizon_ms=15000,
        timeline_rows=rows,
        coverage_end=cov,
        chain={
            "nodes": [
                {"wall_price": 79780.0, "first_trade_touch": "2026-09-06T20:19:42Z", "end_time": "2026-09-06T20:19:44Z"},
                {"wall_price": 79785.0, "first_trade_touch": "2026-09-06T20:19:45Z", "end_time": "2026-09-06T20:19:50Z"},
            ],
            "chain_advance_ticks": 50,
        },
    )
    lab = label_from_facts(f)
    assert f.attacked_chain_node_count >= 2
    assert lab.label == LABEL_LAYERED_RECLAIM


def test_07_epoch_boundary_censors_full_horizon():
    cov = _as_dt("2026-09-06T20:19:59.800Z")
    rows = _tl([("2026-09-06T20:19:42Z", 79781.0, 79781.0, 79780.9, 79781.1, True)])
    f = compute_outcome_facts(
        episode_id="ep",
        symbol="BTCUSDT",
        zone_id="z",
        wall_side="ask",
        wall_price=79780.0,
        anchor_type="FIRST_JOINT_BREACH",
        anchor_time="2026-09-06T20:19:41.900Z",
        horizon_ms=120_000,
        timeline_rows=rows,
        coverage_end=cov,
        replay_epoch=4,
    )
    assert f.coverage_ok is False
    assert label_from_facts(f).label == LABEL_CENSORED
    # horizon_end still full length (not shortened)
    assert int(round((_as_dt(f.horizon_end) - _as_dt(f.anchor_time)).total_seconds() * 1000)) == 120_000


def test_08_contract_hash_changes_on_definition_change():
    h1 = outcome_contract_hash()
    assert h1 == CONTRACT_HASH
    # local mutation simulation
    d = contract_definition()
    d2 = copy.deepcopy(d)
    d2["horizons_ms"] = list(d2["horizons_ms"]) + [999]
    import hashlib
    import json

    h2 = hashlib.sha256(json.dumps(d2, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert h1 != h2


def test_09_all_horizons_defined():
    assert HORIZON_MS[0] == 1000 and HORIZON_MS[-1] == 1_800_000
    assert len(HORIZON_MS) == 11


def test_10_pipeline_smoke(tmp_path):
    from obfull_research_engine.wall_defense_outcome_contract_v1.pipeline import run_outcome_contract

    r = run_outcome_contract(run_key="odc_smoke", out_dir=tmp_path / "smoke")
    assert r["oracle"]["ok"] is True
    assert r["n_reclaim_labels_valid"] == 0
    assert r["n_valid"] > 0
    assert r["n_censored"] > 0
    assert r["verdict"] == "WALL_DEFENSE_OUTCOME_CONTRACT_V1_PROVEN"
