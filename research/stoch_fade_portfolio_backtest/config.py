"""Pinned portfolio-backtest identity. No strategy formulas."""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
JOBS_ROOT = REPO_ROOT / "results" / "stoch_fade_research_jobs"
EVALS_ROOT = REPO_ROOT / "results" / "stoch_fade_research_evaluations"
DEFAULT_OUT_ROOT = REPO_ROOT / "results" / "stoch_fade_portfolio_backtests"
UNIVERSE_PATH = Path(
    "/home/telgenbuescher/projects/Signal_Generator_Ralf/signal_generator_stoch_waves"
    "/config/universe_tradeable_51.json"
)

SIGNAL_STRATEGY_VERSION = "wave_fade_frozen_f16ae32"
SIGNAL_SCOPE = "TIER_A_ONLY"
EXIT_POLICY = "NO_BE50"
OUTCOME_ENGINE = "evaluate_signal_no_be50_full_1m"
INTRABAR_POLICY = "SL_FIRST"
PORTFOLIO_POLICY = "TEN_FIXED_SLOTS_V1"

INITIAL_BALANCE = 1000.0
MAX_SLOTS = 10
NOTIONAL = 100.0
TF_PRIORITY = ("4h", "1h", "30m", "15m")
TF_RANK = {name: idx for idx, name in enumerate(reversed(TF_PRIORITY))}  # 15m=0 .. 4h=3

ALLOWED_OUTCOMES = frozenset({"WIN", "LOSS", "OPEN"})
JOB_ID_RE = r"^[0-9a-f]{32}$"

INCOMPLETE_JOIN = "BLOCKED_BY_INCOMPLETE_51_COIN_SIGNAL_OUTCOME_JOIN"
AMBIGUOUS_EVAL = "BLOCKED_BY_AMBIGUOUS_51_COIN_EVALUATION"
