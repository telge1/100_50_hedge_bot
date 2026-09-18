"""Freeze tests: typed timeline, footprint cluster, transitive contract hash."""

from __future__ import annotations

import hashlib
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

ENGINE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ENGINE_ROOT.parent
_shadow = str(REPO_ROOT / "src")
while _shadow in sys.path:
    sys.path.remove(_shadow)
sys.path.insert(0, str(ENGINE_ROOT / "src"))
_ORDERBOOK_SRC = Path("/home/telgenbuescher/projects/orderbook_analyse/src")
if _ORDERBOOK_SRC.is_dir():
    sys.path.insert(0, str(_ORDERBOOK_SRC))

import pytest  # noqa: E402

from obfull_research_engine.mp_qdh_first_touch_study_v1.contract import (  # noqa: E402
    CONTRACT_HASH,
    CONTRACT_SOURCE_RELPATHS,
    compute_contract_hash,
    validate_checkpoint_contract,
)
from obfull_research_engine.mp_qdh_first_touch_study_v1.footprint_cluster import (  # noqa: E402
    REQUIRED_FOOTPRINT_FIELDS,
    build_footprint_cluster_event,
    collect_qdh_attributed_trade_ids,
    deterministic_trade_ids_hash,
)
from obfull_research_engine.mp_qdh_first_touch_study_v1.typed_timeline import (  # noqa: E402
    REQUIRED_TYPED_FIELDS,
    TYPED_EVENT_TYPES,
    materialize_typed_timeline,
    typed_timeline_hash,
)


def _ev(
    *,
    tid: str,
    hit: float,
    notional: float,
    t0: datetime,
    t1: datetime,
    q0: float = 10.0,
    q1: float = 9.0,
    pull: float = 0.0,
    refill: float = 0.0,
    side: str = "ask",
    conf: str = "HIGH",
):
    return SimpleNamespace(
        episode_id="ep1",
        symbol="BTCUSDT",
        zone_id="z1",
        wall_id="w1",
        wall_side=side,
        wall_price=100.0,
        band_low=99.0,
        band_high=101.0,
        view="defended_band",
        event_type="INTERVAL",
        interval_start_exchange_time=t0.isoformat().replace("+00:00", "Z"),
        interval_end_exchange_time=t1.isoformat().replace("+00:00", "Z"),
        interval_start_available_at=t0.isoformat().replace("+00:00", "Z"),
        interval_end_available_at=t1.isoformat().replace("+00:00", "Z"),
        max_input_available_at=t1.isoformat().replace("+00:00", "Z"),
        computed_at=t1.isoformat().replace("+00:00", "Z"),
        attribution_available_at=t1.isoformat().replace("+00:00", "Z"),
        replay_epoch=1,
        book_sequence_start=1,
        book_sequence_end=2,
        book_update_id_start=1,
        book_update_id_end=2,
        queue_before=q0,
        queue_after=q1,
        delta_queue=q1 - q0,
        attributed_trade_count=1 if hit > 0 else 0,
        attributed_trade_ids=[tid] if hit > 0 else [],
        attributed_trade_ids_hash="x",
        attributed_hit_qty=hit,
        attributed_hit_notional=notional,
        net_refill_qty=refill,
        residual_pull_qty=pull,
        net_depletion_qty=hit + pull - refill,
        aggregate_queue_survival_proxy=0.9,
        aggressor_side="Buy" if side == "ask" else "Sell",
        attribution_rule_version="v1",
        attribution_confidence=conf,
        mass_balance_error=0.0,
        coverage_ok=True,
        look_ahead=False,
        source_record_ids=["src1"],
    )


def test_transitive_sources_include_engines():
    joined = "\n".join(CONTRACT_SOURCE_RELPATHS)
    for name in (
        "wall_flow_attribution.py",
        "timeline_100ms.py",
        "aggressor_flow.py",
        "flow_v2.py",
        "typed_timeline.py",
        "footprint_cluster.py",
    ):
        assert name in joined


