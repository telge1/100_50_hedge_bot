"""Typed analysis failures (no secrets in messages)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_SECRETISH = re.compile(
    r"(password|passwd|secret|token|api[_-]?key|authorization|credential)",
    re.IGNORECASE,
)


def sanitize_error_message(exc: BaseException | str, *, limit: int = 400) -> str:
    text = str(exc)
    if _SECRETISH.search(text):
        return f"{type(exc).__name__ if not isinstance(exc, str) else 'Error'}:redacted"
    # strip connection strings heuristically
    text = re.sub(r"(://[^/\s]+:)[^@\s]+@", r"\1***@", text)
    return text[:limit]


@dataclass
class AnalysisFailure:
    classification: str  # EXPECTED_DATA_QUALITY_FAILURE | INTERNAL_ANALYSIS_ERROR
    failure_stage: str
    failure_reason: str
    error_type: str = ""
    sanitized_error_message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "classification": self.classification,
            "failure_stage": self.failure_stage,
            "failure_reason": self.failure_reason,
            "error_type": self.error_type,
            "sanitized_error_message": self.sanitized_error_message,
        }


def expected_failure(stage: str, reason: str) -> AnalysisFailure:
    return AnalysisFailure(
        classification="EXPECTED_DATA_QUALITY_FAILURE",
        failure_stage=stage,
        failure_reason=reason,
    )


def internal_failure(stage: str, exc: BaseException) -> AnalysisFailure:
    return AnalysisFailure(
        classification="INTERNAL_ANALYSIS_ERROR",
        failure_stage=stage,
        failure_reason="INTERNAL_ANALYSIS_ERROR",
        error_type=type(exc).__name__,
        sanitized_error_message=sanitize_error_message(exc),
    )
