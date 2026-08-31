"""Frozen Clear Pool Selection Rule V1 (selection only)."""

from __future__ import annotations

RULE_ID = "CLEAR_POOL_SELECTION_RULE_V1"
RULE_STATUS = "FROZEN"
RULE_FROZEN_AT = "2026-08-31T18:00:00Z"

ONE_LINE_DE = (
    "Große HTF-Zonen, die beim Anlauf wirklich voll mit Book-Liquidität sind — "
    "der Rest ist nachrangig. Entscheidung = Verhalten dieser Zonen-Liquidität."
)

# Stage A — FILTER
STAGE_A_SYMBOL = "BTCUSDT"
STAGE_A_TIMEFRAMES = ("15m", "30m")
STAGE_A_MIN_P = 5
STAGE_A_TOUCH = "mid_inside_zone_band"
STAGE_A_BOOK_FILL = "resting_liquidity_inside_zone_on_approach"
STAGE_A_BOOK_FILL_SOT = "raw_ob200_zone_depth"
STAGE_A_BOOK_FILL_PROXY = "orderbook_features_1s_v2"  # weak proxy only — never A7 SoT
STAGE_A_A7_MIN_ZONE_LEVELS = 2
STAGE_A_A7_MIN_ZONE_QTY = 0.0

# Stage B — DECIDE (labels only; no entry/PnL)
STAGE_B_LABELS = ("ZONE_HELD", "ZONE_EATEN", "ZONE_PULLED", "ZONE_UNKNOWN")

SECONDARY_IGNORE = (
    "5m_as_primary",
    "singleton_or_pair_as_primary",
    "empty_zones",
    "dominant_1s_wall_without_zone_depth",
    "chart_cosmetics_alone",
    "outcomes_pnl",
)

# Package-local freeze (tracked). Results copies under results/ are regenerable.
from pathlib import Path as _Path

_PKG = _Path(__file__).resolve().parent
SPEC_MD = str(_PKG / "CLEAR_POOL_SELECTION_RULE_V1.md")
SPEC_JSON = str(_PKG / "CLEAR_POOL_SELECTION_RULE_V1.json")
