"""Tests for A_PLUS_NESTED_ASK_POOL_EDGE_SHORT_V1 (no DOGE hardcoding in logic)."""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from orderbook_analyse.a_plus_liquidity_pool_signal_scanner_v1.models import PoolRecord
from orderbook_analyse.a_plus_nested_ask_pool_edge_short_v1.config import (
    MAX_STOP_DISTANCE_PCT,
    REFERENCE_ENTRY_APPROX,
    SETUP_TYPE,
)
from orderbook_analyse.a_plus_nested_ask_pool_edge_short_v1.fills_outcomes import (
    detect_short_limit_fill,
    evaluate_target_variants,
    pnl_from_short,
)
from orderbook_analyse.a_plus_nested_ask_pool_edge_short_v1.geometry import (
    pools_overlap,
    select_nested_ask_structure,
    structural_stop,
    upper_gap_metrics,
)


def _pool(
    pool_id: str,
    tf: str,
    lo: float,
    hi: float,
    *,
    available: datetime,
    invalidated: datetime | None = None,
    side: str = "ASK",
) -> PoolRecord:
    return PoolRecord(
        pool_id=pool_id,
        symbol="TESTUSDT",
        timeframe=tf,
        side=side,
        lower_edge=lo,
        upper_edge=hi,
        midpoint=(lo + hi) / 2,
        component_count=1,
        strength=1.0,
        known_at=available,
        available_at=available,
        invalidated_at=invalidated,
        source_timestamp=available,
        source_bar_end=available,
    )


def test_setup_type_constant():
    assert SETUP_TYPE == "A_PLUS_NESTED_ASK_POOL_EDGE_SHORT_V1"


def test_pool_only_after_available_at():
    p = _pool("a", "1m", 1.0, 1.1, available=datetime(2026, 1, 1, 12, 0))
    assert not p.is_active_at(datetime(2026, 1, 1, 11, 59))
    assert p.is_active_at(datetime(2026, 1, 1, 12, 0))


def test_invalidated_pool_excluded():
    p = _pool(
        "a",
        "1m",
        1.0,
        1.1,
        available=datetime(2026, 1, 1, 12, 0),
        invalidated=datetime(2026, 1, 1, 12, 30),
    )
    assert p.is_active_at(datetime(2026, 1, 1, 12, 29))
    assert not p.is_active_at(datetime(2026, 1, 1, 12, 30))


def test_correct_15m_5m_1m_nesting_and_child_low_entry():
    as_of = datetime(2026, 1, 1, 12, 0)
    p15 = _pool("15", "15m", 100.0, 102.0, available=as_of - timedelta(hours=1))
    p5 = _pool("5", "5m", 100.5, 101.5, available=as_of - timedelta(minutes=30))
    c1 = _pool("1", "1m", 100.8, 101.2, available=as_of - timedelta(minutes=5))
    other = _pool("x", "1m", 103.0, 104.0, available=as_of)  # outside nest
    st = select_nested_ask_structure(
        asks_15m=[p15],
        asks_5m=[p5],
        asks_1m=[c1, other],
        price=100.0,
        as_of=as_of,
    )
    assert st is not None
    assert st.child_1m.pool_id == "1"
    assert st.child_1m.lower_edge == 100.8
    assert pools_overlap(c1, p5) and pools_overlap(c1, p15)


def test_no_merge_of_spatially_separate_pools():
    as_of = datetime(2026, 1, 1, 12, 0)
    p15 = _pool("15", "15m", 100.0, 101.0, available=as_of)
    p5 = _pool("5", "5m", 102.0, 103.0, available=as_of)  # no overlap
    c1 = _pool("1", "1m", 100.2, 100.5, available=as_of)
    st = select_nested_ask_structure(
        asks_15m=[p15], asks_5m=[p5], asks_1m=[c1], price=99.0, as_of=as_of
    )
    assert st is None


def test_stop_above_highest_pool_edge_and_stop_too_wide():
    as_of = datetime(2026, 1, 1, 12, 0)
    p15 = _pool("15", "15m", 100.0, 102.5, available=as_of)
    p5 = _pool("5", "5m", 100.0, 101.0, available=as_of)
    c1 = _pool("1", "1m", 100.1, 100.5, available=as_of)
    st = select_nested_ask_structure(
        asks_15m=[p15], asks_5m=[p5], asks_1m=[c1], price=99.5, as_of=as_of
    )
    assert st is not None
    info = structural_stop(structure=st, atr=0.2, symbol="BTCUSDT")
    assert info["stop_reference"] == 102.5
    assert info["stop_loss"] > 102.5
    # distance ~2.4% → too wide
    assert info["stop_too_wide"] is True
    assert info["stop_distance_pct"] > MAX_STOP_DISTANCE_PCT


