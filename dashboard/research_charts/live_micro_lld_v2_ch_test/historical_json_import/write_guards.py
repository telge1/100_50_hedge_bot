"""Pre-write guards for historical import (fail before any ClickHouse I/O)."""

from __future__ import annotations

import uuid


class InvalidTestRunIdError(ValueError):
    """``test_run_id`` must be a canonical UUID string before meta/member INSERT."""


def is_valid_test_run_uuid(value: str) -> bool:
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def require_valid_test_run_uuid(test_run_id: str, *, context: str = "") -> str:
    """
    Enforce UUID for ``test_run_id`` (ClickHouse ``meta.test_run_id UUID``).

    ``import_batch_id`` may remain a human-readable string on the sidecar only.
    """
    if not is_valid_test_run_uuid(test_run_id):
        prefix = f"{context}: " if context else ""
        raise InvalidTestRunIdError(
            f"{prefix}test_run_id must be a valid UUID, got {test_run_id!r}"
        )
    return str(uuid.UUID(str(test_run_id)))