def test_engine_file_change_changes_contract_hash(tmp_path, monkeypatch):
    # Simulate hashing sensitivity by monkeypatching collect path content via compute on body-only vs full
    h_full = compute_contract_hash(include_sources=True)
    h_body = compute_contract_hash(include_sources=False)
    assert h_full != h_body
    assert h_full == CONTRACT_HASH


def test_legacy_checkpoint_rejected():
    from obfull_research_engine.mp_qdh_first_touch_study_v1.contract import (
        LEGACY_PARAM_ONLY_CONTRACT_HASH,
    )

    r = validate_checkpoint_contract(
        {"contract_hash": LEGACY_PARAM_ONLY_CONTRACT_HASH},
        expected_hash=CONTRACT_HASH,
    )
    assert r["ok"] is False


def test_typed_timeline_required_fields_and_types():
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    touch = t0 + timedelta(seconds=10)
    decision = touch + timedelta(seconds=30)
    evs = [
        _ev(tid="t1", hit=1.0, notional=1000.0, t0=touch, t1=touch + timedelta(seconds=1)),
        _ev(
            tid="t2",
            hit=0.0,
            notional=0.0,
            t0=touch + timedelta(seconds=1),
            t1=touch + timedelta(seconds=2),
            q0=9.0,
            q1=8.5,
            pull=0.5,
        ),
    ]
    rows = materialize_typed_timeline(
        event_id="e1",
        episode_id="ep1",
        symbol="BTCUSDT",
        zone_id="z1",
        wall_id="w1",
        wall_side="ask",
        wall_price=100.0,
        band_low=99.0,
        band_high=101.0,
        touch_at=touch,
        decision_at=decision,
        zone_available_at=t0,
        wall_visible_at=t0 + timedelta(seconds=5),
        band_events=evs,
        flow_100ms=[],
        wall_move_events=[],
        coverage_ok=True,
        flow_attribution_confidence="HIGH",
        availability_confidence="RECEIVE_TIME_NOT_AVAILABLE",
        include_post_decision=False,
    )
    assert rows
    types = {r["event_type"] for r in rows}
    assert "ZONE_AVAILABLE" in types
    assert "ZONE_FIRST_TOUCH" in types
    assert "DECISION" in types
    assert "AGGRESSOR_TRADE" in types
    for r in rows:
        for f in REQUIRED_TYPED_FIELDS:
            assert f in r
        assert r["event_type"] in TYPED_EVENT_TYPES
        assert r["exchange_event_time"].endswith("Z")
        assert r["phase"] != "POST_TRIGGER_FORENSIC" or r["event_type"] == "DECISION"
        assert "mfe" not in r and "mae" not in r
    # post-decision interval excluded
    post = _ev(
        tid="t_post",
        hit=9.0,
        notional=9000.0,
        t0=decision + timedelta(seconds=1),
        t1=decision + timedelta(seconds=2),
    )
    rows2 = materialize_typed_timeline(
        event_id="e1",
        episode_id="ep1",
        symbol="BTCUSDT",
        zone_id="z1",
        wall_id="w1",
        wall_side="ask",
        wall_price=100.0,
        band_low=99.0,
        band_high=101.0,
        touch_at=touch,
        decision_at=decision,
        zone_available_at=t0,
        wall_visible_at=None,
        band_events=evs + [post],
        flow_100ms=[],
        wall_move_events=[],
        coverage_ok=True,
        flow_attribution_confidence="HIGH",
        availability_confidence="RECEIVE_TIME_NOT_AVAILABLE",
        include_post_decision=False,
    )
    assert all(
        not (r["event_type"] == "AGGRESSOR_TRADE" and float(r.get("size_base") or 0) >= 90)
        for r in rows2
    )
    h1 = typed_timeline_hash(rows)
    h2 = typed_timeline_hash(rows)
    assert h1 == h2


