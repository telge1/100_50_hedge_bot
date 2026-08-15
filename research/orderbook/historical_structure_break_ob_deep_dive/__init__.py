"""Historical structure-break OB deep dive (research only).

Reuses C3.4B protected_medium + validated HistoricalBybitReplayer.
No live gates, no AUC, no trend-scanner mutations.
"""

from research.orderbook.historical_structure_break_ob_deep_dive.run import run_deep_dive

__all__ = ["run_deep_dive"]
