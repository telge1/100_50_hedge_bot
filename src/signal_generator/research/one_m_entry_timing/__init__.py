"""Research-only 1m Stoch entry-timing (does NOT change production strategy)."""

from __future__ import annotations

from signal_generator.research.one_m_entry_timing.constants import (
    DEFAULT_RESEARCH_DISPLAY_VARIANT,
    TIMING_VARIANTS,
    TRIGGER_ENTRY_TRIGGERED,
    TRIGGER_NO_ENTRY_TIMEOUT,
    TRIGGER_WAITING_FOR_1M_EXTREME,
    TRIGGER_WAITING_FOR_1M_TURN,
)
from signal_generator.research.one_m_entry_timing.feed import build_research_timing_feed
from signal_generator.research.one_m_entry_timing.timing import evaluate_1m_entry_timing

__all__ = [
    "DEFAULT_RESEARCH_DISPLAY_VARIANT",
    "TIMING_VARIANTS",
    "TRIGGER_ENTRY_TRIGGERED",
    "TRIGGER_NO_ENTRY_TIMEOUT",
    "TRIGGER_WAITING_FOR_1M_EXTREME",
    "TRIGGER_WAITING_FOR_1M_TURN",
    "build_research_timing_feed",
    "evaluate_1m_entry_timing",
]
