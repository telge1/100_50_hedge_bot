"""Plugin parameter and binding contracts for V2."""

from __future__ import annotations

from dataclasses import dataclass

from orderbook_analyse.strategy_lab.models.contracts_v2.feature import ParameterDefinitionV2
from orderbook_analyse.strategy_lab.models.identifiers import StableIdentifier
from orderbook_analyse.strategy_lab.models.strategy import (
    BoolParam,
    DecimalParam,
    DurationParam,
    IdentifierParam,
    IntParam,
    RateParam,
    StringParam,
    TimeframeParam,
)

PluginParameterBindingValueV2 = (
    BoolParam
    | IntParam
    | DecimalParam
    | RateParam
    | DurationParam
    | TimeframeParam
    | StringParam
    | IdentifierParam
)

_PLUGIN_PARAMETER_BINDING_VALUE_TYPES: tuple[type, ...] = (
    BoolParam,
    IntParam,
    DecimalParam,
    RateParam,
    DurationParam,
    TimeframeParam,
    StringParam,
    IdentifierParam,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class PluginParameterDefinitionV2:
    """Plugin-level parameter with typed allowed identifiers."""

    definition: ParameterDefinitionV2


@dataclass(frozen=True, slots=True, kw_only=True)
class BoundParameterBindingV2:
    """One explicitly bound feature parameter value."""

    name: StableIdentifier
    value: PluginParameterBindingValueV2


@dataclass(frozen=True, slots=True, kw_only=True)
class BoundFeatureRequirementV2:
    """Plugin-bound feature usage with explicit parameters and stable alias."""

    alias: StableIdentifier
    feature_id: StableIdentifier
    bindings: tuple[BoundParameterBindingV2, ...]
