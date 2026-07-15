"""Reproducible research-run storage for the regime scanner (separate from candle tables)."""

from research.regime_scanner.research_runs.context import ResearchRunContext
from research.regime_scanner.research_runs.parameters import (
    ResearchParameterSet,
    build_baseline_parameter_set,
    parameter_hash,
)
from research.regime_scanner.research_runs.repository import (
    compare_runs,
    get_run,
    list_runs,
    load_signals,
    load_structure_events,
    load_trend_states,
)

__all__ = [
    "ResearchParameterSet",
    "ResearchRunContext",
    "build_baseline_parameter_set",
    "compare_runs",
    "get_run",
    "list_runs",
    "load_signals",
    "load_structure_events",
    "load_trend_states",
    "parameter_hash",
]