def test_next_ask_gap_detected():
    as_of = datetime(2026, 1, 1, 12, 0)
    parent_high = 101.0
    near = _pool("n", "15m", 101.001, 101.1, available=as_of)  # may be too close depending on tick
    far = _pool("f", "15m", 102.0, 102.5, available=as_of)
    m = upper_gap_metrics(
        parent_zone_high=parent_high,
        asks=[near, far],
        as_of=as_of,
        atr=0.5,
        symbol="BTCUSDT",
    )
    assert m["next_ask_pool_low"] is not None
    assert m["upper_gap_atr"] is not None


def test_no_same_bar_fill_when_birth_bar_touched():
    # child available at 12:05 (= close of 12:04 bar). That bar high already touched entry.
    # Later bars never re-touch → SAME_BAR_SEQUENCE_AMBIGUOUS
    rows = []
    t0 = datetime(2026, 1, 1, 12, 0)
    for i in range(10):
        ot = t0 + timedelta(minutes=i)
        high = 100.8 if i == 4 else 100.5  # 12:04 bar touches 100.8
        rows.append({"open_time": ot, "open": 100.0, "high": high, "low": 99.5, "close": 100.2, "volume": 1})
    df = pd.DataFrame(rows)
    avail = datetime(2026, 1, 1, 12, 5)
    fill = detect_short_limit_fill(
        df,
        entry_price=100.8,
        order_active_at=avail,
        child_available_at=avail,
    )
    assert fill.status == "SAME_BAR_SEQUENCE_AMBIGUOUS"
    assert fill.fill_at is None


def test_fill_on_later_bar_after_available():
    rows = []
    t0 = datetime(2026, 1, 1, 12, 0)
    for i in range(10):
        ot = t0 + timedelta(minutes=i)
        high = 100.9 if i == 7 else 100.5
        rows.append({"open_time": ot, "open": 100.0, "high": high, "low": 99.5, "close": 100.2, "volume": 1})
    df = pd.DataFrame(rows)
    avail = datetime(2026, 1, 1, 12, 5)
    fill = detect_short_limit_fill(
        df,
        entry_price=100.8,
        order_active_at=avail,
        child_available_at=avail,
    )
    assert fill.status == "FILLED"
    assert fill.fill_at == datetime(2026, 1, 1, 12, 8)


def test_only_causal_bids_as_targets_and_tp_sl_ambiguity():
    rows = []
    t0 = datetime(2026, 1, 1, 13, 0)
    # first bar after fill hits both SL and TP
    rows.append({"open_time": t0, "open": 100.5, "high": 101.5, "low": 99.0, "close": 100.0, "volume": 1})
    for i in range(1, 5):
        rows.append(
            {
                "open_time": t0 + timedelta(minutes=i),
                "open": 100.0,
                "high": 100.2,
                "low": 99.8,
                "close": 100.0,
                "volume": 1,
            }
        )
    df = pd.DataFrame(rows)
    bid_info = {
        "bid_pools": [
            {"pool_id": "b1", "timeframe": "15m", "lower_edge": 98.5, "upper_edge": 99.5, "midpoint": 99.0, "available_at": "2026-01-01T10:00:00"}
        ]
    }
    outs = evaluate_target_variants(
        df,
        fill_at=t0,
        entry=100.5,
        stop=101.2,
        bid_info=bid_info,
    )
    a = next(o for o in outs if o["target_variant"] == "A_first_bid_near_edge")
    assert a["result"] == "AMBIGUOUS"


def test_fee_calculation():
    pnl = pnl_from_short(entry=100.0, exit_price=99.0, result="TP_FIRST", stop=101.0, target=99.0, cost_pct=0.15)
    assert abs(pnl["gross_pnl_pct"] - 1.0) < 1e-9
    assert abs(pnl["net_pnl_pct"] - 0.85) < 1e-9


def test_reference_approx_not_imported_into_geometry_module():
    import orderbook_analyse.a_plus_nested_ask_pool_edge_short_v1.geometry as geo
    import inspect

    src = inspect.getsource(geo)
    assert "0.087918" not in src
    assert str(REFERENCE_ENTRY_APPROX) not in src


def test_deterministic_selection_tiebreak():
    as_of = datetime(2026, 1, 1, 12, 0)
    p15 = _pool("15", "15m", 100.0, 102.0, available=as_of)
    p5 = _pool("5", "5m", 100.0, 102.0, available=as_of)
    c_near = _pool("near", "1m", 100.5, 101.0, available=as_of - timedelta(minutes=10))
    c_far = _pool("far", "1m", 101.0, 101.5, available=as_of - timedelta(minutes=10))
    st = select_nested_ask_structure(
        asks_15m=[p15], asks_5m=[p5], asks_1m=[c_far, c_near], price=100.0, as_of=as_of
    )
    assert st is not None
    assert st.child_1m.pool_id == "near"
