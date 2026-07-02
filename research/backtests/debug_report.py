"""Debug and validation helpers for backtest results (Phase 6)."""

from __future__ import annotations

from typing import Any

from .backtest_config_loader import BacktestConfigLoadResult, apply_config_load_result_to_backtest_result
from .backtest_report import BacktestResult
from .config_diagnostics import (
    build_backtest_config_diagnostics,
    build_exit_level_diagnostics_from_intents,
    compare_backtest_config_to_live_configs,
    config_diagnostics_summary_fields,
    extract_initial_exit_trigger,
)
from .hedge_bot_original_simulator import HedgeBotOriginalSimulator
from .intent_diagnostics import compute_final_active_order_diagnostics
from .purpose_utils import purpose_log_fields, preserve_bot_purpose
from .simulated_order_book import SyntheticCandle, VirtualOrder

STRATEGY_STATE_EXCERPT_KEYS = (
    "active_cycle_index",
    "next_required_purpose",
    "initial_structure_built",
    "refill_required",
    "refill_pending",
    "refill_in_progress",
    "recovery_mode",
    "pending_cycle_loss_usdt",
    "realized_long_loss_total",
    "realized_short_loss_total",
    "realized_long_pnl_total",
    "realized_short_pnl_total",
    "active_trade_block_id",
    "completed_cycle_count",
    "initial_long_entry_reconciled",
    "initial_short_entry_reconciled",
    "initial_entry_confirmed",
    "active_cycle_role",
    "cycles_since_last_refill",
)


def extract_strategy_state_excerpt(strategy_state: dict[str, Any]) -> dict[str, Any]:
    excerpt: dict[str, Any] = {}
    for key in STRATEGY_STATE_EXCERPT_KEYS:
        if key not in strategy_state:
            continue
        value = strategy_state.get(key)
        if value is None:
            continue
        excerpt[key] = value
    return excerpt


def active_order_to_dict(order: VirtualOrder) -> dict[str, Any]:
    metadata = dict(order.metadata or {})
    purpose_fields = purpose_log_fields(order.purpose, metadata)
    return {
        "order_id": order.order_id,
        "symbol": order.symbol,
        "side": order.side,
        "qty": float(order.qty),
        "price": order.price,
        "trigger_price": order.trigger_price,
        "trigger_direction": order.trigger_direction,
        "order_type": order.order_type,
        "reduce_only": bool(order.reduce_only),
        "status": order.status,
        **purpose_fields,
    }


def _format_waiting_for_order(order: dict[str, Any]) -> str:
    purpose = order.get("purpose") or "UNKNOWN"
    price = order.get("price")
    trigger = order.get("trigger_price")
    return f"waiting_for_order_fill:{purpose} price={price} trigger={trigger}"


def explain_open_reason(result: BacktestResult) -> str:
    """Explain why a backtest ended open or what blocked closure."""
    if result.final_status == "closed":
        return "closed"
    if result.final_status == "error":
        return f"error:{result.error or result.exit_reason or 'unknown'}"
    if result.exit_reason == "max_candles_reached":
        return "max_candles_reached"
    if result.exit_reason == "no_candles":
        return "no_candles"

    long_qty = float(result.final_long_qty or 0.0)
    short_qty = float(result.final_short_qty or 0.0)
    active_orders = list(result.final_active_orders or [])
    flat = long_qty <= 1e-12 and short_qty <= 1e-12

    if flat and active_orders:
        waiting = [_format_waiting_for_order(order) for order in active_orders]
        return "flat_but_active_orders|" + "; ".join(waiting)

    if active_orders:
        if len(active_orders) == 1:
            return _format_waiting_for_order(active_orders[0])
        waiting = [_format_waiting_for_order(order) for order in active_orders]
        return "active_orders_remaining|" + "; ".join(waiting)

    reasons: list[str] = []
    if long_qty > 1e-12:
        reasons.append("open_long_position")
    if short_qty > 1e-12:
        reasons.append("open_short_position")

    if reasons:
        if result.exit_reason == "series_end_with_open_positions":
            return "|".join(reasons) + "|series_end_with_open_positions"
        return "|".join(reasons)

    if result.exit_reason:
        return str(result.exit_reason)
    return "unknown"


def format_intent_line(intent: dict[str, Any]) -> str:
    return (
        f"{intent.get('candle_index')} {intent.get('timestamp')} "
        f"{intent.get('purpose')} {intent.get('side')} qty={intent.get('qty')} "
        f"price={intent.get('price')} trigger={intent.get('trigger_price')} "
        f"source_fill={intent.get('source_fill_purpose')}"
    )


