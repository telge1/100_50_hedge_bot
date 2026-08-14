"""Env and path configuration for POOL_ORDER_PLAN_V1. Research view default on."""

from __future__ import annotations

import os
from pathlib import Path

STRATEGY_ID = "POOL_ORDER_PLAN_V1"
BASELINE_STRATEGY_ID = "wave_fade_no_be50_v1"

DASHBOARD_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = DASHBOARD_ROOT.parent

DEFAULT_PLANNER_ROOT = Path("/home/telgenbuescher/projects/pool_order_planer")
DEFAULT_SIGNAL_GENERATOR_ROOT = Path(
    "/home/telgenbuescher/projects/Signal_Generator_Ralf/signal_generator_stoch_waves"
)
EXPECTED_PLANNER_COMMIT = "c6c960a82e9a0c538dbe24b03f481893e722072f"
EXPECTED_PLANNER_COMMIT_SHORT = "c6c960a"

FEE_PCT = 0.11
LOOKBACK = 8
REPLAY = False
WARMUP_DAYS = 14
UNIVERSE_HISTORY_START = "2025-12-11T00:00:00Z"

HOLD_MINUTES_BY_TF = {
    "15m": 24 * 60,
    "30m": 48 * 60,
    "1h": 72 * 60,
    "4h": 10 * 24 * 60,
}
DEFAULT_HOLD_MINUTES = 24 * 60

CLICKHOUSE_ENV_FILE = DEFAULT_SIGNAL_GENERATOR_ROOT / ".env"


def _truthy(raw: str | None) -> bool:
    return str(raw or "").strip().lower() in ("1", "true", "yes", "on")


def _falsy(raw: str | None) -> bool:
    return str(raw or "").strip().lower() in ("0", "false", "no", "off")


def artifacts_dir(environ: dict | None = None) -> Path:
    env = environ if environ is not None else os.environ
    override = str(env.get("POOL_ORDER_PLAN_ARTIFACT_DIR") or "").strip()
    if override:
        return Path(override)
    return production_artifacts_dir()


def production_artifacts_dir() -> Path:
    return REPO_ROOT / "results" / "pool_order_plan_v1"


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


def enable_pool_order_plan_v1(environ: dict | None = None) -> bool:
    """Research dropdown default on. Explicit false/0/no/off disables."""
    env = environ if environ is not None else os.environ
    if "ENABLE_POOL_ORDER_PLAN_V1" not in env:
        return True
    raw = env.get("ENABLE_POOL_ORDER_PLAN_V1")
    if _falsy(raw if raw is None else str(raw)):
        return False
    if _truthy(raw if raw is None else str(raw)):
        return True
    return True


def allow_dirty_planner(environ: dict | None = None) -> bool:
    env = environ if environ is not None else os.environ
    return _truthy(env.get("POOL_ORDER_PLAN_ALLOW_DIRTY_PLANNER"))


def hold_minutes_for_tf(timeframe: str | None) -> int:
    return int(HOLD_MINUTES_BY_TF.get(str(timeframe or "").strip(), DEFAULT_HOLD_MINUTES))
