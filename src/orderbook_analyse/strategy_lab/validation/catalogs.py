"""Immutable catalog bundle for P4A validation."""

from __future__ import annotations

from dataclasses import dataclass

from orderbook_analyse.strategy_lab.catalogs.v2.models import (
    FeatureDescriptorV2,
    OperatorDescriptorV2,
    PluginDescriptorV2,
)
from orderbook_analyse.strategy_lab.catalogs.v2.registry import (
    FEATURE_CATALOG_V2,
    OPERATOR_CATALOG_V2,
    PLUGIN_CATALOG_V2,
    CatalogRegistryV2,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class CatalogBundleV2:
    """Closed catalog/v2 registries required for StrategySpecV2 validation."""

    features: CatalogRegistryV2[FeatureDescriptorV2]
    operators: CatalogRegistryV2[OperatorDescriptorV2]
    plugins: CatalogRegistryV2[PluginDescriptorV2]


def production_catalog_bundle_v2() -> CatalogBundleV2:
    """Bundle the static production catalog/v2 registries."""
    return CatalogBundleV2(
        features=FEATURE_CATALOG_V2,
        operators=OPERATOR_CATALOG_V2,
        plugins=PLUGIN_CATALOG_V2,
    )
