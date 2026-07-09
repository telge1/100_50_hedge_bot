from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import pytest

from research.backtests.long_gap_reduction import (
    compute_trigger_price,
    LongGapReductionConfig,
    simulate_long_gap_reduction,
)


@dataclass
class _Candle:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float


def _mk_candles(prices: list[float]) -> list[_Candle]:
    ts0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    candles: list[_Candle] = []
    for i, p in enumerate(prices):
        candles.append(
            _Candle(
                timestamp=ts0,
                open=p,
                high=p,
                low=p,
                close=p,
            )
        )
    return candles


def test_long_reduce_realizes_correct_loss_and_keeps_avg_constant() -> None:
    # Simple synthetic path: price drops steadily in 1% steps.
    prices = [100.0, 99.0, 98.01, 97.0299, 96.059601]  # each ~1% down
    candles = _mk_candles(prices)

    events, summary = simulate_long_gap_reduction(
        candles=candles,
        start_local_candle_index=0,
        absolute_start_index=0,
        initial_long_qty=10.0,
        initial_short_qty=5.0,
        long_avg=100.0,
        short_avg=100.0,
        reference_price=100.0,
        base_main_realized_pnl=0.0,
        cfg=LongGapReductionConfig(step_trigger_pct=1.0, num_steps=4, fee_rate=None),
    )

    # We expect at least one LONG_REDUCE event.
    reduce_events = [e for e in events if e["event_type"] == "LONG_REDUCE"]
    assert reduce_events, "expected at least one LONG_REDUCE event"

    ev = reduce_events[0]
    # Gap is 10 - 5 = 5; each of 4 steps should plan to close 5/4 = 1.25.
    assert ev["reduced_qty"] == pytest.approx(1.25)
    # Execution price should be 1% below 100.0, i.e. 99.0
    assert ev["execution_price"] == pytest.approx(99.0)
    # Gross realized PnL: (99 - 100) * 1.25 = -1.25
    assert ev["gross_realized_pnl_event"] == pytest.approx(-1.25)

    # Long average entry must remain constant.
    assert ev["long_avg"] == pytest.approx(100.0)


def test_long_reduce_never_lets_long_drop_below_short() -> None:
    # Start with long just slightly above short.
    prices = [100.0, 99.0, 98.01]
    candles = _mk_candles(prices)

    events, summary = simulate_long_gap_reduction(
        candles=candles,
        start_local_candle_index=0,
        absolute_start_index=0,
        initial_long_qty=6.0,
        initial_short_qty=5.0,
        long_avg=100.0,
        short_avg=100.0,
        reference_price=100.0,
        base_main_realized_pnl=0.0,
        cfg=LongGapReductionConfig(step_trigger_pct=1.0, num_steps=4, fee_rate=None),
    )

    reduce_events = [e for e in events if e["event_type"] == "LONG_REDUCE"]
    assert reduce_events, "expected one LONG_REDUCE event"
    ev = reduce_events[0]
    # Planned per-step reduction is initial_gap/4 = (6-5)/4 = 0.25.
    assert ev["reduced_qty"] == pytest.approx(0.25)
    # Long is allowed to remain above short but must never dip below.
    assert summary["final_long_qty"] >= summary["final_short_qty"]


def test_trigger_prices_follow_exponential_1pct_steps() -> None:
    reference = 100.0
    step_pct = 1.0
    expected = [
        reference * (0.99**1),
        reference * (0.99**2),
        reference * (0.99**3),
        reference * (0.99**4),
    ]
    actual = [
        compute_trigger_price(
            reference_price=reference,
            step_index=i,
            step_trigger_pct=step_pct,
        )
        for i in range(1, 5)
    ]
    for act, exp in zip(actual, expected):
        assert act == pytest.approx(exp)

