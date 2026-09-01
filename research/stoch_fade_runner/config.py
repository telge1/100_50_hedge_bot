"""Pinned canonical causal Wave-Fade research window. Does not mutate live strategy defaults."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SG_ROOT = Path("/home/telgenbuescher/projects/wave_fade_gold_f16ae32")
DEFAULT_RUNS_ROOT = REPO_ROOT / "results" / "stoch_fade_research_runs"

STRATEGY_ID = "wave_fade_frozen_f16ae32_causal_entry_v1"
SOURCE_COMMIT_PIN = "f16ae32"
GENERATOR_VERSION = "stoch_fade_causal_dashboard_runner_v1"
PHASE = "2A_CAUSAL"
MAX_SYMBOLS = 1
DEFAULT_CANARY_SYMBOL = "ETHUSDT"
CANARY_SYMBOL = DEFAULT_CANARY_SYMBOL
WARMUP_DAYS = 80
REQUESTED_SIGNAL_START = datetime(2025, 12, 11, 0, 0, 0, tzinfo=timezone.utc)
REQUESTED_SIGNAL_END_EXCLUSIVE = datetime(2026, 8, 15, 9, 42, 0, tzinfo=timezone.utc)
CAUSAL_MANIFEST_HASH = "dac39cb3a7749f400126b6f2b8d9fd6aa2ac5524ca2cf8b4ff7e2d3da422d3cf"
CONFIRMATION_POLICY = "cross_recognition"
CONFIRMATION_SOURCE = CONFIRMATION_POLICY
OUTCOME_ENGINE = "research.stoch_fade_evaluation.full_1m_scan.evaluate_signal_no_be50_full_1m"
INTRABAR_POLICY = "SL_FIRST"
EXIT_POLICY = "NO_BE50"

SIDE_EFFECT_FLAGS = {
    "writes_to_clickhouse": False,
    "writes_to_signals": False,
    "writes_to_processing_state": False,
    "cleanup_enabled": False,
    "publish_enabled": False,
    "live_orders_enabled": False,
}

FORBIDDEN_CLI_TOKENS = (
    "--cleanup-first",
    "run_wave_fade_shadow_pipeline",
    "cleanup-first",
)

EVAL_WITH_SIGNALS = "EVALUATED_WITH_SIGNALS"
EVAL_NO_SIGNAL = "EVALUATED_NO_SIGNAL"
EVAL_NO_CANDLE = "NO_CANDLE_DATA"
EVAL_INCOMPLETE = "INCOMPLETE_DATA"
EVAL_ERROR = "RUNNER_ERROR"
EVAL_NOT = "NOT_EVALUATED"
EVAL_RUNTIME_ROOT = "RUNTIME_ROOT_UNSAFE"


def sg_root(environ: dict | None = None) -> Path:
    env = environ if environ is not None else os.environ
    override = str(env.get("STOCH_FADE_SIGNAL_GENERATOR_ROOT") or "").strip()
    if override:
        return Path(override)
    return DEFAULT_SG_ROOT


def runs_root(environ: dict | None = None) -> Path:
    env = environ if environ is not None else os.environ
    override = str(env.get("STOCH_FADE_RUNS_ROOT") or "").strip()
    if override:
        return Path(override)
    return DEFAULT_RUNS_ROOT


def ensure_sg_on_path(environ: dict | None = None) -> Path:
    import sys

    root = sg_root(environ)
    src = str(root / "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    return root


def candle_load_start(signal_start: datetime) -> datetime:
    return signal_start - timedelta(days=WARMUP_DAYS)


def assert_frozen_pin() -> None:
    from .identity import frozen_identity

    frozen_identity()
