"""Plugin mode contract for Strategy Lab catalog/v2."""

from __future__ import annotations

from dataclasses import dataclass

from orderbook_analyse.strategy_lab.models.contracts_v2.enums import (
    PluginModeRequirementV2,
)
from orderbook_analyse.strategy_lab.models.identifiers import StableIdentifier


@dataclass(frozen=True, slots=True, kw_only=True)
class PluginModeContractV2:
    """Closed mode_id contract for signal plugins."""

    requirement: PluginModeRequirementV2
    allowed_modes: tuple[StableIdentifier, ...]

    def __post_init__(self) -> None:
        if type(self.allowed_modes) is not tuple:
            raise TypeError("PluginModeContractV2.allowed_modes must be a tuple")
        if self.requirement is PluginModeRequirementV2.NOT_APPLICABLE:
            if self.allowed_modes:
                raise ValueError(
                    "allowed_modes must be empty when requirement is not_applicable"
                )
        elif not self.allowed_modes:
            raise ValueError(
                "allowed_modes must be non-empty when requirement is required or optional"
            )
