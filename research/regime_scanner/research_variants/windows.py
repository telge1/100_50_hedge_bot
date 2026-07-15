"""Research window model and deterministic hashing."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from research.regime_scanner.research_runs.hashing import json_hash
from research.regime_scanner.timeframes import ensure_utc_timestamp

CANONICAL_WARMUP_START = "2025-12-27T00:00:00Z"
MAX_ANALYSIS_END = "2026-06-27T12:45:00Z"


@dataclass(frozen=True)
class ResearchWindow:
    name: str
    description: str
    warmup_start: datetime
    start_time: datetime
    end_time: datetime
    expected_character: str
    selection_reason: str
    evidence: dict[str, Any]

    def to_canonical_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "warmup_start": iso_utc(self.warmup_start),
            "start_time": iso_utc(self.start_time),
            "end_time": iso_utc(self.end_time),
            "expected_character": self.expected_character,
            "selection_reason": self.selection_reason,
            "evidence": dict(sorted(self.evidence.items())),
        }


@dataclass(frozen=True)
class ResearchWindowSet:
    name: str
    description: str
    windows: tuple[ResearchWindow, ...]

    def to_canonical_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "windows": [w.to_canonical_dict() for w in self.windows],
        }


def iso_utc(ts: datetime | object) -> str:
    return ensure_utc_timestamp(ts).isoformat()


def window_hash(window: ResearchWindow) -> str:
    return json_hash(window.to_canonical_dict())


def window_set_hash(window_set: ResearchWindowSet) -> str:
    return json_hash(window_set.to_canonical_dict())


def window_set_json(window_set: ResearchWindowSet) -> str:
    return json.dumps(window_set.to_canonical_dict(), sort_keys=True, separators=(",", ":"))
