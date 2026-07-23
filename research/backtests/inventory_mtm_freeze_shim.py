"""Backtest-only shim: install inventory MTM freeze policies on the simulator.

``A0``/``None`` is a strict no-op — nothing is wrapped and the fill/intent path
stays byte-for-byte identical to the current live baseline. Variants
``A1``..``A6`` wrap ``sim.process_candle``/``sim.run_entry_smoke`` (to detect the
trigger causally) and install ``sim.intent_filter`` / a
``strategy._calculate_tp_projection`` wrap (to apply the freeze once triggered).

Extended research knobs (combined triggers, staged secondary gates, emergency
neutralization, post-trigger path diagnostics) are opt-in via
``InventoryMtmFreezeConfig`` and unused by the original A0..A6 / B0..B5 audits
when left at defaults.

No live config, runtime, or strategy default is ever touched by this module.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from fixed_cycle_hedge_bot.models import StrategyIntent

from .inventory_mtm_freeze import (
    FreezeRuntimeState,
    InventoryMtmFreezeConfig,
    apply_exit_freeze_long,
    apply_exit_freeze_short,
    evaluate_primary_trigger,
    exit_distance_pct,
    inventory_mtm_usdt,
    is_new_cycle_open_purpose,
    parse_cycle_number,
    required_recovery_move_pct,
    safe_float,
    would_increase_abs_net_exposure,
)
from .safe_cycle_boundary_freeze import (
    FREEZE_ACTIVE,
    FREEZE_NORMAL,
    FREEZE_PENDING,
    SafeBoundaryRuntime,
    classify_allowed_pending_action,
    is_direction_aware_cycle_opener,
    is_next_cycle_first_leg_opener,
    resolve_requested_cycle_at_trigger,
    safe_boundary_ready,
)
from .simulated_execution import fill_order_at_candle_close, stamp_order_causal_eligibility

_CYCLE_FILTER_VARIANTS = frozenset({"A1", "A4", "A5", "A6"})
_EXPOSURE_FILTER_VARIANTS = frozenset({"A2", "A4", "A5", "A6"})
_EXIT_FREEZE_VARIANTS = frozenset({"A3", "A4", "A5", "A6"})
_NEUTRALIZE_VARIANTS = frozenset({"A5", "A6"})

NEUTRALIZE_PURPOSE = "INVENTORY_MTM_FREEZE_NEUTRALIZE"
EMERGENCY_NEUTRALIZE_PURPOSE = "INVENTORY_MTM_EMERGENCY_NEUTRALIZE"


def _fill_closed_pnl(fill: Any) -> float:
    metadata = dict(getattr(fill, "metadata", None) or {})
    value = metadata.get("confirmed_closed_pnl")
    if value is None:
        value = metadata.get("closed_pnl")
    return safe_float(value, 0.0)


def _cycles_seen(strategy_state: dict[str, Any]) -> int:
    active = int(strategy_state.get("active_cycle_index") or 0)
    completed = int(strategy_state.get("completed_cycle_count") or 0)
    return max(active, completed)


def _resolve_active_exit_price(strategy: Any, runtime_state: Any, *, primary_side: str) -> float | None:
    """Best-effort resolution of the currently active basket exit price."""
    state = dict(getattr(runtime_state, "strategy_state", None) or {})
    if str(primary_side or "long").lower() == "long":
        latest = safe_float(state.get("latest_tp_price"))
        if latest > 0:
            return latest
        purpose = getattr(strategy, "LONG_TP_EXIT_PURPOSE", "LONG_TP_EXIT")
    else:
        purpose = getattr(strategy, "SHORT_SL_EXIT_PURPOSE", "SHORT_SL_EXIT")
    snapshot = getattr(runtime_state, "last_snapshot", None)
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


def _resolve_primary_side(strategy: Any) -> str:
    if hasattr(strategy, "_get_primary_position_side"):
        try:
            return str(strategy._get_primary_position_side() or "long")
        except Exception:
            return "long"
    return "long"


def install_inventory_mtm_freeze(sim: Any, config: InventoryMtmFreezeConfig | None) -> None:
    """Install a backtest-only inventory-MTM freeze policy on ``sim``.

    Must be called after ``sim.strategy`` has been constructed (the shim wraps
    ``sim.process_candle``, ``sim.run_entry_smoke``, ``sim.intent_filter`` and
    optionally ``sim.strategy._calculate_tp_projection``).
    """
    strategy = sim.strategy
    if config is None or config.variant == "A0":
        strategy._backtest_inventory_mtm_freeze_variant = "A0"
        strategy._backtest_inventory_mtm_freeze_config = config
        strategy._backtest_inventory_mtm_trigger_event = None
        strategy._backtest_inventory_mtm_trigger_events = []
        strategy._backtest_inventory_mtm_policy_actions = []
        strategy._backtest_inventory_mtm_freeze_state = None
        return

    if getattr(sim, "_inventory_mtm_freeze_shim_installed", False):
        strategy._backtest_inventory_mtm_freeze_variant = config.variant
        strategy._backtest_inventory_mtm_freeze_config = config
        return

    primary_side = _resolve_primary_side(strategy)
    state = FreezeRuntimeState(variant=config.variant)
    if config.emergency_neutralize_after_candles is not None:
        state.emergency_armed = True
    if config.safe_cycle_boundary:
        state.safe_boundary = SafeBoundaryRuntime(
            arm_mode=str(config.safe_boundary_arm_mode or "mtm"),
            stop_after_cycle=config.stop_after_cycle,
        )

    strategy._backtest_inventory_mtm_freeze_variant = config.variant
    strategy._backtest_inventory_mtm_freeze_config = config
    strategy._backtest_inventory_mtm_trigger_event = None
    strategy._backtest_inventory_mtm_trigger_events = []
    strategy._backtest_inventory_mtm_policy_actions = state.policy_actions
    strategy._backtest_inventory_mtm_freeze_state = state

    def _log_action(action: dict[str, Any]) -> None:
        state.policy_actions.append(action)

    _throttle_last: dict[str, tuple[int, tuple[Any, ...]]] = {}
    _THROTTLE_CANDLES = 200

    def _log_action_throttled(
        *, key: str, action: dict[str, Any], candle_index: int, signature: tuple[Any, ...]
    ) -> None:
        last = _throttle_last.get(key)
        if last is not None:
            last_candle_index, last_signature = last
            if signature == last_signature and (candle_index - last_candle_index) < _THROTTLE_CANDLES:
                return
        _throttle_last[key] = (candle_index, signature)
        _log_action(action)

    def _apply_neutralization(
        result: Any,
        *,
        fraction: float,
        purpose: str,
        action_name: str,
    ) -> None:
        long_qty = float(sim.book.long_qty)
        short_qty = float(sim.book.short_qty)
        net = long_qty - short_qty
        if abs(net) <= 1e-9:
            return
        fraction = min(max(float(fraction), 0.0), 1.0)
        reduce_qty = abs(net) * fraction
        if reduce_qty <= 1e-9:
            return

        if net > 0:
            side, qty = "long", min(reduce_qty, long_qty)
        else:
            side, qty = "short", min(reduce_qty, short_qty)
        if qty <= 1e-9:
            return

        intent = StrategyIntent(
            side=side,
            qty=float(qty),
            purpose=purpose,
            order_type="Market",
            reduce_only=True,
        )
        order, _ = sim.book.submit_intent(intent, replace=False)
        stamp_order_causal_eligibility(order, created_candle_index=sim.candle_index)
        fill_event = fill_order_at_candle_close(
            book=sim.book,
            runtime_state=sim.runtime_state,
            order_id=order.order_id,
            candle=sim.candle,
        )
        pnl = _fill_closed_pnl(fill_event)
        state.realized_pnl += pnl
        result.candle_fills.append(fill_event)
        sim._refresh_snapshot_from_book(source="after_inventory_mtm_freeze_neutralize")
        _log_action(
            {
                "action": action_name,
                "candle_index": int(sim.candle_index),
                "side": side,
                "qty": float(qty),
                "fill_price": float(fill_event.exec_price),
                "closed_pnl": pnl,
                "net_before": net,
                "fraction": fraction,
            }
        )

    def _apply_variant_neutralization(result: Any) -> None:
        if state.neutralization_done:
            return
        long_qty = float(sim.book.long_qty)
        short_qty = float(sim.book.short_qty)
        net = long_qty - short_qty
        if abs(net) <= 1e-9:
            state.neutralization_done = True
            return
        fraction = 1.0 if config.variant == "A6" else float(config.partial_neutralize_fraction)
        _apply_neutralization(
            result,
            fraction=fraction,
            purpose=NEUTRALIZE_PURPOSE,
            action_name="neutralize" if config.variant == "A6" else "partial_neutralize",
        )
        state.neutralization_done = True

    def _current_mtm() -> float:
        return inventory_mtm_usdt(
            realized=state.realized_pnl,
            long_qty=float(sim.book.long_qty),
            long_avg=float(sim.book.long_avg),
            short_qty=float(sim.book.short_qty),
            short_avg=float(sim.book.short_avg),
            mark=float(sim.candle.close),
        )

    def _update_post_trigger_path() -> None:
        if not state.triggered:
            return
        candle_index = int(sim.candle_index)
        mark = float(sim.candle.close)
        mtm = _current_mtm()
        if state.worst_mtm_after_trigger is None or mtm < state.worst_mtm_after_trigger:
            state.worst_mtm_after_trigger = mtm
        if state.min_mark_after_trigger is None or mark < state.min_mark_after_trigger:
            state.min_mark_after_trigger = mark
        if state.max_mark_after_trigger is None or mark > state.max_mark_after_trigger:
            state.max_mark_after_trigger = mark
        trigger_mark = float(state.trigger_mark or mark)
        if trigger_mark > 0:
            if primary_side == "long":
                adverse = (trigger_mark - float(state.min_mark_after_trigger or trigger_mark)) / trigger_mark
                favorable = (float(state.max_mark_after_trigger or trigger_mark) - trigger_mark) / trigger_mark
            else:
                adverse = (float(state.max_mark_after_trigger or trigger_mark) - trigger_mark) / trigger_mark
                favorable = (trigger_mark - float(state.min_mark_after_trigger or trigger_mark)) / trigger_mark
            state.max_adverse_price_move_after_trigger = max(0.0, adverse)
            state.max_favorable_price_move_after_trigger = max(0.0, favorable)
        if state.first_reclaim_candle is None and mtm >= 0.0:
            state.first_reclaim_candle = candle_index

    def _enter_safe_boundary_pending(
        *,
        candle_index: int,
        requested_cycle: int,
        reason: str,
        mtm: float | None = None,
    ) -> None:
        sb = state.safe_boundary
        assert sb is not None
        if sb.freeze_state != FREEZE_NORMAL:
            return
        sb.freeze_state = FREEZE_PENDING
        sb.freeze_requested_at_candle = candle_index
        sb.freeze_requested_cycle = int(requested_cycle)
        sb.safe_boundary_reason = reason
        sb.log(
            "freeze_requested",
            candle_index=candle_index,
            requested_cycle=int(requested_cycle),
            reason=reason,
            mtm=mtm,
            safe_boundary_variant=config.safe_boundary_variant,
        )
        sb.log(
            "freeze_pending_entered",
            candle_index=candle_index,
            requested_cycle=int(requested_cycle),
            reason=reason,
        )
        _log_action(
            {
                "action": "freeze_requested",
                "candle_index": candle_index,
                "requested_cycle": int(requested_cycle),
                "reason": reason,
                "mtm": mtm,
                "safe_boundary_variant": config.safe_boundary_variant,
            }
        )
        _log_action(
            {
                "action": "freeze_pending_entered",
                "candle_index": candle_index,
                "requested_cycle": int(requested_cycle),
                "reason": reason,
            }
        )

    def _maybe_activate_safe_boundary(*, source: str) -> None:
        if not config.safe_cycle_boundary or state.safe_boundary is None:
            return
        sb = state.safe_boundary
        if sb.freeze_state != FREEZE_PENDING:
            return
        strategy_state = dict(sim.runtime_state.strategy_state or {})
        requested = int(sb.freeze_requested_cycle or 1)
        ready, reason = safe_boundary_ready(
            strategy_state,
            requested_cycle=requested,
            primary_side=primary_side,
            long_qty=float(sim.book.long_qty),
            short_qty=float(sim.book.short_qty),
        )
        sb.safe_boundary_reason = reason
        if not ready:
            return
        candle_index = int(sim.candle_index)
        signature = strategy_state.get("last_exit_signature")
        sb.freeze_state = FREEZE_ACTIVE
        sb.freeze_activated_at_candle = candle_index
        sb.freeze_activated_after_cycle = requested
        sb.exit_signature_at_activation = signature
        sb.safe_boundary_reason = f"activated:{reason}"
        # Legacy flag unused for safe-boundary blocking; keep False so A1 path
        # does not double-apply is_new_cycle_open_purpose.
        state.cycle_freeze_enabled = False
        payload = {
            "action": "freeze_activated",
            "candle_index": candle_index,
            "activated_after_cycle": requested,
            "exit_signature_at_activation": signature,
            "ready_reason": reason,
            "source": source,
            "safe_boundary_variant": config.safe_boundary_variant,
        }
        sb.log("freeze_activated", **{k: v for k, v in payload.items() if k != "action"})
        sb.log("cycle_complete_confirmed", cycle=requested, candle_index=candle_index)
        sb.log(
            "exit_rebuild_committed",
            candle_index=candle_index,
            exit_signature=signature,
        )
        _log_action(payload)
        _log_action(
            {
                "action": "cycle_complete_confirmed",
                "candle_index": candle_index,
                "cycle": requested,
            }
        )
        _log_action(
            {
                "action": "exit_rebuild_committed",
                "candle_index": candle_index,
                "exit_signature": signature,
            }
        )

    def _maybe_arm_stop_after_cycle() -> None:
        if not config.safe_cycle_boundary or state.safe_boundary is None:
            return
        if str(config.safe_boundary_arm_mode or "") != "stop_after_cycle":
            return
        sb = state.safe_boundary
        if sb.freeze_state != FREEZE_NORMAL or sb.armed_from_entry:
            return
        long_qty = float(sim.book.long_qty)
        short_qty = float(sim.book.short_qty)
        if abs(long_qty) <= 1e-9 and abs(short_qty) <= 1e-9:
            return
        candle_index = int(sim.candle_index)
        stop_n = int(config.stop_after_cycle or 1)
        sb.armed_from_entry = True
        # Mark triggered so terminal-stop / audit hooks see a freeze request.
        if not state.triggered:
            state.triggered = True
            state.trigger_candle = candle_index
            state.trigger_mtm = _current_mtm()
            state.trigger_mark = float(sim.candle.close)
            state.trigger_long_qty = long_qty
            state.trigger_short_qty = short_qty
            state.cycles_at_trigger = _cycles_seen(dict(sim.runtime_state.strategy_state))
            state.trigger_condition_details = {
                "arm_mode": "stop_after_cycle",
                "stop_after_cycle": stop_n,
            }
            event = {
                "variant": config.variant,
                "safe_boundary_variant": config.safe_boundary_variant,
                "trigger_candle": candle_index,
                "trigger_mtm": state.trigger_mtm,
                "trigger_mark": state.trigger_mark,
                "long_qty": long_qty,
                "short_qty": short_qty,
                "cycles_at_trigger": state.cycles_at_trigger,
                "arm_mode": "stop_after_cycle",
                "stop_after_cycle": stop_n,
                "primary_side": primary_side,
            }
            strategy._backtest_inventory_mtm_trigger_event = event
            strategy._backtest_inventory_mtm_trigger_events.append(event)
        _enter_safe_boundary_pending(
            candle_index=candle_index,
            requested_cycle=stop_n,
            reason=f"stop_after_cycle_{stop_n}",
            mtm=state.trigger_mtm,
        )

    def _maybe_fire_trigger(result: Any) -> None:
        if state.triggered:
            return
        # stop_after_cycle arms via _maybe_arm_stop_after_cycle, not MTM.
        if config.safe_cycle_boundary and str(config.safe_boundary_arm_mode or "") == "stop_after_cycle":
            return
        candle_index = int(sim.candle_index)
        if candle_index < 0 or candle_index > int(config.max_trigger_candle):
            return
        mark = float(sim.candle.close)
        long_qty = float(sim.book.long_qty)
        long_avg = float(sim.book.long_avg)
        short_qty = float(sim.book.short_qty)
        short_avg = float(sim.book.short_avg)
        mtm = inventory_mtm_usdt(
            realized=state.realized_pnl,
            long_qty=long_qty,
            long_avg=long_avg,
            short_qty=short_qty,
            short_avg=short_avg,
            mark=mark,
        )
        cycles = _cycles_seen(dict(sim.runtime_state.strategy_state))
        active_exit = _resolve_active_exit_price(strategy, sim.runtime_state, primary_side=primary_side)
        rrm = required_recovery_move_pct(mark=mark, active_exit=active_exit, primary_side=primary_side)
        should_fire, details = evaluate_primary_trigger(
            config=config,
            mtm=mtm,
            cycle_count=cycles,
            exit_increase_count=state.exit_increases_lifetime,
            required_recovery_move=rrm,
        )
        if not should_fire:
            return

        state.triggered = True
        state.trigger_candle = candle_index
        state.trigger_mtm = mtm
        state.trigger_mark = mark
        state.trigger_long_qty = long_qty
        state.trigger_long_avg = long_avg
        state.trigger_short_qty = short_qty
        state.trigger_short_avg = short_avg
        state.cycles_at_trigger = cycles
        state.active_exit_at_trigger = active_exit
        state.net_exposure_at_trigger = long_qty - short_qty
        state.exit_increases_at_trigger = int(state.exit_increases_lifetime)
        state.trigger_gross_notional = long_qty * long_avg + short_qty * short_avg
        state.trigger_net_exposure_usdt = (long_qty - short_qty) * mark
        state.trigger_exit_distance_pct = exit_distance_pct(mark=mark, active_exit=active_exit)
        state.trigger_required_recovery_move_pct = rrm
        state.trigger_pending_cycle_loss = max(0.0, -mtm)
        state.trigger_condition_details = details
        state.worst_mtm_after_trigger = mtm
        state.min_mark_after_trigger = mark
        state.max_mark_after_trigger = mark
        state.max_adverse_price_move_after_trigger = 0.0
        state.max_favorable_price_move_after_trigger = 0.0

        if primary_side == "long":
            state.latched_exit_ceiling = active_exit
        else:
            state.latched_exit_floor = active_exit

        event = {
            "variant": config.variant,
            "safe_boundary_variant": config.safe_boundary_variant,
            "trigger_candle": candle_index,
            "trigger_mtm": mtm,
            "trigger_mark": mark,
            "long_qty": long_qty,
            "long_avg": long_avg,
            "short_qty": short_qty,
            "short_avg": short_avg,
            "cycles_at_trigger": state.cycles_at_trigger,
            "active_exit_at_trigger": active_exit,
            "net_exposure_at_trigger": state.net_exposure_at_trigger,
            "exit_increases_at_trigger": state.exit_increases_at_trigger,
            "gross_notional_at_trigger": state.trigger_gross_notional,
            "net_exposure_usdt_at_trigger": state.trigger_net_exposure_usdt,
            "exit_distance_pct_at_trigger": state.trigger_exit_distance_pct,
            "required_recovery_move_pct_at_trigger": state.trigger_required_recovery_move_pct,
            "realized_cycle_pnl_at_trigger": state.realized_pnl,
            "pending_cycle_loss_at_trigger": state.trigger_pending_cycle_loss,
            "trigger_condition_details": details,
            "primary_side": primary_side,
        }
        strategy._backtest_inventory_mtm_trigger_event = event
        strategy._backtest_inventory_mtm_trigger_events.append(event)
        _log_action({"action": "trigger_fired", "candle_index": candle_index, **event})

        if config.safe_cycle_boundary:
            requested = resolve_requested_cycle_at_trigger(dict(sim.runtime_state.strategy_state))
            _enter_safe_boundary_pending(
                candle_index=candle_index,
                requested_cycle=requested,
                reason="mtm_trigger",
                mtm=mtm,
            )
            # Do NOT enable immediate cycle freeze — PENDING only.
            return

        if config.variant in _NEUTRALIZE_VARIANTS:
            _apply_variant_neutralization(result)

        if config.staged_cycle_freeze:
            state.candles_below_threshold_since_trigger = 1
            _log_action(
                {
                    "action": "stage1_exposure_freeze",
                    "candle_index": candle_index,
                    "trigger_mtm": mtm,
                }
            )
        elif config.variant in _CYCLE_FILTER_VARIANTS:
            # Non-staged A1-style: cycle freeze is active immediately on trigger.
            state.cycle_freeze_enabled = True

    def _maybe_escalate_to_cycle_freeze() -> None:
        """Stage 2: escalate exposure-only freeze to an A1-style cycle freeze."""
        if not config.staged_cycle_freeze or not state.triggered or state.cycle_freeze_enabled:
            return
        candle_index = int(sim.candle_index)
        if candle_index == state.trigger_candle:
            mtm = state.trigger_mtm
        else:
            mtm = _current_mtm()
            if mtm is not None and mtm < config.threshold_usdt:
                state.candles_below_threshold_since_trigger += 1
            else:
                state.candles_below_threshold_since_trigger = 0

        cycles = _cycles_seen(dict(sim.runtime_state.strategy_state))
        reason: str | None = None
        if (
            config.secondary_use_hold
            and state.candles_below_threshold_since_trigger >= config.secondary_hold_candles_below_threshold
        ):
            reason = "hold_candles_below_threshold"
        elif (
            config.secondary_use_mtm
            and mtm is not None
            and mtm < config.secondary_mtm_threshold_usdt
        ):
            reason = "mtm_below_secondary_threshold"
        elif (
            config.secondary_use_exit_increase
            and state.exit_increases_after_trigger >= config.secondary_exit_increase_count
        ):
            reason = "exit_increase_count"
        elif (
            config.secondary_use_cycle
            and config.secondary_cycle_count is not None
            and cycles >= int(config.secondary_cycle_count)
        ):
            reason = "cycle_count"

        if reason is None:
            return

        state.cycle_freeze_enabled = True
        state.secondary_trigger_candle = candle_index
        state.secondary_trigger_reason = reason
        _log_action(
            {
                "action": "stage2_cycle_freeze",
                "candle_index": candle_index,
                "reason": reason,
                "mtm": mtm,
                "candles_below_threshold_since_trigger": state.candles_below_threshold_since_trigger,
                "exit_increases_after_trigger": state.exit_increases_after_trigger,
                "cycles": cycles,
            }
        )

    def _maybe_emergency_neutralize(result: Any) -> None:
        if not state.emergency_armed or state.emergency_fired or not state.triggered:
            return
        if config.emergency_neutralize_after_candles is None:
            return
        candle_index = int(sim.candle_index)
        trigger_candle = int(state.trigger_candle or 0)
        if candle_index - trigger_candle < int(config.emergency_neutralize_after_candles):
            return
        long_qty = float(sim.book.long_qty)
        short_qty = float(sim.book.short_qty)
        if abs(long_qty) <= 1e-9 and abs(short_qty) <= 1e-9:
            return
        _apply_neutralization(
            result,
            fraction=float(config.emergency_neutralize_fraction),
            purpose=EMERGENCY_NEUTRALIZE_PURPOSE,
            action_name="emergency_partial_neutralize",
        )
        state.emergency_fired = True
        state.emergency_candle = candle_index
        state.neutralization_done = True
        state.force_exposure_freeze_after_emergency = True
        state.cycle_freeze_enabled = True
        _log_action(
            {
                "action": "emergency_arm_exposure_and_cycle_freeze",
                "candle_index": candle_index,
                "window_candles": config.emergency_neutralize_after_candles,
                "fraction": config.emergency_neutralize_fraction,
            }
        )

    def _accumulate_realized(fills: list[Any]) -> None:
        for fill in fills:
            state.realized_pnl += _fill_closed_pnl(fill)

    def _observe_cycle_opens_after_trigger(fills: list[Any]) -> None:
        if not state.triggered:
            return
        for fill in fills:
            purpose = str(getattr(fill, "purpose", "") or "")
            if config.safe_cycle_boundary:
                opener = is_direction_aware_cycle_opener(purpose, primary_side=primary_side)
            else:
                opener = is_new_cycle_open_purpose(purpose, primary_side=primary_side)
            if opener:
                state.cycles_after_trigger += 1
                sb = state.safe_boundary
                if sb is not None and sb.freeze_state == FREEZE_PENDING:
                    cycle_n = parse_cycle_number(purpose)
                    if cycle_n is not None and cycle_n == sb.freeze_requested_cycle:
                        sb.log(
                            "current_cycle_first_leg_seen",
                            candle_index=int(sim.candle_index),
                            purpose=purpose,
                            cycle=cycle_n,
                        )
                        _log_action(
                            {
                                "action": "current_cycle_first_leg_seen",
                                "candle_index": int(sim.candle_index),
                                "purpose": purpose,
                                "cycle": cycle_n,
                            }
                        )

    original_run_entry_smoke = sim.run_entry_smoke

    def _wrapped_run_entry_smoke() -> Any:
        entry_result = original_run_entry_smoke()
        _accumulate_realized(list(entry_result.entry_fills or []))
        if config.safe_cycle_boundary:
            _maybe_arm_stop_after_cycle()
            _maybe_activate_safe_boundary(source="after_entry_smoke")
        return entry_result

    original_process_candle = sim.process_candle

    def _wrapped_process_candle(candle: Any, **kwargs: Any) -> Any:
        result = original_process_candle(candle, **kwargs)
        fills = list(result.candle_fills or [])
        _accumulate_realized(fills)
        _observe_cycle_opens_after_trigger(fills)
        if config.safe_cycle_boundary:
            _maybe_arm_stop_after_cycle()
        _maybe_fire_trigger(result)
        if config.safe_cycle_boundary:
            _maybe_activate_safe_boundary(source="after_process_candle")
        _maybe_escalate_to_cycle_freeze()
        _maybe_emergency_neutralize(result)
        _update_post_trigger_path()
        if (
            config.safe_cycle_boundary
            and state.safe_boundary is not None
            and state.triggered
            and abs(float(sim.book.long_qty)) <= 1e-9
            and abs(float(sim.book.short_qty)) <= 1e-9
        ):
            sb = state.safe_boundary
            if not any(e.get("action") == "flat_reached" for e in sb.events):
                sb.log("flat_reached", candle_index=int(sim.candle_index))
                _log_action({"action": "flat_reached", "candle_index": int(sim.candle_index)})
        return result

    sim.run_entry_smoke = _wrapped_run_entry_smoke  # type: ignore[method-assign]
    sim.process_candle = _wrapped_process_candle  # type: ignore[method-assign]

    original_submit_intents = getattr(sim, "submit_intents_to_book", None)

    if config.safe_cycle_boundary and original_submit_intents is not None:

        def _wrapped_submit_intents_to_book(intents: Any, **kwargs: Any) -> Any:
            # Activate before filtering so a just-committed exit can drop the
            # next first-leg opener in the same rebuild batch.
            _maybe_activate_safe_boundary(source="before_submit_intents")
            return original_submit_intents(intents, **kwargs)

        sim.submit_intents_to_book = _wrapped_submit_intents_to_book  # type: ignore[method-assign]

    def _inventory_mtm_freeze_intent_filter(intent: Any) -> bool:
        purpose = str(getattr(intent, "purpose", "") or "")
        candle_index = int(sim.candle_index)

        if config.safe_cycle_boundary and state.safe_boundary is not None:
            sb = state.safe_boundary
            if sb.freeze_state == FREEZE_NORMAL:
                return True
            if sb.freeze_state == FREEZE_PENDING:
                # Never block while pending — finish the in-flight cycle.
                kind = classify_allowed_pending_action(purpose, primary_side=primary_side)
                if kind is not None:
                    sb.allowed_current_cycle_action_count += 1
                    action_name = {
                        "second_leg": "second_leg_allowed_while_pending",
                        "refill": "refill_allowed_while_pending",
                        "exit": "exit_rebuild_allowed_while_pending",
                        "coverage": "coverage_allowed_while_pending",
                    }.get(kind, f"{kind}_allowed_while_pending")
                    # Staged/split second legs share purpose; refine via flags.
                    strategy_state = dict(sim.runtime_state.strategy_state or {})
                    if kind == "second_leg" and (
                        strategy_state.get("normal_cycle_second_leg_split_stage_count")
                        or strategy_state.get("staged_second_leg_tp_stage_count")
                        or strategy_state.get("cycle_waiting_for_short_tp")
                    ):
                        action_name = "staged_second_leg_allowed_while_pending"
                    sb.log(
                        action_name,
                        candle_index=candle_index,
                        purpose=purpose,
                        side=getattr(intent, "side", None),
                        reduce_only=getattr(intent, "reduce_only", None),
                    )
                    _log_action_throttled(
                        key=f"pending_allow:{action_name}:{purpose}",
                        action={
                            "action": action_name,
                            "purpose": purpose,
                            "candle_index": candle_index,
                            "side": getattr(intent, "side", None),
                            "reduce_only": getattr(intent, "reduce_only", None),
                        },
                        candle_index=candle_index,
                        signature=(purpose, action_name),
                    )
                return True

            # FREEZE_ACTIVE: block only DirectionConfig first-leg openers of next cycles.
            activated_after = int(sb.freeze_activated_after_cycle or 0)
            if is_next_cycle_first_leg_opener(
                purpose,
                primary_side=primary_side,
                activated_after_cycle=activated_after,
            ):
                cycle_n = parse_cycle_number(purpose)
                sb.blocked_opener_count += 1
                if purpose not in sb.blocked_opener_purposes:
                    sb.blocked_opener_purposes.append(purpose)
                block_payload = {
                    "action": "next_cycle_opener_blocked",
                    "purpose": purpose,
                    "candle_index": candle_index,
                    "cycle": cycle_n,
                    "side": getattr(intent, "side", None),
                    "reduce_only": getattr(intent, "reduce_only", None),
                    "freeze_state": sb.freeze_state,
                    "completed_cycle": activated_after,
                    "reason": "safe_boundary_active_blocks_next_first_leg_opener",
                    "safe_boundary_variant": config.safe_boundary_variant,
                }
                sb.log(**block_payload)
                _log_action_throttled(
                    key=f"block_safe_opener:{purpose}",
                    action=block_payload,
                    candle_index=candle_index,
                    signature=(purpose, cycle_n),
                )
                return False
            return True

        if not state.triggered:
            return True

        cycle_freeze_active = (
            config.variant in _CYCLE_FILTER_VARIANTS
            or (config.staged_cycle_freeze and state.cycle_freeze_enabled)
            or state.force_exposure_freeze_after_emergency
            or (not config.staged_cycle_freeze and state.cycle_freeze_enabled)
        )
        if cycle_freeze_active and is_new_cycle_open_purpose(purpose, primary_side=primary_side):
            _log_action_throttled(
                key=f"block_new_cycle:{purpose}",
                action={
                    "action": "block_new_cycle",
                    "purpose": purpose,
                    "candle_index": candle_index,
                },
                candle_index=candle_index,
                signature=(purpose,),
            )
            return False

        exposure_freeze_active = (
            config.variant in _EXPOSURE_FILTER_VARIANTS or state.force_exposure_freeze_after_emergency
        )
        if exposure_freeze_active and would_increase_abs_net_exposure(
            long_qty=float(sim.book.long_qty),
            short_qty=float(sim.book.short_qty),
            side=str(getattr(intent, "side", "") or ""),
            qty=float(getattr(intent, "qty", 0.0) or 0.0),
            reduce_only=bool(getattr(intent, "reduce_only", False)),
        ):
            side = getattr(intent, "side", None)
            qty = getattr(intent, "qty", None)
            reduce_only = getattr(intent, "reduce_only", None)
            _log_action_throttled(
                key=f"block_exposure_growth:{purpose}",
                action={
                    "action": "block_exposure_growth",
                    "purpose": purpose,
                    "side": side,
                    "qty": qty,
                    "reduce_only": reduce_only,
                    "candle_index": candle_index,
                },
                candle_index=candle_index,
                signature=(purpose, side, qty, reduce_only),
            )
            return False

        return True

    existing_filter = sim.intent_filter
    if existing_filter is None:
        sim.intent_filter = _inventory_mtm_freeze_intent_filter
    else:

        def _combined_filter(intent: Any) -> bool:
            return bool(existing_filter(intent)) and _inventory_mtm_freeze_intent_filter(intent)

        sim.intent_filter = _combined_filter

    _apply_exit_freeze = (not config.safe_cycle_boundary) and config.variant in _EXIT_FREEZE_VARIANTS
    _observe_exit_increases = (
        bool(config.staged_cycle_freeze)
        or bool(config.use_exit_increase_trigger)
        or bool(config.secondary_use_exit_increase)
        or True  # always observe for diagnostics / exit_increases_at_trigger
    )

    if _apply_exit_freeze or _observe_exit_increases:
        original_tp_projection = strategy._calculate_tp_projection

        def _wrapped_tp_projection(
            break_even_price: float,
            snapshot: Any = None,
            runtime_state: Any = None,
        ) -> Any:
            projection = original_tp_projection(break_even_price, snapshot, runtime_state)
            raw_exit = safe_float(getattr(projection, "tp_price", 0.0))
            if raw_exit > 0:
                prev = state.last_observed_exit
                if prev is not None:
                    if primary_side == "long" and raw_exit > prev + 1e-9:
                        state.exit_increases_lifetime += 1
                    elif primary_side != "long" and raw_exit < prev - 1e-9:
                        state.exit_increases_lifetime += 1
                state.last_observed_exit = raw_exit

            if not state.triggered:
                return projection

            if primary_side == "long":
                if state.latched_exit_ceiling is None:
                    state.latched_exit_ceiling = raw_exit
                    return projection
                effective = apply_exit_freeze_long(
                    raw_exit=raw_exit,
                    latched_ceiling=state.latched_exit_ceiling,
                    active_exit=raw_exit,
                )
            else:
                if state.latched_exit_floor is None:
                    state.latched_exit_floor = raw_exit
                    return projection
                effective = apply_exit_freeze_short(
                    raw_exit=raw_exit,
                    latched_floor=state.latched_exit_floor,
                    active_exit=raw_exit,
                )

            if abs(effective - raw_exit) <= 1e-9:
                return projection

            state.exit_increases_after_trigger += 1
            candle_index = int(sim.candle_index)
            action_name = "prevent_exit_increase" if _apply_exit_freeze else "observed_exit_increase"
            _log_action_throttled(
                key=action_name,
                action={
                    "action": action_name,
                    "candle_index": candle_index,
                    "raw_exit": raw_exit,
                    "effective_exit": effective,
                    "latched_ceiling": state.latched_exit_ceiling,
                    "latched_floor": state.latched_exit_floor,
                    "applied": _apply_exit_freeze,
                },
                candle_index=candle_index,
                signature=(round(raw_exit, 6), round(effective, 6)),
            )
            if not _apply_exit_freeze:
                return projection
            return replace(projection, tp_price=float(effective))

        strategy._calculate_tp_projection = _wrapped_tp_projection  # type: ignore[method-assign]

    sim._inventory_mtm_freeze_shim_installed = True