def format_exit_diagnostic_line(item: dict[str, Any]) -> str:
    return (
        f"{item.get('final_order_purpose')} side={item.get('final_order_side')} "
        f"trigger={item.get('final_order_trigger_price')} "
        f"created_candle={item.get('created_candle_index')} "
        f"max_high={item.get('max_high_after_created')} "
        f"min_low={item.get('min_low_after_created')} "
        f"touchable={item.get('was_touchable_after_created')} "
        f"first_touch={item.get('first_touch_time_after_created')} "
        f"dist_max_high_pct={item.get('distance_to_max_high_pct')}"
    )



def _safe_float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _final_price_from_candles(candles: list[Any] | tuple[Any, ...] | None) -> float | None:
    if not candles:
        return None

    candle = candles[-1]

    for name in ("close", "close_price", "price"):
        value = getattr(candle, name, None)
        parsed = _safe_float_or_none(value)
        if parsed is not None:
            return parsed

    if isinstance(candle, dict):
        for key in ("close", "close_price", "price"):
            parsed = _safe_float_or_none(candle.get(key))
            if parsed is not None:
                return parsed

    return None


def calculate_unrealized_pnl(
    long_qty: float | None,
    long_avg_price: float | None,
    short_qty: float | None,
    short_avg_price: float | None,
    last_price: float | None,
) -> tuple[float | None, float | None, float | None]:
    """
    Calculate unrealized PnL components for a hedge position.

    - long_unrealized  = long_qty * (last_price - long_avg_price)
    - short_unrealized = short_qty * (short_avg_price - last_price)
    - total_unrealized = sum of both components when defined
    """
    final_price = _safe_float_or_none(last_price)
    long_qty_val = _safe_float_or_none(long_qty) or 0.0
    short_qty_val = _safe_float_or_none(short_qty) or 0.0
    long_avg_val = _safe_float_or_none(long_avg_price)
    short_avg_val = _safe_float_or_none(short_avg_price)

    if final_price is None:
        return None, None, None

    unrealized_long_pnl: float | None = None
    unrealized_short_pnl: float | None = None

    if long_avg_val is not None:
        unrealized_long_pnl = long_qty_val * (final_price - long_avg_val)

    if short_avg_val is not None:
        unrealized_short_pnl = short_qty_val * (short_avg_val - final_price)

    parts: list[float] = []
    if unrealized_long_pnl is not None:
        parts.append(unrealized_long_pnl)
    if unrealized_short_pnl is not None:
        parts.append(unrealized_short_pnl)

    total_unrealized = sum(parts) if parts else None
    return unrealized_long_pnl, unrealized_short_pnl, total_unrealized


def _set_unrealized_and_overall_pnl(result: BacktestResult, *, final_price: float | None) -> None:
    result.final_price = final_price

    realized_pnl = _safe_float_or_none(result.realized_pnl)
    final_long_qty = _safe_float_or_none(result.final_long_qty)
    final_short_qty = _safe_float_or_none(result.final_short_qty)
    final_long_avg_price = _safe_float_or_none(result.final_long_avg_price)
    final_short_avg_price = _safe_float_or_none(result.final_short_avg_price)

    unrealized_long_pnl, unrealized_short_pnl, total_unrealized = calculate_unrealized_pnl(
        final_long_qty,
        final_long_avg_price,
        final_short_qty,
        final_short_avg_price,
        final_price,
    )

    result.unrealized_long_pnl = unrealized_long_pnl
    result.unrealized_short_pnl = unrealized_short_pnl
    result.unrealized_pnl = total_unrealized

    if realized_pnl is not None and result.unrealized_pnl is not None:
        result.overall_pnl = realized_pnl + result.unrealized_pnl
    else:
        # Wenn keine saubere Kombination aus realized/unrealized vorliegt,
        # bleibt overall_pnl None.
        result.overall_pnl = None


