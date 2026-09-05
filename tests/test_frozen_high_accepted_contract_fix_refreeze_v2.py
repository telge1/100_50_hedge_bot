"""Tests for FROZEN_HIGH_ACCEPTED_CONTRACT_FIX_REFREEZE_V2."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from orderbook_analyse.aggressor_efficiency_flip.buckets import build_second_buckets
from orderbook_analyse.aggressor_efficiency_flip.models import Trade
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.bucket_semantics_v2 import (
    BucketDataStatus,
    CoverageWindow,
    classify_second,
)
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.contracts import TrapAcceptConfig
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.edge_acceptance_v2 import (
    assert_final_accepted_has_checkpoint,
    evaluate_edge_acceptance_v2,
)
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.episode_contract_v2 import (
    EpisodeTrackerV2,
    episode_id_v2,
    event_id_v2,
)
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.freeze_v1 import FreezeViolation
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.freeze_v2 import (
    NO_FIT_V2,
    PARENT_FREEZE_SHA,
    verify_freeze_v2,
    verify_old_freeze_untouched,
    write_freeze_v2,
)
from orderbook_analyse.aggressor_efficiency_flip.timeutil import floor_second

UTC = timezone.utc
BASE = datetime(2026, 8, 25, 12, 0, 0, tzinfo=UTC)


def T(sec: int) -> datetime:
    return BASE + timedelta(seconds=sec)


def trade(sec: int, side: str, price: float, notional: float, tid: str) -> Trade:
    return Trade(
        trade_ts=T(sec),
        trade_id=tid,
        side=side,
        price=price,
        size=notional / price,
        notional=notional,
    )


def cov(start: datetime, end: datetime) -> CoverageWindow:
    return CoverageWindow(load_start=start, load_end=end, query_ok=True, rows_loaded=10)


def test_valid_empty_vs_source_gap_vs_boundary():
    buckets = build_second_buckets([trade(10, "Buy", 100.0, 1000, "a")])
    ob = {T(11), T(12)}  # book at 11,12 — not at 13
    c = cov(T(0), T(100))
    # empty with OB
    r = classify_second(sec=T(11), buckets=buckets, coverage=c, ob200_seconds=ob, decision_ts=T(10))
    assert r["data_status"] == BucketDataStatus.VALID_EMPTY_BUCKET.value
    assert r["checkpoint_eligible"] is True
    # empty without OB
    r2 = classify_second(sec=T(13), buckets=buckets, coverage=c, ob200_seconds=ob, decision_ts=T(10))
    assert r2["data_status"] == BucketDataStatus.SOURCE_GAP.value
    assert r2["checkpoint_eligible"] is False
    # boundary
    r3 = classify_second(sec=T(200), buckets=buckets, coverage=c, ob200_seconds=ob, decision_ts=T(10))
    assert r3["data_status"] == BucketDataStatus.QUERY_BOUNDARY.value
    # trade present
    r4 = classify_second(sec=T(10), buckets=buckets, coverage=c, ob200_seconds=ob, decision_ts=T(10))
    assert r4["data_status"] == BucketDataStatus.TRADE_PRESENT.value


def test_valid_empty_not_silently_skipped_emits_checkpoint():
    # beyond for 3s then empty at +5 checkpoint second with OB200
    trades = []
    for i in range(10, 14):
        trades.append(trade(i, "Buy", 100.5, 5000, f"b{i}"))
    # skip 14 (bucket for cp at close 15 = decision+5 if decision=10)
    for i in range(15, 25):
        trades.append(trade(i, "Buy", 100.5, 5000, f"c{i}"))
    buckets = build_second_buckets(trades)
    decision = T(10)
    ob = {floor_second(decision) + timedelta(seconds=s) for s in range(0, 60)}
    acc = evaluate_edge_acceptance_v2(
        buckets=buckets,
        trades=trades,
        symbol="BTCUSDT",
        wall_side="ASK",
        edge_price=100.0,
        edge_confidence="high",
        decision_ts=decision,
        aggressor_side="Buy",
        cfg=TrapAcceptConfig(),
        coverage=cov(T(0), T(200)),
        ob200_seconds=ob,
    )
    assert_final_accepted_has_checkpoint(acc)
    # cp_5s must not be UNKNOWN_DATA incomplete_scan
    assert acc["checkpoints_discrete"]["cp_5s"]["state"] != "UNKNOWN_DATA" or acc[
        "checkpoints_discrete"
    ]["cp_5s"].get("data_status") == "VALID_EMPTY_BUCKET"
    assert any(r["data_status"] == "VALID_EMPTY_BUCKET" for r in acc["second_checkpoints"])


def test_final_accepted_without_eligible_checkpoint_not_entry():
    # SOURCE_GAP every second after decision → may get no eligible ACCEPTED
    trades = [trade(i, "Buy", 100.5, 5000, f"t{i}") for i in range(10, 40)]
    buckets = build_second_buckets(trades)
    # no OB200 → empty would be gap; trades present so TRADE_PRESENT
    # Force gaps by clearing buckets artificially for all but leaving final somehow —
    # Use as_of early so incomplete.
    acc = evaluate_edge_acceptance_v2(
        buckets={},
        trades=[],
        symbol="BTCUSDT",
        wall_side="ASK",
        edge_price=100.0,
        edge_confidence="high",
        decision_ts=T(10),
        aggressor_side="Buy",
        cfg=TrapAcceptConfig(),
        coverage=cov(T(0), T(200)),
        ob200_seconds=set(),
    )
    assert acc["entry_eligible"] is False
    assert_final_accepted_has_checkpoint(acc)


def test_first_available_and_entry_ts_convention():
    trades = [trade(i, "Buy", 100.5, 8000, f"t{i}") for i in range(10, 30)]
    buckets = build_second_buckets(trades)
    ob = {T(s) for s in range(0, 80)}
    decision = T(10)
    acc = evaluate_edge_acceptance_v2(
        buckets=buckets,
        trades=trades,
        symbol="BTCUSDT",
        wall_side="ASK",
        edge_price=100.0,
        edge_confidence="high",
        decision_ts=decision,
        aggressor_side="Buy",
        cfg=TrapAcceptConfig(),
        coverage=cov(T(0), T(200)),
        ob200_seconds=ob,
    )
    assert acc["entry_eligible"] is True
    assert acc["acceptance_first_available_ts_v2"] == acc["earliest_causal_entry_ts_v2"]
    first = datetime.fromisoformat(acc["acceptance_first_available_ts_v2"].replace("Z", "+00:00"))
    assert first > decision  # not same open of decision second without close
    # earliest entry is a bucket_close (= integer second)
    assert first.microsecond == 0


def test_prefix_vs_full_and_future_injection():
    trades = [trade(i, "Buy", 100.5, 8000, f"t{i}") for i in range(10, 80)]
    buckets = build_second_buckets(trades)
    ob = {T(s) for s in range(0, 100)}
    decision = T(10)
    cfg = TrapAcceptConfig()
    c = cov(T(0), T(200))
    full = evaluate_edge_acceptance_v2(
        buckets=buckets,
        trades=trades,
        symbol="BTCUSDT",
        wall_side="ASK",
        edge_price=100.0,
        edge_confidence="high",
        decision_ts=decision,
        aggressor_side="Buy",
        cfg=cfg,
        coverage=c,
        ob200_seconds=ob,
    )
    cut = datetime.fromisoformat(full["acceptance_first_available_ts_v2"].replace("Z", "+00:00"))
    trunc = [t for t in trades if t.trade_ts <= cut]
    pref = evaluate_edge_acceptance_v2(
        buckets=build_second_buckets(trunc),
        trades=trunc,
        symbol="BTCUSDT",
        wall_side="ASK",
        edge_price=100.0,
        edge_confidence="high",
        decision_ts=decision,
        aggressor_side="Buy",
        cfg=cfg,
        coverage=c,
        ob200_seconds=ob,
        as_of=cut,
    )
    fut = evaluate_edge_acceptance_v2(
        buckets=buckets,
        trades=trades,
        symbol="BTCUSDT",
        wall_side="ASK",
        edge_price=100.0,
        edge_confidence="high",
        decision_ts=decision,
        aggressor_side="Buy",
        cfg=cfg,
        coverage=c,
        ob200_seconds=ob,
        as_of=cut,
    )
    assert pref["acceptance_first_available_ts_v2"] == full["acceptance_first_available_ts_v2"]
    assert fut["acceptance_first_available_ts_v2"] == pref["acceptance_first_available_ts_v2"]


def test_episode_merge_and_rearm():
    tr = EpisodeTrackerV2()
    path_acc = [
        {
            "checkpoint_ts": "2026-08-25T12:00:15Z",
            "acceptance_state_at_ts": "ACCEPTED_ABOVE",
            "entry_eligible": True,
            "data_status": "TRADE_PRESENT",
            "incomplete_scan": False,
            "checkpoint_eligible": True,
        }
    ]
    r1 = tr.observe_row(
        symbol="BTCUSDT",
        matched_edge_id="e1",
        wall_side="ASK",
        decision_ts=T(10),
        acceptance_state_path=path_acc,
        entry_eligible=True,
        acceptance_first_available_ts_v2=T(15),
        earliest_causal_entry_ts_v2=T(15),
        source_gap_seen=False,
        old_event_id="old1",
        event_id_v2_val="ev_a",
    )
    assert r1["entry_eligible_v2"] is True
    r2 = tr.observe_row(
        symbol="BTCUSDT",
        matched_edge_id="e1",
        wall_side="ASK",
        decision_ts=T(20),
        acceptance_state_path=path_acc,
        entry_eligible=True,
        acceptance_first_available_ts_v2=T(25),
        earliest_causal_entry_ts_v2=T(25),
        source_gap_seen=False,
        old_event_id="old2",
        event_id_v2_val="ev_b",
    )
    assert r2["migration_class"] == "MERGED_INTO_EXISTING_EPISODE"
    assert r2["entry_eligible_v2"] is False
    # close via reclaim path then rearm
    path_end = [
        {
            "checkpoint_ts": "2026-08-25T12:01:00Z",
            "acceptance_state_at_ts": "BREAK_RECLAIMED",
            "entry_eligible": False,
            "data_status": "TRADE_PRESENT",
            "incomplete_scan": False,
            "checkpoint_eligible": True,
        }
    ]
    tr.observe_row(
        symbol="BTCUSDT",
        matched_edge_id="e1",
        wall_side="ASK",
        decision_ts=T(50),
        acceptance_state_path=path_end,
        entry_eligible=False,
        acceptance_first_available_ts_v2=None,
        earliest_causal_entry_ts_v2=None,
        source_gap_seen=False,
        old_event_id="old3",
        event_id_v2_val="ev_c",
    )
    r3 = tr.observe_row(
        symbol="BTCUSDT",
        matched_edge_id="e1",
        wall_side="ASK",
        decision_ts=T(80),
        acceptance_state_path=path_acc,
        entry_eligible=True,
        acceptance_first_available_ts_v2=T(85),
        earliest_causal_entry_ts_v2=T(85),
        source_gap_seen=False,
        old_event_id="old4",
        event_id_v2_val="ev_d",
    )
    assert r3["migration_class"] == "NEW_REARMED_EPISODE"
    assert r3["entry_eligible_v2"] is True


def test_deterministic_ids_ignore_chunk():
    a = event_id_v2(symbol="BTCUSDT", matched_edge_id="e1", decision_ts=T(10), direction="SHORT")
    b = event_id_v2(symbol="BTCUSDT", matched_edge_id="e1", decision_ts=T(10), direction="SHORT")
    assert a == b
    e1 = episode_id_v2(
        symbol="BTCUSDT",
        matched_edge_id="e1",
        acceptance_direction="ACCEPTED_ABOVE",
        wall_side="ASK",
        episode_start_ts=T(15),
    )
    e2 = episode_id_v2(
        symbol="BTCUSDT",
        matched_edge_id="e1",
        acceptance_direction="ACCEPTED_ABOVE",
        wall_side="ASK",
        episode_start_ts=T(15),
    )
    assert e1 == e2


def test_no_fit_flags_false():
    assert all(v is False for v in NO_FIT_V2.values())


def test_old_freeze_untouched_and_new_freeze(tmp_path: Path):
    old = Path(
        "/home/telgenbuescher/projects/orderbook_analyse/results/"
        "frozen_high_edge_forward_outcome_evaluation_v1"
    )
    if not (old / "frozen_hashes.json").is_file():
        pytest.skip("old freeze missing")
    v = verify_old_freeze_untouched(old)
    assert v["freeze_bundle_sha256"] == PARENT_FREEZE_SHA
    hashes = write_freeze_v2(tmp_path / "fv2")
    assert hashes["parent_freeze_bundle_sha256"] == PARENT_FREEZE_SHA
    assert hashes["refreeze_reason"] == "CHECKPOINT_AND_EPISODE_CONTRACT_FIX"
    assert hashes["thresholds_changed"] is False
    ver = verify_freeze_v2(tmp_path / "fv2")
    assert ver["ok"] is True
    # tamper
    p = tmp_path / "fv2" / "frozen_hashes_v2.json"
    data = json.loads(p.read_text())
    data["contract_sha256"] = "0" * 64
    p.write_text(json.dumps(data))
    with pytest.raises(FreezeViolation):
        verify_freeze_v2(tmp_path / "fv2")


def test_other_edge_other_episode():
    tr = EpisodeTrackerV2()
    path = [
        {
            "checkpoint_ts": "2026-08-25T12:00:15Z",
            "acceptance_state_at_ts": "ACCEPTED_ABOVE",
            "entry_eligible": True,
            "data_status": "TRADE_PRESENT",
            "incomplete_scan": False,
            "checkpoint_eligible": True,
        }
    ]
    a = tr.observe_row(
        symbol="BTCUSDT",
        matched_edge_id="e1",
        wall_side="ASK",
        decision_ts=T(10),
        acceptance_state_path=path,
        entry_eligible=True,
        acceptance_first_available_ts_v2=T(15),
        earliest_causal_entry_ts_v2=T(15),
        source_gap_seen=False,
        old_event_id="1",
        event_id_v2_val="a",
    )
    b = tr.observe_row(
        symbol="BTCUSDT",
        matched_edge_id="e2",
        wall_side="ASK",
        decision_ts=T(10),
        acceptance_state_path=path,
        entry_eligible=True,
        acceptance_first_available_ts_v2=T(15),
        earliest_causal_entry_ts_v2=T(15),
        source_gap_seen=False,
        old_event_id="2",
        event_id_v2_val="b",
    )
    assert a["episode_id_v2"] != b["episode_id_v2"]
    assert a["entry_eligible_v2"] and b["entry_eligible_v2"]
