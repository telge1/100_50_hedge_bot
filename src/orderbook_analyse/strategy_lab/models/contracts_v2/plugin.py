"""Plugin parameter and binding contracts for V2."""

from __future__ import annotations

from dataclasses import dataclass

from orderbook_analyse.strategy_lab.models.contracts_v2.enums import (
    PluginModeRequirementV2,
    PluginParameterBindingTargetV2,
)
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
    """Plugin-level parameter with explicit binding target."""

    definition: ParameterDefinitionV2
    binding_target: PluginParameterBindingTargetV2

    def __post_init__(self) -> None:
        if type(self.binding_target) is not PluginParameterBindingTargetV2:
            raise TypeError(
                "PluginParameterDefinitionV2.binding_target must be "
                "PluginParameterBindingTargetV2"
            )


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
