"""Plugin reference for StrategySpec V2."""

from __future__ import annotations

from dataclasses import dataclass

from orderbook_analyse.strategy_lab.models.identifiers import ContractVersion, StableIdentifier
from orderbook_analyse.strategy_lab.models.strategy import ConfigEntry


@dataclass(frozen=True, slots=True, kw_only=True)
class PluginRefV2:
    """Typed plugin reference with catalog contract version."""

    plugin_id: StableIdentifier
    contract_version: ContractVersion
    config: tuple[ConfigEntry, ...]

    def __post_init__(self) -> None:
        if type(self.config) is not tuple:
            raise TypeError("PluginRefV2.config must be a tuple")
