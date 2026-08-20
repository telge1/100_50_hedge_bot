"""Automatic market-event case-study selection (research diagnostic only)."""

from .select import CaseCandidate, apply_cooldown, select_rare_confluence, select_top_n

__all__ = [
    "CaseCandidate",
    "apply_cooldown",
    "select_rare_confluence",
    "select_top_n",
]
