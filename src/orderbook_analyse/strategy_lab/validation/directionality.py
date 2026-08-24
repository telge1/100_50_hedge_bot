"""Rule-based directionality validation for P4B."""

from __future__ import annotations

from orderbook_analyse.strategy_lab.models.enums import Directionality, SideName
from orderbook_analyse.strategy_lab.models.signals import RuleBasedSignalSpec
from orderbook_analyse.strategy_lab.validation._issue_helpers import make_error
from orderbook_analyse.strategy_lab.validation.models import (
    ValidationIssue,
    ValidationIssueCode,
)


def validate_rule_directionality(
    signal: RuleBasedSignalSpec,
) -> tuple[ValidationIssue, ...]:
    issues: list[ValidationIssue] = []

    if signal.directionality is Directionality.LONG:
        if signal.long is None:
            issues.append(
                make_error(
                    ValidationIssueCode.RULE_LONG_BUNDLE_MISSING,
                    path="signal.long",
                    message="directionality LONG requires a long side bundle",
                    context=None,
                )
            )
        if signal.short is not None:
            issues.append(
                make_error(
                    ValidationIssueCode.RULE_SHORT_BUNDLE_UNEXPECTED,
                    path="signal.short",
                    message="directionality LONG forbids a short side bundle",
                    context=None,
                )
            )
    elif signal.directionality is Directionality.SHORT:
        if signal.short is None:
            issues.append(
                make_error(
                    ValidationIssueCode.RULE_SHORT_BUNDLE_MISSING,
                    path="signal.short",
                    message="directionality SHORT requires a short side bundle",
                    context=None,
                )
            )
        if signal.long is not None:
            issues.append(
                make_error(
                    ValidationIssueCode.RULE_LONG_BUNDLE_UNEXPECTED,
                    path="signal.long",
                    message="directionality SHORT forbids a long side bundle",
                    context=None,
                )
            )
    elif signal.directionality is Directionality.BOTH:
        if signal.long is None:
            issues.append(
                make_error(
                    ValidationIssueCode.RULE_LONG_BUNDLE_MISSING,
                    path="signal.long",
                    message="directionality BOTH requires a long side bundle",
                    context=None,
                )
            )
        if signal.short is None:
            issues.append(
                make_error(
                    ValidationIssueCode.RULE_SHORT_BUNDLE_MISSING,
                    path="signal.short",
                    message="directionality BOTH requires a short side bundle",
                    context=None,
                )
            )

    if signal.long is not None and signal.long.side is not SideName.LONG:
        issues.append(
            make_error(
                ValidationIssueCode.RULE_LONG_SIDE_MISMATCH,
                path="signal.long.side",
                message=f"long bundle side must be LONG, got {signal.long.side.value!r}",
                context=None,
            )
        )

    if signal.short is not None and signal.short.side is not SideName.SHORT:
        issues.append(
            make_error(
                ValidationIssueCode.RULE_SHORT_SIDE_MISMATCH,
                path="signal.short.side",
                message=f"short bundle side must be SHORT, got {signal.short.side.value!r}",
                context=None,
            )
        )

    return tuple(issues)
