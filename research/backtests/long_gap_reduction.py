from __future__ import annotations

"""
Offline and runtime long-only gap reduction model.

Pure gap-reduction logic lives in ``LongGapReductionRuntime``. The batch helper
``simulate_long_gap_reduction`` and the integrated backtest shim both delegate
to that runtime so calculations stay identical.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple

from .simulated_pnl import closed_pnl_for_virtual_order_fill


@dataclass
class LongGapReductionConfig:
    """Configuration for the long-only gap reduction scenario."""

    step_trigger_pct: float = 1.0
    num_steps: int = 4
    fee_rate: float | None = None
    gap_reduce_fraction_per_step: float | None = None


@dataclass
class LongGapReductionState:
    long_qty: float
    short_qty: float
    long_avg: float
    short_avg: float
    base_main_realized_pnl: float
    cumulative_gap_reduction_net_pnl: float = 0.0
    cumulative_gap_reduction_gross_pnl: float = 0.0
    cumulative_gap_reduction_fees: float = 0.0
    last_trigger_price: float | None = None


@dataclass
class GapReductionCandleResult:
    events: list[dict[str, Any]] = field(default_factory=list)
    reduced_qty: float = 0.0
    gap_reduction_net_pnl: float = 0.0
    joint_exit_net_pnl: float = 0.0
    gap_fully_closed: bool = False
    recovery_completed: bool = False


def compute_trigger_price(
    *,
    reference_price: float,
    step_index: int,
    step_trigger_pct: float,
) -> float:
    if step_index <= 0:
        raise ValueError(f"step_index must be >=1, got {step_index}")
    factor = 1.0 - (step_trigger_pct / 100.0)
    return float(reference_price) * (factor**step_index)


def planned_reduce_qty_per_step(*, initial_gap_qty: float, cfg: LongGapReductionConfig) -> float:
    if initial_gap_qty <= 0.0:
        return 0.0
    if cfg.gap_reduce_fraction_per_step is not None:
        return float(initial_gap_qty) * float(cfg.gap_reduce_fraction_per_step)
    return float(initial_gap_qty) / max(int(cfg.num_steps), 1)


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


def compute_joint_exit_net_pnl(
    *,
    long_qty: float,
    long_avg: float,
    short_qty: float,
    short_avg: float,
    close_price: float,
    fee_rate: float | None,
) -> float:
    total = 0.0
    if long_qty > 1e-12:
        net_pnl, _ = closed_pnl_for_virtual_order_fill(
            side="long",
            reduce_only=True,
            avg_entry_price=float(long_avg),
            fill_price=float(close_price),
            qty=float(long_qty),
            fee_rate=fee_rate,
        )
        total += float(net_pnl)
    if short_qty > 1e-12:
        net_pnl, _ = closed_pnl_for_virtual_order_fill(
            side="short",
            reduce_only=True,
            avg_entry_price=float(short_avg),
            fill_price=float(close_price),
            qty=float(short_qty),
            fee_rate=fee_rate,
        )
        total += float(net_pnl)
    return float(total)


class LongGapReductionRuntime:
    """Incremental long-gap reduction over a live candle stream."""

    def __init__(
        self,
        *,
        initial_long_qty: float,
        initial_short_qty: float,
        long_avg: float,
        short_avg: float,
        reference_price: float,
        base_main_realized_pnl: float,
        cfg: LongGapReductionConfig | None = None,
        activation_absolute_candle_index: int | None = None,
    ) -> None:
        self.cfg = cfg or LongGapReductionConfig()
        self.reference_price = float(reference_price)
        self.activation_absolute_candle_index = activation_absolute_candle_index
        self.initial_long_qty = float(initial_long_qty)
        self.initial_short_qty = float(initial_short_qty)
        self.initial_gap_qty = max(self.initial_long_qty - self.initial_short_qty, 0.0)
        self.planned_gap_reduce_qty_per_step = planned_reduce_qty_per_step(
            initial_gap_qty=self.initial_gap_qty,
            cfg=self.cfg,
        )
        self.state = LongGapReductionState(
            long_qty=float(initial_long_qty),
            short_qty=float(initial_short_qty),
            long_avg=float(long_avg),
            short_avg=float(short_avg),
            base_main_realized_pnl=float(base_main_realized_pnl),
        )
        self.current_step = 0
        self.next_trigger_price: float | None = None
        self.completed = False
        self.all_events: list[dict[str, Any]] = []
        self.total_long_reduced_qty = 0.0
        if self.initial_gap_qty > 0.0 and self.cfg.num_steps > 0:
            self.current_step = 1
            self.next_trigger_price = compute_trigger_price(
                reference_price=self.reference_price,
                step_index=self.current_step,
                step_trigger_pct=self.cfg.step_trigger_pct,
            )

    def _build_event(
        self,
        *,
        event_type: str,
        candle: Any,
        local_candle_index: int,
        absolute_candle_index: int,
        trigger_price: float | None,
        execution_price: float | None,
        reduced_qty: float,
        realized_long_gross_pnl_event: float,
        realized_long_net_pnl_event: float,
        entry_fee: float | None,
        exit_fee: float | None,
        step_index: int | None,
    ) -> dict[str, Any]:
        mark_price = float(candle.close)
        unreal_long, unreal_short, combined_unreal = _compute_unrealized(
            mark_price=mark_price,
            long_qty=self.state.long_qty,
            long_avg=self.state.long_avg,
            short_qty=self.state.short_qty,
            short_avg=self.state.short_avg,
        )
        total_realized_pnl_after = (
            self.state.base_main_realized_pnl + self.state.cumulative_gap_reduction_net_pnl
        )
        if trigger_price is not None and execution_price is not None:
            slippage_to_trigger = float(execution_price) - float(trigger_price)
        else:
            slippage_to_trigger = 0.0
        return {
            "timestamp": candle.timestamp.isoformat() if candle.timestamp else None,
            "candle_index": local_candle_index,
            "absolute_candle_index": absolute_candle_index,
            "candle_open": float(candle.open),
            "candle_high": float(candle.high),
            "candle_low": float(candle.low),
            "candle_close": float(candle.close),
            "event_type": event_type,
            "trigger_price": trigger_price,
            "expected_fill_price": execution_price,
            "execution_price": execution_price,
            "fill_model": "conservative",
            "slippage_to_trigger": slippage_to_trigger,
            "reduced_qty": reduced_qty,
            "long_qty_after": self.state.long_qty,
            "short_qty": self.state.short_qty,
            "long_avg": self.state.long_avg,
            "short_avg": self.state.short_avg,
            "step_index": step_index,
            "gross_realized_pnl_event": realized_long_gross_pnl_event,
            "net_realized_pnl_event": realized_long_net_pnl_event,
            "entry_fee": entry_fee,
            "exit_fee": exit_fee,
            "fee_rate": self.cfg.fee_rate,
            "closing_fee": (entry_fee or 0.0) + (exit_fee or 0.0),
            "base_main_realized_pnl": self.state.base_main_realized_pnl,
            "cumulative_gap_reduction_gross_pnl": self.state.cumulative_gap_reduction_gross_pnl,
            "cumulative_gap_reduction_fees": self.state.cumulative_gap_reduction_fees,
            "cumulative_gap_reduction_net_pnl": self.state.cumulative_gap_reduction_net_pnl,
            "total_realized_pnl_after_event": total_realized_pnl_after,
            "unrealized_long_pnl": unreal_long,
            "unrealized_short_pnl": unreal_short,
            "combined_unrealized_pnl": combined_unreal,
            "total_trade_pnl": total_realized_pnl_after + combined_unreal,
            "remaining_long_short_gap": max(self.state.long_qty - self.state.short_qty, 0.0),
        }

    def start_event(self, candle: Any, *, local_candle_index: int, absolute_candle_index: int) -> dict[str, Any]:
        event = self._build_event(
            event_type="START",
            candle=candle,
            local_candle_index=local_candle_index,
            absolute_candle_index=absolute_candle_index,
            trigger_price=None,
            execution_price=None,
            reduced_qty=0.0,
            realized_long_gross_pnl_event=0.0,
            realized_long_net_pnl_event=0.0,
            entry_fee=None,
            exit_fee=None,
            step_index=None,
        )
        self.all_events.append(event)
        return event

    def process_candle(
        self,
        candle: Any,
        *,
        local_candle_index: int,
        absolute_candle_index: int,
    ) -> GapReductionCandleResult:
        result = GapReductionCandleResult()
        if self.completed:
            return result

        low = float(candle.low if candle.low is not None else candle.close)
        remaining_gap = max(self.state.long_qty - self.state.short_qty, 0.0)

        if (
            remaining_gap <= 1e-9
            and self.initial_gap_qty > 0.0
            and self.state.long_qty > 1e-12
            and self.state.short_qty > 1e-12
        ):
            joint_exit_net = compute_joint_exit_net_pnl(
                long_qty=self.state.long_qty,
                long_avg=self.state.long_avg,
                short_qty=self.state.short_qty,
                short_avg=self.state.short_avg,
                close_price=float(candle.close),
                fee_rate=self.cfg.fee_rate,
            )
            event = self._build_event(
                event_type="JOINT_EXIT",
                candle=candle,
                local_candle_index=local_candle_index,
                absolute_candle_index=absolute_candle_index,
                trigger_price=None,
                execution_price=float(candle.close),
                reduced_qty=0.0,
                realized_long_gross_pnl_event=0.0,
                realized_long_net_pnl_event=joint_exit_net,
                entry_fee=None,
                exit_fee=None,
                step_index=None,
            )
            self.all_events.append(event)
            result.events.append(event)
            result.joint_exit_net_pnl = joint_exit_net
            result.gap_fully_closed = True
            result.recovery_completed = True
            self.completed = True
            self.state.long_qty = 0.0
            self.state.short_qty = 0.0
            return result

        if self.next_trigger_price is None or remaining_gap <= 1e-9:
            return result

        if low > self.next_trigger_price:
            return result

        reduce_qty = min(self.planned_gap_reduce_qty_per_step, remaining_gap)
        if reduce_qty <= 1e-12:
            return result

        execution_price = float(self.next_trigger_price)
        net_pnl, pnl_details = closed_pnl_for_virtual_order_fill(
            side="long",
            reduce_only=True,
            avg_entry_price=float(self.state.long_avg),
            fill_price=execution_price,
            qty=float(reduce_qty),
            fee_rate=self.cfg.fee_rate,
        )
        gross_pnl = float(
            pnl_details.get("gross_pnl")
            if pnl_details and pnl_details.get("gross_pnl") is not None
            else net_pnl
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

        self.state.long_qty = max(0.0, self.state.long_qty - reduce_qty)
        self.total_long_reduced_qty += float(reduce_qty)
        self.state.cumulative_gap_reduction_gross_pnl += gross_pnl
        self.state.cumulative_gap_reduction_fees += float((entry_fee or 0.0) + (exit_fee or 0.0))
        self.state.cumulative_gap_reduction_net_pnl += net_pnl

        event = self._build_event(
            event_type="LONG_REDUCE",
            candle=candle,
            local_candle_index=local_candle_index,
            absolute_candle_index=absolute_candle_index,
            trigger_price=float(self.next_trigger_price),
            execution_price=execution_price,
            reduced_qty=reduce_qty,
            realized_long_gross_pnl_event=gross_pnl,
            realized_long_net_pnl_event=net_pnl,
            entry_fee=entry_fee,
            exit_fee=exit_fee,
            step_index=self.current_step,
        )
        self.all_events.append(event)
        result.events.append(event)
        result.reduced_qty = reduce_qty
        result.gap_reduction_net_pnl = net_pnl

        if self.current_step >= self.cfg.num_steps:
            self.next_trigger_price = None
        else:
            self.current_step += 1
            self.next_trigger_price = compute_trigger_price(
                reference_price=self.reference_price,
                step_index=self.current_step,
                step_trigger_pct=self.cfg.step_trigger_pct,
            )

        remaining_gap_after = max(self.state.long_qty - self.state.short_qty, 0.0)
        if remaining_gap_after <= 1e-9 and self.initial_gap_qty > 0.0:
            joint_exit_net = compute_joint_exit_net_pnl(
                long_qty=self.state.long_qty,
                long_avg=self.state.long_avg,
                short_qty=self.state.short_qty,
                short_avg=self.state.short_avg,
                close_price=float(candle.close),
                fee_rate=self.cfg.fee_rate,
            )
            joint_event = self._build_event(
                event_type="JOINT_EXIT",
                candle=candle,
                local_candle_index=local_candle_index,
                absolute_candle_index=absolute_candle_index,
                trigger_price=None,
                execution_price=float(candle.close),
                reduced_qty=0.0,
                realized_long_gross_pnl_event=0.0,
                realized_long_net_pnl_event=joint_exit_net,
                entry_fee=None,
                exit_fee=None,
                step_index=None,
            )
            self.all_events.append(joint_event)
            result.events.append(joint_event)
            result.joint_exit_net_pnl = joint_exit_net
            result.gap_fully_closed = True
            result.recovery_completed = True
            self.completed = True
            self.state.long_qty = 0.0
            self.state.short_qty = 0.0

        return result

    def summary(self) -> dict[str, Any]:
        total_reduced_qty = float(self.total_long_reduced_qty)
        remaining_gap_qty = max(self.state.long_qty - self.state.short_qty, 0.0)
        gap_fully_closed = remaining_gap_qty <= 1e-9 and self.initial_gap_qty > 0.0 and self.completed
        return {
            "events": len(self.all_events),
            "initial_long_qty": float(self.initial_long_qty),
            "initial_short_qty": float(self.initial_short_qty),
            "initial_gap_qty": self.initial_gap_qty,
            "planned_gap_reduce_qty_per_step": self.planned_gap_reduce_qty_per_step,
            "total_reduced_qty": total_reduced_qty,
            "total_gap_reduction_gross_pnl": self.state.cumulative_gap_reduction_gross_pnl,
            "total_gap_reduction_fees": self.state.cumulative_gap_reduction_fees,
            "total_gap_reduction_net_pnl": self.state.cumulative_gap_reduction_net_pnl,
            "final_long_qty": self.state.long_qty,
            "final_short_qty": self.state.short_qty,
            "remaining_gap_qty": remaining_gap_qty,
            "gap_fully_closed": gap_fully_closed,
        }


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
    cfg = cfg or LongGapReductionConfig()
    runtime = LongGapReductionRuntime(
        initial_long_qty=initial_long_qty,
        initial_short_qty=initial_short_qty,
        long_avg=long_avg,
        short_avg=short_avg,
        reference_price=reference_price,
        base_main_realized_pnl=base_main_realized_pnl,
        cfg=cfg,
        activation_absolute_candle_index=absolute_start_index,
    )

    initial_gap_qty = runtime.initial_gap_qty
    if initial_gap_qty <= 0.0 or runtime.initial_long_qty <= 0.0:
        return [], {
            "events": 0,
            "initial_long_qty": runtime.initial_long_qty,
            "initial_short_qty": runtime.initial_short_qty,
            "initial_gap_qty": initial_gap_qty,
            "planned_gap_reduce_qty_per_step": 0.0,
            "total_reduced_qty": 0.0,
            "total_gap_reduction_gross_pnl": 0.0,
            "total_gap_reduction_fees": 0.0,
            "total_gap_reduction_net_pnl": 0.0,
            "final_long_qty": runtime.initial_long_qty,
            "final_short_qty": runtime.initial_short_qty,
            "remaining_gap_qty": initial_gap_qty,
            "gap_fully_closed": False,
        }

    start_candle = candles[start_local_candle_index]
    runtime.start_event(
        start_candle,
        local_candle_index=start_local_candle_index,
        absolute_candle_index=absolute_start_index + start_local_candle_index,
    )

    for local_idx in range(start_local_candle_index, len(candles)):
        candle = candles[local_idx]
        runtime.process_candle(
            candle,
            local_candle_index=local_idx,
            absolute_candle_index=absolute_start_index + local_idx,
        )
        if runtime.completed:
            break

    if not runtime.completed:
        end_candle = candles[-1]
        runtime.all_events.append(
            runtime._build_event(
                event_type="END",
                candle=end_candle,
                local_candle_index=len(candles) - 1,
                absolute_candle_index=absolute_start_index + len(candles) - 1,
                trigger_price=None,
                execution_price=None,
                reduced_qty=0.0,
                realized_long_gross_pnl_event=0.0,
                realized_long_net_pnl_event=0.0,
                entry_fee=None,
                exit_fee=None,
                step_index=None,
            )
        )

    summary = runtime.summary()
    if not runtime.completed:
        summary["gap_fully_closed"] = False
    return runtime.all_events, summary
