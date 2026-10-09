"""Phase 11P fixtures: cross-line classification, eviction, rolling trim, reappearance."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from data.models import Candle
from indicators.live_micro_lld_v2.availability import bar_close
from indicators.live_micro_lld_v2.config import LiveMicroLldV2Config
from indicators.live_micro_lld_v2.engine import run_live_micro_lld_v2
from indicators.live_micro_lld_v2.models import LiquidityPoolV2
from research_charts.live_micro_lld_v2_ch_test.phase11p_catalog_resolution import (
    classify_missing_catalog,
)
from research_charts.live_micro_lld_v2_ch_test.remaining_pool_projection_v1 import (
    PoolConsumptionLedger,
    RemainingPoolProjectionError,
    project_pool_v1,
)
import pytest


def _candle(i: int, high: float, low: float) -> Candle:
    return Candle(
        symbol="TESTUSDT",
        timeframe="5m",
        timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=5 * i),
        open=(high + low) / 2,
        high=high,
        low=low,
        close=(high + low) / 2,
        volume=1000.0 if i % 6 == 3 else 100.0,
    )


def _cfg(**kwargs) -> LiveMicroLldV2Config:
    base = dict(
        manual_override=True,
        manual_swing_len=2,
        manual_volume_lookback=10,
        manual_volume_percentile=50,
        manual_min_strength=0.0,
        manual_zone_size=0.2,
        show_micro_pools=False,
        show_reclaim_pools=False,
        show_normal_pools=True,
    )
    base.update(kwargs)
    return LiveMicroLldV2Config(**base)


def test_cross_line_classification():
    assert (
        classify_missing_catalog(
            phase10_line_rows=0,
            snapshot_id_rows=270,
            member_rows=270,
            identity_match=True,
        )
        == "CROSS_LINE_FILTER"
    )
    assert (
        classify_missing_catalog(
            phase10_line_rows=0,
            snapshot_id_rows=0,
            member_rows=270,
            identity_match=True,
        )
        == "REAL_DATA_GAP"
    )
    assert (
        classify_missing_catalog(
            phase10_line_rows=0,
            snapshot_id_rows=10,
            member_rows=270,
            identity_match=True,
        )
        == "UNKNOWN"
    )


def test_synthetic_eviction_hides_remaining():
    candles = [_candle(i, 10 + (i % 6), 9 + (i % 6)) for i in range(80)]
    result = run_live_micro_lld_v2(candles, _cfg(box_amount=2))
    evicted = [p for p in result.pools if (not p.active) and p.break_at is None and p.invalidated_at]
    assert evicted
    dc = bar_close(candles[-1].timestamp, "5m")
    proj = project_pool_v1(evicted[0], candles, decision_close=dc, candle_input_reference="evict")
    assert proj.engine_active is False
    assert proj.remaining_visible is False
    assert evicted[0].strength == proj.strength


def test_synthetic_rolling_suffix_drops_early_pools():
    candles = [_candle(i, 10 + (i % 6), 9 + (i % 6)) for i in range(80)]
    cfg = _cfg(box_amount=50)
    full = run_live_micro_lld_v2(candles, cfg)
    suffix = run_live_micro_lld_v2(candles[40:], cfg)
    early = {p.pool_id for p in full.pools} - {p.pool_id for p in suffix.pools}
    assert early
    visible = {p.pool_id for p in suffix.pools if p.active}
    assert early.isdisjoint(visible)


def test_synthetic_reappearance_does_not_restore_consumption():
    t0 = datetime(2026, 2, 1, tzinfo=timezone.utc)
    pool = LiquidityPoolV2(
        pool_id="lldv2:DOGEUSDT:5m:normal:upper:1",
        pool_type="normal",
        symbol="DOGEUSDT",
        timeframe="5m",
        side="upper",
        source_timestamp=t0,
        created_timestamp=t0,
        known_at=bar_close(t0, "5m"),
        top_price=110,
        bottom_price=100,
        strength=5,
        strength_class="Strong",
        active=True,
    )
    c1 = Candle(
        symbol="DOGEUSDT",
        timeframe="5m",
        timestamp=t0 + timedelta(minutes=5),
        open=104,
        high=106,
        low=103,
        close=104,
        volume=1,
    )
    dc = bar_close(c1.timestamp, "5m")
    first = project_pool_v1(pool, [c1], decision_close=dc, candle_input_reference="re")
    pool.active = False
    hidden = project_pool_v1(pool, [c1], decision_close=dc, candle_input_reference="re")
    assert hidden.remaining_fraction == first.remaining_fraction
    assert hidden.remaining_visible is False
    pool.active = True
    again = project_pool_v1(pool, [c1], decision_close=dc, candle_input_reference="re")
    assert again.remaining_bottom == first.remaining_bottom
    assert again.remaining_fraction == first.remaining_fraction
    ledger = PoolConsumptionLedger()
    ledger.record(first)
    other = LiquidityPoolV2(
        pool_id=pool.pool_id,
        pool_type="normal",
        symbol="DOGEUSDT",
        timeframe="5m",
        side="upper",
        source_timestamp=t0,
        created_timestamp=t0,
        known_at=pool.known_at,
        top_price=111,
        bottom_price=100,
        strength=9,
        strength_class="Extreme",
        active=True,
    )
    with pytest.raises(RemainingPoolProjectionError):
        ledger.prior_for(other)
