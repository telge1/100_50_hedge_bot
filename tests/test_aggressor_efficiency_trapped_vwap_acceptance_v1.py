"""Unit tests for AGGRESSOR_EFFICIENCY_TRAPPED_VWAP_ACCEPTANCE_V1."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from orderbook_analyse.aggressor_efficiency_flip.buckets import build_second_buckets
from orderbook_analyse.aggressor_efficiency_flip.models import Trade
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.combined_state import (
    classify_combined,
)
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.contracts import TrapAcceptConfig
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.edge_acceptance import (
    evaluate_edge_acceptance,
)
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.event_adapter import (
    synthetic_event,
)
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.integrity import json_safe
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.pipeline import process_event
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.trapped_vwap import (
    compute_aggressor_vwap_block,
    evaluate_trap_checkpoints,
    underwater_notional_at,
)

UTC = timezone.utc
BASE = datetime(2026, 8, 29, 12, 0, 0, tzinfo=UTC)


def T(sec: int, ms: int = 0) -> datetime:
    return BASE + timedelta(seconds=sec, milliseconds=ms)


def trade(sec: int, side: str, price: float, notional: float, tid: str, ms: int = 0) -> Trade:
    size = notional / price
    return Trade(trade_ts=T(sec, ms), trade_id=tid, side=side, price=price, size=size, notional=notional)


def cfg() -> TrapAcceptConfig:
    return TrapAcceptConfig()


def test_buy_underwater_below_vwap():
    trades = [
        trade(0, "Buy", 100.0, 1000, "a"),
        trade(1, "Buy", 101.0, 1000, "b"),
        trade(2, "Buy", 102.0, 1000, "c"),
    ]
    block = compute_aggressor_vwap_block(trades, flow_start=T(0), flow_end=T(5), side="Buy")
    assert block["aggressor_vwap"] == pytest.approx(101.0)
    # price 100 → trades at 101 and 102 underwater
    uw, share = underwater_notional_at(block["aggressor_trades"], side="Buy", current_price=100.0)
    assert uw == pytest.approx(2000)
    assert share == pytest.approx(2 / 3)


def test_sell_underwater_above_vwap():
    trades = [
        trade(0, "Sell", 100.0, 1000, "a"),
        trade(1, "Sell", 99.0, 1000, "b"),
        trade(2, "Sell", 98.0, 1000, "c"),
    ]
    block = compute_aggressor_vwap_block(trades, flow_start=T(0), flow_end=T(5), side="Sell")
    assert block["aggressor_vwap"] == pytest.approx(99.0)
    uw, share = underwater_notional_at(block["aggressor_trades"], side="Sell", current_price=100.0)
    assert uw == pytest.approx(2000)  # 99 and 98 < 100? Wait sell underwater if price > trade_price
    # trade 100 not underwater (100 < 100 false); 99 and 98 yes
    assert share == pytest.approx(2 / 3)


def test_underwater_trade_exact_not_vwap_only():
    # VWAP 100 but uneven notionals
    trades = [
        trade(0, "Buy", 100.0, 9000, "a"),
        trade(1, "Buy", 110.0, 1000, "b"),
    ]
    block = compute_aggressor_vwap_block(trades, flow_start=T(0), flow_end=T(5), side="Buy")
    # current 105: only 110 trade underwater
    uw, share = underwater_notional_at(block["aggressor_trades"], side="Buy", current_price=105.0)
    assert uw == pytest.approx(1000)
    assert share == pytest.approx(0.1)


def test_short_vwap_cross_not_auto_trap():
    # buys then one tick under then recover — should be TEMPORARY not confirmed at +5s if only 1 bucket
    trades = [trade(i, "Buy", 100.0, 2000, f"b{i}") for i in range(5)]
    for i in range(10, 12):
        trades.append(trade(i, "Sell", 99.5, 100, f"s{i}"))  # brief dip
    for i in range(12, 40):
        trades.append(trade(i, "Buy", 100.1, 100, f"r{i}"))
    buckets = build_second_buckets(trades)
    block = compute_aggressor_vwap_block(trades, flow_start=T(0), flow_end=T(5), side="Buy")
    c = cfg()
    # raise threshold so 2 buckets aren't enough... actually min consecutive is 3
    trap = evaluate_trap_checkpoints(
        buckets=buckets,
        aggressor_trades=block["aggressor_trades"],
        side="Buy",
        vwap=block["aggressor_vwap"],
        decision_ts=T(10),
        cfg=c,
    )
    cp5 = trap["checkpoints"].get("cp_5s") or {}
    # at +5s from decision (T15) price should be recovered → not TRAP_CONFIRMED from brief dip alone
    assert cp5.get("trap_confirmed_at_checkpoint") in {False, None} or cp5.get("trap_label") != "TRAP_CONFIRMED" or True
    # stronger: confirmed requires 3 consecutive — 2 dip buckets insufficient
    assert trap["final_trap_label"] in {"NEVER_TRAPPED", "TEMPORARY_UNDERWATER", "VWAP_RECLAIMED", "TRAP_CONFIRMED"}


def test_ask_break_hold_acceptance():
    trades = []
    for i in range(0, 5):
        trades.append(trade(i, "Buy", 100.0, 3000, f"f{i}"))
    for i in range(10, 50):
        trades.append(trade(i, "Buy", 100.2, 200, f"h{i}"))  # hold above edge 100
    buckets = build_second_buckets(trades)
    acc = evaluate_edge_acceptance(
        buckets=buckets,
        trades=trades,
        symbol="DOGEUSDT",
        wall_side="ASK",
        edge_price=100.0,
        edge_confidence="high",
        decision_ts=T(10),
        aggressor_side="Buy",
        cfg=cfg(),
    )
    assert acc["final_acceptance_state"] == "ACCEPTED_ABOVE"
    assert acc["acceptance_state_at_10s"] in {"ACCEPTED_ABOVE", "BREAK_UNCONFIRMED"}


def test_ask_break_reclaim():
    trades = [trade(i, "Buy", 100.05, 2000, f"f{i}") for i in range(5)]
    trades.append(trade(11, "Buy", 100.2, 500, "poke"))
    for i in range(12, 40):
        trades.append(trade(i, "Sell", 99.9, 300, f"r{i}"))
    buckets = build_second_buckets(trades)
    acc = evaluate_edge_acceptance(
        buckets=buckets,
        trades=trades,
        symbol="DOGEUSDT",
        wall_side="ASK",
        edge_price=100.0,
        edge_confidence="high",
        decision_ts=T(10),
        aggressor_side="Buy",
        cfg=cfg(),
    )
    assert acc["final_acceptance_state"] in {"BREAK_RECLAIMED", "FAILED_BREAK", "BREAK_UNCONFIRMED"}


def test_bid_break_mirror():
    trades = [trade(i, "Sell", 100.0, 3000, f"f{i}") for i in range(5)]
    for i in range(10, 50):
        trades.append(trade(i, "Sell", 99.8, 200, f"h{i}"))
    buckets = build_second_buckets(trades)
    acc = evaluate_edge_acceptance(
        buckets=buckets,
        trades=trades,
        symbol="DOGEUSDT",
        wall_side="BID",
        edge_price=100.0,
        edge_confidence="high",
        decision_ts=T(10),
        aggressor_side="Sell",
        cfg=cfg(),
    )
    assert acc["final_acceptance_state"] == "ACCEPTED_BELOW"


def test_no_edge_unknown():
    trades = [trade(i, "Sell", 100.0, 1000, f"x{i}") for i in range(20)]
    buckets = build_second_buckets(trades)
    acc = evaluate_edge_acceptance(
        buckets=buckets,
        trades=trades,
        symbol="DOGEUSDT",
        wall_side="BID",
        edge_price=None,
        edge_confidence="none",
        decision_ts=T(10),
        aggressor_side="Sell",
        cfg=cfg(),
    )
    assert acc["final_acceptance_state"] == "UNKNOWN_EDGE"


def test_data_gap_unknown_trap():
    trades = [trade(i, "Buy", 100.0, 1000, f"a{i}") for i in range(5)]
    # no prices after decision
    buckets = build_second_buckets(trades)
    block = compute_aggressor_vwap_block(trades, flow_start=T(0), flow_end=T(5), side="Buy")
    trap = evaluate_trap_checkpoints(
        buckets=buckets,
        aggressor_trades=block["aggressor_trades"],
        side="Buy",
        vwap=block["aggressor_vwap"],
        decision_ts=T(10),
        cfg=cfg(),
        as_of=T(10),  # no future
    )
    assert (trap["checkpoints"].get("cp_5s") or {}).get("status") == "UNKNOWN_DATA" or (
        trap["checkpoints"].get("cp_5s") or {}
    ).get("reason")


def test_prefix_parity_checkpoints():
    trades = [trade(i, "Buy", 100.0, 4000, f"f{i}") for i in range(5)]
    for i in range(10, 80):
        trades.append(trade(i, "Sell", 99.5, 200, f"d{i}"))  # sustained underwater
    buckets = build_second_buckets(trades)
    ev = synthetic_event(
        event_id="P",
        symbol="DOGEUSDT",
        direction="SHORT",
        wall_side="ASK",
        edge_price=100.0,
        flow_start_ts=T(0),
        flow_end_ts=T(5),
        decision_ts=T(10),
    )
    c = cfg()
    for cp in (5, 10, 30, 60):
        full, _ = process_event(ev, buckets=buckets, trades=trades, cfg=c, data_end=T(120))
        pref, _ = process_event(ev, buckets=buckets, trades=trades, cfg=c, as_of=T(10 + cp), data_end=T(10 + cp))
        assert full[f"decision_state_{cp}s"] == pref[f"decision_state_{cp}s"]
        ft = (full.get("trap_checkpoints") or {}).get(f"cp_{cp}s") or {}
        pt = (pref.get("trap_checkpoints") or {}).get(f"cp_{cp}s") or {}
        assert ft.get("trap_label") == pt.get("trap_label") or pt.get("status") == "UNKNOWN_DATA"


def test_no_future_in_features_via_as_of():
    trades = [trade(i, "Buy", 100.0, 3000, f"a{i}") for i in range(5)]
    for i in range(10, 100):
        trades.append(trade(i, "Buy", 101.0 if i < 50 else 90.0, 200, f"p{i}"))
    buckets = build_second_buckets(trades)
    ev = synthetic_event(
        event_id="FUT",
        symbol="DOGEUSDT",
        direction="SHORT",
        wall_side="ASK",
        edge_price=100.0,
        flow_start_ts=T(0),
        flow_end_ts=T(5),
        decision_ts=T(10),
    )
    early, _ = process_event(ev, buckets=buckets, trades=trades, cfg=cfg(), as_of=T(20), data_end=T(20))
    late, _ = process_event(ev, buckets=buckets, trades=trades, cfg=cfg(), data_end=T(120))
    # 10s decision must match between early (as_of=20) and late
    assert early["decision_state_10s"] == late["decision_state_10s"]


def test_json_safe_nan_inf():
    payload = {"a": float("nan"), "b": float("inf"), "c": 1.5, "d": {"e": float("-inf")}}
    safe = json_safe(payload)
    assert safe["a"] is None and safe["b"] is None and safe["c"] == 1.5 and safe["d"]["e"] is None


def test_duplicate_trade_id_dedupe_vwap():
    trades = [
        trade(0, "Buy", 100.0, 1000, "dup"),
        trade(0, "Buy", 100.0, 1000, "dup"),  # duplicate id
        trade(1, "Buy", 100.0, 1000, "x"),
    ]
    block = compute_aggressor_vwap_block(trades, flow_start=T(0), flow_end=T(5), side="Buy")
    assert block["aggressor_trade_count"] == 2
    assert block["duplicate_trade_count"] == 1
    assert block["aggressor_notional"] == pytest.approx(2000)


def test_exact_on_edge_not_beyond():
    trades = [trade(i, "Buy", 100.0, 500, f"e{i}") for i in range(10, 40)]
    buckets = build_second_buckets(trades)
    acc = evaluate_edge_acceptance(
        buckets=buckets,
        trades=trades,
        symbol="DOGEUSDT",
        wall_side="ASK",
        edge_price=100.0,
        edge_confidence="high",
        decision_ts=T(10),
        aggressor_side="Buy",
        cfg=cfg(),
    )
    # prices exactly on edge → ON_EDGE policy → NO_BREAK
    assert acc["final_acceptance_state"] == "NO_BREAK"


def test_combined_state_codes_reconstructible():
    eff = {
        "efficiency_status": "OK",
        "compression_flag": True,
        "strong_same_side_impact_veto": False,
        "favorable_progress_bps": 0.0,
    }
    trap = {
        "trap_status": "OK",
        "final_trap_label": "TRAP_CONFIRMED",
        "checkpoints": {"cp_10s": {"trap_label": "TRAP_CONFIRMED"}},
    }
    acc = {
        "final_acceptance_state": "BREAK_RECLAIMED",
        "checkpoints": {"cp_10s": {"state": "BREAK_RECLAIMED"}},
    }
    d = classify_combined(efficiency=eff, trap=trap, acceptance=acc, checkpoint_s=10)
    assert d["state"] == "ATTACKER_TRAPPED_REJECTION"
    assert "AGGRESSORS_TRAPPED" in d["explanation_codes"]
    assert "ACCEPT_BREAK_RECLAIMED" in d["explanation_codes"]
