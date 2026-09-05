"""Tests for frozen HIGH-edge forward outcome evaluation v1."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.freeze_v1 import (
    FreezeViolation,
    verify_freeze,
    write_freeze,
)
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.integrity import json_safe
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.state_aligned_outcomes import (
    alignment_for_state,
    attach_forward_outcomes_for_event,
    compute_path_metrics,
    primary_outcome_anchor,
)

UTC = timezone.utc
BASE = datetime(2026, 8, 29, 12, 0, 0, tzinfo=UTC)


def T(sec: int) -> datetime:
    return BASE + timedelta(seconds=sec)


def bucket(sec: int, last: float, high: float | None = None, low: float | None = None):
    ts = T(sec)
    return ts, SimpleNamespace(
        high_price=high if high is not None else last,
        low_price=low if low is not None else last,
        last_price=last,
        vwap=last,
    )


def test_outcome_starts_at_state_available():
    feat = {
        "event_id": "e1",
        "direction": "LONG",
        "wall_side": "BID",
        "final_acceptance_state": "ACCEPTED_BELOW",
        "final_research_state": "ATTACKER_TRAPPED_REJECTION",
        "acceptance_checkpoints": {
            "cp_5s": {"state": "UNKNOWN_EDGE", "checkpoint_ts": "2026-08-29T12:00:05Z"},
            "cp_30s": {"state": "ACCEPTED_BELOW", "checkpoint_ts": "2026-08-29T12:00:30Z"},
        },
    }
    ts, reason = primary_outcome_anchor(feat, T(0))
    assert ts == T(30)
    assert "acceptance" in reason


def test_later_acceptance_not_from_flow_start():
    feat = {
        "event_id": "e2",
        "symbol": "BTCUSDT",
        "direction": "LONG",
        "wall_side": "BID",
        "final_acceptance_state": "FAILED_BREAK",
        "final_research_state": "ATTACKER_TRAPPED_REJECTION",
        "final_trap_label": "TRAP_CONFIRMED",
        "edge_match_confidence_class": "HIGH",
        "acceptance_checkpoints": {
            "cp_30s": {"state": "FAILED_BREAK", "checkpoint_ts": "2026-08-29T12:00:30Z"},
        },
    }
    buckets = dict(bucket(i, 100.0 + i * 0.01) for i in range(0, 120))
    rows = attach_forward_outcomes_for_event(
        feat=feat,
        buckets=buckets,
        data_end=T(4000),
        flow_start=T(0),
        flow_end=T(5),
        decision_ts=T(0),
        horizons=(10,),
    )
    primary = [r for r in rows if r["anchor"] == "state_available"][0]
    assert primary["outcome_start_ts"].endswith("30Z") or "12:00:30" in primary["outcome_start_ts"]
    diag = [r for r in rows if r["anchor"] == "flow_start"][0]
    assert diag["outcome_start_ts"] != primary["outcome_start_ts"]


def test_attacker_winning_aligns_with_attack():
    sign, reason = alignment_for_state("ATTACKER_WINNING", direction="LONG", wall_side="BID")
    assert sign == 1 and reason == "attack_direction"
    sign, _ = alignment_for_state("ATTACKER_WINNING", direction="SHORT", wall_side="ASK")
    assert sign == -1


def test_trapped_rejection_against_attack():
    sign, reason = alignment_for_state(
        "ATTACKER_TRAPPED_REJECTION", direction="LONG", wall_side="BID"
    )
    assert sign == -1 and reason == "against_attack_direction"


def test_accepted_above_bullish():
    sign, reason = alignment_for_state("ACCEPTED_ABOVE", direction="SHORT", wall_side="ASK")
    assert sign == 1 and reason == "bullish"


def test_accepted_below_bearish():
    sign, reason = alignment_for_state("ACCEPTED_BELOW", direction="LONG", wall_side="BID")
    assert sign == -1 and reason == "bearish"


def test_failed_break_against_break():
    # ASK break is up; against = down = -1
    sign, reason = alignment_for_state("FAILED_BREAK", direction="SHORT", wall_side="ASK")
    assert sign == -1 and reason == "against_break_direction"
    # BID break is down; against = up = +1
    sign, _ = alignment_for_state("FAILED_BREAK", direction="LONG", wall_side="BID")
    assert sign == 1


def test_absorption_no_invented_direction():
    sign, reason = alignment_for_state(
        "ABSORPTION_NO_RESOLUTION", direction="LONG", wall_side="BID"
    )
    assert sign is None
    assert reason == "non_directional"


def test_mixed_not_in_directional():
    sign, _ = alignment_for_state("MIXED_OR_UNKNOWN", direction="LONG", wall_side=None)
    assert sign is None


def test_incomplete_horizon_marked():
    buckets = dict(bucket(i, 100.0) for i in range(0, 20))
    m = compute_path_metrics(
        entry_ts=T(0),
        entry_price=100.0,
        horizon_s=60,
        buckets=buckets,
        data_end=T(20),
        align_sign=1,
    )
    assert m["outcome_coverage_complete"] is False
    assert m["outcome_data_quality"] == "INCOMPLETE_HORIZON"


def test_mfe_mae_from_anchor():
    # price rises then falls
    buckets = {}
    for i, px in enumerate([100.0, 100.1, 100.2, 100.0, 99.9]):
        ts, b = bucket(i, px, high=px + 0.01, low=px - 0.01)
        buckets[ts] = b
    m = compute_path_metrics(
        entry_ts=T(0),
        entry_price=100.0,
        horizon_s=5,
        buckets=buckets,
        data_end=T(100),
        align_sign=1,
    )
    assert m["MFE_bps"] is not None and m["MFE_bps"] > 0
    assert m["MAE_bps"] is not None


def test_freeze_hash_roundtrip(tmp_path: Path):
    h1 = write_freeze(tmp_path)
    ok = verify_freeze(tmp_path)
    assert ok["ok"] is True
    assert h1["freeze_bundle_sha256"] == ok["freeze_bundle_sha256"]


def test_freeze_abort_on_tamper(tmp_path: Path):
    write_freeze(tmp_path)
    # tamper stored hash
    p = tmp_path / "frozen_hashes.json"
    data = json.loads(p.read_text())
    data["source_file_sha256"]["aggressor_efficiency_trapped_vwap_acceptance/contracts.py"] = "deadbeef"
    p.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(FreezeViolation):
        verify_freeze(tmp_path)


def test_outcomes_do_not_change_alignment_helpers():
    # pure: alignment independent of path metrics
    s1, _ = alignment_for_state("ACCEPTED_BELOW", direction="LONG", wall_side="BID")
    _ = compute_path_metrics(
        entry_ts=T(0), entry_price=100.0, horizon_s=10, buckets={}, data_end=T(100), align_sign=s1
    )
    s2, _ = alignment_for_state("ACCEPTED_BELOW", direction="LONG", wall_side="BID")
    assert s1 == s2


def test_json_safe_nan():
    assert json_safe({"x": float("nan"), "y": float("inf")}) == {"x": None, "y": None}
