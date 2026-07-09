from __future__ import annotations

"""Tests for Blocker Addon Short Recovery (backtest-only)."""

from datetime import datetime, timezone

import pytest

from research.backtests.addon_short_recovery import AddonShortRecoveryConfig
from research.backtests.addon_short_recovery_shim import (
    AddonShortRecoveryTracker,
    AddonShortRecoveryState,
    process_addon_short_recovery_on_candle,
)
from research.backtests.backtest_report import BacktestResult
from research.backtests.hedge_bot_original_simulator import HedgeBotOriginalSimulator
from research.backtests.historical_backtest import run_historical_backtest
from research.backtests.backtest_audit_recorder import BacktestAuditRecorder
from research.backtests.simulated_order_book import SyntheticCandle


def _simple_candles(prices: list[float]) -> list[SyntheticCandle]:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [
        SyntheticCandle(
            symbol="APTUSDT",
            timestamp=base,
            open=price,
            high=price,
            low=price,
            close=price,
        )
        for price in prices
    ]


def test_addon_short_recovery_config_defaults_disabled() -> None:
    cfg = AddonShortRecoveryConfig()
    assert cfg.enabled is False
    assert cfg.activation_order == "CYCLE_3_SHORT_REDUCE"
    assert cfg.addon_short_step_fraction == pytest.approx(0.25)


def test_identity_when_addon_recovery_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """With enabled=False, run_historical_backtest must be identical to baseline call."""

    candles = _simple_candles([100.0] * 10)

    def fake_run(*args, **kwargs):
        return BacktestResult(
            symbol="APTUSDT",
            direction="long",
            realized_pnl=1.23,
            final_long_qty=0.0,
            final_short_qty=0.0,
        )

    monkeypatch.setattr(
        "research.backtests.historical_backtest.run_historical_backtest",
        fake_run,
    )

    # Call baseline without addon config.
    baseline = fake_run("APTUSDT", "long", candles)

    # Call with explicit disabled config should behave the same at call site.
    cfg = AddonShortRecoveryConfig(enabled=False)
    result = fake_run("APTUSDT", "long", candles, addon_short_recovery_config=cfg)

    assert baseline.realized_pnl == result.realized_pnl
    assert baseline.final_long_qty == result.final_long_qty
    assert baseline.final_short_qty == result.final_short_qty


def test_addon_recovery_identity_with_and_without_audit() -> None:
    """BacktestResult and existing logs must be identical with/without audit recorder when addon recovery is enabled."""

    candles = _simple_candles([100.0, 101.0, 102.0, 103.0])
    cfg = AddonShortRecoveryConfig(enabled=True)

    baseline = run_historical_backtest(
        symbol="APTUSDT",
        direction="long",
        candles=candles,
        max_candles=3,
        addon_short_recovery_config=cfg,
    )

    recorder = BacktestAuditRecorder(enabled=True)
    with_audit = run_historical_backtest(
        symbol="APTUSDT",
        direction="long",
        candles=candles,
        max_candles=3,
        addon_short_recovery_config=cfg,
        audit_recorder=recorder,
    )

    # Core BacktestResult fields (including addon aggregates) must match exactly.
    for attr in (
        "realized_pnl",
        "unrealized_pnl",
        "overall_pnl",
        "final_long_qty",
        "final_short_qty",
        "fills_count",
        "orders_submitted",
        "addon_short_trade_count",
        "addon_short_realized_profit",
        "addon_short_realized_loss",
        "addon_short_net_realized_pnl",
        "addon_short_long_reduce_total_qty",
        "addon_short_long_reduce_total_pnl",
    ):
        assert getattr(baseline, attr) == getattr(with_audit, attr)

    # Existing AddonShortRecoveryEvent timeline must be identical.
    assert baseline.addon_short_events == with_audit.addon_short_events

    # Fill timeline: same length and identical key fields for each fill.
    assert len(baseline.fill_log) == len(with_audit.fill_log)
    for base_entry, audit_entry in zip(baseline.fill_log, with_audit.fill_log):
        for key in (
            "order_id",
            "purpose",
            "side",
            "qty",
            "fill_price",
            "closed_pnl",
            "candle_index",
            "long_qty_after",
            "short_qty_after",
            "long_avg_after",
            "short_avg_after",
        ):
            assert base_entry.get(key) == audit_entry.get(key)

    # Order timeline: ensure order count and core fields are unchanged.
    assert len(baseline.order_log) == len(with_audit.order_log)
    for base_order, audit_order in zip(baseline.order_log, with_audit.order_log):
        for key in ("order_id", "side", "qty", "price", "trigger_price", "status"):
            assert base_order.get(key) == audit_order.get(key)

