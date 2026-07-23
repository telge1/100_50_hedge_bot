"""Tests for TEM-FD economic undercoverage classification and coverage guards."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from fixed_cycle_hedge_bot.fixed_cycle_strategy import (
    BasketExitCoverageDecision,
    FinalExitEconomics,
)
from research.backtests.full_dynamic_second_leg_restaging import (
    ECONOMIC_TOLERANCE_USDT,
    FD_COVERED,
    FD_REPLAN_ACTIVE,
    compute_canonical_economics,
)
from research.backtests.second_leg_price_staging_shim import (
    _fd_has_uncovered_remaining,
    _install_full_dynamic_coverage_guards,
)
from research.backtests.tem_fd_undercoverage_economics import (
    classify_closed_economics,
    root_cause_category,
)


def test_canonical_confirmed_realized_counted_once() -> None:
    eco = compute_canonical_economics(
        required_net_total=12.0,
        confirmed_stage_realized_net=2.0,
        initial_pending_cycle_loss_usdt=11.985,
        target_profit_usdt=0.015,
    )
    assert eco.remaining_required_net == pytest.approx(10.0)
    assert eco.pending_cycle_loss_usdt == pytest.approx(9.985)


def test_fd_has_uncovered_remaining_true_when_gap() -> None:
    rt = SimpleNamespace(
        strategy_state={
            "research_fd_required_net_total": {"4": 12.0},
            "staged_second_leg_tp_realized_net": {"4": 2.0},
            FD_COVERED: {"4": False},
            "pending_cycle_loss_usdt": 9.985,
        }
    )
    assert _fd_has_uncovered_remaining(rt) is True


def test_fd_has_uncovered_remaining_false_when_covered() -> None:
    rt = SimpleNamespace(
        strategy_state={
            "research_fd_required_net_total": {"4": 12.0},
            "staged_second_leg_tp_realized_net": {"4": 12.0},
            FD_COVERED: {"4": True},
            "pending_cycle_loss_usdt": 0.0,
        }
    )
    assert _fd_has_uncovered_remaining(rt) is False


def test_replan_in_progress_blocks_coverage_and_exits() -> None:
    econ = FinalExitEconomics(
        expected_total_net_after_exit=10.0,
        target_delta_usdt=1.0,
        required_profit_to_cover_loss=0.0,
        min_profit_target_usdt=2.0,
        min_required_total_usdt=2.0,
        sufficient=True,
    )
    ok_decision = BasketExitCoverageDecision(
        required_net_usdt=2.0,
        realized_net_usdt=0.0,
        expected_basket_net_usdt=10.0,
        remaining_required_usdt=2.0,
        coverage_after_exit_usdt=10.0,
        coverage_ok=True,
        tolerance_usdt=0.1,
        reason_code="coverage_ok_basket_compensates_partial_stages",
        staging_incomplete=True,
        pending_cycle_loss_usdt=0.0,
        economics=econ,
    )

    class FakeStrategy:
        def evaluate_basket_exit_coverage(self, **kwargs: Any) -> BasketExitCoverageDecision:
            return ok_decision

        def _build_exit_intents(self, *args: Any, **kwargs: Any) -> list[Any]:
            return ["EXIT"]

    strategy = FakeStrategy()
    _install_full_dynamic_coverage_guards(strategy)
    rt = SimpleNamespace(strategy_state={FD_REPLAN_ACTIVE: True, "pending_cycle_loss_usdt": 5.0})
    decision = strategy.evaluate_basket_exit_coverage(
        snapshot=SimpleNamespace(long_qty=1.0, short_qty=1.0),
        runtime_state=rt,
        long_tp_price=1.0,
        short_sl_price=1.0,
    )
    assert decision.coverage_ok is False
    assert decision.reason_code == "coverage_blocked_fd_replan_in_progress"
    assert strategy._build_exit_intents(None, rt) == []


def test_skipped_staging_forced_through_sufficient_when_fd_uncovered() -> None:
    insuff = FinalExitEconomics(
        expected_total_net_after_exit=1.0,
        target_delta_usdt=-5.0,
        required_profit_to_cover_loss=9.0,
        min_profit_target_usdt=2.0,
        min_required_total_usdt=11.0,
        sufficient=False,
    )
    skipped = BasketExitCoverageDecision(
        required_net_usdt=11.0,
        realized_net_usdt=0.0,
        expected_basket_net_usdt=1.0,
        remaining_required_usdt=11.0,
        coverage_after_exit_usdt=1.0,
        coverage_ok=True,
        tolerance_usdt=0.1,
        reason_code="coverage_skipped_not_staged",
        staging_incomplete=False,
        pending_cycle_loss_usdt=9.0,
        economics=insuff,
    )

    class FakeStrategy:
        def evaluate_basket_exit_coverage(self, **kwargs: Any) -> BasketExitCoverageDecision:
            return skipped

        def _build_exit_intents(self, *args: Any, **kwargs: Any) -> list[Any]:
            return ["EXIT"]

    strategy = FakeStrategy()
    _install_full_dynamic_coverage_guards(strategy)
    rt = SimpleNamespace(
        strategy_state={
            FD_REPLAN_ACTIVE: False,
            "research_fd_required_net_total": {"4": 12.0},
            "staged_second_leg_tp_realized_net": {"4": 2.0},
            FD_COVERED: {},
            "pending_cycle_loss_usdt": 9.0,
        }
    )
    decision = strategy.evaluate_basket_exit_coverage(
        snapshot=SimpleNamespace(long_qty=1.0, short_qty=1.0),
        runtime_state=rt,
        long_tp_price=1.0,
        short_sl_price=1.0,
    )
    assert decision.coverage_ok is False
    assert decision.reason_code == "coverage_blocked_insufficient_basket"
    assert decision.staging_incomplete is True


def test_root_cause_maps_basket_covered() -> None:
    cat = root_cause_category(
        {
            "economic_class": "covered_by_basket_exit",
            "last_reason_code": "coverage_ok_basket_compensates_partial_stages",
            "cycle_pair_status": "undercovered",
            "cycle_pair_missing_pnl": 9.9,
        }
    )
    assert cat == "covered_by_basket_exit_not_economic_uc"


def test_gold_avax_not_economic_undercoverage_after_guards() -> None:
    import csv
    from pathlib import Path

    from research.backtests.candle_loader import load_candles_for_symbol
    from research.backtests.full_dynamic_second_leg_restaging import resolve_full_dynamic_profile
    from research.backtests.historical_backtest import normalize_candles
    from research.backtests.multicoin_blocker_price_staging import (
        FULL_HISTORY_CANDLE_LIMIT,
        run_isolated_blocker,
    )

    source = Path(
        "research/backtests/results/fixed_step_distance_staging_large_1000_500_20260722/start_points.csv"
    )
    if not source.exists():
        pytest.skip("start_points missing")
    starts = {r["pair_key"]: r for r in csv.DictReader(source.open())}
    pk = "AVAXUSDT|full_history|4156"
    sp = starts[pk]
    si = int(sp["start_index"])
    mw = int(float(sp["max_window_candles"]))
    candles = normalize_candles(
        "AVAXUSDT", load_candles_for_symbol("AVAXUSDT", limit=FULL_HISTORY_CANDLE_LIMIT)
    )
    series = candles[: si + mw]
    cfg = resolve_full_dynamic_profile("two_early_medium_full_dynamic")
    result = run_isolated_blocker(
        coin="AVAXUSDT", candles=series, start_index=si, staging_config=cfg
    )
    eco = classify_closed_economics(result)
    assert eco["economic_undercoverage_closed"] == 0
    assert eco["sufficient_false_closed"] == 0
    # May be flat covered-by-basket or still open — never undercovered economic close.
    if eco["flat"]:
        assert eco["economic_class"] == "covered_by_basket_exit"
        assert eco["last_sufficient"] is True
