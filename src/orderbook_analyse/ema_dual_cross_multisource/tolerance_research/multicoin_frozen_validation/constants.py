"""Frozen multi-coin validation constants (research-only; no production defaults)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

DEFAULT_START = datetime(2026, 7, 24, tzinfo=timezone.utc)
DEFAULT_END = datetime(2026, 8, 23, tzinfo=timezone.utc)  # exclusive

DEFAULT_SYMBOLS_FILE = "config/universe_tradeable_51.json"
DEFAULT_OUTPUT_DIR = "results/edc_sync_tolerance/multicoin_30d_frozen_validation"

NOTIONAL_USDT = 1000.0
PRIMARY_COST_PCT = 0.15
COST_SENSITIVITY = (0.0, 0.11, 0.15, 0.20)
FUNDING_STATUS = "FUNDING_NOT_INCLUDED_DATA_UNAVAILABLE"
SAME_BAR_RULE = "SL_FIRST"
ENTRY_RULE = "SIGNAL_TF_NEXT_OPEN_AFTER_SIGNAL_BAR"
# Checkpoints written under prior entry rules must not be resumed silently.
LEGACY_ENTRY_RULES = frozenset(
    {
        "FIRST_1M_OPEN_AT_OR_AFTER_DECISION_AT",
        "FIRST_1M_OPEN_STRICTLY_AFTER_DECISION_AT",
    }
)

OB_PARSER = "ob200_v3"
OB_DEPTH = 200
EXPECTED_MINUTES_PER_DAY = 1440
EXPECTED_WINDOW_DAYS = 30

# Coverage thresholds frozen before any coin performance is inspected.
# These are *threshold passes*, not proof of complete minute-perfect coverage.
CANDLE_FULL_RATIO = 0.95  # candles_minutes / expected_window_minutes
OB_FULL_RATIO = 0.85  # orderbook minutes / expected_window_minutes
OB_COMPLETE_DAY_RATIO = 0.90  # per-day OB completeness (informational)
TRADES_MIN_RATIO = 0.50  # public_trades_minutes / candles_minutes
OUTCOME_MIN_RATIO = 0.90  # outcome 1m minutes / expected (preflight proxy = candles)
WARMUP_BARS_EXTRA = 20  # required_warmup = ema_slow + this

ELIGIBILITY_THRESHOLDS = {
    "candles_coverage_ratio_min": CANDLE_FULL_RATIO,
    "public_trades_coverage_ratio_min": TRADES_MIN_RATIO,
    "orderbook_coverage_ratio_min": OB_FULL_RATIO,
    "outcome_1m_coverage_ratio_min": OUTCOME_MIN_RATIO,
    "warmup_bars": "ema_slow + WARMUP_BARS_EXTRA (default 59+20=79 signal-TF bars)",
    "note": "Threshold pass ≠ complete coverage; per-candidate local windows still apply.",
}

# Class name retained for preflight compatibility; semantic = threshold pass.
ELIGIBLE_CORE_30D = "ELIGIBLE_CORE_30D"
ELIGIBLE_CORE_30D_THRESHOLD_PASS = "ELIGIBLE_CORE_30D_THRESHOLD_PASS"  # alias / docs
ELIGIBILITY_MEANS_THRESHOLD_PASS_NOT_COMPLETE_COVERAGE = True
ELIGIBLE_CORE_PARTIAL = "ELIGIBLE_CORE_PARTIAL"
INELIGIBLE_CORE = "INELIGIBLE_CORE"
LISTING_LIMITED = "LISTING_LIMITED"

MAIN_ELIGIBILITY = ELIGIBLE_CORE_30D
MIN_ELIGIBLE_FOR_ROBUST = 10
SMALL_SAMPLE_N = 10
EXIT_INCOMPLETE_OUTCOME = "INCOMPLETE_OUTCOME_HORIZON"

CONTROL_GROUPS = (
    "EMA_RAW",
    "CORE_RESEARCH_SUPPORTIVE",
    "CORE_RESEARCH_ADVERSE",
    "CORE_RESEARCH_MIXED",
    "CORE_RESEARCH_INSUFFICIENT",
    "FULL_MULTISOURCE",
    "PRODUCTION_ALLOW",
    "PRODUCTION_BLOCK",
    "PRODUCTION_INCONCLUSIVE",
)

PRIMARY_GROUP = "CORE_RESEARCH_SUPPORTIVE"
PRIMARY_TF = "5m"
PRIMARY_MODE = "M0_STRICT_SYNC"

# Frozen primary cells — do not alter after XRP freeze.
PRIMARY_CELLS: tuple[dict[str, Any], ...] = (
    {
        "cell_id": "M0_TP060_SL050_H6",
        "strategy_id": "TP060_SL050",
        "tp_pct": 0.60,
        "sl_pct": 0.50,
        "horizon": "6h",
        "horizon_min": 360,
        "is_reference": False,
    },
    {
        "cell_id": "M0_TP060_SL050_H8",
        "strategy_id": "TP060_SL050",
        "tp_pct": 0.60,
        "sl_pct": 0.50,
        "horizon": "8h",
        "horizon_min": 480,
        "is_reference": False,
    },
    {
        "cell_id": "M0_TP075_SL050_H6",
        "strategy_id": "TP075_SL050",
        "tp_pct": 0.75,
        "sl_pct": 0.50,
        "horizon": "6h",
        "horizon_min": 360,
        "is_reference": False,
    },
    {
        "cell_id": "M0_TP075_SL050_H8",
        "strategy_id": "TP075_SL050",
        "tp_pct": 0.75,
        "sl_pct": 0.50,
        "horizon": "8h",
        "horizon_min": 480,
        "is_reference": True,
    },
)

PRIMARY_REFERENCE_CELL_ID = "M0_TP075_SL050_H8"

SECONDARY_STRATEGIES: tuple[dict[str, Any], ...] = (
    {
        "strategy_key": "SEC_A_M5_TP040_SL100_H4",
        "label": "A",
        "timeframe": "5m",
        "mode_id": "M5_COMPRESSED_REBOUND",
        "group": PRIMARY_GROUP,
        "strategy_id": "TP040_SL100",
        "tp_pct": 0.40,
        "sl_pct": 1.00,
        "horizon": "4h",
        "horizon_min": 240,
        "role": "secondary",
    },
    {
        "strategy_key": "SEC_B_M4_TP050_SL050_H6",
        "label": "B",
        "timeframe": "15m",
        "mode_id": "M4_TOUCH_05_EXP_1",
        "group": PRIMARY_GROUP,
        "strategy_id": "TP050_SL050",
        "tp_pct": 0.50,
        "sl_pct": 0.50,
        "horizon": "6h",
        "horizon_min": 360,
        "role": "secondary",
    },
)

WARMUP_PAD_DAYS = 5
OUTCOME_PAD_HOURS = 12  # match original XRP horizon-matrix candle pad beyond window end
SOURCE_PAD_HOURS = 2
REQUIRE_FULL_HORIZON = False  # canonical: original XRP matrix; truncated → INCOMPLETE

CHECKPOINT_SCHEMA_VERSION = 1
CODE_STATUS = "MULTICOIN_FROZEN_VALIDATION_CODE_READY"

VERDICT_ROBUST = "MULTICOIN_FROZEN_VALIDATION_ROBUST"
VERDICT_NOT_ROBUST = "MULTICOIN_FROZEN_VALIDATION_NOT_ROBUST"
VERDICT_INSUFFICIENT = "INSUFFICIENT_MULTICOIN_COVERAGE"
VERDICT_FAILED = "MULTICOIN_FROZEN_VALIDATION_FAILED"
