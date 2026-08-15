"""Post-break acceptance vs reclaim audit (historical OB + trades)."""

from __future__ import annotations

from pathlib import Path

CUTOFFS_S: tuple[int, ...] = (1, 2, 5, 10, 20, 30, 60, 120)
PRIMARY_CUTOFFS_S: tuple[int, ...] = (5, 10, 20, 30)
CONFIRMATION_CUTOFFS_S: tuple[int, ...] = (60, 120)

ZONE_BPS = 8.0
DEPTH_BPS = (5, 10, 25)
SAMPLE_EVERY_MS = 250
MATCH_TIME_MS = 750

DEFAULT_SELECTED = Path(
    "/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/"
    "results/historical_structure_break_ob_deep_dive_20260808/selected_deep_dive_events.csv"
)
DEFAULT_CLUSTERED = Path(
    "/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/"
    "results/historical_structure_break_ob_deep_dive_20260808/structure_break_events.csv"
)
DEFAULT_OUT = Path(
    "/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/"
    "results/historical_post_break_acceptance_reclaim_audit_20260808"
)
DEFAULT_OB_ROOT = Path(
    "/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/data/bybit_historical_orderbook"
)
DEFAULT_TRADE_ROOT = Path(
    "/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/data/bybit_historical_trades"
)

OUTCOME_ACCEPTED = "BREAK_ACCEPTED"
OUTCOME_RECLAIM = "RECLAIM"
OUTCOME_AMBIGUOUS = "AMBIGUOUS"

# Existing deep-dive mechanism labels → coarse accept/reclaim (price path overrides when available)
MECH_TO_COARSE = {
    "BREAK_ACCEPTED_NO_QUICK_RECLAIM": OUTCOME_ACCEPTED,
    "WALL_CONSUMED_OR_REMOVED_BREAK": OUTCOME_ACCEPTED,
    "REFILL_THEN_RECLAIM": OUTCOME_RECLAIM,
    "WALL_HELD_OR_RECLAIM": OUTCOME_RECLAIM,
}
