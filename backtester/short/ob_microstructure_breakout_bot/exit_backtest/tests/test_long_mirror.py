"""Long mirror of TP selection, failure order, and regime permission."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ob_microstructure_breakout_bot.exit_backtest.cluster_mass import PoolSnap
from ob_microstructure_breakout_bot.exit_backtest.pool_bounce_long_backtest import (
    select_tp_upper_pool,
    simulate_long_bounce_trade,
)
import ob_microstructure_breakout_bot.exit_backtest.pool_bounce_long_backtest as long_bt

_ROOT = Path(__file__).resolve().parents[5]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from bot.forward_test.failure_exit import long_failure_ready
from bot.forward_test.regime import allows_long, allows_short


class _Bar:
    def __init__(self, ts: datetime, high: float, low: float, close: float) -> None:
        self.ts = ts
        self.high = high
        self.low = low
        self.close = close


def test_select_tp_skips_pools_closer_than_min_room() -> None:
    pools = [
        PoolSnap(100.2, 100.3, 1.0),
        PoolSnap(101.0, 101.2, 2.0),
    ]
    price, room = select_tp_upper_pool(entry_price=100.0, upper_pools=pools, min_room_pct=0.8)
    assert price == 101.0
    assert room == pytest.approx(1.0)


def test_long_failure_gate_requires_all_mirrored_conditions() -> None:
    ok, reason = long_failure_ready(
        last_price=100.0,
        entry_price=100.0,
        stop_price=99.0,
        ob_ratio=0.90,
        delta_10m=-1.0,
        pool_strength=4.0,
        pool_dist_pct=0.4,
    )
    assert ok is True
    assert reason == "failure_exit"

    blocked, blocked_reason = long_failure_ready(
        last_price=100.0,
        entry_price=100.0,
        stop_price=99.0,
        ob_ratio=1.20,
        delta_10m=-1.0,
        pool_strength=4.0,
        pool_dist_pct=0.4,
    )
    assert blocked is False
    assert blocked_reason == "ob_not_weak"


def test_regime_permissions_are_mirrored() -> None:
    assert allows_short("bearish") is True
    assert allows_long("bearish") is False
    assert allows_short("bullish") is False
    assert allows_long("bullish") is True
    assert allows_short("neutral") is True
    assert allows_long("neutral") is True
    assert allows_long("unknown") is False


def test_long_failure_exit_after_sl_tp(monkeypatch) -> None:
    def _fake(symbol, *, entry_price, stop_price, close_price, as_of):
        return {"ok": True, "last_price": 100.1}

    monkeypatch.setattr(long_bt, "_long_failure_at_bar_close", _fake)
    t0 = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 9, 25, 12, 5, tzinfo=timezone.utc)
    bars = [
        _Bar(t0, high=100.8, low=99.6, close=100.6),
        _Bar(t1, high=100.7, low=100.0, close=100.1),
    ]
    trade = simulate_long_bounce_trade(
        bars,
        symbol="LINKUSDT",
        touch_idx=0,
        pool_bottom=99.4,
        pool_top=100.0,
        tp_price=102.0,
        hold_bars=4,
    )
    assert trade["exit_reason"] == "failure"
    assert trade["exit_price"] == 100.1


def test_same_bar_sl_beats_long_failure(monkeypatch) -> None:
    def _fake(*_args, **_kwargs):
        raise AssertionError("failure must not run when the bar hits SL")

    monkeypatch.setattr(long_bt, "_long_failure_at_bar_close", _fake)
    t0 = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 9, 25, 12, 5, tzinfo=timezone.utc)
    bars = [
        _Bar(t0, high=100.8, low=99.6, close=100.6),
        _Bar(t1, high=100.7, low=99.0, close=99.2),
    ]
    trade = simulate_long_bounce_trade(
        bars,
        symbol="LINKUSDT",
        touch_idx=0,
        pool_bottom=99.4,
        pool_top=100.0,
        tp_price=102.0,
        hold_bars=4,
    )
    assert trade["exit_reason"] == "sl"


def test_long_trade_respects_explicit_stop_override() -> None:
    t0 = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 9, 25, 12, 5, tzinfo=timezone.utc)
    bars = [
        _Bar(t0, high=100.3, low=99.7, close=100.2),
        _Bar(t1, high=101.2, low=99.1, close=100.9),
    ]
    trade = simulate_long_bounce_trade(
        bars,
        symbol="LINKUSDT",
        touch_idx=0,
        pool_bottom=99.8,
        pool_top=100.0,
        tp_price=101.0,
        stop_price=99.0,
        hold_bars=4,
    )
    assert trade["exit_reason"] == "tp"
    assert trade["stop_price"] == pytest.approx(99.0)


def test_same_fill_is_one_signal_even_if_rank_and_price_differ() -> None:
    if str(_ROOT) not in sys.path:
        sys.path.insert(0, str(_ROOT))
    from bot.forward_test.fill_identity import fill_key_text
    from ob_microstructure_breakout_bot.exit_backtest.run_longs import (
        _collapse_duplicate_fills,
    )

    first = fill_key_text(
        symbol="DOGEUSDT",
        side="long",
        entry_ts="2026-09-08T12:05:00+00:00",
        stop_price=0.08648169,
        tp_price=0.09073,
    )
    second = fill_key_text(
        symbol="DOGEUSDT",
        side="long",
        entry_ts="2026-09-08T12:05:00+00:00",
        stop_price=0.086481691,
        tp_price=0.0907300004,
    )
    assert first == second

    signals = [
        {
            "rows": [
                {
                    "side": "long",
                    "trade_taken": True,
                    "rank": 1,
                    "long_entry_ts": "2026-09-08T12:05:00+00:00",
                    "long_entry_price": 0.08896,
                    "stop_price": 0.08648169,
                    "tp_price": 0.09073,
                    "pnl_pct": 1.99,
                    "exit_reason": "tp",
                },
                {
                    "side": "long",
                    "trade_taken": True,
                    "rank": 2,
                    "long_entry_ts": "2026-09-08T12:05:00+00:00",
                    "long_entry_price": 0.08894,
                    "stop_price": 0.08648169,
                    "tp_price": 0.09073,
                    "pnl_pct": -1.08,
                    "exit_reason": "sl",
                },
            ]
        }
    ]
    _collapse_duplicate_fills(signals)
    kept = [r for r in signals[0]["rows"] if r["trade_taken"]]
    dropped = [r for r in signals[0]["rows"] if not r["trade_taken"]]
    assert len(kept) == 1
    assert kept[0]["pnl_pct"] == 1.99
    assert dropped[0]["exit_reason"] == "duplicate_fill"
    assert dropped[0]["pnl_pct"] is None


def test_regime_flag_blocks_longs_in_bearish_and_keeps_bullish() -> None:
    from datetime import datetime

    from ob_microstructure_breakout_bot.exit_backtest.run_longs import apply_regime_filter

    def _regime(ts: datetime) -> dict:
        label = "bearish" if ts.hour < 12 else "bullish"
        return {
            "regime": label,
            "allows_long": label in {"bullish", "neutral"},
            "allows_short": label in {"bearish", "neutral"},
        }

    signals = [
        {
            "decision_ts": "2026-09-08T05:00:00+00:00",
            "rows": [
                {
                    "trade_taken": True,
                    "pnl_pct": -1.1,
                    "exit_reason": "sl",
                    "exit_ts": "2026-09-08T06:00:00+00:00",
                    "exit_price": 1.0,
                }
            ],
        },
        {
            "decision_ts": "2026-09-08T15:00:00+00:00",
            "rows": [{"trade_taken": True, "pnl_pct": 1.2, "exit_reason": "tp"}],
        },
    ]
    report = apply_regime_filter(
        signals,
        symbol="DOGEUSDT",
        side="long",
        regime_at=_regime,
    )
    assert report["n_blocked_trades"] == 1
    assert signals[0]["rows"][0]["trade_taken"] is False
    assert signals[0]["rows"][0]["exit_reason"] == "regime_bearish"
    assert signals[0]["rows"][0]["pnl_pct"] is None
    assert signals[1]["rows"][0]["trade_taken"] is True
    assert signals[1]["rows"][0]["pnl_pct"] == 1.2
