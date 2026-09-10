"""EPISODE1 wall-flow / QDH_base research (outcome-blind, no signal rules).

Semantics preserved separately:
  EPISODE_DETECTION | WALL_STATE_DETECTION | SIGNAL_DETECTION
This package reads EPISODE_DETECTION timing only; does not classify WALL_STATE
and does not emit SIGNAL_DETECTION.
"""

from __future__ import annotations

from datetime import datetime, timezone

AUDIT_ID = "LEVEL_FIRST_EPISODE1_WALL_FLOW_QDH_BASE_V1"
CONTRACT_VERSION = "1.0.0"
SCHEMA_VERSION = "level_first_episode1_wall_flow_qdh_base_v1"
RUN_PREFIX = "wfq1_"
SYMBOL = "BTCUSDT"
EPISODE_ID = "ep:pc_7775a856ab22f006:1788725942"
ZONE_ID = "pc_7775a856ab22f006"
LEVEL_ID = "lvl:CLOSED:30m:TPO_VAL:1788723000"
WALL_ID = "w_781b6ed696e777e1"
WALL_SIDE = "ask"
WALL_PRICE = 79780.0
TICK_SIZE = 0.1
# Research default only — not optimized on Episode-1 outcome.
BAND_TICKS_DEFAULT = 5

BUCKET_MS = 100
MASS_BALANCE_ABS_TOL = 1e-9
FLOAT_CMP_ABS_TOL = 1e-9
FLOAT_CMP_REL_TOL = 1e-9

# Research defaults for aggressor EWMA / persistence clip (not optimized).
FAST_HALF_LIFE_MS = 250
SLOW_HALF_LIFE_MS = 2000
PERSISTENCE_MIN = 0.25
PERSISTENCE_MAX = 4.0
QDH_HALF_LIFE_MS = 500
EPSILON = 1e-12
PROGRESS_FLOOR_TICKS = 0.1

M_OI = 1.0
M_LIQ = 1.0

ATTRIBUTION_RULE_VERSION = "ask_wall_buy_taker_interval_open_book_to_next_book_v1"

ALLOW_CLICKHOUSE_WRITES = False
FORBIDDEN_OUTCOME_SUBSTRINGS = ("mfe", "mae", "outcome", "pnl", "entry_price", "exit_price", "trade_result")

VERDICT_EVENT_TIME = "EPISODE1_WALL_FLOW_QDH_BASE_EVENT_TIME_PROVEN"
VERDICT_LIVE_CAUSAL = "EPISODE1_WALL_FLOW_QDH_BASE_LIVE_CAUSAL_PROVEN"
WALL_STATE_NOT_CLASSIFIED = "NOT_CLASSIFIED"

PROTECTED_PRIOR_RUNS = {
    "e2e1_29492befed0c9efca",
    "e2e1_29492befed0c9efcb",
    "ide1_7d99b0cf37701d78a",
    "ide1_7d99b0cf37701d78b",
    "ide1_7d99b0cf37701d78_prove",
}


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)


__all__ = [
    "AUDIT_ID",
    "VERDICT_EVENT_TIME",
    "VERDICT_LIVE_CAUSAL",
    "ALLOW_CLICKHOUSE_WRITES",
]
