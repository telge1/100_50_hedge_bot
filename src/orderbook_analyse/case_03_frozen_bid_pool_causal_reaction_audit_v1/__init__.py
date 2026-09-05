"""CASE_03 frozen BID pool causal reaction audit — read-only research."""

FORMAT_VERSION = "case_03_frozen_bid_pool_causal_reaction_audit/v1"
EXPECTED_FREEZE_BUNDLE_SHA256 = (
    "5ec44b95273af34508c327c841d5734e4ff1193caacb332d1f9d1e2cf79140d8"
)
FREEZE_DIR_REL = "results/liquidity_pool_case_sequence_freeze_v1"
CASE_ID = "CASE_03"
SYMBOL = "BTCUSDT"
COST_RT_BPS = (11.0, 15.0, 20.0)
ACCEPT_VARIANTS_S = (5, 15, 30, 60)
FLOW_WINDOWS_S = (1, 3, 5, 10, 30)
EDGE_TOL_BPS = 2.0
PRE_S = 30
MAX_POST_S = 30 * 60
MAJOR_WALL_RANK = 20
TIMEFRAMES = ("5m", "15m", "30m", "1h")
