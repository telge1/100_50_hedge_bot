"""Research run identity context (scanner-agnostic, no DB coupling)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class ResearchRunContext:
    run_id: str
    run_fingerprint: str
    exchange: str
    symbol: str
    data_source: str
    start_time: datetime
    end_time: datetime
    warmup_start: datetime
    decision_time: datetime | None
    parameter_hash: str
    git_commit: str | None
    git_branch: str | None
    working_tree_dirty: bool
