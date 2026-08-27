"""Frozen fade research job paths. No strategy formulas. No CH writers."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

DASHBOARD_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = DASHBOARD_ROOT.parent
DEFAULT_SG_ROOT = Path("/home/telgenbuescher/projects/wave_fade_gold_f16ae32")
DEFAULT_SG_PYTHON = DEFAULT_SG_ROOT / ".venv" / "bin" / "python"
DEFAULT_JOBS_ROOT = REPO_ROOT / "results" / "stoch_fade_research_jobs"
DEFAULT_UNIVERSE = DEFAULT_SG_ROOT / "config" / "universe_tradeable_51.json"
WORKER_SCRIPT = Path(__file__).resolve().parent / "worker.py"

STRATEGY_VERSION = "wave_fade_frozen_f16ae32_causal_entry_v1"
EZM_STRATEGY_ID = "ema_zone_microstructure_confirmation_v1"
EZM_RUN_INTENT = "candidate_discovery"
EZM_RUNNER_KIND = "ezm_continuous_discovery"
EZM_RESULT_CONTRACT_VERSION = "ezm_stoch_signale_candidates/v1"
EZM_COMPUTATION_MODE_EMA_ONLY = "ema_only"
EZM_COMPUTATION_MODE_EMA_PLUS_MICRO = "ema_plus_microstructure"
FROZEN_RUNNER_KIND = "stoch_fade_runner"
FROZEN_RESULT_CONTRACT_VERSION = "frozen_fade_signals/v1"
FROZEN_RUN_INTENT = "trade_signal_research"
# Server-side whitelist only — never trust browser values as modules/paths.
ALLOWED_STRATEGY_IDS = frozenset({STRATEGY_VERSION, EZM_STRATEGY_ID})
DEFAULT_OA_ROOT = Path("/home/telgenbuescher/projects/orderbook_analyse")
DEFAULT_EZM_COIN_TIMEOUT_S = 2700
SOURCE_COMMIT = "f16ae32da38da86f39e75b09c63c31f62d11996b"
EDGES_VERSION = "apt_is_q4_frozen_20260808"
CAUSAL_MANIFEST_HASH = "dac39cb3a7749f400126b6f2b8d9fd6aa2ac5524ca2cf8b4ff7e2d3da422d3cf"
CONFIRMATION_POLICY = "cross_recognition"
CONFIRMATION_SOURCE = CONFIRMATION_POLICY
EXIT_POLICY = "NO_BE50"
INTRABAR_POLICY = "SL_FIRST"
DEFAULT_SIGNAL_START = datetime(2025, 12, 11, 0, 0, 0, tzinfo=timezone.utc)
MAX_SYMBOLS = 51
MAX_WINDOW_DAYS = 400
DEFAULT_COIN_TIMEOUT_S = 600
COIN_TERM_GRACE_S = 5
EST_SECONDS_PER_COIN = 60
EST_BYTES_PER_COIN = 20 * 1024 * 1024
MIN_FREE_BYTES_51 = 2 * 1024 * 1024 * 1024
PER_COIN_DISK_BYTES = 50 * 1024 * 1024
DISK_RESERVE_BYTES = 512 * 1024 * 1024

FROZEN_MODULE_HASHES = {
    "src/signal_generator/pipeline/versions.py": (
        "0919699d7ac702bc13983a9f810afc201898d7e3d565dd83a535f265afd12680"
    ),
    "src/signal_generator/strategy/wave_fade/parameters.py": (
        "0840ab112f4ca9685bab79901a5b378fa0f70340dbf995a8a3644322e082b7cd"
    ),
    "src/signal_generator/strategy/wave_fade/signals.py": (
        "adbc4d940c69a12ee5dc37af6ee2aa6a35ab35aadb36d5815ccc28516fbe286a"
    ),
    "src/signal_generator/strategy/wave_fade/edges.py": (
        "b3ada4d09ad6c5a588ccdbfa9d724f0ca6757e2b83d5dc6256ac6cd84d6317f7"
    ),
    "src/signal_generator/strategy/wave_fade/trend.py": (
        "d1c1941f3b22eb25400230f60cd92d2bc600dda899fa8460b72e3e22f54c9207"
    ),
    "src/signal_generator/pipeline/trade_plan.py": (
        "e8faaf2909e3d2f726ea344c7e535f9e9a8b1b406fdc262f91591d3faa9b5642"
    ),
    "src/signal_generator/pipeline/mapper.py": (
        "6bf812b467f11f4c3b6e51f8d07323cbe5ab9bdf84af79e10a9dabe856171962"
    ),
}

SIDE_EFFECT_FLAGS = {
    "writes_to_clickhouse": False,
    "writes_to_signals": False,
    "writes_to_processing_state": False,
    "cleanup_enabled": False,
    "publish_enabled": False,
    "live_orders_enabled": False,
    "pool_v1_enabled": False,
    "outcome_evaluation_enabled": False,
    "execution_dedup_applied": False,
}


def jobs_root(environ: dict | None = None) -> Path:
    env = environ if environ is not None else os.environ
    override = str(env.get("STOCH_FADE_RESEARCH_JOBS_ROOT") or "").strip()
    if override:
        return Path(override)
    return DEFAULT_JOBS_ROOT


def universe_path(environ: dict | None = None) -> Path:
    env = environ if environ is not None else os.environ
    override = str(env.get("STOCH_UNIVERSE_51_PATH") or "").strip()
    if override:
        return Path(override)
    return DEFAULT_UNIVERSE


def sg_python(environ: dict | None = None) -> Path:
    env = environ if environ is not None else os.environ
    override = str(env.get("STOCH_FADE_SG_PYTHON") or "").strip()
    if override:
        return Path(override)
    return DEFAULT_SG_PYTHON


def coin_timeout_s(environ: dict | None = None) -> int:
    env = environ if environ is not None else os.environ
    raw = str(env.get("STOCH_FADE_COIN_TIMEOUT_S") or DEFAULT_COIN_TIMEOUT_S).strip()
    try:
        return max(5, int(raw))
    except ValueError:
        return DEFAULT_COIN_TIMEOUT_S


def ezm_coin_timeout_s(environ: dict | None = None) -> int:
    env = environ if environ is not None else os.environ
    raw = str(env.get("STOCH_EZM_COIN_TIMEOUT_S") or DEFAULT_EZM_COIN_TIMEOUT_S).strip()
    try:
        return max(30, int(raw))
    except ValueError:
        return DEFAULT_EZM_COIN_TIMEOUT_S


def oa_root(environ: dict | None = None) -> Path:
    env = environ if environ is not None else os.environ
    override = str(env.get("ORDERBOOK_ANALYSE_ROOT") or "").strip()
    if override:
        return Path(override)
    return DEFAULT_OA_ROOT


def oa_raw_root(environ: dict | None = None) -> Path:
    env = environ if environ is not None else os.environ
    override = str(env.get("STOCH_EZM_RAW_ROOT") or "").strip()
    if override:
        return Path(override)
    return oa_root(environ) / "data/orderbook_raw_shadow/ob200_v3"


def normalize_ezm_computation_mode(mode: str | None) -> str:
    """Normalize EZM job computation mode (before job start)."""
    text = str(mode or "").strip().lower()
    if text in ("", EZM_COMPUTATION_MODE_EMA_PLUS_MICRO, "ema_plus_micro", "full"):
        return EZM_COMPUTATION_MODE_EMA_PLUS_MICRO
    if text == EZM_COMPUTATION_MODE_EMA_ONLY:
        return EZM_COMPUTATION_MODE_EMA_ONLY
    raise ValueError("INVALID_COMPUTATION_MODE")
