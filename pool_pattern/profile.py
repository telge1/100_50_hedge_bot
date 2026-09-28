"""Shared cutoffs. The pattern stays fixed. These numbers are not refit from charts."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PatternProfile:
    cluster_gap_pct: float = 3.0
    standalone_gap_pct: float = 3.0
    wide_upper_gap_pct: float = 4.5
    min_stair_steps: int = 2
    stair_separation_pct: float = 1.2
    relevant_pool_width_pct: float = 2.5
    hold_bars: int = 2
