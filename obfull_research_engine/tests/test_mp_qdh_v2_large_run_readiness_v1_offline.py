"""Offline readiness + V2 gate regression tests."""

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

from obfull_research_engine.mp_qdh_30event_case_control_v2.features_v2 import extract_features_v2  # noqa: E402
from obfull_research_engine.mp_qdh_30event_case_control_v2.flow_v2 import recompute_bucket_mass  # noqa: E402
from obfull_research_engine.mp_qdh_30event_case_control_v2.run_study import _purged_pairs  # noqa: E402
from obfull_research_engine.mp_qdh_v2_large_run_readiness_v1 import ALLOW_CLICKHOUSE_WRITES  # noqa: E402
from obfull_research_engine.mp_qdh_v2_large_run_readiness_v1.run_readiness import (  # noqa: E402
    corrected_ie,
)


def test_readiness_no_ch_writes():
    assert ALLOW_CLICKHOUSE_WRITES is False


def test_unknown_excluded_from_net_depletion():
    m = recompute_bucket_mass(queue_before=10.0, queue_after=4.0, raw_fill=0.0, confidence="LOW")
    assert m["unknown"] == pytest.approx(6.0)
    assert m["net_depletion"] == pytest.approx(0.0)
    assert m["residual_pull"] == pytest.approx(0.0)


def test_refill_unknown_disjoint():
    m_refill = recompute_bucket_mass(queue_before=4.0, queue_after=10.0, raw_fill=0.0, confidence="HIGH")
    m_unk = recompute_bucket_mass(queue_before=10.0, queue_after=4.0, raw_fill=0.0, confidence="LOW")
    assert m_refill["refill"] > 0 and m_refill["unknown"] == 0
    assert m_unk["unknown"] > 0 and m_unk["refill"] == 0


def test_ie_zero_notional_not_available():
    ie, st = corrected_ie(progress_bps=1.0, hit_notional=0.0)
    assert ie is None and st == "NOT_AVAILABLE"
    ie2, st2 = corrected_ie(progress_bps=2.0, hit_notional=1_000_000.0)
    assert ie2 == pytest.approx(2.0) and st2 == "OK"


def test_persistence_na_few_trades():
    touch = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
    decision = touch.replace(second=5)
    rows = [
        {
            "phase": "TOUCH_TO_TRIGGER",
            "post_decision": False,
            "bucket_start": decision.isoformat().replace("+00:00", "Z"),
            "bucket_end": decision.isoformat().replace("+00:00", "Z"),
            "bucket_available_at": decision.isoformat().replace("+00:00", "Z"),
            "qdh_valid": True,
            "qdh_ewma": 0.1,
            "persistence_ratio": 0.0,
            "attributed_fill": 0,
            "attributed_fill_raw": 0,
            "residual_pull": 0,
            "refill": 0,
            "unknown": 0,
            "book_decrease": 0,
            "net_depletion": 0,
            "fill_excess_over_decrease": 0,
            "mid": 100.0,
            "same_side_depth_2bps": 10.0,
            "impact_efficiency_bps_per_million": 1e12,
            "attributed_hit_notional_usdt": 0.0,
            "progress_bps": 1.0,
        }
    ]
    feat = extract_features_v2(
        flow_100ms=rows,
        funnel={"attributed_trade_count": 0},
        touch_at=touch,
        decision_at=decision,
        trade_side="LONG",
        flow_meta={"decision_horizon_s": 5.0},
        coverage={"pass": True, "blockers": []},
    )
    assert feat["persistence_status"] == "PERSISTENCE_NOT_AVAILABLE"
    assert feat["persistence_ratio_at_decision"] is None
    assert feat["impact_efficiency_status"] == "NOT_AVAILABLE"
    assert feat["impact_efficiency_bps_per_million"] is None


def test_qdh_auc_per_valid_second():
    touch = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
    rows = []
    for i in range(5):
        ts = touch.replace(microsecond=0, second=i)
        rows.append(
            {
                "phase": "TOUCH_TO_TRIGGER",
                "post_decision": False,
                "bucket_start": ts.isoformat().replace("+00:00", "Z"),
                "bucket_end": ts.isoformat().replace("+00:00", "Z"),
                "bucket_available_at": ts.isoformat().replace("+00:00", "Z"),
                "qdh_valid": True,
                "qdh_ewma": 1.0,
                "attributed_fill": 0,
                "attributed_fill_raw": 0,
                "residual_pull": 0,
                "refill": 0,
                "unknown": 0,
                "book_decrease": 0,
                "net_depletion": 0,
                "fill_excess_over_decrease": 0,
                "mid": 100.0,
                "same_side_depth_2bps": 10.0,
                "persistence_ratio": 1.0,
            }
        )
    decision = touch.replace(second=10)
    feat = extract_features_v2(
        flow_100ms=rows,
        funnel={"attributed_trade_count": 5},
        touch_at=touch,
        decision_at=decision,
        trade_side="LONG",
        flow_meta={"decision_horizon_s": 10.0},
        coverage={"pass": True, "blockers": []},
    )
    assert feat["qdh_auc"] is not None
    assert feat["qdh_valid_seconds"] == pytest.approx(4.0)
    assert feat["qdh_auc_per_valid_second"] == pytest.approx(feat["qdh_auc"] / 4.0)


def test_purge_keeps_whole_pairs_only():
    pairs = [
        {"pair_id": "P01", "winner_event_id": "w1", "control_event_id": "c1"},
        {"pair_id": "P02", "winner_event_id": "w2", "control_event_id": "c2"},
    ]
    ov = [{"sensitivity_keep_event_id": "w1", "event_ids": "w1|w2"}]
    purged = _purged_pairs(pairs, ov)
    assert len(purged) == 1
    assert purged[0]["pair_id"] == "P01"
    # control of removed pair not reused alone
    assert all(p["control_event_id"] != "c2" for p in purged)
