"""V2 contract tests for A+ pool signal scanner."""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from orderbook_analyse.a_plus_liquidity_pool_signal_scanner_v1.config import ENTRY_FRACTION_FROM_LOWER
from orderbook_analyse.a_plus_liquidity_pool_signal_scanner_v1.fixtures import pool, static_pools
from orderbook_analyse.a_plus_liquidity_pool_signal_scanner_v1.models import PoolRecord
from orderbook_analyse.a_plus_liquidity_pool_signal_scanner_v1.pool_selection import (
    group_overlapping_pools,
    pullback_limit_price,
    select_pullback_entry_pools,
)
from orderbook_analyse.a_plus_liquidity_pool_signal_scanner_v1.setups import (
    atr_available,
    bearish_5m_structural,
    bullish_5m_structural,
    pools_overlap,
)


def _ask(lower: float, upper: float, pid: str, *, n: int = 1) -> PoolRecord:
    return pool(pool_id=pid, tf="15m", side="ASK", lower=lower, upper=upper, known_at=datetime(2026, 8, 28, 3, 30), n=n)


def test_individual_pool_not_displaced_by_distant_cluster():
    approach = datetime(2026, 8, 28, 6, 0)
    individual = _ask(0.08803, 0.08830, "lld:DOGEUSDT:15m:upper:1787886900")
    cluster = _ask(0.08977, 0.08909, "lldc:DOGEUSDT:15m:upper:cluster", n=4)
    selected = select_pullback_entry_pools(
        [individual, cluster], price=0.0881, approach_at=approach, direction="SHORT", atr=0.0001
    )
    assert selected
    assert selected[0][0].pool_id == individual.pool_id


def test_overlapping_pools_single_episode():
    p1 = _ask(0.0880, 0.0884, "lld:a")
    p2 = _ask(0.0881, 0.08835, "lld:b")
    groups = group_overlapping_pools([p1, p2])
    assert len(groups) == 1


def test_pullback_short_limit_at_60_percent():
    p = _ask(0.08803, 0.08830, "lld:x")
    px = pullback_limit_price(p, direction="SHORT")
    expected = 0.08803 + ENTRY_FRACTION_FROM_LOWER * (0.08830 - 0.08803)
    assert px == pytest.approx(expected)


def test_pullback_long_limit_mirrored():
    p = pool(pool_id="lld:y", tf="15m", side="BID", lower=0.08803, upper=0.08830, known_at=datetime(2026, 8, 28))
    px = pullback_limit_price(p, direction="LONG")
    expected = 0.08830 - ENTRY_FRACTION_FROM_LOWER * (0.08830 - 0.08803)
    assert px == pytest.approx(expected)


def test_bearish_structure_no_full_ema_stack_required():
    entry = _ask(0.08803, 0.08830, "lld:z")
    row = pd.Series(
        {
            "close": 0.0881,
            "ema_9": 0.08815,
            "ema_20": 0.08820,
            "ema_59": 0.08825,
            "ema_9_slope_1": -0.00001,
            "ema_20_slope_1": -0.00001,
            "prior_swing_high": 0.0884,
        }
    )
    prev = pd.Series({"prior_swing_high": 0.0886})
    assert bearish_5m_structural(row, entry, prev)


def test_bullish_structure_mirrored():
    entry = pool(pool_id="b", tf="15m", side="BID", lower=0.08803, upper=0.08830, known_at=datetime(2026, 8, 28))
    row = pd.Series(
        {
            "close": 0.0882,
            "ema_9": 0.08815,
            "ema_20": 0.08810,
            "ema_59": 0.08805,
            "ema_9_slope_1": 0.00001,
            "ema_20_slope_1": 0.00001,
            "prior_swing_low": 0.0879,
        }
    )
    prev = pd.Series({"prior_swing_low": 0.0877})
    assert bullish_5m_structural(row, entry, prev)


def test_atr_zero_unavailable():
    assert not atr_available(0.0)
    assert not atr_available(float("nan"))


def test_pools_overlap():
    a = _ask(0.0880, 0.0884, "a")
    b = _ask(0.0882, 0.0885, "b")
    assert pools_overlap(a, b)
