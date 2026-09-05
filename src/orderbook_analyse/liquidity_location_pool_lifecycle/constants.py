"""Sensitivity grids and horizon constants (transparent; no silent invention)."""

from __future__ import annotations

# Outcome sensitivity (no project-wide LLD reclaim/acceptance constants found)
ACCEPTANCE_BARS: tuple[int, ...] = (1, 2, 3)
RECLAIM_HORIZON_BARS: tuple[int, ...] = (1, 3, 6, 12)
REACTION_ATR_MULTS: tuple[float, ...] = (0.25, 0.5, 1.0)

# Post-touch / post-sweep destination horizons (wall-clock)
DESTINATION_HORIZONS_MIN: tuple[int, ...] = (15, 30, 60, 120, 240, 480, 720, 1440)

# Approach / swing conventions for this study
APPROACH_ATR_MULT = 0.5
ATR_PERIOD = 14
SWING_LOOKBACK = 5
EMA_PERIODS: tuple[int, ...] = (9, 20, 59, 200)

# Cluster gap matches TRP default (percent of price)
CLUSTER_GAP_PCT = 0.10

# Smoke scope
SMOKE_SYMBOLS: tuple[str, ...] = ("XRPUSDT", "DOGEUSDT", "BTCUSDT")
SMOKE_TIMEFRAMES: tuple[str, ...] = ("5m", "15m", "30m")

SIDE_MAP = {"lower": "BID", "upper": "ASK"}
SIDE_ENGINE = {"BID": "lower", "ASK": "upper"}

# Pool-count cohort buckets
POOL_COUNT_BUCKETS: tuple[tuple[str, int, int | None], ...] = (
    ("1", 1, 1),
    ("2", 2, 2),
    ("3", 3, 3),
    ("4-5", 4, 5),
    ("6+", 6, None),
)
