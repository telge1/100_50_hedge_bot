"""Frozen fade research job paths. No strategy formulas. No CH writers."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

DASHBOARD_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = DASHBOARD_ROOT.parent
DEFAULT_SG_ROOT = Path(
    "/home/telgenbuescher/projects/Signal_Generator_Ralf/signal_generator_stoch_waves"
)
DEFAULT_SG_PYTHON = DEFAULT_SG_ROOT / ".venv" / "bin" / "python"
DEFAULT_JOBS_ROOT = REPO_ROOT / "results" / "stoch_fade_research_jobs"
DEFAULT_UNIVERSE = DEFAULT_SG_ROOT / "config" / "universe_tradeable_51.json"
WORKER_SCRIPT = Path(__file__).resolve().parent / "worker.py"

STRATEGY_VERSION = "wave_fade_frozen_f16ae32"
SOURCE_COMMIT = "f16ae32da38da86f39e75b09c63c31f62d11996b"
EDGES_VERSION = "apt_is_q4_frozen_20260808"
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
    override = str(env.get("STOCH_FADE_SG_PYTHON") or env.get("STOCH_UNIVERSE_51_SG_PYTHON") or "").strip()
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
