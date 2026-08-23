"""Warmup section for StrategySpec V2."""

from __future__ import annotations

from dataclasses import dataclass

from orderbook_analyse.strategy_lab.models.contracts_v2.padding import (
    OutcomeEvaluationPaddingV2,
    SourceLoadingPaddingV2,
)
from orderbook_analyse.strategy_lab.models.contracts_v2.warmup import SignalEngineWarmupV2


@dataclass(frozen=True, slots=True, kw_only=True)
class WarmupSpecV2:
    """Separated signal-engine, source-loading, and outcome-evaluation warmup."""

    signal_engine: SignalEngineWarmupV2
    source_loading: SourceLoadingPaddingV2
    outcome_evaluation: OutcomeEvaluationPaddingV2
