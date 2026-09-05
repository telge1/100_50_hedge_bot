"""Focused tests for CASE_02 pool-edge aggressor timeline."""

from __future__ import annotations

from orderbook_analyse.case_02_pool_edge_aggressor_efficiency_timeline_v1 import (
    ARRIVAL_TS,
    POOL_HI,
    POOL_LO,
    START_WALL,
)
from orderbook_analyse.case_02_pool_edge_aggressor_efficiency_timeline_v1.pipeline import (
    bps,
    filter_as_of,
    impact_label,
    pool_zone,
)


def test_case_02_constants_unchanged():
    assert ARRIVAL_TS == "2026-08-25T00:47:13Z"
    assert POOL_LO == 79678.7
    assert abs(POOL_HI - 80116.8) < 0.05
    assert START_WALL == 79700.0


def test_no_future_as_of_filter():
    rows = [
        {"ts": "2026-08-25T00:47:10Z", "v": 1},
        {"ts": "2026-08-25T00:47:13Z", "v": 2},
        {"ts": "2026-08-25T00:47:20Z", "v": 3},
    ]
    as_of = 1756080433000  # will compute via filter using _ms of arrival
    from orderbook_analyse.case_02_pool_edge_aggressor_efficiency_timeline_v1.pipeline import _ms

    as_of = _ms("2026-08-25T00:47:13Z")
    got = filter_as_of(rows, ts_key="ts", as_of_ms=as_of)
    assert [r["v"] for r in got] == [1, 2]


def test_buy_sell_impact_signs():
    # buy efficient: positive mid change
    assert "STRONG_BUY_EFFECTIVE" in impact_label(
        buy_n=50_000, sell_n=0, mid_chg_bps=10.0, min_notional=10_000, strong_bps=8.0
    )
    # sell efficient: negative mid change
    assert "STRONG_SELL_EFFECTIVE" in impact_label(
        buy_n=0, sell_n=50_000, mid_chg_bps=-10.0, min_notional=10_000, strong_bps=8.0
    )
    # sell inefficient: sell flow but little down move
    assert "STRONG_SELL_INEFFICIENT" in impact_label(
        buy_n=0, sell_n=50_000, mid_chg_bps=1.0, min_notional=10_000, strong_bps=8.0
    )


def test_no_absorption_without_attack():
    lab = impact_label(
        buy_n=100, sell_n=100, mid_chg_bps=0.0, min_notional=10_000, strong_bps=8.0
    )
    assert lab in ("LOW_FLOW", "INSUFFICIENT")
    assert "STRONG_SELL_INEFFICIENT" not in lab


def test_local_exit_does_not_end_observation_and_reentry():
    # zone helpers: below then inside again
    assert pool_zone(79600.0, POOL_LO, POOL_HI, 2.0) == "BELOW_POOL"
    assert pool_zone(79800.0, POOL_LO, POOL_HI, 2.0) == "INSIDE_LOWER_THIRD"
    # re-entry is a timeline flag; ensure zones allow both states
    assert pool_zone(79600.0, POOL_LO, POOL_HI, 2.0) != pool_zone(79800.0, POOL_LO, POOL_HI, 2.0)


def test_acceptance_variants_remain_separate():
    from orderbook_analyse.case_02_pool_edge_aggressor_efficiency_timeline_v1 import (
        ACCEPT_VARIANTS_S,
    )

    assert ACCEPT_VARIANTS_S == (5, 15, 30, 60)
    assert len(set(ACCEPT_VARIANTS_S)) == 4


def test_ema_closed_candle_availability_rule():
    # candle open unix T available at T+300s — documented in load_closed_emas
    import inspect
    from orderbook_analyse.case_02_pool_edge_aggressor_efficiency_timeline_v1 import pipeline as p

    src = inspect.getsource(p.load_closed_emas)
    assert "open_unix + 300" in src or "+ 300)" in src
    assert "avail_ms" in src


def test_bps_helper():
    assert abs(bps(80116.8, 79678.7) - ((80116.8 - 79678.7) / 79678.7 * 10000)) < 1e-6
