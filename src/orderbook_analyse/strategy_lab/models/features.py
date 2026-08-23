"""Feature binding models for StrategySpec V2."""

from __future__ import annotations

from dataclasses import dataclass

from orderbook_analyse.strategy_lab.models.identifiers import (
    ContractVersion,
    StableIdentifier,
)
from orderbook_analyse.strategy_lab.models.strategy import (
    ParamValue,
    _PARAM_VALUE_TYPES,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class FeatureParameterBinding:
    name: StableIdentifier
    value: ParamValue

    def __post_init__(self) -> None:
        if not isinstance(self.value, _PARAM_VALUE_TYPES):
            raise TypeError(
                "FeatureParameterBinding.value must be a typed ParamValue"
            )


@dataclass(frozen=True, slots=True, kw_only=True)
class FeatureBindingSpec:
    alias: StableIdentifier
    catalog_feature_id: StableIdentifier
    catalog_contract_version: ContractVersion
    bindings: tuple[FeatureParameterBinding, ...]

    def __post_init__(self) -> None:
        if type(self.bindings) is not tuple:
            raise TypeError("FeatureBindingSpec.bindings must be a tuple")
