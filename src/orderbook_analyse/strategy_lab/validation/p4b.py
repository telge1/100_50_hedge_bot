"""P4B validation entry points for StrategySpecV2."""

from __future__ import annotations

from orderbook_analyse.strategy_lab.models.signals import (
    PluginSignalSpec,
    RuleBasedSignalSpec,
    StateMachineSignalSpec,
)
from orderbook_analyse.strategy_lab.models.strategy_v2 import StrategySpecV2
from orderbook_analyse.strategy_lab.validation.catalogs import CatalogBundleV2
from orderbook_analyse.strategy_lab.validation.components import (
    validate_rule_based_components,
    validate_state_machine_components,
)
from orderbook_analyse.strategy_lab.validation.directionality import (
    validate_rule_directionality,
)
from orderbook_analyse.strategy_lab.validation.models import (
    ValidationFailedError,
    ValidationIssue,
    ValidationReport,
    build_report,
)
from orderbook_analyse.strategy_lab.validation.p4a import validate_strategy_v2_p4a
from orderbook_analyse.strategy_lab.validation.state_machine import validate_state_machine


def validate_strategy_v2_p4b(
    spec: StrategySpecV2,
    catalogs: CatalogBundleV2,
) -> ValidationReport:
    """Validate P4A + P4B scope: features, rules, plugins, graph structure."""
    issues: list[ValidationIssue] = list(
        validate_strategy_v2_p4a(spec, catalogs).issues
    )

    if isinstance(spec.signal, RuleBasedSignalSpec):
        issues.extend(validate_rule_directionality(spec.signal))
        issues.extend(validate_rule_based_components(spec.signal))
    elif isinstance(spec.signal, StateMachineSignalSpec):
        issues.extend(validate_state_machine_components(spec.signal))
        issues.extend(validate_state_machine(spec.signal))

    if isinstance(spec.signal, PluginSignalSpec):
        pass

    return build_report(tuple(issues))


def require_valid_strategy_v2_p4b(
    spec: StrategySpecV2,
    catalogs: CatalogBundleV2,
) -> None:
    """Raise ValidationFailedError when P4B validation produces errors."""
    report = validate_strategy_v2_p4b(spec, catalogs)
    if not report.is_valid:
        raise ValidationFailedError(report)
