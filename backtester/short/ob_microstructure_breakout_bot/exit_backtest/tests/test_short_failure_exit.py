"""Failure exit order in the short bounce simulation. No market data."""

from __future__ import annotations

from datetime import datetime, timezone

import ob_microstructure_breakout_bot.exit_backtest.pool_bounce_backtest as bounce


class _Bar:
    def __init__(self, ts: datetime, high: float, low: float, close: float) -> None:
        self.ts = ts
        self.high = high
        self.low = low
        self.close = close


def _bars() -> list[_Bar]:
    t0 = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 9, 25, 12, 5, tzinfo=timezone.utc)
    return [
        _Bar(t0, high=100.4, low=99.2, close=99.5),
        _Bar(t1, high=99.8, low=99.3, close=99.7),
    ]


def test_failure_exit_uses_live_gate_after_sl_tp(monkeypatch) -> None:
    calls: list[float] = []

    def _fake(symbol, *, entry_price, stop_price, close_price, as_of):
        calls.append(close_price)
        return {"ok": True, "last_price": 99.65}

    monkeypatch.setattr(bounce, "_short_failure_at_bar_close", _fake)
    trade = bounce.simulate_short_bounce_trade(
        _bars(),
        symbol="XRPUSDT",
        touch_idx=0,
        pool_bottom=100.0,
        pool_top=101.0,
        tp_price=98.0,
        hold_bars=4,
    )
    assert calls == [99.7]
    assert trade["trade_taken"] is True
    assert trade["exit_reason"] == "failure"
    assert trade["exit_price"] == 99.65
    assert trade["pnl_pct"] == (99.5 - 99.65) / 99.5 * 100.0


def test_same_bar_sl_beats_failure(monkeypatch) -> None:
    def _fake(*_args, **_kwargs):
        raise AssertionError("failure must not run when the bar hits SL")

    monkeypatch.setattr(bounce, "_short_failure_at_bar_close", _fake)
    bars = _bars()
    bars[1].high = 102.0
    trade = bounce.simulate_short_bounce_trade(
        bars,
        symbol="XRPUSDT",
        touch_idx=0,
        pool_bottom=100.0,
        pool_top=101.0,
        tp_price=98.0,
        hold_bars=4,
    )
    assert trade["exit_reason"] == "sl"
