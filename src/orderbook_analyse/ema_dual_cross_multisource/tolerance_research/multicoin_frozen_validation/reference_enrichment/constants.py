"""Frozen reference strategy + enrichment paths (research-only)."""

from __future__ import annotations

from datetime import datetime, timezone

# v2 shared-engine backtest is the binding enrichment input (parity confirmed).
DEFAULT_INPUT_DIR = "results/edc_sync_tolerance/multicoin_30d_frozen_validation_v2_shared_engine"
DEFAULT_OUTPUT_DIR = "results/edc_sync_tolerance/multicoin_reference_enrichment_v2_shared_engine"
DEFAULT_START = datetime(2026, 7, 24, tzinfo=timezone.utc)
DEFAULT_END = datetime(2026, 8, 23, tzinfo=timezone.utc)

REF_TIMEFRAME = "5m"
REF_MODE = "M0_STRICT_SYNC"
REF_GROUP = "CORE_RESEARCH_SUPPORTIVE"
REF_STRATEGY_KEY = "M0_TP075_SL050_H8"
REF_STRATEGY_ID = "TP075_SL050"
REF_TP_PCT = 0.75
REF_SL_PCT = 0.50
REF_HORIZON = "8h"
REF_HORIZON_MIN = 480
REF_COST_PCT = 0.15
REF_NOTIONAL = 1000.0
ENTRY_RULE = "SIGNAL_TF_NEXT_OPEN_AFTER_SIGNAL_BAR"
# Include XRP after shared-engine 1:1 confirmation (needed for XRP vs rest analysis).
EXCLUDE_SYMBOLS: frozenset[str] = frozenset()

CHECKPOINT_SCHEMA_VERSION = 1
CODE_STATUS = "MULTICOIN_REFERENCE_ENRICHMENT_CODE_READY"
STATUS_COMPLETE = "MULTICOIN_REFERENCE_ENRICHMENT_COMPLETE"
STATUS_FAILED_PARITY = "FAILED_REFERENCE_PARITY"
STATUS_EMPTY_REFERENCE = "EMPTY_FROZEN_REFERENCE"
STATUS_INCOMPLETE = "INCOMPLETE_ENRICHMENT"
STATUS_ENRICHMENT_FAILED = "ENRICHMENT_FAILED"
STATUS_V2_COMPLETE = "MULTICOIN_REFERENCE_ENRICHMENT_V2_COMPLETE"
STATUS_V2_PARTIAL = "MULTICOIN_REFERENCE_ENRICHMENT_V2_PARTIAL"
STATUS_V2_FAILED = "MULTICOIN_REFERENCE_ENRICHMENT_V2_FAILED"
EXPECTED_REFERENCE_TRADES_V2 = 503

FEATURE_PREFIX = "feature__"
LABEL_PREFIX = "label__"

OB_PARSER = "ob200_v3"
OB_DEPTH = 200
ATR_PERIOD = 14
ATR_LONG_PERIOD = 56  # baseline for atr_short_long_ratio
OB_STALE_SECONDS = 300