def finalize_backtest_debug(
    result: BacktestResult,
    sim: HedgeBotOriginalSimulator,
    *,
    candles: list[SyntheticCandle] | None = None,
) -> None:
    """Populate debug fields on ``result`` from simulator/book/state."""
    result.final_long_qty = float(sim.book.long_qty)
    result.final_short_qty = float(sim.book.short_qty)
    result.final_long_avg_price = float(sim.book.long_avg) if sim.book.long_qty > 1e-12 else 0.0
    result.final_short_avg_price = float(sim.book.short_avg) if sim.book.short_qty > 1e-12 else 0.0
    _set_unrealized_and_overall_pnl(
        result,
        final_price=_final_price_from_candles(candles),
    )

    active_orders = [active_order_to_dict(order) for order in sim.book.active_orders()]
    result.final_active_orders = active_orders
    result.final_active_order_purposes = [
        preserve_bot_purpose(order.get("purpose")) for order in active_orders
    ]
    result.active_orders_count = len(active_orders)

    strategy_state = dict(sim.runtime_state.strategy_state)
    result.final_strategy_state_excerpt = extract_strategy_state_excerpt(strategy_state)

    if sim.order_log:
        for entry in sim.order_log:
            if entry not in result.order_log:
                result.order_log.append(entry)

    result.intent_log = list(sim.intent_log)

    active_virtual_orders = list(sim.book.active_orders())
    if candles is not None and active_virtual_orders:
        result.final_active_order_diagnostics = compute_final_active_order_diagnostics(
            active_virtual_orders,
            all_candles=candles,
            state=sim.runtime_state,
        )
    else:
        result.final_active_order_diagnostics = []

    if result.fill_log:
        result.last_fill = dict(result.fill_log[-1])
        result.first_fill_time = result.fill_log[0].get("timestamp")
        result.last_fill_time = result.fill_log[-1].get("timestamp")
    else:
        result.last_fill = None
        result.first_fill_time = None
        result.last_fill_time = None

    if result.order_log:
        result.last_order = dict(result.order_log[-1])
    else:
        result.last_order = None

    result.open_reason_detail = explain_open_reason(result)

    apply_config_load_result_to_backtest_result(
        result,
        BacktestConfigLoadResult(
            config=sim.config,
            config_source=getattr(sim, "config_source", "unknown"),
            config_path=getattr(sim, "config_path", None),
            config_loaded=bool(getattr(sim, "config_loaded", False)),
            config_load_warning=getattr(sim, "config_load_warning", None),
            config_unknown_keys=tuple(getattr(sim, "config_unknown_keys", []) or []),
            config_overlay_missing_keys=tuple(getattr(sim, "config_overlay_missing_keys", []) or []),
        ),
    )

    exit_trigger = extract_initial_exit_trigger(result.intent_log)
    result.config_diagnostics = build_backtest_config_diagnostics(
        sim.strategy,
        sim.config,
        symbol=result.symbol,
        entry_price=result.entry_price,
        config_source=getattr(sim, "config_source", "unknown"),
        config_path=getattr(sim, "config_path", None),
        config_loaded=bool(getattr(sim, "config_loaded", False)),
        config_load_warning=getattr(sim, "config_load_warning", None),
        config_unknown_keys=getattr(sim, "config_unknown_keys", []),
        loaded_bot_config=getattr(sim, "loaded_bot_config", {}),
        strategy_state=dict(sim.runtime_state.strategy_state),
        exit_trigger_price=exit_trigger,
        long_qty=float(sim.book.long_qty),
        short_qty=float(sim.book.short_qty),
        long_avg=float(sim.book.long_avg),
        short_avg=float(sim.book.short_avg),
    )
    result.live_config_comparison = compare_backtest_config_to_live_configs(sim.config)
    result.exit_level_diagnostics = build_exit_level_diagnostics_from_intents(result.intent_log)
    summary = config_diagnostics_summary_fields(result.config_diagnostics)
    for key, value in summary.items():
        setattr(result, key, value)


def format_fill_line(fill: dict[str, Any]) -> str:
    return (
        f"{fill.get('timestamp')} {fill.get('purpose')} {fill.get('side')} "
        f"qty={fill.get('qty')} @ {fill.get('fill_price')} pnl={fill.get('closed_pnl')}"
    )


def format_order_line(order: dict[str, Any]) -> str:
    return (
        f"{order.get('timestamp')} {order.get('event_type')} {order.get('purpose')} "
        f"{order.get('side')} qty={order.get('qty')} price={order.get('price')} "
        f"trigger={order.get('trigger_price')} status={order.get('status')}"
    )


def format_config_diagnostics_line(key: str, value: object) -> str:
    return f"{key}={value}"


def print_config_diagnostics_report(result: BacktestResult) -> None:
    print("config_diagnostics:")
    diagnostics = result.config_diagnostics or {}
    for key in (
        "strategy_class",
        "config_source",
        "config_path",
        "config_loaded",
        "config_load_warning",
        "config_unknown_keys",
        "config_overlay_missing_keys",
        "entry_price",
        "exit_trigger_price",
        "trigger_minus_entry",
        "trigger_distance_pct",
        "nearest_config_candidate",
        "nearest_config_candidate_source",
        "nearest_config_candidate_name",
    ):
        if key in diagnostics:
            print(f"  {format_config_diagnostics_line(key, diagnostics.get(key))}")

    loaded_bot = diagnostics.get("loaded_bot_config") or {}
    if loaded_bot:
        print("  loaded_bot_config:")
        for cfg_key, cfg_value in sorted(loaded_bot.items()):
            print(f"    {cfg_key}={cfg_value}")

    nearest = diagnostics.get("nearest_candidate_to_exit_trigger")
    if nearest:
        print(f"  nearest_match={nearest}")

    relevant = diagnostics.get("relevant_config") or {}
    if relevant:
        print("  relevant_config:")
        for cfg_key, cfg_value in sorted(relevant.items()):
            print(f"    {cfg_key}={cfg_value}")

    comparison = getattr(result, "live_config_comparison", None) or {}
    if comparison.get("differences"):
        print("  live_config_differences:")
        for direction, diff in comparison["differences"].items():
            print(f"    {direction}:")
            for cfg_key, values in sorted(diff.items()):
                print(f"      {cfg_key}: backtest={values.get('backtest')} live={values.get('live')}")
    for note in comparison.get("notes") or []:
        print(f"  note: {note}")

    exit_levels = getattr(result, "exit_level_diagnostics", None) or []
    if exit_levels:
        print("  exit_level_diagnostics:")
        for item in exit_levels:
            print(
                f"    {item.get('purpose')} trigger={item.get('trigger_price')} "
                f"entry={item.get('entry_price_at_intent')} abs={item.get('trigger_minus_entry')} "
                f"pct={item.get('trigger_distance_pct')} candidate={item.get('nearest_config_candidate')} "
                f"source={item.get('config_candidate_source')}"
            )


