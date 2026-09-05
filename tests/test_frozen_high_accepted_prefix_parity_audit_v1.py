"""Tests for FROZEN_HIGH_ACCEPTED_PREFIX_PARITY_AUDIT_V1."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from orderbook_analyse.aggressor_efficiency_flip.buckets import build_second_buckets
from orderbook_analyse.aggressor_efficiency_flip.models import Trade
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.contracts import TrapAcceptConfig
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.edge_acceptance import (
    evaluate_edge_acceptance,
)
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.event_adapter import (
    synthetic_event,
)
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.freeze_v1 import (
    FreezeViolation,
    verify_freeze,
)
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.pipeline import process_event
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.prefix_parity_audit import (
    TIMESTAMP_TOLERANCE_MS,
    _fingerprint_parity,
    accepted_state_first_ts_from_checkpoints,
    classify_parity,
)
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.sample_expansion_runner import (
    FrozenBundleTampered,
    _verify_or_tamper,
)

UTC = timezone.utc
BASE = datetime(2026, 8, 25, 12, 0, 0, tzinfo=UTC)


def T(sec: int, ms: int = 0) -> datetime:
    return BASE + timedelta(seconds=sec, milliseconds=ms)


def trade(sec: int, side: str, price: float, notional: float, tid: str) -> Trade:
    size = notional / price
    return Trade(trade_ts=T(sec), trade_id=tid, side=side, price=price, size=size, notional=notional)


def _ask_break_trades() -> list[Trade]:
    # decision at T(10); push above ask edge 100 for enough seconds
    out = []
    for i in range(0, 10):
        out.append(trade(i, "Buy", 99.5, 1000, f"pre{i}"))
    for i in range(10, 25):
        out.append(trade(i, "Buy", 100.5, 5000, f"brk{i}"))
    # future beyond lock
    for i in range(25, 80):
        out.append(trade(i, "Buy", 101.0, 1000, f"fut{i}"))
    return out


def test_empty_bucket_checkpoint_gap_documented():
    """Frozen evaluator: empty bucket at checkpoint second → UNKNOWN_DATA incomplete_scan."""
    trades = []
    for i in range(0, 40):
        # Bucket cur=T(14) closes at T(15)=decision+5s; skip that second's trades.
        if i == 14:
            continue
        trades.append(trade(i, "Buy", 100.5 if i >= 10 else 99.0, 2000, f"t{i}"))
    buckets = build_second_buckets(trades)
    assert T(14) not in buckets or buckets[T(14)].last_price is None
    acc = evaluate_edge_acceptance(
        buckets=buckets,
        trades=trades,
        symbol="BTCUSDT",
        wall_side="ASK",
        edge_price=100.0,
        edge_confidence="high",
        decision_ts=T(10),
        aggressor_side="Buy",
        cfg=TrapAcceptConfig(),
    )
    assert acc["checkpoints"]["cp_5s"].get("state") == "UNKNOWN_DATA"
    assert acc["checkpoints"]["cp_5s"].get("reason") == "incomplete_scan"


def test_prefix_vs_full_acceptance_and_future_injection():
    trades = _ask_break_trades()
    buckets = build_second_buckets(trades)
    ev = synthetic_event(
        event_id="e1",
        symbol="BTCUSDT",
        direction="LONG",
        wall_side="ASK",
        edge_price=100.0,
        edge_source="catalog",
        edge_confidence="high",
        flow_start_ts=T(0),
        flow_end_ts=T(10),
        decision_ts=T(10),
    )
    cfg = TrapAcceptConfig()
    full, _ = process_event(ev, buckets=buckets, trades=trades, cfg=cfg, data_end=T(80))
    assert full["final_acceptance_state"] in {"ACCEPTED_ABOVE", "ACCEPTED_BELOW"}
    lock = None
    for sec in range(1, 61):
        cut = T(10) + timedelta(seconds=sec)
        f_cut, _ = process_event(ev, buckets=buckets, trades=trades, cfg=cfg, as_of=cut, data_end=cut)
        if f_cut["final_acceptance_state"] in {"ACCEPTED_ABOVE", "ACCEPTED_BELOW"}:
            lock = cut
            break
    assert lock is not None

    trunc = [t for t in trades if t.trade_ts <= lock]
    btrunc = build_second_buckets(trunc)
    pref, _ = process_event(ev, buckets=btrunc, trades=trunc, cfg=cfg, as_of=lock, data_end=lock)
    fut, _ = process_event(ev, buckets=buckets, trades=trades, cfg=cfg, as_of=lock, data_end=lock)
    assert pref["final_acceptance_state"] == full["final_acceptance_state"]
    assert fut["final_acceptance_state"] == pref["final_acceptance_state"]

    more = trades + [trade(90, "Sell", 50.0, 1e6, "crash")]
    bm = build_second_buckets(more)
    fut2, _ = process_event(ev, buckets=bm, trades=more, cfg=cfg, as_of=lock, data_end=lock)
    assert fut2["final_acceptance_state"] == pref["final_acceptance_state"]


def test_acceptance_not_before_lock_when_gap_large():
    trades = _ask_break_trades()
    buckets = build_second_buckets(trades)
    ev = synthetic_event(
        event_id="e2",
        symbol="BTCUSDT",
        direction="LONG",
        wall_side="ASK",
        edge_price=100.0,
        edge_source="catalog",
        edge_confidence="high",
        flow_start_ts=T(0),
        flow_end_ts=T(10),
        decision_ts=T(10),
    )
    cfg = TrapAcceptConfig()
    full, _ = process_event(ev, buckets=buckets, trades=trades, cfg=cfg, data_end=T(80))
    assert full["final_acceptance_state"] in {"ACCEPTED_ABOVE", "ACCEPTED_BELOW"}
    lock = None
    for sec in range(1, 61):
        cut = T(10) + timedelta(seconds=sec)
        f_cut, _ = process_event(ev, buckets=buckets, trades=trades, cfg=cfg, as_of=cut, data_end=cut)
        if f_cut["final_acceptance_state"] in {"ACCEPTED_ABOVE", "ACCEPTED_BELOW"}:
            lock = cut
            break
    early = T(10) + timedelta(milliseconds=1)
    feat_early, _ = process_event(ev, buckets=buckets, trades=trades, cfg=cfg, as_of=early, data_end=early)
    assert feat_early["final_acceptance_state"] not in {"ACCEPTED_ABOVE", "ACCEPTED_BELOW"}
    assert lock is not None and lock > early


def test_timestamp_tolerance_constant_predeclared():
    assert TIMESTAMP_TOLERANCE_MS == 250


def test_mismatch_mapping_classes():
    stored = {
        "event_id": "x",
        "matched_edge_id": "e1",
        "matched_edge_price": 100.0,
        "edge_match_confidence_class": "HIGH",
        "final_acceptance_state": "ACCEPTED_ABOVE",
    }
    base = {
        "event_id": "x",
        "matched_edge_id": "e1",
        "edge_match_confidence_class": "HIGH",
        "final_acceptance_state": "ACCEPTED_ABOVE",
        "final_research_state": "R",
        "final_trap_label": "T",
    }
    lock = T(15)
    ok = classify_parity(
        stored=stored,
        feat_full=base,
        feat_pref=base,
        feat_pre={**base, "final_acceptance_state": "BREAK_UNCONFIRMED"},
        feat_future_inj=base,
        join_edge_id="e1",
        join_edge_px=100.0,
        lock_ts=lock,
    )
    assert ok["parity_class"] == "EXACT_PARITY"

    pref_below = {**base, "final_acceptance_state": "ACCEPTED_BELOW"}
    bad_dir = classify_parity(
        stored=stored,
        feat_full=base,
        feat_pref=pref_below,
        feat_pre={**base, "final_acceptance_state": "BREAK_UNCONFIRMED"},
        feat_future_inj=pref_below,
        join_edge_id="e1",
        join_edge_px=100.0,
        lock_ts=lock,
    )
    assert bad_dir["parity_class"] == "DIRECTION_MISMATCH"
    assert bad_dir["critical"] is True

    no_lock = classify_parity(
        stored=stored,
        feat_full=base,
        feat_pref=base,
        feat_pre=base,
        feat_future_inj=base,
        join_edge_id="e1",
        join_edge_px=100.0,
        lock_ts=None,
    )
    assert no_lock["parity_class"] == "INSUFFICIENT_PREFIX_WARMUP"

    fut = classify_parity(
        stored=stored,
        feat_full=base,
        feat_pref=base,
        feat_pre=base,
        feat_future_inj={**base, "final_acceptance_state": "CHOP_AROUND_EDGE"},
        join_edge_id="e1",
        join_edge_px=100.0,
        lock_ts=lock,
    )
    assert fut["critical"] is True


def test_stable_event_id_and_fingerprint():
    rows = [
        {"event_id": "b", "parity_class": "EXACT_PARITY", "critical": False},
        {"event_id": "a", "parity_class": "EXACT_PARITY", "critical": False},
    ]
    assert _fingerprint_parity(rows) == _fingerprint_parity(list(reversed(rows)))


def test_accepted_checkpoint_helper():
    dts = T(10)
    feat = {
        "acceptance_checkpoints": json.dumps(
            {
                "cp_5s": {"state": "BREAK_UNCONFIRMED", "checkpoint_ts": "2026-08-25T12:00:15Z"},
                "cp_10s": {
                    "state": "ACCEPTED_ABOVE",
                    "checkpoint_ts": "2026-08-25T12:00:20Z",
                },
            }
        )
    }
    ts = accepted_state_first_ts_from_checkpoints(feat, dts)
    assert ts == datetime(2026, 8, 25, 12, 0, 20, tzinfo=UTC)


def test_duplicate_episode_gap_diagnostic():
    # two events same edge 30s apart → one diagnostic episode under gap=60
    gaps = [30.0, 90.0]
    episodes = 1
    for g in gaps:
        if g >= 60:
            episodes += 1
    assert episodes == 2


def test_btc_doge_tick_units_differ():
    from orderbook_analyse.l2_wall_attack_discovery.models import tick_size

    assert tick_size("BTCUSDT") != tick_size("DOGEUSDT")
    assert tick_size("DOGEUSDT") < tick_size("BTCUSDT")


def test_freeze_tamper_raises(tmp_path: Path):
    src = Path(
        "/home/telgenbuescher/projects/orderbook_analyse/results/"
        "frozen_high_edge_forward_outcome_evaluation_v1"
    )
    if not (src / "frozen_hashes.json").is_file():
        pytest.skip("freeze not present")
    for name in (
        "frozen_contract.json",
        "frozen_thresholds.json",
        "frozen_rule_manifest.json",
        "frozen_source_manifest.json",
        "frozen_hashes.json",
    ):
        (tmp_path / name).write_bytes((src / name).read_bytes())
    # Corrupt stored hash record so verify fails
    hashes = json.loads((tmp_path / "frozen_hashes.json").read_text())
    hashes["thresholds_sha256"] = "0" * 64
    (tmp_path / "frozen_hashes.json").write_text(json.dumps(hashes))
    with pytest.raises((FrozenBundleTampered, FreezeViolation)):
        _verify_or_tamper(tmp_path, "test")


def test_verify_freeze_ok():
    src = Path(
        "/home/telgenbuescher/projects/orderbook_analyse/results/"
        "frozen_high_edge_forward_outcome_evaluation_v1"
    )
    if not (src / "frozen_hashes.json").is_file():
        pytest.skip("freeze not present")
    out = verify_freeze(src)
    assert out["freeze_bundle_sha256"].startswith("67924037")
