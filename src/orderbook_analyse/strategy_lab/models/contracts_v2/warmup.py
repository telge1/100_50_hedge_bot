"""Signal-engine and feature warmup contracts for V2."""

from __future__ import annotations

from dataclasses import dataclass

from orderbook_analyse.strategy_lab.models.contracts_v2.enums import (
    FeatureWarmupFormulaKindV2,
    WarmupTimeframeBasisV2,
)
from orderbook_analyse.strategy_lab.models.identifiers import StableIdentifier
from orderbook_analyse.strategy_lab.models.strategy import TimeframeValue


@dataclass(frozen=True, slots=True, kw_only=True)
class SignalEngineWarmupRequirementV2:
    """Plugin-declared minimum signal-engine warmup (timeframe may vary)."""

    minimum_bars: int
    timeframe_basis: WarmupTimeframeBasisV2
    fixed_timeframe: TimeframeValue | None

    def __post_init__(self) -> None:
        if type(self.minimum_bars) is not int:
            raise TypeError("minimum_bars must be exact int")
        if self.minimum_bars < 1:
            raise ValueError("minimum_bars must be >= 1")
        if self.timeframe_basis is WarmupTimeframeBasisV2.FIXED_TIMEFRAME:
            if self.fixed_timeframe is None:
                raise ValueError(
                    "fixed_timeframe is required when timeframe_basis is fixed_timeframe"
                )
        elif self.fixed_timeframe is not None:
            raise ValueError(
                "fixed_timeframe must be None unless timeframe_basis is fixed_timeframe"
            )


@dataclass(frozen=True, slots=True, kw_only=True)
class SignalEngineWarmupV2:
    """Frozen strategy signal-engine warmup with concrete bar timeframe."""

    minimum_bars: int
    bar_timeframe: TimeframeValue

    def __post_init__(self) -> None:
        if type(self.minimum_bars) is not int:
            raise TypeError("minimum_bars must be exact int")
        if self.minimum_bars < 1:
            raise ValueError("minimum_bars must be >= 1")


@dataclass(frozen=True, slots=True, kw_only=True)
class FeatureWarmupFormulaV2:
    """Closed warmup formula for a feature output."""

    formula_kind: FeatureWarmupFormulaKindV2
    parameter_name: StableIdentifier | None
    minimum_bars: int | None
    notes: str | None

    def __post_init__(self) -> None:
        if self.formula_kind is FeatureWarmupFormulaKindV2.BARS_FROM_PARAMETER:
            if self.parameter_name is None:
                raise ValueError(
                    "parameter_name is required for bars_from_parameter formula"
                )
        if self.minimum_bars is not None and type(self.minimum_bars) is not int:
            raise TypeError("minimum_bars must be exact int when provided")
