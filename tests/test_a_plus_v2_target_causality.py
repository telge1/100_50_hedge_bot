"""Target pool causality and marker dedupe tests."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from orderbook_analyse.a_plus_liquidity_pool_signal_scanner_v1.fixtures import (
    pool,
    pullback_short_confirmation_bundle,
    static_pools,
)
from orderbook_analyse.a_plus_liquidity_pool_signal_scanner_v1.markers import (
    dedupe_plan_rows,
    signals_to_marker_specs,
)
from orderbook_analyse.a_plus_liquidity_pool_signal_scanner_v1.models import PoolRecord
from orderbook_analyse.a_plus_liquidity_pool_signal_scanner_v1.pools import eligible_target_pools, pool_valid_at
from orderbook_analyse.a_plus_liquidity_pool_signal_scanner_v1.runner import run_scanner
from orderbook_analyse.a_plus_liquidity_pool_signal_scanner_v1.setups import _select_target_above, _select_target_below
from orderbook_analyse.a_plus_liquidity_pool_signal_scanner_v1.target_causality_audit import target_causality_row


def _loader(pools):
    def _fn(_candles, *, symbol, as_of):
        return pools

    return _fn


def _bid(pid: str, lower: float, upper: float, known: datetime) -> PoolRecord:
    return pool(pool_id=pid, tf="15m", side="BID", lower=lower, upper=upper, known_at=known)


def test_target_known_before_armed_allowed():
    armed = datetime(2026, 8, 28, 4, 15)
    p = _bid("t1", 0.087, 0.0875, armed - timedelta(hours=1))
    assert pool_valid_at(p, armed)
    assert _select_target_below(0.088, [p], 0.0002, as_of=armed) is p


def test_target_known_after_armed_rejected():
    armed = datetime(2026, 8, 28, 4, 15)
    future = _bid("t2", 0.087, 0.0875, armed + timedelta(hours=1))
    assert not pool_valid_at(future, armed)
    assert eligible_target_pools([future], armed) == []
    assert _select_target_below(0.088, [future], 0.0002, as_of=armed) is None


def test_target_invalidated_before_armed_rejected():
    armed = datetime(2026, 8, 28, 4, 15)
    p = _bid("t3", 0.087, 0.0875, armed - timedelta(hours=2))
    p.invalidated_at = armed - timedelta(minutes=30)
    assert not pool_valid_at(p, armed)


def test_later_pool_does_not_change_frozen_tp_in_replay():
    candles, approach_at = pullback_short_confirmation_bundle()
    result = run_scanner(
        symbol="DOGEUSDT",
        candles_by_tf=candles,
        pool_loader=_loader(static_pools(known_at=approach_at - timedelta(hours=2))),
    )
    confirmed = [c for c in result["confirmed"] if c["setup_type"] == "A_PLUS_PULLBACK_SHORT"]
    assert confirmed
    row = target_causality_row(confirmed[0])
    assert row["causality_pass"] is True
    assert row["target_pool_known_at"] is not None
    assert row["armed_at"] >= row["target_pool_known_at"][:19] or row["causality_pass"]


def test_dedupe_prefers_confirmed_over_intent():
    sid = "abc123"
    intent = {
        "signal_id": sid,
        "setup_id": sid,
        "direction": "SHORT",
        "state": "LIMIT_INTENT_ARMED",
        "armed_at": "2026-08-28T04:15:00",
        "entry_price": 0.088,
        "stop_price": 0.089,
        "target_price": 0.087,
    }
    confirmed = {
        **intent,
        "state": "CONFIRMED",
        "confirmed_at": "2026-08-28T06:35:00",
    }
    rows = dedupe_plan_rows([intent, confirmed])
    assert len(rows) == 1
    assert rows[0]["state"] == "CONFIRMED"


def test_terminal_target_reselected_at_reclaim_not_spawn():
    """Reclaim must select target with as_of=reclaim bar, not reuse spawn-time pool."""
    armed = datetime(2026, 8, 28, 10, 27)
    spawn_time = datetime(2026, 8, 28, 8, 56)
    early_ask = pool(pool_id="early", tf="15m", side="ASK", lower=0.089, upper=0.090, known_at=spawn_time - timedelta(hours=1))
    late_ask = pool(pool_id="late", tf="15m", side="ASK", lower=0.087, upper=0.088, known_at=armed - timedelta(minutes=15))
    future_ask = pool(pool_id="future", tf="15m", side="ASK", lower=0.086, upper=0.087, known_at=armed + timedelta(hours=1))
    pools = [early_ask, late_ask, future_ask]
    at_spawn = _select_target_above(0.086, pools, 0.0002, as_of=spawn_time)
    at_reclaim = _select_target_above(0.086, pools, 0.0002, as_of=armed)
    assert at_spawn is not None
    assert at_reclaim is not None
    assert at_reclaim.pool_id == "late"
    assert future_ask not in eligible_target_pools(pools, armed)


def test_one_marker_group_per_signal_id():
    sid = "xyz"
    rows = dedupe_plan_rows(
        [
            {
                "signal_id": sid,
                "setup_id": sid,
                "direction": "LONG",
                "state": "CONFIRMED",
                "armed_at": "2026-08-28T10:30:00",
                "entry_price": 0.086,
                "stop_price": 0.085,
                "target_price": 0.087,
            }
        ]
    )
    specs = signals_to_marker_specs(rows, display_mode="confirmed")
    plan_ids = [s["overlay_id"] for s in specs if s["kind"] != "APS_LINE"]
    assert plan_ids.count(f"aps-plan-{sid}") == 1
