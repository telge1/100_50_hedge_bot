"""Frozen six matched research cases (event_id is the join key)."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class PilotCase:
    pair_id: str
    pair_label: str
    outcome_class: str
    event_id: str
    expected_trigger_utc: str  # "YYYY-MM-DD HH:MM:SS UTC"


PILOT_CASES: tuple[PilotCase, ...] = (
    PilotCase("PAIR1", "ABSORB_LONG", "BIG_CLEAN", "mpe_e13a0ab36c241caf4c30", "2026-09-10 13:30:36 UTC"),
    PilotCase("PAIR1", "ABSORB_LONG", "WRONG_WAY", "mpe_bdb7181441404e8b9845", "2026-09-05 20:12:53 UTC"),
    PilotCase("PAIR2", "FAILED_BREAK_SHORT", "VERY_BIG_CLEAN", "mpe_bf0486e2269ec792df12", "2026-09-10 14:47:35 UTC"),
    PilotCase("PAIR2", "FAILED_BREAK_SHORT", "WRONG_WAY", "mpe_a4c9b48511daebe34ca7", "2026-09-06 01:03:52 UTC"),
    PilotCase("PAIR3", "TRUE_BREAK_SHORT", "VERY_BIG_CLEAN", "mpe_aa9edc2d4c8dd2bb21a3", "2026-09-07 07:13:24 UTC"),
    PilotCase("PAIR3", "TRUE_BREAK_SHORT", "NO_EXPANSION", "mpe_5bb88e5fc14c851889d9", "2026-09-05 19:36:12 UTC"),
)


def pilot_cases_as_dicts() -> list[dict[str, Any]]:
    return [asdict(c) for c in PILOT_CASES]
