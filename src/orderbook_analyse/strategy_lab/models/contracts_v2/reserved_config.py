"""Reserved plugin config keys that must not appear in plugin config."""

from __future__ import annotations

from orderbook_analyse.strategy_lab.models.identifiers import StableIdentifier

RESERVED_PLUGIN_CONFIG_KEYS: tuple[StableIdentifier, ...] = (
    StableIdentifier(value="mode_id"),
    StableIdentifier(value="confirmation_policy"),
    StableIdentifier(value="plugin_id"),
    StableIdentifier(value="contract_version"),
)
