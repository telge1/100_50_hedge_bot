from __future__ import annotations

from enum import Enum
from typing import Iterable


class StrategyState(Enum):
    WAIT_FOR_HEDGE = "wait_for_hedge"
    NORMAL = "normal"
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
        return self.state in {StrategyState.NORMAL, StrategyState.RECOVERY}

    def is_recovery(self) -> bool:
        return self.state == StrategyState.RECOVERY

    def is_pool_or_fail(self) -> bool:
        return self.state in {StrategyState.POOL_EDGE, StrategyState.FAIL}

    def is_extended(self) -> bool:
        return self.state == StrategyState.EXTEND
