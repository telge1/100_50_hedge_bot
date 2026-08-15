"""Signal store stub — persist only after gate pass (not used in dry-run)."""

from __future__ import annotations

from typing import Any


class LSRStore:
    """No-op dry-run store. Persist path intentionally unused until gates pass."""

    def __init__(self, *, dry_run: bool = True) -> None:
        self.dry_run = dry_run
        self.persisted = False

    def persist_signals(self, rows: list[dict[str, Any]], *, parent_label: str) -> dict[str, Any]:
        if self.dry_run:
            return {"persisted": False, "reason": "dry_run", "n": len(rows), "parent_label": parent_label}
        # Explicitly refuse auto-persist without gate orchestration.
        return {"persisted": False, "reason": "persist_requires_explicit_gate_pass", "n": len(rows)}
