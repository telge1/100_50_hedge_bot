from __future__ import annotations

from dataclasses import dataclass

from .base import HedgeStrategy, StrategyContext
from .models import CalculationTrace, FillEvent, HedgeSnapshot, RuntimeState, StrategyIntent


@dataclass
class BasketExitConfig:
    target_basket_pnl: float = 0.0
    close_long_purpose: str = "BASKET_EXIT_LONG"
    close_short_purpose: str = "BASKET_EXIT_SHORT"


class BasketExitHedgeStrategy(HedgeStrategy):
    name = "basket_exit_hedge"

    def __init__(self, config: BasketExitConfig | None = None) -> None:
        self.config = config or BasketExitConfig()

    def on_start(
        self,
        snapshot: HedgeSnapshot,
        runtime_state: RuntimeState,
        context: StrategyContext,
    ) -> list[StrategyIntent]:
        runtime_state.strategy_state.setdefault("basket_exit_done", False)
        context.audit.log_event(
            "strategy_start",
            strategy=self.name,
            config=self.config,
            snapshot=snapshot,
        )
        return []

    def on_tick(
        self,
        snapshot: HedgeSnapshot,
        runtime_state: RuntimeState,
        context: StrategyContext,
    ) -> list[StrategyIntent]:
        if runtime_state.strategy_state.get("basket_exit_done"):
            return []
        if snapshot.long_qty <= 0 and snapshot.short_qty <= 0:
            return []
        if snapshot.has_open_purpose(self.config.close_long_purpose) or snapshot.has_open_purpose(
            self.config.close_short_purpose
        ):
            return []
        trace = CalculationTrace(
            name="basket_exit_check",
            formula="exit_when basket_pnl >= target_basket_pnl",
            inputs={
                "basket_pnl": snapshot.basket_pnl,
                "target_basket_pnl": self.config.target_basket_pnl,
            },
            result={"should_exit": 1.0 if snapshot.basket_pnl >= self.config.target_basket_pnl else 0.0},
        )
        context.audit.log_event(
            "basket_exit_evaluated",
            strategy=self.name,
            snapshot=snapshot,
            traces=[trace.to_dict()],
        )
        if snapshot.basket_pnl < self.config.target_basket_pnl:
            return []
        intents: list[StrategyIntent] = []
        if snapshot.long_qty > 0:
            intents.append(
                StrategyIntent(
                    side="long",
                    qty=snapshot.long_qty,
                    price=snapshot.current_price,
                    purpose=self.config.close_long_purpose,
                    reduce_only=True,
                    order_type="Market",
                    trace=[trace],
                )
            )
        if snapshot.short_qty > 0:
            intents.append(
                StrategyIntent(
                    side="short",
                    qty=snapshot.short_qty,
                    price=snapshot.current_price,
                    purpose=self.config.close_short_purpose,
                    reduce_only=True,
                    order_type="Market",
                    trace=[trace],
                )
            )
        return intents

    def on_fill(
        self,
        fill_event: FillEvent,
        snapshot: HedgeSnapshot,
        runtime_state: RuntimeState,
        context: StrategyContext,
    ) -> list[StrategyIntent]:
        if fill_event.purpose in {self.config.close_long_purpose, self.config.close_short_purpose}:
            if snapshot.long_qty <= 0 and snapshot.short_qty <= 0:
                runtime_state.strategy_state["basket_exit_done"] = True
                context.audit.log_event(
                    "basket_exit_completed",
                    strategy=self.name,
                    fill=fill_event.to_dict(),
                    snapshot=snapshot,
                )
        return []