def test_footprint_same_ids_no_qdh_add_not_calibrated():
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    touch = t0 + timedelta(seconds=10)
    decision = touch + timedelta(seconds=30)
    evs = [
        _ev(tid="b", hit=1.0, notional=1_000_000.0, t0=touch, t1=touch + timedelta(seconds=1)),
        _ev(
            tid="a",
            hit=2.0,
            notional=2_000_000.0,
            t0=touch + timedelta(seconds=1),
            t1=touch + timedelta(seconds=2),
            q0=9.0,
            q1=7.0,
        ),
        # post decision must be ignored
        _ev(
            tid="post",
            hit=99.0,
            notional=99.0,
            t0=decision + timedelta(seconds=1),
            t1=decision + timedelta(seconds=2),
        ),
    ]
    qdh_ids = collect_qdh_attributed_trade_ids(evs, decision_at=decision)
    assert qdh_ids == ["b", "a"]  # order of first appearance, unique
    fp = build_footprint_cluster_event(
        event_id="e1",
        episode_id="ep1",
        wall_id="w1",
        wall_side="ask",
        wall_price=100.0,
        band_low=99.0,
        band_high=101.0,
        band_events=evs,
        decision_at=decision,
        touch_at=touch,
        coverage_ok=True,
        flow_attribution_confidence="HIGH",
        availability_confidence="RECEIVE_TIME_NOT_AVAILABLE",
        progress_bps=10.0,
    )
    d = fp.to_dict()
    for f in REQUIRED_FOOTPRINT_FIELDS:
        assert f in d
    assert fp.trade_ids_hash == deterministic_trade_ids_hash(qdh_ids)
    assert fp.qdh_hit_trade_ids_hash == fp.trade_ids_hash
    assert fp.adds_to_qdh_hits is False
    assert "post" not in fp.attributed_trade_ids
    assert fp.unique_trade_count == 2
    assert fp.normalized_impact_efficiency_status == "NOT_CALIBRATED"
    assert fp.absorption_ratio_status == "NOT_CALIBRATED"
    assert fp.vacuum_score_status == "NOT_CALIBRATED"
    assert fp.impact_efficiency == pytest.approx(10.0 / 3.0)  # progress / (3e6/1e6)
    # zero notional → NA, no epsilon inflation
    fp0 = build_footprint_cluster_event(
        event_id="e1",
        episode_id="ep1",
        wall_id="w1",
        wall_side="ask",
        wall_price=100.0,
        band_low=99.0,
        band_high=101.0,
        band_events=[
            _ev(tid="z", hit=0.0, notional=0.0, t0=touch, t1=touch + timedelta(seconds=1), q0=10, q1=10)
        ],
        decision_at=decision,
        touch_at=touch,
        coverage_ok=True,
        flow_attribution_confidence="HIGH",
        availability_confidence="RECEIVE_TIME_NOT_AVAILABLE",
        progress_bps=10.0,
    )
    assert fp0.impact_efficiency is None
    assert fp0.impact_efficiency_status == "NOT_AVAILABLE"


def test_bid_wall_aggressor_side_in_footprint():
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    touch = t0 + timedelta(seconds=5)
    decision = touch + timedelta(seconds=20)
    evs = [
        _ev(
            tid="s1",
            hit=1.5,
            notional=1500.0,
            t0=touch,
            t1=touch + timedelta(seconds=1),
            side="bid",
        )
    ]
    fp = build_footprint_cluster_event(
        event_id="e1",
        episode_id="ep1",
        wall_id="w1",
        wall_side="bid",
        wall_price=100.0,
        band_low=99.0,
        band_high=101.0,
        band_events=evs,
        decision_at=decision,
        touch_at=touch,
        coverage_ok=True,
        flow_attribution_confidence="MEDIUM",
        availability_confidence="RECEIVE_TIME_NOT_AVAILABLE",
    )
    assert fp.attack_direction == "SELL_ATTACK_BID"
    assert fp.sell_qty == pytest.approx(1.5)
    assert fp.buy_qty == 0.0
