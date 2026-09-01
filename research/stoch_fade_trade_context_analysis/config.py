"""Pinned identity for ZEC causal trade-context analysis. No strategy edits."""

from __future__ import annotations

from pathlib import Path

DASHBOARD_ROOT = Path("/home/telgenbuescher/projects/spread_recovery_hedge_short_dev")
GOLD_ROOT = Path("/home/telgenbuescher/projects/wave_fade_gold_f16ae32")

EVALUATION_ID = "94d0cfbfb2da4c829dc0d95588dc052d"
SOURCE_JOB_ID = "f5909d14cba34fc9973a8b431530752d"
STRATEGY_VERSION = "wave_fade_frozen_f16ae32_causal_entry_v1"
SYMBOL = "ZECUSDT"
RUN_ID = EVALUATION_ID

EVAL_DIR = DASHBOARD_ROOT / "results" / "stoch_fade_research_evaluations" / EVALUATION_ID
OUTCOMES_JSONL = EVAL_DIR / "coin_runs" / SYMBOL / "outcomes.jsonl"
SIGNALS_JSONL = (
    DASHBOARD_ROOT
    / "results"
    / "stoch_fade_research_jobs"
    / SOURCE_JOB_ID
    / "coin_runs"
    / SYMBOL
    / "975211a7c4ba4d678ec68d00fb5b6842"
    / "signals.jsonl"
)
OUTPUT_DIR = (
    DASHBOARD_ROOT
    / "results"
    / "stoch_fade_trade_context_analysis"
    / f"{SYMBOL}_{RUN_ID}"
)

EXPECTED_TRADES = 1158
EXPECTED_WINS = 527
EXPECTED_LOSSES = 629
EXPECTED_OPEN = 2

CONFIRMATION_POLICY = "cross_recognition"
EXIT_POLICY = "NO_BE50"
INTRABAR_POLICY = "SL_FIRST"
MAX_HOLD = False
FEE_PP = 0.11
RANDOM_SEED = 20260817
BOOTSTRAP_ITERS = 2000

CANDLE_LOAD_START = "2025-12-11T00:00:00Z"
CANDLE_LOAD_END_EXCLUSIVE = "2026-08-17T00:01:00Z"
PIN_CANDLE_DATA_TO = "2026-08-17T00:00:00Z"

SNAPSHOT_TFS: tuple[str, ...] = ("1m", "5m", "15m", "30m", "1h", "4h", "1d")
SIGNAL_TFS: tuple[str, ...] = ("15m", "30m", "1h", "4h")
TF_MINUTES: dict[str, int] = {
    "1m": 1,
    "5m": 5,
    "15m": 15,
    "30m": 30,
    "1h": 60,
    "4h": 240,
    "1d": 1440,
}
TF_RANK: dict[str, int] = {"15m": 0, "30m": 1, "1h": 2, "4h": 3}
TPSL_BY_TF: dict[str, tuple[float, float]] = {
    "15m": (1.0, 1.0),
    "30m": (2.0, 1.5),
    "1h": (2.0, 1.5),
    "4h": (4.0, 2.0),
}

MANUAL_CASES: tuple[tuple[str, str], ...] = (
    ("2026-08-16T05:31:00Z", "SHORT"),
    ("2026-08-16T09:46:00Z", "SHORT"),
)

LIVE_BOT_ENV = Path("/home/telgenbuescher/projects/wave_fade_gold_live_bot/.env")
EMA_EXTRA_SPANS: tuple[int, ...] = (50, 200)
ATR_LENGTH = 14
STOCH_LOW = 20.0
STOCH_HIGH = 80.0
