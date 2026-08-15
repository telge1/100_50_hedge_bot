"""Paths and window constants. Reuses research ClickHouse env, no new credentials."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

DASHBOARD_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = DASHBOARD_ROOT.parent
DEFAULT_SIGNAL_GENERATOR_ROOT = Path(
    "/home/telgenbuescher/projects/Signal_Generator_Ralf/signal_generator_stoch_waves"
)
DEFAULT_UNIVERSE_PATH = DEFAULT_SIGNAL_GENERATOR_ROOT / "config" / "universe_tradeable_51.json"
DEFAULT_JOBS_ROOT = REPO_ROOT / "results" / "stoch_universe_update_jobs"
DEFAULT_SG_PYTHON = DEFAULT_SIGNAL_GENERATOR_ROOT / ".venv" / "bin" / "python"
BACKFILL_SCRIPT = DEFAULT_SIGNAL_GENERATOR_ROOT / "scripts" / "backfill_bybit_universe.py"
WORKER_SCRIPT = Path(__file__).resolve().parent / "update_worker.py"
ALLOWED_UPDATE_ORIGINS = (
    "http://dash.immotel.de:8080",
    "http://dash.immotel.de",
    "https://dash.immotel.de",
    "http://127.0.0.1:3000",
    "http://localhost:3000",
    "http://127.0.0.1:8080",
    "http://localhost:8080",
)
REQUESTED_FROM = datetime(2025, 12, 11, 0, 0, 0, tzinfo=timezone.utc)
CACHE_TTL_SECONDS = 60
FRESHNESS_GRACE_MINUTES = 10
EXCHANGE = "bybit"
INTERVAL = "1m"


def universe_path(environ: dict | None = None) -> Path:
    env = environ if environ is not None else os.environ
    override = str(env.get("STOCH_UNIVERSE_51_PATH") or "").strip()
    if override:
        return Path(override)
    sg = str(env.get("POOL_ORDER_SIGNAL_GENERATOR_ROOT") or "").strip()
    if sg:
        return Path(sg) / "config" / "universe_tradeable_51.json"
    return DEFAULT_UNIVERSE_PATH


def signal_generator_root(environ: dict | None = None) -> Path:
    env = environ if environ is not None else os.environ
    sg = str(env.get("POOL_ORDER_SIGNAL_GENERATOR_ROOT") or "").strip()
    if sg:
        return Path(sg)
    return DEFAULT_SIGNAL_GENERATOR_ROOT


def jobs_root(environ: dict | None = None) -> Path:
    env = environ if environ is not None else os.environ
    override = str(env.get("STOCH_UNIVERSE_51_JOBS_ROOT") or "").strip()
    if override:
        return Path(override)
    return DEFAULT_JOBS_ROOT


def sg_python(environ: dict | None = None) -> Path:
    env = environ if environ is not None else os.environ
    override = str(env.get("STOCH_UNIVERSE_51_SG_PYTHON") or "").strip()
    if override:
        return Path(override)
    root = signal_generator_root(environ)
    return root / ".venv" / "bin" / "python"


def backfill_script(environ: dict | None = None) -> Path:
    return signal_generator_root(environ) / "scripts" / "backfill_bybit_universe.py"


def freshness_grace_minutes(environ: dict | None = None) -> int:
    """CURRENT if lag_minutes <= this value. Clock can advance during backfill/reload."""
    env = environ if environ is not None else os.environ
    raw = str(env.get("STOCH_UNIVERSE_51_FRESHNESS_GRACE_MINUTES") or FRESHNESS_GRACE_MINUTES).strip()
    try:
        return max(0, int(raw))
    except ValueError:
        return int(FRESHNESS_GRACE_MINUTES)


def cache_ttl_seconds(environ: dict | None = None) -> float:
    env = environ if environ is not None else os.environ
    raw = str(env.get("STOCH_UNIVERSE_51_CACHE_TTL") or CACHE_TTL_SECONDS).strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        return float(CACHE_TTL_SECONDS)
