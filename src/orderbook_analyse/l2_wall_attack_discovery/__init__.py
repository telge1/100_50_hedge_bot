"""L2 Wall Attack Pattern Discovery V1 — causal L2 + public-trade analysis."""

FORMAT_VERSION = "l2_wall_attack_discovery/v1"

TICK_BY_SYMBOL = {
    "BTCUSDT": 0.1,
    "DOGEUSDT": 0.00001,
    # Bybit linear XRPUSDT price tick (confirmed via 1m candle increments = 0.0001).
    # Missing entry previously fell back to 0.01 → zone half_width = 5*tick = 0.05
    # (~9% band) and false STACKED_EMA_ZONE overlaps on every minute.
    "XRPUSDT": 0.0001,
}

DECISION_CUTOFFS_S = (0, 1, 3, 5, 10)
OUTCOME_HORIZONS_S = (1, 3, 5, 10, 30, 60, 180, 300)
RESOLUTION_HORIZONS_S = (1, 3, 5, 10, 30, 60, 180, 300)
COST_BPS = (11, 15, 20)
