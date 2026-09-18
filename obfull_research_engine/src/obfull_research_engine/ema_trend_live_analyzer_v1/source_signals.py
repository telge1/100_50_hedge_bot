"""Signal source stub — SELECT-only patterns; arm forbidden in this phase."""

from __future__ import annotations

from typing import Any


def describe_source() -> dict[str, Any]:
    return {
        "mode": "disarmed",
        "note": "EMA runner arm forbidden in Phase 3-9; smoke uses synthetic EmaSignalEvent.",
        "mysql_writes": False,
    }
