"""Typed errors for PD-aware CH test reads (research only)."""

from __future__ import annotations

from typing import Any


class ChTestError(Exception):
    code: str = "CH_TEST_ERROR"

    def __init__(self, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.details = details or {}


class OutOfCoverageError(ChTestError):
    code = "OUT_OF_COVERAGE"


class ConfigNotAvailableError(ChTestError):
    code = "CONFIG_NOT_AVAILABLE"


class AmbiguousRunError(ChTestError):
    code = "AMBIGUOUS_RUN"


class ConfigMismatchError(ChTestError):
    code = "CONFIG_MISMATCH"


class InputMismatchError(ChTestError):
    code = "INPUT_MISMATCH"
