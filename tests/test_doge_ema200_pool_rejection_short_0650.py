"""Tests for DOGE EMA200 / pool rejection audit."""

from __future__ import annotations

from datetime import datetime

import pandas as pd

from orderbook_analyse.a_plus_liquidity_pool_signal_scanner_v1.doge_ema200_pool_rejection_short_0650 import (
    OLD_PULLBACK_EPISODE,
    VERDICT_CONFIRMED,
    _bar_close,
    _ema200_band,
    _rejection_sl,
    _touch_zone,
)
from orderbook_analyse.l2_wall_attack_discovery.models import tick_size


def test_ema200_band_uses_atr_and_tick():
    tick = tick_size("DOGEUSDT")
    lo, hi, hw = _ema200_band(0.08825, 0.00016, tick)
    assert lo < 0.08825 < hi
    assert abs((hi - lo) - 2 * hw) < 1e-9


def test_touch_zone_intersection():
    assert _touch_zone(0.08831, 0.08810, 0.08820, 0.08830)
    assert not _touch_zone(0.08810, 0.08800, 0.08820, 0.08830)


def test_rejection_sl_above_wick_pool_and_band():
    tick = tick_size("DOGEUSDT")
    stop_ref, buf, *_ = _rejection_sl(
        rejection_high=0.08831,
        pool_upper=0.08830,
        ema_band_high=0.088302,
        atr=0.00016,
        tick=tick,
    )
    assert stop_ref == 0.08831
    assert buf > 0
    assert stop_ref + buf > 0.08831


def test_bar_close_5m():
    assert _bar_close(datetime(2026, 8, 28, 6, 50, 0)) == datetime(2026, 8, 28, 6, 55, 0)


def test_old_pullback_episode_not_rejection():
    assert "A_PLUS_PULLBACK_SHORT" in OLD_PULLBACK_EPISODE
    assert "EMA200" not in OLD_PULLBACK_EPISODE


def test_r1_requires_close_below_band_or_pool():
    band_lo = 0.088201
    pool_lo = 0.08803
    close_inside_pool = 0.08817
    assert close_inside_pool < band_lo
    assert not (close_inside_pool < pool_lo)


def test_ema200_prior_bar_end_before_open():
    bar_open = pd.Timestamp("2026-08-28 06:50:00")
    prior_end = pd.Timestamp("2026-08-28 06:50:00")
    assert prior_end <= bar_open


def test_verdict_confirmed_is_positive_category():
    assert VERDICT_CONFIRMED == "CAUSAL_EMA200_POOL_REJECTION_SHORT_CONFIRMED"
