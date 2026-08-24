"""Validation report models for Strategy Lab P4A."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from orderbook_analyse.strategy_lab.validation.diagnostics import IssueContext


class ValidationSeverity(str, Enum):
    ERROR = "error"
    WARNING = "warning"


class ValidationIssueCode(str, Enum):
    FEATURE_DUPLICATE_ALIAS = "FEATURE_DUPLICATE_ALIAS"
    FEATURE_UNKNOWN_ID = "FEATURE_UNKNOWN_ID"
    FEATURE_CONTRACT_VERSION = "FEATURE_CONTRACT_VERSION"
    FEATURE_DUPLICATE_PARAMETER = "FEATURE_DUPLICATE_PARAMETER"
    FEATURE_MISSING_PARAMETER = "FEATURE_MISSING_PARAMETER"
    FEATURE_UNKNOWN_PARAMETER = "FEATURE_UNKNOWN_PARAMETER"
    FEATURE_PARAMETER_TYPE = "FEATURE_PARAMETER_TYPE"
    FEATURE_PARAMETER_BOUNDS = "FEATURE_PARAMETER_BOUNDS"
    FEATURE_RATE_UNIT = "FEATURE_RATE_UNIT"
    FEATURE_IDENTIFIER_VALUE = "FEATURE_IDENTIFIER_VALUE"

    OPERAND_UNKNOWN_FEATURE_ALIAS = "OPERAND_UNKNOWN_FEATURE_ALIAS"
    OPERAND_UNKNOWN_FEATURE_OUTPUT = "OPERAND_UNKNOWN_FEATURE_OUTPUT"
    OPERATOR_UNKNOWN = "OPERATOR_UNKNOWN"
    OPERATOR_CONTRACT_VERSION = "OPERATOR_CONTRACT_VERSION"
    OPERATOR_SIGNATURE_MISMATCH = "OPERATOR_SIGNATURE_MISMATCH"
    OPERATOR_RESULT_NOT_BOOLEAN = "OPERATOR_RESULT_NOT_BOOLEAN"

    PLUGIN_UNKNOWN = "PLUGIN_UNKNOWN"
    PLUGIN_CONTRACT_VERSION = "PLUGIN_CONTRACT_VERSION"
    PLUGIN_KIND = "PLUGIN_KIND"
    PLUGIN_RESERVED_CONFIG_KEY = "PLUGIN_RESERVED_CONFIG_KEY"
    PLUGIN_DUPLICATE_PARAMETER = "PLUGIN_DUPLICATE_PARAMETER"
    PLUGIN_MISSING_PARAMETER = "PLUGIN_MISSING_PARAMETER"
    PLUGIN_UNKNOWN_PARAMETER = "PLUGIN_UNKNOWN_PARAMETER"
    PLUGIN_PARAMETER_TYPE = "PLUGIN_PARAMETER_TYPE"
    PLUGIN_PARAMETER_BOUNDS = "PLUGIN_PARAMETER_BOUNDS"
    PLUGIN_RATE_UNIT = "PLUGIN_RATE_UNIT"
    PLUGIN_POLICY_MISMATCH = "PLUGIN_POLICY_MISMATCH"
    PLUGIN_MODE_MISMATCH = "PLUGIN_MODE_MISMATCH"
    PLUGIN_REQUIRED_FEATURE_MISSING = "PLUGIN_REQUIRED_FEATURE_MISSING"
    PLUGIN_REQUIRED_FEATURE_MISMATCH = "PLUGIN_REQUIRED_FEATURE_MISMATCH"
    PLUGIN_DIRECTION_UNSUPPORTED = "PLUGIN_DIRECTION_UNSUPPORTED"


@dataclass(frozen=True, slots=True, kw_only=True)
class ValidationIssue:
    code: ValidationIssueCode
    severity: ValidationSeverity
    path: str
    message: str
    context: IssueContext | None


@dataclass(frozen=True, slots=True, kw_only=True)
class ValidationReport:
    issues: tuple[ValidationIssue, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "issues", _sort_issues(self.issues))

    @property
    def errors(self) -> tuple[ValidationIssue, ...]:
        return tuple(
            issue
            for issue in self.issues
            if issue.severity is ValidationSeverity.ERROR
        )

    @property
    def warnings(self) -> tuple[ValidationIssue, ...]:
        return tuple(
            issue
            for issue in self.issues
            if issue.severity is ValidationSeverity.WARNING
        )

    @property
    def is_valid(self) -> bool:
        return not self.errors


class ValidationFailedError(Exception):
    """Raised when require_valid_strategy_v2_p4a encounters validation errors."""

    def __init__(self, report: ValidationReport) -> None:
        self.report = report
        super().__init__(
            f"StrategySpecV2 P4A validation failed with {len(report.errors)} error(s)"
        )


def build_report(issues: tuple[ValidationIssue, ...]) -> ValidationReport:
    return ValidationReport(issues=_sort_issues(issues))


def _sort_issues(
    issues: tuple[ValidationIssue, ...],
) -> tuple[ValidationIssue, ...]:
    return tuple(
        sorted(
            issues,
            key=lambda issue: (
                issue.path,
                0 if issue.severity is ValidationSeverity.ERROR else 1,
                issue.code.value,
                issue.message,
            ),
        )
    )
