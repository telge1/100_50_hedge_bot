from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import logging
from typing import Any

from .config import Emergency100Config
from .logging_utils import log_event
from .state import Emergency100Mode, Emergency100RuntimeState, HedgeSnapshot, MarketBias


class ActionKind(Enum):
    NOOP = "noop"
    FREEZE_TO_100_100 = "freeze_to_100_100"
    ADD_LONG = "add_long"
    ADD_SHORT = "add_short"
    REDUCE_SHORT = "reduce_short"
    HANDOFF_TO_REPAIR = "handoff_to_repair"
    HANDOFF_TO_BURN = "handoff_to_burn"


@dataclass
class StrategyAction:
    kind: ActionKind
    size_usdt: float = 0.0
    reason: str = ""
    reason_code: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class StrategyDecision:
    mode: Emergency100Mode
    reason: str
    reason_code: str
    actions: list[StrategyAction]
    metrics: dict[str, Any]
    reason_details: dict[str, Any] = field(default_factory=dict)
    decision_path: list[dict[str, Any]] = field(default_factory=list)


class Emergency100Strategy:
    def __init__(self, config: Emergency100Config) -> None:
        self.config = config
        self.logger = logging.getLogger("emergency_100.strategy")

    def _append_path(
        self,
        decision_path: list[dict[str, Any]],
        step: str,
        result: str | bool,
        **details: Any,
    ) -> None:
        entry = {"step": step, "result": result}
        if details:
            entry["details"] = details
        decision_path.append(entry)
        log_event(self.logger, "decision_path_step", step=step, result=result, details=details)

    def decide(
        self,
        snapshot: HedgeSnapshot,
        runtime: Emergency100RuntimeState,
        market_bias: MarketBias,
    ) -> StrategyDecision:
        decision_path: list[dict[str, Any]] = []
        metrics = {
            "symbol": snapshot.symbol,
            "price": snapshot.current_price,
            "spread_pct": snapshot.spread_pct,
            "short_ratio": snapshot.short_ratio,
            "atr_pct": snapshot.atr_pct,
            "price_speed_pct": snapshot.price_speed_pct,
            "mode": runtime.mode.value,
            "market_bias": market_bias.value,
        }
        self._append_path(
            decision_path,
            "enter_decide",
            runtime.mode.value,
            market_bias=market_bias.value,
            cycle_id=runtime.cycle_id,
            decision_count=runtime.decision_count,
        )
        log_event(
            self.logger,
            "strategy_decide_start",
            runtime=runtime,
            snapshot=snapshot,
            metrics=metrics,
            config={
                "add_size_usdt": self.config.add_size_usdt,
                "emergency_spread_trigger_pct": self.config.emergency_spread_trigger_pct,
                "emergency_speed_trigger_pct": self.config.emergency_speed_trigger_pct,
                "atr_speed_multiple": self.config.atr_speed_multiple,
                "bridge_resume_spread_pct": self.config.bridge_resume_spread_pct,
                "ratio_tolerance": self.config.ratio_tolerance,
            },
        )

        if runtime.mode == Emergency100Mode.IDLE:
            trigger_context = self._emergency_trigger_context(snapshot)
            trigger_result = bool(trigger_context["trigger_result"])
            self._append_path(
                decision_path,
                "idle_emergency_trigger",
                trigger_result,
                **trigger_context,
            )
            log_event(
                self.logger,
                "strategy_idle_evaluation",
                cycle_id=runtime.cycle_id,
                decision_count=runtime.decision_count,
                trigger_result=trigger_result,
                spread_pct=snapshot.spread_pct,
                price_speed_pct=snapshot.price_speed_pct,
                atr_pct=snapshot.atr_pct,
            )
            if trigger_result:
                return StrategyDecision(
                    mode=Emergency100Mode.FREEZE,
                    reason="Emergency trigger satisfied; freeze hedge first.",
                    reason_code="enter_emergency",
                    actions=[
                        StrategyAction(
                            kind=ActionKind.FREEZE_TO_100_100,
                            reason="Spread/velocity threshold reached.",
                            reason_code="freeze_hedge",
                        )
                    ],
                    metrics=metrics,
                    reason_details={
                        "spread_pct": snapshot.spread_pct,
                        "spread_trigger_pct": self.config.emergency_spread_trigger_pct,
                        "price_speed_pct": snapshot.price_speed_pct,
                        "speed_trigger_pct": self.config.emergency_speed_trigger_pct,
                    },
                    decision_path=decision_path,
                )
            return self._noop(
                runtime.mode,
                "Emergency trigger not reached.",
                metrics,
                decision_path=decision_path,
                reason_code="emergency_not_triggered",
                reason_details=trigger_context,
            )

        if runtime.mode in {Emergency100Mode.FREEZE, Emergency100Mode.PING_PONG}:
            self._append_path(
                decision_path,
                "freeze_ping_pong_mode",
                runtime.mode.value,
                spread_pct=snapshot.spread_pct,
                bridge_resume_spread_pct=self.config.bridge_resume_spread_pct,
                market_bias=market_bias.value,
            )
            log_event(
                self.logger,
                "strategy_ping_pong_evaluation",
                cycle_id=runtime.cycle_id,
                current_mode=runtime.mode,
                spread_pct=snapshot.spread_pct,
                bridge_resume_spread_pct=self.config.bridge_resume_spread_pct,
                market_bias=market_bias,
            )
            if snapshot.spread_pct <= self.config.bridge_resume_spread_pct:
                return StrategyDecision(
                    mode=Emergency100Mode.BRIDGE_TO_NORMAL,
                    reason="Spread healed enough to start bridge back to normal.",
                    reason_code="start_bridge",
                    actions=[],
                    metrics=metrics,
                    reason_details={
                        "spread_pct": snapshot.spread_pct,
                        "bridge_resume_spread_pct": self.config.bridge_resume_spread_pct,
                    },
                    decision_path=decision_path,
                )
            if market_bias == MarketBias.FALLING:
                self._append_path(
                    decision_path,
                    "ping_pong_bias_check",
                    "falling",
                    action="add_short",
                    add_size_usdt=self.config.add_size_usdt,
                )
                return StrategyDecision(
                    mode=Emergency100Mode.PING_PONG,
                    reason="Market still falling; add fixed short protection.",
                    reason_code="ping_pong_add_short",
                    actions=[
                        StrategyAction(
                            kind=ActionKind.ADD_SHORT,
                            size_usdt=self.config.add_size_usdt,
                            reason="Ping-pong protection on renewed weakness.",
                            reason_code="renewed_weakness",
                        )
                    ],
                    metrics=metrics,
                    reason_details={"market_bias": market_bias.value},
                    decision_path=decision_path,
                )
            if market_bias == MarketBias.RISING:
                self._append_path(
                    decision_path,
                    "ping_pong_bias_check",
                    "rising",
                    action="add_long",
                    add_size_usdt=self.config.add_size_usdt,
                )
                return StrategyDecision(
                    mode=Emergency100Mode.PING_PONG,
                    reason="Market rebounding; add fixed long to match last short step.",
                    reason_code="ping_pong_add_long",
                    actions=[
                        StrategyAction(
                            kind=ActionKind.ADD_LONG,
                            size_usdt=self.config.add_size_usdt,
                            reason="Ping-pong rebound response.",
                            reason_code="rebound_response",
                        )
                    ],
                    metrics=metrics,
                    reason_details={"market_bias": market_bias.value},
                    decision_path=decision_path,
                )
            return self._noop(
                Emergency100Mode.PING_PONG,
                "Market unclear; hold frozen hedge.",
                metrics,
                decision_path=decision_path,
                reason_code="ping_pong_hold",
                reason_details={"market_bias": market_bias.value},
            )

        if runtime.mode == Emergency100Mode.BRIDGE_TO_NORMAL:
            target = self._current_bridge_target(runtime)
            self._append_path(
                decision_path,
                "bridge_target_lookup",
                "target_found" if target is not None else "target_missing",
                bridge_step_index=runtime.bridge_step_index,
                target_name=target.name if target is not None else None,
            )
            log_event(
                self.logger,
                "strategy_bridge_evaluation",
                cycle_id=runtime.cycle_id,
                bridge_step_index=runtime.bridge_step_index,
                target=target,
                short_ratio=snapshot.short_ratio,
                long_size_usdt=snapshot.long_size_usdt,
                short_size_usdt=snapshot.short_size_usdt,
            )
            if target is None:
                return StrategyDecision(
                    mode=Emergency100Mode.READY_FOR_HANDOFF,
                    reason="Bridge complete; control can return to normal strategy.",
                    reason_code="bridge_complete",
                    actions=[
                        StrategyAction(
                            kind=ActionKind.HANDOFF_TO_REPAIR,
                            reason="Reached normal target ratio after bridge.",
                            reason_code="handoff_repair",
                        )
                    ],
                    metrics=metrics,
                    reason_details={"bridge_step_index": runtime.bridge_step_index},
                    decision_path=decision_path,
                )

            if snapshot.short_ratio <= target.target_short_ratio + self.config.ratio_tolerance:
                self._append_path(
                    decision_path,
                    "bridge_target_satisfied_check",
                    True,
                    current_ratio=snapshot.short_ratio,
                    target_ratio=target.target_short_ratio,
                    ratio_tolerance=self.config.ratio_tolerance,
                )
                return self._noop(
                    Emergency100Mode.BRIDGE_TO_NORMAL,
                    f"Bridge target {target.name} already satisfied.",
                    metrics,
                    decision_path=decision_path,
                    reason_code="bridge_target_satisfied",
                    reason_details={
                        "bridge_target": target.name,
                        "target_ratio": target.target_short_ratio,
                        "current_ratio": snapshot.short_ratio,
                        "ratio_tolerance": self.config.ratio_tolerance,
                    },
                )

            reduce_amount = min(
                self.config.add_size_usdt,
                max(snapshot.short_size_usdt - snapshot.long_size_usdt * target.target_short_ratio, 0.0),
            )
            self._append_path(
                decision_path,
                "bridge_reduce_amount",
                reduce_amount > 0,
                reduce_amount=reduce_amount,
                target_ratio=target.target_short_ratio,
                current_ratio=snapshot.short_ratio,
            )
            log_event(
                self.logger,
                "strategy_bridge_reduce_calc",
                cycle_id=runtime.cycle_id,
                target_name=target.name,
                target_ratio=target.target_short_ratio,
                current_ratio=snapshot.short_ratio,
                max_step_usdt=self.config.add_size_usdt,
                computed_reduce_amount=reduce_amount,
            )
            if reduce_amount <= 0:
                return self._noop(
                    Emergency100Mode.BRIDGE_TO_NORMAL,
                    f"No short reduction needed for bridge target {target.name}.",
                    metrics,
                    decision_path=decision_path,
                    reason_code="bridge_no_reduction",
                    reason_details={
                        "bridge_target": target.name,
                        "target_ratio": target.target_short_ratio,
                        "current_ratio": snapshot.short_ratio,
                    },
                )
            return StrategyDecision(
                mode=Emergency100Mode.BRIDGE_TO_NORMAL,
                reason=f"Reduce short toward bridge target {target.name}.",
                reason_code="bridge_reduce_short",
                actions=[
                    StrategyAction(
                        kind=ActionKind.REDUCE_SHORT,
                        size_usdt=reduce_amount,
                        reason=f"Bridge to {target.name}.",
                        reason_code="bridge_step",
                        metadata={"target_ratio": target.target_short_ratio},
                    )
                ],
                metrics=metrics,
                reason_details={
                    "bridge_target": target.name,
                    "target_ratio": target.target_short_ratio,
                    "current_ratio": snapshot.short_ratio,
                },
                decision_path=decision_path,
            )

        if runtime.mode == Emergency100Mode.READY_FOR_HANDOFF:
            self._append_path(
                decision_path,
                "ready_for_handoff",
                True,
                bridge_step_index=runtime.bridge_step_index,
            )
            log_event(
                self.logger,
                "strategy_ready_for_handoff",
                cycle_id=runtime.cycle_id,
                runtime=runtime,
            )
            return StrategyDecision(
                mode=Emergency100Mode.READY_FOR_HANDOFF,
                reason="Emergency module is done; waiting for orchestrator handoff.",
                reason_code="awaiting_handoff",
                actions=[
                    StrategyAction(
                        kind=ActionKind.HANDOFF_TO_REPAIR,
                        reason="Default handoff target after bridge.",
                        reason_code="handoff_repair",
                    )
                ],
                metrics=metrics,
                reason_details={},
                decision_path=decision_path,
            )

        return self._noop(
            runtime.mode,
            "Unhandled mode; no action taken.",
            metrics,
            decision_path=decision_path,
            reason_code="unhandled_mode",
            reason_details={"mode": runtime.mode.value},
        )

    def _emergency_trigger_context(self, snapshot: HedgeSnapshot) -> dict[str, Any]:
        spread_trigger_hit = snapshot.spread_pct >= self.config.emergency_spread_trigger_pct
        speed_threshold = None
        speed_trigger_hit = False
        missing_speed_inputs = False
        if snapshot.price_speed_pct is None or snapshot.atr_pct is None:
            missing_speed_inputs = True
        else:
            speed_threshold = max(
                self.config.emergency_speed_trigger_pct,
                snapshot.atr_pct * self.config.atr_speed_multiple,
            )
            speed_trigger_hit = snapshot.price_speed_pct >= speed_threshold
        return {
            "spread_pct": snapshot.spread_pct,
            "spread_trigger_pct": self.config.emergency_spread_trigger_pct,
            "spread_trigger_hit": spread_trigger_hit,
            "price_speed_pct": snapshot.price_speed_pct,
            "atr_pct": snapshot.atr_pct,
            "speed_threshold": speed_threshold,
            "speed_trigger_hit": speed_trigger_hit,
            "missing_speed_inputs": missing_speed_inputs,
            "trigger_result": spread_trigger_hit or speed_trigger_hit,
        }

    def _should_enter_emergency(self, snapshot: HedgeSnapshot) -> bool:
        context = self._emergency_trigger_context(snapshot)
        log_event(
            self.logger,
            "strategy_emergency_trigger_check",
            **context,
        )
        return bool(context["trigger_result"])

    def _current_bridge_target(self, runtime: Emergency100RuntimeState):
        if runtime.bridge_step_index >= len(self.config.bridge_targets):
            log_event(
                self.logger,
                "strategy_bridge_target_lookup",
                cycle_id=runtime.cycle_id,
                bridge_step_index=runtime.bridge_step_index,
                target_found=False,
                total_targets=len(self.config.bridge_targets),
            )
            return None
        target = self.config.bridge_targets[runtime.bridge_step_index]
        log_event(
            self.logger,
            "strategy_bridge_target_lookup",
            cycle_id=runtime.cycle_id,
            bridge_step_index=runtime.bridge_step_index,
            target_found=True,
            target=target,
            total_targets=len(self.config.bridge_targets),
        )
        return target

    def _noop(
        self,
        mode: Emergency100Mode,
        reason: str,
        metrics: dict[str, Any],
        decision_path: list[dict[str, Any]] | None = None,
        reason_code: str = "noop",
        reason_details: dict[str, Any] | None = None,
    ) -> StrategyDecision:
        log_event(
            self.logger,
            "strategy_noop",
            mode=mode,
            reason=reason,
            reason_code=reason_code,
            metrics=metrics,
            decision_path=decision_path or [],
        )
        return StrategyDecision(
            mode=mode,
            reason=reason,
            reason_code=reason_code,
            actions=[StrategyAction(kind=ActionKind.NOOP, reason=reason, reason_code="noop")],
            metrics=metrics,
            reason_details=reason_details or {},
            decision_path=list(decision_path or []),
        )
