"""Phase 11N — RemainingPoolProjectionV1 isolated POC tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from data.models import Candle
from indicators.live_micro_lld_v2.availability import bar_close
from indicators.live_micro_lld_v2.models import LiquidityPoolV2
from research_charts.live_micro_lld_v2_ch_test.remaining_pool_projection_v1 import (
    PoolConsumptionLedger,
    RemainingPoolProjectionError,
    apply_strength_class_filter,
    build_fixed_segments,
    compute_remaining_bounds_lower,
    compute_remaining_bounds_upper,
    project_pool_v1,
)
from research_charts.live_micro_lld_v2_ch_test.remaining_pool_projection_v1_config import (
    ALT_SEGMENT_COUNTS,
    STRENGTH_CLASS_COLORS,
)


def _pool(
    *,
    side: str,
    bottom: float,
    top: float,
    known_open: datetime,
    tf: str = "5m",
    active: bool = True,
    strength_class: str = "Strong",
) -> LiquidityPoolV2:
    known_at = bar_close(known_open, tf)
    anchor = int(known_open.timestamp())
    return LiquidityPoolV2(
        pool_id=f"lldv2:DOGEUSDT:{tf}:normal:{side}:{anchor}",
        pool_type="normal",
        symbol="DOGEUSDT",
        timeframe=tf,
        side=side,
        source_timestamp=known_open,
        created_timestamp=known_open,
        known_at=known_at,
        top_price=top,
        bottom_price=bottom,
        strength=5.0,
        strength_class=strength_class,
        active=active,
    )


def _c5(open_dt: datetime, high: float, low: float, close: float | None = None) -> Candle:
    c = close if close is not None else (high + low) / 2
    return Candle(
        timestamp=open_dt,
        open=c,
        high=high,
        low=low,
        close=c,
        volume=1.0,
        symbol="DOGEUSDT",
        timeframe="5m",
    )


def test_upper_untouched_partial_full_and_retrace():
    t0 = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    pool = _pool(side="upper", bottom=100, top=110, known_open=t0)
    candles = [
        _c5(t0 + timedelta(minutes=5), 99, 98),
        _c5(t0 + timedelta(minutes=10), 104, 103),
        _c5(t0 + timedelta(minutes=15), 107, 106),
        _c5(t0 + timedelta(minutes=20), 102, 101),
    ]
    p1 = project_pool_v1(
        pool,
        candles[:1],
        decision_close=bar_close(t0 + timedelta(minutes=5), "5m"),
        candle_input_reference="t",
    )
    assert p1.remaining_bottom == 100 and p1.remaining_fraction == 1.0
    p2 = project_pool_v1(
        pool,
        candles[:2],
        decision_close=bar_close(t0 + timedelta(minutes=10), "5m"),
        candle_input_reference="t",
    )
    assert p2.remaining_bottom == 104
    p3 = project_pool_v1(
        pool,
        candles[:3],
        decision_close=bar_close(t0 + timedelta(minutes=15), "5m"),
        candle_input_reference="t",
    )
    assert p3.remaining_bottom == 107
    p4 = project_pool_v1(
        pool,
        candles,
        decision_close=bar_close(t0 + timedelta(minutes=20), "5m"),
        candle_input_reference="t",
    )
    assert p4.remaining_bottom == 107
    full = project_pool_v1(
        pool,
        [_c5(t0 + timedelta(minutes=5), 110, 109)],
        decision_close=bar_close(t0 + timedelta(minutes=10), "5m"),
        candle_input_reference="t",
    )
    assert full.fully_traversed and full.remaining_fraction == 0.0


def test_lower_partial_and_full():
    t0 = datetime(2026, 1, 2, 0, 0, tzinfo=timezone.utc)
    pool = _pool(side="lower", bottom=90, top=100, known_open=t0)
    candles = [
        _c5(t0 + timedelta(minutes=5), 101, 96),
        _c5(t0 + timedelta(minutes=10), 95, 92),
        _c5(t0 + timedelta(minutes=15), 93, 90),
    ]
    p = project_pool_v1(
        pool,
        candles[:2],
        decision_close=bar_close(t0 + timedelta(minutes=10), "5m"),
        candle_input_reference="t",
    )
    assert p.remaining_top == 92
    full = project_pool_v1(
        pool,
        candles,
        decision_close=bar_close(t0 + timedelta(minutes=15), "5m"),
        candle_input_reference="t",
    )
    assert full.remaining_top == 90 and full.fully_traversed


def test_boundary_equalities():
    rt, rb, frac, full = compute_remaining_bounds_upper(100, 110, 100.0)
    assert rb == 100 and not full
    rt, rb, frac, full = compute_remaining_bounds_upper(100, 110, 110.0)
    assert full and frac == 0.0
    rt, rb, frac, full = compute_remaining_bounds_lower(90, 100, 100.0)
    assert rt == 100 and not full
    rt, rb, frac, full = compute_remaining_bounds_lower(90, 100, 90.0)
    assert full


def test_partial_segment_precision_and_fixed_boundaries():
    rem_b, rem_t = 104.3, 110.0
    for n in (10, 24, 32):
        segs = build_fixed_segments(
            original_bottom=100,
            original_top=110,
            remaining_bottom=rem_b,
            remaining_top=rem_t,
            strength_class="Strong",
            segment_count=n,
        )
        assert len(segs) == n
        assert segs[0].segment_bottom == 100.0
        assert segs[-1].segment_top == 110.0
    segs10 = build_fixed_segments(
        original_bottom=100,
        original_top=110,
        remaining_bottom=rem_b,
        remaining_top=rem_t,
        strength_class="Strong",
        segment_count=10,
    )
    partial = segs10[4]
    assert partial.segment_bottom == 104.0 and partial.segment_top == 105.0
    assert abs(partial.visible_bottom - 104.3) < 1e-9
    assert abs(partial.visible_fraction - 0.7) < 1e-9


def test_remaining_visible_rules():
    t0 = datetime(2026, 1, 3, 0, 0, tzinfo=timezone.utc)
    pool = _pool(side="upper", bottom=100, top=110, known_open=t0, active=True)
    candles = [_c5(t0 + timedelta(minutes=5), 105, 104)]
    dc = bar_close(t0 + timedelta(minutes=10), "5m")
    vis = project_pool_v1(pool, candles, decision_close=dc, candle_input_reference="t")
    assert vis.engine_active and vis.remaining_visible
    inactive = _pool(side="upper", bottom=100, top=110, known_open=t0, active=False)
    inv = project_pool_v1(inactive, candles, decision_close=dc, candle_input_reference="t")
    assert not inv.remaining_visible
    empty = project_pool_v1(
        pool,
        [_c5(t0 + timedelta(minutes=5), 110, 109)],
        decision_close=dc,
        candle_input_reference="t",
    )
    assert empty.engine_active and not empty.remaining_visible


def test_first_eligible_skips_confirmation_wick():
    t0 = datetime(2026, 1, 4, 0, 0, tzinfo=timezone.utc)
    pool = _pool(side="upper", bottom=100, top=110, known_open=t0)
    only_confirm = [_c5(t0, 108, 99)]
    dc = bar_close(t0, "5m")
    p = project_pool_v1(pool, only_confirm, decision_close=dc, candle_input_reference="t")
    assert p.penetration_high is None and p.remaining_fraction == 1.0


def test_monotonic_ledger_and_reappearance_bounds():
    t0 = datetime(2026, 1, 5, 0, 0, tzinfo=timezone.utc)
    pool = _pool(side="upper", bottom=100, top=110, known_open=t0)
    ledger = PoolConsumptionLedger()
    c1 = [_c5(t0 + timedelta(minutes=5), 104, 103)]
    dc1 = bar_close(t0 + timedelta(minutes=10), "5m")
    p1 = project_pool_v1(
        pool,
        c1,
        decision_close=dc1,
        candle_input_reference="t",
        prior_penetration_high=ledger.prior_for(pool)[0],
    )
    ledger.record(p1)
    c2 = c1 + [_c5(t0 + timedelta(minutes=10), 102, 101)]
    dc2 = bar_close(t0 + timedelta(minutes=15), "5m")
    p2 = project_pool_v1(
        pool,
        c2,
        decision_close=dc2,
        candle_input_reference="t",
        prior_penetration_high=ledger.prior_for(pool)[0],
    )
    assert p2.remaining_bottom == 104
    bad = LiquidityPoolV2(
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
        strength=5.0,
        strength_class="Strong",
        active=True,
    )
    with pytest.raises(RemainingPoolProjectionError):
        ledger.prior_for(bad)


def test_worker_order_invariance_stateless():
    t0 = datetime(2026, 1, 6, 0, 0, tzinfo=timezone.utc)
    pool = _pool(side="lower", bottom=90, top=100, known_open=t0)
    candles = [_c5(t0 + timedelta(minutes=5), 99, 95)]
    dc = bar_close(t0 + timedelta(minutes=10), "5m")
    a = project_pool_v1(pool, candles, decision_close=dc, candle_input_reference="x")
    b = project_pool_v1(pool, candles, decision_close=dc, candle_input_reference="x")
    assert a.remaining_top == b.remaining_top and a.remaining_fraction == b.remaining_fraction


def test_strength_mapping_and_filter():
    for cls, color in STRENGTH_CLASS_COLORS.items():
        segs = build_fixed_segments(
            original_bottom=1,
            original_top=2,
            remaining_bottom=1,
            remaining_top=2,
            strength_class=cls,
            segment_count=4,
        )
        assert segs[0].display_color == color
    t0 = datetime(2026, 1, 7, 0, 0, tzinfo=timezone.utc)
    pools = [
        project_pool_v1(
            _pool(side="upper", bottom=1, top=2, known_open=t0, strength_class="Weak"),
            [],
            decision_close=bar_close(t0, "5m"),
            candle_input_reference="t",
        ),
        project_pool_v1(
            _pool(side="upper", bottom=3, top=4, known_open=t0, strength_class="Extreme"),
            [],
            decision_close=bar_close(t0, "5m"),
            candle_input_reference="t",
        ),
    ]
    filtered = apply_strength_class_filter(pools, frozenset({"Extreme"}))
    assert len(filtered) == 1 and filtered[0].strength_class == "Extreme"


def test_segment_counts_config():
    assert 10 in ALT_SEGMENT_COUNTS and 32 in ALT_SEGMENT_COUNTS
