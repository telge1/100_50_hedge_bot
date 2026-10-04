"""Frozen parameters — long_geometry_ladder24_be100_v1."""

from __future__ import annotations

from datetime import datetime, timezone

STRATEGY_NAME = "long_geometry_ladder24_be100_v1"
STRATEGY_VERSION = "v1"

# Entry geometry (mirrored short baseline, unchanged from long geometry V1 research).
ATTACH_PCT = 1.0
MIN_BRIDGE_GAP_PCT = 0.5  # documented constant (research file); bridge gate uses MIN_POOL_BRIDGE_GAP_PCT.
MIN_POOL_BRIDGE_GAP_PCT = 0.8
MIN_TP_PCT = 0.8
STOP_PAD = 0.002

# Decision / management timeframes.
ENTRY_TIMEFRAME = "15m"
MANAGEMENT_TIMEFRAME = "1m"

# Forward outcome horizon (48h on entry TF).
HORIZON_HOURS = 48

# Quality filter (FROZEN).
LADDER_MAX_AGE_H = 24.0
LADDER_FEATURE = "m15_lower_2_age_h"

# Break-even (FROZEN).
BE_TRIGGER_MFE_PCT = 1.00
BE_STOP_OFFSET_PCT = 0.0  # stop at entry

# Universe.
SYMBOLS = ("XRPUSDT", "ADAUSDT", "DOGEUSDT")

# Pane load (history for pools).
PANE_FROM = datetime(2026, 2, 10, tzinfo=timezone.utc)
PANE_TO = datetime(2026, 9, 30, 23, 59, 59, tzinfo=timezone.utc)
HISTORY_WEEKS = 12

# 1m data window for management (matches long_oos_validation_v1 / rest_sl_mfe research).
BE_1M_LOAD_START = datetime(2026, 5, 1, tzinfo=timezone.utc)
BE_1M_LOAD_END = datetime(2026, 8, 15, 23, 59, 59, tzinfo=timezone.utc)

PERIODS = {
    "jun-jul": (
        datetime(2026, 6, 1, tzinfo=timezone.utc),
        datetime(2026, 7, 31, 23, 59, 59, tzinfo=timezone.utc),
    ),
    "apr-may": (
        datetime(2026, 4, 1, tzinfo=timezone.utc),
        datetime(2026, 5, 31, 23, 59, 59, tzinfo=timezone.utc),
    ),
}

# Reproduction tolerances (documented).
PNL_TOLERANCE_PCT = 0.02
DD_TOLERANCE_PCT = 0.02
