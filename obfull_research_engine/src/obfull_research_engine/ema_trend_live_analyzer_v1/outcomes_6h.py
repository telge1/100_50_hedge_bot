"""6h outcome placeholder — separate from 300s observation; no trading."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from . import OUTCOME_HORIZON_SECONDS
from .schema import iso_z


@dataclass
class OutcomePending:
    snapshot_ready_at: datetime
    candidate_at: datetime | None
    horizon_seconds: float = float(OUTCOME_HORIZON_SECONDS)

    def due_at(self) -> datetime:
        return self.snapshot_ready_at + timedelta(seconds=self.horizon_seconds)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": "OUTCOME_PENDING",
            "snapshot_ready_at": iso_z(self.snapshot_ready_at),
            "candidate_at": iso_z(self.candidate_at),
            "due_at": iso_z(self.due_at()),
            "horizon_seconds": self.horizon_seconds,
            "note": "Outcome finalizer observes price only after Full-OB lease release; not run in Phase 3-9 smoke.",
        }
