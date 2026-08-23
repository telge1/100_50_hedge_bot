"""State machine signal models for StrategySpec V2."""

from __future__ import annotations

from dataclasses import dataclass

from orderbook_analyse.strategy_lab.models.enums import (
    ResetEvent,
    SideName,
    TransitionPurpose,
)
from orderbook_analyse.strategy_lab.models.identifiers import StableIdentifier
from orderbook_analyse.strategy_lab.models.rules import BooleanExpression


def _positive_int(name: str, value: int) -> None:
    if type(value) is not int:
        raise TypeError(f"{name} must be exact int (bool not accepted)")
    if value <= 0:
        raise ValueError(f"{name} must be > 0")


@dataclass(frozen=True, slots=True, kw_only=True)
class StateSpec:
    state_id: StableIdentifier
    description: str


@dataclass(frozen=True, slots=True, kw_only=True)
class SignalEmissionSpec:
    side: SideName
    emission_id: StableIdentifier


@dataclass(frozen=True, slots=True, kw_only=True)
class TransitionSpec:
    transition_id: StableIdentifier
    from_state: StableIdentifier
    to_state: StableIdentifier
    condition: BooleanExpression
    priority: int
    purpose: TransitionPurpose
    emission: SignalEmissionSpec | None

    def __post_init__(self) -> None:
        _positive_int("TransitionSpec.priority", self.priority)


@dataclass(frozen=True, slots=True, kw_only=True)
class TimeoutTransitionSpec:
    timeout_id: StableIdentifier
    in_state: StableIdentifier
    after_bars: int
    to_state: StableIdentifier
    priority: int

    def __post_init__(self) -> None:
        _positive_int("TimeoutTransitionSpec.after_bars", self.after_bars)
        _positive_int("TimeoutTransitionSpec.priority", self.priority)


@dataclass(frozen=True, slots=True, kw_only=True)
class ResetRule:
    event: ResetEvent
    target_state: StableIdentifier
