"""Shared catalog value objects for Strategy Lab catalog/v2."""

from __future__ import annotations

import re
from dataclasses import dataclass

from orderbook_analyse.strategy_lab.models.contracts_v2 import (
    AdapterBindingStatusV2,
    BoundFeatureRequirementV2,
    DataRequirementSpecV2,
    FeatureOutputDescriptorV2,
    LegacyProvenanceRefV2,
    OperatorSignatureV2,
    OutcomeEvaluationPaddingV2,
    ParameterDefinitionV2,
    PluginModeContractV2,
    PluginParameterDefinitionV2,
    SignalEngineWarmupRequirementV2,
    SignalTimeframeContractV2,
    SourceLoadingPaddingV2,
)
from orderbook_analyse.strategy_lab.models.contracts_v2.enums import (
    AvailabilityTimingV2,
    EntryPriceReferenceV2,
    EntryReferenceRuleV2,
    EntryTimingAnchorV2,
    ResearchConfirmationPolicyV2,
)
from orderbook_analyse.strategy_lab.models.enums import CausalityStatus, Directionality, PluginKind
from orderbook_analyse.strategy_lab.models.identifiers import ContractVersion, StableIdentifier
from orderbook_analyse.strategy_lab.models.strategy import TimeframeValue

CATALOG_CONTRACT_VERSION = "catalog/v2"
CATALOG_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


class CatalogError(Exception):
    """Base error for catalog operations."""


class UnknownCatalogEntryError(CatalogError):
    """Raised when a catalog lookup misses a closed registry entry."""


class DuplicateCatalogEntryError(CatalogError):
    """Raised when a catalog would contain duplicate IDs."""


class InvalidCatalogDefinitionError(CatalogError):
    """Raised when a catalog definition violates structural rules."""


@dataclass(frozen=True, slots=True, kw_only=True)
class FeatureDescriptorV2:
    feature_id: StableIdentifier
    contract_version: ContractVersion
    description: str
    outputs: tuple[FeatureOutputDescriptorV2, ...]
    parameters: tuple[ParameterDefinitionV2, ...]
    data_requirements: tuple[str, ...]
    provenance: tuple[LegacyProvenanceRefV2, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class OperatorDescriptorV2:
    operator_id: StableIdentifier
    contract_version: ContractVersion
    description: str
    signatures: tuple[OperatorSignatureV2, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class PluginDescriptorV2:
    plugin_id: StableIdentifier
    contract_version: ContractVersion
    kind: PluginKind
    description: str
    parameters: tuple[PluginParameterDefinitionV2, ...]
    mode_contract: PluginModeContractV2
    required_features: tuple[BoundFeatureRequirementV2, ...]
    data_requirements: tuple[DataRequirementSpecV2, ...]
    confirmation_policy: ResearchConfirmationPolicyV2 | None
    supported_directions: Directionality
    signal_timeframe: SignalTimeframeContractV2
    execution_timeframe: TimeframeValue
    signal_decision_timing: AvailabilityTimingV2
    entry_reference_rule: EntryReferenceRuleV2
    entry_timing_anchor: EntryTimingAnchorV2
    entry_price_reference: EntryPriceReferenceV2
    signal_warmup: SignalEngineWarmupRequirementV2
    source_loading_padding: SourceLoadingPaddingV2 | None
    outcome_evaluation_padding: OutcomeEvaluationPaddingV2 | None
    causality_status: CausalityStatus
    causality_claim: str
    adapter_status: AdapterBindingStatusV2
    provenance: tuple[LegacyProvenanceRefV2, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class CatalogIntegrityIssueV2:
    code: str
    message: str
    entry_id: str | None


@dataclass(frozen=True, slots=True, kw_only=True)
class CatalogIntegrityReportV2:
    issues: tuple[CatalogIntegrityIssueV2, ...]

    @property
    def ok(self) -> bool:
        return not self.issues
