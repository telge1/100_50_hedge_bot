from __future__ import annotations

from dataclasses import dataclass

from .base import HedgeStrategy, StrategyContext
from .models import CalculationTrace, FillEvent, HedgeSnapshot, RuntimeState, StrategyIntent


@dataclass
class DynamicBreakevenConfig:
    long_reduce_fraction: float = 0.33
    short_reduce_fraction: float = 0.33
    long_reduce_trigger_pct: float = 0.01
    edge_buffer_pct: float = 0.0005
    min_short_price: float = 0.0001
    long_reduce_purpose: str = "DYN_LONG_REDUCE"
    short_compensate_purpose: str = "DYN_SHORT_COMPENSATE"


class DynamicBreakevenHedgeStrategy(HedgeStrategy):
    name = "dynamic_breakeven_hedge"

    def __init__(self, config: DynamicBreakevenConfig | None = None) -> None:
        self.config = config or DynamicBreakevenConfig()

    def on_start(
        self,
        snapshot: HedgeSnapshot,
        runtime_state: RuntimeState,
        context: StrategyContext,
    ) -> list[StrategyIntent]:
        context.audit.log_event(
            "strategy_start",
            strategy=self.name,
            config=self.config,
            snapshot=snapshot,
        )
        runtime_state.strategy_state.setdefault("awaiting_short_fill", False)
        runtime_state.strategy_state.setdefault("last_downside_long_sl_fill_price", 0.0)
        runtime_state.strategy_state.setdefault("last_downside_long_sl_fill_ts", "")
        runtime_state.strategy_state.setdefault("last_downside_long_sl_order_id", "")
        return []

    def on_tick(
        self,
        snapshot: HedgeSnapshot,
        runtime_state: RuntimeState,
        context: StrategyContext,
    ) -> list[StrategyIntent]:
        state = runtime_state.strategy_state
        if snapshot.long_qty <= 0 or snapshot.short_qty <= 0:
            return []
        if state.get("awaiting_short_fill"):
            return []
        if snapshot.has_open_purpose(self.config.long_reduce_purpose) or snapshot.has_open_purpose(
            self.config.short_compensate_purpose
        ):
            return []
        trigger_price = snapshot.long_avg * (1 - self.config.long_reduce_trigger_pct)
        trigger_trace = CalculationTrace(
            name="long_reduce_trigger",
            formula="trigger_price = long_avg * (1 - trigger_pct)",
            inputs={
                "long_avg": snapshot.long_avg,
                "trigger_pct": self.config.long_reduce_trigger_pct,
                "current_price": snapshot.current_price,
            },
            result={"trigger_price": trigger_price},
        )
        context.audit.log_event(
            "strategy_tick_evaluated",
            strategy=self.name,
            current_price=snapshot.current_price,
            trigger_price=trigger_price,
            snapshot=snapshot,
            traces=[trigger_trace.to_dict()],
        )
        if snapshot.current_price > trigger_price:
            return []
        long_reduce_qty = snapshot.long_qty * self.config.long_reduce_fraction
        paired_short_qty = min(snapshot.short_qty, snapshot.short_qty * self.config.short_reduce_fraction)
        return [
            StrategyIntent(
                side="long",
                qty=long_reduce_qty,
                price=snapshot.current_price,
                purpose=self.config.long_reduce_purpose,
                reduce_only=True,
                order_type="Market",
                metadata={
                    "paired_short_qty": paired_short_qty,
                    "paired_short_entry": snapshot.short_avg,
                    "long_entry": snapshot.long_avg,
                    "trigger_price": trigger_price,
                },
                trace=[
                    trigger_trace,
                    CalculationTrace(
                        name="long_reduce_qty",
                        formula="long_reduce_qty = long_qty * long_reduce_fraction",
                        inputs={
                            "long_qty": snapshot.long_qty,
                            "long_reduce_fraction": self.config.long_reduce_fraction,
                        },
                        result={"long_reduce_qty": long_reduce_qty},
                        details={"paired_short_qty": paired_short_qty},
                    ),
                ],
            )
        ]

    def on_fill(
        self,
        fill_event: FillEvent,
        snapshot: HedgeSnapshot,
        runtime_state: RuntimeState,
        context: StrategyContext,
    ) -> list[StrategyIntent]:
        state = runtime_state.strategy_state
        if fill_event.purpose == self.config.long_reduce_purpose:
            long_entry = float(fill_event.metadata.get("long_entry") or fill_event.metadata.get("entry_price") or 0.0)
            short_entry = float(fill_event.metadata.get("paired_short_entry") or snapshot.short_avg or 0.0)
            short_close_qty = float(fill_event.metadata.get("paired_short_qty") or 0.0)
            if long_entry <= 0 or short_entry <= 0 or short_close_qty <= 0:
                context.audit.log_event(
                    "strategy_fill_skipped",
                    strategy=self.name,
                    reason="missing_compensation_inputs",
                    fill=fill_event.to_dict(),
                    snapshot=snapshot,
                )
                return []
            incremental_qty = float(fill_event.incremental_qty or fill_event.exec_qty or 0.0)
            long_loss = incremental_qty * (long_entry - fill_event.exec_price)
            saved_fill_price = float(fill_event.exec_price)
            state["last_downside_long_sl_fill_price"] = saved_fill_price
            state["last_downside_long_sl_fill_ts"] = (
                fill_event.occurred_at.isoformat() if fill_event.occurred_at else ""
            )
            state["last_downside_long_sl_order_id"] = (
                fill_event.client_order_id or fill_event.exchange_order_id or ""
            )
            short_tp_price = saved_fill_price * (1.0 - self.config.edge_buffer_pct)
            state["awaiting_short_fill"] = True
            state["last_short_exit_price"] = short_tp_price
            state["last_long_loss"] = long_loss
            traces = [
                CalculationTrace(
                    name="long_loss",
                    formula="long_loss = long_fill_qty * (long_entry - long_fill_price)",
                    inputs={
                        "long_fill_qty": incremental_qty,
                        "long_entry": long_entry,
                        "long_fill_price": fill_event.exec_price,
                    },
                    result={"long_loss": long_loss},
                ),
                CalculationTrace(
                    name="needed_short_move",
                    formula="needed_short_move = long_loss / short_close_qty",
                    inputs={
                        "long_loss": long_loss,
                        "short_close_qty": short_close_qty,
                    },
                    result={"needed_short_move": long_loss / short_close_qty if short_close_qty else 0.0},
                ),
                CalculationTrace(
                    name="short_exit_price",
                    formula="short_exit_price = saved_long_fill_price * (1 - buffer_pct)",
                    inputs={
                        "saved_long_fill_price": saved_fill_price,
                        "buffer_pct": self.config.edge_buffer_pct,
                    },
                    result={"short_exit_price": short_tp_price},
                ),
            ]
            context.audit.log_event(
                "strategy_short_compensation_calculated",
                strategy=self.name,
                fill=fill_event.to_dict(),
                snapshot=snapshot,
                traces=[trace.to_dict() for trace in traces],
            )
            return [
                StrategyIntent(
                    side="short",
                    qty=short_close_qty,
                    price=short_tp_price,
                    purpose=self.config.short_compensate_purpose,
                    reduce_only=True,
                    order_type="Limit",
                    trigger_price=short_tp_price,
                    trigger_direction=2,
                    order_filter="StopOrder",
                    metadata={
                        "replace_open_purpose": self.config.short_compensate_purpose,
                        "target_long_loss": long_loss,
                        "source_long_fill_price": fill_event.exec_price,
                        "source_long_fill_qty": incremental_qty,
                        "short_entry": short_entry,
                        "saved_long_fill_price": saved_fill_price,
                    },
                    trace=traces,
                )
            ]
        if fill_event.purpose == self.config.short_compensate_purpose:
            state["awaiting_short_fill"] = False
            state["last_completed_short_fill_price"] = fill_event.exec_price
            state["last_downside_long_sl_fill_price"] = 0.0
            state["last_downside_long_sl_fill_ts"] = ""
            state["last_downside_long_sl_order_id"] = ""
            context.audit.log_event(
                "strategy_cycle_completed",
                strategy=self.name,
                fill=fill_event.to_dict(),
                snapshot=snapshot,
            )
        return []
