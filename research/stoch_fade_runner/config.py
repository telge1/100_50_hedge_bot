"""Pinned Frozen Wave-Fade research window. Does not mutate live strategy defaults."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SG_ROOT = Path(
    "/home/telgenbuescher/projects/Signal_Generator_Ralf/signal_generator_stoch_waves"
)
DEFAULT_RUNS_ROOT = REPO_ROOT / "results" / "stoch_fade_research_runs"

STRATEGY_ID = "wave_fade_frozen_f16ae32"
SOURCE_COMMIT_PIN = "f16ae32"
GENERATOR_VERSION = "stoch_fade_research_runner_v1"
PHASE = "2A"
MAX_SYMBOLS = 1
DEFAULT_CANARY_SYMBOL = "1000PEPEUSDT"
CANARY_SYMBOL = DEFAULT_CANARY_SYMBOL
WARMUP_DAYS = 80
REQUESTED_SIGNAL_START = datetime(2025, 12, 11, 0, 0, 0, tzinfo=timezone.utc)
REQUESTED_SIGNAL_END_EXCLUSIVE = datetime(2026, 8, 15, 9, 42, 0, tzinfo=timezone.utc)

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
