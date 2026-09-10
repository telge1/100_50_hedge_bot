"""Episode-1 wall structure / generations / migration proxy within continuous epochs.

Frozen packages are NOT modified. Epoch 4→5 is never bridged.

Outcome-blind analysis band (fixed before reading post-breach outcomes):
  side = ask
  original_wall = 79780.0
  tick = 0.1
  band = [original_wall - BAND_TICKS_BELOW*tick, original_wall + BAND_TICKS_ABOVE*tick]
  BAND_TICKS_BELOW = 0
  BAND_TICKS_ABOVE = 500   # research default for structure visibility (+50.0)
  MIN_RELEVANT_ASK_QTY = 0.05  # research default; not tuned on Episode-1 reclaim

Status labels:
  WALL_MIGRATION_NOT_CALIBRATED
  CENSORED_BY_EPOCH_BOUNDARY for horizons past continuous coverage
"""

from __future__ import annotations

AUDIT_ID = "LEVEL_FIRST_EPISODE1_WALL_MIGRATION_STRUCTURE_V1"
SCHEMA_VERSION = "level_first_episode1_wall_migration_structure_v1"
RUN_PREFIX = "wms1_"

SYMBOL = "BTCUSDT"
TICK_SIZE = 0.1
ORIGINAL_WALL_PRICE = 79780.0
ORIGINAL_WALL_SIDE = "ask"
ORIGINAL_WALL_GENERATION_ID = "wg_92a0711707fb2ee4"

# Outcome-blind band contract (documented; not chosen from reclaim outcome).
BAND_TICKS_BELOW = 0
BAND_TICKS_ABOVE = 500  # +50.0 USD on BTC tick 0.1; not reclaim-tuned
MIN_RELEVANT_ASK_QTY = 0.05
EXACT_BAND_TICKS = 5  # same research default as wall-flow defended band

# Continuous Epoch-4 coverage (from coverage audit — do not extend).
EPOCH4_COVERAGE_START = "2026-09-06T20:19:02.300Z"
EPOCH4_COVERAGE_END = "2026-09-06T20:19:59.800Z"
ASK_WALL_BREACH = "2026-09-06T20:19:41.900Z"
ORIGINAL_GEN_END = "2026-09-06T20:19:41.872Z"
EPOCH4 = 4
EPOCH5 = 5
EPOCH5_CHECKPOINT = "2026-09-06T20:19:59.873Z"

BREACH_OUTCOME_OFFSETS_S = (1, 3, 5, 10, 15)

FROZEN_BOOK_DIR = (
    "results/level_first_episode1_wall_flow_qdh_base_v1/BTCUSDT/wfq1_58db1918314881b6a"
)
FROZEN_HANDOFF_PATH = (
    "results/level_first_episode1_detection_to_wall_flow_integration_v1/"
    "BTCUSDT/d2w1_eee5d0eefaffa/episode1_handoff.json"
)
FROZEN_TRADES = (
    "results/level_first_episode1_corrected_sms1_persist_v1/BTCUSDT/"
    "episode1_independent_derivation_inputs_v1/public_trades_zone_window.jsonl"
)
FROZEN_WALL_FLOW_TIMELINE = (
    "results/level_first_episode1_wall_flow_qdh_base_v1/"
    "BTCUSDT/wfq1_58db1918314881b6a/feature_timeline.csv"
)

WALL_MIGRATION_STATUS = "WALL_MIGRATION_NOT_CALIBRATED"
TIME_BASIS = "EVENT_TIME_ONLY"
WALL_STATE = "NOT_CLASSIFIED"

VERDICT_STRUCTURE_PROVEN = "EPISODE1_WITHIN_EPOCH_WALL_MIGRATION_STRUCTURE_PROVEN"
VERDICT_NOT_OBSERVED = "EPISODE1_WALL_MIGRATION_NOT_OBSERVED_WITHIN_VALID_COVERAGE"
VERDICT_INDETERMINATE = "EPISODE1_WALL_MIGRATION_INDETERMINATE_DUE_TO_COVERAGE"

ALLOW_CLICKHOUSE_WRITES = False
EPSILON = 1e-12
MASS_TOL = 1e-9

LAYER_PRE_EXISTING = "PRE_EXISTING_LAYER"
LAYER_POST_BREACH = "POST_BREACH_NEW_GENERATION"
LAYER_POST_EPOCH = "POST_EPOCH_INDEPENDENT"


def analysis_band_bounds(
    *,
    wall_price: float = ORIGINAL_WALL_PRICE,
    tick_size: float = TICK_SIZE,
    ticks_below: int = BAND_TICKS_BELOW,
    ticks_above: int = BAND_TICKS_ABOVE,
) -> tuple[float, float]:
    return (
        float(wall_price) - float(ticks_below) * float(tick_size),
        float(wall_price) + float(ticks_above) * float(tick_size),
    )


BAND_CONTRACT = {
    "name": "episode1_ask_structure_band_v1",
    "side": ORIGINAL_WALL_SIDE,
    "original_wall_price": ORIGINAL_WALL_PRICE,
    "tick_size": TICK_SIZE,
    "ticks_below": BAND_TICKS_BELOW,
    "ticks_above": BAND_TICKS_ABOVE,
    "band_low": analysis_band_bounds()[0],
    "band_high": analysis_band_bounds()[1],
    "min_relevant_ask_qty": MIN_RELEVANT_ASK_QTY,
    "exact_defended_band_ticks": EXACT_BAND_TICKS,
    "selection_rule": (
        "Fixed before inspecting post-breach reclaim/outcome paths. "
        "Ask prices in [wall, wall+500 ticks] with qty>=MIN_RELEVANT_ASK_QTY. "
        "Not optimized on Episode-1 reclaim."
    ),
}

__all__ = [
    "AUDIT_ID",
    "VERDICT_STRUCTURE_PROVEN",
    "VERDICT_NOT_OBSERVED",
    "VERDICT_INDETERMINATE",
    "BAND_CONTRACT",
    "WALL_MIGRATION_STATUS",
]
