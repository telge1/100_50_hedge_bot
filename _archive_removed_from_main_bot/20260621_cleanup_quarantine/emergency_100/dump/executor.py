from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import logging
from typing import Any, Mapping

from emergency_100.logging_utils import log_event
from emergency_100.state import HedgeSnapshot
from emergency_100.strategy import ActionKind, StrategyAction
from strategy.config import StrategyConfig
from strategy.order_manager import BybitOrderManager


@dataclass
class ActionExecutionResult:
    cycle_id: str
    decision_id: str
    action: str
    action_reason: str
    action_reason_code: str
    status: str
    reason: str
    requested_size_usdt: float
    submitted_qty: float = 0.0
    response: Mapping[str, Any] | None = None


class Emergency100Executor:
    def __init__(
        self,
        config: StrategyConfig,
        order_manager: BybitOrderManager,
        logger: logging.Logger,
    ) -> None:
        self.config = config
        self.order_manager = order_manager
        self.logger = logger

    def execute_actions(
        self,
        *,
        snapshot: HedgeSnapshot,
        actions: list[StrategyAction],
        cycle_id: str,
        decision_id: str,
        execute_live: bool,
    ) -> list[ActionExecutionResult]:
        results: list[ActionExecutionResult] = []
        log_event(
            self.logger,
            "executor_start",
            cycle_id=cycle_id,
            decision_id=decision_id,
            execute_live=execute_live,
            action_count=len(actions),
            snapshot=snapshot,
        )
        for action in actions:
            result = self._execute_action(
                snapshot=snapshot,
                action=action,
                cycle_id=cycle_id,
                decision_id=decision_id,
                execute_live=execute_live,
            )
            results.append(result)
        return results

    def _execute_action(
        self,
        *,
        snapshot: HedgeSnapshot,
        action: StrategyAction,
        cycle_id: str,
        decision_id: str,
        execute_live: bool,
    ) -> ActionExecutionResult:
        if action.kind in {
            ActionKind.NOOP,
            ActionKind.FREEZE_TO_100_100,
            ActionKind.HANDOFF_TO_REPAIR,
            ActionKind.HANDOFF_TO_BURN,
        }:
            result = ActionExecutionResult(
                cycle_id=cycle_id,
                decision_id=decision_id,
                action=action.kind.value,
                action_reason=action.reason,
                action_reason_code=action.reason_code,
                status="skipped",
                reason="Action is orchestration-only and not sent to exchange.",
                requested_size_usdt=action.size_usdt,
            )
            self._log_action_result(result)
            return result

        if snapshot.current_price <= 0:
            result = ActionExecutionResult(
                cycle_id=cycle_id,
                decision_id=decision_id,
                action=action.kind.value,
                action_reason=action.reason,
                action_reason_code=action.reason_code,
                status="error",
                reason="Current price is invalid for live execution.",
                requested_size_usdt=action.size_usdt,
            )
            self._log_action_result(result)
            return result

        requested_qty = action.size_usdt / snapshot.current_price
        normalized_qty = self.order_manager.normalize_qty(
            snapshot.symbol,
            requested_qty,
            self.config.category,
        )
        notional = normalized_qty * snapshot.current_price
        log_event(
            self.logger,
            "order_quantity_check",
            cycle_id=cycle_id,
            decision_id=decision_id,
            action=action.kind.value,
            requested_size_usdt=action.size_usdt,
            current_price=snapshot.current_price,
            requested_qty=requested_qty,
            normalized_qty=normalized_qty,
            notional=notional,
            min_order_value=self.config.min_order_value,
        )

        if normalized_qty <= 0:
            result = ActionExecutionResult(
                cycle_id=cycle_id,
                decision_id=decision_id,
                action=action.kind.value,
                action_reason=action.reason,
                action_reason_code=action.reason_code,
                status="skipped",
                reason="Normalized order quantity is zero.",
                requested_size_usdt=action.size_usdt,
                submitted_qty=normalized_qty,
            )
            self._log_action_result(result)
            return result

        if notional < self.config.min_order_value:
            result = ActionExecutionResult(
                cycle_id=cycle_id,
                decision_id=decision_id,
                action=action.kind.value,
                action_reason=action.reason,
                action_reason_code=action.reason_code,
                status="skipped",
                reason=(
                    f"Order notional {notional:.4f} is below min_order_value "
                    f"{self.config.min_order_value:.4f}."
                ),
                requested_size_usdt=action.size_usdt,
                submitted_qty=normalized_qty,
            )
            self._log_action_result(result)
            return result

        side, position_idx, reduce_only = self._map_exchange_fields(action.kind)
        order_link_id = self._build_order_link_id(cycle_id, action.kind)
        log_event(
            self.logger,
            "order_intent",
            cycle_id=cycle_id,
            decision_id=decision_id,
            symbol=snapshot.symbol,
            action=action.kind.value,
            action_reason=action.reason,
            action_reason_code=action.reason_code,
            side=side,
            position_idx=position_idx,
            reduce_only=reduce_only,
            requested_size_usdt=action.size_usdt,
            requested_qty=requested_qty,
            normalized_qty=normalized_qty,
            notional=notional,
            order_link_id=order_link_id,
            execute_live=execute_live,
        )

        if not execute_live:
            result = ActionExecutionResult(
                cycle_id=cycle_id,
                decision_id=decision_id,
                action=action.kind.value,
                action_reason=action.reason,
                action_reason_code=action.reason_code,
                status="planned",
                reason="Dry-run only; no exchange order submitted.",
                requested_size_usdt=action.size_usdt,
                submitted_qty=normalized_qty,
            )
            self._log_action_result(result)
            return result

        if reduce_only:
            log_event(
                self.logger,
                "order_submit_start",
                cycle_id=cycle_id,
                decision_id=decision_id,
                action=action.kind.value,
                submit_path="place_reduce_market_order",
                symbol=snapshot.symbol,
                side=side,
                qty=normalized_qty,
                position_idx=position_idx,
                category=self.config.category,
                order_link_id=order_link_id,
            )
            response = self.order_manager.place_reduce_market_order(
                symbol=snapshot.symbol,
                side=side,
                qty=normalized_qty,
                position_idx=position_idx,
                category=self.config.category,
                order_link_id=order_link_id,
            )
        else:
            log_event(
                self.logger,
                "order_submit_start",
                cycle_id=cycle_id,
                decision_id=decision_id,
                action=action.kind.value,
                submit_path="place_market_order",
                symbol=snapshot.symbol,
                side=side,
                qty=normalized_qty,
                price=snapshot.current_price,
                position_idx=position_idx,
                category=self.config.category,
                order_link_id=order_link_id,
            )
            response = self.order_manager.place_market_order(
                symbol=snapshot.symbol,
                side=side,
                qty=normalized_qty,
                price=snapshot.current_price,
                position_idx=position_idx,
                category=self.config.category,
                order_link_id=order_link_id,
            )
        log_event(
            self.logger,
            "order_submit_response",
            cycle_id=cycle_id,
            decision_id=decision_id,
            action=action.kind.value,
            response=response,
        )

        if not response:
            result = ActionExecutionResult(
                cycle_id=cycle_id,
                decision_id=decision_id,
                action=action.kind.value,
                action_reason=action.reason,
                action_reason_code=action.reason_code,
                status="error",
                reason="Exchange order submission returned no response.",
                requested_size_usdt=action.size_usdt,
                submitted_qty=normalized_qty,
            )
            self._log_action_result(result)
            return result

        result = ActionExecutionResult(
            cycle_id=cycle_id,
            decision_id=decision_id,
            action=action.kind.value,
            action_reason=action.reason,
            action_reason_code=action.reason_code,
            status="submitted",
            reason="Exchange order submitted successfully.",
            requested_size_usdt=action.size_usdt,
            submitted_qty=normalized_qty,
            response=response,
        )
        self._log_action_result(result)
        return result

    def _map_exchange_fields(self, kind: ActionKind) -> tuple[str, int, bool]:
        if kind == ActionKind.ADD_LONG:
            return "Buy", 1, False
        if kind == ActionKind.ADD_SHORT:
            return "Sell", 2, False
        if kind == ActionKind.REDUCE_SHORT:
            return "Buy", 2, True
        raise ValueError(f"Unsupported execution action: {kind.value}")

    def _build_order_link_id(self, cycle_id: str, kind: ActionKind) -> str:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
        compact_cycle = cycle_id.replace("-", "")[:10]
        return f"em100-{compact_cycle}-{kind.value[:8]}-{timestamp}"[:36]

    def _log_action_result(self, result: ActionExecutionResult) -> None:
        log_event(
            self.logger,
            "order_result",
            cycle_id=result.cycle_id,
            decision_id=result.decision_id,
            action=result.action,
            status=result.status,
            action_reason=result.action_reason,
            action_reason_code=result.action_reason_code,
            reason=result.reason,
            requested_size_usdt=result.requested_size_usdt,
            submitted_qty=result.submitted_qty,
            response=result.response,
        )
