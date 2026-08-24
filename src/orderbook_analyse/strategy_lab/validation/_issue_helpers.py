"""Shared helpers for building validation issues."""

from __future__ import annotations

from orderbook_analyse.strategy_lab.validation.diagnostics import IssueContext
from orderbook_analyse.strategy_lab.validation.models import (
    ValidationIssue,
    ValidationIssueCode,
    ValidationSeverity,
)


def make_error(
    code: ValidationIssueCode,
    *,
    path: str,
    message: str,
    context: IssueContext | None,
) -> ValidationIssue:
    return ValidationIssue(
        code=code,
        severity=ValidationSeverity.ERROR,
        path=path,
        message=message,
        context=context,
    )
