"""Backtest-only shim: apply basket exit rebuild policies without live changes."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from research.backtests.exit_rebuild_policy import (
    ExitRebuildPolicyConfig,
    ExitRebuildPolicyName,
    apply_exit_rebuild_policy,
    safe_float,
)


def _active_long_exit_price(strategy: Any, snapshot: Any, runtime_state: Any) -> float | None:
    state = runtime_state.strategy_state if runtime_state else {}
    latest = safe_float(state.get("latest_tp_price"))
    if latest > 0:
        return latest
    if snapshot is None:
        return None
    purpose = getattr(strategy, "LONG_TP_EXIT_PURPOSE", "LONG_TP_EXIT")
    for order in getattr(snapshot, "active_orders", []) or []:
        if str(getattr(order, "purpose", "") or "") != purpose:
            continue
        trigger = safe_float(getattr(order, "trigger_price", 0.0))
        if trigger > 0:
            return trigger
        price = safe_float(getattr(order, "price", 0.0))
        if price > 0:
            return price
    return None


def install_exit_rebuild_policy(
    strategy: Any,
    config: ExitRebuildPolicyConfig | None,
) -> None:
    if config is None or config.policy == "current":
        strategy._backtest_exit_rebuild_policy = "current"
        strategy._backtest_exit_rebuild_policy_config = config or ExitRebuildPolicyConfig()
        strategy._backtest_exit_policy_decisions = []
        return
    if getattr(strategy, "_backtest_exit_rebuild_policy_shim_installed", False):
        strategy._backtest_exit_rebuild_policy = config.policy
        strategy._backtest_exit_rebuild_policy_config = config
        return

    original = strategy._calculate_tp_projection

    def _wrapped(
        break_even_price: float,
        snapshot: Any = None,
        runtime_state: Any = None,
    ) -> Any:
        projection = original(break_even_price, snapshot, runtime_state)
        cfg: ExitRebuildPolicyConfig = strategy._backtest_exit_rebuild_policy_config
        policy: ExitRebuildPolicyName = cfg.policy
        if policy == "current":
            return projection

        state = runtime_state.strategy_state if runtime_state else {}
        long_qty = safe_float(snapshot.long_qty if snapshot else state.get("open_long_qty"))
        short_qty = safe_float(snapshot.short_qty if snapshot else state.get("open_short_qty"))
        long_avg = safe_float(snapshot.long_avg if snapshot else state.get("long_avg"))
        short_avg = safe_float(snapshot.short_avg if snapshot else state.get("short_avg"))
        realized = safe_float(getattr(projection, "realized_cycle_net", 0.0))
        fee_rate = safe_float(getattr(projection, "fee_rate", 0.0))
        tick = safe_float(getattr(strategy.config, "price_tick_size", 0.0), 0.0001)
        active = _active_long_exit_price(strategy, snapshot, runtime_state)
        primary = "long"
        if hasattr(strategy, "_get_primary_position_side"):
            try:
                primary = str(strategy._get_primary_position_side() or "long")
            except Exception:
                primary = "long"

        decision = apply_exit_rebuild_policy(
            policy=policy,
            primary_side=primary,
            raw_exit=safe_float(projection.tp_price),
            active_exit=active,
            long_qty=long_qty,
            long_avg=long_avg,
            short_qty=short_qty,
            short_avg=short_avg,
            realized_trade_pnl=realized,
            fee_rate=fee_rate,
            tp_profit_target_pct=safe_float(strategy.config.tp_profit_target_pct),
            tp_buffer_pct=safe_float(strategy.config.tp_buffer_pct),
            tick_size=tick,
            coverage_tolerance_usdt=cfg.coverage_tolerance_usdt,
        )
        decisions = getattr(strategy, "_backtest_exit_policy_decisions", None)
        if decisions is None:
            strategy._backtest_exit_policy_decisions = []
            decisions = strategy._backtest_exit_policy_decisions
        # Record only material decisions to avoid per-tick spam.
        last = decisions[-1] if decisions else None
        material = (
            decision.prevented_increase
            or abs(decision.effective_exit - decision.raw_exit) > 1e-9
            or (
                decision.active_exit is not None
                and abs(decision.raw_exit - float(decision.active_exit)) > 1e-9
            )
        )
        changed = (
            last is None
            or abs(float(last.get("effective_exit") or 0) - decision.effective_exit) > 1e-9
            or abs(float(last.get("raw_exit") or 0) - decision.raw_exit) > 1e-9
            or bool(last.get("prevented_increase")) != decision.prevented_increase
        )
        if material and changed:
            decisions.append(
                {
                    "policy": decision.policy,
                    "raw_exit": decision.raw_exit,
                    "active_exit": decision.active_exit,
                    "effective_exit": decision.effective_exit,
                    "prevented_increase": decision.prevented_increase,
                    "old_exit_covered": decision.old_exit_covered,
                    "reason": decision.reason,
                    "required_trade_profit": decision.required_trade_profit,
                    "pnl_at_active_exit": decision.pnl_at_active_exit,
                    "pnl_at_effective_exit": decision.pnl_at_effective_exit,
                    "long_qty": long_qty,
                    "short_qty": short_qty,
                    "long_avg": long_avg,
                    "short_avg": short_avg,
                    "realized_trade_pnl": realized,
                }
            )
        if abs(decision.effective_exit - safe_float(projection.tp_price)) <= 1e-12:
            return projection
        return replace(projection, tp_price=float(decision.effective_exit))

    strategy._calculate_tp_projection = _wrapped  # type: ignore[method-assign]
    strategy._backtest_exit_rebuild_policy_shim_installed = True
    strategy._backtest_exit_rebuild_policy = config.policy
    strategy._backtest_exit_rebuild_policy_config = config
    strategy._backtest_exit_policy_decisions = []
