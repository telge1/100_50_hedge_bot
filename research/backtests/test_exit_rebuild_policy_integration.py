"""Integration tests for basket exit rebuild policies (research-only).

These tests exercise the exit-rebuild-policy *glue code* (the backtest shim and
the continuous re-entry loop) rather than the pure policy math, which is
already unit-tested in ``test_exit_rebuild_policy.py``.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from fixed_cycle_hedge_bot.fixed_cycle_strategy import TpProjection
from research.backtests.backtest_report import BacktestResult
from research.backtests.candle_loader import load_candles_for_symbol
from research.backtests.continuous_reentry_backtest import (
    run_continuous_reentry_backtests,
    run_continuous_reentry_for_direction,
)
from research.backtests.exit_rebuild_policy import ExitRebuildPolicyConfig
from research.backtests.exit_rebuild_policy_shim import install_exit_rebuild_policy
from research.backtests.long_add_multistart_metrics import safe_float
from research.backtests.run_exit_policy_multicoin_continuous import (
    APT_BASELINE_CLOSED,
    APT_BASELINE_OPEN,
    APT_BASELINE_SERIES_MTM,
    APT_BASELINE_TOLERANCE,
    APT_BASELINE_TRADES,
    APT_SYMBOL,
    run_apt_safety_stage,
)
from research.backtests.simulated_order_book import SyntheticCandle


def _flat_candles(n: int, *, symbol: str = "APTUSDT", close: float = 1.0) -> list[SyntheticCandle]:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [
        SyntheticCandle(
            symbol=symbol,
            timestamp=base,
            open=close,
            high=close + 0.01,
            low=close - 0.01,
            close=close,
        )
        for _ in range(n)
    ]


# ---------------------------------------------------------------------------
# 1. APT current-policy baseline smoke (real data, full stage-1 window)
# ---------------------------------------------------------------------------


def test_apt_current_baseline_reproduces_known_series_mtm(tmp_path) -> None:
    """The exact regression target the multi-coin runner's stage 1 asserts on."""
    payload = run_apt_safety_stage(output_root=tmp_path)

    assert payload["symbol"] == APT_SYMBOL
    assert payload["baseline_ok"] is True
    assert payload["same_candle_violation"] is False
    assert payload["abort_stage2"] is False

    current = payload["per_policy"]["current"]
    assert current["trades"] == APT_BASELINE_TRADES
    assert current["closed"] == APT_BASELINE_CLOSED
    assert current["open"] == APT_BASELINE_OPEN
    assert current["series_mtm"] == pytest.approx(APT_BASELINE_SERIES_MTM, abs=APT_BASELINE_TOLERANCE)

    # No policy should introduce LONG_ADD/SHORT_REDUCE same-candle causality violations.
    for policy_stats in payload["per_policy"].values():
        assert policy_stats["same_candle_violations"] == 0

    assert (tmp_path / "apt_safety_check.json").exists()


# ---------------------------------------------------------------------------
# 2. non_worsening prevents raising the long exit -- shim-level integration
#    (the pure-math version of this check already exists in
#    test_exit_rebuild_policy.py; this exercises the installed shim wrapper).
# ---------------------------------------------------------------------------


class _FakeSnapshot:
    def __init__(self, *, long_qty: float, long_avg: float, short_qty: float, short_avg: float) -> None:
        self.long_qty = long_qty
        self.long_avg = long_avg
        self.short_qty = short_qty
        self.short_avg = short_avg
        self.active_orders: list[object] = []


class _FakeRuntimeState:
    def __init__(self, strategy_state: dict[str, object]) -> None:
        self.strategy_state = strategy_state


class _FakeConfig:
    tp_profit_target_pct = 0.25
    tp_buffer_pct = 0.0002
    price_tick_size = 0.0001


class _FakeStrategyForShim:
    LONG_TP_EXIT_PURPOSE = "LONG_TP_EXIT"

    def __init__(self, projection: TpProjection) -> None:
        self.config = _FakeConfig()
        self._projection = projection

    def _calculate_tp_projection(
        self,
        break_even_price: float,
        snapshot: object = None,
        runtime_state: object = None,
    ) -> TpProjection:
        return self._projection


def _fake_tp_projection(tp_price: float, *, realized_cycle_net: float = 0.0) -> TpProjection:
    return TpProjection(
        tp_price=tp_price,
        target_delta_usdt=0.0,
        expected_total_net_after_exit=0.0,
        target_total_profit_usdt=0.0,
        required_profit_to_cover_loss=0.0,
        min_profit_target_usdt=0.0,
        min_required_total_usdt=0.0,
        components=None,  # not read by the exit-rebuild-policy shim
        fee_rate=0.00055,
        entry_fee_usdt=0.0,
        close_fee_usdt=0.0,
        pending_cycle_loss_usdt=0.0,
        realized_cycle_net=realized_cycle_net,
    )


