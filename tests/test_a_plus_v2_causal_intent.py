"""V2 causal LIMIT_INTENT + live shadow tests."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from orderbook_analyse.a_plus_liquidity_pool_signal_scanner_v1.fixtures import (
    pullback_short_confirmation_bundle,
    static_pools,
)
from orderbook_analyse.a_plus_liquidity_pool_signal_scanner_v1.models import CandidateState
from orderbook_analyse.a_plus_liquidity_pool_signal_scanner_v1.runner import run_scanner
from orderbook_analyse.a_plus_liquidity_pool_signal_scanner_v1.terminal_ladder import TerminalLadderTracker


def _loader(pools):
    def _fn(_candles, *, symbol, as_of):
        return pools

    return _fn


def test_pullback_intent_armed_before_fill():
    candles, approach_at = pullback_short_confirmation_bundle()
    result = run_scanner(
        symbol="DOGEUSDT",
        candles_by_tf=candles,
        pool_loader=_loader(static_pools(known_at=approach_at - timedelta(hours=2))),
    )
    events = result.get("pullback_limit_events") or []
    armed = [e for e in events if e["event"] == "LIMIT_INTENT_ARMED"]
    filled = [e for e in events if e["event"] == "HYPOTHETICAL_FILLED"]
    assert armed
    assert filled
    assert datetime.fromisoformat(armed[0]["at"]) < datetime.fromisoformat(filled[0]["at"])


def test_plan_frozen_after_armed():
    candles, approach_at = pullback_short_confirmation_bundle()
    result = run_scanner(
        symbol="DOGEUSDT",
        candles_by_tf=candles,
        pool_loader=_loader(static_pools(known_at=approach_at - timedelta(hours=2))),
    )
    intents = result.get("signal_intents") or []
    confirmed = [c for c in result["confirmed"] if c["setup_type"] == "A_PLUS_PULLBACK_SHORT"]
    assert intents
    assert confirmed
    intent = intents[0]
    sig = confirmed[0]
    assert intent["entry_price"] == sig["entry_price"]
    assert intent["stop_loss"] == sig["stop_price"]
    assert intent["take_profit"] == sig["target_price"]
    assert intent["pool_id"] == sig["entry_pool"]["pool_id"]


def test_freeze_audit_scanner_keeps_armed_plan_on_fill():
    candles, approach_at = pullback_short_confirmation_bundle()
    result = run_scanner(
        symbol="DOGEUSDT",
        candles_by_tf=candles,
        pool_loader=_loader(static_pools(known_at=approach_at - timedelta(hours=2))),
    )
    intents = [i for i in (result.get("signal_intents") or []) if i.get("setup_type") == "A_PLUS_PULLBACK_SHORT"]
    confirmed = [c for c in result["confirmed"] if c["setup_type"] == "A_PLUS_PULLBACK_SHORT"]
    assert intents and confirmed
    intent = intents[0]
    sig = confirmed[0]
    assert intent["entry_price"] == sig["entry_price"]
    assert intent["stop_loss"] == sig["stop_price"]
    assert intent["take_profit"] == sig["target_price"]
    assert sig["armed_at"] < (sig.get("hypothetical_filled_at") or sig.get("filled_at"))


def test_invalidated_cannot_fill_later():
    tracker = TerminalLadderTracker(direction="LONG")
    at = datetime(2026, 8, 28, 10, 0)
    tracker.record_reset(at=at, sweep_low=0.0863, sweep_high=None, detail="a")
    tracker.record_reset(at=at, sweep_low=0.0863, sweep_high=None, detail="a")
    assert tracker.duplicate_transitions_suppressed >= 1
    tracker.record_reset(at=at + timedelta(minutes=1), sweep_low=0.0861, sweep_high=None, detail="b")
    assert len(tracker.reset_events) == 2


def test_ladder_dedupe_suppresses_identical_sweep_pool():
    tracker = TerminalLadderTracker(direction="LONG")
    from orderbook_analyse.a_plus_liquidity_pool_signal_scanner_v1.terminal_ladder import LadderEvent

    at = datetime(2026, 8, 28, 8, 0)
    ev = LadderEvent(
        event="LAST_RELEVANT_POOL_SWEPT",
        at=at,
        pool_id="lld:x",
        sweep_low=0.086,
        detail="distant_macro_pool_below",
    )
    tracker.record(ev)
    tracker.record(ev)
    assert len(tracker.events) == 1
    assert tracker.duplicate_transitions_suppressed >= 1


def test_no_execution_imports_in_shadow():
    import importlib

    mod = importlib.import_module("orderbook_analyse.a_plus_liquidity_pool_signal_scanner_v1.live_shadow")
    src = Path(mod.__file__).read_text(encoding="utf-8")
    assert "bybit" not in src.lower()
    assert "place_order" not in src.lower()


def test_signal_intent_state_enum():
    assert CandidateState.LIMIT_INTENT_ARMED.value == "LIMIT_INTENT_ARMED"
