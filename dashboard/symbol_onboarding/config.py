"""Paths and safety constants for the symbol-onboarding dashboard adapter."""

from __future__ import annotations

import os
from pathlib import Path

DASHBOARD_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = DASHBOARD_ROOT.parent
OA_ROOT_DEFAULT = Path("/home/telgenbuescher/projects/orderbook_analyse")

ALLOWED_UPDATE_ORIGINS = (
    "http://dash.immotel.de:8080",
    "http://dash.immotel.de",
    "https://dash.immotel.de",
    "http://127.0.0.1:3000",
    "http://localhost:3000",
    "http://127.0.0.1:8080",
    "http://localhost:8080",
    "http://127.0.0.1:3010",
    "http://localhost:3010",
    "http://127.0.0.1:3099",
    "http://localhost:3099",
)

ALLOWED_HOSTS = {"dash.immotel.de", "127.0.0.1", "localhost"}

# UI / API policy (backend MAX is 800; keep dashboard conservative)
DASHBOARD_MAX_HISTORY_DAYS = 90
DASHBOARD_DEFAULT_HISTORY_DAYS = 30
ALLOWED_PRESET_DAYS = frozenset({7, 30, 90})

MAX_REQUEST_BYTES = 16_384
RATE_LIMIT_WINDOW_SEC = 60.0
RATE_LIMIT_MAX_POSTS = 20

PLAN_TTL_SECONDS = 30 * 60
PLAN_CACHE_DIRNAME = "plans"
QUEUE_DIRNAME = "queue"

FORBIDDEN_REQUEST_KEYS = frozenset(
    {
        "full_ob",
        "fullob",
        "orderbook_full",
        "orderbook.full",
        "shell",
        "cmd",
        "command",
        "argv",
        "subprocess",
        "systemctl",
        "path",
        "file",
        "script",
        "clickhouse_table",
        "table",
        "service",
        "unit",
    }
)

STAGE_LABELS_DE = {
    "validate_bybit": "Bybit prüfen",
    "resolve_instrument_metadata": "Instrumentdaten laden",
    "register_tick": "Tick registrieren",
    "register_universe": "Universe registrieren",
    "register_ob1000": "OB1000 registrieren",
    "backfill_candles": "Candles laden",
    "backfill_oi": "OI 5m laden",
    "backfill_public_trades": "Public Trades laden",
    "activate_raw_collector": "Raw-Collector aktivieren",
    "observe_raw_snapshot": "ersten OB1000-Snapshot prüfen",
    "activate_materializer": "Materializer prüfen",
    "observe_clickhouse": "ClickHouse-Daten prüfen",
    "verify_existing_symbols": "bestehende Symbole kontrollieren",
}

OVERVIEW_CACHE_TTL_SEC = 30.0


def oa_root(environ: dict | None = None) -> Path:
    env = environ if environ is not None else os.environ
    override = str(env.get("SYMBOL_ONBOARDING_OA_ROOT") or "").strip()
    if override:
        return Path(override)
    return OA_ROOT_DEFAULT


def jobs_dir(environ: dict | None = None) -> Path:
    env = environ if environ is not None else os.environ
    override = str(env.get("SYMBOL_ONBOARDING_JOBS_DIR") or "").strip()
    if override:
        return Path(override)
    return oa_root(environ) / "results" / "add_symbol" / "jobs"


def plans_dir(environ: dict | None = None) -> Path:
    env = environ if environ is not None else os.environ
    override = str(env.get("SYMBOL_ONBOARDING_PLANS_DIR") or "").strip()
    if override:
        return Path(override)
    return oa_root(environ) / "results" / "add_symbol" / PLAN_CACHE_DIRNAME


def queue_dir(environ: dict | None = None) -> Path:
    env = environ if environ is not None else os.environ
    override = str(env.get("SYMBOL_ONBOARDING_QUEUE_DIR") or "").strip()
    if override:
        return Path(override)
    return oa_root(environ) / "results" / "add_symbol" / QUEUE_DIRNAME


def dash_python(environ: dict | None = None) -> Path:
    env = environ if environ is not None else os.environ
    override = str(env.get("SYMBOL_ONBOARDING_DASH_PYTHON") or "").strip()
    if override:
        return Path(override)
    return Path(os.environ.get("VIRTUAL_ENV") or REPO_ROOT / ".venv") / "bin" / "python"
