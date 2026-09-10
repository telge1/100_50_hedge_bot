"""Episode-1 causal headroom to next major ask liquidity barrier.

Verdict candidates:
  EPISODE1_NEXT_MAJOR_ASK_BARRIER_HEADROOM_EVENT_TIME_PROVEN
  EPISODE1_NEXT_MAJOR_ASK_BARRIER_DEPTH_INSUFFICIENT
  EPISODE1_NO_PAST_ONLY_MAJOR_ASK_BARRIER_FOUND

No trading signal, no wall-hold probability, no outcome-optimized thresholds.
"""

from __future__ import annotations

AUDIT_ID = "LEVEL_FIRST_EPISODE1_NEXT_MAJOR_ASK_BARRIER_HEADROOM_V1"
SCHEMA_VERSION = "level_first_episode1_next_major_ask_barrier_headroom_v1"
CONTRACT_VERSION = "next_major_ask_barrier_headroom_v1"
RUN_PREFIX = "nabh1_"

SYMBOL = "BTCUSDT"
TICK_SIZE = 0.1
ORIGINAL_WALL_PRICE = 79780.0
ZONE_LOW = 79774.75
ZONE_HIGH = 79775.25
ZONE_ID = "pc_7775a856ab22f006"
EPISODE_ID = "ep:pc_7775a856ab22f006:1788725942"

EPOCH4 = 4
EPOCH5 = 5
EPOCH4_COVERAGE_START = "2026-09-06T20:19:02.300Z"
EPOCH4_COVERAGE_END = "2026-09-06T20:19:59.800Z"
ASK_WALL_BREACH = "2026-09-06T20:19:41.900Z"
EPOCH5_CHECKPOINT = "2026-09-06T20:19:59.873Z"

# Frozen fee / headroom contract (research run — not optimized on Episode-1 PnL).
ENTRY_FEE_PCT = 0.055
EXIT_FEE_PCT = 0.055
ROUNDTRIP_FEE_PCT = 0.110
REQUIRED_NET_PROFIT_PCT = 0.300
REQUIRED_GROSS_HEADROOM_PCT = 0.410  # 0.300 + 0.110

# Slippage kept SEPARATE from fees (versioned research defaults, not Ep1-tuned).
ESTIMATED_ENTRY_SLIPPAGE_PCT = 0.020
ESTIMATED_EXIT_SLIPPAGE_PCT = 0.020

# Past-only major-wall research views (all reported; none selected as best rule).
MAJOR_PERCENTILE_VIEWS = (0.90, 0.95, 0.97, 0.99)

# Largest-wall scan windows as fraction of entry (percent points).
LARGEST_WITHIN_PCT_VIEWS = (0.25, 0.41, 0.50, 1.00)

# Ask-liquidity barrier cluster merge gap — frozen BEFORE outcome inspection.
# 10 ticks = $1.0; NOT chosen from the later ~80k high.
CLUSTER_MERGE_GAP_TICKS = 10
CLUSTER_CONTRACT = {
    "merge_gap_ticks": CLUSTER_MERGE_GAP_TICKS,
    "merge_gap_price": CLUSTER_MERGE_GAP_TICKS * TICK_SIZE,
    "note": "Frozen research default; not optimized on Episode-1 outcome or ~80k high.",
}

# Min qty floor for raw book level to enter wall-candidate set (past-only size filter none).
MIN_WALL_CANDIDATE_QTY = 1.0  # descriptive floor, not Ep1-tuned major threshold
SCAN_MAX_DISTANCE_PCT = 2.0  # scan asks within +2% of entry for barrier research

TIME_BASIS = "EVENT_TIME_ONLY"
ALLOW_CLICKHOUSE_WRITES = False
EPSILON = 1e-12

FROZEN_BOOK_DIR = (
    "results/level_first_episode1_wall_flow_qdh_base_v1/BTCUSDT/wfq1_58db1918314881b6a"
)
FROZEN_DEFENSE_CHAIN_RUN = (
    "results/level_first_episode1_defense_chain_v1/BTCUSDT/dch1_9bb0b8ff5ce8a"
)
FROZEN_WALL_MIGRATION_RUN = (
    "results/level_first_episode1_wall_migration_structure_v1/BTCUSDT/wms1_fa0b8dd6daf1a"
)
FROZEN_HANDOFF_PATH = (
    "results/level_first_episode1_detection_to_wall_flow_integration_v1/"
    "BTCUSDT/d2w1_eee5d0eefaffa/episode1_handoff.json"
)
FROZEN_OUTCOME_CONTRACT_RUN = (
    "results/wall_defense_outcome_contract_v1/BTCUSDT/odc1_efd620a305e8a"
)

VERDICT_PROVEN = "EPISODE1_NEXT_MAJOR_ASK_BARRIER_HEADROOM_EVENT_TIME_PROVEN"
VERDICT_DEPTH_INSUFFICIENT = "EPISODE1_NEXT_MAJOR_ASK_BARRIER_DEPTH_INSUFFICIENT"
VERDICT_NO_BARRIER = "EPISODE1_NO_PAST_ONLY_MAJOR_ASK_BARRIER_FOUND"
VERDICT_BLOCKED = "EPISODE1_NEXT_MAJOR_ASK_BARRIER_HEADROOM_BLOCKED"

COVERAGE_OK = "OK"
COVERAGE_CENSORED_DETECTION = "HEADROOM_CENSORED_AT_DETECTION"
COVERAGE_EPOCH_BOUNDARY = "CENSORED_BY_EPOCH_BOUNDARY"
COVERAGE_BOOK_MISSING = "BOOK_COVERAGE_MISSING"
OUTCOME_CENSORED = "OUTCOME_CENSORED"

FEE_CONTRACT = {
    "entry_fee_pct": ENTRY_FEE_PCT,
    "exit_fee_pct": EXIT_FEE_PCT,
    "roundtrip_fee_pct": ROUNDTRIP_FEE_PCT,
    "required_net_profit_pct": REQUIRED_NET_PROFIT_PCT,
    "required_gross_headroom_pct": REQUIRED_GROSS_HEADROOM_PCT,
    "estimated_entry_slippage_pct": ESTIMATED_ENTRY_SLIPPAGE_PCT,
    "estimated_exit_slippage_pct": ESTIMATED_EXIT_SLIPPAGE_PCT,
    "note": "Slippage is never folded into fees. Binding check: gross >= 0.410.",
}

__all__ = [
    "VERDICT_PROVEN",
    "VERDICT_DEPTH_INSUFFICIENT",
    "VERDICT_NO_BARRIER",
    "FEE_CONTRACT",
    "CLUSTER_CONTRACT",
]
