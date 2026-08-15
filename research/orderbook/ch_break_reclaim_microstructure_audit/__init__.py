"""Causal CH break/reclaim microstructure audit (research only).

No live gates, no trading rules, no trend-scanner changes.
"""

from research.orderbook.ch_break_reclaim_microstructure_audit.outcomes import (
    OUTCOME_TAXONOMY,
    map_outcome_label,
)

__all__ = [
    "OUTCOME_TAXONOMY",
    "map_outcome_label",
]


def run_audit(*args, **kwargs):
    """Lazy import to keep unit tests free of heavy optional deps."""
    from research.orderbook.ch_break_reclaim_microstructure_audit.run import run_audit as _run

    return _run(*args, **kwargs)
