"""Separate padding contracts for source loading and outcome evaluation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from orderbook_analyse.strategy_lab.models.strategy import DurationValue


@dataclass(frozen=True, slots=True, kw_only=True)
class PaddingNotApplicable:
    """Explicit marker that a padding dimension does not apply."""

    _schema_kind: ClassVar[str] = "not_applicable"
    not_applicable: bool

    def __post_init__(self) -> None:
        if self.not_applicable is not True:
            raise ValueError("PaddingNotApplicable.not_applicable must be True")


PaddingDurationV2 = DurationValue | PaddingNotApplicable

_PADDING_DURATION_V2_TYPES: tuple[type, ...] = (
    DurationValue,
    PaddingNotApplicable,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceLoadingPaddingV2:
    """Calendar padding applied when loading market data around a window."""

    candle_history: PaddingDurationV2
    auxiliary_source_history: PaddingDurationV2


@dataclass(frozen=True, slots=True, kw_only=True)
class OutcomeEvaluationPaddingV2:
    """Calendar padding after the evaluation window for outcome simulation."""

    post_window_duration: PaddingDurationV2
