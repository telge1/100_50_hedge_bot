"""P4A validation entry points for StrategySpecV2."""

from __future__ import annotations

from orderbook_analyse.strategy_lab.models.signals import (
    PluginSignalSpec,
    RuleBasedSignalSpec,
    StateMachineSignalSpec,
)
from orderbook_analyse.strategy_lab.models.strategy_v2 import StrategySpecV2
from orderbook_analyse.strategy_lab.validation.catalogs import CatalogBundleV2
from orderbook_analyse.strategy_lab.validation.features import (
    build_feature_resolution_index,
    validate_features,
)
from orderbook_analyse.strategy_lab.validation.models import (
    ValidationFailedError,
    ValidationIssue,
    ValidationReport,
    build_report,
)
from orderbook_analyse.strategy_lab.validation.plugins import validate_plugin_signal
from orderbook_analyse.strategy_lab.validation.rules import validate_rule_trees


def validate_strategy_v2_p4a(
    spec: StrategySpecV2,
    catalogs: CatalogBundleV2,
) -> ValidationReport:
    """Validate P4A scope: features, rule typing, and plugin basics."""
    issues: list[ValidationIssue] = []

    issues.extend(validate_features(spec.features, catalogs))
    index = build_feature_resolution_index(spec.features, catalogs)

    if isinstance(spec.signal, (RuleBasedSignalSpec, StateMachineSignalSpec)):
        issues.extend(validate_rule_trees(signal=spec.signal, index=index, catalogs=catalogs))

    if isinstance(spec.signal, PluginSignalSpec):
        issues.extend(validate_plugin_signal(spec.signal, spec.features, catalogs))

    return build_report(tuple(issues))


def require_valid_strategy_v2_p4a(
    spec: StrategySpecV2,
    catalogs: CatalogBundleV2,
) -> None:
    """Raise ValidationFailedError when P4A validation produces errors."""
    report = validate_strategy_v2_p4a(spec, catalogs)
    if not report.is_valid:
        raise ValidationFailedError(report)
