"""Debug and validation helpers for backtest results (Phase 6)."""

from __future__ import annotations

from typing import Any

from .backtest_report import BacktestResult
from .hedge_bot_original_simulator import HedgeBotOriginalSimulator
from .purpose_utils import purpose_log_fields, preserve_bot_purpose
from .simulated_order_book import VirtualOrder

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


def finalize_backtest_debug(result: BacktestResult, sim: HedgeBotOriginalSimulator) -> None:
    """Populate debug fields on ``result`` from simulator/book/state."""
    result.final_long_qty = float(sim.book.long_qty)
    result.final_short_qty = float(sim.book.short_qty)
    result.final_long_avg_price = float(sim.book.long_avg) if sim.book.long_qty > 1e-12 else 0.0
    result.final_short_avg_price = float(sim.book.short_avg) if sim.book.short_qty > 1e-12 else 0.0

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


def print_debug_report(
    result: BacktestResult,
    *,
    print_fill_log: bool = False,
    print_order_log: bool = False,
    tail: int = 5,
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
