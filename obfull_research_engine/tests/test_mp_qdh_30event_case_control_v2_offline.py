"""Offline tests for mp_qdh_30event_case_control_v2."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

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

from obfull_research_engine.mp_qdh_30event_case_control_v2 import (  # noqa: E402
    ABSORPTION_RATIO_STATUS,
    ALLOW_CLICKHOUSE_WRITES,
    EXPECTED_EVENT_LIST_SHA256,
    EXPECTED_PAIR_LIST_SHA256,
    VACUUM_SCORE_STATUS,
)
from obfull_research_engine.mp_qdh_30event_case_control_v2.coverage import evaluate_coverage_gate  # noqa: E402
from obfull_research_engine.mp_qdh_30event_case_control_v2.features_v2 import (  # noqa: E402
    extract_features_v2,
    pre_touch_depth_baseline,
)
from obfull_research_engine.mp_qdh_30event_case_control_v2.flow_v2 import recompute_bucket_mass  # noqa: E402
from obfull_research_engine.mp_qdh_30event_case_control_v2.universe import (  # noqa: E402
    load_and_verify_frozen_universe,
)
from obfull_research_engine.mp_qdh_30event_case_control_v2.run_study import _purged_pairs  # noqa: E402


def test_frozen_universe_matches_v1_hashes():
    from obfull_research_engine.mp_qdh_30event_case_control_v2 import V1_RUN_REL

    v1 = REPO_ROOT / V1_RUN_REL
    if not (v1 / "frozen_event_universe.csv").exists():
        pytest.skip("v1 frozen 30-event artifacts missing (gitignored runs/)")
    f = load_and_verify_frozen_universe(REPO_ROOT)
    assert f["event_list_sha256"] == EXPECTED_EVENT_LIST_SHA256
    assert f["pair_list_sha256"] == EXPECTED_PAIR_LIST_SHA256
    assert len(f["pairs"]) == 15


def test_fill_cap_pull_semantics():
    m = recompute_bucket_mass(queue_before=10.0, queue_after=7.0, raw_fill=5.0, confidence="HIGH")
    assert m["attributed_fill_capped"] == pytest.approx(3.0)
    assert m["fill_excess"] == pytest.approx(2.0)
    assert m["residual_pull"] == pytest.approx(0.0)
    m2 = recompute_bucket_mass(queue_before=10.0, queue_after=7.0, raw_fill=1.0, confidence="HIGH")
    assert m2["attributed_fill_capped"] == pytest.approx(1.0)
    assert m2["residual_pull"] == pytest.approx(2.0)


def test_unknown_not_silent_pull():
    m = recompute_bucket_mass(queue_before=5.0, queue_after=2.0, raw_fill=0.0, confidence="LOW")
    assert m["unknown"] == pytest.approx(3.0)
    assert m["residual_pull"] == pytest.approx(0.0)
    assert m["net_depletion"] == pytest.approx(0.0)


def test_coverage_gate_blocks():
    g = evaluate_coverage_gate(
        n_trades_raw=0, attribution_stats={}, linkage_status="PRESENT_AT_TOUCH", has_wall=True
    )
    assert not g["pass"]
    assert "PUBLIC_TRADE_COVERAGE_MISSING" in g["blockers"]
    g2 = evaluate_coverage_gate(
        n_trades_raw=10,
        attribution_stats={"cross_epoch": 1, "seq_gaps": 0},
        linkage_status="PRESENT_AT_TOUCH",
        has_wall=True,
    )
    assert "REPLAY_EPOCH_MIX_OR_CHANGE" in g2["blockers"]
    g3 = evaluate_coverage_gate(
        n_trades_raw=10,
        attribution_stats={"cross_epoch": 0, "seq_gaps": 2},
        linkage_status="PRESENT_AT_TOUCH",
        has_wall=True,
    )
    assert "BOOK_SEQUENCE_GAP" in g3["blockers"]


def test_depth_baseline_and_norm_in_features():
    touch = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
    decision = touch.replace(second=30)
    rows = []
    for i in range(5):
        rows.append(
            {
                "phase": "PRE_TOUCH",
                "post_decision": False,
                "same_side_depth_2bps": 100.0 + i,
                "bucket_start": touch.isoformat().replace("+00:00", "Z"),
                "bucket_end": touch.isoformat().replace("+00:00", "Z"),
                "bucket_available_at": touch.isoformat().replace("+00:00", "Z"),
                "qdh_valid": True,
                "qdh_ewma": 0.1,
                "attributed_fill": 0,
                "attributed_fill_raw": 0,
                "residual_pull": 0,
                "refill": 0,
                "unknown": 0,
                "book_decrease": 0,
                "net_depletion": 0,
                "fill_excess_over_decrease": 0,
                "mid": 100.0,
            }
        )
    rows.append(
        {
            "phase": "TOUCH_TO_TRIGGER",
            "post_decision": False,
            "same_side_depth_2bps": 200.0,
            "bucket_start": decision.isoformat().replace("+00:00", "Z"),
            "bucket_end": decision.isoformat().replace("+00:00", "Z"),
            "bucket_available_at": decision.isoformat().replace("+00:00", "Z"),
            "qdh_valid": True,
            "qdh_ewma": 0.2,
            "attributed_fill": 1,
            "attributed_fill_raw": 1,
            "residual_pull": 0,
            "refill": 0,
            "unknown": 0,
            "book_decrease": 1,
            "net_depletion": 1,
            "fill_excess_over_decrease": 0,
            "mid": 101.0,
            "persistence_ratio": 1.2,
            "impact_efficiency_bps_per_million": 3.0,
            "progress_bps": 1.0,
        }
    )
    base = pre_touch_depth_baseline(rows)
    assert base == pytest.approx(102.0)
    feat = extract_features_v2(
        flow_100ms=rows,
        funnel={},
        touch_at=touch,
        decision_at=decision,
        trade_side="LONG",
        flow_meta={"decision_horizon_s": 30.0, "queue_at_touch": 1.0},
        coverage={"pass": True, "blockers": []},
    )
    assert feat["same_side_depth_2bps_at_decision_norm"] == pytest.approx(200.0 / 102.0 - 1.0)
    assert feat["absorption_ratio_status"] == ABSORPTION_RATIO_STATUS
    assert feat["vacuum_score_status"] == VACUUM_SCORE_STATUS
    assert feat["qdh_auc_per_decision_second"] is not None or feat["qdh_auc"] is None


def test_qdh_exhausted_is_na_reason():
    touch = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
    decision = touch.replace(second=5)
    rows = [
        {
            "phase": "TOUCH_TO_TRIGGER",
            "post_decision": False,
            "bucket_start": decision.isoformat().replace("+00:00", "Z"),
            "bucket_end": decision.isoformat().replace("+00:00", "Z"),
            "bucket_available_at": decision.isoformat().replace("+00:00", "Z"),
            "qdh_valid": False,
            "qdh_ewma": None,
            "qdh_invalid_reason": "QUEUE_EXHAUSTED",
            "attributed_fill": 0,
            "attributed_fill_raw": 0,
            "residual_pull": 0,
            "refill": 0,
            "unknown": 0,
            "book_decrease": 0,
            "net_depletion": 0,
            "fill_excess_over_decrease": 0,
            "mid": 100.0,
            "same_side_depth_2bps": None,
        }
    ]
    feat = extract_features_v2(
        flow_100ms=rows,
        funnel={},
        touch_at=touch,
        decision_at=decision,
        trade_side="SHORT",
        flow_meta={"decision_horizon_s": 5.0},
        coverage={"pass": True, "blockers": []},
    )
    assert feat["qdh_at_decision"] is None
    assert feat["qdh_at_decision_na_reason"] == "QUEUE_EXHAUSTED"


def test_overlap_purge_deterministic():
    pairs = [
        {"pair_id": "P01", "winner_event_id": "a", "control_event_id": "b"},
        {"pair_id": "P02", "winner_event_id": "c", "control_event_id": "d"},
    ]
    ov = [{"sensitivity_keep_event_id": "a", "event_ids": "a|c"}]
    purged = _purged_pairs(pairs, ov)
    assert len(purged) == 1
    assert purged[0]["pair_id"] == "P01"


def test_no_ch_writes():
    assert ALLOW_CLICKHOUSE_WRITES is False


def test_persistence_used_in_update_qdh_signature():
    from obfull_research_engine.level_first_episode1_wall_flow_qdh_base_v1.aggressor_flow import update_aggressor
    from obfull_research_engine.level_first_episode1_wall_flow_qdh_base_v1.queue_depletion_hazard import (
        QdhState,
        update_qdh,
    )

    assert callable(update_aggressor)
    s = update_qdh(QdhState(), net_depletion_qty=1.0, interval_duration_s=0.1, current_queue=10.0, persistence_ratio=1.5)
    assert s.m_persistence == pytest.approx(1.5) or s.qdh_toxic_base_only != s.qdh_base