def print_debug_report(
    result: BacktestResult,
    *,
    print_fill_log: bool = False,
    print_order_log: bool = False,
    print_intent_log: bool = False,
    print_exit_diagnostics: bool = False,
    print_config_diagnostics: bool = False,
    config_summary: bool = True,
    tail: int = 5,
    intent_tail: int = 10,
) -> None:
    print(f"--- debug {result.direction} ---")
    print(
        f"fill_model={result.fill_model} max_fills_per_candle={result.max_fills_per_candle} "
        f"same_candle_fills_count={result.same_candle_fills_count} "
        f"paired_exit_fills_count={result.paired_exit_fills_count}"
    )
    print(f"final_status={result.final_status}")
    print(f"exit_reason={result.exit_reason}")
    print(f"open_reason_detail={result.open_reason_detail}")
    print(
        f"final_long_qty={result.final_long_qty} final_short_qty={result.final_short_qty} "
        f"active_orders={result.active_orders_count}"
    )
    if result.final_strategy_state_excerpt:
        print(f"strategy_excerpt={result.final_strategy_state_excerpt}")

    if config_summary and getattr(result, "config_source", None):
        print(
            f"config_source={result.config_source} config_path={getattr(result, 'config_path', None)} "
            f"price_tick_size={getattr(result, 'price_tick_size', None)} "
            f"tp_profit_target_pct={getattr(result, 'tp_profit_target_pct', None)} "
            f"initial_exit_trigger={getattr(result, 'initial_exit_trigger', None)} "
            f"trigger_dist_abs={getattr(result, 'initial_exit_trigger_distance_abs', None)} "
            f"nearest_candidate={getattr(result, 'nearest_config_candidate', None)}"
        )

    if print_config_diagnostics:
        print_config_diagnostics_report(result)

    if result.final_active_orders:
        print("active_orders:")
        for order in result.final_active_orders:
            print(
                f"  {order.get('purpose')} {order.get('side')} "
                f"qty={order.get('qty')} price={order.get('price')} "
                f"trigger={order.get('trigger_price')}"
            )
    else:
        print("active_orders: none")

    recent_fills = result.fill_log[-tail:] if result.fill_log else []
    if recent_fills:
        print(f"last_{len(recent_fills)}_fills:")
        for fill in recent_fills:
            print(f"  {format_fill_line(fill)}")

    recent_orders = result.order_log[-tail:] if result.order_log else []
    if recent_orders:
        print(f"last_{len(recent_orders)}_orders:")
        for order in recent_orders:
            print(f"  {format_order_line(order)}")

    if print_fill_log and result.fill_log:
        print("fill_log:")
        for fill in result.fill_log:
            print(f"  {format_fill_line(fill)}")

    if print_order_log and result.order_log:
        print("order_log:")
        for order in result.order_log:
            print(f"  {format_order_line(order)}")

    recent_intents = result.intent_log[-intent_tail:] if result.intent_log else []
    if recent_intents:
        print(f"last_{len(recent_intents)}_intents:")
        for intent in recent_intents:
            print(f"  {format_intent_line(intent)}")

    if result.final_active_order_diagnostics:
        print("final_active_order_diagnostics:")
        for item in result.final_active_order_diagnostics:
            print(f"  {format_exit_diagnostic_line(item)}")
    else:
        print("final_active_order_diagnostics: none")

    if print_intent_log and result.intent_log:
        print("intent_log:")
        for intent in result.intent_log:
            print(f"  {format_intent_line(intent)}")

    if print_exit_diagnostics and result.final_active_order_diagnostics:
        print("exit_diagnostics_full:")
        for item in result.final_active_order_diagnostics:
            print(f"  {format_exit_diagnostic_line(item)}")
