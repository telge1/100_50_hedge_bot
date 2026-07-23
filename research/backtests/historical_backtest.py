"""Historical mini-backtest runner over 5m candle series (Phase 4/7)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from fixed_cycle_hedge_bot.models import FillEvent

from .backtest_config_loader import (
    DEFAULT_LONG_CONFIG_PATH,
    DEFAULT_SHORT_CONFIG_PATH,
    ConfigSource,
    apply_config_load_result_to_backtest_result,
    resolve_backtest_config,
)
from .addon_short_recovery import AddonShortRecoveryConfig
from .addon_short_recovery_shim import (
    AddonShortRecoveryTracker,
    attach_addon_short_recovery_tracker,
    process_addon_short_recovery_on_candle,
    record_addon_recovery_series_end,
)
from .recovery_bot_config import RecoveryBotConfig
from .recovery_bot_shim import (
    attach_recovery_bot_tracker,
    populate_recovery_bot_result_fields,
    process_recovery_bot_after_normal_candle,
    process_recovery_bot_recovery_only_candle,
    trade_absolute_candle_index,
)
from .backtest_report import BacktestResult, build_fill_log_entry
from .debug_report import finalize_backtest_debug
from .cycle_short_tp_relief import CycleShortTpReliefConfig
from .dynamic_cycle_order_scaling import DynamicCycleOrderScalingConfig
from .exit_rebuild_policy import ExitRebuildPolicyConfig
from .inventory_mtm_freeze import InventoryMtmFreezeConfig, freeze_state_summary
from .second_leg_price_staging import SecondLegPriceStagingConfig
from .stuck_recovery_reload import StuckRecoveryReloadConfig
from .stuck_recovery_reload_shim import maybe_execute_stuck_recovery_reload
from .trade_block_export import ensure_backtest_trade_block_ids
from .fill_models import resolve_fill_model_config
from .hedge_bot_original_simulator import HedgeBotOriginalSimulator, Signal
from .backtest_audit_recorder import BacktestAuditRecorder
from .simulated_order_book import SyntheticCandle


def normalize_candles(symbol: str, candles: Iterable[Any]) -> list[SyntheticCandle]:
    normalized: list[SyntheticCandle] = []
    for row in candles:
        if isinstance(row, SyntheticCandle):
            normalized.append(row)
        else:
            normalized.append(SyntheticCandle.from_row(symbol.upper(), dict(row)))
    return normalized


def _is_trade_closed(sim: HedgeBotOriginalSimulator) -> bool:
    return (
        sim.book.long_qty <= 1e-12
        and sim.book.short_qty <= 1e-12
        and not sim.book.active_orders()
    )


def _cycles_seen(strategy_state: dict[str, Any]) -> int | None:
    active = int(strategy_state.get("active_cycle_index") or 0)
    completed = int(strategy_state.get("completed_cycle_count") or 0)
    value = max(active, completed)
    return value if value > 0 else None


def _update_drawdown(
    *,
    cumulative_pnl: float,
    peak_pnl: float,
    max_drawdown: float,
) -> tuple[float, float]:
    peak = max(peak_pnl, cumulative_pnl)
    drawdown = peak - cumulative_pnl
    return peak, max(max_drawdown, drawdown)


def _append_fill_logs(
    result: BacktestResult,
    sim: HedgeBotOriginalSimulator,
    fills: list[FillEvent],
    *,
    candle: SyntheticCandle | None,
    candle_index: int | None,
) -> float:
    pnl_delta = 0.0
    for fill in fills:
        metadata = dict(fill.metadata or {})
        entry = build_fill_log_entry(
            fill,
            sim.book,
            timestamp=fill.occurred_at,
            candle_index=candle_index,
            candle=candle,
            order_check_price=metadata.get("order_check_price"),
        )
        result.fill_log.append(entry)
        pnl_delta += float(entry["closed_pnl"])
    result.fills_count = len(result.fill_log)
    return pnl_delta


def run_historical_backtest(
    symbol: str,
    direction: str,
    candles: Iterable[Any],
    *,
    max_candles: int | None = None,
    fill_model: str = "conservative",
    max_fills_per_candle: int | None = None,
    conservative_fill_order: bool = True,
    initial_notional_usdt: float = 100.0,
    base_notional_usdt: float | None = None,
    config_source: ConfigSource = "test",
    long_config_path: str | Path = DEFAULT_LONG_CONFIG_PATH,
    short_config_path: str | Path = DEFAULT_SHORT_CONFIG_PATH,
    file_config_path: str | Path | None = None,
    tp_profit_target_pct: float | None = None,
    long_fill_distance_pct: float | None = None,
    target_profit_usdt: float | None = None,
    dynamic_cycle_scaling_config: DynamicCycleOrderScalingConfig | None = None,
    stuck_recovery_reload_config: StuckRecoveryReloadConfig | None = None,
    cycle_short_tp_relief_config: CycleShortTpReliefConfig | None = None,
    use_live_short_tp_relief: bool = False,
    exit_rebuild_policy_config: ExitRebuildPolicyConfig | None = None,
    inventory_mtm_freeze_config: InventoryMtmFreezeConfig | None = None,
    second_leg_price_staging_config: SecondLegPriceStagingConfig | None = None,
    addon_short_recovery_config: AddonShortRecoveryConfig | None = None,
    audit_recorder: BacktestAuditRecorder | None = None,
    recovery_bot_config: RecoveryBotConfig | None = None,
    absolute_trade_start_index: int = 0,
    input_slice_start_index: int = 0,
) -> BacktestResult:
    """Run a mini-backtest over a 5m candle series."""
    signal: Signal = "short" if str(direction).lower() == "short" else "long"
    symbol_upper = symbol.upper()
    candle_list = normalize_candles(symbol_upper, candles)
    fill_config = resolve_fill_model_config(
        fill_model=fill_model,
        max_fills_per_candle=max_fills_per_candle,
    )
    if not candle_list:
        return BacktestResult(
            symbol=symbol_upper,
            direction=signal,
            final_status="error",
            exit_reason="no_candles",
            error="candle series is empty",
            open_reason_detail="no_candles",
            fill_model=fill_config.fill_model,
            max_fills_per_candle=fill_config.max_fills_per_candle,
        )

    first_candle = candle_list[0]
    config_load = resolve_backtest_config(
        config_source=config_source,
        signal=signal,
        symbol=symbol_upper,
        long_config_path=long_config_path,
        short_config_path=short_config_path,
        file_config_path=file_config_path,
    )
    if tp_profit_target_pct is not None:
        config_load.config.tp_profit_target_pct = float(tp_profit_target_pct)
    if long_fill_distance_pct is not None:
        config_load.config.long_fill_distance_pct = float(long_fill_distance_pct)
    if target_profit_usdt is not None:
        config_load.config.target_profit_usdt = float(target_profit_usdt)
    if base_notional_usdt is not None:
        config_load.config.base_notional_usdt = float(base_notional_usdt)
        initial_notional_usdt = float(base_notional_usdt)

    if use_live_short_tp_relief:
        # Aktiviert das Live-Short-TP-Relief-Feature auf Config-Ebene, ohne den
        # Backtest-Shim zu installieren. Optional können die Live-Parameter
        # später aus CLI/JSON verfeinert werden.
        config = config_load.config
        config.cycle_short_tp_relief_enabled = True

    sim = HedgeBotOriginalSimulator(
        signal=signal,
        symbol=symbol_upper,
        candle_close=float(first_candle.close),
        config_load=config_load,
        dynamic_cycle_scaling_config=dynamic_cycle_scaling_config,
        stuck_recovery_reload_config=stuck_recovery_reload_config,
        cycle_short_tp_relief_config=None if use_live_short_tp_relief else cycle_short_tp_relief_config,
        exit_rebuild_policy_config=exit_rebuild_policy_config,
        inventory_mtm_freeze_config=inventory_mtm_freeze_config,
        second_leg_price_staging_config=second_leg_price_staging_config,
        audit_recorder=audit_recorder,
    )
    sim.candle = first_candle
    sim.candle_index = 0
    result = BacktestResult(
        symbol=symbol_upper,
        direction=signal,
        start_time=first_candle.timestamp,
        entry_price=float(first_candle.close),
        fill_model=fill_config.fill_model,
        max_fills_per_candle=fill_config.max_fills_per_candle,
    )
    apply_config_load_result_to_backtest_result(result, config_load)

    cumulative_pnl = 0.0
    peak_pnl = 0.0
    max_drawdown = 0.0
    reload_tracker = sim.stuck_recovery_reload_tracker
    addon_tracker: AddonShortRecoveryTracker | None = attach_addon_short_recovery_tracker(
        sim,
        addon_short_recovery_config,
    )
    recovery_tracker = attach_recovery_bot_tracker(sim, recovery_bot_config)
    last_candle = first_candle
    last_candle_index = 0
    recovery_closed = False

    try:
        entry_result = sim.run_entry_smoke()
        result.orders_submitted = sim.orders_submitted

        entry_pnl = _append_fill_logs(
            result,
            sim,
            entry_result.entry_fills,
            candle=first_candle,
            candle_index=0,
        )
        cumulative_pnl += entry_pnl
        if reload_tracker is not None:
            reload_tracker.note_fills(candle_index=0, fill_count=len(entry_result.entry_fills))
        peak_pnl, max_drawdown = _update_drawdown(
            cumulative_pnl=cumulative_pnl,
            peak_pnl=peak_pnl,
            max_drawdown=max_drawdown,
        )

        loop_candles = candle_list[1:]
        if max_candles is not None:
            loop_candles = loop_candles[: max(0, int(max_candles))]

        for loop_index, candle in enumerate(loop_candles, start=1):
            sim.candle = candle
            sim.candle_index = loop_index
            last_candle = candle
            last_candle_index = loop_index
            absolute_candle_index = trade_absolute_candle_index(
                input_slice_start_index=input_slice_start_index,
                absolute_trade_start_index=absolute_trade_start_index,
                local_candle_index=loop_index,
            )

            if recovery_tracker is not None and recovery_tracker.state.recovery_mode_active:
                recovery_pnl_delta, recovery_closed = process_recovery_bot_recovery_only_candle(
                    recovery_tracker,
                    sim,
                    candle=candle,
                    local_candle_index=loop_index,
                    absolute_candle_index=absolute_candle_index,
                    cumulative_pnl=cumulative_pnl,
                )
                cumulative_pnl += recovery_pnl_delta
                result.candles_processed += 1
                result.end_time = candle.timestamp
                peak_pnl, max_drawdown = _update_drawdown(
                    cumulative_pnl=cumulative_pnl,
                    peak_pnl=peak_pnl,
                    max_drawdown=max_drawdown,
                )
                if recovery_closed:
                    result.final_status = "closed"
                    result.exit_reason = "recovery_joint_exit"
                    break
                continue

            candle_result = sim.process_candle(
                candle,
                fill_model=fill_config.fill_model,
                max_fills_per_candle=fill_config.max_fills_per_candle,
                conservative_fill_order=conservative_fill_order,
            )
            result.candles_processed += 1
            result.end_time = candle.timestamp

            if candle_result.same_candle_fill_count > 1:
                result.same_candle_fills_count += 1
            result.paired_exit_fills_count += candle_result.paired_exit_fills_count

            pnl_delta = _append_fill_logs(
                result,
                sim,
                candle_result.candle_fills,
                candle=candle,
                candle_index=loop_index,
            )
            cumulative_pnl += pnl_delta
            if reload_tracker is not None:
                reload_tracker.note_fills(
                    candle_index=loop_index,
                    fill_count=len(candle_result.candle_fills),
                )
            peak_pnl, max_drawdown = _update_drawdown(
                cumulative_pnl=cumulative_pnl,
                peak_pnl=peak_pnl,
                max_drawdown=max_drawdown,
            )
            result.orders_submitted = sim.orders_submitted

            if reload_tracker is not None and not _is_trade_closed(sim):
                reload_fills = maybe_execute_stuck_recovery_reload(
                    sim,
                    reload_tracker,
                    cumulative_pnl=cumulative_pnl,
                    candle_index=loop_index,
                    trade_closed=False,
                )
                if reload_fills:
                    reload_pnl = _append_fill_logs(
                        result,
                        sim,
                        reload_fills,
                        candle=candle,
                        candle_index=loop_index,
                    )
                    cumulative_pnl += reload_pnl
                    peak_pnl, max_drawdown = _update_drawdown(
                        cumulative_pnl=cumulative_pnl,
                        peak_pnl=peak_pnl,
                        max_drawdown=max_drawdown,
                    )
                    result.orders_submitted = sim.orders_submitted

            # Backtest-only Blocker Addon Short Recovery (subaccount + long reduce).
            if addon_tracker is not None and addon_tracker.config.enabled:
                process_addon_short_recovery_on_candle(
                    sim=sim,
                    result=result,
                    tracker=addon_tracker,
                    candle=candle,
                    candle_index=loop_index,
                    candle_fills=candle_result.candle_fills,
                )

            trade_still_open = not _is_trade_closed(sim)
            if recovery_tracker is not None and recovery_tracker.config.enabled:
                recovery_pnl_delta, recovery_closed = process_recovery_bot_after_normal_candle(
                    recovery_tracker,
                    sim,
                    result=result,
                    candle=candle,
                    local_candle_index=loop_index,
                    absolute_candle_index=absolute_candle_index,
                    candle_fills=candle_result.candle_fills,
                    cumulative_pnl=cumulative_pnl,
                    trade_still_open=trade_still_open,
                )
                cumulative_pnl += recovery_pnl_delta
                peak_pnl, max_drawdown = _update_drawdown(
                    cumulative_pnl=cumulative_pnl,
                    peak_pnl=peak_pnl,
                    max_drawdown=max_drawdown,
                )
                if recovery_closed:
                    result.final_status = "closed"
                    result.exit_reason = "recovery_joint_exit"
                    break

            if _is_trade_closed(sim):
                result.final_status = "closed"
                if not result.exit_reason:
                    result.exit_reason = "flat_no_active_orders"
                break
        else:
            if max_candles is not None and result.candles_processed >= int(max_candles):
                result.final_status = "max_candles"
                result.exit_reason = "max_candles_reached"
            else:
                result.final_status = "open"
                result.exit_reason = "series_end_with_open_positions"

        result.realized_pnl = cumulative_pnl
        if initial_notional_usdt > 0:
            result.realized_pnl_pct = (cumulative_pnl / float(initial_notional_usdt)) * 100.0
            result.max_drawdown_pct = (max_drawdown / float(initial_notional_usdt)) * 100.0
        result.cycles_seen = _cycles_seen(dict(sim.runtime_state.strategy_state))
        populate_recovery_bot_result_fields(result, recovery_tracker)
        finalize_backtest_debug(result, sim, candles=candle_list)
        # Populate addon short recovery aggregates on the BacktestResult.
        if addon_tracker is not None and addon_tracker.config.enabled:
            from dataclasses import asdict as _asdict

            state = addon_tracker.state
            result.addon_short_recovery_enabled = True
            result.addon_short_recovery_activation_order = addon_tracker.config.activation_order
            result.addon_short_recovery_activated = state.activated
            result.addon_short_recovery_activation_candle_index = (
                state.activation_candle_index
            )
            result.addon_short_recovery_activation_price = state.activation_price
            result.addon_short_recovery_long_qty_at_activation = (
                state.long_qty_at_activation
            )
            result.addon_short_recovery_normal_short_qty_at_activation = (
                state.normal_short_qty_at_activation
            )
            result.addon_short_recovery_gap_at_activation = state.recovery_gap_at_activation
            result.addon_short_recovery_completed = state.recovery_completed
            result.addon_short_recovery_completion_reason = (
                state.recovery_completion_reason
            )
            result.addon_short_recovery_completed_candle_index = (
                state.recovery_completed_candle_index
            )
            result.addon_short_realized_profit = state.addon_short_realized_profit
            result.addon_short_realized_loss = state.addon_short_realized_loss
            result.addon_short_net_realized_pnl = (
                state.addon_short_realized_profit - state.addon_short_realized_loss
            )
            result.addon_short_trade_count = state.addon_short_trade_count
            result.addon_short_tp_count = state.addon_short_tp_count
            result.addon_short_rebound_exit_count = state.addon_short_rebound_exit_count
            result.addon_short_hard_stop_count = state.addon_short_hard_stop_count
            result.addon_short_long_reduce_total_qty = state.long_reduce_total_qty
            result.addon_short_long_reduce_total_pnl = state.long_reduce_total_pnl
            result.addon_short_events = [_asdict(ev) for ev in addon_tracker.events]
            # Backtest-only: emit a final RECOVERY_SERIES_END audit record when recorder is enabled.
            record_addon_recovery_series_end(
                sim=sim,
                tracker=addon_tracker,
                result=result,
                last_candle=last_candle,
                last_candle_index=last_candle_index,
            )
    except Exception as exc:
        result.final_status = "error"
        result.exit_reason = "exception"
        result.error = str(exc)
        result.open_reason_detail = f"error:{exc}"
        try:
            finalize_backtest_debug(result, sim, candles=candle_list)
        except Exception:
            pass
    finally:
        decisions = list(getattr(sim.strategy, "_backtest_exit_policy_decisions", []) or [])
        excerpt = dict(result.final_strategy_state_excerpt or {})
        excerpt["exit_rebuild_policy"] = getattr(
            sim.strategy, "_backtest_exit_rebuild_policy", "current"
        )
        excerpt["exit_policy_decisions"] = decisions

        freeze_state = getattr(sim.strategy, "_backtest_inventory_mtm_freeze_state", None)
        excerpt["inventory_mtm_freeze_variant"] = getattr(
            sim.strategy, "_backtest_inventory_mtm_freeze_variant", "A0"
        )
        excerpt["inventory_mtm_trigger_event"] = getattr(
            sim.strategy, "_backtest_inventory_mtm_trigger_event", None
        )
        excerpt["inventory_mtm_policy_actions"] = list(
            getattr(sim.strategy, "_backtest_inventory_mtm_policy_actions", []) or []
        )
        excerpt["inventory_mtm_freeze_state"] = (
            freeze_state_summary(freeze_state) if freeze_state is not None else None
        )
        try:
            excerpt["last_basket_exit_coverage_decision"] = dict(
                (sim.runtime_state.strategy_state or {}).get(
                    "last_basket_exit_coverage_decision"
                )
                or {}
            )
        except Exception:
            excerpt["last_basket_exit_coverage_decision"] = {}
        try:
            excerpt["research_second_leg_price_staging_plan"] = dict(
                (sim.runtime_state.strategy_state or {}).get(
                    "research_second_leg_price_staging_plan"
                )
                or {}
            )
        except Exception:
            excerpt["research_second_leg_price_staging_plan"] = {}
        excerpt["research_second_leg_price_staging_plans"] = list(
            getattr(sim.strategy, "_backtest_slps_plans", []) or []
        )
        # FULL_DYNAMIC diagnostics (research-only).
        try:
            st = sim.runtime_state.strategy_state or {}
            excerpt["research_fd_replan_events"] = list(st.get("research_fd_replan_events") or [])
            excerpt["research_fd_plan_revision"] = dict(st.get("research_fd_plan_revision") or {})
            excerpt["research_fd_required_net_total"] = dict(
                st.get("research_fd_required_net_total") or {}
            )
            excerpt["research_fd_initial_pending"] = dict(
                st.get("research_fd_initial_pending") or {}
            )
            excerpt["research_fd_cycle_covered"] = dict(st.get("research_fd_cycle_covered") or {})
            excerpt["research_fd_stale_generation_fills"] = int(
                st.get("research_fd_stale_generation_fills") or 0
            )
            excerpt["staged_second_leg_tp_realized_net"] = dict(
                st.get("staged_second_leg_tp_realized_net") or {}
            )
            excerpt["staged_second_leg_tp_required_net_total"] = dict(
                st.get("staged_second_leg_tp_required_net_total") or {}
            )
            excerpt["pending_cycle_loss_usdt"] = float(st.get("pending_cycle_loss_usdt") or 0.0)
        except Exception:
            excerpt.setdefault("research_fd_replan_events", [])
        result.final_strategy_state_excerpt = excerpt
        sim.close()

    if result.end_time is None:
        result.end_time = first_candle.timestamp
    result.input_slice_start_index = input_slice_start_index
    result.start_index = absolute_trade_start_index
    ensure_backtest_trade_block_ids(result)
    return result
