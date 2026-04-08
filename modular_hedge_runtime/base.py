from __future__ import annotations

from abc import ABC
from dataclasses import dataclass
from typing import Any

from .audit_logger import AuditLogger
from .models import FillEvent, HedgeSnapshot, RuntimeState, StrategyIntent


@dataclass
class StrategyContext:
    audit: AuditLogger
    runtime_name: str
    symbol: str
    category: str
    min_order_value: float


class HedgeStrategy(ABC):
    name = "unnamed_strategy"

    def on_start(
        self,
        snapshot: HedgeSnapshot,
        runtime_state: RuntimeState,
        context: StrategyContext,
    ) -> list[StrategyIntent]:
        return []

    def on_tick(
        self,
        snapshot: HedgeSnapshot,
        runtime_state: RuntimeState,
        context: StrategyContext,
    ) -> list[StrategyIntent]:
        return []

    def on_fill(
        self,
        fill_event: FillEvent,
        snapshot: HedgeSnapshot,
        runtime_state: RuntimeState,
        context: StrategyContext,
    ) -> list[StrategyIntent]:
        return []

    def on_order_update(
        self,
        order_event: dict[str, Any],
        snapshot: HedgeSnapshot,
        runtime_state: RuntimeState,
        context: StrategyContext,
    ) -> list[StrategyIntent]:
        return []

    def on_reconcile(
        self,
        snapshot: HedgeSnapshot,
        runtime_state: RuntimeState,
        context: StrategyContext,
    ) -> list[StrategyIntent]:
        return []
