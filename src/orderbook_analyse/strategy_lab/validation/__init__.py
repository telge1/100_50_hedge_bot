"""Strategy Lab validation package (P4A + P4B scope)."""

from orderbook_analyse.strategy_lab.validation.catalogs import (
    CatalogBundleV2,
    production_catalog_bundle_v2,
)
from orderbook_analyse.strategy_lab.validation.models import (
    ValidationFailedError,
    ValidationIssue,
    ValidationIssueCode,
    ValidationReport,
    ValidationSeverity,
)
from orderbook_analyse.strategy_lab.validation.p4a import (
    require_valid_strategy_v2_p4a,
    validate_strategy_v2_p4a,
)
from orderbook_analyse.strategy_lab.validation.p4b import (
    require_valid_strategy_v2_p4b,
    validate_strategy_v2_p4b,
)

__all__ = [
    "CatalogBundleV2",
    "ValidationFailedError",
    "ValidationIssue",
    "ValidationIssueCode",
    "ValidationReport",
    "ValidationSeverity",
    "production_catalog_bundle_v2",
    "require_valid_strategy_v2_p4a",
    "require_valid_strategy_v2_p4b",
    "validate_strategy_v2_p4a",
    "validate_strategy_v2_p4b",
]
