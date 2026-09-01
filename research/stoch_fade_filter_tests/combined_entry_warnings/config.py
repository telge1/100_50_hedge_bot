"""Pinned identity for combined causal entry-warning research. No strategy edits."""

from __future__ import annotations

from pathlib import Path

DASHBOARD_ROOT = Path("/home/telgenbuescher/projects/spread_recovery_hedge_short_dev")
GOLD_ROOT = Path("/home/telgenbuescher/projects/wave_fade_gold_f16ae32")

EVALUATION_ID = "94d0cfbfb2da4c829dc0d95588dc052d"
SOURCE_JOB_ID = "f5909d14cba34fc9973a8b431530752d"
RUN_ID = "v1"
STRATEGY_VERSION = "wave_fade_frozen_f16ae32_causal_entry_v1"
ZEC_SYMBOL = "ZECUSDT"
FEE_PP = 0.11
RANDOM_SEED = 20260817
STOCH_LOW = 20.0
STOCH_HIGH = 80.0
W3_THRESHOLD = 0.25
OOS_CAVEAT = "EXPLORATORY_NOT_PRISTINE_OOS"

EVAL_DIR = DASHBOARD_ROOT / "results" / "stoch_fade_research_evaluations" / EVALUATION_ID
JOB_DIR = DASHBOARD_ROOT / "results" / "stoch_fade_research_jobs" / SOURCE_JOB_ID
ZEC_CONTEXT_DIR = (
    DASHBOARD_ROOT
    / "results"
    / "stoch_fade_trade_context_analysis"
    / f"{ZEC_SYMBOL}_{EVALUATION_ID}"
)
ZEC_CONTEXT_PARQUET = ZEC_CONTEXT_DIR / "zec_trade_context.parquet"
PREV_5M_DIR = (
    DASHBOARD_ROOT
    / "results"
    / "stoch_fade_filter_tests"
    / f"zec_5m_exhaustion_{EVALUATION_ID}"
)
PREV_5M_DECISIONS = PREV_5M_DIR / "trade_decisions.parquet"
OUTPUT_DIR = (
    DASHBOARD_ROOT
    / "results"
    / "stoch_fade_filter_tests"
    / f"combined_entry_warnings_{EVALUATION_ID}_{RUN_ID}"
)

EXPECTED_ZEC_TRADES = 1158
EXPECTED_ZEC_WINS = 527
EXPECTED_ZEC_LOSSES = 629
EXPECTED_ZEC_OPEN = 2
EXPECTED_ZEC_GROSS_SUM = -16.5
EXPECTED_ZEC_W1_TRUE = 584
EXPECTED_SPLIT = {"development": 694, "validation": 231, "test": 233}

EXTERNAL_COINS: tuple[str, ...] = (
    "SOLUSDT",
    "HYPEUSDT",
    "XAUTUSDT",
    "LINKUSDT",
    "DOGEUSDT",
    "BNBUSDT",
    "SUIUSDT",
    "ADAUSDT",
    "AVAXUSDT",
)
ALL_COINS: tuple[str, ...] = (ZEC_SYMBOL,) + EXTERNAL_COINS
SNAPSHOT_TFS: tuple[str, ...] = ("1m", "5m", "15m", "30m", "1h", "4h")
RULE_IDS: tuple[str, ...] = ("R0", "R1", "R2", "R3", "R4", "R5", "R6", "R7", "R8", "R9")
MANUAL_CASES: tuple[tuple[str, str], ...] = (
    ("2026-08-16T05:31:00Z", "SHORT"),
    ("2026-08-16T09:46:00Z", "SHORT"),
)
LIVE_BOT_ENV = Path("/home/telgenbuescher/projects/wave_fade_gold_live_bot/.env")
CANDLE_LOAD_START = "2025-12-11T00:00:00Z"
CANDLE_LOAD_END_EXCLUSIVE = "2026-08-17T00:01:00Z"
PIN_CANDLE_DATA_TO = "2026-08-17T00:00:00Z"
HORIZONS_MIN: dict[str, int] = {
    "15m": 15,
    "30m": 30,
    "1h": 60,
    "2h": 120,
    "4h": 240,
    "6h": 360,
    "12h": 720,
    "24h": 1440,
}
