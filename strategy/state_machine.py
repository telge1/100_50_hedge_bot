from __future__ import annotations

from enum import Enum
from typing import Iterable


class StrategyState(Enum):
    WAIT_FOR_HEDGE = "wait_for_hedge"
    NORMAL = "normal"
    NORMAL_FLOW = "normal_flow"
    WAIT_PULLBACK = "wait_pullback"
    NO_PULLBACK_FAILOVER = "no_pullback_failover"
    SPREAD_HEALING = "spread_healing"
    WAIT_NO_ACTION = "wait_no_action"
    SIZE_RESET_ONLY = "size_reset_only"
    FULL_RESET_READY = "full_reset_ready"
    RECOVERY = "recovery"
    # POOL_EDGE is a safety/instrumentation marker when price drops to or below
    # the pool boundary; it does not spawn unique recovery orders.
    POOL_EDGE = "pool_edge"
    # EXTEND is an informational/manual extension state that may be triggered
    # by request_extend, but it does not change the short-spread-based order flow.
    EXTEND = "extend"
    FAIL = "fail"


class StateMachine:
    def __init__(self) -> None:
        self.state = StrategyState.NORMAL
        self.history: list[StrategyState] = [self.state]

    def transition(self, new_state: StrategyState) -> None:
        if new_state != self.state:
            self.state = new_state
            self.history.append(new_state)

    def allow_new_long(self) -> bool:
        return self.state in {
            StrategyState.NORMAL,
            StrategyState.NORMAL_FLOW,
            StrategyState.WAIT_PULLBACK,
            StrategyState.NO_PULLBACK_FAILOVER,
            StrategyState.SPREAD_HEALING,
            StrategyState.WAIT_NO_ACTION,
            StrategyState.SIZE_RESET_ONLY,
            StrategyState.FULL_RESET_READY,
            StrategyState.RECOVERY,
        }

    def is_recovery(self) -> bool:
        return self.state in {
            StrategyState.RECOVERY,
            StrategyState.SPREAD_HEALING,
            StrategyState.SIZE_RESET_ONLY,
        }

    def is_pool_or_fail(self) -> bool:
        return self.state in {StrategyState.POOL_EDGE, StrategyState.FAIL}

    def is_extended(self) -> bool:
        return self.state == StrategyState.EXTEND
