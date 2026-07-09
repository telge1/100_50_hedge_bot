from __future__ import annotations

"""
Offline long-only gap reduction model for trade 0012.

This module is purely backtest/analysis code. It does NOT modify any live
strategy, simulator logic, or fill behaviour. It uses the confirmed baseline
backtest results and candle series to simulate a hypothetical sequence of
long-only reductions after Cycle 3.
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

from .simulated_pnl import closed_pnl_for_virtual_order_fill


@dataclass
class LongGapReductionConfig:
    """Configuration for the long-only gap reduction scenario."""

    # Percentage distance between triggers in percent (1.0 => 1%).
    step_trigger_pct: float = 1.0
    # Total number of planned reduction steps for the initial long-short gap.
    num_steps: int = 4
    # Fee rate used for synthetic reduces (decimal, e.g. 0.00055 for 0.055%).
    # When None, no entry/exit fees are subtracted.
    fee_rate: float | None = None


@dataclass
class LongGapReductionState:
    """Runtime state for the long-only reduction scenario."""

    long_qty: float
    short_qty: float
    long_avg: float
    short_avg: float
    base_main_realized_pnl: float
    # Cumulative net realized PnL from synthetic gap-reduction fills (after fees).
    cumulative_gap_reduction_net_pnl: float = 0.0
    # Cumulative gross realized PnL from synthetic gap-reduction fills (before fees).
    cumulative_gap_reduction_gross_pnl: float = 0.0
    # Cumulative entry+exit fees charged for synthetic gap-reduction fills.
    cumulative_gap_reduction_fees: float = 0.0
    last_trigger_price: float | None = None


def compute_trigger_price(
    *,
    reference_price: float,
    step_index: int,
    step_trigger_pct: float,
) -> float:
    """
    Compute the trigger price for a given step.

    step_index is 1-based: 1, 2, 3, ...

    trigger_price(step) = reference_price * (factor ** step)
    with factor = 1 - step_trigger_pct / 100.0
    """
    if step_index <= 0:
        raise ValueError(f"step_index must be >=1, got {step_index}")
    factor = 1.0 - (step_trigger_pct / 100.0)
    return float(reference_price) * (factor**step_index)


def _compute_unrealized(
    *,
    mark_price: float,
    long_qty: float,
    long_avg: float,
    short_qty: float,
    short_avg: float,
) -> Tuple[float, float, float]:
    unrealized_long = (mark_price - long_avg) * long_qty
    unrealized_short = (short_avg - mark_price) * short_qty
    combined = unrealized_long + unrealized_short
    return unrealized_long, unrealized_short, combined


def simulate_long_gap_reduction(
    *,
    candles: List[Any],
    start_local_candle_index: int,
    absolute_start_index: int,
    initial_long_qty: float,
    initial_short_qty: float,
    long_avg: float,
    short_avg: float,
    reference_price: float,
    base_main_realized_pnl: float,
    cfg: LongGapReductionConfig | None = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Simulate a long-only gap reduction scenario for a single trade.

    - candles: normalized candle objects (same as used by historical_backtest)
    - start_local_candle_index: index in candles from which to start scanning
    - absolute_start_index: absolute candle index in the original series
    - initial_long_qty, initial_short_qty: sizes after Cycle 3
    - long_avg, short_avg: average entry prices (constant in this scenario)
    - reference_price: price basis for the first 1%-trigger
    - base_main_realized_pnl: cumulative realized PnL up to Cycle 3
    """
    cfg = cfg or LongGapReductionConfig()

    events: List[Dict[str, Any]] = []

    state = LongGapReductionState(
        long_qty=float(initial_long_qty),
        short_qty=float(initial_short_qty),
        long_avg=float(long_avg),
        short_avg=float(short_avg),
        base_main_realized_pnl=float(base_main_realized_pnl),
    )

    # Gap definition at Cycle-3 snapshot: only the long-short gap is reduced.
    initial_gap_qty = max(state.long_qty - state.short_qty, 0.0)
    if initial_gap_qty <= 0.0 or state.long_qty <= 0.0:
        # Degenerate start state; record only a single snapshot.
        return events, {
            "events": 0,
            "initial_long_qty": state.long_qty,
            "initial_short_qty": state.short_qty,
            "initial_gap_qty": initial_gap_qty,
            "planned_gap_reduce_qty_per_step": 0.0,
            "total_reduced_qty": 0.0,
            "total_gap_reduction_gross_pnl": 0.0,
            "total_gap_reduction_fees": 0.0,
            "total_gap_reduction_net_pnl": 0.0,
            "final_long_qty": state.long_qty,
            "final_short_qty": state.short_qty,
            "remaining_gap_qty": initial_gap_qty,
            "gap_fully_closed": False,
        }

    # Planned equal reduction size per step so that num_steps fully closes the gap.
    # start_gap_qty = max(start_long_qty - start_short_qty, 0)
    # planned_reduce_qty_per_step = start_gap_qty / num_steps
    planned_gap_reduce_qty_per_step = initial_gap_qty / max(int(cfg.num_steps), 1)

    # First trigger 1% below the given reference price (by default).
    state.last_trigger_price = float(reference_price)
    current_step = 0
    if cfg.num_steps > 0:
        current_step = 1
        next_trigger_price = compute_trigger_price(
            reference_price=reference_price,
            step_index=current_step,
            step_trigger_pct=cfg.step_trigger_pct,
        )
    else:
        next_trigger_price = None

    def add_event(
        *,
        candle_index: int,
        event_type: str,
        candle: Any,
        trigger_price: float | None,
        execution_price: float | None,
        reduced_qty: float,
        realized_long_gross_pnl_event: float,
        realized_long_net_pnl_event: float,
        entry_fee: float | None,
        exit_fee: float | None,
        step_index: int | None,
    ) -> None:
        mark_price = float(candle.close)
        unreal_long, unreal_short, combined_unreal = _compute_unrealized(
            mark_price=mark_price,
            long_qty=state.long_qty,
            long_avg=state.long_avg,
            short_qty=state.short_qty,
            short_avg=state.short_avg,
        )
        total_realized_pnl_after = (
            state.base_main_realized_pnl + state.cumulative_gap_reduction_net_pnl
        )
        total_trade_pnl = total_realized_pnl_after + combined_unreal
        remaining_gap_qty = max(state.long_qty - state.short_qty, 0.0)

        if trigger_price is not None and execution_price is not None:
            slippage_to_trigger = float(execution_price) - float(trigger_price)
        else:
            slippage_to_trigger = 0.0

        events.append(
            {
                "timestamp": candle.timestamp.isoformat() if candle.timestamp else None,
                "candle_index": candle_index,
                "absolute_candle_index": absolute_start_index + candle_index,
                "candle_open": float(candle.open),
                "candle_high": float(candle.high),
                "candle_low": float(candle.low),
                "candle_close": float(candle.close),
                "event_type": event_type,
                "trigger_price": trigger_price,
                # Expected fill price under the trigger model (for now equal to trigger_price).
                "expected_fill_price": execution_price,
                "execution_price": execution_price,
                "fill_model": "conservative",
                "slippage_to_trigger": slippage_to_trigger,
                "reduced_qty": reduced_qty,
                "long_qty_before": None if event_type == "START" else None,
                "long_qty_after": state.long_qty,
                "short_qty": state.short_qty,
                "long_avg": state.long_avg,
                "short_avg": state.short_avg,
                "step_index": step_index,
                "gross_realized_pnl_event": realized_long_gross_pnl_event,
                "net_realized_pnl_event": realized_long_net_pnl_event,
                "entry_fee": entry_fee,
                "exit_fee": exit_fee,
                "fee_rate": cfg.fee_rate,
                "closing_fee": (entry_fee or 0.0) + (exit_fee or 0.0),
                # Realized PnL aggregates after this event.
                "base_main_realized_pnl": state.base_main_realized_pnl,
                "cumulative_gap_reduction_gross_pnl": state.cumulative_gap_reduction_gross_pnl,
                "cumulative_gap_reduction_fees": state.cumulative_gap_reduction_fees,
                "cumulative_gap_reduction_net_pnl": state.cumulative_gap_reduction_net_pnl,
                "total_realized_pnl_after_event": total_realized_pnl_after,
                "cumulative_main_realized_pnl": state.base_main_realized_pnl,
                "unrealized_long_pnl": unreal_long,
                "unrealized_short_pnl": unreal_short,
                "combined_unrealized_pnl": combined_unreal,
                "total_trade_pnl": total_trade_pnl,
                "remaining_long_short_gap": remaining_gap_qty,
            }
        )

    # Initial START snapshot at the candle where the scenario begins.
    start_candle = candles[start_local_candle_index]
    add_event(
        candle_index=start_local_candle_index,
        event_type="START",
        candle=start_candle,
        trigger_price=None,
        execution_price=None,
        reduced_qty=0.0,
        realized_long_gross_pnl_event=0.0,
        realized_long_net_pnl_event=0.0,
        entry_fee=None,
        exit_fee=None,
        step_index=None,
    )

    # Iterate subsequent candles and trigger at most one reduction per candle.
    for local_idx in range(start_local_candle_index + 1, len(candles)):
        candle = candles[local_idx]
        low = float(candle.low if candle.low is not None else candle.close)

        # Stop when we have no remaining gap or no steps left.
        if state.long_qty <= state.short_qty:
            break

        if next_trigger_price is None:
            break

        if low <= next_trigger_price:
            # Compute reduction size based purely on the initial long-short gap.
            max_without_overshoot = max(state.long_qty - state.short_qty, 0.0)
            # Never reduce more than the remaining gap.
            reduce_qty = min(planned_gap_reduce_qty_per_step, max_without_overshoot)
            if reduce_qty <= 0.0:
                # Nothing left to reduce without overshooting.
                break

            # Execute at trigger price under the same assumptions as the backtester:
            # the stop/limit reduce order is filled at its trigger/limit price.
            execution_price = float(next_trigger_price)

            # Closed-PnL including fees using the shared simulated_pnl helper.
            net_pnl, pnl_details = closed_pnl_for_virtual_order_fill(
                side="long",
                reduce_only=True,
                avg_entry_price=float(state.long_avg),
                fill_price=execution_price,
                qty=float(reduce_qty),
                fee_rate=cfg.fee_rate,
            )
            gross_pnl = float(
                pnl_details.get("gross_pnl") if pnl_details and pnl_details.get("gross_pnl") is not None else net_pnl
            )
            entry_fee = (
                float(pnl_details.get("entry_fee"))
                if pnl_details and pnl_details.get("entry_fee") is not None
                else None
            )
            exit_fee = (
                float(pnl_details.get("exit_fee"))
                if pnl_details and pnl_details.get("exit_fee") is not None
                else None
            )
            net_pnl = float(net_pnl)

            # Mutate long position: only the long leg is reduced.
            state.long_qty = max(0.0, state.long_qty - reduce_qty)

            state.cumulative_gap_reduction_gross_pnl += float(gross_pnl)
            state.cumulative_gap_reduction_fees += float(
                (entry_fee or 0.0) + (exit_fee or 0.0)
            )
            state.cumulative_gap_reduction_net_pnl += float(net_pnl)

            add_event(
                candle_index=local_idx,
                event_type="LONG_REDUCE",
                candle=candle,
                trigger_price=float(next_trigger_price),
                execution_price=execution_price,
                reduced_qty=reduce_qty,
                realized_long_gross_pnl_event=float(gross_pnl),
                realized_long_net_pnl_event=float(net_pnl),
                entry_fee=entry_fee,
                exit_fee=exit_fee,
                step_index=current_step,
            )

            # Prepare the next trigger if we still have steps left.
            state.last_trigger_price = next_trigger_price
            if current_step >= cfg.num_steps:
                next_trigger_price = None
            else:
                current_step += 1
                next_trigger_price = compute_trigger_price(
                    reference_price=reference_price,
                    step_index=current_step,
                    step_trigger_pct=cfg.step_trigger_pct,
                )

    # Final END snapshot at the last candle.
    end_candle = candles[-1]
    add_event(
        candle_index=len(candles) - 1,
        event_type="END",
        candle=end_candle,
        trigger_price=None,
        execution_price=None,
        reduced_qty=0.0,
        realized_long_gross_pnl_event=0.0,
        realized_long_net_pnl_event=0.0,
        entry_fee=None,
        exit_fee=None,
        step_index=None,
    )

    total_reduced_qty = float(initial_long_qty - state.long_qty)
    remaining_gap_qty = max(state.long_qty - state.short_qty, 0.0)
    gap_fully_closed = remaining_gap_qty <= 1e-9 and initial_gap_qty > 0.0
    summary = {
        "events": len(events),
        "initial_long_qty": float(initial_long_qty),
        "initial_short_qty": float(initial_short_qty),
        "initial_gap_qty": initial_gap_qty,
        "planned_gap_reduce_qty_per_step": planned_gap_reduce_qty_per_step,
        "total_reduced_qty": total_reduced_qty,
        "total_gap_reduction_gross_pnl": state.cumulative_gap_reduction_gross_pnl,
        "total_gap_reduction_fees": state.cumulative_gap_reduction_fees,
        "total_gap_reduction_net_pnl": state.cumulative_gap_reduction_net_pnl,
        "final_long_qty": state.long_qty,
        "final_short_qty": state.short_qty,
        "remaining_gap_qty": remaining_gap_qty,
        "gap_fully_closed": gap_fully_closed,
    }
    return events, summary

