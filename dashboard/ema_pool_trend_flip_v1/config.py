"""Env and path configuration for EMA_POOL_TREND_FLIP_V1. Research view default on."""

from __future__ import annotations

import os
from pathlib import Path

STRATEGY_ID = "EMA_POOL_TREND_FLIP_V1"
STRATEGY_LABEL = "EMA Pool Trend Flip V1 · Research"
BASELINE_STRATEGY_ID = "wave_fade_no_be50_v1"
FILTER_STRATEGY_ID = "EMA_POOL_DIRECTION_FILTER_V1"
STATIC_VARIANT = "EMA_POOL_TREND_FLIP_V1_STATIC"
RATCHET_VARIANT = "EMA_POOL_TREND_FLIP_V1_RATCHET"

DASHBOARD_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = DASHBOARD_ROOT.parent

DEFAULT_PLANNER_ROOT = Path("/home/telgenbuescher/projects/pool_order_planer")
DEFAULT_SIGNAL_GENERATOR_ROOT = Path(
    "/home/telgenbuescher/projects/Signal_Generator_Ralf/signal_generator_stoch_waves"
)
EXPECTED_PLANNER_COMMIT = "c6c960a82e9a0c538dbe24b03f481893e722072f"

FEE_PCT = 0.11  # percent of notional, round-trip (entry+exit)
LOOKBACK = 8
REPLAY = False
WARMUP_DAYS = 14

EMA_FAST = 9
EMA_SLOW = 20
ATR_PERIOD = 14
EMA_CROSS_CONFIRMATION_BARS = 2
EMA_CROSS_MIN_SEPARATION_ATR = 0.05
EMA_GAP_GROWTH_BARS = 3
ATR_AUDIT_LEVELS = (0.00, 0.025, 0.05, 0.10)

# Stochastic episode (signal TF): leave extreme before a new episode starts.
STOCH_K_PERIOD = 14
STOCH_D_PERIOD = 3
STOCH_SMOOTH = 3
STOCH_OVERBOUGHT = 80.0
STOCH_OVERSOLD = 20.0

# Pool bias: distance-weighted BigBeluga strength_sum * cluster size factor.
POOL_BIAS_DISTANCE_HALFLIFE_PCT = 1.0
POOL_BIAS_CLUSTER_COUNT_WEIGHT = 0.25
POOL_BIAS_MIN_RATIO = 1.0

ACE_FROZEN_START = "2026-08-12T15:03:43Z"
ACE_FROZEN_END = "2026-08-14T15:03:43Z"

CLICKHOUSE_ENV_FILE = DEFAULT_SIGNAL_GENERATOR_ROOT / ".env"


def _truthy(raw: str | None) -> bool:
    return str(raw or "").strip().lower() in ("1", "true", "yes", "on")


def _falsy(raw: str | None) -> bool:
    return str(raw or "").strip().lower() in ("0", "false", "no", "off")


def artifacts_dir(environ: dict | None = None) -> Path:
    env = environ if environ is not None else os.environ
    override = str(env.get("EMA_POOL_TREND_FLIP_ARTIFACT_DIR") or "").strip()
    if override:
        return Path(override)
    return production_artifacts_dir()


def production_artifacts_dir() -> Path:
    return REPO_ROOT / "results" / "ema_pool_trend_flip_v1"


def planner_root(environ: dict | None = None) -> Path:
    env = environ if environ is not None else os.environ
    raw = str(env.get("POOL_ORDER_PLANNER_ROOT") or DEFAULT_PLANNER_ROOT)
    return Path(raw).expanduser().resolve()


def signal_generator_root(environ: dict | None = None) -> Path:
    env = environ if environ is not None else os.environ
    raw = str(env.get("POOL_ORDER_SIGNAL_GENERATOR_ROOT") or DEFAULT_SIGNAL_GENERATOR_ROOT)
    return Path(raw).expanduser().resolve()


def expected_planner_commit(environ: dict | None = None) -> str:
    env = environ if environ is not None else os.environ
    return str(env.get("POOL_ORDER_PLANNER_COMMIT") or EXPECTED_PLANNER_COMMIT).strip()


def enable_ema_pool_trend_flip_v1(environ: dict | None = None) -> bool:
    env = environ if environ is not None else os.environ
    if "ENABLE_EMA_POOL_TREND_FLIP_V1" not in env:
        return True
    raw = env.get("ENABLE_EMA_POOL_TREND_FLIP_V1")
    if _falsy(raw if raw is None else str(raw)):
        return False
    return True


def allow_dirty_planner(environ: dict | None = None) -> bool:
    env = environ if environ is not None else os.environ
    return _truthy(env.get("POOL_ORDER_PLAN_ALLOW_DIRTY_PLANNER"))
