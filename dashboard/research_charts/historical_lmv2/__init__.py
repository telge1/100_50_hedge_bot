"""Isolated research backtester: Floor-Guard Short Baseline × historical Live Micro LLD V2."""

from .catalog import ProductionCatalog, discover_production_catalog
from .errors import (
    FrozenInputMismatchError,
    HistoricalLmV2Error,
    ManifestGateError,
    SnapshotMissingError,
    SnapshotValidationError,
)
from .provider import HistoricalLmV2PoolStore
from .strategy_loop import run_baseline_walk
from .variants import BASELINE_VARIANTS, BaselineVariant

__all__ = [
    "BASELINE_VARIANTS",
    "BaselineVariant",
    "FrozenInputMismatchError",
    "HistoricalLmV2Error",
    "HistoricalLmV2PoolStore",
    "ManifestGateError",
    "ProductionCatalog",
    "SnapshotMissingError",
    "SnapshotValidationError",
    "discover_production_catalog",
    "run_baseline_walk",
]
