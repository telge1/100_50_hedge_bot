"""Protected-level approach pull vs break-risk audit constants.

Outcome and approach rules are fixed BEFORE evaluation (not tuned on labels).
"""

from __future__ import annotations

from pathlib import Path

OB_DAYS: dict[str, tuple[str, ...]] = {
    "APTUSDT": (
        "2025-12-29",
        "2025-12-30",
        "2026-01-06",
        "2026-01-18",
        "2026-05-12",
        "2026-05-23",
    ),
    "DOGEUSDT": (
        "2026-01-06",
        "2026-01-15",
        "2026-02-20",
        "2026-02-28",
    ),
}

# Approach / episode (documented a priori)
APPROACH_BPS_THRESHOLDS = (50.0, 25.0, 10.0, 5.0)
PRIMARY_ANCHOR_BPS = 10.0  # features relative to first time dist <= 10 bps
ENTRY_BPS = 50.0  # episode starts when dist first <= 50 bps
REJECT_AWAY_BPS = 80.0  # hold/reject if after near-level, dist rises above this
REJECT_HOLD_MINUTES = 30  # must stay away this long without break
OUTCOME_HORIZON_MINUTES = 120  # max wait after approach for outcome
MIN_NEAR_BPS_FOR_HOLD = 25.0  # must have reached <=25 bps to count as real approach for hold
COOLDOWN_MINUTES = 45  # after episode ends, same level+side suppressed

# Wall / trade match (reuse prior audit)
ZONE_BPS = 8.0
MATCH_TIME_MS = 750
MATCH_PRICE_BPS = 3.0

# Pull feature sampling offsets from anchor (seconds)
PULL_OFFSETS_S = (0, 5, 10, 20, 30, 60)

DEFAULT_OUT = Path(
    "/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/"
    "results/historical_protected_level_pull_break_risk_audit_20260808"
)
DEFAULT_OB_ROOT = Path(
    "/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/data/bybit_historical_orderbook"
)
DEFAULT_TRADE_ROOT = Path(
    "/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/data/bybit_historical_trades"
)
ONE_M_ROOT = Path("/home/telgenbuescher/projects/Signal_Generator_Ralf/data/bybit/futures")
