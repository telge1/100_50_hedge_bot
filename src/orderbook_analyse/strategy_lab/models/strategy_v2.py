"""StrategySpec V2 root models (trade-backtest + candidate-discovery)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from orderbook_analyse.strategy_lab.models.contracts_v2.data_requirement import (
    DataRequirementSpecV2,
    EntrySpecV2,
)
from orderbook_analyse.strategy_lab.models.contracts_v2.enums import StrategyRunIntentV2
from orderbook_analyse.strategy_lab.models.contracts_v2.phase1_contracts import (
    CostsSpecV2,
    ExecutionAssumptionsV2,
    ExitSpecV2,
    PortfolioAssumptionsV2,
    ProvenanceSpecV2,
    ResearchParameterSpaceV2,
    VersionedUniverseRefV2,
)
from orderbook_analyse.strategy_lab.models.features import FeatureBindingSpec
from orderbook_analyse.strategy_lab.models.identifiers import StableIdentifier
from orderbook_analyse.strategy_lab.models.signals import SignalDefinition
from orderbook_analyse.strategy_lab.models.strategy import (
    AnalysisRequirements,
    IntrabarPolicy,
    Metadata,
    Timeframes,
    ValidationRequirements,
)
from orderbook_analyse.strategy_lab.models.warmup_v2 import WarmupSpecV2

STRATEGY_SPEC_V2_SCHEMA_VERSION = "strategy_spec/v2"


@dataclass(frozen=True, slots=True, kw_only=True)
class TradeBacktestStrategySpecV2:
    """Executable / trade-backtest StrategySpec V2 (entry, exit, costs required)."""

    run_intent: StrategyRunIntentV2
    metadata: Metadata
    universe: VersionedUniverseRefV2
    timeframes: Timeframes
    data_requirements: tuple[DataRequirementSpecV2, ...]
    warmup: WarmupSpecV2
    features: tuple[FeatureBindingSpec, ...]
    signal: SignalDefinition
    entry: EntrySpecV2
    exit: ExitSpecV2
    intrabar_policy: IntrabarPolicy
    execution_assumptions: ExecutionAssumptionsV2
    costs: CostsSpecV2
    portfolio_assumptions: PortfolioAssumptionsV2
    research_parameter_space: ResearchParameterSpaceV2
    analysis_requirements: AnalysisRequirements
    validation_requirements: ValidationRequirements
    provenance: ProvenanceSpecV2

    def __post_init__(self) -> None:
        if self.metadata.schema_version != STRATEGY_SPEC_V2_SCHEMA_VERSION:
            raise ValueError(
                f"metadata.schema_version must be {STRATEGY_SPEC_V2_SCHEMA_VERSION!r}, "
                f"got {self.metadata.schema_version!r}"
            )
        if self.run_intent is not StrategyRunIntentV2.TRADE_BACKTEST:
            raise ValueError(
                "TradeBacktestStrategySpecV2.run_intent must be trade_backtest, "
                f"got {self.run_intent!r}"
            )


@dataclass(frozen=True, slots=True, kw_only=True)
class CandidateDiscoveryStrategySpecV2:
    """Candidate-discovery StrategySpec V2 — research events only, no trade execution."""

    run_intent: StrategyRunIntentV2
    metadata: Metadata
    universe: VersionedUniverseRefV2
    timeframes: Timeframes
    data_requirements: tuple[DataRequirementSpecV2, ...]
    warmup: WarmupSpecV2
    features: tuple[FeatureBindingSpec, ...]
    signal: SignalDefinition
    candidate_states: tuple[StableIdentifier, ...]
    research_parameter_space: ResearchParameterSpaceV2
    analysis_requirements: AnalysisRequirements
    validation_requirements: ValidationRequirements
    provenance: ProvenanceSpecV2

    def __post_init__(self) -> None:
        if self.metadata.schema_version != STRATEGY_SPEC_V2_SCHEMA_VERSION:
            raise ValueError(
                f"metadata.schema_version must be {STRATEGY_SPEC_V2_SCHEMA_VERSION!r}, "
                f"got {self.metadata.schema_version!r}"
            )
        if self.run_intent is not StrategyRunIntentV2.CANDIDATE_DISCOVERY:
            raise ValueError(
                "CandidateDiscoveryStrategySpecV2.run_intent must be candidate_discovery, "
                f"got {self.run_intent!r}"
            )
        if type(self.candidate_states) is not tuple or len(self.candidate_states) < 1:
            raise ValueError("candidate_states must be a non-empty tuple")


# Backward-compatible name: trade-backtest root (existing code/tests).
StrategySpecV2 = TradeBacktestStrategySpecV2

AnyStrategySpecV2 = TradeBacktestStrategySpecV2 | CandidateDiscoveryStrategySpecV2

_STRATEGY_SPEC_V2_TYPES: tuple[type, ...] = (
    TradeBacktestStrategySpecV2,
    CandidateDiscoveryStrategySpecV2,
)