def test_shim_non_worsening_prevents_raising_long_exit() -> None:
    raw_exit = 2.0037  # would raise the exit above the active resting order
    active_exit = 1.9825
    strategy = _FakeStrategyForShim(_fake_tp_projection(raw_exit))
    install_exit_rebuild_policy(strategy, ExitRebuildPolicyConfig(policy="non_worsening"))

    snapshot = _FakeSnapshot(long_qty=20.0, long_avg=1.2, short_qty=10.0, short_avg=1.0)
    runtime_state = _FakeRuntimeState({"latest_tp_price": active_exit})

    projection = strategy._calculate_tp_projection(1.0, snapshot, runtime_state)

    assert projection.tp_price == pytest.approx(active_exit)
    decisions = strategy._backtest_exit_policy_decisions
    assert len(decisions) == 1
    assert decisions[0]["prevented_increase"] is True
    assert decisions[0]["raw_exit"] == pytest.approx(raw_exit)
    assert decisions[0]["effective_exit"] == pytest.approx(active_exit)


def test_shim_current_policy_passes_through_raw_projection() -> None:
    strategy = _FakeStrategyForShim(_fake_tp_projection(2.5))
    install_exit_rebuild_policy(strategy, ExitRebuildPolicyConfig(policy="current"))

    projection = strategy._calculate_tp_projection(1.0, None, None)

    assert projection.tp_price == pytest.approx(2.5)
    assert strategy._backtest_exit_policy_decisions == []


# ---------------------------------------------------------------------------
# 3. Continuous re-entry: no new trade may start before the prior one is flat.
# ---------------------------------------------------------------------------


def test_continuous_no_new_trade_before_flat(monkeypatch: pytest.MonkeyPatch) -> None:
    """closed, closed, open -> exactly 3 trades; a 4th must never be attempted."""
    statuses = ["closed", "closed", "open"]
    call_count = {"n": 0}

    def fake_run(symbol, direction, candles, **kwargs):
        idx = call_count["n"]
        call_count["n"] += 1
        status = statuses[idx]
        return BacktestResult(
            symbol=symbol,
            direction=direction,
            final_status=status,
            exit_reason="flat_no_active_orders" if status == "closed" else "series_end_with_open_positions",
            candles_processed=10,
            fills_count=2,
        )

    monkeypatch.setattr(
        "research.backtests.continuous_reentry_backtest.run_historical_backtest",
        fake_run,
    )
    results = run_continuous_reentry_for_direction(
        "APTUSDT",
        "long",
        _flat_candles(200),
        continuous_start_index=0,
    )

    assert call_count["n"] == 3, "must not attempt a trade 4 once a trade stays open"
    assert [r.final_status for r in results] == ["closed", "closed", "open"]
    assert [r.trade_number for r in results] == [1, 2, 3]


def test_continuous_stops_after_single_open_trade(monkeypatch: pytest.MonkeyPatch) -> None:
    """An immediately-open first trade must also produce no trade 2."""
    call_count = {"n": 0}

    def fake_run(symbol, direction, candles, **kwargs):
        call_count["n"] += 1
        return BacktestResult(
            symbol=symbol,
            direction=direction,
            final_status="open",
            exit_reason="series_end_with_open_positions",
            candles_processed=10,
        )

    monkeypatch.setattr(
        "research.backtests.continuous_reentry_backtest.run_historical_backtest",
        fake_run,
    )
    results = run_continuous_reentry_for_direction(
        "APTUSDT",
        "long",
        _flat_candles(50),
        continuous_start_index=0,
    )
    assert call_count["n"] == 1
    assert len(results) == 1
    assert results[0].final_status == "open"


# ---------------------------------------------------------------------------
# 4. Determinism: repeated runs of the same policy must be bit-for-bit stable.
# ---------------------------------------------------------------------------


def test_apt_current_policy_deterministic_series_mtm() -> None:
    candles = load_candles_for_symbol(APT_SYMBOL, limit=3000)

    def run_once() -> tuple[int, list[str], float]:
        payload = run_continuous_reentry_backtests(
            symbol=APT_SYMBOL,
            direction="long",
            candles=candles,
            continuous_start_index=0,
            config_source="live",
            fill_model="conservative",
            tp_profit_target_pct=0.25,
            long_fill_distance_pct=0.5,
            target_profit_usdt=0.015,
            exit_rebuild_policy_config=ExitRebuildPolicyConfig(policy="current"),
            write_json=False,
            write_csv=False,
        )
        results = list(payload["results"])
        series_mtm = sum(
            safe_float(r.overall_pnl, safe_float(r.realized_pnl) + safe_float(r.unrealized_pnl))
            for r in results
        )
        return len(results), [str(r.final_status) for r in results], series_mtm

    first = run_once()
    second = run_once()

    assert first[0] == second[0]
    assert first[1] == second[1]
    assert first[2] == pytest.approx(second[2])
